"""Vadeli işlem scalping (STRATEGY=futures): strateji, ayarlar, CCXT emir akışı, FuturesBot, Telegram ve backtest."""
from collections import Counter
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
import futures_data
from commands import CALLBACK, Command
from config import ConfigError, futures_liq_distance_pct, strategy_family, with_strategy
from futures_exchange import FuturesError, FuturesExchange, MinNotionalError, PaperFuturesExchange, ccxt_symbol
from futures_trader import FuturesBot
from indicators import ema, rsi
from runtime_settings import apply_settings
from state import BotState, StateStore, TradeLog
from strategy import (FUT_BAR_MS, FuturesScalpStrategy, FuturesSignal, make_strategy, np_ema, np_prev_extreme,
                      np_prev_mean, np_rsi)
from test_bot_flow import CaptureNotifier

HOUR = 3_600_000
BASE_MS = 1_780_000_000_000 // FUT_BAR_MS * FUT_BAR_MS


# ====================================================================== sahte CCXT (Binance USDⓈ-M)
class FakeCcxt:
    """ccxt.binance'in botun kullandığı kısmı: çağrıları sırasıyla kaydeder."""

    def __init__(self, prices=None, free=30.0, hedged=False, min_cost=None):
        self.prices = dict(prices or {"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        self.free = free
        self.hedged = hedged
        self.calls = []
        self.orders = {}
        self.positions = {}
        self.fail_stop = False
        self.margin_error = None
        self.min_cost = min_cost or {}
        self._id = 0

    # --- piyasa
    def market(self, sym):
        if sym not in {ccxt_symbol(s) for s in self.prices}:
            raise KeyError(sym)
        base = sym.split("/")[0]
        return {"id": base + "USDT", "symbol": sym, "swap": True, "linear": True, "quote": "USDT", "active": True,
                "limits": {"cost": {"min": self.min_cost.get(base, 5.0)}, "amount": {"min": 0.001}}}

    def load_markets(self, reload=False):
        self.calls.append(("load_markets",))
        return {ccxt_symbol(s): self.market(ccxt_symbol(s)) for s in self.prices}

    @staticmethod
    def amount_to_precision(sym, amount):
        return f"{int(float(amount) * 1000) / 1000:.3f}"

    @staticmethod
    def price_to_precision(sym, price):
        return f"{round(float(price), 2):.2f}"

    def fetch_ticker(self, sym):
        return {"last": self.prices[sym.split("/")[0] + "USDT"]}

    def fetch_order_book(self, sym, limit=5):
        p = self.prices[sym.split("/")[0] + "USDT"]
        return {"bids": [[p, 1]], "asks": [[p, 1]]}

    def fetch_time(self):
        return 0

    def fapiPublicGetKlines(self, params):
        return []

    # --- hesap
    def fetch_position_mode(self):
        self.calls.append(("fetch_position_mode",))
        return {"hedged": self.hedged}

    def set_position_mode(self, hedged):
        self.calls.append(("set_position_mode", hedged))
        self.hedged = hedged

    def set_margin_mode(self, mode, sym):
        self.calls.append(("set_margin_mode", mode, sym))
        if self.margin_error:
            raise Exception(self.margin_error)

    def set_leverage(self, lev, sym):
        self.calls.append(("set_leverage", lev, sym))

    def fetch_balance(self):
        self.calls.append(("fetch_balance",))
        return {"free": {"USDT": self.free}, "total": {"USDT": self.free}}

    # --- emirler
    def create_order(self, sym, type_, side, amount, price=None, params=None):
        params = params or {}
        self.calls.append(("create_order", sym, type_, side, amount, price, dict(params)))
        if type_ == "STOP_MARKET" and self.fail_stop:
            raise Exception("binance {\"code\":-4120,\"msg\":\"Order type not supported\"}")
        self._id += 1
        oid = str(self._id)
        symbol = sym.split("/")[0] + "USDT"
        if type_ == "market":
            amt = float(amount)
            p = self.prices[symbol]
            if params.get("reduceOnly"):
                self.positions.pop(symbol, None)
            else:
                self.positions[symbol] = {"side": "long" if side == "buy" else "short", "contracts": amt,
                                          "entryPrice": p}
            return {"id": oid, "average": p, "filled": amt, "fee": {"cost": amt * p * 0.0005}}
        self.orders[oid] = {"type": type_, "status": "open", "params": dict(params), "price": price,
                            "amount": amount}
        return {"id": oid}

    def cancel_order(self, oid, sym, params=None):
        self.calls.append(("cancel_order", oid, dict(params or {})))
        self.orders.pop(oid, None)

    def cancel_all_orders(self, sym, params=None):
        self.calls.append(("cancel_all_orders", sym, dict(params or {})))

    def fetch_order(self, oid, sym, params=None):
        o = self.orders.get(oid, {"status": "canceled"})
        return {"status": o["status"], "filled": o.get("filled", 0), "average": o.get("average")}

    def fetch_positions(self, symbols=None):
        out = []
        for symbol, p in self.positions.items():
            out.append({"symbol": ccxt_symbol(symbol), "contracts": p["contracts"], "side": p["side"],
                        "entryPrice": p["entryPrice"], "leverage": 10, "liquidationPrice": 0, "unrealizedPnl": 0,
                        "markPrice": self.prices[symbol], "info": {"symbol": symbol}})
        return out

    def fetch_my_trades(self, sym, since=None):
        return []

    def fetch_funding_history(self, sym, since=None):
        return []


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def fcfg(cfg):
    new, _ = with_strategy(replace(cfg, start_capital=30.0), "futures")
    new.validate()
    return new


def make_fbot(fcfg, tmp_path, live=False, fake=None, clock=None):
    fake = fake or FakeCcxt()
    clock = clock or Clock(BASE_MS / 1000 + 3)          # 5 dk mum kapanışından 3 sn sonra (canlıdaki tarama anı)
    state = BotState.new("live" if live else "paper", 30.0)
    cfg = replace(fcfg, trading_mode="live" if live else "paper")
    ex = (FuturesExchange(cfg, exchange=fake, time_fn=clock) if live
          else PaperFuturesExchange(cfg, state.paper_balances, exchange=fake, time_fn=clock))
    notes = CaptureNotifier()
    bot = FuturesBot(cfg, ex, make_strategy(None, cfg), notes, StateStore(tmp_path / "s.json"), state,
                     TradeLog(tmp_path / "t.csv"), time_fn=clock)
    bot.startup()
    bot.running = True
    return bot, fake, notes, clock


def signal(symbol="BTCUSDT", side="long"):
    return FuturesSignal(symbol, side, 100.0, BASE_MS, 5.0, {"liq_ratio": 5.0, "vol_ratio": 4.0, "rsi_prev": 20.0,
                                                             "rsi_now": 30.0, "btc_trend": 1.0})


# ====================================================================== göstergeler ve sinyal
def test_numpy_indicators_match_pure_python():
    rng = np.random.default_rng(1)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 300)))
    for a, b in zip(np_rsi(closes, 14), rsi(closes, 14)):
        assert (b is None and np.isnan(a)) or a == pytest.approx(b, abs=1e-9)
    for a, b in zip(np_ema(closes, 20), ema(closes, 20)):
        assert (b is None and np.isnan(a)) or a == pytest.approx(b, abs=1e-9)
    x = np.arange(10, dtype=float)
    assert np.isnan(np_prev_mean(x, 3)[2]) and np_prev_mean(x, 3)[3] == pytest.approx(1.0)
    assert np_prev_extreme(x, 3, np.min)[5] == 2.0 and np_prev_extreme(x, 3, np.max)[5] == 4.0


def flush_series(mirror=False):
    """Pozitif uyumsuzluklu likidasyon dalgası: sert düşüşle dip (bar 70), toparlanma, yavaş geri çekilme ve son
    mumda (bar 99) önceki dibin altına inen hacimli zorla satış fitili (kapanış önceki dibin kapanışının üstünde)."""
    n = 100
    c = np.full(n, 100.0)
    c[:60] += np.where(np.arange(60) % 2, 0.05, -0.05)
    c[60:71] = np.linspace(100.0, 95.0, 11)
    c[71:86] = np.linspace(95.2, 98.0, 15)
    c[86:99] = np.linspace(97.85, 96.05, 13)
    c[99] = 95.8
    o = np.concatenate(([100.0], c[:-1]))
    h = np.maximum(o, c) + 0.1
    l = np.minimum(o, c) - 0.1
    l[70] = 94.5
    o[99], h[99], l[99] = 96.0, 96.1, 94.0
    v = np.full(n, 10.0)
    tb = np.full(n, 5.0)
    v[99], tb[99] = 60.0, 5.0
    t = BASE_MS + np.arange(n, dtype=np.int64) * FUT_BAR_MS
    if mirror:   # Tepe tarafı (short): fiyatı ters çevir, alış ve satış hacmini değiştir
        o, h, l, c, tb = 200 - o, 200 - l, 200 - h, 200 - c, v - tb
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "tb": tb, "qv": v * c, "n": v}


def btc_hourly(up=True, end_ms=None):
    n = 120
    closes = np.linspace(90, 110, n) if up else np.linspace(110, 90, n)
    t = end_ms - (n - np.arange(n, dtype=np.int64)) * HOUR
    return {"t": t, "o": closes, "h": closes + 0.5, "l": closes - 0.5, "c": closes, "v": np.ones(n),
            "tb": np.ones(n), "qv": np.ones(n), "n": np.ones(n)}


def test_long_signal_needs_all_four_conditions(fcfg):
    k = flush_series()
    close_ms = int(k["t"][-1]) + FUT_BAR_MS
    strat = FuturesScalpStrategy(None, fcfg)
    sigs, rejects = strat.evaluate({"XRPUSDT": k}, btc_hourly(True, close_ms), close_ms + 3000)
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].symbol == "XRPUSDT"
    m = sigs[0].metrics
    assert m["liq_ratio"] >= fcfg.futures_liq_ratio and m["vol_ratio"] >= fcfg.futures_volume_ratio
    assert m["rsi_now"] >= m["rsi_prev"] + fcfg.futures_div_rise and m["rsi_prev"] <= fcfg.futures_div_oversold
    assert "LONG" not in sigs[0].note() and "uyumsuzluk" in sigs[0].note()
    # BTC 1 saatlik trend düşüşteyken long açılmaz
    sigs, rejects = strat.evaluate({"XRPUSDT": k}, btc_hourly(False, close_ms), close_ms + 3000)
    assert not sigs and rejects["BTC trendi ters"] == 1
    # Hacim onayı yoksa (2.5x altı) sinyal yok
    weak = dict(k, v=k["v"].copy(), tb=k["tb"].copy())
    weak["v"][99], weak["tb"][99] = 20.0, 0.0
    sigs, rejects = strat.evaluate({"XRPUSDT": weak}, btc_hourly(True, close_ms), close_ms + 3000)
    assert not sigs and (rejects["hacim düşük"] == 1 or rejects["likidasyon dalgası yok"] == 1)
    # Eski mum (bot geç kaldı) değerlendirilmez
    sigs, rejects = strat.evaluate({"XRPUSDT": k}, btc_hourly(True, close_ms), close_ms + 3 * FUT_BAR_MS)
    assert not sigs and rejects["mum eski"] == 1


def test_short_signal_mirror_and_side_setting(fcfg):
    k = flush_series(mirror=True)
    close_ms = int(k["t"][-1]) + FUT_BAR_MS
    sigs, _ = FuturesScalpStrategy(None, fcfg).evaluate({"ETHUSDT": k}, btc_hourly(False, close_ms), close_ms + 1000)
    assert len(sigs) == 1 and sigs[0].side == "short"
    only_long = replace(fcfg, futures_sides="long")
    sigs, rejects = FuturesScalpStrategy(None, only_long).evaluate({"ETHUSDT": k}, btc_hourly(False, close_ms),
                                                                    close_ms + 1000)
    assert not sigs and rejects["yön kapalı"] == 1


def test_liquidation_feed_source(fcfg):
    k = flush_series()
    close_ms = int(k["t"][-1]) + FUT_BAR_MS

    class Feed:
        def __init__(self, long_usdt):
            self.long_usdt = long_usdt

        def recent(self, symbol, seconds):
            return self.long_usdt, 0.0

    cfg = replace(fcfg, futures_liq_source="both", futures_liq_min_usdt=50_000)
    strat = FuturesScalpStrategy(None, cfg)
    assert strat.evaluate({"BTCUSDT": k}, btc_hourly(True, close_ms), close_ms, feed=Feed(80_000))[0]
    sigs, rejects = strat.evaluate({"BTCUSDT": k}, btc_hourly(True, close_ms), close_ms, feed=Feed(10_000))
    assert not sigs and rejects["likidasyon dalgası yok"] == 1
    sigs, rejects = strat.evaluate({"BTCUSDT": k}, btc_hourly(True, close_ms), close_ms, feed=None)
    assert not sigs and rejects["likidasyon akışı yok"] == 1


def test_backtest_and_live_use_same_signal(fcfg):
    """Backtest'in tüm geçmişe uyguladığı değerlendirme, canlının son mumda bulduğuyla aynı sinyali verir."""
    k = flush_series()
    close_ms = int(k["t"][-1]) + FUT_BAR_MS
    btc = btc_hourly(True, close_ms)
    nxt = {key: np.concatenate((val, val[-1:])) for key, val in k.items()}
    nxt["t"][-1] = close_ms
    data = fb.FutData(int(k["t"][0]), close_ms + FUT_BAR_MS, [fb.Segment(int(k["t"][0]), close_ms + 1, ("XRP",))],
                      {"XRPUSDT": nxt}, fb.btc_trend_1h(btc), (btc["t"], btc["c"]), {}, None, "5m")
    signals, funnel = fb.find_signals(fcfg, data)
    assert [(s.symbol, s.side, s.close_ms) for s in signals] == [("XRPUSDT", "long", close_ms)]
    assert funnel.signals == 1 and funnel.liq >= 1


# ====================================================================== ayarlar
def test_futures_profile_and_validation(fcfg, cfg):
    assert strategy_family("futures") == "futures"
    assert (fcfg.take_profit_pct, fcfg.stop_loss_pct, fcfg.breakeven_trigger_pct, fcfg.breakeven_floor_pct,
            fcfg.max_hold_minutes, fcfg.futures_leverage) == (1.2, 0.9, 0.5, 0.2, 60, 10)
    with pytest.raises(ConfigError):
        replace(fcfg, futures_leverage=25).validate()
    with pytest.raises(ConfigError):     # 20x'te likidasyon ~%4.5: zarar kes %4.5 çok yakın
        replace(fcfg, futures_leverage=20, stop_loss_pct=4.5).validate()
    replace(fcfg, futures_leverage=20, stop_loss_pct=1.0).validate()
    assert futures_liq_distance_pct(10, 0.5) == pytest.approx(9.5)
    new, _ = apply_settings(fcfg, {"FUTURES_LEVERAGE": "15"})
    assert new.futures_leverage == 15
    with pytest.raises(ConfigError):
        apply_settings(fcfg, {"FUTURES_LEVERAGE": "21"})


# ====================================================================== CCXT bağlantısı
def test_one_way_mode_check(fcfg):
    fake = FakeCcxt(hedged=False)
    assert "tek yönlü" in FuturesExchange(fcfg, exchange=fake).check_one_way()
    fake = FakeCcxt(hedged=True)
    note = FuturesExchange(fcfg, exchange=fake).check_one_way()
    assert ("set_position_mode", False) in fake.calls and "alındı" in note
    with pytest.raises(FuturesError):
        FuturesExchange(fcfg, exchange=FakeCcxt(hedged=True)).check_one_way(has_positions=True)


def test_prepare_symbol_isolated_then_leverage(fcfg):
    fake = FakeCcxt()
    ex = FuturesExchange(fcfg, exchange=fake)
    ex.prepare_symbol("BTCUSDT", 10)
    assert fake.calls == [("set_margin_mode", "isolated", "BTC/USDT:USDT"), ("set_leverage", 10, "BTC/USDT:USDT")]
    fake.margin_error = 'binance {"code":-4046,"msg":"No need to change margin type."}'   # Zaten isolated
    ex.prepare_symbol("BTCUSDT", 5)
    assert fake.calls[-1] == ("set_leverage", 5, "BTC/USDT:USDT")


def test_min_notional_detection(fcfg):
    fake = FakeCcxt(min_cost={"BTC": 100.0})
    ex = FuturesExchange(fcfg, exchange=fake)
    ex.load_markets()
    assert ex.amount_to_precision("BTCUSDT", 0.12345) == 0.123     # CCXT amount_to_precision (aşağı)
    with pytest.raises(MinNotionalError):
        ex.check_order_size("BTCUSDT", 0.5, 100.0)                  # 50 USDT < 100 USDT
    ex.check_order_size("ETHUSDT", 0.5, 100.0)

    def reject(*a, **k):
        raise Exception('binance {"code":-4164,"msg":"Order\'s notional must be no smaller than 5"}')
    fake.create_order = reject
    with pytest.raises(MinNotionalError):
        ex.market_order("ETHUSDT", "buy", 0.01)


# ====================================================================== FuturesBot
def test_open_position_order_sequence_live(fcfg, tmp_path):
    bot, fake, notes, _ = make_fbot(fcfg, tmp_path, live=True)
    assert any("Vadeli Scalping Botu" in m for m in notes.messages)
    fake.calls.clear()
    pos = bot.open_futures(signal(), bot.time_fn())
    names = [c[0] for c in fake.calls]
    # İşleme girmeden HEMEN ÖNCE isolated + kaldıraç, sonra bakiye, piyasa emri, Binance'te stop ve hedef
    assert names[:3] == ["set_margin_mode", "set_leverage", "fetch_balance"]
    orders = [c for c in fake.calls if c[0] == "create_order"]
    assert [o[2] for o in orders] == ["market", "STOP_MARKET", "limit"]
    market, stop, tp = orders
    assert market[4] == pytest.approx(int(30 * 0.95 * 10 / 100 * 1000) / 1000)   # tüm serbest bakiye × kaldıraç
    assert stop[4] is None and stop[6]["closePosition"] is True and stop[6]["stopPrice"] == pytest.approx(99.1)
    assert tp[3] == "sell" and tp[6]["reduceOnly"] is True and tp[5] == pytest.approx(101.2)
    assert pos.futures and pos.side == "long" and pos.leverage == 10 and pos.regime == "LONG 10X"
    assert any("[LONG 10X]" in m and "POZİSYON AÇILDI" in m for m in notes.messages)


def test_min_notional_skips_trade_and_notifies(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(fcfg, tmp_path, live=True, fake=FakeCcxt(free=0.3))
    close_ms = int(clock.t * 1000) // FUT_BAR_MS * FUT_BAR_MS

    def klines(symbol, interval, limit):
        t = np.array([close_ms - FUT_BAR_MS], dtype=np.int64)
        one = np.ones(1)
        return {"t": t, "o": one, "h": one, "l": one, "c": one, "v": one, "tb": one, "qv": one, "n": one}
    bot.exchange.klines = klines
    bot.strategy.evaluate = lambda *a, **k: ([signal()], Counter())
    bot.scan_and_enter(clock.t)                      # Çökmeden pas geçer
    assert not bot.state.positions
    assert any("pas geçildi" in m and "en küçük emir tutarı" in m for m in notes.messages)
    assert not any(c[0] == "create_order" for c in fake.calls)


def test_failed_exchange_stop_closes_position(fcfg, tmp_path):
    fake = FakeCcxt()
    bot, fake, notes, _ = make_fbot(fcfg, tmp_path, live=True, fake=fake)
    fake.fail_stop = True
    assert bot.open_futures(signal(), bot.time_fn()) is None
    assert not bot.state.positions and bot.state.trades == 1
    assert any("GÜVENLİK KAPATMASI" in m for m in notes.messages)


def test_breakeven_shield_moves_stop(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(fcfg, tmp_path, live=True)
    pos = bot.open_futures(signal(), clock.t)
    old_stop = pos.stop_order_id
    fake.prices["BTCUSDT"] = 100.6                       # +%0.6 > +%0.5 tetik
    bot.manage_positions(clock.t + 60)
    assert pos.breakeven_active and pos.sl_price == pytest.approx(100.2)
    assert ("cancel_order", old_stop, {"trigger": True}) in fake.calls
    new = [c for c in fake.calls if c[0] == "create_order" and c[2] == "STOP_MARKET"][-1]
    assert new[6]["stopPrice"] == pytest.approx(100.2)
    assert any("başabaş kalkanı devrede" in m for m in notes.messages)


def test_time_stop_and_exchange_tp_live(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(fcfg, tmp_path, live=True)
    bot.open_futures(signal(), clock.t)
    bot.manage_positions(clock.t + 59 * 60)
    assert bot.state.positions
    bot.manage_positions(clock.t + 61 * 60)            # 1 saat doldu: piyasa emriyle kapatılır
    assert not bot.state.positions and bot.state.trades == 1
    assert any("ZAMAN STOPU" in m for m in notes.messages)
    # Kâr Al Binance'te doldu (pozisyon kayboldu): bot kapanışı kaydeder
    pos = bot.open_futures(signal(), clock.t + 3600 * 2)
    fake.orders[pos.tp_order_id].update(status="closed", filled=pos.qty, average=101.2)
    fake.positions.clear()
    bot.manage_positions(clock.t + 3600 * 2 + 30)
    assert not bot.state.positions and bot.state.trades == 2
    assert any("KÂR AL" in m for m in notes.messages)


def test_paper_stop_loss_accounting(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(fcfg, tmp_path, live=False)
    pos = bot.open_futures(signal("ETHUSDT", "short"), clock.t)
    assert pos.side == "short" and pos.sl_price == pytest.approx(pos.entry_price * 1.009, abs=0.01)
    fake.prices["ETHUSDT"] = 101.0                       # Short'ta fiyat yükseldi: stop
    bot.manage_positions(clock.t + 120)
    assert not bot.state.positions
    pnl = bot.state.realized_pnl
    assert -4.0 < pnl < -2.5                              # ~%1 fiyat × 10x × 28.5 USDT teminat + komisyon
    assert bot.exchange.total_balance() == pytest.approx(30.0 + pnl, abs=1e-6)
    assert any("ZARAR KES" in m and "[SHORT 10X]" in m for m in notes.messages)


def test_board_status_and_position_show_tag_and_roe(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(fcfg, tmp_path, live=False)
    bot.open_futures(signal(), clock.t)
    fake.prices["BTCUSDT"] = 100.3
    bot.manage_positions(clock.t + 10)
    board = bot.smart.board_text(clock.t + 10)
    pos = bot.state.positions["BTCUSDT"]
    roe = (100.3 / pos.entry_price - 1) * 100 * 10        # Binance'teki gibi: fiyat hareketi × kaldıraç
    assert "[LONG 10X]" in board and f"ROE <b>+{roe:.2f}%" in board
    bot.commands.handle(Command("pozisyon", []))
    assert "[LONG 10X]" in notes.messages[-1] and "Likidasyon" in notes.messages[-1]
    bot.send_status(silent=True)
    assert "[LONG 10X] ROE" in notes.messages[-1]


def test_leverage_buttons_in_dynamic_settings(fcfg, tmp_path):
    bot, fake, notes, _ = make_fbot(fcfg, tmp_path, live=False)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    bot.commands.handle(Command(CALLBACK, ["cfg"], kind="callback", message_id=5))
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert "Kaldıraç" in text and "lev:15" in data and "adj:LEV:+" in data
    bot.commands.handle(Command(CALLBACK, ["lev", "15"], kind="callback", message_id=5))
    assert bot.cfg.futures_leverage == 15 and "FUTURES_LEVERAGE=15" in env.read_text(encoding="utf-8")
    bot.commands.handle(Command(CALLBACK, ["adj", "LEV", "+"], kind="callback", message_id=5))
    assert bot.cfg.futures_leverage == 16
    bot.commands.handle(Command(CALLBACK, ["adj", "TP", "+"], kind="callback", message_id=5))
    assert bot.cfg.take_profit_pct == pytest.approx(1.3)   # Vadeli: 0.1'lik adım


# ====================================================================== backtest
def params(**over):
    base = dict(leverage=10, tp_pct=1.2, sl_pct=0.9, be_enabled=True, be_trigger_pct=0.5, be_floor_pct=0.2,
                hold_min=60, margin_usage_pct=100.0, taker_fee=0.0, maker_fee=0.0, slippage_pct=0.0, tp_limit=True,
                mmr_pct=0.5, start_capital=100.0, kill_pct=0.0)
    base.update(over)
    return fb.SimParams(**base)


def window(rows, bar_ms=fb.MIN_MS, start=BASE_MS):
    t = start + np.arange(len(rows), dtype=np.int64) * bar_ms
    a = np.array(rows, dtype=float)
    return bar_ms, t, a[:, 0], a[:, 1], a[:, 2], a[:, 3]


def sig(side="long", close_ms=BASE_MS, symbol="BTCUSDT"):
    return fb.FutSignal(close_ms, symbol, side, 1.0, 100.0)


@pytest.mark.parametrize("rows, reason, exit_price", [
    ([(100, 100.3, 99.9, 100.2), (100.3, 101.3, 100.25, 101.0)], "TP", 101.2),
    ([(100, 100.1, 99.5, 99.6), (99.6, 99.7, 99.0, 99.1)], "SL", 99.1),
    ([(100, 100.6, 100.3, 100.4), (100.4, 100.45, 100.1, 100.15)], "BE", 100.2),
    ([(100, 100.1, 99.5, 99.6), (98.5, 98.6, 98.0, 98.2)], "SL", 98.5),          # Stopun altında açıldı
    ([(100, 100.1, 99.5, 99.6), (90.0, 90.1, 89.0, 89.5)], "LIQ", 90.0),          # Likidasyon fiyatının ötesinde
])
def test_run_trade_exits(rows, reason, exit_price):
    tr = fb.run_trade(params(), sig(), window(rows), margin=10.0, notional=100.0)
    assert tr.reason == reason and tr.exit == pytest.approx(exit_price)
    if reason == "LIQ":
        assert tr.liquidated and tr.pnl == pytest.approx(-10.0)
    if reason == "TP":
        assert tr.roe_pct == pytest.approx(12.0)


def test_run_trade_time_stop_and_short():
    flat = [(100, 100.1, 99.95, 100.0)] * 70
    tr = fb.run_trade(params(), sig(), window(flat), margin=10.0, notional=100.0)
    assert tr.reason == "TIME" and tr.exit_ms == BASE_MS + 60 * fb.MIN_MS
    tr = fb.run_trade(params(), sig("short"), window([(100, 100.2, 98.7, 98.9)]), margin=10.0, notional=100.0)
    assert tr.reason == "TP" and tr.exit == pytest.approx(98.8) and tr.pnl > 0


def test_simulate_single_position_compounding_and_ruin():
    data = fb.FutData(BASE_MS, BASE_MS + 10 * HOUR, [], {}, None, (np.zeros(0), np.zeros(0)), {}, None, "1m")
    s1, s2, s3 = sig(close_ms=BASE_MS), sig(close_ms=BASE_MS + 5 * fb.MIN_MS), sig(close_ms=BASE_MS + 2 * HOUR)
    win_tp = window([(100, 101.3, 99.9, 101.0)])
    windows = {("BTCUSDT", s1.close_ms): window([(100, 100.1, 99.95, 100.0)] * 70, start=s1.close_ms),
               ("BTCUSDT", s2.close_ms): win_tp, ("BTCUSDT", s3.close_ms): window([(100, 101.3, 99.9, 101.0)],
                                                                                    start=s3.close_ms)}
    res = fb.simulate(params(), data, [s1, s2, s3], windows)
    assert len(res.trades) == 2 and res.skipped["açık pozisyon vardı"] == 1
    assert res.trades[1].notional == pytest.approx(res.trades[0].equity_after * 10)   # bileşik: tüm bakiye
    x1, x3 = sig(close_ms=s1.close_ms, symbol="XRPUSDT"), sig(close_ms=s3.close_ms, symbol="XRPUSDT")
    losing = {("XRPUSDT", x.close_ms): window([(100, 100.1, 98.0, 98.5)], start=x.close_ms) for x in (x1, x3)}
    res = fb.simulate(params(start_capital=0.6), data, [x1, x3], losing)
    assert len(res.trades) == 2 and res.ruined_ms is not None     # Bakiye × kaldıraç en küçük emrin (5 USDT) altında
    res = fb.simulate(params(start_capital=1.0), data, [s1], windows)
    assert not res.trades and res.skipped["minimum emir tutarının altında"] == 1   # BTC: en az 100 USDT


def test_archive_csv_parse_and_resample():
    header = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,tbq,ignore\n"
    start = BASE_MS // HOUR * HOUR
    rows = "".join(f"{start + i * FUT_BAR_MS},{100 + i},{101 + i},{99 + i},{100.5 + i},10,0,1000,5,4,400,0\n"
                   for i in range(14))
    a = futures_data.parse_csv((header + rows).encode())
    b = futures_data.parse_csv(rows.encode())
    assert len(a["t"]) == len(b["t"]) == 14 and a["tb"][0] == 4 and a["c"][-1] == 113.5
    micro = futures_data.parse_csv(rows.replace(str(start), str(start * 1000), 1).encode())
    assert micro["t"][0] == start
    hourly = futures_data.resample(a, HOUR)
    assert len(hourly["t"]) == 1 and hourly["v"][0] == 120 and hourly["h"][0] == 112   # Eksik 2. saat atılır
