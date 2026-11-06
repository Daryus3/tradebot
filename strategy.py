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

Pozitif RSI uyumsuzluğu ("rsi_divergence", 1 SAATLİK mumlar, aynı çıkış grubu): en hacimli RSI_DIV_UNIVERSE coinde
fiyat daha düşük dip yaparken RSI daha yüksek dip yapınca (satış gücü azalıyor; bkz. position_rules.bullish_divergence)
ve son kapanan saatlik mum dönüşü teyit edince (yeşil, dipten beri görülen en yüksek seviyenin üstünde, ortalamanın
üstünde hacimle) alır. RSI_DIV_TREND_FILTER=true ise yalnızca fiyat 1 saatlik EMA200'ün üstündeyken:
    python main.py --backtest --strateji rsi_divergence

Gün içi al-sat ("intraday", 5 dk mumlar): yalnızca en büyük coinlerde (INTRADAY_COINS, varsayılan Kasım 2023'ün ilk
10'u). Giriş taktiği INTRADAY_TACTIC: vwap (gün başı VWAP'ından sapma), bollinger, rsi2, orb (açılış aralığı
kırılımı), ema_cross, squeeze, donchian, pullback, dip (bkz. config.INTRADAY_TACTICS). Küçük hedef / zarar kes, işlem
gün içinde kapanır (config.INTRADAY_PROFILE). Hangi taktiğin işe yaradığını giriş taraması gösterir:
    python main.py --backtest --strateji intraday --gun 1095 --tara-giris
9 yıllık testte (2017-2026, her dönem kendi başındaki ilk 10 coinle) hiçbir taktik kâr etmedi: işlem başına sonuç
alış-satış maliyetine (~%0.2) eşitti, komisyonsuzken bile avantaj en fazla +%0.13 / işlem (vwap + trend filtresi).

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
from config import (STRATEGY_NAMES, fee_from_coin, futures_crash_rules, futures_modules, futures_pump_rules,
                    intraday_coin_list, intraday_tactic_list, rotation_coin_list, rotation_hold_count)
from rotation import RotationPlan, btc_trend, momentum_pct, plan_rotation
from exchange import SymbolRules
from entry_filters import candle_reject, prior_trend_reject
from indicators import atr, ema, mean, rsi
from logger import get_logger
from position_rules import bullish_divergence

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

# Pozitif RSI uyumsuzluğu (rsi_divergence): 1 saatlik mumlar, en hacimli coinler, dönüş teyidiyle
DIV_MAX_RETRACE = 0.6            # Dipten iki dip arasındaki tepeye kadarki yolun en fazla %60'ı alınmış olmalı
DIV_STRICT_OVERSOLD = 5.0        # Zayıf piyasada ilk dipteki RSI bu kadar daha düşük olmalı
DIV_STRICT_RISE = 2.0            # ...ve ikinci dipte RSI bu kadar daha fazla yükselmiş olmalı

# Haftalık momentum rotasyonu: günlük mumlar
# Gün içi al-sat (intraday, 5 dk mumlar, en büyük coinler)
INTRADAY_INTERVAL = "5m"
INTRADAY_KLINE_LIMIT = 400          # EMA200 trend filtresi + gün başından beri (en fazla 288 mum) VWAP / açılış aralığı
INTRADAY_MIN_BARS = 250
INTRADAY_DAY_MS = 86_400_000        # Gün başı: UTC 00:00 (Binance günlük mumu)
INTRADAY_SESSION_MIN_BARS = 24      # vwap: gün başından en az 2 saat geçmiş olmalı (VWAP otursun)
INTRADAY_STRICT_BAND_EXTRA = 0.5    # Zayıf piyasada aşırı satım bandı bu kadar standart sapma daha derin
INTRADAY_ORB_MAX_BARS = 144         # orb: kırılım gün başından en geç 12 saat sonra (sonrası "açılış" sayılmaz)
INTRADAY_SQUEEZE_BARS = 120         # squeeze: bant genişliği son 10 saatle karşılaştırılır
INTRADAY_DIP_WINDOW = 36            # dip: zirve / dip son 3 saatte aranır
INTRADAY_DIP_MAX_BARS = 18          # dip: dip en fazla 1.5 saat önce oluşmuş olmalı
INTRADAY_DIP_ATR_MULT = 4.0         # dip: düşüş en az 5 dk ATR'nin bu katı
INTRADAY_MAX_EXTENSION_PCT = 1.5    # ema_cross / pullback: fiyat EMA21 / EMA20'nin bu kadar üstündeyse kovalanmaz
INTRADAY_TACTIC_LABELS = {
    "vwap": "VWAP sapması", "bollinger": "Bollinger alt bandı", "rsi2": "RSI(2) aşırı satım",
    "orb": "Açılış aralığı kırılımı", "ema_cross": "EMA 9/21 kesişimi", "squeeze": "Sıkışma kırılımı",
    "donchian": "Zirve (Donchian) kırılımı", "pullback": "Trend içi geri çekilme", "dip": "Sert düşüş dönüşü",
}
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
        if kind == "intraday":
            parts = [INTRADAY_TACTIC_LABELS.get(str(m.get("tactic", "")), str(m.get("tactic", "")))]
            if "z" in m:
                parts.append(f"VWAP'tan {m['z']:.1f}σ")
            if "rsi2" in m:
                parts.append(f"RSI(2) {m['rsi2']:.0f}")
            if "dip_pct" in m:
                parts.append(f"Düşüş %{m['dip_pct']:.1f}")
            if "breakout_pct" in m:
                parts.append(f"Kırılım %{m['breakout_pct']:.2f}")
            if "room_pct" in m:
                parts.append(f"Hedefe %{m['room_pct']:.1f}")
            if "vol_ratio" in m:
                parts.append(f"Hacim x{m['vol_ratio']:.1f}")
            if "rsi" in m:
                parts.append(f"RSI {m['rsi']:.0f}")
            if "trend_pct" in m:
                parts.append(f"Trend %{m['trend_pct']:+.2f}")
            return " | ".join(parts)
        if kind == "rotation":
            return (f"Momentum %{m.get('momentum_pct', 0):+.1f} (son {int(m.get('lookback_days', 0))} gün) | "
                    f"sıra {int(m.get('rank', 0))}")
        if kind == "mean_revert":
            return (f"Aşırı satım {m.get('z', 0):.1f}σ (20 saatlik ortalamanın altında) | Ortalamaya "
                    f"%{m.get('room_pct', 0):.1f} | Trend %{m.get('trend_pct', 0):+.1f} (1s EMA50/200) | "
                    f"RSI {m.get('rsi', 0):.0f}")
        if kind == "rsi_divergence":
            return (f"RSI uyumsuzluğu: dip %{m.get('price_drop_pct', 0):+.1f}, RSI {m.get('rsi_prev', 0):.0f}→"
                    f"{m.get('rsi_low', 0):.0f} ({int(m.get('bars_apart', 0))} saat arayla) | Dönüş mumu hacmi "
                    f"x{m.get('vol_ratio', 0):.1f} | Tepeye %{m.get('room_pct', 0):.1f} | RSI {m.get('rsi', 0):.0f}")
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
            # Küçük bakiyede LOT adımı büyükse satışta ciddi 'toz' kaybı olur (komisyon BNB'den ödeniyorsa olmaz)
            if invest_hint > 0 and fee_from_coin(cfg) and r.step_value(last) / invest_hint * 100 > cfg.max_dust_pct:
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
            why = candle_reject(cfg, raw)   # Ek mum filtreleri (FAKEOUT / VWAP / OVERHEAT; backtest'te de aynı)
            if not why:
                why = prior_trend_reject(cfg, self.client, symbol)   # Yalnızca sinyal veren coinde ek istek
            if why:
                result.rejects[why] += 1
                result.near_misses.append((symbol, why))
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


# ================================================================ gün içi: en büyük coinler (5 dk)
class _IntradayBars:
    """Kapanmış 5 dk mumların sütunları (gerektiği kadarı, sondan) + anlık fiyat. Taktikler pencerenin yalnızca
    ihtiyaç duyduğu kısmını okur (backtest'te her mumda 10 coin analiz edilir); bir analizde hesaplanan ATR / trend /
    gün başı bilgisi saklanır."""
    __slots__ = ("rows", "n", "price", "_cols", "_memo")

    def __init__(self, raw_klines: List[list]):
        self.rows = raw_klines[:-1]          # Son satır henüz kapanmamış mumdur (kapanışı = güncel fiyat)
        self.n = len(self.rows)
        self.price = float(raw_klines[-1][4])
        self._cols: Dict[Tuple[int, int], List[float]] = {}
        self._memo: Dict[str, object] = {}

    def col(self, idx: int, count: int) -> List[float]:
        """Son `count` kapanmış mumun `idx` sütunu (1 açılış, 2 en yüksek, 3 en düşük, 4 kapanış, 7 quote hacim)."""
        key = (idx, count)
        out = self._cols.get(key)
        if out is None:
            out = [float(r[idx]) for r in self.rows[-count:]]
            self._cols[key] = out
        return out

    def green(self) -> bool:
        last = self.rows[-1]
        return float(last[4]) > float(last[1])

    def session(self) -> int:
        """Son kapanmış mumun günü (UTC 00:00'dan beri) kaç kapanmış mum içeriyor."""
        m = self._memo.get("session")
        if m is None:
            day = int(self.rows[-1][0]) // INTRADAY_DAY_MS
            m = 0
            for r in reversed(self.rows):
                if int(r[0]) // INTRADAY_DAY_MS != day:
                    break
                m += 1
            self._memo["session"] = m
        return m   # type: ignore[return-value]

    def atr_pct(self) -> float:
        val = self._memo.get("atr")
        if val is None:
            h, lo, c = self.col(2, 60), self.col(3, 60), self.col(4, 60)
            a = atr(h, lo, c, 14)[-1]
            val = a / self.price * 100 if a and self.price > 0 else 0.0
            self._memo["atr"] = val
        return val   # type: ignore[return-value]

    def trend(self) -> Optional[Tuple[bool, float]]:
        """(yükseliş trendi mi: EMA50 > EMA200 ve kapanış EMA200 üstünde, EMA50/EMA200 farkı %); veri azsa None."""
        if "trend" not in self._memo:
            c = self.col(4, INTRADAY_KLINE_LIMIT)
            e50, e200 = ema(c, 50), ema(c, 200)
            if e50[-1] is None or e200[-1] is None:
                self._memo["trend"] = None
            else:
                self._memo["trend"] = (e50[-1] > e200[-1] and c[-1] > e200[-1], (e50[-1] / e200[-1] - 1) * 100)
        return self._memo["trend"]   # type: ignore[return-value]

    def volume_ratio(self, base_bars: int = 24) -> float:
        """Son kapanmış mumun hacmi ÷ önceki `base_bars` mumun ortalaması."""
        qv = self.col(7, base_bars + 1)
        base = mean(qv[:-1])
        return qv[-1] / base if base > 0 else 0.0


def _mean_sd(values: List[float]) -> Tuple[float, float]:
    avg = mean(values)
    var = sum((x - avg) ** 2 for x in values) / len(values) if values else 0.0
    return avg, math.sqrt(var) if var > 0 else 0.0


class IntradayStrategy(ReboundStrategy):
    """Yalnızca en büyük coinlerde (INTRADAY_COINS, boşsa en hacimli INTRADAY_UNIVERSE coin) 5 dk mumlarla gün içi
    al-sat. Giriş taktiği INTRADAY_TACTIC (bkz. config.INTRADAY_TACTICS; virgülle birden çoğu: yazılış sırasıyla ilk
    sinyal veren alınır). Çıkışlar config.INTRADAY_PROFILE: küçük Kâr Al / Zarar Kes, en fazla 6 saat.

    Bütün taktiklerde ortak: 5 dk ATR ≥ INTRADAY_MIN_ATR_PCT (fiyat hedefe gidebilecek kadar oynak) ve
    INTRADAY_TREND_FILTER açıksa 5 dk EMA50 > EMA200 ve fiyat EMA200 üstünde (yükselen günde alım).
    """
    name = "intraday"
    interval = INTRADAY_INTERVAL
    kline_limit = INTRADAY_KLINE_LIMIT
    uses_24h_range = False   # Büyük coinler sakin günlerde %4'lük 24 saat aralığını göremeyebilir
    uses_room = True         # vwap / bollinger / rsi2 / pullback / dip hedef alanı ölçer (kırılımlar ölçmez)
    analysis_fields = ("intraday_tactic", "intraday_band_std", "intraday_rsi2_max", "intraday_orb_minutes",
                       "intraday_breakout_bars", "intraday_volume_ratio", "intraday_trend_filter",
                       "intraday_min_atr_pct", "max_atr_pct", "rsi_period")

    @property
    def bar_interval(self) -> str:
        return INTRADAY_INTERVAL   # Taktiklerin süreleri (açılış aralığı, gün başı) 5 dk mumlara göredir

    @property
    def candidate_limit(self) -> int:
        coins = intraday_coin_list(self.cfg)
        return len(coins) if coins else self.cfg.intraday_universe

    def change_ok(self, change_24h_pct: float) -> bool:
        return -self.cfg.max_24h_drop_pct <= change_24h_pct <= self.cfg.max_24h_gain_pct

    def candidate_key(self, change_24h_pct: float, quote_volume_24h: float = 0.0) -> float:
        return -quote_volume_24h   # En hacimli (en büyük) coinler önce

    def prefilter(self, tickers: List[dict], rules: Dict[str, SymbolRules],
                  excluded_symbols: Set[str], invest_hint: float) -> List[dict]:
        coins = intraday_coin_list(self.cfg)
        if coins:
            wanted = {c + self.cfg.quote_asset for c in coins}
            tickers = [t for t in tickers if t.get("symbol") in wanted]
        return super().prefilter(tickers, rules, excluded_symbols, invest_hint)

    def analyze(self, symbol: str, raw_klines: List[list], strict: bool = False) -> Tuple[Optional[Signal], str]:
        if len(raw_klines) <= INTRADAY_MIN_BARS:
            return None, "veri az"
        bars = _IntradayBars(raw_klines)
        if bars.price <= 0:
            return None, "veri hatalı"
        first = ""
        for tactic in intraday_tactic_list(self.cfg):
            fn = getattr(self, f"_t_{tactic}", None)
            if fn is None:
                continue
            signal, why = fn(symbol, bars, strict)
            if signal is not None:
                return signal, ""
            first = first or why
        return None, first or "sinyal yok"

    # ------------------------------------------------------------ ortak son kontroller
    def _finish(self, symbol: str, b: _IntradayBars, tactic: str, score: float, metrics: Dict[str, float],
                need_trend: bool = False) -> Tuple[Optional[Signal], str]:
        cfg = self.cfg
        atr_pct = b.atr_pct()
        if atr_pct < cfg.intraday_min_atr_pct:
            return None, "oynaklık düşük"
        if atr_pct > cfg.max_atr_pct:
            return None, "aşırı oynak"
        out: Dict[str, float] = {"kind": "intraday", "tactic": tactic, "atr_pct": atr_pct, **metrics}  # type: ignore
        if need_trend or cfg.intraday_trend_filter:
            trend = b.trend()
            if trend is None:
                return None, "veri az"
            if not trend[0]:
                return None, "trend yok"
            out["trend_pct"] = trend[1]
        return Signal(symbol, b.price, score + min(atr_pct, 1.0) * 2, out), ""

    def _band(self, strict: bool) -> float:
        return self.cfg.intraday_band_std + (INTRADAY_STRICT_BAND_EXTRA if strict else 0.0)

    # ------------------------------------------------------------ ortalamaya dönüş taktikleri
    def _t_vwap(self, symbol: str, b: _IntradayBars, strict: bool):
        """Fiyat gün başından beri oluşan VWAP'ın band × standart sapma altına inip yeşil mumla döndü."""
        m = b.session()
        if m < INTRADAY_SESSION_MIN_BARS:
            return None, "gün yeni başladı"
        rows = b.rows[-m:]
        tps = [(float(r[2]) + float(r[3]) + float(r[4])) / 3 for r in rows]
        vols = [float(r[7]) for r in rows]
        sv = sum(vols)
        if sv <= 0:
            return None, "hacim yok"
        vw = sum(t * v for t, v in zip(tps, vols)) / sv
        var = sum(v * (t - vw) ** 2 for t, v in zip(tps, vols)) / sv
        sd = math.sqrt(var) if var > 0 else 0.0
        if sd <= 0 or vw <= 0:
            return None, "veri az"
        close = float(rows[-1][4])
        z = (close - vw) / sd
        band = self._band(strict)
        if z > -band:
            return None, "VWAP'tan sapma az"
        if not b.green():
            return None, "son mum kırmızı"
        if b.price >= vw:
            return None, "VWAP'a dönmüş"
        room = (vw - b.price) / b.price * 100
        return self._finish(symbol, b, "vwap", -z * 3 + min(room, 3) * 2,
                            {"z": z, "room_pct": room, "band_std": band})

    def _t_bollinger(self, symbol: str, b: _IntradayBars, strict: bool):
        """Önceki mum Bollinger alt bandının altında kapandı, son mum bandın içine yeşil döndü."""
        c = b.col(4, 22)
        if len(c) < 22:
            return None, "veri az"
        band = self._band(strict)
        prev_mid, prev_sd = _mean_sd(c[-21:-1])
        mid, sd = _mean_sd(c[-20:])
        if prev_sd <= 0 or sd <= 0:
            return None, "veri az"
        prev_low = prev_mid - band * prev_sd
        if c[-2] >= prev_low:
            return None, "alt banda inmedi"
        if c[-1] <= mid - band * sd:
            return None, "bant altında sürüyor"
        if not b.green():
            return None, "son mum kırmızı"
        if b.price >= mid:
            return None, "orta banda dönmüş"
        room = (mid - b.price) / b.price * 100
        depth = (prev_low - c[-2]) / prev_sd
        return self._finish(symbol, b, "bollinger", band * 2 + min(depth, 3) * 3 + min(room, 3) * 2,
                            {"room_pct": room, "band_std": band})

    def _t_rsi2(self, symbol: str, b: _IntradayBars, strict: bool):
        """2 periyotluk RSI çok kısa süreli aşırı satımda (INTRADAY_RSI2_MAX altı)."""
        c = b.col(4, 40)
        r2 = rsi(c, 2)[-1]
        if r2 is None:
            return None, "veri az"
        limit = max(1.0, self.cfg.intraday_rsi2_max - (3.0 if strict else 0.0))
        if r2 > limit:
            return None, "RSI(2) yüksek"
        sma5 = mean(c[-5:])
        if b.price >= sma5:
            return None, "ortalamaya dönmüş"
        room = (sma5 - b.price) / b.price * 100
        return self._finish(symbol, b, "rsi2", (limit - r2) * 0.5 + min(room, 3) * 2 + 5,
                            {"rsi2": r2, "room_pct": room})

    def _t_dip(self, symbol: str, b: _IntradayBars, strict: bool):
        """Son 3 saatte ATR'nin en az 4 katı düşüş, dipte RSI 30 altı; dönüş başladı (düşüşün %15-60'ı alındı)."""
        h, lo, c = b.col(2, 60), b.col(3, 60), b.col(4, 60)
        n = len(c)
        start = n - INTRADAY_DIP_WINDOW
        hi_idx = max(range(start, n), key=lambda i: h[i])
        lo_idx = min(range(hi_idx, n), key=lambda i: lo[i])
        high, low = h[hi_idx], lo[lo_idx]
        if high <= 0 or low <= 0 or high <= low:
            return None, "düşüş az"
        drop = (high - low) / high * 100
        if drop < INTRADAY_DIP_ATR_MULT * b.atr_pct() * (1.25 if strict else 1.0):
            return None, "düşüş az"
        since = n - 1 - lo_idx
        if since < 2 or b.price < low:
            return None, "dip sürüyor"
        if since > INTRADAY_DIP_MAX_BARS:
            return None, "dip eski"
        retrace = (b.price - low) / (high - low)
        if retrace < 0.15:
            return None, "toparlanma yok"
        if retrace > 0.6:
            return None, "toparlanma bitmiş"
        rs = rsi(c, self.cfg.rsi_period)
        dip_rsis = [v for v in rs[max(lo_idx - 3, 0):] if v is not None]
        if rs[-1] is None or rs[-3] is None or not dip_rsis:
            return None, "veri az"
        if min(dip_rsis) > (25.0 if strict else 30.0):
            return None, "RSI aşırı satım görmedi"
        if rs[-1] <= rs[-3] or not b.green():
            return None, "dönüş yok"
        room = (high - b.price) / b.price * 100
        return self._finish(symbol, b, "dip", drop * 2 + (1 - retrace) * 5 + (rs[-1] - min(dip_rsis)) / 5,
                            {"dip_pct": drop, "retrace": retrace, "rsi": rs[-1], "room_pct": room})

    def _t_pullback(self, symbol: str, b: _IntradayBars, strict: bool):
        """Yükselen trendde (her zaman şart) EMA20'ye geri çekilip önceki mumun zirvesinin üstünde kapandı."""
        c, h, lo = b.col(4, 120), b.col(2, 120), b.col(3, 120)
        e20 = ema(c, 20)
        if e20[-8] is None:
            return None, "veri az"
        if not any(lo[-i] <= e20[-i] * 1.001 for i in range(2, 8)):
            return None, "EMA20'ye inmedi"
        if c[-1] <= e20[-1] or c[-1] <= h[-2] or not b.green():
            return None, "dönüş teyidi yok"
        r = rsi(c[-60:], self.cfg.rsi_period)[-1]
        if r is None:
            return None, "veri az"
        if not 45 <= r <= 68:
            return None, "RSI aralık dışı"
        if (b.price - e20[-1]) / e20[-1] * 100 > INTRADAY_MAX_EXTENSION_PCT:
            return None, "fazla uzamış"
        room = (max(h[-36:]) - b.price) / b.price * 100
        signal, why = self._finish(symbol, b, "pullback", (r - 45) / 5 + min(max(room, 0), 3),
                                   {"rsi": r, "room_pct": room}, need_trend=True)   # Trend en pahalı kontrol: en son
        if signal is not None:
            signal.score += min(signal.metrics["trend_pct"], 3) * 3
        return signal, why

    # ------------------------------------------------------------ kırılım / momentum taktikleri
    def _t_orb(self, symbol: str, b: _IntradayBars, strict: bool):
        """Gün başının ilk INTRADAY_ORB_MINUTES dakikasındaki zirve, hacimli mumla ilk kez yukarı kırıldı."""
        m = b.session()
        orb = self.cfg.intraday_orb_minutes // 5
        if m <= orb:
            return None, "açılış aralığı oluşmadı"
        if m > INTRADAY_ORB_MAX_BARS:
            return None, "açılıştan çok geçti"
        rows = b.rows[-m:]
        or_high = max(float(r[2]) for r in rows[:orb])
        or_low = min(float(r[3]) for r in rows[:orb])
        close, prev_close = float(rows[-1][4]), float(rows[-2][4])
        if close <= or_high:
            return None, "kırılım yok"
        if prev_close > or_high:
            return None, "kırılım eski"
        if not b.green():
            return None, "son mum kırmızı"
        if b.price < or_high * 0.998:
            return None, "kırılım geri döndü"
        vol_ratio = b.volume_ratio()
        if vol_ratio < self.cfg.intraday_volume_ratio * (1.3 if strict else 1.0):
            return None, "hacim zayıf"
        breakout = (close / or_high - 1) * 100
        width = (or_high - or_low) / or_low * 100 if or_low > 0 else 0.0
        return self._finish(symbol, b, "orb", min(vol_ratio, 6) * 2 + min(breakout, 2) * 3 + min(width, 3),
                            {"breakout_pct": breakout, "range_pct": width, "vol_ratio": vol_ratio})

    def _t_ema_cross(self, symbol: str, b: _IntradayBars, strict: bool):
        """EMA9 son kapanan mumda EMA21'i yukarı kesti (RSI 50-70, fiyat EMA21'den fazla uzaklaşmamış)."""
        c = b.col(4, 120)
        e9, e21 = ema(c, 9), ema(c, 21)
        if e9[-2] is None or e21[-2] is None:
            return None, "veri az"
        if not (e9[-1] > e21[-1] and e9[-2] <= e21[-2]):
            return None, "kesişim yok"
        if not b.green():
            return None, "son mum kırmızı"
        r = rsi(c[-60:], self.cfg.rsi_period)[-1]
        if r is None:
            return None, "veri az"
        if not (55.0 if strict else 50.0) <= r <= 70.0:
            return None, "RSI aralık dışı"
        if (b.price - e21[-1]) / e21[-1] * 100 > INTRADAY_MAX_EXTENSION_PCT:
            return None, "fazla uzamış"
        return self._finish(symbol, b, "ema_cross", (r - 50) / 4 + (e9[-1] / e21[-1] - 1) * 1000,
                            {"rsi": r})

    def _t_squeeze(self, symbol: str, b: _IntradayBars, strict: bool):
        """Bollinger bant genişliği son 10 saatin en dar %15'indeyken üst bant hacimli mumla kırıldı."""
        k = INTRADAY_SQUEEZE_BARS
        c = b.col(4, k + 20)
        if len(c) < k + 20:
            return None, "veri az"
        band = self.cfg.intraday_band_std
        s1, s2 = sum(c[:20]), sum(x * x for x in c[:20])
        widths, mids, sds = [], [], []
        for end in range(20, len(c) + 1):   # Pencere c[end-20:end]
            if end > 20:
                s1 += c[end - 1] - c[end - 21]
                s2 += c[end - 1] ** 2 - c[end - 21] ** 2
            avg = s1 / 20
            var = s2 / 20 - avg * avg
            sd = math.sqrt(var) if var > 0 else 0.0
            mids.append(avg)
            sds.append(sd)
            widths.append(2 * band * sd / avg if avg > 0 else 0.0)
        history = sorted(widths[:-1])
        threshold = history[int(len(history) * 0.15)]
        if min(widths[-7:-1]) > threshold:
            return None, "sıkışma yok"
        upper = mids[-1] + band * sds[-1]
        if c[-1] <= upper:
            return None, "kırılım yok"
        if c[-2] > mids[-2] + band * sds[-2]:
            return None, "kırılım eski"
        if not b.green():
            return None, "son mum kırmızı"
        vol_ratio = b.volume_ratio()
        if vol_ratio < self.cfg.intraday_volume_ratio * (1.3 if strict else 1.0):
            return None, "hacim zayıf"
        breakout = (c[-1] / upper - 1) * 100
        return self._finish(symbol, b, "squeeze", min(vol_ratio, 6) * 2 + min(breakout, 2) * 3 + 3,
                            {"breakout_pct": breakout, "vol_ratio": vol_ratio, "width_pct": widths[-2] * 100})

    def _t_donchian(self, symbol: str, b: _IntradayBars, strict: bool):
        """Son kapanan mum önceki INTRADAY_BREAKOUT_BARS mumun zirvesinin üstünde kapandı (taze, hacimli kırılım)."""
        n = self.cfg.intraday_breakout_bars
        h, c = b.col(2, n + 2), b.col(4, n + 2)
        if len(c) < n + 2:
            return None, "veri az"
        prior_high = max(h[-n - 1:-1])
        if c[-1] <= prior_high:
            return None, "kırılım yok"
        if c[-2] > max(h[-n - 2:-2]):
            return None, "kırılım eski"
        if not b.green():
            return None, "son mum kırmızı"
        if b.price < prior_high * 0.998:
            return None, "kırılım geri döndü"
        vol_ratio = b.volume_ratio()
        if vol_ratio < self.cfg.intraday_volume_ratio * (1.3 if strict else 1.0):
            return None, "hacim zayıf"
        r = rsi(b.col(4, 60), self.cfg.rsi_period)[-1]
        if r is None:
            return None, "veri az"
        if r > BREAKOUT_MAX_RSI:
            return None, "aşırı alım"
        breakout = (c[-1] / prior_high - 1) * 100
        return self._finish(symbol, b, "donchian", min(vol_ratio, 6) * 2 + min(breakout, 2) * 3,
                            {"breakout_pct": breakout, "vol_ratio": vol_ratio, "rsi": r})


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


class RsiDivergenceStrategy(_SwingBase):
    """En hacimli coinlerde 1 saatlik pozitif RSI uyumsuzluğunu dönüş teyidiyle al: fiyat daha düşük dip yaptı ama RSI
    daha yüksek dip yaptı (satış gücü azalıyor) ve son kapanan saatlik mum yeşil, dipten beri görülen en yüksek
    seviyenin üstünde ve ortalamanın üstünde hacimle kapandı. Çıkışlar ortalamaya dönüşle aynı gruptadır
    (MEAN_REVERSION_PROFILE). Canlı botta RSI_DIV_TRADE de ana stratejinin yanında bu sınıfla tarar."""
    name = "rsi_divergence"
    # Hedef alanı = iki dip arasındaki tepeye (boyun) kalan mesafe: MIN_ROOM_RATIO "dönüşe yer var mı?" ölçer
    uses_room = True
    analysis_fields = ("rsi_period", "rsi_div_oversold", "rsi_div_rise", "rsi_div_trend_filter", "min_volume_ratio")

    @property
    def candidate_limit(self) -> int:
        return self.cfg.rsi_div_universe

    def change_ok(self, change_24h_pct: float) -> bool:
        return -self.cfg.max_24h_drop_pct <= change_24h_pct <= self.cfg.max_24h_gain_pct

    def candidate_key(self, change_24h_pct: float, quote_volume_24h: float = 0.0) -> float:
        return -quote_volume_24h   # En hacimli (en likit, en büyük) coinler önce

    def analyze(self, symbol: str, raw_klines: List[list], strict: bool = False) -> Tuple[Optional[Signal], str]:
        cfg = self.cfg
        if len(raw_klines) < SWING_MIN_BARS:
            return None, "veri az"
        # 1) Uyumsuzluk (en seçici kontrol önce; mum nesneleri yalnızca uyumsuzluk varsa oluşturulur). Zayıf piyasada
        #    daha derin aşırı satım ve daha belirgin RSI yükselişi istenir.
        div = bullish_divergence(raw_klines[:-1], cfg.rsi_period,
                                 oversold=cfg.rsi_div_oversold - (DIV_STRICT_OVERSOLD if strict else 0.0),
                                 rise=cfg.rsi_div_rise + (DIV_STRICT_RISE if strict else 0.0))
        if div is None:
            return None, "uyumsuzluk yok"
        current, closed, closes, highs, lows, vols = self._series(raw_klines)
        price, last = current.close, closed[-1]
        p2 = len(closed) - 1 - int(div["bars_since_low"])
        low, peak = div["low"], div["peak"]
        if min(lows[p2:]) < low or current.low < low:
            return None, "dip sürüyor"   # Uyumsuz dipten sonra yeni (uyumsuzluksuz) bir dip geldi

        # 2) Dönüş teyidi: son kapanan mum yeşil ve dipten beri görülen en yüksek seviyenin üstünde kapandı
        if last.close <= last.open:
            return None, "son mum kırmızı"
        if last.close <= max(highs[p2:-1]):
            return None, "dönüş teyidi yok"
        if self._chased(last, price):
            return None, "fiyat kaçtı"
        if peak <= low or price <= 0:
            return None, "veri hatalı"
        retrace = (price - low) / (peak - low)
        if retrace > DIV_MAX_RETRACE:
            return None, "toparlanma bitmiş"

        # 3) Hacim: dönüş mumunun hacmi son 24 saatin ortalamasının en az MIN_VOLUME_RATIO katı
        base_vol = mean(vols[-25:-1])
        vol_ratio = vols[-1] / base_vol if base_vol > 0 else 0.0
        if vol_ratio < cfg.min_volume_ratio * (1.3 if strict else 1.0):
            return None, "hacim zayıf"

        # 4) İsteğe bağlı trend filtresi: düşüş trendinde uyumsuzluk sık sık bozulur ("düşen bıçak")
        e200 = ema(closes, 200)[-1]
        if e200 is None:
            return None, "veri az"
        if cfg.rsi_div_trend_filter and price <= e200:
            return None, "trend yok"

        rsi_now = rsi(closes, cfg.rsi_period)[-1]
        if rsi_now is None:
            return None, "veri az"
        atr_vals = atr(highs, lows, closes, 14)
        atr_pct = atr_vals[-1] / price * 100 if atr_vals[-1] else 0.0
        room_pct = (peak - price) / price * 100
        trend_pct = (price / e200 - 1) * 100
        # RSI'daki güçlenme, ilk dipteki aşırı satımın derinliği, tepeye kalan alan ve dönüş hacmi
        score = ((div["rsi_now"] - div["rsi_prev"]) / 2 + (cfg.rsi_div_oversold - div["rsi_prev"]) / 5
                 + min(room_pct, 5) * 2 + min(vol_ratio, 4) * 2 + (1 - retrace) * 3)
        metrics = {
            "kind": "rsi_divergence", "price_drop_pct": div["price_drop_pct"], "rsi_prev": div["rsi_prev"],
            "rsi_low": div["rsi_now"], "bars_apart": float(div["bars_apart"]),
            "bars_since_low": float(div["bars_since_low"]), "rsi": rsi_now, "vol_ratio": vol_ratio,
            "atr_pct": atr_pct, "retrace": retrace, "room_pct": room_pct, "trend_pct": trend_pct,
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
        """Sıralamaya girecek en hacimli ROTATION_UNIVERSE parite (stabil / fiat / kaldıraçlı / ince fiyat adımlı
        hariç). ROTATION_COINS verilmişse (ör. BTC,ETH) yalnızca o coinler sıralanır (büyük coin trendi)."""
        cfg = self.cfg
        coins = rotation_coin_list(cfg)
        rows: List[Tuple[float, str]] = []
        for t in tickers:
            symbol = t.get("symbol", "")
            r = rules.get(symbol)
            if r is None or symbol in excluded_symbols or r.base_asset in cfg.excluded_assets:
                continue
            if coins and r.base_asset not in coins:
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
        return [sym for _, sym in (rows if coins else rows[: cfg.rotation_universe])]

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
        plan, note = self.plan([], rules, excluded_symbols, rotation_hold_count(self.cfg))
        result.market_ok = plan.risk_on is not False
        result.market_note = note
        result.candidates = result.analyzed = len(plan.ranking)
        for symbol in plan.candidates[: plan.wanted]:
            mom = dict(plan.ranking)[symbol]
            result.signals.append(Signal(symbol, 0.0, mom, {"kind": "rotation", "momentum_pct": mom,
                                                            "rank": float(plan.rank_of(symbol) or 0),
                                                            "lookback_days": float(self.cfg.rotation_lookback_days)}))
        return result


# ===================================================================== Vadeli işlem scalping (STRATEGY=futures)
# Binance USDⓈ-M vadeli işlemler, 5 dk mumlar. İki giriş taktiği (FUTURES_TACTIC):
#
# crash (varsayılan) — çöküş dönüşü: son KAPANMIŞ 5 dk mumda fiyat son FUTURES_CRASH_HOURS (4) saatte en az
#   FUTURES_CRASH_DROP_PCT (%10) düşmüşse (likidasyon şelalesi) long. İsteğe bağlı (FUTURES_CRASH_LIQ) o mumda likidasyon
#   dalgası da şart. 2020-2026 araştırması: 5 dk - 1 saat ufkunda küçük likidasyon dalgalarında maliyeti aşan avantaj
#   yok; büyük çöküşlerden sonra ise fiyat ortalama toparlanıyor (her yıl, coinlerin çoğunda; seçim 2020-2023 ve
#   doğrulama 2023-2026 dönemlerinde). Short tarafında avantaj bulunamadı: crash yalnızca long açar.
#
# liq4 — ilk sürüm (7 yılın 7'sinde zarar etti, karşılaştırma için): dört şart AYNI ANDA:
#   1) Likidasyon dalgası: long için zorla SATIŞ (long'lar likide oluyor), short için zorla ALIŞ (short'lar likide).
#      Vekil ölçüt (canlı ve backtest aynı): mumun piyasa satış hacmi (hacim - taker buy) önceki 20 mumun ortalamasının
#      en az FUTURES_LIQ_RATIO katı, mum son 1 saatin dibini kırmış (stop / likidasyon kümesi süpürülmüş) ve açılıştan
#      en az 1 ATR aşağı itmiş. Short için tersi (taker buy hacmi, son 1 saatin zirvesi). Gerçek likidasyon kayıtlarıyla
#      (Binance COIN-M, Haz 2023 - Mar 2024) karşılaştırıldı: işaretlenen mumların BTC'de %21'i, ETH'de %26'sı gerçek
#      likidasyonların en yoğun %1'lik 5 dk dilimine denk geldi (rastgele seçimde %1). FUTURES_LIQ_SOURCE=feed / both
#      canlıda Binance'in anlık likidasyon akışını (forceOrder) kullanır; geçmiş verisi olmadığı için backtest edilemez.
#   2) Hacim onayı: mum hacmi önceki 20 mumun ortalamasının en az FUTURES_VOLUME_RATIO (2.5) katı.
#   3) RSI uyumsuzluğu: long için fiyat son 3 mumda yeni dip yaptı ama RSI(14) 5-30 mum önceki dipten (RSI ≤
#      FUTURES_DIV_OVERSOLD) en az FUTURES_DIV_RISE puan yüksek (pozitif uyumsuzluk); short için tersi (negatif).
#   4) BTC trendi: 1 saatlik BTC kapanışı EMA50'nin üstünde ve EMA20 > EMA50 ise yalnızca long, tersiyse yalnızca short;
#      arada (trend yok) işlem açılmaz.
# Canlı bot (futures_trader.FuturesBot) ve backtest (futures_backtest.py) aşağıdaki AYNI fonksiyonları kullanır.
try:
    import numpy as np
except ImportError:      # numpy yalnızca vadeli strateji için gerekir (pip install -r requirements.txt)
    np = None

FUT_INTERVAL = "5m"
FUT_BAR_MS = 300_000
FUT_HOUR_MS = 3_600_000
FUT_AVG_BARS = 20            # Hacim / taker hacmi ortalaması: önceki 20 mum (sinyal mumu hariç)
FUT_SWEEP_BARS = 12          # Likidasyon dalgası son 1 saatin (12 × 5 dk) dibini / zirvesini kırmalı
FUT_ATR_BARS = 14            # ...ve mum içinde en az 1 ATR itmiş olmalı (önceki 14 mumun ortalama gerçek aralığı)
FUT_DIV_MIN_GAP, FUT_DIV_MAX_GAP = 5, 30   # Uyumsuzluk: önceki dip / tepe 5-30 mum (25 dk - 2.5 saat) önce
FUT_DIV_RECENT = 3           # Yeni dip / tepe son 3 kapanmış mum içinde (sinyal mumu dahil)
FUT_PIVOT = 2                # Önceki dip: iki yanındaki 2'şer mumdan düşük (tepe: yüksek)
FUT_BTC_EMA_FAST, FUT_BTC_EMA_SLOW = 20, 50   # BTC 1 saatlik trend
FUT_KLINE_LIMIT = 300        # Canlı: coin başına istenen 5 dk mum (RSI ve ortalamalar için yeterli geçmiş)
FUT_BTC_1H_LIMIT = 300       # Canlı: BTC 1 saatlik mum (EMA50 için yeterli geçmiş)
FUT_MIN_BARS = 80            # Bundan az kapanmış mum varsa sinyal aranmaz
FUT_SIDES = ("both", "long", "short")
FUT_LIQ_SOURCES = ("proxy", "feed", "both")
FUT_Z_LOOKBACK_BARS = 288    # Oynaklık eşiği: pencereden önceki 24 saatin (288 × 5 dk) getirilerinin standart sapması
FUT_CONFIRM_AVG_BARS = FUT_AVG_BARS   # Hacim zirvesi teyidi: önceki 20 mumun ortalama hacmine göre
FUT_KLINE_MAX = 1500         # Binance tek istekte en fazla 1500 mum verir
FUT_BB_BARS = 20             # Bollinger modülü: 20 mumluk kapanış ortalaması
FUT_BB_RSI_PERIOD = 14       # Bollinger modülü: RSI(14)
# Aynı mumda birden çok sinyal: önce öncelik (çöküş > pompa > kırılım > Bollinger), sonra modülün kendi puanı
FUT_PRIORITY = {"crash": 0, "pump": -1, "breakout": -2, "bb": -3}
FUT_TACTIC_CODE = {"crash": 1.0, "pump": 2.0, "bb": 3.0, "breakout": 4.0}   # metrics["tactic"]
FUT_MODULE_NAMES = {"crash": "çöküş dönüşü", "pump": "pompa dönüşü", "bb": "Bollinger + RSI",
                    "breakout": "hacimli kırılım"}


def _need_numpy() -> None:
    if np is None:
        raise RuntimeError("Vadeli strateji numpy gerektirir: .venv/bin/pip install -r requirements.txt")


def np_prev_mean(x, window: int):
    """Her mum için ÖNCEKİ `window` mumun ortalaması (mumun kendisi hariç); ilk `window` değer NaN."""
    x = np.asarray(x, dtype=np.float64)
    out = np.full(len(x), np.nan)
    if len(x) > window:
        cs = np.concatenate(([0.0], np.cumsum(x)))
        out[window:] = (cs[window:-1] - cs[:-window - 1]) / window
    return out


def np_prev_extreme(x, window: int, fn):
    """Her mum için ÖNCEKİ `window` mumun en düşüğü (fn=np.min) / en yükseği (fn=np.max); ilk `window` değer NaN."""
    x = np.asarray(x, dtype=np.float64)
    out = np.full(len(x), np.nan)
    if len(x) > window:
        view = np.lib.stride_tricks.sliding_window_view(x, window)
        out[window:] = fn(view, axis=1)[:-1]
    return out


def np_rsi(close, period: int = 14):
    """Wilder RSI (indicators.rsi ile aynı hesap); ilk `period` değer NaN."""
    c = np.asarray(close, dtype=np.float64)
    n = len(c)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    d = np.diff(c)
    gain = np.where(d > 0, d, 0.0).tolist()
    loss = np.where(d < 0, -d, 0.0).tolist()
    ag = sum(gain[:period]) / period
    al = sum(loss[:period]) / period
    vals = [0.0] * n
    vals[period] = 100.0 - 100.0 / (1.0 + ag / al) if al > 0 else (100.0 if ag > 0 else 50.0)
    k = period - 1
    for i in range(period, n - 1):            # d[i] = c[i+1] - c[i]
        ag = (ag * k + gain[i]) / period
        al = (al * k + loss[i]) / period
        vals[i + 1] = 100.0 - 100.0 / (1.0 + ag / al) if al > 0 else (100.0 if ag > 0 else 50.0)
    out[period:] = vals[period:]
    return out


def np_ema(x, period: int):
    """indicators.ema ile aynı: ilk değer ilk `period` değerin basit ortalaması; öncesi NaN."""
    v = np.asarray(x, dtype=np.float64)
    n = len(v)
    out = np.full(n, np.nan)
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    prev = float(v[:period].mean())
    vals = v.tolist()
    res = [0.0] * n
    res[period - 1] = prev
    for i in range(period, n):
        prev = vals[i] * k + prev * (1 - k)
        res[i] = prev
    out[period - 1:] = res[period - 1:]
    return out


def futures_features(k: Dict[str, "np.ndarray"], cfg) -> Dict[str, "np.ndarray"]:
    """5 dk mum dizilerinden (futures_data.FIELDS: o, h, l, c, v, tb) sinyal ölçüleri. Tüm diziler aynı uzunlukta."""
    _need_numpy()
    o, h, l, c, v, tb = (np.asarray(k[x], dtype=np.float64) for x in ("o", "h", "l", "c", "v", "tb"))
    sell = np.maximum(v - tb, 0.0)                      # Piyasa emriyle SATAN tarafın hacmi
    prev_c = np.concatenate((c[:1], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = v / np_prev_mean(v, FUT_AVG_BARS)
        sell_ratio = sell / np_prev_mean(sell, FUT_AVG_BARS)
        buy_ratio = tb / np_prev_mean(tb, FUT_AVG_BARS)
    atr_prev = np_prev_mean(tr, FUT_ATR_BARS)
    low_prev = np_prev_extreme(l, FUT_SWEEP_BARS, np.min)
    high_prev = np_prev_extreme(h, FUT_SWEEP_BARS, np.max)
    piv_low = np.zeros(len(c), dtype=bool)
    piv_high = np.zeros(len(c), dtype=bool)
    if len(c) > 2 * FUT_PIVOT:
        m = l[2:-2]
        piv_low[2:-2] = (m <= l[1:-3]) & (m <= l[:-4]) & (m < l[3:-1]) & (m < l[4:])
        m = h[2:-2]
        piv_high[2:-2] = (m >= h[1:-3]) & (m >= h[:-4]) & (m > h[3:-1]) & (m > h[4:])
    bars = max(1, int(getattr(cfg, "futures_crash_hours", 4)) * 12)
    crash = np.full(len(c), np.nan)
    if len(c) > bars:
        crash[bars:] = (c[bars:] / c[:-bars] - 1) * 100
    return {"o": o, "h": h, "l": l, "c": c, "v": v, "crash": crash,
            "vol_ratio": vol_ratio, "sell_ratio": sell_ratio, "buy_ratio": buy_ratio,
            "atr": atr_prev, "low_prev": low_prev, "high_prev": high_prev, "rsi": np_rsi(c, cfg.rsi_period),
            "piv_low": piv_low, "piv_high": piv_high,
            "liq_long": (sell_ratio >= cfg.futures_liq_ratio) & (l < low_prev) & ((o - l) >= atr_prev),
            "liq_short": (buy_ratio >= cfg.futures_liq_ratio) & (h > high_prev) & ((h - o) >= atr_prev),
            "vol_ok": vol_ratio >= cfg.futures_volume_ratio}


# ------------------------------------------------------------------ dinamik çöküş (çok pencere, oynaklık, teyit)
def _memo(f: Dict, key, fn):
    """f sözlüğünde önbellek: aynı mum dizileri için pencere başına hesap bir kez yapılır (ızgara testi)."""
    out = f.get(key)
    if out is None:
        out = f[key] = fn()
    return out


def crash_drop_pct(f: Dict[str, "np.ndarray"], hours: int):
    """Son `hours` saatte kapanıştan kapanışa düşüş (%; pozitif = düşüş). İlk hours × 12 mum NaN."""
    def calc():
        c = f["c"]
        bars = max(1, int(hours) * 12)
        out = np.full(len(c), np.nan)
        if len(c) > bars:
            out[bars:] = (1 - c[bars:] / c[:-bars]) * 100
        return out
    return _memo(f, ("drop", int(hours)), calc)


def window_sigma(c, bars: int, lookback: int = FUT_Z_LOOKBACK_BARS):
    """`bars` mumluk pencerenin olağan hareketi (log, 1 standart sapma): pencere başlangıcından önceki `lookback`
    mumun 5 dk log getirilerinin standart sapması × √bars. Çöküşün kendisi ölçüye karışmaz."""
    c = np.asarray(c, dtype=np.float64)
    n = len(c)
    out = np.full(n, np.nan)
    if n <= bars + lookback:
        return out
    r = np.zeros(n)
    with np.errstate(divide="ignore", invalid="ignore"):
        r[1:] = np.diff(np.log(c))
    cs = np.concatenate(([0.0], np.cumsum(r)))
    cs2 = np.concatenate(([0.0], np.cumsum(r * r)))
    k = np.arange(lookback, n)                       # r[k - lookback + 1 .. k] (r[0] hariç)
    s1 = (cs[k + 1] - cs[k + 1 - lookback]) / lookback
    s2 = (cs2[k + 1] - cs2[k + 1 - lookback]) / lookback
    sd = np.full(n, np.nan)
    sd[lookback:] = np.sqrt(np.maximum(s2 - s1 * s1, 0.0))
    out[bars:] = sd[:-bars] * math.sqrt(bars)        # i. mumda biten pencere i - bars'ta başlar
    return out


def crash_sigma_pct(f: Dict[str, "np.ndarray"], hours: int, z: float):
    """Oynaklık eşiği (%): fiyatın `hours` saatte z standart sapma düşmesi = 1 - e^(-z·σ)."""
    sig = _memo(f, ("sigma", int(hours), FUT_Z_LOOKBACK_BARS),
                lambda: window_sigma(f["c"], max(1, int(hours) * 12), FUT_Z_LOOKBACK_BARS))
    return (1 - np.exp(-z * sig)) * 100


def crash_state(f: Dict[str, "np.ndarray"], cfg):
    """Çöküş kuralları (config.futures_crash_rules; FUTURES_CRASH_Z > 0 ise sabit yüzdeler yerine oynaklık eşiği)
    her mum için: (tetik, en güçlü kuralın şiddeti = düşüş / eşik, o kuralın düşüşü %, saati, eşiği %)."""
    rules = futures_crash_rules(cfg)
    z = float(getattr(cfg, "futures_crash_z", 0.0) or 0.0)
    z_min = float(getattr(cfg, "futures_crash_z_min_pct", 3.0))
    key = (rules, z, z_min if z > 0 else None, FUT_Z_LOOKBACK_BARS if z > 0 else None)
    hit = f.get("_crash_state")
    if hit is not None and hit[0] == key:
        return hit[1]
    n = len(f["c"])
    trig = np.zeros(n, dtype=bool)
    sev, drop_b, hours_b, thr_b = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n)
    for hours, pct in rules:
        drop = crash_drop_pct(f, hours)
        thr = np.maximum(crash_sigma_pct(f, hours, z), z_min) if z > 0 else np.full(n, float(pct))
        with np.errstate(invalid="ignore", divide="ignore"):
            ok = drop >= thr                                   # NaN (yetersiz geçmiş) → False
            s = np.where(np.isfinite(drop) & (thr > 0), drop / thr, 0.0)
        trig |= ok
        better = s > sev
        sev = np.where(better, s, sev)
        drop_b = np.where(better, drop, drop_b)
        hours_b = np.where(better, float(hours), hours_b)
        thr_b = np.where(better, thr, thr_b)
    out = (trig, sev, drop_b, hours_b, thr_b)
    f["_crash_state"] = (key, out)
    return out


def crash_peak(f: Dict[str, "np.ndarray"], cfg, idx):
    """Giriş mumları `idx` için izleme penceresindeki (son FUTURES_CONFIRM_BARS mum, giriş mumu dahil) en güçlü çöküş
    mumunun indeksi; close teyidinde giriş mumunun kendisi. Eşitlikte en eski mum."""
    idx = np.atleast_1d(np.asarray(idx, dtype=np.int64))
    if getattr(cfg, "futures_confirm", "close") == "close" or not len(idx):
        return idx
    trig, sev = crash_state(f, cfg)[:2]
    masked = np.where(trig, sev, -1.0)
    win = idx[:, None] + np.arange(-int(cfg.futures_confirm_bars), 1)[None, :]
    vals = np.where(win >= 0, masked[np.maximum(win, 0)], -2.0)
    return np.maximum(win, 0)[np.arange(len(idx)), np.argmax(vals, axis=1)]


def crash_scores(f: Dict[str, "np.ndarray"], cfg, idx):
    """Eş zamanlı sinyallerin sıralama puanı (giriş mumları `idx`): tek sabit kuralda düşüş (%); birden çok kural ya
    da oynaklık eşiğinde düşüş / eşik × ana kuralın yüzdesi (eşiğini en çok aşan önce)."""
    trig, sev, drop = crash_state(f, cfg)[:3]
    p = crash_peak(f, cfg, idx)
    if len(futures_crash_rules(cfg)) == 1 and not getattr(cfg, "futures_crash_z", 0.0):
        return drop[p]
    return sev[p] * float(cfg.futures_crash_drop_pct)


def confirm_volume_ratio(f: Dict[str, "np.ndarray"]):
    """Hacim zirvesi teyidi için mum hacmi / önceki FUT_CONFIRM_AVG_BARS mumun ortalaması."""
    if FUT_CONFIRM_AVG_BARS == FUT_AVG_BARS and "vol_ratio" in f:
        return f["vol_ratio"]

    def calc():
        with np.errstate(divide="ignore", invalid="ignore"):
            return f["v"] / np_prev_mean(f["v"], FUT_CONFIRM_AVG_BARS)
    return _memo(f, ("vr", FUT_CONFIRM_AVG_BARS), calc)


def crash_entries(f: Dict[str, "np.ndarray"], cfg, trig):
    """Teyit (izleme modu): close → tetik mumunun kendisi; volume / green / either → son FUTURES_CONFIRM_BARS mum
    içinde (bu mum dahil) tetik görülmüş ve bu mum teyit veriyor (hacim zirvesi / yeşil mum). Durumsuz: canlı bot
    yeniden başlasa da aynı mumda aynı karar verilir."""
    mode = getattr(cfg, "futures_confirm", "close")
    if mode == "close":
        return trig
    w = int(cfg.futures_confirm_bars)
    cs = np.cumsum(trig, dtype=np.int64)
    recent = cs.copy()
    if len(cs) > w + 1:
        recent[w + 1:] -= cs[:-(w + 1)]
    conf = np.zeros(len(trig), dtype=bool)
    if mode in ("volume", "either"):
        with np.errstate(invalid="ignore"):
            conf |= confirm_volume_ratio(f) >= cfg.futures_confirm_volume_ratio
    if mode in ("green", "either"):
        conf |= f["c"] > f["o"]
    return (recent > 0) & conf


# ------------------------------------------------------------------ ek modüller (pompa short, Bollinger + RSI, kırılım)
def _crash_entry_mask(f: Dict[str, "np.ndarray"], cfg):
    """crash_entries önbellekli (sinyal başına yeniden hesaplanmasın)."""
    key = (futures_crash_rules(cfg), float(getattr(cfg, "futures_crash_z", 0.0) or 0.0),
           float(getattr(cfg, "futures_crash_z_min_pct", 3.0)), getattr(cfg, "futures_confirm", "close"),
           int(getattr(cfg, "futures_confirm_bars", 12)), float(getattr(cfg, "futures_confirm_volume_ratio", 3.0)))
    hit = f.get("_crash_entry")
    if hit is not None and hit[0] == key:
        return hit[1]
    out = crash_entries(f, cfg, crash_state(f, cfg)[0])
    f["_crash_entry"] = (key, out)
    return out


def pump_state(f: Dict[str, "np.ndarray"], cfg):
    """Pompa kuralları (FUTURES_PUMP_RULES) her mum için: (tetik, şiddet = yükseliş / eşik, yükseliş %, saat, eşik %)."""
    rules = futures_pump_rules(cfg)
    hit = f.get("_pump_state")
    if hit is not None and hit[0] == rules:
        return hit[1]
    n = len(f["c"])
    trig = np.zeros(n, dtype=bool)
    sev, rise_b, hours_b, thr_b = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n)
    for hours, pct in rules:
        rise = -crash_drop_pct(f, hours)                       # (kapanış / H saat önceki kapanış - 1) × 100
        with np.errstate(invalid="ignore"):
            ok = rise >= pct
            sv = np.where(np.isfinite(rise), rise / pct, 0.0)
        trig |= ok
        better = sv > sev
        sev = np.where(better, sv, sev)
        rise_b = np.where(better, rise, rise_b)
        hours_b = np.where(better, float(hours), hours_b)
        thr_b = np.where(better, float(pct), thr_b)
    out = (trig, sev, rise_b, hours_b, thr_b)
    f["_pump_state"] = (rules, out)
    return out


def rsi_14(f: Dict[str, "np.ndarray"], cfg):
    """Bollinger modülünün RSI(14)'ü (RSI_PERIOD 14 ise futures_features'ınki)."""
    if int(getattr(cfg, "rsi_period", FUT_BB_RSI_PERIOD)) == FUT_BB_RSI_PERIOD and "rsi" in f:
        return f["rsi"]
    return _memo(f, ("rsi", FUT_BB_RSI_PERIOD), lambda: np_rsi(f["c"], FUT_BB_RSI_PERIOD))


def bollinger(f: Dict[str, "np.ndarray"], bars: int = FUT_BB_BARS):
    """Son `bars` kapanışın ortalaması ve standart sapması (mumun kendisi dahil; ilk bars - 1 mum NaN)."""
    def calc():
        c = f["c"]
        n = len(c)
        mid, sd = np.full(n, np.nan), np.full(n, np.nan)
        if n >= bars:
            view = np.lib.stride_tricks.sliding_window_view(c, bars)
            mid[bars - 1:] = view.mean(axis=1)
            sd[bars - 1:] = view.std(axis=1)
        return mid, sd
    return _memo(f, ("bb", bars), calc)


def bb_state(f: Dict[str, "np.ndarray"], cfg):
    """Bollinger + RSI: (long, short, alt bant, üst bant). Long: kapanış alt bandın altında ve RSI(14) <
    FUTURES_BB_RSI_LOW; short: kapanış üst bandın üstünde ve RSI(14) > FUTURES_BB_RSI_HIGH (FUTURES_BB yönleri)."""
    key = (cfg.futures_bb, float(cfg.futures_bb_std), float(cfg.futures_bb_rsi_low), float(cfg.futures_bb_rsi_high))
    hit = f.get("_bb_state")
    if hit is not None and hit[0] == key:
        return hit[1]
    mid, sd = bollinger(f)
    lower, upper = mid - cfg.futures_bb_std * sd, mid + cfg.futures_bb_std * sd
    r = rsi_14(f, cfg)
    c = f["c"]
    with np.errstate(invalid="ignore"):
        long_ok = (c < lower) & (r < cfg.futures_bb_rsi_low)
        short_ok = (c > upper) & (r > cfg.futures_bb_rsi_high)
    if cfg.futures_bb not in ("both", "long"):
        long_ok = np.zeros(len(c), dtype=bool)
    if cfg.futures_bb not in ("both", "short"):
        short_ok = np.zeros(len(c), dtype=bool)
    out = (long_ok, short_ok, lower, upper)
    f["_bb_state"] = (key, out)
    return out


def breakout_state(f: Dict[str, "np.ndarray"], cfg):
    """Hacimli kırılım: (long, önceki FUTURES_BREAKOUT_HOURS saatin en yükseği). Hacim önceki 20 mumun ortalamasının
    FUTURES_BREAKOUT_VOLUME_RATIO katından fazla ve kapanış o zirvenin üstünde."""
    bars = max(1, int(cfg.futures_breakout_hours) * 12)
    key = (bars, float(cfg.futures_breakout_volume_ratio))
    hit = f.get("_bo_state")
    if hit is not None and hit[0] == key:
        return hit[1]
    high_prev = _memo(f, ("hprev", bars), lambda: np_prev_extreme(f["h"], bars, np.max))
    with np.errstate(invalid="ignore"):
        ok = (f["vol_ratio"] > cfg.futures_breakout_volume_ratio) & (f["c"] > high_prev)
    out = (ok, high_prev)
    f["_bo_state"] = (key, out)
    return out


def _entry_key(cfg, liq_long) -> tuple:
    return (futures_crash_rules(cfg), float(getattr(cfg, "futures_crash_z", 0.0) or 0.0),
            float(getattr(cfg, "futures_crash_z_min_pct", 3.0)), getattr(cfg, "futures_confirm", "close"),
            int(getattr(cfg, "futures_confirm_bars", 12)), float(getattr(cfg, "futures_confirm_volume_ratio", 3.0)),
            bool(cfg.futures_crash_liq), float(cfg.futures_liq_ratio), futures_modules(cfg), futures_pump_rules(cfg),
            getattr(cfg, "futures_bb", "off"), float(getattr(cfg, "futures_bb_std", 2.0)),
            float(getattr(cfg, "futures_bb_rsi_low", 25.0)), float(getattr(cfg, "futures_bb_rsi_high", 75.0)),
            bool(getattr(cfg, "futures_breakout", False)), float(getattr(cfg, "futures_breakout_volume_ratio", 3.0)),
            int(getattr(cfg, "futures_breakout_hours", 4)), cfg.futures_sides, int(getattr(cfg, "rsi_period", 14)),
            None if liq_long is None else id(liq_long))


def entry_masks(f: Dict[str, "np.ndarray"], cfg, liq_long=None) -> Dict[str, list]:
    """Çöküş taktiğinin giriş modülleri yön başına öncelik sırasıyla: {"long": [(modül, mask)], "short": [...]}.
    Likidasyon şartı (FUTURES_CRASH_LIQ) yalnızca çöküş modülüne uygulanır; FUTURES_SIDES yönleri süzer."""
    key = _entry_key(cfg, liq_long)
    hit = f.get("_entry_masks")
    if hit is not None and hit[0] == key:
        return hit[1]
    crash = _crash_entry_mask(f, cfg)
    if cfg.futures_crash_liq:                                   # Likidasyon dalgası giriş mumunda
        crash = crash & (f["liq_long"] if liq_long is None else liq_long)
    longs, shorts = [("crash", crash)], []
    mods = futures_modules(cfg)
    if "pump" in mods:
        shorts.append(("pump", pump_state(f, cfg)[0]))
    if "breakout" in mods:
        longs.append(("breakout", breakout_state(f, cfg)[0]))
    if "bb" in mods:
        bl, bs = bb_state(f, cfg)[:2]
        longs.append(("bb", bl))
        shorts.append(("bb", bs))
    if cfg.futures_sides == "long":
        shorts = []
    elif cfg.futures_sides == "short":
        longs = []
    out = {"long": longs, "short": shorts}
    f["_entry_masks"] = (key, out)
    return out


def _any(masks, n: int):
    out = np.zeros(n, dtype=bool)
    for _name, m in masks:
        out = out | m
    return out


def entry_scores(f: Dict[str, "np.ndarray"], cfg, idx, side: str, liq_long=None):
    """Giriş mumları `idx` (bu yönde en az bir modül sinyal veriyor) için (modül adları, öncelik, puan): modül =
    öncelik sırasında ilk sinyal veren. Puan: çöküşte crash_scores, pompada şiddet × 10, kırılımda hacim katı,
    Bollinger'da RSI'ın eşiği aşma miktarı."""
    idx = np.atleast_1d(np.asarray(idx, dtype=np.int64))
    masks = entry_masks(f, cfg, liq_long)[side]
    names = np.full(len(idx), "", dtype=object)
    prio = np.full(len(idx), -99, dtype=np.int64)
    score = np.zeros(len(idx))
    left = np.ones(len(idx), dtype=bool)
    for name, m in masks:
        hit = left & m[idx]
        if not hit.any():
            continue
        sub = idx[hit]
        if name == "crash":
            sc = crash_scores(f, cfg, sub)
        elif name == "pump":
            sc = pump_state(f, cfg)[1][sub] * 10.0
        elif name == "breakout":
            sc = f["vol_ratio"][sub]
        else:
            r = rsi_14(f, cfg)[sub]
            sc = cfg.futures_bb_rsi_low - r if side == "long" else r - cfg.futures_bb_rsi_high
        names[hit], prio[hit], score[hit] = name, FUT_PRIORITY[name], sc
        left &= ~hit
    return names, prio, score


def futures_entries(f: Dict[str, "np.ndarray"], cfg, active=None):
    """Tüm mumlar için çöküş taktiğinin girişleri (ızgara testi): (mum indeksleri, yön +1 / -1, öncelik, puan).
    Aynı mumda iki yön de sinyal verirse önceliği / puanı yüksek olan (eşitlikte long): canlıdaki seçimle aynı."""
    long_ok, short_ok = futures_candidates(f, cfg)
    if active is not None:
        long_ok, short_ok = long_ok & active, short_ok & active
    li, si = np.flatnonzero(long_ok), np.flatnonzero(short_ok)
    _ln, lp, ls = entry_scores(f, cfg, li, "long")
    _sn, sp, ss = entry_scores(f, cfg, si, "short")
    if len(li) and len(si):
        both, a, b = np.intersect1d(li, si, return_indices=True)
        if len(both):
            short_wins = (sp[b] > lp[a]) | ((sp[b] == lp[a]) & (ss[b] > ls[a]))
            keep_l = np.ones(len(li), dtype=bool)
            keep_l[a[short_wins]] = False
            keep_s = np.ones(len(si), dtype=bool)
            keep_s[b[~short_wins]] = False
            li, lp, ls = li[keep_l], lp[keep_l], ls[keep_l]
            si, sp, ss = si[keep_s], sp[keep_s], ss[keep_s]
    idx = np.concatenate((li, si))
    side = np.concatenate((np.ones(len(li), dtype=np.int64), -np.ones(len(si), dtype=np.int64)))
    prio = np.concatenate((lp, sp))
    score = np.concatenate((ls, ss))
    order = np.argsort(idx, kind="stable")
    return idx[order], side[order], prio[order], score[order]


def futures_kline_limit(cfg) -> int:
    """Canlı: coin başına istenecek 5 dk mum sayısı (en uzun pencere + izleme süresi + oynaklık geçmişi)."""
    hours = [h for h, _p in futures_crash_rules(cfg)] + [h for h, _p in futures_pump_rules(cfg)]
    if getattr(cfg, "futures_breakout", False):
        hours.append(int(cfg.futures_breakout_hours))
    need = max(hours) * 12 + FUT_AVG_BARS + 20
    if getattr(cfg, "futures_confirm", "close") != "close":
        need += int(cfg.futures_confirm_bars)
    if float(getattr(cfg, "futures_crash_z", 0.0) or 0.0) > 0:
        need += FUT_Z_LOOKBACK_BARS
    return int(min(FUT_KLINE_MAX, max(FUT_KLINE_LIMIT, need)))


def btc_trend_1h(btc_1h: Dict[str, "np.ndarray"]):
    """BTC 1 saatlik KAPANMIŞ mumlarından (kapanış zamanı ms, yükseliş, düşüş) dizileri."""
    _need_numpy()
    c = np.asarray(btc_1h["c"], dtype=np.float64)
    fast, slow = np_ema(c, FUT_BTC_EMA_FAST), np_ema(c, FUT_BTC_EMA_SLOW)
    up = (c > slow) & (fast > slow)
    down = (c < slow) & (fast < slow)
    return np.asarray(btc_1h["t"], dtype=np.int64) + FUT_HOUR_MS, up, down


def trend_at(close_ms, trend) -> Tuple["np.ndarray", "np.ndarray"]:
    """Her zaman için (ms) o anda kapanmış son saatlik mumun BTC trendi: (yükseliş, düşüş) maskeleri."""
    t_end, up, down = trend
    close_ms = np.atleast_1d(np.asarray(close_ms, dtype=np.int64))
    j = np.searchsorted(t_end, close_ms, "right") - 1
    ok = j >= 0
    u = np.zeros(len(close_ms), dtype=bool)
    d = np.zeros(len(close_ms), dtype=bool)
    u[ok], d[ok] = up[j[ok]], down[j[ok]]
    return u, d


def _divergence(f: Dict[str, "np.ndarray"], i: int, side: str, oversold: float, rise: float) -> Optional[dict]:
    """i. mumda pozitif (long) / negatif (short) RSI uyumsuzluğu: (önceki dip, yeni dip, RSI'lar) ya da None."""
    r = f["rsi"]
    lo = max(0, i - FUT_DIV_RECENT + 1)
    if side == "long":
        series, pivots = f["l"], f["piv_low"]
        p2 = lo + int(np.argmin(series[lo:i + 1]))
    else:
        series, pivots = f["h"], f["piv_high"]
        p2 = lo + int(np.argmax(series[lo:i + 1]))
    r2 = r[p2]
    if not np.isfinite(r2):
        return None
    stop = max(p2 - FUT_DIV_MAX_GAP - 1, FUT_PIVOT - 1)
    for p1 in range(p2 - FUT_DIV_MIN_GAP, stop, -1):   # Önceki dip / tepe en yakından uzağa (p1 + 2 ≤ i: teyitli)
        if not pivots[p1] or not np.isfinite(r[p1]):
            continue
        if side == "long" and series[p1] > series[p2] and r[p1] <= oversold and r2 >= r[p1] + rise:
            return {"p1": p1, "p2": p2, "rsi_prev": float(r[p1]), "rsi_now": float(r2)}
        if (side == "short" and series[p1] < series[p2] and r[p1] >= 100.0 - oversold
                and r2 <= r[p1] - rise):
            return {"p1": p1, "p2": p2, "rsi_prev": float(r[p1]), "rsi_now": float(r2)}
    return None


def futures_candidates(f: Dict[str, "np.ndarray"], cfg, up=None, down=None,
                       liq_long=None, liq_short=None) -> Tuple["np.ndarray", "np.ndarray"]:
    """Uyumsuzluk dışındaki şartları (likidasyon, hacim, BTC trendi, yön ayarı) sağlayan mumlar: (long, short).
    `up` / `down` None ise BTC trendi aranmaz (FUTURES_BTC_TREND=false); `liq_*` verilirse vekil ölçütün yerine
    geçer (canlı likidasyon akışı)."""
    if getattr(cfg, "futures_tactic", "crash") == "crash":
        masks = entry_masks(f, cfg, liq_long)            # Çöküş (+ açıksa pompa / kırılım / Bollinger), VEYA
        n = len(f["c"])
        return _any(masks["long"], n), _any(masks["short"], n)
    long_ok = (f["liq_long"] if liq_long is None else liq_long) & f["vol_ok"]
    short_ok = (f["liq_short"] if liq_short is None else liq_short) & f["vol_ok"]
    if cfg.futures_btc_trend:
        long_ok = long_ok & up
        short_ok = short_ok & down
    if cfg.futures_sides == "long":
        short_ok = np.zeros_like(short_ok)
    elif cfg.futures_sides == "short":
        long_ok = np.zeros_like(long_ok)
    return long_ok, short_ok


def _module_signal(f, i: int, side: str, cfg, module: str, priority: int, score: float) -> dict:
    """Ek modül sinyalinin ölçüleri (tactic: 2 pompa, 3 Bollinger, 4 kırılım)."""
    c = float(f["c"][i])
    out = {"side": side, "price": c, "score": score, "priority": priority, "tactic": FUT_TACTIC_CODE[module],
           "vol_ratio": float(f["vol_ratio"][i])}
    if module == "pump":
        _trig, _sev, rise, hours, thr = pump_state(f, cfg)
        out.update(rise_pct=float(rise[i]), hours=float(hours[i]), thr_pct=float(thr[i]),
                   liq_ratio=float(f["buy_ratio"][i]))
    elif module == "bb":
        _l, _s, lower, upper = bb_state(f, cfg)
        band = float(lower[i] if side == "long" else upper[i])
        out.update(rsi=float(rsi_14(f, cfg)[i]), band_pct=(c / band - 1) * 100 if band > 0 else 0.0)
    else:
        high_prev = float(breakout_state(f, cfg)[1][i])
        out.update(break_pct=(c / high_prev - 1) * 100 if high_prev > 0 else 0.0,
                   hours=float(cfg.futures_breakout_hours))
    return out


def futures_signal_at(f: Dict[str, "np.ndarray"], i: int, side: str, cfg, liq_long=None) -> Optional[dict]:
    """i. mumda (aday olduğu bilinen) sinyalin ölçüleri; liq4'te uyumsuzluk yoksa None. Puan: crash'te düşüşün derinliği,
    liq4'te likidasyon gücü (eş zamanlı sinyallerden en güçlüsü alınır). Ek modüllerde "priority" de döner."""
    if getattr(cfg, "futures_tactic", "crash") == "crash":
        names, prio, score = entry_scores(f, cfg, [i], side, liq_long)
        module = names[0] or "crash"
        if module != "crash":
            return _module_signal(f, i, side, cfg, module, int(prio[0]), float(score[0]))
        _trig, _sev, drop, hours, thr = crash_state(f, cfg)
        mode = getattr(cfg, "futures_confirm", "close")
        p = int(crash_peak(f, cfg, [i])[0])                    # İzlemedeki en güçlü çöküş mumu (close: bu mum)
        out = {"side": side, "price": float(f["c"][i]), "drop_pct": float(drop[p]),
               "liq_ratio": float(f["sell_ratio"][i]), "vol_ratio": float(f["vol_ratio"][i]), "hours": float(hours[p]),
               "score": float(crash_scores(f, cfg, [i])[0]), "tactic": 1.0, "thr_pct": float(thr[p])}
        if getattr(cfg, "futures_crash_z", 0.0):
            out["z"] = float(cfg.futures_crash_z)
        if mode != "close":
            vr = float(confirm_volume_ratio(f)[i])
            by_volume = np.isfinite(vr) and vr >= cfg.futures_confirm_volume_ratio and mode in ("volume", "either")
            out["confirm"] = 1.0 if by_volume else 2.0               # 1 = hacim zirvesi, 2 = yeşil mum
            out["confirm_vol"] = vr
            out["confirm_bars"] = float(i - p)
        return out
    div = _divergence(f, i, side, cfg.futures_div_oversold, cfg.futures_div_rise)
    if div is None:
        return None
    o, h, l, c, atr_v = f["o"][i], f["h"][i], f["l"][i], f["c"][i], f["atr"][i]
    if side == "long":
        liq, push = f["sell_ratio"][i], (o - l) / atr_v
        sweep = (f["low_prev"][i] / l - 1) * 100
    else:
        liq, push = f["buy_ratio"][i], (h - o) / atr_v
        sweep = (h / f["high_prev"][i] - 1) * 100
    return {"side": side, "price": float(c), "liq_ratio": float(liq), "vol_ratio": float(f["vol_ratio"][i]),
            "push_atr": float(push), "sweep_pct": float(sweep), "rsi_prev": div["rsi_prev"], "rsi_now": div["rsi_now"],
            "div_bars": float(div["p2"] - div["p1"]), "score": float(liq)}


def pick_signal(f: Dict[str, "np.ndarray"], i: int, cfg, long_ok, short_ok, liq_long=None) -> Optional[dict]:
    """i. mumun sinyali: iki yön de adaysa önceliği, eşitse puanı yüksek olan (eşitlikte long). Canlı bot ve backtest
    aynı seçimi yapar (futures_entries'in vektörel karşılığı)."""
    found = []
    for side, ok in (("long", long_ok), ("short", short_ok)):
        if ok[i]:
            sig = futures_signal_at(f, i, side, cfg, liq_long)
            if sig is not None:
                found.append(sig)
    return max(found, key=lambda x: (x.get("priority", 0), x["score"])) if found else None


@dataclass
class FuturesSignal:
    symbol: str
    side: str            # "long" | "short"
    price: float         # Sinyal mumunun kapanışı
    candle_ms: int       # Sinyal mumunun açılış zamanı
    score: float
    metrics: Dict[str, float] = field(default_factory=dict)
    priority: int = 0    # Aynı anda sinyallerde önce bu (FUT_PRIORITY: çöküş 0 > pompa > kırılım > Bollinger)

    def note(self) -> str:
        m = self.metrics
        if m.get("tactic") == 2.0:
            return (f"Pompa dönüşü (short): son {m.get('hours', 4):g} saatte +%{m.get('rise_pct', 0):.1f} | "
                    f"Alış hacmi x{m.get('liq_ratio', 0):.1f} | Hacim x{m.get('vol_ratio', 0):.1f}")
        if m.get("tactic") == 3.0:
            band = "alt" if self.side == "long" else "üst"
            return (f"Bollinger + RSI ({self.side}): kapanış {band} bandın %{abs(m.get('band_pct', 0)):.2f} "
                    f"{'altında' if self.side == 'long' else 'üstünde'}, RSI {m.get('rsi', 0):.0f} | "
                    f"Hacim x{m.get('vol_ratio', 0):.1f}")
        if m.get("tactic") == 4.0:
            return (f"Hacimli kırılım (long): son {m.get('hours', 4):g} saatin zirvesi %{m.get('break_pct', 0):.2f} "
                    f"aşıldı | Hacim x{m.get('vol_ratio', 0):.1f}")
        if m.get("tactic") == 1.0:
            text = f"Çöküş dönüşü: son {m.get('hours', 4):g} saatte -%{m.get('drop_pct', 0):.1f}"
            if m.get("z"):
                text += f" (eşik %{m.get('thr_pct', 0):.1f} = {m['z']:g} standart sapma)"
            if m.get("confirm") == 1.0:
                text += f" | Teyit: hacim zirvesi x{m.get('confirm_vol', 0):.1f}"
            elif m.get("confirm") == 2.0:
                text += " | Teyit: ilk yeşil mum"
            if m.get("confirm"):
                bars = int(m.get("confirm_bars", 0))
                text += f" (çöküşten {bars * 5} dk sonra)" if bars else " (çöküş mumunda)"
            text += f" | Satış hacmi x{m.get('liq_ratio', 0):.1f} | Hacim x{m.get('vol_ratio', 0):.1f}"
            if m.get("feed_usdt"):
                text += f" | Likidasyon akışı {m['feed_usdt']:,.0f} USDT".replace(",", ".")
            return text
        liq = "zorla satış" if self.side == "long" else "zorla alış"
        div = "pozitif" if self.side == "long" else "negatif"
        trend = {1.0: "yükseliş", -1.0: "düşüş"}.get(m.get("btc_trend", 0.0), "kapalı")
        text = (f"Likidasyon ({liq}) x{m.get('liq_ratio', 0):.1f} | Hacim x{m.get('vol_ratio', 0):.1f} | "
                f"RSI {div} uyumsuzluk {m.get('rsi_prev', 0):.0f}→{m.get('rsi_now', 0):.0f} | BTC 1s {trend}")
        if m.get("feed_usdt"):
            text += f" | Akış {m['feed_usdt']:,.0f} USDT".replace(",", ".")
        return text


class FuturesScalpStrategy(ReboundStrategy):
    """Vadeli scalping (STRATEGY=futures): FUTURES_COINS'teki coinlerde son kapanan 5 dk mumda 4'lü onay
    (likidasyon + hacim + RSI uyumsuzluğu + BTC 1 saatlik trend). Veriyi FuturesBot getirir; bu sınıf yalnızca
    hesaplar (`evaluate`). Spot tarama altyapısı (`scan`) bu strateji için çalışmaz."""
    name = "futures"
    interval = FUT_INTERVAL
    uses_room = False
    uses_24h_range = False
    is_futures = True
    analysis_fields = ("rsi_period", "futures_volume_ratio", "futures_liq_ratio", "futures_div_oversold",
                       "futures_div_rise", "futures_btc_trend", "futures_sides", "futures_tactic",
                       "futures_crash_hours", "futures_crash_drop_pct", "futures_crash_liq", "futures_crash_extra",
                       "futures_crash_z", "futures_crash_z_min_pct", "futures_confirm", "futures_confirm_bars",
                       "futures_confirm_volume_ratio")

    @property
    def needs_btc_trend(self) -> bool:
        """BTC 1 saatlik trendi yalnızca liq4 taktiğinde (FUTURES_BTC_TREND=true) kullanılır."""
        return self.cfg.futures_tactic == "liq4" and self.cfg.futures_btc_trend

    def scan(self, rules: Dict[str, SymbolRules], excluded_symbols: Set[str], invest_hint: float) -> ScanResult:
        result = ScanResult()
        result.market_ok = False
        result.market_note = "Vadeli strateji spot piyasayı taramaz (FuturesBot kendi taramasını yapar)."
        return result

    def evaluate(self, klines: Dict[str, Dict[str, "np.ndarray"]], btc_1h: Optional[Dict[str, "np.ndarray"]],
                 now_ms: int, feed=None) -> Tuple[List[FuturesSignal], Counter]:
        """Her coinin son KAPANMIŞ 5 dk mumunu değerlendirir. `klines`: sembol → kapanmış mum dizileri (son satır
        en yeni kapanmış mum). `feed`: liquidations.LiquidationFeed (FUTURES_LIQ_SOURCE=feed/both). (sinyaller
        en güçlüden zayıfa, elenme nedenleri)."""
        _need_numpy()
        cfg = self.cfg
        rejects: Counter = Counter()
        trend = btc_trend_1h(btc_1h) if self.needs_btc_trend and btc_1h is not None and len(btc_1h["t"]) else None
        signals: List[FuturesSignal] = []
        for symbol, k in klines.items():
            n = len(k["t"])
            if n < FUT_MIN_BARS:
                rejects["veri az"] += 1
                continue
            i = n - 1
            close_ms = int(k["t"][i]) + FUT_BAR_MS
            if now_ms - close_ms > 2 * FUT_BAR_MS:
                rejects["mum eski"] += 1
                continue
            f = futures_features(k, cfg)
            up = down = None
            if self.needs_btc_trend:
                if trend is None:
                    rejects["BTC trendi okunamadı"] += 1
                    continue
                up, down = trend_at([close_ms], trend)
                up = np.concatenate((np.zeros(n - 1, dtype=bool), up))
                down = np.concatenate((np.zeros(n - 1, dtype=bool), down))
            liq_long = liq_short = None
            feed_long = feed_short = 0.0
            uses_liq = cfg.futures_tactic == "liq4" or cfg.futures_crash_liq
            if uses_liq and cfg.futures_liq_source != "proxy":
                if feed is None:
                    rejects["likidasyon akışı yok"] += 1
                    continue
                feed_long, feed_short = feed.recent(symbol, FUT_BAR_MS / 1000)
                fl = np.zeros(n, dtype=bool)
                fs = np.zeros(n, dtype=bool)
                fl[i] = feed_long >= cfg.futures_liq_min_usdt
                fs[i] = feed_short >= cfg.futures_liq_min_usdt
                if cfg.futures_liq_source == "both":
                    fl &= f["liq_long"]
                    fs &= f["liq_short"]
                liq_long, liq_short = fl, fs
            long_ok, short_ok = futures_candidates(f, cfg, up, down, liq_long, liq_short)
            found = pick_signal(f, i, cfg, long_ok, short_ok, liq_long)
            if found is None:
                rejects[self._reject_reason(f, i, up, down, liq_long, liq_short)] += 1
                continue
            found["btc_trend"] = 1.0 if up is not None and up[i] else -1.0 if down is not None and down[i] else 0.0
            found["feed_usdt"] = feed_long if found["side"] == "long" else feed_short
            signals.append(FuturesSignal(symbol, found["side"], found["price"], int(k["t"][i]), found["score"],
                                         {k2: v for k2, v in found.items() if k2 not in ("side", "price", "priority")},
                                         int(found.get("priority", 0))))
        signals.sort(key=lambda s: (s.priority, s.score), reverse=True)
        return signals, rejects

    def _reject_reason(self, f, i, up, down, liq_long, liq_short) -> str:
        if self.cfg.futures_tactic == "crash":
            cfg = self.cfg
            trig = crash_state(f, cfg)[0]
            mode = getattr(cfg, "futures_confirm", "close")
            w = 0 if mode == "close" else int(cfg.futures_confirm_bars)
            if not trig[max(0, i - w):i + 1].any():
                return "sinyal yok (çöküş / pompa / bant / kırılım)" if futures_modules(cfg) else "çöküş yok"
            if cfg.futures_sides == "short":
                return "yön kapalı"
            if not crash_entries(f, cfg, trig)[i]:
                return {"volume": "teyit bekleniyor (hacim zirvesi)", "green": "teyit bekleniyor (yeşil mum)"}.get(
                    mode, "teyit bekleniyor (hacim zirvesi / yeşil mum)")
            return "likidasyon dalgası yok"
        ll = bool((f["liq_long"] if liq_long is None else liq_long)[i])
        ls = bool((f["liq_short"] if liq_short is None else liq_short)[i])
        if not (ll or ls):
            return "likidasyon dalgası yok"
        if not f["vol_ok"][i]:
            return "hacim düşük"
        if self.cfg.futures_btc_trend and not ((ll and up[i]) or (ls and down[i])):
            return "BTC trendi ters"
        if self.cfg.futures_sides != "both" and not ((ll and self.cfg.futures_sides == "long")
                                                     or (ls and self.cfg.futures_sides == "short")):
            return "yön kapalı"
        return "RSI uyumsuzluğu yok"


STRATEGY_CLASSES = {"rebound": ReboundStrategy, "trend": TrendPullbackStrategy, "breakout": BreakoutStrategy,
                    "intraday": IntradayStrategy, "swing": SwingBreakoutStrategy, "swing_pullback": SwingPullbackStrategy,
                    "mean_revert": MeanRevertStrategy, "rsi_divergence": RsiDivergenceStrategy,
                    "rotation": RotationStrategy, "futures": FuturesScalpStrategy}
assert tuple(STRATEGY_CLASSES) == STRATEGIES


def strategy_class(name: str):
    """Strateji sınıfı (interval, uses_room gibi sınıf bilgilerine nesne oluşturmadan erişmek için)."""
    return STRATEGY_CLASSES[name]


def make_strategy(client, cfg, coin_filter=None, regime=None) -> ReboundStrategy:
    """STRATEGY ayarına göre strateji nesnesi (canlı bot, self-test ve backtest aynı fabrikayı kullanır)."""
    return strategy_class(getattr(cfg, "strategy", "rebound"))(client, cfg, coin_filter=coin_filter, regime=regime)
