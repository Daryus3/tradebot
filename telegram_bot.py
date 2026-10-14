"""
Telegram entegrasyonu.

`TelegramNotifier` mesajları gönderir (hata olursa botu durdurmaz),
`Messages` ise emojili, okunaklı mesaj şablonlarını üretir.
Telegram ayarlı değilse mesajlar yalnızca terminale/log dosyasına yazılır.
"""
from __future__ import annotations

import html
import math
import re
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

import requests

from config import in_time_range
from logger import get_logger, redact, register_secrets

log = get_logger()

API_URL = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000
LINE = "━━━━━━━━━━━━━━━━"


# ---------------------------------------------------------------- biçimlendirme
def esc(text) -> str:
    return html.escape(str(text), quote=False)


def fmt_price(p: float) -> str:
    if p <= 0:
        return "0"
    if p >= 1000:
        d = 2
    elif p >= 1:
        d = 4
    else:
        d = min(10, 4 - int(math.floor(math.log10(p))))
    return f"{p:.{d}f}"


def fmt_qty(q: float) -> str:
    return f"{q:.8f}".rstrip("0").rstrip(".") or "0"


def fmt_usdt(x: float) -> str:
    return f"{x:.2f}"


def fmt_rate(rate: float) -> str:
    """Komisyon oranı: 0.001 → '%0.10', 0.00075 → '%0.075'."""
    text = f"{rate * 100:.3f}"
    return "%" + (text[:-1] if text.endswith("0") else text)


def fmt_signed(x: float, d: int = 2) -> str:
    return f"{x:+.{d}f}"


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} dk"
    if minutes < 1440:
        return f"{minutes // 60} sa {minutes % 60} dk"
    return f"{minutes // 1440} gün {minutes % 1440 // 60} sa"


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# ---------------------------------------------------------------- gönderici
_OK, _DROP, _RETRY = "ok", "drop", "retry"
_RETRY_DELAYS = (2, 5, 10, 30)


class TelegramNotifier:
    """Mesajları arka planda (ayrı bir iş parçacığında) sırayla gönderir.

    - Telegram yavaşlasa bile ana döngü (kâr al / zarar kes kontrolleri) hiç beklemez.
    - İnternet kesilirse mesajlar kaybolmaz: kuyrukta bekler, bağlantı gelince gönderilir.
    - Sessiz saatlerde rutin mesajlar bildirim sesi olmadan gelir; kritik mesajlar her zaman sesli.
    - Token / API anahtarları loglara ve mesajlara asla açık yazılmaz.
    `async_mode=False` ile senkron çalışır (self-test ve kurulum için).
    """

    def __init__(self, token: str, chat_id: str, prefix: str = "", timeout: float = 10.0,
                 async_mode: bool = True, quiet_window: Optional[Tuple[int, int]] = None,
                 max_queue: int = 300, now_fn=None):
        self.token = token
        self.chat_id = chat_id
        self.prefix = prefix
        self.timeout = timeout
        self.async_mode = async_mode
        self.quiet_window = quiet_window
        self.max_queue = max_queue
        self.now_fn = now_fn or time.time
        self.session = requests.Session()
        self._last_error_sent: Dict[str, float] = {}
        self._items: Deque[Tuple[str, str, bool, bool]] = deque()  # (tür, içerik, sessiz, kritik)
        self._cond = threading.Condition()
        self._busy = False
        self._worker: Optional[threading.Thread] = None
        self.down_since: Optional[float] = None
        register_secrets(token)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def is_quiet_now(self) -> bool:
        lt = time.localtime(self.now_fn())
        return in_time_range(lt.tm_hour * 60 + lt.tm_min, self.quiet_window)

    def send(self, text: str, silent: bool = False, critical: bool = False) -> bool:
        """Mesajı gönderir (veya kuyruğa ekler). Asla exception fırlatmaz."""
        full = redact(f"{self.prefix}{text}")
        log.info("📨 %s", _strip_html(full).replace("\n", " | "))
        if not self.enabled:
            return False
        if not critical and self.is_quiet_now():
            silent = True
        if not self.async_mode:
            return self._deliver_text(full, silent) == _OK
        self._enqueue(("text", full, silent, critical))
        return True

    def send_document(self, path, caption: str = "") -> bool:
        """Dosya gönderir (örn. işlem geçmişi CSV'si)."""
        if not self.enabled:
            return False
        if not self.async_mode:
            return self._deliver_document(str(path), redact(caption)) == _OK
        self._enqueue(("doc", f"{path}\n{redact(caption)}", False, False))
        return True

    def send_error(self, key: str, text: str, cooldown_sec: int = 600) -> bool:
        """Aynı hata için Telegram'ı spam'lememek adına `cooldown_sec` içinde tek mesaj."""
        now = time.time()
        if now - self._last_error_sent.get(key, 0) < cooldown_sec:
            log.warning("(tekrarlanan hata, Telegram'a gönderilmedi) %s",
                        redact(_strip_html(text)).replace("\n", " | "))
            return False
        self._last_error_sent[key] = now
        return self.send(text, critical=True)

    def flush(self, timeout: float = 20.0) -> None:
        """Kuyruktaki mesajların gönderilmesini bekler (bot kapanırken)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._cond:
                if not self._items and not self._busy:
                    return
            time.sleep(0.1)

    @property
    def pending(self) -> int:
        with self._cond:
            return len(self._items)

    # ------------------------------------------------------------ iç işler
    def _enqueue(self, item) -> None:
        with self._cond:
            if len(self._items) >= self.max_queue:
                # Kuyruk dolu: en eski kritik olmayan mesajı at (kritikler korunur)
                for i, old in enumerate(self._items):
                    if not old[3]:
                        del self._items[i]
                        break
                else:
                    self._items.popleft()
            self._items.append(item)
            self._cond.notify()
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="telegram", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        attempt = 0
        while True:
            with self._cond:
                while not self._items:
                    self._cond.wait()
                item = self._items[0]
                self._busy = True
            kind, payload, silent, _ = item
            try:
                if kind == "doc":
                    path, caption = payload.split("\n", 1)
                    result = self._deliver_document(path, caption)
                else:
                    result = self._deliver_text(payload, silent)
            except Exception as exc:  # noqa: BLE001 - gönderici iş parçacığı asla ölmesin
                log.warning("Telegram gönderim hatası: %s", redact(str(exc)))
                result = _DROP
            if result == _RETRY:
                if self.down_since is None:
                    self.down_since = time.time()
                with self._cond:
                    self._busy = False
                time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])
                attempt += 1
                continue
            attempt = 0
            with self._cond:
                if self._items and self._items[0] is item:
                    self._items.popleft()
                self._busy = False
            if result == _OK and self.down_since is not None:
                minutes = (time.time() - self.down_since) / 60
                self.down_since = None
                if minutes >= 1:
                    note = (f"📶 Telegram bağlantısı yaklaşık {minutes:.0f} dk kesikti. "
                            f"Bu sürede biriken {self.pending} mesaj sırayla gönderiliyor.")
                    log.info(note)
                    self._deliver_text(f"{self.prefix}{note}", True)

    def _deliver_text(self, full: str, silent: bool) -> str:
        if len(full) > MAX_LEN:
            full = full[: MAX_LEN - 20] + "\n…(kısaltıldı)"
        payload = {"chat_id": self.chat_id, "text": full, "parse_mode": "HTML",
                   "disable_web_page_preview": True, "disable_notification": silent}
        url = API_URL.format(token=self.token, method="sendMessage")
        for _ in range(3):
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("Telegram'a ulaşılamadı, mesaj kuyrukta bekletiliyor: %s", redact(str(exc)))
                return _RETRY
            if resp.status_code == 429:
                wait = resp.json().get("parameters", {}).get("retry_after", 2)
                time.sleep(min(float(wait), 30))
                continue
            if resp.ok:
                return _OK
            if resp.status_code == 400 and "parse" in resp.text.lower():
                # HTML biçimlendirme hatası: düz metin olarak tekrar dene
                payload.pop("parse_mode", None)
                payload["text"] = _strip_html(full)
                continue
            if resp.status_code >= 500:
                return _RETRY
            log.warning("Telegram mesajı gönderilemedi (HTTP %s): %s", resp.status_code, redact(resp.text[:200]))
            return _DROP
        return _RETRY

    def _deliver_document(self, path: str, caption: str) -> str:
        url = API_URL.format(token=self.token, method="sendDocument")
        try:
            with open(path, "rb") as fh:
                resp = self.session.post(url, data={"chat_id": self.chat_id, "caption": caption[:1000]},
                                         files={"document": fh}, timeout=max(self.timeout, 30))
        except FileNotFoundError:
            log.warning("Gönderilecek dosya bulunamadı: %s", path)
            return _DROP
        except requests.RequestException as exc:
            log.warning("Telegram dosya gönderimi başarısız: %s", redact(str(exc)))
            return _RETRY
        if resp.ok:
            return _OK
        if resp.status_code >= 500 or resp.status_code == 429:
            return _RETRY
        log.warning("Telegram dosyası gönderilemedi (HTTP %s): %s", resp.status_code, redact(resp.text[:200]))
        return _DROP


def discover_chat_ids(token: str) -> List[Tuple[str, str]]:
    """Bota mesaj atmış kişilerin/grupların chat ID'lerini bulur (TELEGRAM_CHAT_ID kurulumu için)."""
    resp = requests.get(API_URL.format(token=token, method="getUpdates"), timeout=15)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram hatası"))
    found: Dict[str, str] = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) \
                or chat.get("username") or "?"
            found[str(chat["id"])] = name
    return list(found.items())


# ---------------------------------------------------------------- şablonlar
_EXIT_HEADERS = {
    "TAKE_PROFIT": "🚀 <b>KÂR HEDEFİNE ULAŞILDI!</b>",
    "STOP_LOSS": "⚠️ <b>STOP-LOSS ÇALIŞTI</b>",
    "TRAILING_STOP": "🛡️ <b>İZLEYEN STOP ÇALIŞTI (kâr korundu)</b>",
    "MAX_HOLD": "⏱️ <b>MAKSİMUM SÜRE DOLDU</b>",
    "KILL_SWITCH": "☠️ <b>KILL-SWITCH SATIŞI</b>",
    "EXTERNAL": "❔ <b>POZİSYON BOT DIŞINDA KAPANMIŞ</b>",
    "BREAKEVEN": "🔒 <b>BAŞABAŞ KORUMASI ÇALIŞTI</b>",
    "MANUAL": "✋ <b>ELLE SATIŞ (Telegram komutu)</b>",
    "BTC_CRASH": "📉 <b>BTC ÇÖKÜŞÜ — ERKEN ÇIKIŞ</b>",
    "BACKUP_STOP": "🧷 <b>BINANCE YEDEK STOP EMRİ ÇALIŞTI</b>",
}

# Kritik mesajlar sessiz saatlerde de sesli gelir
CRITICAL_EXITS = {"STOP_LOSS", "KILL_SWITCH", "EXTERNAL", "BTC_CRASH", "BACKUP_STOP"}


class Messages:
    @staticmethod
    def started(cfg, capital: float, realized: float, pairs: int, slots: int = 1, loss_limit: float = 0.0,
                loss_pct: float = 0.0) -> str:
        mode = "🧪 TEST (sanal bakiye, gerçek para kullanılmaz)" if cfg.is_paper else "💵 CANLI (gerçek para!)"
        trailing = (f"+%{cfg.trailing_activation_pct:g} sonrası zirve kârın yarısı (en az +%{cfg.trailing_floor_pct:g})"
                    if cfg.trailing_dynamic else
                    f"+%{cfg.trailing_activation_pct:g} görülünce, +%{cfg.trailing_floor_pct:g}'e düşerse sat")
        hold = f"\n⏱️ Maks. tutma süresi: {fmt_duration(cfg.max_hold_minutes * 60)}" if cfg.max_hold_minutes > 0 else ""
        return (
            f"🤖 <b>Kripto Al-Sat Botu Başlatıldı</b>\n{LINE}\n"
            f"⚙️ Mod: {mode}\n"
            f"💰 Güncel Bakiye: <b>{fmt_usdt(capital)} {cfg.quote_asset}</b>"
            f" (K/Z: {fmt_signed(realized)})\n"
            f"🎯 Kâr Al: +%{cfg.take_profit_pct:g} | 🛑 Zarar Kes: -%{cfg.stop_loss_pct:g}\n"
            f"📈 İzleyen Stop: {trailing}\n"
            f"💸 Komisyon: {fmt_rate(cfg.fee_rate)} (alış + satış, net hesaplanır)\n"
            f"☠️ Kill-switch: toplam zarar sermayenin %{loss_pct:.4g}'i "
            f"(şu an {fmt_usdt(loss_limit)} {cfg.quote_asset}){hold}\n"
            f"📦 Aynı anda pozisyon: şu an <b>{slots}</b> (ayar: en fazla {cfg.max_open_positions}, "
            f"bakiye arttıkça otomatik artar)\n"
            f"🔍 Tarama: {cfg.scan_interval_sec} sn | {pairs} parite"
        )

    @staticmethod
    def buy(pos, levels: Dict[str, float], note: str, cfg, open_count: int = 1, slots: int = 1,
            backup_line: str = "") -> str:
        q = cfg.quote_asset
        extra = f"\n{backup_line}" if backup_line else ""
        return (
            f"🟢 <b>ALIM YAPILDI</b> — <b>{esc(pos.symbol)}</b> (📦 {open_count}/{slots})\n{LINE}\n"
            f"💵 Harcanan: {fmt_usdt(pos.cost_usdt)} {q}\n"
            f"🪙 Miktar: {fmt_qty(pos.qty)} {esc(pos.base_asset)}\n"
            f"🏷️ Ort. alış fiyatı: {fmt_price(pos.entry_price)}\n"
            f"💸 Alış komisyonu: {pos.buy_fee_usdt:.4f} {q}\n{LINE}\n"
            f"🎯 Kâr Al (+%{cfg.take_profit_pct:g}): {fmt_price(levels['tp'])} → ≈{fmt_usdt(levels['tp_value'])} {q}\n"
            f"📈 İzleyen stop başlar (+%{cfg.trailing_activation_pct:g}): {fmt_price(levels['trail_on'])}\n"
            f"🛡️ İzleyen stop satış (+%{cfg.trailing_floor_pct:g}): {fmt_price(levels['trail_floor'])}"
            f" → ≈{fmt_usdt(levels['trail_value'])} {q}\n"
            f"🛑 Zarar Kes (-%{cfg.stop_loss_pct:g}): {fmt_price(levels['sl'])} → ≈{fmt_usdt(levels['sl_value'])} {q}"
            f"{extra}\n"
            f"🔍 Sinyal: {esc(note)}"
        )

    @staticmethod
    def trailing_activated(pos, pnl_pct: float, floor_price: float, floor_pct: float, cfg) -> str:
        return (
            f"📈 <b>İZLEYEN STOP AKTİF</b> — {esc(pos.symbol)}\n"
            f"Net kâr %{pnl_pct:.2f}'ye ulaştı. 🎯 Ana hedef: +%{cfg.take_profit_pct:g}\n"
            f"🛡️ Fiyat {fmt_price(floor_price)} seviyesine (net +%{floor_pct:g}) düşerse kâr korunarak satılacak."
        )

    @staticmethod
    def sell(reason: str, pos, exit_price: float, proceeds: float, sell_fee: float, pnl: float,
             pnl_pct_value: float, duration_sec: float, balance: float, total_pnl: float,
             trades: int, wins: int, losses: int, cfg, estimated: bool = False,
             open_positions: int = 0) -> str:
        q = cfg.quote_asset
        header = _EXIT_HEADERS.get(reason, "🔴 <b>SATIŞ</b>")
        result = (f"✅ Net Kâr: <b>{fmt_signed(pnl)} {q}</b> ({fmt_signed(pnl_pct_value)}%)" if pnl >= 0 else
                  f"❌ Net Zarar: <b>{fmt_signed(pnl)} {q}</b> ({fmt_signed(pnl_pct_value)}%)")
        est = " (tahmini)" if estimated else ""
        still_open = f"\n📦 Açık pozisyon: {open_positions}" if open_positions else ""
        return (
            f"{header}\n"
            f"🔴 <b>SATIŞ YAPILDI</b> — <b>{esc(pos.symbol)}</b>{est}\n{LINE}\n"
            f"🏷️ Alış: {fmt_price(pos.entry_price)} → Satış: {fmt_price(exit_price)}\n"
            f"💵 Maliyet: {fmt_usdt(pos.cost_usdt)} → Net gelir: {fmt_usdt(proceeds)} {q}\n"
            f"💸 Komisyon (alış+satış): {pos.buy_fee_usdt + sell_fee:.4f} {q}\n"
            f"{result}\n"
            f"⏳ Süre: {fmt_duration(duration_sec)}\n{LINE}\n"
            f"💰 Güncel Bakiye: <b>{fmt_usdt(balance)} {q}</b>\n"
            f"📊 Toplam K/Z: {fmt_signed(total_pnl)} {q} | İşlem: {trades} (✅{wins} / ❌{losses})"
            f"{still_open}"
        )

    @staticmethod
    def kill_switch(total_pnl: float, balance: float, cfg, loss_limit: float, loss_pct: float,
                    positions_left: Optional[List[str]] = None, retrying: bool = False) -> str:
        head = (f"🛑☠️ <b>KILL-SWITCH TETİKLENDİ!</b>\n{LINE}\n"
                f"Toplam zarar {fmt_usdt(-total_pnl)} {cfg.quote_asset} ile limite "
                f"(sermayenin %{loss_pct:.4g}'i = {fmt_usdt(loss_limit)} {cfg.quote_asset}) ulaştı.\n")
        if retrying and positions_left:
            return (head + f"⛔ Yeni alımlar kalıcı olarak durduruldu.\n"
                    f"⚠️ <b>Henüz satılamayan: {esc(', '.join(positions_left))}</b> — satış birkaç saniyede bir "
                    f"tekrar deneniyor; bu sırada Binance'teki yedek stop emri korumada. Hepsi satılınca bot kapanacak.")
        return (head + f"⛔ Tüm pozisyonlar satıldı, bot kapatılıyor.\n"
                f"💰 Son Bakiye: <b>{fmt_usdt(balance)} {cfg.quote_asset}</b>\n"
                f"🔒 Yeniden başlatmak için: <code>python main.py --reset-state</code>")

    @staticmethod
    def status(balance: float, realized: float, trades: int, wins: int, losses: int, cfg,
               position_lines: str = "", remaining_loss: float = 0.0, open_count: int = 0,
               slots: int = 1) -> str:
        q = cfg.quote_asset
        pos = (f"📦 Açık pozisyon: {open_count}/{slots}\n{position_lines}" if position_lines
               else f"📦 Açık pozisyon yok (0/{slots}), fırsat taranıyor 🔍")
        return (
            f"📊 <b>Durum Raporu</b>\n{LINE}\n"
            f"💰 Güncel Bakiye: <b>{fmt_usdt(balance)} {q}</b>\n"
            f"📈 Toplam K/Z: {fmt_signed(realized)} {q}\n"
            f"🔁 İşlem: {trades} (✅{wins} / ❌{losses})\n"
            f"{pos}\n"
            f"☠️ Kill-switch'e kalan: {fmt_usdt(remaining_loss)} {q}"
        )

    @staticmethod
    def breakeven_activated(pos, pnl_pct: float, floor_price: float, cfg) -> str:
        return (f"🔒 <b>BAŞABAŞ KORUMASI AKTİF</b> — {esc(pos.symbol)}\n"
                f"Net kâr %{pnl_pct:.2f} görüldü. Fiyat {fmt_price(floor_price)} seviyesine "
                f"(net +%{cfg.breakeven_floor_pct:g}) dönerse zarara geçmeden satılacak.")

    @staticmethod
    def outage(minutes: float, open_symbols: List[str], startup: bool = False) -> str:
        what = "Bot kapalıydı" if startup else "Bot çalışmadı (bilgisayar uykuda olabilir)"
        if open_symbols:
            tail = (f"\n⚠️ Bu sürede açık pozisyonların ({esc(', '.join(open_symbols))}) kâr al / zarar kes "
                    f"kontrolü bot tarafından yapılamadı. Şimdi kontrol ediliyor.")
        else:
            tail = "\nAçık pozisyon yoktu."
        return f"⏰ <b>{what}: yaklaşık {fmt_duration(minutes * 60)}</b>{tail}"

    @staticmethod
    def network_restored(minutes: float, lines: List[str]) -> str:
        body = "\n".join(lines) if lines else "📦 Açık pozisyon yok."
        return f"📶 <b>İnternet bağlantısı geri geldi</b> (yaklaşık {fmt_duration(minutes * 60)} kesikti)\n{body}"

    @staticmethod
    def position_resumed(pos) -> str:
        return (f"♻️ <b>Açık pozisyon devralındı</b> — {esc(pos.symbol)}\n"
                f"🏷️ Alış: {fmt_price(pos.entry_price)} | 💵 Maliyet: {fmt_usdt(pos.cost_usdt)}\n"
                f"Kâr al / zarar kes takibi kaldığı yerden devam ediyor.")

    @staticmethod
    def error(text: str, fatal: bool = False) -> str:
        tail = "⛔ Bot durdu." if fatal else "Bot çalışmaya devam ediyor."
        return f"❌ <b>Sistem Hatası</b>\n<code>{esc(text)[:1500]}</code>\n{tail}"

    @staticmethod
    def warning(text: str) -> str:
        return f"⚠️ <b>Uyarı</b>\n{esc(text)}"

    @staticmethod
    def info(text: str) -> str:
        return f"ℹ️ <b>Bilgi</b>\n{esc(text)}"

    @staticmethod
    def bnb_status(info, position_usdt: float, quote: str = "USDT") -> str:
        """BNB indirimi durumu (düz metin), örn. 'BNB indirimi: açık (%0.075, BNB ≈ 3.00 USDT ≈ 90 işlem)'."""
        if info.covers(position_usdt):
            left = info.bnb_trades(position_usdt)
            if left is None:  # BNB fiyatı öğrenilemedi
                value = f", {fmt_qty(info.bnb_free)} BNB"
            else:
                count = f"≈ {int(left)}" if left >= 1 else "< 1"
                value = f", BNB ≈ {fmt_usdt(info.bnb_value)} {quote} {count} işlem"
            return f"BNB indirimi: açık ({fmt_rate(info.discounted_rate)}{value})"
        if info.bnb_enabled:
            what = "BNB yok" if info.bnb_free <= 0 else "BNB bir işleme yetmiyor"
            return f"BNB indirimi: açık ama {what} ({fmt_rate(info.taker)} kesiliyor)"
        return "BNB indirimi: kapalı"

    @staticmethod
    def stopped(balance: float, realized: float, cfg, open_symbols: Optional[List[str]] = None,
                protected: int = 0) -> str:
        warn = ""
        if open_symbols:
            guard = (f"Binance'teki yedek stop emri (-%{cfg.backup_stop_pct:g}) zarar kesi korumaya devam ediyor; "
                     f"kâr al ve izleyen stop ise bot kapalıyken ÇALIŞMAZ."
                     if protected == len(open_symbols) else
                     "bot kapalıyken kâr al / zarar kes ÇALIŞMAZ!")
            warn = (f"\n⚠️ Açık pozisyon var: <b>{esc(', '.join(open_symbols))}</b> — {guard} "
                    f"Tekrar başlatınca takip kaldığı yerden devam eder.")
        return (f"👋 <b>Bot durduruldu</b>\n"
                f"💰 Güncel Bakiye: {fmt_usdt(balance)} {cfg.quote_asset} (K/Z: {fmt_signed(realized)}){warn}")
