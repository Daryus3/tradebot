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

Uzun vadeli (swing) stratejiler — 1 SAATLİK mumlar, 1-5 gün tutulan, %8-15 hedefli işlemler:
  - "swing"         : Çok günlük zirve kırılımı (trend takibi). 1 saatlik EMA50 > EMA200 (≈ 2 gün / 8 gün),
                      EMA200 yükseliyor ve fiyat 7 gün öncesinin üstündeyken, son kapanan saatlik mum son
                      SWING_LOOKBACK_HOURS (varsayılan 72 = 3 gün) saatin zirvesini yüksek hacimle kırınca alır.
  - "swing_pullback": Aynı çok günlük yükseliş trendinde fiyat zirveden %4-12 geri çekilip EMA50'ye inince ve
                      saatlik mumda yukarı dönünce alır.
5 dk stratejilerinde 5 dakikalık hareketler komisyon + spread'i (~%0.25) karşılamadı; günlerce süren %8-15'lik
hareketlerde aynı maliyet hedefin küçük bir parçasıdır. Çıkış ayarları da buna göre olmalıdır (bkz.
config.SWING_EXIT_PROFILE). Backtest 1 yıllık saatlik veriyle yapılır:
    python main.py --backtest --gun 365 --strateji swing

Ortalamaya dönüş ("mean_revert", 1 SAATLİK mumlar, saatler-2 gün, küçük hedef): yalnızca en hacimli MR_UNIVERSE
coinde; çok günlük yükseliş trendindeki (EMA50 > EMA200, fiyat EMA200 üstünde) coinin saatlik kapanışı 20 saatlik
ortalamasının MR_BAND_STD standart sapma altına inince (aşırı satım) alır, ortalamaya dönüşte küçük kârla satar
(config.MEAN_REVERSION_PROFILE). Kazanma oranı doğası gereği yüksektir; kâr, kazananların kaybedenleri karşılamasına
bağlıdır:
    python main.py --backtest --strateji mean_revert

Haftalık momentum rotasyonu ("rotation", günlük mumlar): sinyal/kâr al mantığı yerine en güçlü birkaç coini tutar ve
her hafta yeniden dengeler (bkz. rotation.py). Canlı botta TradingBot.rotation_tick, backtest'te
_Simulator._rotation_step aynı rotation.plan_rotation fonksiyonunu kullanır:
    python main.py --backtest --strateji rotation
"""
from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from binance_client import BinanceClient, RateLimitError
from config import STRATEGY_NAMES
from rotation import RotationPlan, btc_trend, momentum_pct, plan_rotation
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
STRATEGIES = STRATEGY_NAMES
BACKTEST_WARMUP_DAYS = 1.5   # 5 dk stratejileri: 24 saatlik ön eleme penceresi + 300 mumluk (25 saat) analiz geçmişi

# Uzun vadeli (swing) stratejiler: 1 saatlik mumlar
SWING_INTERVAL = "1h"
SWING_KLINE_LIMIT = 400          # ~16.7 gün: EMA200 (≈ 8 gün) ve 7 günlük momentum için yeterli geçmiş
SWING_MIN_BARS = 260
SWING_SLOPE_BARS = 24            # EMA200 son 24 saatte yükselmiş olmalı
SWING_MOMENTUM_BARS = 168        # swing: fiyat 7 gün (168 saat) öncesinin üstünde olmalı
SWING_MAX_EXTENSION_PCT = 8.0    # Fiyat EMA50'nin bu kadar üstündeyse "fazla uzamış", kovalanmaz
SWING_MAX_RSI = 78.0
SWING_MAX_BARS_SINCE_LOW = 12    # swing_pullback: dönüş en fazla 12 saat önceki dipten başlamış olmalı
SWING_MAX_RETRACE = 0.6          # swing_pullback: geri çekilmenin en fazla %60'ı geri alınmış olmalı (yukarıda yer kalsın)
SWING_DIP_RSI = 45.0             # swing_pullback: geri çekilmede RSI bunun altını görmüş olmalı
# Canlı bot saatlik mum kapandıktan sonraki bir saat boyunca her taramada aynı sinyali görür; fiyat kapanıştan bu
# kadar yükseldiyse kovalamaz (backtest kapanış fiyatından alır, canlı giriş de ona yakın olsun)
SWING_MAX_CHASE_PCT = 1.0
SWING_CANDIDATE_FACTOR = 3       # Saatlik mumda sinyal yavaş değişir: her taramada MAX_CANDIDATES'in 3 katı aday incelenir
# MIN_ATR_PCT / MAX_ATR_PCT 5 dk mum içindir. Oynaklık sürenin kareköküyle büyüdüğünden saatlik muma √12 ile
# ölçeklenir (örn. %0.25 → %0.87, %4 → %13.9).
SWING_ATR_SCALE = math.sqrt(12)
SWING_WARMUP_DAYS = 18.0         # Backtest ısınması: 400 saatlik analiz geçmişi (16.7 gün) + pay

# Ortalamaya dönüş (mean_revert): 1 saatlik mumlar, Bollinger tarzı aşırı satım + trend filtresi
MR_BAND_BARS = 20                # Ortalama ve standart sapma son 20 saatlik kapanıştan
MR_STRICT_BAND_EXTRA = 0.5       # Zayıf piyasada aşırı satım eşiği bu kadar standart sapma daha derin
MR_MAX_CHASE_PCT = 0.5           # Fiyat sinyal mumunun kapanışından bu kadar yükseldiyse alınmaz (dönüşün çoğu kaçmış)

# Haftalık momentum rotasyonu: günlük mumlar
ROTATION_INTERVAL = "1d"
ROTATION_WARMUP_DAYS = 210.0     # En uzun momentum (180 gün) / BTC ortalaması (200 gün) için geçmiş
LEVERAGED_SUFFIXES = ("DOWN", "BULL", "BEAR")


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
        if kind == "swing":
            return (f"{int(m.get('lookback_hours', 0))} saatlik zirve kırılımı %{m.get('breakout_pct', 0):.2f} | "
                    f"Trend %{m.get('trend_pct', 0):+.1f} (1s EMA50/200) | 7 gün %{m.get('momentum_pct', 0):+.1f} | "
                    f"RSI {m.get('rsi', 0):.0f} | Hacim x{m.get('vol_ratio', 0):.1f}")
        if kind == "rotation":
            return (f"Momentum %{m.get('momentum_pct', 0):+.1f} (son {int(m.get('lookback_days', 0))} gün) | "
                    f"sıra {int(m.get('rank', 0))}")
        if kind == "mean_revert":
            return (f"Aşırı satım {m.get('z', 0):.1f}σ (20 saatlik ortalamanın altında) | Ortalamaya "
                    f"%{m.get('room_pct', 0):.1f} | Trend %{m.get('trend_pct', 0):+.1f} (1s EMA50/200) | "
                    f"RSI {m.get('rsi', 0):.0f}")
        if kind == "swing_pullback":
            return (f"Trend %{m.get('trend_pct', 0):+.1f} (1s EMA50/200) | Zirveden geri çekilme "
                    f"%{m.get('pullback_pct', 0):.1f} | RSI {m.get('rsi_min', 0):.0f}→{m.get('rsi', 0):.0f} | "
                    f"Hacim x{m.get('vol_ratio', 0):.1f}")
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
    interval = "5m"                # Backtest'in simülasyon aralığı (canlıda KLINE_INTERVAL, varsayılan 5m)
    kline_limit = KLINE_LIMIT
    warmup_days = BACKTEST_WARMUP_DAYS
    uses_room = True               # Sinyal "eski zirveye kalan alan" (room_pct) ölçer mi? (MIN_ROOM_RATIO etkili mi)
    uses_24h_range = True          # Ön elemede MIN_24H_RANGE_PCT (24 saatlik fiyat aralığı) uygulanır mı?
    is_rotation = False            # Sinyal yerine haftalık sıralamayla tutan / satan strateji mi? (RotationStrategy)
    # Canlı bot her adayın analizinden önce çağırır (Telegram komutlarına cevap, süre sınırı); False = taramayı bitir
    between_candidates: Optional[Callable[[], bool]] = None
    # Analiz sonucunu değiştiren ayarlar: backtest'in sinyal önbelleği bunlara göre ayrılır (giriş taraması
    # bunlardan birini değiştirirse önbellekteki eski sinyaller kullanılmaz)
    analysis_fields: Tuple[str, ...] = ("min_dip_pct", "min_retrace", "max_retrace", "rsi_period", "rsi_oversold",
                                        "rsi_entry_min", "rsi_entry_max", "min_volume_ratio", "min_atr_pct",
                                        "max_atr_pct")

    def __init__(self, client: BinanceClient, cfg, coin_filter=None, regime=None):
        self.client = client
        self.cfg = cfg
        self.coin_filter = coin_filter  # coin_filters.CoinSafetyFilter (delist / yeni / pompa)
        self.regime = regime            # market_regime.MarketRegime
        self._seen: Dict[str, int] = {}  # Art arda kaç taramada sinyal verdi (teyit için)
        self._last_scan_at = 0.0
        self.last_btc_1h_pct: Optional[float] = None

    @property
    def bar_interval(self) -> str:
        """Canlı taramada istenen mum aralığı."""
        return self.cfg.kline_interval

    @property
    def candidate_limit(self) -> int:
        """Her taramada ayrıntılı analiz edilecek en fazla aday."""
        return self.cfg.max_candidates

    def analysis_key(self) -> tuple:
        return tuple(getattr(self.cfg, f) for f in self.analysis_fields)

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

    def candidate_key(self, change_24h_pct: float, quote_volume_24h: float = 0.0) -> float:
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
            if r.base_asset.endswith(LEVERAGED_SUFFIXES):  # Kaldıraçlı tokenler
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
            if self.uses_24h_range and (high - low) / low * 100 < cfg.min_24h_range_pct:
                continue
            # Fiyat adımı çok kabaysa (düşük fiyatlı coinler) hedefler hassas yakalanamaz
            if r.tick_size > 0 and float(r.tick_size) / last > 0.002:
                continue
            # Küçük bakiyede LOT adımı büyükse satışta ciddi 'toz' kaybı olur
            if invest_hint > 0 and r.step_value(last) / invest_hint * 100 > cfg.max_dust_pct:
                continue
            out.append(t)
        out.sort(key=lambda x: self.candidate_key(float(x["priceChangePercent"]), float(x["quoteVolume"])))
        return out[: self.candidate_limit]

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
        for i, t in enumerate(candidates):
            if self.between_candidates is not None and not self.between_candidates():
                result.rejects["tarama süresi doldu"] += len(candidates) - i
                log.warning("Tarama süre sınırına ulaştı: %d adaydan %d tanesi incelenmedi (Binance yavaş yanıt "
                            "veriyor olabilir)", len(candidates), len(candidates) - i)
                break
            symbol = t["symbol"]
            if self.coin_filter is not None:
                safe, why = self.coin_filter.check(symbol)
                if not safe:
                    result.rejects[why.split(" (")[0]] += 1
                    result.near_misses.append((symbol, why))
                    continue
            try:
                raw = self.client.klines(symbol, self.bar_interval, self.kline_limit)
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
def _volatility_reject(cfg, closed: List[Kline], highs, lows, closes, price: float, scale: float = 1.0):
    """Ortak ATR filtresi; (atr_pct, ortalama_mum_aralığı_pct, elenme_nedeni_veya_boş).

    MIN/MAX_ATR_PCT 5 dk mum içindir; `scale` daha uzun mumlar için eşikleri büyütür (1 saat: √12).
    """
    avg_range_pct = mean([(k.high - k.low) / k.close * 100 for k in closed[-24:] if k.close > 0])
    atr_vals = atr(highs, lows, closes, 14)
    atr_pct = atr_vals[-1] / price * 100 if atr_vals[-1] and price > 0 else 0.0
    if atr_pct < cfg.min_atr_pct * scale:
        return atr_pct, avg_range_pct, "oynaklık düşük"
    if atr_pct > cfg.max_atr_pct * scale:
        return atr_pct, avg_range_pct, "aşırı oynak"
    return atr_pct, avg_range_pct, ""


class _TrendBase(ReboundStrategy):
    """Yükselen coinler evreni (24s değişim MIN_24H_GAIN_PCT..MAX_24H_GAIN_PCT) ve uzun mum geçmişi."""
    kline_limit = TREND_KLINE_LIMIT

    def change_ok(self, change_24h_pct: float) -> bool:
        return self.cfg.min_24h_gain_pct <= change_24h_pct <= self.cfg.max_24h_gain_pct

    def candidate_key(self, change_24h_pct: float, quote_volume_24h: float = 0.0) -> float:
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
    analysis_fields = ("trend_pullback_min_pct", "trend_pullback_max_pct", "rsi_period", "min_volume_ratio",
                       "min_atr_pct", "max_atr_pct")

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
    uses_room = False   # Kırılımda üstte "eski zirve" yoktur
    analysis_fields = ("breakout_lookback_bars", "breakout_volume_ratio", "rsi_period", "min_atr_pct", "max_atr_pct")

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


# ================================================================ uzun vadeli (swing) stratejiler
class _SwingBase(_TrendBase):
    """1 saatlik mumlar, çok günlük trend. Kısa vadeli stratejilerle aynı tarama altyapısını kullanır."""
    interval = SWING_INTERVAL
    kline_limit = SWING_KLINE_LIMIT
    warmup_days = SWING_WARMUP_DAYS
    # Günlerce süren işlemde son 24 saatin oynaklığı belirleyici değil (sıkışma sonrası kırılım dar aralıktan
    # başlar); oynaklık saatlik ATR filtresiyle ölçülür
    uses_24h_range = False

    @property
    def bar_interval(self) -> str:
        return SWING_INTERVAL      # KLINE_INTERVAL yalnızca 5 dk stratejileri içindir

    @property
    def candidate_limit(self) -> int:
        return self.cfg.max_candidates * SWING_CANDIDATE_FACTOR

    @staticmethod
    def _chased(last: Kline, price: float) -> bool:
        return last.close > 0 and price > last.close * (1 + SWING_MAX_CHASE_PCT / 100)

    @staticmethod
    def _uptrend(closes: List[float], price: float):
        """Çok günlük yükseliş trendi: (ema50, ema200, elenme_nedeni_veya_boş)."""
        e50, e200 = ema(closes, 50), ema(closes, 200)
        if None in (e50[-1], e200[-1], e200[-1 - SWING_SLOPE_BARS]):
            return e50, e200, "veri az"
        if e50[-1] <= e200[-1] or price <= e200[-1]:
            return e50, e200, "trend yok"
        if e200[-1] <= e200[-1 - SWING_SLOPE_BARS]:
            return e50, e200, "trend zayıf"
        return e50, e200, ""


class SwingBreakoutStrategy(_SwingBase):
    """Çok günlük yükseliş trendinde son SWING_LOOKBACK_HOURS saatin zirvesini yüksek hacimle kıranı al."""
    name = "swing"
    uses_room = False   # Kırılımda üstte "eski zirve" yoktur
    analysis_fields = ("swing_lookback_hours", "swing_volume_ratio", "rsi_period", "min_atr_pct", "max_atr_pct")

    def change_ok(self, change_24h_pct: float) -> bool:
        # Taze kırılımda fiyat son günlerin zirvesindedir, 24 saat öncesinin altında olamaz
        return 0 <= change_24h_pct <= self.cfg.max_24h_gain_pct

    def analyze(self, symbol: str, raw_klines: List[list], strict: bool = False) -> Tuple[Optional[Signal], str]:
        cfg = self.cfg
        lookback = cfg.swing_lookback_hours
        if len(raw_klines) < max(SWING_MIN_BARS, lookback + 30, SWING_MOMENTUM_BARS + 2):
            return None, "veri az"
        current, closed, closes, highs, lows, vols = self._series(raw_klines)
        price, last = current.close, closed[-1]

        # 1) Kırılım (en seçici kontrol önce): son kapanan saatlik mum önceki `lookback` saatin zirvesinin üstünde
        #    kapandı, bir önceki mum henüz kırmamıştı (taze kırılım) ve fiyat hâlâ kırılım seviyesinde
        prior_high = max(highs[-lookback - 1:-1])
        if last.close <= prior_high:
            return None, "kırılım yok"
        if closes[-2] > max(highs[-lookback - 2:-2]):
            return None, "kırılım eski"
        if last.close <= last.open:
            return None, "son mum kırmızı"
        if price < prior_high * 0.995:
            return None, "kırılım geri döndü"
        if self._chased(last, price):
            return None, "fiyat kaçtı"
        breakout_pct = (last.close / prior_high - 1) * 100

        # 2) Çok günlük yükseliş trendi ve 7 günlük momentum
        e50, e200, why = self._uptrend(closes, price)
        if why:
            return None, why
        momentum_pct = (price / closes[-SWING_MOMENTUM_BARS] - 1) * 100 if closes[-SWING_MOMENTUM_BARS] > 0 else 0.0
        if momentum_pct <= 0:
            return None, "momentum yok"

        # 3) Hacim: kırılım mumu son 24 saatin ortalamasının belirgin üstünde
        base_vol = mean(vols[-25:-1])
        vol_ratio = vols[-1] / base_vol if base_vol > 0 else 0.0
        if vol_ratio < cfg.swing_volume_ratio * (1.3 if strict else 1.0):
            return None, "hacim zayıf"

        # 4) Kovalamama: aşırı alım / EMA50'den çok uzak değil
        rsi_now = rsi(closes, cfg.rsi_period)[-1]
        if rsi_now is None:
            return None, "veri az"
        if rsi_now > SWING_MAX_RSI:
            return None, "aşırı alım"
        if (price - e50[-1]) / e50[-1] * 100 > SWING_MAX_EXTENSION_PCT:
            return None, "fazla uzamış"

        atr_pct, avg_range_pct, why = _volatility_reject(cfg, closed, highs, lows, closes, price, SWING_ATR_SCALE)
        if why:
            return None, why
        taker_ratio = last.taker_buy_quote / last.quote_volume if last.quote_volume > 0 else 0.5

        trend_pct = (e50[-1] / e200[-1] - 1) * 100
        score = (min(vol_ratio, 5) * 2 + min(breakout_pct, 4) * 2 + min(trend_pct, 10) * 1.5
                 + min(momentum_pct, 30) / 3 + min(avg_range_pct, 5) * 2 + (taker_ratio - 0.5) * 10)
        metrics = {
            "kind": "swing", "breakout_pct": breakout_pct, "trend_pct": trend_pct, "momentum_pct": momentum_pct,
            "rsi": rsi_now, "vol_ratio": vol_ratio, "avg_range_pct": avg_range_pct, "taker_ratio": taker_ratio,
            "atr_pct": atr_pct, "lookback_hours": float(lookback),
        }
        return Signal(symbol, price, score, metrics), ""


class SwingPullbackStrategy(_SwingBase):
    """Çok günlük yükseliş trendinde zirveden %4-12 geri çekilip EMA50'ye inen ve yukarı dönen coini al."""
    name = "swing_pullback"
    analysis_fields = ("swing_lookback_hours", "swing_pullback_min_pct", "swing_pullback_max_pct", "rsi_period",
                       "min_volume_ratio", "min_atr_pct", "max_atr_pct")

    def change_ok(self, change_24h_pct: float) -> bool:
        return -self.cfg.swing_pullback_max_pct <= change_24h_pct <= self.cfg.max_24h_gain_pct

    def candidate_key(self, change_24h_pct: float, quote_volume_24h: float = 0.0) -> float:
        return change_24h_pct   # En çok geri çekilen önce

    def analyze(self, symbol: str, raw_klines: List[list], strict: bool = False) -> Tuple[Optional[Signal], str]:
        cfg = self.cfg
        lookback = cfg.swing_lookback_hours
        if len(raw_klines) < max(SWING_MIN_BARS, lookback + 30):
            return None, "veri az"
        current, closed, closes, highs, lows, vols = self._series(raw_klines)
        price, n, last = current.close, len(closed), closed[-1]

        # 1) Geri çekilme (ucuz kontrol önce): son `lookback` saatin zirvesinden sonraki dip
        start = n - lookback
        hi_idx = max(range(start, n), key=lambda i: highs[i])
        lo_idx = min(range(hi_idx, n), key=lambda i: lows[i])
        recent_high, pull_low = highs[hi_idx], lows[lo_idx]
        if recent_high <= 0 or pull_low <= 0:
            return None, "veri hatalı"
        pullback = (recent_high - pull_low) / recent_high * 100
        if pullback < cfg.swing_pullback_min_pct * (1.25 if strict else 1.0):
            return None, "geri çekilme az"
        if pullback > cfg.swing_pullback_max_pct:
            return None, "geri çekilme derin"
        bars_since_low = n - 1 - lo_idx
        if bars_since_low < 1 or current.low < pull_low:
            return None, "geri çekilme sürüyor"
        if bars_since_low > SWING_MAX_BARS_SINCE_LOW:
            return None, "dip eski"
        if self._chased(last, price):
            return None, "fiyat kaçtı"
        retrace = (price - pull_low) / (recent_high - pull_low)
        if retrace > SWING_MAX_RETRACE:
            return None, "toparlanma bitmiş"

        # 2) Dönüş: son saatlik mum yeşil ve bir öncekinin zirvesinin üstünde kapandı
        if last.close <= last.open:
            return None, "son mum kırmızı"
        if last.close <= closed[-2].high:
            return None, "dönüş teyidi yok"

        # 3) Trend sağlam: dip EMA50 civarına inmiş ama EMA200'ün altında kapanmamış
        e50, e200, why = self._uptrend(closes, price)
        if why:
            return None, why
        if e50[lo_idx] is None or e200[lo_idx] is None:
            return None, "veri az"
        if pull_low > e50[lo_idx] * 1.01:
            return None, "EMA50'ye inmedi"
        if closes[lo_idx] < e200[lo_idx]:
            return None, "trend kırıldı"
        e9 = ema(closes, 9)
        if e9[-1] is None or price <= e9[-1]:
            return None, "momentum yok"

        # 4) RSI: geri çekilmede soğumuş, şimdi yükseliyor
        rsi_vals = rsi(closes, cfg.rsi_period)
        rsi_now, rsi_prev = rsi_vals[-1], rsi_vals[-2]
        dip_rsis = [v for v in rsi_vals[max(lo_idx - 2, 0):] if v is not None]
        if rsi_now is None or rsi_prev is None or not dip_rsis:
            return None, "veri az"
        rsi_min = min(dip_rsis)
        if rsi_min > SWING_DIP_RSI:
            return None, "RSI soğumadı"
        if not 40 <= rsi_now <= 65:
            return None, "RSI aralık dışı"
        if rsi_now <= rsi_prev:
            return None, "RSI yükselmiyor"

        # 5) Hacim: dönüş mumlarında hacim son 24 saatin ortalamasının üstünde
        base_vol = mean(vols[-27:-3])
        vol_ratio = mean(vols[-3:]) / base_vol if base_vol > 0 else 0.0
        if vol_ratio < cfg.min_volume_ratio * (1.3 if strict else 1.0):
            return None, "hacim zayıf"
        recent_quote = sum(vols[-3:])
        taker_ratio = sum(k.taker_buy_quote for k in closed[-3:]) / recent_quote if recent_quote > 0 else 0.5

        atr_pct, avg_range_pct, why = _volatility_reject(cfg, closed, highs, lows, closes, price, SWING_ATR_SCALE)
        if why:
            return None, why

        trend_pct = (e50[-1] / e200[-1] - 1) * 100
        score = (min(trend_pct, 10) * 1.5 + pullback + (1 - retrace) * 5 + min(vol_ratio, 4) * 2
                 + (rsi_now - rsi_min) / 5 + min(avg_range_pct, 5) * 2 + (taker_ratio - 0.5) * 10)
        metrics = {
            "kind": "swing_pullback", "trend_pct": trend_pct, "pullback_pct": pullback, "retrace": retrace,
            "rsi": rsi_now, "rsi_min": rsi_min, "vol_ratio": vol_ratio, "avg_range_pct": avg_range_pct,
            "taker_ratio": taker_ratio, "atr_pct": atr_pct, "bars_since_low": float(bars_since_low),
            # Geri çekilme öncesi zirveye kalan alan
            "room_pct": (recent_high - price) / price * 100 if price > 0 else 0.0,
        }
        return Signal(symbol, price, score, metrics), ""


class MeanRevertStrategy(_SwingBase):
    """En hacimli coinlerde, çok günlük yükseliş trendindeyken saatlik kapanış 20 saatlik ortalamanın belirgin altına
    inince (aşırı satım) al; küçük hedefle ortalamaya dönüşte sat (çıkışlar MEAN_REVERSION_PROFILE)."""
    name = "mean_revert"
    # Hedef alanı = 20 saatlik ortalamaya kalan mesafe: MIN_ROOM_RATIO "dönüş hedefi Kâr Al'a yetiyor mu?" ölçer
    uses_room = True
    analysis_fields = ("mr_band_std", "rsi_period")

    @property
    def candidate_limit(self) -> int:
        return self.cfg.mr_universe

    def change_ok(self, change_24h_pct: float) -> bool:
        return -self.cfg.max_24h_drop_pct <= change_24h_pct <= self.cfg.max_24h_gain_pct

    def candidate_key(self, change_24h_pct: float, quote_volume_24h: float = 0.0) -> float:
        return -quote_volume_24h   # En hacimli (en likit, en büyük) coinler önce

    def analyze(self, symbol: str, raw_klines: List[list], strict: bool = False) -> Tuple[Optional[Signal], str]:
        cfg = self.cfg
        if len(raw_klines) < SWING_MIN_BARS:
            return None, "veri az"
        current, closed, closes, highs, lows, vols = self._series(raw_klines)
        price, last = current.close, closed[-1]

        # 1) Aşırı satım (en seçici kontrol önce): son kapanan saatlik mum 20 saatlik ortalamanın en az
        #    MR_BAND_STD standart sapma altında kapandı
        window = closes[-MR_BAND_BARS:]
        avg = mean(window)
        sd = math.sqrt(sum((x - avg) ** 2 for x in window) / len(window))
        if avg <= 0 or sd <= 0:
            return None, "veri az"
        z = (last.close - avg) / sd
        band = cfg.mr_band_std + (MR_STRICT_BAND_EXTRA if strict else 0.0)
        if z > -band:
            return None, "aşırı satım yok"
        if last.close > 0 and price > last.close * (1 + MR_MAX_CHASE_PCT / 100):
            return None, "fiyat kaçtı"
        if price >= avg:
            return None, "ortalamaya dönmüş"

        # 2) Çok günlük yükseliş trendi: yalnızca yükselen coinin düşüşü alınır (düşen bıçak tutulmaz)
        e50, e200 = ema(closes, 50), ema(closes, 200)
        if e50[-1] is None or e200[-1] is None:
            return None, "veri az"
        if e50[-1] <= e200[-1] or last.close <= e200[-1]:
            return None, "trend yok"

        rsi_now = rsi(closes, cfg.rsi_period)[-1]
        if rsi_now is None:
            return None, "veri az"
        base_vol = mean(vols[-25:-1])
        vol_ratio = vols[-1] / base_vol if base_vol > 0 else 0.0
        atr_vals = atr(highs, lows, closes, 14)
        atr_pct = atr_vals[-1] / price * 100 if atr_vals[-1] and price > 0 else 0.0
        room_pct = (avg - price) / price * 100 if price > 0 else 0.0
        trend_pct = (e50[-1] / e200[-1] - 1) * 100
        # Daha derin aşırı satım, ortalamaya daha çok alan ve daha güçlü trend önce
        score = -z * 3 + min(room_pct, 5) * 2 + min(trend_pct, 10) * 0.5 + (50 - rsi_now) / 10
        metrics = {
            "kind": "mean_revert", "z": z, "room_pct": room_pct, "trend_pct": trend_pct, "rsi": rsi_now,
            "vol_ratio": vol_ratio, "atr_pct": atr_pct, "band_std": band,
        }
        return Signal(symbol, price, score, metrics), ""


# ================================================================ haftalık momentum rotasyonu
class RotationStrategy(ReboundStrategy):
    """En hacimli coinler arasında son ROTATION_LOOKBACK_DAYS günün en güçlü ROTATION_TOP_N'ini tutar.

    Sinyal taraması yapmaz: canlı bot yeniden dengeleme zamanı geldiğinde `plan()` çağırır. `scan()` yalnızca
    self-test ve /tarama gibi bilgi amaçlı çağrılar içindir (o an alınacak coinleri sinyal olarak listeler).
    """
    name = "rotation"
    interval = ROTATION_INTERVAL
    warmup_days = ROTATION_WARMUP_DAYS
    uses_room = False
    uses_24h_range = False
    is_rotation = True
    analysis_fields: Tuple[str, ...] = ()

    @property
    def kline_limit(self) -> int:   # type: ignore[override]
        return max(self.cfg.rotation_lookback_days, self.cfg.rotation_btc_ma_days) + 2

    @property
    def bar_interval(self) -> str:
        return ROTATION_INTERVAL

    # ------------------------------------------------------------ veri
    def daily_closes(self, symbol: str, days: int) -> List[float]:
        """Son `days` KAPANMIŞ günün kapanışları (Binance'in son satırı bugünün açık mumudur, atılır)."""
        rows = self.client.klines(symbol, ROTATION_INTERVAL, days + 1) or []
        return [float(r[4]) for r in rows[:-1]]

    def universe(self, tickers: List[dict], rules: Dict[str, SymbolRules], excluded_symbols: Set[str]) -> List[str]:
        """Sıralamaya girecek en hacimli ROTATION_UNIVERSE parite (stabil / fiat / kaldıraçlı / ince fiyat adımlı hariç)."""
        cfg = self.cfg
        rows: List[Tuple[float, str]] = []
        for t in tickers:
            symbol = t.get("symbol", "")
            r = rules.get(symbol)
            if r is None or symbol in excluded_symbols or r.base_asset in cfg.excluded_assets:
                continue
            if r.base_asset.endswith(LEVERAGED_SUFFIXES):
                continue
            try:
                last, quote_vol = float(t["lastPrice"]), float(t["quoteVolume"])
            except (KeyError, TypeError, ValueError):
                continue
            if last <= 0 or quote_vol < cfg.min_quote_volume_24h:
                continue
            if r.tick_size > 0 and float(r.tick_size) / last > 0.002:
                continue
            rows.append((quote_vol, symbol))
        rows.sort(key=lambda x: (-x[0], x[1]))
        return [sym for _, sym in rows[: cfg.rotation_universe]]

    def risk_on(self) -> Tuple[Optional[bool], str]:
        """BTC trend filtresi: (olumlu mu, açıklama)."""
        cfg = self.cfg
        ma = cfg.rotation_btc_ma_days
        if ma <= 0 or cfg.quote_asset != "USDT":
            return True, ""
        closes = self.daily_closes("BTCUSDT", ma)
        on, avg = btc_trend(closes, ma)
        if on is None:
            return None, "BTC günlük verisi yetersiz, trend filtresi uygulanamadı"
        word = "üstünde" if on else "ALTINDA"
        return on, f"BTC {closes[-1]:,.0f} — {ma} günlük ortalamanın ({avg:,.0f}) {word}"

    def plan(self, held: List[str], rules: Dict[str, SymbolRules], excluded_symbols: Set[str],
             slots: int) -> Tuple[RotationPlan, str]:
        """Yeniden dengeleme planı ve açıklaması. Ağ hatası / istek limiti yukarı iletilir (döngü tekrar dener)."""
        cfg = self.cfg
        risk, note = self.risk_on()
        if risk is False:
            return plan_rotation(held, {}, slots, risk), note
        tickers = self.client.tickers_24h()
        universe = self.universe(tickers, rules, excluded_symbols)
        if self.coin_filter is not None:
            try:
                self.coin_filter.refresh_delist()
            except RateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("Delist listesi yenilenemedi: %s", exc)
        momentum: Dict[str, Optional[float]] = {}
        skipped: List[str] = []
        for symbol in universe:
            if self.coin_filter is not None:
                safe, why = self.coin_filter.check(symbol)
                if not safe:   # Delist takvimi / yeni listelenme / pompa: sıralamaya girmez (elde varsa satılır)
                    skipped.append(f"{symbol} ({why})")
                    continue
            momentum[symbol] = momentum_pct(self.daily_closes(symbol, cfg.rotation_lookback_days + 1),
                                            cfg.rotation_lookback_days)
        if skipped:
            note = (note + " | " if note else "") + "güvenlik filtresi: " + ", ".join(skipped[:5])
        return plan_rotation(held, momentum, slots, risk), note

    # ------------------------------------------------------------ bilgi amaçlı tarama (self-test, /tarama)
    def scan(self, rules: Dict[str, SymbolRules], excluded_symbols: Set[str], invest_hint: float) -> ScanResult:
        result = ScanResult()
        plan, note = self.plan([], rules, excluded_symbols, self.cfg.rotation_top_n)
        result.market_ok = plan.risk_on is not False
        result.market_note = note
        result.candidates = result.analyzed = len(plan.ranking)
        for symbol in plan.candidates[: plan.wanted]:
            mom = dict(plan.ranking)[symbol]
            result.signals.append(Signal(symbol, 0.0, mom, {"kind": "rotation", "momentum_pct": mom,
                                                            "rank": float(plan.rank_of(symbol) or 0),
                                                            "lookback_days": float(self.cfg.rotation_lookback_days)}))
        return result


STRATEGY_CLASSES = {"rebound": ReboundStrategy, "trend": TrendPullbackStrategy, "breakout": BreakoutStrategy,
                    "swing": SwingBreakoutStrategy, "swing_pullback": SwingPullbackStrategy,
                    "mean_revert": MeanRevertStrategy, "rotation": RotationStrategy}
assert tuple(STRATEGY_CLASSES) == STRATEGIES


def strategy_class(name: str):
    """Strateji sınıfı (interval, uses_room gibi sınıf bilgilerine nesne oluşturmadan erişmek için)."""
    return STRATEGY_CLASSES[name]


def make_strategy(client, cfg, coin_filter=None, regime=None) -> ReboundStrategy:
    """STRATEGY ayarına göre strateji nesnesi (canlı bot, self-test ve backtest aynı fabrikayı kullanır)."""
    return strategy_class(getattr(cfg, "strategy", "rebound"))(client, cfg, coin_filter=coin_filter, regime=regime)
