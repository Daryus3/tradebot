"""Sunucu durumu (Telegram: /sistem, menü: 🖥️ Sunucu Durumu): bellek (RAM, takas), botun kullandığı bellek, işlemci
yükü, disk, açık kalma süresi ve sıcaklık. Yalnızca okur; ek paket gerekmez (Linux'ta /proc ve /sys). Sanal sunucularda
(VPS) donanım sıcaklığı genelde görünmez: o durumda bu açıkça yazılır."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RAM_WARN_FREE_PCT = 10.0       # Kullanılabilir bellek bundan azsa uyarı
DISK_WARN_FREE_PCT = 10.0      # Diskte boş yer bundan azsa uyarı
TEMP_WARN_C = 80.0             # Bu sıcaklık ve üstü uyarı
TEMP_SHOW = 6                  # En çok bu kadar sensör yazılır (en sıcaklar)


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def meminfo(proc: Path) -> Dict[str, int]:
    """/proc/meminfo → {alan: bayt} (MemTotal, MemAvailable, SwapTotal, SwapFree...). Okunamazsa boş."""
    out: Dict[str, int] = {}
    for line in (_read(proc / "meminfo") or "").splitlines():
        key, _sep, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key.strip()] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1)
    return out


def process_rss(proc: Path) -> Optional[int]:
    """Bu sürecin (botun) bellekte kapladığı yer (bayt; /proc/self/status VmRSS)."""
    for line in (_read(proc / "self" / "status") or "").splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def loadavg(proc: Path) -> Optional[Tuple[float, float, float]]:
    parts = (_read(proc / "loadavg") or "").split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return None


def uptime_sec(proc: Path) -> Optional[float]:
    parts = (_read(proc / "uptime") or "").split()
    try:
        return float(parts[0])
    except (IndexError, ValueError):
        return None


def temperatures(sys_root: Path) -> List[Tuple[str, float]]:
    """(sensör adı, °C): /sys/class/thermal/thermal_zone*/temp ve /sys/class/hwmon/hwmon*/temp*_input (milidere).
    Anlamsız değerler (≤ −40 ya da ≥ 150 °C) atılır."""
    temps: List[Tuple[str, float]] = []
    for zone in sorted((sys_root / "class" / "thermal").glob("thermal_zone*")):
        raw = _read(zone / "temp")
        try:
            value = int(raw or "") / 1000
        except ValueError:
            continue
        temps.append((_read(zone / "type") or zone.name, value))
    for hw in sorted((sys_root / "class" / "hwmon").glob("hwmon*")):
        base = _read(hw / "name") or hw.name
        for item in sorted(hw.glob("temp*_input")):
            try:
                value = int(_read(item) or "") / 1000
            except ValueError:
                continue
            label = _read(item.with_name(item.name.replace("_input", "_label")))
            temps.append((f"{base} {label}" if label else base, value))
    return [(name, t) for name, t in temps if -40 < t < 150]


def read(proc: str = "/proc", sys_root: str = "/sys", disk_path: str = ".") -> dict:
    """Sunucu ölçüleri (okunamayan alanlar None / boş)."""
    p = Path(proc)
    mem = meminfo(p)
    try:
        disk = shutil.disk_usage(disk_path)
    except OSError:
        disk = None
    return {"mem": mem, "rss": process_rss(p), "load": loadavg(p), "cpus": os.cpu_count(),
            "uptime": uptime_sec(p), "disk": disk, "temps": temperatures(Path(sys_root))}


def _size(n: Optional[float]) -> str:
    if n is None:
        return "?"
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n / 1024:.0f} KB"


def _duration(sec: float) -> str:
    days, rem = divmod(int(sec), 86400)
    hours, rem = divmod(rem, 3600)
    return f"{days} gün {hours} sa" if days else f"{hours} sa {rem // 60} dk"


def format_text(info: dict) -> str:
    """Telegram mesajı (HTML)."""
    lines = ["🖥️ <b>Sunucu Durumu</b>"]
    warn: List[str] = []
    mem = info.get("mem") or {}
    total, avail = mem.get("MemTotal"), mem.get("MemAvailable", mem.get("MemFree"))
    if total and avail is not None:
        used = total - avail
        lines.append(f"🧠 RAM: <b>{_size(used)}</b> / {_size(total)} kullanılıyor (%{used / total * 100:.0f}) | "
                     f"kullanılabilir {_size(avail)}")
        if avail / total * 100 < RAM_WARN_FREE_PCT:
            warn.append(f"⚠️ Kullanılabilir bellek az (%{avail / total * 100:.0f}).")
    else:
        lines.append("🧠 RAM: okunamadı")
    swap_total, swap_free = mem.get("SwapTotal"), mem.get("SwapFree")
    if swap_total:
        lines.append(f"💤 Takas alanı (swap): {_size(swap_total - (swap_free or 0))} / {_size(swap_total)} kullanılıyor")
    elif total:
        lines.append("💤 Takas alanı (swap): yok")
    if info.get("rss"):
        lines.append(f"🤖 Botun kendisi: {_size(info['rss'])}")
    load, cpus = info.get("load"), info.get("cpus")
    if load:
        cores = f" ({cpus} çekirdek)" if cpus else ""
        lines.append(f"⚙️ İşlemci yükü (1 / 5 / 15 dk): {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}{cores}")
    disk = info.get("disk")
    room = (disk.used + disk.free) if disk is not None else 0     # df gibi: ayrılmış / kotalı alan sayılmaz
    if room:
        lines.append(f"💾 Disk: {_size(disk.used)} dolu, {_size(disk.free)} boş (%{disk.used / room * 100:.0f} dolu)")
        if disk.free / room * 100 < DISK_WARN_FREE_PCT:
            warn.append("⚠️ Disk dolmak üzere: backtest önbelleği (data/) yer kaplıyor olabilir.")
    if info.get("uptime"):
        lines.append(f"⏱️ Sunucu açık kalma süresi: {_duration(info['uptime'])}")
    temps = sorted(info.get("temps") or [], key=lambda x: -x[1])
    if temps:
        shown = ", ".join(f"{name} {t:.0f}°C" for name, t in temps[:TEMP_SHOW])
        lines.append(f"🌡️ Sıcaklık: en yüksek <b>{temps[0][1]:.0f}°C</b> ({shown})")
        if temps[0][1] >= TEMP_WARN_C:
            warn.append(f"⚠️ Sıcaklık yüksek ({temps[0][1]:.0f}°C).")
    else:
        lines.append("🌡️ Sıcaklık: sensör görünmüyor. Sanal sunucularda (VPS) donanım sıcaklığı genelde gösterilmez; "
                     "sağlayıcının panelinden bakılır.")
    if load and cpus and load[1] > cpus:
        warn.append(f"⚠️ İşlemci yükü çekirdek sayısının üstünde (5 dk ortalaması {load[1]:.2f}).")
    return "\n".join(lines + warn)
