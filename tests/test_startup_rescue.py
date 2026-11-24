"""Açılış bekleme modu (rescue.py, STARTUP_RETRY_MINUTES): vadeli bot açılışta Binance'e bağlanamazsa kapanmaz; Telegram
açık kalır (/ip, /dene, /durum, /kapat), belli aralıklarla yeniden dener ve bağlantı düzelince normal açılışa devam
eder. Sahte dinleyici / bildirimci / borsa ile; gerçek istek yok."""
import logging
from dataclasses import replace

import pytest

import futures_exchange
import futures_trader
import main
import rescue
from commands import CALLBACK, Command
from config import ConfigError, load_config
from futures_exchange import FuturesError
from runtime_settings import EDITABLE
from state import StateStore
from test_bot_flow import CaptureNotifier
from test_futures import fcfg  # noqa: F401 - pytest fikstürü

IP_ERR = ('Vadeli pariteler yüklenemedi: binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for '
          'action."}')
IPS = {"IPv4": "85.10.20.30", "IPv6": "2a02::1"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeListener:
    """Belirli anlarda komut veren sahte Telegram dinleyicisi (wakeup yok: bekleme sahte saatle ilerler)."""

    def __init__(self, clock, script=()):
        self.clock = clock
        self.script = sorted(script, key=lambda x: x[0])
        self.started = 0

    def start(self):
        self.started += 1

    def get_commands(self):
        out = [cmd for at, cmd in self.script if at <= self.clock.t]
        self.script = [(at, cmd) for at, cmd in self.script if at > self.clock.t]
        return out


def flaky(fails, error=IP_ERR, value="BOT"):
    calls = {"n": 0}

    def attempt():
        calls["n"] += 1
        if calls["n"] <= fails:
            raise FuturesError(error)
        return value
    return attempt, calls


def make(clock, script=(), retry=120.0, remind=6 * 3600):
    notes = CaptureNotifier()
    listener = FakeListener(clock, script)
    r = rescue.StartupRescue(notes, listener, retry, time_fn=clock, sleep_fn=clock.sleep, ips_fn=lambda: dict(IPS),
                             remind_sec=remind)
    return r, notes, listener


def test_retries_on_its_own_and_continues_when_the_connection_is_fixed():
    clock = Clock()
    r, notes, listener = make(clock)
    attempt, calls = flaky(3)                       # İlk açılış + 2 otomatik deneme başarısız, 3.'sü başarılı
    first = None
    try:
        attempt()
    except FuturesError as exc:
        first = exc
    assert r.run(attempt, first, futures_exchange.describe_error, (FuturesError,)) == "BOT"
    assert calls["n"] == 4 and listener.started == 1
    assert clock.t - 1000.0 == pytest.approx(3 * 120.0, abs=3)   # Her 2 dakikada bir
    text, keyboard, _mid = notes.menus[0]
    assert "🛟 <b>Bekleme modu</b>" in text and "Güvenilir IP" in text and "85.10.20.30" in text
    assert "2 dk'da bir yeniden deniyor" in text
    assert [b[1] for row in keyboard for b in row] == ["rs:ip", "rs:dene", "rs:durum", "rs:kapat"]
    assert len(notes.menus) == 1                      # Aynı hata: her denemede mesaj yağmuru yok
    assert "✅ <b>Binance bağlantısı düzeldi</b> (4. deneme" in notes.messages[-1]


def test_commands_ip_retry_status_help_and_close():
    clock = Clock()
    script = [(1001, Command("ip")), (1005, Command("dene")), (1010, Command("durum")), (1015, Command("pozisyon")),
              (1020, Command(CALLBACK, ["rs", "kapat"], kind="callback")),
              (1025, Command(CALLBACK, ["rs", "kapat", "evet"], kind="callback"))]
    r, notes, _listener = make(clock, script)
    attempt, calls = flaky(100)
    assert r.run(attempt, FuturesError(IP_ERR), futures_exchange.describe_error, (FuturesError,)) is None
    texts = notes.messages
    assert any("Sunucunun şu anki dış IP adresi" in t and "85.10.20.30" in t and "2a02::1" in t for t in texts)
    assert calls["n"] == 1                            # /dene: beklemeden hemen yeniden denendi
    assert any(t.startswith("❌ Yine bağlanılamadı (2. deneme)") for t in texts)
    assert any("dk'dır Binance'e bağlanılamıyor (2 deneme)" in t and "Sonraki otomatik deneme" in t for t in texts)
    assert any(t.startswith("Bekleme modunda çalışan komutlar") for t in texts)          # Bilinmeyen komut
    assert any("Bot kapatılsın mı?" in t for t in texts)
    assert "⏹️ <b>Bot kapatıldı</b>" in texts[-1]


def test_typed_close_needs_confirmation_and_error_changes_are_reported():
    clock = Clock()
    r, notes, _listener = make(clock, [(1200, Command("kapat")), (1300, Command("kapat", ["evet"]))])
    errors = iter([FuturesError("HTTPSConnectionPool: Read timed out")] * 10)

    def attempt():
        raise next(errors)
    assert r.run(attempt, FuturesError(IP_ERR), futures_exchange.describe_error, (FuturesError,)) is None
    changed = [t for t in notes.messages if t.startswith("⚠️ Bağlantı hâlâ kurulamadı, hata değişti")]
    assert changed and "internet bağlantısı" in changed[0]


def test_reminder_while_waiting_long():
    clock = Clock()
    r, notes, _listener = make(clock, [(1000 + 3 * 3600, Command("kapat", ["evet"]))], retry=600, remind=3600)
    attempt, _calls = flaky(1000)
    r.run(attempt, FuturesError(IP_ERR), futures_exchange.describe_error, (FuturesError,))
    assert sum(1 for t in notes.messages if t.startswith("⏳ Hâlâ bekleme modunda")) == 2


def test_hints_for_common_problems():
    assert "Güvenilir IP" in rescue.problem_hint(IP_ERR)
    assert "USDⓈ-M Vadeli" in rescue.problem_hint("Vadeli cüzdanınızda serbest USDT yok")
    assert "saati" in rescue.problem_hint('binance {"code":-1021,"msg":"Timestamp for this request is outside"}')
    assert ".env" in rescue.problem_hint('binance {"code":-2014,"msg":"API-key format invalid."}')
    assert "internet" in rescue.problem_hint("Connection reset by peer")


# ====================================================================== main.py: vadeli açılış
class OnNotifier(CaptureNotifier):
    @property
    def enabled(self):
        return True


class FakeBot:
    starts = 0

    def __init__(self, *a, **k):
        self.runs = 0

    def startup(self):
        FakeBot.starts += 1
        if FakeBot.starts <= FakeBot.fail:
            raise FuturesError(IP_ERR)

    def run(self, do_startup=True):
        self.runs += 1
        return 0


def _patch(monkeypatch, fails, script):
    FakeBot.starts, FakeBot.fail = 0, fails
    holder = {}

    class Listener(FakeListener):
        def __init__(self, token, chat_id):
            super().__init__(Clock(), [(0, cmd) for cmd in script])
            holder["listener"] = self
    import commands
    monkeypatch.setattr(commands, "TelegramCommandListener", Listener)
    monkeypatch.setattr(futures_exchange, "make_exchange", lambda cfg, state: object())
    monkeypatch.setattr(futures_trader, "FuturesBot", FakeBot)
    monkeypatch.setattr(rescue, "fetch_ips", lambda: dict(IPS))
    monkeypatch.setattr(main, "_graceful_sigterm", lambda: None)
    return holder


def test_futures_start_enters_waiting_mode_instead_of_exiting(fcfg, monkeypatch, tmp_path):
    _patch(monkeypatch, 1, [Command("dene")])
    cfg = replace(fcfg, telegram_commands=True)
    notes = OnNotifier()
    code = main._start_futures_bot(cfg, StateStore(tmp_path / "s.json"), notes, logging.getLogger("t"))
    assert code == 0 and FakeBot.starts == 2
    assert any("🛟 <b>Bekleme modu</b>" in t for t in notes.messages)
    assert any("Binance bağlantısı düzeldi" in t for t in notes.messages)
    assert not any("Bot durdu" in t for t in notes.messages)


def test_futures_start_can_be_closed_from_telegram(fcfg, monkeypatch, tmp_path):
    _patch(monkeypatch, 99, [Command("kapat", ["evet"])])
    notes = OnNotifier()
    code = main._start_futures_bot(replace(fcfg, telegram_commands=True), StateStore(tmp_path / "s.json"), notes,
                                   logging.getLogger("t"))
    assert code == main.EXIT_OK and FakeBot.starts == 1
    assert "⏹️ <b>Bot kapatıldı</b>" in notes.messages[-1]


def test_old_behaviour_when_disabled_or_without_telegram(fcfg, monkeypatch, tmp_path):
    _patch(monkeypatch, 99, [])
    notes = OnNotifier()
    off = replace(fcfg, telegram_commands=True, startup_retry_minutes=0)
    assert main._start_futures_bot(off, StateStore(tmp_path / "a.json"), notes, logging.getLogger("t")) == \
        main.EXIT_CONFIG
    assert "⛔ Bot durdu." in notes.messages[-1] and "Enable Futures" in notes.messages[-1]
    quiet = CaptureNotifier()                         # Telegram yok: beklemenin anlamı yok, eskisi gibi kapanır
    assert main._start_futures_bot(replace(fcfg, telegram_commands=True), StateStore(tmp_path / "b.json"), quiet,
                                   logging.getLogger("t")) == main.EXIT_CONFIG


def test_setting_defaults_validation_env_and_runtime(fcfg, monkeypatch, tmp_path):
    assert fcfg.startup_retry_minutes == 2
    for bad in (-1, 61):
        with pytest.raises(ConfigError):
            replace(fcfg, startup_retry_minutes=bad).validate()
    monkeypatch.setenv("STARTUP_RETRY_MINUTES", "5")
    assert load_config(env_file=str(tmp_path / "x.env"), mode_override="paper").startup_retry_minutes == 5
    assert EDITABLE["STARTUP_RETRY_MINUTES"].attr == "startup_retry_minutes"
