"""Yeni özelliklerin testleri (test/paper modu, Telegram, komutlar, kayıt, strateji filtreleri)."""
import csv
import time
from dataclasses import replace

import pytest

from commands import Command, TelegramCommandListener, parse_command
from config import parse_time_range
from logger import redact, register_secrets
from risk import ExitReason
from state import BotState, EquityLog, StateStore, TradeLog
from strategy import ReboundStrategy
from telegram_bot import TelegramNotifier

from test_bot_flow import CaptureNotifier, make_bot, tick
from test_strategy import build_dip_and_bounce, kline


# ------------------------------------------------------------------ başabaş / BTC / spread
def test_breakeven_exit_when_enabled(cfg, fake_client, tmp_path):
    be = replace(cfg, breakeven_enabled=True)
    bot, client, notes = make_bot(be, fake_client, tmp_path)
    tick(bot)
    client.prices["ABCUSDT"] = 1.06       # net ~%5.8 → koruma aktif
    tick(bot)
    assert bot.state.positions["ABCUSDT"].breakeven_active
    assert any("BAŞABAŞ KORUMASI AKTİF" in m for m in notes.messages)
    client.prices["ABCUSDT"] = 1.004      # net ~%0.2 → başabaşta sat
    tick(bot)
    assert not bot.state.positions
    assert any("BAŞABAŞ KORUMASI ÇALIŞTI" in m for m in notes.messages)


def test_breakeven_enabled_by_default(cfg):
    assert cfg.breakeven_enabled


def test_breakeven_can_be_disabled(cfg, fake_client, tmp_path):
    bot, client, _ = make_bot(replace(cfg, breakeven_enabled=False), fake_client, tmp_path)
    tick(bot)
    client.prices["ABCUSDT"] = 1.06
    tick(bot)
    client.prices["ABCUSDT"] = 1.004
    tick(bot)
    assert "ABCUSDT" in bot.state.positions


def test_btc_crash_exit(cfg, fake_client, tmp_path):
    crash = replace(cfg, btc_crash_exit=True)
    bot, client, notes = make_bot(crash, fake_client, tmp_path)
    tick(bot)
    client.prices["BTCUSDT"] = 60000.0

    def btc_klines(symbol, interval="5m", limit=100, start_time=None, end_time=None):
        rows = []
        for i in range(limit):
            p = 60000 if i < limit - 1 else 58500   # zirveden %2.5 düşüş
            rows.append([i, str(p), str(p), str(p), str(p), "0", i, "0", 0, "0", "0", "0"])
        return rows

    client.klines = btc_klines
    tick(bot)
    assert not bot.state.positions
    assert any("BTC ÇÖKÜŞÜ" in m for m in notes.messages)


def test_wide_spread_blocks_entry(cfg, fake_client, tmp_path):
    bot, client, _ = make_bot(cfg, fake_client, tmp_path)
    client.spread = 0.005                 # ~%1 spread > %0.3
    tick(bot)
    assert not bot.state.positions
    assert bot.day_stats.rejects["spread geniş"] == 1


# ------------------------------------------------------------------ yedek stop (paper)
def test_paper_backup_stop_offline_simulation(cfg, fake_client, tmp_path):
    bot, client, _ = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    assert pos.backup_order_id and pos.backup_stop_price < 0.9
    bot.state.last_alive = time.time() - 3600
    bot.store.save(bot.state)
    saved = StateStore(tmp_path / "state.json").load()

    def crashed(symbol, interval="5m", limit=100, start_time=None, end_time=None):
        return [[0, "1.0", "1.0", "0.80", "0.85", "0", 0, "0", 0, "0", "0", "0"]]

    bot2, client2, notes2 = make_bot(cfg, fake_client, tmp_path, state=saved)  # startup içinde kontrol edilir
    assert "ABCUSDT" in bot2.state.positions       # Varsayılan sahte mumlarda düşüş yok
    bot2.state.last_alive = time.time() - 3600
    client2.klines = crashed
    bot2._reconcile_positions_on_startup()
    assert not bot2.state.positions
    assert any("YEDEK STOP EMRİ ÇALIŞTI" in m for m in notes2.messages)


# ------------------------------------------------------------------ kesinti / internet
def test_loop_gap_sends_outage_alert(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    bot.last_loop_at = time.time() - 1800
    bot._check_loop_gap(time.time())
    assert any("Bot çalışmadı" in m and "ABCUSDT" in m for m in notes.messages)


def test_startup_reports_downtime(cfg, fake_client, tmp_path):
    state = BotState.new("paper", 11.0)
    state.last_alive = time.time() - 3 * 3600
    _, _, notes = make_bot(cfg, fake_client, tmp_path, state=state)
    assert any("Bot kapalıydı" in m for m in notes.messages)


def test_network_restored_message(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    bot.net_down_since = time.time() - 600
    bot._network_ok()
    assert any("İnternet bağlantısı geri geldi" in m for m in notes.messages)


# ------------------------------------------------------------------ Telegram gönderici
class FakeResp:
    def __init__(self, status=200):
        self.status_code = status
        self.ok = status == 200
        self.text = "{}"

    def json(self):
        return {"ok": self.ok}


def test_quiet_hours_silence_routine_but_not_critical(monkeypatch):
    sent = []
    n = TelegramNotifier("t" * 10, "1", async_mode=False, quiet_window=parse_time_range("00:00-23:59"))
    monkeypatch.setattr(n.session, "post", lambda url, json, timeout: sent.append(json) or FakeResp())
    n.send("rutin")
    n.send("stop-loss", critical=True)
    assert sent[0]["disable_notification"] is True
    assert sent[1]["disable_notification"] is False


def test_messages_survive_network_outage(monkeypatch):
    import requests
    import telegram_bot
    monkeypatch.setattr(telegram_bot, "_RETRY_DELAYS", (0.01,))
    calls = {"n": 0}
    sent = []

    def flaky(url, json, timeout):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise requests.ConnectionError("internet yok")
        sent.append(json["text"])
        return FakeResp()

    n = TelegramNotifier("t" * 10, "1")
    monkeypatch.setattr(n.session, "post", flaky)
    n.send("mesaj 1")
    n.send("mesaj 2")
    n.flush(timeout=5)
    assert sent == ["mesaj 1", "mesaj 2"]   # Kaybolmadı, sırası korundu


def test_secrets_redacted(monkeypatch):
    register_secrets("SUPERSECRETAPIKEY")
    assert "SUPERSECRET" not in redact("hata: key=SUPERSECRETAPIKEY")
    assert redact("url /bot123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAA/sendMessage") == "url /bot***/sendMessage"
    sent = []
    n = TelegramNotifier("t" * 10, "1", async_mode=False)
    monkeypatch.setattr(n.session, "post", lambda url, json, timeout: sent.append(json) or FakeResp())
    n.send("Hata: SUPERSECRETAPIKEY")
    assert "SUPERSECRET" not in sent[0]["text"]


# ------------------------------------------------------------------ komutlar
def test_parse_command_variants():
    assert parse_command("/Durum@KriptoBot").name == "durum"
    assert parse_command("/sat eth").args == ["eth"]
    assert parse_command("selam") is None


def test_pause_and_resume_commands(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    bot.commands.handle(Command("durdur"))
    assert bot.state.paused
    tick(bot)
    assert not bot.state.positions                       # Durdurulmuşken alım yok
    assert StateStore(tmp_path / "state.json").load().paused
    bot.commands.handle(Command("devam"))
    tick(bot)
    assert bot.state.positions


def test_sell_requires_confirmation_and_pin(cfg, fake_client, tmp_path):
    pinned = replace(cfg, command_pin="1234")
    bot, _, notes = make_bot(pinned, fake_client, tmp_path)
    tick(bot)
    bot.commands.handle(Command("sat", ["ABC"]))
    bot.commands.handle(Command("evet"))                 # PIN yok
    assert bot.state.positions
    bot.commands.handle(Command("evet", ["9999"]))       # Yanlış PIN
    assert bot.state.positions
    bot.commands.handle(Command("evet", ["1234"]))
    assert not bot.state.positions


def test_confirmation_expires(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    bot.commands.handle(Command("hepsinisat"))
    bot.commands.pending.expires = time.time() - 1
    bot.commands.handle(Command("evet"))
    assert bot.state.positions
    assert any("süresi doldu" in m for m in notes.messages)


def test_close_command_stops_bot_with_exit_0(cfg, fake_client, tmp_path):
    bot, _, _ = make_bot(cfg, fake_client, tmp_path)
    bot.commands.handle(Command("kapat"))
    assert bot.running
    bot.commands.handle(Command("evet"))
    assert not bot.running and bot.exit_code == 0


def test_info_commands_reply(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    tick(bot)
    for name in ("ping", "durum", "pozisyon", "bakiye", "tarama", "ayarlar", "yardim", "bilinmeyen"):
        before = len(notes.messages)
        bot.commands.handle(Command(name))
        assert len(notes.messages) > before, name
    joined = "\n".join(notes.messages)
    assert "Çalışıyorum" in joined and "Açık Pozisyonlar" in joined and "Son Tarama" in joined
    assert "Bilinmeyen komut" in joined


class FakeHttp:
    def __init__(self, updates):
        self.updates = updates
        self.calls = []

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        resp = FakeResp()
        if method == "getUpdates":
            data = {"ok": True, "result": self.updates if json.get("offset") != -1 else self.updates[-1:]}
        else:
            data = {"ok": True, "result": True}
        resp.json = lambda: data
        return resp


def test_listener_skips_backlog_and_rejects_strangers():
    backlog = [{"update_id": 5, "message": {"chat": {"id": 42}, "text": "/hepsinisat"}}]
    http = FakeHttp(backlog)
    lst = TelegramCommandListener("t" * 10, "42", http=http)
    lst._setup()
    assert lst._offset == 6                               # Eski komut atlandı
    lst._handle_update({"update_id": 6, "message": {"chat": {"id": 42}, "text": "/durum"}})
    lst._handle_update({"update_id": 7, "message": {"chat": {"id": 99}, "text": "/hepsinisat"}})
    cmds = lst.get_commands()
    assert [c.name for c in cmds] == ["durum"]
    assert any(m == "sendMessage" and j["chat_id"] == "99" for m, j in http.calls)
    assert any(m == "setMyCommands" for m, _ in http.calls)


# ------------------------------------------------------------------ ayrıntılı kayıt
def test_trade_log_has_detail_columns_and_migrates_old_file(cfg, fake_client, tmp_path):
    old = tmp_path / "trades.csv"
    old.write_text(",".join(TradeLog.BASE_HEADER) + "\n" +
                   "2026-01-01 10:00:00,2026-01-01 10:30:00,XUSDT,1,1,1.1,11,12,1,9.09,0.02,TAKE_PROFIT,30.0\n",
                   encoding="utf-8")
    bot, client, _ = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    client.prices["ABCUSDT"] = 1.16
    tick(bot)
    with old.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == TradeLog.HEADER
    assert rows[0]["parite"] == "XUSDT" and rows[0]["sinyal_puani"] == ""
    assert rows[1]["parite"] == "ABCUSDT"
    assert float(rows[1]["sinyal_puani"]) == pytest.approx(10.0)
    assert float(rows[1]["max_kar_pct"]) > 14


def test_equity_log(tmp_path):
    log = EquityLog(tmp_path / "eq.csv")
    log.append(time.time(), 11.0, -0.5, 1)
    text = (tmp_path / "eq.csv").read_text(encoding="utf-8")
    assert "toplam_bakiye" in text and "10.5000" in text


# ------------------------------------------------------------------ strateji filtreleri
def test_atr_filter_rejects_too_volatile(cfg):
    strict = replace(cfg, max_atr_pct=0.05)
    signal, reason = ReboundStrategy(None, strict).analyze("ABCUSDT", build_dip_and_bounce())
    assert signal is None and reason == "aşırı oynak"
    calm = replace(cfg, min_atr_pct=3.0, max_atr_pct=4.0)
    signal, reason = ReboundStrategy(None, calm).analyze("ABCUSDT", build_dip_and_bounce())
    assert signal is None and reason == "oynaklık düşük"


def test_weak_regime_is_stricter(cfg):
    signal, _ = ReboundStrategy(None, cfg).analyze("ABCUSDT", build_dip_and_bounce())
    assert signal is not None
    strict_cfg = replace(cfg, min_volume_ratio=1.7)       # x1.3 ile ~2.2 → toparlanma hacmi yetmez
    signal, reason = ReboundStrategy(None, strict_cfg).analyze("ABCUSDT", build_dip_and_bounce(), strict=True)
    assert signal is None


class ScanClient:
    def __init__(self, rows):
        self.rows = rows

    def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
        if symbol == "BTCUSDT":
            return [kline(i * 300_000, 100, 100, 100, 100) for i in range(13)]
        return self.rows

    def tickers_24h(self):
        return [{"symbol": "ABCUSDT", "lastPrice": "1.0", "priceChangePercent": "-6", "quoteVolume": "9000000",
                 "highPrice": "1.06", "lowPrice": "0.93"}]


def scan_rules(fake_client):
    from exchange import parse_symbol_rules
    return parse_symbol_rules(fake_client({"ABCUSDT": 1.0}).exchange_info())


def scaled_rows():
    return [[r[0], *(str(float(x) / 100) for x in r[1:5]), *r[5:]] for r in build_dip_and_bounce()]


def test_signal_confirmation_over_two_scans(cfg, fake_client):
    two = replace(cfg, signal_confirm_scans=2)
    strategy = ReboundStrategy(ScanClient(scaled_rows()), two)
    rules = scan_rules(fake_client)
    first = strategy.scan(rules, set(), 11)
    assert not first.signals and first.rejects["teyit bekleniyor"] == 1
    second = strategy.scan(rules, set(), 11)
    assert [s.symbol for s in second.signals] == ["ABCUSDT"]


def test_min_signal_score(cfg, fake_client):
    high = replace(cfg, min_signal_score=999)
    result = ReboundStrategy(ScanClient(scaled_rows()), high).scan(scan_rules(fake_client), set(), 11)
    assert not result.signals and result.rejects["puan düşük"] == 1


class FixedRegime:
    def __init__(self, regime):
        self.regime = regime

    def evaluate(self, tickers, rules):
        from types import SimpleNamespace
        return SimpleNamespace(regime=self.regime, note="test", btc_24h_pct=None, breadth_down_pct=None)


class BlockAll:
    def refresh_delist(self, force=False):
        pass

    def check(self, symbol):
        return False, "delist takviminde"

    def open_position_warnings(self, symbols):
        return []


def test_crash_regime_blocks_and_coin_filter_rejects(cfg, fake_client):
    rules = scan_rules(fake_client)
    crash = ReboundStrategy(ScanClient(scaled_rows()), cfg, regime=FixedRegime("COKUS")).scan(rules, set(), 11)
    assert not crash.market_ok and "çöküş" in crash.market_note
    blocked = ReboundStrategy(ScanClient(scaled_rows()), cfg, coin_filter=BlockAll()).scan(rules, set(), 11)
    assert not blocked.signals and blocked.rejects["delist takviminde"] == 1


def test_weak_regime_limits_to_one_position(cfg, fake_client, tmp_path):
    from test_bot_flow import PRICES3

    bot, _, _ = make_bot(cfg, fake_client, tmp_path, prices=PRICES3, capital=25.0)
    original = bot.strategy.scan

    def weak_scan(*a, **k):
        res = original(*a, **k)
        res.regime = "ZAYIF"
        return res

    bot.strategy.scan = weak_scan
    tick(bot)
    assert len(bot.state.positions) == 1


def test_exit_reason_values_are_stable():
    assert {"BREAKEVEN", "MANUAL", "BTC_CRASH", "BACKUP_STOP"} <= {r.value for r in ExitReason}


def test_capture_notifier_accepts_critical():
    CaptureNotifier().send("x", critical=True)


# ------------------------------------------------------------------ raporlar / istatistik
def _ts(y, mo, d, h, mi):
    from datetime import datetime
    return datetime(y, mo, d, h, mi).timestamp()


def test_daily_and_weekly_reports_sent_once(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    client.prices["ABCUSDT"] = 1.16
    tick(bot)                                            # Bir işlem kapansın
    bot.state.last_daily_report = "2026-10-09"
    bot.state.last_weekly_report = "2026-W40"
    now = _ts(2026, 10, 12, 0, 10)                       # Pazartesi 00:10 → dün (Pazar) raporu kaçırılmış
    before = len(notes.messages)
    bot._maybe_reports(now)
    new = notes.messages[before:]
    assert any("Günlük Özet (11.10.2026)" in m for m in new)
    assert any("Haftalık Özet" in m for m in new)
    assert bot.state.last_daily_report == "2026-10-11" and bot.state.last_weekly_report == "2026-W41"
    count = len(notes.messages)
    bot._maybe_reports(now + 60)
    assert len(notes.messages) == count                  # Tekrar gönderilmez
    bot._maybe_reports(_ts(2026, 10, 12, 23, 56))        # Bugünün rapor saati geldi
    assert any("Günlük Özet (12.10.2026)" in m for m in notes.messages[count:])


def test_no_report_flood_on_first_start(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    before = len(notes.messages)
    bot._maybe_reports(time.time())
    assert len(notes.messages) == before


def test_stats_commands(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    bot.commands.handle(Command("istatistik"))
    assert "Henüz kapanmış işlem yok" in notes.messages[-1]
    tick(bot)
    client.prices["ABCUSDT"] = 1.16
    tick(bot)
    for name, args in (("istatistik", []), ("islemler", []), ("rapor", ["gun"]), ("rapor", ["hafta"]),
                       ("rapor", ["ay"])):
        before = len(notes.messages)
        bot.commands.handle(Command(name, args))
        assert len(notes.messages) == before + 1, name
        assert "Hata" not in notes.messages[-1], notes.messages[-1]
    assert "ABCUSDT" in notes.messages[-4]                # /islemler son işlemi gösterir


def test_stats_optional_columns_match_trade_log():
    import stats
    assert tuple(TradeLog.EXTRA_HEADER) == stats.OPTIONAL_COLUMNS
    assert tuple(TradeLog.BASE_HEADER) == stats.BASE_COLUMNS


# ------------------------------------------------------------------ komut satırı
def test_main_exit_codes(monkeypatch, tmp_path):
    import main
    monkeypatch.setenv("MAX_TOTAL_LOSS_PCT", "150")       # %100'den büyük kill-switch → ayar hatası
    assert main.main(["--env", str(tmp_path / "yok.env")]) == main.EXIT_CONFIG


def test_main_stats_cli_without_trades(monkeypatch, tmp_path, capsys):
    import main
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    assert main.main(["--env", str(tmp_path / "yok.env"), "--istatistik"]) == main.EXIT_OK
    assert "Henüz kapanmış işlem yok" in capsys.readouterr().out


def test_room_filter_rejects_signals_without_room_to_target(cfg, fake_client):
    from strategy import required_move_pct, room_ok
    signal, _ = ReboundStrategy(None, cfg).analyze("ABCUSDT", build_dip_and_bounce())
    room = signal.metrics["room_pct"]
    assert 0 < room < 10                                   # ~%6.5 düşüş, kısmen toparlanmış
    assert room_ok(signal, cfg)                            # Varsayılan: filtre kapalı
    need = required_move_pct(replace(cfg, take_profit_pct=2))
    assert 2.19 < need < 2.21                              # Net %2 için ~%2.2 fiyat hareketi (komisyon dahil)
    assert room_ok(signal, replace(cfg, take_profit_pct=room / 3, min_room_ratio=1))
    assert not room_ok(signal, replace(cfg, take_profit_pct=room, min_room_ratio=1))
    strict = replace(cfg, take_profit_pct=15, min_room_ratio=1)
    result = ReboundStrategy(ScanClient(scaled_rows()), strict).scan(scan_rules(fake_client), set(), 11)
    assert not result.signals and result.rejects["hedef alanı dar"] == 1
    assert any("hedef alanı dar" in why for _, why in result.near_misses)


def test_live_scan_skips_weak_market_when_configured(cfg, fake_client):
    weak = ReboundStrategy(ScanClient(scaled_rows()), cfg, regime=FixedRegime("ZAYIF")).scan(
        scan_rules(fake_client), set(), 11)
    assert weak.market_ok                                    # Varsayılan: zayıfta sıkı kurallarla devam
    skip = ReboundStrategy(ScanClient(scaled_rows()), replace(cfg, regime_skip_weak=True),
                           regime=FixedRegime("ZAYIF")).scan(scan_rules(fake_client), set(), 11)
    assert not skip.market_ok and not skip.signals and "REGIME_SKIP_WEAK" in skip.market_note
