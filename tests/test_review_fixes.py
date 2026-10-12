"""Bağımsız incelemede bulunan hataların regresyon testleri (önce düşer, düzeltmeden sonra geçer)."""
import pytest


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("exchange.time.sleep", lambda s: None)

from binance_client import BinanceAPIError
from risk import plan_entry, position_slots
from state import BotState, StateStore, TradeLog

from fake_binance import FakeBinance
from test_bot_flow import make_bot, tick as paper_tick
from test_live_features import make_live_bot, tick


class Flaky5xx(FakeBinance):
    """Emri GERÇEKLEŞTİRİP 503 / -1007 döndüren Binance (sonuç belirsiz)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fail_5xx_sides = set()
        self.fail_sell_transient = 0

    def order_market(self, symbol, side, quantity=None, quote_order_qty=None, client_order_id=None):
        if side == "SELL" and self.fail_sell_transient > 0:
            self.fail_sell_transient -= 1
            raise BinanceAPIError(500, -1001, "Internal error; unable to process your request.")
        resp = super().order_market(symbol, side, quantity, quote_order_qty, client_order_id)
        if side in self.fail_5xx_sides:
            self.fail_5xx_sides.discard(side)
            raise BinanceAPIError(503, -1007, "Timeout waiting for response from backend server. "
                                              "Send status unknown; execution status unknown.")
        return resp


# ---------------------------------------------------------------- 5xx = sonuç belirsiz
def test_5xx_buy_is_tracked_not_lost(cfg, tmp_path):
    fake = Flaky5xx({"ABCUSDT": 1.0})
    fake.fail_5xx_sides.add("BUY")
    bot, fake, notes = make_live_bot(cfg, tmp_path, fake=fake)
    tick(bot)
    # Emir gerçekleşti: bot ya hemen sorgulayıp pozisyonu açmalı ya da belirsiz emir olarak takip etmeli
    bot._next_pending_check = 0
    tick(bot)
    assert "ABCUSDT" in bot.state.positions
    assert bot.state.positions["ABCUSDT"].backup_order_id is not None


def test_5xx_sell_not_sold_twice(cfg, tmp_path):
    fake = Flaky5xx({"ABCUSDT": 1.0})
    bot, fake, notes = make_live_bot(cfg, tmp_path, fake=fake)
    fake.free["ABC"] = 5.0          # Kullanıcının bottan bağımsız kendi coinleri
    tick(bot)
    fake.prices["ABCUSDT"] = 1.16
    fake.fail_5xx_sides.add("SELL")
    tick(bot)
    tick(bot)
    assert not bot.state.positions
    # Kullanıcının 5 coini satılmamış olmalı
    assert fake.free.get("ABC", 0) == pytest.approx(5.0 + 10.989 - 10.98, abs=0.02)
    sells = [t for t in fake.trades if not t["isBuyer"]]
    assert len(sells) == 1


# ---------------------------------------------------------------- kill-switch satışı tekrarlanır
def test_kill_switch_retries_sell_and_keeps_protection(cfg, tmp_path):
    state = BotState.new("live", 11.0)
    state.realized_pnl = -4.5
    fake = Flaky5xx({"ABCUSDT": 1.0}, usdt=6.5)
    bot, fake, notes = make_live_bot(cfg, tmp_path, fake=fake, state=state)
    tick(bot)
    fake.prices["ABCUSDT"] = 0.92
    fake.fail_sell_transient = 2
    tick(bot)
    # Satılamadı: bot kapanmamalı, yeni alım yapmamalı, pozisyon korunmalı
    assert bot.state.killed
    assert bot.running
    assert "ABCUSDT" in bot.state.positions
    for _ in range(4):
        bot._next_kill_retry = 0     # Gerçekte ~5 sn sonra tekrar denenir
        tick(bot)
    assert not bot.state.positions
    assert not bot.running
    assert fake.free.get("ABC", 0) < 0.02
    assert any("KILL-SWITCH TETİKLENDİ" in m for m in notes.messages)


def test_killed_state_with_positions_can_restart_to_liquidate(cfg, tmp_path, monkeypatch):
    import main
    state = BotState.new("paper", 11.0)
    state.killed = True
    assert main.must_refuse_killed(state) is True
    from risk import Position
    state.positions["ABCUSDT"] = Position("ABCUSDT", "ABC", 1, 1, 1, 0)
    assert main.must_refuse_killed(state) is False


# ---------------------------------------------------------------- kilitli stop iptali sonrası satış hatası
def test_failed_sell_after_unlocking_orphan_stop_keeps_position(cfg, tmp_path):
    fake = Flaky5xx({"ABCUSDT": 1.0})
    bot, fake, notes = make_live_bot(cfg, tmp_path, fake=fake)
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    pos.backup_order_id = None          # Yetim stop: Binance'te var, bot bilmiyor
    fake.prices["ABCUSDT"] = 0.89
    fake.fail_sell_transient = 1
    tick(bot)
    assert "ABCUSDT" in bot.state.positions, "coinler cüzdandayken pozisyon kapatılmamalı"
    assert bot.state.realized_pnl == 0
    tick(bot)
    assert not bot.state.positions and bot.state.losses == 1


# ---------------------------------------------------------------- kısmi dolum + geçici hata
class PartialStop(Flaky5xx):
    def trigger_partial(self, fraction):
        for order in self.orders.values():
            if order["status"] == "NEW" and order["type"].startswith("STOP_LOSS"):
                qty = float(order["origQty"]) * fraction
                qty = int(qty / 0.01) * 0.01
                price = float(order["price"]) if float(order["price"]) > 0 else self.prices[order["symbol"]]
                base = order["symbol"][:-4]
                fee = qty * price * self.FEE
                self._add(self.locked, base, -qty)
                self._add(self.free, "USDT", qty * price - fee)
                order.update(status="PARTIALLY_FILLED", executedQty=f"{qty:.8f}",
                             cummulativeQuoteQty=f"{qty * price:.8f}")
                self._record_trade(order, qty, price, False, fee, "USDT")

    def cancel_order(self, symbol, order_id):
        order = self.orders.get(order_id)
        if order is not None and order["status"] == "PARTIALLY_FILLED":
            order["status"] = "NEW"
            resp = super().cancel_order(symbol, order_id)
            return resp
        return super().cancel_order(symbol, order_id)


def test_partial_backup_fill_then_transient_sell_error(cfg, tmp_path):
    fake = PartialStop({"ABCUSDT": 1.0}, order_types=("LIMIT", "MARKET", "STOP_LOSS_LIMIT"))
    bot, fake, notes = make_live_bot(cfg, tmp_path, fake=fake)
    tick(bot)
    fake.prices["ABCUSDT"] = 0.88
    fake.trigger_partial(0.3)
    fake.fail_sell_transient = 1
    tick(bot)
    assert not bot.state.killed
    pos = bot.state.positions.get("ABCUSDT")
    assert pos is not None, "kalan coinler takipte kalmalı"
    assert pos.qty == pytest.approx(10.98 * 0.7 + 0.009, abs=0.05)
    assert -0.6 < bot.state.realized_pnl < -0.2      # Sadece satılan %30'un zararı
    tick(bot)
    assert not bot.state.positions
    assert -1.6 < bot.state.realized_pnl < -1.0      # Toplam gerçek zarar ~%12


def test_startup_reconcile_realizes_partial_gone_backup(cfg, tmp_path):
    fake = PartialStop({"ABCUSDT": 1.0}, order_types=("LIMIT", "MARKET", "STOP_LOSS_LIMIT"))
    bot, fake, _ = make_live_bot(cfg, tmp_path, fake=fake)
    tick(bot)
    saved = StateStore(tmp_path / "state.json").load()
    fake.prices["ABCUSDT"] = 0.88
    fake.trigger_partial(0.3)
    oid = saved.positions["ABCUSDT"].backup_order_id
    fake.orders[oid]["status"] = "NEW"
    fake.cancel_order("ABCUSDT", oid)                 # Kullanıcı kalanını iptal etti
    fake.orders[oid]["status"] = "CANCELED"
    bot2, _, _ = make_live_bot(cfg, tmp_path, fake=fake, state=saved)
    pos = bot2.state.positions["ABCUSDT"]
    assert pos.qty == pytest.approx(fake.free["ABC"], abs=0.02) or pos.backup_order_id is not None
    assert -0.6 < bot2.state.realized_pnl < -0.2


def test_external_partial_sale_detected(cfg, tmp_path):
    bot, fake, _ = make_live_bot(cfg, tmp_path)
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    fake.cancel_order("ABCUSDT", pos.backup_order_id)
    fake.order_market("ABCUSDT", "SELL", quantity="4.00")   # Kullanıcı 4 coini elle sattı
    bot._next_backup_check["ABCUSDT"] = 0
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    assert pos.qty == pytest.approx(6.98, abs=0.02)
    assert pos.cost_usdt == pytest.approx(11.0 * 6.989 / 10.989, abs=0.05)
    fake.prices["ABCUSDT"] = 0.89
    tick(bot)
    assert not bot.state.killed
    assert -1.2 < bot.state.realized_pnl < -0.5


# ---------------------------------------------------------------- CSV kilitli (Excel açık)
def test_locked_csv_does_not_break_selling_or_kill_switch(cfg, fake_client, tmp_path, monkeypatch):
    state = BotState.new("paper", 11.0)
    state.realized_pnl = -4.5
    state.paper_balances["USDT"] = 6.5
    bot, client, notes = make_bot(cfg, fake_client, tmp_path, state=state)
    paper_tick(bot)

    def locked(*a, **k):
        raise PermissionError(13, "Permission denied", "trades_paper.csv")

    monkeypatch.setattr(TradeLog, "_write_rows", locked)
    client.prices["ABCUSDT"] = 0.92
    paper_tick(bot)
    assert bot.state.killed and not bot.state.positions
    assert any("SATIŞ YAPILDI" in m for m in notes.messages)
    assert any("KILL-SWITCH TETİKLENDİ" in m for m in notes.messages)
    monkeypatch.undo()
    bot.trade_log.flush_pending()
    assert "ABCUSDT" in (tmp_path / "trades.csv").read_text(encoding="utf-8")


def test_empty_csv_file_keeps_first_trade(tmp_path):
    from risk import Position
    path = tmp_path / "t.csv"
    path.write_text("", encoding="utf-8")
    log = TradeLog(path)
    log.append(Position("AAAUSDT", "AAA", 1, 1, 11, 0), 1.1, 12.0, 0.02, "TAKE_PROFIT", 900)
    log.append(Position("BBBUSDT", "BBB", 1, 1, 11, 0), 0.9, 9.8, 0.02, "STOP_LOSS", 900)
    import stats
    assert [t["parite"] for t in stats.load_trades(path)] == ["AAAUSDT", "BBBUSDT"]


# ---------------------------------------------------------------- slot / yuvarlama kilidi
@pytest.mark.parametrize("capital", [11.0, 11.67, 11.68, 12.0, 17.5, 17.51, 17.52, 25.0, 100.0])
def test_every_slot_size_is_plannable(cfg, capital):
    from decimal import Decimal
    from exchange import SymbolRules
    d = Decimal
    rules = SymbolRules("TESTUSDT", "TEST", "USDT", d("0.0001"), d("0.01"), d("0.01"), d("0.01"), d("0.01"),
                        d("1000000"), d("5"))
    slots = position_slots(capital, 5.0, cfg)
    assert plan_entry(capital / slots, rules, 1.0, cfg).ok, f"{capital} USDT, {slots} slot"


# ================================================================ ikinci inceleme grubu
def test_startup_retries_transient_binance_errors(cfg, monkeypatch):
    import logging
    import main
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise BinanceAPIError(503, -1008, "Server is currently overloaded")
        return "tamam"

    assert main.wait_for_network(flaky, "test", cfg, logging.getLogger("t")) == "tamam"
    with pytest.raises(BinanceAPIError):        # Gerçek ayar hatası tekrar denenmez
        main.wait_for_network(lambda: (_ for _ in ()).throw(BinanceAPIError(401, -2015, "Invalid API-key")),
                              "test", cfg, logging.getLogger("t"))


class _ErrHttp:
    def __init__(self, status, data):
        self.status, self.data, self.calls = status, data, 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        data = self.data

        class R:
            status_code = self.status
            text = "x"

            def json(self_inner):
                return data
        return R()


def test_listener_backs_off_on_telegram_errors():
    import time
    from commands import TelegramCommandListener
    for status, data in ((401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
                         (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 5}}),
                         (502, {"ok": False, "error_code": 502, "description": "Bad Gateway"})):
        http = _ErrHttp(status, data)
        lst = TelegramCommandListener("t" * 10, "1", http=http)
        lst.start()
        time.sleep(0.4)
        lst.stop()
        assert http.calls <= 6, (status, http.calls)   # Meşgul döngü yok


def test_listener_ignores_commands_for_other_bots():
    from commands import TelegramCommandListener
    lst = TelegramCommandListener("t" * 10, "42", http=_ErrHttp(200, {"ok": True, "result": []}))
    lst.username = "kriptocanlibot"
    lst._handle_update({"update_id": 1, "message": {"chat": {"id": 42}, "text": "/hepsinisat@KriptoTestBot"}})
    lst._handle_update({"update_id": 2, "message": {"chat": {"id": 42}, "text": "/durum@KriptoCanliBot"}})
    lst._handle_update({"update_id": 3, "message": {"chat": {"id": 42}, "text": "/ping"}})
    assert [c.name for c in lst.get_commands()] == ["durum", "ping"]


def test_rate_limit_wait_not_cut_by_commands(cfg, fake_client, tmp_path):
    import threading
    import time
    bot, _, _ = make_bot(cfg, fake_client, tmp_path)

    class L:
        wakeup = threading.Event()
    bot.command_listener = L()
    L.wakeup.set()                                 # Komut geldi
    t0 = time.monotonic()
    bot._wait_rate_limit(0.5)
    assert time.monotonic() - t0 >= 0.45
    assert time.time() - bot.last_loop_at < 1      # Sahte "bot çalışmadı" uyarısı üretmez


def test_scan_propagates_rate_limit(cfg, fake_client):
    from binance_client import RateLimitError
    from test_new_features import ScanClient, scaled_rows, scan_rules
    from strategy import ReboundStrategy

    class Limited(ScanClient):
        def klines(self, symbol, *a, **k):
            if symbol != "BTCUSDT":
                raise RateLimitError(429, 60, "limit")
            return super().klines(symbol, *a, **k)

    with pytest.raises(RateLimitError):
        ReboundStrategy(Limited(scaled_rows()), cfg).scan(scan_rules(fake_client), set(), 11)


def test_startup_announces_once(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    bot.startup()
    assert sum("Botu Başlatıldı" in m for m in notes.messages) == 1


def test_chat_id_error_is_redacted(cfg, monkeypatch, capsys):
    import requests
    import main
    import telegram_bot
    from dataclasses import replace
    token = "1234567890:AAHfiqksKZ8WmR2zSjiQ7_v4TMAKdiHm9T0"
    monkeypatch.setattr(telegram_bot, "discover_chat_ids", lambda t: (_ for _ in ()).throw(
        requests.ConnectionError(f"Max retries exceeded with url: /bot{token}/getUpdates")))
    main.find_chat_id(replace(cfg, telegram_bot_token=token))
    out = capsys.readouterr().out
    assert token not in out and "bot***" in out


def test_daily_report_window_covers_late_trades(cfg, fake_client, tmp_path):
    from datetime import datetime
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    start, end = bot._report_window(datetime(2026, 10, 12).date(), 23 * 60 + 55, 1)
    assert start == datetime(2026, 10, 11, 23, 55) and end == datetime(2026, 10, 12, 23, 55)
    # 00:00 ayarında rapor bir önceki günü kapsar ve etiket o günü gösterir
    from dataclasses import replace
    bot.cfg = replace(cfg, daily_report_time="00:00")
    bot.state.last_daily_report = "2026-10-10"
    bot._maybe_reports(datetime(2026, 10, 12, 0, 5).timestamp())
    assert any("Günlük Özet (11.10.2026)" in m for m in notes.messages)


def test_weekly_report_without_daily(cfg, fake_client, tmp_path):
    from dataclasses import replace
    from datetime import datetime
    bot, _, notes = make_bot(replace(cfg, daily_report_time=""), fake_client, tmp_path)
    bot.state.last_weekly_report = "2026-W40"
    bot._maybe_reports(datetime(2026, 10, 12, 0, 10).timestamp())   # Pazartesi: dünkü pazar raporu
    assert any("Haftalık Özet" in m for m in notes.messages)
    assert not any("Günlük Özet" in m for m in notes.messages)


def test_btc_filter_rejects_grouped(cfg, fake_client, tmp_path):
    import time
    from strategy import ScanResult
    bot, _, _ = make_bot(cfg, fake_client, tmp_path)
    for pct in ("-1.62", "-1.75", "-2.01"):
        bot._record_scan(ScanResult(market_ok=False, market_note=f"BTC son 1 saatte %{pct} düştü (piyasa riskli)"),
                         time.time())
    assert bot.day_stats.rejects["BTC filtresi"] == 3


def test_confirm_counter_resets_after_gap(cfg, fake_client, monkeypatch):
    from dataclasses import replace
    import strategy as strategy_mod
    from test_new_features import ScanClient, scaled_rows, scan_rules
    two = replace(cfg, signal_confirm_scans=2)
    strat = strategy_mod.ReboundStrategy(ScanClient(scaled_rows()), two)
    rules = scan_rules(fake_client)
    clock = {"t": 1000.0}
    monkeypatch.setattr(strategy_mod.time, "time", lambda: clock["t"])
    assert not strat.scan(rules, set(), 11).signals
    clock["t"] += 3600                         # Slot doluydu, 1 saat tarama yapılmadı
    assert not strat.scan(rules, set(), 11).signals   # Tek taramalık sinyal teyit sayılmaz
    clock["t"] += 30
    assert strat.scan(rules, set(), 11).signals
