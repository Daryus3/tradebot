"""
Bekçi (watchdog): botu ayrı bir süreçte çalıştırır, beklenmedik şekilde kapanırsa
kısa bir beklemeden sonra otomatik olarak yeniden başlatır.

Kullanım (watchdog'a özel olmayan tüm argümanlar olduğu gibi main.py'ye aktarılır):
    python watchdog.py --mode paper              # Test modu, çökerse yeniden başlatılır
    python watchdog.py --mode live               # Canlı onayı ilk açılışta sorulur; sonra --yes eklenir
    python watchdog.py --restart-delay 30        # Yeniden başlatmadan önce 30 sn bekle (varsayılan 10)
    python watchdog.py --install-autostart       # Windows açılışında test modunda otomatik başlat
    python watchdog.py --remove-autostart        # Otomatik başlatmayı kaldır

main.py çıkış kodu sözleşmesi:
    0 = normal durdurma (Ctrl+C, Telegram /kapat)  → yeniden başlatılmaz
    1 = ayar / kullanıcı hatası                   → yeniden başlatılmaz
    2 = kill-switch                               → ASLA yeniden başlatılmaz
    3 = çökme / beklenmedik hata                  → yeniden başlatılır
    diğer kodlar (negatif / sinyal dahil)         → yeniden başlatılır
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Deque, List, Optional, Sequence, Tuple

from config import BASE_DIR, LOG_DIR

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_KILL_SWITCH = 2
EXIT_CRASH = 3
NO_RESTART_CODES = frozenset({EXIT_OK, EXIT_USER_ERROR, EXIT_KILL_SWITCH})

MAIN_SCRIPT = BASE_DIR / "main.py"
LOG_FILE = LOG_DIR / "watchdog.log"
DEFAULT_RESTART_DELAY = 10.0
STOP_GRACE_SEC = 15.0       # Ctrl+C sonrası botun kendiliğinden kapanması için süre
TELEGRAM_TIMEOUT = 5.0

Notify = Callable[[str], None]


# ---------------------------------------------------------------- kurallar
def should_restart(exit_code: Optional[int]) -> bool:
    """Çıkış koduna göre botun yeniden başlatılıp başlatılmayacağı (0/1/2 → hayır, diğer → evet)."""
    return exit_code not in NO_RESTART_CODES


class RestartLimiter:
    """Belirli bir zaman penceresindeki yeniden başlatma sayısını sınırlar (sonsuz çökme döngüsünü önler)."""

    def __init__(self, max_restarts: int = 5, window_sec: float = 1800,
                 time_fn: Callable[[], float] = time.time):
        self.max_restarts = max_restarts
        self.window_sec = window_sec
        self.time_fn = time_fn
        self._stamps: Deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= self.window_sec:
            self._stamps.popleft()

    def count(self) -> int:
        """Penceredeki yeniden başlatma sayısı."""
        self._prune(self.time_fn())
        return len(self._stamps)

    def allow(self) -> bool:
        """Sınır aşılmadıysa yeniden başlatmayı kaydedip True döndürür."""
        now = self.time_fn()
        self._prune(now)
        if len(self._stamps) >= self.max_restarts:
            return False
        self._stamps.append(now)
        return True


def build_child_args(user_args: Sequence[str], is_restart: bool) -> List[str]:
    """Alt süreç (main.py) argümanları. Yeniden başlatmada '--yes' eklenir:
    kullanıcı canlı mod onayını ilk açılışta zaten verdi, kimse yokken soru beklenmesin."""
    args = list(user_args)
    if is_restart and "--yes" not in args:
        args.append("--yes")
    return args


def describe_exit_code(code: Optional[int]) -> str:
    """Çıkış kodunu okunur hale getirir: -9 → '-9/SIGKILL', 3221225477 → '3221225477/0xC0000005'."""
    if code is None:
        return "?"
    if code < 0:
        try:
            return f"{code}/{signal.Signals(-code).name}"
        except ValueError:
            return str(code)
    if code > 255:  # Windows NTSTATUS kodları (ör. erişim ihlali)
        return f"{code}/0x{code & 0xFFFFFFFF:08X}"
    return str(code)


# ---------------------------------------------------------------- yardımcılar
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchdog.py", add_help=False, allow_abbrev=False,
        description="Botu çalıştırır; beklenmedik şekilde kapanırsa otomatik yeniden başlatır. "
                    "Diğer tüm argümanlar main.py'ye aktarılır (örn. --mode paper).")
    parser.add_argument("--restart-delay", type=float, default=DEFAULT_RESTART_DELAY,
                        help="Yeniden başlatmadan önce beklenecek saniye (varsayılan 10)")
    parser.add_argument("--install-autostart", action="store_true",
                        help="Windows açılışında botu otomatik başlat (--mode ile mod seçilir, varsayılan paper)")
    parser.add_argument("--remove-autostart", action="store_true", help="Otomatik başlatmayı kaldır")
    parser.add_argument("--allow-live-autostart", action="store_true",
                        help="Canlı (live) modda otomatik başlatmaya izin ver (dikkat: onay sorulmaz!)")
    return parser


def split_args(argv: Sequence[str]) -> Tuple[argparse.Namespace, List[str]]:
    """Watchdog'a özel bayrakları ayırır; kalanlar (main.py argümanları) sırası korunarak döner."""
    return _parser().parse_known_args(list(argv))


def _peek_option(args: Sequence[str], name: str) -> Optional[str]:
    """'--mode live' veya '--mode=live' biçimindeki değeri okur (main.py argümanlarına dokunmadan)."""
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def _log_event(log_file: Optional[Path], text: str) -> None:
    if log_file is None:
        return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {text}\n")
    except OSError:
        pass  # Log yazılamasa da bekçi çalışmaya devam etsin


def _say(text: str, log_file: Optional[Path] = None) -> None:
    print(f"[bekçi] {text}", flush=True)
    _log_event(log_file, text)


def _safe_notify(notify: Notify, text: str) -> None:
    try:
        notify(text)
    except Exception as exc:  # noqa: BLE001 - bildirim hatası bekçiyi durdurmasın
        print(f"[bekçi] ⚠️ Telegram bildirimi gönderilemedi: {exc}", flush=True)


def _restore_environ(saved: dict) -> None:
    for key in set(os.environ) - set(saved):
        del os.environ[key]
    for key, value in saved.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


def telegram_notify(text: str, user_args: Sequence[str]) -> None:
    """Telegram'a senkron bildirim gönderir; hiçbir hata dışarı sızmaz.

    load_config .env değerlerini os.environ'a yazar; bunlar sonraki alt süreçlere miras kalıp
    kullanıcının .env'de yaptığı değişiklikleri gölgelemesin diye ortam geri yüklenir.
    """
    saved = dict(os.environ)
    try:
        from config import load_config
        from telegram_bot import TelegramNotifier

        cfg = load_config(_peek_option(user_args, "--env"), mode_override=_peek_option(user_args, "--mode"))
        prefix = "🧪 <i>[TEST MODU]</i>\n" if cfg.is_paper else ""
        TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, prefix=prefix,
                         timeout=TELEGRAM_TIMEOUT, async_mode=False).send(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[bekçi] ⚠️ Telegram bildirimi gönderilemedi: {exc}", flush=True)
    finally:
        _restore_environ(saved)


class _TerminateRequest(KeyboardInterrupt):
    """SIGTERM alındı (ör. systemd 'stop'); Ctrl+C gibi ele alınır."""


def _install_sigterm_handler() -> None:
    if os.name == "nt":
        return

    def _handler(signum, frame):  # noqa: ARG001
        raise _TerminateRequest()

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        pass


def _wait_child(proc, poll_sec: float = 1.0) -> int:
    """Alt sürecin bitmesini bekler. Kısa aralıklarla bekler ki Windows'ta da Ctrl+C hemen işlensin."""
    while True:
        try:
            return proc.wait(timeout=poll_sec)
        except subprocess.TimeoutExpired:
            continue


def _stop_child(proc, grace: float = STOP_GRACE_SEC, forward_sigint: bool = False) -> None:
    """Ctrl+C sonrası botun güvenli kapanmasını bekler; süre dolarsa sonlandırır.

    Aynı konsolda Ctrl+C alt sürece de gider; SIGTERM gibi yalnız bekçiye gelen durdurma
    isteklerinde ise (forward_sigint) bota SIGINT iletilir ki o da düzgün kapansın.
    """
    if forward_sigint:
        try:
            proc.send_signal(signal.SIGINT)
        except (OSError, ValueError, AttributeError):
            pass
    try:
        proc.wait(timeout=grace)
        return
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        pass
    print("[bekçi] ⚠️ Bot zamanında kapanmadı, sonlandırılıyor...", flush=True)
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        try:
            proc.kill()
        except OSError:
            pass


def _stop_message(code: int) -> str:
    if code == EXIT_OK:
        return "✅ Bot normal şekilde durduruldu (kod 0)."
    if code == EXIT_USER_ERROR:
        return "ℹ️ Bot ayar/kullanıcı hatası nedeniyle kapandı (kod 1); yeniden başlatılmıyor. Yukarıdaki mesaja bakın."
    return ("🛑 Bot kill-switch nedeniyle durdu (kod 2); güvenlik için yeniden BAŞLATILMAYACAK. "
            "Yukarıdaki mesaja bakın.")


def _handle_autostart(opts: argparse.Namespace, user_args: Sequence[str]) -> int:
    import autostart

    if opts.install_autostart and opts.remove_autostart:
        print("❌ --install-autostart ve --remove-autostart birlikte kullanılamaz.")
        return EXIT_USER_ERROR
    if opts.remove_autostart:
        if autostart.remove_autostart():
            print("✅ Otomatik başlatma kaldırıldı.")
        else:
            print("ℹ️ Kayıtlı bir otomatik başlatma bulunamadı.")
        return EXIT_OK

    mode = (_peek_option(user_args, "--mode") or "paper").strip().lower()
    try:
        path = autostart.install_autostart(mode, allow_live=opts.allow_live_autostart)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"❌ {exc}")
        return EXIT_USER_ERROR
    print(f"✅ Otomatik başlatma kuruldu: {path}")
    print(f"   Bilgisayarda oturum açtığınızda bot '{mode}' modunda bekçiyle birlikte başlar.")
    if mode == "live":
        print("   ⚠️ CANLI mod: açılışta onay SORULMAZ, bot gerçek parayla işlem yapar!")
    print("   Kaldırmak için: python watchdog.py --remove-autostart")
    return EXIT_OK


# ---------------------------------------------------------------- ana döngü
def main(argv: Optional[Sequence[str]] = None, *, popen: Optional[Callable] = None,
         sleep: Optional[Callable[[float], None]] = None, notify: Optional[Notify] = None,
         limiter: Optional[RestartLimiter] = None, log_file: Optional[Path] = LOG_FILE) -> int:
    """Botu çalıştırır ve gerekirse yeniden başlatır. Son alt süreç çıkış koduna göre kod döndürür.

    popen/sleep/notify/limiter test için enjekte edilebilir (varsayılan: subprocess.Popen, time.sleep,
    Telegram bildirimi, 30 dk'da en fazla 5 yeniden başlatma).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        _parser().print_help()
        print("\nmain.py seçenekleri için: python main.py --help")
        return EXIT_OK

    opts, user_args = split_args(argv)
    if opts.install_autostart or opts.remove_autostart:
        return _handle_autostart(opts, user_args)

    popen = popen or subprocess.Popen
    sleep = sleep or time.sleep
    notify = notify or (lambda text: telegram_notify(text, user_args))
    limiter = limiter or RestartLimiter()
    delay = max(0.0, opts.restart_delay)

    _say(f"🛡️ Bekçi etkin: bot beklenmedik şekilde kapanırsa {delay:g} sn sonra yeniden başlatılır. "
         f"Durdurmak için Ctrl+C.", log_file)

    proc = None
    is_restart = False
    try:
        while True:
            cmd = [sys.executable, str(MAIN_SCRIPT), *build_child_args(user_args, is_restart)]
            try:
                # stdin miras alınır: canlı mod onay sorusu (EVET) aynı pencerede yanıtlanabilir
                proc = popen(cmd, cwd=str(BASE_DIR))
            except OSError as exc:
                _say(f"❌ Bot başlatılamadı: {exc}", log_file)
                return EXIT_USER_ERROR

            code = _wait_child(proc)
            if not should_restart(code):
                _say(_stop_message(code), log_file)
                return code

            if not limiter.allow():
                minutes = max(1, round(limiter.window_sec / 60))
                msg = (f"⛔ Bot {minutes} dk içinde {limiter.count() + 1} kez çöktü, yeniden başlatma durduruldu. "
                       f"Lütfen logs/bot.log dosyasını kontrol edin. (son kod {describe_exit_code(code)})")
                _say(msg, log_file)
                _safe_notify(notify, msg)
                return code if 0 <= code <= 255 else EXIT_CRASH

            msg = (f"♻️ Bot beklenmedik şekilde kapandı (kod {describe_exit_code(code)}), "
                   f"{delay:g} sn sonra yeniden başlatılıyor "
                   f"[{max(1, round(limiter.window_sec / 60))} dk içinde {limiter.count()}/{limiter.max_restarts}].")
            _say(msg, log_file)
            _safe_notify(notify, msg)
            sleep(delay)
            is_restart = True
    except KeyboardInterrupt as exc:
        if proc is not None and proc.poll() is None:
            _say("⏹ Durdurma isteği alındı; botun güvenli şekilde kapanması bekleniyor "
                 f"(en fazla {STOP_GRACE_SEC:g} sn)...", log_file)
            _stop_child(proc, forward_sigint=isinstance(exc, _TerminateRequest))
        _say("⏹ Bekçi durduruldu.", log_file)
        return EXIT_OK


if __name__ == "__main__":
    # Windows terminallerinde emoji/Türkçe karakterler hata vermesin diye
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    _install_sigterm_handler()
    sys.exit(main())
