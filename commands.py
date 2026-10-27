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
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional

import requests

from logger import get_logger, redact
from config import is_rotation
from risk import ExitReason, trailing_floor_pct
from telegram_bot import (API_URL, LINE, Messages, esc, fmt_duration, fmt_price, fmt_qty, fmt_signed,
                          fmt_usdt)

if TYPE_CHECKING:  # pragma: no cover
    from trader import TradingBot

log = get_logger()

CONFIRM_TIMEOUT_SEC = 60
STRATEGY_LABELS = {"rebound": "rebound (düşen coinde dip)", "trend": "trend (yükselişte geri çekilme)",
                   "breakout": "breakout (zirve kırılımı)",
                   "swing": "swing (uzun vade: çok günlük zirve kırılımı, 1 saatlik mumlar)",
                   "swing_pullback": "swing_pullback (uzun vade: trendde geri çekilme, 1 saatlik mumlar)",
                   "mean_revert": "mean_revert (büyük coinlerde trend içi aşırı satım alımı, küçük hedef, 1 saatlik mumlar)",
                   "rsi_divergence": "rsi_divergence (büyük coinlerde pozitif RSI uyumsuzluğu + dönüş teyidi, küçük hedef, "
                                     "1 saatlik mumlar)",
                   "rotation": "rotation (haftalık momentum rotasyonu, günlük mumlar)"}

# Telegram'ın "/" menüsünde görünecek komutlar
COMMAND_MENU = [
    ("menu", "Kontrol paneli (düğmeli menü)"),
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
    ("sermayeekle", "Hesaba eklediğiniz parayı bota tanıtın: /sermayeekle 10"),
    ("ayarlar", "Geçerli kurallar ve ayarlar"),
    ("ayar", "Ayar değiştir: /ayar tp 2.5"),
    ("piyasa", "Piyasayı tara: dipte olup hacmi artan coinler"),
    ("gecikme", "Binance'e ağ gecikmesi (ms)"),
    ("loglar", "Son 15 log satırı"),
    ("ip", "Sunucunun dış IP adresi"),
    ("pano", "Canlı K/Z panosu (aç / kapat)"),
    ("backtest", "Backtest menüsü: ayarla, başlat, sonuçları al"),
    ("toz", "Küçük bakiyeleri (toz) BNB'ye çevir"),
    ("ping", "Bot çalışıyor mu?"),
    ("yardim", "Komut listesi"),
]


@dataclass
class Command:
    name: str
    args: List[str] = field(default_factory=list)
    at: float = field(default_factory=time.time)
    kind: str = "command"      # command: yazılan komut | callback: menü düğmesine basıldı (args = düğme verisi)
    message_id: int = 0        # callback: düğmenin bulunduğu mesaj (menü yerinde güncellenir)


CALLBACK = "menü"              # Düğme basımlarının komut adı (loglarda görünür; yazılarak çağrılamaz)


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
        self.username = ""  # Botun Telegram kullanıcı adı (/komut@BaskaBot ayırt etmek için)

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
        me = self._call("getMe").get("result")
        self.username = str(me.get("username") or "").lower() if isinstance(me, dict) else ""
        self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMAND_MENU]})

    def _run(self) -> None:
        backoff = 2
        ready = False
        while not self._stop.is_set():
            try:
                if not ready:
                    self._setup()
                    ready = True
                params = {"timeout": self.poll_timeout, "allowed_updates": ["message", "callback_query"]}
                if self._offset is not None:
                    params["offset"] = self._offset
                data = self._call("getUpdates", params, timeout=self.poll_timeout + 10)
                if not data.get("ok"):
                    wait = (data.get("parameters") or {}).get("retry_after")
                    if wait:  # 429: Telegram'ın istediği kadar bekle
                        self._stop.wait(float(wait))
                        continue
                    code = data.get("error_code")
                    if code in (401, 404):  # Token geçersiz/iptal edilmiş: uzun bekle, spam yapma
                        log.error("Telegram komutları alınamıyor: bot token'ı geçersiz (%s). .env'deki "
                                  "TELEGRAM_BOT_TOKEN'ı kontrol edin.", data.get("description"))
                        self._stop.wait(300)
                        continue
                    raise RuntimeError(f"getUpdates başarısız: {data.get('description')}")
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
        if update.get("callback_query"):
            self._handle_callback(update["callback_query"])
            return
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        cmd = parse_command(text)
        if cmd is None:
            return
        head = text.split(maxsplit=1)[0]
        if "@" in head and self.username and head.split("@", 1)[1].lower() != self.username:
            return  # Gruptaki başka bir bota yazılmış komut
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

    def _handle_callback(self, query: dict) -> None:
        """Menü düğmesine basıldı. Komutlardaki gibi YALNIZCA TELEGRAM_CHAT_ID sohbetindeki düğmeler kabul edilir."""
        msg = query.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        data = str(query.get("data") or "")
        if chat_id != self.chat_id:
            log.warning("Yetkisiz sohbetten düğme basımı reddedildi (chat %s)", chat_id or "?")
            self._answer(query.get("id"), "⛔ Bu bot size ait değil.")
            return
        self._answer(query.get("id"))   # Telegram'daki "yükleniyor" göstergesini hemen kapat
        if not data:
            return
        self.commands.put(Command(CALLBACK, data.split(":"), kind="callback",
                                  message_id=int(msg.get("message_id") or 0)))
        self.wakeup.set()

    def _answer(self, query_id, text: str = "") -> None:
        if not query_id:
            return
        try:
            self._call("answerCallbackQuery", {"callback_query_id": query_id, **({"text": text} if text else {})})
        except Exception as exc:  # noqa: BLE001
            log.debug("Düğme cevabı gönderilemedi: %s", redact(str(exc)))


class ConflictError(Exception):
    pass


@dataclass
class PendingConfirm:
    action: str            # sell | sellall | close | addcap | restart | strategy (symbol = strateji adı)
    symbol: str = ""
    amount: float = 0.0
    expires: float = 0.0
    token: str = ""        # Onay düğmesinin kimliği: eski bir mesajdaki düğme sonraki bir işlemi onaylayamaz


class CommandProcessor:
    """Komutları TradingBot üzerinde çalıştırır (ana döngüden çağrılır)."""

    def __init__(self, bot: "TradingBot", time_fn=time.time):
        self.bot = bot
        self.time_fn = time_fn
        self.pending: Optional[PendingConfirm] = None
        self._try_rate: Optional[float] = None
        self._try_at = 0.0
        from control_panel import ControlPanel   # döngüsel içe aktarmayı önlemek için
        self.panel = ControlPanel(self)

    # ------------------------------------------------------------ yardımcılar
    def reply(self, text: str) -> None:
        self.bot.notifier.send(text)

    def reply_menu(self, text: str, keyboard, message_id: Optional[int] = None) -> None:
        self.bot.notifier.send_menu(text, keyboard, message_id)

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
        if cmd.kind == "callback":
            try:
                self.panel.handle_callback(cmd)
            except Exception as exc:  # noqa: BLE001 - tek düğme hatası botu durdurmasın
                log.exception("Menü hatası: %s", ":".join(cmd.args))
                self.reply(Messages.error(f"Menü işlemi yapılamadı: {exc}"))
            return
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
        if bot.freeze_note():
            notes.append(esc(bot.freeze_note()) + " — /menu → Risk")
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
            targets = (f"🛑 Felaket zarar kesi: {fmt_price(lv['sl'])} | 🔄 sıralamadan düşünce satılır"
                       + (f" ({esc(pos.signal_note)})" if pos.signal_note else "")
                       if is_rotation(bot.cfg.strategy) else
                       f"🎯 TP: {fmt_price(lv['tp'])} | 🛑 SL: {fmt_price(lv['sl'])} | "
                       f"📈 İz. başlar: {fmt_price(lv['trail_on'])}")
            parts.append(
                f"<b>{esc(pos.symbol)}</b> — {fmt_duration(now - pos.opened_at)}\n"
                f"🪙 {fmt_qty(pos.qty)} | Alış: {fmt_price(pos.entry_price)} | Maliyet: {fmt_usdt(pos.cost_usdt)} {q}\n"
                f"{status}\n"
                f"{targets}"
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
            f"Kill-switch'e kalan: {fmt_usdt(bot.loss_limit + bot.state.realized_pnl + unrealized)} {q}",
        ]
        if bot.state.capital_added:
            lines.append(f"Bota verilen sermaye: {fmt_usdt(bot.state.start_capital)} {q} "
                         f"(sonradan eklenen: {fmt_usdt(bot.state.capital_added)} {q})")
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
            f"🧭 Strateji: {STRATEGY_LABELS.get(cfg.strategy, cfg.strategy)}\n"
            + (f"🔄 En güçlü {cfg.rotation_top_n} coin | momentum {cfg.rotation_lookback_days} gün | her "
               f"{cfg.rotation_days} günde dengeleme | "
               + (f"BTC {cfg.rotation_btc_ma_days} günlük ortalama altında USDT" if cfg.rotation_btc_ma_days
                  else "BTC trend filtresi kapalı") + "\n"
               f"🛑 Felaket zarar kesi -%{cfg.stop_loss_pct:g} (kâr al / izleyen stop / başabaş / maks. süre "
               f"kullanılmaz)\n"
               if is_rotation(cfg.strategy) else
               f"🎯 Kâr Al +%{cfg.take_profit_pct:g} | 🛑 Zarar Kes -%{cfg.stop_loss_pct:g}\n"
               f"📈 İzleyen stop: +%{cfg.trailing_activation_pct:g} → +%{cfg.trailing_floor_pct:g}\n") +
            f"☠️ Kill-switch: sermayenin %{self.bot.loss_pct:.4g}'i "
            f"(şu an {fmt_usdt(self.bot.loss_limit)} {cfg.quote_asset})\n"
            f"📦 En fazla pozisyon: {cfg.max_open_positions} (şu an {self.bot.target_slots()})\n"
            f"🧷 Binance yedek stop: {on(cfg.backup_stop_enabled)} (-%{cfg.backup_stop_pct:g})\n"
            + (f"📉 BTC çöküş çıkışı: {on(cfg.btc_crash_exit)} | ↔️ Maks. spread: %{cfg.max_spread_pct:g}\n"
               f"{esc(self.bot.fee_status_line())}\n"
               f"⏱️ Kontrol: {cfg.scan_interval_sec} sn'de bir (kararlar UTC 00:05 sonrası)\n"
               if is_rotation(cfg.strategy) else
               f"🔒 Başabaş: {on(cfg.breakeven_enabled)} | 📉 BTC çöküş çıkışı: {on(cfg.btc_crash_exit)}\n"
               f"↔️ Maks. spread: %{cfg.max_spread_pct:g} | 🌡️ Piyasa rejimi filtresi: {on(cfg.regime_filter)}\n"
               f"{esc(self.bot.fee_status_line())}\n"
               f"⏱️ Tarama: {cfg.scan_interval_sec} sn | Maks. tutma: "
               f"{fmt_duration(cfg.max_hold_minutes * 60, exact=True) if cfg.max_hold_minutes else 'sınırsız'}\n")
            + self._extras_line()
            + "<i>Ayarları /menu'den (veya /ayar ile) değiştirebilirsiniz; değişiklik sunucudaki .env'ye kaydedilir.</i>")

    def _extras_line(self) -> str:
        """/ayarlar: ek filtreler, risk sigortaları, yedek akçe ve bildirim modu."""
        from control_panel import FILTER_KEYS, NOTIFY_LABELS
        from runtime_settings import EDITABLE
        cfg = self.bot.cfg
        extra = [EDITABLE[k].label for k in FILTER_KEYS[2:] if getattr(cfg, EDITABLE[k].attr)]
        guards = []
        if cfg.daily_loss_limit_pct > 0:
            guards.append(f"günlük zarar -%{cfg.daily_loss_limit_pct:g}")
        if cfg.daily_profit_target_pct > 0:
            guards.append(f"günlük hedef +%{cfg.daily_profit_target_pct:g}")
        if cfg.loss_streak_limit > 0:
            guards.append(f"{cfg.loss_streak_limit} SL → {cfg.loss_streak_pause_hours:g} sa ara")
        return (f"🛡️ Ek filtreler: {esc(', '.join(extra)) if extra else 'kapalı'} | 🧯 Sigortalar: "
                f"{esc(', '.join(guards)) if guards else 'kapalı'}\n"
                f"💰 İlk giriş: %{cfg.capital_usage_pct:g} | 🔔 Bildirim: {NOTIFY_LABELS.get(cfg.notify_mode)}\n"
                + (f"{esc(self.bot.freeze_note())}\n" if self.bot.freeze_note() else ""))

    def cmd_menu(self, args) -> None:
        self.panel.show_main()

    cmd_panel = cmd_menu

    def cmd_ayar(self, args) -> None:
        self.panel.set_from_text(args)

    def cmd_piyasa(self, args) -> None:
        self.panel.market_scan()

    def cmd_gecikme(self, args) -> None:
        self.panel.latency()

    def cmd_loglar(self, args) -> None:
        self.panel.logs()

    cmd_log = cmd_loglar

    def cmd_ip(self, args) -> None:
        self.panel.ip_info()

    def cmd_pano(self, args) -> None:
        smart = self.bot.smart
        if smart.board is not None or (args and ascii_tr(args[0]) in ("kapat", "off", "dur")):
            smart.board_off()
        else:
            smart.board_on()

    def cmd_toz(self, args) -> None:
        self.bot.smart.show_dust()

    def cmd_backtest(self, args) -> None:
        self.panel.backtest.show()

    cmd_bt = cmd_backtest

    # ------------------------------------------------------------ kontrol komutları
    def cmd_durdur(self, args) -> None:
        self.bot.set_paused(True)
        if is_rotation(self.bot.cfg.strategy):
            self.reply("⏸️ <b>Yeniden dengeleme ertelendi</b> (sıralamaya göre alım ve satış yapılmaz).\n"
                       "Felaket zarar kesi ve BTC trend filtresi satışları sürüyor. Devam için /devam yazın.")
            return
        self.reply("⏸️ <b>Yeni alımlar durduruldu.</b>\nAçık pozisyonların kâr al / zarar kes takibi sürüyor. "
                   "Devam etmek için /devam yazın.")

    def cmd_devam(self, args) -> None:
        self.bot.set_paused(False)
        if is_rotation(self.bot.cfg.strategy):
            state, cfg = self.bot.state, self.bot.cfg
            due_day = state.last_rebalance_day + cfg.rotation_days
            if state.last_rebalance_day < 0 or self.bot.rotation_day(self.time_fn()) >= due_day:
                when = "zamanı geldiği için birkaç saniye içinde yapılacak"
            else:
                when = f"{(date(1970, 1, 1) + timedelta(days=due_day)).strftime('%d.%m.%Y')} UTC 00:05'ten sonra yapılacak"
            self.reply(f"▶️ <b>Rotasyona devam ediliyor.</b> Yeniden dengeleme {when}.")
            return
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

    def cmd_sermayeekle(self, args) -> None:
        bot, q = self.bot, self.bot.cfg.quote_asset
        usage = (f"Kullanım: /sermayeekle 10  → Binance Spot hesabınıza eklediğiniz {q} tutarını bota tanıtır.\n"
                 f"Bot normalde yalnızca kendisine verilen sermayeyle işlem yapar; para eklemek tek başına yetmez.")
        try:
            amount = float(args[0].replace(",", ".")) if args else 0.0
        except ValueError:
            amount = 0.0
        if not amount > 0 or amount != amount or amount == float("inf"):
            self.reply(usage)
            return
        problem = self._capital_problem(amount)
        if problem:
            self.reply(problem)
            return
        new_base = bot.state.start_capital + amount
        new_limit = bot.cfg.loss_limit(new_base, bot.state.capital_added + amount)
        detail = (f"\n💼 Bota verilen sermaye: {fmt_usdt(bot.state.start_capital)} → <b>{fmt_usdt(new_base)} {q}</b>"
                  f"\n☠️ Kill-switch limiti: {fmt_usdt(bot.loss_limit)} → "
                  f"<b>{fmt_usdt(new_limit)} {q}</b> (%{new_limit / new_base * 100:.4g})"
                  + ("\n🧪 Test modu: tutar sanal bakiyeye eklenecek." if bot.exchange.is_paper else ""))
        self._ask(PendingConfirm("addcap", amount=amount), detail,
                  f"Bota <b>{fmt_usdt(amount)} {q}</b> sermaye eklensin mi?")

    def _capital_problem(self, amount: float) -> Optional[str]:
        """Sermaye eklenemiyorsa nedenini (Telegram mesajı) döndürür, eklenebiliyorsa None."""
        bot, q = self.bot, self.bot.cfg.quote_asset
        if bot.state.killed:
            return "⛔ Kill-switch tetiklenmiş; sermaye eklenemez. Önce durumu sıfırlayın (--reset-state)."
        if bot.exchange.is_paper:
            return None
        try:
            pending = bot.unreconciled_reason()
            if pending:
                return (f"⏳ Şu an sermaye eklenemiyor: {esc(pending)}. Bot bunu işledikten sonra "
                        f"(genelde 1 dakika içinde) tekrar deneyin.")
            spare = bot.unallocated_balance()
        except Exception as exc:  # noqa: BLE001
            return Messages.error(f"Binance bakiyesi okunamadı, sermaye eklenmedi: {exc}")
        if spare is not None and amount > spare + 0.01:
            return (f"⚠️ Spot cüzdanında bota ayrılmamış serbest bakiye yalnızca "
                    f"<b>{fmt_usdt(max(0.0, spare))} {q}</b>.\nÖnce parayı Binance'te Spot cüzdanına "
                    f"aktarın, sonra tekrar deneyin.")
        return None

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
        elif pending.action == "restart":
            self.panel.restart("Telegram'dan yeniden başlatma istendi")
        elif pending.action == "strategy":
            self.panel.apply_strategy(pending.symbol)
        elif pending.action == "question":
            token, _, choice = pending.symbol.partition(":")
            self.bot.smart.answer(token, choice, confirmed=True)
        elif pending.action == "addcap":
            bot, q = self.bot, self.bot.cfg.quote_asset
            problem = self._capital_problem(pending.amount)  # Onay beklerken bakiye değişmiş olabilir
            if problem:
                self.reply(problem)
                return
            bot.add_capital(pending.amount)
            self.reply(f"✅ <b>{fmt_usdt(pending.amount)} {q} sermaye eklendi.</b>\n"
                       f"💼 Bota verilen sermaye: {fmt_usdt(bot.state.start_capital)} {q} | "
                       f"Güncel: {fmt_usdt(bot.state.capital)} {q}\n"
                       f"☠️ Kill-switch limiti: {fmt_usdt(bot.loss_limit)} {q} | "
                       f"📦 Pozisyon hakkı: {bot.slots()}")

    def cmd_hayir(self, args) -> None:
        had = self.pending is not None
        self.pending = None
        self.reply("❌ İptal edildi." if had else "ℹ️ İptal edilecek bir işlem yok.")

    cmd_iptal = cmd_hayir

    def _ask(self, pending: PendingConfirm, detail: str, question: str) -> None:
        pending.expires = self.time_fn() + CONFIRM_TIMEOUT_SEC
        pending.token = secrets.token_hex(4)
        self.pending = pending
        pin = bool(self.bot.cfg.command_pin)
        text = (f"❓ {question}{detail}\n\n✅ Onaylamak için {CONFIRM_TIMEOUT_SEC} sn içinde "
                f"<b>/evet{' PIN' if pin else ''}</b>, vazgeçmek için /hayir yazın.")
        # PIN ayarlıysa onay yalnızca "/evet PIN" yazılarak verilir (düğmeyle PIN girilemez)
        no = ("❌ Hayır", f"no:{pending.token}")
        buttons = [[no]] if pin else [[("✅ Evet", f"yes:{pending.token}"), no]]
        self.reply_menu(text, buttons)

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
