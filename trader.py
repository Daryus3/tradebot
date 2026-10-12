"""
Ana işlem motoru (TradingBot).

Döngü:
  - Boş pozisyon yeri (slot) varsa: her SCAN_INTERVAL_SEC (30 sn) piyasayı tarar, sinyal varsa alır.
  - Açık pozisyon(lar) varsa: her POSITION_CHECK_SEC (5 sn) fiyatları kontrol eder;
    kâr al / zarar kes / izleyen stop / kill-switch koşullarına "anında" tepki verir.

Aynı anda kaç coin tutulacağı (MAX_OPEN_POSITIONS) bakiyeye göre otomatik ayarlanır:
her pozisyon, fiyat stop-loss'a düşse bile Binance'in minimum emir tutarının (~5 USDT)
üstünde kalmalıdır. 11 USDT → 1 pozisyon, ~12 USDT → 2, ~17.5 USDT ve üstü → 3.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

from binance_client import BinanceAPIError, RateLimitError
from exchange import BaseExchange, OrderError
from logger import get_logger
from risk import (ExitReason, Position, evaluate_exit, kill_switch_hit, net_exit_value,
                  plan_entry, pnl_pct, position_slots, price_for_pnl, trailing_floor_pct, update_trailing)
from state import BotState, StateStore, TradeLog
from strategy import ReboundStrategy
from telegram_bot import Messages, TelegramNotifier, fmt_duration, fmt_signed

log = get_logger()

RULES_REFRESH_SEC = 3600
POSITION_LOG_SEC = 60


@dataclass
class Mark:
    """Açık pozisyonun son değerlemesi."""
    bid: float
    net_value: float
    pnl_usdt: float
    pnl_pct: float
    at: float


class TradingBot:
    def __init__(self, cfg, exchange: BaseExchange, strategy: ReboundStrategy, notifier: TelegramNotifier,
                 store: StateStore, state: BotState, trade_log: TradeLog):
        self.cfg = cfg
        self.exchange = exchange
        self.strategy = strategy
        self.notifier = notifier
        self.store = store
        self.state = state
        self.trade_log = trade_log
        self.running = False
        self.next_scan_at = 0.0
        self.next_status_at = (time.time() + cfg.status_report_minutes * 60
                               if cfg.status_report_minutes > 0 else float("inf"))
        self.marks: Dict[str, Mark] = {}
        self._last_position_log = 0.0
        self._error_streak = 0

    # ================================================================ yaşam döngüsü
    def run(self) -> int:
        self.startup()
        self.running = True
        try:
            while self.running:
                try:
                    self.tick()
                    self._error_streak = 0
                except RateLimitError as exc:
                    log.warning("Binance istek limiti: %s sn bekleniyor", exc.retry_after)
                    self.notifier.send_error("ratelimit", Messages.warning(
                        f"Binance istek limiti aşıldı, {exc.retry_after} sn bekleniyor."))
                    self._sleep(exc.retry_after)
                    continue
                except (requests.RequestException, BinanceAPIError) as exc:
                    self._handle_error(exc, "network")
                    continue
                except Exception as exc:  # noqa: BLE001 - bot beklenmedik hatada çökmesin
                    log.exception("Beklenmedik hata")
                    self._handle_error(exc, f"unexpected-{type(exc).__name__}")
                    continue
                if self.running:
                    self._sleep(self._next_sleep())
        except KeyboardInterrupt:
            log.info("Ctrl+C alındı, bot durduruluyor...")
        finally:
            self.store.save(self.state)
            if not self.state.killed:
                self.notifier.send(Messages.stopped(
                    self.state.capital, self.state.realized_pnl, self.cfg, list(self.state.positions)))
        return 2 if self.state.killed else 0

    def startup(self) -> None:
        if not self.exchange.is_paper:
            offset = self.exchange.client.sync_time()
            log.info("Binance saat farkı: %d ms", offset)
            self.exchange.check_trading_enabled()
        pairs = self.exchange.refresh_rules()
        log.info("%d adet %s paritesi yüklendi", pairs, self.cfg.quote_asset)

        self.notifier.send(Messages.started(self.cfg, self.state.capital, self.state.realized_pnl, pairs,
                                            self.slots()))

        for pos in list(self.state.positions.values()):
            if not self.exchange.is_paper and self.exchange.sellable_qty(
                    pos.symbol, min(pos.qty, self.exchange.free_balance(pos.base_asset))) <= 0:
                self.notifier.send(Messages.warning(
                    f"Kayıtlı {pos.symbol} pozisyonu cüzdanda bulunamadı (elle satılmış olabilir). "
                    f"Son bilinen fiyattan kapatılmış sayılıyor."))
                self._finalize(pos, pos.entry_price, net_exit_value(pos.qty, pos.entry_price, self.cfg.fee_rate),
                               0.0, ExitReason.EXTERNAL, estimated=True)
            else:
                self.notifier.send(Messages.position_resumed(pos))

    def stop(self) -> None:
        self.running = False

    # ================================================================ ana adım
    def tick(self) -> None:
        now = time.time()
        if now - self.exchange.rules_loaded_at > RULES_REFRESH_SEC:
            try:
                self.exchange.refresh_rules()
            except Exception as exc:  # noqa: BLE001 - eski kurallarla devam et
                log.warning("Parite kuralları yenilenemedi: %s", exc)

        if self.state.positions:
            self.manage_positions(now)
        elif kill_switch_hit(self.state.realized_pnl, 0.0, self.cfg.max_total_loss):
            self.trigger_kill(self.state.realized_pnl)
        if not self.running:
            return

        if now >= self.next_scan_at and self.free_slots() > 0:
            self.next_scan_at = now + self.cfg.scan_interval_sec
            self.scan_and_enter(now)

        if self.running and now >= self.next_status_at:
            self.next_status_at = now + self.cfg.status_report_minutes * 60
            self.send_status()

    # ================================================================ pozisyon sayısı
    def slots(self) -> int:
        """Mevcut sermayeyle güvenle açılabilecek pozisyon sayısı."""
        return position_slots(self.state.capital, self.exchange.typical_min_notional(), self.cfg)

    def free_slots(self) -> int:
        return max(0, self.slots() - len(self.state.positions))

    # ================================================================ giriş
    def scan_and_enter(self, now: float) -> None:
        state = self.state
        state.cooldowns = {s: t for s, t in state.cooldowns.items() if t > now}
        slots = self.slots()
        free_slots = slots - len(state.positions)
        if free_slots <= 0:
            return

        capital = state.capital
        target = capital / slots  # Sermaye pozisyonlara eşit bölünür
        excluded = set(state.cooldowns) | set(state.positions)
        result = self.strategy.scan(self.exchange.rules, excluded, target)
        log.info("%s | pozisyon %d/%d", result.summary(), len(state.positions), slots)
        if not result.signals:
            return

        free = self.exchange.free_balance(self.cfg.quote_asset)
        opened = 0
        for signal in result.signals[: free_slots + 2]:
            if opened >= free_slots:
                break
            rules = self.exchange.rules.get(signal.symbol)
            if rules is None or signal.symbol in state.positions:
                continue
            remaining = capital - state.committed
            is_last_slot = len(state.positions) + 1 >= slots
            budget = remaining if is_last_slot else min(target, remaining)  # Son slot artanı da kullanır
            plan = plan_entry(min(budget, free), rules, signal.price, self.cfg)
            if not plan.ok:
                log.info("%s atlandı: %s", signal.symbol, plan.reason)
                if "bakiye" in plan.reason.lower():
                    self.notifier.send_error("low-balance", Messages.warning(
                        f"{plan.reason}. Serbest bakiye: {free:.2f} {self.cfg.quote_asset}"), cooldown_sec=3600)
                    return
                continue
            if self.open_position(signal, plan.quote_amount, slots):
                opened += 1
                free -= state.positions[signal.symbol].cost_usdt

    def open_position(self, signal, quote_amount: float, slots: int = 1) -> bool:
        try:
            fill = self.exchange.market_buy(signal.symbol, quote_amount)
        except OrderError as exc:
            self.notifier.send_error(f"buy-{signal.symbol}", Messages.error(
                f"{signal.symbol} alımı başarısız: {exc}"))
            return False

        rules = self.exchange.rules_for(signal.symbol)
        pos = Position(symbol=signal.symbol, base_asset=rules.base_asset, qty=fill.qty,
                       entry_price=fill.avg_price, cost_usdt=fill.usdt, opened_at=time.time(),
                       buy_fee_usdt=fill.fee_usdt, signal_note=signal.note())
        if not self.exchange.is_paper:
            try:  # Cüzdandaki gerçek miktar daha azsa onu esas al
                held = self.exchange.free_balance(rules.base_asset)
                if 0 < held < pos.qty:
                    pos.qty = held
            except Exception as exc:  # noqa: BLE001
                log.warning("Alış sonrası bakiye doğrulanamadı: %s", exc)

        self.state.positions[pos.symbol] = pos
        self.state.total_fees += fill.fee_usdt
        self.store.save(self.state)
        self.notifier.send(Messages.buy(pos, self.levels(pos), signal.note(), self.cfg,
                                        len(self.state.positions), slots))
        return True

    def levels(self, pos: Position) -> dict:
        """Net K/Z hedeflerine karşılık gelen fiyat seviyeleri."""
        cfg = self.cfg
        qty = self.exchange.sellable_qty(pos.symbol, pos.qty)
        cost = pos.cost_usdt
        return {
            "tp": price_for_pnl(cfg.take_profit_pct, cost, qty, cfg.fee_rate),
            "tp_value": cost * (1 + cfg.take_profit_pct / 100),
            "sl": price_for_pnl(-cfg.stop_loss_pct, cost, qty, cfg.fee_rate),
            "sl_value": cost * (1 - cfg.stop_loss_pct / 100),
            "trail_on": price_for_pnl(cfg.trailing_activation_pct, cost, qty, cfg.fee_rate),
            "trail_floor": price_for_pnl(cfg.trailing_floor_pct, cost, qty, cfg.fee_rate),
            "trail_value": cost * (1 + cfg.trailing_floor_pct / 100),
        }

    # ================================================================ pozisyon takibi
    def mark_position(self, pos: Position, now: float) -> Mark:
        cfg = self.cfg
        bid, _ = self.exchange.get_bid_ask(pos.symbol)
        sellable = self.exchange.sellable_qty(pos.symbol, pos.qty)
        value = net_exit_value(sellable, bid, cfg.fee_rate)
        mark = Mark(bid, value, value - pos.cost_usdt, pnl_pct(value, pos.cost_usdt), now)
        self.marks[pos.symbol] = mark

        old_peak = pos.peak_pnl_pct
        if update_trailing(pos, mark.pnl_pct, cfg):
            floor = trailing_floor_pct(pos, cfg)
            self.notifier.send(Messages.trailing_activated(
                pos, mark.pnl_pct, price_for_pnl(floor, pos.cost_usdt, sellable, cfg.fee_rate), floor, cfg))
            self.store.save(self.state)
        elif pos.peak_pnl_pct > old_peak + 0.25:
            self.store.save(self.state)
        return mark

    def unrealized(self) -> float:
        return sum(m.pnl_usdt for s, m in self.marks.items() if s in self.state.positions)

    def manage_positions(self, now: float) -> None:
        cfg = self.cfg
        for pos in list(self.state.positions.values()):
            self.mark_position(pos, now)

        if now - self._last_position_log >= POSITION_LOG_SEC:
            self._last_position_log = now
            for pos in self.state.positions.values():
                m = self.marks[pos.symbol]
                log.info("Pozisyon %s | fiyat %.8g | net değer %.4f | K/Z %s USDT (%s%%) | zirve %.2f%% | %s",
                         pos.symbol, m.bid, m.net_value, fmt_signed(m.pnl_usdt, 4), fmt_signed(m.pnl_pct),
                         pos.peak_pnl_pct, fmt_duration(now - pos.opened_at))

        # Kill-switch: gerçekleşen + tüm açık pozisyonların zararı toplamı
        if self._check_kill_switch():
            return

        for pos in list(self.state.positions.values()):
            mark = self.marks[pos.symbol]
            reason = evaluate_exit(pos, mark.pnl_pct, now, cfg)
            if reason:
                log.info("%s çıkış koşulu: %s (net K/Z %%%.2f)", pos.symbol, reason.value, mark.pnl_pct)
                self.close_position(pos, reason)

        # Bir pozisyon zararla kapandıysa, kalanlarla birlikte limit aşılmış olabilir
        self._check_kill_switch()

    def _check_kill_switch(self) -> bool:
        realized, unrealized = self.state.realized_pnl, self.unrealized()
        if not kill_switch_hit(realized, unrealized, self.cfg.max_total_loss):
            return False
        log.error("KILL-SWITCH: toplam K/Z %.4f USDT", realized + unrealized)
        left: List[str] = []
        for pos in list(self.state.positions.values()):
            if not self.close_position(pos, ExitReason.KILL_SWITCH):
                left.append(pos.symbol)
        self.trigger_kill(self.state.realized_pnl + self.unrealized(), left)
        return True

    def close_position(self, pos: Position, reason: ExitReason) -> bool:
        try:
            fill = self.exchange.market_sell(pos.symbol, pos.qty)
        except OrderError as exc:
            if exc.code == "NO_BALANCE" and not self.exchange.is_paper:
                mark = self.marks.get(pos.symbol)
                proceeds = mark.net_value if mark else pos.cost_usdt
                price = mark.bid if mark else pos.entry_price
                self.notifier.send(Messages.warning(
                    f"{pos.symbol} cüzdanda bulunamadı (elle satılmış olabilir). Pozisyon kapatıldı sayılıyor."))
                self._finalize(pos, price, proceeds, 0.0, ExitReason.EXTERNAL, estimated=True)
                return True
            self.notifier.send_error(f"sell-{pos.symbol}-{exc.code}", Messages.error(
                f"{pos.symbol} satılamadı ({reason.value}): {exc}. Tekrar denenecek."), cooldown_sec=300)
            return False
        self._finalize(pos, fill.avg_price, fill.usdt, fill.fee_usdt, reason)
        return True

    def _finalize(self, pos: Position, exit_price: float, proceeds: float, sell_fee: float,
                  reason: ExitReason, estimated: bool = False) -> None:
        now = time.time()
        pnl = proceeds - pos.cost_usdt
        pct = pnl_pct(proceeds, pos.cost_usdt)
        state = self.state
        state.realized_pnl += pnl
        state.trades += 1
        if pnl >= 0:
            state.wins += 1
        else:
            state.losses += 1
        state.total_fees += sell_fee
        state.positions.pop(pos.symbol, None)
        state.cooldowns[pos.symbol] = now + self.cfg.symbol_cooldown_minutes * 60
        self.marks.pop(pos.symbol, None)
        self.store.save(state)
        self.trade_log.append(pos, exit_price, proceeds, pos.buy_fee_usdt + sell_fee, reason.value, now)

        self.notifier.send(Messages.sell(
            reason.value, pos, exit_price, proceeds, sell_fee, pnl, pct, now - pos.opened_at,
            state.capital, state.realized_pnl, state.trades, state.wins, state.losses, self.cfg, estimated,
            open_positions=len(state.positions)))

    # ================================================================ kill-switch
    def trigger_kill(self, total_pnl: float, positions_left: Optional[List[str]] = None) -> None:
        if self.state.killed:
            self.running = False
            return
        self.state.killed = True
        self.state.kill_reason = f"Toplam K/Z {total_pnl:.4f} USDT (limit -{self.cfg.max_total_loss})"
        self.state.killed_at = time.time()
        self.store.save(self.state)
        balance = self.state.start_capital + total_pnl
        self.notifier.send(Messages.kill_switch(total_pnl, balance, self.cfg, positions_left or []))
        self.running = False

    # ================================================================ yardımcılar
    def send_status(self) -> None:
        state = self.state
        lines = []
        for pos in state.positions.values():
            m = self.marks.get(pos.symbol)
            if m:
                lines.append(f"📦 <b>{pos.symbol}</b> {fmt_signed(m.pnl_pct)}% "
                             f"({fmt_signed(m.pnl_usdt)} {self.cfg.quote_asset}) | "
                             f"{fmt_duration(time.time() - pos.opened_at)}")
        unrealized = self.unrealized()
        total = state.realized_pnl + unrealized
        self.notifier.send(Messages.status(
            state.capital + unrealized, total, state.trades, state.wins, state.losses, self.cfg,
            "\n".join(lines), self.cfg.max_total_loss + total, len(state.positions), self.slots()), silent=True)

    def _next_sleep(self) -> float:
        if self.state.positions:
            return self.cfg.position_check_sec
        return max(1.0, min(self.next_scan_at - time.time(), self.cfg.scan_interval_sec))

    def _handle_error(self, exc: Exception, key: str) -> None:
        self._error_streak += 1
        log.error("Hata (%d. kez üst üste): %s", self._error_streak, exc)
        self.notifier.send_error(key, Messages.error(f"{type(exc).__name__}: {exc}"))
        self._sleep(min(60, 5 * self._error_streak))

    @staticmethod
    def _sleep(seconds: float) -> None:
        time.sleep(max(0.0, seconds))
