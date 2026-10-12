"""Botun uçtan uca akışı: sanal borsa + sahte fiyatlar + sabit sinyaller."""
import json
from dataclasses import replace

import pytest

from exchange import PaperExchange
from risk import ExitReason, position_slots
from state import BotState, StateStore, TradeLog
from strategy import ScanResult, Signal
from telegram_bot import TelegramNotifier
from trader import TradingBot

METRICS = {"dip_pct": 5, "retrace": 0.3, "rsi": 45, "rsi_min": 25, "vol_ratio": 1.5}


class StubStrategy:
    """Her taramada verilen coinlerin hepsi için sinyal üretir (puan sırasıyla)."""

    def __init__(self, client, symbols):
        self.client = client
        self.symbols = symbols
        self.invest_hints = []

    def scan(self, rules, excluded, invest_hint):
        self.invest_hints.append(invest_hint)
        signals = [Signal(s, self.client.prices[s], 10.0 - i, METRICS)
                   for i, s in enumerate(self.symbols) if s not in excluded]
        return ScanResult(signals=signals, candidates=len(signals), analyzed=len(signals))


class CaptureNotifier(TelegramNotifier):
    def __init__(self):
        super().__init__("", "")
        self.messages = []

    def send(self, text, silent=False):
        self.messages.append(text)
        return True


def make_bot(cfg, fake_client, tmp_path, prices=None, state=None, capital=None):
    prices = prices or {"ABCUSDT": 1.0}
    client = fake_client(dict(prices))
    state = state or BotState.new("paper", capital or cfg.start_capital)
    exchange = PaperExchange(client, state.paper_balances, fee_rate=cfg.fee_rate)
    notifier = CaptureNotifier()
    bot = TradingBot(cfg, exchange, StubStrategy(client, list(prices)), notifier,
                     StateStore(tmp_path / "state.json"), state, TradeLog(tmp_path / "trades.csv"))
    bot.startup()
    bot.running = True
    return bot, client, notifier


def tick(bot):
    bot.next_scan_at = 0  # Testte 30 sn beklemeden tara
    bot.tick()


def test_take_profit_flow(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    assert pos.cost_usdt == pytest.approx(11.0)
    assert any("ALIM YAPILDI" in m and "1/1" in m for m in notes.messages)

    client.prices["ABCUSDT"] = 1.10
    tick(bot)
    assert "ABCUSDT" in bot.state.positions
    client.prices["ABCUSDT"] = 1.16
    tick(bot)
    assert not bot.state.positions
    assert bot.state.realized_pnl > 1.6
    assert bot.state.wins == 1
    assert any("KÂR HEDEFİNE ULAŞILDI" in m and "Güncel Bakiye" in m for m in notes.messages)
    assert (tmp_path / "trades.csv").read_text(encoding="utf-8").count("\n") == 2


def test_trailing_stop_flow(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    for p in (1.06, 1.12, 1.09, 1.07):
        client.prices["ABCUSDT"] = p
        tick(bot)
        assert "ABCUSDT" in bot.state.positions
    assert any("İZLEYEN STOP AKTİF" in m for m in notes.messages)
    client.prices["ABCUSDT"] = 1.05
    tick(bot)
    assert not bot.state.positions
    assert 0.4 < bot.state.realized_pnl < 0.6   # ≈ %5 net kâr korundu
    assert any("İZLEYEN STOP ÇALIŞTI" in m for m in notes.messages)


def test_stop_loss_then_cooldown(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    client.prices["ABCUSDT"] = 0.89
    tick(bot)
    assert not bot.state.positions and bot.state.losses == 1
    assert any("STOP-LOSS ÇALIŞTI" in m for m in notes.messages)
    tick(bot)  # Aynı coine bekleme süresi dolmadan tekrar girmez
    assert not bot.state.positions


def test_kill_switch_with_open_position(cfg, fake_client, tmp_path):
    state = BotState.new("paper", cfg.start_capital)
    state.realized_pnl = -4.5
    state.paper_balances["USDT"] = 6.5
    bot, client, notes = make_bot(cfg, fake_client, tmp_path, state=state)
    tick(bot)
    assert bot.state.positions
    client.prices["ABCUSDT"] = 0.92   # -%8 → açık zarar ~0.52 → toplam -5.02
    tick(bot)
    assert bot.state.killed and not bot.running
    assert not bot.state.positions
    assert any("KILL-SWITCH TETİKLENDİ" in m for m in notes.messages)
    assert StateStore(tmp_path / "state.json").load().killed  # Kalıcı


def test_kill_switch_after_realized_loss(cfg, fake_client, tmp_path):
    state = BotState.new("paper", cfg.start_capital)
    state.realized_pnl = -4.0
    state.paper_balances["USDT"] = 7.0
    tight = replace(cfg, max_total_loss=4.5)
    bot, client, notes = make_bot(tight, fake_client, tmp_path, state=state)
    tick(bot)
    client.prices["ABCUSDT"] = 0.89   # SL: ~-0.77 → toplam -4.77 ≤ -4.5
    tick(bot)
    assert bot.state.killed


def test_state_roundtrip_with_position(cfg, fake_client, tmp_path):
    bot, client, _ = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    loaded = StateStore(tmp_path / "state.json").load()
    assert loaded.positions["ABCUSDT"].qty == pytest.approx(bot.state.positions["ABCUSDT"].qty)
    assert loaded.paper_balances["USDT"] == pytest.approx(0.0)

    # Yeniden başlatınca pozisyon devralınır
    bot2, client2, notes2 = make_bot(cfg, fake_client, tmp_path, state=loaded)
    assert any("devralındı" in m for m in notes2.messages)
    client2.prices["ABCUSDT"] = 1.2
    tick(bot2)
    assert not bot2.state.positions and bot2.state.realized_pnl > 0


def test_legacy_single_position_state_is_migrated(cfg, fake_client, tmp_path):
    """Eski sürümün kaydettiği 'position' alanı yeni sürümde okunabilmeli."""
    bot, _, _ = make_bot(cfg, fake_client, tmp_path)
    tick(bot)
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    data["position"] = data.pop("positions")[0]
    (tmp_path / "state.json").write_text(json.dumps(data), encoding="utf-8")
    loaded = StateStore(tmp_path / "state.json").load()
    assert list(loaded.positions) == ["ABCUSDT"]


# ------------------------------------------------------------------ çoklu pozisyon
PRICES3 = {"AAAUSDT": 1.0, "BBBUSDT": 2.0, "CCCUSDT": 0.5}


def test_slot_count_follows_balance(cfg):
    assert position_slots(11, 5, cfg) == 1
    assert position_slots(12, 5, cfg) == 2
    assert position_slots(25, 5, cfg) == 3
    assert position_slots(100, 5, cfg) == 3                        # MAX_OPEN_POSITIONS=3
    assert position_slots(100, 5, replace(cfg, max_open_positions=1)) == 1


def test_11_usdt_opens_single_position(cfg, fake_client, tmp_path):
    bot, _, _ = make_bot(cfg, fake_client, tmp_path, prices=PRICES3)
    tick(bot)
    assert list(bot.state.positions) == ["AAAUSDT"]
    assert bot.state.positions["AAAUSDT"].cost_usdt == pytest.approx(11.0)


def test_25_usdt_opens_three_equal_positions(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path, prices=PRICES3, capital=25.0)
    tick(bot)
    assert set(bot.state.positions) == set(PRICES3)
    costs = [p.cost_usdt for p in bot.state.positions.values()]
    assert all(c == pytest.approx(25 / 3, abs=0.02) for c in costs)
    assert sum(costs) <= 25.0 + 1e-9
    assert bot.state.paper_balances["USDT"] >= -1e-9
    assert bot.strategy.invest_hints[-1] == pytest.approx(25 / 3)  # Toz filtresi pozisyon boyutuna göre
    assert any("3/3" in m for m in notes.messages)


def test_positions_close_independently_and_slot_refills(cfg, fake_client, tmp_path):
    bot, client, _ = make_bot(cfg, fake_client, tmp_path, prices=PRICES3, capital=25.0)
    tick(bot)
    client.prices["BBBUSDT"] = 2.0 * 1.16   # Sadece BBB kâr hedefine ulaşır
    tick(bot)
    assert set(bot.state.positions) == {"AAAUSDT", "CCCUSDT"}
    assert bot.state.wins == 1

    # BBB bekleme süresinde; yeni bir coin çıkarsa boşalan yere girer
    client.prices["DDDUSDT"] = 3.0
    bot.exchange.rules.update(__import__("exchange").parse_symbol_rules(client.exchange_info()))
    bot.strategy.symbols.append("DDDUSDT")
    tick(bot)
    assert set(bot.state.positions) == {"AAAUSDT", "CCCUSDT", "DDDUSDT"}
    assert sum(p.cost_usdt for p in bot.state.positions.values()) <= bot.state.capital + 1e-9


def test_kill_switch_counts_all_positions(cfg, fake_client, tmp_path):
    state = BotState.new("paper", 25.0)
    state.realized_pnl = -2.0
    state.paper_balances["USDT"] = 23.0
    bot, client, notes = make_bot(cfg, fake_client, tmp_path, prices=PRICES3, state=state)
    tick(bot)
    assert len(bot.state.positions) == 3
    for sym in PRICES3:                    # Her biri ~%-6 (tek başına SL değil)
        client.prices[sym] *= 0.94
    tick(bot)                              # -2 + 3 × ~-0.5 ≈ -3.5 → henüz değil
    assert not bot.state.killed and len(bot.state.positions) == 3
    for sym in PRICES3:
        client.prices[sym] = PRICES3[sym] * 0.87   # Hepsi SL bölgesinde; toplam ≈ -5.2
    tick(bot)
    assert bot.state.killed and not bot.running
    assert not bot.state.positions
    assert any("KILL-SWITCH TETİKLENDİ" in m for m in notes.messages)


def test_exit_reasons_have_headers():
    from telegram_bot import _EXIT_HEADERS
    assert set(_EXIT_HEADERS) == {r.value for r in ExitReason}
