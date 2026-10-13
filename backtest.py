"""
Geriye dönük test (backtest): stratejiyi geçmiş 5 dakikalık mumlarla dener.

Canlı botla AYNI karar fonksiyonları kullanılır:
  - Giriş sinyali : strategy.ReboundStrategy.analyze
  - Boyutlandırma : risk.position_slots + risk.plan_entry (son slot kalan sermayenin tamamını kullanır)
  - Çıkışlar      : risk.evaluate_exit / update_trailing / update_breakeven / price_for_pnl / net_exit_value
  - Kill-switch   : risk.kill_switch_hit

Akış:
  1. Semboller: güncel 24 saatlik hacme göre en hacimli USDT pariteleri (+ BTC filtresi için BTCUSDT).
  2. Veri: 5 dk mumlar Binance'ten sayfalanarak indirilir ve data/backtest_cache/ altına kaydedilir;
     aynı dönem tekrar test edilirken Binance'e istek atılmaz.
  3. Simülasyon: tüm semboller ortak zaman çizelgesinde, her 5 dk mum kapanışında
     pozisyon takibi → kill-switch → yeni giriş taraması.

Örnek:
    from backtest import run_backtest, format_backtest_report
    result = run_backtest(cfg, BinanceClient(), days=30)
    print(format_backtest_report(result, cfg))
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from binance_client import RateLimitError
from config import DATA_DIR
from exchange import SymbolRules, parse_symbol_rules, to_dec
from risk import (ExitReason, Position, breakeven_effective, evaluate_exit, kill_switch_hit, net_exit_value,
                  plan_entry, pnl_pct, position_slots, price_for_pnl, trailing_floor_pct, update_breakeven,
                  update_trailing)
from strategy import KLINE_LIMIT, ReboundStrategy
from telegram_bot import LINE, fmt_duration, fmt_price, fmt_signed, fmt_usdt

INTERVAL = "5m"
INTERVAL_MS = 300_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
BARS_PER_DAY = DAY_MS // INTERVAL_MS   # 288
BARS_24H = BARS_PER_DAY                # Ön eleme için 24 saatlik kayan pencere
BTC_BARS_1H = 12                       # BTC filtresi: son 12 kapanmış mum (1 saat)
WARMUP_DAYS = 1.5                      # 24s pencere + 99 mumluk analiz için ısınma süresi
PAGE_LIMIT = 1000                      # Binance klines tek istekte en fazla 1000 mum
REQUEST_PAUSE_SEC = 0.1                # İstekler arası bekleme (istek limitine saygı)
RATE_LIMIT_RETRIES = 3
CACHE_VERSION = 1
END_REASON = "BACKTEST_END"            # Dönem sonunda hâlâ açık olan pozisyonlar

LEVERAGED_SUFFIXES = ("DOWN", "BULL", "BEAR")

REASON_LABELS = {
    ExitReason.TAKE_PROFIT.value: "🎯 Kâr Al",
    ExitReason.STOP_LOSS.value: "🛑 Zarar Kes",
    ExitReason.TRAILING_STOP.value: "🛡️ İzleyen Stop",
    ExitReason.BREAKEVEN.value: "🔒 Başabaş",
    ExitReason.MAX_HOLD.value: "⏱️ Süre Doldu",
    ExitReason.KILL_SWITCH.value: "☠️ Kill-switch",
    END_REASON: "🏁 Dönem Sonu",
}

Progress = Optional[Callable[[str], None]]


class BacktestError(Exception):
    """Backtest başlatılamadı (ör. hiç mum verisi indirilemedi)."""


# ================================================================ sonuç yapıları
@dataclass
class BacktestTrade:
    symbol: str
    open_time_ms: int     # Girişin yapıldığı mum kapanışı
    close_time_ms: int    # Çıkışın yapıldığı mumun kapanışı
    entry_price: float    # Spread eklenmiş alış fiyatı (ask)
    exit_price: float     # Spread düşülmüş satış fiyatı (bid)
    cost: float           # Alışta harcanan USDT
    proceeds: float       # Satış komisyonu düşülmüş net gelir
    pnl: float
    pnl_pct: float
    reason: str           # ExitReason değeri veya BACKTEST_END
    score: float          # Giriş sinyalinin puanı

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self.close_time_ms - self.open_time_ms) / 1000)


@dataclass
class BacktestResult:
    trades: List[BacktestTrade]
    start_capital: float
    end_capital: float
    realized_pnl: float
    total_fees: float
    max_drawdown_usdt: float
    max_drawdown_pct: float
    killed: bool
    killed_at_ms: Optional[int]
    symbols_used: List[str]
    days: float
    notes: List[str]
    equity_curve: List[Tuple[int, float]]
    # Rapor için ek bilgiler
    start_ms: int = 0                    # Simülasyonun başladığı an
    end_ms: int = 0                      # Verinin bittiği an (saate yuvarlanmış)
    spread_pct: float = 0.0
    stats: Dict[str, int] = field(default_factory=dict)  # analiz/sinyal/BTC engeli sayıları

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl >= 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl < 0)


# ================================================================ sembol seçimi
def is_tradable_asset(rules: SymbolRules, cfg) -> bool:
    """Canlı ön elemedeki varlık filtresi: stabil/fiat ve kaldıraçlı tokenler hariç."""
    return (rules.base_asset not in cfg.excluded_assets
            and not rules.base_asset.endswith(LEVERAGED_SUFFIXES))


def select_symbols(cfg, tickers: Iterable[dict], rules: Dict[str, SymbolRules],
                   max_symbols: int) -> List[str]:
    """Güncel 24s quoteVolume'a göre en hacimli `max_symbols` parite (büyükten küçüğe)."""
    scored: List[Tuple[float, str]] = []
    for t in tickers:
        symbol = t.get("symbol", "")
        r = rules.get(symbol)
        if r is None or not is_tradable_asset(r, cfg):
            continue
        try:
            quote_vol = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        scored.append((quote_vol, symbol))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:max(0, max_symbols)]]


def typical_min_notional(rules: Dict[str, SymbolRules]) -> float:
    """Paritelerde en sık görülen minimum emir tutarı (canlı botla aynı mantık)."""
    values = [float(r.min_notional) for r in rules.values() if r.min_notional > 0]
    return Counter(values).most_common(1)[0][0] if values else 5.0


# ================================================================ veri indirme + önbellek
def _utc_tag(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H")


def cache_path(cache_dir: Union[str, Path], symbol: str, start_ms: int, end_ms: int,
               interval: str = INTERVAL) -> Path:
    """Önbellek dosyası: sembol + aralık + (saate yuvarlanmış) başlangıç/bitiş."""
    return Path(cache_dir) / f"{symbol}_{interval}_{_utc_tag(start_ms)}_{_utc_tag(end_ms)}.json"


def _compact(row: Sequence) -> list:
    """Ham Binance mumu → [açılış_zamanı, o, h, l, c, quote_hacim, taker_alış_quote]."""
    return [int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
            float(row[7]), float(row[10])]


def _read_cache(path: Path, symbol: str) -> Optional[List[list]]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("v") != CACHE_VERSION or data.get("symbol") != symbol:
            return None
        rows = data["rows"]
        return [[int(r[0])] + [float(x) for x in r[1:7]] for r in rows]
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def _write_cache(path: Path, symbol: str, start_ms: int, end_ms: int, rows: List[list]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = {"v": CACHE_VERSION, "symbol": symbol, "interval": INTERVAL,
                   "start": start_ms, "end": end_ms, "rows": rows}
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass  # Önbellek yazılamazsa test yine de çalışsın


def _request_klines(client, symbol: str, start_ms: int, end_ms: int, progress: Progress) -> list:
    params = {"symbol": symbol, "interval": INTERVAL, "startTime": start_ms,
              "endTime": end_ms - 1, "limit": PAGE_LIMIT}
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return client._request("GET", "/api/v3/klines", params) or []
        except RateLimitError as exc:
            if attempt >= RATE_LIMIT_RETRIES:
                raise
            wait = min(max(exc.retry_after, 1), 120)
            if progress:
                progress(f"   ⏳ Binance istek limiti: {wait} sn bekleniyor...")
            time.sleep(wait)
    return []


def load_klines(client, symbol: str, start_ms: int, end_ms: int,
                cache_dir: Optional[Union[str, Path]] = None,
                progress: Progress = None) -> Tuple[List[list], bool]:
    """[start_ms, end_ms) aralığındaki 5 dk mumlar (sıkıştırılmış biçimde) ve önbellekten mi geldiği.

    Satır biçimi: [açılış_zamanı_ms, open, high, low, close, quote_hacim, taker_alış_quote]
    """
    path = cache_path(cache_dir, symbol, start_ms, end_ms) if cache_dir else None
    if path is not None and path.exists():
        rows = _read_cache(path, symbol)
        if rows is not None:
            return rows, True

    by_time: Dict[int, list] = {}
    cursor = start_ms
    while cursor < end_ms:
        batch = _request_klines(client, symbol, cursor, end_ms, progress)
        if REQUEST_PAUSE_SEC > 0:
            time.sleep(REQUEST_PAUSE_SEC)  # Her istekten sonra (semboller arası da) kısa bekleme
        if not batch:
            break
        for raw in batch:
            row = _compact(raw)
            if start_ms <= row[0] < end_ms:
                by_time[row[0]] = row
        next_cursor = int(batch[-1][0]) + INTERVAL_MS
        if len(batch) < PAGE_LIMIT or next_cursor <= cursor:
            break
        cursor = next_cursor

    rows = [by_time[t] for t in sorted(by_time)]
    if path is not None and rows:
        _write_cache(path, symbol, start_ms, end_ms, rows)
    return rows, False


# ================================================================ hizalanmış seri
class _Series:
    """Bir sembolün ortak 5 dk zaman çizelgesine hizalanmış mumları.

    İlk ve son gerçek mum arasındaki boşluklar (işlem durması) hacimsiz düz mumla doldurulur.
    """

    def __init__(self, symbol: str, rows: Sequence[Sequence[float]], t0: int, n: int,
                 rules: Optional[SymbolRules] = None):
        self.symbol = symbol
        self.rules = rules
        self.t0 = t0
        self.n = n
        o = [0.0] * n
        h = [0.0] * n
        lo = [0.0] * n
        c = [0.0] * n
        qv = [0.0] * n
        tbq = [0.0] * n
        real = [False] * n
        for r in rows:
            k = (int(r[0]) - t0) // INTERVAL_MS
            if 0 <= k < n:
                o[k], h[k], lo[k], c[k], qv[k], tbq[k] = r[1], r[2], r[3], r[4], r[5], r[6]
                real[k] = True
        self.first = next((k for k in range(n) if real[k]), -1)
        self.last = next((k for k in range(n - 1, -1, -1) if real[k]), -1)
        if self.first >= 0:
            prev = c[self.first]
            for k in range(self.first, self.last + 1):
                if real[k]:
                    prev = c[k]
                else:
                    o[k] = h[k] = lo[k] = c[k] = prev
        self.o, self.h, self.l, self.c, self.qv, self.tbq = o, h, lo, c, qv, tbq
        self.change24: List[Optional[float]] = [None] * n  # Ön elemeyi geçen mumlarda 24s değişim %
        self._raw: Dict[int, list] = {}

    def has(self, k: int) -> bool:
        return self.first >= 0 and self.first <= k <= self.last

    def last_index(self, k: int) -> int:
        """k'de veri yoksa en son mevcut mum."""
        return min(k, self.last)

    def prepare_prefilter(self, cfg) -> None:
        """Canlı `prefilter`'ın fiyata/hacme bağlı kısmını her mum için önceden hesaplar.

        24s quote hacmi PREFIX SUM, 24s en yüksek/en düşük kayan pencere (monoton kuyruk) ile bulunur.
        Toz kontrolü hedef pozisyona bağlı olduğundan simülasyon sırasında yapılır.
        """
        if self.first < 0 or self.rules is None:
            return
        o, h, lo, c = self.o, self.h, self.l, self.c
        prefix = [0.0] * (self.n + 1)
        for k in range(self.n):
            prefix[k + 1] = prefix[k] + self.qv[k]
        tick = float(self.rules.tick_size)
        min_vol, min_range = cfg.min_quote_volume_24h, cfg.min_24h_range_pct
        lo_change, hi_change = -cfg.max_24h_drop_pct, -cfg.min_24h_drop_pct
        dq_hi: deque = deque()
        dq_lo: deque = deque()
        for k in range(self.first, self.last + 1):
            while dq_hi and h[dq_hi[-1]] <= h[k]:
                dq_hi.pop()
            dq_hi.append(k)
            while dq_lo and lo[dq_lo[-1]] >= lo[k]:
                dq_lo.pop()
            dq_lo.append(k)
            j = k - BARS_24H + 1  # 24s penceresinin ilk mumu
            if j < self.first:
                continue
            while dq_hi[0] < j:
                dq_hi.popleft()
            while dq_lo[0] < j:
                dq_lo.popleft()
            last, open24 = c[k], o[j]
            high24, low24 = h[dq_hi[0]], lo[dq_lo[0]]
            if last <= 0 or low24 <= 0 or open24 <= 0:
                continue
            if prefix[k + 1] - prefix[j] < min_vol:
                continue
            change = (last - open24) / open24 * 100
            if not lo_change <= change <= hi_change:
                continue
            if (high24 - low24) / low24 * 100 < min_range:
                continue
            if tick > 0 and tick / last > 0.002:  # Fiyat adımı çok kaba
                continue
            self.change24[k] = change

    def _row(self, j: int) -> list:
        row = self._raw.get(j)
        if row is None:
            t = self.t0 + j * INTERVAL_MS
            row = [t, self.o[j], self.h[j], self.l[j], self.c[j], 0.0, t + INTERVAL_MS - 1,
                   self.qv[j], 0, 0.0, self.tbq[j], 0.0]
            self._raw[j] = row
        return row

    def window(self, k: int) -> List[list]:
        """k. mum kapandığı anda canlı botun göreceği veri: son 99 kapanmış mum + sentetik açık mum."""
        rows = [self._row(j) for j in range(k - (KLINE_LIMIT - 1) + 1, k + 1)]
        t_next = self.t0 + (k + 1) * INTERVAL_MS
        p = self.c[k]
        rows.append([t_next, p, p, p, p, "0", t_next + INTERVAL_MS - 1, "0", 0, "0", "0", "0"])
        return rows


# ================================================================ simülasyon
@dataclass
class _OpenPos:
    pos: Position
    rules: SymbolRules
    sellable: float       # LOT adımına göre satılabilir miktar (kalan toz satılamaz)
    score: float
    open_ms: int
    tp_price: float       # Kâr Al'ın tetiklendiği PİYASA fiyatı (spread öncesi)
    sl_price: float       # Zarar Kes'in tetiklendiği PİYASA fiyatı


class _Simulator:
    def __init__(self, cfg, series: Dict[str, _Series], btc: Optional[_Series],
                 min_notional: float, spread_pct: float, progress: Progress):
        self.cfg = cfg
        self.series = series
        self.btc = btc
        self.min_notional = min_notional
        self.entry_factor = 1 + spread_pct / 200   # Alış ask'tan
        self.exit_factor = 1 - spread_pct / 200    # Satış bid'den
        self.progress = progress
        self.strategy = ReboundStrategy(None, cfg)

        self.cash = cfg.start_capital
        self.realized = 0.0
        self.fees = 0.0
        self.open: Dict[str, _OpenPos] = {}
        self.cooldown: Dict[str, int] = {}
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[Tuple[int, float]] = []
        self.peak_equity = cfg.start_capital
        self.max_dd_usdt = 0.0
        self.max_dd_pct = 0.0
        self.killed = False
        self.killed_at: Optional[int] = None
        self.stats: Counter = Counter()

    # ------------------------------------------------------------ yardımcılar
    def _market_level(self, target_pct: float, op: _OpenPos) -> float:
        """Net K/Z'nin `target_pct` olduğu piyasa fiyatı (satış bid = piyasa × exit_factor)."""
        bid = price_for_pnl(target_pct, op.pos.cost_usdt, op.sellable, self.cfg.fee_rate)
        return bid / self.exit_factor

    def _value_at(self, op: _OpenPos, market_price: float) -> float:
        return net_exit_value(op.sellable, market_price * self.exit_factor, self.cfg.fee_rate)

    def _pnl_at(self, op: _OpenPos, market_price: float) -> float:
        return pnl_pct(self._value_at(op, market_price), op.pos.cost_usdt)

    def _close_price(self, symbol: str, k: int) -> float:
        s = self.series[symbol]
        return s.c[s.last_index(k)]

    # ------------------------------------------------------------ ana döngü
    def run(self, t0: int, k_start: int, n: int) -> None:
        total = max(1, n - k_start)
        next_report = 0.25
        for k in range(k_start, n):
            t_close = t0 + (k + 1) * INTERVAL_MS
            self._manage(k, t_close)
            if self._check_kill_switch(k, t_close):
                break
            self._scan_and_enter(k, t_close)
            self._record_equity(k, t_close)
            done = (k - k_start + 1) / total
            if self.progress and done >= next_report and done < 1:
                self.progress(f"   ⏳ Simülasyon %{done * 100:.0f} | işlem: {len(self.trades)}")
                next_report += 0.25
        if not self.killed:
            self._close_remaining(t0, n)

    # ------------------------------------------------------------ 1) pozisyon takibi
    def _bar_exit(self, op: _OpenPos, o: float, h: float, low: float, c: float,
                  now_sec: float) -> Optional[Tuple[float, str]]:
        """KÖTÜMSER mum içi sıralama: önce düşük taraf (SL / izleyen stop tabanı), sonra TP."""
        cfg, pos = self.cfg, op.pos
        # a) Düşük taraf: en yüksek koruma seviyesi (SL / izleyen stop tabanı / başabaş tabanı)
        level, reason, level_pct = op.sl_price, ExitReason.STOP_LOSS, -cfg.stop_loss_pct
        if pos.trailing_active:
            floor = trailing_floor_pct(pos, cfg)
            if floor > level_pct:
                level, reason, level_pct = self._market_level(floor, op), ExitReason.TRAILING_STOP, floor
        if pos.breakeven_active and cfg.breakeven_floor_pct > level_pct:
            # Canlıda izleyen stop önce kontrol edilir; başabaş yalnızca tabanı daha yüksekse önce tetiklenir
            level, reason = self._market_level(cfg.breakeven_floor_pct, op), ExitReason.BREAKEVEN
        if low <= level:
            price = min(level, o)  # Açılış zaten eşiğin altındaysa açılıştan
            if reason is not ExitReason.STOP_LOSS and self._pnl_at(op, price) <= -cfg.stop_loss_pct:
                reason = ExitReason.STOP_LOSS  # Canlı evaluate_exit de SL'yi önce kontrol eder
            return price, reason.value
        # b) Yüksek taraf
        if h >= op.tp_price:
            return max(op.tp_price, o), ExitReason.TAKE_PROFIT.value
        # c) Zirve güncellemesi (düşük taraf kontrolünden SONRA); canlıdaki gibi önce izleyen stop, sonra başabaş
        update_trailing(pos, self._pnl_at(op, h), cfg)
        update_breakeven(pos, cfg)
        # d) Kapanışta canlı kurallar (yeni aktifleşen izleyen stop, MAX_HOLD)
        exit_reason = evaluate_exit(pos, self._pnl_at(op, c), now_sec, cfg)
        if exit_reason:
            return c, exit_reason.value
        return None

    def _manage(self, k: int, t_close: int) -> None:
        for symbol in list(self.open):
            s = self.series[symbol]
            if not s.has(k):
                if k > s.last:  # Veri bitti (işlem durmuş/delist): son kapanıştan kapat, slotu boşalt
                    self._close(symbol, s.c[s.last], END_REASON, s.t0 + (s.last + 1) * INTERVAL_MS)
                continue
            hit = self._bar_exit(self.open[symbol], s.o[k], s.h[k], s.l[k], s.c[k], t_close / 1000)
            if hit:
                self._close(symbol, hit[0], hit[1], t_close)

    def _close(self, symbol: str, market_price: float, reason: str, t_ms: int) -> None:
        op = self.open.pop(symbol)
        bid = market_price * self.exit_factor
        proceeds = net_exit_value(op.sellable, bid, self.cfg.fee_rate)
        fee = op.sellable * bid * self.cfg.fee_rate  # Satış komisyonu USDT'den
        cost = op.pos.cost_usdt
        pnl = proceeds - cost
        self.cash += proceeds
        self.realized += pnl
        self.fees += fee
        self.trades.append(BacktestTrade(
            symbol=symbol, open_time_ms=op.open_ms, close_time_ms=t_ms, entry_price=op.pos.entry_price,
            exit_price=bid, cost=cost, proceeds=proceeds, pnl=pnl, pnl_pct=pnl_pct(proceeds, cost),
            reason=reason, score=op.score))
        self.cooldown[symbol] = t_ms + self.cfg.symbol_cooldown_minutes * 60_000

    # ------------------------------------------------------------ 2) kill-switch
    def _check_kill_switch(self, k: int, t_close: int) -> bool:
        unrealized = sum(self._value_at(op, self._close_price(sym, k)) - op.pos.cost_usdt
                         for sym, op in self.open.items())
        if not kill_switch_hit(self.realized, unrealized, self.cfg.loss_limit(self.cfg.start_capital)):
            return False
        for symbol in list(self.open):
            self._close(symbol, self._close_price(symbol, k), ExitReason.KILL_SWITCH.value, t_close)
        self.killed = True
        self.killed_at = t_close
        self._record_equity(k, t_close)
        return True

    # ------------------------------------------------------------ 3) giriş
    def _btc_ok(self, k: int) -> bool:
        """BTC son 1 saatte (12 kapanmış mum) sert düştüyse giriş yok (canlı `market_ok` ile aynı)."""
        cfg, btc = self.cfg, self.btc
        if not cfg.btc_filter or cfg.quote_asset != "USDT" or btc is None:
            return True
        j = k - BTC_BARS_1H + 1
        if not (btc.has(k) and btc.has(j)) or btc.o[j] <= 0:
            return True  # Veri yoksa canlı bot da filtreyi atlar
        change = (btc.c[k] - btc.o[j]) / btc.o[j] * 100
        return change > -cfg.btc_max_1h_drop_pct

    def _scan_and_enter(self, k: int, t_close: int) -> None:
        cfg = self.cfg
        capital = cfg.start_capital + self.realized
        slots = position_slots(capital, self.min_notional, cfg)
        free_slots = slots - len(self.open)
        if free_slots <= 0:
            return
        if not self._btc_ok(k):
            self.stats["btc_engel"] += 1
            return

        target = capital / slots
        candidates: List[Tuple[float, str]] = []
        for symbol, s in self.series.items():
            if s.rules is None or symbol in self.open or self.cooldown.get(symbol, 0) > t_close:
                continue
            if not s.has(k):
                continue
            change = s.change24[k]
            if change is None:
                continue
            if target > 0 and s.rules.step_value(s.c[k]) / target * 100 > cfg.max_dust_pct:
                continue
            candidates.append((change, symbol))
        if not candidates:
            return
        candidates.sort()  # En çok düşen önce

        signals = []
        for _, symbol in candidates[: cfg.max_candidates]:
            self.stats["analiz"] += 1
            signal, _reason = self.strategy.analyze(symbol, self.series[symbol].window(k))
            if signal and cfg.min_signal_score > 0 and signal.score < cfg.min_signal_score:
                self.stats["puan_dusuk"] += 1  # Canlı bot da MIN_SIGNAL_SCORE altını almaz
                continue
            if signal:
                signals.append(signal)
        if not signals:
            return
        self.stats["sinyal"] += len(signals)
        signals.sort(key=lambda x: x.score, reverse=True)

        opened = 0
        for signal in signals[: free_slots + 2]:
            if opened >= free_slots:
                break
            rules = self.series[signal.symbol].rules
            committed = sum(op.pos.cost_usdt for op in self.open.values())
            remaining = capital - committed
            is_last_slot = len(self.open) + 1 >= slots
            budget = remaining if is_last_slot else min(target, remaining)  # Son slot artanı kullanır
            entry_price = signal.price * self.entry_factor
            plan = plan_entry(min(budget, self.cash), rules, entry_price, cfg)
            if not plan.ok:
                self.stats["plan_red"] += 1
                if "bakiye" in plan.reason.lower():
                    return
                continue
            if self._open(signal, rules, plan.quote_amount, entry_price, t_close):
                opened += 1

    def _open(self, signal, rules: SymbolRules, quote_amount: float, ask: float, t_close: int) -> bool:
        """PaperExchange.market_buy ile aynı hesap: komisyon alınan coinden kesilir."""
        cfg = self.cfg
        spend = min(quote_amount, self.cash)
        qty = rules.floor_market_qty(to_dec(spend) / to_dec(ask))
        if qty <= 0 or qty < rules.market_min_qty:
            return False
        qty_f = float(qty)
        notional = qty_f * ask
        if notional < float(rules.min_notional) or notional > self.cash + 1e-9:
            return False
        fee_base = qty_f * cfg.fee_rate
        net_qty = qty_f - fee_base
        sell_q = rules.floor_market_qty(net_qty)
        sellable = float(sell_q) if sell_q >= rules.market_min_qty else 0.0
        if sellable <= 0:
            return False  # Komisyon sonrası satılabilir miktar kalmıyor (toz)

        pos = Position(symbol=signal.symbol, base_asset=rules.base_asset, qty=net_qty, entry_price=ask,
                       cost_usdt=notional, opened_at=t_close / 1000, buy_fee_usdt=fee_base * ask,
                       signal_note=signal.note())
        op = _OpenPos(pos=pos, rules=rules, sellable=sellable, score=signal.score, open_ms=t_close,
                      tp_price=0.0, sl_price=0.0)
        op.tp_price = self._market_level(cfg.take_profit_pct, op)
        op.sl_price = self._market_level(-cfg.stop_loss_pct, op)
        self.open[signal.symbol] = op
        self.cash -= notional
        self.fees += fee_base * ask
        return True

    # ------------------------------------------------------------ özkaynak / dönem sonu
    def _record_equity(self, k: int, t_close: int) -> None:
        equity = self.cash + sum(self._value_at(op, self._close_price(sym, k)) for sym, op in self.open.items())
        self.equity_curve.append((t_close, equity))
        if equity > self.peak_equity:
            self.peak_equity = equity
        dd = self.peak_equity - equity
        if dd > self.max_dd_usdt:
            self.max_dd_usdt = dd
        if self.peak_equity > 0:
            self.max_dd_pct = max(self.max_dd_pct, dd / self.peak_equity * 100)

    def _close_remaining(self, t0: int, n: int) -> None:
        if not self.open:
            return
        for symbol in list(self.open):
            kk = self.series[symbol].last_index(n - 1)
            self._close(symbol, self.series[symbol].c[kk], END_REASON, t0 + (kk + 1) * INTERVAL_MS)
        self._record_equity(n - 1, t0 + n * INTERVAL_MS)


# ================================================================ ana fonksiyon
def _base_notes(spread_pct: float) -> List[str]:
    return [
        "Hayatta kalma yanlılığı: semboller BUGÜNÜN en hacimli paritelerinden seçildi; dönem içinde "
        "delist olan veya çöken coinler teste dahil değil (sonuçlar olduğundan iyi görünebilir).",
        "Çözünürlük: canlı bot ~30 sn'de bir tarar ve birkaç saniyede bir fiyat kontrol eder; backtest "
        "yalnızca 5 dk mum kapanışlarında karar verir.",
        "Kötümser mum içi sıralama: aynı mumda hem zarar-kes hem kâr-al seviyesi görüldüyse zarar-kes "
        "varsayıldı; zirve (izleyen stop) güncellemesi düşük taraf kontrolünden sonra yapıldı.",
        f"Sabit %{spread_pct:g} spread varsayıldı (alış +%{spread_pct / 2:g}, satış -%{spread_pct / 2:g}); "
        "kayma (slippage) ve emir defteri derinliği hesaba katılmadı.",
        "Spread, delist, piyasa rejimi ve pompa filtreleri; başabaş koruması, BTC çöküş çıkışı ve "
        "sinyal teyidi simüle edilmedi (ATR filtresi ve minimum sinyal puanı uygulandı).",
        "Geçmiş performans gelecekteki sonuçları garanti etmez.",
    ]


def run_backtest(cfg, client, days: float = 30, max_symbols: int = 40,
                 cache_dir: Optional[Union[str, Path]] = DATA_DIR / "backtest_cache",
                 progress: Progress = print, symbols: Optional[Sequence[str]] = None,
                 end_time_ms: Optional[int] = None, spread_pct: float = 0.05) -> BacktestResult:
    """Son `days` günü 5 dk mumlarla simüle eder. Ağ hatası exchange_info/tickers_24h'den yükselebilir."""
    say = progress or (lambda _msg: None)
    if days <= 0:
        raise ValueError("days sıfırdan büyük olmalı")
    if spread_pct < 0:
        raise ValueError("spread_pct negatif olamaz")

    rules = parse_symbol_rules(client.exchange_info(), cfg.quote_asset)
    if not rules:
        raise BacktestError(f"Hiç {cfg.quote_asset} paritesi bulunamadı (exchangeInfo boş)")
    notes = _base_notes(spread_pct)

    # --- semboller
    if symbols is None:
        trade_symbols = select_symbols(cfg, client.tickers_24h(), rules, max_symbols)
    else:
        trade_symbols, skipped = [], []
        for sym in dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip()):
            r = rules.get(sym)
            (trade_symbols if r is not None and is_tradable_asset(r, cfg) else skipped).append(sym)
        if skipped:
            notes.append(f"İşlem kuralı olmayan / filtrelenen semboller atlandı: {', '.join(skipped)}")
    if not trade_symbols:
        raise BacktestError("Test edilecek uygun sembol bulunamadı")
    btc_symbol = "BTCUSDT" if cfg.btc_filter and cfg.quote_asset == "USDT" else None

    # --- zaman aralığı (saate yuvarlanmış → önbellek anahtarı sabit kalır)
    now_ms = int(time.time() * 1000) if end_time_ms is None else int(end_time_ms)
    end_ms = now_ms // HOUR_MS * HOUR_MS
    total_hours = int(math.ceil((days + WARMUP_DAYS) * 24))
    start_ms = end_ms - total_hours * HOUR_MS
    n = (end_ms - start_ms) // INTERVAL_MS
    sim_bars = min(n, max(1, int(round(days * BARS_PER_DAY))))
    k_start = n - sim_bars

    # --- veri
    to_load = list(trade_symbols)
    if btc_symbol and btc_symbol not in to_load:
        to_load.append(btc_symbol)
    say(f"📥 Mum verileri hazırlanıyor: {len(to_load)} sembol, {total_hours / 24:g} gün ({INTERVAL})")
    data: Dict[str, List[list]] = {}
    failed: List[str] = []
    cached = 0
    for i, sym in enumerate(to_load, 1):
        try:
            rows, from_cache = load_klines(client, sym, start_ms, end_ms, cache_dir, say)
        except Exception as exc:  # noqa: BLE001 - tek sembol hatası testi durdurmasın
            say(f"   ⚠️ [{i}/{len(to_load)}] {sym} indirilemedi: {exc}")
            failed.append(sym)
            continue
        cached += from_cache
        if not from_cache:
            say(f"   [{i}/{len(to_load)}] {sym}: {len(rows)} mum indirildi")
        if rows:
            data[sym] = rows
        else:
            failed.append(sym)
    if cached:
        say(f"   💾 {cached} sembol önbellekten okundu")
    if failed:
        notes.append(f"Verisi alınamayan semboller atlandı: {', '.join(failed)}")

    series: Dict[str, _Series] = {}
    for sym in trade_symbols:
        if sym in data:
            s = _Series(sym, data[sym], start_ms, n, rules[sym])
            s.prepare_prefilter(cfg)
            series[sym] = s
    if not series:
        raise BacktestError("Hiç mum verisi indirilemedi (internet bağlantısını kontrol edin)")
    btc: Optional[_Series] = None
    if btc_symbol:
        btc = series.get(btc_symbol)
        if btc is None and btc_symbol in data:
            btc = _Series(btc_symbol, data[btc_symbol], start_ms, n)
        if btc is None:
            notes.append("BTC verisi alınamadı: BTC filtresi uygulanmadı.")

    # --- simülasyon
    say(f"⚙️ Simülasyon: {len(series)} sembol, {sim_bars} mum ({days:g} gün)")
    sim = _Simulator(cfg, series, btc, typical_min_notional(rules), spread_pct, progress)
    sim.run(start_ms, k_start, n)

    if any(t.reason == END_REASON for t in sim.trades):
        notes.append("Dönem sonunda (veya sembolün verisi bittiğinde) açık kalan pozisyonlar son kapanış "
                     "fiyatından kapatılmış sayıldı.")
    if sim.killed:
        notes.append("Kill-switch tetiklendi: sonraki sürede işlem yapılmadı.")

    return BacktestResult(
        trades=sim.trades,
        start_capital=cfg.start_capital,
        end_capital=cfg.start_capital + sim.realized,
        realized_pnl=sim.realized,
        total_fees=sim.fees,
        max_drawdown_usdt=sim.max_dd_usdt,
        max_drawdown_pct=sim.max_dd_pct,
        killed=sim.killed,
        killed_at_ms=sim.killed_at,
        symbols_used=list(series),
        days=float(days),
        notes=notes,
        equity_curve=sim.equity_curve,
        start_ms=start_ms + k_start * INTERVAL_MS,
        end_ms=end_ms,
        spread_pct=spread_pct,
        stats=dict(sim.stats),
    )


# ================================================================ rapor
def _fmt_time(ms: Optional[int]) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _trade_line(i: int, t: BacktestTrade, quote: str) -> str:
    label = REASON_LABELS.get(t.reason, t.reason)
    return (f"   {i}) {t.symbol} {fmt_signed(t.pnl, 4)} {quote} ({fmt_signed(t.pnl_pct)}%) | "
            f"{fmt_price(t.entry_price)} → {fmt_price(t.exit_price)} | {_fmt_time(t.open_time_ms)} | "
            f"{fmt_duration(t.duration_sec)} | {label}")


def format_backtest_report(result: BacktestResult, cfg) -> str:
    """Konsol için Türkçe düz metin rapor."""
    q = cfg.quote_asset
    trades = result.trades
    wins = [t for t in trades if t.pnl >= 0]
    losses = [t for t in trades if t.pnl < 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    net_pct = result.realized_pnl / result.start_capital * 100 if result.start_capital > 0 else 0.0
    avg_dur = sum(t.duration_sec for t in trades) / len(trades) if trades else 0.0
    trailing = (f"+%{cfg.trailing_activation_pct:g} → zirvenin yarısı (en az +%{cfg.trailing_floor_pct:g})"
                if cfg.trailing_dynamic else
                f"+%{cfg.trailing_activation_pct:g} → +%{cfg.trailing_floor_pct:g}")
    hold = f" | Maks. süre {cfg.max_hold_minutes} dk" if cfg.max_hold_minutes > 0 else ""
    if not cfg.breakeven_enabled:
        breakeven = "kapalı"
    elif breakeven_effective(cfg):
        breakeven = f"+%{cfg.breakeven_trigger_pct:g} → +%{cfg.breakeven_floor_pct:g}"
    else:
        breakeven = f"etkisiz (tetik +%{cfg.breakeven_trigger_pct:g}, izleyen stop / kâr al'dan küçük olmalı)"

    lines = [
        "📊 BACKTEST RAPORU (geçmiş veriyle strateji testi)",
        LINE,
        f"🗓️ Dönem: {_fmt_time(result.start_ms)} → {_fmt_time(result.end_ms)} ({result.days:g} gün, 5 dk mumlar)",
        f"🪙 Sembol sayısı: {len(result.symbols_used)}",
        f"⚙️ Kâr Al +%{cfg.take_profit_pct:g} | Zarar Kes -%{cfg.stop_loss_pct:g} | İzleyen stop {trailing}{hold}",
        f"   Başabaş: {breakeven}",
        f"   Komisyon %{cfg.fee_rate * 100:.2f} | Spread %{result.spread_pct:g} | "
        f"Kill-switch -%{cfg.loss_pct(result.start_capital):.4g} "
        f"(-{fmt_usdt(cfg.loss_limit(result.start_capital))} {q}) | "
        f"En fazla {cfg.max_open_positions} pozisyon",
        f"💼 Başlangıç sermayesi: {fmt_usdt(result.start_capital)} {q}",
        LINE,
        f"🔢 İşlem sayısı: {len(trades)} (✅ {len(wins)} kazanan / ❌ {len(losses)} kaybeden)",
        f"🎯 Kazanma oranı: %{win_rate:.1f}",
        f"💰 Net K/Z: {fmt_signed(result.realized_pnl, 4)} {q} ({fmt_signed(net_pct)}%)",
        f"🏦 Son sermaye: {fmt_usdt(result.end_capital)} {q}",
        f"📉 Maks. düşüş: {fmt_usdt(result.max_drawdown_usdt)} {q} (%{result.max_drawdown_pct:.2f})",
        f"🧾 Toplam komisyon: {fmt_usdt(result.total_fees)} {q}",
        f"⏱️ Ortalama süre: {fmt_duration(avg_dur) if trades else '-'}",
    ]
    st = result.stats or {}
    if st:
        lines.append(f"🔎 Analiz: {st.get('analiz', 0)} | Sinyal: {st.get('sinyal', 0)} | "
                     f"BTC filtresi engeli: {st.get('btc_engel', 0)} mum")

    lines.append(LINE)
    if trades:
        lines.append("🚪 Çıkış nedenleri:")
        for reason, count in Counter(t.reason for t in trades).most_common():
            subset = [t for t in trades if t.reason == reason]
            total = sum(t.pnl for t in subset)
            lines.append(f"   {REASON_LABELS.get(reason, reason)}: {count} işlem "
                         f"({fmt_signed(total, 4)} {q})")
        best = sorted(wins, key=lambda t: t.pnl, reverse=True)[:3]
        worst = sorted(losses, key=lambda t: t.pnl)[:3]
        lines.append("🏆 En iyi 3 işlem:")
        lines.extend(_trade_line(i, t, q) for i, t in enumerate(best, 1))
        if not best:
            lines.append("   (kazanan işlem yok)")
        lines.append("🥀 En kötü 3 işlem:")
        lines.extend(_trade_line(i, t, q) for i, t in enumerate(worst, 1))
        if not worst:
            lines.append("   (kaybeden işlem yok)")
    else:
        lines.append("ℹ️ Bu dönemde strateji hiç işlem açmadı.")

    lines.append(LINE)
    if result.killed:
        lines.append(f"☠️ Kill-switch: TETİKLENDİ ({_fmt_time(result.killed_at_ms)}) — "
                     f"sonrasında işlem yapılmadı")
    else:
        lines.append(f"✅ Kill-switch: tetiklenmedi (limit -%{cfg.loss_pct(result.start_capital):.4g} = "
                     f"-{fmt_usdt(cfg.loss_limit(result.start_capital))} {q})")

    if result.notes:
        lines.append(LINE)
        lines.append("📝 Notlar / sınırlamalar:")
        lines.extend(f"   • {note}" for note in result.notes)
    return "\n".join(lines)
