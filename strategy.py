"""
Strateji: "Düşüş sonrası toparlanma" (dip rebound) tarayıcısı.

Her taramada (varsayılan 30 sn):
  1. Ön eleme (tek istek, 24 saatlik veriler):
     - Yeterli hacim (likidite), son 24 saatte düşmüş, yeterince oynak coinler.
     - Stabil coinler / fiat / küçük bakiyeye uygun olmayan LOT adımına sahip pariteler elenir.
  2. Ayrıntılı analiz (adaylar için 5 dakikalık mumlar):
     - Son ~3 saatte belirgin bir düşüş (dip) olmuş olmalı.
     - Dip en az 2 mum önce oluşmuş ve fiyat dipten toparlanmaya başlamış olmalı,
       ama düşüşün en fazla belirli bir kısmını geri almış olmalı (yukarıda yer kalsın).
     - RSI dipte aşırı satımdaydı ve şimdi yükseliyor.
     - Fiyat EMA(9) üzerinde ve EMA(9) yukarı dönmüş; son kapanan mum yeşil.
     - Toparlanma mumlarında hacim ortalamanın üzerinde.
  3. Puanlama: düşüş derinliği, kalan toparlanma alanı, hacim, RSI ivmesi ve oynaklık.
  4. (İsteğe bağlı, MIN_ROOM_RATIO) Hedef alanı: fiyatın düşüş öncesi zirveye kadar olan mesafesi
     Kâr Al hedefine yetmiyorsa alım yapılmaz. Toparlanma çoğu zaman eski zirvede durur; hedef o
     zirvenin üstündeyse işlem genelde ya süre dolunca ya da zarar kesle kapanır.

Amaç: 15-60 dakika içinde sonuçlanabilecek kısa vadeli toparlanma hareketlerini yakalamak.
Ek güvenlik: BTC son 1 saatte sert düşüyorsa (piyasa geneli çöküyorsa) alım yapılmaz.

STRATEGY ayarıyla iki alternatif giriş yaklaşımı seçilebilir (tarama altyapısı, filtreler ve çıkış kuralları
aynıdır; yalnızca hangi coinlere bakılacağı ve alım sinyali değişir):
  - "trend"   : Yükselen trendde geri çekilmeden alım. Son 24 saatte YÜKSELEN coinler arasında, 5 dk
                EMA50 > EMA200 (≈ son 17 saatlik yükseliş trendi) olanlarda, fiyat EMA20'ye kadar geri
                çekilip tekrar yukarı dönünce alır ("yükselen trendde ucuzlayınca al").
  - "breakout": Kırılım. Yükselen trenddeki coinlerde fiyat son 4 saatin zirvesini yüksek hacimle
                yukarı kırınca alır.
Düşen coinlerde dip avlayan "rebound" stratejisi backtestlerde iki ayrı 90 günlük dönemde de kaybettirdi;
diğer ikisi trendin yönünde işlem yapar. Hangisinin işe yaradığını backtest gösterir:
    python main.py --backtest --gun 90 --strateji-karsilastir
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from binance_client import BinanceClient, RateLimitError
from exchange import SymbolRules
from indicators import atr, ema, mean, rsi
from logger import get_logger

log = get_logger()

DIP_WINDOW = 36        # 5 dk mumlarla son 3 saat içinde dip ara
MAX_BARS_SINCE_LOW = 24  # Dip en fazla 2 saat önce oluşmuş olmalı
MIN_BARS_SINCE_LOW = 2   # Dipten sonra en az 2 kapanmış mum (toparlanma başlamış)
KLINE_LIMIT = 100

# Trend / kırılım stratejileri: EMA200 için 5 dk mumlarla ~25 saatlik veri (Binance'te 100-499 mum aynı ağırlık)
TREND_KLINE_LIMIT = 300
TREND_MIN_BARS = 250
TREND_HIGH_WINDOW = 36       # Geri çekilmenin başladığı zirve son 3 saat içinde aranır
TREND_SLOPE_BARS = 24        # EMA200 son 2 saatte yükselmiş olmalı
TREND_MAX_EXTENSION_PCT = 3.0  # Fiyat EMA50'nin / EMA20'nin bu kadar üstündeyse "fazla uzamış", kovalanmaz
BREAKOUT_MAX_RSI = 75.0
STRATEGIES = ("rebound", "trend", "breakout")


@dataclass
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float
    taker_buy_quote: float

    @classmethod
    def from_raw(cls, row: list) -> "Kline":
        return cls(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                   float(row[7]), float(row[10]))


@dataclass
class Signal:
    symbol: str
    price: float
    score: float
    metrics: Dict[str, float] = field(default_factory=dict)

    def note(self) -> str:
        m = self.metrics
        kind = m.get("kind", "rebound")
        if kind == "trend":
            return (f"Trend %{m.get('trend_pct', 0):+.2f} (EMA50/200) | Geri çekilme %{m.get('pullback_pct', 0):.1f} | "
                    f"RSI {m.get('rsi', 0):.0f} | Hacim x{m.get('vol_ratio', 0):.1f}")
        if kind == "breakout":
            return (f"Kırılım %{m.get('breakout_pct', 0):.2f} ({int(m.get('lookback_bars', 0)) * 5} dk zirvesi) | "
                    f"Trend %{m.get('trend_pct', 0):+.2f} | RSI {m.get('rsi', 0):.0f} | Hacim x{m.get('vol_ratio', 0):.1f}")
        return (f"Düşüş %{m.get('dip_pct', 0):.1f} | Toparlanma %{m.get('retrace', 0) * 100:.0f} | "
                f"RSI {m.get('rsi_min', 0):.0f}→{m.get('rsi', 0):.0f} | Hacim x{m.get('vol_ratio', 0):.1f}")


@dataclass
class ScanResult:
    signals: List[Signal] = field(default_factory=list)
    candidates: int = 0
    analyzed: int = 0
    market_ok: bool = True
    market_note: str = ""
    rejects: Counter = field(default_factory=Counter)
    regime: str = "NORMAL"
    regime_note: str = ""
    btc_1h_pct: Optional[float] = None
    near_misses: List[Tuple[str, str]] = field(default_factory=list)  # (parite, elenme nedeni)

    def summary(self) -> str:
        if not self.market_ok:
            return f"Tarama atlandı: {self.market_note}"
        top = ", ".join(f"{s.symbol}({s.score:.1f})" for s in self.signals[:3]) or "yok"
        rej = ", ".join(f"{k}:{v}" for k, v in self.rejects.most_common(4))
        return (f"Tarama: {self.candidates} aday, {self.analyzed} analiz, {len(self.signals)} sinyal "
                f"[{top}]" + (f" | elenme: {rej}" if rej else ""))


def required_move_pct(cfg) -> float:
    """Net Kâr Al hedefine ulaşmak için gereken yaklaşık FİYAT artışı (%): hedef + alış/satış komisyonu."""
    return ((1 + cfg.take_profit_pct / 100) / (1 - cfg.fee_rate) ** 2 - 1) * 100


def room_ok(signal: Signal, cfg) -> bool:
    """Hedef alanı filtresi (MIN_ROOM_RATIO > 0 ise): düşüş öncesi zirveye kadar olan alan, Kâr Al için
    gereken fiyat hareketinin en az MIN_ROOM_RATIO katı olmalı. Canlı tarama ve backtest aynı fonksiyonu kullanır.
    """
    ratio = getattr(cfg, "min_room_ratio", 0.0)
    room = signal.metrics.get("room_pct")
    if ratio <= 0 or room is None:   # Kırılım stratejisinde "eski zirve" yoktur: filtre uygulanmaz
        return True
    return room >= ratio * required_move_pct(cfg)


class ReboundStrategy:
    name = "rebound"
    kline_limit = KLINE_LIMIT

    def __init__(self, client: BinanceClient, cfg, coin_filter=None, regime=None):
        self.client = client
        self.cfg = cfg
        self.coin_filter = coin_filter  # coin_filters.CoinSafetyFilter (delist / yeni / pompa)
        self.regime = regime            # market_regime.MarketRegime
        self._seen: Dict[str, int] = {}  # Art arda kaç taramada sinyal verdi (teyit için)
        self._last_scan_at = 0.0
        self.last_btc_1h_pct: Optional[float] = None

    # ------------------------------------------------------------ piyasa filtresi
    def market_ok(self) -> Tuple[bool, str]:
        """BTC son 1 saatte sert düşüyorsa altcoin toparlanmaları genelde başarısız olur."""
        if not self.cfg.btc_filter or self.cfg.quote_asset != "USDT":
            return True, ""
        rows = self.client.klines("BTCUSDT", "5m", 13)
        if len(rows) < 13:
            return True, ""
        closed = [Kline.from_raw(r) for r in rows[:-1]]
        change = (float(rows[-1][4]) - closed[0].open) / closed[0].open * 100
        self.last_btc_1h_pct = change
        if change <= -self.cfg.btc_max_1h_drop_pct:
            return False, f"BTC son 1 saatte %{change:.2f} düştü (piyasa riskli)"
        return True, f"BTC 1s: %{change:+.2f}"

    # ------------------------------------------------------------ evren (hangi coinlere bakılır)
    def change_ok(self, change_24h_pct: float) -> bool:
        """24 saatlik değişim bu stratejinin aradığı aralıkta mı? (rebound: düşmüş coinler)"""
        return -self.cfg.max_24h_drop_pct <= change_24h_pct <= -self.cfg.min_24h_drop_pct

    def candidate_key(self, change_24h_pct: float) -> float:
        """Aday sıralaması (küçükten büyüğe): rebound için en çok düşen önce."""
        return change_24h_pct

    # ------------------------------------------------------------ ön eleme
    def prefilter(self, tickers: List[dict], rules: Dict[str, SymbolRules],
                  excluded_symbols: Set[str], invest_hint: float) -> List[dict]:
        cfg = self.cfg
        out = []
        for t in tickers:
            symbol = t.get("symbol", "")
            r = rules.get(symbol)
            if r is None or symbol in excluded_symbols or r.base_asset in cfg.excluded_assets:
                continue
            if r.base_asset.endswith(("DOWN", "BULL", "BEAR")):  # Kaldıraçlı tokenler
                continue
            try:
                last = float(t["lastPrice"])
                change = float(t["priceChangePercent"])
                quote_vol = float(t["quoteVolume"])
                high, low = float(t["highPrice"]), float(t["lowPrice"])
            except (KeyError, TypeError, ValueError):
                continue
            if last <= 0 or low <= 0:
                continue
            if quote_vol < cfg.min_quote_volume_24h:
                continue
            if not self.change_ok(change):
                continue
            if (high - low) / low * 100 < cfg.min_24h_range_pct:
                continue
            # Fiyat adımı çok kabaysa (düşük fiyatlı coinler) hedefler hassas yakalanamaz
            if r.tick_size > 0 and float(r.tick_size) / last > 0.002:
                continue
            # Küçük bakiyede LOT adımı büyükse satışta ciddi 'toz' kaybı olur
            if invest_hint > 0 and r.step_value(last) / invest_hint * 100 > cfg.max_dust_pct:
                continue
            out.append(t)
        out.sort(key=lambda x: self.candidate_key(float(x["priceChangePercent"])))
        return out[: cfg.max_candidates]

    # ------------------------------------------------------------ ayrıntılı analiz
    def analyze(self, symbol: str, raw_klines: List[list], strict: bool = False) -> Tuple[Optional[Signal], str]:
        """Sinyal varsa (Signal, ""), yoksa (None, elenme_nedeni) döner.

        strict=True: piyasa 'zayıf' rejimdeyken giriş kuralları sıkılaşır (daha derin aşırı satım,
        daha güçlü hacim teyidi).
        """
        cfg = self.cfg
        rsi_oversold = cfg.rsi_oversold - (5 if strict else 0)
        min_volume_ratio = cfg.min_volume_ratio * (1.3 if strict else 1.0)
        if len(raw_klines) < 60:
            return None, "veri az"
        klines = [Kline.from_raw(r) for r in raw_klines]
        current = klines[-1]          # Henüz kapanmamış mum
        closed = klines[:-1]
        price = current.close

        closes = [k.close for k in closed]
        highs = [k.high for k in closed]
        lows = [k.low for k in closed]
        n = len(closed)

        # 1) Son 3 saatteki zirve ve sonrasındaki dip
        start = n - DIP_WINDOW
        hi_idx = max(range(start, n), key=lambda i: highs[i])
        lo_idx = min(range(hi_idx, n), key=lambda i: lows[i])
        high, low = highs[hi_idx], lows[lo_idx]
        if high <= 0 or low <= 0:
            return None, "veri hatalı"
        dip_pct = (high - low) / high * 100
        if dip_pct < cfg.min_dip_pct:
            return None, "düşüş az"

        bars_since_low = n - 1 - lo_idx
        if bars_since_low < MIN_BARS_SINCE_LOW or current.low < low:
            return None, "dip sürüyor"
        if bars_since_low > MAX_BARS_SINCE_LOW:
            return None, "dip eski"

        # 2) Toparlanma oranı: düşüşün ne kadarı geri alındı?
        retrace = (price - low) / (high - low)
        if retrace < cfg.min_retrace:
            return None, "toparlanma yok"
        if retrace > cfg.max_retrace:
            return None, "toparlanma bitmiş"

        # 3) RSI: dipte aşırı satım, şimdi yükseliyor
        rsi_vals = rsi(closes, cfg.rsi_period)
        rsi_now, rsi_prev = rsi_vals[-1], rsi_vals[-3]
        dip_rsis = [v for v in rsi_vals[max(lo_idx - 3, 0):] if v is not None]
        if rsi_now is None or rsi_prev is None or not dip_rsis:
            return None, "veri az"
        rsi_min = min(dip_rsis)
        if rsi_min > rsi_oversold:
            return None, "RSI aşırı satım görmedi"
        if not cfg.rsi_entry_min <= rsi_now <= cfg.rsi_entry_max:
            return None, "RSI aralık dışı"
        if rsi_now <= rsi_prev:
            return None, "RSI yükselmiyor"

        # 4) Kısa vadeli momentum
        ema_fast = ema(closes, 9)
        if ema_fast[-1] is None or ema_fast[-2] is None:
            return None, "veri az"
        if price <= ema_fast[-1] or ema_fast[-1] <= ema_fast[-2]:
            return None, "momentum yok"
        if closed[-1].close <= closed[-1].open:
            return None, "son mum kırmızı"

        # 5) Hacim teyidi
        vols = [k.quote_volume for k in closed]
        base_vol = mean(vols[-23:-3])
        vol_ratio = mean(vols[-3:]) / base_vol if base_vol > 0 else 0.0
        if vol_ratio < min_volume_ratio:
            return None, "hacim zayıf"
        recent_quote = sum(vols[-3:])
        taker_ratio = sum(k.taker_buy_quote for k in closed[-3:]) / recent_quote if recent_quote > 0 else 0.5

        # 6) Oynaklık: 15-60 dk içinde hedefe gidebilmek için fiyat yeterince hareketli olmalı,
        #    ama zarar kesi gürültüyle tetikleyecek kadar da çılgın olmamalı (ATR filtresi)
        avg_range_pct = mean([(k.high - k.low) / k.close * 100 for k in closed[-24:] if k.close > 0])
        atr_vals = atr(highs, lows, closes, 14)
        atr_pct = atr_vals[-1] / price * 100 if atr_vals[-1] and price > 0 else 0.0
        if atr_pct < cfg.min_atr_pct:
            return None, "oynaklık düşük"
        if atr_pct > cfg.max_atr_pct:
            return None, "aşırı oynak"

        score = (
            dip_pct * 1.0
            + (1 - retrace) * 5
            + min(vol_ratio, 4) * 2
            + (rsi_now - rsi_min) / 5
            + min(avg_range_pct, 5) * 3
            + (taker_ratio - 0.5) * 10
        )
        metrics = {
            "dip_pct": dip_pct, "retrace": retrace, "rsi": rsi_now, "rsi_min": rsi_min,
            "vol_ratio": vol_ratio, "avg_range_pct": avg_range_pct, "taker_ratio": taker_ratio,
            "bars_since_low": float(bars_since_low), "atr_pct": atr_pct,
            # Düşüş öncesi zirveye kadar kalan fiyat alanı (%): tam toparlanmada en fazla bu kadar yükselir
            "room_pct": (high - price) / price * 100 if price > 0 else 0.0,
            "kind": "rebound",
        }
        return Signal(symbol, price, score, metrics), ""

    # ------------------------------------------------------------ tam tarama
    def scan(self, rules: Dict[str, SymbolRules], excluded_symbols: Set[str],
             invest_hint: float) -> ScanResult:
        cfg = self.cfg
        result = ScanResult()
        ok, note = self.market_ok()
        result.market_ok, result.market_note = ok, note
        result.btc_1h_pct = self.last_btc_1h_pct
        if not ok:
            self._seen.clear()
            return result

        tickers = self.client.tickers_24h()
        if self.regime is not None:
            reg = self.regime.evaluate(tickers, rules)
            result.regime, result.regime_note = reg.regime, reg.note
            if reg.regime == "COKUS":
                result.market_ok = False
                result.market_note = f"Piyasa çöküş modunda: {reg.note}"
                self._seen.clear()
                return result
            if reg.regime == "ZAYIF" and getattr(cfg, "regime_skip_weak", False):
                result.market_ok = False
                result.market_note = f"Piyasa zayıf, REGIME_SKIP_WEAK açık: {reg.note}"
                self._seen.clear()
                return result
        strict = result.regime == "ZAYIF"
        if self.coin_filter is not None:
            try:
                self.coin_filter.refresh_delist()
            except RateLimitError:
                raise  # Binance istek limiti: tarama durmalı, döngü beklesin
            except Exception as exc:  # noqa: BLE001
                log.debug("Delist listesi yenilenemedi: %s", exc)

        candidates = self.prefilter(tickers, rules, excluded_symbols, invest_hint)
        result.candidates = len(candidates)
        found: Dict[str, Signal] = {}
        for t in candidates:
            symbol = t["symbol"]
            if self.coin_filter is not None:
                safe, why = self.coin_filter.check(symbol)
                if not safe:
                    result.rejects[why.split(" (")[0]] += 1
                    result.near_misses.append((symbol, why))
                    continue
            try:
                raw = self.client.klines(symbol, cfg.kline_interval, self.kline_limit)
            except RateLimitError:
                raise  # Binance istek limiti: tarama durmalı, döngü beklesin
            except Exception as exc:  # noqa: BLE001 - tek coin hatası taramayı durdurmasın
                log.debug("%s mum verisi alınamadı: %s", symbol, exc)
                result.rejects["veri hatası"] += 1
                continue
            result.analyzed += 1
            signal, reason = self.analyze(symbol, raw, strict=strict)
            if signal is None:
                result.rejects[reason] += 1
                result.near_misses.append((symbol, reason))
                continue
            if cfg.min_signal_score > 0 and signal.score < cfg.min_signal_score:
                result.rejects["puan düşük"] += 1
                result.near_misses.append((symbol, f"puan düşük ({signal.score:.1f})"))
                continue
            if not room_ok(signal, cfg):
                result.rejects["hedef alanı dar"] += 1
                result.near_misses.append((symbol, f"hedef alanı dar (%{signal.metrics['room_pct']:.1f})"))
                continue
            found[symbol] = signal

        # Art arda tarama teyidi: sinyal N taramadır kesintisiz görülüyor mu?
        # (Slot doluyken / durdurulmuşken tarama yapılmadıysa önceki sayımlar "art arda" sayılmaz.)
        now = time.time()
        if now - self._last_scan_at > 2 * cfg.scan_interval_sec:
            self._seen = {}
        self._last_scan_at = now
        self._seen = {s: self._seen.get(s, 0) + 1 for s in found}
        for symbol, signal in found.items():
            if self._seen[symbol] >= cfg.signal_confirm_scans:
                result.signals.append(signal)
            else:
                result.rejects["teyit bekleniyor"] += 1
                result.near_misses.append((symbol, "teyit bekleniyor"))
        result.signals.sort(key=lambda s: s.score, reverse=True)
        return result


# ================================================================ alternatif stratejiler
def _volatility_reject(cfg, closed: List[Kline], highs, lows, closes, price: float):
    """Ortak ATR filtresi; (atr_pct, ortalama_mum_aralığı_pct, elenme_nedeni_veya_boş)."""
    avg_range_pct = mean([(k.high - k.low) / k.close * 100 for k in closed[-24:] if k.close > 0])
    atr_vals = atr(highs, lows, closes, 14)
    atr_pct = atr_vals[-1] / price * 100 if atr_vals[-1] and price > 0 else 0.0
    if atr_pct < cfg.min_atr_pct:
        return atr_pct, avg_range_pct, "oynaklık düşük"
    if atr_pct > cfg.max_atr_pct:
        return atr_pct, avg_range_pct, "aşırı oynak"
    return atr_pct, avg_range_pct, ""


class _TrendBase(ReboundStrategy):
    """Yükselen coinler evreni (24s değişim MIN_24H_GAIN_PCT..MAX_24H_GAIN_PCT) ve uzun mum geçmişi."""
    kline_limit = TREND_KLINE_LIMIT

    def change_ok(self, change_24h_pct: float) -> bool:
        return self.cfg.min_24h_gain_pct <= change_24h_pct <= self.cfg.max_24h_gain_pct

    def candidate_key(self, change_24h_pct: float) -> float:
        return -change_24h_pct  # En çok yükselen önce

    @staticmethod
    def _series(raw_klines: List[list]):
        klines = [Kline.from_raw(r) for r in raw_klines]
        closed = klines[:-1]
        return (klines[-1], closed, [k.close for k in closed], [k.high for k in closed],
                [k.low for k in closed], [k.quote_volume for k in closed])


class TrendPullbackStrategy(_TrendBase):
    """Yükselen trendde EMA20'ye geri çekilip tekrar yukarı dönünce al."""
    name = "trend"

    def analyze(self, symbol: str, raw_klines: List[list], strict: bool = False) -> Tuple[Optional[Signal], str]:
        cfg = self.cfg
        if len(raw_klines) < TREND_MIN_BARS:
            return None, "veri az"
        current, closed, closes, highs, lows, vols = self._series(raw_klines)
        price, n = current.close, len(closed)
        e9, e20, e50, e200 = ema(closes, 9), ema(closes, 20), ema(closes, 50), ema(closes, 200)
        if None in (e9[-2], e20[-1], e50[-1], e200[-1], e200[-1 - TREND_SLOPE_BARS]):
            return None, "veri az"

        # 1) Trend: EMA50 > EMA200, fiyat EMA200 üstünde, EMA200 yükseliyor
        if e50[-1] <= e200[-1] or price <= e200[-1]:
            return None, "trend yok"
        if e200[-1] <= e200[-1 - TREND_SLOPE_BARS]:
            return None, "trend zayıf"

        # 2) Geri çekilme: son 3 saatin zirvesinden sonraki dip, EMA20'ye değmiş olmalı
        start = n - TREND_HIGH_WINDOW
        hi_idx = max(range(start, n), key=lambda i: highs[i])
        lo_idx = min(range(hi_idx, n), key=lambda i: lows[i])
        recent_high, pull_low = highs[hi_idx], lows[lo_idx]
        pullback = (recent_high - pull_low) / recent_high * 100 if recent_high > 0 else 0.0
        min_pb = cfg.trend_pullback_min_pct * (1.25 if strict else 1.0)
        if pullback < min_pb:
            return None, "geri çekilme az"
        if pullback > cfg.trend_pullback_max_pct:
            return None, "geri çekilme derin"
        if lo_idx >= n - 1 or current.low < pull_low:
            return None, "geri çekilme sürüyor"
        if pull_low > e20[lo_idx] * 1.002:
            return None, "EMA20'ye inmedi"
        if closes[lo_idx] < e50[lo_idx] * 0.99:
            return None, "trend kırıldı"

        # 3) Yeniden yükseliş: son mum yeşil, fiyat yükselen EMA9 üstünde, RSI toparlanıyor
        if closed[-1].close <= closed[-1].open:
            return None, "son mum kırmızı"
        if price <= e9[-1] or e9[-1] <= e9[-2]:
            return None, "momentum yok"
        rsi_vals = rsi(closes, cfg.rsi_period)
        rsi_now, rsi_prev = rsi_vals[-1], rsi_vals[-3]
        if rsi_now is None or rsi_prev is None:
            return None, "veri az"
        if not 45 <= rsi_now <= 68:
            return None, "RSI aralık dışı"
        if rsi_now <= rsi_prev:
            return None, "RSI yükselmiyor"
        if (price - e50[-1]) / e50[-1] * 100 > TREND_MAX_EXTENSION_PCT:
            return None, "fazla uzamış"

        # 4) Hacim teyidi
        base_vol = mean(vols[-23:-3])
        vol_ratio = mean(vols[-3:]) / base_vol if base_vol > 0 else 0.0
        if vol_ratio < cfg.min_volume_ratio * (1.3 if strict else 1.0):
            return None, "hacim zayıf"
        recent_quote = sum(vols[-3:])
        taker_ratio = sum(k.taker_buy_quote for k in closed[-3:]) / recent_quote if recent_quote > 0 else 0.5

        atr_pct, avg_range_pct, why = _volatility_reject(cfg, closed, highs, lows, closes, price)
        if why:
            return None, why

        trend_pct = (e50[-1] / e200[-1] - 1) * 100
        score = (min(trend_pct, 5) * 3 + pullback * 1.5 + min(vol_ratio, 4) * 2 + (rsi_now - rsi_prev) / 2
                 + min(avg_range_pct, 5) * 3 + (taker_ratio - 0.5) * 10)
        metrics = {
            "kind": "trend", "trend_pct": trend_pct, "pullback_pct": pullback, "rsi": rsi_now,
            "vol_ratio": vol_ratio, "avg_range_pct": avg_range_pct, "taker_ratio": taker_ratio,
            "atr_pct": atr_pct, "bars_since_low": float(n - 1 - lo_idx),
            # Geri çekilme öncesi zirveye kalan alan: hedef bu zirvenin çok üstündeyse işlem zorlaşır
            "room_pct": (recent_high - price) / price * 100 if price > 0 else 0.0,
        }
        return Signal(symbol, price, score, metrics), ""


class BreakoutStrategy(_TrendBase):
    """Yükselen trendde son BREAKOUT_LOOKBACK_BARS mumun zirvesini yüksek hacimle kıranı al."""
    name = "breakout"

    def analyze(self, symbol: str, raw_klines: List[list], strict: bool = False) -> Tuple[Optional[Signal], str]:
        cfg = self.cfg
        lookback = cfg.breakout_lookback_bars
        if len(raw_klines) < max(TREND_MIN_BARS, lookback + 30):
            return None, "veri az"
        current, closed, closes, highs, lows, vols = self._series(raw_klines)
        price, last = current.close, closed[-1]
        e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
        if None in (e20[-1], e50[-1], e200[-1]):
            return None, "veri az"

        # 1) Trend
        if e50[-1] <= e200[-1] or price <= e50[-1]:
            return None, "trend yok"

        # 2) Kırılım: son kapanan mum önceki `lookback` mumun zirvesinin üstünde kapandı (ve bir önceki mum
        #    henüz kırmamıştı → kırılım taze); fiyat hâlâ kırılım seviyesinde
        prior_high = max(highs[-lookback - 1:-1])
        if last.close <= prior_high:
            return None, "kırılım yok"
        if closes[-2] > max(highs[-lookback - 2:-2]):
            return None, "kırılım eski"
        if last.close <= last.open:
            return None, "son mum kırmızı"
        if price < prior_high * 0.998:
            return None, "kırılım geri döndü"
        breakout_pct = (last.close / prior_high - 1) * 100

        # 3) Hacim: kırılım mumu son 2 saatin ortalamasının belirgin üstünde
        base_vol = mean(vols[-25:-1])
        vol_ratio = vols[-1] / base_vol if base_vol > 0 else 0.0
        if vol_ratio < cfg.breakout_volume_ratio * (1.3 if strict else 1.0):
            return None, "hacim zayıf"

        # 4) Kovalamama: aşırı alım / EMA20'den çok uzak değil
        rsi_vals = rsi(closes, cfg.rsi_period)
        rsi_now = rsi_vals[-1]
        if rsi_now is None:
            return None, "veri az"
        if rsi_now > BREAKOUT_MAX_RSI:
            return None, "aşırı alım"
        if (price - e20[-1]) / e20[-1] * 100 > TREND_MAX_EXTENSION_PCT:
            return None, "fazla uzamış"

        atr_pct, avg_range_pct, why = _volatility_reject(cfg, closed, highs, lows, closes, price)
        if why:
            return None, why
        taker_ratio = last.taker_buy_quote / last.quote_volume if last.quote_volume > 0 else 0.5

        trend_pct = (e50[-1] / e200[-1] - 1) * 100
        score = (min(vol_ratio, 6) * 2 + min(breakout_pct, 3) * 3 + min(trend_pct, 5) * 3
                 + min(avg_range_pct, 5) * 3 + (taker_ratio - 0.5) * 10)
        metrics = {
            "kind": "breakout", "breakout_pct": breakout_pct, "trend_pct": trend_pct, "rsi": rsi_now,
            "vol_ratio": vol_ratio, "avg_range_pct": avg_range_pct, "taker_ratio": taker_ratio,
            "atr_pct": atr_pct, "lookback_bars": float(lookback),
        }
        return Signal(symbol, price, score, metrics), ""


def make_strategy(client, cfg, coin_filter=None, regime=None) -> ReboundStrategy:
    """STRATEGY ayarına göre strateji nesnesi (canlı bot, self-test ve backtest aynı fabrikayı kullanır)."""
    classes = {"rebound": ReboundStrategy, "trend": TrendPullbackStrategy, "breakout": BreakoutStrategy}
    return classes[getattr(cfg, "strategy", "rebound")](client, cfg, coin_filter=coin_filter, regime=regime)
