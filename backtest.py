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
from bisect import bisect_right
import os
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from binance_client import RateLimitError
from config import DATA_DIR
from exchange import SymbolRules, parse_symbol_rules, to_dec
from market_regime import COKUS, KLINE_1D_LIMIT, KLINE_4H_LIMIT, ZAYIF, classify
from risk import (ExitReason, Position, breakeven_effective, evaluate_exit, kill_switch_hit, net_exit_value,
                  plan_entry, pnl_pct, position_slots, price_for_pnl, trailing_floor_pct, update_breakeven,
                  update_trailing)
from strategy import KLINE_LIMIT, ReboundStrategy, Signal, required_move_pct, room_ok
from telegram_bot import LINE, fmt_duration, fmt_price, fmt_rate, fmt_signed, fmt_usdt

INTERVAL = "5m"
INTERVAL_MS = 300_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
INTERVAL_MS_BY_NAME = {"5m": INTERVAL_MS, "4h": 4 * HOUR_MS, "1d": DAY_MS}
REGIME_HISTORY_DAYS = 31               # Rejim için BTC 4s EMA50 (~9 gün) ve günlük EMA20 (20 gün) geçmişi
MIN_BREADTH_PAIRS_SIM = 20             # market_regime.MIN_BREADTH_PAIRS ile aynı
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
    # Teşhis bilgileri (rapordaki "Teşhis" bölümü ve --tara için)
    peak_pct: float = 0.0          # İşlem açıkken görülen en iyi net K/Z % (mum yükseklerinden)
    trough_pct: float = 0.0        # İşlem açıkken görülen en kötü net K/Z % (mum diplerinden)
    move_pct: float = 0.0          # Piyasa fiyatının girişten çıkışa değişimi % (komisyon ve spread öncesi)
    fee_usdt: float = 0.0          # Alış + satış komisyonu
    peak_after_sec: float = 0.0    # Girişten en iyi ana kadar geçen süre
    room_pct: Optional[float] = None  # Girişte düşüş öncesi zirveye kalan alan % (sinyal ölçümü)

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


def _write_cache(path: Path, symbol: str, start_ms: int, end_ms: int, rows: List[list],
                 interval: str = INTERVAL) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = {"v": CACHE_VERSION, "symbol": symbol, "interval": interval,
                   "start": start_ms, "end": end_ms, "rows": rows}
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass  # Önbellek yazılamazsa test yine de çalışsın


def _request_klines(client, symbol: str, start_ms: int, end_ms: int, progress: Progress,
                    interval: str = INTERVAL) -> list:
    params = {"symbol": symbol, "interval": interval, "startTime": start_ms,
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
                progress: Progress = None, interval: str = INTERVAL) -> Tuple[List[list], bool]:
    """[start_ms, end_ms) aralığındaki mumlar (varsayılan 5 dk, sıkıştırılmış biçimde) ve önbellekten mi geldiği.

    Satır biçimi: [açılış_zamanı_ms, open, high, low, close, quote_hacim, taker_alış_quote]
    """
    step_ms = INTERVAL_MS_BY_NAME[interval]
    path = cache_path(cache_dir, symbol, start_ms, end_ms, interval) if cache_dir else None
    if path is not None and path.exists():
        rows = _read_cache(path, symbol)
        if rows is not None:
            return rows, True

    by_time: Dict[int, list] = {}
    cursor = start_ms
    while cursor < end_ms:
        batch = _request_klines(client, symbol, cursor, end_ms, progress, interval)
        if REQUEST_PAUSE_SEC > 0:
            time.sleep(REQUEST_PAUSE_SEC)  # Her istekten sonra (semboller arası da) kısa bekleme
        if not batch:
            break
        for raw in batch:
            row = _compact(raw)
            if start_ms <= row[0] < end_ms:
                by_time[row[0]] = row
        next_cursor = int(batch[-1][0]) + step_ms
        if len(batch) < PAGE_LIMIT or next_cursor <= cursor:
            break
        cursor = next_cursor

    rows = [by_time[t] for t in sorted(by_time)]
    if path is not None and rows:
        _write_cache(path, symbol, start_ms, end_ms, rows, interval)
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
        # Hacim / oynaklık / fiyat adımı ön elemesini geçen mumlarda 24s değişim %. Değişim aralığı
        # (MIN/MAX_24H_DROP_PCT) simülasyonda uygulanır ki giriş taraması onu da değiştirebilsin.
        self.change24: List[Optional[float]] = [None] * n
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


# ================================================================ piyasa rejimi (canlı market_regime ile aynı)
class _RegimeSim:
    """Canlı `MarketRegime`'in backtest karşılığı: her mumda NORMAL / ZAYIF / COKUS.

    BTC 24s değişimi ve fiyatı 5 dk seriden, 4s/1g EMA'lar ayrıca indirilen BTC 4s ve 1g mumlarından
    hesaplanır. Düşen coin oranı (genişlik) canlıda tüm likit paritelerden, burada yalnızca test edilen
    sembollerden hesaplanır (yaklaşık).
    """

    def __init__(self, cfg, btc: _Series, series: Dict[str, "_Series"], rows_4h: Sequence[Sequence[float]],
                 rows_1d: Sequence[Sequence[float]]):
        self.btc = btc
        # (kapanış zamanları, kapanış fiyatları) — zamana göre sıralı
        self.k4 = ([int(r[0]) + 4 * HOUR_MS for r in rows_4h], [float(r[4]) for r in rows_4h])
        self.k1 = ([int(r[0]) + DAY_MS for r in rows_1d], [float(r[4]) for r in rows_1d])
        self.cache: Dict[int, str] = {}
        n = btc.n
        self.total = [0] * n
        self.down = [0] * n
        for s in series.values():
            if s.first < 0:
                continue
            prefix = [0.0] * (s.n + 1)
            for k in range(s.n):
                prefix[k + 1] = prefix[k] + s.qv[k]
            for k in range(max(s.first + BARS_24H - 1, 0), s.last + 1):
                j = k - BARS_24H + 1
                if s.o[j] <= 0 or prefix[k + 1] - prefix[j] < cfg.min_quote_volume_24h:
                    continue
                self.total[k] += 1
                if s.c[k] < s.o[j]:
                    self.down[k] += 1

    @staticmethod
    def _closes(rows: Tuple[List[int], List[float]], t_ms: int, limit: int, price: float) -> List[float]:
        """Canlıdaki gibi: son tamamlanmış (limit-1) mum + şu anki fiyat (oluşmakta olan mum)."""
        times, closes = rows
        i = bisect_right(times, t_ms)
        return closes[max(0, i - (limit - 1)):i] + [price]

    def at(self, k: int, t_close: int) -> str:
        regime = self.cache.get(k)
        if regime is not None:
            return regime
        btc = self.btc
        price = btc.c[k] if btc.has(k) else None
        j = k - BARS_24H + 1
        btc_24h = ((btc.c[k] - btc.o[j]) / btc.o[j] * 100
                   if price is not None and btc.has(j) and btc.o[j] > 0 else None)
        breadth = self.down[k] / self.total[k] * 100 if self.total[k] >= MIN_BREADTH_PAIRS_SIM else None
        closes_4h = self._closes(self.k4, t_close, KLINE_4H_LIMIT, price) if price else []
        closes_1d = self._closes(self.k1, t_close, KLINE_1D_LIMIT, price) if price else []
        regime = classify(price, closes_4h, closes_1d, btc_24h, breadth).regime
        self.cache[k] = regime
        return regime


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
    market_entry: float = 0.0   # Spread öncesi giriş fiyatı
    buy_fee: float = 0.0        # Alış komisyonu (USDT)
    peak: float = 0.0           # Görülen en iyi / en kötü net K/Z % (teşhis)
    trough: float = 0.0
    peak_ms: int = 0
    room: Optional[float] = None


class _Simulator:
    def __init__(self, cfg, series: Dict[str, _Series], btc: Optional[_Series],
                 min_notional: float, spread_pct: float, progress: Progress,
                 regime: Optional[_RegimeSim] = None, signal_cache: Optional[dict] = None):
        self.cfg = cfg
        self.series = series
        self.btc = btc
        self.regime = regime if cfg.regime_filter else None
        # (sembol, mum, sıkı_mod) → Signal | None. Giriş analizi çıkış ayarlarına bağlı değildir; --tara'da
        # aynı veriyle denenen tüm kombinasyonlar bu önbelleği paylaşır (analiz bir kez yapılır).
        self.signal_cache = signal_cache if signal_cache is not None else {}
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
        # Teşhis: işlemin gördüğü en iyi / en kötü an (çıkış mumu dahil; karar mantığını etkilemez)
        hi_pct, lo_pct = self._pnl_at(op, h), self._pnl_at(op, low)
        if hi_pct > op.peak:
            op.peak, op.peak_ms = hi_pct, int(now_sec * 1000)
        op.trough = min(op.trough, lo_pct)
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
        exit_pct = pnl_pct(proceeds, cost)
        self.trades.append(BacktestTrade(
            symbol=symbol, open_time_ms=op.open_ms, close_time_ms=t_ms, entry_price=op.pos.entry_price,
            exit_price=bid, cost=cost, proceeds=proceeds, pnl=pnl, pnl_pct=exit_pct,
            reason=reason, score=op.score,
            peak_pct=max(op.peak, exit_pct), trough_pct=min(op.trough, exit_pct),
            move_pct=(market_price / op.market_entry - 1) * 100 if op.market_entry > 0 else 0.0,
            fee_usdt=op.buy_fee + fee, peak_after_sec=max(0, op.peak_ms - op.open_ms) / 1000,
            room_pct=op.room))
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
        regime = self.regime.at(k, t_close) if self.regime is not None else "NORMAL"
        if regime == COKUS:                  # Canlıda da çöküş modunda yeni alım yapılmaz
            self.stats["rejim_cokus"] += 1
            return
        strict = regime == ZAYIF
        if strict:
            self.stats["rejim_zayif"] += 1
            if cfg.regime_skip_weak:         # Canlıda da zayıf piyasada hiç alım yapılmaz
                return

        target = capital / slots
        candidates: List[Tuple[float, str]] = []
        for symbol, s in self.series.items():
            if s.rules is None or symbol in self.open or self.cooldown.get(symbol, 0) > t_close:
                continue
            if not s.has(k):
                continue
            change = s.change24[k]
            if change is None or not -cfg.max_24h_drop_pct <= change <= -cfg.min_24h_drop_pct:
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
            signal = self._analyze(symbol, k, strict)
            if signal and cfg.min_signal_score > 0 and signal.score < cfg.min_signal_score:
                self.stats["puan_dusuk"] += 1  # Canlı bot da MIN_SIGNAL_SCORE altını almaz
                continue
            if signal and not room_ok(signal, cfg):
                self.stats["alan_dar"] += 1    # Canlı tarama da hedef alanı dar sinyali almaz
                continue
            if signal:
                signals.append(signal)
        if not signals:
            return
        self.stats["sinyal"] += len(signals)
        signals.sort(key=lambda x: x.score, reverse=True)
        if strict:  # Canlıda da zayıf piyasada aynı anda en fazla 1 pozisyon
            free_slots = min(free_slots, max(0, 1 - len(self.open)))
            if free_slots <= 0:
                return

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

    def _analyze(self, symbol: str, k: int, strict: bool) -> Optional[Signal]:
        key = (symbol, k, strict)
        if key in self.signal_cache:
            return self.signal_cache[key]
        signal, _reason = self.strategy.analyze(symbol, self.series[symbol].window(k), strict=strict)
        self.signal_cache[key] = signal
        return signal

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
                      tp_price=0.0, sl_price=0.0, market_entry=ask / self.entry_factor, buy_fee=fee_base * ask,
                      room=signal.metrics.get("room_pct"))
        op.tp_price = self._market_level(cfg.take_profit_pct, op)
        op.sl_price = self._market_level(-cfg.stop_loss_pct, op)
        op.peak = op.trough = self._pnl_at(op, op.market_entry)   # Girişte: -(komisyon + spread)
        op.peak_ms = t_close
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
        "Spread, delist ve pompa filtreleri, BTC çöküş çıkışı ve sinyal teyidi simüle edilmedi. Başabaş, "
        "ATR filtresi, minimum sinyal puanı ve hedef alanı filtresi canlıdaki gibi uygulandı.",
        "Piyasa rejimi filtresi yaklaşık simüle edildi: 'düşen coin oranı' canlıda tüm likit paritelerden, "
        "burada yalnızca test edilen sembollerden hesaplanır.",
        "Geçmiş performans gelecekteki sonuçları garanti etmez.",
    ]


@dataclass
class BacktestData:
    """İndirilmiş ve hizalanmış veri: aynı veriyle farklı ayarlar (--tara) denenebilir."""
    series: Dict[str, _Series]
    btc: Optional[_Series]
    regime: Optional[_RegimeSim]
    min_notional: float
    start_ms: int          # Verinin (ısınma dahil) başladığı an
    end_ms: int
    n: int                 # Toplam mum sayısı
    k_start: int           # Simülasyonun başladığı mum
    days: float
    notes: List[str]

    @property
    def sim_start_ms(self) -> int:
        return self.start_ms + self.k_start * INTERVAL_MS


def prepare_backtest(cfg, client, days: float = 30, max_symbols: int = 40,
                     cache_dir: Optional[Union[str, Path]] = DATA_DIR / "backtest_cache",
                     progress: Progress = print, symbols: Optional[Sequence[str]] = None,
                     end_time_ms: Optional[int] = None) -> BacktestData:
    """Sembolleri seçer, mumları indirir (önbellekli) ve simülasyona hazırlar."""
    say = progress or (lambda _msg: None)
    if days <= 0:
        raise ValueError("days sıfırdan büyük olmalı")

    rules = parse_symbol_rules(client.exchange_info(), cfg.quote_asset)
    if not rules:
        raise BacktestError(f"Hiç {cfg.quote_asset} paritesi bulunamadı (exchangeInfo boş)")
    notes: List[str] = []

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
    usdt = cfg.quote_asset == "USDT"
    btc_symbol = "BTCUSDT" if (cfg.btc_filter or cfg.regime_filter) and usdt else None

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
            notes.append("BTC verisi alınamadı: BTC filtresi ve piyasa rejimi uygulanmadı.")

    # --- piyasa rejimi için BTC 4s / 1g mumları (canlıda MarketRegime ile aynı EMA'lar)
    regime: Optional[_RegimeSim] = None
    if cfg.regime_filter and btc is not None:
        hist_start = start_ms - REGIME_HISTORY_DAYS * DAY_MS
        try:
            rows_4h, _ = load_klines(client, btc_symbol, hist_start // DAY_MS * DAY_MS, end_ms, cache_dir, say,
                                     interval="4h")
            rows_1d, _ = load_klines(client, btc_symbol, hist_start // DAY_MS * DAY_MS, end_ms, cache_dir, say,
                                     interval="1d")
            regime = _RegimeSim(cfg, btc, series, rows_4h, rows_1d)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"BTC 4s/1g mumları alınamadı ({exc}); piyasa rejimi filtresi uygulanmadı.")

    return BacktestData(series=series, btc=btc, regime=regime, min_notional=typical_min_notional(rules),
                        start_ms=start_ms, end_ms=end_ms, n=n, k_start=k_start, days=float(days), notes=notes)


def simulate(cfg, data: BacktestData, spread_pct: float = 0.05, progress: Progress = None,
             signal_cache: Optional[dict] = None) -> BacktestResult:
    """Hazırlanmış veri üzerinde bir ayar setini simüle eder."""
    if spread_pct < 0:
        raise ValueError("spread_pct negatif olamaz")
    sim = _Simulator(cfg, data.series, data.btc if cfg.btc_filter else None, data.min_notional, spread_pct,
                     progress, regime=data.regime, signal_cache=signal_cache)
    sim.run(data.start_ms, data.k_start, data.n)

    notes = _base_notes(spread_pct) + list(data.notes)
    if not cfg.regime_filter:
        notes.append("Piyasa rejimi filtresi ayarlarda kapalı (REGIME_FILTER=false).")
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
        symbols_used=list(data.series),
        days=data.days,
        notes=notes,
        equity_curve=sim.equity_curve,
        start_ms=data.sim_start_ms,
        end_ms=data.end_ms,
        spread_pct=spread_pct,
        stats=dict(sim.stats),
    )


def run_backtest(cfg, client, days: float = 30, max_symbols: int = 40,
                 cache_dir: Optional[Union[str, Path]] = DATA_DIR / "backtest_cache",
                 progress: Progress = print, symbols: Optional[Sequence[str]] = None,
                 end_time_ms: Optional[int] = None, spread_pct: float = 0.05) -> BacktestResult:
    """Son `days` günü 5 dk mumlarla simüle eder. Ağ hatası exchange_info/tickers_24h'den yükselebilir."""
    if spread_pct < 0:
        raise ValueError("spread_pct negatif olamaz")
    data = prepare_backtest(cfg, client, days, max_symbols, cache_dir, progress, symbols, end_time_ms)
    say = progress or (lambda _msg: None)
    say(f"⚙️ Simülasyon: {len(data.series)} sembol, {data.n - data.k_start} mum ({days:g} gün)")
    return simulate(cfg, data, spread_pct, progress)


# ================================================================ parametre taraması (--tara / --tara-giris)
# Çıkış taraması: aynı veriyle denenecek çıkış ayarları. İzleyen stop tabanı = başlangıcın yarısı.
SWEEP_TP = (2.0, 3.0, 5.0)
SWEEP_SL = (2.0, 3.5)
SWEEP_TRAIL = (1.0, 2.0)
SWEEP_HOLD = (60, 120, 240)
SWEEP_ROOM = (0.0, 1.0)
# Giriş taraması: çıkışlar .env'deki gibi kalır, hangi sinyallerin alınacağı değişir
SWEEP_MAX_DROP = (15.0, 8.0)          # + .env'deki MAX_24H_DROP_PCT
SWEEP_ENTRY_ROOM = (0.0, 0.5, 1.0)
SWEEP_SCORE_QUANTILES = (0.5, 0.75)   # Sinyal puanı eşikleri: verideki sinyallerin ortanca ve üst çeyrek puanı
SWEEP_KILL_PCT = 99.0      # Taramada kill-switch fiilen kapalı: her ayar tüm dönem boyunca karşılaştırılır
SWEEP_SHOW = 12

# .env anahtarı → (Config alanı, tür, tablo başlığı; None = tabloda gösterilmez)
SWEEP_FIELDS: Dict[str, Tuple[str, type, Optional[str]]] = {
    "TAKE_PROFIT_PCT": ("take_profit_pct", float, "TP"),
    "STOP_LOSS_PCT": ("stop_loss_pct", float, "SL"),
    "TRAILING_ACTIVATION_PCT": ("trailing_activation_pct", float, "İz."),
    "TRAILING_FLOOR_PCT": ("trailing_floor_pct", float, None),
    "MAX_HOLD_MINUTES": ("max_hold_minutes", int, "Süre"),
    "REGIME_SKIP_WEAK": ("regime_skip_weak", bool, "Zayıf"),
    "MAX_24H_DROP_PCT": ("max_24h_drop_pct", float, "MaxDüş"),
    "MIN_SIGNAL_SCORE": ("min_signal_score", float, "Puan"),
    "MIN_ROOM_RATIO": ("min_room_ratio", float, "Alan"),
}
EXIT_KEYS = ("TAKE_PROFIT_PCT", "STOP_LOSS_PCT", "TRAILING_ACTIVATION_PCT", "TRAILING_FLOOR_PCT",
             "MAX_HOLD_MINUTES", "MIN_ROOM_RATIO")
ENTRY_KEYS = ("REGIME_SKIP_WEAK", "MAX_24H_DROP_PCT", "MIN_SIGNAL_SCORE", "MIN_ROOM_RATIO")

Settings = Dict[str, Union[float, int, bool]]
Combo = Tuple[Settings, bool]          # (ayarlar, .env'deki mevcut ayar mı)


@dataclass
class SweepRow:
    settings: Settings      # .env anahtarı → değer
    baseline: bool          # .env'deki mevcut ayarlar
    trades: int
    win_rate: float
    net: float
    mean_net_pct: float
    max_dd_pct: float
    first_net: float
    second_net: float
    profit_factor: Optional[float]
    se_net_pct: float = 0.0  # mean_net_pct'nin standart hatası

    @property
    def consistent(self) -> bool:
        """Dönemin iki yarısında da kârlı mı? (tek bir şanslı dönemin sonucu olmadığına dair işaret)"""
        return self.first_net > 0 and self.second_net > 0

    @property
    def enough_trades(self) -> bool:
        return self.trades >= MIN_TRADES_FOR_EVIDENCE

    def proven(self, tested: int) -> bool:
        """Aday sayılır mı? Yeterli işlem + iki yarıda kâr + çoklu denemeye göre düzeltilmiş anlamlılık.

        Çok sayıda ayar denendiğinde (örn. 54) birkaçı tamamen şans eseri "iki yarıda da kârlı" çıkar
        (rastgele fiyat verisiyle bile). Bu yüzden işlem başı sonucun alt güven sınırı, denenen ayar
        sayısına göre sıkılaştırılmış (Bonferroni) eşikle sıfırın üstünde olmalı.
        """
        if not (self.enough_trades and self.net > 0 and self.consistent and self.se_net_pct > 0):
            return False
        return self.mean_net_pct - sweep_z(tested) * self.se_net_pct > 0


def sweep_z(tested: int) -> float:
    """Denenen ayar sayısına göre (Bonferroni, iki yönlü %5) gereken z değeri: 1 ayar → 1.96, 60 → ~3.1."""
    from statistics import NormalDist
    return NormalDist().inv_cdf(1 - 0.05 / (2 * max(1, tested)))


def _current(cfg, keys: Sequence[str]) -> Settings:
    return {k: getattr(cfg, SWEEP_FIELDS[k][0]) for k in keys}


def _apply(cfg, settings: Settings):
    """Ayarları Config'e uygular; taramada kill-switch kapalıdır (eski USDT ayarı dahil)."""
    changes = {SWEEP_FIELDS[k][0]: SWEEP_FIELDS[k][1](v) for k, v in settings.items()}
    return replace(cfg, max_total_loss_pct=SWEEP_KILL_PCT, legacy_loss_usdt=None, **changes)


def sweep_combos(cfg, tps: Sequence[float] = SWEEP_TP, sls: Sequence[float] = SWEEP_SL,
                 trails: Sequence[float] = SWEEP_TRAIL, holds: Sequence[int] = SWEEP_HOLD,
                 rooms: Sequence[float] = SWEEP_ROOM) -> List[Combo]:
    """Çıkış taraması kombinasyonları; ilk satır .env'deki mevcut ayarlardır."""
    base = _current(cfg, EXIT_KEYS)
    combos: List[Combo] = [(base, True)]
    for tp in tps:
        for sl in sls:
            for trail in trails:
                if trail >= tp:      # İzleyen stop Kâr Al'dan önce başlamalı
                    continue
                for hold in holds:
                    for room in rooms:
                        st = {"TAKE_PROFIT_PCT": tp, "STOP_LOSS_PCT": sl, "TRAILING_ACTIVATION_PCT": trail,
                              "TRAILING_FLOOR_PCT": trail / 2, "MAX_HOLD_MINUTES": int(hold), "MIN_ROOM_RATIO": room}
                        same = all(st[k] == base[k] for k in EXIT_KEYS if k != "TRAILING_FLOOR_PCT")
                        if not same:
                            combos.append((st, False))
    return combos


def score_thresholds(signal_cache: dict) -> List[float]:
    """Önbellekteki sinyal puanlarından eşikler: 0 (filtre yok) + ortanca + üst çeyrek."""
    scores = sorted(sig.score for sig in signal_cache.values() if sig is not None)
    out = [0.0]
    if len(scores) >= 20:
        for q in SWEEP_SCORE_QUANTILES:
            value = round(scores[min(len(scores) - 1, int(q * len(scores)))], 1)
            if value > out[-1]:
                out.append(value)
    return out


def entry_combos(cfg, scores: Sequence[float], max_drops: Sequence[float] = SWEEP_MAX_DROP,
                 rooms: Sequence[float] = SWEEP_ENTRY_ROOM) -> List[Combo]:
    """Giriş taraması kombinasyonları (çıkışlar .env'deki gibi); ilk satır mevcut ayarlardır."""
    base = _current(cfg, ENTRY_KEYS)
    drops = sorted({cfg.max_24h_drop_pct, *(d for d in max_drops if d > cfg.min_24h_drop_pct)}, reverse=True)
    combos: List[Combo] = [(base, True)]
    for skip in (False, True):
        for drop in drops:
            for score in scores:
                for room in rooms:
                    st = {"REGIME_SKIP_WEAK": skip, "MAX_24H_DROP_PCT": drop, "MIN_SIGNAL_SCORE": score,
                          "MIN_ROOM_RATIO": room}
                    if st != base:
                        combos.append((st, False))
    return combos


def run_sweep(cfg, data: BacktestData, spread_pct: float = 0.05, progress: Progress = None,
              combos: Optional[Sequence[Combo]] = None, signal_cache: Optional[dict] = None) -> List[SweepRow]:
    """Her kombinasyonu aynı veriyle simüle eder. Giriş analizleri ortak önbellekten gelir (hızlı)."""
    say = progress or (lambda _msg: None)
    combos = list(combos) if combos is not None else sweep_combos(cfg)
    cache = signal_cache if signal_cache is not None else {}
    rows: List[SweepRow] = []
    for i, (settings, is_base) in enumerate(combos, 1):
        res = simulate(_apply(cfg, settings), data, spread_pct, None, signal_cache=cache)
        sm = summarize(res.trades)
        first, second = split_halves(res.trades, res.start_ms, res.end_ms)
        rows.append(SweepRow(dict(settings), is_base, sm.count, sm.win_rate, sm.net, sm.mean_net_pct,
                             res.max_drawdown_pct, sum(t.pnl for t in first), sum(t.pnl for t in second),
                             sm.profit_factor, sm.se_net_pct))
        if i == 1 or i % 10 == 0 or i == len(combos):
            say(f"   ⏳ Tarama {i}/{len(combos)} ayar denendi")
    return rows


def run_entry_sweep(cfg, data: BacktestData, spread_pct: float = 0.05, progress: Progress = None,
                    signal_cache: Optional[dict] = None) -> List[SweepRow]:
    """Giriş taraması: önce mevcut ayarlar çalışır (puan eşikleri onun sinyallerinden çıkar), sonra diğerleri."""
    cache = signal_cache if signal_cache is not None else {}
    base = run_sweep(cfg, data, spread_pct, None, [(_current(cfg, ENTRY_KEYS), True)], cache)
    combos = entry_combos(cfg, score_thresholds(cache))[1:]
    return base + run_sweep(cfg, data, spread_pct, progress, combos, cache)


def _fmt_setting(key: str, value) -> str:
    if SWEEP_FIELDS[key][1] is bool:
        return "true" if value else "false"
    if SWEEP_FIELDS[key][1] is int:
        return str(int(value))
    return f"{float(value):g}"


def _env_lines(r: SweepRow) -> List[str]:
    """.env'e yapıştırılabilir satırlar (her ayar ayrı satırda)."""
    return [f"{k}={_fmt_setting(k, v)}" for k, v in r.settings.items()]


def save_sweep_csv(rows: Sequence[SweepRow], path: Union[str, Path]) -> Path:
    """Tüm tarama satırlarını Excel ile açılabilir CSV'ye yazar."""
    import csv
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(dict.fromkeys(k for r in rows for k in r.settings))
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(keys + ["mevcut", "islem", "kazanma_yuzde", "net_usdt", "islem_basi_net_yuzde",
                                "maks_dusus_yuzde", "ilk_yari_usdt", "ikinci_yari_usdt", "kar_faktoru",
                                "standart_hata", "aday"])
        for r in rows:
            pf = "" if r.profit_factor is None or math.isinf(r.profit_factor) else f"{r.profit_factor:.3f}"
            writer.writerow([_fmt_setting(k, r.settings[k]) if k in r.settings else "" for k in keys] + [
                "evet" if r.baseline else "", r.trades, f"{r.win_rate:.1f}", f"{r.net:.4f}",
                f"{r.mean_net_pct:.3f}", f"{r.max_dd_pct:.2f}", f"{r.first_net:.4f}", f"{r.second_net:.4f}", pf,
                f"{r.se_net_pct:.4f}", "evet" if r.proven(len(rows)) else ""])
    return path


def _rank(rows: Sequence[SweepRow]) -> List[SweepRow]:
    """Önce yeterli işlemi olanlar (işlem başı sonuca göre), sonra az işlemliler.

    Toplam USDT'ye göre sıralamak, hiç kârlı ayar yokken "az işlem açan = az kaybeden" ayarları öne çıkarır.
    """
    def key(r: SweepRow):
        return (r.enough_trades, r.mean_net_pct, r.net)
    return sorted(rows, key=key, reverse=True)


def format_sweep_report(rows: Sequence[SweepRow], cfg, data: BacktestData, spread_pct: float = 0.05,
                        kind: str = "cikis") -> str:
    q = cfg.quote_asset
    entry = kind == "giris"
    ranked = _rank(rows)
    shown = ranked[:SWEEP_SHOW]
    base = next((r for r in rows if r.baseline), None)
    if base is not None and base not in shown:
        shown = shown + [base]
    few = [r for r in rows if not r.enough_trades]
    profitable = [r for r in rows if r.net > 0]
    robust = [r for r in ranked if r.proven(len(rows))]
    lucky = [r for r in ranked if r.net > 0 and r.consistent and r.enough_trades and not r.proven(len(rows))]
    columns = [k for k in dict.fromkeys(k for r in rows for k in r.settings) if SWEEP_FIELDS[k][2]]

    title = ("🧪 GİRİŞ TARAMASI (aynı veriyle farklı giriş filtreleri; çıkışlar .env'deki gibi)" if entry
             else "🧪 PARAMETRE TARAMASI (aynı veriyle farklı çıkış ayarları)")
    lines = [
        title,
        LINE,
        f"🗓️ Dönem: {_fmt_time(data.sim_start_ms)} → {_fmt_time(data.end_ms)} ({data.days:g} gün) | "
        f"{len(data.series)} sembol | sermaye {fmt_usdt(cfg.start_capital)} {q} | komisyon {fmt_rate(cfg.fee_rate)} "
        f"| spread %{spread_pct:g}",
        f"🔢 Denenen ayar: {len(rows)} | kârlı: {len(profitable)} | iki yarıda da kârlı ve "
        f"≥{MIN_TRADES_FOR_EVIDENCE} işlem: {len(robust) + len(lucky)} | şans eleğinden de geçen: {len(robust)}",
        "   (Karşılaştırma adil olsun diye taramada kill-switch kapalıdır; maks. düşüşe bakın.)",
        f"   Sıralama: işlem başına net sonuca göre; {MIN_TRADES_FOR_EVIDENCE}'dan az işlem açan {len(few)} ayar "
        f"(sonuçları şans) en alta alındı.",
        LINE,
        "   " + " ".join(f"{SWEEP_FIELDS[k][2]:>6}" for k in columns)
        + " | İşlem  Kaz%   Net USDT  İşl.başı  MaksDD |  1.yarı  2.yarı",
    ]
    for r in shown:
        if r.baseline:
            mark = "⭐"
        elif r in robust:
            mark = "✅"
        elif r in lucky:
            mark = "≈ "
        else:
            mark = "· " if not r.enough_trades else "  "
        cells = " ".join(f"{_fmt_setting(k, r.settings.get(k, 0)):>6}" for k in columns)
        lines.append(
            f"{mark} {cells} | {r.trades:>5d} {r.win_rate:>5.1f} {r.net:>+10.4f} {r.mean_net_pct:>+8.2f}% "
            f"{r.max_dd_pct:>6.1f}% | {r.first_net:>+7.3f} {r.second_net:>+7.3f}")
    legend = [f"   ⭐ = .env'deki mevcut ayarlarınız | ✅ = aday (iki yarıda kârlı ve şans eleğinden geçti) |",
              f"   ≈ = iki yarıda kârlı ama şans olabilir | · = {MIN_TRADES_FOR_EVIDENCE}'dan az işlem (güvenilmez)"]
    if entry:
        legend += ["   Zayıf = REGIME_SKIP_WEAK (true: piyasa zayıfken alım yok), MaxDüş = MAX_24H_DROP_PCT,",
                   "   Puan = MIN_SIGNAL_SCORE, Alan = MIN_ROOM_RATIO (0 = kapalı)"]
    else:
        legend += ["   TP/SL/İz. = Kâr Al / Zarar Kes / izleyen stop başlangıcı (%), Süre = maks. tutma (dk),",
                   "   Alan = MIN_ROOM_RATIO (0 = kapalı)"]
    lines += legend + ["   İşl.başı = işlem başına ortalama net K/Z", LINE]

    best = next((r for r in ranked if r.enough_trades), None)
    if not profitable:
        where = ("Giriş filtreleriyle de" if entry else "Çıkış ayarlarıyla")
        lines += [
            f"❗ Denenen ayarların HİÇBİRİ kârlı değil. {where} kâra geçilemiyor: bu dönemde strateji",
            "   fiyatın yükseleceği anları yakalayamıyor. Canlıda gerçek parayla ÇALIŞTIRMAYIN.",
        ]
        if best is not None:
            lines.append(f"   En iyi işlem başı sonuç (≥{MIN_TRADES_FOR_EVIDENCE} işlem): "
                         f"{fmt_signed(best.mean_net_pct)}% — kâra geçmek için işlem başına bu kadarlık iyileşme gerekir.")
        lines.append("   Daha önceki bir dönemi de deneyin (aşağıdaki --bitis komutu); orada da kârlı ayar çıkmazsa")
        lines.append("   strateji bu piyasa koşullarında işe yaramıyor demektir.")
        if not entry:
            lines.append("   Giriş filtrelerini denemek için: python main.py --backtest --gun "
                         f"{data.days:g} --tara-giris")
    elif not robust:
        top = max(profitable, key=lambda r: r.net)
        lines += [
            f"⚠️ Kârlı görünen ayarlar var (en iyisi {fmt_signed(top.net, 4)} {q}) ama hiçbiri şans eleğinden",
            f"   geçmiyor: {len(rows)} ayar denendiğinde birkaçının tesadüfen kârlı (hatta iki yarıda da kârlı)",
            "   çıkması beklenir — tamamen rastgele fiyat verisinde bile. Canlıya geçmek için yeterli kanıt yok.",
        ]
        if lucky:
            lines.append(f"   ≈ işaretli {len(lucky)} ayarı yine de daha önceki bir dönemde deneyebilirsiniz (aşağıdaki "
                         f"--bitis komutu); orada da kârlı çıkan ayar ciddiye alınabilir.")
    else:
        cand = robust[0]
        lines += [
            f"✅ En tutarlı aday ({cand.trades} işlem, net {fmt_signed(cand.net, 4)} {q}, iki yarıda da kârlı).",
            "   .env dosyanızdaki şu satırları DEĞİŞTİRİN (aynı isimli eski satırları silin):",
        ] + [f"      {line}" for line in _env_lines(cand)] + [
            f"   (İşlem başı sonucun alt sınırı, {len(rows)} ayar denendiği hesaba katılarak sıfırın üstünde.)",
            "   Yine de: çok ayar denendiği için en iyi sonuç iyimserdir. Önce bu ayarı test edilen dönemle",
            "   ÇAKIŞMAYAN daha önceki bir dönemde deneyin (aşağıdaki --bitis komutu); orada da kârlıysa",
            "   birkaç gün TEST modunda izleyin. --gun 30 / 60 aynı dönemin parçasıdır, bağımsız kanıt sayılmaz.",
        ]
    flag = "--tara-giris" if entry else "--tara"
    lines.append(f"🔁 Bağımsız dönem (bu testten önceki {data.days:g} gün): python main.py --backtest --gun "
                 f"{data.days:g} {flag} --bitis {_fmt_date(data.sim_start_ms)}")
    lines.append("📝 Kayma (slippage) yok, 5 dk mum çözünürlüğü, bugünün en hacimli coinleri (hayatta kalma "
                 "yanlılığı). Geçmiş sonuçlar geleceği garanti etmez.")
    return "\n".join(lines)


# ================================================================ rapor
def _fmt_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _fmt_time(ms: Optional[int]) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _trade_line(i: int, t: BacktestTrade, quote: str) -> str:
    label = REASON_LABELS.get(t.reason, t.reason)
    return (f"   {i}) {t.symbol} {fmt_signed(t.pnl, 4)} {quote} ({fmt_signed(t.pnl_pct)}%) | "
            f"{fmt_price(t.entry_price)} → {fmt_price(t.exit_price)} | {_fmt_time(t.open_time_ms)} | "
            f"{fmt_duration(t.duration_sec)} | {label}")


# ---------------------------------------------------------------- teşhis
@dataclass
class TradeSummary:
    count: int
    net: float             # Toplam net K/Z (USDT)
    win_rate: float        # %
    mean_net_pct: float    # İşlem başı net K/Z (maliyete göre %)
    mean_move_pct: float   # İşlem başı fiyat hareketi (komisyon + spread öncesi %)
    se_net_pct: float      # mean_net_pct'nin standart hatası
    profit_factor: Optional[float]

    @property
    def cost_pct(self) -> float:
        """İşlem başı ortalama maliyet: fiyat hareketiyle net sonuç arasındaki fark (komisyon + spread + toz)."""
        return self.mean_move_pct - self.mean_net_pct


def summarize(trades: Sequence[BacktestTrade]) -> TradeSummary:
    n = len(trades)
    if n == 0:
        return TradeSummary(0, 0.0, 0.0, 0.0, 0.0, 0.0, None)
    pcts = [t.pnl_pct for t in trades]
    mean = sum(pcts) / n
    var = sum((x - mean) ** 2 for x in pcts) / (n - 1) if n > 1 else 0.0
    gains = sum(t.pnl for t in trades if t.pnl > 0)
    pains = -sum(t.pnl for t in trades if t.pnl < 0)
    return TradeSummary(
        count=n, net=sum(t.pnl for t in trades), win_rate=sum(1 for t in trades if t.pnl >= 0) / n * 100,
        mean_net_pct=mean, mean_move_pct=sum(t.move_pct for t in trades) / n, se_net_pct=math.sqrt(var / n),
        profit_factor=gains / pains if pains > 0 else (math.inf if gains > 0 else None))


def split_halves(trades: Sequence[BacktestTrade], start_ms: int, end_ms: int
                 ) -> Tuple[List[BacktestTrade], List[BacktestTrade]]:
    """İşlemleri açılış zamanına göre dönemin ilk ve ikinci yarısına böler (tutarlılık kontrolü)."""
    mid = start_ms + (end_ms - start_ms) // 2
    return [t for t in trades if t.open_time_ms < mid], [t for t in trades if t.open_time_ms >= mid]


MIN_TRADES_FOR_EVIDENCE = 30
# Student-t %97.5 kritik değerleri (serbestlik derecesi → değer); 30 ve üstü için ~1.96-2.04
_T_TABLE = ((1, 12.71), (2, 4.30), (3, 3.18), (4, 2.78), (5, 2.57), (6, 2.45), (7, 2.36), (8, 2.31), (9, 2.26),
            (10, 2.23), (12, 2.18), (15, 2.13), (20, 2.09), (25, 2.06), (30, 2.04), (60, 2.00), (120, 1.98))


def _t_critical(df: int) -> float:
    """Küçük örneklemde %95 güven aralığı için t değeri (tabloda yoksa bir alttaki, yani daha temkinli)."""
    if df < 1:
        return math.inf
    return next(t for d, t in reversed(_T_TABLE) if df >= d)


def _share(trades: Sequence[BacktestTrade], pred) -> str:
    return f"%{sum(1 for t in trades if pred(t)) / len(trades) * 100:.0f}" if trades else "-"


def _pf_text(pf: Optional[float]) -> str:
    if pf is None:
        return "-"
    return "∞" if math.isinf(pf) else f"{pf:.2f}"


def diagnostics_lines(result: BacktestResult, cfg) -> List[str]:
    """Neden kazanıp kaybettiğini gösteren teşhis satırları (işlem yoksa boş)."""
    trades = result.trades
    if not trades:
        return []
    q = cfg.quote_asset
    sm = summarize(trades)
    lines = ["🔬 TEŞHİS (strateji nerede kazanıyor / kaybediyor?)",
             f"   İşlem başına ortalama: fiyat hareketi {fmt_signed(sm.mean_move_pct)}% | komisyon+spread+toz "
             f"-{sm.cost_pct:.2f}% | net {fmt_signed(sm.mean_net_pct)}%",
             f"   Kâr faktörü: {_pf_text(sm.profit_factor)} (toplam kazanç / toplam kayıp; 1'in altı zarar demek)"]
    margin = _t_critical(sm.count - 1) * sm.se_net_pct
    lo, hi = sm.mean_net_pct - margin, sm.mean_net_pct + margin
    # Az işlemde (veya hepsi aynı seviyeden kapandıysa) aralık yanıltıcı derecede dar çıkar: sonuç sayılmaz
    significant = sm.count >= MIN_TRADES_FOR_EVIDENCE and sm.se_net_pct > 0 and (lo > 0 or hi < 0)
    if sm.count < MIN_TRADES_FOR_EVIDENCE:
        verdict = f"(işlem sayısı {MIN_TRADES_FOR_EVIDENCE}'dan az: sonuç şanstan ayırt edilemez)"
    elif significant:
        verdict = "(şans eseri olma ihtimali düşük)"
    else:
        verdict = "(sıfırdan ayırt edilemiyor; daha uzun dönemle doğrulayın)"
    lines.append(f"   İşlem başı net sonucun %95 güven aralığı: {fmt_signed(lo)}% … {fmt_signed(hi)}% {verdict}")
    lines.append("   (Aynı anda açılan işlemler aynı piyasa hareketinden etkilenir; gerçek belirsizlik daha büyüktür.)")
    proven_gain = significant and lo > 0 and sm.net > 0 and (sm.profit_factor or 0) > 1
    if proven_gain:
        lines.append("   ✅ Komisyon sonrası beklenti pozitif ve bu dönemde tutarlı: bu ayarlarla işlem sayısı arttıkça "
                     "toplam kârın da artması beklenir (aynı risk sınırlarıyla). Yine de farklı bir dönemle doğrulayın.")
    elif sm.mean_move_pct <= 0:
        lines.append("   ❗ Fiyat hareketi komisyon ÖNCESİNDE bile ortalamada aleyhte: bu haliyle işlem sayısını artırmak "
                     "zararı büyütür, komisyon indirimi de kurtarmaz. Önce giriş/çıkış kuralları düzelmeli."
                     + ("" if significant else " (Kanıt henüz kesin değil.)"))
    elif sm.mean_net_pct <= 0 or sm.net <= 0:
        lines.append("   ⚠️ Fiyat hareketi lehte ama komisyon + spread'i karşılamıyor: daha seçici giriş "
                     "(daha az ama daha iyi işlem) veya daha düşük maliyet gerekir.")
    else:
        lines.append("   ➖ Ortalama sonuç pozitif görünüyor ama kanıt yetersiz (az işlem / güven aralığı sıfırı "
                     "içeriyor / kâr faktörü 1'e yakın). İşlem sayısını artırmadan önce daha uzun ve farklı "
                     "bir dönemle doğrulayın.")

    tp, sl = cfg.take_profit_pct, cfg.stop_loss_pct
    ups = sorted({x for x in (0.5, 1.0, 2.0, 3.0) if x < tp} | {tp})
    downs = sorted({x for x in (1.0, 2.0) if x < sl} | {sl})
    lines.append("   İşlem açıkken en iyi anı şu kâra ulaşanlar: " + " | ".join(
        f"+%{x:g}{' (Kâr Al)' if x == tp else ''}: {_share(trades, lambda t, x=x: t.peak_pct >= x)}" for x in ups))
    lines.append("   En kötü anı şu zarara düşenler: " + " | ".join(
        f"-%{x:g}{' (Zarar Kes)' if x == sl else ''}: {_share(trades, lambda t, x=x: t.trough_pct <= -x)}"
        for x in downs))
    reached = sorted(t.peak_after_sec / 60 for t in trades if t.peak_pct >= 1.0)
    if reached:
        lines.append(f"   +%1 ve üstünü görenlerde en iyi ana varış süresi (ortanca): {reached[len(reached) // 2]:.0f} dk")
    timeouts = [t for t in trades if t.reason == ExitReason.MAX_HOLD.value]
    if timeouts:
        lines.append(f"   Süre dolan {len(timeouts)} işlem: ortalama en iyi an {fmt_signed(sum(t.peak_pct for t in timeouts) / len(timeouts))}%, "
                     f"ortalama sonuç {fmt_signed(sum(t.pnl_pct for t in timeouts) / len(timeouts))}% "
                     f"(komisyon {fmt_usdt(sum(t.fee_usdt for t in timeouts))} {q})")

    need = required_move_pct(cfg)
    with_room = [t for t in trades if t.room_pct is not None]
    if with_room:
        wide = [t for t in with_room if t.room_pct >= need]
        narrow = [t for t in with_room if t.room_pct < need]

        def part(ts: Sequence[BacktestTrade]) -> str:
            if not ts:
                return "0 işlem"
            m = summarize(ts)
            return f"{m.count} işlem, kazanma %{m.win_rate:.0f}, işlem başı net {fmt_signed(m.mean_net_pct)}%"
        lines.append(f"   Hedef alanı (düşüş öncesi zirveye mesafe) Kâr Al için gereken ~%{need:.1f}'den büyük: "
                     f"{part(wide)} | küçük: {part(narrow)}")

    first, second = split_halves(trades, result.start_ms, result.end_ms)
    a, b = summarize(first), summarize(second)
    lines.append(f"   Dönemin 1. yarısı: {a.count} işlem, net {fmt_signed(a.net, 4)} {q} | "
                 f"2. yarısı: {b.count} işlem, net {fmt_signed(b.net, 4)} {q}")
    lines.append(f"   💡 Farklı Kâr Al / Zarar Kes / süre ayarlarını aynı veriyle karşılaştırmak için: "
                 f"python main.py --backtest --gun {result.days:g} --tara")
    lines.append(f"   💡 Bağımsız doğrulama (bu dönemle çakışmayan, daha önceki {result.days:g} gün): "
                 f"python main.py --backtest --gun {result.days:g} --bitis {_fmt_date(result.start_ms)}")
    return lines


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
        f"   Komisyon {fmt_rate(cfg.fee_rate)} | Spread %{result.spread_pct:g} | "
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
        if st.get("rejim_cokus") or st.get("rejim_zayif"):
            weak = "alım yapılmayan" if cfg.regime_skip_weak else "sıkı kurallı"
            lines.append(f"🌡️ Piyasa rejimi: çöküş nedeniyle alım yapılmayan {st.get('rejim_cokus', 0)} mum, "
                         f"zayıf piyasada {weak} {st.get('rejim_zayif', 0)} mum")
        if st.get("alan_dar"):
            lines.append(f"📏 Hedef alanı dar olduğu için alınmayan sinyal: {st['alan_dar']}")

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

    diag = diagnostics_lines(result, cfg)
    if diag:
        lines.append(LINE)
        lines.extend(diag)

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
