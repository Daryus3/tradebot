"""Akıllı pozisyon yönetimi ve giriş: hedef uzatma, DCA, piramit, süre / kaçış soruları, ATR hedefleri, sinyal onayı,
kademeli alım, canlı pano, likidasyon / RSI uyumsuzluğu bildirimleri, BNB takviyesi, toz → BNB, backtest karşılıkları."""
import json
import time
from dataclasses import replace

import pytest

import smart_manager
from commands import CALLBACK, Command
from config import ConfigError
from liquidations import LiquidationFeed, spot_symbol
from market_scan import DivergenceWatcher
from position_rules import atr_targets, bullish_divergence, is_flat, momentum_strong, volume_breakdown
from risk import ExitReason, Position, evaluate_exit
from test_backtest import make_client, no_pause  # noqa: F401 - sentetik backtest istemcisi
from test_bot_flow import make_bot, tick


def press(bot, data, message_id=7):
    bot.commands.handle(Command(CALLBACK, data.split(":"), kind="callback", message_id=message_id))


def last_buttons(notes):
    return [data for row in notes.menus[-1][1] for _label, data in row]


def answer(bot, notes, choice):
    token = next(d for d in last_buttons(notes) if d.startswith("q:")).split(":")[1]
    press(bot, f"q:{token}:{choice}")


def row(o, h, low, c, qv=1000.0, tb=500.0, t=0):
    return [t, str(o), str(h), str(low), str(c), "0", t + 1, str(qv), 0, "0", str(tb), "0"]


# ------------------------------------------------------------------ saf kurallar
def test_rules_atr_momentum_breakdown(cfg):
    on = replace(cfg, atr_targets=True)
    assert atr_targets(cfg, 2.0) == (None, None)                    # Kapalı: ayardaki değerler
    assert atr_targets(on, 2.0) == (6.0, 4.0)
    assert atr_targets(on, 9.0) == (15.0, 10.0)                     # TP üst sınır, SL en fazla STOP_LOSS_PCT
    assert atr_targets(on, 0.1) == (1.0, 1.0)
    flat = [row(1, 1.01, 0.99, 1, t=i) for i in range(30)]
    up = flat[:-3] + [row(1, 1.03, 1, 1.02, qv=3000, t=i) for i in range(27, 30)]
    assert not momentum_strong(flat) and momentum_strong(up)
    dump = flat[:-1] + [row(1, 1, 0.9, 0.92, qv=5000, tb=1000)]
    assert volume_breakdown(dump, 1.5) and not volume_breakdown(dump[:-1] + [row(1, 1, 0.9, 0.92, qv=5000, tb=4000)], 1.5)
    assert is_flat(-0.3, 0.5) and not is_flat(0.8, 0.5)


def test_bullish_divergence_found():
    closes = [100 + (0.1 if i % 2 else -0.1) for i in range(30)]
    closes += [100 - i for i in range(1, 11)]                           # Sert düşüş: RSI çok düşük
    closes += [90 + 0.6 * i for i in range(1, 9)]                        # Tepki
    p = closes[-1]
    for i in range(11):                                                  # Yavaş ve dalgalı yeni dip
        p += -1.2 if i % 2 == 0 else 0.3
        closes.append(p)
    closes += [p + 0.8, p + 1.6]
    rows = [row(c, c + 0.2, c - 0.2, c, t=i) for i, c in enumerate(closes)]
    found = bullish_divergence(rows)
    assert found and found["price_drop_pct"] < 0 and found["rsi_now"] > found["rsi_prev"] + 3
    assert bullish_divergence(rows[:45]) is None


def test_evaluate_exit_extension_lock_and_time(cfg):
    pos = Position("X", "X", 1, 1, 10, time.time(), tp_extended=True, peak_pnl_pct=20.0)
    assert evaluate_exit(pos, 19.7, time.time(), cfg) is None
    assert evaluate_exit(pos, 19.4, time.time(), cfg) == ExitReason.TP_EXTENDED
    pos = Position("X", "X", 1, 1, 10, time.time(), lock_pct=0.3)
    assert evaluate_exit(pos, 0.2, time.time(), cfg) == ExitReason.PROFIT_LOCK
    pos = Position("X", "X", 1, 1, 10, time.time(), exit_at_breakeven=True, tp_pct=3.0, sl_pct=2.0)
    assert evaluate_exit(pos, -0.1, time.time(), cfg) is None
    assert evaluate_exit(pos, 0.0, time.time(), cfg) == ExitReason.TIME_EXIT
    pos.exit_at_breakeven = False
    assert evaluate_exit(pos, 3.0, time.time(), cfg) == ExitReason.TAKE_PROFIT
    assert evaluate_exit(pos, -2.0, time.time(), cfg) == ExitReason.STOP_LOSS


def test_config_validation_only_when_enabled(cfg):
    replace(cfg, stop_loss_pct=3.0, take_profit_pct=12.0).validate()   # DCA kapalı: tetik SL'den büyük olabilir
    with pytest.raises(ConfigError):
        replace(cfg, stop_loss_pct=3.0, take_profit_pct=12.0, dca_mode="ask").validate()
    with pytest.raises(ConfigError):
        replace(cfg, escape_mode="maybe").validate()


# ------------------------------------------------------------------ canlı: açık pozisyon yönetimi
def test_tp_extension_trails_from_peak(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(replace(cfg, tp_extend=True), fake_client, tmp_path)
    bot.smart.momentum = lambda symbol: True
    tick(bot)
    client.prices["ABCUSDT"] = 1.16
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    assert pos.tp_extended and any("HEDEF UZATILDI" in m for m in notes.messages)
    client.prices["ABCUSDT"] = 1.20
    tick(bot)
    assert bot.state.positions
    client.prices["ABCUSDT"] = 1.19
    tick(bot)
    assert not bot.state.positions and any("UZATILMIŞ KÂR AL" in m for m in notes.messages)
    assert bot.state.realized_pnl > 2.0


def test_tp_without_momentum_sells_normally(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(replace(cfg, tp_extend=True), fake_client, tmp_path)
    bot.smart.momentum = lambda symbol: False
    tick(bot)
    client.prices["ABCUSDT"] = 1.16
    tick(bot)
    assert not bot.state.positions and any("KÂR HEDEFİNE ULAŞILDI" in m for m in notes.messages)


def reserve_bot(cfg, fake_client, tmp_path, **kw):
    bot, client, notes = make_bot(replace(cfg, capital_usage_pct=50, **kw), fake_client, tmp_path, capital=40)
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    assert pos.reserve_usdt >= 6.0 and pos.cost_usdt < 7.0
    return bot, client, notes, pos


def test_dca_question_adds_from_reserve(cfg, fake_client, tmp_path):
    bot, client, notes, pos = reserve_bot(cfg, fake_client, tmp_path, dca_mode="ask")
    cost, qty = pos.cost_usdt, pos.qty
    client.prices["ABCUSDT"] = 0.94
    tick(bot)
    assert "ORTALAMA DÜŞÜRME" in notes.menus[-1][0] and pos.adds == 0
    answer(bot, notes, "add")
    assert pos.adds == 1 and pos.cost_usdt > 2 * cost - 0.1 and pos.qty > qty
    assert 0.94 < pos.entry_price < 1.0 and pos.reserve_usdt < 1.0
    assert pos.backup_order_id is not None and any("DCA: ORTALAMA DÜŞÜRÜLDÜ" in m for m in notes.messages)
    tick(bot)
    assert sum(1 for t, *_ in notes.menus if "ORTALAMA DÜŞÜRME" in t) == 1   # Bir kez sorulur


def test_dca_auto_and_question_expiry(cfg, fake_client, tmp_path):
    bot, client, notes, pos = reserve_bot(cfg, fake_client, tmp_path, dca_mode="auto")
    client.prices["ABCUSDT"] = 0.94
    tick(bot)
    assert pos.adds == 1
    bot.smart.questions["x"] = smart_manager.Question("dca", "ABCUSDT", int(pos.opened_at), expires=0)
    press(bot, "q:x:add")
    assert "süresi doldu" in notes.messages[-1] and pos.adds == 1


def test_pyramid_auto_locks_profit(cfg, fake_client, tmp_path):
    bot, client, notes, pos = reserve_bot(cfg, fake_client, tmp_path, pyramid_mode="auto")
    bot.smart.momentum = lambda symbol: True
    client.prices["ABCUSDT"] = 1.04
    tick(bot)
    assert pos.adds == 1 and pos.lock_pct == pytest.approx(0.3)
    assert any("PİRAMİT" in m for m in notes.messages)
    client.prices["ABCUSDT"] = 1.005
    tick(bot)
    assert not bot.state.positions and any("PİRAMİT KİLİDİ" in m for m in notes.messages)


def test_time_question_breakeven_exit(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(replace(cfg, time_exit_mode="ask"), fake_client, tmp_path)
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    tick(bot)
    assert not any("SÜRE SORUSU" in t for t, *_ in notes.menus)       # Süre dolmadı
    pos.opened_at -= 4 * 3600
    tick(bot)
    assert "SÜRE SORUSU" in notes.menus[-1][0]
    answer(bot, notes, "be")
    assert pos.exit_at_breakeven
    client.prices["ABCUSDT"] = 1.003
    tick(bot)
    assert not bot.state.positions and any("BAŞABAŞTA SATILDI" in m for m in notes.messages)


def test_escape_auto_on_volume_breakdown(cfg, fake_client, tmp_path, monkeypatch):
    bot, client, notes = make_bot(replace(cfg, escape_mode="auto"), fake_client, tmp_path)
    monkeypatch.setattr(smart_manager, "volume_breakdown", lambda rows, ratio: True)
    tick(bot)
    client.prices["ABCUSDT"] = 0.97                                       # -%3: tetik (-%4) gelmedi
    tick(bot)
    assert bot.state.positions
    client.prices["ABCUSDT"] = 0.95
    tick(bot)
    assert not bot.state.positions and any("ERKEN KAÇIŞ" in m for m in notes.messages)


def test_escape_question_with_pin(cfg, fake_client, tmp_path, monkeypatch):
    bot, client, notes = make_bot(replace(cfg, escape_mode="ask", command_pin="1234"), fake_client, tmp_path)
    monkeypatch.setattr(smart_manager, "volume_breakdown", lambda rows, ratio: True)
    tick(bot)
    client.prices["ABCUSDT"] = 0.95
    tick(bot)
    answer(bot, notes, "sell")
    assert bot.state.positions and bot.commands.pending.action == "question"   # PIN istenir
    bot.commands.handle(Command("evet", ["1234"]))
    assert not bot.state.positions


def test_atr_targets_per_position(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(replace(cfg, atr_targets=True), fake_client, tmp_path)
    bot.smart.hourly_atr = lambda symbol, price: 2.0
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    assert (pos.tp_pct, pos.sl_pct) == (6.0, 4.0) and any("+%6 ATR" in m for m in notes.messages)
    client.prices["ABCUSDT"] = 1.07
    tick(bot)
    assert not bot.state.positions and bot.state.wins == 1


# ------------------------------------------------------------------ canlı: akıllı giriş
def test_signal_approval_yes_no_and_timeout(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(replace(cfg, signal_approval="all"), fake_client, tmp_path)
    tick(bot)
    assert not bot.state.positions and "SİNYAL ONAYI" in notes.menus[-1][0] and bot.free_slots() == 0
    answer(bot, notes, "yes")
    assert "ABCUSDT" in bot.state.positions and not bot.smart.entries

    bot2, _, notes2 = make_bot(replace(cfg, signal_approval="all"), fake_client, tmp_path / "b")
    tick(bot2)
    answer(bot2, notes2, "no")
    assert not bot2.state.positions and "alınmadı" in notes2.menus[-1][0]
    asked = len(notes2.menus)
    tick(bot2)
    assert len(notes2.menus) == asked                                     # Reddedilen sinyal hemen tekrar sorulmaz
    bot2.state.cooldowns.clear()
    tick(bot2)                                                            # Yeni sinyal → yeni soru
    bot2.smart.entries["ABCUSDT"].expires = 0
    tick(bot2)
    assert not bot2.smart.entries and bot2.day_stats.rejects["onay gelmedi"] == 1
    assert "ABCUSDT" in bot2.state.cooldowns


def test_signal_approval_volatile_uses_atr(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(replace(cfg, signal_approval="volatile"), fake_client, tmp_path)
    bot.smart.hourly_atr = lambda symbol, price: 1.0
    tick(bot)
    assert "ABCUSDT" in bot.state.positions                               # Sakin piyasa: sormadan alır
    bot2, _, notes2 = make_bot(replace(cfg, signal_approval="volatile"), fake_client, tmp_path / "b")
    bot2.smart.hourly_atr = lambda symbol, price: 5.0
    tick(bot2)
    assert not bot2.state.positions and "oynaklık %5.0" in notes2.menus[-1][0]


def test_trailing_buy_waits_for_bounce(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(replace(cfg, trailing_buy=True), fake_client, tmp_path)
    tick(bot)
    assert not bot.state.positions and "ABCUSDT" in bot.smart.entries
    client.prices["ABCUSDT"] = 0.98
    tick(bot)
    assert not bot.state.positions and bot.smart.entries["ABCUSDT"].low == pytest.approx(0.98)
    client.prices["ABCUSDT"] = 0.983
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    assert pos.entry_price == pytest.approx(0.983) and not bot.smart.entries


def test_pending_entries_cancelled_when_paused(cfg, fake_client, tmp_path):
    bot, _, _ = make_bot(replace(cfg, trailing_buy=True), fake_client, tmp_path)
    tick(bot)
    bot.set_paused(True)
    tick(bot)
    assert not bot.smart.entries


# ------------------------------------------------------------------ pano, likidasyon, uyumsuzluk
def test_live_board_edits_one_message(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    bot.commands.handle(Command("pano"))
    first_mid = bot.smart.board["mid"]
    assert first_mid and "Canlı K/Z Panosu" in notes.menus[-1][0] and "ABCUSDT" in notes.menus[-1][0]
    count = len(notes.menus)
    bot.smart.update_board(time.time() + 1)                               # Aralık dolmadı
    assert len(notes.menus) == count
    client.prices["ABCUSDT"] = 1.05
    tick(bot)
    bot.smart.update_board(time.time() + 6)
    assert notes.menus[-1][2] == first_mid                                # Aynı mesaj düzenlendi
    press(bot, "board:off", message_id=first_mid)
    assert bot.smart.board is None and "durduruldu" in notes.menus[-1][0]


class FakeWs:
    def __init__(self, url, on_open, on_message, on_error, on_close):
        self.on_message = on_message


def liq(symbol, side, price, qty):
    return json.dumps({"e": "forceOrder", "o": {"s": symbol, "S": side, "ap": str(price), "z": str(qty),
                                                "p": str(price), "q": str(qty)}})


def test_liquidation_feed_and_alert(cfg, fake_client, tmp_path):
    assert spot_symbol("1000PEPEUSDT") == "PEPEUSDT" and spot_symbol("BTCUSDT") == "BTCUSDT"
    feed = LiquidationFeed(ws_factory=FakeWs)
    feed.handle_message(liq("BTCUSDT", "SELL", 50_000, 4))
    feed.handle_message(liq("BTCUSDT", "BUY", 50_000, 1))
    feed.handle_message("bozuk")
    assert feed.recent("BTCUSDT") == (200_000, 50_000)
    assert feed.spikes(100_000) == [("BTCUSDT", 200_000, 50_000)] and feed.spikes(500_000) == []
    bot, _, notes = make_bot(replace(cfg, liq_alert_usdt=100_000), fake_client, tmp_path)
    bot.smart.liq_feed = feed
    bot.smart.check_liquidations(time.time())
    assert "Likidasyon dalgası" in notes.messages[-1] and "BTCUSDT" in notes.messages[-1]
    before = len(notes.messages)
    bot.smart.check_liquidations(time.time() + 120)                      # Aynı coin için 1 saat bekler
    assert len(notes.messages) == before


def test_divergence_watcher_notifies_once(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(replace(cfg, rsi_div_alerts=True), fake_client, tmp_path)
    hit = ("ETHUSDT", {"price_drop_pct": -1.2, "rsi_prev": 22.0, "rsi_now": 31.0, "bars_apart": 12}, 2000.0)
    watcher = DivergenceWatcher(lambda c: None, scan_fn=lambda client, rules, c: [hit])
    assert watcher.maybe_start(bot.cfg, {}, bot.notifier, 0.0)
    watcher._thread.join(5)
    assert "Pozitif RSI uyumsuzluğu" in notes.messages[-1] and "ETHUSDT" in notes.messages[-1]
    before = len(notes.messages)
    assert not watcher.maybe_start(bot.cfg, {}, bot.notifier, 60.0)       # Aralık dolmadı
    watcher.next_at = 0
    watcher.maybe_start(bot.cfg, {}, bot.notifier, 120.0)
    watcher._thread.join(5)
    assert len(notes.messages) == before                                  # 12 saat aynı coin bildirilmez


# ------------------------------------------------------------------ BNB takviyesi ve toz (canlı mod gibi)
def live_like_bot(cfg, fake_client, tmp_path):
    from exchange import PaperExchange
    bot, client, notes = make_bot(cfg, fake_client, tmp_path, prices={"ABCUSDT": 1.0, "BNBUSDT": 600.0})

    class LiveLike(PaperExchange):
        is_paper = False
        converted = None

        def dust_assets(self):
            return [("XYZ", 0.5, 0.0002), ("ABC", 1.0, 0.0001), ("DOGE", 3.0, 0.0003)]

        def convert_dust(self, assets):
            LiveLike.converted = assets
            return 0.0005, 0.00001

    exchange = LiveLike(client, bot.state.paper_balances, fee_rate=cfg.fee_rate)
    exchange.rules = bot.exchange.rules
    bot.exchange = exchange
    return bot, client, notes, LiveLike


def test_bnb_topup_offer_uses_spare_balance(cfg, fake_client, tmp_path):
    bot, _, notes, _ = live_like_bot(cfg, fake_client, tmp_path)
    bot.smart.notice_with_bnb_offer("BNB azaldı")
    assert "BNB azaldı" in notes.menus[-1][0] and "6.00 USDT'lik BNB" in notes.menus[-1][0]
    answer(bot, notes, "buy")
    assert "serbest bakiye" in notes.menus[-1][0]                          # Bota ayrılmamış para yok
    bot.state.paper_balances["USDT"] += 10
    bot.smart.notice_with_bnb_offer("BNB azaldı")
    answer(bot, notes, "buy")
    assert "BNB alındı" in notes.menus[-1][0] and bot.state.paper_balances.get("BNB", 0) > 0
    assert bot.state.capital == pytest.approx(cfg.start_capital)        # Botun sermayesi değişmedi


def test_dust_conversion_skips_open_positions(cfg, fake_client, tmp_path):
    bot, _, notes, live = live_like_bot(cfg, fake_client, tmp_path)
    bot.exchange.rules = {k: v for k, v in bot.exchange.rules.items() if k == "ABCUSDT"}
    tick(bot)
    assert "ABCUSDT" in bot.state.positions
    bot.commands.handle(Command("toz"))
    assert "XYZ" in notes.menus[-1][0] and "ABC:" not in notes.menus[-1][0]
    answer(bot, notes, "go")
    assert live.converted == ["XYZ", "DOGE"] and "BNB'ye çevrildi" in notes.menus[-1][0]
    bot2, _, notes2 = make_bot(cfg, fake_client, tmp_path / "p")
    bot2.commands.handle(Command("toz"))
    assert "canlı modda" in notes2.messages[-1]


# ------------------------------------------------------------------ menü
def test_smart_menu_toggles_and_cycles(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    bot.settings_path = tmp_path / ".env"
    press(bot, "smart")
    assert "Akıllı Yönetim" in notes.menus[-1][0] and "cy:DCA_MODE" in last_buttons(notes)
    for expected in ("ask", "auto", "off"):
        press(bot, "cy:DCA_MODE")
        assert bot.cfg.dca_mode == expected
    press(bot, "tg:TP_EXTEND")
    assert bot.cfg.tp_extend and "TP_EXTEND=true" in (tmp_path / ".env").read_text(encoding="utf-8")
    press(bot, "cy:DCA_MODE")
    assert "Yedek akçe kapalı" in notes.menus[-1][0]


# ------------------------------------------------------------------ backtest karşılıkları
def _data(cfg, make_client, tmp_path, after):
    import backtest
    from test_backtest import DAYS, END_TIME_MS, btc_flat, scenario
    client = make_client({"ABCUSDT": scenario(after), "BTCUSDT": btc_flat()})
    return backtest.prepare_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                                     symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)


def test_backtest_dca_and_trailing_buy(cfg, make_client, tmp_path):
    import backtest
    from test_backtest import trend
    big = replace(cfg, start_capital=40.0)
    data = _data(big, make_client, tmp_path, lambda p: trend(p, 0.97, 8))
    base = backtest.simulate(replace(big, capital_usage_pct=50), data)
    dca = backtest.simulate(replace(big, capital_usage_pct=50, dca_mode="auto"), data)
    assert base.trades and dca.trades and dca.stats.get("ekleme_dca") == 1
    assert dca.trades[0].cost > 1.9 * base.trades[0].cost
    up = _data(cfg, make_client, tmp_path / "u", lambda p: trend(p, 1.03, 8))
    trail = backtest.simulate(replace(cfg, trailing_buy=True), up)
    assert trail.trades and trail.stats.get("kademeli_alim") == 1
    plain = backtest.simulate(cfg, up)
    assert trail.trades[0].entry_price > plain.trades[0].entry_price   # Dipten dönüş teyidi daha pahalıya aldı


def test_backtest_escape_atr_and_extension_run(cfg, make_client, tmp_path, monkeypatch):
    import backtest
    from test_backtest import trend
    down = _data(cfg, make_client, tmp_path, lambda p: trend(p, 0.97, 8))
    monkeypatch.setattr(backtest, "volume_breakdown", lambda rows, ratio: True)
    esc = backtest.simulate(replace(cfg, escape_mode="auto"), down)
    assert esc.trades[0].reason == ExitReason.ESCAPE.value and esc.stats.get("erken_kacis") == 1
    up = _data(cfg, make_client, tmp_path / "u", lambda p: trend(p, 1.03, 8))
    monkeypatch.setattr(backtest, "momentum_strong", lambda rows: True)
    ext = backtest.simulate(replace(cfg, tp_extend=True), up)
    assert ext.stats.get("hedef_uzatma") == 1
    atr = backtest.simulate(replace(cfg, atr_targets=True), up)
    assert atr.trades
    rows = backtest.run_ab_test(cfg, up)
    text = backtest.format_ab_report(rows, cfg, up)
    assert "Hedef uzatma: kapalı → açık" in text and "Yedek akçe %50 + DCA: kapalı → otomatik" in text
    assert "Süre kuralı (başabaşta sat): kapalı → otomatik" in text and "sorarak" in text
