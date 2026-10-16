"""Haftalık momentum rotasyonu: ortak karar fonksiyonları, backtest (günlük mumlar) ve canlı bot akışı."""
from dataclasses import replace

import pytest

import backtest
from backtest import DAY_MS, HOUR_MS, entry_combos, prepare_backtest, simulate, sweep_combos
from config import ConfigError, _setting_notes, load_config, with_strategy
from exchange import PaperExchange
from risk import ExitReason, Position, breakeven_effective, evaluate_exit, update_trailing
from rotation import btc_trend, momentum_pct, plan_rotation, pump_pct
from state import BotState, StateStore, TradeLog
from strategy import RotationStrategy, make_strategy
from trader import TradingBot

from test_backtest import aggregate, no_pause  # noqa: F401 - istek beklemesi kapalı
from test_bot_flow import CaptureNotifier


@pytest.fixture
def rcfg(cfg):
    rot, applied = with_strategy(replace(cfg, start_capital=25.0), "rotation")
    assert applied
    return rot


# ---------------------------------------------------------------- ortak karar fonksiyonları
def test_momentum_and_btc_trend():
    assert momentum_pct([100, 110, 121], 2) == pytest.approx(21.0)
    assert momentum_pct([100, 110], 2) is None and momentum_pct([0, 1, 2], 2) is None
    assert btc_trend([10] * 49 + [20], 50) == (True, pytest.approx(10.2))
    assert btc_trend([20] * 49 + [10], 50)[0] is False
    assert btc_trend([1, 2], 50) == (None, None) and btc_trend([1], 0) == (True, None)
    # Canlı coin filtresinin pompa ölçüsü: son 3 günün zirvesi / önceki 3 günün dibi
    assert pump_pct([1, 1, 1, 1.2, 1.6, 1.4], [1, 0.9, 1, 1, 1, 1], 3) == pytest.approx((1.6 / 0.9 - 1) * 100)


def test_plan_keeps_strong_holdings_and_replaces_weak_ones():
    mom = {"A": 50.0, "B": 40.0, "C": 30.0, "D": 20.0, "E": 10.0, "F": -5.0}
    plan = plan_rotation(["D", "F"], mom, 2, True)
    assert plan.keep == ["D"]                      # 4. sırada: ilk 2×2 içinde, tutulur (gereksiz al-sat yok)
    assert plan.sell == ["F"]                      # Negatif momentum: satılır
    assert plan.candidates[: plan.wanted] == ["A"]
    plan = plan_rotation(["E"], mom, 2, True)
    assert plan.sell == ["E"] and plan.candidates[:plan.wanted] == ["A", "B"]   # 5. sıra: ilk 4'ün dışında
    plan = plan_rotation(["A", "B", "C"], mom, 2, True)                          # Fazlası en zayıftan satılır
    assert plan.keep == ["A", "B"] and plan.sell == ["C"] and plan.wanted == 0
    off = plan_rotation(["A", "B"], mom, 2, False)
    assert off.sell == ["A", "B"] and off.keep == [] and off.wanted == 2 and off.candidates == []
    unknown = plan_rotation([], mom, 2, None)                                    # Trend bilinmiyor: alım yapılır
    assert unknown.candidates[:2] == ["A", "B"]


# ---------------------------------------------------------------- ayarlar / risk
def test_rotation_config_profile_and_notes(cfg, tmp_path, monkeypatch):
    for key, value in {"STRATEGY": "rotation", "ROTATION_TOP_N": "2", "ROTATION_LOOKBACK_DAYS": "56",
                       "STOP_LOSS_PCT": "5", "BACKUP_STOP_PCT": "12", "TAKE_PROFIT_PCT": "15",
                       "TRAILING_ACTIVATION_PCT": "10", "TRAILING_FLOOR_PCT": "5"}.items():
        monkeypatch.setenv(key, value)
    c = load_config(env_file=str(tmp_path / "yok.env"), mode_override="paper")
    assert c.strategy == "rotation" and c.rotation_top_n == 2 and c.rotation_lookback_days == 56
    assert any("felaket korumasıdır" in n for n in c.notes)
    assert not any("Başabaş" in n for n in c.notes)           # Kâr al / başabaş uyarıları rotasyonda anlamsız
    monkeypatch.setenv("ROTATION_BTC_MA_DAYS", "5")
    with pytest.raises(ConfigError, match="ROTATION_BTC_MA_DAYS"):
        load_config(env_file=str(tmp_path / "yok.env"), mode_override="paper")
    monkeypatch.setenv("ROTATION_BTC_MA_DAYS", "50")
    monkeypatch.setenv("ROTATION_UNIVERSE", "1")
    with pytest.raises(ConfigError, match="ROTATION_UNIVERSE"):
        load_config(env_file=str(tmp_path / "yok.env"), mode_override="paper")

    rot, applied = with_strategy(cfg, "rotation")
    assert applied and (rot.stop_loss_pct, rot.backup_stop_pct) == (20.0, 25.0)
    swing, applied = with_strategy(rot, "swing")               # Rotasyon .env'inden swing'e: swing profili
    assert applied and swing.take_profit_pct == 12.0
    assert any("MAX_OPEN_POSITIONS" in n for n in _setting_notes(replace(rot, rotation_top_n=5)))


def test_rotation_exits_only_on_catastrophic_stop(rcfg):
    pos = Position("AUSDT", "A", 1.0, 1.0, 10.0, 0.0)
    for pct in (5.0, 40.0, 120.0, 3.0):
        assert not update_trailing(pos, pct, rcfg)
        assert evaluate_exit(pos, pct, 10 ** 9, replace(rcfg, max_hold_minutes=1)) is None
    assert not pos.trailing_active and not breakeven_effective(rcfg)
    assert evaluate_exit(pos, -rcfg.stop_loss_pct - 0.1, 0, rcfg) == ExitReason.STOP_LOSS


# ---------------------------------------------------------------- backtest (günlük mumlar)
END_TIME_MS = 1_700_000_000_000
END_MS = END_TIME_MS // DAY_MS * DAY_MS      # Günlük mumlarda dönem UTC gün başında biter (son mum kapanmış gün)
DAYS = 120
N = DAYS + 210


def day_rows(closes, start, vol=2e7):
    rows = []
    for i, c in enumerate(closes):
        t = start + i * DAY_MS
        o = closes[i - 1] if i else c
        rows.append([t, str(o), str(max(o, c) * 1.01), str(min(o, c) * 0.99), str(c), "0", t + DAY_MS - 1,
                     str(vol), 0, "0", str(vol / 2), "0"])
    return rows


def path(*legs, start=1.0):
    """[(gün sayısı, günlük çarpan), ...] → kapanış listesi."""
    out, p = [], start
    for n, f in legs:
        for _ in range(n):
            p *= f
            out.append(p)
    return out


class DailyClient:
    def __init__(self, fake_client, series, listed=None):
        class C(fake_client):
            def tickers_24h(inner):
                return [{"symbol": s, "quoteVolume": "50000000"} for s in inner.prices]

            def _request(inner, method, path, params=None, signed=False):
                if params["startTime"] == 0:
                    t = (listed or {}).get(params["symbol"], 1_500_000_000_000)
                    return [[t, "1", "1", "1", "1", "0", t + DAY_MS - 1, "0", 0, "0", "0", "0"]]
                assert params["interval"] == "1d", params
                rows = [r for r in series[params["symbol"]] if params["startTime"] <= r[0] <= params["endTime"]]
                return rows[: params["limit"]]
        self.client = C({s: 1.0 for s in series})


def _start_ms():
    return END_MS - N * DAY_MS


def _series(btc_crash_at=None):
    start = _start_ms()
    # Gün 0-209 ısınma; simülasyon 210'da başlar. LEAD önce güçlü, sonra çöker; NEXT sonradan yükselir.
    lead = path((260, 1.004), (30, 0.97), (40, 1.0))
    nxt = path((260, 1.0005), (30, 1.02), (40, 1.0))
    flat = path((330, 1.0))
    btc = path((330, 1.001), start=30_000.0) if btc_crash_at is None else \
        path((btc_crash_at, 1.001), (330 - btc_crash_at, 0.99), start=30_000.0)
    return {"LEADUSDT": day_rows(lead, start), "NEXTUSDT": day_rows(nxt, start), "FLATUSDT": day_rows(flat, start),
            "BTCUSDT": day_rows(btc, start, vol=5e9)}


def _prepare(cfg, client, tmp_path):
    return prepare_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                            symbols=["LEADUSDT", "NEXTUSDT", "FLATUSDT"], end_time_ms=END_TIME_MS)


def test_rotation_backtest_holds_leader_then_rotates(rcfg, fake_client, tmp_path):
    cfg = replace(rcfg, rotation_top_n=1, rotation_lookback_days=14, start_capital=11.0)
    data = _prepare(cfg, DailyClient(fake_client, _series()).client, tmp_path)
    assert data.interval == "1d" and data.n == N and data.k_start == 210 and data.regime is None
    result = simulate(cfg, data)
    first = result.trades[0]
    assert first.symbol == "LEADUSDT" and first.open_time_ms == data.start_ms + 211 * DAY_MS
    assert first.reason == ExitReason.ROTATION.value                     # Çökünce sıralamadan düştü
    assert any(t.symbol == "NEXTUSDT" for t in result.trades)           # Yerine yükselen alındı
    opens = [(t.open_time_ms - data.start_ms) // DAY_MS - 1 for t in result.trades]
    assert all((k - 210) % cfg.rotation_days == 0 for k in opens)       # Yalnızca dengeleme günlerinde alım
    assert result.stats["dengeleme"] == len(range(210, N - 1, cfg.rotation_days))   # Son mumda dengeleme yok
    assert data.start_ms == _start_ms() and data.start_ms % DAY_MS == 0
    # Kârlı satışın geliri aynı dengelemede yerine alınan coine gider (canlıdaki gibi satış SONRASI sermaye)
    sold = result.trades[0]
    replacement = next(t for t in result.trades if t.open_time_ms == sold.close_time_ms)
    assert replacement.cost == pytest.approx(cfg.start_capital + sold.pnl, rel=0.01)
    assert result.btc_hold_pct == pytest.approx((1.001 ** (N - 1 - 210) - 1) * 100, rel=1e-6)
    report = backtest.format_backtest_report(result, cfg)
    assert "Rotasyon: En güçlü 1 coin" in report and "BTC'yi alıp tutmak" in report and "Kâr Al +%" not in report


def test_rotation_backtest_trend_filter_goes_to_cash(rcfg, fake_client, tmp_path):
    cfg = replace(rcfg, rotation_top_n=1, rotation_lookback_days=14, start_capital=11.0)
    data = _prepare(cfg, DailyClient(fake_client, _series(btc_crash_at=240)).client, tmp_path)
    result = simulate(cfg, data)
    assert any(t.reason == ExitReason.TREND_FILTER.value for t in result.trades)
    last_trend_exit = max(t.close_time_ms for t in result.trades if t.reason == ExitReason.TREND_FILTER.value)
    assert all(t.open_time_ms <= last_trend_exit for t in result.trades)   # Trend bozulduktan sonra alım yok
    assert result.stats["trend_kapali_gun"] > 30
    no_filter = simulate(replace(cfg, rotation_btc_ma_days=0), data)
    assert not any(t.reason == ExitReason.TREND_FILTER.value for t in no_filter.trades)


def test_rotation_backtest_respects_listing_filter(rcfg, fake_client, tmp_path):
    cfg = replace(rcfg, rotation_top_n=1, rotation_lookback_days=14, start_capital=11.0)
    listed = {"LEADUSDT": END_MS - (DAYS + 5) * DAY_MS}            # Simülasyondan 5 gün önce listelenmiş
    data = _prepare(cfg, DailyClient(fake_client, _series(), listed).client, tmp_path)
    result = simulate(cfg, data)
    lead = [t for t in result.trades if t.symbol == "LEADUSDT"]
    assert not lead or lead[0].open_time_ms >= listed["LEADUSDT"] + 13 * DAY_MS
    assert result.stats.get("yeni_coin", 0) > 0


def test_rotation_sweeps_and_advice(rcfg):
    combos = sweep_combos(rcfg)
    assert combos[0] == ({"STOP_LOSS_PCT": 20.0, "ROTATION_DAYS": 7}, True)
    assert {c["STOP_LOSS_PCT"] for c, _ in combos} == {10.0, 20.0, 30.0} and len(combos) == 6
    entries = entry_combos(rcfg, [0.0])
    assert {c["ROTATION_LOOKBACK_DAYS"] for c, _ in entries} == {14, 28, 56}
    assert {c["ROTATION_BTC_MA_DAYS"] for c, _ in entries} == {0, 50, 100} and len(entries) == 18
    from backtest import SweepRow, _advice_lines
    row = SweepRow({"STOP_LOSS_PCT": 30.0, "ROTATION_DAYS": 14}, False, 80, 50, 5, 1, 10, 1, 1, 2)
    lines = _advice_lines(rcfg, row)
    for line in ("STRATEGY=rotation", "STOP_LOSS_PCT=30", "ROTATION_DAYS=14", "ROTATION_TOP_N=3",
                 "ROTATION_LOOKBACK_DAYS=28", "BACKUP_STOP_PCT=34"):
        assert line in lines, line
    assert sum(1 for ln in lines if ln.startswith("BACKUP_STOP_PCT=")) == 1


def test_cli_rotation_compare_falls_back_to_report(cfg, monkeypatch, capsys):
    import binance_client
    import main
    monkeypatch.setattr(binance_client, "BinanceClient", lambda *a, **k: object())
    ran = []
    monkeypatch.setattr(backtest, "run_backtest", lambda *a, **k: ran.append(1) or (_ for _ in ()).throw(RuntimeError("dur")))
    monkeypatch.setattr(backtest, "prepare_backtest", lambda *a, **k: (_ for _ in ()).throw(AssertionError("indirme")))
    main.run_backtest_cli(cfg, main.parse_args(["--backtest", "--strateji", "rotation", "--strateji-karsilastir"]))
    assert ran and "tek stratejidir" in capsys.readouterr().out


def test_cli_rotation_defaults(cfg, monkeypatch, capsys):
    import binance_client
    import main
    monkeypatch.setattr(binance_client, "BinanceClient", lambda *a, **k: object())
    seen = {}

    def fake_run(c, client, days, max_symbols, end_time_ms=None):
        seen.update(strategy=c.strategy, days=days, sl=c.stop_loss_pct)
        raise RuntimeError("dur")
    monkeypatch.setattr(backtest, "run_backtest", fake_run)
    main.run_backtest_cli(cfg, main.parse_args(["--backtest", "--strateji", "rotation"]))
    assert seen == {"strategy": "rotation", "days": 1095, "sl": 20.0}
    assert "STOP_LOSS_PCT=20" in capsys.readouterr().out


# ---------------------------------------------------------------- canlı bot (sanal borsa)
T0 = 19676 * 86400 + 600     # Bir UTC gününün 00:10'u


class LiveDaily:
    """Günlük kapanışları değiştirilebilen sahte Binance (son satır = bugünün açık mumu)."""

    def __init__(self, fake_client, closes):
        outer = self

        class C(fake_client):
            def klines(inner, symbol, interval="5m", limit=100, start_time=None, end_time=None):
                assert interval == "1d"
                c = outer.closes[symbol]
                return [[i * DAY_MS, str(x), str(x), str(x), str(x), "0", i * DAY_MS + DAY_MS - 1, "1e7", 0, "0",
                         "0", "0"] for i, x in enumerate(c)][-limit:]

            def tickers_24h(inner):
                return [{"symbol": s, "lastPrice": str(inner.prices[s]), "quoteVolume": "50000000",
                         "priceChangePercent": "0", "highPrice": str(inner.prices[s]),
                         "lowPrice": str(inner.prices[s])} for s in outer.closes]
        self.closes = dict(closes)
        self.client = C({s: c[-1] for s, c in closes.items()})

    def set(self, symbol, closes):
        self.closes[symbol] = closes
        self.client.prices[symbol] = closes[-1]


def make_rotation_bot(rcfg, fake_client, tmp_path, closes, clock):
    feed = LiveDaily(fake_client, closes)
    state = BotState.new("paper", rcfg.start_capital)
    exchange = PaperExchange(feed.client, state.paper_balances, fee_rate=rcfg.fee_rate)
    notes = CaptureNotifier()
    bot = TradingBot(rcfg, exchange, RotationStrategy(feed.client, rcfg), notes, StateStore(tmp_path / "s.json"),
                     state, TradeLog(tmp_path / "t.csv"), time_fn=lambda: clock[0])
    bot.startup()
    bot.running = True
    return bot, feed, notes


def rising(n=60, f=1.01, start=1.0):
    return path((n, f), start=start)


def faded(peak_factor=1.0205, start=1.0):
    """Önce yükselip son 4 haftada hafifçe gerileyen coin: momentum negatif ama fiyat alış seviyesine yakın
    (felaket zarar kesi devreye girmez)."""
    return path((32, peak_factor), (28, 0.998), start=start)


def closed_last(closes, value):
    """Son KAPANMIŞ günün kapanışını değiştirir (listenin son elemanı bugünün açık mumu)."""
    return closes[:-2] + [value, value]


def test_live_rotation_rebalances_weekly_and_rotates(rcfg, fake_client, tmp_path):
    cfg = replace(rcfg, rotation_top_n=2)
    clock = [T0]
    closes = {"AUSDT": rising(f=1.02), "BUSDT": rising(f=1.01), "CUSDT": rising(f=1.001),
              "BTCUSDT": rising(f=1.002, start=30_000.0)}
    bot, feed, notes = make_rotation_bot(cfg, fake_client, tmp_path, closes, clock)
    bot.tick()
    assert set(bot.state.positions) == {"AUSDT", "BUSDT"}
    assert bot.state.last_rebalance_day == 19676
    assert any("ROTASYON — YENİDEN DENGELEME" in m for m in notes.messages)
    assert any("ROTASYON ALIMI" in m and "Felaket zarar kesi" in m for m in notes.messages)

    clock[0] += 3 * 86400              # Dengeleme günü değil: işlem yok
    feed.set("BUSDT", faded())
    bot.next_scan_at = 0
    bot.tick()
    assert set(bot.state.positions) == {"AUSDT", "BUSDT"}

    clock[0] += 4 * 86400              # 7. gün: B negatif momentum → satılır, C alınır
    bot.next_scan_at = 0
    bot.tick()
    assert set(bot.state.positions) == {"AUSDT", "CUSDT"}
    assert bot.state.last_rebalance_day == 19676 + 7
    assert "ROTATION" in (tmp_path / "t.csv").read_text(encoding="utf-8")
    assert "ROTASYON" in bot.scan_report()


def test_live_rotation_trend_filter_and_pause(rcfg, fake_client, tmp_path):
    clock = [T0]
    closes = {"AUSDT": rising(f=1.02), "BUSDT": rising(f=1.01), "BTCUSDT": rising(f=1.002, start=30_000.0)}
    bot, feed, notes = make_rotation_bot(rcfg, fake_client, tmp_path, closes, clock)
    bot.tick()
    assert bot.state.positions
    clock[0] += 86400                  # Ertesi gün BTC ortalamanın altına indi → hepsi satılır
    feed.set("BTCUSDT", closed_last(rising(f=1.002, start=30_000.0), 20_000.0))
    bot.next_scan_at = 0
    bot.tick()
    assert not bot.state.positions and not bot.state.rotation_sells
    assert any("BTC düşüş trendine girdi" in m for m in notes.messages)
    assert "TREND_FILTER" in (tmp_path / "t.csv").read_text(encoding="utf-8")

    bot.set_paused(True)               # /durdur: dengeleme günü gelse de alım yok
    feed.set("BTCUSDT", rising(f=1.002, start=30_000.0))
    clock[0] += 7 * 86400
    bot.next_scan_at = 0
    bot.tick()
    assert not bot.state.positions and bot.state.last_rebalance_day == 19676
    bot.set_paused(False)
    bot.next_scan_at = 0
    bot.tick()
    assert bot.state.positions and bot.state.last_rebalance_day == 19676 + 8


def test_live_rotation_retries_failed_sell(rcfg, fake_client, tmp_path, monkeypatch):
    cfg = replace(rcfg, rotation_top_n=1)
    clock = [T0]
    closes = {"AUSDT": rising(f=1.02), "BUSDT": rising(f=1.001), "BTCUSDT": rising(f=1.002, start=30_000.0)}
    bot, feed, notes = make_rotation_bot(cfg, fake_client, tmp_path, closes, clock)
    bot.tick()
    assert set(bot.state.positions) == {"AUSDT"}
    real_close = bot.close_position
    monkeypatch.setattr(bot, "close_position", lambda pos, reason: False)
    feed.set("AUSDT", faded(peak_factor=1.0405))
    clock[0] += 7 * 86400
    bot.next_scan_at = 0
    bot.tick()
    assert bot.state.rotation_sells == {"AUSDT": "ROTATION"} and "AUSDT" in bot.state.positions
    assert any("Henüz satılamayan" in m for m in notes.messages)
    reloaded = StateStore(tmp_path / "s.json").load()
    assert reloaded.rotation_sells == {"AUSDT": "ROTATION"}          # Yeniden başlatmada da hatırlanır
    monkeypatch.setattr(bot, "close_position", real_close)
    clock[0] += 60
    bot.tick()
    # Satıldı ve boşalan yer hemen dolduruldu (backtest satış + alımı aynı anda yapar)
    assert "AUSDT" not in bot.state.positions and not bot.state.rotation_sells
    assert set(bot.state.positions) == {"BUSDT"} and not bot.state.rotation_refill
    assert bot.state.last_rebalance_day == 19676 + 7      # Takvim kaymaz
    assert any("BOŞALAN YER DOLDURULDU" in m for m in notes.messages)


def test_live_rotation_network_error_mid_rebalance_is_retried(rcfg, fake_client, tmp_path, monkeypatch):
    import requests
    cfg = replace(rcfg, rotation_top_n=2)
    clock = [T0]
    closes = {"AUSDT": rising(f=1.02), "BUSDT": rising(f=1.01), "BTCUSDT": rising(f=1.002, start=30_000.0)}
    bot, feed, notes = make_rotation_bot(cfg, fake_client, tmp_path, closes, clock)
    real_buy, calls = bot.open_position, []

    def flaky(signal, *a, **k):
        calls.append(signal.symbol)
        if len(calls) == 2:
            raise requests.ConnectionError("ağ koptu")
        return real_buy(signal, *a, **k)
    monkeypatch.setattr(bot, "open_position", flaky)
    with pytest.raises(requests.ConnectionError):
        bot.tick()
    assert set(bot.state.positions) == {"AUSDT"} and bot.state.last_rebalance_day == -1   # Gün kaydedilmedi
    bot.next_scan_at = 0
    bot.tick()                                             # Yeniden planlanır: A elde, yalnızca B alınır
    assert set(bot.state.positions) == {"AUSDT", "BUSDT"} and bot.state.last_rebalance_day == 19676
    assert calls == ["AUSDT", "BUSDT", "BUSDT"]


def test_live_rotation_stops_buying_after_unknown_buy(rcfg, fake_client, tmp_path, monkeypatch):
    cfg = replace(rcfg, rotation_top_n=2)
    clock = [T0]
    closes = {"AUSDT": rising(f=1.02), "BUSDT": rising(f=1.01), "BTCUSDT": rising(f=1.002, start=30_000.0)}
    bot, feed, notes = make_rotation_bot(cfg, fake_client, tmp_path, closes, clock)
    tried = []

    def unknown(signal, *a, **k):
        tried.append(signal.symbol)
        bot.state.pending_orders["cid-1"] = {"symbol": signal.symbol, "side": "BUY", "created_at": clock[0]}
        return False
    monkeypatch.setattr(bot, "open_position", unknown)
    bot.tick()
    assert tried == ["AUSDT"]                              # Sonuç belirsizken ikinci coin alınmadı


def test_rotation_position_size_keeps_backup_stop_placeable(cfg, rcfg):
    from risk import min_position_usdt, plan_entry
    assert min_position_usdt(5.0, cfg) == pytest.approx(5 * 1.05 / 0.9)          # 5 dk ayarları değişmedi
    rot_min = min_position_usdt(5.0, rcfg)
    assert rot_min == pytest.approx(5 / (0.75 * 0.99))                             # -%25 yedek stop ≥ 5 USDT
    from exchange import parse_symbol_rules
    from conftest import FakeClient
    rules = parse_symbol_rules(FakeClient({"AUSDT": 1.0}).exchange_info(), "USDT")["AUSDT"]
    small = plan_entry(6.6, rules, 1.0, rcfg)
    assert not small.ok and "yedek stop" in small.reason
    assert plan_entry(6.8, rules, 1.0, rcfg).ok


def test_rotation_pause_and_resume_replies(rcfg, fake_client, tmp_path):
    from commands import CommandProcessor
    clock = [T0]
    bot, feed, notes = make_rotation_bot(rcfg, fake_client, tmp_path,
                                         {"AUSDT": rising(f=1.02), "BTCUSDT": rising(f=1.002, start=30_000.0)}, clock)
    replies = []
    proc = CommandProcessor(bot, time_fn=lambda: clock[0])
    proc.reply = replies.append
    proc.cmd_durdur([])
    assert "Yeniden dengeleme ertelendi" in replies[-1] and bot.state.paused
    proc.cmd_devam([])
    assert "birkaç saniye içinde" in replies[-1]
    bot.tick()
    proc.cmd_devam([])
    assert "UTC 00:05" in replies[-1]


def test_rotation_scan_for_selftest(rcfg, fake_client):
    feed = LiveDaily(fake_client, {"AUSDT": rising(f=1.02), "BUSDT": rising(f=0.99),
                                   "BTCUSDT": rising(f=1.002, start=30_000.0)})
    from exchange import parse_symbol_rules
    rules = parse_symbol_rules(feed.client.exchange_info(), "USDT")
    strat = make_strategy(feed.client, rcfg)
    assert strat.is_rotation and strat.kline_limit == 52
    result = strat.scan(rules, set(), 25.0)
    # BTC de sıralamaya girer (en güçlüler arasındaysa tutulur); düşen B girmez
    assert result.market_ok and [s.symbol for s in result.signals] == ["AUSDT", "BTCUSDT"]
    assert "Momentum" in result.signals[0].note()
