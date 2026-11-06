"""
Vadeli işlem motoru (STRATEGY=futures): Binance USDⓈ-M'de kaldıraçlı long / short scalping.

FuturesBot, TradingBot'un Telegram komutlarını, menüleri, canlı panoyu, raporları, kill-switch'i ve durum kaydını
aynen kullanır; yalnızca tarama, pozisyon açma / yönetme / kapatma kısımları vadeli işleme göre değişir:

  Döngü (her 5 dk mum kapanışından ~3 sn sonra; açık pozisyon yokken):
    1. FUTURES_COINS'teki coinlerin son 300 kapanmış 5 dk mumu + BTC'nin 1 saatlik mumları alınır.
    2. strategy.FuturesScalpStrategy.evaluate 4'lü onayı arar (likidasyon + hacim + RSI uyumsuzluğu + BTC trendi).
    3. En güçlü sinyalde: set_margin_mode(ISOLATED) + set_leverage(FUTURES_LEVERAGE) → serbest bakiyenin
       FUTURES_MARGIN_USAGE_PCT'si teminat (bileşik) → piyasa emri → Binance'e STOP_MARKET zarar kes (konamazsa
       pozisyon hemen kapatılır) ve Kâr Al emri.
  Açık pozisyonda (~2 sn'de bir):
    - Binance'te stop / hedef çalıştıysa kapanış kaydedilir (bot kapalıyken çalıştıysa açılışta).
    - Başabaş kalkanı: fiyat girişten +BREAKEVEN_TRIGGER_PCT'ye gelince Binance'teki stop girişin
      +BREAKEVEN_FLOOR_PCT üstüne taşınır (short'ta altına).
    - Zaman stopu: MAX_HOLD_MINUTES dolunca piyasa emriyle kapatılır.
    - Yedek: fiyat stopun ötesine geçtiği hâlde Binance'teki emir çalışmadıysa bot piyasa emriyle kapatır.
Yüzdeler (Kâr Al, Zarar Kes, başabaş) FİYAT hareketidir; 10x'te fiyat +%1.2 ≈ teminatın +%12'si (ROE).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

from config import futures_coin_list, futures_crash_rules, futures_liq_distance_pct, futures_modules, futures_pump_rules
from futures_exchange import FuturesError, MinNotionalError, describe_error
from logger import get_logger
from risk import ExitReason, Position, cooldown_minutes, kill_switch_hit
from strategy import FUT_BAR_MS, FUT_BTC_1H_LIMIT, FuturesSignal, ScanResult, futures_kline_limit
from telegram_bot import CRITICAL_EXITS, Messages, esc, fmt_duration, fmt_price, fmt_signed, fmt_usdt, futures_tag
from trader import ALIVE_SAVE_SEC, POSITION_LOG_SEC, RULES_REFRESH_SEC, Mark, TradingBot

log = get_logger()

SCAN_DELAY_SEC = 3            # 5 dk mum kapandıktan bu kadar sonra taranır (Binance'te yerleşsin)
POSITION_POLL_SEC = 2.0       # Açık pozisyon kontrol aralığı
MAX_SCAN_LAG_SEC = 90         # Mum kapanışından bu kadar sonra (kesinti / yavaşlık) o mumun sinyali alınmaz
MARGIN_RETRY_FACTOR = 0.9     # "Margin is insufficient" (-2019): miktar %10 küçültülüp bir kez daha denenir
SOFT_STOP_GRACE_PCT = 0.15    # Fiyat stopu bu kadar (fiyat %) geçtiği hâlde Binance emri çalışmadıysa bot kapatır
BE_RETRY_SEC = 30             # Başabaş stopu taşınamadıysa bu kadar sonra tekrar denenir
TRAIL_MOVE_SEC = 10           # İzleyen stop: Binance'teki stop emri en sık bu aralıkla taşınır (bot arada kendisi izler)
TRAIL_MIN_STEP_PCT = 0.1      # ...ve yeni seviye eskisinden en az bu kadar (fiyat %) iyiyse
PREFILTER_SLACK_PCT = 1.0     # En hacimli N + çöküş: 24 saatlik tepeye göre ön elemede pay (fiyat puanı)
KLINE_PARALLEL_MIN = 16       # Bundan çok coinin mumları paralel istenir (120-150 coinlik evrende tarama gecikmesin)
KLINE_WORKERS = 6
UNIVERSE_REFRESH_SEC = 24 * 3600   # En hacimli N köklü coin sıralaması günde bir yenilenir (parite başına 1 istek)


@dataclass
class FMark(Mark):
    roe: float = 0.0          # Binance'in gösterdiği ROE (%): gerçekleşmemiş K/Z ÷ teminat (komisyonsuz)


def _sign(pos) -> int:
    return 1 if pos.side == "long" else -1


def _worse(pos, price: float, level: float) -> bool:
    """Fiyat seviyenin zarar tarafında mı (long: altında, short: üstünde)?"""
    return price <= level if pos.side == "long" else price >= level


def _stop_reason(pos) -> ExitReason:
    """Binance'teki stop emri çalıştığında kapanış nedeni: izleyen stop > başabaş > zarar kes."""
    if pos.trailing_active:
        return ExitReason.TRAILING_STOP
    return ExitReason.BREAKEVEN if pos.breakeven_active else ExitReason.STOP_LOSS


def _reached(pos, price: float, level: float) -> bool:
    """Fiyat lehte seviyeye ulaştı mı (long: üstünde, short: altında)?"""
    return price >= level if pos.side == "long" else price <= level


class FuturesBot(TradingBot):
    is_futures = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.next_scan_at = self._next_close(self.time_fn()) + SCAN_DELAY_SEC
        self.last_signals: List[FuturesSignal] = []
        self._be_retry_at: Dict[str, float] = {}
        self._trail_at: Dict[str, float] = {}
        self.universe: List[str] = []          # FUTURES_UNIVERSE: en hacimli N köklü parite (sıralamanın ilk N'i)
        self._ranking: List[str] = []          # Tüm köklü pariteler, ortalama hacme göre
        self._ranking_at = 0.0
        self._ranking_key: tuple = ()

    # ================================================================ kapasite
    def slots(self) -> int:
        return 1                  # Tüm serbest bakiye tek pozisyona (bileşik)

    def target_slots(self) -> int:
        return 1

    def free_slots(self) -> int:
        return 0 if self.state.positions else 1

    @staticmethod
    def _next_close(now: float) -> float:
        bar = FUT_BAR_MS // 1000
        return (int(now) // bar + 1) * bar

    # ================================================================ açılış
    def startup(self) -> None:
        cfg, ex = self.cfg, self.exchange
        ex.load_markets()
        if cfg.futures_universe > 0:
            self._refresh_universe(self.time_fn())
            coins = [s[:-4] for s in self._universe_symbols()]
            missing: List[str] = []
        else:
            coins = list(futures_coin_list(cfg))
            missing = [c for c in coins if not ex.has_market(c + "USDT")]
        mode_note = ex.check_one_way(has_positions=bool(self.state.positions))
        free = None if ex.is_paper else ex.free_balance()
        now = self.time_fn()
        if not self._announced:
            self.notifier.send(Messages.futures_started(
                cfg, self.state.capital, self.state.realized_pnl, len(coins) - len(missing),
                [c for c in coins if c not in missing], self.loss_limit, self.loss_pct, mode_note, free))
            for note in cfg.notes:
                self.notifier.send(Messages.warning(note))
            if missing:
                self.notifier.send(Messages.warning("Binance vadeli işlemlerde bulunamayan (atlanan) coinler: "
                                                    + ", ".join(missing)))
            if self.state.paused:
                self.notifier.send("⏸️ Yeni işlemler daha önce /durdur ile durdurulmuştu. Devam için /devam yazın.")
            if self.state.last_alive and now - self.state.last_alive > cfg.outage_alert_sec:
                self.notifier.send(Messages.outage((now - self.state.last_alive) / 60, list(self.state.positions),
                                                   startup=True), critical=bool(self.state.positions))
            self._announced = True
        log.info("Vadeli bot başladı: %s | %d coin | %s", "test" if ex.is_paper else "CANLI", len(coins), mode_note)
        self._reconcile_positions_on_startup()
        self._init_report_markers(now)
        self.state.last_alive = now
        self.store.save(self.state)
        if self.command_listener is not None:
            self.command_listener.start()
        if self.ip_monitor is not None:
            self.ip_monitor.start()
        self._sync_liq_feed()

    def _sync_liq_feed(self) -> None:
        """Likidasyon akışı: FUTURES_LIQ_SOURCE=feed / both (giriş şartı) veya LIQ_FEED (bildirim) için."""
        smart = self.smart
        want = self.cfg.liq_feed or self.cfg.futures_liq_source != "proxy"
        if want and smart.liq_feed is None:
            from liquidations import LiquidationFeed
            feed = LiquidationFeed()
            if feed.available:
                smart.liq_feed = feed
                feed.start()
            else:
                log.warning("websocket-client kurulu değil; likidasyon akışı başlatılamadı")
        elif not want and smart.liq_feed is not None:
            smart.liq_feed.stop()
            smart.liq_feed = None

    def apply_config(self, cfg) -> None:
        """Telegram'dan değişen ayarlar: borsa katmanı da (komisyon / kayma varsayımları) yeni ayarları kullanır."""
        super().apply_config(cfg)
        self.exchange.cfg = cfg

    def _check_commission(self) -> None:
        return   # Spot BNB / komisyon kontrolü vadeli işlemde yok

    def fee_status_line(self) -> str:
        cfg = self.cfg
        return (f"💸 Vadeli komisyon (ayar): piyasa %{cfg.futures_taker_fee * 100:.3f} / limit "
                f"%{cfg.futures_maker_fee * 100:.3f}")

    # ================================================================ ana adım
    def tick(self) -> None:
        now = self.time_fn()
        ex = self.exchange
        if now - ex.markets_loaded_at > RULES_REFRESH_SEC:
            try:
                ex.load_markets()
            except Exception as exc:  # noqa: BLE001 - eski kurallarla devam
                log.warning("Vadeli pariteler yenilenemedi: %s", exc)
        self.process_commands()
        if not self.running:
            return
        if self.state.positions:
            self.manage_positions(now)
        elif self.state.killed or kill_switch_hit(self.state.realized_pnl, 0.0, self.loss_limit):
            self.trigger_kill(self.state.realized_pnl)
        if not self.running:
            return
        if now >= self.next_scan_at:
            self.next_scan_at = self._next_close(now) + SCAN_DELAY_SEC
            if (not self.state.positions and not self.state.paused and not self.state.killed
                    and not self.entries_frozen(now)):
                self.scan_and_enter(now)
        self._periodic(now)

    def _periodic(self, now: float) -> None:
        if now >= self.next_status_at:
            self.next_status_at = now + self.cfg.status_report_minutes * 60
            self.send_status(routine=True)
        self._maybe_reports(now)
        self._check_freeze_end(now)
        self._check_ip()
        self.smart.update_board(now)
        self.smart.check_liquidations(now)
        self.backtests.poll(now)
        if self.equity_log is not None and now >= self.next_equity_at:
            self.next_equity_at = now + self.cfg.equity_log_minutes * 60
            self.equity_log.append(now, self.state.capital, self.unrealized(), len(self.state.positions))
        if now - self._last_alive_save >= ALIVE_SAVE_SEC:
            self._last_alive_save = now
            self.trade_log.flush_pending()
            self.state.last_alive = now
            self.store.save(self.state)

    def _next_sleep(self) -> float:
        if self.state.positions:
            return POSITION_POLL_SEC
        return max(1.0, min(self.next_scan_at - self.time_fn(), 30.0))

    def bid_ask(self, symbol: str):
        return self.exchange.bid_ask(symbol)

    # ================================================================ tarama ve giriş
    def scan_and_enter(self, now: float) -> None:
        ex = self.exchange
        now_ms = int(now * 1000)
        closed_ms = now_ms // FUT_BAR_MS * FUT_BAR_MS          # Az önce kapanan mumun kapanış zamanı
        if now_ms - closed_ms > MAX_SCAN_LAG_SEC * 1000:
            return
        state = self.state
        state.cooldowns = {s: t for s, t in state.cooldowns.items() if t > now}   # Süresi dolan beklemeler
        klines, errors = {}, []
        symbols, skipped = self._scan_symbols()
        limit = futures_kline_limit(self.cfg)       # En uzun pencere + izleme süresi + oynaklık geçmişi
        excluded = self._scan_excluded()
        targets = [s for s in symbols if ex.has_market(s) and s not in excluded]

        def fetch(symbol: str):
            try:
                return symbol, ex.klines(symbol, "5m", limit), None
            except FuturesError as exc:
                return symbol, None, exc

        if len(targets) > KLINE_PARALLEL_MIN:        # Ağ hatası (ConnectionError) sıralı sürümdeki gibi yükselir
            with ThreadPoolExecutor(max_workers=KLINE_WORKERS) as pool:
                results = list(pool.map(fetch, targets))
        else:
            results = [fetch(s) for s in targets]
        for symbol, k, exc in results:
            if exc is not None:
                errors.append(f"{symbol}: {exc}")
                continue
            if len(k["t"]) and int(k["t"][-1]) + FUT_BAR_MS == closed_ms:   # Yalnızca az önce kapanan mum
                klines[symbol] = k
        btc_1h = ex.klines("BTCUSDT", "1h", FUT_BTC_1H_LIMIT) if self.strategy.needs_btc_trend else None
        signals, rejects = self.strategy.evaluate(klines, btc_1h, now_ms, feed=self.smart.liq_feed)
        if errors:
            log.warning("Vadeli tarama: %d coinin mumları alınamadı (%s)", len(errors), "; ".join(errors[:3]))
            rejects["veri alınamadı"] += len(errors)
        if skipped:
            rejects["çöküş / pompa yok (24 saatlik aralık dar)" if futures_pump_rules(self.cfg)
                    else "çöküş yok (24 saatlik tepeye yakın)"] += skipped
        result = ScanResult(signals=list(signals), candidates=len(symbols) + skipped, analyzed=len(klines),
                            rejects=rejects)
        self._record_scan(result, now)
        self.last_signals = list(signals)
        if not signals:
            return
        sig = signals[0]
        log.info("Vadeli sinyal: %s %s | %s", sig.symbol, sig.side, sig.note())
        try:
            self.open_futures(sig, now)
        except MinNotionalError as exc:
            log.warning("Minimum emir tutarı: %s", exc)
            self.notifier.send(Messages.warning(f"{sig.symbol} {sig.side.upper()} sinyali pas geçildi: "
                                                f"{describe_error(exc)}"))
        except FuturesError as exc:
            log.error("Vadeli pozisyon açılamadı: %s", exc)
            self.notifier.send(Messages.error(f"{sig.symbol} {sig.side.upper()} pozisyonu açılamadı: "
                                              f"{describe_error(exc)}"))

    def _refresh_universe(self, now: float, force: bool = False) -> None:
        """En hacimli N köklü parite listesini (gerekirse) yeniler: günde bir ya da ayar değişince."""
        cfg = self.cfg
        key = (cfg.futures_universe_min_age_days, cfg.futures_universe_avg_days)
        if not force and self._ranking and key == self._ranking_key and now - self._ranking_at < UNIVERSE_REFRESH_SEC:
            self.universe = self._ranking[:cfg.futures_universe]
            return
        log.info("En hacimli köklü pariteler sıralanıyor (en az %d günlük, son %d gün ortalaması)...", *key)
        try:
            ranking = [s for s, _qv in self.exchange.seasoned_ranking(*key)]
        except (FuturesError, requests.ConnectionError) as exc:
            log.warning("Hacim sıralaması alınamadı (önceki liste kullanılıyor): %s", exc)
            self._ranking_at = now - UNIVERSE_REFRESH_SEC + 600          # 10 dk sonra tekrar dene
            self.universe = self._ranking[:cfg.futures_universe]
            return
        if ranking:
            self._ranking, self._ranking_key, self._ranking_at = ranking, key, now
        self.universe = self._ranking[:cfg.futures_universe]
        log.info("En hacimli %d köklü parite: %s", len(self.universe), ", ".join(s[:-4] for s in self.universe))

    def _universe_symbols(self) -> List[str]:
        """En hacimli N köklü parite (+ FUTURES_UNIVERSE_WITH_LIST açıksa FUTURES_COINS listesindekiler)."""
        symbols = list(self.universe)
        if self.cfg.futures_universe_with_list:
            symbols += [c + "USDT" for c in futures_coin_list(self.cfg) if c + "USDT" not in symbols]
        return symbols

    def _scan_symbols(self) -> Tuple[List[str], int]:
        """Taranacak pariteler ve ön elemede atlananların sayısı.

        FUTURES_UNIVERSE=0: FUTURES_COINS listesi. N > 0: en hacimli N köklü parite (sıralama günde bir yenilenir).
        Çöküş taktiğinde, fiyatı 24 saatlik tepesinin (en küçük eşik - pay) kadar altında olmayan coinlerin mumları
        hiç istenmez: son H (≤ 24) saatte %X düşen coin, 24 saatlik tepesinin de en az %X altındadır. Teyit beklenirken
        (izleme modu) fiyat çöküşten sonra toparlanmış olabilir: o zaman 24 saatlik DİP tepenin %X altında mı diye
        bakılır (pencere + izleme süresi 24 saati aşarsa ön eleme yapılmaz). Pompa modülü açıksa fiyatı 24 saatlik
        dibinin en az %Y üstünde olanlar da taranır; Bollinger / kırılım modülleri her coinde sinyal verebileceği için
        onlar açıkken ön eleme yapılmaz."""
        cfg = self.cfg
        if cfg.futures_universe <= 0:
            return [c + "USDT" for c in futures_coin_list(cfg)], 0
        self._refresh_universe(self.time_fn())
        symbols = self._universe_symbols()
        if cfg.futures_tactic != "crash" or not symbols:
            return symbols, 0
        mods = futures_modules(cfg)
        if "bb" in mods or "breakout" in mods:
            return symbols, 0
        rules = futures_crash_rules(cfg)
        watch = cfg.futures_confirm != "close"
        reach_h = max(h for h, _p in rules) + (cfg.futures_confirm_bars / 12 if watch else 0)
        if reach_h > 24:
            return symbols, 0
        least = cfg.futures_crash_z_min_pct if cfg.futures_crash_z > 0 else min(p for _h, p in rules)
        pump = futures_pump_rules(cfg)
        try:
            tickers = self.exchange.tickers_24h()
        except (FuturesError, requests.ConnectionError) as exc:
            log.warning("24 saatlik özetler alınamadı (ön eleme yapılmadı): %s", exc)
            return symbols, 0
        need = max(0.0, least - PREFILTER_SLACK_PCT) / 100
        need_up = max(0.0, min(p for _h, p in pump) - PREFILTER_SLACK_PCT) / 100 if pump else None
        keep = []
        for symbol in symbols:
            t = tickers.get(symbol)
            price = t.get("low", 0.0) if t is not None and watch else (t or {}).get("last", 0.0)
            if t is None or t["high"] <= 0 or price <= 0 or price <= t["high"] * (1 - need):
                keep.append(symbol)                  # Özet yoksa temkinli: mumlara bakılır
            elif need_up is not None and 0 < t.get("low", 0.0) and t["last"] >= t["low"] * (1 + need_up):
                keep.append(symbol)                  # Pompa adayı: 24 saatlik dibinin yeterince üstünde
        return keep, len(symbols) - len(keep)

    def open_futures(self, sig: FuturesSignal, now: float) -> Optional[Position]:
        cfg, ex = self.cfg, self.exchange
        symbol, side = sig.symbol, sig.side
        lev = int(cfg.futures_leverage)
        room = futures_liq_distance_pct(lev, cfg.futures_mmr_pct)
        if cfg.stop_loss_pct >= room - 0.5:   # Ayar doğrulaması da engeller; kaldıraç çalışırken değişebilir
            raise FuturesError(f"Zarar kes (%{cfg.stop_loss_pct:g}) {lev}x'te likidasyona (%{room:.1f}) çok yakın")
        # 1) İşleme girmeden HEMEN ÖNCE: yalıtılmış teminat + kaldıraç (her girişte, zorunlu)
        ex.prepare_symbol(symbol, lev)
        # 2) Büyüklük: serbest bakiyenin FUTURES_MARGIN_USAGE_PCT'si teminat × kaldıraç (bileşik)
        free = ex.free_balance()
        bid, ask = ex.bid_ask(symbol)
        price = ask if side == "long" else bid
        margin = free * cfg.futures_margin_usage_pct / 100
        qty = ex.amount_to_precision(symbol, margin * lev / price)
        ex.check_order_size(symbol, qty, price)                  # MinNotionalError → işlem pas geçilir
        order_side = "buy" if side == "long" else "sell"
        try:
            fill = ex.market_order(symbol, order_side, qty)
        except FuturesError as exc:
            if exc.code != "-2019":
                raise
            qty = ex.amount_to_precision(symbol, qty * MARGIN_RETRY_FACTOR)   # Komisyon payı yetmedi: biraz küçült
            ex.check_order_size(symbol, qty, price)
            fill = ex.market_order(symbol, order_side, qty)
        entry, qty = fill.price, fill.qty
        sign = 1 if side == "long" else -1
        tp = ex.price_to_precision(symbol, entry * (1 + sign * cfg.take_profit_pct / 100))
        sl = ex.price_to_precision(symbol, entry * (1 - sign * cfg.stop_loss_pct / 100))
        margin_used = qty * entry / lev
        pos = Position(symbol=symbol, base_asset=symbol[:-4], qty=qty, entry_price=entry,
                       cost_usdt=margin_used + fill.fee, opened_at=now, buy_fee_usdt=fill.fee,
                       signal_note=sig.note(), score=sig.score, metrics=dict(sig.metrics), futures=True, side=side,
                       leverage=lev, margin_usdt=margin_used,
                       liq_price=entry * (1 - sign * (1 / lev - cfg.futures_mmr_pct / 100)), sl_price=sl, tp_price=tp,
                       regime=f"{side.upper()} {lev}X")      # İşlem kaydında (CSV) yön ve kaldıraç görünsün
        self.state.positions[symbol] = pos
        if ex.is_paper:
            ex.open_paper(symbol, side, qty, entry, margin_used, lev)
        self.store.save(self.state)
        # 3) Koruma: zarar kes Binance'te bekler (bot kapansa da çalışır); konamazsa pozisyon korumasız kalmaz
        try:
            pos.stop_order_id = ex.place_stop(symbol, side, sl)
        except FuturesError as exc:
            self.notifier.send(Messages.error(f"{symbol} zarar kes emri Binance'e konamadı: {describe_error(exc)}"),
                               critical=True)
            if cfg.futures_exchange_stop:
                self.notifier.send(Messages.warning(f"{symbol} korumasız kalmasın diye pozisyon hemen kapatılıyor "
                                                    f"(FUTURES_EXCHANGE_STOP=true)."), critical=True)
                self.close_position(pos, ExitReason.SAFETY)
                return None
        try:
            pos.tp_order_id = ex.place_take_profit(symbol, side, qty, tp, limit=cfg.futures_tp_order == "limit")
        except FuturesError as exc:
            self.notifier.send(Messages.warning(f"{symbol} kâr al emri Binance'e konamadı ({exc}); bot hedefi "
                                                f"kendisi izleyecek."))
        self.store.save(self.state)
        self.day_stats.buys += 1
        log.info("Vadeli pozisyon açıldı: %s %s %gx | giriş %.8g | miktar %g | teminat %.4f | TP %.8g | SL %.8g",
                 symbol, side, lev, entry, qty, margin_used, tp, sl)
        self.notifier.notify(Messages.futures_open(pos, pos.signal_note, cfg, qty * entry), "open")
        return pos

    # ================================================================ değerleme
    def mark_position(self, pos: Position, now: float, price: Optional[float] = None) -> Mark:
        if not pos.futures:
            return super().mark_position(pos, now)
        if price is None:
            price = self.exchange.last_price(pos.symbol)
        gross = _sign(pos) * pos.qty * (price - pos.entry_price)
        exit_fee = pos.qty * price * self.cfg.futures_taker_fee
        pnl = gross - pos.buy_fee_usdt - exit_fee
        net_pct = pnl / pos.cost_usdt * 100 if pos.cost_usdt else 0.0
        roe = gross / pos.margin_usdt * 100 if pos.margin_usdt else 0.0
        mark = FMark(price, pos.cost_usdt + pnl, pnl, net_pct, now, roe)
        self.marks[pos.symbol] = mark
        pos.peak_pnl_pct = max(pos.peak_pnl_pct, net_pct)
        pos.min_pnl_pct = min(pos.min_pnl_pct, net_pct)
        return mark

    def levels(self, pos: Position) -> dict:
        if not pos.futures:
            return super().levels(pos)
        trig = pos.entry_price * (1 + _sign(pos) * self.cfg.breakeven_trigger_pct / 100)
        return {"tp": pos.tp_price, "sl": pos.sl_price, "trail_on": trig, "liq": pos.liq_price}

    # ================================================================ pozisyon yönetimi
    def manage_positions(self, now: float) -> None:
        ex = self.exchange
        for pos in list(self.state.positions.values()):
            if not pos.futures:
                continue
            if not ex.is_paper and ex.position(pos.symbol) is None:
                self._closed_on_exchange(pos)                      # Stop / hedef / likidasyon / elle kapatma
                continue
            price = ex.last_price(pos.symbol)
            if ex.is_paper:
                hit = ex.triggered(pos.symbol, price)
                if hit is not None:
                    self._paper_fill(pos, hit[1], price)
                    continue
            mark = self.mark_position(pos, now, price)
            reason = self._exit_reason(pos, price, now)
            if reason is not None:
                log.info("%s çıkış: %s (fiyat %.8g, ROE %.2f%%)", pos.symbol, reason.value, price, mark.roe)
                self.close_position(pos, reason)
                continue
            self._check_breakeven(pos, price, mark, now)
            if pos.symbol in self.state.positions:
                self._check_trailing(pos, price, mark, now)
        if now - self._last_position_log >= POSITION_LOG_SEC:
            self._last_position_log = now
            for pos in self.state.positions.values():
                m = self.marks.get(pos.symbol)
                if m:
                    log.info("Pozisyon %s %s | fiyat %.8g | ROE %.2f%% | net %s USDT | %s", pos.symbol,
                             futures_tag(pos), m.bid, getattr(m, "roe", 0.0), fmt_signed(m.pnl_usdt, 4),
                             fmt_duration(now - pos.opened_at))
        self._check_kill_switch()

    def _exit_reason(self, pos: Position, price: float, now: float) -> Optional[ExitReason]:
        cfg = self.cfg
        if self.cfg.max_hold_minutes > 0 and now - pos.opened_at >= cfg.max_hold_minutes * 60:
            return ExitReason.MAX_HOLD                           # Zaman stopu: piyasa emriyle kapat
        stop_reason = _stop_reason(pos)
        if _worse(pos, price, pos.sl_price):
            # Binance'teki stop normalde kendisi çalışır; emir yoksa ya da fiyat stopu belirgin geçtiyse bot kapatır
            beyond = abs(price / pos.sl_price - 1) * 100 if pos.sl_price else 0.0
            if self.exchange.is_paper or not pos.stop_order_id or beyond >= SOFT_STOP_GRACE_PCT:
                return stop_reason
        if not pos.tp_order_id and _reached(pos, price, pos.tp_price):
            return ExitReason.TAKE_PROFIT
        return None

    def _check_breakeven(self, pos: Position, price: float, mark: Mark, now: float) -> None:
        """Erken başabaş kalkanı: fiyat +BREAKEVEN_TRIGGER_PCT'ye gelince stop girişin +BREAKEVEN_FLOOR_PCT'sine."""
        cfg, ex = self.cfg, self.exchange
        if not cfg.breakeven_enabled or pos.breakeven_active or now < self._be_retry_at.get(pos.symbol, 0.0):
            return
        sign = _sign(pos)
        if not _reached(pos, price, pos.entry_price * (1 + sign * cfg.breakeven_trigger_pct / 100)):
            return
        new_stop = ex.price_to_precision(pos.symbol, pos.entry_price * (1 + sign * cfg.breakeven_floor_pct / 100))
        if _worse(pos, price, new_stop):
            self.close_position(pos, ExitReason.BREAKEVEN)
            return
        if not _worse(pos, pos.sl_price, new_stop):      # İzleyen stop zaten daha iyi bir yerde
            pos.breakeven_active = True
            self.store.save(self.state)
            return
        if not self._move_stop(pos, new_stop, now, "başabaş"):
            return
        pos.breakeven_active = True
        self.store.save(self.state)
        self.notifier.notify(Messages.futures_breakeven(pos, price, new_stop, getattr(mark, "roe", 0.0)), "update")

    def _check_trailing(self, pos: Position, price: float, mark: Mark, now: float) -> None:
        """İzleyen stop: en iyi fiyat girişten +FUTURES_TRAILING_ACT_PCT gidince zarar kes, o tepenin
        FUTURES_TRAILING_GAP_PCT gerisine çekilir ve yalnızca lehte ilerler. Bot her kontrolde tepeyi izler ve
        seviye geçilirse hemen kapatır; Binance'teki stop emri de (en sık TRAIL_MOVE_SEC'de bir) taşınır."""
        cfg, ex = self.cfg, self.exchange
        if pos.peak_price <= 0 or _reached(pos, price, pos.peak_price):
            pos.peak_price = price
        if not cfg.futures_trailing:
            return
        sign = _sign(pos)
        if not _reached(pos, pos.peak_price, pos.entry_price * (1 + sign * cfg.futures_trailing_act_pct / 100)):
            return
        level = ex.price_to_precision(pos.symbol, pos.peak_price * (1 - sign * cfg.futures_trailing_gap_pct / 100))
        if not _worse(pos, pos.sl_price, level) or level == pos.sl_price:
            return                                       # Mevcut stop (başabaş / önceki izleyen) daha iyi
        if _worse(pos, price, level):                    # Tepeden mesafe kadar geri döndü
            log.info("%s izleyen stop: fiyat %.8g, seviye %.8g (tepe %.8g)", pos.symbol, price, level, pos.peak_price)
            self.close_position(pos, ExitReason.TRAILING_STOP)
            return
        first = not pos.trailing_active
        step = abs(level / pos.sl_price - 1) * 100 if pos.sl_price else 100.0
        if not first and (step < TRAIL_MIN_STEP_PCT or now < self._trail_at.get(pos.symbol, 0.0)):
            return
        self._trail_at[pos.symbol] = now + TRAIL_MOVE_SEC
        if not self._move_stop(pos, level, now, "izleyen stop"):
            return
        pos.trailing_active = True
        self.store.save(self.state)
        if first:
            self.notifier.notify(Messages.futures_trailing(pos, price, level, getattr(mark, "roe", 0.0), cfg),
                                 "update")

    def _move_stop(self, pos: Position, new_stop: float, now: float, what: str) -> bool:
        """Binance'teki STOP_MARKET emrini yeni seviyeye taşır (iptal + yeni emir). Olmazsa eski stopu geri koyar ve
        False döner (bir süre sonra tekrar denenir); eski de konamazsa pozisyon güvenlik için kapatılır."""
        ex = self.exchange
        old_id, old_stop = pos.stop_order_id, pos.sl_price
        try:
            if old_id:
                ex.cancel(pos.symbol, old_id, conditional=True)
            pos.stop_order_id = ex.place_stop(pos.symbol, pos.side, new_stop)
        except FuturesError as exc:
            self._be_retry_at[pos.symbol] = now + BE_RETRY_SEC
            self._trail_at[pos.symbol] = now + BE_RETRY_SEC
            log.warning("%s %s Binance'e taşınamadı: %s", pos.symbol, what, exc)
            try:                                                    # Eski stopu geri koy: korumasız kalmasın
                pos.stop_order_id = ex.place_stop(pos.symbol, pos.side, old_stop)
            except FuturesError as exc2:
                self.notifier.send(Messages.error(f"{pos.symbol} stop emri yeniden konamadı ({exc2}); pozisyon "
                                                  f"güvenlik için kapatılıyor."), critical=True)
                self.close_position(pos, ExitReason.SAFETY)
                return False
            self.notifier.send_error(f"stop-{pos.symbol}", Messages.warning(
                f"{pos.symbol} {what} Binance'e taşınamadı ({exc}); eski zarar kes duruyor, tekrar denenecek."),
                cooldown_sec=300)
            self.store.save(self.state)
            return False
        pos.sl_price = new_stop
        return True

    # ================================================================ kapanış
    def close_position(self, pos: Position, reason: ExitReason) -> bool:
        """Piyasa emriyle (reduceOnly) kapatır, kalan stop / hedef emirlerini iptal eder."""
        if not pos.futures:
            return super().close_position(pos, reason)
        ex = self.exchange
        close_side = "sell" if pos.side == "long" else "buy"
        try:
            fill = ex.market_order(pos.symbol, close_side, pos.qty, reduce_only=True)
        except FuturesError as exc:
            if not ex.is_paper:
                try:
                    gone = ex.position(pos.symbol) is None
                except Exception:  # noqa: BLE001
                    gone = False
                if gone:                                        # Bu arada stop / hedef çalışmış
                    self._closed_on_exchange(pos)
                    return True
            self.notifier.send_error(f"close-{pos.symbol}", Messages.error(
                f"{pos.symbol} kapatılamadı: {describe_error(exc)} — bir sonraki kontrolde tekrar denenecek "
                f"(Binance'teki zarar kes emri bekliyor)."), cooldown_sec=120)
            return False
        funding = 0.0
        if not ex.is_paper:
            ex.cancel_all(pos.symbol)
            funding = ex.funding_paid(pos.symbol, int(pos.opened_at * 1000))
        self._finalize_futures(pos, fill.price, fill.fee, reason, funding)
        return True

    def _paper_fill(self, pos: Position, order: dict, price: float) -> None:
        """Test modu: fiyat sanal stop / hedef emrine ulaştı."""
        cfg = self.cfg
        slip = cfg.futures_slippage_pct / 100
        if order["kind"] == "tp":
            limit = order.get("limit", True)
            exit_price = order["price"] if limit else price * (1 - _sign(pos) * slip)
            fee = pos.qty * exit_price * (cfg.futures_maker_fee if limit else cfg.futures_taker_fee)
            reason = ExitReason.TAKE_PROFIT
        else:
            exit_price = price * (1 - _sign(pos) * slip)              # STOP_MARKET: tetiklenince piyasa fiyatından
            fee = pos.qty * exit_price * cfg.futures_taker_fee
            reason = _stop_reason(pos)
        self._finalize_futures(pos, exit_price, fee, reason)

    def _closed_on_exchange(self, pos: Position) -> None:
        """Pozisyon Binance'te kapanmış (stop / hedef / likidasyon / elle): kapanışı bul ve kaydet."""
        ex = self.exchange
        since = int(pos.opened_at * 1000)
        fill = ex.closing_fill(pos.symbol, since, pos.side)
        stop_reason = _stop_reason(pos)
        if pos.tp_order_id and ex.order_filled(pos.symbol, pos.tp_order_id) is not None:
            reason = ExitReason.TAKE_PROFIT
        elif pos.stop_order_id and ex.order_filled(pos.symbol, pos.stop_order_id, conditional=True) is not None:
            reason = stop_reason
        elif fill is not None and pos.liq_price and not _worse(pos, pos.liq_price, fill.price):
            reason = ExitReason.LIQUIDATION                     # Likidasyon fiyatında / ötesinde kapanmış
        elif fill is not None and pos.sl_price and (_worse(pos, fill.price, pos.sl_price)
                                                    or abs(fill.price / pos.sl_price - 1) * 100 <= 0.3):
            reason = stop_reason                                # Stop fiyatında / ötesinde kapanmış
        elif fill is not None and pos.tp_price and _reached(pos, fill.price, pos.tp_price * (1 - _sign(pos) * 0.001)):
            reason = ExitReason.TAKE_PROFIT
        else:
            reason = ExitReason.EXTERNAL
        estimated = fill is None
        if fill is None:
            price = self.marks[pos.symbol].bid if pos.symbol in self.marks else pos.sl_price or pos.entry_price
            fee = pos.qty * price * self.cfg.futures_taker_fee
        else:
            price, fee = fill.price, fill.fee
        ex.cancel_all(pos.symbol)
        funding = ex.funding_paid(pos.symbol, since)
        if reason == ExitReason.LIQUIDATION:
            self.notifier.send(Messages.error(f"{pos.symbol} {futures_tag(pos)} LİKİDE EDİLDİ: yalıtılmış teminat "
                                              f"({fmt_usdt(pos.margin_usdt)} USDT) kaybedildi."), critical=True)
        self._finalize_futures(pos, price, fee, reason, funding, estimated)

    def _finalize_futures(self, pos: Position, exit_price: float, exit_fee: float, reason: ExitReason,
                          funding: float = 0.0, estimated: bool = False) -> None:
        now = self.time_fn()
        gross = _sign(pos) * pos.qty * (exit_price - pos.entry_price)
        pnl = gross - pos.buy_fee_usdt - exit_fee - funding
        if reason == ExitReason.LIQUIDATION:
            pnl = min(pnl, -pos.margin_usdt - pos.buy_fee_usdt)
        proceeds = pos.cost_usdt + pnl
        if self.exchange.is_paper:
            self.exchange.close_paper(pos.symbol, exit_price, pos.qty, exit_fee)
        state = self.state
        state.realized_pnl += pnl
        state.trades += 1
        if pnl >= 0:
            state.wins += 1
        else:
            state.losses += 1
        state.total_fees += exit_fee
        state.positions.pop(pos.symbol, None)
        state.cooldowns[pos.symbol] = now + cooldown_minutes(self.cfg, pnl) * 60
        self.marks.pop(pos.symbol, None)
        self._be_retry_at.pop(pos.symbol, None)
        self._trail_at.pop(pos.symbol, None)
        self.store.save(state)
        try:
            logged = self.trade_log.append(pos, exit_price, proceeds, pos.buy_fee_usdt + exit_fee, reason.value, now)
        except Exception as exc:  # noqa: BLE001 - kayıt hatası bildirimi / kill-switch'i engellemesin
            log.error("İşlem kaydı yazılamadı: %s", exc)
            logged = False
        if not logged:
            self.notifier.send_error("tradelog-locked", Messages.warning(
                f"İşlem kaydı dosyası ({self.trade_log.path.name}) yazılamadı — Excel'de açık olabilir."),
                cooldown_sec=3600)
        roe = pnl / pos.margin_usdt * 100 if pos.margin_usdt else 0.0
        log.info("Vadeli pozisyon kapandı: %s %s | %s | çıkış %.8g | net %.4f USDT (ROE %.2f%%)", pos.symbol,
                 futures_tag(pos), reason.value, exit_price, pnl, roe)
        self.notifier.notify(Messages.futures_close(
            reason.value, pos, exit_price, exit_fee, funding, pnl, roe, now - pos.opened_at, state.capital,
            state.realized_pnl, state.trades, state.wins, state.losses, self.cfg, estimated),
            "close", critical=reason.value in CRITICAL_EXITS)
        self._after_close(pos.symbol, pnl, reason, now)

    # ================================================================ mutabakat
    def _reconcile_positions_on_startup(self) -> None:
        """Kayıtlı pozisyonları Binance ile karşılaştırır; bot kapalıyken kapanmışsa kaydeder, açıksa korumayı
        (stop + hedef) yeniden kurar. Botun bilmediği vadeli pozisyonlar için uyarır (dokunmaz)."""
        ex = self.exchange
        for pos in list(self.state.positions.values()):
            if not pos.futures:
                self.notifier.send(Messages.warning(f"{pos.symbol} spot pozisyonu vadeli modda yönetilmez."))
                continue
            if ex.is_paper:
                ex.open_paper(pos.symbol, pos.side, pos.qty, pos.entry_price, pos.margin_usdt, pos.leverage)
            elif ex.position(pos.symbol) is None:
                self._closed_on_exchange(pos)
                continue
            else:
                ex.cancel_all(pos.symbol)
            try:
                pos.stop_order_id = ex.place_stop(pos.symbol, pos.side, pos.sl_price)
            except FuturesError as exc:
                self.notifier.send(Messages.error(f"{pos.symbol} zarar kes emri yeniden konamadı ({exc}); pozisyon "
                                                  f"güvenlik için kapatılıyor."), critical=True)
                self.close_position(pos, ExitReason.SAFETY)
                continue
            try:
                pos.tp_order_id = ex.place_take_profit(pos.symbol, pos.side, pos.qty, pos.tp_price,
                                                       limit=self.cfg.futures_tp_order == "limit")
            except FuturesError as exc:
                pos.tp_order_id = ""
                log.warning("%s kâr al emri yeniden konamadı: %s", pos.symbol, exc)
            self.notifier.send(f"♻️ {esc(pos.symbol)} {futures_tag(pos)} pozisyonu takibe devam ediliyor "
                               f"(giriş {fmt_price(pos.entry_price)}, stop {fmt_price(pos.sl_price)}).")
        if not ex.is_paper:
            try:
                others = [p for p in ex.all_positions() if p.symbol not in self.state.positions]
            except FuturesError as exc:
                log.warning("Vadeli pozisyonlar okunamadı: %s", exc)
                others = []
            if others:
                self.notifier.send(Messages.warning(
                    "Binance vadeli hesabında botun açmadığı pozisyon(lar) var; bot bunlara dokunmaz ama serbest "
                    "bakiye azalır: " + ", ".join(f"{p.symbol} {p.side.upper()} {p.qty:g}" for p in others)))
        self.store.save(self.state)

    def unreconciled_reason(self) -> Optional[str]:
        return None

    def unallocated_balance(self) -> Optional[float]:
        if self.exchange.is_paper:
            return None
        free = self.exchange.free_balance()
        return free - max(0.0, self.state.capital - self.state.committed)

    # ================================================================ bilgi
    def scan_report(self) -> str:
        r = self.last_scan
        if r is None:
            return "🔍 Henüz vadeli tarama yapılmadı (5 dk mum kapanışlarında, pozisyon yokken taranır)."
        age = self.time_fn() - self.last_scan_at
        lines = [f"🔍 <b>Son Vadeli Tarama</b> ({age:.0f} sn önce)",
                 f"Coin: {r.candidates} | Analiz: {r.analyzed} | Sinyal: {len(r.signals)}"]
        if self.state.positions:
            lines.append("📦 Açık pozisyon var: yeni tarama pozisyon kapanınca yapılır.")
        if self.state.paused:
            lines.append("⏸️ Yeni işlemler durdurulmuş (/devam).")
        for s in self.last_signals[:3]:
            lines.append(f"• {esc(s.symbol)} {s.side.upper()}: {esc(s.note())}")
        if r.rejects:
            lines.append("Elenme nedenleri: " + ", ".join(f"{esc(k)}: {v}" for k, v in r.rejects.most_common(5)))
        d = self.day_stats
        if d.scans:
            lines.append(f"\n📅 Bugün: {d.scans} tarama, {d.signals} sinyal, {d.buys} işlem")
        return "\n".join(lines)
