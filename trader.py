"""
Ana işlem motoru (TradingBot).

Döngü:
  - Açık pozisyon YOKSA: her SCAN_INTERVAL_SEC (30 sn) piyasayı tarar, sinyal varsa alır.
  - Açık pozisyon VARSA: her POSITION_CHECK_SEC (5 sn) fiyatı kontrol eder;
    kâr al / zarar kes / izleyen stop / kill-switch koşullarına "anında" tepki verir.

11 USDT'lik bakiye ve Binance'in ~5 USDT minimum emir kuralı nedeniyle aynı anda
tek pozisyon açılır (iki pozisyona bölünürse stop-loss'ta satış minimumun altına düşer).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests

from binance_client import BinanceAPIError, RateLimitError
from exchange import BaseExchange, OrderError
from logger import get_logger
from risk import (ExitReason, Position, evaluate_exit, kill_switch_hit, net_exit_value,
                  plan_entry, pnl_pct, price_for_pnl, trailing_floor_pct, update_trailing)
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
        self.last_mark: Optional[Mark] = None
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
                    self.state.capital, self.state.realized_pnl, self.cfg,
                    self.state.position.symbol if self.state.position else None))
        return 2 if self.state.killed else 0

    def startup(self) -> None:
        if not self.exchange.is_paper:
            offset = self.exchange.client.sync_time()
            log.info("Binance saat farkı: %d ms", offset)
            self.exchange.check_trading_enabled()
        pairs = self.exchange.refresh_rules()
        log.info("%d adet %s paritesi yüklendi", pairs, self.cfg.quote_asset)

        self.notifier.send(Messages.started(self.cfg, self.state.capital, self.state.realized_pnl, pairs))

        pos = self.state.position
        if pos:
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

        if self.state.position:
            self.manage_position(now)
        else:
            if kill_switch_hit(self.state.realized_pnl, 0.0, self.cfg.max_total_loss):
                self.trigger_kill(self.state.realized_pnl)
                return
            if now >= self.next_scan_at:
                self.next_scan_at = now + self.cfg.scan_interval_sec
                self.scan_and_enter(now)

        if self.running and now >= self.next_status_at:
            self.next_status_at = now + self.cfg.status_report_minutes * 60
            self.send_status()

    # ================================================================ giriş
    def scan_and_enter(self, now: float) -> None:
        self.state.cooldowns = {s: t for s, t in self.state.cooldowns.items() if t > now}
        capital = self.state.capital
        result = self.strategy.scan(self.exchange.rules, set(self.state.cooldowns), capital)
        log.info(result.summary())
        if not result.signals:
            return

        free = self.exchange.free_balance(self.cfg.quote_asset)
        available = min(capital, free)
        for signal in result.signals[:3]:
            rules = self.exchange.rules.get(signal.symbol)
            if rules is None:
                continue
            plan = plan_entry(available, rules, signal.price, self.cfg)
            if not plan.ok:
                log.info("%s atlandı: %s", signal.symbol, plan.reason)
                if "bakiye" in plan.reason.lower():
                    self.notifier.send_error("low-balance", Messages.warning(
                        f"{plan.reason}. Serbest bakiye: {free:.2f} {self.cfg.quote_asset}"), cooldown_sec=3600)
                    return
                continue
            self.open_position(signal, plan.quote_amount)
            return

    def open_position(self, signal, quote_amount: float) -> bool:
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

        self.state.position = pos
        self.state.total_fees += fill.fee_usdt
        self.store.save(self.state)
        self.notifier.send(Messages.buy(pos, self.levels(pos), signal.note(), self.cfg))
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
    def manage_position(self, now: float) -> None:
        pos = self.state.position
        cfg = self.cfg
        bid, _ = self.exchange.get_bid_ask(pos.symbol)
        sellable = self.exchange.sellable_qty(pos.symbol, pos.qty)
        value = net_exit_value(sellable, bid, cfg.fee_rate)
        pnl_usdt = value - pos.cost_usdt
        pnl = pnl_pct(value, pos.cost_usdt)
        self.last_mark = Mark(bid, value, pnl_usdt, pnl, now)

        old_peak = pos.peak_pnl_pct
        if update_trailing(pos, pnl, cfg):
            floor = trailing_floor_pct(pos, cfg)
            self.notifier.send(Messages.trailing_activated(
                pos, pnl, price_for_pnl(floor, pos.cost_usdt, sellable, cfg.fee_rate), floor, cfg))
            self.store.save(self.state)
        elif pos.peak_pnl_pct > old_peak + 0.25:
            self.store.save(self.state)

        if now - self._last_position_log >= POSITION_LOG_SEC:
            self._last_position_log = now
            log.info("Pozisyon %s | fiyat %.8g | net değer %.4f | K/Z %s USDT (%s%%) | zirve %.2f%% | %s",
                     pos.symbol, bid, value, fmt_signed(pnl_usdt, 4), fmt_signed(pnl),
                     pos.peak_pnl_pct, fmt_duration(now - pos.opened_at))

        # Kill-switch: gerçekleşen + açık pozisyon zararı toplamı
        if kill_switch_hit(self.state.realized_pnl, pnl_usdt, cfg.max_total_loss):
            total = self.state.realized_pnl + pnl_usdt
            log.error("KILL-SWITCH: toplam K/Z %.4f USDT", total)
            sold = self.close_position(ExitReason.KILL_SWITCH)
            self.trigger_kill(self.state.realized_pnl if sold else total,
                              position_left=None if sold else pos.symbol)
            return

        reason = evaluate_exit(pos, pnl, now, cfg)
        if reason:
            log.info("Çıkış koşulu: %s (net K/Z %%%.2f)", reason.value, pnl)
            self.close_position(reason)

    def close_position(self, reason: ExitReason) -> bool:
        pos = self.state.position
        try:
            fill = self.exchange.market_sell(pos.symbol, pos.qty)
        except OrderError as exc:
            if exc.code == "NO_BALANCE" and not self.exchange.is_paper:
                mark = self.last_mark
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
        state.position = None
        state.cooldowns[pos.symbol] = now + self.cfg.symbol_cooldown_minutes * 60
        self.last_mark = None
        self.store.save(state)
        self.trade_log.append(pos, exit_price, proceeds, pos.buy_fee_usdt + sell_fee, reason.value, now)

        self.notifier.send(Messages.sell(
            reason.value, pos, exit_price, proceeds, sell_fee, pnl, pct, now - pos.opened_at,
            state.capital, state.realized_pnl, state.trades, state.wins, state.losses, self.cfg, estimated))

        if reason != ExitReason.KILL_SWITCH and kill_switch_hit(state.realized_pnl, 0.0, self.cfg.max_total_loss):
            self.trigger_kill(state.realized_pnl)

    # ================================================================ kill-switch
    def trigger_kill(self, total_pnl: float, position_left: Optional[str] = None) -> None:
        if self.state.killed:
            self.running = False
            return
        self.state.killed = True
        self.state.kill_reason = f"Toplam K/Z {total_pnl:.4f} USDT (limit -{self.cfg.max_total_loss})"
        self.state.killed_at = time.time()
        self.store.save(self.state)
        balance = self.state.capital if position_left is None else self.state.start_capital + total_pnl
        self.notifier.send(Messages.kill_switch(total_pnl, balance, self.cfg, position_left))
        self.running = False

    # ================================================================ yardımcılar
    def send_status(self) -> None:
        state = self.state
        unrealized = 0.0
        position_line = ""
        if state.position and self.last_mark:
            m = self.last_mark
            unrealized = m.pnl_usdt
            position_line = (f"📦 Açık pozisyon: <b>{state.position.symbol}</b> "
                             f"{fmt_signed(m.pnl_pct)}% ({fmt_signed(m.pnl_usdt)} {self.cfg.quote_asset}) | "
                             f"{fmt_duration(time.time() - state.position.opened_at)}")
        total = state.realized_pnl + unrealized
        self.notifier.send(Messages.status(
            state.capital + unrealized, total, state.trades, state.wins, state.losses, self.cfg,
            position_line, self.cfg.max_total_loss + total), silent=True)

    def _next_sleep(self) -> float:
        if self.state.position:
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
