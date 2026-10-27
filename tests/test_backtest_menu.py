"""Telegram'dan backtest: düğmeli menü, komut satırı argümanları, ayrı süreçte çalıştırma ve sonuçların gönderilmesi."""
import os
import sys
import time
from datetime import date
from pathlib import Path

import pytest

import backtest_runner as br
from backtest_runner import BacktestDraft, build_args, child_env, clean_output, default_days, split_chunks
from commands import CALLBACK, Command
from test_bot_flow import make_bot


def press(bot, data, message_id=5):
    bot.commands.handle(Command(CALLBACK, data.split(":"), kind="callback", message_id=message_id))


def buttons(notes):
    return [data for row in notes.menus[-1][1] for _label, data in row]


class FakeProc:
    last = None

    def __init__(self, cmd, **kw):
        self.cmd, self.kw = cmd, kw
        self.code = None
        self.pid = 0
        self.terminated = False
        FakeProc.last = self

    def write(self, text):
        self.kw["stdout"].write(text)
        self.kw["stdout"].flush()

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


@pytest.fixture
def bt_bot(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    bot.backtests.runs_dir = tmp_path / "runs"
    bot.backtests.popen = FakeProc
    sent = []
    notes.send_photo = lambda path, caption="": sent.append(("photo", Path(path).name)) or True
    notes.send_document = lambda path, caption="": sent.append(("doc", Path(path).name)) or True
    return bot, notes, sent


# ------------------------------------------------------------------ taslak ve argümanlar
def test_default_days_follow_cli_rules():
    assert default_days("normal", "rebound") == 30
    assert default_days("turnuva", "rebound") == 365
    assert default_days("normal", "mean_revert") == 365 and default_days("normal", "swing") == 365
    assert default_days("normal", "rotation") == 1095 and default_days("turnuva", "rotation") == 365


def test_draft_from_garbage_is_safe():
    d = BacktestDraft.from_dict({"kind": "yok", "strategy": "x", "days": "90", "coins": 20, "chart": False, "z": 1})
    assert (d.kind, d.strategy, d.days, d.coins, d.chart) == ("normal", "", 0, 20, False)
    assert BacktestDraft.from_dict(None) == BacktestDraft()


def test_build_args_full_draft(cfg, fake_client, tmp_path):
    bot, _, _ = make_bot(cfg, fake_client, tmp_path)
    bot.settings_path = tmp_path / ".env"
    draft = BacktestDraft(kind="tara", strategy="trend", days=90, end="prev", capital="50", coins=60, chart=False,
                          grids={"tp": "2,3,5", "sure": "0,240"}, overrides={"VWAP_FILTER": "true"})
    args = build_args(draft, bot, tmp_path / "out", date(2026, 10, 27))
    assert args[:2] == ["--backtest", "--tara"]
    joined = " ".join(args)
    for part in ("--gun 90", "--coin-sayisi 60", "--strateji trend", "--bitis 2026-07-29", "--sermaye 50",
                 "--grafiksiz", "--tp 2,3,5", "--sure 0,240", "--ayar VWAP_FILTER=true",
                 f"--cikti-klasoru {tmp_path / 'out'}", f"--env {tmp_path / '.env'}"):
        assert part in joined, part
    turnuva = build_args(BacktestDraft(kind="turnuva", strategy="swing", capital="bot"), bot, tmp_path, date.today())
    assert "--strateji" not in turnuva and "--turnuva" in turnuva and "--gun 365" in " ".join(turnuva)
    assert f"--sermaye {bot.state.capital:g}" in " ".join(turnuva)


def test_child_env_rereads_setting_files(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TAKE_PROFIT_PCT=3\nBINANCE_API_KEY=gizli\n", encoding="utf-8")
    (tmp_path / "ayarlar.env").write_text("STOP_LOSS_PCT=5\n", encoding="utf-8")
    monkeypatch.setenv("TAKE_PROFIT_PCT", "15")     # Bot açılırken yüklenmiş (eski) değer
    monkeypatch.setenv("STOP_LOSS_PCT", "10")
    monkeypatch.setenv("HOME_TEST_VAR", "kalsin")
    out = child_env(env)
    assert "TAKE_PROFIT_PCT" not in out and "STOP_LOSS_PCT" not in out and out["HOME_TEST_VAR"] == "kalsin"
    assert out["PYTHONUNBUFFERED"] == "1"


def test_output_helpers():
    text = "⏳ İndiriliyor %50\n2026-10-27 10:00:00 | INFO    | x\nRAPOR\n2026-10-27 10:00:00 | WARNING | dikkat"
    assert clean_output(text) == "RAPOR\n2026-10-27 10:00:00 | WARNING | dikkat"
    chunks = split_chunks("\n".join(f"satır {i:03d}" for i in range(100)), 100)
    assert all(len(c) <= 100 for c in chunks) and "".join(chunks).count("satır") == 100


# ------------------------------------------------------------------ menü
def test_menu_choices_persist(bt_bot):
    bot, notes, _ = bt_bot
    bot.commands.handle(Command("backtest"))
    assert "Backtest" in notes.menus[-1][0] and "bt:go" in buttons(notes)
    press(bot, "bt:kind")
    assert "bt:kind:turnuva" in buttons(notes)
    press(bot, "bt:kind:ab")
    press(bot, "bt:days:180")
    press(bot, "bt:end:prev")
    press(bot, "bt:cap:100")
    press(bot, "bt:coin:60")
    press(bot, "bt:chart")
    draft = BacktestDraft.from_dict(bot.state.backtest_draft)
    assert (draft.kind, draft.days, draft.end, draft.capital, draft.coins, draft.chart) == \
        ("ab", 180, "prev", "100", 60, False)
    text = notes.menus[-1][0]
    assert "A/B filtre testi" in text and "180 gün" in text and "önceki dönem" in text and "100 USDT" in text
    assert bot.store.load().backtest_draft["kind"] == "ab"           # Yeniden başlayınca kaybolmaz


def test_overrides_toggle_validate_and_reset(bt_bot):
    bot, notes, _ = bt_bot
    press(bot, "bt:ov")
    press(bot, "bt:ovt:VWAP_FILTER")
    assert BacktestDraft.from_dict(bot.state.backtest_draft).overrides == {"VWAP_FILTER": "true"}
    press(bot, "bt:ovt:VWAP_FILTER")                                # Botun değerine dönünce değişiklik silinir
    assert BacktestDraft.from_dict(bot.state.backtest_draft).overrides == {}
    press(bot, "bt:ovp:tp")
    assert "bt:ovv:tp:4" in buttons(notes)
    press(bot, "bt:ovv:tp:4")                                       # TP 3: izleyen stop (10) TP'den küçük olmalı
    assert "TRAILING_ACTIVATION_PCT" in notes.menus[-1][0]
    assert BacktestDraft.from_dict(bot.state.backtest_draft).overrides == {}
    press(bot, "bt:ovv:tr:2")                                       # İzleyen stop 2 → 1
    press(bot, "bt:ovv:tp:4")
    press(bot, "bt:ovm:DCA_MODE")
    overrides = BacktestDraft.from_dict(bot.state.backtest_draft).overrides
    assert overrides == {"TRAILING_ACTIVATION_PCT": "2", "TRAILING_FLOOR_PCT": "1", "TAKE_PROFIT_PCT": "3",
                         "DCA_MODE": "auto"}
    press(bot, "bt:ovv:be:0")                                       # Başabaş kapalı
    assert BacktestDraft.from_dict(bot.state.backtest_draft).overrides["BREAKEVEN_ENABLED"] == "false"
    press(bot, "bt:ovx")
    assert BacktestDraft.from_dict(bot.state.backtest_draft).overrides == {}


def test_strategy_change_rejected_when_overrides_conflict(bt_bot):
    bot, notes, _ = bt_bot
    bot.state.backtest_draft = {"overrides": {"STOP_LOSS_PCT": "11"}}   # Yedek stop (12) altında: geçerli
    press(bot, "bt:strat:mean_revert")                               # Profil yedek stopu 8: SL 11 ile çelişir
    assert "BACKUP_STOP_PCT" in notes.menus[-1][0]
    assert BacktestDraft.from_dict(bot.state.backtest_draft).strategy == ""
    press(bot, "bt:strat:trend")                                     # Aynı grup: profil yok, geçerli
    assert BacktestDraft.from_dict(bot.state.backtest_draft).strategy == "trend"


def test_grid_values_for_exit_sweep(bt_bot):
    bot, notes, _ = bt_bot
    press(bot, "bt:kind:tara")
    assert "bt:grid" in buttons(notes)
    press(bot, "bt:gridp:sl")
    press(bot, "bt:gridv:sl:2")
    assert BacktestDraft.from_dict(bot.state.backtest_draft).grids == {"sl": "2,3,5"}
    press(bot, "bt:gridv:sl:0")
    assert BacktestDraft.from_dict(bot.state.backtest_draft).grids == {}


# ------------------------------------------------------------------ çalıştırma
def test_run_success_sends_report_and_files(bt_bot):
    bot, notes, sent = bt_bot
    press(bot, "bt:kind:ab")
    press(bot, "bt:go")
    proc = FakeProc.last
    assert proc.cmd[proc.cmd.index(sys.executable) + 1].endswith("main.py")
    assert "--tara-ab" in proc.cmd and proc.kw["stdin"] is not None
    status_mid = bot.backtests.job.message_id
    assert status_mid and "bt:stop" in [d for _t, kb, _m in notes.menus for row in kb for _l, d in row]
    press(bot, "bt:go")
    assert "Zaten çalışan" in notes.menus[-1][0]
    proc.write("⏳ Simülasyon %50\n")
    bot.backtests.poll(time.time() + br.PROGRESS_EDIT_SEC + 1)
    assert notes.menus[-1][2] == status_mid and "Simülasyon %50" in notes.menus[-1][0]
    run_dir = bot.backtests.job.run_dir
    (run_dir / "tarama_ab_x.csv").write_text("a;b\n", encoding="utf-8")
    (run_dir / "backtest_grafik_x.png").write_bytes(b"png")
    proc.write("🧪 A/B FİLTRE TESTİ\n" + "\n".join(f"satır {i} <b>" for i in range(400)) + "\n")
    proc.code = 0
    bot.backtests.poll(time.time())
    assert not bot.backtests.running
    texts = notes.messages
    assert any("Backtest bitti" in m for m in texts)
    pres = [m for m in texts if m.startswith("<pre>")]
    assert pres and all(len(m) < 4000 for m in pres) and "&lt;b&gt;" in pres[0]
    assert ("photo", "backtest_grafik_x.png") in sent and ("doc", "tarama_ab_x.csv") in sent
    assert ("doc", "rapor.txt") in sent and "⏳" not in (run_dir / "rapor.txt").read_text(encoding="utf-8")
    sent.clear()
    press(bot, "bt:last")
    assert ("doc", "rapor.txt") in sent


def test_run_failure_cancel_and_timeout(bt_bot, monkeypatch):
    bot, notes, _ = bt_bot
    press(bot, "bt:go")
    FakeProc.last.write("❌ Backtest çalıştırılamadı: ağ yok\n")
    FakeProc.last.code = 1
    bot.backtests.poll(time.time())
    assert any("Backtest başarısız" in m and "ağ yok" in m for m in notes.messages)
    press(bot, "bt:go")
    press(bot, "bt:stop")
    assert FakeProc.last.terminated and not bot.backtests.running
    assert "durduruldu" in notes.menus[-2][0] or "durduruldu" in notes.menus[-1][0]
    press(bot, "bt:go")
    bot.backtests.poll(time.time() + br.BACKTEST_TIMEOUT_SEC + 5)
    assert FakeProc.last.terminated and not bot.backtests.running
    bot.backtests.popen = FakeProc
    press(bot, "bt:go")
    bot.backtests.shutdown()
    assert FakeProc.last.terminated and not bot.backtests.running


def test_invalid_settings_do_not_start(bt_bot):
    bot, notes, _ = bt_bot
    bot.state.backtest_draft = {"overrides": {"STOP_LOSS_PCT": "20"}}   # Yedek stop (12) zarar kesten büyük olmalı
    FakeProc.last = None
    press(bot, "bt:go")
    assert FakeProc.last is None and "geçersiz" in notes.menus[-1][0]


def test_old_runs_cleaned_up(bt_bot):
    bot, _, _ = bt_bot
    runs = bot.backtests.runs_dir
    for i in range(8):
        (runs / f"2026010{i}-000000").mkdir(parents=True)
    press(bot, "bt:go")
    assert len([p for p in runs.iterdir() if p.is_dir()]) == br.RUNS_KEPT
    bot.backtests.shutdown()


def test_real_subprocess_round_trip(bt_bot, monkeypatch):
    bot, notes, sent = bt_bot
    bot.backtests.popen = br.subprocess.Popen

    def command(args):
        out = args[args.index("--cikti-klasoru") + 1]
        code = (f"import pathlib; print('⏳ %50'); print('GERÇEK RAPOR'); "
                f"pathlib.Path(r'{out}', 'tarama_x.csv').write_text('a;b')")
        return [sys.executable, "-c", code]

    monkeypatch.setattr(br, "command_for", command)
    press(bot, "bt:go")
    bot.backtests.job.proc.wait(30)
    bot.backtests.poll(time.time())
    assert any("GERÇEK RAPOR" in m for m in notes.messages) and ("doc", "tarama_x.csv") in sent


# ------------------------------------------------------------------ komut satırı: --ayar ve --cikti-klasoru
def test_cli_overrides_applied_after_strategy_profile(cfg, monkeypatch, capsys, tmp_path):
    import backtest
    import main
    seen = {}
    monkeypatch.setattr(main, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(backtest, "run_backtest", lambda c, *a, **k: seen.setdefault("cfg", c) and None)
    monkeypatch.setattr(backtest, "format_backtest_report", lambda *a, **k: "RAPOR")
    assert main.main(["--backtest", "--strateji", "swing", "--ayar", "TAKE_PROFIT_PCT=20",
                      "--ayar", "vwap_filter=evet"]) == 0
    used = seen["cfg"]
    assert used.take_profit_pct == 20 and used.stop_loss_pct == 6.0 and used.vwap_filter   # SL swing profilinden
    assert "Yalnızca bu test için" in capsys.readouterr().out
    assert main.main(["--backtest", "--ayar", "BINANCE_API_KEY=x"]) == 1
    assert main.main(["--backtest", "--ayar", "TAKE_PROFIT_PCT"]) == 1
    assert cfg.take_profit_pct == 15                                  # Asıl ayarlar değişmedi


def test_cli_output_folder_for_sweeps(cfg, monkeypatch, tmp_path):
    import backtest
    import main
    saved = []
    monkeypatch.setattr(main, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(backtest, "prepare_backtest", lambda *a, **k: "VERI")
    monkeypatch.setattr(backtest, "run_ab_test", lambda *a, **k: ["satır"])
    monkeypatch.setattr(backtest, "format_ab_report", lambda *a, **k: "AB")
    monkeypatch.setattr(backtest, "save_sweep_csv", lambda rows, path: saved.append(path) or path)
    out = tmp_path / "sonuc"
    out.mkdir()
    assert main.main(["--backtest", "--tara-ab", "--cikti-klasoru", str(out)]) == 0
    assert saved and saved[0].parent == out
    calls = []
    monkeypatch.setattr(main, "_send_backtest_chart", lambda c, r, rep, out_dir, send=True: calls.append((out_dir, send)))
    monkeypatch.setattr(backtest, "run_backtest", lambda *a, **k: type("R", (), {"trades": [1]})())
    monkeypatch.setattr(backtest, "format_backtest_report", lambda *a, **k: "RAPOR")
    assert main.main(["--backtest", "--cikti-klasoru", str(out)]) == 0
    assert calls == [(out, False)]                                    # Telegram'a bot gönderir, süreç göndermez
    assert os.path.isdir(out)
