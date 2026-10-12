"""Canlı mod akışları: yedek stop, mutabakat, belirsiz emir kurtarma (sahte Binance ile)."""
import pytest

from exchange import LiveExchange
from risk import ExitReason
from state import BotState, StateStore, TradeLog
from strategy import ScanResult, Signal
from trader import TradingBot

from fake_binance import FakeBinance
from test_bot_flow import METRICS, CaptureNotifier


class OneShotStrategy:
    def __init__(self, client, symbols):
        self.client, self.symbols = client, symbols

    def scan(self, rules, excluded, invest_hint):
        sigs = [Signal(s, self.client.prices[s], 10.0 - i, METRICS)
                for i, s in enumerate(self.symbols) if s not in excluded]
        return ScanResult(signals=sigs, candidates=len(sigs), analyzed=len(sigs))


def make_live_bot(cfg, tmp_path, fake=None, state=None, startup=True):
    cfg_live = cfg.__class__(**{**cfg.__dict__, "trading_mode": "live"})
    fake = fake or FakeBinance({"ABCUSDT": 1.0})
    state = state or BotState.new("live", 11.0)
    notes = CaptureNotifier()
    bot = TradingBot(cfg_live, LiveExchange(fake, fee_rate=cfg.fee_rate), OneShotStrategy(fake, list(fake.prices)),
                     notes, StateStore(tmp_path / "state.json"), state, TradeLog(tmp_path / "trades.csv"))
    if startup:
        bot.startup()
    bot.running = True
    return bot, fake, notes


def tick(bot):
    bot.next_scan_at = 0
    bot.tick()


def test_backup_stop_placed_and_locks_coins(cfg, tmp_path):
    bot, fake, notes = make_live_bot(cfg, tmp_path)
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    assert pos.backup_order_id is not None
    # -%12 net zarar seviyesi: ~0.882 civarı
    assert 0.87 < pos.backup_stop_price < 0.885
    assert fake.locked["ABC"] == pytest.approx(pos.backup_qty)
    assert any("yedek stop" in m for m in notes.messages)


def test_sell_cancels_backup_first(cfg, tmp_path):
    bot, fake, notes = make_live_bot(cfg, tmp_path)
    tick(bot)
    fake.prices["ABCUSDT"] = 1.16
    tick(bot)
    assert not bot.state.positions
    assert fake.locked.get("ABC", 0) == pytest.approx(0)
    assert all(o["status"] != "NEW" for o in fake.orders.values())
    assert bot.state.realized_pnl > 1.5


def test_backup_stop_filled_while_bot_was_off(cfg, tmp_path):
    bot, fake, _ = make_live_bot(cfg, tmp_path)
    tick(bot)
    saved = StateStore(tmp_path / "state.json").load()
    # Bot kapalıyken fiyat çöktü ve Binance'teki stop çalıştı
    fake.prices["ABCUSDT"] = 0.85
    fake.trigger_stops()
    bot2, _, notes2 = make_live_bot(cfg, tmp_path, fake=fake, state=saved)
    assert not bot2.state.positions
    assert bot2.state.losses == 1
    assert any("YEDEK STOP EMRİ ÇALIŞTI" in m for m in notes2.messages)
    # Gerçek gerçekleşme fiyatı kullanılmalı (0.85), alış fiyatı değil
    row = (tmp_path / "trades.csv").read_text(encoding="utf-8").splitlines()[-1]
    assert "BACKUP_STOP" in row and "0.85000000" in row


def test_backup_stop_triggered_during_gap_while_running(cfg, tmp_path):
    bot, fake, notes = make_live_bot(cfg, tmp_path)
    tick(bot)
    fake.prices["ABCUSDT"] = 0.80   # Ani çöküş: borsadaki stop tetiklendi
    fake.trigger_stops()
    tick(bot)
    assert not bot.state.positions
    assert any("YEDEK STOP EMRİ ÇALIŞTI" in m for m in notes.messages)


def test_external_sale_uses_real_fill_price(cfg, tmp_path):
    bot, fake, _ = make_live_bot(cfg, tmp_path)
    tick(bot)
    saved = StateStore(tmp_path / "state.json").load()
    fake.prices["ABCUSDT"] = 0.95
    fake.manual_sell_all("ABCUSDT")          # Kullanıcı bot kapalıyken elle sattı
    bot2, _, notes2 = make_live_bot(cfg, tmp_path, fake=fake, state=saved)
    assert not bot2.state.positions
    # Gerçek zarar (~%5) kill-switch hesabına yansımalı; alış fiyatından kapanmış sayılmamalı
    assert -0.7 < bot2.state.realized_pnl < -0.45
    assert any("gerçek satış fiyatı" in m for m in notes2.messages)


def test_unknown_buy_is_recovered(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("exchange.time.sleep", lambda s: None)
    fake = FakeBinance({"ABCUSDT": 1.0})
    bot, fake, notes = make_live_bot(cfg, tmp_path, fake=fake)
    fake.fail_next_order_network = True   # Emir gerçekleşti ama yanıt gelmedi
    fake.fail_get_order = True            # Hemen sorgulama da başarısız
    tick(bot)
    assert not bot.state.positions
    assert len(bot.state.pending_orders) == 1
    fake.fail_get_order = False
    bot._next_pending_check = 0
    tick(bot)
    assert "ABCUSDT" in bot.state.positions
    assert not bot.state.pending_orders
    assert any("kurtarıldı" in m or "takibe alındı" in m for m in notes.messages)


def test_commission_mismatch_warning(cfg, tmp_path):
    fake = FakeBinance({"ABCUSDT": 1.0, "BTCUSDT": 60000.0})
    fake.taker = 0.00075
    _, _, notes = make_live_bot(cfg, tmp_path, fake=fake)
    assert any("FEE_RATE=0.00075" in m for m in notes.messages)


def test_kill_switch_cancels_backups_and_sells(cfg, tmp_path):
    state = BotState.new("live", 11.0)
    state.realized_pnl = -4.5
    fake = FakeBinance({"ABCUSDT": 1.0}, usdt=6.5)
    bot, fake, notes = make_live_bot(cfg, tmp_path, fake=fake, state=state)
    tick(bot)
    fake.prices["ABCUSDT"] = 0.92
    tick(bot)
    assert bot.state.killed
    assert fake.locked.get("ABC", 0) == pytest.approx(0)
    assert any("KILL-SWITCH" in m for m in notes.messages)


def test_manual_sell_command_cancels_backup(cfg, tmp_path):
    from commands import Command
    bot, fake, notes = make_live_bot(cfg, tmp_path)
    tick(bot)
    bot.commands.handle(Command("sat", ["abc"]))
    assert "ABCUSDT" in bot.state.positions          # Onay bekleniyor
    bot.commands.handle(Command("evet"))
    assert not bot.state.positions
    assert any("ELLE SATIŞ" in m for m in notes.messages)
    assert ExitReason.MANUAL.value in (tmp_path / "trades.csv").read_text(encoding="utf-8")


def test_partial_backup_fill_is_realized(cfg, tmp_path):
    from exchange import Fill
    bot, fake, _ = make_live_bot(cfg, tmp_path)
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    cost, qty = pos.cost_usdt, pos.qty
    bot._realize_partial(pos, Fill("ABCUSDT", "SELL", qty / 2, 0.88, qty / 2 * 0.88 * 0.999, 0.004))
    assert pos.qty == pytest.approx(qty / 2)
    assert pos.cost_usdt == pytest.approx(cost / 2)
    assert bot.state.realized_pnl == pytest.approx(qty / 2 * 0.88 * 0.999 - cost / 2)
