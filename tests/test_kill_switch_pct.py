"""Yüzdelik kill-switch, eski USDT ayarının taşınması, /sermayeekle ve başabaş varsayılanı."""
from dataclasses import replace

import pytest

from commands import Command
from config import ConfigError, load_config
from exchange import LiveExchange
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


def test_legacy_usdt_setting_keeps_exact_limit(monkeypatch, tmp_path):
    cfg = load(monkeypatch, tmp_path, MAX_TOTAL_LOSS_USDT="5", START_CAPITAL_USDT="11")
    assert cfg.legacy_loss_usdt == 5
    assert cfg.loss_limit(11) == pytest.approx(5.0)
    assert cfg.loss_limit(10.36) == pytest.approx(5.0)          # Canlı ilk açılış: sermaye = serbest bakiye
    assert cfg.loss_limit(22, capital_added=11) == pytest.approx(10.0)   # Sermaye 2 katına çıkınca limit de
    assert cfg.loss_pct(10) == pytest.approx(50)
    assert any("MAX_TOTAL_LOSS_USDT=5" in n and "MAX_TOTAL_LOSS_PCT" in n for n in cfg.notes)


def test_legacy_usdt_ignores_edited_start_capital(monkeypatch, tmp_path, fake_client):
    # START_CAPITAL_USDT sonradan 31 yapılmış ama kayıtlı sermaye hâlâ 11: limit eskisi gibi 5 USDT kalmalı
    cfg = load(monkeypatch, tmp_path, MAX_TOTAL_LOSS_USDT="5", START_CAPITAL_USDT="31")
    state = BotState.new("paper", 11.0)
    state.realized_pnl = -2.0
    bot, _, _ = make_bot(cfg, fake_client, tmp_path, state=state)
    assert bot.loss_limit == pytest.approx(5.0)
    tick(bot)
    assert not bot.state.killed
    bot.add_capital(11.0)
    assert bot.loss_limit == pytest.approx(10.0)
    assert bot.loss_pct == pytest.approx(10 / 22 * 100)


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


def test_add_capital_refused_while_backup_stop_fill_is_unbooked(cfg, tmp_path):
    fake = FakeBinance({"ABCUSDT": 1.0}, usdt=11.0)
    bot, notes = _live_bot(cfg, tmp_path, fake)
    tick(bot)
    fake.prices["ABCUSDT"] = 0.85
    fake.trigger_stops()                     # Ani iğne: Binance'teki yedek stop çalıştı...
    fake.prices["ABCUSDT"] = 1.0             # ...fiyat geri geldi, bot henüz fark etmedi
    bot.commands.handle(Command("sermayeekle", ["9"]))
    bot.commands.handle(Command("evet"))
    assert bot.state.start_capital == pytest.approx(11.0)   # Botun kendi satış geliri "yatırılan para" sayılmadı
    assert any("cüzdanda yok" in m for m in notes.messages)


def test_add_capital_refused_while_sell_outcome_unknown(cfg, tmp_path):
    fake = FakeBinance({"ABCUSDT": 1.0}, usdt=11.0)
    bot, notes = _live_bot(cfg, tmp_path, fake)
    tick(bot)
    bot.state.positions["ABCUSDT"].pending_sell_client_id = "x123"
    fake.free["USDT"] += 20.0
    bot.commands.handle(Command("sermayeekle", ["10"]))
    assert bot.commands.pending is None
    assert any("sonucu henüz belli değil" in m for m in notes.messages)


def test_add_capital_balance_error_is_escaped_once(cfg, tmp_path):
    fake = FakeBinance({"ABCUSDT": 1.0}, usdt=11.0)
    bot, notes = _live_bot(cfg, tmp_path, fake)

    def broken():
        raise RuntimeError("a&b <x>")
    fake.account = broken
    bot.commands.handle(Command("sermayeekle", ["10"]))
    assert any("a&amp;b &lt;x&gt;" in m for m in notes.messages)
    assert not any("&amp;amp;" in m for m in notes.messages)


def test_no_breakeven_noise_when_ineffective(cfg, fake_client, tmp_path):
    # Kullanıcının ayarları: TP 5, izleyen stop +2 → +1; varsayılan başabaş tetiği 5 hiçbir işe yaramaz
    user = replace(cfg, take_profit_pct=5, stop_loss_pct=3.5, trailing_activation_pct=2, trailing_floor_pct=1,
                   trailing_dynamic=True)
    bot, client, notes = make_bot(user, fake_client, tmp_path)
    tick(bot)
    client.prices["ABCUSDT"] = 1.055                      # Doğrudan Kâr Al bölgesine sıçradı
    tick(bot)
    assert not bot.state.positions
    assert not any("BAŞABAŞ KORUMASI AKTİF" in m or "İZLEYEN STOP AKTİF" in m for m in notes.messages)
    assert any("KÂR HEDEFİNE" in m for m in notes.messages)


def test_breakeven_with_suggested_trigger_protects_small_gains(cfg, fake_client, tmp_path):
    user = replace(cfg, take_profit_pct=5, stop_loss_pct=3.5, trailing_activation_pct=2, trailing_floor_pct=1,
                   trailing_dynamic=True, breakeven_trigger_pct=1, breakeven_floor_pct=0.3)
    bot, client, notes = make_bot(user, fake_client, tmp_path)
    tick(bot)
    client.prices["ABCUSDT"] = 1.015                      # net ~+%1.3 → başabaş aktif, izleyen stop değil
    tick(bot)
    assert bot.state.positions["ABCUSDT"].breakeven_active
    client.prices["ABCUSDT"] = 1.004                      # net ~+%0.2 → zarara dönmeden sat
    tick(bot)
    assert not bot.state.positions and bot.state.wins == 1


def test_settings_file_loaded_and_env_wins(tmp_path):
    """ayarlar.env (GitHub'daki ayarlar) okunur; sunucudaki .env (gizli bilgiler) aynı ayarı ezer."""
    import os
    from pathlib import Path
    keys = ("MIN_DIP_PCT", "RSI_PERIOD", "TELEGRAM_CHAT_ID")
    saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
    try:
        (tmp_path / "ayarlar.env").write_text("MIN_DIP_PCT=2.2\nRSI_PERIOD=20\n", encoding="utf-8")
        (tmp_path / ".env").write_text("RSI_PERIOD=9\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
        cfg = load_config(env_file=str(tmp_path / ".env"), mode_override="paper")
        assert cfg.min_dip_pct == 2.2 and cfg.rsi_period == 9 and cfg.telegram_chat_id == "42"
    finally:
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(saved)
    # Depodaki ayarlar.env gizli bilgi içermez
    repo = (Path(__file__).resolve().parent.parent / "ayarlar.env").read_text(encoding="utf-8")
    for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        assert f"\n{key}=" not in repo


def test_live_start_with_rejected_api_key_is_config_error(cfg, monkeypatch, tmp_path, capsys):
    """-2015 (geçersiz anahtar / IP / izin) yeniden denemekle düzelmez: çökme (3) değil ayar hatası (1) → servis
    sonsuz yeniden başlatmaz."""
    import binance_client
    import main
    from binance_client import BinanceAPIError

    class Rejecting(binance_client.BinanceClient):
        def _request(self, method, path, params=None, signed=False, _retry=True):
            if path == "/api/v3/time":
                return {"serverTime": 0}
            raise BinanceAPIError(401, -2015, "Invalid API-key, IP, or permissions for action.")
    live = replace(cfg, trading_mode="live", binance_api_key="k", binance_api_secret="s")
    monkeypatch.setattr(main, "load_config", lambda *a, **k: live)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(binance_client, "BinanceClient", Rejecting)
    assert main.main(["--mode", "live", "--yes"]) == 1
    assert "IP kısıtlamasını" in capsys.readouterr().out
