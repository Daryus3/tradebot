"""Kurulum sihirbazı testleri: sahte Telegram/Binance ile senaryolu girdiler (internet gerekmez)."""
import io
import os
import re
import time
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values

from binance_client import BinanceAPIError
from setup_wizard import mask, parse_env, render_env, run_wizard

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_TEXT = (ROOT / ".env.example").read_text(encoding="utf-8")

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
TOKEN_UNKNOWN = "987654321:BBHdqTcvCH1vGWJxfSeofSAs0K5PALDsXY"
API_KEY = "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"
API_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
CHAT_PRIVATE = "555111222"
CHAT_GROUP = "-1001234567890"
OK_RESTR = {"enableReading": True, "enableSpotAndMarginTrading": True, "enableWithdrawals": False,
            "ipRestrict": True}


# ------------------------------------------------------------------ sahteler
class FakeResp:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status

    def json(self):
        return self.data


class FakeHttp:
    """Telegram Bot API'nin requests benzeri sahtesi."""

    def __init__(self, updates=None, empty_polls=0, send_results=None, offline=False):
        self.bots = {TOKEN: "kripto_bot"}
        self.updates = updates or []
        self.empty_polls = empty_polls
        self.send_results = list(send_results or [])
        self.offline = offline
        self.polls = 0
        self.sent = []
        self.calls = []

    @staticmethod
    def _split(url):
        m = re.match(r"https://api\.telegram\.org/bot(.+)/(\w+)$", url)
        assert m, url
        return m.group(1), m.group(2)

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        assert timeout, "zaman aşımı verilmeli"
        if self.offline:
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
        token, method = self._split(url)
        if token not in self.bots:
            return FakeResp({"ok": False, "error_code": 401, "description": "Unauthorized"}, 401)
        if method == "getMe":
            return FakeResp({"ok": True, "result": {"id": 1, "is_bot": True, "username": self.bots[token]}})
        if method == "getUpdates":
            self.polls += 1
            return FakeResp({"ok": True, "result": [] if self.polls <= self.empty_polls else self.updates})
        raise AssertionError(f"beklenmeyen metod {method}")

    def post(self, url, json=None, timeout=None):
        self.calls.append(url)
        if self.offline:
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
        _, method = self._split(url)
        assert method == "sendMessage"
        self.sent.append(json)
        if self.send_results:
            return FakeResp(self.send_results.pop(0))
        return FakeResp({"ok": True, "result": {"message_id": 7}})


class FakeBinance:
    """binance_factory olarak kullanılır: çağrılınca kendini (istemci) döndürür."""

    def __init__(self, free="25.5", restrictions=None, account_errors=None, restr_error=None):
        self.free = free
        self.restrictions = list(restrictions or [OK_RESTR])
        self.account_errors = list(account_errors or [])
        self.restr_error = restr_error
        self.created = []
        self.restr_calls = 0

    def __call__(self, key, secret):
        self.created.append((key, secret))
        return self

    def account(self):
        if self.account_errors:
            raise self.account_errors.pop(0)
        return {"canTrade": True, "balances": [{"asset": "BTC", "free": "0.001", "locked": "0"},
                                               {"asset": "USDT", "free": self.free, "locked": "0"}]}

    def api_restrictions(self):
        if self.restr_error:
            raise self.restr_error
        self.restr_calls += 1
        return self.restrictions[min(self.restr_calls, len(self.restrictions)) - 1]


def chat_update(chat_id, first_name=None, title=None, kind="private"):
    chat = {"id": int(chat_id), "type": kind}
    if first_name:
        chat["first_name"] = first_name
    if title:
        chat["title"] = title
    return {"update_id": 1, "message": {"message_id": 1, "chat": chat, "text": "/start"}}


class Session:
    """Sihirbazı senaryolu cevaplarla çalıştırır; çıktıyı ve soruları kaydeder."""

    def __init__(self, tmp_path, answers, http=None, binance=None, old_env=None, example=True):
        self.env = tmp_path / ".env"
        self.example = tmp_path / ".env.example"
        if example:
            self.example.write_text(EXAMPLE_TEXT, encoding="utf-8")
        if old_env is not None:
            self.env.write_text(old_env, encoding="utf-8")
        self.dir = tmp_path
        self.answers = list(answers)
        self.prompts, self.out, self.sleeps = [], [], []
        self.clock = [1_760_000_000.0]
        self.http = http or FakeHttp()
        self.binance = binance or FakeBinance()

    def _input(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"Beklenmeyen soru: {prompt!r}")
        answer = self.answers.pop(0)
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer()
        return answer

    def _print(self, *args, **kwargs):
        self.out.append(" ".join(str(a) for a in args))

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock[0] += seconds

    def run(self, all_answers_used=True):
        result = run_wizard(env_path=self.env, example_path=self.example, input_fn=self._input,
                            print_fn=self._print, http=self.http, sleep_fn=self._sleep,
                            binance_factory=self.binance, now_fn=lambda: self.clock[0])
        if all_answers_used:
            assert not self.answers, f"Kullanılmayan cevaplar: {self.answers}"
        return result

    @property
    def text(self):
        return "\n".join(self.out + self.prompts)

    def values(self):
        return parse_env(self.env.read_text(encoding="utf-8"))

    def backups(self):
        return sorted(self.dir.glob(".env.bak-*"))


# ------------------------------------------------------------------ saf fonksiyonlar
def test_mask():
    assert mask(TOKEN) == "1234****"
    assert mask(API_SECRET) == "NhqP****"
    assert mask("") == "(boş)"
    assert mask("abc") == "****"        # kısa değer hiç gösterilmez
    assert mask("12345678") == "****"


def test_parse_env():
    text = ("\ufeff# yorum\n\nA=1\r\nexport B = iki \nC='tek tırnak # yorum değil'\n"
            "D=\"çift \\\"tırnak\\\"\\nsatır\"\nE=deger   # satır sonu yorumu\nF=a#b\n"
            "bozuk satır\n=deger\nG=\n  H = boşluklu \n")
    assert parse_env(text) == {"A": "1", "B": "iki", "C": "tek tırnak # yorum değil",
                               "D": 'çift "tırnak"\nsatır', "E": "deger", "F": "a#b", "G": "",
                               "H": "boşluklu"}


def test_render_env_keeps_comments_and_appends_extras():
    example = "# Başlık\nA=1\n# açıklama\nB=\n\nC=varsayılan\n# D=yorumdaki\n\n"
    out = render_env(example, {"B": "yeni", "A": "5", "X": "ekstra", "Y": "iki kelime"})
    assert out == ("# Başlık\nA=5\n# açıklama\nB=yeni\n\nC=varsayılan\n# D=yorumdaki\n\n"
                   "# Diğer ayarlar\nX=ekstra\nY='iki kelime'\n")
    # Ek anahtar yoksa başlık da eklenmez
    assert "Diğer ayarlar" not in render_env(example, {"A": "2"})


def test_render_env_roundtrip_matches_python_dotenv():
    values = {"A": "p@ss w0rd", "B": "has #hash", "C": "it's", "D": 'quote"d', "E": "$HOME 5$",
              "F": "back\\slash", "G": "23:00-08:00", "H": "", "I": "PEPE,SHIB", "J": "çok güzel"}
    text = render_env("A=\nB=\n", values)
    assert parse_env(text) == values
    assert dict(dotenv_values(stream=io.StringIO(text))) == values  # bot da aynı değerleri okur


# ------------------------------------------------------------------ uçtan uca senaryolar
def test_fresh_install_full_flow(tmp_path):
    http = FakeHttp(updates=[chat_update(CHAT_PRIVATE, first_name="Ali")], empty_polls=2)
    s = Session(tmp_path, [TOKEN, "", API_KEY, API_SECRET, ""], http=http)
    assert s.run() is True

    vals = s.values()
    assert vals["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert vals["TELEGRAM_CHAT_ID"] == CHAT_PRIVATE
    assert vals["BINANCE_API_KEY"] == API_KEY and vals["BINANCE_API_SECRET"] == API_SECRET
    assert vals["TRADING_MODE"] == "paper"
    assert vals["START_CAPITAL_USDT"] == "11"            # kilitli kurallar örnekteki gibi
    assert vals["TAKE_PROFIT_PCT"] == "15" and vals["STOP_LOSS_PCT"] == "10"
    content = s.env.read_text(encoding="utf-8")
    assert "KRİPTO AL-SAT BOTU AYARLARI" in content and "Enable Withdrawals" in content  # yorumlar korundu
    assert "Diğer ayarlar" not in content
    assert dict(dotenv_values(s.env)) == vals
    assert s.backups() == []                              # eski dosya yoktu
    if os.name != "nt":
        assert (s.env.stat().st_mode & 0o777) == 0o600

    # getUpdates 3 sn arayla yoklandı, test mesajı doğru sohbete gitti
    assert http.polls == 3 and s.sleeps == [3, 3]
    assert http.sent == [{"chat_id": CHAT_PRIVATE, "text": http.sent[0]["text"]}]
    assert "Kurulum başarılı" in http.sent[0]["text"]
    assert s.binance.created == [(API_KEY, API_SECRET)]

    out = s.text
    assert "@kripto_bot" in out and "Ali" in out
    assert "25.50" in out and "USDT" in out
    assert "Para çekme izni kapalı" in out
    for step in ("TEST_ET.bat", "BASLAT.bat", "CANLI_BASLAT.bat", "--selftest"):
        assert step in out
    # Gizli bilgiler asla tam yazdırılmaz
    assert API_SECRET not in out and TOKEN not in out and API_KEY not in out
    # Kilitli risk kuralları sorulmaz
    assert not any(w in p.upper() for p in s.prompts for w in ("TAKE", "STOP", "KILL", "SERMAYE"))


def test_existing_env_values_kept_merged_and_backed_up(tmp_path):
    old = ("# eski dosyam\nTELEGRAM_BOT_TOKEN=" + TOKEN + "\nTELEGRAM_CHAT_ID=" + CHAT_PRIVATE + "\n"
           "TRADING_MODE=live\nSTART_CAPITAL_USDT=20\nQUIET_HOURS=23:00-08:00\nMY_CUSTOM='bir iki'\n"
           "BINANCE_API_KEY=\nBINANCE_API_SECRET=\n")
    s = Session(tmp_path, ["", "", "", ""], old_env=old)   # token koru, sohbet koru, Binance atla, kaydet
    assert s.run() is True

    # Yedek: .env.bak-YYYYmmdd-HHMMSS, içerik birebir aynı
    backups = s.backups()
    assert len(backups) == 1
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(s.clock[0]))
    assert backups[0].name == f".env.bak-{stamp}"
    assert re.fullmatch(r"\.env\.bak-\d{8}-\d{6}", backups[0].name)
    assert backups[0].read_text(encoding="utf-8") == old

    vals = s.values()
    assert vals["TELEGRAM_BOT_TOKEN"] == TOKEN and vals["TELEGRAM_CHAT_ID"] == CHAT_PRIVATE
    assert vals["START_CAPITAL_USDT"] == "20"             # eski değerler taşındı
    assert vals["QUIET_HOURS"] == "23:00-08:00" and vals["MY_CUSTOM"] == "bir iki"
    assert vals["TRADING_MODE"] == "paper"                # canlı mod sihirbazla açılmaz
    assert vals["MAX_OPEN_POSITIONS"] == "3"              # örnekteki varsayılan
    content = s.env.read_text(encoding="utf-8")
    tail = content.split("# Diğer ayarlar", 1)[1]
    # .env.example'da olan ayar kendi yerinde, olmayan özel ayar "Diğer ayarlar" altında korunur
    assert "QUIET_HOURS=23:00-08:00" in content and "MY_CUSTOM='bir iki'" in tail
    assert "KRİPTO AL-SAT BOTU AYARLARI" in content

    out = s.text
    assert "1234****" in out and "5551****" in out        # mevcut değerler maskeli gösterildi
    assert TOKEN not in out
    assert "TRADING_MODE=live" in out and "CANLI_BASLAT.bat" in out
    assert len(s.http.sent) == 1                          # kayıtlı sohbete test mesajı
    assert s.binance.created == []                        # Binance atlandı, bağlanılmadı


def test_missing_example_uses_old_env_as_template(tmp_path):
    old = "# eski dosyam\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\nSTART_CAPITAL_USDT=30\n"
    s = Session(tmp_path, ["", "", ""], old_env=old, example=False)
    assert s.run() is True
    content = s.env.read_text(encoding="utf-8")
    assert content.startswith("# eski dosyam\n")
    vals = s.values()
    assert vals["START_CAPITAL_USDT"] == "30" and vals["TRADING_MODE"] == "paper"


def test_skip_telegram_and_binance(tmp_path):
    s = Session(tmp_path, ["", "", ""])
    assert s.run() is True
    vals = s.values()
    assert vals["TELEGRAM_BOT_TOKEN"] == "" and vals["TELEGRAM_CHAT_ID"] == ""
    assert vals["BINANCE_API_KEY"] == "" and vals["TRADING_MODE"] == "paper"
    assert "GÖNDEREMEZ" in s.text and "test modu için gerekmez" in s.text
    assert s.http.calls == []


def test_invalid_token_is_asked_again(tmp_path):
    answers = ["123:abc",              # biçim hatalı → ağ çağrısı yok
               TOKEN_UNKNOWN,          # Telegram 401 döner
               f'"bot{TOKEN}"',        # tırnaklı ve 'bot' önekli yapıştırma da kabul
               "a",                    # sohbeti atla
               "", ""]
    s = Session(tmp_path, answers)
    assert s.run() is True
    assert s.values()["TELEGRAM_BOT_TOKEN"] == TOKEN
    out = s.text
    assert "benzemiyor" in out and "kabul etmedi" in out and "@kripto_bot" in out
    assert sum("getMe" in c for c in s.http.calls) == 2


def test_invalid_saved_token_enter_then_means_skip(tmp_path):
    old = f"TELEGRAM_BOT_TOKEN={TOKEN_UNKNOWN}\nTELEGRAM_CHAT_ID={CHAT_PRIVATE}\n"
    s = Session(tmp_path, ["", "", "", ""], old_env=old)  # Enter: kayıtlı (geçersiz) token; Enter: atla
    assert s.run() is True
    assert s.values()["TELEGRAM_BOT_TOKEN"] == ""
    assert "kabul etmedi" in s.text


def test_chat_id_multiple_chats_user_chooses(tmp_path):
    http = FakeHttp(updates=[chat_update(CHAT_PRIVATE, first_name="Ali"),
                             chat_update(CHAT_GROUP, title="Aile Grubu", kind="supergroup"),
                             chat_update(CHAT_PRIVATE, first_name="Ali")])
    s = Session(tmp_path, [TOKEN, "", "7", "2", "", ""], http=http)
    assert s.run() is True
    assert s.values()["TELEGRAM_CHAT_ID"] == CHAT_GROUP
    assert http.sent[0]["chat_id"] == CHAT_GROUP
    out = s.text
    assert "2 sohbet bulundu" in out and "Ali" in out and "Aile Grubu" in out
    assert "1 ile 2 arasında" in out


def test_chat_not_found_after_two_minutes_then_manual(tmp_path):
    s = Session(tmp_path, [TOKEN, "", CHAT_PRIVATE, "", ""])
    assert s.run() is True
    assert s.http.polls == 41                       # ~2 dk boyunca 3 sn arayla
    assert set(s.sleeps) == {3} and sum(s.sleeps) == 120
    assert "bulunamadı" in s.text and "Hâlâ bekleniyor" in s.text
    assert s.values()["TELEGRAM_CHAT_ID"] == CHAT_PRIVATE
    assert s.http.sent[0]["chat_id"] == CHAT_PRIVATE


def test_saved_chat_test_message_fails_then_rediscovered(tmp_path):
    old = f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=111222333\n"
    http = FakeHttp(updates=[chat_update(CHAT_PRIVATE, first_name="Ali")],
                    send_results=[{"ok": False, "error_code": 403,
                                   "description": "Forbidden: bot can't initiate conversation with a user"}])
    s = Session(tmp_path, ["", "", "", "", ""], http=http, old_env=old)
    assert s.run() is True
    assert [m["chat_id"] for m in http.sent] == ["111222333", CHAT_PRIVATE]
    assert "/start" in s.text and "Forbidden" in s.text
    assert s.values()["TELEGRAM_CHAT_ID"] == CHAT_PRIVATE


def test_saved_chat_rediscover_with_b(tmp_path):
    old = f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=111222333\n"
    http = FakeHttp(updates=[chat_update(CHAT_PRIVATE, first_name="Ali")])
    s = Session(tmp_path, ["", "b", "", ""], http=http, old_env=old)
    assert s.run() is True
    assert s.values()["TELEGRAM_CHAT_ID"] == CHAT_PRIVATE


def test_telegram_offline_keeps_token_and_manual_chat(tmp_path):
    s = Session(tmp_path, [TOKEN, "abc", CHAT_PRIVATE, "", ""], http=FakeHttp(offline=True))
    assert s.run() is True
    vals = s.values()
    assert vals["TELEGRAM_BOT_TOKEN"] == TOKEN and vals["TELEGRAM_CHAT_ID"] == CHAT_PRIVATE
    out = s.text
    assert "bağlanılamadı" in out and "doğrulanamadı" in out
    assert TOKEN not in out                         # hata mesajındaki URL'de token maskelendi
    assert "1234****" in out


# ------------------------------------------------------------------ Binance
def test_withdrawal_permission_strong_warning_and_recheck(tmp_path):
    binance = FakeBinance(restrictions=[{**OK_RESTR, "enableWithdrawals": True}, OK_RESTR])
    s = Session(tmp_path, ["", API_KEY, API_SECRET, "", ""], binance=binance)
    assert s.run() is True
    out = s.text
    assert "PARA ÇEKME" in out and "Enable Withdrawals" in out and "KAPATIN" in out
    assert binance.restr_calls == 2
    assert "Para çekme izni kapalı" in out
    assert s.values()["BINANCE_API_KEY"] == API_KEY
    assert API_SECRET not in out


def test_withdrawal_left_open_and_spot_trading_disabled(tmp_path):
    bad = {**OK_RESTR, "enableWithdrawals": True, "enableSpotAndMarginTrading": False}
    binance = FakeBinance(restrictions=[bad], free="3")
    s = Session(tmp_path, ["", API_KEY, API_SECRET, "", "devam", ""], binance=binance)
    assert s.run() is True
    out = s.text
    assert out.count("Enable Spot & Margin Trading' izni KAPALI") == 1
    assert out.count("PARA ÇEKME (Withdrawals) izni AÇIK") == 2
    assert "AÇIK bırakıldı" in out
    assert "Spot'a aktarın" in out                  # 3 USDT < 11 USDT sermaye
    assert s.values()["BINANCE_API_SECRET"] == API_SECRET


def test_binance_network_error_warns_but_keeps_keys(tmp_path):
    binance = FakeBinance(account_errors=[requests.ConnectionError("bağlantı zaman aşımı")])
    s = Session(tmp_path, ["", API_KEY, API_SECRET, ""], binance=binance)
    assert s.run() is True
    vals = s.values()
    assert vals["BINANCE_API_KEY"] == API_KEY and vals["BINANCE_API_SECRET"] == API_SECRET
    assert "Binance'e bağlanılamadı" in s.text and "doğrulanamadı" in s.text


def test_binance_rejected_key_asked_again(tmp_path):
    err = BinanceAPIError(401, -2015, "Invalid API-key, IP, or permissions for action.")
    binance = FakeBinance(account_errors=[err])
    s = Session(tmp_path, ["", "yanlisanahtar", "yanlissecret", API_KEY, API_SECRET, ""], binance=binance)
    assert s.run() is True
    assert binance.created == [("yanlisanahtar", "yanlissecret"), (API_KEY, API_SECRET)]
    assert s.values()["BINANCE_API_KEY"] == API_KEY
    out = s.text
    assert "kabul etmedi" in out and "beklenen biçimde değil" in out
    assert "yanlissecret" not in out


def test_binance_rejected_then_skip_keeps_old_values(tmp_path):
    old = "BINANCE_API_KEY=eskianahtar123\nBINANCE_API_SECRET=eskisecret456\n"
    err = BinanceAPIError(400, -1022, "Signature for this request is not valid.")
    binance = FakeBinance(account_errors=[err])
    # Telegram atla, Binance: Enter (kayıtlı key), Enter (kayıtlı secret) → reddedildi → Enter = atla
    s = Session(tmp_path, ["", "", "", "", ""], binance=binance, old_env=old)
    assert s.run() is True
    assert binance.created == [("eskianahtar123", "eskisecret456")]
    vals = s.values()
    assert vals["BINANCE_API_KEY"] == "eskianahtar123" and vals["BINANCE_API_SECRET"] == "eskisecret456"
    assert "eskisecret456" not in s.text and "eski****" in s.text


def test_new_key_requires_new_secret(tmp_path):
    old = "BINANCE_API_KEY=eskianahtar123\nBINANCE_API_SECRET=eskisecret456\n"
    s = Session(tmp_path, ["", API_KEY, "", ""], old_env=old)  # yeni key, secret boş → değişmez
    assert s.run() is True
    assert s.values()["BINANCE_API_KEY"] == "eskianahtar123"
    assert s.binance.created == []
    assert "Secret girilmedi" in s.text


# ------------------------------------------------------------------ iptal
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
def test_cancel_writes_nothing(tmp_path, interrupt):
    old = f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT_PRIVATE}\nSTART_CAPITAL_USDT=20\n"
    s = Session(tmp_path, ["", "", interrupt], old_env=old)   # Binance sorusunda Ctrl+C
    assert s.run() is False
    assert s.env.read_text(encoding="utf-8") == old
    assert s.backups() == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", ".env.example"]
    assert "iptal" in s.text


def test_cancel_on_fresh_install_creates_no_env(tmp_path):
    s = Session(tmp_path, [KeyboardInterrupt])
    assert s.run() is False
    assert not s.env.exists()


def test_declining_save_writes_nothing(tmp_path):
    s = Session(tmp_path, ["", "", "h"])
    assert s.run() is False
    assert not s.env.exists() and s.backups() == []
    assert "Kaydedilmedi" in s.text
