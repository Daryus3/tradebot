"""Bekçi (watchdog), otomatik başlatma ve Windows .bat dosyaları testleri (ağ / gerçek süreç yok)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import autostart
import watchdog
from config import BASE_DIR

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- sahte süreçler
class FakeProc:
    """subprocess.Popen benzeri sahte süreç.

    interrupt=True: ilk wait() KeyboardInterrupt fırlatır (Ctrl+C). hang=True: terminate edilene
    kadar zaman aşımlı wait() çağrıları TimeoutExpired fırlatır (kapanmayan bot).
    """

    def __init__(self, code, interrupt=False, hang=False):
        self.code = code
        self.interrupt = interrupt
        self.hang = hang
        self.returncode = None
        self.waits = []
        self.terminated = False
        self.killed = False
        self.signals = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.interrupt and len(self.waits) == 1:
            raise KeyboardInterrupt
        if self.hang and not self.terminated:
            raise subprocess.TimeoutExpired("main.py", timeout)
        self.returncode = -15 if self.terminated else self.code
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def send_signal(self, sig):
        self.signals.append(sig)


class FakeLauncher:
    """popen yerine geçer: her çağrıda sıradaki sahte süreci döndürür, komutları kaydeder."""

    def __init__(self, procs):
        self.procs = [p if isinstance(p, FakeProc) else FakeProc(p) for p in procs]
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        return self.procs.pop(0)

    @property
    def child_args(self):
        return [cmd[2:] for cmd, _ in self.calls]


def run_watchdog(argv, procs, limiter=None, notify=None, sleep=None, tmp_path=None):
    launcher = FakeLauncher(procs)
    sleeps, messages = [], []
    code = watchdog.main(
        argv, popen=launcher,
        sleep=sleep or sleeps.append,
        notify=notify or messages.append,
        limiter=limiter or watchdog.RestartLimiter(time_fn=lambda: 0.0),
        log_file=(tmp_path / "watchdog.log") if tmp_path else None,
    )
    return code, launcher, sleeps, messages


# ---------------------------------------------------------------- kurallar
@pytest.mark.parametrize("code, expected", [
    (0, False),            # normal durdurma (Ctrl+C, /kapat)
    (1, False),            # ayar / kullanıcı hatası
    (2, False),            # kill-switch: ASLA
    (3, True),             # çökme
    (4, True),
    (137, True),
    (255, True),
    (-9, True),            # SIGKILL
    (-2, True),            # SIGINT ile ölmüş (sinyal) → yeniden başlat
    (-15, True),
    (3221225477, True),    # Windows 0xC0000005 erişim ihlali
])
def test_should_restart_table(code, expected):
    assert watchdog.should_restart(code) is expected


def test_restart_limiter_window_with_fake_clock():
    now = [1000.0]
    lim = watchdog.RestartLimiter(max_restarts=5, window_sec=1800, time_fn=lambda: now[0])
    for _ in range(5):
        assert lim.allow()
        now[0] += 60
    assert lim.count() == 5
    assert not lim.allow()          # 6. yeniden başlatma pencerede reddedilir
    assert not lim.allow()          # red sayaç büyütmez
    assert lim.count() == 5

    now[0] = 1000.0 + 1800          # ilk kayıt pencereden çıktı
    assert lim.count() == 4
    assert lim.allow()
    assert not lim.allow()

    now[0] += 1800 + 300            # hepsi eskidi
    assert lim.count() == 0
    assert lim.allow()


def test_restart_limiter_zero_never_allows():
    lim = watchdog.RestartLimiter(max_restarts=0, time_fn=lambda: 0.0)
    assert not lim.allow()


def test_build_child_args():
    user = ["--mode", "live"]
    assert watchdog.build_child_args(user, is_restart=False) == ["--mode", "live"]
    assert watchdog.build_child_args(user, is_restart=True) == ["--mode", "live", "--yes"]
    assert user == ["--mode", "live"]   # girdi değiştirilmez
    assert watchdog.build_child_args(["--yes", "--mode", "live"], True) == ["--yes", "--mode", "live"]
    assert watchdog.build_child_args([], True) == ["--yes"]


def test_split_args_strips_watchdog_flags():
    opts, rest = watchdog.split_args(["--mode", "paper", "--restart-delay", "3", "--env", "x.env",
                                      "--allow-live-autostart"])
    assert rest == ["--mode", "paper", "--env", "x.env"]
    assert opts.restart_delay == 3.0
    assert opts.allow_live_autostart and not opts.install_autostart and not opts.remove_autostart
    opts, rest = watchdog.split_args(["--restart-delay=7", "--backtest", "--gun", "30"])
    assert opts.restart_delay == 7.0 and rest == ["--backtest", "--gun", "30"]


def test_describe_exit_code():
    assert watchdog.describe_exit_code(3) == "3"
    assert watchdog.describe_exit_code(-9) == "-9/SIGKILL"
    assert watchdog.describe_exit_code(3221225477) == "3221225477/0xC0000005"


# ---------------------------------------------------------------- ana döngü
def test_main_restarts_on_crash_then_stops_on_kill_switch(tmp_path):
    code, launcher, sleeps, messages = run_watchdog(["--mode", "live"], [3, 2], tmp_path=tmp_path)

    assert code == 2
    assert len(launcher.calls) == 2
    first_cmd, kwargs = launcher.calls[0]
    assert first_cmd[0] == sys.executable
    assert Path(first_cmd[1]) == BASE_DIR / "main.py"
    assert kwargs.get("cwd") == str(BASE_DIR)
    assert "stdin" not in kwargs            # stdin miras alınmalı (canlı onay sorusu)
    # İlk açılışta onay kullanıcıya sorulur, yeniden başlatmada --yes eklenir
    assert launcher.child_args == [["--mode", "live"], ["--mode", "live", "--yes"]]
    assert sleeps == [10.0]
    assert len(messages) == 1
    assert "♻️" in messages[0] and "kod 3" in messages[0]
    assert "10 sn sonra yeniden başlatılıyor" in messages[0]
    log_text = (tmp_path / "watchdog.log").read_text(encoding="utf-8")
    assert "kod 3" in log_text and "kill-switch" in log_text


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_main_does_not_restart_on_clean_user_or_kill_switch_exit(exit_code):
    code, launcher, sleeps, messages = run_watchdog(["--mode", "paper"], [exit_code])
    assert code == exit_code
    assert len(launcher.calls) == 1
    assert sleeps == [] and messages == []


def test_main_restarts_on_signal_and_strips_watchdog_flags():
    code, launcher, sleeps, _ = run_watchdog(["--mode", "paper", "--restart-delay", "0"], [-9, 3, 0])
    assert code == 0
    assert launcher.child_args == [["--mode", "paper"], ["--mode", "paper", "--yes"],
                                   ["--mode", "paper", "--yes"]]
    assert sleeps == [0.0, 0.0]


def test_main_does_not_duplicate_yes():
    _, launcher, _, _ = run_watchdog(["--mode", "live", "--yes"], [3, 0])
    assert launcher.child_args == [["--mode", "live", "--yes"], ["--mode", "live", "--yes"]]


def test_main_stops_when_restart_limit_exceeded():
    limiter = watchdog.RestartLimiter(max_restarts=2, window_sec=1800, time_fn=lambda: 0.0)
    code, launcher, sleeps, messages = run_watchdog([], [3, 3, 3], limiter=limiter)
    assert code == 3
    assert len(launcher.calls) == 3
    assert len(sleeps) == 2
    assert len(messages) == 3
    assert all("♻️" in m for m in messages[:2])
    assert "⛔" in messages[-1]
    assert "30 dk içinde 3 kez çöktü" in messages[-1]
    assert "durduruldu" in messages[-1]


def test_main_limit_with_negative_code_returns_crash_code():
    limiter = watchdog.RestartLimiter(max_restarts=0, time_fn=lambda: 0.0)
    code, _, _, messages = run_watchdog([], [-11], limiter=limiter)
    assert code == watchdog.EXIT_CRASH
    assert "⛔" in messages[0]


def test_notify_failure_does_not_stop_restart():
    def broken_notify(text):
        raise RuntimeError("telegram yok")

    code, launcher, _, _ = run_watchdog([], [3, 0], notify=broken_notify)
    assert code == 0
    assert len(launcher.calls) == 2


def test_ctrl_c_waits_for_child_to_exit_gracefully():
    proc = FakeProc(0, interrupt=True)
    code, launcher, sleeps, messages = run_watchdog(["--mode", "paper"], [proc])
    assert code == 0
    assert len(launcher.calls) == 1
    assert proc.waits[1] == watchdog.STOP_GRACE_SEC   # 15 sn kapanması beklendi
    assert not proc.terminated and not proc.killed
    assert proc.signals == []                         # Ctrl+C zaten aynı konsoldan bota da gider
    assert sleeps == [] and messages == []


def test_ctrl_c_terminates_child_that_does_not_exit():
    proc = FakeProc(0, interrupt=True, hang=True)
    code, _, _, _ = run_watchdog([], [proc])
    assert code == 0
    assert proc.terminated
    assert not proc.killed


def test_sigterm_forwards_sigint_to_child():
    import signal

    class TermProc(FakeProc):
        def wait(self, timeout=None):
            self.waits.append(timeout)
            if len(self.waits) == 1:
                raise watchdog._TerminateRequest()
            self.returncode = 0
            return 0

    proc = TermProc(0)
    code, _, _, _ = run_watchdog([], [proc])
    assert code == 0
    assert proc.signals == [signal.SIGINT]
    assert not proc.terminated


def test_ctrl_c_during_restart_delay_cancels_restart():
    def interrupted_sleep(sec):
        raise KeyboardInterrupt

    code, launcher, _, messages = run_watchdog([], [3, 0], sleep=interrupted_sleep)
    assert code == 0
    assert len(launcher.calls) == 1
    assert len(messages) == 1          # "yeniden başlatılıyor" bildirimi gitmişti


def test_popen_failure_returns_error():
    def failing_popen(cmd, **kwargs):
        raise OSError("python bulunamadı")

    code = watchdog.main([], popen=failing_popen, sleep=lambda s: None, notify=lambda t: None, log_file=None)
    assert code == 1


def test_help_does_not_start_bot(capsys):
    def no_popen(cmd, **kwargs):
        raise AssertionError("bot başlatılmamalı")

    assert watchdog.main(["--help"], popen=no_popen, log_file=None) == 0
    assert "--restart-delay" in capsys.readouterr().out


def test_telegram_notify_uses_config_and_restores_environment(monkeypatch, tmp_path):
    import telegram_bot

    for key in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TRADING_MODE", "WATCHDOG_TEST_VAR"]:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / "test.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=42\nWATCHDOG_TEST_VAR=1\n", encoding="utf-8")

    sent = []

    class RecordingNotifier:
        def __init__(self, token, chat_id, prefix="", timeout=10.0, async_mode=True):
            sent.append({"token": token, "chat": chat_id, "prefix": prefix, "async": async_mode})

        def send(self, text, silent=False):
            sent[-1]["text"] = text
            return True

    monkeypatch.setattr(telegram_bot, "TelegramNotifier", RecordingNotifier)
    watchdog.telegram_notify("♻️ deneme", ["--mode", "paper", "--env", str(env_file)])

    assert sent == [{"token": "abc", "chat": "42", "prefix": "🧪 <i>[TEST MODU]</i>\n",
                     "async": False, "text": "♻️ deneme"}]
    # .env değerleri bekçinin ortamında kalıp sonraki bot süreçlerine miras kalmamalı
    for key in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WATCHDOG_TEST_VAR"]:
        assert key not in os.environ


def test_telegram_notify_never_raises(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / "bozuk.env"
    env_file.write_text("START_CAPITAL_USDT=abc\n", encoding="utf-8")
    monkeypatch.delenv("START_CAPITAL_USDT", raising=False)
    watchdog.telegram_notify("x", ["--env", str(env_file)])   # ConfigError yutulur
    assert "gönderilemedi" in capsys.readouterr().out
    assert "START_CAPITAL_USDT" not in os.environ


# ---------------------------------------------------------------- otomatik başlatma
def _assert_crlf(data: bytes):
    assert b"\r\n" in data
    assert data.count(b"\n") == data.count(b"\r\n"), "tek başına LF satır sonu var"
    assert data.count(b"\r") == data.count(b"\r\n"), "tek başına CR var"


def test_install_autostart_writes_crlf_bat(tmp_path):
    project = tmp_path / "Kripto Bot Klasörü"
    path = autostart.install_autostart("paper", target_dir=tmp_path / "Startup", project_dir=project)

    assert path == tmp_path / "Startup" / "kripto_bot_baslat.bat"
    data = path.read_bytes()
    _assert_crlf(data)
    assert not data.startswith(b"\xef\xbb\xbf")        # BOM yok
    text = data.decode("utf-8")
    lines = text.split("\r\n")
    assert lines[0] == "@echo off"
    assert "chcp 65001 >nul" in lines
    assert f'cd /d "{project}"' in lines
    assert "venv\\Scripts\\activate.bat" in text
    assert "call " in text
    assert "python watchdog.py --mode paper --yes" in lines


def test_install_autostart_live_requires_permission(tmp_path):
    with pytest.raises(ValueError, match="allow-live-autostart"):
        autostart.install_autostart("live", target_dir=tmp_path)
    assert not (tmp_path / autostart.STARTUP_FILENAME).exists()

    path = autostart.install_autostart("live", allow_live=True, target_dir=tmp_path)
    assert "python watchdog.py --mode live --yes" in path.read_text(encoding="utf-8")


def test_install_autostart_rejects_invalid_mode(tmp_path):
    with pytest.raises(ValueError):
        autostart.install_autostart("futures", target_dir=tmp_path)


def test_install_autostart_without_startup_dir_raises(monkeypatch):
    monkeypatch.setattr(autostart, "startup_dir", lambda: None)
    with pytest.raises(RuntimeError) as exc:
        autostart.install_autostart("paper")
    assert "cron" in str(exc.value) and "systemd" in str(exc.value)


def test_startup_dir():
    assert autostart.startup_dir(platform="linux") is None
    assert autostart.startup_dir(environ={}, platform="win32") is None
    got = autostart.startup_dir(environ={"APPDATA": "/x/AppData/Roaming"}, platform="win32")
    assert got == Path("/x/AppData/Roaming") / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def test_remove_autostart(tmp_path, monkeypatch):
    autostart.install_autostart("paper", target_dir=tmp_path)
    assert autostart.remove_autostart(tmp_path) is True
    assert not (tmp_path / autostart.STARTUP_FILENAME).exists()
    assert autostart.remove_autostart(tmp_path) is False
    monkeypatch.setattr(autostart, "startup_dir", lambda: None)
    assert autostart.remove_autostart() is False


def test_watchdog_autostart_flags(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)

    def no_popen(cmd, **kwargs):
        raise AssertionError("bot başlatılmamalı")

    target = tmp_path / autostart.STARTUP_FILENAME
    assert watchdog.main(["--install-autostart"], popen=no_popen, log_file=None) == 0
    assert "--mode paper --yes" in target.read_text(encoding="utf-8")

    assert watchdog.main(["--install-autostart", "--mode", "live"], popen=no_popen, log_file=None) == 1
    assert "--mode paper --yes" in target.read_text(encoding="utf-8")   # canlıya çevrilmedi

    assert watchdog.main(["--install-autostart", "--mode", "live", "--allow-live-autostart"],
                         popen=no_popen, log_file=None) == 0
    assert "--mode live --yes" in target.read_text(encoding="utf-8")

    assert watchdog.main(["--remove-autostart"], popen=no_popen, log_file=None) == 0
    assert not target.exists()
    assert watchdog.main(["--remove-autostart"], popen=no_popen, log_file=None) == 0
    capsys.readouterr()


# ---------------------------------------------------------------- .bat dosyaları
BAT_COMMANDS = {
    "BASLAT.bat": "python watchdog.py --mode paper",
    "TEST_ET.bat": "python main.py --selftest",
    "CANLI_BASLAT.bat": "python watchdog.py --mode live",
    "KURULUM.bat": "python main.py --kurulum",
    "ISTATISTIK.bat": "python main.py --istatistik",
    "BACKTEST.bat": "python main.py --backtest --gun 30",
}
ALL_BATS = ["_hazirla.bat", *BAT_COMMANDS]
CALL_LINE = 'call "%~dp0_hazirla.bat" || exit /b 1'


def _bat_lines(name):
    data = (ROOT / name).read_bytes()
    return data, data.decode("utf-8").split("\r\n")


def _unescaped(text):
    """cmd'nin ^ kaçışlarını atarak kalan karakterler."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "^":
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


@pytest.mark.parametrize("name", ALL_BATS)
def test_bat_files_are_crlf_utf8_and_safe(name):
    data, lines = _bat_lines(name)
    _assert_crlf(data)
    assert not data.startswith(b"\xef\xbb\xbf"), "BOM cmd'de '@echo off' satırını bozar"
    assert lines[0] == "@echo off"
    chcp = lines.index("chcp 65001 >nul")
    for line in lines[:chcp]:
        assert line.isascii(), "chcp 65001'den önce ASCII dışı karakter olmamalı"

    # Parantez bloklarının içindeki echo satırlarında kaçışsız ( ) < > | & olmamalı
    depth = 0
    for no, raw in enumerate(lines, 1):
        line = raw.strip()
        if line.lower().startswith("echo") and (len(line) == 4 or line[4] in " .:"):
            text = _unescaped(line[5:])
            bad = "<>|&" + ("()" if depth else "")
            assert not any(ch in text for ch in bad), f"{name}:{no} kaçışsız özel karakter: {raw!r}"
        if line.startswith(")"):
            depth -= 1
        if line.endswith("("):
            depth += 1
        assert depth >= 0, f"{name}:{no} fazladan kapanan parantez"
    assert depth == 0, f"{name}: kapanmamış parantez bloğu"


@pytest.mark.parametrize("name, command", sorted(BAT_COMMANDS.items()))
def test_launcher_bats_call_prepare_then_run_command(name, command):
    _, lines = _bat_lines(name)
    assert "chcp 65001 >nul" in lines
    assert any(line.startswith("echo   ") for line in lines[:lines.index(CALL_LINE)]), "Türkçe açıklama yok"
    call_idx = lines.index(CALL_LINE)
    cmd_idx = lines.index(command)
    pause_idx = len(lines) - 1 - lines[::-1].index("pause")
    assert call_idx < cmd_idx < pause_idx


def test_live_bat_asks_confirmation():
    _, lines = _bat_lines("CANLI_BASLAT.bat")
    text = "\n".join(lines)
    assert "--yes" not in text            # onayı main.py sorar (EVET)
    assert "GERÇEK" in text and "EVET" in text


def test_prepare_bat_contents():
    _, lines = _bat_lines("_hazirla.bat")
    text = "\n".join(lines)
    assert 'cd /d "%~dp0"' in lines
    assert "py -3 --version" in text and "python --version" in text
    assert "python.org" in text and "Add Python to PATH" in text
    assert "pause" in lines and "exit /b 1" in lines
    assert "-m venv" in text
    assert 'call "venv\\Scripts\\activate.bat"' in lines
    assert 'fc /b "requirements.txt" "venv\\.req_installed"' in text
    assert "pip install" in text and "-r requirements.txt" in text
    assert 'copy /y "requirements.txt" "venv\\.req_installed" >nul' in text
    # setlocal olursa venv etkinliği çağıran .bat'a geçmez
    assert not any(line.strip().lower().startswith("setlocal") for line in lines)
    assert lines.count("exit /b 0") == 1


def test_gitattributes_forces_crlf_for_bat():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in text.splitlines()
