"""
Geriye dönük test (backtest): stratejiyi geçmiş mumlarla dener (5 dk stratejileri 5 dakikalık, uzun vadeli
swing stratejileri 1 saatlik mumlarla).

Canlı botla AYNI karar fonksiyonları kullanılır:
  - Giriş sinyali : strategy.make_strategy(cfg).analyze (STRATEGY: rebound / trend / breakout)
  - Boyutlandırma : risk.position_slots + risk.plan_entry (son slot kalan sermayenin tamamını kullanır)
  - Çıkışlar      : risk.evaluate_exit / update_trailing / update_breakeven / price_for_pnl / net_exit_value
  - Kill-switch   : risk.kill_switch_hit

Akış:
  1. Semboller: güncel 24 saatlik hacme göre en hacimli USDT pariteleri (+ BTC filtresi için BTCUSDT).
  2. Veri: stratejinin mum aralığındaki (5m / 1h) mumlar Binance'ten sayfalanarak indirilir ve
     data/backtest_cache/ altına kaydedilir; aynı dönem tekrar test edilirken Binance'e istek atılmaz.
  3. Simülasyon: tüm semboller ortak zaman çizelgesinde, her mum kapanışında
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
from position_rules import atr_pct, atr_targets, is_flat, momentum_strong, trailing_buy_trigger, volume_breakdown
from risk import (ExitReason, Position, breakeven_effective, evaluate_exit, extension_floor, kill_switch_hit,
                  net_exit_value, plan_entry, pnl_pct, position_sl, position_slots, position_tp, price_for_pnl,
                  target_exits, trailing_floor_pct, update_breakeven, update_trailing)
from rotation import btc_trend, momentum_pct, plan_rotation, pump_pct
from config import (PROFILE_ATTRS, PROFILES, ConfigError, is_long_horizon, is_mean_reversion, is_rotation,
                    strategy_family)
from entry_filters import candle_filters_on, candle_reject
from strategy import KLINE_LIMIT, STRATEGIES, Signal, make_strategy, required_move_pct, room_ok, strategy_class
from telegram_bot import LINE, fmt_duration, fmt_price, fmt_rate, fmt_signed, fmt_usdt

INTERVAL = "5m"
INTERVAL_MS = 300_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
INTERVAL_MS_BY_NAME = {"5m": INTERVAL_MS, "1h": HOUR_MS, "4h": 4 * HOUR_MS, "1d": DAY_MS}
INTERVAL_LABELS = {"5m": "5 dk", "1h": "1 saatlik", "4h": "4 saatlik", "1d": "günlük"}
REGIME_HISTORY_DAYS = 31               # Rejim için BTC 4s EMA50 (~9 gün) ve günlük EMA20 (20 gün) geçmişi
MIN_BREADTH_PAIRS_SIM = 20             # market_regime.MIN_BREADTH_PAIRS ile aynı
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
    ExitReason.ROTATION.value: "🔄 Rotasyon (sıralamadan düştü)",
    ExitReason.TREND_FILTER.value: "📉 BTC trend filtresi",
    ExitReason.TP_EXTENDED.value: "🚀 Uzatılmış Kâr Al",
    ExitReason.ESCAPE.value: "🏃 Erken kaçış (hacimli satış)",
    ExitReason.TIME_EXIT.value: "⏳ Süre: başabaşta satış",
    ExitReason.PROFIT_LOCK.value: "🔐 Piramit kilidi",
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
    interval: str = INTERVAL                             # Mum aralığı (5m / 1h / 1d)
    btc_hold_pct: Optional[float] = None                 # Kıyas: aynı dönemde BTC'yi alıp tutmak (%)
    btc_hold_dd_pct: Optional[float] = None              # ...ve en büyük düşüşü (%)

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
                    interval: str = INTERVAL, limit: int = PAGE_LIMIT) -> list:
    params = {"symbol": symbol, "interval": interval, "startTime": start_ms,
              "endTime": end_ms - 1, "limit": limit}
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


LISTING_CACHE_FILE = "listing_times.json"


def listing_times(client, symbols: Sequence[str], end_ms: int, cache_dir: Optional[Union[str, Path]] = None,
                  progress: Progress = None) -> Dict[str, Optional[int]]:
    """Her sembolün Binance'teki ilk günlük mumunun açılış zamanı (≈ listelenme günü, ms).

    Canlı coin filtresi (MIN_LISTING_DAYS) listelenmeden bu yana geçen günlük mum sayısına bakar; backtest'te de
    aynısı için gerekir. İndirilen dönemin başından bağımsızdır (dönemden kısa süre önce listelenen veya dönem başında
    işlemi kısa süre durmuş coinler de doğru değerlendirilir). Değişmediği için kalıcı olarak önbelleklenir; alınamazsa
    None (canlıdaki gibi filtre o coin için uygulanmaz).
    """
    path = Path(cache_dir) / LISTING_CACHE_FILE if cache_dir else None
    known: Dict[str, int] = {}
    if path is not None and path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                known = {str(k): int(v) for k, v in json.load(fh).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            known = {}
    out: Dict[str, Optional[int]] = {}
    fetched = False
    for sym in symbols:
        if sym in known:
            out[sym] = known[sym]
            continue
        try:
            rows = _request_klines(client, sym, 0, end_ms, progress, interval="1d", limit=1)
        except RateLimitError:
            break  # İstek limiti / IP yasağı: ısrar etmek yasağı uzatır; kalan semboller için filtre uygulanmaz
        except Exception:  # noqa: BLE001 - listelenme günü alınamazsa filtre uygulanmaz (fail-open)
            rows = []
        if REQUEST_PAUSE_SEC > 0:
            time.sleep(REQUEST_PAUSE_SEC)
        out[sym] = int(rows[0][0]) if rows else None
        if rows:
            known[sym] = out[sym]
            fetched = True
    if fetched and path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(known, fh)
            os.replace(tmp, path)
        except OSError:
            pass
    return out


# ================================================================ hizalanmış seri
class _Series:
    """Bir sembolün ortak zaman çizelgesine (5 dk veya 1 saat) hizalanmış mumları.

    İlk ve son gerçek mum arasındaki boşluklar (işlem durması) hacimsiz düz mumla doldurulur.
    """

    def __init__(self, symbol: str, rows: Sequence[Sequence[float]], t0: int, n: int,
                 rules: Optional[SymbolRules] = None, step_ms: int = INTERVAL_MS):
        self.symbol = symbol
        self.rules = rules
        self.t0 = t0
        self.n = n
        self.step_ms = step_ms
        self.bars_24h = DAY_MS // step_ms    # 24 saatlik pencere (5 dk: 288, 1 saat: 24)
        o = [0.0] * n
        h = [0.0] * n
        lo = [0.0] * n
        c = [0.0] * n
        qv = [0.0] * n
        tbq = [0.0] * n
        real = [False] * n
        for r in rows:
            k = (int(r[0]) - t0) // step_ms
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
        self.vol24: List[float] = [0.0] * n    # 24 saatlik quote hacmi (hacme göre aday sıralayan stratejiler için)
        self._raw: Dict[int, list] = {}
        self.listed_ms: Optional[int] = None   # Binance'teki ilk günlük mum (yeni coin filtresi için)

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
        min_vol = cfg.min_quote_volume_24h
        # Aynı veriyle denenen stratejiler aynı aileden (aynı mum aralığı) olduğu için bu ayrım veriyle sabittir
        min_range = cfg.min_24h_range_pct if strategy_class(cfg.strategy).uses_24h_range else 0.0
        dq_hi: deque = deque()
        dq_lo: deque = deque()
        for k in range(self.first, self.last + 1):
            while dq_hi and h[dq_hi[-1]] <= h[k]:
                dq_hi.pop()
            dq_hi.append(k)
            while dq_lo and lo[dq_lo[-1]] >= lo[k]:
                dq_lo.pop()
            dq_lo.append(k)
            j = k - self.bars_24h + 1  # 24s penceresinin ilk mumu
            if j < self.first:
                continue
            while dq_hi[0] < j:
                dq_hi.popleft()
            while dq_lo[0] < j:
                dq_lo.popleft()
            last, open24 = c[k], o[j]
            high24, low24 = h[dq_hi[0]], lo[dq_lo[0]]
            self.vol24[k] = prefix[k + 1] - prefix[j]
            if last <= 0 or low24 <= 0 or open24 <= 0:
                continue
            if self.vol24[k] < min_vol:
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
            t = self.t0 + j * self.step_ms
            row = [t, self.o[j], self.h[j], self.l[j], self.c[j], 0.0, t + self.step_ms - 1,
                   self.qv[j], 0, 0.0, self.tbq[j], 0.0]
            self._raw[j] = row
        return row

    def window(self, k: int, limit: int = KLINE_LIMIT) -> List[list]:
        """k. mum kapandığı anda canlı botun göreceği veri: son (limit-1) kapanmış mum + sentetik açık mum.

        Binance gibi yalnızca var olan mumları verir: dönem içinde listelenen coinde ilk gerçek mumdan önceki
        boş (sıfır fiyatlı) satırlar pencereye girmez, strateji canlıdaki gibi "veri az" der.
        """
        rows = [self._row(j) for j in range(max(0, self.first, k - (limit - 1) + 1), k + 1)]
        t_next = self.t0 + (k + 1) * self.step_ms
        p = self.c[k]
        rows.append([t_next, p, p, p, p, "0", t_next + self.step_ms - 1, "0", 0, "0", "0", "0"])
        return rows


# ================================================================ piyasa rejimi (canlı market_regime ile aynı)
class _RegimeSim:
    """Canlı `MarketRegime`'in backtest karşılığı: her mumda NORMAL / ZAYIF / COKUS.

    BTC 24s değişimi ve fiyatı simülasyon serisinden (5 dk / 1 saat), 4s/1g EMA'lar ayrıca indirilen BTC 4s ve 1g mumlarından
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
        self.bars_24h = bars_24h = btc.bars_24h
        self.total = [0] * n
        self.down = [0] * n
        for s in series.values():
            if s.first < 0:
                continue
            prefix = [0.0] * (s.n + 1)
            for k in range(s.n):
                prefix[k + 1] = prefix[k] + s.qv[k]
            for k in range(max(s.first + bars_24h - 1, 0), s.last + 1):
                j = k - bars_24h + 1
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
        j = k - self.bars_24h + 1
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


@dataclass
class _PendingBuy:
    """TRAILING_BUY: sinyalden sonra dipten dönüş bekleyen alım."""
    signal: Signal
    available: float
    low: float          # Sinyalden beri görülen en düşük piyasa fiyatı
    expires_ms: int


class _Simulator:
    def __init__(self, cfg, series: Dict[str, _Series], btc: Optional[_Series],
                 min_notional: float, spread_pct: float, progress: Progress,
                 regime: Optional[_RegimeSim] = None, signal_cache: Optional[dict] = None):
        self.cfg = cfg
        self.series = series
        self.btc = btc
        self.regime = regime if cfg.regime_filter else None
        # (strateji, analiz ayarları, sembol, mum, sıkı_mod) → Signal | None. Giriş analizi çıkış ayarlarına bağlı değildir; --tara'da
        # aynı veriyle denenen tüm kombinasyonlar bu önbelleği paylaşır (analiz bir kez yapılır).
        self.signal_cache = signal_cache if signal_cache is not None else {}
        self.min_notional = min_notional
        self.entry_factor = 1 + spread_pct / 200   # Alış ask'tan
        self.exit_factor = 1 - spread_pct / 200    # Satış bid'den
        self.progress = progress
        self.strategy = make_strategy(None, cfg)
        self.step_ms = next(iter(series.values())).step_ms if series else INTERVAL_MS
        self.btc_bars_1h = max(1, HOUR_MS // self.step_ms)   # BTC filtresi: son 1 saatin mumları
        self.analysis_key = self.strategy.analysis_key()

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
        # Risk sigortaları (canlı TradingBot._after_close ile aynı kurallar; gün sınırı UTC)
        self.freeze_until = 0
        self.day = -1
        self.day_pnl = 0.0
        self.sl_streak = 0
        self.pending_buys: Dict[str, _PendingBuy] = {}

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
        self.k_start = k_start   # Rotasyonun yeniden dengeleme günleri simülasyonun ilk gününden sayılır
        self.last_k = n - 1
        total = max(1, n - k_start)
        next_report = 0.25
        for k in range(k_start, n):
            t_close = t0 + (k + 1) * self.step_ms
            self._manage(k, t_close)
            if self._check_kill_switch(k, t_close):
                break
            if self.strategy.is_rotation:
                self._rotation_step(k, t_close)
            else:
                self._process_pending_buys(k, t_close)
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
                  now_sec: float, k: int = -1) -> Optional[Tuple[float, str]]:
        """KÖTÜMSER mum içi sıralama: önce düşük taraf (SL / izleyen stop tabanı), sonra TP."""
        cfg, pos = self.cfg, op.pos
        sl_pct = position_sl(pos, cfg)
        # Teşhis: işlemin gördüğü en iyi / en kötü an (çıkış mumu dahil; karar mantığını etkilemez)
        hi_pct, lo_pct = self._pnl_at(op, h), self._pnl_at(op, low)
        if hi_pct > op.peak:
            op.peak, op.peak_ms = hi_pct, int(now_sec * 1000)
        op.trough = min(op.trough, lo_pct)
        # a) Düşük taraf: en yüksek koruma seviyesi (SL / izleyen stop tabanı / başabaş tabanı)
        level, reason, level_pct = op.sl_price, ExitReason.STOP_LOSS, -sl_pct
        if pos.trailing_active:
            floor = trailing_floor_pct(pos, cfg)
            if floor > level_pct:
                level, reason, level_pct = self._market_level(floor, op), ExitReason.TRAILING_STOP, floor
        if pos.breakeven_active and cfg.breakeven_floor_pct > level_pct:
            # Canlıda izleyen stop önce kontrol edilir; başabaş yalnızca tabanı daha yüksekse önce tetiklenir
            level, reason, level_pct = (self._market_level(cfg.breakeven_floor_pct, op), ExitReason.BREAKEVEN,
                                        cfg.breakeven_floor_pct)
        if pos.tp_extended and extension_floor(pos, cfg) > level_pct:   # Uzatılmış hedef: zirveden geri dönüş
            level_pct = extension_floor(pos, cfg)
            level, reason = self._market_level(level_pct, op), ExitReason.TP_EXTENDED
        if pos.lock_pct is not None and pos.lock_pct > level_pct:        # Piramit kilidi
            level_pct = pos.lock_pct
            level, reason = self._market_level(level_pct, op), ExitReason.PROFIT_LOCK
        if low <= level:
            price = min(level, o)  # Açılış zaten eşiğin altındaysa açılıştan
            if reason is not ExitReason.STOP_LOSS and self._pnl_at(op, price) <= -sl_pct:
                reason = ExitReason.STOP_LOSS  # Canlı evaluate_exit de SL'yi önce kontrol eder
            return price, reason.value
        # b) Yüksek taraf
        if pos.exit_at_breakeven:   # Süre kuralı: başabaşa gelince sat
            even = self._market_level(0.0, op)
            if h >= even:
                return max(even, o), ExitReason.TIME_EXIT.value
        if not pos.tp_extended and h >= op.tp_price:
            if not (cfg.tp_extend and "tp_ext" not in pos.asked and self._extend_tp(op, k)):
                return max(op.tp_price, o), ExitReason.TAKE_PROFIT.value
            # Hedef bu mumda uzatıldı: kötümser varsayım, fiyat zirveden sonra kapanışa iner
            update_trailing(pos, self._pnl_at(op, h), cfg)
            floor = extension_floor(pos, cfg)
            if self._pnl_at(op, c) <= floor:
                return self._market_level(floor, op), ExitReason.TP_EXTENDED.value
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
                    self._close(symbol, s.c[s.last], END_REASON, s.t0 + (s.last + 1) * s.step_ms)
                continue
            hit = self._bar_exit(self.open[symbol], s.o[k], s.h[k], s.l[k], s.c[k], t_close / 1000, k)
            if hit:
                self._close(symbol, hit[0], hit[1], t_close)
            else:
                self._smart_step(symbol, k, t_close)

    # ------------------------------------------------------------ 1b) akıllı yönetim ("auto" modları)
    def _closed_rows(self, symbol: str, k: int, count: int) -> List[list]:
        """k. mum dahil son `count` kapanmış mum (canlıda o an görülen kapanmış mumlar)."""
        if k < 0:
            return []
        return self.series[symbol].window(k, count + 1)[:-1]

    def _extend_tp(self, op: _OpenPos, k: int) -> bool:
        """TP_EXTEND: Kâr Al'a gelindi; son kapanmış mumlarda momentum güçlüyse hedef uzatılır (pozisyon başına bir
        kez bakılır, canlıdaki gibi)."""
        op.pos.asked.append("tp_ext")
        if momentum_strong(self._closed_rows(op.pos.symbol, k - 1, 40)):
            op.pos.tp_extended = True
            self.stats["hedef_uzatma"] += 1
            return True
        return False

    def _smart_step(self, symbol: str, k: int, t_close: int) -> None:
        """Mum kapanışında süre / kaçış / DCA / piramit kuralları (yalnızca "auto" modlar simüle edilir)."""
        cfg = self.cfg
        if not target_exits(cfg):
            return
        op = self.open[symbol]
        pos = op.pos
        c = self.series[symbol].c[k]
        pnl = self._pnl_at(op, c)
        if (cfg.time_exit_mode == "auto" and "time" not in pos.asked
                and t_close / 1000 - pos.opened_at >= cfg.time_exit_minutes * 60 and is_flat(pnl, cfg.time_exit_band_pct)):
            pos.asked.append("time")
            pos.exit_at_breakeven = True
            self.stats["sure_kurali"] += 1
        if cfg.escape_mode == "auto" and "escape" not in pos.asked and pnl <= -cfg.escape_trigger_pct:
            if volume_breakdown(self._closed_rows(symbol, k, 30), cfg.escape_volume_ratio):
                pos.asked.append("escape")
                self.stats["erken_kacis"] += 1
                self._close(symbol, c, ExitReason.ESCAPE.value, t_close)
                return
        if pos.adds or pos.reserve_usdt < float(op.rules.min_notional) * cfg.min_notional_safety:
            return
        if cfg.dca_mode == "auto" and "dca" not in pos.asked and pnl <= -cfg.dca_trigger_pct:
            pos.asked.append("dca")
            self._add(symbol, k, "dca")
        elif (cfg.pyramid_mode == "auto" and "pyramid" not in pos.asked and not pos.tp_extended
              and pnl >= cfg.pyramid_trigger_pct and momentum_strong(self._closed_rows(symbol, k, 40))):
            pos.asked.append("pyramid")
            self._add(symbol, k, "pyramid")

    def _add(self, symbol: str, k: int, kind: str) -> None:
        """Yedek akçeyle pozisyona ekleme (canlı SmartManager.add_to_position ile aynı birleştirme)."""
        cfg = self.cfg
        op = self.open[symbol]
        pos, rules = op.pos, op.rules
        price = self.series[symbol].c[k] * self.entry_factor
        spend = min(pos.reserve_usdt, self.cash)
        qty = rules.floor_market_qty(to_dec(spend) / to_dec(price))
        if qty <= 0 or qty < rules.market_min_qty:
            return
        qty_f = float(qty)
        notional = qty_f * price
        if notional < float(rules.min_notional) or notional > self.cash + 1e-9:
            return
        fee_base = qty_f * cfg.fee_rate
        net_qty = qty_f - fee_base
        pos.entry_price = (pos.entry_price * pos.qty + price * net_qty) / (pos.qty + net_qty)
        pos.qty += net_qty
        pos.cost_usdt += notional
        pos.buy_fee_usdt += fee_base * price
        pos.reserve_usdt = round(max(0.0, pos.reserve_usdt - notional), 2)
        pos.adds += 1
        op.buy_fee += fee_base * price
        sell_q = rules.floor_market_qty(pos.qty)
        op.sellable = float(sell_q) if sell_q >= rules.market_min_qty else 0.0
        op.tp_price = self._market_level(position_tp(pos, cfg), op)
        op.sl_price = self._market_level(-position_sl(pos, cfg), op)
        pos.peak_pnl_pct = self._pnl_at(op, self.series[symbol].c[k])   # Korumalar yeni maliyete göre baştan
        pos.trailing_active = pos.breakeven_active = False
        if kind == "pyramid":
            pos.lock_pct = max(0.1, cfg.breakeven_floor_pct)
        self.cash -= notional
        self.fees += fee_base * price
        self.stats["ekleme_dca" if kind == "dca" else "ekleme_piramit"] += 1

    def _hourly_atr(self, symbol: str, k: int, price: float) -> Optional[float]:
        """k. mumun kapanışında görülen son 29 kapanmış 1 saatlik mumun ATR'si (canlı: 1h klines, 30 mum)."""
        s = self.series[symbol]
        if s.step_ms >= DAY_MS:
            return None
        per = max(1, HOUR_MS // s.step_ms)
        last_end = (s.t0 + (k + 1) * s.step_ms) // HOUR_MS * HOUR_MS
        rows = []
        for i in range(29, 0, -1):
            start = last_end - i * HOUR_MS
            j0 = (start - s.t0) // s.step_ms
            j1 = j0 + per - 1
            if j0 < max(0, s.first) or j1 > k:
                continue
            rows.append([start, s.o[j0], max(s.h[j0:j1 + 1]), min(s.l[j0:j1 + 1]), s.c[j1]])
        return atr_pct(rows, price)

    def _process_pending_buys(self, k: int, t_close: int) -> None:
        """TRAILING_BUY: fiyat düştükçe dip izlenir, dipten TRAILING_BUY_PCT dönünce alınır.

        Kötümser mum içi sıralama: önce eski dibe göre dönüş (yükseliş) aranır, sonra yeni dip; yeni dipten dönüş
        ancak kapanışta görülürse kapanıştan alınır.
        """
        cfg = self.cfg
        for symbol, pb in list(self.pending_buys.items()):
            s = self.series[symbol]
            if self.killed or t_close < self.freeze_until or not s.has(k) or t_close > pb.expires_ms:
                del self.pending_buys[symbol]
                self.stats["kademeli_iptal"] += 1
                continue
            price = None
            trigger = trailing_buy_trigger(pb.low, cfg.trailing_buy_pct)
            if s.h[k] >= trigger:
                price = max(trigger, s.o[k])
            else:
                pb.low = min(pb.low, s.l[k])
                if s.c[k] >= trailing_buy_trigger(pb.low, cfg.trailing_buy_pct):
                    price = s.c[k]
            if price is None:
                continue
            del self.pending_buys[symbol]
            rules = s.rules
            entry_price = price * self.entry_factor
            plan = plan_entry(min(pb.available, self.cash), rules, entry_price, cfg)
            if not plan.ok:
                self.stats["plan_red"] += 1
                continue
            reserve = round(max(0.0, pb.available - plan.quote_amount), 2) if cfg.capital_usage_pct < 100 else 0.0
            if self._open(pb.signal, rules, plan.quote_amount, entry_price, t_close, k=k, reserve=reserve):
                self.stats["kademeli_alim"] += 1

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
        self._after_close(pnl, reason, t_ms)

    def _after_close(self, pnl: float, reason: str, t_ms: int) -> None:
        """Günlük zarar limiti / kâr hedefi ve zarar serisi sigortası: tetiklenirse yeni alımlar dondurulur."""
        cfg = self.cfg
        if self.strategy.is_rotation:
            return
        day = t_ms // DAY_MS
        if day != self.day:
            self.day, self.day_pnl = day, 0.0
        self.day_pnl += pnl
        midnight = (day + 1) * DAY_MS
        if cfg.daily_loss_limit_pct > 0 and self.day_pnl <= -cfg.start_capital * cfg.daily_loss_limit_pct / 100 + 1e-9:
            self._freeze(midnight)
        if cfg.daily_profit_target_pct > 0 and self.day_pnl >= cfg.start_capital * cfg.daily_profit_target_pct / 100:
            self._freeze(midnight)
        if reason in (ExitReason.STOP_LOSS.value, ExitReason.BACKUP_STOP.value):
            self.sl_streak += 1
        elif reason not in (ExitReason.MANUAL.value, ExitReason.KILL_SWITCH.value, ExitReason.EXTERNAL.value,
                            END_REASON):
            self.sl_streak = 0
        if cfg.loss_streak_limit > 0 and self.sl_streak >= cfg.loss_streak_limit:
            self.sl_streak = 0
            self._freeze(t_ms + int(cfg.loss_streak_pause_hours * 3_600_000))

    def _freeze(self, until_ms: int) -> None:
        if until_ms > self.freeze_until:
            self.freeze_until = until_ms
            self.stats["dondurma"] += 1

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
        """BTC son 1 saatte (5 dk: 12 mum, 1 saat: 1 mum) sert düştüyse giriş yok (canlı `market_ok` ile aynı)."""
        cfg, btc = self.cfg, self.btc
        if not cfg.btc_filter or cfg.quote_asset != "USDT" or btc is None:
            return True
        j = k - self.btc_bars_1h + 1
        if not (btc.has(k) and btc.has(j)) or btc.o[j] <= 0:
            return True  # Veri yoksa canlı bot da filtreyi atlar
        change = (btc.c[k] - btc.o[j]) / btc.o[j] * 100
        return change > -cfg.btc_max_1h_drop_pct

    def _listing_ok(self, s: _Series, t_close: int) -> bool:
        """Canlı coin filtresi (DELIST_FILTER): MIN_LISTING_DAYS günlük mumu olmayan yeni coin alınmaz.

        Günlük mum sayısı canlıdaki gibi listelendiği gün (ve bugün) dahil sayılır. Listelenme günü bilinmiyorsa
        (alınamadı) canlıdaki gibi filtre uygulanmaz.
        """
        cfg = self.cfg
        if not cfg.delist_filter or cfg.min_listing_days <= 0 or s.listed_ms is None:
            return True
        return t_close // DAY_MS - s.listed_ms // DAY_MS + 1 >= cfg.min_listing_days

    def _scan_and_enter(self, k: int, t_close: int) -> None:
        cfg = self.cfg
        capital = cfg.start_capital + self.realized
        slots = position_slots(capital, self.min_notional, cfg)
        free_slots = slots - len(self.open) - len(self.pending_buys)
        if free_slots <= 0:
            return
        if t_close < self.freeze_until:      # Canlıda da risk sigortası tetiklenince yeni alım yapılmaz
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
            if (s.rules is None or symbol in self.open or symbol in self.pending_buys
                    or self.cooldown.get(symbol, 0) > t_close):
                continue
            if not s.has(k):
                continue
            change = s.change24[k]
            if change is None or not self.strategy.change_ok(change):
                continue
            if target > 0 and s.rules.step_value(s.c[k]) / target * 100 > cfg.max_dust_pct:
                continue
            candidates.append((self.strategy.candidate_key(change, s.vol24[k]), symbol))
        if not candidates:
            return
        candidates.sort()  # Canlı ön elemeyle aynı sıra (rebound: en çok düşen, trend: en çok yükselen önce)

        signals = []
        for _, symbol in candidates[: self.strategy.candidate_limit]:
            if not self._listing_ok(self.series[symbol], t_close):   # Canlıda da ön elemeden sonra elenir
                self.stats["yeni_coin"] += 1
                continue
            self.stats["analiz"] += 1
            signal = self._analyze(symbol, k, strict)
            if signal and cfg.min_signal_score > 0 and signal.score < cfg.min_signal_score:
                self.stats["puan_dusuk"] += 1  # Canlı bot da MIN_SIGNAL_SCORE altını almaz
                continue
            if signal and not room_ok(signal, cfg):
                self.stats["alan_dar"] += 1    # Canlı tarama da hedef alanı dar sinyali almaz
                continue
            if signal and candle_filters_on(cfg):   # Canlı taramayla aynı ek mum filtreleri
                why = candle_reject(cfg, self.series[symbol].window(k, self.strategy.kline_limit))
                if why:
                    self.stats[f"filtre:{why}"] += 1
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
            committed = (sum(op.pos.cost_usdt for op in self.open.values())
                         + sum(pb.available for pb in self.pending_buys.values()))   # Bekleyen alımlara ayrılan
            remaining = capital - committed
            is_last_slot = len(self.open) + len(self.pending_buys) + 1 >= slots
            # Son slot artanı kullanır (yedek akçe açıkken, CAPITAL_USAGE_PCT < 100, kullanmaz: canlıdaki gibi)
            budget = remaining if is_last_slot and cfg.capital_usage_pct >= 100 else min(target, remaining)
            entry_price = signal.price * self.entry_factor
            available = min(budget, self.cash)
            plan = plan_entry(available, rules, entry_price, cfg)
            if not plan.ok:
                self.stats["plan_red"] += 1
                if "bakiye" in plan.reason.lower():
                    return
                continue
            if cfg.trailing_buy:   # Canlıdaki gibi: hemen alma, dipten dönüşü bekle (en az bir mum)
                wait_ms = max(cfg.trailing_buy_minutes * 60_000, self.step_ms)
                self.pending_buys[signal.symbol] = _PendingBuy(signal, available, signal.price, t_close + wait_ms)
                opened += 1
                continue
            reserve = round(max(0.0, available - plan.quote_amount), 2) if cfg.capital_usage_pct < 100 else 0.0
            if self._open(signal, rules, plan.quote_amount, entry_price, t_close, k=k, reserve=reserve):
                opened += 1

    # ------------------------------------------------------------ 3b) momentum rotasyonu
    def _pumped(self, s: _Series, k: int) -> bool:
        """Canlı coin filtresinin pompa kontrolü (DELIST_FILTER, PUMP_FILTER_PCT) günlük mumlarla.

        Canlıda yeniden dengeleme gün kapanışından hemen sonra yapılır; günlük mum listesinin sonunda yeni açılmış
        (fiyatı ≈ kapanış) bugünün mumu vardır. Burada da aynı liste kurulur.
        """
        cfg = self.cfg
        days = cfg.pump_lookback_days
        if not cfg.delist_filter or cfg.pump_filter_pct <= 0 or days <= 0 or s.step_ms != DAY_MS:
            return False
        lo = k - 2 * days + 2
        if lo < max(0, s.first):
            return False
        highs = s.h[lo:k + 1] + [s.c[k]]
        lows = s.l[lo:k + 1] + [s.c[k]]
        pump = pump_pct(highs, lows, days)
        return pump is not None and pump >= cfg.pump_filter_pct

    def _rotation_step(self, k: int, t_close: int) -> None:
        """Canlı TradingBot.rotation_tick'in karşılığı (gün kapanışında, kapanış fiyatından):
        her gün BTC trend kontrolü, ROTATION_DAYS günde bir yeniden dengeleme."""
        cfg = self.cfg
        risk_on: Optional[bool] = True
        if cfg.rotation_btc_ma_days > 0 and cfg.quote_asset == "USDT":
            btc = self.btc
            if btc is not None and btc.has(k):
                lo = max(btc.first, k - cfg.rotation_btc_ma_days + 1)
                risk_on, _avg = btc_trend(btc.c[lo:k + 1], cfg.rotation_btc_ma_days)
            else:
                risk_on = None
        if risk_on is False:
            self.stats["trend_kapali_gun"] += 1
            for symbol in list(self.open):
                self._close(symbol, self._close_price(symbol, k), ExitReason.TREND_FILTER.value, t_close)
        if (k - self.k_start) % cfg.rotation_days != 0 or risk_on is False:
            return
        if k >= self.last_k:   # Son mum yalnızca değerlemedir: alınan coin aynı fiyattan hemen "kapatılırdı"
            return
        self.stats["dengeleme"] += 1

        capital = cfg.start_capital + self.realized
        slots = min(cfg.rotation_top_n, position_slots(capital, self.min_notional, cfg))
        liquid: List[Tuple[float, str]] = []
        for symbol, s in self.series.items():
            if s.rules is None or not s.has(k) or s.change24[k] is None:   # Hacim / fiyat adımı ön elemesi
                continue
            liquid.append((s.qv[k], symbol))
        liquid.sort(key=lambda x: (-x[0], x[1]))
        lookback = cfg.rotation_lookback_days
        momentum: Dict[str, Optional[float]] = {}
        for _, symbol in liquid[: cfg.rotation_universe]:
            s = self.series[symbol]
            if not self._listing_ok(s, t_close):      # Canlıda da güvenlik filtresine takılan sıralamaya girmez
                self.stats["yeni_coin"] += 1
                continue
            if self._pumped(s, k):
                self.stats["pompa"] += 1
                continue
            momentum[symbol] = momentum_pct(s.c[k - lookback:k + 1], lookback) if k - lookback >= s.first else None
        plan = plan_rotation(list(self.open), momentum, slots, risk_on)
        for symbol in plan.sell:
            self._close(symbol, self._close_price(symbol, k), ExitReason.ROTATION.value, t_close)

        # Canlıdaki gibi: alım tutarı ve yer sayısı satışlardan SONRAKİ sermayeyle (satışın kârı / zararı dahil)
        capital = cfg.start_capital + self.realized
        slots = min(slots, position_slots(capital, self.min_notional, cfg))
        target = capital / slots if slots > 0 else 0.0
        ranks = {sym: i + 1 for i, (sym, _) in enumerate(plan.ranking)}
        moms = dict(plan.ranking)
        for symbol in plan.candidates:
            if len(self.open) >= slots:
                break
            s = self.series[symbol]
            committed = sum(op.pos.cost_usdt for op in self.open.values())
            remaining = capital - committed
            is_last_slot = len(self.open) + 1 >= slots
            budget = remaining if is_last_slot else min(target, remaining)
            entry_price = s.c[k] * self.entry_factor
            entry = plan_entry(min(budget, self.cash), s.rules, entry_price, cfg)
            if not entry.ok:
                self.stats["plan_red"] += 1
                if "bakiye" in entry.reason.lower():
                    break
                continue   # Toz / LOT adımı: sıradakini dene (canlıda da)
            signal = Signal(symbol, s.c[k], moms[symbol], {"kind": "rotation", "momentum_pct": moms[symbol],
                                                           "rank": float(ranks[symbol]),
                                                           "lookback_days": float(lookback)})
            if self._open(signal, s.rules, entry.quote_amount, entry_price, t_close):
                self.stats["sinyal"] += 1

    def _analyze(self, symbol: str, k: int, strict: bool) -> Optional[Signal]:
        key = (self.strategy.name, self.analysis_key, symbol, k, strict)
        if key in self.signal_cache:
            return self.signal_cache[key]
        window = self.series[symbol].window(k, self.strategy.kline_limit)
        signal, _reason = self.strategy.analyze(symbol, window, strict=strict)
        self.signal_cache[key] = signal
        return signal

    def _open(self, signal, rules: SymbolRules, quote_amount: float, ask: float, t_close: int,
              k: Optional[int] = None, reserve: float = 0.0) -> bool:
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
        pos.reserve_usdt = reserve
        if k is not None and cfg.atr_targets:   # Canlıdaki gibi: girişte coinin 1 saatlik ATR'sine göre hedefler
            pos.tp_pct, pos.sl_pct = atr_targets(cfg, self._hourly_atr(signal.symbol, k, ask / self.entry_factor))
        op = _OpenPos(pos=pos, rules=rules, sellable=sellable, score=signal.score, open_ms=t_close,
                      tp_price=0.0, sl_price=0.0, market_entry=ask / self.entry_factor, buy_fee=fee_base * ask,
                      room=signal.metrics.get("room_pct"))
        op.tp_price = self._market_level(position_tp(pos, cfg), op) if target_exits(cfg) else math.inf
        op.sl_price = self._market_level(-position_sl(pos, cfg), op)
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
            self._close(symbol, self.series[symbol].c[kk], END_REASON, t0 + (kk + 1) * self.step_ms)
        self._record_equity(n - 1, t0 + n * self.step_ms)


# ================================================================ ana fonksiyon
def _base_notes(spread_pct: float, interval: str = INTERVAL, rotation: bool = False) -> List[str]:
    label = INTERVAL_LABELS.get(interval, interval)
    notes = [
        "Hayatta kalma yanlılığı: semboller BUGÜNÜN en hacimli paritelerinden seçildi; dönem içinde "
        "delist olan veya çöken coinler teste dahil değil (sonuçlar olduğundan iyi görünebilir).",
        "Çözünürlük: canlı bot ~30 sn'de bir tarar ve birkaç saniyede bir fiyat kontrol eder; backtest "
        f"yalnızca {label} mum kapanışlarında karar verir.",
        "Kötümser mum içi sıralama: aynı mumda hem zarar-kes hem kâr-al seviyesi görüldüyse zarar-kes "
        "varsayıldı; zirve (izleyen stop) güncellemesi düşük taraf kontrolünden sonra yapıldı.",
        f"Sabit %{spread_pct:g} spread varsayıldı (alış +%{spread_pct / 2:g}, satış -%{spread_pct / 2:g}); "
        "kayma (slippage) ve emir defteri derinliği hesaba katılmadı.",
        "Spread, delist takvimi ve pompa filtreleri, BTC çöküş çıkışı ve sinyal teyidi simüle edilmedi. Başabaş, "
        "ATR filtresi, minimum sinyal puanı, hedef alanı ve yeni listelenen coin filtresi (MIN_LISTING_DAYS, "
        "listelenme günü Binance'in ilk günlük mumundan) canlıdaki gibi uygulandı.",
        "Piyasa rejimi filtresi yaklaşık simüle edildi: 'düşen coin oranı' canlıda tüm likit paritelerden, "
        "burada yalnızca test edilen sembollerden hesaplanır.",
        "Geçmiş performans gelecekteki sonuçları garanti etmez.",
    ]
    if interval != INTERVAL:
        notes.insert(1, "Uzun dönem: bugünün en hacimli coinlerinden dönem içinde listelenenler, canlıdaki gibi "
                        "yeterli geçmişleri oluşunca (saatlik mumda ~11 gün, MIN_LISTING_DAYS) teste girer.")
    if rotation:   # Kâr al / piyasa rejimi rotasyonda yok; yeni listelenen coinler için daha açık bir not
        notes = [n for n in notes if not n.startswith(("Kötümser mum", "Piyasa rejimi", "Uzun dönem"))]
        notes.insert(1, "Kötümser mum içi sıralama: felaket zarar kesi gün içinde görüldüyse o seviyeden satıldı.")
        notes.insert(2, "Dönem içinde listelenen coinler canlıdaki gibi momentum süresi kadar geçmişleri ve "
                        "MIN_LISTING_DAYS dolunca sıralamaya girer.")
        notes[0] = ("Hayatta kalma yanlılığı (rotasyonda ÖNEMLİ): coinler BUGÜNÜN en hacimlilerinden seçildi. Geçmişte "
                    "yükselip sonra çöken / delist olan coinler listede yok; momentum stratejisi tam da bu coinleri "
                    "alırdı. Sonuç olduğundan iyi görünebilir.")
        notes = [n for n in notes if not n.startswith("Spread, delist")] + [
            "Rotasyon gün kapanışında, kapanış fiyatından simüle edildi (canlı bot UTC 00:05 sonrası işlem yapar). "
            "Yeni listelenen coin ve pompa filtresi canlıdaki gibi uygulandı; delist takvimi, spread filtresi ve "
            "(açıksa) BTC_CRASH_EXIT simüle edilmedi."]
    return notes


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
    interval: str = INTERVAL   # Mum aralığı (stratejiye göre: 5m / 1h)

    @property
    def step_ms(self) -> int:
        return INTERVAL_MS_BY_NAME[self.interval]

    @property
    def sim_start_ms(self) -> int:
        return self.start_ms + self.k_start * self.step_ms


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
        if strategy_class(cfg.strategy).is_rotation and max_symbols < cfg.rotation_universe:
            notes.append(f"Rotasyon evreni (ROTATION_UNIVERSE={cfg.rotation_universe}) için {max_symbols} yerine "
                         f"{cfg.rotation_universe} sembol indirildi.")
            max_symbols = cfg.rotation_universe
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
    btc_symbol = "BTCUSDT" if usdt else None   # BTC filtresi, piyasa rejimi, rotasyon trend filtresi ve kıyas için
    strategy = strategy_class(cfg.strategy)
    interval = strategy.interval
    step_ms = INTERVAL_MS_BY_NAME[interval]

    # --- zaman aralığı (saate yuvarlanmış → önbellek anahtarı sabit kalır). Günlük mumlarda UTC gün başına:
    #     Binance günlük mumları 00:00 UTC'de açılır; böylece son mum kapanmış gün olur ve karar anı gerçek kapanıştır
    now_ms = int(time.time() * 1000) if end_time_ms is None else int(end_time_ms)
    if step_ms >= DAY_MS:
        end_ms = now_ms // DAY_MS * DAY_MS
        total_hours = int(math.ceil(days + strategy.warmup_days)) * 24
    else:
        end_ms = now_ms // HOUR_MS * HOUR_MS
        total_hours = int(math.ceil((days + strategy.warmup_days) * 24))
    start_ms = end_ms - total_hours * HOUR_MS
    n = (end_ms - start_ms) // step_ms
    sim_bars = min(n, max(1, int(round(days * DAY_MS / step_ms))))
    k_start = n - sim_bars

    # --- veri
    to_load = list(trade_symbols)
    if btc_symbol and btc_symbol not in to_load:
        to_load.append(btc_symbol)
    say(f"📥 Mum verileri hazırlanıyor: {len(to_load)} sembol, {total_hours / 24:g} gün ({interval})")
    data: Dict[str, List[list]] = {}
    failed: List[str] = []
    cached = 0
    for i, sym in enumerate(to_load, 1):
        try:
            rows, from_cache = load_klines(client, sym, start_ms, end_ms, cache_dir, say, interval=interval)
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
            s = _Series(sym, data[sym], start_ms, n, rules[sym], step_ms)
            s.prepare_prefilter(cfg)
            series[sym] = s
    if not series:
        raise BacktestError("Hiç mum verisi indirilemedi (internet bağlantısını kontrol edin)")
    if cfg.delist_filter and cfg.min_listing_days > 0:   # Yeni coin filtresi için listelenme günleri
        listed = listing_times(client, list(series), end_ms, cache_dir, say)
        for sym, s in series.items():
            s.listed_ms = listed.get(sym)
        unknown = [sym for sym in series if listed.get(sym) is None]
        if unknown:
            notes.append(f"Listelenme günü alınamayan sembollerde yeni coin filtresi uygulanmadı: {', '.join(unknown)}")
    btc: Optional[_Series] = None
    if btc_symbol:
        btc = series.get(btc_symbol)
        if btc is None and btc_symbol in data:
            btc = _Series(btc_symbol, data[btc_symbol], start_ms, n, step_ms=step_ms)
        if btc is None:
            notes.append("BTC verisi alınamadı: BTC filtreleri, piyasa rejimi ve BTC kıyası uygulanmadı.")

    # --- piyasa rejimi için BTC 4s / 1g mumları (canlıda MarketRegime ile aynı EMA'lar)
    regime: Optional[_RegimeSim] = None
    if cfg.regime_filter and btc is not None and not strategy.is_rotation:   # Rotasyonun kendi BTC filtresi var
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
                        start_ms=start_ms, end_ms=end_ms, n=n, k_start=k_start, days=float(days), notes=notes,
                        interval=interval)


def simulate(cfg, data: BacktestData, spread_pct: float = 0.05, progress: Progress = None,
             signal_cache: Optional[dict] = None) -> BacktestResult:
    """Hazırlanmış veri üzerinde bir ayar setini simüle eder."""
    if spread_pct < 0:
        raise ValueError("spread_pct negatif olamaz")
    needed = strategy_class(cfg.strategy).interval
    if needed != data.interval:
        raise BacktestError(f"{cfg.strategy} stratejisi {needed} mumlarla çalışır ama veri {data.interval} "
                            f"mumlarla hazırlandı (aynı taramada farklı aralıklı stratejiler karşılaştırılamaz)")
    sim = _Simulator(cfg, data.series, data.btc, data.min_notional, spread_pct, progress, regime=data.regime,
                     signal_cache=signal_cache)
    sim.run(data.start_ms, data.k_start, data.n)

    notes = _base_notes(spread_pct, data.interval, is_rotation(cfg.strategy)) + list(data.notes)
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
        interval=data.interval,
        **dict(zip(("btc_hold_pct", "btc_hold_dd_pct"), btc_hold(data))),
    )


def btc_hold(data: BacktestData) -> Tuple[Optional[float], Optional[float]]:
    """Kıyas: simülasyonun ilk kararından dönem sonuna BTC'yi alıp tutmanın getirisi ve en büyük düşüşü (%)."""
    btc = data.btc
    if btc is None or not btc.has(data.k_start):
        return None, None
    closes = [btc.c[k] for k in range(data.k_start, min(data.n, btc.last + 1))]
    if not closes or closes[0] <= 0:
        return None, None
    peak, max_dd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        max_dd = max(max_dd, (peak - c) / peak * 100 if peak > 0 else 0.0)
    return (closes[-1] / closes[0] - 1) * 100, max_dd


def run_backtest(cfg, client, days: float = 30, max_symbols: int = 40,
                 cache_dir: Optional[Union[str, Path]] = DATA_DIR / "backtest_cache",
                 progress: Progress = print, symbols: Optional[Sequence[str]] = None,
                 end_time_ms: Optional[int] = None, spread_pct: float = 0.05) -> BacktestResult:
    """Son `days` günü stratejinin mum aralığıyla simüle eder. Ağ hatası exchange_info/tickers_24h'den yükselebilir."""
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
# Uzun vadeli (swing) stratejilerin çıkış taraması: günlerce süren, %8-15 hedefli işlemler (süre dakika: 2 / 5 gün)
SWING_SWEEP_TP = (8.0, 12.0, 15.0)
SWING_SWEEP_SL = (4.0, 6.0, 8.0)
SWING_SWEEP_TRAIL = (6.0, 10.0)
SWING_SWEEP_HOLD = (2880, 7200)
SWING_SWEEP_LOOKBACK = (48, 72, 168)  # Giriş taraması: SWING_LOOKBACK_HOURS (2 / 3 / 7 gün)
# Ortalamaya dönüş grubu (mean_revert, rsi_divergence): küçük hedef, saatler-2 gün (süre dk: 12 saat / 1 gün / 2 gün)
MR_SWEEP_TP = (1.0, 1.5, 2.5)
MR_SWEEP_SL = (3.0, 5.0, 8.0)
MR_SWEEP_TRAIL = (0.7, 1.0)
MR_SWEEP_HOLD = (720, 1440, 2880)
MR_SWEEP_BAND = (1.5, 2.0, 2.5)       # Giriş taraması: MR_BAND_STD (aşırı satım eşiği, standart sapma)
DIV_SWEEP_OVERSOLD = (30.0, 35.0, 40.0)   # rsi_divergence giriş taraması: RSI_DIV_OVERSOLD (ilk dipte RSI en fazla)
# Momentum rotasyonu: "çıkış" = felaket zarar kesi ve yeniden dengeleme sıklığı, "giriş" = momentum süresi,
# BTC trend filtresi ve tutulan coin sayısı
ROTATION_SWEEP_SL = (10.0, 20.0, 30.0)
ROTATION_SWEEP_DAYS = (7, 14)
ROTATION_SWEEP_LOOKBACK = (14, 28, 56)
ROTATION_SWEEP_MA = (0, 50, 100)
ROTATION_SWEEP_TOP = (2, 3)
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
    "STRATEGY": ("strategy", str, "Strateji"),
    "MAX_24H_DROP_PCT": ("max_24h_drop_pct", float, "MaxDüş"),
    "MAX_24H_GAIN_PCT": ("max_24h_gain_pct", float, "MaxYük"),
    "MIN_SIGNAL_SCORE": ("min_signal_score", float, "Puan"),
    "MIN_ROOM_RATIO": ("min_room_ratio", float, "Alan"),
    "SWING_LOOKBACK_HOURS": ("swing_lookback_hours", int, "Zirve"),
    "ROTATION_DAYS": ("rotation_days", int, "Gün"),
    "ROTATION_LOOKBACK_DAYS": ("rotation_lookback_days", int, "Mom"),
    "ROTATION_BTC_MA_DAYS": ("rotation_btc_ma_days", int, "BTCort"),
    "ROTATION_TOP_N": ("rotation_top_n", int, "Coin"),
    # Başka bir strateji grubuna geçerken profille .env'e yazılacak satırlar (tabloda gösterilmez)
    "BREAKEVEN_TRIGGER_PCT": ("breakeven_trigger_pct", float, None),
    "BREAKEVEN_FLOOR_PCT": ("breakeven_floor_pct", float, None),
    "BACKUP_STOP_PCT": ("backup_stop_pct", float, None),
    "ROTATION_UNIVERSE": ("rotation_universe", int, None),
    "MR_BAND_STD": ("mr_band_std", float, "Bant"),
    "MR_UNIVERSE": ("mr_universe", int, None),
    "RSI_DIV_OVERSOLD": ("rsi_div_oversold", float, "RSIdip"),
    "RSI_DIV_UNIVERSE": ("rsi_div_universe", int, None),
    # Filtre gevşetme taraması (--tara-filtre); kendi tablosunda gösterilir
    "MIN_24H_DROP_PCT": ("min_24h_drop_pct", float, None),
    "MIN_24H_RANGE_PCT": ("min_24h_range_pct", float, None),
    "MIN_DIP_PCT": ("min_dip_pct", float, None),
    "MIN_RETRACE": ("min_retrace", float, None),
    "MAX_RETRACE": ("max_retrace", float, None),
    "RSI_OVERSOLD": ("rsi_oversold", float, None),
    "RSI_ENTRY_MIN": ("rsi_entry_min", float, None),
    "RSI_ENTRY_MAX": ("rsi_entry_max", float, None),
    "MIN_VOLUME_RATIO": ("min_volume_ratio", float, None),
    "MIN_ATR_PCT": ("min_atr_pct", float, None),
    "MAX_CANDIDATES": ("max_candidates", int, None),
    "SYMBOL_COOLDOWN_MINUTES": ("symbol_cooldown_minutes", int, None),
    "BTC_MAX_1H_DROP_PCT": ("btc_max_1h_drop_pct", float, None),
    # A/B filtre testi (--tara-ab)
    "BTC_FILTER": ("btc_filter", bool, None),
    "REGIME_FILTER": ("regime_filter", bool, None),
    "FAKEOUT_FILTER": ("fakeout_filter", bool, None),
    "VWAP_FILTER": ("vwap_filter", bool, None),
    "OVERHEAT_FILTER": ("overheat_filter", bool, None),
    "RSI_DIV_FILTER": ("rsi_div_filter", bool, None),
    "RSI_DIV_TREND_FILTER": ("rsi_div_trend_filter", bool, None),
    "LOSS_STREAK_LIMIT": ("loss_streak_limit", int, None),
    "DAILY_LOSS_LIMIT_PCT": ("daily_loss_limit_pct", float, None),
    "ATR_TARGETS": ("atr_targets", bool, None),
    "TP_EXTEND": ("tp_extend", bool, None),
    "TRAILING_BUY": ("trailing_buy", bool, None),
    "TIME_EXIT_MODE": ("time_exit_mode", str, None),
    "ESCAPE_MODE": ("escape_mode", str, None),
    "DCA_MODE": ("dca_mode", str, None),
    "PYRAMID_MODE": ("pyramid_mode", str, None),
    "CAPITAL_USAGE_PCT": ("capital_usage_pct", float, None),
}
# Filtre gevşetme taraması (rebound): her satırda TEK filtre bir adım gevşetilir. (.env anahtarı, yön: +1 = değeri
# büyütmek gevşetir / -1 = küçültmek gevşetir, adımlar hafiften güçlüye; mevcut değerden gevşek olan ilki denenir)
FILTER_STEPS: Tuple[Tuple[str, int, Tuple[Union[float, bool], ...]], ...] = (
    ("MIN_24H_DROP_PCT", -1, (1.0, 0.0)),        # Ön eleme: 24 saatte en az ne kadar düşmüş
    ("MIN_24H_RANGE_PCT", -1, (3.0, 2.0, 0.0)),  # Ön eleme: 24 saatlik fiyat aralığı
    ("MAX_CANDIDATES", +1, (25, 40)),            # Taramada incelenen aday sayısı
    ("MIN_DIP_PCT", -1, (2.5, 2.0, 1.5)),        # Son 3 saatte zirveden dibe düşüş
    ("MIN_RETRACE", -1, (0.10, 0.05)),           # Düşüşün en az ne kadarı geri alınmış
    ("MAX_RETRACE", +1, (0.75, 0.9)),            # ... en fazla ne kadarı
    ("RSI_OVERSOLD", +1, (40.0, 45.0)),          # Dipte görülmesi gereken RSI
    ("RSI_ENTRY_MIN", -1, (35.0, 30.0)),
    ("RSI_ENTRY_MAX", +1, (70.0, 75.0)),
    ("MIN_VOLUME_RATIO", -1, (0.8, 0.6, 0.0)),   # Hacim teyidi
    ("MIN_ATR_PCT", -1, (0.15, 0.1, 0.0)),       # Oynaklık alt sınırı
    ("SYMBOL_COOLDOWN_MINUTES", -1, (30, 15, 0)),
    ("BTC_MAX_1H_DROP_PCT", +1, (2.5, 4.0)),     # BTC filtresi
    ("MIN_SIGNAL_SCORE", -1, (0.0,)),
    ("MIN_ROOM_RATIO", -1, (0.0,)),
    ("REGIME_SKIP_WEAK", -1, (False,)),
)
ROTATION_EXIT_KEYS = ("STOP_LOSS_PCT", "ROTATION_DAYS")
ROTATION_ENTRY_KEYS = ("ROTATION_LOOKBACK_DAYS", "ROTATION_BTC_MA_DAYS", "ROTATION_TOP_N")
ROTATION_KEYS = ROTATION_EXIT_KEYS[1:] + ROTATION_ENTRY_KEYS + ("ROTATION_UNIVERSE",)
EXIT_KEYS = ("TAKE_PROFIT_PCT", "STOP_LOSS_PCT", "TRAILING_ACTIVATION_PCT", "TRAILING_FLOOR_PCT",
             "MAX_HOLD_MINUTES", "MIN_ROOM_RATIO")
ENTRY_KEYS = ("REGIME_SKIP_WEAK", "MAX_24H_DROP_PCT", "MIN_SIGNAL_SCORE", "MIN_ROOM_RATIO")
SWEEP_MAX_GAIN = (12.0, 6.0)          # trend / breakout: + .env'deki MAX_24H_GAIN_PCT


def _range_key(strategy: str) -> str:
    """Giriş taramasında stratejiye göre denenen eşik ayarı."""
    if strategy == "rebound":
        return "MAX_24H_DROP_PCT"
    if strategy == "rsi_divergence":
        return "RSI_DIV_OVERSOLD"
    if is_long_horizon(strategy):
        return "SWING_LOOKBACK_HOURS"
    return "MR_BAND_STD" if is_mean_reversion(strategy) else "MAX_24H_GAIN_PCT"


def entry_keys(cfg) -> Tuple[str, ...]:
    """Giriş taramasının değiştirdiği ayarlar: rebound düşüş aralığını, trend/breakout yükseliş aralığını,
    swing stratejileri zirve penceresini (SWING_LOOKBACK_HOURS), mean_revert aşırı satım bandını, rsi_divergence ilk
    dipteki RSI eşiğini."""
    if is_rotation(cfg.strategy):
        return ROTATION_ENTRY_KEYS
    range_key = _range_key(cfg.strategy)
    return tuple(range_key if k == "MAX_24H_DROP_PCT" else k for k in ENTRY_KEYS)

Settings = Dict[str, Union[float, int, bool, str]]
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
        return self.mean_net_pct - sweep_critical(tested, self.trades) * self.se_net_pct > 0

    @property
    def profitable(self) -> bool:
        """Hem toplam USDT hem işlem başı ortalama pozitif (pozisyon büyüklüğü değiştiği için ikisi ayrışabilir)."""
        return self.net > 0 and self.mean_net_pct > 0


def sweep_z(tested: int) -> float:
    """Denenen ayar sayısına göre (Bonferroni, iki yönlü %5) gereken z değeri: 1 ayar → 1.96, 60 → ~3.34."""
    from statistics import NormalDist
    return NormalDist().inv_cdf(1 - 0.05 / (2 * max(1, tested)))


def sweep_critical(tested: int, trades: int) -> float:
    """sweep_z'nin az işlem için Student-t karşılığı (Cornish-Fisher yaklaşımı, serbestlik = işlem - 1).

    Örn. 54 ayar denenmiş ve 30 işlem → ~3.69 (normal dağılımla 3.31 olurdu, fazla gevşek kalırdı).
    """
    z = sweep_z(tested)
    df = max(1, trades - 1)
    return z + (z ** 3 + z) / (4 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df ** 2)


def _current(cfg, keys: Sequence[str]) -> Settings:
    return {k: getattr(cfg, SWEEP_FIELDS[k][0]) for k in keys}


def _apply(cfg, settings: Settings):
    """Ayarları Config'e uygular; taramada kill-switch kapalıdır (eski USDT ayarı dahil)."""
    changes = {SWEEP_FIELDS[k][0]: SWEEP_FIELDS[k][1](v) for k, v in settings.items()}
    return replace(cfg, max_total_loss_pct=SWEEP_KILL_PCT, legacy_loss_usdt=None, **changes)


def sweep_combos(cfg, tps: Optional[Sequence[float]] = None, sls: Optional[Sequence[float]] = None,
                 trails: Optional[Sequence[float]] = None, holds: Optional[Sequence[int]] = None,
                 rooms: Optional[Sequence[float]] = None) -> List[Combo]:
    """Çıkış taraması kombinasyonları; ilk satır .env'deki mevcut ayarlardır.

    Verilmeyen listeler stratejinin vadesine göre seçilir (5 dk: TP %2-5 / süre 1-4 saat, swing: TP %8-15 / 2-5 gün,
    mean_revert / rsi_divergence: TP %1-2.5 / süre 12 saat-2 gün).
    Rotasyonda yalnızca felaket zarar kesi (--sl) ve yeniden dengeleme sıklığı denenir.
    """
    if is_rotation(cfg.strategy):
        base = _current(cfg, ROTATION_EXIT_KEYS)
        combos = [(base, True)]
        for sl in sls or ROTATION_SWEEP_SL:
            for days in ROTATION_SWEEP_DAYS:
                st = {"STOP_LOSS_PCT": float(sl), "ROTATION_DAYS": int(days)}
                if st != base:
                    combos.append((st, False))
        return combos
    swing, mr = is_long_horizon(cfg.strategy), is_mean_reversion(cfg.strategy)
    tps = tps or (SWING_SWEEP_TP if swing else MR_SWEEP_TP if mr else SWEEP_TP)
    sls = sls or (SWING_SWEEP_SL if swing else MR_SWEEP_SL if mr else SWEEP_SL)
    trails = trails or (SWING_SWEEP_TRAIL if swing else MR_SWEEP_TRAIL if mr else SWEEP_TRAIL)
    holds = holds or (SWING_SWEEP_HOLD if swing else MR_SWEEP_HOLD if mr else SWEEP_HOLD)
    rooms = rooms or SWEEP_ROOM
    base = _current(cfg, EXIT_KEYS)
    # Kırılımda "eski zirveye alan" ölçülmez; alan filtresi hiçbir şeyi değiştirmez (aynı satırlar iki kez denenmesin)
    rooms_for_strategy = tuple(rooms) if strategy_class(cfg.strategy).uses_room else (cfg.min_room_ratio,)
    combos: List[Combo] = [(base, True)]
    for tp in tps:
        for sl in sls:
            for trail in trails:
                if trail >= tp:      # İzleyen stop Kâr Al'dan önce başlamalı
                    continue
                for hold in holds:
                    for room in rooms_for_strategy:
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
                 rooms: Sequence[float] = SWEEP_ENTRY_ROOM, regime_available: bool = True) -> List[Combo]:
    """Giriş taraması kombinasyonları (çıkışlar .env'deki gibi); ilk satır mevcut ayarlardır.

    Piyasa rejimi filtresi kapalıysa (veya BTC verisi yoksa) REGIME_SKIP_WEAK hiçbir şeyi değiştirmez;
    o durumda aynı satırları iki kez denememek için yalnızca mevcut değeri kullanılır.
    """
    keys = entry_keys(cfg)
    base = _current(cfg, keys)
    if is_rotation(cfg.strategy):   # Sinyal puanı / alan / piyasa rejimi rotasyonda kullanılmaz
        combos = [(base, True)]
        for lookback in sorted({cfg.rotation_lookback_days, *ROTATION_SWEEP_LOOKBACK}):
            for ma in sorted({cfg.rotation_btc_ma_days, *ROTATION_SWEEP_MA}):
                for top in sorted({cfg.rotation_top_n, *ROTATION_SWEEP_TOP}):
                    st = {"ROTATION_LOOKBACK_DAYS": lookback, "ROTATION_BTC_MA_DAYS": ma, "ROTATION_TOP_N": top}
                    if st != base:
                        combos.append((st, False))
        return combos
    range_key = _range_key(cfg.strategy)
    if range_key == "MAX_24H_DROP_PCT":
        ranges = sorted({cfg.max_24h_drop_pct, *(d for d in max_drops if d > cfg.min_24h_drop_pct)}, reverse=True)
    elif range_key == "SWING_LOOKBACK_HOURS":
        ranges = sorted({cfg.swing_lookback_hours, *SWING_SWEEP_LOOKBACK})
    elif range_key == "MR_BAND_STD":
        ranges = sorted({cfg.mr_band_std, *MR_SWEEP_BAND})
    elif range_key == "RSI_DIV_OVERSOLD":
        ranges = sorted({cfg.rsi_div_oversold, *DIV_SWEEP_OVERSOLD})
    else:
        ranges = sorted({cfg.max_24h_gain_pct, *(g for g in SWEEP_MAX_GAIN if g > cfg.min_24h_gain_pct)},
                        reverse=True)
    skips = (False, True) if cfg.regime_filter and regime_available else (bool(cfg.regime_skip_weak),)
    if not strategy_class(cfg.strategy).uses_room:   # Kırılımda "eski zirveye alan" ölçülmez; filtre etkisiz
        rooms = (cfg.min_room_ratio,)
    combos: List[Combo] = [(base, True)]
    for skip in skips:
        for rng in ranges:
            for score in scores:
                for room in rooms:
                    st = {"REGIME_SKIP_WEAK": skip, range_key: rng, "MIN_SIGNAL_SCORE": score,
                          "MIN_ROOM_RATIO": room}
                    if st != base:
                        combos.append((st, False))
    return combos


def comparable_strategies(strategy: str) -> List[str]:
    """Aynı veriyle (mum aralığı) ve aynı çıkış mantığıyla (strateji grubu) çalışan stratejiler: 5 dk olanlar ya da
    swing olanlar. Farklı çıkış mantıklı stratejileri (örn. mean_revert) --turnuva kendi çıkışlarıyla karşılaştırır."""
    interval, family = strategy_class(strategy).interval, strategy_family(strategy)
    return [s for s in STRATEGIES if strategy_class(s).interval == interval and strategy_family(s) == family]


def strategy_combos(cfg) -> List[Combo]:
    """Strateji karşılaştırması: aynı mum aralığındaki her strateji .env'deki çıkış ayarlarıyla; ilk satır mevcut strateji.

    MIN_SIGNAL_SCORE ve MIN_ROOM_RATIO stratejiye özeldir (puan ölçekleri ve "alan" tanımı farklıdır;
    örn. trend'de alan en fazla geri çekilme kadar olabilir). Rebound için seçilmiş bir değer diğer
    stratejileri haksız yere eleyebileceği için karşılaştırmada ikisi de kapalıdır (0).
    5 dk ve 1 saatlik stratejiler farklı veriyle çalıştığından birlikte karşılaştırılmaz.
    """
    order = [cfg.strategy] + [s for s in comparable_strategies(cfg.strategy) if s != cfg.strategy]
    return [({"STRATEGY": s, "MIN_SIGNAL_SCORE": 0.0, "MIN_ROOM_RATIO": 0.0}, s == cfg.strategy) for s in order]


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
    """Giriş taraması: önce mevcut ayarlar çalışır (puan eşikleri YALNIZCA onun sinyallerinden çıkar; önceki
    bir --tara'nın önbelleği eşikleri değiştirmez), sonra diğerleri."""
    cache = signal_cache if signal_cache is not None else {}
    own: dict = {}
    base = run_sweep(cfg, data, spread_pct, None, [(_current(cfg, entry_keys(cfg)), True)], own)
    thresholds = score_thresholds(own)
    cache.update(own)
    combos = entry_combos(cfg, thresholds, regime_available=data.regime is not None)[1:]
    return base + run_sweep(cfg, data, spread_pct, progress, combos, cache)


def _valid(cfg) -> bool:
    try:
        cfg.validate()
    except ConfigError:
        return False
    return True


def filter_combos(cfg) -> List[Combo]:
    """Filtre gevşetme taraması: ilk satır mevcut ayarlar, sonra her filtre için tek adımlık gevşetme, en sonda
    hepsi birden. Zaten kapalı / gevşek olan veya bu ayarlarla anlamsız olan filtreler atlanır."""
    combos: List[Combo] = [({}, True)]
    together: Settings = {}
    for key, direction, steps in FILTER_STEPS:
        attr, kind = SWEEP_FIELDS[key][0], SWEEP_FIELDS[key][1]
        if key == "BTC_MAX_1H_DROP_PCT" and not cfg.btc_filter:
            continue
        if key == "REGIME_SKIP_WEAK" and not cfg.regime_filter:
            continue
        current = getattr(cfg, attr)
        for step in steps:
            value = kind(step)
            if (value - current) * direction <= 0:
                continue   # Mevcut değerden gevşek değil
            if not _valid(replace(cfg, **{attr: value})):
                continue
            combos.append(({key: value}, False))
            if _valid(_apply(cfg, {**together, key: value})):
                together[key] = value
            break
    if len(together) > 1:
        combos.append((together, False))   # Hepsi birden (her filtrenin ilk adımı)
    return combos


def _prefilter_all(data: BacktestData, cfg) -> None:
    """24s ön elemesi (MIN_24H_RANGE_PCT) veriyle birlikte hesaplanır: değişince yeniden hesaplanır."""
    for s in data.series.values():
        s.change24 = [None] * len(s.change24)
        s.prepare_prefilter(cfg)


def run_filter_sweep(cfg, data: BacktestData, spread_pct: float = 0.05, progress: Progress = None,
                     signal_cache: Optional[dict] = None) -> List[SweepRow]:
    """Her filtreyi tek tek gevşetip aynı veriyle simüle eder (çıkışlar .env'deki gibi)."""
    say = progress or (lambda _msg: None)
    cache = signal_cache if signal_cache is not None else {}
    combos = filter_combos(cfg)
    rows: List[SweepRow] = []
    prepared = cfg.min_24h_range_pct
    try:
        for i, (settings, is_base) in enumerate(combos, 1):
            run_cfg = _apply(cfg, settings)
            if run_cfg.min_24h_range_pct != prepared:
                _prefilter_all(data, run_cfg)
                prepared = run_cfg.min_24h_range_pct
            res = simulate(run_cfg, data, spread_pct, None, signal_cache=cache)
            sm = summarize(res.trades)
            first, second = split_halves(res.trades, res.start_ms, res.end_ms)
            rows.append(SweepRow(dict(settings), is_base, sm.count, sm.win_rate, sm.net, sm.mean_net_pct,
                                 res.max_drawdown_pct, sum(t.pnl for t in first), sum(t.pnl for t in second),
                                 sm.profit_factor, sm.se_net_pct))
            say(f"   ⏳ Filtre taraması {i}/{len(combos)} ayar denendi")
    finally:
        if prepared != cfg.min_24h_range_pct:
            _prefilter_all(data, cfg)   # Aynı veriyle sonraki taramalar .env'deki ön elemeyi görsün
    return rows


def _filter_label(cfg, settings: Settings) -> str:
    if not settings:
        return "mevcut ayarlar"
    if len(settings) > 1:
        return "hepsi birden (ilk adımlar)"
    (key, value), = settings.items()
    return f"{key} {_fmt_setting(key, getattr(cfg, SWEEP_FIELDS[key][0]))} → {_fmt_setting(key, value)}"


def format_filter_report(rows: Sequence[SweepRow], cfg, data: BacktestData, spread_pct: float = 0.05) -> str:
    """Filtre gevşetme raporu: hangi filtre işlem sayısını ne kadar artırıyor, işlem başı sonucu ne oluyor."""
    q = cfg.quote_asset
    base = next(r for r in rows if r.baseline)
    days = max(data.days, 1e-9)
    width = max([24] + [len(_filter_label(cfg, r.settings)) for r in rows])
    label = INTERVAL_LABELS.get(data.interval, data.interval)
    lines = [
        f"🧪 FİLTRE GEVŞETME TARAMASI — strateji: {cfg.strategy} (her satırda TEK filtre gevşetildi; çıkışlar sabit)",
        LINE,
        f"🗓️ Dönem: {_fmt_time(data.sim_start_ms)} → {_fmt_time(data.end_ms)} ({data.days:g} gün, {label} mumlar) | "
        f"{len(data.series)} sembol | sermaye {fmt_usdt(cfg.start_capital)} {q} | komisyon {fmt_rate(cfg.fee_rate)} "
        f"| spread %{spread_pct:g}",
        f"⚙️ Çıkışlar (tüm satırlarda): {_exit_summary(cfg)}",
        "   (Karşılaştırma adil olsun diye taramada kill-switch kapalıdır; maks. düşüşe bakın.)",
        LINE,
        f"   {'Gevşetilen ayar':<{width}} | İşlem  Fark  Gün.  Kaz%  İşl.başı   Net USDT  MaksDD |  1.yarı  2.yarı "
        f"Yarı",
    ]
    more: List[SweepRow] = []     # İşlem sayısını artıranlar
    better: List[SweepRow] = []   # ... yeterli işlemle, işlem başı sonucu ve iki yarının net sonucunu bozmadan
    mixed: List[SweepRow] = []    # ... işlem başı bozulmadı ama bir yarıda net sonuç kötüleşti
    worse: List[SweepRow] = []    # ... yeterli işlemle ama işlem başı sonucu kötüleştirerek
    # Mevcut ayar çok az işlem açıyorsa onun işlem başı sonucu şanstır: kıyas sıfıra (kâr / zarar sınırına) yapılır
    reference = base.mean_net_pct if base.enough_trades else 0.0
    min_diff = max(1, round(base.trades * FILTER_MIN_MORE_PCT / 100))   # +1-2 işlem "artış" sayılmaz
    for r in rows:
        diff = r.trades - base.trades
        halves = "" if r.baseline else _halves(r.first_net - base.first_net, r.second_net - base.second_net)
        if r.baseline:
            mark = "⭐"
        elif diff < min_diff:
            mark = "· "
        elif not r.enough_trades:
            mark = "? "
            more.append(r)
        elif r.mean_net_pct < reference:
            mark = "🔴"
            more.append(r)
            worse.append(r)
        elif "-" not in halves:
            mark = "🟢"
            more.append(r)
            better.append(r)
        else:
            mark = "🟡"
            more.append(r)
            mixed.append(r)
        lines.append(
            f"{mark} {_filter_label(cfg, r.settings):<{width}} | {r.trades:>5d} {diff:>+5d} {r.trades / days:>5.1f} "
            f"{r.win_rate:>5.1f} {r.mean_net_pct:>+8.2f}% {r.net:>+10.4f} {r.max_dd_pct:>6.1f}% | "
            f"{r.first_net:>+7.3f} {r.second_net:>+7.3f} {halves:>4}")
    lines += [
        "   ⭐ = .env'deki mevcut ayarlarınız | Fark = mevcut ayara göre işlem farkı | Gün. = günde ortalama işlem",
        "   Yarı = net K/Z farkı dönemin 1. ve 2. yarısında (+ arttı, - azaldı)",
        "   🟢 = daha çok işlem; işlem başı sonuç da, iki yarının net sonucu da kötüleşmedi",
        "   🟡 = daha çok işlem, işlem başı kötüleşmedi ama bir yarıda net zarar arttı (tutarsız, şans olabilir)",
        "   🔴 = daha çok işlem ama işlem başı sonuç kötüleşti",
        f"   ? = {MIN_TRADES_FOR_EVIDENCE}'dan az işlem (sonuç şans olabilir) | · = işlem sayısı belirgin artmadı "
        f"(%{FILTER_MIN_MORE_PCT:g}'den az; bu filtre şu an işlemleri kısıtlamıyor)",
        "   İşl.başı = işlem başına ortalama net K/Z (komisyon dahil)",
        LINE,
    ]
    if not base.enough_trades:
        lines.append(f"ℹ️ Mevcut ayarlarla yalnızca {base.trades} işlem var: işlem başı sonucu şans olabileceği için "
                     "satırlar sıfırla (kâr / zarar sınırı) kıyaslandı.")
    if not more:
        lines.append("ℹ️ Hiçbir filtre tek başına işlem sayısını artırmadı. İşlemleri kısıtlayan şey filtreler değil;")
        lines.append("   pozisyon yeri (MAX_OPEN_POSITIONS / sermaye) veya piyasa koşulları olabilir.")
    else:
        top = sorted(more, key=lambda r: r.trades, reverse=True)[:3]
        lines.append("📈 İşlem sayısını en çok artıranlar: " + ", ".join(
            f"{_filter_label(cfg, r.settings)} ({r.trades - base.trades:+d})" for r in top))
    combined = next((r for r in rows if len(r.settings) > 1), None)
    single = sum(max(0, r.trades - base.trades) for r in rows if len(r.settings) == 1)
    if combined is not None and combined.trades - base.trades > 2 * max(single, 1):
        lines += [f"🔗 Filtreler birbirini örtüyor: tek tek gevşetmek toplam {single:+d} işlem getirirken hepsi birden "
                  f"{combined.trades - base.trades:+d} getiriyor.",
                  "   Bir sinyal birden çok filtreye takılıyor; tek filtre gevşetilince yine diğerine takılır."]
    if base.mean_net_pct < 0:
        lines += [
            f"❗ Mevcut ayarlarla işlem başı sonuç {fmt_signed(base.mean_net_pct)}% (negatif): bu durumda işlem",
            "   sayısını artırmak toplam zararı da artırır. Daha çok işlem ancak işlem başı sonuç da sıfırın üstüne",
            "   çıkıyorsa kâr getirir.",
        ]
    if mixed:
        lines.append("🟡 Tutarsız (dönemin bir yarısında net zararı artırdı; önerilmez): " + ", ".join(
            f"{_filter_label(cfg, r.settings)} ({r.trades - base.trades:+d} işlem)" for r in mixed))
    if better:
        best = max(better, key=lambda r: r.mean_net_pct)
        lines.append("🟢 İşlem sayısını artırıp işlem başı sonucu ve iki yarının net sonucunu bozmayan filtreler "
                     "(tek tek denendi):")
        for r in sorted(better, key=lambda r: r.mean_net_pct, reverse=True):
            lines.append(f"   {_filter_label(cfg, r.settings)}: {r.trades - base.trades:+d} işlem, işlem başı "
                         f"{fmt_signed(r.mean_net_pct)}% → .env: " + "  ".join(
                             f"{k}={_fmt_setting(k, v)}" for k, v in r.settings.items()))
        lines += [
            "   Fark küçükse şans olabilir. .env'yi değiştirmeden önce en iyisini daha önceki bir dönemde sınayın:",
            f"   python main.py --backtest --gun {data.days:g} --strateji {cfg.strategy} --bitis "
            f"{_fmt_date(data.sim_start_ms)} " + " ".join(
                f"--ayar {k}={_fmt_setting(k, v)}" for k, v in best.settings.items()),
        ]
        if not best.profitable:
            lines.append("   Not: bu ayarlarla da toplamda zarar var; yalnızca işlem başı zarar büyümüyor.")
    elif worse:
        if base.enough_trades:
            lines.append("🔴 Yeterli işlemle ölçülebilen her gevşetme işlem başı sonucu kötüleştiriyor: filtreler kötü")
        else:
            lines.append("🔴 Yeterli işlemle ölçülebilen her gevşetmede işlem başı sonuç zararda: filtreler kötü")
        lines.append("   sinyalleri eliyor. Gevşetirseniz daha çok ama daha zararlı işlem beklenir.")
    elif more and not mixed:
        lines.append(f"? İşlem sayısını artıran gevşetmeler {MIN_TRADES_FOR_EVIDENCE}'dan az işlem açtı: etkileri bu veriyle "
                     "ölçülemiyor; daha uzun bir dönem deneyin (--gun 180).")
    lines.append(f"🔁 Aynı taramayı önceki {data.days:g} günde yapmak için: python main.py --backtest --gun "
                 f"{data.days:g} --tara-filtre --strateji {cfg.strategy} --bitis {_fmt_date(data.sim_start_ms)}")
    lines.append(f"📝 Backtest en hacimli {len(data.series)} coinle yapılır; canlı bot daha çok coine bakar ve 30 sn'de "
                 "bir tarar, işlem sayıları canlıda farklı olabilir. Kayma yok, geçmiş sonuçlar geleceği garanti etmez.")
    return "\n".join(lines)


# ================================================================ A/B filtre testi (--tara-ab)
# Her satırda TEK filtre / sigorta mevcut durumunun tersine çevrilir (açıksa kapatılır, kapalıysa açılır); çıkışlar
# ve diğer ayarlar .env'deki gibi kalır. (.env anahtarı, rapordaki ad, "açık" değeri, "kapalı" değeri)
AB_TESTS: Tuple[Tuple[str, str, Union[bool, int, float], Union[bool, int, float]], ...] = (
    ("BTC_FILTER", "BTC sigortası", True, False),
    ("REGIME_FILTER", "Piyasa rejimi filtresi", True, False),
    ("FAKEOUT_FILTER", "Sahte yükseliş (üst fitil)", True, False),
    ("VWAP_FILTER", "VWAP filtresi", True, False),
    ("OVERHEAT_FILTER", "Aşırı ısınma", True, False),
    ("LOSS_STREAK_LIMIT", "Zarar serisi sigortası", 3, 0),
    ("DAILY_LOSS_LIMIT_PCT", "Günlük zarar limiti", 3.0, 0.0),
    # Akıllı yönetim: "sorarak" (ask) modu geçmiş veriyle denenemez; "otomatik" hali denenir
    ("ATR_TARGETS", "ATR hedefleri", True, False),
    ("TP_EXTEND", "Hedef uzatma", True, False),
    ("TRAILING_BUY", "Kademeli alım", True, False),
    ("TIME_EXIT_MODE", "Süre kuralı (başabaşta sat)", "auto", "off"),
    ("ESCAPE_MODE", "Erken kaçış", "auto", "off"),
    ("RSI_DIV_FILTER", "RSI uyumsuzluğu teyidi", True, False),
    ("RSI_DIV_TREND_FILTER", "Uyumsuzlukta trend filtresi", True, False),
)
# Yalnızca bir stratejide anlamlı A/B satırları (diğerlerinde hiçbir şeyi değiştirmez ya da hep aynı sonucu verir)
AB_ONLY_FOR = {"RSI_DIV_TREND_FILTER": frozenset({"rsi_divergence"})}
AB_NOT_FOR = {"RSI_DIV_FILTER": frozenset({"rsi_divergence"})}   # Strateji zaten uyumsuzlukla alıyor
# Yedek akçe gerektirenler: ilk giriş %100 ise yedek %50 ile denenir (ve kıyas için yalnızca yedek %50)
AB_RESERVE_TESTS = (("DCA_MODE", "DCA"), ("PYRAMID_MODE", "Piramit"))
AB_RESERVE_PCT = 50.0
AB_MIN_EFFECT_PCT = 0.5   # Net kâr farkı başlangıç sermayesinin bu %'sinden küçükse "önemsiz" (öneri yapılmaz)
FILTER_MIN_MORE_PCT = 1.0  # Filtre gevşetme: işlem sayısı en az bu % artmalı (+1-2 işlem "artış" sayılmaz)
AB_CANDLE_KEYS = ("FAKEOUT_FILTER", "VWAP_FILTER", "OVERHEAT_FILTER")
AB_LIVE_ONLY = "Satış duvarı radarı, emir defteri dengesizliği ve tape teyidi"


def _ab_state(key: str, value) -> str:
    if isinstance(value, bool):
        return "açık" if value else "kapalı"
    if isinstance(value, str):
        return {"off": "kapalı", "ask": "sorarak", "auto": "otomatik"}.get(value, value)
    if not value:
        return "kapalı"
    return f"{value:g} SL" if key == "LOSS_STREAK_LIMIT" else f"-%{value:g}"


def _ab_flip(current, on, off):
    """Mevcut durumun tersi: açık / otomatik ise kapalı, değilse açık / otomatik ("sorarak" kapalı sayılır)."""
    if isinstance(on, str):
        return off if current == on else on
    return off if current else on


def ab_combos(cfg, regime_available: bool = True) -> List[Combo]:
    """İlk satır mevcut ayarlar; sonra her filtre tek tek ters çevrilir; en sonda üç mum filtresi birden açık."""
    combos: List[Combo] = [({}, True)]
    for key, _label, on, off in AB_TESTS:
        attr = SWEEP_FIELDS[key][0]
        current = getattr(cfg, attr)
        if key == "REGIME_FILTER" and not (current and regime_available):
            continue   # Rejim verisi yalnızca filtre açıkken hazırlanır: kapalıyken "açık" hali denenemez
        if is_rotation(cfg.strategy) and key != "REGIME_FILTER":
            continue
        if cfg.strategy not in AB_ONLY_FOR.get(key, (cfg.strategy,)) or cfg.strategy in AB_NOT_FOR.get(key, ()):
            continue
        value = _ab_flip(current, on, off)
        if _valid(replace(cfg, **{attr: SWEEP_FIELDS[key][1](value)})):
            combos.append(({key: value}, False))
    if not is_rotation(cfg.strategy):
        together = {k: True for k in AB_CANDLE_KEYS if not getattr(cfg, SWEEP_FIELDS[k][0])}
        if len(together) > 1:
            combos.append((together, False))
        reserve = {} if cfg.capital_usage_pct < 100 else {"CAPITAL_USAGE_PCT": AB_RESERVE_PCT}
        if reserve:
            combos.append((dict(reserve), False))   # Kıyas: yalnızca yedek akçe (ekleme yok)
        for key, _label in AB_RESERVE_TESTS:
            value = _ab_flip(getattr(cfg, SWEEP_FIELDS[key][0]), "auto", "off")
            settings = {**reserve, key: value} if value == "auto" else {key: value}
            if _valid(_apply(cfg, settings)):
                combos.append((settings, False))
    return combos


def run_ab_test(cfg, data: BacktestData, spread_pct: float = 0.05, progress: Progress = None,
                signal_cache: Optional[dict] = None) -> List[SweepRow]:
    """A/B filtre testi: her filtre açık / kapalı aynı veriyle simüle edilir (sinyal analizi önbellekten)."""
    return run_sweep(cfg, data, spread_pct, progress, ab_combos(cfg, data.regime is not None), signal_cache)


def _ab_label(cfg, settings: Settings) -> str:
    if not settings:
        return "mevcut ayarlar"
    names = {k: label for k, label, _on, _off in AB_TESTS}
    names.update(dict(AB_RESERVE_TESTS))
    parts = []
    usage = settings.get("CAPITAL_USAGE_PCT")
    if usage is not None:
        parts.append(f"Yedek akçe %{100 - usage:g}")
    for key, value in settings.items():
        if key == "CAPITAL_USAGE_PCT":
            continue
        if key in AB_CANDLE_KEYS and len(settings) > 1:
            return "3 mum filtresi birden açık"
        parts.append(f"{names[key]}: {_ab_state(key, getattr(cfg, SWEEP_FIELDS[key][0]))} → {_ab_state(key, value)}")
    return " + ".join(parts)


def _ab_reference(row: SweepRow, base: SweepRow, reserve: Optional[SweepRow]) -> SweepRow:
    """Satırın kıyaslandığı satır: yedek akçeyle denenen DCA / piramit, yalnızca yedek akçe satırıyla kıyaslanır
    (yedek akçe pozisyonları küçülttüğü için mevcut ayarla farkın çoğu eklemeden değil, küçülmeden gelir)."""
    if reserve is not None and row is not reserve and "CAPITAL_USAGE_PCT" in row.settings:
        return reserve
    return base


def _halves(d_first: float, d_second: float) -> str:
    return "".join("=" if abs(d) < 1e-9 else "+" if d > 0 else "-" for d in (d_first, d_second))


def format_ab_report(rows: Sequence[SweepRow], cfg, data: BacktestData, spread_pct: float = 0.05) -> str:
    """A/B raporu: her filtrenin kazanma oranına ve net kâra etkisi.

    Yalnızca dönemin İKİ yarısında da kârı artıran değişiklik önerilir: tek bir yarıdaki fark çoğu zaman şanstır
    (aynı filtre başka bir dönemde ters sonuç verebilir).
    """
    q = cfg.quote_asset
    base = next(r for r in rows if r.baseline)
    reserve = next((r for r in rows if r.settings == {"CAPITAL_USAGE_PCT": AB_RESERVE_PCT}), None)
    width = max([26] + [len(_ab_label(cfg, r.settings)) for r in rows])
    label = INTERVAL_LABELS.get(data.interval, data.interval)
    lines = [
        f"🧪 A/B FİLTRE TESTİ — strateji: {cfg.strategy} (her satırda TEK filtre açıldı / kapatıldı; çıkışlar sabit)",
        LINE,
        f"🗓️ Dönem: {_fmt_time(data.sim_start_ms)} → {_fmt_time(data.end_ms)} ({data.days:g} gün, {label} mumlar) | "
        f"{len(data.series)} sembol | sermaye {fmt_usdt(cfg.start_capital)} {q} | komisyon {fmt_rate(cfg.fee_rate)} "
        f"| spread %{spread_pct:g}",
        f"⚙️ Çıkışlar (tüm satırlarda): {_exit_summary(cfg)}",
        "   (Karşılaştırma adil olsun diye kill-switch kapalıdır; maks. düşüşe bakın.)",
        LINE,
        f"   {'Değişen filtre':<{width}} | İşlem  Fark  Kaz%  ΔKaz  İşl.başı   Net USDT     ΔNet Yarı  MaksDD",
    ]
    better: List[SweepRow] = []
    mixed: List[SweepRow] = []
    worse: List[SweepRow] = []
    for r in rows:
        ref = _ab_reference(r, base, reserve)
        d_net = r.net - ref.net
        halves = _halves(r.first_net - ref.first_net, r.second_net - ref.second_net)
        if r.baseline:
            mark, halves = "⭐", ""
        elif r is reserve:
            mark = "⚪"
        elif r.trades == ref.trades and abs(d_net) < 1e-9:
            mark = "· "
        elif not r.enough_trades:
            mark = "? "
        elif abs(d_net) < cfg.start_capital * AB_MIN_EFFECT_PCT / 100:
            mark = "≈ "
        elif d_net > 0 and r.mean_net_pct >= ref.mean_net_pct and halves == "++":
            mark = "🟢"
            better.append(r)
        elif d_net < 0:
            mark = "🔴"
            worse.append(r)
        else:
            mark = "🟡"
            mixed.append(r)
        lines.append(
            f"{mark} {_ab_label(cfg, r.settings):<{width}} | {r.trades:>5d} {r.trades - ref.trades:>+5d} "
            f"{r.win_rate:>5.1f} {r.win_rate - ref.win_rate:>+5.1f} {r.mean_net_pct:>+8.2f}% {r.net:>+10.4f} "
            f"{d_net:>+8.4f} {halves:>4} {r.max_dd_pct:>6.1f}%")
    lines += [
        "   ⭐ = .env'deki mevcut ayarlarınız | Fark / ΔKaz / ΔNet = mevcut ayara göre işlem sayısı, kazanma oranı",
        "   (puan) ve net kâr farkı" + (f"; DCA / piramit: \"Yedek akçe %{100 - AB_RESERVE_PCT:g}\" satırına göre"
                                     if reserve is not None else "") + " | Yarı = ΔNet dönemin 1. ve 2. yarısında",
        "   🟢 = net kâr arttı, işlem başı kötüleşmedi ve İKİ yarıda da arttı | 🟡 = arttı ama tutarsız (şans olabilir)",
        f"   🔴 = net kâr azaldı | ≈ = fark önemsiz (sermayenin %{AB_MIN_EFFECT_PCT:g}'inden az) | ? = "
        f"{MIN_TRADES_FOR_EVIDENCE}'dan az işlem",
        "   · = sonuç değişmedi (filtre hiçbir sinyale takılmadı)" + (" | ⚪ = kıyas satırı" if reserve is not None
                                                                     else ""),
        LINE,
    ]

    def advice(r: SweepRow) -> str:
        ref = _ab_reference(r, base, reserve)
        return f"   {_ab_label(cfg, r.settings)} ({r.net - ref.net:+.4f} {q}) → " + "  ".join(
            f"{k}={_fmt_setting(k, v)}" for k, v in r.settings.items())
    if better:
        lines.append("🟢 Dönemin iki yarısında da kârı artıranlar (.env'ye yazmadan önce aşağıdaki komutla daha önceki "
                     "bir dönemde de doğrulayın):")
        lines += [advice(r) for r in sorted(better, key=lambda r: r.net, reverse=True)]
    else:
        lines.append("ℹ️ Hiçbir değişiklik net kârı dönemin iki yarısında da (yeterli işlemle) artırmadı.")
    if mixed:
        lines.append("🟡 Tutarsız, ayar değiştirmek için yeterli değil (bir yarıda zarar ettirdi ya da işlem başı "
                     "sonucu kötüleştirdi): " + ", ".join(_ab_label(cfg, r.settings) for r in mixed))
    if worse:
        lines.append("🔴 Kârı azaltanlar: " + ", ".join(_ab_label(cfg, r.settings) for r in worse))
    lines.append(f"🔁 Aynı testi önceki dönemde yapmak için: python main.py --backtest --gun {data.days:g} --tara-ab "
                 f"--bitis {_fmt_date(data.sim_start_ms)}")
    if reserve is not None:
        lines.append(f"⚪ Yedek akçe %{100 - AB_RESERVE_PCT:g} kendi başına kâr artırmaz: pozisyonları küçültür (zarar eden "
                     "stratejide zararı, kâr edende kârı azaltır) ve küçük pozisyonda LOT adımı büyük coinler (örn. "
                     "BTC) toz kontrolüne takılıp elenebilir. DCA / piramit satırları bu satırla kıyaslanır.")
    if base.mean_net_pct < 0 and not any(r.mean_net_pct > 0 for r in better):
        lines.append(f"❗ Mevcut ayarlarla işlem başı sonuç {fmt_signed(base.mean_net_pct)}%: filtreler zararı azaltabilir "
                     "ama stratejiyi kârlı yapmıyor (bkz. --turnuva).")
    lines.append(f"📝 {AB_LIVE_ONLY} geçmiş emir defteri verisi olmadığı için backtest edilemez; test modunda canlı "
                 "verilerle /tarama raporundaki elenme sayılarına bakın.")
    lines.append("📝 Akıllı yönetimin \"sorarak\" modu (Telegram'dan sorar) geçmiş veriyle denenemez; tabloda sormadan "
                 "uygulanan \"otomatik\" hali var. DCA / piramit yedek akçe ister: ilk giriş %100 ise yedek "
                 f"%{AB_RESERVE_PCT:g} ile denenir. Ekleme ve süre kuralları mum kapanışında, kademeli alım mumlarla "
                 "yaklaşık simüle edilir.")
    lines.append("📝 Günlük zarar limiti backtest'te UTC gün sınırıyla, canlıda yerel saatle hesaplanır. Kayma yok, "
                 "geçmiş sonuçlar geleceği garanti etmez.")
    return "\n".join(lines)


# ================================================================ strateji turnuvası (--turnuva)
# Aynı dönemde farklı giriş stratejileri × çıkış profilleri × piyasa filtresi (1 saatlik veri) + momentum rotasyonu
# (günlük veri). Her satır kendi çıkışlarıyla denenir; amaç "işlem başı kârı pozitif, mümkünse kazanma oranı yüksek"
# bir birleşim bulmaktır. Kazanma oranı tek başına yetmez: küçük kazançları büyük kayıplar silebilir.
TOURNAMENT_ENTRIES = ("mean_revert", "rsi_divergence", "swing_pullback", "swing")
_EXIT_PROFILE_KEYS = ("TAKE_PROFIT_PCT", "STOP_LOSS_PCT", "TRAILING_ACTIVATION_PCT", "TRAILING_FLOOR_PCT",
                      "MAX_HOLD_MINUTES", "BREAKEVEN_TRIGGER_PCT", "BREAKEVEN_FLOOR_PCT")
TOURNAMENT_EXITS: Dict[str, Settings] = {
    "küçük hedef": {k: PROFILES["mr"][k] for k in _EXIT_PROFILE_KEYS},      # = mean_revert'in önerilen çıkışları
    "dengeli": {"TAKE_PROFIT_PCT": 3.0, "STOP_LOSS_PCT": 3.0, "TRAILING_ACTIVATION_PCT": 2.0,
                "TRAILING_FLOOR_PCT": 1.0, "MAX_HOLD_MINUTES": 4320, "BREAKEVEN_TRIGGER_PCT": 1.5,
                "BREAKEVEN_FLOOR_PCT": 0.3},
    "trend": {k: PROFILES["swing"][k] for k in _EXIT_PROFILE_KEYS},          # = swing'in önerilen çıkışları
}
TOURNAMENT_EXIT_NOTES = {
    "küçük hedef": "Kâr Al +%1.5, Zarar Kes -%5, en fazla 2 gün (kazanma oranı yüksek olsun diye)",
    "dengeli": "Kâr Al +%3, Zarar Kes -%3, en fazla 3 gün",
    "trend": "Kâr Al +%12, Zarar Kes -%6, izleyen stop +%8 → +%4, en fazla 5 gün (az ama büyük kazanç)",
}
TOURNAMENT_ROTATIONS: Dict[str, Settings] = {
    "haftalık rotasyon": {},                                   # .env'deki ROTATION_* ayarları
    "en hacimli coin trendi": {"ROTATION_TOP_N": 1, "ROTATION_UNIVERSE": 1},   # Genelde BTC: trend varken tut
}


@dataclass
class TournamentRow:
    entry: str               # Strateji (giriş)
    exit: str                # Çıkış profili ya da rotasyon türü
    skip_weak: Optional[bool]  # Zayıf piyasada alım yok mu? (rotasyonda None: kendi BTC filtresi var)
    row: SweepRow
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0


@dataclass
class TournamentResult:
    rows: List[TournamentRow]
    data: BacktestData                       # 1 saatlik veri (dönem, BTC kıyası)
    rotation_data: Optional[BacktestData]
    notes: List[str] = field(default_factory=list)


def _tournament_row(cfg, data: BacktestData, settings: Settings, spread_pct: float, cache: dict,
                    entry: str, exit_name: str, skip_weak: Optional[bool]) -> TournamentRow:
    res = simulate(_apply(cfg, settings), data, spread_pct, None, signal_cache=cache)
    sm = summarize(res.trades)
    first, second = split_halves(res.trades, res.start_ms, res.end_ms)
    wins = [t.pnl_pct for t in res.trades if t.pnl >= 0]
    losses = [t.pnl_pct for t in res.trades if t.pnl < 0]
    row = SweepRow(dict(settings), False, sm.count, sm.win_rate, sm.net, sm.mean_net_pct, res.max_drawdown_pct,
                   sum(t.pnl for t in first), sum(t.pnl for t in second), sm.profit_factor, sm.se_net_pct)
    return TournamentRow(entry, exit_name, skip_weak, row, sum(wins) / len(wins) if wins else 0.0,
                         sum(losses) / len(losses) if losses else 0.0)


def run_tournament(cfg, client, days: float = 365, max_symbols: int = 40, spread_pct: float = 0.05,
                   progress: Progress = print, end_time_ms: Optional[int] = None,
                   cache_dir: Optional[Union[str, Path]] = DATA_DIR / "backtest_cache") -> TournamentResult:
    """1 saatlik veride giriş × çıkış × piyasa filtresi birleşimlerini, günlük veride rotasyonu aynı dönemde dener."""
    say = progress or (lambda _msg: None)
    hour_cfg = replace(cfg, strategy=TOURNAMENT_ENTRIES[0])
    data = prepare_backtest(hour_cfg, client, days=days, max_symbols=max_symbols, cache_dir=cache_dir,
                            progress=say, end_time_ms=end_time_ms)
    skips = (False, True) if data.regime is not None else (bool(cfg.regime_skip_weak),)
    common: Settings = {"MIN_SIGNAL_SCORE": 0.0, "MIN_ROOM_RATIO": 0.0,
                        "MAX_24H_GAIN_PCT": max(cfg.max_24h_gain_pct, 25.0)}
    plan = [(entry, name, skip) for entry in TOURNAMENT_ENTRIES for name in TOURNAMENT_EXITS for skip in skips]
    rows: List[TournamentRow] = []
    cache: dict = {}
    for i, (entry, name, skip) in enumerate(plan, 1):
        settings = {"STRATEGY": entry, **TOURNAMENT_EXITS[name], **common, "REGIME_SKIP_WEAK": skip}
        rows.append(_tournament_row(cfg, data, settings, spread_pct, cache, entry, name, skip))
        say(f"   ⏳ Turnuva {i}/{len(plan) + len(TOURNAMENT_ROTATIONS)}: {entry} + {name}"
            + (" (zayıf piyasada alım yok)" if skip else ""))

    notes: List[str] = []
    rotation_data: Optional[BacktestData] = None
    rot_base: Settings = {"STRATEGY": "rotation", **{k: v for k, v in PROFILES["rotation"].items()}}
    try:
        rotation_data = prepare_backtest(replace(cfg, strategy="rotation"), client, days=days,
                                         max_symbols=max(max_symbols, cfg.rotation_universe), cache_dir=cache_dir,
                                         progress=say, end_time_ms=end_time_ms)
    except Exception as exc:  # noqa: BLE001 - rotasyon verisi alınamazsa saatlik sonuçlar yine gösterilsin
        notes.append(f"Rotasyon için günlük veri hazırlanamadı ({exc}); rotasyon satırları yok.")
    if rotation_data is not None:
        for j, (name, extra) in enumerate(TOURNAMENT_ROTATIONS.items(), len(plan) + 1):
            settings = {**rot_base, **extra}
            rows.append(_tournament_row(cfg, rotation_data, settings, spread_pct, {}, "rotation", name, None))
            say(f"   ⏳ Turnuva {j}/{len(plan) + len(TOURNAMENT_ROTATIONS)}: {name}")
    return TournamentResult(rows, data, rotation_data, notes)


def _tournament_mark(t: TournamentRow, tested: int) -> str:
    r = t.row
    if r.proven(tested):
        return "✅"
    if r.profitable and r.consistent and r.enough_trades:
        return "≈ "
    if not r.enough_trades:
        return "· "
    return "  "


def format_tournament_report(result: TournamentResult, cfg, spread_pct: float = 0.05) -> str:
    q, data, rows = cfg.quote_asset, result.data, result.rows
    tested = len(rows)
    ranked = sorted(rows, key=lambda t: (not t.row.enough_trades, -t.row.mean_net_pct))
    slots = position_slots(cfg.start_capital, data.min_notional, cfg)
    lines = [
        "🏆 STRATEJİ TURNUVASI — aynı dönem, her strateji kendi çıkış kurallarıyla",
        LINE,
        f"🗓️ Dönem: {_fmt_time(data.sim_start_ms)} → {_fmt_time(data.end_ms)} ({data.days:g} gün) | "
        f"{len(data.series)} sembol | sermaye {fmt_usdt(cfg.start_capital)} {q} | aynı anda en fazla {slots} pozisyon "
        f"| komisyon {fmt_rate(cfg.fee_rate)} | spread %{spread_pct:g}",
    ] + _benchmark_lines(data) + [
        f"🔢 Denenen birleşim: {tested} | işlem başı kârlı: {sum(1 for t in rows if t.row.profitable)} | "
        f"iki yarıda da kârlı ve ≥{MIN_TRADES_FOR_EVIDENCE} işlem: "
        f"{sum(1 for t in rows if t.row.profitable and t.row.consistent and t.row.enough_trades)} | "
        f"şans eleğinden de geçen: {sum(1 for t in rows if t.row.proven(tested))}",
        "   (Karşılaştırma adil olsun diye kill-switch kapalıdır; maks. düşüşe bakın.)",
        LINE,
    ]
    width_e = max(len(t.entry) for t in rows)
    width_x = max(len(t.exit) for t in rows)
    lines.append(f"   {'Strateji':<{width_e}} {'Çıkış':<{width_x}} Zayıf | İşlem  Kaz%  Ort.kazanç Ort.kayıp "
                 f"İşl.başı   Net USDT  Getiri  MaksDD |  1.yarı  2.yarı")
    for t in ranked:
        r = t.row
        weak = "-    " if t.skip_weak is None else ("atla " if t.skip_weak else "al   ")
        gain = r.net / cfg.start_capital * 100 if cfg.start_capital > 0 else 0.0
        lines.append(
            f"{_tournament_mark(t, tested)} {t.entry:<{width_e}} {t.exit:<{width_x}} {weak} | {r.trades:>5d} "
            f"{r.win_rate:>5.1f} {t.avg_win_pct:>+9.2f}% {t.avg_loss_pct:>+8.2f}% {r.mean_net_pct:>+7.2f}% "
            f"{r.net:>+10.4f} {gain:>+6.1f}% {r.max_dd_pct:>6.1f}% | {r.first_net:>+7.3f} {r.second_net:>+7.3f}")
    lines += [
        "   ✅ = aday (iki yarıda kârlı, ≥30 işlem ve denenen birleşim sayısına göre şanstan ayırt edilebiliyor)",
        "   ≈ = iki yarıda kârlı ama şans olabilir | · = 30'dan az işlem (güvenilmez)",
        "   Zayıf: al = piyasa zayıfken sıkı kurallarla alır, atla = zayıf piyasada hiç almaz (REGIME_SKIP_WEAK=true)",
        "   Kaz% = kazanma oranı | Ort.kazanç / Ort.kayıp = kazanan / kaybeden işlemlerin ortalama net sonucu",
        "   İşl.başı = işlem başına ortalama net K/Z — kârlılığı bu belirler, kazanma oranı değil",
        "   Strateji: mean_revert = büyük coinlerde trend içi aşırı satım alımı | rsi_divergence = pozitif RSI "
        "uyumsuzluğu + dönüş teyidi",
        "             swing_pullback = trendde geri çekilmede alım",
        "             swing = çok günlük zirve kırılımı | rotation = haftalık momentum rotasyonu (günlük mumlar)",
    ]
    lines += [f"   Çıkış: {name} = {note}" for name, note in TOURNAMENT_EXIT_NOTES.items()]
    lines += ["   Çıkış: haftalık rotasyon = .env'deki ROTATION_* ayarları | en hacimli coin trendi = genelde BTC; "
              "momentum pozitif ve BTC 50 günlük ortalamanın üstündeyken tutar", LINE]

    enough = [t for t in rows if t.row.enough_trades]
    if enough:
        top_win = sorted(enough, key=lambda t: -t.row.win_rate)[:3]
        lines.append("🎯 Kazanma oranı en yüksek olanlar (≥30 işlem): " + "; ".join(
            f"{t.entry} + {t.exit} %{t.row.win_rate:.0f} (işlem başı {fmt_signed(t.row.mean_net_pct)}%)"
            for t in top_win))
    proven = [t for t in ranked if t.row.proven(tested)]
    lucky = [t for t in ranked if t.row.profitable and t.row.consistent and t.row.enough_trades
             and not t.row.proven(tested)]
    pick = proven[0] if proven else (lucky[0] if lucky else None)
    flag = f" --bitis {_fmt_date(data.sim_start_ms)}"
    if pick is not None:
        title = ("✅ En iyi aday" if proven else "≈ En iyi görünen (ama şans olabilir)")
        lines += [
            f"{title}: {pick.entry} + {pick.exit} — {pick.row.trades} işlem, kazanma oranı %{pick.row.win_rate:.0f}, "
            f"işlem başı {fmt_signed(pick.row.mean_net_pct)}%, net {fmt_signed(pick.row.net, 4)} {q}.",
            "   Kullanmak isterseniz .env'ye şu satırları yazın (aynı isimli eski satırları silin):",
        ] + [f"      {line}" for line in _advice_lines(cfg, pick.row)] + [
            f"   ÖNCE önceki (çakışmayan) dönemde doğrulayın: python main.py --backtest --turnuva --gun {data.days:g}"
            f"{flag}",
            "   Orada da aynı birleşim kârlıysa birkaç hafta TEST modunda izleyin; sonra karar verin.",
        ]
    else:
        best = next((t for t in ranked if t.row.enough_trades), None)
        lines += ["❗ Bu dönemde hiçbir birleşim iki yarıda da kârlı değil. Canlıda gerçek parayla ÇALIŞTIRMAYIN."]
        if best is not None:
            lines.append(f"   En iyi işlem başı sonuç: {best.entry} + {best.exit} {fmt_signed(best.row.mean_net_pct)}% "
                         f"(kazanma oranı %{best.row.win_rate:.0f}).")
        lines += ["   Yüksek kazanma oranı kâr getirmiyorsa sebep: kayıplar kazançlardan büyük (Ort.kayıp sütunu).",
                  f"   Önceki yılı da deneyin: python main.py --backtest --turnuva --gun {data.days:g}{flag}"]
    lines += result.notes
    lines.append("📝 Kayma yok, mum çözünürlüğünde kötümser sıralama, bugünün en hacimli coinleri (hayatta kalma "
                 "yanlılığı). Geçmiş sonuçlar geleceği garanti etmez.")
    return "\n".join(lines)


def _fmt_setting(key: str, value) -> str:
    if SWEEP_FIELDS[key][1] is str:
        return str(value)
    if SWEEP_FIELDS[key][1] is bool:
        return "true" if value else "false"
    if SWEEP_FIELDS[key][1] is int:
        return str(int(value))
    return f"{float(value):g}"


def _advice_lines(cfg, r: SweepRow) -> List[str]:
    """Önerilen satırın .env'e yazılacak TÜM satırları: strateji + satırın ayarları.

    Swing / rotasyon stratejisinde o grubun profil ayarlarının tamamı da yazılır: .env'deki çıkışlar çoğu zaman 5 dk
    stratejisine göredir (test bunların yerine SWING_EXIT_PROFILE / ROTATION_PROFILE ile yapılmış olabilir).
    Rotasyonda tüm ROTATION_* ayarları da yazılır.
    """
    strategy = str(r.settings.get("STRATEGY", cfg.strategy))
    settings: Settings = {"STRATEGY": strategy}
    family = strategy_family(strategy)
    if family in PROFILES:
        settings.update({k: getattr(cfg, PROFILE_ATTRS[k]) for k in PROFILES[family]})
    if family == "rotation":
        settings.update({k: getattr(cfg, SWEEP_FIELDS[k][0]) for k in ROTATION_KEYS})
    settings.update(r.settings)
    lines = [f"{k}={_fmt_setting(k, v)}" for k, v in settings.items()]
    stop = float(settings.get("STOP_LOSS_PCT", cfg.stop_loss_pct))
    backup = float(settings.get("BACKUP_STOP_PCT", cfg.backup_stop_pct))
    if cfg.backup_stop_enabled and backup <= stop:   # Yedek stop, zarar kesin altında beklemeli
        fixed = f"BACKUP_STOP_PCT={max(12.0, stop + 4):g}"
        lines = [ln for ln in lines if not ln.startswith("BACKUP_STOP_PCT=")] + [fixed]
    return lines


def _csv_num(text: str) -> str:
    """Türkçe Excel: alan ayırıcı ';' iken ondalık ayırıcı ',' olmalı (yoksa 3.5 tarihe, 1.234 bine dönüşür)."""
    return text.replace(".", ",")


def save_sweep_csv(rows: Sequence[SweepRow], path: Union[str, Path]) -> Path:
    """Tüm tarama satırlarını Türkçe Excel'in doğrudan açabileceği CSV'ye yazar (';' ve ondalık ',')."""
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
            values = [_fmt_setting(k, r.settings[k]) if k in r.settings else "" for k in keys] + [
                "evet" if r.baseline else "", str(r.trades), f"{r.win_rate:.1f}", f"{r.net:.4f}",
                f"{r.mean_net_pct:.3f}", f"{r.max_dd_pct:.2f}", f"{r.first_net:.4f}", f"{r.second_net:.4f}", pf,
                f"{r.se_net_pct:.4f}", "evet" if r.proven(len(rows)) else ""]
            writer.writerow([_csv_num(v) for v in values])
    return path


def _strategy_flag(cfg, r: Optional[SweepRow] = None) -> str:
    """Önerilen komutlardaki --strateji: satırın stratejisi (karşılaştırmada) veya taranan strateji."""
    return f" --strateji {r.settings.get('STRATEGY', cfg.strategy) if r is not None else cfg.strategy}"


def _benchmark_lines(data: BacktestData) -> List[str]:
    hold, dd = btc_hold(data)
    if hold is None:
        return []
    return [f"📈 Kıyas: aynı dönemde BTC'yi alıp tutmak {fmt_signed(hold)}% (maks. düşüş %{dd:.1f}); "
            f"Getiri sütunu = net K/Z ÷ başlangıç sermayesi"]


def _rotation_summary(cfg) -> str:
    btc = f"BTC {cfg.rotation_btc_ma_days} günlük ortalamanın altındayken USDT" if cfg.rotation_btc_ma_days else \
        "BTC trend filtresi kapalı"
    return (f"En güçlü {cfg.rotation_top_n} coin | momentum: son {cfg.rotation_lookback_days} gün | her "
            f"{cfg.rotation_days} günde dengeleme | {btc} | evren: en hacimli {cfg.rotation_universe} | felaket zarar "
            f"kesi -%{cfg.stop_loss_pct:g}")


def _exit_summary(cfg) -> str:
    if is_rotation(cfg.strategy):
        return _rotation_summary(cfg)
    hold = fmt_duration(cfg.max_hold_minutes * 60, exact=True) if cfg.max_hold_minutes > 0 else "sınırsız"
    breakeven = (f"+%{cfg.breakeven_trigger_pct:g} → +%{cfg.breakeven_floor_pct:g}" if breakeven_effective(cfg)
                 else "kapalı/etkisiz")
    return (f"Kâr Al +%{cfg.take_profit_pct:g} | Zarar Kes -%{cfg.stop_loss_pct:g} | İzleyen stop "
            f"+%{cfg.trailing_activation_pct:g} → +%{cfg.trailing_floor_pct:g}"
            f"{' (dinamik)' if cfg.trailing_dynamic else ''} | Maks. süre {hold} | Başabaş {breakeven}")


def _validation_lines(data: BacktestData, strat_flag: str = "") -> List[str]:
    """Bir satırı bağımsız (çakışmayan, daha önceki) dönemde sınama talimatı."""
    return ["   Bu satırları .env'e yazın (aynı isimli eski satırları silin) ve test edilen dönemle ÇAKIŞMAYAN",
            f"   önceki dönemde deneyin: python main.py --backtest --gun {data.days:g}{strat_flag} --bitis "
            f"{_fmt_date(data.sim_start_ms)}"]


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
    entry, compare = kind == "giris", kind == "strateji"
    ranked = _rank(rows)
    shown = ranked[:SWEEP_SHOW]
    base = next((r for r in rows if r.baseline), None)
    if base is not None and base not in shown:
        shown = shown + [base]
    few = [r for r in rows if not r.enough_trades]
    profitable = [r for r in rows if r.profitable]
    robust = [r for r in ranked if r.proven(len(rows))]
    lucky = [r for r in ranked if r.profitable and r.consistent and r.enough_trades and not r.proven(len(rows))]
    columns = [k for k in dict.fromkeys(k for r in rows for k in r.settings) if SWEEP_FIELDS[k][2]]
    widths = {k: max([6, len(SWEEP_FIELDS[k][2])] + [len(_fmt_setting(k, r.settings.get(k, 0))) for r in shown])
              for k in columns}

    titles = {"giris": "🧪 GİRİŞ TARAMASI (aynı veriyle farklı giriş filtreleri; çıkışlar sabit)",
              "strateji": "🧪 STRATEJİ KARŞILAŞTIRMASI (aynı veri ve çıkış ayarları; puan ve alan filtresi kapalı)",
              "cikis": "🧪 PARAMETRE TARAMASI (aynı veriyle farklı çıkış ayarları)"}
    # .env'e yazılacak satırlara (_advice_lines) ve önerilen komutlara strateji HER ZAMAN eklenir: --strateji ile
    # seçildiyse .env'dekinden farklı olabilir (rebound dahil). Karşılaştırmada doğrulama komutu adayın stratejisini,
    # taramayı tekrarlama komutu taranan stratejiyi alır (5 dk / 1 saat ayrımı, yani neyle karşılaştırılacağı korunur).
    swing = is_long_horizon(cfg.strategy)
    strat_flag = _strategy_flag(cfg)
    label = INTERVAL_LABELS.get(data.interval, data.interval)
    lines = [
        titles.get(kind, titles["cikis"]) + ("" if compare else f" — strateji: {cfg.strategy}"),
        LINE,
        f"🗓️ Dönem: {_fmt_time(data.sim_start_ms)} → {_fmt_time(data.end_ms)} ({data.days:g} gün, {label} mumlar) | "
        f"{len(data.series)} sembol | sermaye {fmt_usdt(cfg.start_capital)} {q} | komisyon {fmt_rate(cfg.fee_rate)} "
        f"| spread %{spread_pct:g}",
        f"🔢 Denenen ayar: {len(rows)} | kârlı: {len(profitable)} | iki yarıda da kârlı ve "
        f"≥{MIN_TRADES_FOR_EVIDENCE} işlem: {len(robust) + len(lucky)} | şans eleğinden de geçen: {len(robust)}",
        "   (Karşılaştırma adil olsun diye taramada kill-switch kapalıdır; maks. düşüşe bakın.)",
    ] + _benchmark_lines(data) + (
        [f"⚙️ Rotasyon (taranmayan ayarlar sabit): {_exit_summary(cfg)}"] if is_rotation(cfg.strategy) else
        [] if kind == "cikis" else [f"⚙️ Çıkışlar (tüm satırlarda): {_exit_summary(cfg)}"]) + [
        f"   Sıralama: işlem başına net sonuca göre; {MIN_TRADES_FOR_EVIDENCE}'dan az işlem açan {len(few)} ayar "
        f"(sonuçları şans) en alta alındı.",
        LINE,
        "   " + " ".join(f"{SWEEP_FIELDS[k][2]:>{widths[k]}}" for k in columns)
        + " | İşlem  Kaz%   Net USDT   Getiri  İşl.başı  MaksDD |  1.yarı  2.yarı",
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
        cells = " ".join(f"{_fmt_setting(k, r.settings.get(k, 0)):>{widths[k]}}" for k in columns)
        lines.append(
            f"{mark} {cells} | {r.trades:>5d} {r.win_rate:>5.1f} {r.net:>+10.4f} "
            f"{r.net / cfg.start_capital * 100 if cfg.start_capital > 0 else 0.0:>+7.1f}% {r.mean_net_pct:>+8.2f}% "
            f"{r.max_dd_pct:>6.1f}% | {r.first_net:>+7.3f} {r.second_net:>+7.3f}")
    rotation = is_rotation(cfg.strategy)
    mine = ("mevcut ayarlar (.env'de 5 dk stratejisi varsa önerilen uzun vade çıkışlarıyla)" if swing
            else "mevcut ayarlar (.env'de başka tür strateji varsa önerilen rotasyon ayarlarıyla)" if rotation
            else "mevcut ayarlar (.env'de başka tür strateji varsa önerilen ortalamaya dönüş çıkışlarıyla)"
            if is_mean_reversion(cfg.strategy) else ".env'deki mevcut ayarlarınız")
    legend = [f"   ⭐ = {mine} | ✅ = aday (iki yarıda kârlı ve şans eleğinden geçti) |",
              f"   ≈ = iki yarıda kârlı ama şans olabilir | · = {MIN_TRADES_FOR_EVIDENCE}'dan az işlem (güvenilmez)"]
    if rotation and entry:
        legend += ["   Mom = ROTATION_LOOKBACK_DAYS (momentum için son kaç gün), BTCort = ROTATION_BTC_MA_DAYS (BTC bu",
                   "   ortalamanın altındayken USDT'de beklenir; 0 = filtre yok), Coin = ROTATION_TOP_N (tutulan coin sayısı)"]
    elif rotation and not compare:
        legend += ["   SL = felaket zarar kesi (%), Gün = ROTATION_DAYS (kaç günde bir yeniden dengelenir)"]
    elif compare and swing:
        legend += ["   swing = çok günlük yükseliş trendinde son günlerin zirvesinin hacimle kırılmasında alım |",
                   "   swing_pullback = aynı trendde zirveden geri çekilip EMA50'den dönüşte alım (1 saatlik mumlar)"]
    elif compare and is_mean_reversion(cfg.strategy):
        legend += ["   mean_revert = trend içi aşırı satımda (20 saatlik ortalamanın altı) alım | rsi_divergence = pozitif",
                   "   RSI uyumsuzluğu + dönüş teyidiyle alım (en hacimli coinler, 1 saatlik mumlar, aynı çıkışlar)"]
    elif compare:
        legend += ["   rebound = düşen coinde dip alımı | trend = yükselen trendde EMA20'ye geri çekilmede alım |",
                   "   breakout = yükselen trendde 4 saatlik zirvenin hacimle kırılmasında alım"]
    elif entry and cfg.strategy == "rsi_divergence":
        legend += ["   Zayıf = REGIME_SKIP_WEAK (true: piyasa zayıfken alım yok), RSIdip = RSI_DIV_OVERSOLD (ilk dipte RSI",
                   "   en fazla; küçük = daha derin aşırı satım), Puan = MIN_SIGNAL_SCORE, Alan = MIN_ROOM_RATIO (0 = kapalı)"]
    elif entry and is_mean_reversion(cfg.strategy):
        legend += ["   Zayıf = REGIME_SKIP_WEAK (true: piyasa zayıfken alım yok), Bant = MR_BAND_STD (kapanış 20 saatlik",
                   "   ortalamanın kaç standart sapma altında olmalı), Puan = MIN_SIGNAL_SCORE, Alan = MIN_ROOM_RATIO (0 = kapalı)"]
    elif entry and swing:
        legend += ["   Zayıf = REGIME_SKIP_WEAK (true: piyasa zayıfken alım yok), Zirve = SWING_LOOKBACK_HOURS (kırılım /",
                   "   geri çekilme için bakılan son saatler; 72 = 3 gün), Puan = MIN_SIGNAL_SCORE, Alan = MIN_ROOM_RATIO (0 = kapalı)"]
    elif entry:
        legend += ["   Zayıf = REGIME_SKIP_WEAK (true: piyasa zayıfken alım yok), MaxDüş = MAX_24H_DROP_PCT,",
                   "   MaxYük = MAX_24H_GAIN_PCT, Puan = MIN_SIGNAL_SCORE, Alan = MIN_ROOM_RATIO (0 = kapalı)"]
    else:
        legend += ["   TP/SL/İz. = Kâr Al / Zarar Kes / izleyen stop başlangıcı (%), Süre = maks. tutma (dk"
                   + ("; 1440 = 1 gün" if swing or is_mean_reversion(cfg.strategy) else "") + "),",
                   "   Alan = MIN_ROOM_RATIO (0 = kapalı)"]
    lines += legend + ["   İşl.başı = işlem başına ortalama net K/Z", LINE]

    best = next((r for r in ranked if r.enough_trades), None)
    if not profitable:
        if compare:
            lines += ["❗ Stratejilerin HİÇBİRİ bu ayarlarla kârlı değil. Canlıda gerçek parayla ÇALIŞTIRMAYIN.",
                      "   İşlem başı sonucu en iyi olan stratejiyi --strateji ile seçip --tara ve --tara-giris ile",
                      "   ayarlarını deneyin (çıkış ayarları stratejiye göre çok farklı olabilir)."]
        else:
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
        if kind == "cikis":
            lines.append("   Giriş filtrelerini denemek için: python main.py --backtest --gun "
                         f"{data.days:g} --tara-giris{strat_flag}")
        if kind != "strateji" and cfg.strategy == "rebound":
            lines.append("   Trendin yönünde işlem yapan diğer stratejileri karşılaştırmak için: python main.py "
                         f"--backtest --gun {data.days:g} --strateji-karsilastir{strat_flag}")
        lines.append("   Tüm strateji türlerini (ortalamaya dönüş, swing, rotasyon) kendi çıkışlarıyla aynı yılda "
                     "karşılaştırmak için: python main.py --backtest --turnuva")
    elif not robust:
        top = max(profitable, key=lambda r: r.net)
        lines += [
            f"⚠️ Kârlı görünen ayarlar var (en iyisi {fmt_signed(top.net, 4)} {q}) ama hiçbiri şans eleğinden",
            f"   geçmiyor: {len(rows)} ayar denendiğinde birkaçının tesadüfen kârlı (hatta iki yarıda da kârlı)",
            "   çıkması beklenir — tamamen rastgele fiyat verisinde bile. Canlıya geçmek için yeterli kanıt yok.",
        ]
        if lucky:
            lines.append(f"   ≈ işaretli {len(lucky)} ayardan en iyisini yine de daha önceki bir dönemde sınayabilirsiniz:")
            lines += [f"      {line}" for line in _advice_lines(cfg, lucky[0])]
            lines += _validation_lines(data, _strategy_flag(cfg, lucky[0]))
    else:
        cand = robust[0]
        lines += [
            f"✅ En tutarlı aday ({cand.trades} işlem, net {fmt_signed(cand.net, 4)} {q}, iki yarıda da kârlı).",
            "   .env dosyanızdaki şu satırları DEĞİŞTİRİN (aynı isimli eski satırları silin):",
        ] + [f"      {line}" for line in _advice_lines(cfg, cand)] + [
            f"   (İşlem başı sonucun alt sınırı, {len(rows)} ayar denendiği hesaba katılarak sıfırın üstünde.)",
            "   Yine de: çok ayar denendiği için en iyi sonuç iyimserdir.",
        ] + _validation_lines(data, _strategy_flag(cfg, cand)) + [
            "   Orada da kârlıysa birkaç gün TEST modunda izleyin. --gun 30 / 60 aynı dönemin parçasıdır,",
            "   bağımsız kanıt sayılmaz.",
        ]
    flag = {"giris": "--tara-giris", "strateji": "--strateji-karsilastir"}.get(kind, "--tara")
    note = " (puan eşikleri o dönemin verisinden yeniden hesaplanır)" if entry else ""
    lines.append(f"🔁 Aynı taramayı önceki {data.days:g} günde yapmak için{note}: python main.py --backtest --gun "
                 f"{data.days:g} {flag}{strat_flag} --bitis {_fmt_date(data.sim_start_ms)}")
    lines.append(f"📝 Kayma (slippage) yok, {label} mum çözünürlüğü, bugünün en hacimli coinleri (hayatta kalma "
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
    if target_exits(cfg):
        ups = sorted({x for x in (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0) if x < tp}, reverse=True)[:4]
        ups = sorted(set(ups) | {tp})
        downs = sorted({x for x in ((1.0, 2.0) if sl <= 4 else (2.0, 4.0)) if x < sl} | {sl})
    else:   # Rotasyonda kâr al yok: kârın ne kadar büyüdüğü ve felaket zarar kesine yaklaşma
        tp = math.inf
        ups = [5.0, 10.0, 20.0, 50.0]
        downs = sorted({x for x in (5.0, 10.0) if x < sl} | {sl})
    lines.append("   İşlem açıkken en iyi anı şu kâra ulaşanlar: " + " | ".join(
        f"+%{x:g}{' (Kâr Al)' if x == tp else ''}: {_share(trades, lambda t, x=x: t.peak_pct >= x)}" for x in ups))
    lines.append("   En kötü anı şu zarara düşenler: " + " | ".join(
        f"-%{x:g}{' (Zarar Kes)' if x == sl else ''}: {_share(trades, lambda t, x=x: t.trough_pct <= -x)}"
        for x in downs))
    reached = sorted(t.peak_after_sec for t in trades if t.peak_pct >= 1.0)
    if reached:
        lines.append(f"   +%1 ve üstünü görenlerde en iyi ana varış süresi (ortanca): "
                     f"{fmt_duration(reached[len(reached) // 2])}")
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
    flag = f" --strateji {cfg.strategy}"   # .env'deki stratejiden farklı olabilir (--strateji)
    what = ("zarar kes / dengeleme sıklığı" if is_rotation(cfg.strategy) else "Kâr Al / Zarar Kes / süre")
    lines.append(f"   💡 Farklı {what} ayarlarını aynı veriyle karşılaştırmak için: "
                 f"python main.py --backtest --gun {result.days:g} --tara{flag}")
    lines.append(f"   💡 Bağımsız doğrulama (bu dönemle çakışmayan, daha önceki {result.days:g} gün): "
                 f"python main.py --backtest --gun {result.days:g}{flag} --bitis {_fmt_date(result.start_ms)}")
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
    hold = f" | Maks. süre {fmt_duration(cfg.max_hold_minutes * 60, exact=True)}" if cfg.max_hold_minutes > 0 else ""
    if not cfg.breakeven_enabled:
        breakeven = "kapalı"
    elif breakeven_effective(cfg):
        breakeven = f"+%{cfg.breakeven_trigger_pct:g} → +%{cfg.breakeven_floor_pct:g}"
    else:
        breakeven = f"etkisiz (tetik +%{cfg.breakeven_trigger_pct:g}, izleyen stop / kâr al'dan küçük olmalı)"

    lines = [
        "📊 BACKTEST RAPORU (geçmiş veriyle strateji testi)",
        LINE,
        f"🗓️ Dönem: {_fmt_time(result.start_ms)} → {_fmt_time(result.end_ms)} ({result.days:g} gün, "
        f"{INTERVAL_LABELS.get(result.interval, result.interval)} mumlar)",
        f"🪙 Sembol sayısı: {len(result.symbols_used)}",
        f"🧭 Strateji: {cfg.strategy}" + (f" (zirve penceresi {cfg.swing_lookback_hours} saat)"
                                          if is_long_horizon(cfg.strategy) else ""),
    ] + ([f"⚙️ Rotasyon: {_rotation_summary(cfg)}"] if is_rotation(cfg.strategy) else [
        f"⚙️ Kâr Al +%{cfg.take_profit_pct:g} | Zarar Kes -%{cfg.stop_loss_pct:g} | İzleyen stop {trailing}{hold}",
        f"   Başabaş: {breakeven}"]) + [
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
    if result.btc_hold_pct is not None:
        lines.append(f"📈 Kıyas — aynı dönemde BTC'yi alıp tutmak: {fmt_signed(result.btc_hold_pct)}% (maks. düşüş "
                     f"%{result.btc_hold_dd_pct:.1f}) | USDT'de beklemek: %0")
    st = result.stats or {}
    if st and is_rotation(cfg.strategy):
        days = max(1, round(result.days))
        lines.append(f"🔄 Yeniden dengeleme: {st.get('dengeleme', 0)} kez | alınan coin: {st.get('sinyal', 0)} | "
                     f"BTC trend filtresiyle USDT'de geçen gün: {st.get('trend_kapali_gun', 0)} "
                     f"(%{st.get('trend_kapali_gun', 0) / days * 100:.0f})")
        if st.get("pompa"):
            lines.append(f"🚀 Pompa filtresine takılan aday: {st['pompa']}")
        if st.get("yeni_coin"):
            lines.append(f"🆕 Yeni listelendiği için (MIN_LISTING_DAYS) sıralamaya girmeyen aday: {st['yeni_coin']}")
    elif st:
        lines.append(f"🔎 Analiz: {st.get('analiz', 0)} | Sinyal: {st.get('sinyal', 0)} | "
                     f"BTC filtresi engeli: {st.get('btc_engel', 0)} mum")
        if st.get("rejim_cokus") or st.get("rejim_zayif"):
            weak = "alım yapılmayan" if cfg.regime_skip_weak else "sıkı kurallı"
            lines.append(f"🌡️ Piyasa rejimi: çöküş nedeniyle alım yapılmayan {st.get('rejim_cokus', 0)} mum, "
                         f"zayıf piyasada {weak} {st.get('rejim_zayif', 0)} mum")
        if st.get("alan_dar"):
            lines.append(f"📏 Hedef alanı dar olduğu için alınmayan sinyal: {st['alan_dar']}")
        if st.get("yeni_coin"):
            lines.append(f"🆕 Yeni listelendiği için (MIN_LISTING_DAYS) incelenmeyen aday: {st['yeni_coin']}")
        filtered = {k.split(":", 1)[1]: v for k, v in st.items() if k.startswith("filtre:")}
        if filtered:
            lines.append("🛡️ Ek filtrelere takılan sinyal: " + ", ".join(f"{k}: {v}" for k, v in filtered.items()))
        if st.get("dondurma"):
            lines.append(f"🧊 Risk sigortası (zarar serisi / günlük limit) alımları {st['dondurma']} kez dondurdu")
        smart = {"hedef_uzatma": "hedef uzatma", "ekleme_dca": "DCA ekleme", "ekleme_piramit": "piramit ekleme",
                 "erken_kacis": "erken kaçış", "sure_kurali": "süre kuralı", "kademeli_alim": "kademeli alım",
                 "kademeli_iptal": "dönmeyen kademeli alım"}
        used = [f"{label}: {st[key]}" for key, label in smart.items() if st.get(key)]
        if used:
            lines.append("🧠 Akıllı yönetim: " + ", ".join(used))

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
