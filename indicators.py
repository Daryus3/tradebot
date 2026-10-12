"""Teknik göstergeler (saf Python; pandas/numpy gerektirmez)."""
from __future__ import annotations

from typing import List, Optional, Sequence


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Üssel hareketli ortalama. İlk değer basit ortalama ile başlatılır."""
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """Wilder yöntemiyle RSI (0-100)."""
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out

    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
