"""
Kurulum sihirbazı:  python main.py --kurulum

Teknik bilgisi olmayan kullanıcıyı adım adım yönlendirir:
  1. Telegram bot token'ı (getMe ile doğrulanır)
  2. Telegram chat ID (bota /start yazılınca otomatik bulunur, test mesajı gönderilir)
  3. Binance API anahtarları (isteğe bağlı; bakiye ve API izinleri kontrol edilir)
  4. .env dosyası yazılır (eski dosya yedeklenir, diğer tüm ayarlar korunur)

Bot her zaman TEST modunda (TRADING_MODE=paper) kurulur; canlı mod CANLI_BASLAT.bat ile açılır.
Kâr al / zarar kes / kill-switch gibi kilitli kurallar burada SORULMAZ.
Ctrl+C ile iptal edilirse hiçbir dosya değiştirilmez.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from binance_client import BinanceAPIError, BinanceClient, RateLimitError
from config import BASE_DIR

TG_API = "https://api.telegram.org/bot{token}/{method}"
HTTP_TIMEOUT = 10
POLL_SECONDS = 120        # getUpdates en fazla bu kadar süre yoklanır (~2 dk)
POLL_INTERVAL = 3         # yoklama aralığı (sn)
TEST_MESSAGE = "✅ Kurulum başarılı!\n🤖 Kripto Al-Sat Botu bildirimleri bu sohbete gelecek."
LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

K_TOKEN = "TELEGRAM_BOT_TOKEN"
K_CHAT = "TELEGRAM_CHAT_ID"
K_KEY = "BINANCE_API_KEY"
K_SECRET = "BINANCE_API_SECRET"
K_MODE = "TRADING_MODE"

_KEY_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN_RE = re.compile(r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_SAFE_VALUE_RE = re.compile(r"[A-Za-z0-9_\-.,:/@+=%*!?~^&|;<>()\[\]{}]+")
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
_CHAT_RE = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,}")
_BINANCE_KEY_RE = re.compile(r"[A-Za-z0-9]{32,128}")
_DQ_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\"}
_NO = {"h", "hayir", "hayır", "n", "no"}

# .env.example bulunamazsa kullanılacak en sade şablon
_MINIMAL_TEMPLATE = """\
# Kripto Al-Sat Botu ayarları (kurulum sihirbazı tarafından yazıldı)
# Bu dosyayı KİMSEYLE paylaşmayın!

BINANCE_API_KEY=
BINANCE_API_SECRET=

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

TRADING_MODE=paper
"""

_WITHDRAW_WARNING = (
    "\n🚨🚨🚨 DİKKAT: Bu API anahtarında PARA ÇEKME (Withdrawals) izni AÇIK! 🚨🚨🚨\n"
    "   Anahtar bir şekilde ele geçirilirse paranız başka bir hesaba çekilebilir.\n"
    "   Bot para çekme iznine HİÇBİR ZAMAN ihtiyaç duymaz.\n"
    "   👉 Binance → Profil → API Yönetimi → bu anahtarda 'Düzenle' →\n"
    "      'Enable Withdrawals' (Para çekmeyi etkinleştir) kutusunu KAPATIN ve kaydedin."
)


# ------------------------------------------------------------ saf yardımcılar
def mask(value: str) -> str:
    """Gizli bilgiyi ekranda göstermek için: ilk 4 karakter + '****' (kısa değerlerde sadece '****')."""
    value = value or ""
    if not value:
        return "(boş)"
    return value[:4] + "****" if len(value) > 8 else "****"


def _unquote(value: str) -> str:
    """Tırnaklı değerleri açar; tırnaksız değerde ' #' sonrası yorumu atar (python-dotenv ile uyumlu)."""
    if value[:1] in ("'", '"'):
        quote, buf, i = value[0], [], 1
        while i < len(value):
            ch = value[i]
            if ch == "\\" and i + 1 < len(value):
                nxt = value[i + 1]
                if quote == '"' and nxt in _DQ_ESCAPES:
                    buf.append(_DQ_ESCAPES[nxt])
                    i += 2
                    continue
                if quote == "'" and nxt in ("\\", "'"):
                    buf.append(nxt)
                    i += 2
                    continue
            if ch == quote:
                return "".join(buf)
            buf.append(ch)
            i += 1
        return value  # kapanmamış tırnak: olduğu gibi bırak
    match = re.search(r"\s#", value)
    if match:
        value = value[:match.start()]
    return value.strip()


def parse_env(text: str) -> Dict[str, str]:
    """.env metnini {ANAHTAR: değer} sözlüğüne çevirir (yorumlar ve boş satırlar atlanır).

    ${X} gibi değişken açılımı yapılmaz: değer olduğu gibi okunur ve olduğu gibi geri yazılır.
    """
    result: Dict[str, str] = {}
    for raw in (text or "").lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_NAME_RE.fullmatch(key):
            continue
        result[key] = _unquote(value.strip())
    return result


def _quote(value: str) -> str:
    """Değeri .env'e güvenle yazılacak biçime getirir (gerekirse tırnaklar)."""
    value = "" if value is None else str(value)
    if value == "" or _SAFE_VALUE_RE.fullmatch(value):
        return value
    if "'" not in value and "\\" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"  # tek tırnak: $ vb. yorumlanmaz
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{escaped}"'


def render_env(example_text: str, values: Dict[str, str]) -> str:
    """Örnek dosyadaki 'KEY=' satırlarının değerini `values` ile değiştirir, yorumları korur.

    Örnekte olmayan anahtarlar dosyanın sonuna '# Diğer ayarlar' başlığı altında eklenir.
    `values` içinde olmayan anahtarlar örnekteki varsayılan değeriyle kalır.
    """
    out: List[str] = []
    example_keys = set()
    for line in (example_text or "").lstrip("\ufeff").splitlines():
        match = _ASSIGN_RE.match(line)
        if match:
            indent, export, key = match.group(1), match.group(2) or "", match.group(3)
            example_keys.add(key)
            if key in values:
                out.append(f"{indent}{export}{key}={_quote(values[key])}")
                continue
        out.append(line)

    extra = [k for k in values if k not in example_keys and _KEY_NAME_RE.fullmatch(k)]
    if extra:
        while out and not out[-1].strip():
            out.pop()
        if out:
            out.append("")
        out.append("# Diğer ayarlar")
        out.extend(f"{k}={_quote(values[k])}" for k in extra)
    return "\n".join(out) + "\n"


def _clean(raw: Optional[str]) -> str:
    """Kullanıcı girdisini temizler: boşluklar ve yapıştırırken gelen tırnaklar atılır."""
    text = (raw or "").replace("\ufeff", "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


def _redact(text: str, *secrets: str) -> str:
    """Hata mesajlarında (ör. URL içinde) geçebilecek gizli bilgileri maskeler."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, mask(secret))
    return text


def _to_float(raw: Optional[str], default: float) -> float:
    try:
        return float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def migrate_legacy(values: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """Eski ayar adlarını yenilerine çevirir (koruma seviyesi değişmeden).

    MAX_TOTAL_LOSS_USDT (sabit USDT) → MAX_TOTAL_LOSS_PCT (sermayenin yüzdesi) = USDT / START_CAPITAL × 100.
    """
    out, notes = dict(values), []
    usdt_raw = (out.get("MAX_TOTAL_LOSS_USDT") or "").strip()
    if "MAX_TOTAL_LOSS_USDT" in out and not (out.get("MAX_TOTAL_LOSS_PCT") or "").strip():
        usdt = _to_float(usdt_raw, 0.0)
        capital = _to_float(out.get("START_CAPITAL_USDT"), 11.0)
        if usdt > 0 and capital > 0:
            pct = round(usdt / capital * 100, 2)
            out["MAX_TOTAL_LOSS_PCT"] = f"{pct:g}"
            notes.append(f"Kill-switch artık yüzdeyle çalışıyor: MAX_TOTAL_LOSS_USDT={usdt_raw} → "
                         f"MAX_TOTAL_LOSS_PCT={pct:g} (sermayenin %{pct:g}'i, koruma seviyesi aynı).")
    if "MAX_TOTAL_LOSS_USDT" in out and (out.get("MAX_TOTAL_LOSS_PCT") or "").strip():
        del out["MAX_TOTAL_LOSS_USDT"]
    return out, notes


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:  # Not Defteri'nin ANSI (Windows-1254) kaydı
        return path.read_text(encoding="cp1254", errors="replace")


def _extract_chats(updates) -> List[Tuple[str, str, str]]:
    """getUpdates sonucundan (chat_id, ad, tür) listesi çıkarır (tekrarsız, görülme sırasıyla)."""
    found: Dict[str, Tuple[str, str]] = {}
    for upd in updates or []:
        if not isinstance(upd, dict):
            continue
        for field in ("message", "edited_message", "channel_post", "edited_channel_post",
                      "my_chat_member", "chat_member", "callback_query"):
            obj = upd.get(field)
            if field == "callback_query" and isinstance(obj, dict):
                obj = obj.get("message")
            chat = obj.get("chat") if isinstance(obj, dict) else None
            if isinstance(chat, dict) and "id" in chat:
                name = (chat.get("title")
                        or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                        or (f"@{chat['username']}" if chat.get("username") else "")
                        or "?")
                found.setdefault(str(chat["id"]), (name, chat.get("type") or "private"))
                break
    return [(cid, name, kind) for cid, (name, kind) in found.items()]


def _classify_binance_error(exc: Exception) -> str:
    """'rejected' (anahtar hatalı), 'clock' (saat farkı) veya 'offline' (bağlantı/sunucu sorunu)."""
    if isinstance(exc, RateLimitError):
        return "offline"
    if isinstance(exc, BinanceAPIError):
        if exc.code == -1021:
            return "clock"
        if not exc.status or exc.status >= 500:
            return "offline"
        return "rejected"
    return "offline"


def _utf8_console() -> None:
    """Windows terminallerinde emoji/Türkçe karakterler hata vermesin."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _write_env(env_path: Path, text: str, now_fn: Callable[[], float]) -> Optional[Path]:
    """Eski .env'i .env.bak-YYYYmmdd-HHMMSS olarak yedekler, yenisini güvenli biçimde yazar."""
    backup = None
    if env_path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now_fn()))
        backup = env_path.with_name(f"{env_path.name}.bak-{stamp}")
        n = 1
        while backup.exists():
            backup = env_path.with_name(f"{env_path.name}.bak-{stamp}-{n}")
            n += 1
        shutil.copy2(env_path, backup)
    tmp = env_path.with_name(env_path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        if os.name != "nt":
            try:
                os.chmod(tmp, 0o600)  # gizli bilgiler: sadece dosya sahibi okuyabilsin
            except OSError:
                pass
        os.replace(tmp, env_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return backup


# ------------------------------------------------------------ sihirbaz
class _Wizard:
    def __init__(self, input_fn, print_fn, http, sleep_fn, binance_factory, now_fn):
        self.input_fn = input_fn
        self.print_fn = print_fn
        self.http = http
        self.sleep_fn = sleep_fn
        self.binance_factory = binance_factory
        self.now_fn = now_fn
        self.bot_name: Optional[str] = None
        self.tg_status = "skip"       # ok / offline / skip
        self.bn_status = "skip"       # ok / offline / skip
        self.test_sent = False

    # -------------------------------------------------------------- G/Ç
    def say(self, text: str = "") -> None:
        self.print_fn(text)

    def ask(self, question: str, keep: str = "", enter_hint: str = "") -> str:
        if keep:
            hint = f" [Enter = koru: {mask(keep)}]"
        elif enter_hint:
            hint = f" [Enter = {enter_hint}]"
        else:
            hint = ""
        return _clean(self.input_fn(f"{question}{hint}: "))

    def header(self, step: str, title: str) -> None:
        self.say("")
        self.say(LINE)
        self.say(f"{step}  {title}")
        self.say(LINE)

    # -------------------------------------------------------------- akış
    def collect(self, old: Dict[str, str], env_exists: bool) -> Optional[Dict[str, str]]:
        """Soruları sorar; kaydedilecek değerleri döndürür (kullanıcı kaydetmezse None)."""
        self.intro(old, env_exists)
        token = self.step_token(old.get(K_TOKEN, ""))
        chat = self.step_chat(token, old.get(K_CHAT, ""))
        quote = (old.get("QUOTE_ASSET") or "USDT").upper()
        capital = _to_float(old.get("START_CAPITAL_USDT"), 11.0)
        key, secret = self.step_binance(old.get(K_KEY, ""), old.get(K_SECRET, ""), quote, capital)
        self.step_mode(old.get(K_MODE, ""))
        updates = {K_TOKEN: token, K_CHAT: chat, K_KEY: key, K_SECRET: secret, K_MODE: "paper"}
        self.summary(updates)
        answer = self.ask("💾 Ayarlar kaydedilsin mi? (E/h)", enter_hint="Evet")
        if answer.lower() in _NO:
            self.say("🚫 Kaydedilmedi — hiçbir dosya değiştirilmedi.")
            return None
        return updates

    def intro(self, old: Dict[str, str], env_exists: bool) -> None:
        self.say(LINE)
        self.say("🧙 KURULUM SİHİRBAZI — Kripto Al-Sat Botu")
        self.say(LINE)
        self.say("Birkaç kısa soruyla Telegram bildirimlerini ve (isteğe bağlı) Binance bağlantısını ayarlayacağız.")
        self.say("🧪 Bot TEST modunda kurulur: sanal bakiye kullanılır, gerçek para harcanmaz.")
        self.say("⌨️  Vazgeçmek için istediğiniz an Ctrl+C'ye basın — o durumda hiçbir dosya değiştirilmez.")
        if env_exists:
            self.say("")
            self.say("📄 Mevcut ayarlarınız bulundu. Bir soruda sadece Enter'a basarsanız mevcut değer korunur:")
            self.say(f"   🤖 Telegram token : {mask(old.get(K_TOKEN, ''))}")
            self.say(f"   💬 Telegram chat  : {mask(old.get(K_CHAT, ''))}")
            self.say(f"   🔑 Binance key    : {mask(old.get(K_KEY, ''))}")
            self.say(f"   🔒 Binance secret : {mask(old.get(K_SECRET, ''))}")

    # -------------------------------------------------------------- Telegram
    def tg(self, token: str, method: str, payload: Optional[dict] = None) -> dict:
        url = TG_API.format(token=token, method=method)
        if payload is None:
            resp = self.http.get(url, timeout=HTTP_TIMEOUT)
        else:
            resp = self.http.post(url, json=payload, timeout=HTTP_TIMEOUT)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ConnectionError(f"Telegram beklenmeyen yanıt verdi (HTTP {getattr(resp, 'status_code', '?')})")
        return data

    def check_token(self, token: str) -> Tuple[str, str]:
        """('ok', bot_adı) | ('invalid', açıklama) | ('offline', hata)."""
        try:
            data = self.tg(token, "getMe")
        except Exception as exc:  # noqa: BLE001 - bağlantı sorunu
            return "offline", _redact(str(exc), token)
        if data.get("ok"):
            res = data.get("result") or {}
            return "ok", str(res.get("username") or res.get("first_name") or "?")
        return "invalid", str(data.get("description") or "geçersiz token")

    def step_token(self, current: str) -> str:
        self.header("1/4", "🤖 TELEGRAM BOTU")
        self.say("Bot, alım-satım bildirimlerini size Telegram'dan gönderir.")
        self.say("👉 Telegram'da @BotFather'ı açın, /newbot yazın ve adımları izleyin.")
        self.say("   Size verdiği uzun kodu (token, örn. 123456789:AAH...) buraya yapıştırın.")
        while True:
            raw = self.ask("🔑 Bot token", keep=current, enter_hint="atla")
            if raw.lower().startswith("bot") and _TOKEN_RE.fullmatch(raw[3:]):
                raw = raw[3:]
            token = raw or current
            if not token:
                self.say("⚠️ Telegram atlandı: bot çalışır ama size bildirim GÖNDEREMEZ.")
                self.say("   Daha sonra 'python main.py --kurulum' ile ekleyebilirsiniz.")
                self.tg_status = "skip"
                return ""
            if not _TOKEN_RE.fullmatch(token):
                self.say("❌ Bu bir bot token'ına benzemiyor (örnek: 123456789:AAH...). Tekrar deneyin.")
                if not raw:
                    current = ""
                continue
            self.say("⏳ Telegram'da kontrol ediliyor...")
            status, info = self.check_token(token)
            if status == "ok":
                self.say(f"✅ Token doğru! Botunuz: @{info}")
                self.tg_status, self.bot_name = "ok", info
                return token
            if status == "offline":
                self.say(f"⚠️ Telegram'a bağlanılamadı ({info}).")
                self.say("   Token kaydedilecek ama doğrulanamadı; internet gelince TEST_ET.bat ile kontrol edin.")
                self.tg_status = "offline"
                return token
            self.say(f"❌ Telegram bu token'ı kabul etmedi ({info}).")
            self.say("   BotFather'daki token'ı eksiksiz kopyaladığınızdan emin olun.")
            if not raw:
                current = ""  # kayıtlı token geçersiz: artık Enter 'atla' demek

    def find_chats(self, token: str) -> Optional[List[Tuple[str, str, str]]]:
        """getUpdates'i ~2 dk boyunca yoklar; bulunan sohbetleri döndürür (yoksa None)."""
        self.say(f"⏳ Mesajınız bekleniyor (en fazla {POLL_SECONDS // 60} dakika)...")
        deadline = self.now_fn() + POLL_SECONDS
        max_attempts = POLL_SECONDS // POLL_INTERVAL + 1
        last_error = ""
        next_note = POLL_SECONDS - 30
        for attempt in range(1, max_attempts + 1):
            try:
                data = self.tg(token, "getUpdates")
            except Exception as exc:  # noqa: BLE001 - kısa kesinti olabilir, yoklamaya devam
                last_error = _redact(str(exc), token)
                data = {"ok": True, "result": []}
            if not data.get("ok"):
                desc = data.get("description") or "bilinmeyen hata"
                self.say(f"❌ Telegram mesajları okunamadı: {desc}")
                if data.get("error_code") == 409:
                    self.say("   👉 Bot şu anda başka bir yerde çalışıyor olabilir (BASLAT.bat açık mı?). "
                             "Kapatıp tekrar deneyin.")
                return None
            chats = _extract_chats(data.get("result"))
            if chats:
                return chats
            remaining = deadline - self.now_fn()
            if remaining <= 0 or attempt >= max_attempts:
                break
            if remaining <= next_note:
                self.say(f"   ⏳ Hâlâ bekleniyor... (kalan ~{int(remaining)} sn) — botunuza /start yazdınız mı?")
                next_note -= 30
            self.sleep_fn(POLL_INTERVAL)
        self.say(f"⚠️ {POLL_SECONDS // 60} dakika içinde botunuza gelen mesaj bulunamadı.")
        if last_error:
            self.say(f"   (Bağlantı hatası: {last_error})")
        return None

    def choose_chat(self, chats: List[Tuple[str, str, str]]) -> str:
        icons = {"private": "👤", "group": "👥", "supergroup": "👥", "channel": "📢"}
        if len(chats) == 1:
            cid, name, kind = chats[0]
            self.say(f"✅ Sohbet bulundu: {icons.get(kind, '💬')} {name} — ID {cid}")
            return cid
        self.say(f"🔎 {len(chats)} sohbet bulundu:")
        for i, (cid, name, kind) in enumerate(chats, 1):
            self.say(f"   {i}) {icons.get(kind, '💬')} {name} — ID {cid}")
        while True:
            raw = self.ask(f"👉 Bildirimler hangi sohbete gelsin? Numara yazın (1-{len(chats)})", enter_hint="1")
            if not raw:
                return chats[0][0]
            if raw.isdigit() and 1 <= int(raw) <= len(chats):
                return chats[int(raw) - 1][0]
            self.say(f"❌ Lütfen 1 ile {len(chats)} arasında bir numara yazın.")

    def send_test(self, token: str, chat_id: str) -> Optional[bool]:
        """Test mesajı: True = gitti, False = Telegram reddetti, None = bağlantı sorunu."""
        try:
            data = self.tg(token, "sendMessage", {"chat_id": chat_id, "text": TEST_MESSAGE})
        except Exception as exc:  # noqa: BLE001
            self.say(f"⚠️ Test mesajı gönderilemedi (bağlantı sorunu: {_redact(str(exc), token)}).")
            self.say("   Chat ID yine de kaydedilecek.")
            return None
        if data.get("ok"):
            self.say("✅ Test mesajı gönderildi — Telegram'da '✅ Kurulum başarılı' mesajını görmelisiniz.")
            self.test_sent = True
            return True
        self.say(f"❌ Test mesajı gönderilemedi: {data.get('description') or 'bilinmeyen hata'}")
        if data.get("error_code") == 403:
            self.say("   👉 Botunuzu Telegram'da açıp /start yazın (bot size ilk mesajı kendisi atamaz).")
        else:
            self.say("   👉 Chat ID yanlış olabilir.")
        return False

    def step_chat(self, token: str, current: str) -> str:
        self.header("2/4", "💬 TELEGRAM SOHBETİ")
        if not token:
            self.say("⏭️ Telegram token'ı olmadığı için bu adım atlandı.")
            return current
        if self.tg_status != "ok":
            self.say("⚠️ Telegram'a bağlanılamadığı için sohbet otomatik bulunamıyor.")
            self.say("   Chat ID'nizi biliyorsanız yazın; bilmiyorsanız Enter ile geçin.")
            while True:
                raw = self.ask("💬 Chat ID", keep=current, enter_hint="atla")
                if not raw:
                    return current
                if _CHAT_RE.fullmatch(raw):
                    return raw
                self.say("❌ Chat ID sadece rakamlardan oluşur (grupta başında '-' olabilir).")

        bot = f"@{self.bot_name}" if self.bot_name else "botunuzu"
        offer_current = bool(current)
        while True:
            if offer_current:
                offer_current = False
                self.say(f"Kayıtlı sohbet ID: {mask(current)}")
                raw = self.ask("💬 Enter = bu sohbeti kullan · 'b' = yeniden bul · ya da yeni chat ID yazın")
                if not raw:
                    chat: Optional[str] = current
                elif raw.lower() in ("b", "bul"):
                    self.say(f"👉 Telegram'da {bot} açın ve /start yazın.")
                    chats = self.find_chats(token)
                    chat = self.choose_chat(chats) if chats else None
                else:
                    chat = raw
            else:
                self.say(f"👉 Telegram'da {bot} açın ve /start yazın (ya da herhangi bir mesaj gönderin).")
                raw = self.ask("   Yazdıktan sonra Enter'a basın · chat ID'nizi biliyorsanız yazın · atlamak için 'a'")
                if raw.lower() in ("a", "atla"):
                    self.say("⚠️ Sohbet ayarlanmadı: bot bildirim gönderemez. Daha sonra --kurulum ile ekleyebilirsiniz.")
                    return current
                if raw:
                    chat = raw
                else:
                    chats = self.find_chats(token)
                    chat = self.choose_chat(chats) if chats else None
            if not chat:
                continue
            if not _CHAT_RE.fullmatch(chat):
                self.say("❌ Chat ID sadece rakamlardan oluşur (grupta başında '-' olabilir).")
                continue
            if self.send_test(token, chat) is not False:
                return chat

    # -------------------------------------------------------------- Binance
    def check_binance(self, key: str, secret: str, quote: str, capital: float) -> str:
        """Anahtarları doğrular: 'ok' | 'offline' (kaydedilir) | 'rejected' (tekrar sorulur)."""
        self.say("⏳ Binance'e bağlanılıyor...")
        try:
            client = self.binance_factory(key, secret)
            sync = getattr(client, "sync_time", None)
            if callable(sync):
                try:
                    sync()
                except Exception:  # noqa: BLE001 - account() hatayı zaten gösterecek
                    pass
            acc = client.account() or {}
        except Exception as exc:  # noqa: BLE001
            kind = _classify_binance_error(exc)
            msg = _redact(str(exc), key, secret)
            if kind == "rejected":
                self.say(f"❌ Binance anahtarı kabul etmedi: {msg}")
                self.say("   👉 API Key ve Secret'ı eksiksiz kopyaladığınızdan emin olun.")
                self.say("   👉 Anahtarda IP kısıtlaması varsa bu bilgisayarın IP adresini ekleyin.")
                return "rejected"
            if kind == "clock":
                self.say("⚠️ Bilgisayarınızın saati Binance ile uyuşmuyor. Windows'ta "
                         "Ayarlar → Saat ve dil → 'Saati otomatik ayarla' seçeneğini açın.")
            else:
                self.say(f"⚠️ Binance'e bağlanılamadı ({msg}).")
            self.say("   Anahtarlar kaydedilecek ama doğrulanamadı; daha sonra TEST_ET.bat ile kontrol edin.")
            return "offline"

        free = 0.0
        for bal in acc.get("balances") or []:
            if isinstance(bal, dict) and str(bal.get("asset", "")).upper() == quote:
                free = _to_float(bal.get("free"), 0.0)
                break
        self.say("✅ Binance bağlantısı başarılı!")
        self.say(f"   💰 Spot cüzdanında serbest {quote}: {free:.2f}")
        if free < capital:
            self.say(f"   ℹ️ Canlı mod için Spot cüzdanında en az {capital:g} {quote} olmalı. Paranız Futures/Funding "
                     f"cüzdanındaysa Binance'te Cüzdan → Transfer ile Spot'a aktarın (ücretsiz).")
        if acc.get("canTrade") is False:
            self.say("   ⚠️ Hesabınızda işlem izni kapalı görünüyor (canTrade=false).")
        self.check_restrictions(client, key, secret)
        return "ok"

    def check_restrictions(self, client, key: str, secret: str) -> None:
        spot_warned = False
        while True:
            try:
                restr = client.api_restrictions() or {}
            except Exception as exc:  # noqa: BLE001
                self.say(f"⚠️ API izinleri okunamadı ({_redact(str(exc), key, secret)}).")
                self.say("   Binance'te bu anahtarda 'Enable Withdrawals' (para çekme) izninin KAPALI olduğundan emin olun.")
                return
            if not spot_warned and not restr.get("enableSpotAndMarginTrading", True):
                spot_warned = True
                self.say("⚠️ 'Enable Spot & Margin Trading' izni KAPALI: canlı modda bot alım-satım YAPAMAZ.")
                self.say("   👉 Binance → API Yönetimi → anahtarı düzenle → bu kutuyu işaretleyin.")
            if not restr.get("enableWithdrawals"):
                self.say("✅ Para çekme izni kapalı — güvenli.")
                if restr.get("ipRestrict") is False:
                    self.say("💡 İpucu: Anahtara IP kısıtlaması eklemek güvenliği daha da artırır (isteğe bağlı).")
                return
            self.say(_WITHDRAW_WARNING)
            raw = self.ask("👉 İzni kapattıktan sonra Enter'a basın, tekrar kontrol edeyim "
                           "('devam' = kapatmadan geç, ÖNERİLMEZ)")
            if raw.lower() == "devam":
                self.say("🚨 Para çekme izni AÇIK bırakıldı! Canlı moda geçmeden önce MUTLAKA kapatın.")
                return
            self.say("⏳ Tekrar kontrol ediliyor...")

    def step_binance(self, cur_key: str, cur_secret: str, quote: str, capital: float) -> Tuple[str, str]:
        self.header("3/4", "🏦 BINANCE API ANAHTARLARI (isteğe bağlı)")
        self.say("Sadece CANLI mod (gerçek para) için gerekir. Test modu için Enter'a basıp geçebilirsiniz.")
        self.say("Binance'te API anahtarı oluştururken izinler şöyle olmalı:")
        self.say("   ✅ Enable Reading   ✅ Enable Spot & Margin Trading   🚫 Enable Withdrawals KAPALI")
        first = True
        while True:
            default_key = cur_key if first else ""
            raw_key = self.ask("🔑 API Key", keep=default_key, enter_hint="atla; test modu için gerekmez")
            if not raw_key and not default_key:
                if cur_key:
                    self.say("ℹ️ Binance anahtarları değiştirilmedi.")
                else:
                    self.say("⏭️ Binance atlandı — test modunda gerekmez.")
                return cur_key, cur_secret
            key = raw_key or default_key
            secret_default = cur_secret if key == cur_key else ""
            raw_secret = self.ask("🔒 API Secret", keep=secret_default, enter_hint="atla")
            secret = raw_secret or secret_default
            if not secret:
                self.say("⚠️ Secret girilmedi; Binance anahtarları değiştirilmedi.")
                return cur_key, cur_secret
            for label, val in (("API Key", key), ("API Secret", secret)):
                if not _BINANCE_KEY_RE.fullmatch(val):
                    self.say(f"⚠️ {label} beklenen biçimde değil (genelde 64 harf/rakam). "
                             f"Eksik ya da fazla kopyalanmış olabilir.")
            status = self.check_binance(key, secret, quote, capital)
            if status != "rejected":
                self.bn_status = status
                return key, secret
            first = False
            self.bn_status = "skip"
            self.say("🔁 Anahtarları yeniden girin (Enter = atla; önceki ayar korunur).")

    # -------------------------------------------------------------- mod ve özet
    def step_mode(self, old_mode: str) -> None:
        self.header("4/4", "🧪 ÇALIŞMA MODU ve KAYIT")
        self.say("Bot TEST modunda (TRADING_MODE=paper) kurulur: sanal bakiye, gerçek para kullanılmaz.")
        if old_mode.strip().lower() == "live":
            self.say("ℹ️ Ayar dosyanızda TRADING_MODE=live vardı; güvenlik için TEST moduna (paper) alındı.")
        self.say("   Canlı moda (gerçek para) daha sonra CANLI_BASLAT.bat ile geçebilirsiniz.")
        self.say("   🔒 Kâr al / zarar kes / kill-switch gibi güvenlik kuralları sabittir; sihirbaz bunlara dokunmaz.")

    def summary(self, upd: Dict[str, str]) -> None:
        if not upd[K_TOKEN]:
            tg = "❌ ayarlanmadı (bildirim gelmez)"
        elif self.tg_status == "ok":
            tg = f"✅ @{self.bot_name}"
        else:
            tg = f"⚠️ {mask(upd[K_TOKEN])} (doğrulanamadı)"
        if not upd[K_CHAT]:
            chat = "❌ ayarlanmadı"
        else:
            chat = f"{upd[K_CHAT]}" + (" ✅ (test mesajı gitti)" if self.test_sent else "")
        if not (upd[K_KEY] and upd[K_SECRET]):
            bn = "⏭️ yok (test modu için gerekmez)"
        elif self.bn_status == "ok":
            bn = f"✅ {mask(upd[K_KEY])} (doğrulandı)"
        else:
            bn = f"⚠️ {mask(upd[K_KEY])} (doğrulanamadı)"
        self.say("")
        self.say("📋 ÖZET")
        self.say(f"   🤖 Telegram botu : {tg}")
        self.say(f"   💬 Sohbet ID     : {chat}")
        self.say(f"   🏦 Binance       : {bn}")
        self.say("   🧪 Mod           : TEST (paper)")


# ------------------------------------------------------------ giriş noktası
def run_wizard(env_path: Path = BASE_DIR / ".env",
               example_path: Path = BASE_DIR / ".env.example",
               input_fn: Callable[[str], str] = input,
               print_fn: Callable[..., None] = print,
               http=None,
               sleep_fn: Callable[[float], None] = time.sleep,
               binance_factory: Optional[Callable[[str, str], object]] = None,
               now_fn: Callable[[], float] = time.time) -> bool:
    """Etkileşimli kurulum. .env yazıldıysa True, iptal/hata durumunda False döner."""
    env_path, example_path = Path(env_path), Path(example_path)
    if print_fn is print:
        _utf8_console()
    if http is None:
        import requests
        http = requests

    try:
        old_text: Optional[str] = _read_text(env_path) if env_path.exists() else None
    except OSError as exc:
        print_fn(f"❌ Mevcut ayar dosyası okunamadı ({env_path}): {exc}")
        return False
    old_values = parse_env(old_text) if old_text is not None else {}

    if binance_factory is None:
        base_url = old_values.get("BINANCE_BASE_URL") or "https://api.binance.com"

        def binance_factory(key: str, secret: str):
            return BinanceClient(key, secret, base_url=base_url)

    wizard = _Wizard(input_fn, print_fn, http, sleep_fn, binance_factory, now_fn)
    try:
        updates = wizard.collect(old_values, env_exists=old_text is not None)
    except (KeyboardInterrupt, EOFError):
        print_fn("\n🚫 Kurulum iptal edildi — hiçbir dosya değiştirilmedi.")
        return False
    if updates is None:
        return False

    merged, migration_notes = migrate_legacy(old_values)
    merged.update(updates)
    try:
        template = _read_text(example_path) if example_path.exists() else None
    except OSError:
        template = None
    new_text = render_env(template or old_text or _MINIMAL_TEMPLATE, merged)

    try:
        backup = _write_env(env_path, new_text, now_fn)
    except OSError as exc:
        print_fn(f"❌ Ayar dosyası yazılamadı ({env_path}): {exc}")
        print_fn("   Dosya başka bir programda açık olabilir; kapatıp tekrar deneyin.")
        return False

    print_fn("")
    print_fn(f"💾 Ayarlar kaydedildi: {env_path}")
    for note in migration_notes:
        print_fn(f"🔁 {note}")
    if backup:
        print_fn(f"🗂️ Eski ayar dosyanız yedeklendi: {backup.name}")
    print_fn("")
    print_fn("🎉 Kurulum tamamlandı! Sonraki adımlar:")
    print_fn("   1️⃣  TEST_ET.bat  → bağlantıları ve kuralları kontrol eder (python main.py --selftest)")
    print_fn("   2️⃣  BASLAT.bat   → botu TEST modunda (sanal bakiye) başlatır")
    print_fn("   3️⃣  Her şey yolundaysa CANLI_BASLAT.bat → gerçek parayla çalıştırır ⚠️")
    print_fn("   ⚙️  Ayarları tekrar değiştirmek için: python main.py --kurulum")
    return True


if __name__ == "__main__":
    sys.exit(0 if run_wizard() else 1)
