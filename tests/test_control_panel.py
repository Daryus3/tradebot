"""Telegram kontrol paneli (/menu), çalışırken ayar değiştirme, risk sigortaları, ek filtreler, IP izleme, A/B testi."""
import os
from dataclasses import replace

import pytest

import control_panel
from commands import CALLBACK, Command, TelegramCommandListener
from config import ConfigError
from entry_filters import (BELOW_VWAP, BOOK_IMBALANCE, FAKEOUT, OVERHEAT, SELL_WALL, WEAK_TAPE, book_reject,
                           candle_reject, tape_reject, vwap)
from ip_monitor import IpMonitor, fetch_ip
from market_scan import analyze_coin, scan_market
from risk import ExitReason
from runtime_settings import apply_settings, find_key, parse_value, write_env_values
from telegram_bot import TelegramNotifier
from test_backtest import make_client, no_pause  # noqa: F401 - sentetik backtest istemcisi
from test_bot_flow import make_bot, tick
from test_new_features import FakeHttp


def press(bot, data, message_id=7):
    bot.commands.handle(Command(CALLBACK, data.split(":"), kind="callback", message_id=message_id))


def buttons(keyboard):
    return [data for row in keyboard for _label, data in row]


@pytest.fixture
def panel_bot(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n# yorum\nTAKE_PROFIT_PCT=15\nTAKE_PROFIT_PCT=15\n", encoding="utf-8")
    os.chmod(env, 0o600)
    bot.settings_path = env
    return bot, client, notes, env


# ------------------------------------------------------------------ .env yazma ve doğrulama
def test_write_env_values_keeps_other_lines_and_permissions(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n# yorum\nexport STOP_LOSS_PCT=10\nSTOP_LOSS_PCT=9\n", encoding="utf-8")
    os.chmod(env, 0o600)
    write_env_values(env, {"STOP_LOSS_PCT": "3", "NOTIFY_MODE": "closes", "OLD_KEY": None})
    text = env.read_text(encoding="utf-8")
    assert text.startswith("BINANCE_API_KEY=gizli\n# yorum\n")
    assert text.count("STOP_LOSS_PCT=3") == 2 and "=10" not in text and "=9" not in text
    assert "Telegram menüsünden" in text and text.rstrip().endswith("NOTIFY_MODE=closes")
    assert os.stat(env).st_mode & 0o777 == 0o600
    assert not (tmp_path / ".env.tmp").exists()
    write_env_values(env, {"STOP_LOSS_PCT": None})
    assert "STOP_LOSS_PCT" not in env.read_text(encoding="utf-8")


def test_apply_settings_validates_and_parses(cfg):
    new, _ = apply_settings(cfg, {"TAKE_PROFIT_PCT": "12,5", "FAKEOUT_FILTER": "açık", "NOTIFY_MODE": "Silent"})
    assert new.take_profit_pct == 12.5 and new.fakeout_filter is True and new.notify_mode == "silent"
    with pytest.raises(ConfigError):   # İzleyen stop başlangıcı (10) Kâr Al'dan küçük olmalı
        apply_settings(cfg, {"TAKE_PROFIT_PCT": 5})
    with pytest.raises(ConfigError):
        apply_settings(cfg, {"BINANCE_API_KEY": "x"})
    with pytest.raises(ConfigError):
        parse_value("VWAP_FILTER", "belki")
    assert find_key("tp") == "TAKE_PROFIT_PCT" and find_key("trading_mode") is None


# ------------------------------------------------------------------ Telegram düğmeleri
def test_listener_accepts_buttons_only_from_own_chat():
    http = FakeHttp([])
    lst = TelegramCommandListener("t" * 10, "42", http=http)
    lst._handle_update({"update_id": 1, "callback_query": {
        "id": "a", "data": "adj:TP:+", "message": {"message_id": 5, "chat": {"id": 42}}}})
    lst._handle_update({"update_id": 2, "callback_query": {
        "id": "b", "data": "rst", "message": {"message_id": 6, "chat": {"id": 99}}}})
    cmds = lst.get_commands()
    assert len(cmds) == 1 and cmds[0].kind == "callback" and cmds[0].args == ["adj", "TP", "+"]
    assert cmds[0].message_id == 5
    answers = [j for m, j in http.calls if m == "answerCallbackQuery"]
    assert answers[0] == {"callback_query_id": "a"} and "size ait değil" in answers[1]["text"]


def test_menu_shows_buttons_and_pause_toggle(panel_bot):
    bot, _, notes, _ = panel_bot
    bot.commands.handle(Command("menu"))
    text, keyboard, _ = notes.menus[-1]
    assert "Kontrol Paneli" in text
    assert {"st", "pause", "cfg", "cap", "strat", "ntf", "flt", "risk", "scan", "lat", "log", "rst"} <= set(
        buttons(keyboard))
    press(bot, "pause")
    assert bot.state.paused
    _, keyboard, mid = notes.menus[-1]
    assert "resume" in buttons(keyboard) and mid == 7   # Menü yerinde güncellendi


def test_settings_buttons_apply_live_and_persist(panel_bot):
    bot, _, notes, env = panel_bot
    press(bot, "adj:TP:+")
    assert bot.cfg.take_profit_pct == 15.5 and bot.strategy.__class__  # Bota hemen uygulandı
    text = env.read_text(encoding="utf-8")
    assert text.count("TAKE_PROFIT_PCT=15.5") == 2 and "BINANCE_API_KEY=gizli" in text
    for _ in range(12):   # TP 9.5'e inerken izleyen stop başlangıcının (10) altına düşemez
        press(bot, "adj:TP:-")
    assert bot.cfg.take_profit_pct == 10.5
    assert "TRAILING_ACTIVATION_PCT" in notes.menus[-1][0]
    press(bot, "adj:POS:-")
    assert bot.cfg.max_open_positions == 2 and "MAX_OPEN_POSITIONS=2" in env.read_text(encoding="utf-8")


def test_filter_toggle_capital_and_typed_setting(panel_bot):
    bot, _, notes, env = panel_bot
    press(bot, "tg:FAKEOUT_FILTER")
    assert bot.cfg.fakeout_filter and "FAKEOUT_FILTER=true" in env.read_text(encoding="utf-8")
    press(bot, "tg:FAKEOUT_FILTER")
    assert not bot.cfg.fakeout_filter
    press(bot, "capset:50")
    assert bot.cfg.capital_usage_pct == 50
    bot.commands.handle(Command("ayar", ["sl", "4"]))
    assert bot.cfg.stop_loss_pct == 4 and "STOP_LOSS_PCT=4" in env.read_text(encoding="utf-8")
    bot.commands.handle(Command("ayar", ["TRADING_MODE", "live"]))
    assert "değiştirilemez" in notes.messages[-1] and bot.cfg.is_paper


def test_notify_mode_mutes_routine_but_not_replies(panel_bot):
    bot, client, notes, _ = panel_bot
    press(bot, "ntfset:closes")
    assert bot.notifier.mode == "closes"
    before = len(notes.messages)
    tick(bot)   # Alım yapıldı ama "closes" modunda alım mesajı gelmez
    assert bot.state.positions and not any("ALIM YAPILDI" in m for m in notes.messages[before:])
    bot.commands.handle(Command("durum"))
    assert len(notes.messages) > before   # Komut cevabı her modda gelir
    client.prices["ABCUSDT"] = 1.16
    tick(bot)
    assert any("KÂR HEDEFİNE ULAŞILDI" in m for m in notes.messages)


def test_strategy_switch_saves_profile_and_restarts(panel_bot, monkeypatch):
    bot, _, notes, env = panel_bot
    monkeypatch.setenv("INVOCATION_ID", "x")
    monkeypatch.setattr(control_panel, "sudo_restart", lambda service: (False, "sudo yok"))
    press(bot, "stp:mean_revert")
    assert bot.commands.pending.action == "strategy" and "TAKE_PROFIT_PCT=1.5" in notes.menus[-1][0]
    token = bot.commands.pending.token
    assert buttons(notes.menus[-1][1]) == [f"yes:{token}", f"no:{token}"]
    press(bot, f"yes:{token}")
    text = env.read_text(encoding="utf-8")
    assert "STRATEGY=mean_revert" in text and "TAKE_PROFIT_PCT=1.5" in text and "BINANCE_API_KEY=gizli" in text
    assert bot.state.family_settings["short"]["TAKE_PROFIT_PCT"] == "15"
    assert not bot.running and bot.exit_code == control_panel.RESTART_EXIT_CODE


def test_strategy_switch_blocked_with_open_positions_and_pin(panel_bot):
    bot, _, notes, env = panel_bot
    tick(bot)
    press(bot, "stp:swing")
    assert "Açık pozisyon varken" in notes.messages[-1] and bot.commands.pending is None
    bot.cfg = replace(bot.cfg, command_pin="1234")
    press(bot, "stp:trend")   # Aynı grup: açık pozisyon engel değil; PIN varken düğmede "Evet" yok
    token = bot.commands.pending.token
    assert buttons(notes.menus[-1][1]) == [f"no:{token}"]
    press(bot, f"yes:{token}")
    assert "PIN" in notes.messages[-1] and "STRATEGY" not in env.read_text(encoding="utf-8")


def test_restart_outside_systemd_does_not_stop(panel_bot, monkeypatch):
    bot, _, notes, _ = panel_bot
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    press(bot, "rst")
    press(bot, f"yes:{bot.commands.pending.token}")
    assert bot.running and "systemd" in notes.messages[-1]
    monkeypatch.setenv("INVOCATION_ID", "x")
    monkeypatch.setattr(control_panel, "sudo_restart", lambda service: (True, ""))
    press(bot, "rst")
    press(bot, f"yes:{bot.commands.pending.token}")
    assert bot.running and "sudo systemctl" in notes.messages[-1]   # systemd botu SIGINT ile durduracak


def test_old_confirm_button_cannot_confirm_newer_action(panel_bot):
    bot, client, notes, _ = panel_bot
    tick(bot)
    bot.commands.handle(Command("hepsinisat"))
    old = bot.commands.pending.token
    press(bot, "rst")   # Onaylanmadan başka bir onay istendi
    assert bot.commands.pending.action == "restart" and bot.commands.pending.token != old
    press(bot, f"yes:{old}")
    assert "yerini başka bir işlem aldı" in notes.messages[-1] and bot.commands.pending.action == "restart"
    press(bot, f"no:{bot.commands.pending.token}")
    assert bot.commands.pending is None and bot.state.positions


def test_info_tools_latency_logs_ip(panel_bot, tmp_path, monkeypatch):
    bot, client, notes, _ = panel_bot
    client.ping = lambda: True
    press(bot, "lat")
    assert "Ağ Gecikmesi" in notes.messages[-1] and "ms" in notes.messages[-1]
    log = tmp_path / "bot.log"
    log.write_text("".join(f"satır {i} <b>\n" for i in range(40)), encoding="utf-8")
    monkeypatch.setattr(control_panel, "log_file_path", lambda: log)
    press(bot, "log")
    assert "satır 39" in notes.messages[-1] and "satır 24" not in notes.messages[-1]
    assert "&lt;b&gt;" in notes.messages[-1]
    bot.ip_monitor = IpMonitor(0, http=FakeIpHttp({"https://api.ipify.org": "1.2.3.4"}))
    press(bot, "ip")
    assert "1.2.3.4" in notes.messages[-1]


def test_market_scan_runs_in_background(panel_bot):
    bot, _, notes, _ = panel_bot
    bot.commands.panel.scan_fn = lambda client, rules, cfg: "🔎 sonuç"
    press(bot, "scan")
    bot.commands.panel._scan_thread.join(5)
    assert notes.messages[-1] == "🔎 sonuç"


def test_scan_time_callbacks_split_read_only(panel_bot):
    bot, _, _, _ = panel_bot

    class Listener:
        def __init__(self, cmds):
            self.cmds = cmds

        def get_commands(self):
            out, self.cmds = self.cmds, []
            return out

    bot.command_listener = Listener([Command(CALLBACK, ["menu"], kind="callback"),
                                     Command(CALLBACK, ["adj", "TP", "+"], kind="callback")])
    bot._scan_deadline = float("inf")
    bot._during_scan()
    assert [c.args[0] for c in bot._deferred_commands] == ["adj"]


# ------------------------------------------------------------------ risk sigortaları
def test_loss_streak_freezes_new_entries(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(replace(cfg, loss_streak_limit=2, coin_sl_limit=0), fake_client, tmp_path)
    for _ in range(2):
        bot.state.cooldowns.clear()
        client.prices["ABCUSDT"] = 1.0
        tick(bot)
        client.prices["ABCUSDT"] = 0.89
        tick(bot)
    assert bot.entries_frozen(bot.time_fn()) and "üst üste 2 zarar kes" in bot.state.freeze_reason
    assert any("Yeni alımlar donduruldu" in m for m in notes.messages)
    bot.state.cooldowns.clear()
    client.prices["ABCUSDT"] = 1.0
    tick(bot)
    assert not bot.state.positions                 # Dondurulmuşken alım yok
    bot.state.freeze_until = bot.time_fn() - 1
    tick(bot)
    assert any("dondurması sona erdi" in m for m in notes.messages)


def test_daily_loss_limit_and_coin_report_card(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(replace(cfg, daily_loss_limit_pct=5, coin_sl_limit=1), fake_client, tmp_path)
    tick(bot)
    client.prices["ABCUSDT"] = 0.89
    tick(bot)
    assert bot.today_pnl() < 0 and bot.entries_frozen(bot.time_fn())
    assert "günlük zarar limiti" in bot.state.freeze_reason
    text, keyboard, _ = notes.menus[-1]
    assert "Coin karnesi" in text and buttons(keyboard) == ["bl:ABCUSDT", "nobl:ABCUSDT"]
    press(bot, "bl:ABCUSDT")
    assert bot.state.cooldowns["ABCUSDT"] >= bot.time_fn() + 23 * 3600
    press(bot, "unfreeze")
    assert not bot.entries_frozen(bot.time_fn())


def test_win_resets_streak(cfg, fake_client, tmp_path):
    bot, _, _ = make_bot(replace(cfg, loss_streak_limit=3), fake_client, tmp_path)
    bot._after_close("ABCUSDT", -1.0, ExitReason.STOP_LOSS, bot.time_fn())
    bot._after_close("XUSDT", -1.0, ExitReason.MANUAL, bot.time_fn())   # Elle satış seriyi bozmaz
    assert bot.state.sl_streak == 1
    bot._after_close("ABCUSDT", 1.0, ExitReason.TAKE_PROFIT, bot.time_fn())
    assert bot.state.sl_streak == 0 and "ABCUSDT" not in bot.state.coin_sl_streak


def test_slippage_average_alert(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    for _ in range(4):
        bot._track_slippage(0.5)
    assert not any("ortalama kayma" in m for m in notes.messages)
    bot._track_slippage(0.5)
    assert any("ortalama kayma %0.50" in m for m in notes.messages)
    for _ in range(20):
        bot._track_slippage(0.0)
    assert len(bot.state.slippage_hist) == 10


def test_reserve_keeps_last_slot_from_using_other_reserves(cfg, fake_client, tmp_path):
    bot, _, _ = make_bot(replace(cfg, capital_usage_pct=50), fake_client, tmp_path,
                         prices={"ABCUSDT": 1.0, "XYZUSDT": 1.0}, capital=40)
    tick(bot)
    costs = sorted(p.cost_usdt for p in bot.state.positions.values())
    assert len(costs) == 2 and costs[1] <= 10.01   # 40 / 2 slot × %50; ikinci pozisyon yedeğe dokunmaz


# ------------------------------------------------------------------ IP izleme
class FakeIpHttp:
    def __init__(self, answers):
        self.answers = answers

    def get(self, url, timeout=None):
        text = self.answers.get(url)

        class Resp:
            ok = text is not None
        Resp.text = text or ""
        return Resp()


def test_ip_change_alert(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    http = FakeIpHttp({"https://api.ipify.org": "1.2.3.4", "https://api6.ipify.org": "bozuk"})
    bot.ip_monitor = IpMonitor(0, http=http)
    bot.ip_monitor.check_once()
    bot._check_ip()
    assert bot.state.public_ips == {"IPv4": "1.2.3.4"} and not any("IP adresi değişti" in m for m in notes.messages)
    http.answers["https://api.ipify.org"] = "5.6.7.8"
    bot.ip_monitor.check_once()
    bot._check_ip()
    assert any("IP adresi değişti" in m and "5.6.7.8" in m for m in notes.messages)
    assert fetch_ip(http, "https://api6.ipify.org", 6) is None


# ------------------------------------------------------------------ bildirim gönderici
class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None, data=None, files=None):
        self.calls.append((url.rsplit("/", 1)[-1], json))
        status, body = self.responses.pop(0) if self.responses else (200, "{}")

        class Resp:
            status_code = status
            ok = status < 400
            text = body

            def json(self):
                return {}
        return Resp()


def test_notifier_menu_edit_and_fallback():
    n = TelegramNotifier("t" * 10, "1", async_mode=False)
    n.session = FakeSession([(200, "{}"), (400, "Bad Request: message is not modified"),
                             (400, "Bad Request: message to edit not found"), (200, "{}")])
    assert n.send_menu("menü", [[("A", "a")]])
    method, payload = n.session.calls[0]
    assert method == "sendMessage" and payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "A", "callback_data": "a"}]]}
    assert n.send_menu("menü", [[("A", "a")]], message_id=9)          # Değişmemiş menü hata sayılmaz
    assert n.send_menu("menü", [[("A", "a")]], message_id=10)         # Düzenlenemeyen mesaj → yeni mesaj
    assert [m for m, _ in n.session.calls[1:]] == ["editMessageText", "editMessageText", "sendMessage"]
    n.mode = "silent"
    assert not n.notify("alım", "open") and n.notify("zarar kes", "close", critical=True)


# ------------------------------------------------------------------ giriş filtreleri
def kl(o, h, low, c, qv=1000.0, t=0):
    return [t, str(o), str(h), str(low), str(c), "0", t + 299_999, str(qv), 0, "0", "0", "0"]


def test_candle_filters(cfg):
    base = [kl(1.0, 1.01, 0.99, 1.0, t=i * 300_000) for i in range(20)]
    wick = base + [kl(1.0, 1.05, 0.99, 1.01, t=20 * 300_000), kl(1.01, 1.01, 1.01, 1.01, t=21 * 300_000)]
    assert candle_reject(cfg, wick) == ""                                     # Filtreler kapalı
    assert candle_reject(replace(cfg, fakeout_filter=True), wick) == FAKEOUT
    low = base + [kl(1.0, 1.0, 0.9, 0.9, t=20 * 300_000), kl(0.9, 0.9, 0.9, 0.9, t=21 * 300_000)]
    assert vwap(low[:-1]) > 0.9 and candle_reject(replace(cfg, vwap_filter=True), low) == BELOW_VWAP
    pump = base + [kl(1.0, 1.12, 1.0, 1.12, t=20 * 300_000), kl(1.12, 1.12, 1.12, 1.12, t=21 * 300_000)]
    assert candle_reject(replace(cfg, overheat_filter=True), pump) == OVERHEAT
    assert candle_reject(replace(cfg, overheat_filter=True, overheat_pct=20), pump) == ""


class BookClient:
    def __init__(self, asks, bids, trades=()):
        self.asks, self.bids, self.trades = asks, bids, list(trades)
        self.time_offset_ms = 0

    def depth(self, symbol, limit=100):
        return {"asks": self.asks, "bids": self.bids}

    def agg_trades(self, symbol, limit=500):
        return self.trades


def test_book_and_tape_filters(cfg):
    asks = [[str(1.0 + i * 0.001), "10"] for i in range(40)]
    bids = [[str(0.999 - i * 0.001), "10"] for i in range(40)]
    on = replace(cfg, ob_wall_filter=True, ob_imbalance_filter=True)
    assert book_reject(on, BookClient(asks, bids), "X", 1.0) == ""
    wall = [list(a) for a in asks]
    wall[3][1] = "500"
    assert book_reject(on, BookClient(wall, bids), "X", 1.0) == SELL_WALL
    assert book_reject(replace(on, ob_wall_filter=False), BookClient(asks, [["0.95", "10"]]), "X", 1.0) == \
        BOOK_IMBALANCE
    tape = replace(cfg, tape_filter=True)
    sells = [{"p": "1", "q": "100", "T": 1_000_000, "m": True}, {"p": "1", "q": "10", "T": 1_000_000, "m": False}]
    assert tape_reject(tape, BookClient([], [], sells), "X", now_ms=1_010_000) == WEAK_TAPE
    buys = [dict(t, m=not t["m"]) for t in sells]
    assert tape_reject(tape, BookClient([], [], buys), "X", now_ms=1_010_000) == ""

    class Broken:
        def depth(self, *a):
            raise ValueError("yok")
    assert book_reject(on, Broken(), "X", 1.0) == ""   # Veri yoksa alımı engellemez


def test_live_filters_block_entry(cfg, fake_client, tmp_path):
    bot, client, _ = make_bot(replace(cfg, tape_filter=True), fake_client, tmp_path)
    client.agg_trades = lambda symbol, limit=500: []
    tick(bot)
    assert not bot.state.positions and bot.day_stats.rejects[WEAK_TAPE] == 1


# ------------------------------------------------------------------ piyasa taraması
def test_market_scan_finds_bottom_with_rising_volume(cfg, fake_client):
    rows = [kl(1.0, 1.02, 0.98, 1.0, qv=100.0, t=i) for i in range(26)]
    rows += [kl(0.98, 0.99, 0.96, 0.965, qv=400.0, t=26 + i) for i in range(3)] + [kl(0.965, 0.97, 0.96, 0.966)]
    view = analyze_coin("ABCUSDT", rows, -3.0)
    assert view.position_pct < 25 and view.volume_ratio > 1.5

    class Client(fake_client):
        def tickers_24h(self):
            return [{"symbol": "ABCUSDT", "lastPrice": "1", "quoteVolume": "9", "priceChangePercent": "-3"}]

        def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
            return rows

    from exchange import parse_symbol_rules
    client = Client({"ABCUSDT": 1.0})
    rules = parse_symbol_rules(client.exchange_info())
    text = scan_market(client, rules, cfg)
    assert "Dipte ve hacmi artan" in text and "ABCUSDT" in text


# ------------------------------------------------------------------ backtest: filtreler, sigortalar, A/B
def test_backtest_candle_filter_and_ab_report(cfg, make_client, tmp_path):
    import backtest
    from test_backtest import DAYS, END_TIME_MS, btc_flat, scenario, trend
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    data = backtest.prepare_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                                     symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)
    base = backtest.simulate(cfg, data)
    blocked = backtest.simulate(replace(cfg, vwap_filter=True, vwap_bars=200), data)
    assert base.trades and not blocked.trades           # Toparlanma girişinde fiyat 200 mumluk VWAP'ın altında
    assert blocked.stats.get(f"filtre:{BELOW_VWAP}", 0) > 0
    combos = backtest.ab_combos(cfg, regime_available=data.regime is not None)
    keys = [next(iter(st)) for st, _ in combos[1:] if len(st) == 1]
    assert combos[0] == ({}, True) and "FAKEOUT_FILTER" in keys and "LOSS_STREAK_LIMIT" in keys
    rows = backtest.run_ab_test(cfg, data)
    text = backtest.format_ab_report(rows, cfg, data)
    assert "A/B FİLTRE TESTİ" in text and "VWAP filtresi: kapalı → açık" in text and "backtest edilemez" in text


def test_ab_report_recommends_only_consistent_changes(cfg):
    """Yalnızca iki yarıda da kârı artıran değişiklik önerilir; DCA yedek akçe satırıyla kıyaslanır; küçük fark önemsiz."""
    from types import SimpleNamespace
    import backtest
    from backtest import SweepRow
    big = replace(cfg, start_capital=1000.0, vwap_filter=True, trailing_buy=True)
    spec = [({}, 86, 22.5, 0.06, 30.0, -7.5),
            ({"BTC_FILTER": False}, 86, 23.2, 0.06, 30.4, -7.2),              # +0.7 USDT: önemsiz
            ({"VWAP_FILTER": False}, 206, 180.7, 0.12, 200.0, -19.3),          # Bir yarıda kötüleşti: tutarsız
            ({"TRAILING_BUY": False}, 90, 138.4, 0.19, 120.0, 18.4),           # İki yarıda da arttı
            ({"CAPITAL_USAGE_PCT": 50.0}, 86, 16.7, 0.05, 20.0, -3.3),         # Kıyas satırı
            ({"CAPITAL_USAGE_PCT": 50.0, "DCA_MODE": "auto"}, 85, 75.3, 0.41, 50.0, 25.3)]
    rows = [SweepRow(st, not st, n, 70.0, net, pct, 20.0, first, second, None, 0.3)
            for st, n, net, pct, first, second in spec]
    data = SimpleNamespace(sim_start_ms=1_785_000_000_000, end_ms=1_792_800_000_000, days=90, interval="5m",
                           series={})
    text = backtest.format_ab_report(rows, big, data)
    advice = text.split("iki yarısında da kârı artıranlar")[1].split("🟡")[0]
    assert "TRAILING_BUY=false" in advice and "DCA_MODE=auto" in advice
    assert "VWAP_FILTER" not in advice and "BTC_FILTER" not in advice
    assert "+58.6000 USDT" in advice                                      # DCA farkı yedek akçe satırına göre
    assert "Tutarsız" in text and "VWAP filtresi: açık → kapalı" in text.split("Tutarsız")[1]
    assert "≈  BTC sigortası" in text and "⚪ Yedek akçe %50 " in text and "kendi başına kâr artırmaz" in text


def test_backtest_loss_streak_freeze(cfg, make_client, tmp_path):
    import backtest
    from test_backtest import DAYS, END_TIME_MS, btc_flat, scenario, trend
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 0.97, 8)), "BTCUSDT": btc_flat()})
    data = backtest.prepare_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                                     symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)
    res = backtest.simulate(replace(cfg, loss_streak_limit=1), data)
    assert res.trades and res.trades[0].reason == ExitReason.STOP_LOSS.value
    assert res.stats.get("dondurma") == 1


def test_pnl_chart_written_when_matplotlib_present(tmp_path):
    pytest.importorskip("matplotlib")
    from backtest import BacktestTrade
    from charts import save_pnl_chart
    trades = [BacktestTrade("X", i * 3_600_000, (i + 1) * 3_600_000, 1, 1, 10, 10, p / 10, p, "TP", 1.0)
              for i, p in enumerate((2.5, -3.0, 1.0, -0.5, 2.5))]
    path = save_pnl_chart(trades, tmp_path / "g.png", "Backtest", 2.5, 3.0)
    assert path is not None and path.stat().st_size > 10_000
    assert save_pnl_chart([], tmp_path / "bos.png", "x") is None


def test_cli_ab_test_and_chart(cfg, monkeypatch, capsys, tmp_path):
    import backtest
    import main
    monkeypatch.setattr(main, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(backtest, "prepare_backtest", lambda *a, **k: "VERI")
    monkeypatch.setattr(backtest, "run_ab_test", lambda c, data, **k: ["satır"] if data == "VERI" else [])
    monkeypatch.setattr(backtest, "format_ab_report", lambda rows, *a, **k: f"AB RAPOR {rows}")
    monkeypatch.setattr(backtest, "save_sweep_csv", lambda rows, path: tmp_path / "ab.csv")
    assert main.main(["--backtest", "--tara-ab"]) == 0
    out = capsys.readouterr().out
    assert "A/B filtre testi" in out and "AB RAPOR ['satır']" in out

    sent = []
    monkeypatch.setattr(main, "_send_backtest_chart", lambda c, result, report, *a, **k: sent.append(report))
    monkeypatch.setattr(backtest, "run_backtest", lambda *a, **k: type("R", (), {"trades": [1]})())
    monkeypatch.setattr(backtest, "format_backtest_report", lambda *a, **k: "RAPOR")
    assert main.main(["--backtest"]) == 0 and sent == ["RAPOR"]
    assert main.main(["--backtest", "--grafiksiz"]) == 0 and sent == ["RAPOR"]
