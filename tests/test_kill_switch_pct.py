"""Yüzdelik kill-switch, eski USDT ayarının taşınması, /sermayeekle ve başabaş varsayılanı."""
from dataclasses import replace

import pytest

from commands import Command
from config import ConfigError, load_config
from exchange import LiveExchange
from setup_wizard import migrate_legacy
from state import BotState, StateStore, TradeLog
from trader import TradingBot

from fake_binance import FakeBinance
from test_bot_flow import CaptureNotifier, make_bot, tick
from test_live_features import OneShotStrategy


def load(monkeypatch, tmp_path, **env):
    for key in ("MAX_TOTAL_LOSS_USDT", "MAX_TOTAL_LOSS_PCT", "START_CAPITAL_USDT", "BREAKEVEN_ENABLED",
                "BREAKEVEN_TRIGGER_PCT", "BREAKEVEN_FLOOR_PCT", "TAKE_PROFIT_PCT", "STOP_LOSS_PCT",
                "TRAILING_ACTIVATION_PCT", "TRAILING_FLOOR_PCT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_config(env_file=str(tmp_path / "yok.env"), mode_override="paper")


# ------------------------------------------------------------------ ayarlar
def test_default_is_percent_of_capital(cfg):
    assert cfg.max_total_loss_pct == 45
    assert cfg.loss_limit(11) == pytest.approx(4.95)
    assert cfg.loss_limit(100) == pytest.approx(45)
    assert not cfg.notes


def test_legacy_usdt_setting_is_converted_without_loosening(monkeypatch, tmp_path):
    cfg = load(monkeypatch, tmp_path, MAX_TOTAL_LOSS_USDT="5", START_CAPITAL_USDT="11")
    assert cfg.max_total_loss_pct == pytest.approx(45.45)
    assert cfg.loss_limit(11) == pytest.approx(5.0, abs=0.001)
    assert any("MAX_TOTAL_LOSS_PCT=45.45" in n for n in cfg.notes)


def test_percent_setting_wins_over_legacy_usdt(monkeypatch, tmp_path):
    cfg = load(monkeypatch, tmp_path, MAX_TOTAL_LOSS_USDT="2", MAX_TOTAL_LOSS_PCT="30")
    assert cfg.max_total_loss_pct == 30
    assert any("artık kullanılmıyor" in n for n in cfg.notes)


@pytest.mark.parametrize("value", ["0", "100", "-5", "150"])
def test_invalid_percent_rejected(monkeypatch, tmp_path, value):
    with pytest.raises(ConfigError):
        load(monkeypatch, tmp_path, MAX_TOTAL_LOSS_PCT=value)


def test_capital_no_longer_has_to_exceed_usdt_limit(monkeypatch, tmp_path):
    # Eskiden START_CAPITAL < 5 USDT limit → ayar hatasıydı; yüzdeyle her sermaye geçerli
    cfg = load(monkeypatch, tmp_path, START_CAPITAL_USDT="4")
    assert cfg.loss_limit(4) == pytest.approx(1.8)


def test_breakeven_enabled_by_default_and_warns_when_ineffective(monkeypatch, tmp_path):
    cfg = load(monkeypatch, tmp_path)
    assert cfg.breakeven_enabled and not cfg.notes
    tight = load(monkeypatch, tmp_path, TAKE_PROFIT_PCT="5", STOP_LOSS_PCT="3.5",
                 TRAILING_ACTIVATION_PCT="2", TRAILING_FLOOR_PCT="1")
    assert tight.breakeven_enabled
    assert any("BREAKEVEN_TRIGGER_PCT=1" in n for n in tight.notes)
    fixed = load(monkeypatch, tmp_path, TAKE_PROFIT_PCT="5", STOP_LOSS_PCT="3.5", TRAILING_ACTIVATION_PCT="2",
                 TRAILING_FLOOR_PCT="1", BREAKEVEN_TRIGGER_PCT="1")
    assert not fixed.notes
    off = load(monkeypatch, tmp_path, BREAKEVEN_ENABLED="false", TAKE_PROFIT_PCT="5",
               TRAILING_ACTIVATION_PCT="2", TRAILING_FLOOR_PCT="1")
    assert not off.breakeven_enabled and not off.notes


def test_wizard_migrates_legacy_usdt():
    out, notes = migrate_legacy({"MAX_TOTAL_LOSS_USDT": "2", "START_CAPITAL_USDT": "10"})
    assert out["MAX_TOTAL_LOSS_PCT"] == "20" and "MAX_TOTAL_LOSS_USDT" not in out and notes
    out, notes = migrate_legacy({"MAX_TOTAL_LOSS_USDT": "5", "MAX_TOTAL_LOSS_PCT": "40"})
    assert out == {"MAX_TOTAL_LOSS_PCT": "40"} and not notes
    out, notes = migrate_legacy({"TRADING_MODE": "paper"})
    assert out == {"TRADING_MODE": "paper"} and not notes


# ------------------------------------------------------------------ bot
def test_kill_switch_scales_with_bot_capital(cfg, fake_client, tmp_path):
    state = BotState.new("paper", 100.0)
    state.realized_pnl = -40.0                      # %40 < %45 → devam
    bot, _, _ = make_bot(cfg, fake_client, tmp_path, state=state)
    assert bot.loss_limit == pytest.approx(45.0)
    tick(bot)
    assert not bot.state.killed
    state2 = BotState.new("paper", 100.0)
    state2.realized_pnl = -45.5                     # %45.5 → kill
    bot2, _, notes2 = make_bot(cfg, fake_client, tmp_path / "b", state=state2)
    tick(bot2)
    assert bot2.state.killed
    assert "%45" in bot2.state.kill_reason
    assert any("KILL-SWITCH" in m and "%45" in m for m in notes2.messages)


def test_add_capital_paper_requires_confirmation(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    assert bot.slots() == 1
    bot.commands.handle(Command("sermayeekle", ["20"]))
    assert bot.state.start_capital == pytest.approx(11.0)       # Onay bekleniyor
    assert any("sermaye eklensin mi" in m for m in notes.messages)
    bot.commands.handle(Command("evet"))
    assert bot.state.start_capital == pytest.approx(31.0)
    assert bot.state.capital_added == pytest.approx(20.0)
    assert bot.state.paper_balances["USDT"] == pytest.approx(31.0)
    assert bot.loss_limit == pytest.approx(31 * 0.45)
    assert bot.slots() == 3
    assert StateStore(tmp_path / "state.json").load().start_capital == pytest.approx(31.0)
    assert any("sermaye eklendi" in m for m in notes.messages)


def test_add_capital_keeps_pnl_history(cfg, fake_client, tmp_path):
    state = BotState.new("paper", 11.0)
    state.realized_pnl = -3.0
    bot, _, _ = make_bot(cfg, fake_client, tmp_path, state=state)
    bot.add_capital(9.0)
    assert bot.state.realized_pnl == -3.0
    assert bot.state.capital == pytest.approx(17.0)
    assert bot.loss_limit == pytest.approx(9.0)


@pytest.mark.parametrize("args", [[], ["abc"], ["0"], ["-5"], ["nan"], ["inf"]])
def test_add_capital_rejects_bad_amounts(cfg, fake_client, tmp_path, args):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    bot.commands.handle(Command("sermayeekle", args))
    bot.commands.handle(Command("evet"))
    assert bot.state.start_capital == pytest.approx(11.0)
    assert any("Kullanım: /sermayeekle" in m for m in notes.messages)


def test_add_capital_refused_after_kill(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    bot.state.killed = True
    bot.commands.handle(Command("sermayeekle", ["10"]))
    bot.commands.handle(Command("evet"))
    assert bot.state.start_capital == pytest.approx(11.0)
    assert any("Kill-switch tetiklenmiş" in m for m in notes.messages)


def _live_bot(cfg, tmp_path, fake):
    cfg_live = replace(cfg, trading_mode="live")
    notes = CaptureNotifier()
    bot = TradingBot(cfg_live, LiveExchange(fake, fee_rate=cfg.fee_rate), OneShotStrategy(fake, list(fake.prices)),
                     notes, StateStore(tmp_path / "state.json"), BotState.new("live", 11.0),
                     TradeLog(tmp_path / "trades.csv"))
    bot.startup()
    bot.running = True
    return bot, notes


def test_add_capital_live_checks_spot_balance(cfg, tmp_path):
    fake = FakeBinance({"ABCUSDT": 1.0}, usdt=11.0)
    bot, notes = _live_bot(cfg, tmp_path, fake)
    bot.commands.handle(Command("sermayeekle", ["20"]))         # Spot'ta bota ayrılmamış para yok
    assert bot.commands.pending is None
    assert any("bota ayrılmamış serbest bakiye" in m for m in notes.messages)

    fake.free["USDT"] += 20.0                                   # Kullanıcı Spot'a 20 USDT aktardı
    bot.commands.handle(Command("sermayeekle", ["20"]))
    fake.free["USDT"] -= 15.0                                   # Onay beklerken para başka yere gitti
    bot.commands.handle(Command("evet"))
    assert bot.state.start_capital == pytest.approx(11.0)

    fake.free["USDT"] += 15.0
    bot.commands.handle(Command("sermayeekle", ["20"]))
    bot.commands.handle(Command("evet"))
    assert bot.state.start_capital == pytest.approx(31.0)
    assert bot.slots() == 3


def test_add_capital_live_with_open_position(cfg, tmp_path):
    fake = FakeBinance({"ABCUSDT": 1.0}, usdt=11.0)
    bot, notes = _live_bot(cfg, tmp_path, fake)
    tick(bot)
    assert "ABCUSDT" in bot.state.positions                     # Paranın tamamı pozisyonda
    fake.free["USDT"] += 10.0
    assert bot.unallocated_balance() == pytest.approx(10.0, abs=0.05)
    bot.commands.handle(Command("sermayeekle", ["10"]))
    bot.commands.handle(Command("evet"))
    assert bot.state.start_capital == pytest.approx(21.0)
