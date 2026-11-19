"""Sunucu durumu (/sistem, menü: 🖥️ Sunucu Durumu): RAM, takas, botun belleği, işlemci yükü, disk, açık kalma süresi ve
sıcaklık. Sahte /proc ve /sys dosyalarıyla; gerçek sunucuya bağlı değil."""
from collections import namedtuple

import system_info as si
from commands import Command
from test_bot_flow import make_bot
from test_control_panel import press
from trader import READ_ONLY_COMMANDS

GB = 1024 ** 3
Disk = namedtuple("Disk", "total used free")


def fake_root(tmp_path, avail_kb=2_000_000, swap=True, temps=True):
    proc, sys_root = tmp_path / "proc", tmp_path / "sys"
    (proc / "self").mkdir(parents=True)
    (proc / "meminfo").write_text(
        "MemTotal:        4000000 kB\nMemFree:          500000 kB\n"
        f"MemAvailable:    {avail_kb} kB\nSwapTotal:       {2_000_000 if swap else 0} kB\n"
        f"SwapFree:        {1_500_000 if swap else 0} kB\n", encoding="utf-8")
    (proc / "self" / "status").write_text("Name:\tpython\nVmRSS:\t  307200 kB\n", encoding="utf-8")
    (proc / "loadavg").write_text("0.35 0.40 0.38 1/120 4321\n", encoding="utf-8")
    (proc / "uptime").write_text("1054800.12 2000000.00\n", encoding="utf-8")
    if temps:
        zone = sys_root / "class" / "thermal" / "thermal_zone0"
        zone.mkdir(parents=True)
        (zone / "temp").write_text("47000\n", encoding="utf-8")
        (zone / "type").write_text("x86_pkg_temp\n", encoding="utf-8")
        hw = sys_root / "class" / "hwmon" / "hwmon1"
        hw.mkdir(parents=True)
        (hw / "name").write_text("coretemp\n", encoding="utf-8")
        (hw / "temp2_input").write_text("52000\n", encoding="utf-8")
        (hw / "temp2_label").write_text("Core 0\n", encoding="utf-8")
        (hw / "temp9_input").write_text("-273000\n", encoding="utf-8")          # Anlamsız değer: atılır
    else:
        sys_root.mkdir()
    return str(proc), str(sys_root)


def test_reads_memory_load_uptime_disk_and_temperatures(tmp_path):
    proc, sys_root = fake_root(tmp_path)
    info = si.read(proc, sys_root, str(tmp_path))
    assert info["mem"]["MemTotal"] == 4_000_000 * 1024 and info["mem"]["MemAvailable"] == 2_000_000 * 1024
    assert info["rss"] == 307200 * 1024 and info["load"] == (0.35, 0.40, 0.38) and info["uptime"] == 1054800.12
    assert info["temps"] == [("x86_pkg_temp", 47.0), ("coretemp Core 0", 52.0)] and info["disk"].total > 0


def test_message_shows_each_measure_in_turkish(tmp_path):
    proc, sys_root = fake_root(tmp_path)
    info = dict(si.read(proc, sys_root, str(tmp_path)), cpus=2, disk=Disk(40 * GB, 12 * GB, 28 * GB))
    text = si.format_text(info)
    assert text.startswith("🖥️ <b>Sunucu Durumu</b>")
    assert "🧠 RAM: <b>1.9 GB</b> / 3.8 GB kullanılıyor (%50) | kullanılabilir 1.9 GB" in text
    assert "💤 Takas alanı (swap): 488.3 MB / 1.9 GB kullanılıyor" in text and "🤖 Botun kendisi: 300.0 MB" in text
    assert "⚙️ İşlemci yükü (1 / 5 / 15 dk): 0.35 / 0.40 / 0.38 (2 çekirdek)" in text
    assert "💾 Disk: 12.0 GB dolu, 28.0 GB boş (%30 dolu)" in text
    assert "⏱️ Sunucu açık kalma süresi: 12 gün 5 sa" in text
    assert "🌡️ Sıcaklık: en yüksek <b>52°C</b> (coretemp Core 0 52°C, x86_pkg_temp 47°C)" in text
    assert "⚠️" not in text


def test_vps_without_sensors_and_warnings(tmp_path):
    proc, sys_root = fake_root(tmp_path, avail_kb=200_000, swap=False, temps=False)
    info = dict(si.read(proc, sys_root, str(tmp_path)), cpus=1, load=(0.5, 1.5, 1.2), disk=Disk(40 * GB, 38 * GB, 2 * GB))
    text = si.format_text(info)
    assert "🌡️ Sıcaklık: sensör görünmüyor. Sanal sunucularda (VPS)" in text and "💤 Takas alanı (swap): yok" in text
    assert "⚠️ Kullanılabilir bellek az (%5)." in text and "⚠️ Disk dolmak üzere" in text
    assert "⚠️ İşlemci yükü çekirdek sayısının üstünde (5 dk ortalaması 1.50)." in text
    hot = dict(info, temps=[("cpu", 85.0)])
    assert "⚠️ Sıcaklık yüksek (85°C)." in si.format_text(hot)


def test_missing_files_never_raise(tmp_path):
    info = si.read(str(tmp_path / "yok"), str(tmp_path / "yok2"), str(tmp_path / "yok3"))
    assert info["mem"] == {} and info["rss"] is None and info["load"] is None and info["temps"] == []
    text = si.format_text(info)
    assert "🧠 RAM: okunamadı" in text and "sensör görünmüyor" in text


def test_telegram_command_and_menu_button(cfg, fake_client, tmp_path, monkeypatch):
    proc, sys_root = fake_root(tmp_path)
    real = si.read
    monkeypatch.setattr(si, "read", lambda disk_path=".": real(proc, sys_root, disk_path))
    bot, _client, notes = make_bot(cfg, fake_client, tmp_path)
    bot.commands.handle(Command("sistem"))
    assert notes.messages[-1].startswith("🖥️ <b>Sunucu Durumu</b>") and "52°C" in notes.messages[-1]
    bot.commands.handle(Command("sunucu"))
    assert notes.messages[-1].startswith("🖥️ <b>Sunucu Durumu</b>")
    press(bot, "sys")
    assert notes.messages[-1].startswith("🖥️ <b>Sunucu Durumu</b>")
    assert {"sistem", "sunucu"} <= READ_ONLY_COMMANDS                    # Tarama sürerken de hemen cevaplanır
