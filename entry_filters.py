"""
Ek giriş filtreleri. Hepsi .env veya Telegram menüsünden (/menu → Filtreler) açılıp kapatılır; varsayılan kapalı.

Mum filtreleri — canlı tarama ve backtest AYNI fonksiyonu kullanır (`--tara-ab` etkilerini ölçebilsin diye):
  - FAKEOUT_FILTER: son kapanmış mumun üst fitili gövdesinden uzunsa (satıcılar yükselişi geri itti) alma.
  - VWAP_FILTER: fiyat son VWAP_BARS mumun hacim ağırlıklı ortalama fiyatının (VWAP) altındaysa alma.
  - OVERHEAT_FILTER: fiyat son OVERHEAT_MINUTES dakikanın en düşüğünden OVERHEAT_PCT'den fazla yükseldiyse alma.
  - RSI_DIV_FILTER: son DIV_FILTER_RECENT_BARS mumda pozitif RSI uyumsuzluğu (daha düşük dip + daha yüksek RSI dibi;
    bkz. position_rules.bullish_divergence) yoksa alma — satış gücünün azaldığı teyidi (stratejinin kendi mumlarıyla).

Önceki trend filtresi (PRIOR_TREND_FILTER) — canlıda 1 saatlik mumlarla, backtest'te simülasyon mumlarıyla aynı kural:
  coin son düşüşten ÖNCE yükselişte olmalı (1 gün önceki fiyat ≥ 7 gün önceki fiyat). Haftalardır düşen coinde
  ("düşen bıçak") dip alınmaz; yükselen coindeki tek günlük düşüş alınır.

Emir defteri / işlem akışı filtreleri yalnızca canlı taramada çalışır (geçmiş emir defteri verisi yoktur,
backtest edilemez):
  - OB_WALL_FILTER: alış fiyatının +OB_BAND_PCT üstüne kadar (komisyonları karşılayan başabaş bölgesi) tek bir
    fiyat seviyesinde, defterdeki ortanca seviyenin OB_WALL_RATIO katından büyük satış emri ("duvar") varsa alma.
  - OB_IMBALANCE_FILTER: ±OB_BAND_PCT bandındaki satış emirleri alış emirlerinin OB_IMBALANCE_RATIO katıysa alma.
  - TAPE_FILTER: son TAPE_SECONDS saniyede piyasa alımlarının (alıcının başlattığı işlemler) toplam işlem
    hacmindeki payı TAPE_MIN_BUY_RATIO'dan düşükse alma.
Veri alınamazsa filtre uygulanmaz (alımı engellemez); durum loga yazılır.
"""
from __future__ import annotations

import time
from statistics import median
from typing import List, Optional, Sequence, Tuple

from binance_client import RateLimitError
from logger import get_logger
from position_rules import bullish_divergence

log = get_logger()

# Elenme nedenleri (tarama istatistiklerinde sayılır: /tarama)
FAKEOUT = "üst fitil uzun"
BELOW_VWAP = "VWAP altında"
OVERHEAT = "aşırı ısınmış"
NO_DIVERGENCE = "RSI uyumsuzluğu yok"
NO_PRIOR_TREND = "düşüşten önce de düşüyordu"
PRIOR_TREND_DAYS = 7          # Önceki trend: 7 gün önceki fiyattan 1 gün önceki fiyata
SELL_WALL = "satış duvarı"
BOOK_IMBALANCE = "defterde satıcı baskısı"
WEAK_TAPE = "piyasa alımı zayıf"
DIV_FILTER_RECENT_BARS = 24   # RSI_DIV_FILTER: ikinci dip son 24 mum içinde olmalı (5 dk: 2 saat, 1 saat: 1 gün)


def candle_filters_on(cfg) -> bool:
    return bool(getattr(cfg, "fakeout_filter", False) or getattr(cfg, "vwap_filter", False)
                or getattr(cfg, "overheat_filter", False) or getattr(cfg, "rsi_div_filter", False))


def vwap(rows: Sequence[Sequence]) -> Optional[float]:
    """Hacim ağırlıklı ortalama fiyat. Tipik fiyat (yüksek + düşük + kapanış) / 3 ile, quote hacimden hesaplanır:
    Σ(quote hacim) / Σ(quote hacim / tipik fiyat). Hacim yoksa None."""
    num = den = 0.0
    for r in rows:
        tp = (float(r[2]) + float(r[3]) + float(r[4])) / 3
        qv = float(r[7])
        if tp > 0 and qv > 0:
            num += qv
            den += qv / tp
    return num / den if den > 0 else None


def candle_reject(cfg, raw_klines: Sequence[Sequence]) -> str:
    """Mum filtreleri. '' = geçti, aksi halde elenme nedeni.

    `raw_klines` Binance klines biçimindedir; son satır henüz kapanmamış mumdur (kapanışı = güncel fiyat).
    """
    if not candle_filters_on(cfg) or len(raw_klines) < 3:
        return ""
    closed = raw_klines[:-1]
    price = float(raw_klines[-1][4])
    if price <= 0:
        return ""
    if cfg.fakeout_filter:
        o, h, c = float(closed[-1][1]), float(closed[-1][2]), float(closed[-1][4])
        upper, body = h - max(o, c), abs(c - o)
        if upper > 0 and upper > body:
            return FAKEOUT
    if cfg.vwap_filter:
        level = vwap(closed[-cfg.vwap_bars:])
        if level is not None and price < level:
            return BELOW_VWAP
    if cfg.overheat_filter:
        step_ms = int(float(closed[-1][0])) - int(float(closed[-2][0]))
        bars = max(1, round(cfg.overheat_minutes * 60_000 / step_ms)) if step_ms > 0 else 1
        lows = [float(r[3]) for r in closed[-bars:] if float(r[3]) > 0]
        low = min(lows + [price])
        if (price - low) / low * 100 >= cfg.overheat_pct:
            return OVERHEAT
    if getattr(cfg, "rsi_div_filter", False) and bullish_divergence(
            closed, cfg.rsi_period, cfg.rsi_div_oversold, cfg.rsi_div_rise, recent=DIV_FILTER_RECENT_BARS) is None:
        return NO_DIVERGENCE
    return ""


def prior_trend_ok(price_week_ago: float, price_day_ago: float) -> bool:
    """PRIOR_TREND_FILTER: 1 gün önceki fiyat 7 gün önceki fiyatın altında değilse geçer (veri yoksa engellemez)."""
    return price_week_ago <= 0 or price_day_ago <= 0 or price_day_ago >= price_week_ago


def prior_trend_reject(cfg, client, symbol: str) -> str:
    """Önceki trend filtresi (canlı): 1 saatlik mumlarla 7 gün ve 1 gün önceki kapanış. '' = geçti / kapalı / veri yok."""
    if not getattr(cfg, "prior_trend_filter", False):
        return ""
    week = PRIOR_TREND_DAYS * 24
    try:
        rows = client.klines(symbol, "1h", week + 2)
    except RateLimitError:
        raise
    except Exception as exc:  # noqa: BLE001 - veri yoksa filtre alımı engellemez
        log.info("%s önceki trend için mum alınamadı, filtre atlandı: %s", symbol, exc)
        return ""
    if not rows or len(rows) < week + 2:
        return ""   # Yeterli geçmişi olmayan yeni coin (MIN_LISTING_DAYS ayrıca eler)
    # Son satır açık mum; [-2] son kapanan, [-26] 24 saat önce, [-170] 7 gün önce kapanan saatlik mum
    return "" if prior_trend_ok(float(rows[-week - 2][4]), float(rows[-26][4])) else NO_PRIOR_TREND


def _levels(rows) -> List[Tuple[float, float]]:
    return [(float(p), float(q)) for p, q, *_ in rows or []]


def book_reject(cfg, client, symbol: str, price: float) -> str:
    """Emir defteri filtreleri (canlı). '' = geçti veya kapalı / veri yok."""
    if not (cfg.ob_wall_filter or cfg.ob_imbalance_filter) or price <= 0:
        return ""
    try:
        book = client.depth(symbol, 100)
    except RateLimitError:
        raise
    except Exception as exc:  # noqa: BLE001 - veri yoksa filtre alımı engellemez
        log.info("%s emir defteri alınamadı, defter filtreleri atlandı: %s", symbol, exc)
        return ""
    asks, bids = _levels(book.get("asks")), _levels(book.get("bids"))
    if not asks or not bids:
        return ""
    band = cfg.ob_band_pct / 100
    near_asks = [(p, p * q) for p, q in asks if p <= price * (1 + band)]
    if cfg.ob_wall_filter and near_asks:
        typical = median(p * q for p, q in asks)
        wall_price, wall = max(near_asks, key=lambda x: x[1])
        if typical > 0 and wall >= cfg.ob_wall_ratio * typical:
            log.info("%s atlandı: %.0f USDT satış duvarı (+%%%.2f, ortanca seviyenin %.0f katı)", symbol,
                     wall, (wall_price / price - 1) * 100, wall / typical)
            return SELL_WALL
    if cfg.ob_imbalance_filter:
        sell = sum(n for _, n in near_asks)
        buy = sum(p * q for p, q in bids if p >= price * (1 - band))
        if sell > 0 and sell >= cfg.ob_imbalance_ratio * buy:
            log.info("%s atlandı: ±%%%.1f bandında satış %.0f USDT, alış %.0f USDT", symbol, cfg.ob_band_pct,
                     sell, buy)
            return BOOK_IMBALANCE
    return ""


def tape_reject(cfg, client, symbol: str, now_ms: Optional[float] = None) -> str:
    """İşlem akışı (tape) teyidi (canlı). '' = geçti veya kapalı / veri yok."""
    if not cfg.tape_filter:
        return ""
    try:
        trades = client.agg_trades(symbol, 500)
    except RateLimitError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.info("%s işlem akışı alınamadı, tape filtresi atlandı: %s", symbol, exc)
        return ""
    if now_ms is None:
        now_ms = time.time() * 1000 + getattr(client, "time_offset_ms", 0)
    since = now_ms - cfg.tape_seconds * 1000
    buy = total = 0.0
    for t in trades or []:
        if float(t.get("T", 0)) < since:
            continue
        notional = float(t["p"]) * float(t["q"])
        total += notional
        if not t.get("m"):   # Alıcı piyasa emri verdi (satıcı defterdeydi)
            buy += notional
    ratio = buy / total if total > 0 else 0.0
    if ratio < cfg.tape_min_buy_ratio:
        log.info("%s atlandı: son %d sn'de piyasa alımı payı %%%.0f (işlem hacmi %.0f USDT)", symbol,
                 cfg.tape_seconds, ratio * 100, total)
        return WEAK_TAPE
    return ""


def live_reject(cfg, client, symbol: str, price: float) -> str:
    """Canlıya özel filtreler sırayla: emir defteri, işlem akışı."""
    return book_reject(cfg, client, symbol, price) or tape_reject(cfg, client, symbol)
