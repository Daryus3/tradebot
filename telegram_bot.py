"""
Telegram entegrasyonu.

`TelegramNotifier` mesajları gönderir (hata olursa botu durdurmaz),
`Messages` ise emojili, okunaklı mesaj şablonlarını üretir.
Telegram ayarlı değilse mesajlar yalnızca terminale/log dosyasına yazılır.
"""
from __future__ import annotations

import html
import math
import queue
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests

from logger import get_logger

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


def fmt_signed(x: float, d: int = 2) -> str:
    return f"{x:+.{d}f}"


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} dk"
    return f"{minutes // 60} sa {minutes % 60} dk"


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# ---------------------------------------------------------------- gönderici
class TelegramNotifier:
    """Mesajları arka planda (ayrı bir iş parçacığında) sırayla gönderir.

    Böylece Telegram yavaşlasa veya erişilemez olsa bile ana döngü (kâr al / zarar kes
    kontrolleri) hiç beklemez. `async_mode=False` ile senkron çalışır (self-test için).
    """

    def __init__(self, token: str, chat_id: str, prefix: str = "", timeout: float = 10.0,
                 async_mode: bool = True):
        self.token = token
        self.chat_id = chat_id
        self.prefix = prefix
        self.timeout = timeout
        self.async_mode = async_mode
        self.session = requests.Session()
        self._last_error_sent: Dict[str, float] = {}
        self._queue: "queue.Queue[Tuple[str, bool]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, silent: bool = False) -> bool:
        """Mesajı gönderir (veya kuyruğa ekler). Asla exception fırlatmaz."""
        full = f"{self.prefix}{text}"
        log.info("📨 %s", _strip_html(full).replace("\n", " | "))
        if not self.enabled:
            return False
        if not self.async_mode:
            return self._deliver(full, silent)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="telegram", daemon=True)
            self._worker.start()
        self._queue.put((full, silent))
        return True

    def flush(self, timeout: float = 20.0) -> None:
        """Kuyruktaki mesajların gönderilmesini bekler (bot kapanırken)."""
        deadline = time.time() + timeout
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.1)

    def _run(self) -> None:
        while True:
            full, silent = self._queue.get()
            try:
                self._deliver(full, silent)
            except Exception as exc:  # noqa: BLE001 - gönderici iş parçacığı asla ölmesin
                log.warning("Telegram gönderim hatası: %s", exc)
            finally:
                self._queue.task_done()

    def _deliver(self, full: str, silent: bool) -> bool:
        if len(full) > MAX_LEN:
            full = full[: MAX_LEN - 20] + "\n…(kısaltıldı)"
        payload = {"chat_id": self.chat_id, "text": full, "parse_mode": "HTML",
                   "disable_web_page_preview": True, "disable_notification": silent}
        url = API_URL.format(token=self.token, method="sendMessage")
        for attempt in range(3):
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
                if resp.status_code == 429:
                    wait = resp.json().get("parameters", {}).get("retry_after", 2)
                    time.sleep(min(float(wait), 30))
                    continue
                if resp.ok:
                    return True
                if resp.status_code == 400 and "parse" in resp.text.lower():
                    # HTML biçimlendirme hatası: düz metin olarak tekrar dene
                    payload.pop("parse_mode", None)
                    payload["text"] = _strip_html(full)
                    continue
                log.warning("Telegram mesajı gönderilemedi (HTTP %s): %s", resp.status_code, resp.text[:200])
                return False
            except requests.RequestException as exc:
                log.warning("Telegram bağlantı hatası (deneme %d/3): %s", attempt + 1, exc)
                time.sleep(2)
        return False

    def send_error(self, key: str, text: str, cooldown_sec: int = 600) -> bool:
        """Aynı hata için Telegram'ı spam'lememek adına `cooldown_sec` içinde tek mesaj."""
        now = time.time()
        if now - self._last_error_sent.get(key, 0) < cooldown_sec:
            log.warning("(tekrarlanan hata, Telegram'a gönderilmedi) %s", _strip_html(text).replace("\n", " | "))
            return False
        self._last_error_sent[key] = now
        return self.send(text)


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
}


class Messages:
    @staticmethod
    def started(cfg, capital: float, realized: float, pairs: int, slots: int = 1) -> str:
        mode = "🧪 TEST (sanal bakiye, gerçek para kullanılmaz)" if cfg.is_paper else "💵 CANLI (gerçek para!)"
        trailing = (f"+%{cfg.trailing_activation_pct:g} sonrası zirve kârın yarısı (en az +%{cfg.trailing_floor_pct:g})"
                    if cfg.trailing_dynamic else
                    f"+%{cfg.trailing_activation_pct:g} görülünce, +%{cfg.trailing_floor_pct:g}'e düşerse sat")
        hold = f"\n⏱️ Maks. tutma süresi: {cfg.max_hold_minutes} dk" if cfg.max_hold_minutes > 0 else ""
        return (
            f"🤖 <b>Kripto Al-Sat Botu Başlatıldı</b>\n{LINE}\n"
            f"⚙️ Mod: {mode}\n"
            f"💰 Güncel Bakiye: <b>{fmt_usdt(capital)} {cfg.quote_asset}</b>"
            f" (K/Z: {fmt_signed(realized)})\n"
            f"🎯 Kâr Al: +%{cfg.take_profit_pct:g} | 🛑 Zarar Kes: -%{cfg.stop_loss_pct:g}\n"
            f"📈 İzleyen Stop: {trailing}\n"
            f"💸 Komisyon: %{cfg.fee_rate * 100:.2f} (alış + satış, net hesaplanır)\n"
            f"☠️ Kill-switch: toplam {fmt_usdt(cfg.max_total_loss)} {cfg.quote_asset} zarar{hold}\n"
            f"📦 Aynı anda pozisyon: şu an <b>{slots}</b> (ayar: en fazla {cfg.max_open_positions}, "
            f"bakiye arttıkça otomatik artar)\n"
            f"🔍 Tarama: {cfg.scan_interval_sec} sn | {pairs} parite"
        )

    @staticmethod
    def buy(pos, levels: Dict[str, float], note: str, cfg, open_count: int = 1, slots: int = 1) -> str:
        q = cfg.quote_asset
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
            f"🛑 Zarar Kes (-%{cfg.stop_loss_pct:g}): {fmt_price(levels['sl'])} → ≈{fmt_usdt(levels['sl_value'])} {q}\n"
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
    def kill_switch(total_pnl: float, balance: float, cfg, positions_left: Optional[List[str]] = None) -> str:
        warn = (f"\n⚠️ <b>Satılamayan pozisyon: {esc(', '.join(positions_left))}</b> — Binance'ten elle "
                f"kontrol edin." if positions_left else "")
        return (
            f"🛑☠️ <b>KILL-SWITCH TETİKLENDİ!</b>\n{LINE}\n"
            f"Toplam zarar {fmt_usdt(-total_pnl)} {cfg.quote_asset} ile limite "
            f"({fmt_usdt(cfg.max_total_loss)} {cfg.quote_asset}) ulaştı.\n"
            f"⛔ Tüm işlemler durduruldu, bot kapatılıyor.\n"
            f"💰 Son Bakiye: <b>{fmt_usdt(balance)} {cfg.quote_asset}</b>{warn}\n"
            f"🔒 Yeniden başlatmak için: <code>python main.py --reset-state</code>"
        )

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
    def stopped(balance: float, realized: float, cfg, open_symbols: Optional[List[str]] = None) -> str:
        warn = (f"\n⚠️ Açık pozisyon var: <b>{esc(', '.join(open_symbols))}</b> — bot kapalıyken kâr al / "
                f"zarar kes ÇALIŞMAZ! Tekrar başlatınca takip kaldığı yerden devam eder." if open_symbols else "")
        return (f"👋 <b>Bot durduruldu</b>\n"
                f"💰 Güncel Bakiye: {fmt_usdt(balance)} {cfg.quote_asset} (K/Z: {fmt_signed(realized)}){warn}")
