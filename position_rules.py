"""
Akıllı pozisyon yönetimi ve sinyal kuralları. Canlı bot ve backtest AYNI fonksiyonları kullanır.

Girdi mumları Binance klines biçimindedir ve yalnızca KAPANMIŞ mumlardır
(0 açılış zamanı, 1 açılış, 2 yüksek, 3 düşük, 4 kapanış, 7 quote hacim, 10 alıcı-başlatan quote hacim).
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

from indicators import atr, ema, mean, rsi

MOMENTUM_VOLUME_RATIO = 1.2     # Momentum: son 3 mumun hacmi önceki 20 mumun ortalamasının en az bu katı
DIV_LOOKBACK = 40               # RSI uyumsuzluğu: son kaç mumda dip aranır
DIV_MIN_GAP, DIV_MAX_GAP = 5, 30
DIV_RSI_OVERSOLD = 35.0         # İlk dipte RSI en fazla bu olmalı
DIV_RSI_RISE = 3.0              # İkinci dipte RSI en az bu kadar yüksek olmalı


def _col(rows: Sequence[Sequence], i: int):
    return [float(r[i]) for r in rows]


def atr_pct(rows: Sequence[Sequence], price: float, period: int = 14) -> Optional[float]:
    """Kapanmış mumlardan ATR(period) / fiyat (%). Veri azsa None."""
    if len(rows) < period + 1 or price <= 0:
        return None
    value = atr(_col(rows, 2), _col(rows, 3), _col(rows, 4), period)[-1]
    return value / price * 100 if value else None


def atr_targets(cfg, atr_value_pct: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """ATR_TARGETS: (Kâr Al %, Zarar Kes %). Kapalıysa veya ATR bilinmiyorsa (None, None) = ayardaki değerler.

    Zarar Kes yalnızca DARALIR (en fazla STOP_LOSS_PCT): pozisyon boyutu ve Binance yedek stop'u ayardaki zarar kese
    göre hesaplanır; daha geniş bir stop onları bozardı.
    """
    if not getattr(cfg, "atr_targets", False) or not atr_value_pct:
        return None, None
    tp = min(max(atr_value_pct * cfg.atr_tp_mult, cfg.atr_tp_min_pct), cfg.atr_tp_max_pct)
    sl = min(max(atr_value_pct * cfg.atr_sl_mult, cfg.atr_sl_min_pct), cfg.stop_loss_pct)
    return round(tp, 2), round(sl, 2)


def momentum_strong(rows: Sequence[Sequence]) -> bool:
    """Son kapanmış mum yeşil, kapanış EMA(9) üstünde ve son 3 mumun hacmi önceki 20 mumun ortalamasından en az
    %20 fazla mı? (Kâr Al'da hedef uzatma ve piramit için.)"""
    if len(rows) < 24:
        return False
    last = rows[-1]
    o, c = float(last[1]), float(last[4])
    if c <= o:
        return False
    line = ema(_col(rows, 4), 9)[-1]
    if line is None or c <= line:
        return False
    vols = _col(rows, 7)
    base = mean(vols[-23:-3])
    return base > 0 and mean(vols[-3:]) >= MOMENTUM_VOLUME_RATIO * base


def volume_breakdown(rows: Sequence[Sequence], ratio: float) -> bool:
    """Hacimli satış baskısı: son kapanmış mum kırmızı, hacmi önceki 20 mumun ortalamasının `ratio` katı ve hacmin
    yarıdan fazlası piyasa satışı (alıcı-başlatan hacim < %50)."""
    if len(rows) < 21:
        return False
    last = rows[-1]
    if float(last[4]) >= float(last[1]):
        return False
    qv = float(last[7])
    base = mean(_col(rows[-21:-1], 7))
    if base <= 0 or qv < ratio * base:
        return False
    return float(last[10]) / qv < 0.5


def is_flat(pnl_pct: float, band_pct: float) -> bool:
    return abs(pnl_pct) <= band_pct


def bullish_divergence(rows: Sequence[Sequence], period: int = 14) -> Optional[dict]:
    """Pozitif RSI uyumsuzluğu: fiyat daha düşük dip yaparken RSI daha yüksek dip yapıyor (satış gücü azalıyor).

    Son dip son 4 mum içinde olmalı (yeni oluşmuş); önceki dip 5-30 mum önce ve RSI'ı aşırı satımda (≤35).
    Bulursa {"price_drop_pct", "rsi_prev", "rsi_now", "bars_apart"}, yoksa None.
    """
    n = len(rows)
    if n < max(DIV_LOOKBACK, period + 10):
        return None
    lows = _col(rows, 3)
    values = rsi(_col(rows, 4), period)
    pivots = [i for i in range(max(2, n - DIV_LOOKBACK), n - 1)
              if lows[i] <= min(lows[i - 2:i]) and lows[i] < lows[i + 1] and values[i] is not None]
    recent = [i for i in pivots if i >= n - 5]
    if not recent:
        return None
    p2 = recent[-1]
    for p1 in reversed(pivots):
        gap = p2 - p1
        if gap < DIV_MIN_GAP:
            continue
        if gap > DIV_MAX_GAP:
            break
        if (lows[p2] < lows[p1] and values[p1] <= DIV_RSI_OVERSOLD
                and values[p2] >= values[p1] + DIV_RSI_RISE):
            return {"price_drop_pct": (lows[p2] / lows[p1] - 1) * 100, "rsi_prev": values[p1],
                    "rsi_now": values[p2], "bars_apart": gap}
    return None


def trailing_buy_trigger(low: float, pct: float) -> float:
    """Kademeli alım: dipten bu fiyata dönünce alınır."""
    return low * (1 + pct / 100)
