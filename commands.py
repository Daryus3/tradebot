"""
Telegram'dan bot kontrolü (iki yönlü komutlar).

- `TelegramCommandListener`: arka planda Telegram'ı dinler (long polling). YALNIZCA .env'deki
  TELEGRAM_CHAT_ID sohbetinden gelen komutları kabul eder; başkası yazarsa reddeder.
  Bot kapalıyken yazılmış eski komutlar açılışta yok sayılır.
- `CommandProcessor`: komutları ana döngüde (iş parçacığı çakışması olmadan) işler.
  Satış ve kapatma gibi riskli komutlar /evet onayı (ve ayarlıysa PIN) ister.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional

import requests

from logger import get_logger, redact
from risk import ExitReason, trailing_floor_pct
from telegram_bot import (API_URL, LINE, Messages, esc, fmt_duration, fmt_price, fmt_qty, fmt_signed,
                          fmt_usdt)

if TYPE_CHECKING:  # pragma: no cover
    from trader import TradingBot

log = get_logger()

CONFIRM_TIMEOUT_SEC = 60

# Telegram'ın "/" menüsünde görünecek komutlar
COMMAND_MENU = [
    ("durum", "Genel durum ve bakiye"),
    ("pozisyon", "Açık pozisyonlar, hedef ve stop fiyatları"),
    ("bakiye", "Bakiye (TL karşılığıyla)"),
    ("islemler", "Son 10 işlem"),
    ("csv", "İşlem geçmişi dosyası"),
    ("istatistik", "Performans istatistikleri"),
    ("rapor", "Özet rapor: /rapor gun | hafta | ay"),
    ("tarama", "Neden alım yapmıyor? Son tarama raporu"),
    ("durdur", "Yeni alımları durdur (açık pozisyon takibi sürer)"),
    ("devam", "Yeni alımlara devam et"),
    ("sat", "Bir coini hemen sat: /sat ETH"),
    ("hepsinisat", "Tüm pozisyonları hemen sat"),
    ("kapat", "Botu güvenle kapat"),
    ("ayarlar", "Geçerli kurallar ve ayarlar"),
    ("ping", "Bot çalışıyor mu?"),
    ("yardim", "Komut listesi"),
]


@dataclass
class Command:
    name: str
    args: List[str] = field(default_factory=list)
    at: float = field(default_factory=time.time)


def parse_command(text: str) -> Optional[Command]:
    """'/sat eth' → Command('sat', ['eth']); '/durum@BotAdi' → Command('durum')."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    name = ascii_tr(parts[0].split("@", 1)[0])
    return Command(name, parts[1:])


_TR_MAP = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iissgguuoocc")


def ascii_tr(text: str) -> str:
    """Türkçe karakterleri sadeleştirip küçük harfe çevirir: '/Yardım' → 'yardim'."""
    return text.translate(_TR_MAP).lower()


class TelegramCommandListener:
    """Telegram'dan gelen komutları arka planda toplar."""

    def __init__(self, token: str, chat_id: str, http=None, poll_timeout: int = 25):
        self.token = token
        self.chat_id = str(chat_id)
        self.http = http or requests.Session()
        self.poll_timeout = poll_timeout
        self.commands: "queue.Queue[Command]" = queue.Queue()
        self.wakeup = threading.Event()  # Komut gelince ana döngünün uykusunu böler
        self._offset: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._rejected: Dict[str, float] = {}
        self.conflict = False

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, params: Optional[dict] = None, timeout: float = 10.0) -> dict:
        url = API_URL.format(token=self.token, method=method)
        resp = self.http.post(url, json=params or {}, timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": False, "description": resp.text[:200]}
        if resp.status_code == 409:
            raise ConflictError(data.get("description", "Conflict"))
        return data

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-commands", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_commands(self) -> List[Command]:
        out = []
        while True:
            try:
                out.append(self.commands.get_nowait())
            except queue.Empty:
                break
        self.wakeup.clear()
        return out

    def _setup(self) -> None:
        # Webhook kuruluysa getUpdates çalışmaz; eski (bot kapalıyken yazılmış) komutları atla
        self._call("deleteWebhook")
        data = self._call("getUpdates", {"offset": -1, "timeout": 0})
        results = data.get("result") or []
        self._offset = results[-1]["update_id"] + 1 if results else None
        self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMAND_MENU]})

    def _run(self) -> None:
        backoff = 2
        ready = False
        while not self._stop.is_set():
            try:
                if not ready:
                    self._setup()
                    ready = True
                params = {"timeout": self.poll_timeout, "allowed_updates": ["message"]}
                if self._offset is not None:
                    params["offset"] = self._offset
                data = self._call("getUpdates", params, timeout=self.poll_timeout + 10)
                for update in data.get("result") or []:
                    self._offset = update["update_id"] + 1
                    self._handle_update(update)
                backoff = 2
                self.conflict = False
            except ConflictError as exc:
                # Aynı token'la başka bir bot (ör. test + canlı aynı anda) çalışıyor
                if not self.conflict:
                    log.warning("Telegram komutları alınamıyor: aynı bot token'ını kullanan başka bir "
                                "program çalışıyor (%s). Test ve canlı mod için farklı bot kullanın.", exc)
                self.conflict = True
                self._stop.wait(30)
            except Exception as exc:  # noqa: BLE001 - dinleyici asla ölmesin
                log.debug("Telegram komut dinleme hatası: %s", redact(str(exc)))
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        cmd = parse_command(msg.get("text", ""))
        if cmd is None:
            return
        if chat_id != self.chat_id:
            log.warning("Yetkisiz sohbetten komut reddedildi (chat %s): /%s", chat_id, cmd.name)
            if time.time() - self._rejected.get(chat_id, 0) > 3600:
                self._rejected[chat_id] = time.time()
                try:
                    self._call("sendMessage", {"chat_id": chat_id, "text": "⛔ Bu bot size ait değil."})
                except Exception:  # noqa: BLE001
                    pass
            return
        self.commands.put(cmd)
        self.wakeup.set()


class ConflictError(Exception):
    pass


@dataclass
class PendingConfirm:
    action: str            # sell | sellall | close
    symbol: str = ""
    expires: float = 0.0


class CommandProcessor:
    """Komutları TradingBot üzerinde çalıştırır (ana döngüden çağrılır)."""

    def __init__(self, bot: "TradingBot", time_fn=time.time):
        self.bot = bot
        self.time_fn = time_fn
        self.pending: Optional[PendingConfirm] = None
        self._try_rate: Optional[float] = None
        self._try_at = 0.0

    # ------------------------------------------------------------ yardımcılar
    def reply(self, text: str) -> None:
        self.bot.notifier.send(text)

    def _find_position(self, query: str):
        q = query.upper().strip()
        positions = self.bot.state.positions
        if q in positions:
            return positions[q]
        for pos in positions.values():
            if pos.base_asset.upper() == q or pos.symbol == f"{q}{self.bot.cfg.quote_asset}":
                return pos
        return None

    def try_rate(self) -> Optional[float]:
        """USDT/TRY kuru (5 dk önbellekli). Bilgi amaçlıdır."""
        if not self.bot.cfg.show_try:
            return None
        now = self.time_fn()
        if self._try_rate is None or now - self._try_at > 300:
            try:
                self._try_rate = float(self.bot.exchange.client.price("USDTTRY"))
                self._try_at = now
            except Exception as exc:  # noqa: BLE001
                log.debug("USDT/TRY kuru alınamadı: %s", exc)
        return self._try_rate

    def _tl(self, usdt: float) -> str:
        rate = self.try_rate()
        return f" (≈{usdt * rate:,.0f} TL)".replace(",", ".") if rate else ""

    # ------------------------------------------------------------ giriş noktası
    def handle(self, cmd: Command) -> None:
        handler = getattr(self, f"cmd_{cmd.name}", None)
        if handler is None:
            self.reply(f"❓ Bilinmeyen komut: /{esc(cmd.name)}\nKomut listesi için /yardim yazın.")
            return
        try:
            handler(cmd.args)
        except Exception as exc:  # noqa: BLE001 - tek komut hatası botu durdurmasın
            log.exception("Komut hatası: /%s", cmd.name)
            self.reply(Messages.error(f"/{cmd.name} çalıştırılamadı: {exc}"))

    # ------------------------------------------------------------ bilgi komutları
    def cmd_yardim(self, args) -> None:
        lines = [f"/{c} — {esc(d)}" for c, d in COMMAND_MENU]
        self.reply("🤖 <b>Komutlar</b>\n" + "\n".join(lines))

    cmd_help = cmd_yardim
    cmd_start = cmd_yardim

    def cmd_ping(self, args) -> None:
        bot = self.bot
        age = self.time_fn() - bot.last_loop_at if bot.last_loop_at else 0
        stream = bot.price_stream
        ws = ("⚡ anlık fiyat akışı bağlı" if stream is not None and stream.connected
              else "🔁 fiyatlar 5 sn'de bir sorgulanıyor")
        paused = " | ⏸️ alımlar durdurulmuş" if bot.state.paused else ""
        self.reply(f"🏓 Çalışıyorum! Son kontrol {age:.0f} sn önce.\n"
                   f"📦 Açık pozisyon: {len(bot.state.positions)} | {ws}{paused}")

    def cmd_durum(self, args) -> None:
        bot = self.bot
        bot.send_status(silent=False)
        notes = []
        if bot.state.paused:
            notes.append("⏸️ Yeni alımlar DURDURULMUŞ (/devam ile açın)")
        if bot.last_scan is not None and bot.last_scan.regime != "NORMAL":
            notes.append(f"🌡️ Piyasa rejimi: {bot.last_scan.regime} — {esc(bot.last_scan.regime_note)}")
        if notes:
            self.reply("\n".join(notes))

    def cmd_pozisyon(self, args) -> None:
        bot = self.bot
        if not bot.state.positions:
            self.reply("📦 Açık pozisyon yok.")
            return
        now = self.time_fn()
        q = bot.cfg.quote_asset
        parts = []
        for pos in bot.state.positions.values():
            m = bot.marks.get(pos.symbol)
            lv = bot.levels(pos)
            status = (f"🏷️ Şu an: {fmt_price(m.bid)} | Net: {fmt_signed(m.pnl_usdt)} {q} ({fmt_signed(m.pnl_pct)}%)"
                      if m else "🏷️ Fiyat henüz alınmadı")
            protect = []
            if pos.trailing_active:
                floor = trailing_floor_pct(pos, bot.cfg)
                protect.append(f"📈 İzleyen stop AKTİF (zirve %{pos.peak_pnl_pct:.1f}, +%{floor:g}'de sat)")
            if pos.breakeven_active:
                protect.append("🔒 Başabaş koruması aktif")
            if pos.backup_order_id:
                protect.append(f"🧷 Binance yedek stop: {fmt_price(pos.backup_stop_price)}")
            parts.append(
                f"<b>{esc(pos.symbol)}</b> — {fmt_duration(now - pos.opened_at)}\n"
                f"🪙 {fmt_qty(pos.qty)} | Alış: {fmt_price(pos.entry_price)} | Maliyet: {fmt_usdt(pos.cost_usdt)} {q}\n"
                f"{status}\n"
                f"🎯 TP: {fmt_price(lv['tp'])} | 🛑 SL: {fmt_price(lv['sl'])} | "
                f"📈 İz. başlar: {fmt_price(lv['trail_on'])}"
                + ("\n" + "\n".join(protect) if protect else ""))
        self.reply(f"📦 <b>Açık Pozisyonlar</b>\n{LINE}\n" + f"\n{LINE}\n".join(parts))

    def cmd_bakiye(self, args) -> None:
        bot = self.bot
        q = bot.cfg.quote_asset
        unrealized = bot.unrealized()
        capital = bot.state.capital
        equity = capital + unrealized
        lines = [
            "💰 <b>Bakiye</b>",
            f"Gerçekleşen bakiye: {fmt_usdt(capital)} {q}{self._tl(capital)}",
            f"Açık pozisyon K/Z: {fmt_signed(unrealized)} {q}",
            f"Toplam (tahmini): <b>{fmt_usdt(equity)} {q}</b>{self._tl(equity)}",
            f"Toplam K/Z: {fmt_signed(bot.state.realized_pnl + unrealized)} {q} | "
            f"Kill-switch'e kalan: {fmt_usdt(bot.cfg.max_total_loss + bot.state.realized_pnl + unrealized)} {q}",
        ]
        if not bot.exchange.is_paper:
            try:
                free = bot.exchange.free_balance(q)
                lines.append(f"Binance Spot serbest {q}: {fmt_usdt(free)}")
            except Exception as exc:  # noqa: BLE001
                log.debug("Spot bakiye okunamadı: %s", exc)
        if self.try_rate():
            lines.append(f"<i>TL karşılığı bilgi amaçlıdır (USDT/TRY ≈ {self.try_rate():.2f}).</i>")
        self.reply("\n".join(lines))

    def cmd_islemler(self, args) -> None:
        import stats
        trades = stats.load_trades(self.bot.trade_log.path)
        if not trades:
            self.reply("📭 Henüz kapanmış işlem yok.")
            return
        self.reply(stats.format_recent_trades(trades, 10, self.bot.cfg.quote_asset))

    def cmd_csv(self, args) -> None:
        path = self.bot.trade_log.path
        if not path.exists():
            self.reply("📭 Henüz işlem dosyası oluşmadı.")
            return
        self.bot.notifier.send_document(path, "📄 İşlem geçmişi (Excel ile açılabilir)")

    def cmd_istatistik(self, args) -> None:
        self.reply(self.bot.stats_text())

    def cmd_rapor(self, args) -> None:
        period = ascii_tr(args[0]) if args else "gun"
        now = datetime.fromtimestamp(self.time_fn())
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period.startswith("haf"):
            start, title = today - timedelta(days=today.weekday()), "🗓️ Bu Haftanın Özeti"
        elif period.startswith("ay"):
            start, title = today.replace(day=1), "🗓️ Bu Ayın Özeti"
        else:
            start, title = today, "📅 Bugünün Özeti"
        self.reply(self.bot.period_report(start, now + timedelta(seconds=1), title))

    def cmd_tarama(self, args) -> None:
        self.reply(self.bot.scan_report())

    def cmd_ayarlar(self, args) -> None:
        cfg = self.bot.cfg
        on = lambda b: "açık" if b else "kapalı"  # noqa: E731
        self.reply(
            "⚙️ <b>Geçerli Ayarlar</b>\n"
            f"Mod: {'🧪 TEST' if cfg.is_paper else '💵 CANLI'}\n"
            f"🎯 Kâr Al +%{cfg.take_profit_pct:g} | 🛑 Zarar Kes -%{cfg.stop_loss_pct:g}\n"
            f"📈 İzleyen stop: +%{cfg.trailing_activation_pct:g} → +%{cfg.trailing_floor_pct:g}\n"
            f"☠️ Kill-switch: {fmt_usdt(cfg.max_total_loss)} {cfg.quote_asset}\n"
            f"📦 En fazla pozisyon: {cfg.max_open_positions} (şu an {self.bot.slots()})\n"
            f"🧷 Binance yedek stop: {on(cfg.backup_stop_enabled)} (-%{cfg.backup_stop_pct:g})\n"
            f"🔒 Başabaş: {on(cfg.breakeven_enabled)} | 📉 BTC çöküş çıkışı: {on(cfg.btc_crash_exit)}\n"
            f"↔️ Maks. spread: %{cfg.max_spread_pct:g} | 🌡️ Piyasa rejimi filtresi: {on(cfg.regime_filter)}\n"
            f"⏱️ Tarama: {cfg.scan_interval_sec} sn | Maks. tutma: "
            f"{str(cfg.max_hold_minutes) + ' dk' if cfg.max_hold_minutes else 'sınırsız'}\n"
            "<i>Ayarlar bilgisayardaki .env dosyasından değiştirilir.</i>")

    # ------------------------------------------------------------ kontrol komutları
    def cmd_durdur(self, args) -> None:
        self.bot.set_paused(True)
        self.reply("⏸️ <b>Yeni alımlar durduruldu.</b>\nAçık pozisyonların kâr al / zarar kes takibi sürüyor. "
                   "Devam etmek için /devam yazın.")

    def cmd_devam(self, args) -> None:
        self.bot.set_paused(False)
        self.reply("▶️ <b>Alımlara devam ediliyor.</b> Bir sonraki taramada fırsat aranacak.")

    def cmd_sat(self, args) -> None:
        if not args:
            self.reply("Kullanım: /sat ETH  (veya /sat ETHUSDT). Hepsini satmak için /hepsinisat")
            return
        pos = self._find_position(args[0])
        if pos is None:
            self.reply(f"❓ {esc(args[0].upper())} için açık pozisyon yok. Açık pozisyonlar için /pozisyon")
            return
        self._ask(PendingConfirm("sell", pos.symbol), self._estimate_line([pos]),
                  f"<b>{esc(pos.symbol)}</b> hemen satılsın mı?")

    def cmd_hepsinisat(self, args) -> None:
        positions = list(self.bot.state.positions.values())
        if not positions:
            self.reply("📦 Satılacak açık pozisyon yok.")
            return
        self._ask(PendingConfirm("sellall"), self._estimate_line(positions),
                  f"<b>{len(positions)} pozisyonun tamamı</b> hemen satılsın mı?")

    def cmd_kapat(self, args) -> None:
        open_note = (f"\n⚠️ {len(self.bot.state.positions)} açık pozisyon var; bot kapalıyken onları yalnızca "
                     f"Binance'teki yedek stop emirleri korur." if self.bot.state.positions else "")
        self._ask(PendingConfirm("close"), open_note,
                  "Bot kapatılsın mı? (Yeniden başlatmak için bilgisayardan açmanız gerekir)")

    def cmd_evet(self, args) -> None:
        pending = self.pending
        if pending is None or self.time_fn() > pending.expires:
            self.pending = None
            self.reply("ℹ️ Onay bekleyen bir işlem yok (veya süresi doldu).")
            return
        pin = self.bot.cfg.command_pin
        if pin and (not args or args[0] != pin):
            self.reply("🔐 PIN hatalı veya eksik. Kullanım: /evet PIN")
            return
        self.pending = None
        if pending.action == "sell":
            pos = self.bot.state.positions.get(pending.symbol)
            if pos is None:
                self.reply(f"ℹ️ {esc(pending.symbol)} pozisyonu zaten kapanmış.")
                return
            if not self.bot.close_position(pos, ExitReason.MANUAL):
                self.reply(f"⚠️ {esc(pending.symbol)} satılamadı, ayrıntı yukarıdaki hata mesajında.")
        elif pending.action == "sellall":
            for pos in list(self.bot.state.positions.values()):
                self.bot.close_position(pos, ExitReason.MANUAL)
        elif pending.action == "close":
            self.reply("👋 Bot kapatılıyor...")
            self.bot.stop()

    def cmd_hayir(self, args) -> None:
        had = self.pending is not None
        self.pending = None
        self.reply("❌ İptal edildi." if had else "ℹ️ İptal edilecek bir işlem yok.")

    cmd_iptal = cmd_hayir

    def _ask(self, pending: PendingConfirm, detail: str, question: str) -> None:
        pending.expires = self.time_fn() + CONFIRM_TIMEOUT_SEC
        self.pending = pending
        pin_hint = " PIN" if self.bot.cfg.command_pin else ""
        self.reply(f"❓ {question}{detail}\n\n✅ Onaylamak için {CONFIRM_TIMEOUT_SEC} sn içinde "
                   f"<b>/evet{pin_hint}</b>, vazgeçmek için /hayir yazın.")

    def _estimate_line(self, positions) -> str:
        q = self.bot.cfg.quote_asset
        lines = []
        for pos in positions:
            m = self.bot.marks.get(pos.symbol)
            if m:
                lines.append(f"• {esc(pos.symbol)}: tahmini net {fmt_signed(m.pnl_usdt)} {q} ({fmt_signed(m.pnl_pct)}%)")
            else:
                lines.append(f"• {esc(pos.symbol)}")
        return "\n" + "\n".join(lines) + "\n<i>Elle satıştaki zarar da kill-switch toplamına sayılır.</i>"
