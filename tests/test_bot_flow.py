"""Botun uçtan uca akışı: sanal borsa + sahte fiyatlar + sabit sinyal."""
from dataclasses import replace

import pytest

from exchange import PaperExchange
from risk import ExitReason
from state import BotState, StateStore, TradeLog
from strategy import ScanResult, Signal
from telegram_bot import TelegramNotifier
from trader import TradingBot


class StubStrategy:
    def __init__(self, symbol, price):
        self.signal = Signal(symbol, price, 10.0, {"dip_pct": 5, "retrace": 0.3, "rsi": 45, "rsi_min": 25,
                                                    "vol_ratio": 1.5})

    def scan(self, rules, excluded, invest_hint):
        if self.signal.symbol in excluded:
            return ScanResult()
        return ScanResult(signals=[self.signal], candidates=1, analyzed=1)


class CaptureNotifier(TelegramNotifier):
    def __init__(self):
        super().__init__("", "")
        self.messages = []

    def send(self, text, silent=False):
        self.messages.append(text)
        return True


def make_bot(cfg, fake_client, tmp_path, price=1.0, state=None):
    client = fake_client({"ABCUSDT": price})
    state = state or BotState.new("paper", cfg.start_capital)
    exchange = PaperExchange(client, state.paper_balances, fee_rate=cfg.fee_rate)
    notifier = CaptureNotifier()
    bot = TradingBot(cfg, exchange, StubStrategy("ABCUSDT", price), notifier,
                     StateStore(tmp_path / "state.json"), state, TradeLog(tmp_path / "trades.csv"))
    bot.startup()
    bot.running = True
    return bot, client, notifier


def set_price(client, p):
    client.prices["ABCUSDT"] = p


def test_take_profit_flow(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    bot.tick()
    pos = bot.state.position
    assert pos is not None and pos.cost_usdt == pytest.approx(11.0)
    assert any("ALIM YAPILDI" in m for m in notes.messages)

    set_price(client, 1.10)
    bot.tick()
    assert bot.state.position is not None
    set_price(client, 1.16)
    bot.tick()
    assert bot.state.position is None
    assert bot.state.realized_pnl > 1.6
    assert bot.state.wins == 1
    assert any("KÂR HEDEFİNE ULAŞILDI" in m and "Güncel Bakiye" in m for m in notes.messages)
    assert (tmp_path / "trades.csv").read_text(encoding="utf-8").count("\n") == 2


def test_trailing_stop_flow(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    bot.tick()
    for p in (1.06, 1.12, 1.09, 1.07):
        set_price(client, p)
        bot.tick()
        assert bot.state.position is not None
    assert any("İZLEYEN STOP AKTİF" in m for m in notes.messages)
    set_price(client, 1.05)
    bot.tick()
    assert bot.state.position is None
    assert 0.4 < bot.state.realized_pnl < 0.6   # ≈ %5 net kâr korundu
    assert any("İZLEYEN STOP ÇALIŞTI" in m for m in notes.messages)


def test_stop_loss_then_cooldown(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    bot.tick()
    set_price(client, 0.89)
    bot.tick()
    assert bot.state.position is None and bot.state.losses == 1
    assert any("STOP-LOSS ÇALIŞTI" in m for m in notes.messages)
    bot.next_scan_at = 0
    bot.tick()  # Aynı coine bekleme süresi dolmadan tekrar girmez
    assert bot.state.position is None


def test_kill_switch_with_open_position(cfg, fake_client, tmp_path):
    state = BotState.new("paper", cfg.start_capital)
    state.realized_pnl = -4.5
    state.paper_balances["USDT"] = 6.5
    bot, client, notes = make_bot(cfg, fake_client, tmp_path, state=state)
    bot.tick()
    assert bot.state.position is not None
    set_price(client, 0.92)   # -%8 → açık zarar ~0.52 → toplam -5.02
    bot.tick()
    assert bot.state.killed and not bot.running
    assert bot.state.position is None
    assert any("KILL-SWITCH TETİKLENDİ" in m for m in notes.messages)

    # Durum dosyasında kill-switch kalıcı
    reloaded = StateStore(tmp_path / "state.json").load()
    assert reloaded.killed


def test_kill_switch_after_realized_loss(cfg, fake_client, tmp_path):
    state = BotState.new("paper", cfg.start_capital)
    state.realized_pnl = -4.0
    state.paper_balances["USDT"] = 7.0
    tight = replace(cfg, max_total_loss=4.5)
    bot, client, notes = make_bot(tight, fake_client, tmp_path, state=state)
    bot.tick()
    set_price(client, 0.89)   # SL: ~-0.77 → toplam -4.77 ≤ -4.5
    bot.tick()
    assert bot.state.killed


def test_state_roundtrip_with_position(cfg, fake_client, tmp_path):
    bot, client, _ = make_bot(cfg, fake_client, tmp_path)
    bot.tick()
    loaded = StateStore(tmp_path / "state.json").load()
    assert loaded.position.symbol == "ABCUSDT"
    assert loaded.position.qty == pytest.approx(bot.state.position.qty)
    assert loaded.paper_balances["USDT"] == pytest.approx(0.0)

    # Yeniden başlatınca pozisyon devralınır
    bot2, client2, notes2 = make_bot(cfg, fake_client, tmp_path, state=loaded)
    assert any("devralındı" in m for m in notes2.messages)
    set_price(client2, 1.2)
    bot2.tick()
    assert bot2.state.position is None and bot2.state.realized_pnl > 0


def test_exit_reasons_have_headers():
    from telegram_bot import _EXIT_HEADERS
    assert set(_EXIT_HEADERS) == {r.value for r in ExitReason}
