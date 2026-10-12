"""
Piyasa rejimi tespiti.

Toparlanma (dip rebound) stratejisi genel piyasa çökerken sık sık zarar eder.
Bu modül her taramada piyasayı üç sınıftan birine koyar:
  - COKUS : BTC 24 saatte sert düştü veya coinlerin neredeyse tamamı düşüşte ve
            BTC kısa vadeli trendin (4 saatlik EMA20) altında → yeni alım yapılmaz.
  - ZAYIF : BTC hem 4 saatlik EMA50 hem günlük EMA20 altında veya coinlerin çoğu
            düşüşte → kurallar sıkılaşır, en fazla 1 pozisyon.
  - NORMAL: diğer durumlar.

`classify` saf bir fonksiyondur (ağ yok); `MarketRegime` BTC mumlarını önbelleğe
alarak her taramada çağrılabilir.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from binance_client import RateLimitError
from indicators import ema
from logger import get_logger

log = get_logger()

NORMAL, ZAYIF, COKUS = "NORMAL", "ZAYIF", "COKUS"

CRASH_BTC_24H_PCT = -6.0     # BTC 24 saatte bu kadar (veya daha fazla) düştüyse çöküş
CRASH_BREADTH_PCT = 85.0     # Coinlerin bu kadarı düşüşte + BTC 4s EMA20 altında → çöküş
WEAK_BREADTH_PCT = 70.0      # Coinlerin bu kadarı düşüşte → zayıf
MIN_BREADTH_PAIRS = 20       # Genişlik hesabı için gereken en az parite sayısı
BTC_SYMBOL = "BTCUSDT"
KLINE_4H_LIMIT = 60
KLINE_1D_LIMIT = 30
RETRY_SEC = 60               # BTC mumları alınamazsa en erken bu kadar sonra tekrar dene


@dataclass
class RegimeResult:
    regime: str                          # 'NORMAL' | 'ZAYIF' | 'COKUS'
    note: str                            # Türkçe açıklama (Telegram/rapor için)
    btc_24h_pct: Optional[float] = None
    breadth_down_pct: Optional[float] = None


def _f(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _last_ema(closes: Sequence[float], period: int) -> Optional[float]:
    if not closes or len(closes) < period:
        return None
    return ema(list(closes), period)[-1]


def compute_breadth(tickers: List[dict], rules: Dict, cfg) -> Optional[float]:
    """Likit USDT paritelerinin yüzde kaçı son 24 saatte düşüşte? (<20 parite → None)"""
    total = down = 0
    for t in tickers or []:
        symbol = t.get("symbol", "")
        r = rules.get(symbol)
        if r is None or getattr(r, "quote_asset", "") != "USDT" or r.base_asset in cfg.excluded_assets:
            continue
        change, quote_vol = _f(t.get("priceChangePercent")), _f(t.get("quoteVolume"))
        if change is None or quote_vol is None or quote_vol < cfg.min_quote_volume_24h:
            continue
        total += 1
        if change < 0:
            down += 1
    if total < MIN_BREADTH_PAIRS:
        return None
    return down / total * 100


def classify(btc_price: Optional[float], btc_4h_closes: Sequence[float], btc_1d_closes: Sequence[float],
             btc_24h_pct: Optional[float], breadth_down_pct: Optional[float]) -> RegimeResult:
    """Piyasa rejimini verilen verilerle belirler (saf fonksiyon, ağ çağrısı yok).

    Eksik veri: yalnızca eldeki verilerle karar verilir; hiç veri yoksa NORMAL.
    """
    closes_4h = [float(x) for x in (btc_4h_closes or [])]
    closes_1d = [float(x) for x in (btc_1d_closes or [])]
    price = btc_price if btc_price and btc_price > 0 else None
    if price is None:  # Ticker yoksa son kapanışı kullan
        price = (closes_4h or closes_1d or [None])[-1]

    ema20_4h = _last_ema(closes_4h, 20)
    ema50_4h = _last_ema(closes_4h, 50)
    ema20_1d = _last_ema(closes_1d, 20)

    def result(regime: str, note: str) -> RegimeResult:
        return RegimeResult(regime, note, btc_24h_pct, breadth_down_pct)

    # --- Çöküş
    crash = []
    if btc_24h_pct is not None and btc_24h_pct <= CRASH_BTC_24H_PCT:
        crash.append(f"BTC 24 saatte %{btc_24h_pct:+.1f}")
    if (breadth_down_pct is not None and breadth_down_pct >= CRASH_BREADTH_PCT
            and price is not None and ema20_4h is not None and price < ema20_4h):
        crash.append(f"düşen coin oranı %{breadth_down_pct:.0f} ve BTC 4s EMA20 altında")
    if crash:
        return result(COKUS, "; ".join(crash))

    # --- Zayıf
    weak = []
    trend = [(name, v) for name, v in (("4s EMA50", ema50_4h), ("günlük EMA20", ema20_1d)) if v is not None]
    if price is not None and trend and all(price < v for _, v in trend):
        weak.append("BTC " + " ve ".join(name for name, _ in trend) + " altında")
    if breadth_down_pct is not None and breadth_down_pct >= WEAK_BREADTH_PCT:
        weak.append(f"düşen coin oranı %{breadth_down_pct:.0f}")
    if weak:
        return result(ZAYIF, "; ".join(weak))

    # --- Normal
    parts = []
    if btc_24h_pct is not None:
        parts.append(f"BTC 24s %{btc_24h_pct:+.1f}")
    if breadth_down_pct is not None:
        parts.append(f"düşen coin %{breadth_down_pct:.0f}")
    if not parts:
        has_trend = price is not None and (trend or ema20_4h is not None)
        return result(NORMAL, "BTC trendi normal" if has_trend else "veri yetersiz, rejim değerlendirilemedi")
    return result(NORMAL, " | ".join(parts))


class MarketRegime:
    """Her taramada piyasa rejimini hesaplar; BTC 4s/1g mumlarını `cache_sec` boyunca saklar."""

    def __init__(self, client, cfg, time_fn: Callable[[], float] = time.time, cache_sec: float = 900):
        self.client = client
        self.cfg = cfg
        self.time_fn = time_fn
        self.cache_sec = cache_sec
        self._closes_4h: List[float] = []
        self._closes_1d: List[float] = []
        self._fetched_at: Optional[float] = None
        self._retry_at: float = 0.0
        self.last: Optional[RegimeResult] = None

    def _btc_closes(self) -> Tuple[List[float], List[float], bool]:
        """(4s kapanışlar, 1g kapanışlar, taze_mi). Hata olursa eldeki (eski) veri döner."""
        now = self.time_fn()
        if self._fetched_at is not None and now - self._fetched_at < self.cache_sec:
            return self._closes_4h, self._closes_1d, True
        if now < self._retry_at:
            return self._closes_4h, self._closes_1d, False
        try:
            c4 = [float(r[4]) for r in self.client.klines(BTC_SYMBOL, "4h", limit=KLINE_4H_LIMIT) or []]
            c1 = [float(r[4]) for r in self.client.klines(BTC_SYMBOL, "1d", limit=KLINE_1D_LIMIT) or []]
        except RateLimitError:
            raise  # Binance istek limiti: tarama durmalı, döngü beklesin
        except Exception as exc:  # noqa: BLE001 - rejim verisi alınamazsa tarama yine sürsün
            log.debug("BTC rejim mumları alınamadı: %s", exc)
            self._retry_at = now + RETRY_SEC
            return self._closes_4h, self._closes_1d, False
        self._closes_4h, self._closes_1d, self._fetched_at = c4, c1, now
        return c4, c1, True

    def evaluate(self, tickers: List[dict], rules: Dict) -> RegimeResult:
        if not self.cfg.regime_filter:
            return RegimeResult(NORMAL, "")
        try:
            btc = next((t for t in tickers or [] if t.get("symbol") == BTC_SYMBOL), None) or {}
            btc_price, btc_24h = _f(btc.get("lastPrice")), _f(btc.get("priceChangePercent"))
            breadth = compute_breadth(tickers, rules, self.cfg)
            c4, c1, fresh = self._btc_closes()
            res = classify(btc_price, c4, c1, btc_24h, breadth)
            if not fresh:
                extra = "BTC mumları için veri alınamadı" + (" (eski veri)" if c4 or c1 else "")
                res.note = f"{res.note}; {extra}" if res.note else extra
        except RateLimitError:
            raise  # Binance istek limiti: tarama durmalı, döngü beklesin
        except Exception as exc:  # noqa: BLE001
            log.debug("Piyasa rejimi hesaplanamadı: %s", exc)
            res = RegimeResult(NORMAL, "veri alınamadı")
        if self.last is None or res.regime != self.last.regime:
            log.info("Piyasa rejimi: %s%s", res.regime, f" ({res.note})" if res.note else "")
        self.last = res
        return res
