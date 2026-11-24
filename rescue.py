"""
Açılış bekleme modu: vadeli bot açılışta Binance'e bağlanamazsa (API anahtarı / IP kısıtlaması / izin, ağ hatası,
vadeli cüzdanda USDT yok) kapanmak yerine bekler. Telegram açık kalır, işlem açılmaz, belli aralıklarla yeniden
denenir. Amaç sunucunun başında olmadan telefondan düzeltebilmek: /ip sunucunun şu anki dış IP adresini gösterir, adres
Binance → API Yönetimi'nde eklenir, bot kendiliğinden (ya da 🔄 Şimdi dene ile hemen) bağlanıp normal açılışa devam eder.

Komutlar yalnızca TELEGRAM_CHAT_ID sohbetinden kabul edilir (botun kendi dinleyicisi kullanılır): /ip, /dene, /durum
(/menu, /start, /yardim aynı), /kapat (onaylı). Diğer komutlar bot bağlanınca çalışır.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Optional, Tuple, Type, TypeVar

from logger import get_logger
from telegram_bot import esc

log = get_logger()
T = TypeVar("T")

KEYBOARD = [[("🌐 IP adresi", "rs:ip"), ("🔄 Şimdi dene", "rs:dene")],
            [("📋 Durum", "rs:durum"), ("⏹️ Botu kapat", "rs:kapat")]]
CONFIRM_KEYBOARD = [[("⏹️ Evet, kapat", "rs:kapat:evet"), ("↩️ Vazgeç", "rs:durum")]]
REMIND_SEC = 6 * 3600           # Aynı hata sürerken hatırlatma aralığı
POLL_SEC = 2.0                  # Komut bekleme dilimi (komut gelince hemen uyanır)
STATUS_WORDS = frozenset({"durum", "menu", "start", "yardim", "help", "panel", "ping"})
RETRY_WORDS = frozenset({"dene", "tekrar", "yeniden"})
HELP = ("Bekleme modunda çalışan komutlar: /ip (sunucunun IP adresi), /dene (hemen yeniden dene), /durum, /kapat. "
        "Diğer komutlar bot Binance'e bağlanınca çalışır.")


def problem_hint(text: str) -> str:
    """Hata metnine göre telefondan yapılabilecek düzeltme."""
    if "-2015" in text or "Invalid API-key" in text:
        return ("Binance (telefondan da olur) → Profil → API Yönetimi → bu anahtarda Düzenle: 'Güvenilir IP'lere erişimi "
                "kısıtla' listesine aşağıdaki IPv4 adresini ekleyin, 'Vadeli işlemleri etkinleştir' izni açık olsun. Ev "
                "internetinde dış IP zaman zaman değişir (modem yeniden başlayınca vb.).")
    if "-2014" in text or "-1022" in text or "API-key format" in text or "Signature" in text:
        return ("API anahtarı ya da gizli anahtar hatalı görünüyor (.env'deki BINANCE_API_KEY / BINANCE_API_SECRET); "
                "anahtar Binance'te silindiyse yenisi sunucudaki .env'ye yazılmalı.")
    if "-1021" in text or "Timestamp for this request" in text:
        return "Sunucunun saati kaymış olabilir; saat eşitlenince bot kendiliğinden bağlanır."
    if "USDT yok" in text or "-2019" in text:
        return "Binance'te Cüzdan → Transfer ile Spot'tan 'USDⓈ-M Vadeli' cüzdanına USDT aktarın (telefondan da olur)."
    return "Sunucunun internet bağlantısı ya da Binance'e erişim sorunlu olabilir; bağlantı gelince bot kendiliğinden devam eder."


def fetch_ips() -> Dict[str, str]:
    """Sunucunun şu anki dış IP adresleri ({"IPv4": ..., "IPv6": ...}); alınamazsa boş."""
    from ip_monitor import IpMonitor
    try:
        return IpMonitor(0).check_once()
    except Exception:  # noqa: BLE001 - bekleme modu IP servisine takılmasın
        return {}


def ip_lines(ips: Dict[str, str]) -> str:
    return "\n".join(f"🌐 {family}: <code>{esc(ips.get(family) or 'alınamadı')}</code>" for family in ("IPv4", "IPv6"))


class StartupRescue:
    """Açılış hatasında bekleme döngüsü. run() denemeyi başarana kadar tekrarlar ve sonucunu döndürür; kullanıcı
    /kapat derse None. Ctrl+C / systemctl stop (KeyboardInterrupt) olduğu gibi yukarı geçer."""

    def __init__(self, notifier, listener, retry_sec: float, what: str = "Vadeli bot", time_fn=time.time,
                 sleep_fn=time.sleep, ips_fn: Optional[Callable[[], Dict[str, str]]] = None,
                 remind_sec: float = REMIND_SEC):
        self.notifier = notifier
        self.listener = listener
        self.retry_sec = max(10.0, float(retry_sec))
        self.what = what
        self.time_fn = time_fn
        self.sleep_fn = sleep_fn
        self.ips_fn = ips_fn or fetch_ips
        self.remind_sec = remind_sec
        self.started_at = time_fn()
        self.attempts = 0
        self.last_error = ""
        self.next_try = 0.0
        self.last_notice = 0.0

    # ---------------------------------------------------------------- döngü
    def run(self, attempt: Callable[[], T], first: BaseException, describe: Callable[[BaseException], str],
            retry_on: Tuple[Type[BaseException], ...]) -> Optional[T]:
        self.attempts = 1
        self._failed(describe(first), asked=False)
        if self.listener is not None:
            self.listener.start()
        while True:
            retry_now = False
            for cmd in (self.listener.get_commands() if self.listener is not None else []):
                action = self._handle(cmd)
                if action == "quit":
                    log.warning("Bekleme modu: kullanıcı botu kapattı")
                    self.notifier.send("⏹️ <b>Bot kapatıldı</b> (bekleme modundan). Yeniden başlatmak için sunucuda: "
                                       "<code>sudo systemctl start tradebot</code>", critical=True)
                    return None
                retry_now = retry_now or action == "retry"
            if retry_now or self.time_fn() >= self.next_try:
                self.attempts += 1
                try:
                    value = attempt()
                except retry_on as exc:
                    self._failed(describe(exc), asked=retry_now)
                    continue
                mins = (self.time_fn() - self.started_at) / 60
                log.info("Bekleme modu: bağlantı düzeldi (%d. deneme)", self.attempts)
                self.notifier.send(f"✅ <b>Binance bağlantısı düzeldi</b> ({self.attempts}. deneme, {mins:.0f} dk "
                                   f"sonra). {esc(self.what)} normal açılışa devam ediyor.", critical=True)
                return value
            self._wait(max(0.0, min(POLL_SEC, self.next_try - self.time_fn())))

    def _wait(self, seconds: float) -> None:
        wakeup = getattr(self.listener, "wakeup", None)
        if wakeup is not None:
            wakeup.wait(seconds)            # Komut gelince hemen uyanır
        else:
            self.sleep_fn(seconds)

    # ---------------------------------------------------------------- bildirimler
    def _failed(self, error: str, asked: bool) -> None:
        now = self.time_fn()
        self.next_try = now + self.retry_sec
        changed = error != self.last_error
        self.last_error = error
        log.warning("Bekleme modu (%d. deneme): %s", self.attempts, error.replace("\n", " | "))
        mins = self.retry_sec / 60
        if self.attempts == 1:
            self.last_notice = now
            self.notifier.send_menu(
                f"🛟 <b>Bekleme modu</b>: {esc(self.what)} açılışta Binance'e bağlanamadı ama kapanmadı. Telegram açık, "
                f"işlem açılmıyor.\n<code>{esc(error)[:900]}</code>\n\n🔧 {esc(problem_hint(error))}\n"
                f"{ip_lines(self.ips_fn())}\n\n⏱️ Bot {mins:g} dk'da bir yeniden deniyor; düzelttikten sonra hemen "
                f"denemek için 🔄 Şimdi dene.", KEYBOARD)
            return
        if changed or asked or now - self.last_notice >= self.remind_sec:
            self.last_notice = now
            head = ("❌ Yine bağlanılamadı" if asked and not changed else
                    "⚠️ Bağlantı hâlâ kurulamadı, hata değişti" if changed else "⏳ Hâlâ bekleme modunda")
            self.notifier.send_menu(
                f"{head} ({self.attempts}. deneme).\n<code>{esc(error)[:900]}</code>\n🔧 {esc(problem_hint(error))}\n"
                f"Sonraki deneme {mins:g} dk sonra.", KEYBOARD)

    # ---------------------------------------------------------------- komutlar
    def _handle(self, cmd) -> Optional[str]:
        if getattr(cmd, "kind", "command") == "callback":
            args = list(cmd.args)
            if not args or args[0] != "rs":
                self.notifier.send_menu(HELP, KEYBOARD)
                return None
            action = args[1] if len(args) > 1 else "durum"
            if action == "kapat" and len(args) > 2 and args[2] == "evet":
                return "quit"
        else:
            action = cmd.name
            if action == "kapat" and cmd.args and cmd.args[0].lower() == "evet":
                return "quit"
            if action in RETRY_WORDS:
                action = "dene"
            elif action in STATUS_WORDS:
                action = "durum"
        if action == "ip":
            ips = self.ips_fn()
            self.notifier.send_menu("🌐 <b>Sunucunun şu anki dış IP adresi</b>\n" + ip_lines(ips) +
                                    "\n<i>Binance API anahtarında IP kısıtlaması varsa bu IPv4 adresi orada ekli "
                                    "olmalı.</i>", KEYBOARD)
            return None
        if action == "dene":
            self.notifier.send("🔄 Hemen yeniden deneniyor...")
            return "retry"
        if action == "durum":
            mins = (self.time_fn() - self.started_at) / 60
            left = max(0.0, self.next_try - self.time_fn())
            self.notifier.send_menu(
                f"🛟 <b>Bekleme modu</b>: {mins:.0f} dk'dır Binance'e bağlanılamıyor ({self.attempts} deneme). İşlem "
                f"açılmıyor.\nSon hata: <code>{esc(self.last_error)[:600]}</code>\n🔧 {esc(problem_hint(self.last_error))}"
                f"\n⏱️ Sonraki otomatik deneme {left:.0f} sn sonra.", KEYBOARD)
            return None
        if action == "kapat":
            self.notifier.send_menu("⏹️ Bot kapatılsın mı? Kapanırsa yalnızca sunucuda yeniden başlatılabilir "
                                    "(<code>sudo systemctl start tradebot</code>); bekleme modunda kalırsa sorun "
                                    "düzelince kendiliğinden devam eder.", CONFIRM_KEYBOARD)
            return None
        self.notifier.send_menu(HELP, KEYBOARD)
        return None
