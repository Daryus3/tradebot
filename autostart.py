"""
Windows açılışında otomatik başlatma.

Kullanıcının Başlangıç (Startup) klasörüne, botu bekçiyle (watchdog.py) başlatan küçük bir
.bat dosyası yazar / siler. Yönetici izni gerekmez; oturum açılınca bot bir konsol penceresinde
açılır. Canlı (live) modda otomatik başlatma, açılışta onay sorulmayacağı için ayrıca izin ister.
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Mapping, Optional

from config import BASE_DIR

STARTUP_FILENAME = "kripto_bot_baslat.bat"
VALID_MODES = ("paper", "live")


def startup_dir(environ: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Optional[Path]:
    """Windows'ta %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup; değilse None."""
    platform = sys.platform if platform is None else platform
    if not platform.startswith("win"):
        return None
    appdata = (os.environ if environ is None else environ).get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def build_startup_script(mode: str, project_dir: Path) -> str:
    """Başlangıç .bat içeriği (CRLF satır sonlarıyla)."""
    project = str(project_dir).replace("%", "%%")  # cmd, %...% kısımlarını değişken sanmasın
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "rem Kripto Al-Sat Botu - Windows açılışında otomatik başlatma.",
        "rem Kaldırmak için proje klasöründe: python watchdog.py --remove-autostart",
        f"title Kripto Bot - otomatik başlatma - {mode}",
        f'cd /d "{project}"',
        'if exist "venv\\Scripts\\activate.bat" call "venv\\Scripts\\activate.bat"',
        f"python watchdog.py --mode {mode} --yes",
        "pause",
    ]
    return "\r\n".join(lines) + "\r\n"


def install_autostart(mode: str = "paper", allow_live: bool = False, target_dir: Optional[Path] = None,
                      project_dir: Path = BASE_DIR) -> Path:
    """Başlangıç klasörüne kripto_bot_baslat.bat yazar ve yolunu döndürür."""
    mode = (mode or "").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"Geçersiz mod: {mode!r}. 'paper' veya 'live' olmalı.")
    if mode == "live" and not allow_live:
        raise ValueError(
            "Canlı (live) modda otomatik başlatma güvenlik nedeniyle kapalı: bilgisayar açılınca bot "
            "onay sormadan GERÇEK parayla işlem yapar. Gerçekten istiyorsanız --allow-live-autostart ekleyin.")

    directory = Path(target_dir) if target_dir is not None else startup_dir()
    if directory is None:
        project = shlex.quote(str(project_dir))
        raise RuntimeError(
            "Otomatik başlatma yalnızca Windows'ta destekleniyor (Başlangıç klasörü bulunamadı).\n"
            "   Linux/macOS'ta cron veya systemd kullanın. Örn. 'crontab -e' ile şu satırı ekleyin:\n"
            f"     @reboot cd {project} && venv/bin/python watchdog.py --mode {mode} --yes\n"
            "   veya bir systemd servisi tanımlayın:\n"
            f"     ExecStart={project_dir}/venv/bin/python {project_dir}/watchdog.py --mode {mode} --yes")

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STARTUP_FILENAME
    # BOM'suz UTF-8: BOM olursa cmd ilk satırı (@echo off) tanımaz
    path.write_bytes(build_startup_script(mode, Path(project_dir)).encode("utf-8"))
    return path


def remove_autostart(target_dir: Optional[Path] = None) -> bool:
    """Başlangıç dosyasını siler; silindiyse True, zaten yoksa False."""
    directory = Path(target_dir) if target_dir is not None else startup_dir()
    if directory is None:
        return False
    path = directory / STARTUP_FILENAME
    if not path.exists():
        return False
    path.unlink()
    return True
