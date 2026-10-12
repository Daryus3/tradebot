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

Amaç: 15-60 dakika içinde sonuçlanabilecek kısa vadeli toparlanma hareketlerini yakalamak.
Ek güvenlik: BTC son 1 saatte sert düşüyorsa (piyasa geneli çöküyorsa) alım yapılmaz.
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


class ReboundStrategy:
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
            if not -cfg.max_24h_drop_pct <= change <= -cfg.min_24h_drop_pct:
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
        out.sort(key=lambda x: float(x["priceChangePercent"]))  # En çok düşen önce
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
                raw = self.client.klines(symbol, cfg.kline_interval, KLINE_LIMIT)
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
