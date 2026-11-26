"""
Vadeli scalping backtesti (STRATEGY=futures): Binance USDⓈ-M geçmiş verisiyle 4'lü onay stratejisi.

    python main.py --backtest --strateji futures --gun 365          # Son 1 yıl, FUTURES_COINS
    python main.py --backtest --strateji futures --tum-donem        # Ocak 2020'den bugüne, dönem başı ilk 10 coin

Sinyaller canlı botla AYNI fonksiyonlarla bulunur (strategy.futures_features / futures_candidates /
futures_signal_at). Simülasyon:
  - Giriş: sinyal mumunun kapanışında (bir sonraki mumun açılışı) piyasa emri + kayma (FUTURES_SLIPPAGE_PCT).
  - Pozisyon: serbest bakiyenin FUTURES_MARGIN_USAGE_PCT'si teminat × kaldıraç (bileşik); aynı anda tek pozisyon
    (tüm bakiye kullanıldığı için). Notional Binance'in minimum emir tutarının altındaysa sinyal atlanır.
  - Çıkış (fiyat yüzdesi, girişten): Kâr Al (limit → maker komisyon), Zarar Kes (stop-market + kayma; mum stopun
    ötesinde açılırsa açılıştan), başabaş (fiyat +BREAKEVEN_TRIGGER_PCT'ye gelince stop girişin
    +BREAKEVEN_FLOOR_PCT üstüne), zaman stopu (MAX_HOLD_MINUTES sonra piyasa emriyle). Çıkışlar 1 dk mumlarla
    izlenir (yoksa 5 dk); mum içindeki sıra için standart varsayım: yükselen mumda önce dip sonra tepe, düşen
    mumda önce tepe sonra dip.
  - Komisyon (BNB_FEES=true → %10 indirim), gerçek fonlama oranları (pozisyon 00/08/16 UTC'yi geçerse),
    yalıtılmış (isolated) teminatta likidasyon: mum likidasyon fiyatının ötesinde açılırsa teminat kaybedilir.
Rapor: yıllara göre kazanma oranı, getiri, maks. düşüş, en uzun kayıp serisi; likidasyon / marjin riski; kaldıraç ve
TP / SL duyarlılığı. Grafik: sermaye eğrisi, düşüş, yıllık getiri ve kazanma oranı.
"""
from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
import heapq
import json
import math
import re
import zlib
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

import numpy as np

from config import (DATA_DIR, DAY_BRAKE_HOURS, cushion_factor, dd_scale_factor, format_crash_rules, futures_coin_list,
                    futures_crash_rules, futures_funding_rules, futures_liq_distance_pct, futures_long_windows,
                    futures_modules, futures_scan_shifts, futures_snapshot_on, futures_wave_cfg, futures_wave_rules,
                    parse_crash_rules, scan_log_path, tr_from, tr_from_num, window_peak)
from futures_data import FuturesArchive, concat, resample, slice_range
from futures_depth import DEPTH_CHECK_MIN_NOTIONAL, DepthBook, daily_volume_fn
from strategy import (BEAR_SMA_CHOICES, BIG_EXTRA_SLOTS, FUT_BAR_MS, FUT_HOUR_MS, QUALITY_CUTS, basis_pct,
                      basis_size_factor, bear_size_factor, bounce_boost_factor, support_size_factor,
                      SUPPORT_DAY_CHOICES,
                      big_extra_slots, big_funding_skip, big_rules_on, breadth_flags, btc_trend_1h, crash_state,
                      funding_per_bar, futures_candidates, futures_entries, futures_features, isolated_factor,
                      pick_signal, quality_roundup_ok, quality_score, quality_size_factor, recent_size_factor,
                      spot_pairs, trend_at)

DAY_MS = 86_400_000
MIN_MS = 60_000
WARMUP_MS = 4 * DAY_MS                  # 5 dk göstergeler + BTC 1 saatlik EMA50 (≥ 50 saat) için ısınma
# Binance USDⓈ-M minimum emir tutarı (USDT, 2024): BTC 100, ETH 20, diğerleri 5
MIN_NOTIONAL = {"BTC": 100.0, "ETH": 20.0}
DEFAULT_MIN_NOTIONAL = 5.0
MAX_NOTIONAL = 1_000_000.0              # Bileşik büyüme sınırı (kaldıraç kademeleri / likidite); üstü gerçekçi değil
FUNDING_DEFAULT = 0.0001                # Fonlama verisi yoksa 8 saatte %0.01 (pozitif: long öder)
FUNDING_HOURS = (0, 8, 16)
BNB_FUTURES_DISCOUNT = 0.10             # Vadeli işlemde BNB ile komisyon indirimi
LEVERAGES = (2, 3, 5, 8, 10)            # "Zarar kes olmasaydı" satırındaki likidasyon mesafeleri (+ şimdiki kaldıraç)
LEVERAGE_TABLE = tuple(range(2, 11))    # Kaldıraç duyarlılığı tablosu: 2x-10x (+ ayardaki kaldıraç; 10x üstü
                                        # 71 hesapta kill-switch'le bitiyordu, bkz. README "Kaldıraç yeniden")
LIVE_SL_MARGIN = 0.5                    # Canlı bot: zarar kes likidasyona en az bu kadar (puan) uzak olmalı
TP_GRID = (1.0, 1.2, 1.5)
SL_GRID = (0.8, 0.9, 1.0)
MAX_WINDOW_MIN = 240                    # Çıkış penceresi (zaman stopu duyarlılığı için en uzun süre)
LONG_WINDOW_MIN = 720                   # Bundan uzun tutma sürelerinde çıkışlar 5 dk mumlarla izlenir (bellek)
# Dönem başında piyasa değeri en büyük 10 coin (stabil coinler hariç; o tarihte USDⓈ-M vadeli işlemi olanlar).
# Dönem dönem değişen liste, bugünün büyük coinlerini geçmişe taşımanın yaratacağı "hayatta kalan" yanılgısını önler.
HISTORY_SEGMENTS = (
    ("2020-01-01", "BTC,ETH,XRP,BCH,LTC,EOS,BNB,XTZ,XLM,ADA"),
    ("2020-11-01", "BTC,ETH,XRP,LINK,BCH,LTC,DOT,BNB,ADA,XLM"),
    ("2023-11-01", "BTC,ETH,BNB,XRP,SOL,ADA,DOGE,TRX,LINK,DOT"),
)
REASON_TR = {"TP": "Kâr Al", "SL": "Zarar Kes", "BE": "Başabaş stopu", "TRAIL": "İzleyen stop", "TIME": "Zaman stopu",
             "LIQ": "LİKİDASYON",
             "END": "Veri bitti"}


def _ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _year(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def min_notional(symbol: str) -> float:
    return MIN_NOTIONAL.get(symbol[:-4] if symbol.endswith("USDT") else symbol, DEFAULT_MIN_NOTIONAL)


# ===================================================================== veri
@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    coins: Tuple[str, ...]


@dataclass
class FutData:
    start_ms: int
    end_ms: int
    segments: List[Segment]
    k5: Dict[str, Dict[str, np.ndarray]]
    btc_trend: tuple
    btc_close: Tuple[np.ndarray, np.ndarray]
    funding: Dict[str, Tuple[np.ndarray, np.ndarray]]
    archive: FuturesArchive
    exit_interval: str = "1m"
    notes: List[str] = field(default_factory=list)
    # En hacimli N köklü coin (FUTURES_UNIVERSE): 5 dk mumlar bellekte tutulmaz, coin coin diskten okunur
    loader: Optional[Callable[[str], Dict[str, np.ndarray]]] = None
    lean_symbols: List[str] = field(default_factory=list)
    universe_note: str = ""
    # Dalga modu genişliği: (ilk mum kapanışı ms, mum başına son 1 saatte %3+ düşen evren coini sayısı)
    breadth: Optional[Tuple[int, np.ndarray]] = None
    # Dakikalık tarama (FUTURES_MINUTE_SCAN): coinin 1 dk mumlarını veren yükleyici, ara dakikaların genişliği (mum içi
    # kayma ms → (ilk kapanış ms, sayılar)) ve çıkış pencerelerinin aylık 1 dk dosyalardan okunması
    minute_loader: Optional[Callable[[str], Dict[str, np.ndarray]]] = None
    breadth_shift: Dict[int, Tuple[int, np.ndarray]] = field(default_factory=dict)
    monthly_windows: bool = False
    # Emir defteri derinliği (futures_depth.DepthBook): derinliğe göre boy ve kayma; run_cli gerektiğinde kurar
    depth: Optional[DepthBook] = None


def history_segments(end_ms: int, start_ms: Optional[int] = None) -> List[Segment]:
    """HISTORY_SEGMENTS → [Segment] (start_ms'den önce başlayan dönemler kırpılır)."""
    out = []
    for i, (start, coins) in enumerate(HISTORY_SEGMENTS):
        a = _ms(start)
        b = _ms(HISTORY_SEGMENTS[i + 1][0]) if i + 1 < len(HISTORY_SEGMENTS) else end_ms
        a, b = max(a, start_ms or a), min(b, end_ms)
        if a < b:
            out.append(Segment(a, b, tuple(c.strip().upper() for c in coins.split(","))))
    return out


def universe_segments(cfg, start_ms: int, end_ms: int, archive: FuturesArchive,
                      progress: Optional[Callable[[str], None]] = None) -> List[Segment]:
    """FUTURES_UNIVERSE: her takvim ayı için o ayın "en hacimli N köklü coin" listesi (canlıyla aynı kural, geçmiş
    aylık hacimlerle; sonradan kaldırılan pariteler dahil). FUTURES_UNIVERSE_MODE=daily: her UTC günü için bir önceki
    günün hacmine göre ilk N."""
    from futures_universe import universe_days, universe_months
    build = universe_days if getattr(cfg, "futures_universe_mode", "avg") == "daily" else universe_months
    return [Segment(a, b, coins) for a, b, coins in build(cfg, start_ms, end_ms, archive, progress)]


def with_list(segments: List[Segment], lists: List[Segment]) -> List[Segment]:
    """Aylık evrene o tarihteki liste coinlerini ekler (tüm dönemde dönem başı listeleri, aksi hâlde FUTURES_COINS)."""
    out = []
    for seg in segments:
        extra = next((ls.coins for ls in lists if ls.start_ms <= seg.start_ms < ls.end_ms), ())
        out.append(Segment(seg.start_ms, seg.end_ms, tuple(dict.fromkeys(tuple(seg.coins) + tuple(extra)))))
    return out


def prepare_universe(cfg, start_ms: int, end_ms: int, segments: List[Segment], archive: FuturesArchive,
                     exit_interval: str = "1m", progress: Optional[Callable[[str], None]] = None) -> FutData:
    """Evren modu: her coinin yalnızca evrende olduğu aylar (+ bir önceki ay, göstergeler için) okunur ve sinyaller
    coin coin bulunur; tüm coinlerin mumları aynı anda belleğe alınmaz (80 coin × 7 yıl ≈ 5 GB olurdu)."""
    from futures_universe import month_add, month_ms, months_between
    say = progress or (lambda _m: None)
    months: Dict[str, set] = {}
    for seg in segments:
        for key in months_between(seg.start_ms, seg.end_ms):
            for coin in seg.coins:
                months.setdefault(coin + "USDT", set()).update({key, month_add(key, -1)})

    def loader(symbol: str) -> Dict[str, np.ndarray]:
        parts = [archive.klines(symbol, month_ms(k), month_ms(month_add(k, 1)), "5m")
                 for k in sorted(months.get(symbol, ())) if k >= "2020-01"]
        return concat(parts)

    def minute_loader(symbol: str) -> Dict[str, np.ndarray]:     # Dakikalık tarama: aynı ayların 1 dk mumları
        parts = [archive.klines(symbol, month_ms(k), min(month_ms(month_add(k, 1)), end_ms), "1m")
                 for k in sorted(months.get(symbol, ())) if k >= "2020-01" and month_ms(k) < end_ms]
        return concat(parts)

    say(f"📥 BTCUSDT: trend için 5 dk mumlar | evren: {len(months)} farklı coin")
    btc = archive.klines("BTCUSDT", start_ms - WARMUP_MS, end_ms, "5m")
    inside = slice_range(btc, start_ms, end_ms)
    age, avg = cfg.futures_universe_min_age_days, cfg.futures_universe_avg_days
    plus = " + liste" if getattr(cfg, "futures_universe_with_list", False) else ""
    if getattr(cfg, "futures_universe_mode", "avg") == "daily":
        note = (f"her gün bir önceki günün en hacimli {cfg.futures_universe} coini{plus} (en az {age} gün listelenmiş; "
                f"toplam {len(months)} farklı coin)")
    else:
        note = (f"her ay en hacimli {cfg.futures_universe} köklü coin{plus}"
                f" (en az {age} gün listelenmiş, son {avg} gün ortalama hacim; toplam {len(months)} farklı coin)")
    return FutData(start_ms, end_ms, segments, {}, btc_trend_1h(resample(btc, FUT_HOUR_MS)), (inside["t"], inside["c"]),
                   {}, archive, exit_interval, ["Evren modunda fonlama için ortalama oran (8 saatte %0.01) kullanıldı."],
                   loader=loader, lean_symbols=sorted(months), universe_note=note, minute_loader=minute_loader)


def prepare(cfg, start_ms: int, end_ms: int, segments: Optional[List[Segment]] = None, cache_dir=None,
            exit_interval: str = "1m", archive: Optional[FuturesArchive] = None,
            progress: Optional[Callable[[str], None]] = None) -> FutData:
    """Dönemin 5 dk mumlarını (ısınmalı), BTC 1 saatlik trendini ve fonlama oranlarını indirir / önbellekten okur."""
    archive = archive or FuturesArchive(cache_dir or DATA_DIR / "futures_cache", cfg.futures_data_url)
    if getattr(cfg, "futures_universe", 0) > 0:      # Aylık en hacimli N köklü coin (+ liste)
        lists = segments or [Segment(start_ms, end_ms, futures_coin_list(cfg))]
        segments = universe_segments(cfg, start_ms, end_ms, archive, progress)
        if cfg.futures_universe_with_list:
            segments = with_list(segments, lists)
        return prepare_universe(cfg, start_ms, end_ms, segments, archive, exit_interval, progress)
    segments = segments or [Segment(start_ms, end_ms, futures_coin_list(cfg))]
    coins = sorted({c for s in segments for c in s.coins})
    say = progress or (lambda _m: None)
    k5: Dict[str, Dict[str, np.ndarray]] = {}
    funding: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    notes: List[str] = []
    for coin in coins:
        symbol = coin + "USDT"
        active = [s for s in segments if coin in s.coins]
        a, b = min(s.start_ms for s in active) - WARMUP_MS, max(s.end_ms for s in active)
        say(f"📥 {symbol}: 5 dk mumlar {_date(a + WARMUP_MS)} → {_date(b)}")
        k = archive.klines(symbol, a, b, "5m")
        if len(k["t"]) < 500:
            notes.append(f"{symbol}: bu dönemde vadeli işlem verisi yok, atlandı")
            continue
        k5[symbol] = k
        funding[symbol] = archive.funding(symbol, a, b)
    btc = k5.get("BTCUSDT")
    if btc is None or btc["t"][0] > start_ms:
        say("📥 BTCUSDT: trend için 5 dk mumlar")
        btc = archive.klines("BTCUSDT", start_ms - WARMUP_MS, end_ms, "5m")
    btc_1h = resample(btc, FUT_HOUR_MS)
    inside = slice_range(btc, start_ms, end_ms)
    return FutData(start_ms, end_ms, segments, k5, btc_trend_1h(btc_1h), (inside["t"], inside["c"]), funding, archive,
                   exit_interval, notes,
                   minute_loader=lambda symbol: archive.klines(symbol, start_ms - WARMUP_MS, end_ms, "1m"))


# ===================================================================== sinyaller
@dataclass
class FutSignal:
    close_ms: int           # Sinyal mumunun kapanışı = giriş anı
    symbol: str
    side: str
    score: float
    entry_open: float       # Bir sonraki mumun açılışı (kaymasız giriş fiyatı)
    metrics: Dict[str, float] = field(default_factory=dict)
    priority: int = 0       # Aynı mumda önce bu (strategy.FUT_PRIORITY), sonra puan: canlıdaki sıralama
    breadth: int = 0        # Dalga modu: o mumda son 1 saatte %3+ düşen evren coini sayısı (strategy.market_breadth)


def signal_order(s: "FutSignal"):
    """Sinyallerin işlenme sırası: zaman, sonra öncelik ve puanı yüksek olan (canlıdaki seçimle aynı)."""
    return s.close_ms, -s.priority, -s.score


@dataclass
class Funnel:
    """4'lü onay hunisi: aktif dönemdeki 5 dk mumlardan kaçı hangi şartı geçti."""
    candles: int = 0
    liq: int = 0
    liq_vol: int = 0
    liq_vol_trend: int = 0
    signals: int = 0
    wave: int = 0           # Faz 6: dalga eşiğiyle gelen (genişlik şartını geçen) ek sinyal
    between: int = 0        # Dakikalık tarama: 5 dk sınırları dışındaki dakikalarda bulunan sinyaller (dalga eşiği dahil)


def _active_mask(t_close: np.ndarray, coin: str, segments: List[Segment]) -> np.ndarray:
    """Coinin evrende olduğu dönemlerdeki mumlar (start < kapanış ≤ end). t_close artan sırada."""
    mask = np.zeros(len(t_close), dtype=bool)
    for s in segments:
        if coin in s.coins:
            i, j = np.searchsorted(t_close, s.start_ms, "right"), np.searchsorted(t_close, s.end_ms, "right")
            mask[i:j] = True
    return mask


def load_funding(archive: FuturesArchive, symbol: str, t: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Mumların bulunduğu aylar (+ her birinin bir önceki ayı: ay başında bilinen son oran için) için fonlama oranları;
    evren modunda coin yalnızca bazı aylarda evrendeyse aradaki aylar indirilmez."""
    if not len(t):
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    months = np.unique(np.asarray(t, dtype=np.int64).astype("datetime64[ms]").astype("datetime64[M]"))
    keys = set()
    for key in months.astype(str):
        y, m = int(key[:4]), int(key[5:7])
        keys.add((y, m))
        keys.add((y - (m == 1), 12 if m == 1 else m - 1))
    parts_t, parts_r = [], []
    for y, m in sorted(keys):
        a = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
        b = int(datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        ft, fr = archive.funding(symbol, a, b)
        parts_t.append(ft)
        parts_r.append(fr)
    ft, fr = np.concatenate(parts_t), np.concatenate(parts_r)
    order = np.argsort(ft, kind="stable")
    ft, fr = ft[order], fr[order]
    keep = np.r_[True, ft[1:] != ft[:-1]] if len(ft) else np.zeros(0, dtype=bool)
    return ft[keep], fr[keep]


def with_extras(k: Dict[str, np.ndarray], symbol: str, data: FutData, cfgs) -> Dict[str, np.ndarray]:
    """Faz 4 modüllerinin ek verisi mum sözlüğüne eklenir (canlıda FuturesBot ekler): fonlama modülü açıksa k["fr"]
    (mumun kapanışında bilinen son fonlama, 8 saatlik %), düzeltme modülü açıksa k["h1"] (5 dk mumlardan 1 saatlik
    mumlar). İndirilen fonlama oranları data.funding'e de yazılır: işlemlerin fonlama maliyeti gerçek oranlarla."""
    mods = set()
    for c in cfgs:
        mods.update(futures_modules(c))
    if not ({"funding", "pullback"} & mods) or not len(k["t"]):
        return k
    k = dict(k)
    if "funding" in mods:
        if symbol not in data.funding:
            data.funding[symbol] = load_funding(data.archive, symbol, k["t"])
            note = "Fonlama modülü açık: fonlama maliyeti gerçek oranlarla hesaplandı."
            if note not in data.notes:
                data.notes[:] = [n for n in data.notes if "ortalama oran" not in n] + [note]
        ft, fr = data.funding[symbol]
        k["fr"] = funding_per_bar(k["t"] + FUT_BAR_MS, ft, fr)
    if "pullback" in mods:
        k["h1"] = resample(k, FUT_HOUR_MS)
    return k


def _series(data: FutData):
    """(parite, 5 dk mumlar): bellekteki veri ya da evren modunda coin coin diskten."""
    if data.loader is None:
        yield from data.k5.items()
        return
    for symbol in data.lean_symbols:
        k = data.loader(symbol)
        if len(k["t"]) >= 500:
            yield symbol, k


class BreadthCounter:
    """Dalga modu genişliği: sinyal taramasıyla aynı geçişte her coinin "son 1 saatte %3+ düştü" mumları sayılır (o tarihte
    evrende olan coinler; canlıda taranan coinler)."""

    def __init__(self, data: FutData, shift: int = 0):
        self.data, self.shift = data, int(shift)        # shift > 0: dakikalık taramanın ara dakikası (mum içi kayma ms)
        self.t0 = data.start_ms // FUT_BAR_MS * FUT_BAR_MS + self.shift
        self.need = (data.breadth if not self.shift else data.breadth_shift.get(self.shift)) is None
        self.counts = np.zeros((data.end_ms - self.t0) // FUT_BAR_MS + 2, dtype=np.int32) if self.need else None

    def add(self, f: Dict[str, np.ndarray], close: np.ndarray, coin: str) -> None:
        if not self.need:
            return
        idx = (close - self.t0) // FUT_BAR_MS
        ok = (idx >= 0) & (idx < len(self.counts)) & _active_mask(close, coin, self.data.segments) & breadth_flags(f)
        np.add.at(self.counts, idx[ok], 1)

    def finish(self, signals: Sequence[FutSignal]) -> None:
        if self.need and self.shift:
            self.data.breadth_shift[self.shift] = (self.t0, self.counts)
        elif self.need:
            self.data.breadth = (self.t0, self.counts)
        for s in signals:
            s.breadth = breadth_at(self.data, s.close_ms)


def breadth_at(data: FutData, close_ms: int) -> int:
    """O kapanıştaki genişlik; 5 dk sınırı dışındaki (dakikalık tarama) kapanışlar kendi kaydırılmış ızgarasından."""
    shift = int(close_ms) % FUT_BAR_MS
    store = data.breadth if not shift else data.breadth_shift.get(shift)
    if store is None:
        return 0
    t0, counts = store
    i = (int(close_ms) - t0) // FUT_BAR_MS
    return int(counts[i]) if 0 <= i < len(counts) else 0


def _coin_signals(cfg, wave_cfg, data: FutData, symbol: str, k: Dict[str, np.ndarray], breadth: "BreadthCounter",
                  funnel: Optional[Funnel], signals: List[FutSignal], wave_sigs: List[FutSignal]) -> None:
    """Bir coinin 5 dk mumlarında (dakikalık taramada: kaydırılmış 5 dk mumlarında) canlı botun her mum kapanışındaki
    değerlendirmesi; sinyaller listelere eklenir. `funnel` verilirse huni sayıları güncellenir."""
    coin = symbol[:-4]
    k = with_extras(k, symbol, data, [cfg])
    t = k["t"]
    close = t + FUT_BAR_MS
    f = futures_features(k, cfg)
    breadth.add(f, close, coin)
    active = _active_mask(close, coin, data.segments)
    nxt_ok = np.zeros(len(t), dtype=bool)
    nxt_ok[:-1] = t[1:] == close[:-1]                 # Bir sonraki mum boşluksuz geliyor (giriş yapılabilir)
    active &= nxt_ok
    up = down = None
    crash = cfg.futures_tactic == "crash"
    if cfg.futures_btc_trend and not crash:
        up, down = trend_at(close, data.btc_trend)
    long_ok, short_ok = futures_candidates(f, cfg, up, down)
    both = (long_ok | short_ok) & active
    if funnel is not None:
        funnel.candles += int(active.sum())
        if crash:   # Huni: çöküş (tüm kurallar) → (likidasyon dalgası) → sinyal (teyitle)
            drop = crash_state(f, cfg)[0] & active
            funnel.liq += int(drop.sum())
            funnel.liq_vol += int((drop & f["liq_long"]).sum())
            funnel.liq_vol_trend += int(both.sum())
        else:
            funnel.liq += int(((f["liq_long"] | f["liq_short"]) & active).sum())
            funnel.liq_vol += int(((f["liq_long"] | f["liq_short"]) & f["vol_ok"] & active).sum())
            funnel.liq_vol_trend += int(both.sum())
    for i in np.flatnonzero(both):
        found = pick_signal(f, i, cfg, long_ok, short_ok)          # Canlıdaki seçim (öncelik, puan, long)
        if found is None:
            continue
        found["btc_trend"] = (1.0 if up is not None and up[i] else -1.0 if down is not None and down[i] else 0.0)
        signals.append(FutSignal(int(close[i]), symbol, found["side"], found["score"], float(k["o"][i + 1]),
                                 {key: v for key, v in found.items() if key not in ("side", "price", "priority")},
                                 int(found.get("priority", 0))))
    if wave_cfg is not None:    # Ana kuralın sinyal vermediği mumlarda dalga eşikleri (genişlik sonra süzülür)
        wl, ws = futures_candidates(f, wave_cfg, up, down)
        for i in np.flatnonzero((wl | ws) & active & ~both):
            found = pick_signal(f, i, wave_cfg, wl, ws)
            if found is None:
                continue
            found["btc_trend"] = 0.0
            found["wave_rule"] = 1.0
            wave_sigs.append(FutSignal(int(close[i]), symbol, found["side"], found["score"], float(k["o"][i + 1]),
                                       {key: v for key, v in found.items()
                                        if key not in ("side", "price", "priority")},
                                       int(found.get("priority", 0))))


def find_signals(cfg, data: FutData,
                 progress: Optional[Callable[[str], None]] = None) -> Tuple[List[FutSignal], Funnel]:
    """Canlı botun her 5 dk kapanışında yaptığı değerlendirmenin tüm geçmişe uygulanışı. Dakikalık taramada
    (FUTURES_MINUTE_SCAN; config.futures_scan_shifts) ara dakikalar da değerlendirilir: coinin 1 dk mumlarından o
    dakikada kapanan (kaydırılmış) 5 dk mumlar üretilir ve aynı fonksiyonlardan geçirilir (canlı bot aynısını yapar).
    5 dk sınırlarındaki sinyaller iki modda da birebir aynıdır."""
    signals: List[FutSignal] = []
    wave_sigs: List[FutSignal] = []
    funnel = Funnel()
    breadth = BreadthCounter(data)
    wave_cfg = futures_wave_cfg(cfg)            # Faz 6 dalga eşikleri (kapalıysa None)
    for symbol, k in _series(data):
        _coin_signals(cfg, wave_cfg, data, symbol, k, breadth, funnel, signals, wave_sigs)
    funnel.signals = len(signals)
    shifts = futures_scan_shifts(cfg) if data.minute_loader is not None else ()
    counters = {sh: BreadthCounter(data, sh) for sh in shifts}
    if shifts:
        symbols = list(data.lean_symbols) if data.loader is not None else list(data.k5)
        if progress:
            progress(f"⏱️ Her dakika tarama: {len(symbols)} coinin 1 dk mumları okunuyor (ilk seferde ~2 GB indirilir, "
                     f"birkaç dakika sürer)...")
        for n, symbol in enumerate(symbols):
            if progress and n and n % 10 == 0:
                progress(f"   1 dk mumlar: {n} / {len(symbols)} coin")
            k1 = data.minute_loader(symbol)
            if len(k1["t"]) < 500 * (FUT_BAR_MS // MIN_MS):
                continue
            for sh in shifts:
                _coin_signals(cfg, wave_cfg, data, symbol, resample(k1, FUT_BAR_MS, shift_ms=sh), counters[sh], None,
                              signals, wave_sigs)
        data.monthly_windows = True             # Çıkış pencereleri de aylık 1 dk dosyalardan (zaten indirildi)
    for counter in counters.values():
        counter.finish(())
    breadth.finish(signals + wave_sigs)
    if wave_sigs:                   # Canlıdaki gibi: dalga eşikleri yalnızca genişlik ≥ FUTURES_WAVE_BREADTH iken
        need = int(getattr(cfg, "futures_wave_breadth", 12))
        kept = [s for s in wave_sigs if s.breadth >= need]
        funnel.wave = sum(1 for s in kept if not s.close_ms % FUT_BAR_MS)
        signals += kept
    funnel.between = sum(1 for s in signals if s.close_ms % FUT_BAR_MS)
    signals.sort(key=signal_order)
    return signals, funnel


# ===================================================================== çıkış pencereleri
def load_windows(data: FutData, signals: Sequence[FutSignal], minutes: int = MAX_WINDOW_MIN,
                 progress: Optional[Callable[[str], None]] = None,
                 cache: Optional[Dict[Tuple[str, int], tuple]] = None) -> Dict[Tuple[str, int], tuple]:
    """Her sinyal için girişten itibaren `minutes` dakikalık mumlar: (aralık ms, t, o, h, l, c). 1 dk mumlar ay ay
    okunur (bellek: bir coinin tüm 1 dk geçmişi GB'larca olurdu); 1 dk verisi yoksa 5 dk mumlar kullanılır."""
    if data.loader is not None:                  # Evren modu: pencereler günlük dosyalardan, gerektiğinde
        return cache if isinstance(cache, LazyWindows) else LazyWindows(data, minutes)
    out: Dict[Tuple[str, int], tuple] = {} if cache is None else cache
    span = minutes * MIN_MS
    by_symbol: Dict[str, List[FutSignal]] = {}
    for s in signals:
        if (s.symbol, s.close_ms) not in out:
            by_symbol.setdefault(s.symbol, []).append(s)
    for symbol, sigs in by_symbol.items():
        k5 = data.k5[symbol]
        if data.exit_interval == "1m" and minutes <= LONG_WINDOW_MIN:
            months: Dict[Tuple[int, int], List[FutSignal]] = {}
            for s in sigs:
                d = datetime.fromtimestamp(s.close_ms / 1000, tz=timezone.utc)
                months.setdefault((d.year, d.month), []).append(s)
            if progress:
                progress(f"📥 {symbol}: 1 dk mumlar ({len(months)} ay)")
            for (y, m), group in sorted(months.items()):
                parts = [data.archive.month(symbol, "1m", y, m)]
                last_end = max(s.close_ms for s in group) + span
                ny, nm = y + (m == 12), m % 12 + 1
                if last_end > datetime(ny, nm, 1, tzinfo=timezone.utc).timestamp() * 1000:
                    parts.append(data.archive.month(symbol, "1m", ny, nm))
                k1 = concat(parts)
                for s in group:
                    win = slice_range(k1, s.close_ms, s.close_ms + span)
                    if len(win["t"]) and win["t"][0] == s.close_ms:
                        out[(symbol, s.close_ms)] = (MIN_MS, win["t"], win["o"], win["h"], win["l"], win["c"])
        for s in sigs:
            if (symbol, s.close_ms) not in out:
                win = slice_range(k5, s.close_ms, s.close_ms + span)
                out[(symbol, s.close_ms)] = (FUT_BAR_MS, win["t"], win["o"], win["h"], win["l"], win["c"])
    return out


MONTH_CACHE = 64        # Dakikalık taramada bellekte tutulan (parite, ay) 1 dk mum sayısı (≈ 2 MB / ay)


class LazyWindows(dict):
    """(parite, giriş ms) → çıkış penceresi; günlük 1 dk (uzun pencerede 5 dk) dosyalardan gerektiğinde okunur.
    Simülasyon yalnızca gerçekten açılan işlemlerin penceresini ister: binlerce sinyalin hepsi belleğe alınmaz."""

    def __init__(self, data: FutData, minutes: int):
        super().__init__()
        self.data = data
        self.span = minutes * MIN_MS
        self.interval = "1m" if data.exit_interval == "1m" and minutes <= LONG_WINDOW_MIN else "5m"
        self.bar = MIN_MS if self.interval == "1m" else FUT_BAR_MS
        self.days: Dict[Tuple[str, int], tuple] = {}
        self.months: Dict[Tuple[str, int, int], tuple] = {}     # Dakikalık tarama: (parite, yıl, ay) → 1 dk mumlar

    def _month(self, symbol: str, year: int, month: int) -> tuple:
        """Ayın 1 dk mumları (bitmemiş ayda günlük dosyalardan); en çok MONTH_CACHE ay bellekte tutulur."""
        key = (symbol, year, month)
        hit = self.months.pop(key, None)
        if hit is None:
            a = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
            b = int(datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            arr = self.data.archive.klines(symbol, a, b, "1m")
            hit = tuple(arr[k] for k in ("t", "o", "h", "l", "c"))
            while len(self.months) >= MONTH_CACHE:
                self.months.pop(next(iter(self.months)))
        self.months[key] = hit                               # En son kullanılan sona (LRU)
        return hit

    def _day(self, symbol: str, day_ms: int) -> tuple:
        key = (symbol, day_ms)
        if key not in self.days:
            arr = self.data.archive.day(symbol, self.interval, datetime.fromtimestamp(day_ms / 1000, tz=timezone.utc))
            self.days[key] = tuple(arr[k] for k in ("t", "o", "h", "l", "c"))
        return self.days[key]

    def get(self, key, default=None):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        symbol, close_ms = key
        if self.interval == "1m" and getattr(self.data, "monthly_windows", False):
            a = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
            b = datetime.fromtimestamp((close_ms + self.span - 1) / 1000, tz=timezone.utc)
            parts = [self._month(symbol, a.year, a.month)]
            if (b.year, b.month) != (a.year, a.month):
                parts.append(self._month(symbol, b.year, b.month))
        else:
            first = close_ms // DAY_MS * DAY_MS
            parts = [self._day(symbol, d) for d in range(first, close_ms + self.span, DAY_MS)]
        cols = list(parts[0]) if len(parts) == 1 else [np.concatenate([p[i] for p in parts]) for i in range(5)]
        t = cols[0]
        i, j = np.searchsorted(t, close_ms), np.searchsorted(t, close_ms + self.span)
        win = (self.bar, t[i:j].copy(), cols[1][i:j].copy(), cols[2][i:j].copy(), cols[3][i:j].copy(),
               cols[4][i:j].copy()) if j > i and int(t[i]) == close_ms else None
        dict.__setitem__(self, key, win)
        return win if win is not None else default


# ===================================================================== simülasyon
# Giriş gecikmesi. Backtest girişi sinyal mumundan sonraki mumun AÇILIŞINDAN sayar; canlı bot mum kapandıktan sonra
# tarar (SCAN_DELAY_SEC + mumların indirilmesi + emir ≈ 5 sn). Aynı saniyelik veride çöküş mumunun kapanışından sonra
# fiyat ortalama 3 sn'de %0.04, 5 sn'de %0.05, 10 sn'de %0.08 yükseliyor (2.563 işlem; ± %0.01): piyasa emriyle
# girişlerde bu kadar daha pahalıya alınmış sayılır (limit girişte uygulanmaz).
ENTRY_LAG_PCT = 0.05
# Pay, botun giriş yoluna göre (entry_lag_for): hızlı giriş açıkken sinyaller kapanıştan ~2.0 sn sonra hazır (rapor 22,
# 1.478 canlı tarama), dolum ~2.5-3 sn → saniyelik veride 3 sn'de +%0.04. Anlık taramada (FUTURES_SNAPSHOT_SCAN) canlı
# dolumlar 0.6 sn'de (Kasım 2026, ilk 3 gerçek giriş: kapanış fiyatına göre %0.000 / +%0.111 / −%0.043, ortalama +%0.023
# kayma dahil) → saniyelik veride 1 sn'de +%0.015-0.023: pay %0.02. Bu pay kayma (futures_slippage_pct) üstüne eklenir.
ENTRY_LAG_FAST_PCT = 0.04
FAST_ENTRY_LAG_PCT = 0.02
# Stop dolumu (zarar kes / başabaş / izleyen stop; canlıda Binance'te STOP_MARKET, son işlem fiyatıyla tetiklenir).
# Saniyelik işlem verisinde (300 coin-gün; rapor 13 ayarıyla 179 zarar kes ve ~1.010 başabaş çıkışı; Kasım 2026) emrin
# dolumuna en yakın ölçüler (stopu tetikleyen işlemden hemen sonraki işlem / sonraki 50 ms'nin ortancası) kaymanın
# (futures_slippage_pct, %0.02) üstüne başabaşta +%0.015 / +%0.019 (± %0.012), zarar keste −%0.018 / +%0.044 (± %0.02-0.05)
# gösterdi; tetikleyen işlemin kendisi +%0.024-0.028. Fark çöküş anlarından geliyor (ortanca dolum stoptan ~%0.01
# aşağıda, %99'luk dilim %0.7-0.8). Kötümser stop stresi (fitilin yarısı) bunun çok ötesindedir: ölçülen dolum mumun stop
# ötesindeki fitilinin ~%2-3'ü.
STOP_SLIP_PCT = 0.015


def entry_lag_for(cfg) -> Tuple[float, str]:
    """Ayardaki giriş yoluna göre gecikme payı (fiyat %) ve açıklaması: anlık tarama ~0.6 sn (canlıda ölçüldü), hızlı
    giriş ~2.5 sn, ikisi de kapalıyken ~5 sn."""
    if futures_snapshot_on(cfg):
        return FAST_ENTRY_LAG_PCT, "~0.6 sn sonra (anlık tarama; canlıda ölçüldü)"
    if getattr(cfg, "futures_fast_entry", False):
        return ENTRY_LAG_FAST_PCT, "~2.5 sn sonra (hızlı giriş)"
    return ENTRY_LAG_PCT, "~5 sn sonra"


def lag_text(lag: float) -> str:
    """Gecikme payının rapordaki açıklaması."""
    if lag <= FAST_ENTRY_LAG_PCT:
        return "~0.6 sn sonra (anlık tarama; canlıda ölçüldü)"
    if lag <= ENTRY_LAG_FAST_PCT:
        return "~2.5 sn sonra (hızlı giriş)"
    return "~5 sn sonra"


@dataclass(frozen=True)
class SimParams:
    leverage: int
    tp_pct: float
    sl_pct: float
    be_enabled: bool
    be_trigger_pct: float
    be_floor_pct: float
    hold_min: int
    margin_usage_pct: float
    taker_fee: float
    maker_fee: float
    slippage_pct: float
    tp_limit: bool
    mmr_pct: float
    start_capital: float
    kill_pct: float
    max_notional: float = MAX_NOTIONAL
    symbol_cooldown_min: int = 0        # Her işlemden sonra aynı coine bekleme (SYMBOL_COOLDOWN_MINUTES, canlıyla aynı)
    harsh_stops: bool = False           # Stres: stop emri, mumun stopun ötesindeki fitilinin yarısında dolar
    # Canlı botun risk sigortaları (trader._after_close ile aynı): art arda zarar kes molası, günlük zarar / kâr limiti
    loss_streak_limit: int = 0
    loss_streak_pause_h: float = 4.0
    daily_loss_pct: float = 0.0
    daily_profit_pct: float = 0.0
    loss_cooldown_min: int = 0          # Zararla kapanınca (daha uzunsa) bekleme (LOSS_COOLDOWN_MINUTES)
    # İzleyen stop (fiyat %): görülen en iyi fiyat girişten +trail_act_pct'ye ulaşınca stop, o tepenin trail_gap_pct
    # gerisine taşınır ve yalnızca lehte yönde ilerler (canlı: FuturesBot._check_trailing)
    trail_enabled: bool = False
    trail_act_pct: float = 3.0
    trail_gap_pct: float = 1.5
    # Limit (maker) giriş (FUTURES_ENTRY_ORDER=limit): sinyal anındaki fiyatın limit_offset_pct gerisine emir; süre
    # (limit_ttl_min) içinde bir mumun ucu limitin limit_through_pct ötesine geçerse limit fiyattan dolar (maker
    # komisyon, kayma yok), geçmezse işlem yok (canlı: emir iptal)
    entry_limit: bool = False
    limit_offset_pct: float = 0.05
    limit_ttl_min: int = 3
    limit_through_pct: float = 0.02
    limit_strict: bool = False          # Stres: limitin dolduğu mumda lehte hareket (başabaş / hedef) sayılmaz
    limit_fallback: bool = False        # Süre dolunca dolmayan emir yerine piyasa emri (taker) ile girilir
    # Dalga modu (FUTURES_MAX_POSITIONS ...): en fazla max_positions pozisyon, her biri bakiyenin usage / N'i teminat;
    # açık pozisyon varken yenisi yalnızca genişlik ≥ extra_breadth ise; genişlik < isolated_breadth ise büyüklük
    # isolated_size_pct (%)
    max_positions: int = 1
    extra_breadth: int = 8
    isolated_size_pct: float = 100.0
    isolated_breadth: int = 2
    # Çöküş sürüyor mu (FUTURES_RECENT_SIZE_PCT; canlı: FuturesBot._entry_margin): girişten önceki 15 dk'da düşüş
    # recent_drop_pct'nin altındaysa (sinyal ölçüsü metrics["drop15"]) pozisyon recent_size_pct (%; 100 = kapalı)
    recent_size_pct: float = 100.0
    recent_drop_pct: float = 2.4
    # Vadeli-spot farkı (FUTURES_BASIS_SIZE_PCT; canlı: FuturesBot._entry_margin): girişte vadeli fiyat spotun
    # basis_deep_pct (%) değerinden fazla altındaysa (sinyal ölçüsü metrics["basis"], attach_basis) pozisyon basis_size_pct (%)
    basis_size_pct: float = 100.0
    basis_deep_pct: float = 0.5
    # Sinyal kalitesi (FUTURES_QUALITY_SIZE_PCT; canlı: FuturesBot._entry_margin): strategy.quality_score sinyallerin en
    # zayıf quality_cut_pct yüzdesindeyse (strategy.QUALITY_CUTS) pozisyon quality_size_pct (%; 100 = kapalı)
    quality_size_pct: float = 100.0
    quality_cut_pct: float = 60.0
    # Büyük hesap kuralları (FUTURES_BIG_ACCOUNT_USDT; canlı: FuturesBot._scan_and_enter): gerçekleşen bakiye bu tutarı
    # aşınca en güçlü %20'lik sinyaller BIG_EXTRA_SLOTS ek pozisyon açabilir, zayıf sinyale son fonlama ≤ 0 ise girilmez
    big_account_usdt: float = 0.0
    # En küçük emre yuvarlama (FUTURES_QUALITY_ROUNDUP; canlı: FuturesBot.open_futures): kalite puanı en güçlü %40'taki
    # sinyal en küçük emir tutarının altında kalırsa boşta teminat yetiyorsa o tutarla açılır
    quality_roundup: bool = False
    # Faz 6: ek pozisyonların büyüklüğü (ilk pozisyonun %'si; 100 = eşit, Faz 5) ve dalgada yeniden giriş (genişlik
    # ≥ reentry_breadth iken zarar sonrası bekleme atlanır; 0 = kapalı)
    extra_size_pct: float = 100.0
    reentry_breadth: int = 0
    # Faz 8: ek pozisyonların (başka pozisyon açıkken açılanlar) başabaş tetiği (fiyat %; 0 = be_trigger_pct ile aynı)
    extra_be_trigger_pct: float = 0.0
    # Faz 9: pozisyona göre zaman stopu (dk; 0 = hold_min): ek pozisyonlar; ilk pozisyon girişte genişlik ≥
    # wave_hold_breadth ise (config.futures_hold_minutes ile aynı kural)
    extra_hold_min: int = 0
    wave_hold_min: int = 0
    wave_hold_breadth: int = 8
    # Düşüşte küçültme (FUTURES_DD_SCALE_PCT; canlı: FuturesBot._entry_margin): gerçekleşen bakiye o ana kadarki
    # zirvesinin dd_scale_pct (%) ya da daha fazla altındayken yeni pozisyonun teminatı × dd_scale_size_pct / 100
    # (0 = kapalı; sabit tutarlı C senaryosunda uygulanmaz)
    dd_scale_pct: float = 0.0
    dd_scale_size_pct: float = 60.0
    # 24 saat freni (FUTURES_DAY_BRAKE_PCT; canlı: FuturesBot._entry_margin): gerçekleşen bakiye son 24 saatin zirvesinin
    # day_brake_pct (%) ya da daha fazla altındayken yeni pozisyonun boyu × day_brake_size_pct / 100 (0 = kapalı; sabit
    # tutarlı C senaryosunda uygulanmaz)
    day_brake_pct: float = 0.0
    day_brake_size_pct: float = 50.0
    # Ayı piyasası küçültmesi (FUTURES_BEAR_SIZE_PCT; canlı: FuturesBot._bear_factor): BTC'nin girişten önceki günkü
    # kapanışı bear_sma_days günlük ortalamasının altındaysa boy × bear_size_pct / 100 (sinyal ölçüsü metrics["bearN"],
    # attach_regime; 100 = kapalı)
    bear_size_pct: float = 100.0
    bear_sma_days: int = 100
    # Sermaye koruması (FUTURES_GUARD_PCT; canlı: FuturesBot._guard_factor): gerçekleşen bakiye başlangıç sermayesinin
    # guard_pct (%) ya da daha fazla altındayken (toplam zarar) yeni pozisyonun boyu × guard_size_pct / 100 (0 = kapalı;
    # sabit tutarlı C senaryosunda uygulanmaz)
    guard_pct: float = 0.0
    guard_size_pct: float = 60.0
    # Kâr yastığı (FUTURES_CUSHION_X; canlı: FuturesBot._guard_factor): gerçekleşen bakiye başlangıç sermayesinin
    # cushion_x katı ya da üstündeyken yeni pozisyonun boyu × cushion_size_pct / 100 (0 = kapalı; C'de uygulanmaz)
    cushion_x: float = 0.0
    cushion_size_pct: float = 112.5
    # Destek bölgesi (FUTURES_SUPPORT_SIZE_PCT; canlı: FuturesBot._support_factor): fiyat coinin önceki support_days
    # günlük dibinin (sinyalden en az 1 saat önce kapanmış 1 saatlik mumlar; sinyal ölçüsü metrics["lowNh"], attach_support)
    # altında ama en fazla support_zone_pct (%) altındaysa boy × support_size_pct / 100 (100 = kapalı)
    support_size_pct: float = 100.0
    support_days: int = 30
    support_zone_pct: float = 5.0
    # V dönüşü (FUTURES_BOUNCE_*; canlı: FuturesBot._entry_margin): fiyat son 15 dk'nın dibinden (sinyal ölçüsü
    # metrics["bounce15"]) bounce_calm_pct yukarıdaysa durulmuş küçültmesi yok; bounce_boost_pct yukarıdaysa boy ×
    # bounce_boost_size_pct / 100 (0 = kapalı)
    bounce_calm_pct: float = 0.0
    bounce_boost_pct: float = 0.0
    bounce_boost_size_pct: float = 150.0
    # Emir defteri derinliği (futures_depth; veri FutData.depth'te, yoksa ikisi de uygulanmaz): depth_cap_pct > 0 ise giriş
    # tutarı girişten önceki 2 dk'da ±%1 içindeki alış / satış emirlerinin küçüğünün bu yüzdesiyle sınırlı ve aynı mumdaki
    # sinyallerde sınıra takılınca kapasitesi büyük olan önce (FUTURES_DEPTH_CAP_PCT); depth_impact: piyasa emrinin defteri
    # tüketmesinden gelen kayma giriş ve çıkışta kârdan düşülür (FUTURES_BACKTEST_DEPTH)
    depth_cap_pct: float = 0.0
    depth_impact: bool = False
    # Mum içi başabaş gerçekçiliği (bkz. be_retouch_prob): başabaşın tetiklendiği 1 dk mumda fiyat tabana geri dönmüş
    # olabilir. False = eski iyimser varsayım (yükselen mumda yalnızca kapanış tabanın altındaysa çıkılır).
    be_intrabar: bool = True
    entry_lag_pct: float = ENTRY_LAG_PCT   # Piyasa emriyle girişte gecikme payı (fiyat %; 0 = mum açılışından giriş)
    stop_slip_pct: float = 0.0          # Stop dolumunda kaymaya ek pay (fiyat %; raporda STOP_SLIP_PCT, ölçülen)
    be_force: Optional[bool] = None     # Yalnızca kalibrasyon / test: o mumdaki çekilişi zorla (True çık, False kal)
    be_seed: int = 0                    # Mum içi başabaş çekilişinin tohumu (0 = raporun kendi çekilişi; bkz. draw_spread)


def params_from_cfg(cfg, **over) -> SimParams:
    discount = 1 - BNB_FUTURES_DISCOUNT if getattr(cfg, "bnb_fees", False) else 1.0
    p = SimParams(leverage=cfg.futures_leverage, tp_pct=cfg.take_profit_pct, sl_pct=cfg.stop_loss_pct,
                  be_enabled=cfg.breakeven_enabled, be_trigger_pct=cfg.breakeven_trigger_pct,
                  be_floor_pct=cfg.breakeven_floor_pct, hold_min=cfg.max_hold_minutes,
                  margin_usage_pct=cfg.futures_margin_usage_pct, taker_fee=cfg.futures_taker_fee * discount,
                  maker_fee=cfg.futures_maker_fee * discount, slippage_pct=cfg.futures_slippage_pct,
                  tp_limit=cfg.futures_tp_order == "limit", mmr_pct=cfg.futures_mmr_pct,
                  start_capital=cfg.start_capital, kill_pct=cfg.max_total_loss_pct,
                  symbol_cooldown_min=cfg.symbol_cooldown_minutes, loss_cooldown_min=cfg.loss_cooldown_minutes,
                  loss_streak_limit=cfg.loss_streak_limit, loss_streak_pause_h=cfg.loss_streak_pause_hours,
                  daily_loss_pct=cfg.daily_loss_limit_pct, daily_profit_pct=cfg.daily_profit_target_pct,
                  trail_enabled=cfg.futures_trailing, trail_act_pct=cfg.futures_trailing_act_pct,
                  trail_gap_pct=cfg.futures_trailing_gap_pct,
                  entry_limit=getattr(cfg, "futures_entry_order", "market") == "limit",
                  limit_offset_pct=float(getattr(cfg, "futures_limit_offset_pct", 0.05)),
                  limit_ttl_min=int(getattr(cfg, "futures_limit_ttl_min", 3)),
                  max_positions=int(getattr(cfg, "futures_max_positions", 1)),
                  extra_breadth=int(getattr(cfg, "futures_extra_breadth", 8)),
                  isolated_size_pct=float(getattr(cfg, "futures_isolated_size_pct", 100.0)),
                  isolated_breadth=int(getattr(cfg, "futures_isolated_breadth", 2)),
                  recent_size_pct=float(getattr(cfg, "futures_recent_size_pct", 100.0)),
                  recent_drop_pct=float(getattr(cfg, "futures_recent_drop_pct", 2.4)),
                  basis_size_pct=float(getattr(cfg, "futures_basis_size_pct", 100.0)),
                  basis_deep_pct=float(getattr(cfg, "futures_basis_deep_pct", 0.5)),
                  quality_size_pct=float(getattr(cfg, "futures_quality_size_pct", 100.0)),
                  quality_cut_pct=float(getattr(cfg, "futures_quality_cut_pct", 60.0)),
                  big_account_usdt=float(getattr(cfg, "futures_big_account_usdt", 0.0)),
                  quality_roundup=bool(getattr(cfg, "futures_quality_roundup", False)),
                  extra_size_pct=float(getattr(cfg, "futures_extra_size_pct", 100.0)),
                  reentry_breadth=int(getattr(cfg, "futures_reentry_breadth", 0)),
                  extra_be_trigger_pct=float(getattr(cfg, "futures_extra_be_trigger_pct", 0.0)),
                  extra_hold_min=int(getattr(cfg, "futures_extra_hold_minutes", 0)),
                  wave_hold_min=int(getattr(cfg, "futures_wave_hold_minutes", 0)),
                  wave_hold_breadth=int(getattr(cfg, "futures_wave_hold_breadth", 8)),
                  dd_scale_pct=float(getattr(cfg, "futures_dd_scale_pct", 0.0) or 0.0),
                  dd_scale_size_pct=float(getattr(cfg, "futures_dd_scale_size_pct", 60.0)),
                  day_brake_pct=float(getattr(cfg, "futures_day_brake_pct", 0.0) or 0.0),
                  day_brake_size_pct=float(getattr(cfg, "futures_day_brake_size_pct", 50.0)),
                  bear_size_pct=float(getattr(cfg, "futures_bear_size_pct", 100.0)),
                  bear_sma_days=int(getattr(cfg, "futures_bear_sma_days", 100)),
                  guard_pct=float(getattr(cfg, "futures_guard_pct", 0.0) or 0.0),
                  guard_size_pct=float(getattr(cfg, "futures_guard_size_pct", 60.0)),
                  cushion_x=float(getattr(cfg, "futures_cushion_x", 0.0) or 0.0),
                  cushion_size_pct=float(getattr(cfg, "futures_cushion_size_pct", 112.5)),
                  support_size_pct=float(getattr(cfg, "futures_support_size_pct", 100.0)),
                  support_days=int(getattr(cfg, "futures_support_days", 30)),
                  support_zone_pct=float(getattr(cfg, "futures_support_zone_pct", 5.0)),
                  bounce_calm_pct=float(getattr(cfg, "futures_bounce_calm_pct", 0.0) or 0.0),
                  bounce_boost_pct=float(getattr(cfg, "futures_bounce_boost_pct", 0.0) or 0.0),
                  bounce_boost_size_pct=float(getattr(cfg, "futures_bounce_boost_size_pct", 150.0)),
                  depth_cap_pct=float(getattr(cfg, "futures_depth_cap_pct", 0.0) or 0.0),
                  depth_impact=bool(getattr(cfg, "futures_backtest_depth", False)),
                  entry_lag_pct=entry_lag_for(cfg)[0], stop_slip_pct=STOP_SLIP_PCT)
    return replace(p, **over) if over else p


def hold_minutes(p: SimParams, n_open: int, breadth: int) -> int:
    """Yeni pozisyonun zaman stopu (dk), config.futures_hold_minutes ile aynı kural (Faz 9): ek pozisyonda (n_open ≥ 1,
    dalga modu) extra_hold_min; ilk pozisyonda genişlik ≥ wave_hold_breadth ise wave_hold_min; yoksa hold_min."""
    if n_open >= 1:
        return p.extra_hold_min if p.extra_hold_min > 0 and p.max_positions > 1 else p.hold_min
    if p.wave_hold_min > 0 and breadth >= p.wave_hold_breadth:
        return p.wave_hold_min
    return p.hold_min


def phase9_active(p: SimParams) -> bool:
    """Faz 9 etkin mi: bazı pozisyonlar ana ayardan farklı bir zaman stopu kullanıyor."""
    return (p.extra_hold_min > 0 and p.max_positions > 1 and p.extra_hold_min != p.hold_min) or (
        p.wave_hold_min > 0 and p.wave_hold_min != p.hold_min)


def window_minutes(cfg) -> int:
    """Çıkış penceresinin uzunluğu (dk): en uzun zaman stopu + pay (Faz 9 süreleri dahil)."""
    longest = max(int(cfg.max_hold_minutes), int(getattr(cfg, "futures_extra_hold_minutes", 0)),
                  int(getattr(cfg, "futures_wave_hold_minutes", 0)))
    return max(MAX_WINDOW_MIN, longest + 10)


@dataclass
class FutTrade:
    symbol: str
    side: str
    entry_ms: int
    exit_ms: int
    entry: float
    exit: float
    reason: str
    leverage: int
    margin: float
    notional: float
    fees: float
    funding: float
    pnl: float
    roe_pct: float
    equity_after: float
    mae_pct: float          # İşlem boyunca en kötü fiyat hareketi (% , ters yönde pozitif)
    window_mae_pct: float   # Zaman stopuna kadar (stop olmasaydı) en kötü hareket: marjin riski için
    gap: bool               # Stop / likidasyon fiyatının ötesinde açılan mumla kapandı
    liquidated: bool
    bar_ms: int
    score: float = 0.0
    kind: float = 0.0       # Giriş modülü (metrics["tactic"]: 1 çöküş, 2 pompa, 3 Bollinger, 4 kırılım; 0 = liq4)
    breadth: int = 0        # Girişte genişlik (son 1 saatte %3+ düşen coin sayısı)
    slot: int = 1           # Açıldığında kaçıncı pozisyondu (dalga modu)
    reentry: bool = False   # Faz 6: dalgada zarar sonrası beklemesi atlanarak açıldı
    wave_rule: bool = False  # Faz 6: dalga eşiğiyle (FUTURES_WAVE_RULES) gelen sinyal
    be_bar: tuple = ()      # Başabaşın tetiklendiği mumda taban da görüldüyse o mum (açılış, lehte uç, ters uç, kapanış)
    be_q: float = -1.0      # O mumda tabana geri dönüş olasılığı (be_retouch_prob; -1 = böyle bir mum olmadı)
    dd_scaled: bool = False  # Düşüşte küçültmeyle (FUTURES_DD_SCALE_PCT) küçük boyla açıldı
    braked: bool = False     # 24 saat freniyle (FUTURES_DAY_BRAKE_PCT) küçük boyla açıldı
    bear: bool = False       # Ayı piyasası küçültmesiyle (FUTURES_BEAR_SIZE_PCT) küçük boyla açıldı
    guarded: bool = False    # Sermaye korumasıyla (FUTURES_GUARD_PCT) küçük boyla açıldı
    cushioned: bool = False  # Kâr yastığıyla (FUTURES_CUSHION_X) büyük boyla açıldı
    support: bool = False    # Destek bölgesi kuralıyla (FUTURES_SUPPORT_SIZE_PCT) küçük boyla açıldı
    bounced: int = 0         # V dönüşü (FUTURES_BOUNCE_*): 1 = durulmuş küçültmesi kalktı, 2 = büyük boyla açıldı
    depth_capped: bool = False  # Derinliğe göre boy sınırına takıldı (FUTURES_DEPTH_CAP_PCT)
    impact_pct: float = 0.0  # Emir defteri kayması, giriş + çıkış (fiyat %; FUTURES_BACKTEST_DEPTH)
    drop15: float = float("nan")  # Girişten önceki 15 dk'daki düşüş (%; sinyal ölçüsü, FUTURES_RECENT_SIZE_PCT)
    basis: float = float("nan")   # Girişte vadeli / spot fiyat farkı (%; FUTURES_BASIS_SIZE_PCT, attach_basis)
    quality: float = float("nan")  # Sinyal kalitesi puanı (0-1; FUTURES_QUALITY_SIZE_PCT, strategy.quality_score)
    rounded: bool = False          # En küçük emir tutarına yuvarlanarak açıldı (FUTURES_QUALITY_ROUNDUP)


@dataclass
class FutResult:
    params: SimParams
    trades: List[FutTrade]
    curve: List[Tuple[int, float]]
    skipped: Counter
    start_ms: int
    end_ms: int
    ruined_ms: Optional[int] = None
    kill_ms: Optional[int] = None

    @property
    def final_equity(self) -> float:
        return self.curve[-1][1] if self.curve else self.params.start_capital

    @property
    def net_pct(self) -> float:
        return (self.final_equity / self.params.start_capital - 1) * 100


def _funding_cost(data: FutData, symbol: str, side: int, notional: float, entry_ms: int, exit_ms: int) -> float:
    t, r = data.funding.get(symbol, (np.zeros(0, dtype=np.int64), np.zeros(0)))
    if len(t):
        i, j = np.searchsorted(t, entry_ms, "right"), np.searchsorted(t, exit_ms, "right")
        total = float(r[i:j].sum())
        if j == len(t) and exit_ms > t[-1] + 8 * FUT_HOUR_MS:   # Verinin bittiği yer (bitmemiş ay): varsayılan oran
            total += FUNDING_DEFAULT * _funding_count(max(entry_ms, int(t[-1])), exit_ms)
    else:
        total = FUNDING_DEFAULT * _funding_count(entry_ms, exit_ms)
    return side * notional * total


def _funding_count(a_ms: int, b_ms: int) -> int:
    """(a, b] aralığındaki fonlama anı sayısı (00/08/16 UTC)."""
    step = 8 * FUT_HOUR_MS
    return max(0, b_ms // step - a_ms // step)


def limit_fill(p: SimParams, s: FutSignal, window: tuple) -> Optional[Tuple[int, float]]:
    """Limit (maker) giriş: (dolduğu mumun penceredeki indeksi, limit fiyatı) ya da None (süre içinde dolmadı).

    Referans fiyat sinyal mumunun kapanışındaki fiyattır (bir sonraki mumun açılışı; canlıda bid / ask). Limit = referans
    × (1 - offset) long'da (short'ta +). Dolum için süre (FUTURES_LIMIT_TTL_MIN) içindeki bir mumun dibi limitin
    limit_through_pct altına inmeli: yalnızca dokunması yetmez (sıradaki emirler önce dolar; temkinli varsayım).
    5 dk pencerede (1 dk veri yoksa) yalnızca ilk mum bakılır (3 dk yerine 5 dk: biraz iyimser)."""
    _bar, t, _o, h, l, _c = window
    side = 1 if s.side == "long" else -1
    limit = s.entry_open * (1 - side * p.limit_offset_pct / 100)
    need = limit * (1 - side * p.limit_through_pct / 100)
    end = s.close_ms + p.limit_ttl_min * MIN_MS
    for j in range(len(t)):
        if int(t[j]) >= end:
            break
        if (l[j] < need) if side > 0 else (h[j] > need):
            return j, float(limit)
    return None


# Mum içi başabaş gerçekçiliği. 1 dk mumun içindeki sıra bilinmez; başabaş tetiği ile taban birbirine yakınsa (ör. Faz
# 8'de %0.2) fiyat tetiğe değdiği dakikanın içinde çoğu zaman tabana geri döner ve stop çalışır. Eski varsayım
# (yükselen mumda dip → tepe → kapanış) bunu görmez ve dar aralıklı başabaşı olduğundan kârlı gösterir. Kasım 2026'da
# Binance'in saniyelik işlem verisiyle (aggTrades; 920 coin-gün, 2.563 işlem) canlı botun mantığı (2 sn'de bir fiyat
# yoklaması, stopun taşınması) yeniden oynatıldı: eski motor işlem başına tetik +%1'de ~%0.02-0.10, +%0.5'te
# ~%0.13-0.17, +%0.4'te ~%0.16-0.21 iyimserdi. Aşağıdaki olasılık modeli o örnekle ayarlandı (örneğin yarısıyla
# ayarlanıp diğer yarısında doğrulandı; 26 başabaş / zarar kes ayarında fark ±%0.04 içinde, bkz. README).
BE_RETOUCH_K = 45.0


def be_retouch_prob(side: int, best: float, adverse: float, c: float, trig: float, floor: float,
                    k: Optional[float] = None) -> float:
    """Başabaşın tetiklendiği mumda (ters uç tabanın gerisinde, kapanış ilerisinde) fiyatın tetiğe değdikten SONRA
    tabana dönmüş olma olasılığı: 1 / (1 + k · x), x = aralık · (kapanış - taban) / mum_aralığı². Aralık (tetik - taban)
    dar, mum geniş ve kapanış tabana yakınsa olasılık yüksek; geniş aralıkta ve sakin mumda sıfıra yakın."""
    k = BE_RETOUCH_K if k is None else k
    gap = abs(trig - floor)
    rng = abs(best - adverse)
    if gap <= 0 or rng <= 0:
        return 1.0
    x = gap * max(0.0, side * (c - floor)) / (rng * rng)
    return 1.0 / (1.0 + k * x)


def trade_uniform(symbol: str, close_ms: int, seed: int = 0) -> float:
    """İşlem başına sabit, ayarlardan bağımsız [0, 1) sayı (coin + giriş zamanı): aynı işlem farklı ayarlarda aynı
    çekilişi kullanır, sonuçlar çalıştırmadan çalıştırmaya değişmez. `seed` ≠ 0: başka bir çekiliş dizisi (aynı
    ayarın sonucu çekilişe ne kadar bağlı: draw_spread)."""
    key = f"{symbol}:{int(close_ms)}" if not seed else f"{int(seed)}|{symbol}:{int(close_ms)}"
    return zlib.crc32(key.encode()) / 4294967296.0


def run_trade(p: SimParams, s: FutSignal, window: tuple, margin: float, notional: float,
              data: Optional[FutData] = None) -> Optional[FutTrade]:
    """Tek işlem: giriş, başabaş, izleyen stop, Kâr Al / Zarar Kes / zaman stopu, likidasyon (yalıtılmış teminat).
    Limit girişte emir süresinde dolmazsa None (işlem yok)."""
    bar_ms, t, o, h, l, c = window
    side = 1 if s.side == "long" else -1
    slip = p.slippage_pct / 100
    sslip = slip + p.stop_slip_pct / 100                # Stop dolumu: kayma + ölçülen stop payı
    lag = p.entry_lag_pct / 100
    start, entry_ms, maker = 0, s.close_ms, False
    if p.entry_limit:
        got = limit_fill(p, s, window)
        if got is not None:
            (start, entry), maker = got, True
        elif p.limit_fallback:                          # Süre doldu: kalan fırsat piyasa emriyle (taker)
            start = int(np.searchsorted(t, s.close_ms + p.limit_ttl_min * MIN_MS))
            if start >= len(t):
                return None
            entry = o[start] * (1 + side * (slip + lag))
        else:
            return None
        entry_ms = int(t[start])
    else:
        entry = s.entry_open * (1 + side * (slip + lag))
    tp = entry * (1 + side * p.tp_pct / 100)
    sl = entry * (1 - side * p.sl_pct / 100)
    be_trig = entry * (1 + side * p.be_trigger_pct / 100)
    be_stop = entry * (1 + side * p.be_floor_pct / 100)
    trail_trig = entry * (1 + side * p.trail_act_pct / 100)
    trail_gap = p.trail_gap_pct / 100
    liq = entry * (1 - side * (1 / p.leverage - p.mmr_pct / 100))
    deadline = entry_ms + p.hold_min * MIN_MS
    stop, kind = sl, "SL"                                   # Geçerli stop ve türü (SL → BE → TRAIL)
    be_on = trail_on = False
    peak = entry
    exit_price, reason, exit_ms, gap, liquidated = None, "END", None, False, False
    worst = 0.0
    be_bar, be_q = (), -1.0

    def worse(price: float, level: float) -> bool:      # Fiyat seviyenin ters tarafında (veya üstünde)
        return price <= level if side > 0 else price >= level

    def reached(price: float, level: float) -> bool:    # Fiyat lehte seviyeye ulaştı
        return price >= level if side > 0 else price <= level

    def favorable(fj: float) -> None:
        """Mumun lehteki ucu görüldü: başabaş tetiği ve izleyen stop (stop yalnızca lehte yönde ilerler)."""
        nonlocal stop, kind, be_on, trail_on, peak
        if p.be_enabled and not be_on and reached(fj, be_trig):
            be_on = True
            if worse(stop, be_stop):
                stop, kind = be_stop, "BE"
        if p.trail_enabled:
            if reached(fj, peak):
                peak = fj
            if not trail_on and reached(peak, trail_trig):
                trail_on = True
            level = peak * (1 - side * trail_gap)
            if trail_on and worse(stop, level) and stop != level:
                stop, kind = level, "TRAIL"

    adverse = l if side > 0 else h
    best = h if side > 0 else l
    n = len(t)
    for j in range(start, n):
        tj = int(t[j])
        if maker and j == start:
            # Limitin dolduğu mum: fiyat limiti geçerken dolduk, ters uç (dip) dolumdan sonra görüldü → önce zarar kes.
            # Mum içi sıra standart varsayımla: yükselen mumda dip tepeden önce gelir (dolum → dip → tepe → kapanış);
            # düşen mumda tepe dolumdan önceydi, dolumdan sonra yalnızca kapanış görülür. limit_strict: o mumda lehte
            # hareket hiç sayılmaz (stres).
            worst = max(worst, side * (entry - adverse[j]) / entry * 100)
            if worse(adverse[j], stop):
                exit_price, reason, exit_ms = stop * (1 - side * sslip), kind, tj
                break
            if p.limit_strict:
                continue
            cj = c[j]
            adverse_first = cj >= o[j] if side > 0 else cj <= o[j]
            fj = best[j] if adverse_first else cj
            if reached(fj, tp):
                exit_price = tp if p.tp_limit else tp * (1 - side * slip)
                reason, exit_ms = "TP", tj
                break
            favorable(fj)
            if worse(cj, stop):
                exit_price, reason, exit_ms = stop * (1 - side * sslip), kind, tj
                break
            continue
        if tj >= deadline:
            exit_price, reason, exit_ms = o[j] * (1 - side * slip), "TIME", tj
            break
        oj, aj, fj = o[j], adverse[j], best[j]
        if worse(oj, liq):                                  # Likidasyon fiyatının ötesinde açıldı: stop çalışamadı
            exit_price, reason, exit_ms, gap, liquidated = oj, "LIQ", tj, True, True
            worst = max(worst, side * (entry - oj) / entry * 100)
            break
        if worse(oj, stop):                                 # Stopun ötesinde açıldı (fiyat boşluğu)
            exit_price, reason, exit_ms, gap = oj * (1 - side * slip), kind, tj, True
            worst = max(worst, side * (entry - oj) / entry * 100)
            break
        worst = max(worst, side * (entry - aj) / entry * 100)
        # Mum içi sıra (standart OHLC varsayımı): yükselen mum açılış → dip → tepe → kapanış, düşen mum açılış →
        # tepe → dip → kapanış. Pozisyonun ters tarafı önce görüldüyse önce stop, sonra hedef / başabaş / izleyen
        # stop bakılır; tepeden kapanışa inerken yeni stop geçildiyse o mumda stoptan çıkılır.
        cj = c[j]
        adverse_first = cj >= oj if side > 0 else cj <= oj
        if adverse_first and worse(aj, stop):
            fill = (stop + aj) / 2 if p.harsh_stops else stop
            exit_price, reason, exit_ms = fill * (1 - side * sslip), kind, tj
            break
        if reached(fj, tp):
            exit_price = tp if p.tp_limit else tp * (1 - side * slip)
            reason, exit_ms = "TP", tj
            break
        was_on = be_on
        favorable(fj)
        after = cj if adverse_first else aj                 # Tepeden sonra görülen en kötü fiyat
        if worse(after, stop):
            fill = (stop + after) / 2 if p.harsh_stops and not adverse_first else stop
            exit_price, reason, exit_ms = fill * (1 - side * sslip), kind, tj
            break
        if p.be_intrabar and be_on and not was_on and kind == "BE" and adverse_first and worse(aj, stop):
            # Başabaş bu mumda tetiklendi, mumun ters ucu yeni stopun gerisinde ama kapanış ilerisinde. Standart sıra
            # (dip → tepe → kapanış) "çıkılmadı" der; gerçekte fiyat tetiğe değdikten sonra tabana dönmüş olabilir
            # (dar aralıkta sık). Olasılık be_retouch_prob ile, çekiliş işlem başına sabit (coin + giriş zamanı).
            be_bar = (float(oj), float(fj), float(aj), float(cj))
            be_q = be_retouch_prob(side, fj, aj, cj, be_trig, stop)
            hit = p.be_force if p.be_force is not None else trade_uniform(s.symbol, s.close_ms, p.be_seed) < be_q
            if hit:
                exit_price, reason, exit_ms = stop * (1 - side * sslip), kind, tj
                break
    if exit_price is None:                                  # Veri penceresi bitti (son mumun kapanışı)
        last = n - 1
        exit_price = (c[last] if n else entry) * (1 - side * slip)
        exit_ms = int(t[last]) + bar_ms if n else s.close_ms
        reason = "END" if not n or int(t[last]) + bar_ms < deadline else "TIME"
    upto = np.searchsorted(t, deadline, "left")
    window_mae = (float(np.max(side * (entry - adverse[start:upto]) / entry * 100)) if upto > start else 0.0)
    qty = notional / entry
    fee_in = notional * (p.maker_fee if maker else p.taker_fee)
    fee_out = qty * exit_price * (p.maker_fee if reason == "TP" and p.tp_limit else p.taker_fee)
    funding = _funding_cost(data, s.symbol, side, notional, entry_ms, exit_ms) if data is not None else 0.0
    if liquidated:
        pnl = -margin - fee_in
        fees = fee_in
        funding = 0.0
    else:
        pnl = side * qty * (exit_price - entry) - fee_in - fee_out - funding
        fees = fee_in + fee_out
    return FutTrade(s.symbol, s.side, entry_ms, exit_ms, entry, float(exit_price), reason, p.leverage, margin,
                    notional, fees, funding, pnl, pnl / margin * 100 if margin else 0.0, 0.0, worst,
                    max(window_mae, worst), gap, liquidated, bar_ms, s.score, float(s.metrics.get("tactic", 0.0)),
                    int(getattr(s, "breadth", 0)), be_bar=be_bar, be_q=be_q)


def depth_cap_notional(p: SimParams, data: "FutData", s: "FutSignal") -> float:
    """Derinliğe göre boy: girişte izin verilen en büyük pozisyon tutarı (kapalıysa / veri yoksa sonsuz)."""
    if p.depth_cap_pct <= 0 or data.depth is None:
        return math.inf
    return p.depth_cap_pct / 100 * data.depth.cap_depth(s.symbol, s.close_ms)


def apply_depth(p: SimParams, data: "FutData", s: "FutSignal", notional: float) -> Tuple[float, bool]:
    """(tutar, sınıra takıldı mı). Canlıyla aynı: pozisyon DEPTH_CHECK_MIN_NOTIONAL'ın altındaysa defter okunmaz."""
    if p.depth_cap_pct <= 0 or data.depth is None or notional < DEPTH_CHECK_MIN_NOTIONAL:
        return notional, False
    cap = depth_cap_notional(p, data, s)
    return (cap, True) if cap < notional else (notional, False)


def charge_impact(p: SimParams, data: "FutData", s: "FutSignal", tr: "FutTrade") -> None:
    """Ölçülen (ya da tahmin edilen) emir defteri kayması: giriş ve çıkış kayması işlemin kârından düşülür; yalıtılmış
    teminatta zarar teminatı aşamaz (likidasyon)."""
    if not p.depth_impact or data.depth is None or tr.notional <= 0:
        return
    entry_side, exit_side = ("ask", "bid") if tr.side == "long" else ("bid", "ask")
    imp = (data.depth.impact(tr.symbol, s.close_ms, entry_side, tr.notional)
           + data.depth.impact(tr.symbol, tr.exit_ms, exit_side, tr.notional))
    if imp <= 0:
        return
    cost = tr.notional * imp / 100
    pnl = max(tr.pnl - cost, -tr.margin) if tr.margin > 0 else tr.pnl - cost
    tr.fees += tr.pnl - pnl
    tr.pnl = pnl
    if tr.margin > 0:
        tr.roe_pct = pnl / tr.margin * 100
    tr.impact_pct = imp


def capacity_order(signals: Sequence["FutSignal"], p: SimParams, data: "FutData",
                   want: Callable[[int], float]) -> Iterable["FutSignal"]:
    """Kapasite önceliği (derinliğe göre boy açıkken): aynı mumdaki sinyallerden sıradaki, pozisyon derinlik sınırına
    takılıyorsa kapasitesi (sınır ile istenen tutarın küçüğü) en büyük olandır; takılmıyorsa eski sıra (puan) korunur.
    `want(mum ms)` sıradaki pozisyonun istenen tutarı (o andaki bakiyeyle; canlı: FuturesBot._next_signal)."""
    if p.depth_cap_pct <= 0 or data.depth is None:
        yield from signals
        return
    i, n = 0, len(signals)
    while i < n:
        j = i
        while j < n and signals[j].close_ms == signals[i].close_ms:
            j += 1
        group = list(signals[i:j])
        while group:
            pick = 0
            if len(group) > 1:
                need = want(group[0].close_ms)
                if need >= DEPTH_CHECK_MIN_NOTIONAL and depth_cap_notional(p, data, group[0]) < need:
                    caps = [min(depth_cap_notional(p, data, x), need) for x in group]
                    pick = int(np.argmax(caps))
            yield group.pop(pick)
        i = j


def signal_quality(s: "FutSignal") -> float:
    """Çöküş sinyalinin kalite puanı (strategy.quality_score); ek modül sinyallerinde NaN."""
    return quality_score(s.score, s.metrics) if s.metrics.get("tactic", 1.0) == 1.0 else float("nan")


def size_factor(p: SimParams, s: "FutSignal") -> float:
    """Sinyalin boy çarpanı (canlı: FuturesBot._entry_margin ile aynı): yalnız çöküşte (genişlik < isolated_breadth)
    × isolated_size_pct; çöküş durulmuşsa (son 15 dk düşüş < recent_drop_pct) × recent_size_pct; vadeli spotun
    basis_deep_pct (%) değerinden fazla altındaysa × basis_size_pct; kalite puanı en zayıf quality_cut_pct yüzdesindeyse
    × quality_size_pct."""
    out = (isolated_factor(p.isolated_size_pct, p.isolated_breadth, s.breadth)
           * recent_size_factor(p.recent_size_pct, p.recent_drop_pct, s.metrics.get("drop15"),
                                s.metrics.get("bounce15"), p.bounce_calm_pct)
           * bounce_boost_factor(p.bounce_boost_pct, p.bounce_boost_size_pct, s.metrics.get("bounce15"))
           * basis_size_factor(p.basis_size_pct, p.basis_deep_pct, s.metrics.get("basis")))
    if p.quality_size_pct < 100 and s.metrics.get("tactic", 1.0) == 1.0:
        out *= quality_size_factor(p.quality_size_pct, p.quality_cut_pct, quality_score(s.score, s.metrics))
    return (out * bear_size_factor(p.bear_size_pct, signal_bear(s, p.bear_sma_days))
            * support_size_factor(p.support_size_pct, p.support_zone_pct, s.metrics.get(f"low{int(p.support_days)}h")))


def bounce_flag(p: SimParams, s: "FutSignal") -> int:
    """V dönüşünün bu sinyalde etkisi (rapor sayımı): 2 = büyük boy (FUTURES_BOUNCE_BOOST_PCT), 1 = durulmuş küçültmesi
    kalktı (FUTURES_BOUNCE_CALM_PCT), 0 = yok."""
    b = s.metrics.get("bounce15")
    if b is None or b != b:
        return 0
    if bounce_boost_factor(p.bounce_boost_pct, p.bounce_boost_size_pct, b) != 1:
        return 2
    d = s.metrics.get("drop15")
    if (p.bounce_calm_pct > 0 and p.recent_size_pct < 100 and d is not None and d == d and d < p.recent_drop_pct
            and b >= p.bounce_calm_pct):
        return 1
    return 0


def signal_bear(s: "FutSignal", days: int) -> Optional[bool]:
    """Sinyal anında ayı piyasası mı (attach_regime'in yazdığı metrics["bearN"]: 1 / 0; ölçü yoksa None)."""
    v = s.metrics.get(f"bear{int(days)}")
    return None if v is None or v != v else bool(v)


def slot_share(p: SimParams, n_open: int) -> float:
    """Yeni pozisyonun teminatı, kullanılabilir bakiyenin kaçta kaçı (config.futures_slot_share ile aynı kural): tek
    pozisyonda 1; dalga modunda ilk pozisyon 1, ek pozisyonlar x = extra_size_pct / 100 ağırlıkta, toplam 1 + (N-1)·x."""
    slots = max(1, int(p.max_positions))
    if slots <= 1:
        return 1.0
    x = max(0.0, min(1.0, p.extra_size_pct / 100))
    return (1.0 if n_open <= 0 else x) / (1 + (slots - 1) * x)


DAY_BRAKE_MS = DAY_BRAKE_HOURS * 3600 * 1000


def brake_factor(p: SimParams, curve: Sequence[Tuple[int, float]], now_ms: int, equity: float) -> float:
    """24 saat freni çarpanı (canlı: FuturesBot._brake_factor): gerçekleşen bakiye son 24 saatin zirvesinin
    (config.window_peak; curve = kapanışlardaki bakiye) day_brake_pct altındaysa day_brake_size_pct / 100."""
    if p.day_brake_pct <= 0:
        return 1.0
    return dd_scale_factor(p.day_brake_pct, p.day_brake_size_pct, equity,
                           window_peak(curve, now_ms, DAY_BRAKE_MS, equity))


def guard_factor(p: SimParams, equity: float) -> float:
    """Sermaye koruması × kâr yastığı çarpanı (canlı: FuturesBot._guard_factor; config.futures_guard_factor /
    futures_cushion_factor): gerçekleşen bakiye başlangıç sermayesinin guard_pct altındaysa guard_size_pct / 100,
    cushion_x katı ya da üstündeyse cushion_size_pct / 100."""
    return (dd_scale_factor(p.guard_pct, p.guard_size_pct, equity, p.start_capital)
            * cushion_factor(p.cushion_x, p.cushion_size_pct, equity, p.start_capital))


def cooldown_block(p: SimParams, s: FutSignal, cooldown: Dict[str, int], reentry_at: Dict[str, int]) -> Optional[str]:
    """Sinyalin coini beklemede mi: atlama nedeni ya da None. Faz 6 (reentry_breadth > 0): zarar sonrası beklemedeki
    coin, genişlik ≥ reentry_breadth iken (ve SYMBOL_COOLDOWN bittiyse) yeniden alınabilir (canlı: FuturesBot)."""
    if s.close_ms >= cooldown.get(s.symbol, -1):
        return None
    at = reentry_at.get(s.symbol)
    if at is not None and p.reentry_breadth > 0 and s.close_ms >= at and s.breadth >= p.reentry_breadth:
        return None
    if at is not None and p.reentry_breadth > 0:
        return f"coin beklemede (zarar sonrası; dalga yok, genişlik < {p.reentry_breadth})"
    return "coin beklemede (önceki işlemden sonra)"


def set_cooldown(p: SimParams, tr: FutTrade, free_at: int, cooldown: Dict[str, int], reentry_at: Dict[str, int]) -> None:
    """Kapanan işlemden sonra coinin beklemesi (canlıdaki risk.cooldown_minutes ile aynı: zararla kapandıysa
    LOSS_COOLDOWN, daha uzunsa). Faz 6: zarar sonrası beklemenin dalgada atlanabileceği an (SYMBOL_COOLDOWN sonrası);
    dalgada yeniden girilen işlem kârla kapanırsa önceki zarar sonrası bekleme sonuna kadar sürer (o süre içinde coine
    yine yalnızca dalgada girilir; canlı: FuturesBot._mark_reentry)."""
    sym = tr.symbol
    loss = tr.pnl < 0 and p.loss_cooldown_min > 0
    wait = max(p.symbol_cooldown_min, p.loss_cooldown_min) if loss else p.symbol_cooldown_min
    prev_until, prev_at = cooldown.get(sym, -1), reentry_at.get(sym)
    if wait > 0:
        cooldown[sym] = tr.exit_ms + tr.bar_ms + wait * MIN_MS
    else:
        cooldown.pop(sym, None)
    if p.reentry_breadth <= 0:
        reentry_at.pop(sym, None)
    elif loss and p.loss_cooldown_min > p.symbol_cooldown_min:
        reentry_at[sym] = free_at + p.symbol_cooldown_min * MIN_MS
    elif prev_at is not None and prev_until > tr.exit_ms:        # Önceki zararın beklemesi sürüyor
        cooldown[sym] = max(prev_until, cooldown.get(sym, -1))
        reentry_at[sym] = max(prev_at, free_at + p.symbol_cooldown_min * MIN_MS)
    else:
        reentry_at.pop(sym, None)


def simulate(p: SimParams, data: FutData, signals: Sequence[FutSignal], windows: Dict[Tuple[str, int], tuple],
             kill_switch: bool = False, start_ms: Optional[int] = None, end_ms: Optional[int] = None,
             fixed_notional: Optional[float] = None) -> FutResult:
    """Sinyalleri zaman sırasıyla işler: aynı anda tek pozisyon, tüm bakiye (bileşik). start_ms / end_ms verilirse
    yalnızca o aralıktaki sinyaller (yıl yıl yeniden başlama için). fixed_notional verilirse her işlem bu tutarla
    açılır (bakiye ne olursa olsun; erime / minimum emir yok): stratejinin işlem başı gücünü ölçmek için.
    Dalga modunda (p.max_positions > 1) simulate_slots."""
    start_ms = data.start_ms if start_ms is None else start_ms
    end_ms = data.end_ms if end_ms is None else end_ms
    if p.max_positions > 1:
        return simulate_slots(p, data, signals, windows, kill_switch, start_ms, end_ms, fixed_notional)
    signals = [s for s in signals if start_ms <= s.close_ms < end_ms]
    equity = p.start_capital
    curve: List[Tuple[int, float]] = [(start_ms, equity)]
    trades: List[FutTrade] = []
    skipped: Counter = Counter()
    busy_until = -1
    last_entry = -1
    cooldown: Dict[str, int] = {}
    reentry_at: Dict[str, int] = {}
    freeze_until = -1
    sl_streak = 0
    day_key, day_pnl = -1, 0.0
    ruined = kill_ms = None
    usage = p.margin_usage_pct / 100
    kill_level = p.start_capital * (1 - p.kill_pct / 100) if p.kill_pct else -1.0
    peak = p.start_capital                  # Düşüşte küçültme: gerçekleşen bakiyenin zirvesi

    def want(_t: int) -> float:             # Kapasite önceliği: sıradaki pozisyonun istenen tutarı
        return min(equity * usage * p.leverage * dd_scale_factor(p.dd_scale_pct, p.dd_scale_size_pct, equity, peak),
                   p.max_notional)
    for s in capacity_order(signals, p, data, want):
        if s.close_ms < busy_until:
            skipped["açık pozisyon vardı"] += 1
            continue
        if s.close_ms == last_entry:
            skipped["aynı mumda daha güçlü sinyal alındı"] += 1
            continue
        why = cooldown_block(p, s, cooldown, reentry_at)
        if why:
            skipped[why] += 1
            continue
        reentry = s.close_ms < cooldown.get(s.symbol, -1)      # Faz 6: dalgada zarar sonrası beklemesi atlandı
        if s.close_ms < freeze_until:
            skipped["risk sigortası (zarar serisi / günlük limit)"] += 1
            continue
        factor = size_factor(p, s)          # Yalnız çöküş / çöküş durulmuş: küçük
        ddf = 1.0 if fixed_notional else dd_scale_factor(p.dd_scale_pct, p.dd_scale_size_pct, equity, peak)
        bf = 1.0 if fixed_notional else brake_factor(p, curve, s.close_ms, equity)
        gf = 1.0 if fixed_notional else guard_factor(p, equity)
        capped = False
        if fixed_notional:
            notional = fixed_notional * factor
            margin = notional / p.leverage
        else:
            # Tek pozisyonda büyütülecek boş teminat yok (kâr yastığı da kullanımı aşamaz; canlı: _entry_margin)
            margin = equity * usage * min(min(factor, 1.0) * bf * gf, 1.0) * ddf
            notional = min(margin * p.leverage, p.max_notional)
            notional, capped = apply_depth(p, data, s, notional)
            margin = notional / p.leverage
        if not fixed_notional and notional < min_notional(s.symbol):
            skipped["minimum emir tutarının altında"] += 1
            if equity * usage * p.leverage * ddf < DEFAULT_MIN_NOTIONAL:
                ruined = s.close_ms
                break
            continue
        window = windows.get((s.symbol, s.close_ms))
        if window is None or not len(window[1]):
            skipped["çıkış verisi yok"] += 1
            continue
        hold = hold_minutes(p, 0, s.breadth)            # Faz 9: dalgada açılan pozisyon daha uzun tutulur
        tr = run_trade(p if hold == p.hold_min else replace(p, hold_min=hold), s, window, margin, notional, data)
        if tr is None:                                  # Limit emir süresinde dolmadı: iptal, işlem yok
            skipped[f"limit emir {p.limit_ttl_min} dk içinde dolmadı"] += 1
            busy_until = s.close_ms + p.limit_ttl_min * MIN_MS
            last_entry = s.close_ms
            continue
        charge_impact(p, data, s, tr)
        tr.depth_capped = capped
        equity = equity + tr.pnl if fixed_notional else max(0.0, equity + tr.pnl)
        tr.equity_after = equity
        tr.reentry = reentry
        tr.wave_rule = bool(s.metrics.get("wave_rule"))
        tr.drop15 = float(s.metrics.get("drop15", float("nan")))
        tr.basis = float(s.metrics.get("basis", float("nan")))
        tr.quality = quality_score(s.score, s.metrics) if s.metrics.get("tactic", 1.0) == 1.0 else float("nan")
        tr.dd_scaled = ddf < 1
        tr.braked = bf < 1
        tr.guarded = gf < 1
        tr.cushioned = gf > 1
        tr.bear = p.bear_size_pct < 100 and bool(signal_bear(s, p.bear_sma_days))
        tr.support = support_size_factor(p.support_size_pct, p.support_zone_pct,
                                         s.metrics.get(f"low{int(p.support_days)}h")) < 1
        tr.bounced = bounce_flag(p, s)
        peak = max(peak, equity)
        trades.append(tr)
        curve.append((tr.exit_ms, equity))
        # Mum içinde kapanan işlem o mum bitene kadar "açık" sayılır (canlıda çıkış dakikanın içinde olur); zaman
        # stopu mum açılışında kapanır, aynı 5 dk kapanışındaki yeni sinyal alınabilir (canlı: önce çıkış, sonra tarama)
        busy_until = tr.exit_ms if tr.reason == "TIME" else tr.exit_ms + tr.bar_ms
        last_entry = s.close_ms
        set_cooldown(p, tr, busy_until, cooldown, reentry_at)     # canlıdaki risk.cooldown_minutes ile aynı
        # Canlıdaki risk sigortaları (trader._after_close): günlük zarar / kâr limiti ve art arda zarar kes molası
        day = tr.exit_ms // DAY_MS
        if day != day_key:
            day_key, day_pnl = day, 0.0
        day_pnl += tr.pnl
        midnight = (day + 1) * DAY_MS
        if p.daily_loss_pct > 0 and day_pnl <= -p.start_capital * p.daily_loss_pct / 100:
            freeze_until = max(freeze_until, midnight)
        if p.daily_profit_pct > 0 and day_pnl >= p.start_capital * p.daily_profit_pct / 100:
            freeze_until = max(freeze_until, midnight)
        sl_streak = sl_streak + 1 if tr.reason == "SL" else 0
        if p.loss_streak_limit > 0 and sl_streak >= p.loss_streak_limit:
            freeze_until = max(freeze_until, tr.exit_ms + int(p.loss_streak_pause_h * FUT_HOUR_MS))
            sl_streak = 0
        if fixed_notional:
            continue
        if kill_ms is None and equity <= kill_level:
            kill_ms = tr.exit_ms
            if kill_switch:
                break
        # Erime: en küçük emir bile açılamıyor (düşüşte küçültme açıksa küçültülmüş boyla; bakiye artık büyüyemez)
        if equity * usage * p.leverage * dd_scale_factor(p.dd_scale_pct, p.dd_scale_size_pct, equity, peak) \
                < DEFAULT_MIN_NOTIONAL:
            ruined = tr.exit_ms
            break
    return FutResult(p, trades, curve, skipped, start_ms, end_ms, ruined, kill_ms)


def simulate_slots(p: SimParams, data: FutData, signals: Sequence[FutSignal], windows, kill_switch: bool,
                   start_ms: int, end_ms: int, fixed_notional: Optional[float] = None) -> FutResult:
    """Dalga modu: aynı anda en fazla p.max_positions pozisyon (farklı coinler). Her pozisyonun teminatı gerçekleşmiş
    bakiyenin usage / N'i (Faz 6, extra_size_pct < 100: ilk pozisyon 1, ek pozisyonlar x ağırlıkta, bkz. slot_share;
    yalnız çöküşte × isolated_size_pct); açık pozisyon varken yenisi yalnızca genişlik ≥ p.extra_breadth ise açılır
    (canlı: FuturesBot.scan_and_enter). Kapanan pozisyonun kârı / zararı çıkış sırasıyla bakiyeye yazılır; risk
    sigortaları (bekleme, günlük limit, zarar serisi, kill-switch) tek pozisyondaki gibi. Limit girişte bir mumda tek
    emir denenir (canlıda aynı anda tek bekleyen giriş emri)."""
    signals = [s for s in signals if start_ms <= s.close_ms < end_ms]
    slots = max(1, int(p.max_positions))
    equity = p.start_capital
    curve: List[Tuple[int, float]] = [(start_ms, equity)]
    trades: List[FutTrade] = []
    skipped: Counter = Counter()
    heap: List[tuple] = []                  # (boşalma ms, sıra, işlem, teminat)
    open_syms: Dict[str, int] = {}
    used = 0.0
    seq = 0
    limit_bar = -1
    cooldown: Dict[str, int] = {}
    reentry_at: Dict[str, int] = {}
    freeze_until = -1
    sl_streak = 0
    day_key, day_pnl = -1, 0.0
    ruined = kill_ms = None
    stop = False
    usage = p.margin_usage_pct / 100
    kill_level = p.start_capital * (1 - p.kill_pct / 100) if p.kill_pct else -1.0
    first_share = slot_share(p, 0)          # Faz 6: ilk pozisyon büyük, ek pozisyonlar küçük (100% = eşit, 1/N)
    peak = p.start_capital                  # Düşüşte küçültme: gerçekleşen bakiyenin zirvesi
    # Faz 8: ek pozisyonlar (başka pozisyon açıkken açılanlar) kendi başabaş tetiğiyle (config.futures_be_trigger).
    # Tetik stopun taşınacağı yerin (be_floor_pct) üstünde olmalı: duyarlılık satırı tabanı yükseltirse (ör. "Başabaş
    # +%5 → +%2") ek tetik (+%0.5) geçersiz kalır ve ana tetik kullanılır (yoksa stop fiyatın üstüne konmuş sayılır).
    p_extra = (replace(p, be_trigger_pct=p.extra_be_trigger_pct) if phase8_active(p) else p)
    hold_cache: Dict[tuple, SimParams] = {}

    def realize(until: int) -> None:
        nonlocal equity, used, freeze_until, sl_streak, day_key, day_pnl, kill_ms, ruined, stop, peak
        while heap and heap[0][0] <= until and not stop:
            _free, _q, tr, margin = heapq.heappop(heap)
            used -= margin
            open_syms.pop(tr.symbol, None)
            equity = equity + tr.pnl if fixed_notional else max(0.0, equity + tr.pnl)
            tr.equity_after = equity
            peak = max(peak, equity)
            curve.append((tr.exit_ms, equity))
            day = tr.exit_ms // DAY_MS
            if day != day_key:
                day_key, day_pnl = day, 0.0
            day_pnl += tr.pnl
            midnight = (day + 1) * DAY_MS
            if p.daily_loss_pct > 0 and day_pnl <= -p.start_capital * p.daily_loss_pct / 100:
                freeze_until = max(freeze_until, midnight)
            if p.daily_profit_pct > 0 and day_pnl >= p.start_capital * p.daily_profit_pct / 100:
                freeze_until = max(freeze_until, midnight)
            sl_streak = sl_streak + 1 if tr.reason == "SL" else 0
            if p.loss_streak_limit > 0 and sl_streak >= p.loss_streak_limit:
                freeze_until = max(freeze_until, tr.exit_ms + int(p.loss_streak_pause_h * FUT_HOUR_MS))
                sl_streak = 0
            if fixed_notional:
                continue
            if kill_ms is None and equity <= kill_level:
                kill_ms = tr.exit_ms
                if kill_switch:
                    stop = True
            if not heap and equity * usage * p.leverage * first_share * dd_scale_factor(
                    p.dd_scale_pct, p.dd_scale_size_pct, equity, peak) < DEFAULT_MIN_NOTIONAL:
                ruined, stop = tr.exit_ms, True

    def want(t: int) -> float:              # Kapasite önceliği: sıradaki pozisyonun istenen tutarı (o andaki bakiyeyle)
        realize(t)
        ddf_now = dd_scale_factor(p.dd_scale_pct, p.dd_scale_size_pct, equity, peak)
        return min(equity * usage * p.leverage * slot_share(p, len(heap)) * ddf_now, p.max_notional)

    for s in capacity_order(signals, p, data, want):
        realize(s.close_ms)
        if stop:
            break
        if s.close_ms == limit_bar:
            skipped["aynı mumda limit emir denendi"] += 1
            continue
        big_on = slots > 1 and big_rules_on(p.big_account_usdt, equity)
        if len(heap) >= slots and not (big_on and len(heap) < slots + BIG_EXTRA_SLOTS
                                       and big_extra_slots(p.big_account_usdt, equity, signal_quality(s)) > 0):
            skipped["açık pozisyon vardı"] += 1
            continue
        if s.symbol in open_syms:
            skipped["bu coinde açık pozisyon var"] += 1
            continue
        if heap and s.breadth < p.extra_breadth:
            skipped[f"ek pozisyon: dalga yok (genişlik < {p.extra_breadth})"] += 1
            continue
        why = cooldown_block(p, s, cooldown, reentry_at)
        if why:
            skipped[why] += 1
            continue
        reentry = s.close_ms < cooldown.get(s.symbol, -1)      # Faz 6: dalgada zarar sonrası beklemesi atlandı
        if s.close_ms < freeze_until:
            skipped["risk sigortası (zarar serisi / günlük limit)"] += 1
            continue
        if big_on and big_funding_skip(p.big_account_usdt, equity, signal_quality(s), s.metrics.get("last_funding")):
            skipped["büyük hesap: zayıf sinyal, son fonlama ≤ 0"] += 1
            continue
        factor = size_factor(p, s)
        bf = 1.0 if fixed_notional else brake_factor(p, curve, s.close_ms, equity)
        factor *= bf                        # 24 saat freni (canlıda da diğer boy çarpanlarıyla birlikte)
        gf = 1.0 if fixed_notional else guard_factor(p, equity)
        factor *= gf                        # Sermaye koruması (başlangıç sermayesine göre zarar)
        share = slot_share(p, len(heap))
        ddf = 1.0 if fixed_notional else dd_scale_factor(p.dd_scale_pct, p.dd_scale_size_pct, equity, peak)
        capped = False
        rounded = False
        if fixed_notional:                  # fixed_notional = toplam / N (evaluate): eşit büyüklükte değişmez
            notional = fixed_notional * slots * share * factor
            margin = notional / p.leverage
        else:
            margin = min(equity * usage * share * factor, max(0.0, equity * usage - used)) * ddf
            notional = min(margin * p.leverage, p.max_notional)
            notional, capped = apply_depth(p, data, s, notional)
            margin = notional / p.leverage
            rounded = False
            if (notional < min_notional(s.symbol) and quality_roundup_ok(p.quality_roundup, signal_quality(s))
                    and (equity * usage - used) * p.leverage >= min_notional(s.symbol)):
                notional = min_notional(s.symbol)               # Güçlü sinyal: en küçük tutarla açılır
                margin, rounded = notional / p.leverage, True
            if notional < min_notional(s.symbol):
                skipped["minimum emir tutarının altında"] += 1
                if not heap and equity * usage * p.leverage * first_share * ddf < DEFAULT_MIN_NOTIONAL:
                    ruined = s.close_ms
                    break
                continue
        window = windows.get((s.symbol, s.close_ms))
        if window is None or not len(window[1]):
            skipped["çıkış verisi yok"] += 1
            continue
        pp = p_extra if heap else p
        hold = hold_minutes(p, len(heap), s.breadth)    # Faz 9: ek pozisyon / dalgada ilk pozisyon kendi zaman stopuyla
        if hold != pp.hold_min:
            key = (bool(heap), hold)
            if key not in hold_cache:
                hold_cache[key] = replace(pp, hold_min=hold)
            pp = hold_cache[key]
        tr = run_trade(pp, s, window, margin, notional, data)
        if p.entry_limit:
            limit_bar = s.close_ms
        if tr is None:
            skipped[f"limit emir {p.limit_ttl_min} dk içinde dolmadı"] += 1
            continue
        charge_impact(p, data, s, tr)
        tr.depth_capped = capped
        tr.slot = len(heap) + 1
        tr.reentry = reentry
        tr.wave_rule = bool(s.metrics.get("wave_rule"))
        tr.drop15 = float(s.metrics.get("drop15", float("nan")))
        tr.basis = float(s.metrics.get("basis", float("nan")))
        tr.quality = quality_score(s.score, s.metrics) if s.metrics.get("tactic", 1.0) == 1.0 else float("nan")
        tr.dd_scaled = ddf < 1
        tr.braked = bf < 1
        tr.guarded = gf < 1
        tr.cushioned = gf > 1
        tr.bear = p.bear_size_pct < 100 and bool(signal_bear(s, p.bear_sma_days))
        tr.support = support_size_factor(p.support_size_pct, p.support_zone_pct,
                                         s.metrics.get(f"low{int(p.support_days)}h")) < 1
        tr.bounced = bounce_flag(p, s)
        tr.rounded = not fixed_notional and rounded
        trades.append(tr)
        free_at = tr.exit_ms if tr.reason == "TIME" else tr.exit_ms + tr.bar_ms
        seq += 1
        heapq.heappush(heap, (free_at, seq, tr, margin))
        open_syms[s.symbol] = free_at
        used += margin
        set_cooldown(p, tr, free_at, cooldown, reentry_at)
    if not stop:
        realize(2 ** 62)
    curve.sort(key=lambda x: x[0])
    return FutResult(p, trades, curve, skipped, start_ms, end_ms, ruined, kill_ms)


# ===================================================================== istatistik
def max_drawdown(curve: Sequence[Tuple[int, float]]) -> Tuple[float, Optional[int], Optional[int]]:
    """Gerçekleşen sermaye eğrisinde en büyük düşüş (%), zirve ve dip zamanı."""
    peak, peak_t, dd, at = -1.0, None, 0.0, (None, None)
    for t, e in curve:
        if e > peak:
            peak, peak_t = e, t
        elif peak > 0 and (peak - e) / peak * 100 > dd:
            dd, at = (peak - e) / peak * 100, (peak_t, t)
    return dd, at[0], at[1]


def streaks(trades: Sequence[FutTrade]) -> Tuple[int, int, float]:
    """(en uzun kayıp serisi, en uzun kazanç serisi, en uzun kayıp serisinde bakiye kaybı %)."""
    best_loss = best_win = cur_loss = cur_win = 0
    worst_drop, start_eq = 0.0, 0.0
    for tr in trades:
        if tr.pnl <= 0:
            if cur_loss == 0:
                start_eq = tr.equity_after - tr.pnl
            cur_loss += 1
            cur_win = 0
            drop = (start_eq - tr.equity_after) / start_eq * 100 if start_eq > 0 else 0.0
            if cur_loss > best_loss or (cur_loss == best_loss and drop > worst_drop):
                best_loss, worst_drop = cur_loss, drop
        else:
            cur_win += 1
            cur_loss = 0
            best_win = max(best_win, cur_win)
    return best_loss, best_win, worst_drop


def net_move_pct(tr: FutTrade) -> float:
    """İşlemin kaldıraçsız net sonucu: fiyat hareketi - komisyon - kayma - fonlama, pozisyon tutarına göre (%)."""
    return tr.pnl / tr.notional * 100 if tr.notional else 0.0


@dataclass
class Stats:
    label: str
    trades: int
    wins: int
    win_rate: float
    net_pct: float
    max_dd: float
    loss_streak: int
    avg_roe: float
    avg_move: float
    profit_factor: float
    liquidations: int
    gaps: int
    start_eq: float
    end_eq: float
    ruined: bool = False


def stats_of(res: FutResult, label: str = "") -> Stats:
    trades = res.trades
    wins = sum(1 for tr in trades if tr.pnl > 0)
    gain = sum(tr.pnl for tr in trades if tr.pnl > 0)
    loss = -sum(tr.pnl for tr in trades if tr.pnl <= 0)
    start = res.params.start_capital
    return Stats(label, len(trades), wins, wins / len(trades) * 100 if trades else 0.0, res.net_pct,
                 max_drawdown(res.curve)[0], streaks(trades)[0],
                 float(np.mean([tr.roe_pct for tr in trades])) if trades else 0.0,
                 float(np.mean([net_move_pct(tr) for tr in trades])) if trades else 0.0,
                 gain / loss if loss > 0 else float("inf") if gain > 0 else 0.0,
                 sum(tr.liquidated for tr in trades), sum(tr.gap for tr in trades), start, res.final_equity,
                 res.ruined_ms is not None)


def year_ranges(data: FutData) -> List[Tuple[int, int, int]]:
    out = []
    y = _year(data.start_ms)
    while True:
        a = max(_ms(f"{y}-01-01"), data.start_ms)
        b = min(_ms(f"{y + 1}-01-01"), data.end_ms)
        if a >= data.end_ms:
            break
        out.append((y, a, b))
        y += 1
    return out


@dataclass
class Evaluation:
    """Bir ayar setinin üç senaryosu:
    A) chained: tek hesap, başlangıç sermayesiyle baştan sona (bileşik, gerçekçi; eriyince durur)
    B) years: her yıl başlangıç sermayesiyle yeniden (yıllara göre getiri / düşüş)
    C) fixed: her sinyal sabit tutarla (başlangıç teminatı × kaldıraç), bakiye etkisi yok: stratejinin işlem başı
       gücü tüm yıllarda kesintisiz ölçülür (kazanma oranı, işlem başı sonuç, çıkış nedenleri)."""
    chained: FutResult
    years: List[Tuple[int, FutResult]]
    fixed: FutResult

    @property
    def trades(self) -> List[FutTrade]:
        return self.fixed.trades

    @property
    def profitable_years(self) -> int:
        return sum(1 for _y, r in self.years if r.trades and r.net_pct > 0)

    @property
    def avg_move(self) -> float:
        return float(np.mean([net_move_pct(tr) for tr in self.trades])) if self.trades else 0.0

    @property
    def win_rate(self) -> float:
        return sum(tr.pnl > 0 for tr in self.trades) / len(self.trades) * 100 if self.trades else 0.0

    def fixed_years(self) -> List[Tuple[int, List[FutTrade]]]:
        out: Dict[int, List[FutTrade]] = {}
        for tr in self.trades:
            out.setdefault(_year(tr.entry_ms), []).append(tr)
        return sorted(out.items())


def evaluate(p: SimParams, data: FutData, signals: Sequence[FutSignal], windows) -> Evaluation:
    chained = simulate(p, data, signals, windows)
    years = [(y, simulate(p, data, signals, windows, start_ms=a, end_ms=b)) for y, a, b in year_ranges(data)]
    fixed = simulate(p, data, signals, windows,
                     fixed_notional=p.start_capital * p.margin_usage_pct / 100 * p.leverage / max(1, p.max_positions))
    return Evaluation(chained, years, fixed)


DRAW_SEEDS = 10     # Çekiliş duyarlılığı: raporda aynı ayarın denendiği farklı çekiliş sayısı


def draw_spread(p: SimParams, data: FutData, signals: Sequence[FutSignal], windows,
                seeds: int = DRAW_SEEDS) -> List[float]:
    """Mum içi başabaş çekilişine duyarlılık: aynı ayarın tek hesap son bakiyesi `seeds` farklı çekilişle. Başabaşın
    tetiklendiği dakikada fiyatın tabana dönüp dönmediği bir olasılıkla çekilir (be_retouch_prob); kaldıraçlı ve
    bileşik hesapta birkaç işlemin çekilişi son bakiyeyi katlarca oynatır. İki ayarı tek rapordaki son bakiyeyle
    karşılaştırmak bu yüzden yanıltabilir: fark bu aralığın içindeyse anlamlı sayılmaz. Pay kapalıysa boş liste."""
    if seeds <= 0 or not (p.be_enabled and p.be_intrabar) or p.be_force is not None:
        return []
    return [simulate(replace(p, be_seed=seed), data, signals, windows).final_equity for seed in range(1, seeds + 1)]


def btc_hold_pct(data: FutData, start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> Optional[float]:
    t, c = data.btc_close
    if start_ms is not None:
        i, j = np.searchsorted(t, start_ms), np.searchsorted(t, end_ms or data.end_ms) - 1
        if j <= i:
            return None
        return (float(c[j]) / float(c[i]) - 1) * 100
    return (float(c[-1]) / float(c[0]) - 1) * 100 if len(c) > 1 else None


# ===================================================================== duyarlılık
class _RowProgress:
    """Uzun duyarlılık hesaplarında ilerleme satırı ("⏳ ...: k / toplam"); Telegram backtest durumunda görünür, son
    rapordan atılır. Her `every` hesapta ve sonda bir satır."""

    def __init__(self, label: str, total: int, progress: Optional[Callable[[str], None]], every: int = 10):
        self.label, self.total, self.progress, self.every, self.done = label, total, progress, every, 0

    def step(self) -> None:
        self.done += 1
        if self.progress and self.total and (self.done % self.every == 0 or self.done == self.total):
            self.progress(f"⏳ {self.label}: {self.done} / {self.total} ayar hesaplandı")

    def wrap(self, fn: Callable) -> Callable:
        def run(*a, **k):
            out = fn(*a, **k)
            self.step()
            return out
        return run


class LevRow(NamedTuple):
    """Kaldıraç tablosunun bir satırı: kaldıraç, değerlendirme (hesaplanmadıysa None), not, her ayın 1'inde başlayan
    hesapların özeti (rolling_summary)."""
    lev: int
    ev: Optional["Evaluation"]
    note: str
    roll: Optional[dict] = None
    label: str = ""             # Ek satırın adı (ör. "7x + 24 sa freni"); boşsa "Nx"


def rolling_summary(p: SimParams, data: FutData, signals, windows) -> Optional[dict]:
    """Her ayın 1'inde başlayan hesapların ilk RISK_START_MONTHS ayı (kill-switch uygulanır: bot durunca hesap orada
    kalır): ortanca getiri, %60'ı aşan düşüş yaşayan ve kill-switch'in durdurduğu hesap sayısı. Bugün başlayan biri için
    kaldıraç seçiminde asıl ölçü (A tek bir başlangıç tarihidir)."""
    starts = _month_starts(data.start_ms, data.end_ms, RISK_START_MONTHS)
    if not starts:
        return None
    rets, deep, kills = [], 0, 0
    for s0 in starts:
        res = simulate(p, data, signals, windows, True, s0, s0 + RISK_START_MONTHS * 30 * DAY_MS + 5 * DAY_MS)
        rets.append((res.final_equity / p.start_capital - 1) * 100)
        deep += curve_stats(res.curve, p.start_capital, p.kill_pct)["dd"] > 60
        kills += res.kill_ms is not None
    return {"n": len(starts), "median": float(np.median(rets)), "dd60": int(deep), "kills": int(kills)}


def leverage_table(cfg, data: FutData, signals, windows,
                   progress: Optional[Callable[[str], None]] = None) -> List[LevRow]:
    """Kaldıraç duyarlılığı: 2x-10x ve ayardaki kaldıraç, aynı sinyaller ve çıkışlarla; her satırda A / B ve her ayın
    1'inde başlayan hesapların özeti. Zarar kes likidasyonun ötesindeyse (her zarar kes likidasyon olurdu) hesaplanmaz;
    likidasyona LIVE_SL_MARGIN puandan yakınsa hesaplanır ama canlı bot bu kaldıraçla işlem açmaz (satırda not)."""
    levs = sorted(set(LEVERAGE_TABLE) | {int(cfg.futures_leverage)})
    rooms = {lev: futures_liq_distance_pct(lev, cfg.futures_mmr_pct) for lev in levs}
    bar = _RowProgress(f"Kaldıraç {levs[0]}x-{levs[-1]}x", sum(1 for r in rooms.values() if cfg.stop_loss_pct < r),
                       progress, every=3)
    rows: List[LevRow] = []
    for lev, room in rooms.items():
        if cfg.stop_loss_pct >= room:
            rows.append(LevRow(lev, None, f"zarar kes (%{cfg.stop_loss_pct:g}) likidasyonun (%{room:.1f}) ötesinde: her "
                                          f"zarar kes likidasyon olurdu"))
            continue
        note = (f"⛔ canlı bot açmaz: zarar kes likidasyona (%{room:.1f}) {LIVE_SL_MARGIN:g} puandan yakın"
                if cfg.stop_loss_pct >= room - LIVE_SL_MARGIN else "")
        p = params_from_cfg(cfg, leverage=lev)
        rows.append(LevRow(lev, evaluate(p, data, signals, windows), note,
                           rolling_summary(p, data, signals, windows) if data is not None else None))
        bar.step()
    return rows + brake_lev_rows(cfg, data, signals, windows, rooms) + guard_lev_rows(cfg, data, signals, windows)


def brake_lev_rows(cfg, data: FutData, signals, windows, rooms: Dict[int, float]) -> List[LevRow]:
    """24 saat freninin kaldıraçla birlikte etkisi (frenin asıl ölçüsü her ayın 1'inde başlayan hesaplar): fren kapalıysa
    şimdiki kaldıraç ve iki üstü frenle (%10 → %50), açıksa şimdiki ve iki altı frensiz. Dalga modunda (çöküş taktiği)."""
    base = params_from_cfg(cfg)
    if base.max_positions <= 1 or cfg.futures_tactic != "crash":
        return []
    on = base.day_brake_pct > 0
    out: List[LevRow] = []
    for lev in ((base.leverage - 2, base.leverage - 1, base.leverage) if on else
                (base.leverage, base.leverage + 1, base.leverage + 2)):
        room = rooms.get(lev, futures_liq_distance_pct(lev, cfg.futures_mmr_pct)) if lev >= 1 else 0.0
        if lev < 1 or cfg.stop_loss_pct >= room - LIVE_SL_MARGIN:
            continue
        over = {"day_brake_pct": 0.0} if on else {"day_brake_pct": 10.0, "day_brake_size_pct": 50.0}
        p = params_from_cfg(cfg, leverage=lev, **over)
        label = f"{lev}x, fren yok" if on else f"{lev}x + 24 sa fren"
        out.append(LevRow(lev, evaluate(p, data, signals, windows), "", rolling_summary(p, data, signals, windows)
                          if data is not None else None, label))
    return out


def guard_lev_rows(cfg, data: FutData, signals, windows) -> List[LevRow]:
    """Sermaye korumasının asıl ölçüsü (her ayın 1'inde başlayan hesaplarda kill-switch): şimdiki kaldıraçta koruma
    açıksa kapalı, kapalıysa %30 → %60 ile aynı hesap. Dalga modunda (çöküş taktiği)."""
    base = params_from_cfg(cfg)
    if base.max_positions <= 1 or cfg.futures_tactic != "crash":
        return []
    on = base.guard_pct > 0
    p = params_from_cfg(cfg, **({"guard_pct": 0.0} if on else {"guard_pct": 30.0, "guard_size_pct": 60.0}))
    label = f"{base.leverage}x, koruma yok" if on else f"{base.leverage}x + koruma"
    return [LevRow(base.leverage, evaluate(p, data, signals, windows), "",
                   rolling_summary(p, data, signals, windows) if data is not None else None, label)]


def exit_grid(cfg, data: FutData, signals, windows) -> List[Tuple[float, float, Evaluation]]:
    """Kâr Al / Zarar Kes ızgarası. Çöküş taktiğinde şimdiki ayarın çevresi: Kâr Al ±1, Zarar Kes ±0.5 (canlı botun kabul
    ettiği aralıkta; Kâr Al başabaş tetiğinin üstünde); 4'lü onayda sabit küçük ızgara."""
    if cfg.futures_tactic == "crash":
        base = params_from_cfg(cfg)
        room = futures_liq_distance_pct(base.leverage, base.mmr_pct)
        tps = [t for t in (base.tp_pct - 1, base.tp_pct, base.tp_pct + 1)
               if t >= 1 and (not base.be_enabled or t > base.be_trigger_pct)]
        sls = [x for x in (base.sl_pct - 0.5, base.sl_pct, base.sl_pct + 0.5) if 0.5 <= x < room - LIVE_SL_MARGIN]
    else:
        tps, sls = TP_GRID, SL_GRID
    return [(tp, sl, evaluate(params_from_cfg(cfg, tp_pct=tp, sl_pct=sl), data, signals, windows))
            for tp in tps for sl in sls]


PHASE6_SIM = dict(max_positions=4, leverage=3, extra_breadth=8, isolated_size_pct=50.0, isolated_breadth=2,
                  extra_size_pct=16.7, reentry_breadth=3, loss_cooldown_min=240,       # config.FUTURES_PHASE6_PRESET
                  extra_be_trigger_pct=0.0,                                            # (Faz 8 kapalı: saf Faz 6)
                  extra_hold_min=0, wave_hold_min=0)                                   # (Faz 9 kapalı)
PHASE6_SINGLE_SIM = dict(max_positions=1, leverage=2, isolated_size_pct=50.0, isolated_breadth=2, extra_size_pct=100.0,
                         reentry_breadth=3, loss_cooldown_min=240,               # config.FUTURES_PHASE6_SINGLE_PRESET
                         extra_hold_min=0, wave_hold_min=0)
PHASE6_WAVE = {"futures_wave_rules": "1:5,4:8", "futures_wave_breadth": 12}
PHASE6_OFF_SIM = dict(extra_size_pct=100.0, reentry_breadth=0)
PHASE8_BE = 0.5                                 # config.FUTURES_PHASE8_BE_TRIGGER: ek pozisyonların başabaş tetiği
PHASE9_EXTRA, PHASE9_WAVE = 35, 60              # config.FUTURES_PHASE9_EXTRA_HOLD / _WAVE_HOLD (zaman stopları, dk)


def phase6_on(cfg) -> bool:
    """Faz 6 eklerinden biri açık mı (dalgada yeniden giriş, dalga eşikleri, küçük ek pozisyonlar)."""
    return (int(getattr(cfg, "futures_reentry_breadth", 0)) > 0 or bool(futures_wave_rules(cfg))
            or (int(getattr(cfg, "futures_max_positions", 1)) > 1
                and float(getattr(cfg, "futures_extra_size_pct", 100.0)) < 100))


def no_wave(signals: Sequence[FutSignal]) -> List[FutSignal]:
    """Dalga eşiğiyle (Faz 6) gelen sinyaller çıkarılmış liste."""
    return [s for s in signals if not s.metrics.get("wave_rule")]


def cascade_rows(cfg, ev, signals=None) -> List[Tuple[str, "Evaluation"]]:
    """Dalga modu önerisi: kapalıysa Faz 5 seti ve yalnız çöküş yarım (dalga modu açıkken satır yok: başka kaldıraçla
    "tek pozisyon" kıyası yanıltıcıydı)."""
    base = params_from_cfg(cfg)
    on = phase6_on(cfg)
    plain = no_wave(signals) if signals is not None and on else None
    off = dict(PHASE6_OFF_SIM) if on else {}

    def ev0(**over):            # Faz 6 ekleri kapalı
        return ev(plain, **off, **over) if plain is not None else ev(**off, **over)
    rows = []
    if base.max_positions == 1:
        rows.append(("🌊 Dalga modu (2 pozisyon, 3x, ek ≥8 coin, yalnız çöküş %50)",
                     ev0(max_positions=2, leverage=3, extra_breadth=8, isolated_size_pct=50.0, isolated_breadth=2,
                         extra_be_trigger_pct=0.0, extra_hold_min=0, wave_hold_min=0)))
    if base.isolated_size_pct >= 100:
        rows.append(("Yalnız çöküş yarım büyüklük (<2 coin)", ev(isolated_size_pct=50.0, isolated_breadth=2)))
    return rows


def phase6_rows(cfg, ev, signals=None) -> List[Tuple[str, "Evaluation"]]:
    """Faz 6 eklerinin tek tek kapatılması (açıksa) ya da dalgada yeniden girişin tek başına etkisi (kapalıysa)."""
    base = params_from_cfg(cfg)
    rows = []
    if base.reentry_breadth > 0:
        rows.append(("Dalgada yeniden giriş kapalı (zarardan sonra bekler)", ev(reentry_breadth=0)))
    elif base.loss_cooldown_min > base.symbol_cooldown_min:
        rows.append(("Dalgada yeniden giriş (≥3 coin düşerken bekleme yok)", ev(reentry_breadth=3)))
    if futures_wave_rules(cfg) and signals is not None:
        rows.append(("Dalga eşikleri kapalı", ev(no_wave(signals))))
    if base.max_positions > 1 and base.extra_size_pct < 100:
        rows.append(("Ek pozisyonlar ilk pozisyonla eşit büyüklükte", ev(extra_size_pct=100.0)))
    return rows


def realism_label(p: SimParams) -> str:
    """Rapor başlığı: saniyelik veriyle ayarlanan gerçekçilik payları (Kasım 2026). Kapalıysa boş."""
    parts = []
    if p.entry_lag_pct > 0 and not p.entry_limit:
        parts.append(f"giriş mum kapanışından {lag_text(p.entry_lag_pct)}, fiyat ort. %{p.entry_lag_pct:g} daha yukarıda")
    if p.be_intrabar and p.be_enabled:
        parts.append("başabaşın tetiklendiği dakikada fiyatın tabana geri dönme olasılığı")
    if p.stop_slip_pct > 0:
        parts.append(f"zarar kes / başabaş stopu stoptan %{p.stop_slip_pct:g} daha kötü dolar (kaymaya ek)")
    if not parts:
        return ""
    return ("Gerçekçilik payları (saniyelik işlem verisiyle ölçüldü): " + "; ".join(parts)
            + ". Kasım 2026 öncesi raporlar bunları saymadığı için daha yüksek çıkıyordu.")


def realism_rows(cfg, ev) -> List[Tuple[str, "Evaluation"]]:
    """Giriş maliyeti kıyası: canlıda en az LIVE_FILL_MIN giriş ölçüldüyse o ortalama dolumla; yoksa giriş yoluna göre
    1 sn'de dolsaydı (anlık taramanın hedefi) ya da eski ~5 sn varsayımıyla. Limit girişte satır yok."""
    base = params_from_cfg(cfg)
    if not realism_label(base) or base.entry_limit:
        return []
    fills = live_entry_fills(cfg)
    if len(fills) >= LIVE_FILL_MIN:
        mean = float(np.mean(fills))
        lag = round(max(0.0, mean - base.slippage_pct), 3)
        if abs(lag - base.entry_lag_pct) >= 0.005:
            return [(f"Canlı giriş dolumuyla (kapanıştan {mean:+.3f}%, {len(fills)} giriş)", ev(entry_lag_pct=lag))]
        return []
    if base.entry_lag_pct > FAST_ENTRY_LAG_PCT:
        return [(f"Giriş 1 sn'de dolsaydı (gecikme payı %{base.entry_lag_pct:g} → %{FAST_ENTRY_LAG_PCT:g})",
                 ev(entry_lag_pct=FAST_ENTRY_LAG_PCT))]
    if base.entry_lag_pct < ENTRY_LAG_PCT:
        return [(f"Giriş ~5 sn'de dolsaydı (gecikme payı %{base.entry_lag_pct:g} → %{ENTRY_LAG_PCT:g})",
                 ev(entry_lag_pct=ENTRY_LAG_PCT))]
    return []


LIVE_FILL_MIN = 5                       # Canlı giriş maliyeti stres satırı için en az ölçülmüş giriş


def live_entry_fills(cfg, path: Optional[Path] = None) -> List[float]:
    """Canlı botun tarama kaydındaki (futures_scans_<mod>.jsonl) girişlerin dolum farkı: kapanış fiyatına göre %, kayma
    dahil. Yalnızca şimdiki giriş yolunun (anlık tarama / hızlı giriş / eski yol) taramalarından. Kayıt yoksa boş."""
    path = Path(path) if path is not None else scan_log_path(DATA_DIR, getattr(cfg, "trading_mode", "paper"))
    if not path.exists():
        return []
    want = 2 if futures_snapshot_on(cfg) else int(bool(getattr(cfg, "futures_fast_entry", False)))
    out: List[float] = []
    for r in read_scan_log(path, 0, 2 ** 62):
        if (2 if r.get("sn") else int(bool(r.get("fast")))) != want:
            continue
        out += [float(f["p"]) for f in (r.get("f") or {}).values() if isinstance(f, dict) and f.get("p") is not None]
    return out


def phase8_active(p: SimParams) -> bool:
    """Faz 8 etkin mi: dalga modunda ek pozisyonlar ana ayardan farklı bir başabaş tetiği kullanıyor."""
    return (p.max_positions > 1 and p.be_enabled and p.extra_be_trigger_pct > p.be_floor_pct
            and p.extra_be_trigger_pct != p.be_trigger_pct)


def phase8_rows(cfg, ev) -> List[Tuple[str, "Evaluation"]]:
    """Faz 8 karşılaştırması (dalga modunda, başabaş açıkken): kapalıysa ek pozisyonlarda başabaş +%0.5, açıksa ana
    ayarla aynı tetik. Tek pozisyonda ek pozisyon olmadığı için satır yok."""
    base = params_from_cfg(cfg)
    if base.max_positions <= 1 or not base.be_enabled:
        return []
    if phase8_active(base):
        return [(f"Ek pozisyonlarda da başabaş +%{base.be_trigger_pct:g} (Faz 8 kapalı)",
                 ev(extra_be_trigger_pct=0.0))]
    if base.be_trigger_pct <= PHASE8_BE:
        return []
    return [(f"Ek pozisyonlarda başabaş +%{PHASE8_BE:g} (Faz 8)", ev(extra_be_trigger_pct=PHASE8_BE))]


def phase9_label(p: SimParams) -> str:
    """Faz 9 açıklaması (rapor başlığı): hangi pozisyon kaç dakika tutulur; kapalıysa boş."""
    parts = []
    if p.extra_hold_min > 0 and p.max_positions > 1 and p.extra_hold_min != p.hold_min:
        parts.append(f"ek pozisyonlarda {p.extra_hold_min} dk")
    if p.wave_hold_min > 0 and p.wave_hold_min != p.hold_min:
        parts.append(f"son 1 saatte ≥{p.wave_hold_breadth} coin %3+ düşerken açılan ilk pozisyonda {p.wave_hold_min} dk")
    return "; ".join(parts)


def phase9_rows(cfg, ev) -> List[Tuple[str, "Evaluation"]]:
    """Faz 9 karşılaştırması: açıksa tüm pozisyonlarda aynı zaman stopu; kapalıysa dalgada ilk pozisyonu uzun tutmak ve
    (dalga modunda) ek pozisyonları erken kapatmakla birlikte."""
    base = params_from_cfg(cfg)
    if phase9_active(base):
        return [(f"Zaman stopu her pozisyonda {base.hold_min} dk (Faz 9 kapalı)", ev(extra_hold_min=0, wave_hold_min=0))]
    rows = [(f"Dalgada (≥8 coin) ilk pozisyon {PHASE9_WAVE} dk (Faz 9)", ev(wave_hold_min=PHASE9_WAVE, wave_hold_breadth=8))]
    if base.max_positions > 1:
        rows.append((f"Faz 9: ek pozisyonlar {PHASE9_EXTRA} dk + dalgada ilk pozisyon {PHASE9_WAVE} dk",
                     ev(extra_hold_min=PHASE9_EXTRA, wave_hold_min=PHASE9_WAVE, wave_hold_breadth=8)))
    return rows


def long_window_rows(cfg, other) -> List[Tuple[str, "Evaluation"]]:
    """Uzun ek pencere (≥8 saat, ör. 12 sa %15) varsa onsuz satır: Kasım 2026 testinde (rapor_13) bu pencerenin tek
    başına bulduğu işlemler işlem başı ~%0 getirip tam boy ilk pozisyonu kapattı. `other(**cfg değişikliği)` yeni
    sinyallerle değerlendirir (variant_rows verir). Oynaklık (σ) kurallarında satır yok."""
    long_w = futures_long_windows(cfg)
    if not long_w or cfg.futures_crash_z:
        return []
    keep = tuple(r for r in parse_crash_rules(cfg.futures_crash_extra) if r not in long_w)
    names = " + ".join(f"{h} sa %{p:g}" for h, p in long_w)
    rest = (" (ek: " + " + ".join(f"{h} sa %{p:g}" for h, p in keep) + ")") if keep else " (ek pencere yok)"
    return [(f"{names} penceresi kapalı{rest}", other(futures_crash_extra=format_crash_rules(keep)))]


# Tek kural değişince: mevcut ayarın çevresinde denenen değerler (mevcut değere eşit olan atlanır)
HOLD_TRIES = (30, 60, 120)                      # Zaman stopu (dk)
BE_TRIGGER_TRIES = (0.8, 1.5, 2.0)              # Başabaş tetiği (fiyat %)
BE_FLOOR_TRIES = (0.15, 0.35, 0.5)              # Başabaş tabanı (fiyat %)
EXTRA_BE_TRIES = (0.3, 0.6)                     # Faz 8: ek pozisyonların başabaş tetiği
EXTRA_HOLD_TRIES = (25, 45)                     # Faz 9: ek pozisyonların zaman stopu (dk)
WAVE_HOLD_TRIES = (50, 75)                      # Faz 9: dalgada ilk pozisyonun zaman stopu (dk)
MARGIN_TRIES = (0.75, 0.6)                      # Teminat kullanımı: mevcut değerin bu katı
POSITION_TRIES = (2, 3, 5)                      # Dalga modu: en fazla pozisyon
EXTRA_SIZE_TRIES = (8.0, 25.0, 33.3)            # Faz 6: ek pozisyon boyu (ilkinin %'si)
ISOLATED_TRIES = (25.0, 50.0, 100.0)            # Yalnız çöküş boyu (%)
ISOLATED_BREADTH_TRIES = (2, 3, 5)               # Yalnız çöküş eşiği: bundan az coin düşerken (rapor 12: 5)
RECENT_SIZE_TRIES = (33.0, 50.0, 100.0)          # Çöküş durulmuşsa boy (%; rapor 14 araştırması)
RECENT_DROP_TRIES = (2.0, 2.4, 3.0)              # ...tam boy için girişten önceki 15 dk'da gereken düşüş (%)
REENTRY_TRIES = (5, 8)                          # Faz 6: dalgada yeniden giriş eşiği (coin)
DD_SCALE_TRIES = (20.0, 40.0)                   # Düşüşte küçültme eşiği (%)
DD_SIZE_TRIES = (50.0, 75.0)                    # Düşüşte küçültme boyu (%)
LOSS_COOLDOWN_TRIES = (120, 480)                # Zarardan sonra bekleme (dk)
SYMBOL_COOLDOWN_TRIES = (15, 30)                # Her işlemden sonra aynı coine bekleme (dk)
SLIPPAGE_TRIES = (2, 3, 5)                      # Kayma: mevcut değerin bu katı
DEPTH_CAP_TRIES = (7.5, 10.0, 12.5)             # Derinliğe göre boy (±%1 içindeki küçük tarafın %'si)


def _same(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) < 1e-6


def exit_variant_rows(cfg, ev) -> List[Tuple[str, "Evaluation"]]:
    """Çıkışların mevcut ayar çevresinde tek tek değişmesi: başabaş tetiği / tabanı, zaman stopu, Faz 8 ek başabaş ve
    Faz 9 zaman stopları (açıksa). Kâr Al / Zarar Kes komşuları ızgarada (exit_grid)."""
    base = params_from_cfg(cfg)
    rows: List[Tuple[str, "Evaluation"]] = []
    if base.be_enabled:
        for t in BE_TRIGGER_TRIES:
            if not _same(t, base.be_trigger_pct) and t > base.be_floor_pct:
                rows.append((f"Başabaş tetiği +%{t:g} (şimdi +%{base.be_trigger_pct:g})", ev(be_trigger_pct=t)))
        for f in BE_FLOOR_TRIES:     # Faz 8 açıkken taban ek tetiğin altında kalmalı (yoksa ek pozisyon ana tetiğe döner)
            if (not _same(f, base.be_floor_pct) and f < base.be_trigger_pct
                    and (not phase8_active(base) or f < base.extra_be_trigger_pct)):
                rows.append((f"Başabaş tabanı +%{f:g} (şimdi +%{base.be_floor_pct:g})", ev(be_floor_pct=f)))
    rows += [(f"Zaman stopu {m} dk", ev(hold_min=m)) for m in HOLD_TRIES if m != base.hold_min]
    if phase8_active(base):
        for x in EXTRA_BE_TRIES:
            if not _same(x, base.extra_be_trigger_pct) and x > base.be_floor_pct:
                rows.append((f"Ek pozisyon başabaşı +%{x:g} (şimdi +%{base.extra_be_trigger_pct:g})",
                             ev(extra_be_trigger_pct=x)))
    if base.max_positions > 1 and base.extra_hold_min > 0:
        rows += [(f"Ek pozisyon zaman stopu {m} dk (şimdi {base.extra_hold_min} dk)", ev(extra_hold_min=m))
                 for m in EXTRA_HOLD_TRIES if m != base.extra_hold_min]
    if base.wave_hold_min > 0:
        rows += [(f"Dalgada ilk pozisyon {m} dk (şimdi {base.wave_hold_min} dk)", ev(wave_hold_min=m))
                 for m in WAVE_HOLD_TRIES if m != base.wave_hold_min]
    return rows


def size_variant_rows(cfg, ev, depth: bool = False, has_basis: bool = False,
                      has_funding: bool = False, has_regime: bool = False,
                      has_support: bool = False) -> List[Tuple[str, "Evaluation"]]:
    """Pozisyon büyüklüğü ve dalga modu: teminat kullanımı, en fazla pozisyon, ek pozisyon boyu ve kapısı, yalnız çöküş
    boyu, dalgada yeniden giriş eşiği, düşüşte küçültme (açıksa çevresi, kapalıysa %30 / %60), derinlik verisi varsa
    derinliğe göre boy (açıksa çevresi, kapalıysa %10 + kapasite önceliği)."""
    base = params_from_cfg(cfg)
    rows: List[Tuple[str, "Evaluation"]] = []
    if depth and base.depth_cap_pct > 0:
        rows.append(("Derinliğe göre boy kapalı", ev(depth_cap_pct=0.0)))
        rows += [(f"Derinliğe göre boy %{x:g} (şimdi %{base.depth_cap_pct:g})", ev(depth_cap_pct=x))
                 for x in DEPTH_CAP_TRIES if not _same(x, base.depth_cap_pct)]
    elif depth:
        rows.append(("Derinliğe göre boy %10 + kapasite önceliği", ev(depth_cap_pct=10.0)))
    for frac in MARGIN_TRIES:
        u = float(round(base.margin_usage_pct * frac))     # Canlıda yazılacak tam sayı (ör. 95 → 71, 57)
        rows.append((f"Teminat kullanımı %{u:g} (şimdi %{base.margin_usage_pct:g})", ev(margin_usage_pct=u)))
    if base.max_positions > 1:
        for n in POSITION_TRIES:
            if n != base.max_positions:
                first = base.margin_usage_pct * slot_share(replace(base, max_positions=n), 0)
                rows.append((f"En fazla {n} pozisyon (ilki bakiyenin {_pct_of(first, 1)})", ev(max_positions=n)))
        if base.extra_size_pct < 100:
            for x in EXTRA_SIZE_TRIES:
                if abs(x - base.extra_size_pct) > 0.05:
                    first = base.margin_usage_pct * slot_share(replace(base, extra_size_pct=x), 0)
                    rows.append((f"Ek pozisyon ilkinin {_pct_of(x, 1)} (ilki bakiyenin {_pct_of(first, 1)})",
                                 ev(extra_size_pct=x)))
        for b in (base.extra_breadth - 2, base.extra_breadth + 2):
            if b >= 1:
                rows.append((f"Ek pozisyon: ≥{b} coin düşerken (şimdi ≥{base.extra_breadth})", ev(extra_breadth=b)))
    if base.isolated_size_pct < 100:            # Tam boydayken "yarım büyüklük" satırı cascade_rows'ta
        for v in ISOLATED_TRIES:
            if not _same(v, base.isolated_size_pct):
                name = "Yalnız çöküş tam boy" if v >= 100 else f"Yalnız çöküş boyu %{v:g}"
                rows.append((f"{name} (şimdi %{base.isolated_size_pct:g})", ev(isolated_size_pct=v)))
        rows += [(f"Yalnız çöküş eşiği <{b} coin (şimdi <{base.isolated_breadth})", ev(isolated_breadth=b))
                 for b in ISOLATED_BREADTH_TRIES if b != base.isolated_breadth]
    rows += recent_variant_rows(base, ev)
    rows += bounce_variant_rows(base, ev)
    rows += basis_variant_rows(base, ev, has_basis)
    rows += quality_variant_rows(base, ev)
    rows += big_variant_rows(base, ev, has_funding)
    rows.append(("Güçlü sinyalde en küçük emre yuvarlama " + ("kapalı" if base.quality_roundup else "açık"),
                 ev(quality_roundup=not base.quality_roundup)))
    if base.reentry_breadth > 0:
        rows += [(f"Dalgada yeniden giriş ≥{b} coin (şimdi ≥{base.reentry_breadth})", ev(reentry_breadth=b))
                 for b in REENTRY_TRIES if b != base.reentry_breadth]
    if base.dd_scale_pct > 0:
        rows.append(("Düşüşte küçültme kapalı", ev(dd_scale_pct=0.0)))
        rows += [(f"Düşüşte küçültme eşiği %{t:g} (şimdi %{base.dd_scale_pct:g})", ev(dd_scale_pct=t))
                 for t in DD_SCALE_TRIES if not _same(t, base.dd_scale_pct)]
        rows += [(f"Düşüşte küçültme boyu %{z:g} (şimdi %{base.dd_scale_size_pct:g})", ev(dd_scale_size_pct=z))
                 for z in DD_SIZE_TRIES if not _same(z, base.dd_scale_size_pct)]
    else:
        rows.append(("Düşüşte küçültme: zirveden %30 düşünce %60 boy", ev(dd_scale_pct=30.0, dd_scale_size_pct=60.0)))
    rows += brake_variant_rows(base, ev)
    rows += bear_variant_rows(base, ev, has_regime)
    rows += guard_variant_rows(base, ev)
    rows += cushion_variant_rows(base, ev)
    rows += support_variant_rows(base, ev, has_support)
    return rows


BEAR_SIZE_TRIES = (65.0, 75.0, 85.0)     # Ayı piyasasındaki boy (%; rapor 25 araştırması, 25 Kasım)
BEAR_DAYS_TRIES = (50, 100, 150)         # BTC'nin kaç günlük ortalaması


def bear_variant_rows(base: SimParams, ev, has_regime: bool) -> List[Tuple[str, "Evaluation"]]:
    """Ayı piyasası küçültmesi (FUTURES_BEAR_SIZE_PCT): açıksa kapalı + boy / gün çevresi, kapalıysa %75 / 100 gün ve
    araştırmanın önerdiği birleşim (düşüşte küçültme kapalıyla). BTC rejimi ölçülemediyse satır yok."""
    if not has_regime:
        return []
    if base.bear_size_pct >= 100:
        rows = [("Ayı piyasasında (BTC < 100 günlük ort.) boy %75", ev(bear_size_pct=75.0, bear_sma_days=100))]
        if base.dd_scale_pct > 0:
            rows.append(("Ayı piyasası %75 + düşüşte küçültme kapalı",
                         ev(bear_size_pct=75.0, bear_sma_days=100, dd_scale_pct=0.0)))
        return rows
    rows = [("Ayı piyasası küçültmesi kapalı", ev(bear_size_pct=100.0))]
    rows += [(f"Ayı piyasası boyu %{z:g} (şimdi %{base.bear_size_pct:g})", ev(bear_size_pct=z))
             for z in BEAR_SIZE_TRIES if not _same(z, base.bear_size_pct)]
    rows += [(f"Ayı piyasası: BTC {d} günlük ort. (şimdi {base.bear_sma_days})", ev(bear_sma_days=d))
             for d in BEAR_DAYS_TRIES if d != base.bear_sma_days]
    return rows


SUPPORT_SIZE_TRIES = (45.0, 60.0, 75.0)  # Destek bölgesindeki boy (%; rapor 26 araştırması, 26 Kasım)
SUPPORT_ZONE_TRIES = (3.0, 5.0, 8.0)     # Dibin en fazla % kaç altı
SUPPORT_DAYS_TRIES = (14, 30)


def support_variant_rows(base: SimParams, ev, has_support: bool) -> List[Tuple[str, "Evaluation"]]:
    """Destek bölgesi (FUTURES_SUPPORT_SIZE_PCT): açıksa kapalı + boy / bölge / gün çevresi, kapalıysa 30 gün, %5, %60.
    Destek dipleri ölçülemediyse satır yok."""
    if not has_support:
        return []
    if base.support_size_pct >= 100:
        return [("Destek bölgesinde (30 g dibin 0-%5 altı) boy %60",
                 ev(support_size_pct=60.0, support_days=30, support_zone_pct=5.0))]
    rows = [("Destek bölgesi kuralı kapalı", ev(support_size_pct=100.0))]
    rows += [(f"Destek bölgesi boyu %{z:g} (şimdi %{base.support_size_pct:g})", ev(support_size_pct=z))
             for z in SUPPORT_SIZE_TRIES if not _same(z, base.support_size_pct)]
    rows += [(f"Destek bölgesi: dibin 0-%{z:g} altı (şimdi %{base.support_zone_pct:g})", ev(support_zone_pct=z))
             for z in SUPPORT_ZONE_TRIES if not _same(z, base.support_zone_pct)]
    rows += [(f"Destek bölgesi: {d} günlük dip (şimdi {base.support_days})", ev(support_days=d))
             for d in SUPPORT_DAYS_TRIES if d != base.support_days]
    return rows


CUSHION_X_TRIES = (1.5, 2.0, 3.0)        # Kâr yastığı katı (rapor 26 araştırması, 26 Kasım)
CUSHION_SIZE_TRIES = (112.5, 125.0)     # Yastıktaki boy (%)


def cushion_variant_rows(base: SimParams, ev) -> List[Tuple[str, "Evaluation"]]:
    """Kâr yastığı (FUTURES_CUSHION_X): açıksa kapalı + kat / boy çevresi, kapalıysa 2 kat → %112.5 (yalnız dalga modunda;
    tek pozisyonda büyütülecek boş teminat yok)."""
    if base.max_positions <= 1:
        return []
    if base.cushion_x <= 0:
        return [("Kâr yastığı: başlangıcın 2 katında %112.5 boy", ev(cushion_x=2.0, cushion_size_pct=112.5))]
    rows = [("Kâr yastığı kapalı", ev(cushion_x=0.0))]
    rows += [(f"Kâr yastığı {x:g} katında (şimdi {base.cushion_x:g})", ev(cushion_x=x))
             for x in CUSHION_X_TRIES if not _same(x, base.cushion_x)]
    rows += [(f"Kâr yastığı boyu %{z:g} (şimdi %{base.cushion_size_pct:g})", ev(cushion_size_pct=z))
             for z in CUSHION_SIZE_TRIES if not _same(z, base.cushion_size_pct)]
    return rows


GUARD_TRIES = (20.0, 30.0, 40.0)        # Sermaye koruması eşiği (%; rapor 26 araştırması, 26 Kasım)
GUARD_SIZE_TRIES = (50.0, 60.0, 70.0)   # Korumadaki boy (%)


def guard_variant_rows(base: SimParams, ev) -> List[Tuple[str, "Evaluation"]]:
    """Sermaye koruması (FUTURES_GUARD_PCT): açıksa kapalı + eşik / boy çevresi, kapalıysa %30 → %60. Asıl ölçüsü kaldıraç
    tablosundaki koruma satırı (her ayın 1'inde başlayan hesaplarda kill-switch); A ve B'de etkisi küçüktür."""
    if base.guard_pct <= 0:
        return [("Sermaye koruması: toplam zarar %30 iken %60 boy", ev(guard_pct=30.0, guard_size_pct=60.0))]
    rows = [("Sermaye koruması kapalı", ev(guard_pct=0.0))]
    rows += [(f"Sermaye koruması eşiği %{t:g} (şimdi %{base.guard_pct:g})", ev(guard_pct=t))
             for t in GUARD_TRIES if not _same(t, base.guard_pct)]
    rows += [(f"Sermaye koruması boyu %{z:g} (şimdi %{base.guard_size_pct:g})", ev(guard_size_pct=z))
             for z in GUARD_SIZE_TRIES if not _same(z, base.guard_size_pct)]
    return rows


DAY_BRAKE_TRIES = (10.0, 15.0)          # 24 saat freni eşiği (%; rapor 22 araştırması, 23 Kasım)
DAY_BRAKE_SIZE_TRIES = (50.0, 70.0)     # Frendeki boy (%)


def brake_variant_rows(base: SimParams, ev) -> List[Tuple[str, "Evaluation"]]:
    """24 saat freni (FUTURES_DAY_BRAKE_PCT): açıksa kapalı + eşik / boy çevresi, kapalıysa %10 → %50. Freni kaldıraçla
    birlikte değerlendirmek için asıl ölçü kaldıraç tablosundaki frenli satırlardır (her ay başlayan hesaplar)."""
    if base.day_brake_pct <= 0:
        return [("24 saat freni: son 24 saatin zirvesinden %10 düşünce %50 boy",
                 ev(day_brake_pct=10.0, day_brake_size_pct=50.0))]
    rows = [("24 saat freni kapalı", ev(day_brake_pct=0.0))]
    rows += [(f"24 saat freni eşiği %{t:g} (şimdi %{base.day_brake_pct:g})", ev(day_brake_pct=t))
             for t in DAY_BRAKE_TRIES if not _same(t, base.day_brake_pct)]
    rows += [(f"24 saat freni boyu %{z:g} (şimdi %{base.day_brake_size_pct:g})", ev(day_brake_size_pct=z))
             for z in DAY_BRAKE_SIZE_TRIES if not _same(z, base.day_brake_size_pct)]
    return rows


QUALITY_SIZE_TRIES = (40.0, 60.0)       # Kalite puanına göre boy (%; rapor 18 araştırması, 22 Kasım)
QUALITY_CUT_TRIES = (40.0, 60.0)        # Zayıf sayılan sinyallerin yüzdesi


def quality_variant_rows(base: SimParams, ev) -> List[Tuple[str, "Evaluation"]]:
    """Sinyal kalitesi (FUTURES_QUALITY_SIZE_PCT): açıksa kapalı + boy / eşik çevresi, kapalıysa en zayıf %60 → %60."""
    if base.quality_size_pct >= 100:
        return [("Kalite puanı en zayıf %60 → boy %60", ev(quality_size_pct=60.0, quality_cut_pct=60.0))]
    rows = [("Kalite puanına göre boy kapalı", ev(quality_size_pct=100.0))]
    rows += [(f"Kalite puanı boyu %{v:g} (şimdi %{base.quality_size_pct:g})", ev(quality_size_pct=v))
             for v in QUALITY_SIZE_TRIES if not _same(v, base.quality_size_pct)]
    rows += [(f"Kalite puanı: en zayıf %{c:g} (şimdi %{base.quality_cut_pct:g})", ev(quality_cut_pct=c))
             for c in QUALITY_CUT_TRIES if not _same(c, base.quality_cut_pct)]
    return rows


BIG_ACCOUNT_TRIES = (2000.0, 5000.0, 10000.0)   # Büyük hesap kurallarının bakiye eşiği (USDT; rapor 19 araştırması)


def big_variant_rows(base: SimParams, ev, has_funding: bool) -> List[Tuple[str, "Evaluation"]]:
    """Büyük hesap kuralları (FUTURES_BIG_ACCOUNT_USDT): açıksa kapalı + eşik çevresi, kapalıysa ≥5.000 USDT denemesi.
    Tek pozisyon modunda ya da fonlama verisi yoksa satır yok."""
    if base.max_positions <= 1 or not has_funding:
        return []
    if base.big_account_usdt <= 0:
        return [("Büyük hesap kuralları: bakiye ≥5.000 USDT", ev(big_account_usdt=5000.0))]
    rows = [("Büyük hesap kuralları kapalı", ev(big_account_usdt=0.0))]
    rows += [(f"Büyük hesap kuralları: bakiye ≥{_num(v, 0)} USDT (şimdi ≥{_num(base.big_account_usdt, 0)})",
              ev(big_account_usdt=v)) for v in BIG_ACCOUNT_TRIES if not _same(v, base.big_account_usdt)]
    return rows


BASIS_SIZE_TRIES = (130.0, 150.0)       # Vadeli-spot farkına göre boy (%; rapor 17 araştırması, 22 Kasım)
BASIS_DEEP_TRIES = (0.3, 0.5, 0.7)      # Eşik: vadelinin spota göre iskontosu (%)


def basis_variant_rows(base: SimParams, ev, has_basis: bool) -> List[Tuple[str, "Evaluation"]]:
    """Vadeli-spot farkı (FUTURES_BASIS_SIZE_PCT): açıksa kapalı + boy / eşik çevresi, kapalıysa %0.5 / %130 denemesi.
    Sinyallerde fark ölçüsü yoksa (spot verisi alınamadı) satır yok."""
    if not has_basis:
        return []
    if base.basis_size_pct == 100:
        return [("Vadeli spotun %0.5 altındayken boy %130", ev(basis_size_pct=130.0, basis_deep_pct=0.5))]
    rows = [("Vadeli-spot farkına göre boy kapalı", ev(basis_size_pct=100.0))]
    rows += [(f"Vadeli-spot boyu %{v:g} (şimdi %{base.basis_size_pct:g})", ev(basis_size_pct=v))
             for v in BASIS_SIZE_TRIES if not _same(v, base.basis_size_pct)]
    rows += [(f"Vadeli-spot eşiği %{d:g} (şimdi %{base.basis_deep_pct:g})", ev(basis_deep_pct=d))
             for d in BASIS_DEEP_TRIES if not _same(d, base.basis_deep_pct)]
    return rows


def recent_variant_rows(base: SimParams, ev) -> List[Tuple[str, "Evaluation"]]:
    """Çöküş sürüyor mu (FUTURES_RECENT_SIZE_PCT): açıksa boy ve eşik çevresi, kapalıysa %50 / son 15 dk < %2.4."""
    rows: List[Tuple[str, "Evaluation"]] = []
    if base.recent_size_pct >= 100:
        rows.append(("Çöküş durulmuşsa (son 15 dk düşüş < %2.4) boy %50",
                     ev(recent_size_pct=50.0, recent_drop_pct=2.4)))
        return rows
    for v in RECENT_SIZE_TRIES:
        if not _same(v, base.recent_size_pct):
            name = "Çöküş durulmuşsa da tam boy" if v >= 100 else f"Çöküş durulmuşsa boy %{v:g}"
            rows.append((f"{name} (şimdi %{base.recent_size_pct:g})", ev(recent_size_pct=v)))
    rows += [(f"Durulmuş sayılan: 15 dk düşüş < %{d:g} (şimdi < %{base.recent_drop_pct:g})",
              ev(recent_drop_pct=d)) for d in RECENT_DROP_TRIES if not _same(d, base.recent_drop_pct)]
    return rows


BOUNCE_CALM_TRIES = (2.0, 3.0, 4.0)      # V dönüşü: durulmuş muafiyeti eşiği (%; rapor 27 araştırması, 26 Kasım)
BOUNCE_BOOST_TRIES = (6.0, 8.0, 10.0)    # Güçlü V dönüşü eşiği (%)
BOUNCE_SIZE_TRIES = (125.0, 150.0)       # Güçlü V dönüşündeki boy (%)


def bounce_variant_rows(base: SimParams, ev) -> List[Tuple[str, "Evaluation"]]:
    """V dönüşü (FUTURES_BOUNCE_*): açıksa kapalı + eşik / boy çevresi, kapalıysa %3 muafiyet + %8'de %150 (yalnız dalga
    modunda büyütme anlamlı; tek pozisyonda büyütülecek boş teminat yok)."""
    if base.bounce_calm_pct <= 0 and base.bounce_boost_pct <= 0:
        return [("V dönüşü: son 15 dk'nın dibinden %3+ yukarıdaysa durulmuş sayılmaz, %8+ ise %150 boy",
                 ev(bounce_calm_pct=3.0, bounce_boost_pct=8.0, bounce_boost_size_pct=150.0))]
    rows = [("V dönüşü kuralları kapalı", ev(bounce_calm_pct=0.0, bounce_boost_pct=0.0))]
    if base.bounce_calm_pct > 0:
        rows += [(f"V dönüşünde durulmuş muafiyeti %{t:g}+ (şimdi %{base.bounce_calm_pct:g})", ev(bounce_calm_pct=t))
                 for t in BOUNCE_CALM_TRIES if not _same(t, base.bounce_calm_pct)]
    if base.bounce_boost_pct > 0:
        rows += [(f"Güçlü V dönüşü %{t:g}+ (şimdi %{base.bounce_boost_pct:g})", ev(bounce_boost_pct=t))
                 for t in BOUNCE_BOOST_TRIES if not _same(t, base.bounce_boost_pct)]
        rows += [(f"Güçlü V dönüşünde boy %{z:g} (şimdi %{base.bounce_boost_size_pct:g})", ev(bounce_boost_size_pct=z))
                 for z in BOUNCE_SIZE_TRIES if not _same(z, base.bounce_boost_size_pct)]
    return rows


def wait_variant_rows(cfg, ev) -> List[Tuple[str, "Evaluation"]]:
    """Bekleme ve risk sigortaları: zarardan sonra bekleme, aynı coine bekleme, zarar serisi molası."""
    base = params_from_cfg(cfg)
    rows: List[Tuple[str, "Evaluation"]] = []
    if base.loss_cooldown_min > base.symbol_cooldown_min:
        rows.append(("Zarardan sonra bekleme yok", ev(loss_cooldown_min=0)))
        rows += [(f"Zarardan sonra bekleme {m / 60:g} saat (şimdi {base.loss_cooldown_min / 60:g} saat)",
                  ev(loss_cooldown_min=m)) for m in LOSS_COOLDOWN_TRIES if m != base.loss_cooldown_min]
    else:
        rows.append(("Zarardan sonra 4 saat bekleme", ev(loss_cooldown_min=240)))
    if base.symbol_cooldown_min > 0:
        rows.append(("Aynı coine bekleme yok", ev(symbol_cooldown_min=0)))
    else:
        rows += [(f"Her işlemden sonra aynı coine {m} dk bekleme", ev(symbol_cooldown_min=m))
                 for m in SYMBOL_COOLDOWN_TRIES]
    if base.loss_streak_limit > 0:
        rows.append(("Zarar serisi molası kapalı", ev(loss_streak_limit=0)))
    else:
        rows.append(("Üst üste 3 zarar keste 4 saat mola", ev(loss_streak_limit=3, loss_streak_pause_h=4.0)))
    return rows


def window_variant_rows(cfg, other, tag: str = "") -> List[Tuple[str, "Evaluation"]]:
    """Ek çöküş pencerelerinin eşiği bir puan aşağı / yukarı ve ek pencereler kapalı (yeni sinyallerle; `other`
    variant_rows verir). Ek pencere ya da sabit yüzde kuralı yoksa satır yok."""
    if cfg.futures_crash_z:
        return []
    try:
        rules = parse_crash_rules(cfg.futures_crash_extra)
    except ValueError:
        return []
    if not rules:
        return []
    rows = []
    for d in (-1, 1):
        moved = tuple((h, q + d) for h, q in rules)
        if all(q > 0 for _h, q in moved):
            names = " + ".join(f"{h} sa %{q:g}" for h, q in moved)
            rows.append((f"Ek pencere {names}{tag}", other(futures_crash_extra=format_crash_rules(moved))))
    rows.append((f"Ek pencere yok (yalnız {cfg.futures_crash_hours} sa %{cfg.futures_crash_drop_pct:g}){tag}",
                 other(futures_crash_extra="")))
    return rows


def cost_variant_rows(cfg, ev, depth: bool = False) -> List[Tuple[str, "Evaluation"]]:
    """Maliyet stresi: kayma 2 / 3 / 5 katı, komisyon 2 katı, kötümser stop dolumu (en kötü durum)."""
    base = params_from_cfg(cfg)
    rows = [(f"Kayma {k} kat (%{base.slippage_pct * k:.2f})", ev(slippage_pct=base.slippage_pct * k))
            for k in SLIPPAGE_TRIES]
    rows.append((FEE2_ROW, ev(taker_fee=base.taker_fee * 2, maker_fee=base.maker_fee * 2)))
    rows.append((HARSH_STOP_ROW, ev(harsh_stops=True)))
    return rows


SCAN5_REF = "5 dk'da bir tarama (dakikalık tarama kapalı)"   # "· 5 dk taramayla" satırlarının kıyas satırı
FEE2_ROW = "Komisyon 2 kat"
HARSH_STOP_ROW = "Kötümser stop dolumu (fitilin yarısı; en kötü durum)"
# Tek kural tablosunda satırın altına yazılan açıklama (FEE2_ROW'unki yalnızca başabaş açıkken)
ROW_NOTES = {
    SCAN5_REF: "alttaki \"· 5 dk taramayla\" satırları yeni sinyal gerektirdiği için 5 dk taramayla hesaplandı; "
               "▲ / ▼ bu satıra göre",
    FEE2_ROW: "kayma ve komisyon artınca başabaş çıkışları (net ~%0) küçük zarara döner; C'deki kazanma oranı bu yüzden "
              "düşer",
    HARSH_STOP_ROW: "saniyelik veride gerçek stop dolumu fitilin ~%3'ü; fitilin yarısı çok kötümser bir varsayımdır",
}


def _crash_variant_rows(cfg, signals, minute: bool, tag: str, ev, other,
                        depth: bool = False) -> List[Tuple[str, object]]:
    """Çöküş taktiğinin "tek kural değişince" satırları (gruplu; değeri None olan satır grup başlığı). `ev(sinyaller,
    **SimParams değişikliği)` ve `other(**cfg değişikliği)` (yeni sinyallerle) değerlendirir; variant_rows önce taslak
    fonksiyonlarla satırları sayar, sonra gerçek değerlendirmeyle çağırır. Kâr Al / Zarar Kes komşuları ızgarada (exit_grid).
    Dakikalık taramada yeni sinyal gerektiren satırlar 5 dk taramayla hesaplanır: önce kıyas satırları (SCAN5_REF) gelir,
    rapor bu satırları ona göre işaretler."""
    base = params_from_cfg(cfg)
    shield = (("Başabaş kalkanı kapalı", ev(be_enabled=False)) if base.be_enabled else
              ("Başabaş kalkanı açık (+%1 → +%0.3)", ev(be_enabled=True, be_trigger_pct=1.0, be_floor_pct=0.3)))
    tp_order = (("Kâr Al piyasa emriyle (taker)", ev(tp_limit=False)) if base.tp_limit else
                ("Kâr Al limit emirle (maker)", ev(tp_limit=True)))
    trail = [("İzleyen stop kapalı", ev(trail_enabled=False))] if base.trail_enabled else []
    scan5 = [(SCAN5_REF, ev([s for s in signals if not s.close_ms % FUT_BAR_MS]))] if minute else []
    return ([("Çıkışlar ve başabaş", None), shield] + trail
            + exit_variant_rows(cfg, ev) + [tp_order] + phase8_rows(cfg, ev) + phase9_rows(cfg, ev)
            + [("Pozisyon büyüklüğü ve dalga modu", None)]
            + size_variant_rows(cfg, ev, depth, any("basis" in s.metrics for s in signals),
                                any("last_funding" in s.metrics for s in signals),
                                any("bear100" in s.metrics for s in signals),
                                any("low30h" in s.metrics for s in signals))
            + cascade_rows(cfg, ev, signals) + phase6_rows(cfg, ev, signals)
            + [("Giriş kuralı, tarama ve bekleme", None)] + scan5
            + [(f"Çöküş eşiği %{cfg.futures_crash_drop_pct - 2:g}{tag}",
                other(futures_crash_drop_pct=cfg.futures_crash_drop_pct - 2)),
               (f"Çöküş eşiği %{cfg.futures_crash_drop_pct + 2:g}{tag}",
                other(futures_crash_drop_pct=cfg.futures_crash_drop_pct + 2)),
               (("+likidasyon dalgası şartı" if not cfg.futures_crash_liq else "Likidasyon şartı kapalı") + tag,
                other(futures_crash_liq=not cfg.futures_crash_liq))]
            + window_variant_rows(cfg, other, tag)
            + [(name + tag, e) for name, e in long_window_rows(cfg, other)]
            + wait_variant_rows(cfg, ev)
            + [("Maliyet ve gerçekçilik", None)] + cost_variant_rows(cfg, ev, depth) + realism_rows(cfg, ev))


def variant_rows(cfg, data: FutData, signals, windows,
                 progress: Optional[Callable[[str], None]] = None) -> List[Tuple[str, Evaluation]]:
    """Tek bir kuralı değiştirmenin etkisi (diğer her şey aynı). Çöküş taktiğinde satırlar gruplanır: değeri None olan
    satır grup başlığıdır (rapor "▸ başlık" yazar). `progress` verilirse 10 ayarda bir "⏳ ..." satırı."""
    def ev(sigs=signals, **over):
        return evaluate(params_from_cfg(cfg, **over), data, sigs, windows)
    if cfg.futures_tactic == "crash":
        # Dakikalık taramada yeni sinyal gerektiren satırlar 5 dk taramayla hesaplanır (her biri için tüm 1 dk geçmişi
        # yeniden taramak raporu çok uzatırdı); etiketleri bunu söyler, "5 dk'da bir tarama" satırıyla karşılaştırılır
        minute = bool(futures_scan_shifts(cfg)) and data.minute_loader is not None
        tag = " · 5 dk taramayla" if minute else ""

        def other(**changes):
            c2 = replace(cfg, **changes, **({"futures_minute_scan": False} if minute else {}))
            sigs, _f = find_signals(c2, data)
            load_windows(data, sigs, window_minutes(cfg), cache=windows)
            if data.depth is not None:
                data.depth.prefetch([(x.symbol, x.close_ms) for x in sigs])
            attach_basis(c2, data, sigs)
            attach_funding(c2, data, sigs)
            attach_regime(c2, data, sigs)
            attach_support(c2, data, sigs)
            return evaluate(params_from_cfg(c2), data, sigs, windows)
        def stub(*_a, **_k):
            return 0
        depth = data.depth is not None
        total = sum(1 for _n, e in _crash_variant_rows(cfg, signals, minute, tag, stub, stub, depth) if e is not None)
        bar = _RowProgress("Tek kural değişince", total, progress)
        return _crash_variant_rows(cfg, signals, minute, tag, bar.wrap(ev), bar.wrap(other), depth)
    out = [("Başabaş kalkanı kapalı", ev(be_enabled=False)),
           ("Zaman stopu 30 dk", ev(hold_min=30)),
           ("Zaman stopu 120 dk", ev(hold_min=120)),
           ("Kâr Al piyasa emriyle (taker)", ev(tp_limit=False)),
           ("Komisyonsuz + kaymasız (kıyas)", ev(taker_fee=0.0, maker_fee=0.0, slippage_pct=0.0)),
           ("Yalnızca long", ev([s for s in signals if s.side == "long"])),
           ("Yalnızca short", ev([s for s in signals if s.side == "short"]))]
    return out + realism_rows(cfg, ev)


# ===================================================================== ızgara testi (--izgara)
GRID_HOURS = (1, 2, 4, 8)                       # Düşüş zamanları (saat)
GRID_DROPS = (5.0, 8.0, 10.0, 15.0)             # Düşüş oranları (%)
GRID_CONFIRMS = ("close", "volume", "green")    # Teyit: sadece kapanış, hacim zirvesi, ilk yeşil mum
GRID_Z = (2.5, 3.0, 4.0)                        # Oynaklık eşiği denemeleri (standart sapma)
GRID_MULTI = "1:5,12:15"                        # Mikro (1 saatte %5) + makro (12 saatte %15); ana kural 4 saatte %10
# Ana kurala (ayarlarınızdaki saat / %) VEYA ile eklenen pencereler (çöküş mumunun kapanışında)
GRID_EXTRAS = ("1:5", "1:6", "1:7", "2:8", "12:15", "1:6,12:15", "1:7,12:15")
GRID_SPLIT = "2023-11-01"                       # Seçim (öncesi) / doğrulama (sonrası) dönemi sınırı
GRID_TARGET = 400                               # Hedef: yılda en az 400 işlem
GRID_VERIFY = 6                                 # 1 dk mumlarla doğrulanan aday sayısı (mevcut ayar hariç)
GRID_STRESS_SLIP = 3.0                          # Seçimde maliyet stresi: kayma bu kat (önerilen ayar orada da iyi olmalı)
CONFIRM_TR = {"close": "sadece kapanış", "volume": "hacim zirvesi", "green": "ilk yeşil mum",
              "either": "hacim veya yeşil"}
GRID_ENV = (("futures_crash_hours", "FUTURES_CRASH_HOURS"), ("futures_crash_drop_pct", "FUTURES_CRASH_DROP_PCT"),
            ("futures_crash_extra", "FUTURES_CRASH_EXTRA"), ("futures_crash_z", "FUTURES_CRASH_Z"),
            ("futures_crash_z_min_pct", "FUTURES_CRASH_Z_MIN_PCT"), ("futures_confirm", "FUTURES_CONFIRM"),
            ("futures_confirm_bars", "FUTURES_CONFIRM_BARS"),
            ("futures_confirm_volume_ratio", "FUTURES_CONFIRM_VOLUME_RATIO"))


@dataclass(frozen=True)
class GridVariant:
    """Izgaranın bir noktası: ayar adı → değer (yalnızca giriş kuralı; çıkışlar ve evren ayarlarınızdaki gibi)."""
    label: str
    over: Tuple[Tuple[str, object], ...] = ()

    def cfg(self, base):
        return replace(base, futures_tactic="crash", **dict(self.over)) if self.over else base

    def settings(self, base) -> List[Tuple[str, object]]:
        """Bu kuralı canlıda kurmak için ayarlar (ENV adı, değer)."""
        c = self.cfg(base)
        out = [(env, getattr(c, attr)) for attr, env in GRID_ENV]
        if not c.futures_crash_z:
            out = [(k, v) for k, v in out if k != "FUTURES_CRASH_Z_MIN_PCT"]
        if c.futures_confirm == "close":
            out = [(k, v) for k, v in out if k not in ("FUTURES_CONFIRM_BARS", "FUTURES_CONFIRM_VOLUME_RATIO")]
        elif c.futures_confirm == "green":
            out = [(k, v) for k, v in out if k != "FUTURES_CONFIRM_VOLUME_RATIO"]
        return out


def rule_label(c) -> str:
    """Giriş kuralının kısa adı: "4s %10 · sadece kapanış", "1s %5 | 4s %10 | 12s %15 · ...", "4s 3σ (≥%3) · ..."."""
    rules = futures_crash_rules(c)
    if c.futures_crash_z:
        text = "/".join(f"{h}s" for h, _p in rules) + f" {c.futures_crash_z:g}σ (≥%{c.futures_crash_z_min_pct:g})"
    elif len(rules) == 1:
        text = f"{rules[0][0]}s %{rules[0][1]:g}"
    else:
        text = " | ".join(f"{h}s %{p:g}" for h, p in rules)
    conf = CONFIRM_TR.get(c.futures_confirm, c.futures_confirm)
    if c.futures_confirm != "close":
        conf += f" ({c.futures_confirm_bars * 5} dk)"
    return f"{text} · {conf}"


def grid_variants(cfg) -> List[GridVariant]:
    """Varsayılan ızgara: mevcut ayarınız + [1, 2, 4, 8 saat] × [%5, 8, 10, 15] × [kapanış, hacim zirvesi, yeşil mum]
    + mikro/standart/makro (VEYA) × 4 teyit + oynaklık eşiği [1, 2, 4, 8 saat] × [2.5, 3, 4 σ] × 3 teyit + ana kurala
    VEYA ile eklenen pencereler (GRID_EXTRAS)."""
    base = {"futures_crash_extra": "", "futures_crash_z": 0.0}
    out = [GridVariant("Mevcut ayarınız: " + rule_label(cfg))]
    for h in GRID_HOURS:
        for d in GRID_DROPS:
            for conf in GRID_CONFIRMS:
                over = dict(base, futures_crash_hours=h, futures_crash_drop_pct=d, futures_confirm=conf)
                out.append(GridVariant(rule_label(replace(cfg, **over)), tuple(over.items())))
    for conf in GRID_CONFIRMS + ("either",):
        over = dict(futures_crash_hours=4, futures_crash_drop_pct=10.0, futures_crash_extra=GRID_MULTI,
                    futures_crash_z=0.0, futures_confirm=conf)
        out.append(GridVariant(rule_label(replace(cfg, **over)), tuple(over.items())))
    for h in GRID_HOURS:
        for z in GRID_Z:
            for conf in GRID_CONFIRMS:
                over = dict(base, futures_crash_hours=h, futures_crash_z=z, futures_confirm=conf)
                out.append(GridVariant(rule_label(replace(cfg, **over)), tuple(over.items())))
    for extra in GRID_EXTRAS:
        over = dict(futures_crash_hours=cfg.futures_crash_hours, futures_crash_drop_pct=cfg.futures_crash_drop_pct,
                    futures_crash_extra=extra, futures_crash_z=0.0, futures_confirm="close")
        c2 = replace(cfg, **over)
        if futures_crash_rules(c2) != futures_crash_rules(replace(cfg, futures_crash_extra=GRID_MULTI)):
            out.append(GridVariant(rule_label(c2), tuple(over.items())))
    return out


class GridWindows(dict):
    """(parite, giriş ms) → 5 dk çıkış penceresi. Izgarada her coinin mumları bir kez okunurken, herhangi bir
    kuralın sinyal verdiği mumların sonraki `bars` mumu saklanır (tüm ızgara tek okumayla taranır)."""

    def __init__(self, bars: int):
        super().__init__()
        self.bars = bars
        self.blocks: Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}

    def add(self, symbol: str, k: Dict[str, np.ndarray], idx: np.ndarray) -> None:
        n = len(k["t"])
        idx = idx[idx + 1 < n]
        if not len(idx):
            return
        rows = idx[:, None] + 1 + np.arange(self.bars)[None, :]
        lens = (rows < n).sum(axis=1)
        rows = np.minimum(rows, n - 1)
        prices = np.stack([k[x][rows] for x in ("o", "h", "l", "c")], axis=-1)
        self.blocks[symbol] = (k["t"][idx] + FUT_BAR_MS, k["t"][rows], prices, lens)

    def get(self, key, default=None):
        block = self.blocks.get(key[0])
        if block is None:
            return default
        closes, t, prices, lens = block
        j = int(np.searchsorted(closes, key[1]))
        if j >= len(closes) or int(closes[j]) != key[1]:
            return default
        m = int(lens[j])
        pr = prices[j, :m]
        return FUT_BAR_MS, t[j, :m], pr[:, 0], pr[:, 1], pr[:, 2], pr[:, 3]


@dataclass
class GridPart:
    """Bir dönemin özeti: sabit pozisyonla (işlem sayısı, kazanma, işlem başı net) ve tek hesapla (getiri, düşüş)."""
    name: str
    years: float
    trades: int
    win_rate: float
    avg_move: float
    sum_move: float
    cagr: float
    max_dd: float
    ruined: bool
    liquidations: int = 0           # Sabit pozisyonda likidasyonla kapanan işlem
    gaps: int = 0                   # Stopun / likidasyonun ötesinde açılan mumla kapanan işlem
    worst_move: float = 0.0         # En kötü işlemin net sonucu (kaldıraçsız fiyat %)

    @property
    def per_year(self) -> float:
        return self.trades / self.years if self.years > 0 else 0.0


@dataclass
class GridRow:
    variant: GridVariant
    signals: int
    whole: GridPart
    parts: List[GridPart]
    profitable_years: int
    n_years: int
    exit_label: str = ""                 # Çıkış denemesi (boş = ayarlarınızdaki çıkışlar)
    exit_over: Tuple[Tuple[str, object], ...] = ()

    def part(self, name: str) -> Optional[GridPart]:
        return next((x for x in self.parts if x.name == name), None)


def _cagr(res: FutResult) -> float:
    days = (res.end_ms - res.start_ms) / DAY_MS
    if res.ruined_ms is not None or res.final_equity <= 0:
        return -100.0
    return ((res.final_equity / res.params.start_capital) ** (365.25 / max(days, 1.0)) - 1) * 100


def _grid_part(name: str, trades: Sequence[FutTrade], chained: FutResult) -> GridPart:
    moves = [net_move_pct(tr) for tr in trades]
    years = (chained.end_ms - chained.start_ms) / DAY_MS / 365.25
    return GridPart(name, years, len(trades), sum(m > 0 for m in moves) / len(moves) * 100 if moves else 0.0,
                    float(np.mean(moves)) if moves else 0.0, float(np.sum(moves)) if moves else 0.0,
                    _cagr(chained), max_drawdown(chained.curve)[0], chained.ruined_ms is not None,
                    sum(tr.liquidated for tr in trades), sum(tr.gap for tr in trades),
                    float(min(moves)) if moves else 0.0)


def grid_row(variant: GridVariant, p: SimParams, data: FutData, signals: Sequence[FutSignal], windows,
             split_ms: int) -> GridRow:
    """Bir kuralın sonucu: tüm dönem + seçim / doğrulama dönemleri (tek hesap her dönemde baştan başlar)."""
    size = p.start_capital * p.margin_usage_pct / 100 * p.leverage
    fixed = simulate(p, data, signals, windows, fixed_notional=size)
    whole = _grid_part("Tümü", fixed.trades, simulate(p, data, signals, windows))
    parts = []
    split_ms = min(max(split_ms, data.start_ms), data.end_ms)        # Kısa testte dönem verinin içine kırpılır
    for name, a, b in (("Seçim", data.start_ms, split_ms), ("Doğrulama", split_ms, data.end_ms)):
        if a < b:
            chained = simulate(p, data, signals, windows, start_ms=a, end_ms=b)
            parts.append(_grid_part(name, [tr for tr in fixed.trades if a <= tr.entry_ms < b], chained))
    by_year: Dict[int, float] = {}
    for tr in fixed.trades:
        by_year[_year(tr.entry_ms)] = by_year.get(_year(tr.entry_ms), 0.0) + net_move_pct(tr)
    return GridRow(variant, len(signals), whole, parts, sum(v > 0 for v in by_year.values()),
                   len(year_ranges(data)))


def grid_search(cfg, data: FutData, variants: Optional[Sequence[GridVariant]] = None,
                split_ms: Optional[int] = None, progress: Optional[Callable[[str], None]] = None,
                exits: Optional[Sequence[Tuple[str, Dict[str, object]]]] = None) -> List[GridRow]:
    """Tüm kuralları tek veri okumasıyla dener: her coinin 5 dk mumları bir kez okunur, ölçüler (pencere düşüşleri,
    oynaklık) bir kez hesaplanır, her kuralın sinyalleri canlı botla aynı fonksiyonlarla (futures_candidates /
    crash_scores) bulunur. Çıkışlar 5 dk mumlarla izlenir (hızlı tarama; en iyiler 1 dk mumlarla doğrulanır).
    `exits`: her kural için ayrıca denenecek çıkışlar [(ad, SimParams alanları)]; yoksa ayarlarınızdaki çıkışlar."""
    variants = list(variants or grid_variants(cfg))
    exits = list(exits or [("", {})])
    say = progress or (lambda _m: None)
    split_ms = split_ms if split_ms is not None else _ms(GRID_SPLIT)
    cfgs = [v.cfg(cfg) for v in variants]
    hold = max([c.max_hold_minutes for c in cfgs] + [int(o.get("hold_min", 0)) for _n, o in exits]
               + [int(getattr(c, "futures_extra_hold_minutes", 0)) for c in cfgs]          # Faz 9 süreleri
               + [int(getattr(c, "futures_wave_hold_minutes", 0)) for c in cfgs])
    windows = GridWindows(hold // 5 + 2)
    store: List[List[tuple]] = [[] for _ in variants]
    coins = len(data.lean_symbols) if data.loader is not None else len(data.k5)
    breadth = BreadthCounter(data)
    for n, (symbol, k) in enumerate(_series(data), 1):
        k = with_extras(k, symbol, data, cfgs)
        t = k["t"]
        close = t + FUT_BAR_MS
        f = futures_features(k, cfg)
        breadth.add(f, close, symbol[:-4])
        active = _active_mask(close, symbol[:-4], data.segments)
        nxt_ok = np.zeros(len(t), dtype=bool)
        nxt_ok[:-1] = t[1:] == close[:-1]
        active &= nxt_ok
        used = np.zeros(len(t), dtype=bool)
        for vi, c2 in enumerate(cfgs):
            idx, side, prio, score = futures_entries(f, c2, active)
            if len(idx):
                used[idx] = True
                store[vi].append((close[idx], symbol, side, prio, score, k["o"][idx + 1]))
        windows.add(symbol, k, np.flatnonzero(used))
        if n % 10 == 0:
            say(f"   {n}/{coins} coin tarandı")
    breadth.finish(())
    rows = []
    for vi, (variant, c2) in enumerate(zip(variants, cfgs)):
        signals = [FutSignal(int(ms), sym, "long" if sd > 0 else "short", float(sc), float(eo), priority=int(pr),
                             breadth=breadth_at(data, int(ms)))
                   for closes, sym, sides, prios, scores, opens in store[vi]
                   for ms, sd, pr, sc, eo in zip(closes, sides, prios, scores, opens)]
        signals.sort(key=signal_order)
        for name, over in exits:
            row = grid_row(variant, params_from_cfg(c2, **over), data, signals, windows, split_ms)
            row.exit_label, row.exit_over = name, tuple(over.items())
            rows.append(row)
        store[vi] = []
        if (vi + 1) % 10 == 0:
            say(f"   {vi + 1}/{len(variants)} kural simüle edildi")
    return rows


EXIT_ENV = (("tp_pct", "TAKE_PROFIT_PCT"), ("sl_pct", "STOP_LOSS_PCT"), ("be_enabled", "BREAKEVEN_ENABLED"),
            ("be_trigger_pct", "BREAKEVEN_TRIGGER_PCT"), ("be_floor_pct", "BREAKEVEN_FLOOR_PCT"),
            ("hold_min", "MAX_HOLD_MINUTES"))


def exit_label(p: SimParams) -> str:
    be = f"başabaş +{p.be_trigger_pct:g} → +{p.be_floor_pct:g}" if p.be_enabled else "başabaş yok"
    return f"TP +{p.tp_pct:g} / SL -{p.sl_pct:g} / {be} / {p.hold_min} dk"


def grid_exits(cfg) -> List[Tuple[str, Dict[str, object]]]:
    """2. aşama: en iyi giriş kurallarıyla denenen çıkışlar (ayarlarınız + TP / SL / başabaş / zaman stopu). Başabaş
    tabanı en az +0.3: +0.2'de maliyetler (komisyon + kayma ≈ %0.14) biraz artınca küçük kazançlar zarara döner."""
    out: List[Tuple[str, Dict[str, object]]] = [("ayarlarınız", {})]
    for tp in (6.0, 8.0):
        for sl in (4.0, 5.0):
            for trig, floor in ((1.5, 0.3), (1.0, 0.3), (1.5, 0.5), (1.0, 0.5)):
                for hold in (30, 45, 60):
                    over = dict(tp_pct=tp, sl_pct=sl, be_enabled=True, be_trigger_pct=trig, be_floor_pct=floor,
                                hold_min=hold)
                    out.append((exit_label(params_from_cfg(cfg, **over)), over))
    return out


def grid_kept(row: GridRow, base: GridRow) -> Tuple[bool, bool]:
    """(kazanma oranı korundu, kâr korundu): kazanma oranı mevcut ayardan düşük değil; sabit pozisyon toplamı ve
    seçim / doğrulama dönemlerinin yıllık getirisi mevcut ayardan düşük değil."""
    win = row.whole.win_rate >= base.whole.win_rate - 1e-9
    profit = row.whole.sum_move >= base.whole.sum_move - 1e-9
    for name in ("Seçim", "Doğrulama"):
        a, b = row.part(name), base.part(name)
        if a is not None and b is not None and a.cagr < b.cagr:
            profit = False
    return win, profit


def grid_pick(rows: Sequence[GridRow], base: GridRow, n: int = 2, target: int = GRID_TARGET,
              robust: Optional[Callable[[GridRow], bool]] = None) -> List[GridRow]:
    """En iyi n yapılandırma (farklı giriş kuralları). İlki hedefe göre: kazanma ve kâr korunanlardan yılda ≥ hedef
    işlem yapanların en kârlısı (yoksa en çok işlem yapanı). İkincisi sağlamlığa göre: robust verilirse (maliyet
    stresinde ve yıllara göre de korunan) en çok işlem yapanı. Kalanlar korunanlardan en çok işlem yapanlar."""
    rows = [r for r in rows if r is not base and (r.variant.over or r.exit_over)]
    kept = [r for r in rows if all(grid_kept(r, base))]
    sturdy = [r for r in kept if robust is not None and robust(r)]
    tiers = (sorted([r for r in kept if r.whole.per_year >= target], key=lambda r: -r.whole.sum_move)[:1],
             sorted(sturdy, key=lambda r: -r.whole.per_year)[:1],
             sorted(kept, key=lambda r: -r.whole.per_year),
             sorted(rows, key=lambda r: -r.whole.per_year))
    out: List[GridRow] = []
    for tier in tiers:
        for r in tier:
            if len(out) < n and all(r.variant.label != o.variant.label for o in out):
                out.append(r)
    return out


def grid_candidates(exit_rows: Sequence[GridRow], base: GridRow, per_rule: int = 3) -> List[GridRow]:
    """1 dk doğrulamasına gidecek adaylar: 2. aşamadaki her giriş kuralının (mevcut kural dahil) kazanma ve kârı
    koruyan en kârlı `per_rule` çıkışı. 5 dk sıralaması başabaşı iyimser gösterebildiği için tek bir sıralamadan
    değil, her kuraldan aday alınır."""
    out: List[GridRow] = []
    labels = list(dict.fromkeys(r.variant.label for r in exit_rows))
    for label in labels:
        mine = [r for r in exit_rows if r.variant.label == label and r.exit_over and all(grid_kept(r, base))]
        out += sorted(mine, key=lambda r: -r.whole.sum_move)[:per_rule]
    return out


def grid_stage2(rows: Sequence[GridRow], base: GridRow, n: int = 4) -> List[GridVariant]:
    """Çıkış denemelerine alınacak giriş kuralları: kârı mevcut ayarın en az %80'i ve işlemi en az 1.2 katı olanlardan
    en çok işlem yapan n ve en kârlı n (tekrarsız)."""
    pool = [r for r in rows if r is not base and r.variant.over
            and r.whole.sum_move >= base.whole.sum_move * 0.8 and r.whole.per_year >= base.whole.per_year * 1.2]
    out: List[GridVariant] = []
    for r in (sorted(pool, key=lambda r: -r.whole.per_year)[:n] + sorted(pool, key=lambda r: -r.whole.sum_move)[:n]):
        if r.variant not in out:
            out.append(r.variant)
    return out


def _py(x: Optional[GridPart]) -> str:
    """Dönemin işlem/yıl değeri (dönem yoksa —)."""
    return "—" if x is None else f"{x.per_year:.0f}"


def _pc(x: Optional[GridPart]) -> str:
    return "—" if x is None else ("eridi" if x.ruined else _pct(x.cagr))


def _pd(x: Optional[GridPart]) -> str:
    return "—" if x is None else f"%{x.max_dd:.0f}"


def _all_cagr_positive(row: GridRow) -> bool:
    return all(x.cagr > 0 for x in row.parts) if row.parts else row.whole.cagr > 0


def _part_text(x: Optional[GridPart]) -> str:
    if x is None:
        return "—"
    return (f"{x.per_year:4.0f} işlem/yıl, kazanma %{x.win_rate:4.1f}, işlem başı {x.avg_move:+.3f}%, "
            f"yıllık getiri {'eridi' if x.ruined else _pct(x.cagr)}, maks. düşüş %{x.max_dd:.0f}")


def grid_settings(cfg, row: GridRow) -> List[Tuple[str, object]]:
    """Satırı canlıda kurmak için ayarlar: giriş kuralı + (denendiyse) çıkışlar."""
    out = row.variant.settings(cfg)
    over = dict(row.exit_over)
    for attr, env in EXIT_ENV:
        if attr in over:
            out.append((env, over[attr]))
    return out


def format_grid_report(cfg, data: FutData, rows: Sequence[GridRow], base: GridRow, picks: Sequence[GridRow],
                       verified: Sequence[Tuple[GridRow, Evaluation]] = (), exit_rows: Sequence[GridRow] = (),
                       limit: int = 30, stress: Optional[Dict[int, GridRow]] = None) -> str:
    """Izgara raporu: tüm kurallar (işlem sayısına göre), en iyi 2 yapılandırma ve 1 dk doğrulaması."""
    from runtime_settings import format_value
    p = params_from_cfg(cfg)
    days = (data.end_ms - data.start_ms) / DAY_MS
    split = _date(_ms(GRID_SPLIT))
    L = [f"🧮 IZGARA TESTİ (çöküş dönüşü) — {_date(data.start_ms)} → {_date(data.end_ms)} ({days:.0f} gün)", "━" * 30]
    if data.universe_note:
        L.append(f"Coinler: {data.universe_note}")
    else:
        L.append("Coinler: " + "; ".join(f"{_date(s.start_ms)}'den: {','.join(s.coins)}" for s in data.segments))
    L.append(f"Çıkışlar (1. aşama, ayarlarınız): {exit_label(p)} | {p.leverage}x, teminat %{p.margin_usage_pct:g}, "
             f"{p.start_capital:g} USDT | zarardan sonra aynı coine {p.loss_cooldown_min} dk girilmez")
    L.append(f"Seçim dönemi {_date(data.start_ms)} → {split}, doğrulama {split} → {_date(data.end_ms)} (tek hesap "
             f"her dönemde {p.start_capital:g} USDT ile baştan). Tarama 5 dk mumlarla; en iyiler 1 dk mumlarla "
             f"doğrulandı.")
    L.append(f"Hedef: yılda ≥{GRID_TARGET} işlem; kazanma oranı ve kâr (sabit pozisyon toplamı + iki dönemin yıllık "
             f"getirisi) mevcut ayardan düşük olmamalı. ✅ ikisi de korundu, 🟡 biri, ❌ hiçbiri. Kârlı yıl: sabit "
             f"pozisyonla (1-2. aşama) ya da her yıl {p.start_capital:g} USDT ile baştan (3. aşama).")
    L.append("")

    years_b = {id(r): ev.profitable_years for r, ev in verified}

    def row_line(r: GridRow, with_exit: bool = False) -> str:
        s, o = r.part("Seçim"), r.part("Doğrulama")
        win, profit = grid_kept(r, base)
        mark = "✅" if win and profit else "🟡" if win or profit else "❌"
        name = r.variant.label + (f" | {r.exit_label}" if with_exit and r.exit_label else "")
        years = (f"kârlı yıl (her yıl baştan) {years_b[id(r)]}/{r.n_years}" if id(r) in years_b
                 else f"kârlı yıl {r.profitable_years}/{r.n_years}")
        return (f"{mark} {name}\n     {r.whole.per_year:4.0f}/yıl (seçim {_py(s)} / doğr. {_py(o)}) | "
                f"kazanma %{r.whole.win_rate:.1f} | işlem başı {r.whole.avg_move:+.3f}% | toplam {r.whole.sum_move:+.0f}% | "
                f"yıllık {_pc(s)} / {_pc(o)} | düşüş {_pd(s)} / {_pd(o)} | {years}")

    L.append(f"📋 1) GİRİŞ KURALLARI ({len(rows)} kural, ayarlarınızdaki çıkışlarla; işlem sayısına göre, ilk {limit})")
    L.append("   işlem/yıl | kazanma | işlem başı net | toplam (sabit pozisyon, fiyat %) | yıllık getiri seçim / "
             "doğrulama | maks. düşüş seçim / doğrulama")
    L.append("Mevcut: " + row_line(base)[2:])
    for r in sorted(rows, key=lambda r: -r.whole.per_year)[:limit]:
        if r is not base:
            L.append(row_line(r))
    by_conf: Dict[str, List[GridRow]] = {}
    for r in rows:
        conf = dict(r.variant.over).get("futures_confirm")
        if conf:
            by_conf.setdefault(conf, []).append(r)
    if by_conf:
        L.append("")
        L.append("🔎 Teyit mekanizmalarının ortalaması (aynı kurallarda):")
        for conf, group in by_conf.items():
            L.append(f"   {CONFIRM_TR.get(conf, conf):16s}: {len(group)} kural, ortalama "
                     f"{np.mean([g.whole.per_year for g in group]):.0f} işlem/yıl, kazanma "
                     f"%{np.mean([g.whole.win_rate for g in group]):.1f}, işlem başı "
                     f"{np.mean([g.whole.avg_move for g in group]):+.3f}%")
    if exit_rows:
        L.append("")
        L.append(f"🎯 2) ÇIKIŞ DENEMELERİ (en iyi giriş kurallarıyla, {len(exit_rows)} birleşim; toplam kâra göre ilk 12)")
        for r in sorted(exit_rows, key=lambda r: -r.whole.sum_move)[:12]:
            L.append(row_line(r, True))
    base1 = verified[0][0] if verified else base
    stress = stress or {}

    def stress_line(r: GridRow) -> Optional[str]:
        x = stress.get(id(r))
        if x is None:
            return None
        s, o = x.part("Seçim"), x.part("Doğrulama")
        return (f"     kayma ×{GRID_STRESS_SLIP:g} (%{p.slippage_pct * GRID_STRESS_SLIP:g}): kazanma %{x.whole.win_rate:.1f} | "
                f"işlem başı {x.whole.avg_move:+.3f}% | toplam {x.whole.sum_move:+.0f}% | yıllık {_pc(s)} / {_pc(o)}")
    if verified:
        L.append("")
        L.append(f"✔️ 3) 1 DK MUMLARLA DOĞRULAMA (adaylar; seçim buna ve kayma ×{GRID_STRESS_SLIP:g} stresine göre)")
        L.append("Mevcut: " + row_line(base1, True)[2:])
        if stress_line(base1):
            L.append(stress_line(base1))
        for r, _ev in verified[1:]:
            win, profit = grid_kept(r, base1)
            L.append(("✅" if win and profit else "🟡" if win or profit else "❌") + row_line(r, True)[1:])
            if stress_line(r):
                L.append(stress_line(r))
    L.append("")
    L.append("🏆 EN İYİ 2 YAPILANDIRMA" + (" (1 dk doğrulamasıyla)" if verified else " (5 dk tarama)")
             + f": #1 yılda ≥{GRID_TARGET} işlem yapanların en kârlısı, #2 maliyet stresinde ve yıllara göre de "
               "sağlam olanların en çok işlem yapanı")
    if not picks:
        L.append("  Uygun kural bulunamadı.")
    evals = {id(r): ev for r, ev in verified}
    for i, r in enumerate(picks, 1):
        win, profit = grid_kept(r, base1)
        L.append(f"#{i} {r.variant.label}" + (f" | {r.exit_label}" if r.exit_label else ""))
        for name, part in (("Tümü", r.whole), ("Seçim", r.part("Seçim")), ("Doğrulama", r.part("Doğrulama"))):
            L.append(f"   {name + ':':10s} {_part_text(part)}")
        b = base1.whole
        L.append(f"   Mevcut ayara göre: işlem ×{r.whole.trades / max(b.trades, 1):.1f}, kazanma "
                 f"{r.whole.win_rate - b.win_rate:+.1f} puan, toplam kâr {r.whole.sum_move - b.sum_move:+.0f} puan")
        if r.whole.per_year < GRID_TARGET:
            L.append(f"   ⚠️ Yılda {GRID_TARGET} işlem hedefine ulaşmadı.")
        if not (win and profit):
            L.append("   ⚠️ " + ("Kazanma oranı mevcut ayarın altında. " if not win else "")
                     + ("Kâr mevcut ayarın altında (toplam ya da bir dönemin yıllık getirisi)." if not profit else ""))
        if stress_line(r):
            L.append(stress_line(r).replace("     ", "   Stres: ", 1))
        L.append("   Ayarlar: " + " ".join(f"{k}={format_value(v) if v != '' else '(boş)'}"
                                         for k, v in grid_settings(cfg, r)))
        ev = evals.get(id(r))
        if ev is not None:
            ch = ev.chained
            L.append(f"   Tek hesap: {p.start_capital:g} USDT → {_num(ch.final_equity)} USDT, maks. düşüş "
                     f"%{max_drawdown(ch.curve)[0]:.0f} | kârlı yıl {ev.profitable_years}/{len(ev.years)}")
            L.append(f"   {'Yıl':4s}  {'İşlem':>5s}  {'Kazanma':>7s}  {'Getiri (her yıl baştan)':>23s}  "
                     f"{'Maks.düşüş':>10s}")
            for y, res in ev.years:
                st = stats_of(res)
                L.append(f"   {str(y):4s}  {st.trades:5d}  {_pct_cell(st.win_rate, 7)}  {_pct(st.net_pct):>23s}  "
                         f"{_pct_cell(st.max_dd, 10, 0)}")
    L.append("")
    L.append("ℹ️ Kazanma oranına başabaş stopuyla küçük kârla kapanan işlemler de dahildir. Tek pozisyon: aynı anda "
             "çöken coinlerden yalnızca eşiğini en çok aşan alınır. Geçmiş sonuç gelecek garantisi değildir; yeni "
             "ayarı önce test modunda (paper) izleyin.")
    return "\n".join(L)


def run_grid_cli(cfg, days: Optional[int], end_ms: Optional[int], out_dir: Path, full_history: bool = True,
                 exits: bool = True, verify: bool = True,
                 progress: Callable[[str], None] = print) -> Tuple[str, List[GridRow], Optional[Path]]:
    """--izgara: 1) giriş kuralları ızgarası (ayarlarınızdaki çıkışlarla), 2) en iyi giriş kurallarıyla çıkış
    denemeleri, 3) en iyi 2 yapılandırma ve mevcut ayarın 1 dk mumlarla doğrulanması. Rapor `out_dir`'e de yazılır."""
    if cfg.futures_tactic != "crash":
        cfg = replace(cfg, futures_tactic="crash")
    start, end, segments = period(cfg, days, end_ms, full_history)
    data = prepare(cfg, start, end, segments, exit_interval="1m", progress=progress)
    variants = grid_variants(cfg)
    progress(f"🧮 1. aşama: {len(variants)} giriş kuralı (5 dk mumlarla)...")
    rows = grid_search(cfg, data, variants, progress=progress)
    base = rows[0]
    exit_rows: List[GridRow] = []
    if exits:
        tops = [base.variant] + grid_stage2(rows, base)
        progress(f"🎯 2. aşama: {len(tops)} giriş kuralı × {len(grid_exits(cfg))} çıkış...")
        exit_rows = grid_search(cfg, data, tops, progress=progress, exits=grid_exits(cfg))
    picks = (grid_candidates(exit_rows, base) if exit_rows else []) or grid_pick(list(rows), base, n=GRID_VERIFY)
    if not verify:
        picks = grid_pick(list(rows) + exit_rows, base)
    verified: List[Tuple[GridRow, Evaluation]] = []
    stress: Dict[int, GridRow] = {}
    if verify:                  # 5 dk mumlarda mum içi sıra varsayımı başabaşı iyimser gösterebilir: seçim 1 dk ile
        windows = None
        split = _ms(GRID_SPLIT)
        found: Dict[str, List[FutSignal]] = {}
        for r in [base] + list(picks):
            c2 = r.variant.cfg(cfg)
            p2 = params_from_cfg(c2, **dict(r.exit_over))
            progress(f"✔️ 1 dk doğrulama: {r.variant.label} {r.exit_label}".rstrip())
            if r.variant.label not in found:                    # Aynı giriş kuralının sinyalleri bir kez bulunur
                found[r.variant.label] = find_signals(c2, data)[0]
            sigs = found[r.variant.label]
            windows = load_windows(data, sigs, max(MAX_WINDOW_MIN, max(p2.hold_min, p2.extra_hold_min,
                                                                     p2.wave_hold_min) + 10), cache=windows)
            r1 = grid_row(r.variant, p2, data, sigs, windows, split)
            r1.exit_label, r1.exit_over = r.exit_label, r.exit_over
            verified.append((r1, evaluate(p2, data, sigs, windows)))
            # Maliyet stresi: işlem başı kâr payı inceldikçe gerçek kayma sonucu daha çok bozar
            stress[id(r1)] = grid_row(r.variant, replace(p2, slippage_pct=p2.slippage_pct * GRID_STRESS_SLIP), data,
                                      sigs, windows, split)
        base1, base_ev = verified[0]
        years = {id(r): ev.profitable_years for r, ev in verified}
        picks = grid_pick([r for r, _ev in verified], base1,
                          robust=lambda r: (all(grid_kept(stress[id(r)], stress[id(base1)]))
                                            and years[id(r)] >= base_ev.profitable_years))
    report = format_grid_report(cfg, data, rows, base, picks, verified, exit_rows, stress=stress)
    path: Optional[Path] = out_dir / f"vadeli_izgara_{datetime.now():%Y%m%d-%H%M}.txt"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        report += f"\n📄 Rapor dosyası: {path}"
    except OSError as exc:
        report += f"\n⚠️ Rapor dosyası yazılamadı: {exc}"
        path = None
    return report, list(rows) + exit_rows, path


# ===================================================================== modül ızgarası (--modul-izgara, Faz 3)
MODULE_UNIVERSES = (20, 120, 150)               # Denenen evrenler: en hacimli N köklü coin + listeniz
MODULE_TARGET = 1000                            # Hedef: yılda 1000+ işlem (günde ~3)
MODULE_PUMP = "1:6,4:10,12:15"                  # Pompa kuralı (ayarlarınızda yoksa)
MODULE_OFF = (("futures_pump_rules", ""), ("futures_bb", "off"), ("futures_breakout", False))
CRASH_OFF = (("futures_crash_hours", 1), ("futures_crash_drop_pct", 50.0), ("futures_crash_extra", ""),
             ("futures_crash_z", 0.0), ("futures_confirm", "close"))     # Çöküş fiilen kapalı (1 saatte %50)


def module_variants(cfg) -> List[GridVariant]:
    """Faz 3: çöküş (ayarlarınız) + pompa short / Bollinger + RSI / hacimli kırılım tek tek ve VEYA ile birlikte."""
    pump = cfg.futures_pump_rules or MODULE_PUMP
    bb = cfg.futures_bb if cfg.futures_bb != "off" else "both"

    def v(label, *parts):
        over = dict(MODULE_OFF)
        for part in parts:
            over.update(dict(part))
        return GridVariant(label, tuple(over.items()))
    P, B, K = (("futures_pump_rules", pump),), (("futures_bb", bb),), (("futures_breakout", True),)
    S = (("futures_sides", "short"),)
    return [v("Çöküş (ayarlarınız): " + rule_label(cfg)),
            v(f"Yalnız pompa short ({pump})", P, S),
            v(f"Yalnız Bollinger + RSI ({bb})", CRASH_OFF, B),
            v("Yalnız Bollinger long", CRASH_OFF, (("futures_bb", "long"),)),
            v("Yalnız Bollinger short", CRASH_OFF, (("futures_bb", "short"),)),
            v("Yalnız hacimli kırılım", CRASH_OFF, K),
            v("Çöküş + pompa", P), v("Çöküş + Bollinger", B), v("Çöküş + kırılım", K),
            v("Çöküş + pompa + kırılım", P, K),
            v("HEPSİ: çöküş + pompa + Bollinger + kırılım", P, B, K)]


@dataclass
class ModuleUniverse:
    n: int
    note: str
    rows: List[Tuple[GridRow, GridRow]]          # (normal, kayma stresi) her kural için
    verified: List[Tuple[GridRow, Evaluation]] = field(default_factory=list)


def module_universe(cfg, n: int, start: int, end: int, segments, verify: bool,
                    progress: Callable[[str], None]) -> ModuleUniverse:
    c_u = replace(cfg, futures_tactic="crash", futures_universe=n,
                  futures_universe_with_list=True if n > 0 else cfg.futures_universe_with_list)
    data = prepare(c_u, start, end, segments, exit_interval="1m", progress=progress)
    variants = module_variants(c_u)
    p = params_from_cfg(c_u)
    stress = f"kayma ×{GRID_STRESS_SLIP:g}"
    progress(f"🧩 Evren {n or 'liste'}: {len(variants)} kural × (normal, {stress}) (5 dk mumlarla)...")
    rows = grid_search(c_u, data, variants, progress=progress,
                       exits=[("", {}), (stress, {"slippage_pct": p.slippage_pct * GRID_STRESS_SLIP})])
    pairs = [(rows[i], rows[i + 1]) for i in range(0, len(rows), 2)]
    out = ModuleUniverse(n, data.universe_note or "liste", pairs)
    if verify:                                   # 1 dk doğrulama: çöküş ve en çok işlem yapan kârlı birleşim
        base = pairs[0][0]
        best = max((r for r, _x in pairs[1:] if r.whole.sum_move > 0), key=lambda r: r.whole.per_year,
                   default=None)
        windows = None
        for r in [base] + ([best] if best is not None else []):
            c2 = r.variant.cfg(c_u)
            progress(f"✔️ 1 dk doğrulama ({n or 'liste'}): {r.variant.label}")
            sigs, _f = find_signals(c2, data)
            windows = load_windows(data, sigs, window_minutes(c2), cache=windows)
            r1 = grid_row(r.variant, params_from_cfg(c2), data, sigs, windows, _ms(GRID_SPLIT))
            out.verified.append((r1, evaluate(params_from_cfg(c2), data, sigs, windows)))
    return out


def module_hit(row: GridRow, stressed: GridRow) -> bool:
    """Hedef: yılda ≥ MODULE_TARGET işlem, kârlı ve kayma stresinde de her dönemde yıllık getiri pozitif."""
    return (row.whole.per_year >= MODULE_TARGET and row.whole.sum_move > 0 and _all_cagr_positive(row)
            and _all_cagr_positive(stressed))


def format_module_report(cfg, start_ms: int, end_ms: int, unis: Sequence[ModuleUniverse]) -> str:
    p = params_from_cfg(cfg)
    days = (end_ms - start_ms) / DAY_MS
    split = _date(_ms(GRID_SPLIT))
    L = [f"🧩 MODÜL IZGARASI (Faz 3) — {_date(start_ms)} → {_date(end_ms)} ({days:.0f} gün)", "━" * 30,
         f"Çıkışlar sabit (ayarlarınız): {exit_label(p)} | {p.leverage}x, teminat %{p.margin_usage_pct:g}, "
         f"{p.start_capital:g} USDT; tek pozisyon, aynı anda sinyallerde öncelik çöküş > pompa > kırılım > Bollinger.",
         f"Seçim dönemi → {split}, doğrulama {split} →. Tarama 5 dk mumlarla (başabaşta iyimser), seçilenler 1 dk ile "
         f"doğrulandı. Hedef: yılda ≥{MODULE_TARGET} işlem, kâr ve kayma ×{GRID_STRESS_SLIP:g} stresinde de kâr.", ""]
    for u in unis:
        base, base_x = u.rows[0]
        L.append(f"🪙 Evren: {u.note}")
        L.append("   işlem/yıl | kazanma | işlem başı | toplam (sabit poz.) | yıllık seçim / doğr. | maks. düşüş | "
                 f"kayma ×{GRID_STRESS_SLIP:g}: işlem başı, yıllık")
        for r, x in u.rows:
            s, o = r.part("Seçim"), r.part("Doğrulama")
            xs, xo = x.part("Seçim"), x.part("Doğrulama")
            ok = module_hit(r, x)
            mark = "✅" if ok else "❌" if r.whole.avg_move <= 0 else "🟡"
            L.append(f"{mark} {r.variant.label}\n     {r.whole.per_year:5.0f} | %{r.whole.win_rate:4.1f} | "
                     f"{r.whole.avg_move:+.3f}% | {r.whole.sum_move:+6.0f}% | {_pc(s)} / {_pc(o)} | "
                     f"{_pd(s)} / {_pd(o)} | {x.whole.avg_move:+.3f}%, {_pc(xs)} / {_pc(xo)}")
        for r1, ev in u.verified:
            ch = ev.chained
            L.append(f"   ✔️ 1 dk: {r1.variant.label}: {r1.whole.per_year:.0f} işlem/yıl, kazanma %{r1.whole.win_rate:.1f}, "
                     f"işlem başı {r1.whole.avg_move:+.3f}%, yıllık {_pc(r1.part('Seçim'))} / "
                     f"{_pc(r1.part('Doğrulama'))}, maks. düşüş %{max_drawdown(ch.curve)[0]:.0f}, kârlı yıl "
                     f"{ev.profitable_years}/{len(ev.years)}")
            L.append("      yıl: " + " | ".join(f"{y} {stats_of(res).net_pct:+.0f}%" for y, res in ev.years))
        L.append("")
    hits = [(u, r) for u in unis for r, x in u.rows if module_hit(r, x)]
    if hits:
        L.append(f"🏁 Yılda {MODULE_TARGET}+ işlemle kârlı ve stresi geçen: "
                 + "; ".join(f"{r.variant.label} ({u.n or 'liste'})" for u, r in hits))
    else:
        L.append(f"🏁 Yılda {MODULE_TARGET}+ işleme ulaşıp kâr eden ve kayma ×{GRID_STRESS_SLIP:g} stresini geçen "
                 f"birleşim YOK. Ek modüller işlem sayısını artırıyor ama işlem başı sonuç maliyetin altında kalıyor.")
    L.append("ℹ️ Geçmiş sonuç gelecek garantisi değildir; yeni modülü önce test modunda (paper) izleyin.")
    return "\n".join(L)


def run_module_grid_cli(cfg, days: Optional[int], end_ms: Optional[int], out_dir: Path, full_history: bool = True,
                        universes: Sequence[int] = MODULE_UNIVERSES, verify: bool = True,
                        progress: Callable[[str], None] = print) -> Tuple[str, List[ModuleUniverse], Optional[Path]]:
    """--modul-izgara: her evrende (en hacimli N köklü coin + liste) çöküş ve ek modüller, sabit çıkışlarla."""
    start, end, segments = period(cfg, days, end_ms, full_history)
    unis = [module_universe(cfg, n, start, end, segments, verify, progress) for n in universes]
    report = format_module_report(cfg, start, end, unis)
    path: Optional[Path] = out_dir / f"vadeli_modul_izgara_{datetime.now():%Y%m%d-%H%M}.txt"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        report += f"\n📄 Rapor dosyası: {path}"
    except OSError as exc:
        report += f"\n⚠️ Rapor dosyası yazılamadı: {exc}"
        path = None
    return report, unis, path


# ===================================================================== Faz 4 ızgarası
# Faz 4 (--faz4-izgara): limit (maker) giriş, fonlama sıkışması, trend içi düzeltme ve günlük "en hacimli 30" evreni;
# yalnızca LONG, çıkışlar sabit (ayarlarınız), piyasa / limit giriş × kayma ×1 / ×3.
PHASE4_UNIVERSES = (
    ("ayarlarınız", {}),
    ("günlük en hacimli 30 (≥30 gün)", (("futures_universe", 30), ("futures_universe_mode", "daily"),
                                        ("futures_universe_min_age_days", 30), ("futures_universe_with_list", False))),
    ("günlük en hacimli 30 (≥3 yıl)", (("futures_universe", 30), ("futures_universe_mode", "daily"),
                                       ("futures_universe_min_age_days", 1095), ("futures_universe_with_list", False))),
)
PHASE4_OFF = MODULE_OFF + (("futures_funding", False), ("futures_pullback", False), ("futures_sides", "long"))
PHASE4_OFFSET = 0.05            # Limit giriş denemesi: ayarlarınızda limit yoksa bid'in %0.05 altı


def phase4_variants(cfg) -> List[GridVariant]:
    """Faz 4: çöküş (ayarlarınız) + fonlama / düzeltme tek tek ve birlikte; hepsi yalnızca long."""
    def v(label, *parts):
        over = dict(PHASE4_OFF)
        for part in parts:
            over.update(dict(part))
        return GridVariant(label, tuple(over.items()))
    F, P = (("futures_funding", True),), (("futures_pullback", True),)
    fund = (f"fonlama ≤ %{cfg.futures_funding_rate_pct:g} + "
            + ",".join(f"{h}s %{p:g}" for h, p in (futures_funding_rules(cfg) or ((1, 5.0),))))
    pull = f"1s EMA{cfg.futures_pullback_ema} üstü + RSI < {cfg.futures_pullback_rsi:g}"
    return [v("Çöküş (ayarlarınız): " + rule_label(cfg)), v(f"Çöküş + fonlama ({fund})", F),
            v(f"Çöküş + düzeltme ({pull})", P), v("Çöküş + fonlama + düzeltme", F, P),
            v(f"Yalnız fonlama ({fund})", CRASH_OFF, F), v(f"Yalnız düzeltme ({pull})", CRASH_OFF, P)]


def phase4_entries(cfg) -> List[Tuple[str, Dict[str, object]]]:
    """Giriş emri × kayma: piyasa, piyasa ×3, limit (maker), limit ×3."""
    p = params_from_cfg(cfg)
    offset = cfg.futures_limit_offset_pct if cfg.futures_entry_order == "limit" else PHASE4_OFFSET
    lim = {"entry_limit": True, "limit_offset_pct": offset, "limit_ttl_min": cfg.futures_limit_ttl_min}
    x3 = {"slippage_pct": p.slippage_pct * GRID_STRESS_SLIP}
    return [("piyasa", {"entry_limit": False}), (f"piyasa ×{GRID_STRESS_SLIP:g}", {"entry_limit": False, **x3}),
            (f"limit %{offset:g}", lim), (f"limit ×{GRID_STRESS_SLIP:g}", {**lim, **x3})]


@dataclass
class Phase4Universe:
    label: str
    note: str
    rows: List[List[GridRow]]                      # kural başına [piyasa, piyasa ×3, limit, limit ×3]
    verified: List[Tuple[str, GridRow, GridRow, Evaluation]] = field(default_factory=list)


def phase4_universe(cfg, label: str, over, start: int, end: int, segments, verify: bool,
                    progress: Callable[[str], None]) -> Phase4Universe:
    c_u = replace(cfg, futures_tactic="crash", **dict(over))
    data = prepare(c_u, start, end, segments, exit_interval="1m", progress=progress)
    variants = phase4_variants(c_u)
    entries = phase4_entries(c_u)
    progress(f"⚡ Evren {label}: {len(variants)} kural × {len(entries)} giriş / kayma (5 dk mumlarla)...")
    rows = grid_search(c_u, data, variants, progress=progress, exits=entries)
    groups = [rows[i:i + len(entries)] for i in range(0, len(rows), len(entries))]
    out = Phase4Universe(label, data.universe_note or "liste", groups)
    if verify:                      # 1 dk doğrulama: çöküş (piyasa / limit) ve çöküş + düzeltme (piyasa)
        windows = None
        for vi, ei in ((0, 0), (0, 2), (2, 0)):
            r = groups[vi][ei]
            name, ov = entries[ei]
            c2 = r.variant.cfg(c_u)
            progress(f"✔️ 1 dk doğrulama ({label}): {r.variant.label} / {name}")
            sigs, _f = find_signals(c2, data)
            windows = load_windows(data, sigs, window_minutes(c2), cache=windows)
            p = params_from_cfg(c2, **ov)
            r1 = grid_row(r.variant, p, data, sigs, windows, _ms(GRID_SPLIT))
            r3 = grid_row(r.variant, replace(p, slippage_pct=p.slippage_pct * GRID_STRESS_SLIP), data, sigs, windows,
                          _ms(GRID_SPLIT))
            out.verified.append((name, r1, r3, evaluate(p, data, sigs, windows)))
    return out


def format_phase4_report(cfg, start_ms: int, end_ms: int, unis: Sequence[Phase4Universe]) -> str:
    p = params_from_cfg(cfg)
    days = (end_ms - start_ms) / DAY_MS
    split = _date(_ms(GRID_SPLIT))
    L = [f"⚡ FAZ 4 IZGARASI — {_date(start_ms)} → {_date(end_ms)} ({days:.0f} gün, yalnızca LONG)", "━" * 30,
         f"Çıkışlar sabit (ayarlarınız): {exit_label(p)} | {p.leverage}x, teminat %{p.margin_usage_pct:g}, "
         f"{p.start_capital:g} USDT; tek pozisyon, öncelik çöküş > fonlama > düzeltme.",
         f"Komisyon: piyasa (taker) %{p.taker_fee * 100:g}, limit (maker) %{p.maker_fee * 100:g}; kayma piyasa "
         f"emirlerinde %{p.slippage_pct:g} (stres ×{GRID_STRESS_SLIP:g}). Limit emir: sinyal anındaki fiyatın "
         f"altına, {cfg.futures_limit_ttl_min} dk içinde fiyat limitin %{SimParams.limit_through_pct:g} ötesine "
         f"geçerse dolar, geçmezse işlem yok.",
         f"Seçim dönemi → {split}, doğrulama {split} →. Tarama 5 dk mumlarla (başabaşta ve limit dolumunda iyimser), "
         f"öne çıkanlar 1 dk ile doğrulandı.", ""]
    for u in unis:
        L.append(f"🪙 Evren: {u.label} — {u.note}")
        L.append("   işlem/yıl | kazanma | işlem başı | yıllık seçim / doğr. | maks. düşüş || aynısı kayma ×3 "
                 "(işlem başı, yıllık)")
        for group in u.rows:
            L.append(f"• {group[0].variant.label}")
            for r, x in ((group[0], group[1]), (group[2], group[3])):
                s, o = r.part("Seçim"), r.part("Doğrulama")
                xs, xo = x.part("Seçim"), x.part("Doğrulama")
                mark = "✅" if r.whole.avg_move > 0 and _all_cagr_positive(r) and _all_cagr_positive(x) else (
                    "❌" if r.whole.avg_move <= 0 else "🟡")
                L.append(f"   {mark} {r.exit_label:10s} {r.whole.per_year:5.0f} | %{r.whole.win_rate:4.1f} | "
                         f"{r.whole.avg_move:+.3f}% | {_pc(s)} / {_pc(o)} | {_pd(s)} / {_pd(o)} || "
                         f"{x.whole.avg_move:+.3f}%, {_pc(xs)} / {_pc(xo)}")
        for name, r1, r3, ev in u.verified:
            ch = ev.chained
            L.append(f"   ✔️ 1 dk: {r1.variant.label} / {name}: {r1.whole.per_year:.0f} işlem/yıl, kazanma "
                     f"%{r1.whole.win_rate:.1f}, işlem başı {r1.whole.avg_move:+.3f}% (×{GRID_STRESS_SLIP:g} kayma: "
                     f"{r3.whole.avg_move:+.3f}%), yıllık {_pc(r1.part('Seçim'))} / {_pc(r1.part('Doğrulama'))}, maks. "
                     f"düşüş %{max_drawdown(ch.curve)[0]:.0f}, kârlı yıl {ev.profitable_years}/{len(ev.years)}")
            L.append("      yıl: " + " | ".join(f"{y} {stats_of(res).net_pct:+.0f}%" for y, res in ev.years))
        L.append("")
    L.append("ℹ️ Geçmiş sonuç gelecek garantisi değildir; yeni ayarı önce test modunda (paper) izleyin.")
    return "\n".join(L)


def run_phase4_grid_cli(cfg, days: Optional[int], end_ms: Optional[int], out_dir: Path, full_history: bool = True,
                        universes=PHASE4_UNIVERSES, verify: bool = True,
                        progress: Callable[[str], None] = print) -> Tuple[str, List[Phase4Universe], Optional[Path]]:
    """--faz4-izgara: evren × (çöküş, fonlama, düzeltme) × (piyasa / limit giriş) × kayma, yalnızca long."""
    start, end, segments = period(cfg, days, end_ms, full_history)
    unis = [phase4_universe(cfg, label, over, start, end, segments, verify, progress) for label, over in universes]
    report = format_phase4_report(cfg, start, end, unis)
    path: Optional[Path] = out_dir / f"vadeli_faz4_izgara_{datetime.now():%Y%m%d-%H%M}.txt"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        report += f"\n📄 Rapor dosyası: {path}"
    except OSError as exc:
        report += f"\n⚠️ Rapor dosyası yazılamadı: {exc}"
        path = None
    return report, unis, path


# ===================================================================== rapor
def _pct(v: Optional[float], sign: bool = True) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if abs(v) >= 1e9:
        return f"{v:+.1e}%" if sign else f"{v:.1e}%"
    if abs(v) >= 1e5:                       # "+100512%": satırlar karşılaştırılabilsin (eski "+1.0e+05%" farkı gizliyordu)
        return f"{v:+.0f}%" if sign else f"{v:.0f}%"
    return f"{v:+.1f}%" if sign else f"{v:.1f}%"


def _pf(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def _pct_cell(v: float, w: int = 6, d: int = 1) -> str:
    """Tablo hücresi: sağa hizalı "%47.4" (eski "% 47.4" yerine)."""
    return f"{'%' + format(v, f'.{d}f'):>{w}s}"


def cooldown_text(p: SimParams) -> str:
    """Taktik satırının bekleme kısmı; bekleme yoksa boş (eski "işlemden sonra 0 dk" yazılmaz)."""
    loss = max(p.symbol_cooldown_min, p.loss_cooldown_min)
    if p.symbol_cooldown_min and loss > p.symbol_cooldown_min:
        return f"; aynı coine işlemden sonra {p.symbol_cooldown_min} dk, zarardan sonra {loss} dk girilmez"
    if p.symbol_cooldown_min:
        return f"; aynı coine işlemden sonra {p.symbol_cooldown_min} dk girilmez"
    return f"; zarardan sonra aynı coine {loss} dk girilmez" if loss else ""


def _num(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


EVAL_HEAD = f"{'A) tek hesap':>16s} │ {'B) kârlı yıl':12s} │ C) sabit pozisyon"


def _eval_line(ev: Evaluation) -> str:
    """Tablo satırının sonuç kısmı (EVAL_HEAD ile hizalı): A'nın sonucu, B'de kârlı yıl, C'nin işlem başı gücü."""
    ch = ev.chained
    ruined = f"eridi {_date(ch.ruined_ms)}" if ch.ruined_ms else _pct(ch.net_pct)
    years = f"{ev.profitable_years}/{len(ev.years)}"
    return (f"{ruined:>16s} │ {years:^12s} │ {len(ev.trades):5d} işlem, kazanma %{ev.win_rate:4.1f}, işlem başı "
            f"{ev.avg_move:+.3f}%")


LEV_LABEL_W = 17
LEV_HEAD = (f"  {'Kaldıraç':{LEV_LABEL_W}s}{'12 ay ortanca':>14s}{'%60+ düşüş':>12s}{'kill-switch':>13s}  │  "
            f"{'A) tek hesap':<20s}{'A düşüş':>8s}  B en kötü yıl")


def _lev_line(ev: Evaluation, roll: Optional[dict] = None) -> str:
    """Kaldıraç tablosu satırı (etiketten sonrası, LEV_HEAD ile hizalı): her ayın 1'inde başlayan hesapların özeti (12 ay
    ortanca getiri, %60'ı aşan düşüş, kill-switch'in durdurduğu hesap; kill-switch uygulanır), sonra A'nın sonucu ve en
    büyük düşüşü (A kill-switch uygulamaz: durduracağı gün yazılır) ve B'de en kötü yıl içi düşüş."""
    ch = ev.chained
    a = f"eridi {_date(ch.ruined_ms)}" if ch.ruined_ms else _pct(ch.net_pct)
    if ch.kill_ms and not ch.ruined_ms:
        a += f" (durur {_date(ch.kill_ms)})"
    worst = max(((max_drawdown(r.curve)[0], y) for y, r in ev.years), default=(0.0, None))
    med, deep, kills = ((_pct(roll["median"]), f"{roll['dd60']} / {roll['n']}", str(roll["kills"])) if roll
                        else ("—", "—", "—"))
    return (f"{med:>14s}{deep:>12s}{kills:>13s}  │  {a:<20s}{'%' + format(max_drawdown(ch.curve)[0], '.1f'):>8s}  "
            f"%{worst[0]:.1f}" + (f" ({worst[1]})" if worst[1] else ""))


def cascade_label(p: SimParams) -> str:
    """Dalga modu / yalnız çöküş büyüklüğü / Faz 6 dalgada yeniden giriş özeti (tek satır)."""
    return "; ".join(cascade_parts(p))


def cascade_lines(p: SimParams) -> List[str]:
    """Rapor başlığındaki dalga modu bloğu: ilk satır dalga modu, sonraki kurallar birer alt satırda."""
    parts = cascade_parts(p)
    if not parts:
        return []
    head, rest = (parts[0], parts[1:]) if parts[0].startswith("🌊") else ("📐 Pozisyon boyu kuralları:", parts)
    return [head] + [f"   · {x}" for x in rest]


def cascade_parts(p: SimParams) -> List[str]:
    """cascade_label / cascade_lines'ın parçaları (kural başına bir metin)."""
    parts = []
    if p.max_positions > 1 and p.extra_size_pct >= 100:
        parts.append(f"🌊 Dalga modu: en fazla {p.max_positions} pozisyon (her biri bakiyenin "
                     f"{_pct_of(p.margin_usage_pct / p.max_positions, 1)} teminat × {p.leverage}x); ek pozisyon yalnızca son 1 "
                     f"saatte ≥{p.extra_breadth} coin %3+ düşerken")
    elif p.max_positions > 1:
        first, each = p.margin_usage_pct * slot_share(p, 0), p.margin_usage_pct * slot_share(p, 1)
        parts.append(f"🌊 Dalga modu: en fazla {p.max_positions} pozisyon (ilk pozisyon bakiyenin {_pct_of(first, 1)} teminat "
                     f"× {p.leverage}x, ek pozisyonlar {_pct_of(each, 1)}: ilkinin {_pct_of(p.extra_size_pct, 1)}); ek pozisyon "
                     f"yalnızca son 1 saatte ≥{p.extra_breadth} coin %3+ düşerken")
    if p.isolated_size_pct < 100:
        parts.append(f"yalnız çöküşte (≤{p.isolated_breadth - 1} coin düşüyor) pozisyon %{p.isolated_size_pct:g}")
    if p.recent_size_pct < 100:
        parts.append(f"çöküş durulmuşsa (girişten önceki 15 dk'da düşüş %{tr_from_num(p.recent_drop_pct)} az) pozisyon "
                     f"%{p.recent_size_pct:g}")
    if p.reentry_breadth > 0:
        parts.append(f"dalgada yeniden giriş: ≥{p.reentry_breadth} coin düşerken zarar sonrası bekleme yok")
    if phase8_active(p):
        parts.append(f"ek pozisyonlarda başabaş +%{p.extra_be_trigger_pct:g} (Faz 8; ilk pozisyonda "
                     f"+%{p.be_trigger_pct:g})")
    return parts


FIRST_BREADTH_BUCKETS = ((0, 2), (2, 5), (5, 8), (8, None))   # İlk pozisyonlar: genişlik kovaları (alt, üst hariç)


def breadth_breakdown(trades: Sequence[FutTrade], p: SimParams) -> str:
    """Girişteki genişliğe göre işlem başı sonuç (sabit tutar): yalnız çöküş (genişlik < FUTURES_ISOLATED_BREADTH) /
    ara / piyasa geneli dalga (8+ coin)."""
    lo = max(1, int(p.isolated_breadth))
    wave = max(8, lo)
    groups = [(f"yalnız çöküş (0-{lo - 1} coin)" if lo > 1 else "yalnız çöküş (0 coin)", lambda b: b < lo)]
    if lo < 8:
        groups.append((f"{lo}-7 coin", lambda b: lo <= b < 8))
    groups.append((f"dalga ({wave}+ coin)", lambda b: b >= wave))
    out = []
    for name, ok in groups:
        mv = [net_move_pct(t) for t in trades if ok(t.breadth)]
        if mv:
            out.append(f"{name}: {len(mv)} işlem, ort. {np.mean(mv):+.3f}%")
    text = "Genişliğe göre (son 1 saatte %3+ düşen coin, sabit tutar): " + " | ".join(out)
    if p.max_positions > 1:
        extra = [t for t in trades if t.slot > 1]
        if extra:
            text += f" | ek pozisyonlar: {len(extra)} işlem, ort. {np.mean([net_move_pct(t) for t in extra]):+.3f}%"
    for name, group in (("dalgada yeniden giriş", [t for t in trades if t.reentry]),
                        ("dalga eşiği sinyali", [t for t in trades if t.wave_rule])):
        if group:
            text += f" | {name}: {len(group)} işlem, ort. {np.mean([net_move_pct(t) for t in group]):+.3f}%"
    return text


def first_breadth_line(trades: Sequence[FutTrade], p: SimParams) -> Optional[str]:
    """Dalga modunda ilk pozisyonların (hiç pozisyon yokken açılan, en büyük boy) girişteki genişliğe göre işlem başı
    sonucu (sabit tutar). Rapor 12 araştırması: 2-4 coin düşerken açılan ilk pozisyonlar riskin büyüğünü taşıyıp az
    kazandırıyordu (FUTURES_ISOLATED_BREADTH ile küçültülebilir)."""
    if p.max_positions <= 1:
        return None
    first = [t for t in trades if t.slot == 1]
    parts = []
    for a, b in FIRST_BREADTH_BUCKETS:
        mv = [net_move_pct(t) for t in first if t.breadth >= a and (b is None or t.breadth < b)]
        if mv:
            name = f"{a}+ coin" if b is None else f"{a}-{b - 1} coin"
            parts.append(f"{name}: {len(mv)} işlem, ort. {np.mean(mv):+.3f}%")
    if not parts:
        return None
    size = (f"{tr_from(p.isolated_breadth)} az coin düşerken boy %{p.isolated_size_pct:g}"
            if p.isolated_size_pct < 100 else "hepsi tam boy")
    return f"İlk pozisyonlar genişliğe göre (sabit tutar; {size}): " + " | ".join(parts)


RECENT_DROP_BUCKETS = ((None, 1.0), (1.0, 2.4), (2.4, 5.5), (5.5, None))   # Son 15 dk düşüş kovaları (%)


def recent_drop_line(trades: Sequence[FutTrade], p: SimParams) -> Optional[str]:
    """Girişten önceki 15 dk'daki düşüşe göre işlem başı sonuç (sabit tutar). Rapor 14 araştırması: çöküş durulmuşken
    (son 15 dk'da az düşüş) açılan işlemler her yıl ~%0 getirdi, hâlâ sert düşerken açılanlar en kârlısıydı
    (FUTURES_RECENT_SIZE_PCT ile küçültülebilir)."""
    known = [t for t in trades if t.drop15 == t.drop15]
    if not known:
        return None
    parts = []
    for a, b in RECENT_DROP_BUCKETS:
        mv = [net_move_pct(t) for t in known if (a is None or t.drop15 >= a) and (b is None or t.drop15 < b)]
        if mv:
            name = f"%{tr_from_num(b)} az" if a is None else f"%{a:g}+" if b is None else f"%{a:g}-{b:g}"
            parts.append(f"{name}: {len(mv)} işlem, ort. {np.mean(mv):+.3f}%")
    size = (f"%{tr_from_num(p.recent_drop_pct)} az düşüşte boy %{p.recent_size_pct:g}" if p.recent_size_pct < 100
            else "hepsi tam boy")
    return f"Girişten önceki 15 dk'daki düşüşe göre (sabit tutar; {size}): " + " | ".join(parts)


BASIS_BUCKETS = ((None, -0.5), (-0.5, -0.1), (-0.1, 0.0), (0.0, None))     # Girişte vadeli / spot farkı kovaları (%)


def basis_breakdown_line(trades: Sequence[FutTrade], p: SimParams) -> Optional[str]:
    """Girişteki vadeli / spot farkına göre işlem başı sonuç (sabit tutar). Rapor 17 araştırması (22 Kasım): vadeli spotun
    belirgin altındayken (likidasyon zincirinde zorla satış) açılan işlemler her yıl ortalamanın çok üstündeydi
    (FUTURES_BASIS_SIZE_PCT ile büyütülebilir). Fark ölçülemediyse (spot verisi yok) satır yok."""
    known = [t for t in trades if t.basis == t.basis]
    if not known:
        return None
    parts = []
    for a, b in BASIS_BUCKETS:
        mv = [net_move_pct(t) for t in known if (a is None or t.basis >= a) and (b is None or t.basis < b)]
        if mv:
            name = (f"−%{-b:g}'ten derin" if a is None else "0 ve üstü" if b is None
                    else f"−%{-a:g} … {'0' if b == 0 else f'−%{-b:g}'}")
            parts.append(f"{name}: {len(mv)} işlem, ort. {np.mean(mv):+.3f}%")
    size = (f"−%{p.basis_deep_pct:g}'ten derinde boy %{p.basis_size_pct:g}" if p.basis_size_pct != 100
            else "hepsi aynı boy")
    return f"Girişteki vadeli / spot farkına göre (sabit tutar; {size}): " + " | ".join(parts)


QUALITY_BUCKETS = ((None, 40), (40, 60), (60, 80), (80, None))   # Kalite puanının 2020-2023 sinyallerindeki dilimi (%)
QUALITY_EDGES = {20: 0.3397, 40: QUALITY_CUTS[40], 60: QUALITY_CUTS[60], 80: QUALITY_CUTS[80]}


def quality_breakdown_line(trades: Sequence[FutTrade], p: SimParams) -> Optional[str]:
    """Sinyal kalitesi puanına göre işlem başı sonuç (sabit tutar). Rapor 18 araştırması (22 Kasım): en zayıf %60'taki
    işlemler ortalamada ~%0, en güçlü %20 her yıl +%0.37 … +%0.54 (FUTURES_QUALITY_SIZE_PCT ile küçültülebilir)."""
    known = [t for t in trades if t.quality == t.quality]
    if not known:
        return None
    parts = []
    for a, b in QUALITY_BUCKETS:
        lo = QUALITY_EDGES[a] if a is not None else -1.0
        hi = QUALITY_EDGES[b] if b is not None else 2.0
        mv = [net_move_pct(t) for t in known if lo < t.quality <= hi]
        if mv:
            name = (f"en zayıf %{b}" if a is None else f"en güçlü %{100 - a}" if b is None else f"%{a}-{b}")
            parts.append(f"{name}: {len(mv)} işlem, ort. {np.mean(mv):+.3f}%")
    size = (f"en zayıf %{p.quality_cut_pct:g}'ta boy %{p.quality_size_pct:g}" if p.quality_size_pct < 100
            else "hepsi aynı boy")
    return f"Sinyal kalitesi puanına göre (sabit tutar; {size}): " + " | ".join(parts)


# ===================================================================== canlı bot karşılaştırması
def read_scan_log(path: Path, start_ms: int, end_ms: int) -> List[dict]:
    """Canlı botun tarama kaydı (FuturesBot, futures_scans_<mod>.jsonl): [start, end) aralığındaki satırlar. Evren ('u')
    yalnızca değişince yazıldığı için her tarama satırına o anki evren eklenir."""
    rows: List[dict] = []
    universe = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("t"), (int, float)):
                    continue
                if "u" in row:
                    universe = row["u"]
                elif universe is not None and "skip" not in row:
                    row["u"] = universe
                if start_ms <= row["t"] < end_ms:
                    rows.append(row)
    except OSError:
        return []
    return rows


def live_status(symbol: str, row: Optional[dict]) -> Tuple[str, str]:
    """Backtestin işlem açtığı mumda canlı bot ne yaptı: (kısa neden, açıklama)."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    if row is None:
        return "kayıt yok", "kayıt yok: canlı bot o anda çalışmıyordu ya da o mumu taramadı"
    if "skip" in row:
        reason = str(row["skip"])
        return reason.split(" (")[0], f"taranmadı: {reason}"
    outcome = (row.get("s") or {}).get(base)
    if outcome is not None:
        if outcome == "açıldı":
            return "açtı", "✅ canlı da açtı"
        return outcome.split(" (")[0].split(":")[0], f"sinyal vardı ama {outcome}"
    if base in (row.get("x") or []):
        return "coin beklemedeydi", "coin beklemedeydi (zarar sonrası bekleme ya da açık pozisyon)"
    universe = row.get("u")
    if universe is not None and base not in universe:
        return "coin evrende yoktu", f"coin canlı evrende yoktu (canlı {len(universe)} coin tarıyordu)"
    if base in (row.get("a") or []):
        return "sinyal çıkmadı", "coin analiz edildi ama canlı mumlarda sinyal çıkmadı"
    return "analiz edilmedi", "coin analiz edilmedi (24 saatlik ön eleme ya da mumu alınamadı)"


LIVE_TIMING_MIN_SCANS = 20    # Şimdi kullanılmayan giriş yolunun zamanlama satırı en az bu kadar taramayla
LIVE_FILL_MIN_SHOWN = 3       # ...ve dolum satırı en az bu kadar girişle gösterilir


def live_timing(cfg, journal: Sequence[dict]) -> List[str]:
    """Canlı botun ölçtüğü giriş gecikmesi (tarama kaydındaki "ms" ve "f" alanları): taramanın mum kapanışından kaç
    saniye sonra başladığı / mumların ne zaman alındığı ve açılan pozisyonların ne zaman, kapanış fiyatına göre kaça
    dolduğu. Hızlı giriş (FUTURES_FAST_ENTRY) açık ve kapalı dönemler ayrı satırlarda. Ölçüm yoksa boş liste."""
    slip = float(getattr(cfg, "futures_slippage_pct", 0.0))
    lag_of = {0: ENTRY_LAG_PCT, 1: ENTRY_LAG_FAST_PCT, 2: FAST_ENTRY_LAG_PCT}     # Backtestin o giriş yolu için payı
    out: List[str] = []

    def mode_of(r: dict) -> int:                  # 2: anlık tarama (o taramada kullanıldı), 1: hızlı giriş, 0: eski yol
        return 2 if r.get("sn") else int(bool(r.get("fast")))
    tags = {0: "hızlı giriş kapalı: ", 1: "hızlı giriş açık: ", 2: "anlık tarama: "}
    modes = sorted({mode_of(r) for r in journal if r.get("ms") or r.get("f")})
    current = 2 if futures_snapshot_on(cfg) else int(bool(getattr(cfg, "futures_fast_entry", False)))
    for fast in modes:
        rows = [r for r in journal if mode_of(r) == fast]
        tag = "" if len(modes) == 1 and not fast else tags[fast]
        scans = [r["ms"] for r in rows if isinstance(r.get("ms"), list) and len(r["ms"]) >= 3]
        few = fast != current and len(modes) > 1           # Şimdi kullanılmayan yol: az örnekli satırı gürültü
        if scans and not (few and len(scans) < LIVE_TIMING_MIN_SCANS):
            st, kl, sg = (float(np.median([m[i] for m in scans])) / 1000 for i in range(3))
            retried = sum(1 for r in rows if r.get("kt"))
            quiet = sum(1 for r in rows if r.get("kq"))
            offsets = [abs(float(r["co"])) for r in rows if isinstance(r.get("co"), (int, float))]
            got = "anlık fiyatlar" if fast == 2 else "mumlar"
            digits = 2 if fast == 2 else 1
            out.append(f"  Tarama zamanlaması ({tag}mum kapanışından sonra, ortanca): başlangıç {st:.{digits}f} sn → "
                       f"{got} alındı {kl:.{digits}f} sn → sinyaller hazır {sg:.{digits}f} sn ({len(scans)} tarama"
                       + (f"; {retried} taramada mumlar yeniden istendi" if retried else "")
                       + (f"; {quiet} taramada sakin coinlerin yeni mumu beklenmedi" if quiet else "")
                       + (f"; Binance ile saat farkı ortanca {np.median(offsets):.0f} ms" if offsets else "") + ")")
        fills = [f for r in rows for f in (r.get("f") or {}).values() if isinstance(f, dict)]
        lags = [float(f.get("x", f.get("r"))) / 1000 for f in fills if f.get("x", f.get("r")) is not None]
        if lags and not (few and len(lags) < LIVE_FILL_MIN_SHOWN):
            slips = [float(f["p"]) for f in fills if f.get("p") is not None]
            text = (f"  Giriş gecikmesi ({tag}mum kapanışı → dolum): ortalama {np.mean(lags):.1f} sn, ortanca "
                    f"{np.median(lags):.1f} sn, en uzun {max(lags):.1f} sn ({len(lags)} giriş)")
            if slips:
                text += (f" | dolum, kapanış fiyatına göre ortalama {np.mean(slips):+.3f}% (backtest varsayımı "
                         f"+%{lag_of[fast] + slip:.2f}: gecikme %{lag_of[fast]:g} + kayma)")
                if len(slips) >= 3:                 # Az girişte tek bir kötü dolum ortalamayı çok oynatır
                    text += f"; ortanca {np.median(slips):+.3f}%, en kötü {max(slips):+.3f}%"
            out.append(text)
    return out + live_snapshot_check(journal)


def live_snapshot_check(journal: Sequence[dict]) -> List[str]:
    """Anlık taramanın (FUTURES_SNAPSHOT_SCAN) canlıdaki kendi doğrulaması (tarama kaydındaki "sv" ve "sx" alanları): bot
    her anlık taramadan sonra, kesinleşen mumlarla anlık fiyatın kapanıştan ne kadar saptığına ve aynı sinyallerin çıkıp
    çıkmadığına bakar. Kayıt yoksa boş liste."""
    out: List[str] = []
    checks = [c for r in journal for c in (r.get("sv") if isinstance(r.get("sv"), list) else [r.get("sv")])
              if isinstance(c, dict) and c.get("n")]
    if checks:
        av = float(np.mean([float(c.get("av", 0.0)) for c in checks]))
        mx = max(float(c.get("mx", 0.0)) for c in checks)
        same = [int(c["ok"]) for c in checks if "ok" in c]
        text = (f"  Anlık tarama doğrulaması ({len(checks)} tarama, sonradan kesinleşen mumlarla): anlık fiyat ile mum "
                f"kapanışı arasındaki fark ortalama %{av:.3f}, en çok %{mx:.3f}")
        if same:
            # Sinyalsiz taramada "aynı" olmak kolaydır; asıl sınav sinyal çıkan taramalardır: ayrıca sayılır
            with_signal = sum(1 for c in checks if c.get("sg"))
            text += (f" | kesinleşmiş mumlarla aynı sinyaller: {sum(same)} / {len(same)} tarama (sinyal çıkan tarama: "
                     f"{with_signal})")
            if sum(same) < len(same):
                text += " (fark, eşiği kıl payı geçen / geçemeyen sinyallerdedir)"
        out.append(text)
    # Nedenler sayılardan arındırılıp gruplanır ("geçmiş mumlar 6 / 24 coinde hazır" ile "8 / 24" aynı neden)
    missed = Counter(re.sub(r"\d+", "N", str(r["sx"]).split(":")[0].strip()) for r in journal if r.get("sx"))
    if missed:
        out.append(f"  Anlık fiyat {sum(missed.values())} taramada kullanılamadı (olağan hızlı yoldan tarandı): "
                   + "; ".join(f"{why}: {n}" for why, n in missed.most_common(3)))
    return out


ACCOUNT_BLOCK_RECENT_MS = 6 * FUT_HOUR_MS    # Hesap modu reddi bundan yeniyse "hâlâ sürüyor olabilir" diye uyarılır
ACCOUNT_BLOCK_STALE_MS = 3 * DAY_MS          # Çözülmüş (sonra pozisyon açılmış) ret bundan eskiyse rapora yazılmaz


def live_account_block(journal: Sequence[dict]) -> Optional[str]:
    """Canlı bot tarama kaydında hesap modu reddi (Binance -4168: hesap o an Çoklu Varlık modunda, yalıtılmış teminat
    reddedildi) var mı: varsa rapora yazılacak satır. Ret eskiyse (kaydın sonundan 6 saatten önce) ya da sonrasında
    pozisyon açılabildiyse yalnızca bilgi verilir: bot bu modu kendisi Tek Varlık moduna alır, eski kayıt sorunun
    sürdüğünü göstermez. Yalnızca kaydın sonuna yakın bir ret "hâlâ engelliyor olabilir" diye uyarılır."""
    hits = [int(r["t"]) for r in journal
            if any("-4168" in str(o) or "Çoklu Varlık" in str(o) for o in (r.get("s") or {}).values())]
    if not hits:
        return None
    last, end = max(hits), max(int(r["t"]) for r in journal)
    when = datetime.fromtimestamp(last / 1000).strftime("%d.%m %H:%M")
    head = (f"{len(hits)} taramada giriş reddedildi (son {when}): Binance hesabı o an Çoklu Varlık (Multi-Assets) "
            f"modundaydı, bu modda yalıtılmış teminat kullanılamaz (-4168)")
    if any(int(r["t"]) > last and "açıldı" in (r.get("s") or {}).values() for r in journal):
        if end - last > ACCOUNT_BLOCK_STALE_MS:          # Çözülmüş ve eski: rapora yazılmaz
            return None
        return f"  ℹ️ {head}. Sonrasında pozisyon açılabilmiş: engel kalkmış."
    if end - last <= ACCOUNT_BLOCK_RECENT_MS:
        return (f"  ⛔ {head}. Ret çok yeni; hesap hâlâ bu moddaysa bot hiçbir sinyali açamaz. Binance → Vadeli → "
                f"Tercihler (⚙️) → Varlık Modu 'Tek Varlık Modu' olmalı (bot bunu kendisi de dener; /tarama'da ⛔ satırı "
                f"varsa engel sürüyordur).")
    return (f"  ℹ️ {head}. O zamandan beri giriş denemesi olmadı. Bot bu modu kendisi Tek Varlık moduna alır; Binance → "
            f"Vadeli → Tercihler → Varlık Modu 'Tek Varlık Modu' görünüyorsa ve /tarama'da ⛔ satırı yoksa sorun "
            f"kalmamıştır.")


def live_comparison(cfg, data: FutData, trades: Sequence[FutTrade], path: Optional[Path] = None,
                    limit: int = 15) -> List[str]:
    """Backtestin (tek hesap) işlemlerini canlı botun aynı mumdaki tarama kaydıyla karşılaştırır: canlı da açtı mı,
    açmadıysa neden (bot taramadı / coin evrende yoktu / sinyal çıkmadı / yer yoktu...). Kayıt yoksa boş."""
    mode_key = getattr(cfg, "trading_mode", "paper")
    path = Path(path) if path is not None else scan_log_path(DATA_DIR, mode_key)
    mode = "test modu" if mode_key == "paper" else "canlı"

    def when(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000).strftime("%d.%m %H:%M")
    journal = read_scan_log(path, data.start_ms, data.end_ms + FUT_BAR_MS)
    if not journal:
        head = f"🔍 CANLI BOT KARŞILAŞTIRMASI ({mode} bot kaydı)"
        every = read_scan_log(path, 0, 2 ** 62)
        if not every:
            other = scan_log_path(path.parent, "live" if mode_key == "paper" else "paper")
            note = (f"  Not: {other.name} dosyasında kayıt var; bot başka modda çalışıyor olabilir (TRADING_MODE)."
                    if read_scan_log(other, 0, 2 ** 62) else "")
            return [head, "  Canlı tarama kaydı henüz yok: bot güncellendikten sonra her 5 dk'da bir kayıt tutar."]\
                + ([note] if note else [])
        first = min(int(r["t"]) for r in every)
        if first >= data.end_ms:
            return [head, f"  Kayıt {when(first)}'de başladı ama backtest dönemi {when(data.end_ms)}'de bitiyor (arşivde "
                          f"bitmiş günler var): karşılaştırma yarınki backtestte görünür."] + live_timing(cfg, every)
        return [head, "  Bu backtest döneminde canlı tarama kaydı yok."]
    by_t = {int(r["t"]): r for r in journal}
    first, last = min(by_t), max(by_t)
    step = MIN_MS if any(t % FUT_BAR_MS for t in by_t) else FUT_BAR_MS      # Dakikalık taramada satırlar dakikada bir
    expected = (last - first) // step + 1
    skips = Counter(str(r["skip"]).split(" (")[0] for r in journal if "skip" in r)
    scanned = sum(1 for r in journal if "skip" not in r)
    L = [f"🔍 CANLI BOT KARŞILAŞTIRMASI ({mode} bot kaydı: {when(first)} → {when(last)})",
         f"  Kayıtlı {'tarama (dakikada bir)' if step == MIN_MS else '5 dk mum'}: {len(by_t)} / {expected} (bot "
         f"kapalıyken kayıt olmaz) | taranan {scanned}"
         + (" | taranmayan: " + ", ".join(f"{k}: {v}" for k, v in skips.most_common(4)) if skips else "")]
    last_u = next((r["u"] for r in reversed(journal) if r.get("u") is not None), None)
    if last_u is not None:
        bt = [c for c in (data.segments[-1].coins if data.segments else ())]
        missing = [c for c in bt if c not in last_u]
        L.append(f"  Canlı evren (son kayıt): {len(last_u)} coin | backtest evreni: {len(bt)} coin"
                 + (f" | backtestte olup canlıda olmayan: {', '.join(missing[:12])}" + (" …" if len(missing) > 12 else "")
                    if missing else ""))
    # Zamanlama, anlık tarama doğrulaması ve hesap modu satırları kaydın TAMAMINDAN: backtest verisi bitmiş günde kalır,
    # kayıt ise şu ana kadar sürer; güncellemeden / ayar değişikliğinden sonraki saatler de aynı gün raporda görünür
    full = read_scan_log(path, data.start_ms, 2 ** 62)
    timing = live_timing(cfg, full)
    L.extend(timing)
    newest = max(int(r["t"]) for r in full)
    if timing and newest > last:
        L.append(f"  (Zamanlama ve doğrulama satırları kaydın tamamından, {when(newest)}'e kadar; işlem karşılaştırması "
                 f"backtest verisinin bittiği {when(last)}'e kadar)")
    block = live_account_block(full)
    if block:
        L.append(block)
    inside = [tr for tr in trades if first <= tr.entry_ms <= last]
    if not inside:
        L.append("  Bu aralıkta backtestte işlem yok.")
        return L
    counts: Counter = Counter()
    rows = []
    for tr in inside:
        # Sinyal mumunun kapanışı: piyasa girişinde giriş anının kendisi, limit girişte (dolum birkaç dk sonra) o mum
        between = bool(int(tr.entry_ms) % FUT_BAR_MS)             # Dakikalık taramanın ara dakikasında açılan işlem
        row = by_t.get(int(tr.entry_ms) // MIN_MS * MIN_MS) if between else None
        if between and row is None:
            # Canlı bot o dakikada taramamış (o sırada 5 dk'da bir tarıyordu): aynı 5 dk içinde açtıysa söylenir
            near = by_t.get(int(tr.entry_ms) // FUT_BAR_MS * FUT_BAR_MS)
            base = tr.symbol[:-4] if tr.symbol.endswith("USDT") else tr.symbol
            if near is not None and (near.get("s") or {}).get(base) == "açıldı":
                key, text = "açtı", "✅ canlı da açtı (5 dk kapanışında; o sırada dakikalık tarama kapalıydı)"
            else:
                key, text = ("ara dakika taranmadı", "ara dakika: canlı bot o sırada 5 dk'da bir tarıyordu "
                                                    "(dakikalık tarama kapalıydı)")
        else:
            key, text = live_status(tr.symbol, row if between else by_t.get(int(tr.entry_ms) // FUT_BAR_MS * FUT_BAR_MS))
        counts[key] += 1
        rows.append(f"  {when(tr.entry_ms)} {tr.symbol[:-4]}: {text}")
    L.append(f"  Backtest işlemleri bu aralıkta: {len(inside)} | canlı da açtı: {counts.get('açtı', 0)}")
    L.extend(rows[:limit])
    if len(rows) > limit:
        L.append(f"  … {len(rows) - limit} işlem daha")
    others = [(k, v) for k, v in counts.most_common() if k != "açtı"]
    if others:
        L.append("  Kaçanların nedeni: " + ", ".join(f"{k}: {v}" for k, v in others))
    return L


# ===================================================================== kaldıraç riski
RISK_START_MONTHS = 12          # "Her ayın 1'inde başlayan hesap" penceresi (ay)
RISK_WORST_DAYS = 6             # Raporda gösterilen en kötü gün sayısı
RISK_DEEP_TRADES = 5            # Zarar kes kayarsa: gösterilen en derin işlem sayısı


def risk_ref_leverage(lev: int) -> Optional[int]:
    """Kaldıraç riski bölümündeki kıyas kaldıracı: 5x'in üstünde 5x, 4-5x'te 3x, 2-3x'te bir alt kaldıraç; 1x'te yok."""
    lev = int(lev)
    if lev > 5:
        return 5
    if lev >= 4:
        return 3
    if lev >= 2:
        return lev - 1
    return None


def curve_stats(curve: Sequence[Tuple[int, float]], start_capital: float, kill_pct: float = 0.0) -> dict:
    """Bakiye eğrisinin risk ölçüleri: en büyük düşüş (%, zirve / dip zamanı ve bakiyeleri), en düşük bakiye ve zamanı,
    kill-switch'in (toplam zarar başlangıç sermayesinin kill_pct %'si; simulate ile aynı kural) tetiklendiği an, son bakiye."""
    peak, peak_t = -1.0, None
    dd, at = 0.0, (None, None, None, None)
    low_t, low = None, float("inf")
    kill_level = start_capital * (1 - kill_pct / 100) if kill_pct else -1.0
    kill_ms = None
    for t, e in curve:
        if e > peak:
            peak, peak_t = e, t
        elif peak > 0 and (peak - e) / peak * 100 > dd:
            dd, at = (peak - e) / peak * 100, (peak_t, peak, t, e)
        if e < low:
            low_t, low = t, e
        if kill_ms is None and e <= kill_level:
            kill_ms = t
    return {"dd": dd, "peak_ms": at[0], "peak": at[1], "trough_ms": at[2], "trough": at[3], "low_ms": low_t,
            "low": low if curve else start_capital, "kill_ms": kill_ms, "final": curve[-1][1] if curve else start_capital}


def day_lows(curve: Sequence[Tuple[int, float]], start_capital: float) -> Dict[int, Tuple[float, float]]:
    """Gün (UTC, gün sayısı) → (gün içi en düşük bakiye %, gün sonu %), bir önceki günün son bakiyesine göre."""
    by_day: Dict[int, List[float]] = {}
    for t, e in curve:
        by_day.setdefault(int(t) // DAY_MS, []).append(e)
    out, prev = {}, start_capital
    for day in sorted(by_day):
        vals = by_day[day]
        if prev > 0:
            out[day] = ((min(vals) / prev - 1) * 100, (vals[-1] / prev - 1) * 100)
        prev = vals[-1]
    return out


def _month_starts(start_ms: int, end_ms: int, months: int) -> List[int]:
    """start_ms'ten sonraki her ayın 1'i (UTC); `months` ay sonrası end_ms'i aşmayanlar."""
    d = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    y, m = d.year, d.month
    if d != datetime(y, m, 1, tzinfo=timezone.utc):
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    out = []
    while True:
        s = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
        if s + months * 30 * DAY_MS + 5 * DAY_MS > end_ms:
            break
        out.append(s)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _equity_at(curve: Sequence[Tuple[int, float]], t: int) -> float:
    ts = [c[0] for c in curve]
    i = int(np.searchsorted(ts, t, side="right")) - 1
    return curve[max(i, 0)][1]


def leverage_risk(cfg, data: FutData, signals, windows, fixed_trades: Sequence[FutTrade]) -> Optional[dict]:
    """Kaldıraç riski bölümünün ölçüleri (ayardaki kaldıraç ve risk_ref_leverage kıyası; aynı sinyaller ve çıkışlar):
    her yıl 1 Ocak'tan başlayan hesabın düşüşleri ve kill-switch'i, en kötü günler, her ayın 1'inde başlayan hesapların
    ilk 12 ayı, zarar kes kayarsa likidasyon bölgesine giren işlemler (sabit boy işlemlerin stop dakikasındaki dibi).
    Kayma stresi tek kural tablosunun maliyet grubunda. A / B tabloları kill-switch'i uygulamaz."""
    base = params_from_cfg(cfg)
    lev = int(base.leverage)
    ref = risk_ref_leverage(lev)
    levs = [lev] + ([ref] if ref else [])
    kill = float(base.kill_pct or 0.0)
    cap = float(base.start_capital)
    out = {"lev": lev, "ref": ref, "levs": levs, "kill": kill, "start": cap, "yearly": {L: [] for L in levs},
           "daily": {L: {} for L in levs}, "roll": {L: [] for L in levs}}
    for y in range(_year(data.start_ms), _year(data.end_ms - 1) + 1):
        a, b = max(data.start_ms, _ms(f"{y}-01-01")), min(data.end_ms, _ms(f"{y + 1}-01-01"))
        if b - a < 30 * DAY_MS:
            continue
        for L in levs:
            res = simulate(replace(base, leverage=L), data, signals, windows, False, a, b)
            out["yearly"][L].append((y, curve_stats(res.curve, cap, kill), res.ruined_ms))
            out["daily"][L].update(day_lows(res.curve, cap))
    for s in _month_starts(data.start_ms, data.end_ms, RISK_START_MONTHS):
        e = s + RISK_START_MONTHS * 30 * DAY_MS + 5 * DAY_MS
        for L in levs:
            res = simulate(replace(base, leverage=L), data, signals, windows, False, s, e)
            out["roll"][L].append((s, curve_stats(res.curve, cap, kill), _equity_at(res.curve, s + 91 * DAY_MS),
                                   res.curve[-1][1] if res.curve else cap, res.ruined_ms))
    sl = [t for t in fixed_trades if t.reason == "SL"]
    out["sl"] = len(sl)
    out["liq"] = {L: futures_liq_distance_pct(L, base.mmr_pct) for L in levs}
    out["beyond"] = {L: sorted((t for t in sl if t.mae_pct >= out["liq"][L]), key=lambda t: -t.mae_pct) for L in levs}
    return out


def _usdt(v: float) -> str:
    return _num(v, 2) if v < 1000 else _num(v, 0)


def leverage_risk_lines(risk: Optional[dict]) -> List[str]:
    """Kaldıraç riski bölümünün rapor satırları (leverage_risk sonucu); sonuç yoksa boş."""
    if not risk:
        return []
    lev, ref, levs, kill, cap = risk["lev"], risk["ref"], risk["levs"], risk["kill"], risk["start"]
    tag = f"{lev}x" + (f", kıyas {ref}x" if ref else "")
    L = [f"🧯 KALDIRAÇ RİSKİ ({tag}; aynı sinyaller ve çıkışlar, {_usdt(cap)} USDT ile)",
         "  A / B tabloları kill-switch'i uygulamaz; bu bölüm yıl içindeki dipleri, kill-switch'i ve stopun "
         "likidasyona kadar kayma riskini gösterir."]
    if risk["yearly"].get(lev):
        L.append("  Her yıl 1 Ocak'tan başlayan hesap: yıl sonu | yıl içindeki en büyük düşüş (dip tarihi) | en düşük bakiye")
        by_year = {L2: {y: (st, ruined) for y, st, ruined in risk["yearly"][L2]} for L2 in levs}
        rows = []                                   # (yıl, [(kaldıraç, getiri, düşüş, dip, en düşük, eridi)])
        for y in sorted(by_year[lev]):
            cells = []
            for L2 in levs:
                st, ruined = by_year[L2].get(y, (None, None))
                if st is not None:
                    cells.append((f"{L2}x", _pct((st["final"] / cap - 1) * 100), f"−%{st['dd']:.1f}",
                                  f" ({_date(st['trough_ms'])})" if st["trough_ms"] else " " * 13, _usdt(st["low"]),
                                  f" | ERİDİ {_date(ruined)}" if ruined else ""))
            rows.append((y, cells))
        wid = [max([len(c[k]) for _y, cs in rows for c in cs] or [0]) for k in range(5)]   # Sütunlar alt alta hizalı
        for y, cells in rows:
            L.append(f"    {y}: " + "   ·   ".join(
                f"{c[0]:{wid[0]}s} {c[1]:>{wid[1]}s} | {c[2]:>{wid[2]}s}{c[3]} | {c[4]:>{wid[4]}s} USDT{c[5]}"
                for c in cells))
        if kill:
            parts = []
            for L2 in levs:
                stops = [f"{y} ({_date(st['kill_ms'])})" for y, st, _r in risk["yearly"][L2] if st["kill_ms"]]
                parts.append(f"{L2}x: " + (", ".join(stops) if stops else "hiçbir yılda"))
            L.append(f"  Kill-switch (toplam zarar sermayenin {_pct_of(kill, 1)}) uygulansaydı 1 Ocak başlangıçlı hesabı durdururdu: "
                     + " | ".join(parts))
    daily = {day: v for day, v in (risk["daily"].get(lev) or {}).items() if v[0] < 0}      # Yalnızca kayıplı günler
    if daily:
        worst = sorted(daily.items(), key=lambda kv: kv[1][0])[:RISK_WORST_DAYS]
        cells = []
        for day, (low, _close) in sorted(worst):
            other = risk["daily"].get(ref, {}).get(day) if ref else None
            cells.append(f"{_date(day * DAY_MS)} −%{-low:.0f}"
                         + ((f" / −%{-other[0]:.0f}" if other[0] < 0 else " / %0") if other else ""))
        L.append(f"  En kötü günler (gün içi en düşük bakiye, bir önceki günün sonuna göre; {lev}x"
                 + (f" / {ref}x" if ref else "") + "): " + ", ".join(cells))
    roll = risk["roll"].get(lev) or []
    if roll:
        n = len(roll)
        L.append(f"  Her ayın 1'inde başlayan {n} hesap ({_date(roll[0][0])} → {_date(roll[-1][0])}), ilk 12 ayda:")
        for L2 in levs:
            rows = risk["roll"][L2]
            dds = [st["dd"] for _s, st, _v3, _v12, _r in rows]
            kills = [(st["kill_ms"] - s) / DAY_MS for s, st, _v3, _v12, _r in rows if st["kill_ms"]]
            ret12 = [(v12 / cap - 1) * 100 for _s, _st, _v3, v12, _r in rows]
            text = (f"    {L2}x: %80'i aşan düşüş {sum(d > 80 for d in dds)}, %90'ı aşan {sum(d > 90 for d in dds)}, bakiye 5 "
                    f"USDT'nin altına inen {sum(st['low'] < 5 for _s, st, _v3, _v12, _r in rows)}, eriyen "
                    f"{sum(1 for row in rows if row[4])}, 3 ay sonra zararda {sum(v3 < cap for _s, _st, v3, _v12, _r in rows)}, "
                    f"12 ay sonra zararda {sum(r < 0 for r in ret12)}, ortanca 12 ay getirisi {_pct(float(np.median(ret12)))}")
            if kill:
                text += (f", kill-switch {len(kills)}" + (f" (ortanca {np.median(kills):.0f}. gün)" if kills else ""))
            L.append(text)
        if ref:
            other = {s: v12 for s, _st, _v3, v12, _r in risk["roll"][ref]}
            behind = [(s, v12, other[s]) for s, _st, _v3, v12, _r in roll if s in other and v12 < other[s]]
            line = f"    12 ay sonunda {lev}x'in {ref}x'ten az bakiyeyle bittiği başlangıç: {len(behind)} / {n}"
            if behind:
                s, a, b = min(behind, key=lambda x: x[1] / max(x[2], 1e-9))
                line += f" (en kötüsü {_date(s)}: {lev}x {_usdt(a)}, {ref}x {_usdt(b)} USDT)"
            L.append(line)
    if risk.get("sl"):
        cur = risk["beyond"][lev]
        line = (f"  Zarar kes kayarsa: {risk['sl']} zarar kes işleminin {_int_case(len(cur), 'loc')} stop dakikasında "
                f"fiyat girişin {_pct_case(risk['liq'][lev], 'abl', 1)} fazla altına indi ({lev}x'te stop o dibe kayarsa "
                f"likidasyon")
        if ref:
            line += f"; {ref}x için %{risk['liq'][ref]:.1f}: {len(risk['beyond'][ref])} işlem"
        line += ")"
        if cur:
            line += ". En derinleri: " + ", ".join(f"{_date(t.entry_ms)} {t.symbol[:-4] if t.symbol.endswith('USDT') else t.symbol} "
                                                    f"−%{t.mae_pct:.1f}" for t in cur[:RISK_DEEP_TRADES])
        L.append(line)
    return L


# ===================================================================== aylık işlem yığılması
MONTHS_TR = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
             "Aralık")
CLUSTER_RATIO = 1.5     # Aylık işlem yığılması: ayın günlük işlem hızı o yılın ortalamasının en az bu katı
MONTH_DAYS = 365.25 / 12


_UNIT_SFX = {1: "i", 2: "si", 3: "ü", 4: "ü", 5: "i", 6: "sı", 7: "si", 8: "i", 9: "u"}      # bir, iki ... dokuz
_TEN_SFX = {1: "u", 2: "si", 3: "u", 4: "ı", 5: "si", 6: "ı", 7: "i", 8: "i", 9: "ı"}       # on, yirmi ... doksan


def _int_sfx(n: int) -> str:
    """Tam sayının Türkçe iyelik eki (okunuştaki son kelimeye göre): 1'i, 2'si, 6'sı, 10'u, 40'ı, 100'ü, 1000'i."""
    n = abs(int(n))
    if n % 10:
        return _UNIT_SFX[n % 10]
    if n % 100:
        return _TEN_SFX[(n // 10) % 10]
    if n == 0:
        return "ı"                                  # sıfır
    return "ü" if n % 1000 else "i"                 # yüz / bin


def _pct_parts(v: float, digits: int = 0) -> Tuple[str, str]:
    """(%-li sayı, iyelik eki): ondalıklıda son basamağa, tam sayıda birler ya da onlar basamağına göre."""
    text = f"{abs(v):.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if "." in text:
        last = int(text[-1])
        sfx = _UNIT_SFX[last] if last else "ı"
    else:
        sfx = _int_sfx(int(text))
    return f"{'−' if v < 0 and float(text) else ''}%{text}", sfx


def _pct_of(v: float, digits: int = 0) -> str:
    """Yüzdenin Türkçe iyelik ekiyle yazımı (`digits` ondalık, sondaki sıfırlar atılır): %27'si, %65'i, %60'ı, %100'ü,
    %81.4'ü. Ek, okunuştaki son kelimeye göre: ondalıklıda son basamak, tam sayıda birler ya da onlar basamağı."""
    text, sfx = _pct_parts(v, digits)
    return f"{text}'{sfx}"


def _case(sfx: str, kind: str) -> str:
    """İyelik ekinin ardına hâl eki (ünlü uyumuyla): acc → -ni / -nı / -nu / -nü, abl → -nden / -ndan, loc → -nde /
    -nda, ins → -yle / -yla."""
    v = sfx[-1]
    back = v in "ıu"
    if kind == "acc":
        return sfx + "n" + v
    if kind == "abl":
        return sfx + ("ndan" if back else "nden")
    if kind == "loc":
        return sfx + ("nda" if back else "nde")
    return sfx + ("yla" if back else "yle")


def _pct_case(v: float, kind: str, digits: int = 0) -> str:
    """%-li sayı iyelik + hâl ekiyle: _pct_case(93, "acc") → "%93'ünü", _pct_case(16.2, "abl", 1) → "%16.2'sinden",
    _pct_case(10, "ins") → "%10'uyla"."""
    text, sfx = _pct_parts(v, digits)
    return f"{text}'{_case(sfx, kind)}"


def _int_case(n: int, kind: str) -> str:
    """Sayı iyelik + hâl ekiyle: _int_case(8, "loc") → "8'inde", _int_case(6, "loc") → "6'sında"."""
    return f"{int(n)}'{_case(_int_sfx(int(n)), kind)}"


def _month_range(y: int, m: int) -> Tuple[int, int]:
    nxt = (y + 1, 1) if m == 12 else (y, m + 1)
    return _ms(f"{y}-{m:02d}-01"), _ms(f"{nxt[0]}-{nxt[1]:02d}-01")


def monthly_clusters(ev: Evaluation, data: FutData) -> List[dict]:
    """Yıl yıl aylık işlem yığılması. İşlemler C'den (sabit pozisyon; bakiye etkisi yok): ay ay işlem sayısı, net sonuç
    toplamı (fiyat %) ve kazanan; ayın günlük işlem hızı o yılın ortalamasının CLUSTER_RATIO katı ya da üstündeyse
    yığılma (kısmi aylar gün sayısıyla ölçülür). B'den (o yıl yeniden başlayan hesap) ayın getirisi ve ay içindeki en
    büyük düşüş (ayın başındaki bakiyeden itibaren)."""
    years_b = dict(ev.years)
    out = []
    for y, group in ev.fixed_years():
        ya, yb = max(_ms(f"{y}-01-01"), data.start_ms), min(_ms(f"{y + 1}-01-01"), data.end_ms)
        if yb <= ya or not group:
            continue
        res = years_b.get(y)
        curve = res.curve if res is not None else []
        months = []
        for m in range(1, 13):
            a, b = _month_range(y, m)
            a, b = max(a, ya), min(b, yb)
            if b <= a:
                continue
            trs = [tr for tr in group if a <= tr.entry_ms < b]
            mo = {"m": m, "a": a, "b": b, "days": (b - a) / DAY_MS, "n": len(trs),
                  "sum": float(sum(net_move_pct(tr) for tr in trs)), "wins": sum(tr.pnl > 0 for tr in trs),
                  "ret": None, "dd": None, "cluster": False}
            if curve:
                e0, e1 = _equity_at(curve, a - 1), _equity_at(curve, b - 1)
                if e0 > 0:
                    peak, dd = e0, 0.0
                    for t, e in curve:
                        if a <= t < b:
                            peak = max(peak, e)
                            dd = max(dd, (peak - e) / peak * 100 if peak > 0 else 0.0)
                    mo["ret"], mo["dd"] = (e1 / e0 - 1) * 100, dd
            months.append(mo)
        days = (yb - ya) / DAY_MS
        rate = len(group) / days
        for mo in months:
            mo["x"] = (mo["n"] / mo["days"]) / rate if rate > 0 and mo["days"] > 0 else 0.0
            mo["cluster"] = bool(mo["n"]) and mo["x"] >= CLUSTER_RATIO
        out.append({"year": y, "n": len(group), "days": days, "sum": float(sum(net_move_pct(tr) for tr in group)),
                    "months": months, "clusters": [mo for mo in months if mo["cluster"]],
                    "b_ret": res.net_pct if res is not None and not res.ruined_ms else None})
    return out


def monthly_cluster_lines(rows: Sequence[dict]) -> List[str]:
    """Aylık işlem yığılması bölümünün satırları (monthly_clusters'tan); yığılma yoksa yılın en yoğun ayı."""
    if not rows:
        return []
    L = ["📆 AYLIK İŞLEM YIĞILMASI (C: sabit pozisyon işlemleri; B: her yıl yeniden başlayan hesap)",
         f"  Yığılma: ayın günlük işlem hızı o yılın ortalamasının en az {CLUSTER_RATIO:g} katı (×). Paylar yılın işlem "
         f"sayısına ve C'deki net kârına göre."]
    tot_months = tot_cl = tot_n = tot_cn = 0
    tot_sum = tot_csum = 0.0
    for r in rows:
        n, ysum, cl = r["n"], r["sum"], r["clusters"]
        tot_months += len(r["months"])
        tot_n += n
        tot_sum += ysum
        head = f"  {r['year']}: {n} işlem (ayda ort. {n / (r['days'] / MONTH_DAYS):.0f})"
        if not cl:
            top = max(r["months"], key=lambda mo: mo["x"])
            L.append(f"{head} | yığılma yok; en yoğun ay {MONTHS_TR[top['m'] - 1]} {top['n']} işlem (×{top['x']:.1f})")
            continue
        cn, csum = sum(mo["n"] for mo in cl), sum(mo["sum"] for mo in cl)
        tot_cl += len(cl)
        tot_cn += cn
        tot_csum += csum
        share = f", C kârının {_pct_of(csum / ysum * 100)}" if ysum > 0 else ""
        L.append(f"{head} | yığılma: " + " · ".join(f"{MONTHS_TR[mo['m'] - 1]} {mo['n']} (×{mo['x']:.1f})" for mo in cl)
                 + f" → işlemlerin {_pct_of(cn / n * 100)}{share}")
        rest = [mo for mo in r["months"] if not mo["cluster"]]
        rn, rsum = sum(mo["n"] for mo in rest), sum(mo["sum"] for mo in rest)
        txt = f"    Yığılma ayları olmadan: C {rsum:+.1f}% (yerine {ysum:+.1f}%)"
        if rn:
            txt += f", işlem başı {rsum / rn:+.3f}% (yığılma aylarında {csum / cn:+.3f}%)"
        if r["b_ret"] is not None and all(mo["ret"] is not None for mo in r["months"]):
            factor = float(np.prod([1 + mo["ret"] / 100 for mo in rest])) if rest else 1.0
            txt += f" | B (bu aylar yatay geçseydi) {_pct((factor - 1) * 100)} (yerine {_pct(r['b_ret'])})"
        L.append(txt)
    if tot_n:
        share = f", C kârının {_pct_of(tot_csum / tot_sum * 100)}" if tot_sum > 0 else ""
        txt = f"  Tüm yıllar: {tot_cl} / {tot_months} ayda yığılma; bu aylarda işlemlerin {_pct_of(tot_cn / tot_n * 100)}{share}"
        if tot_cn and tot_n > tot_cn:
            txt += (f" | işlem başı yığılma aylarında {tot_csum / tot_cn:+.3f}%, diğer aylarda "
                    f"{(tot_sum - tot_csum) / (tot_n - tot_cn):+.3f}%")
        L.append(txt)
    return L


DEPTH_MEASURED_FROM = _ms("2023-01-01")      # Arşivde bookDepth'in başladığı gün (öncesi hacimden tahmin)


def depth_lines(p: SimParams, data: FutData, res: FutResult) -> List[str]:
    """Rapor başlığı: emir defteri kayması ve derinliğe göre boy (açıksa); kapalıysa büyük bakiyede uyarı."""
    out = []
    if p.depth_cap_pct > 0 and data.depth is not None:
        capped = sum(1 for tr in res.trades if tr.depth_capped)
        out.append(f"📏 Derinliğe göre boy: pozisyon, girişten önceki 2 dk'da fiyatın ±%1 içindeki alış ve satış "
                   f"emirlerinden küçük olanın {_pct_case(p.depth_cap_pct, 'ins', 1)} sınırlı (1.000 USDT altındaki pozisyonda "
                   f"bakılmaz); aynı anda birden çok sinyalde sınıra takılınca kapasitesi büyük olan önce. A'da "
                   f"{capped} / {len(res.trades)} işlem sınıra takıldı")
    if p.depth_impact and data.depth is not None:
        cost = sum(tr.notional * tr.impact_pct / 100 for tr in res.trades)
        early = sum(1 for tr in res.trades if tr.entry_ms < DEPTH_MEASURED_FROM)
        out.append("📚 Emir defteri kayması: büyük pozisyon defteri tüketir; giriş ve çıkışta pozisyon büyüklüğüne göre "
                   "(2023'ten itibaren Binance'in ölçülen derinliği" + (f", öncesi 7 günlük hacimden tahmin: {early} "
                                                                       f"işlem" if early else "")
                   + f"). A'da toplam kayma {_num(cost, 0)} USDT")
    elif not p.depth_impact:
        out.append("⚠️ Kayma pozisyon büyüklüğünden bağımsız sayıldı (FUTURES_BACKTEST_DEPTH=false): büyük bakiyede A "
                   "gerçekçi değil (ölçülen derinlikle pozisyon onlarca bin USDT'yi geçince kayma kârı yer)")
    return out


def _noise_band(draws: Optional[Sequence[float]]) -> Optional[Tuple[float, float]]:
    """Çekiliş duyarlılığından şans bandı: aynı ayarın farklı mum içi başabaş çekilişlerindeki son bakiyelerinin
    ortancaya oranı (en düşük, en yüksek). En az 3 çekiliş yoksa None."""
    vals = [float(v) for v in (draws or ()) if v and v > 0]
    if len(vals) < 3:
        return None
    med = float(np.median(vals))
    return min(vals) / med, max(vals) / med


def _mark(ev: Evaluation, base_final: float, band: Optional[Tuple[float, float]]) -> str:
    """Satır işareti: A'daki son bakiye mevcut ayarınkinin şans bandının üstünde (▲) / altında ya da eridi (▼)."""
    if band is None:
        return ""
    if ev.chained.ruined_ms:
        return "▼ "
    if base_final <= 0:
        return "  "
    r = ev.chained.final_equity / base_final
    return "▲ " if r > band[1] else "▼ " if r < band[0] else "  "


def half_year_periods(start_ms: int, end_ms: int) -> List[Tuple[int, int]]:
    """[start_ms, end_ms) aralığı 1 Ocak / 1 Temmuz (UTC) sınırlarından 6 aylık dönemlere bölünür; ilk ve son dönem
    kısmi olabilir."""
    d = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    y, m = (d.year, 7) if d.month < 7 else (d.year + 1, 1)
    edges = [start_ms]
    while True:
        b = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
        if b >= end_ms:
            break
        edges.append(b)
        y, m = (y, 7) if m == 1 else (y + 1, 1)
    edges.append(end_ms)
    return list(zip(edges[:-1], edges[1:]))


def half_year_lines(res: FutResult, start_capital: float, start_ms: int, end_ms: int,
                    kill_pct: float = 0.0) -> List[str]:
    """A'nın 6 aylık bakiyesi: her dönemin sonundaki bakiye (kapanan işlemlerle; açık pozisyonun anlık K/Z'si sayılmaz),
    dönemdeki değişim, dönem içindeki en düşük bakiye ve en büyük düşüş, dönemde açılan işlem sayısı. Kill-switch'in
    botu durduracağı ve hesabın eridiği dönem işaretlenir."""
    curve = sorted(res.curve, key=lambda x: x[0])
    entries = np.sort(np.array([tr.entry_ms for tr in res.trades], dtype=np.int64))
    money = lambda v: _num(v, 2 if abs(v) < 1000 else 0)       # noqa: E731
    L = [f"📒 A'NIN 6 AYLIK BAKİYESİ ({money(start_capital)} USDT ile başlayan tek hesap; kapanan işlemlerle, açık "
         f"pozisyonun anlık kârı / zararı sayılmaz)",
         f"  {'Dönem':25s}  {'Dönem sonu bakiye':>17s}  {'6 ayda':>9s}  {'Dönem içi en düşük':>18s}  "
         f"{'Dönem içi düşüş':>15s}  {'İşlem':>6s}"]
    i, bal = 0, start_capital
    for a, b in half_year_periods(start_ms, end_ms):
        start_bal = bal
        pts = [(a, start_bal)]
        while i < len(curve) and curve[i][0] < b:
            if curve[i][0] >= a:
                pts.append(curve[i])
            bal = curve[i][1]
            i += 1
        change = (bal / start_bal - 1) * 100 if start_bal > 0 else None
        low = min(e for _t, e in pts)
        dd = max_drawdown(pts)[0]
        n = int(np.searchsorted(entries, b, "left") - np.searchsorted(entries, a, "left"))
        name = f"{_date(a)} → {_date(b) if b == end_ms else _date(b - 1)}"
        mark = ""
        if res.kill_ms is not None and a <= res.kill_ms < b:
            mark += f"  ⚠️ kill-switch (-%{kill_pct:g}) botu burada durdururdu"
        if res.ruined_ms is not None and a <= res.ruined_ms < b:
            mark += "  🛑 hesap eridi"
        L.append(f"  {name:25s}  {money(bal) + ' USDT':>17s}  {_pct(change):>9s}  {money(low) + ' USDT':>18s}  "
                 f"{_pct_cell(dd, 15)}  {n:6d}{mark}")
    return L


def summary_lines(p: SimParams, res: FutResult, ev: Evaluation, levs, live: Optional[List[str]], days: float) -> List[str]:
    """Raporun başındaki özet: A'nın sonucu, her ayın 1'inde başlayan hesaplar (kaldıraç tablosundaki şimdiki satır),
    stratejinin işlem başı gücü ve çıkış dağılımı, canlı giriş maliyeti (ölçüldüyse)."""
    L = ["📋 ÖZET"]
    ch = res
    a = (f"eridi {_date(ch.ruined_ms)}" if ch.ruined_ms else f"{_num(ch.final_equity, 0)} USDT ({_pct(ch.net_pct)})")
    L.append(f"  A) {_num(p.start_capital, 0)} USDT → {a} | en büyük düşüş %{max_drawdown(ch.curve)[0]:.1f} | "
             f"B'de kârlı yıl {ev.profitable_years}/{len(ev.years)}"
             + (f" | ⚠️ kill-switch {_date(ch.kill_ms)}'de durdururdu" if ch.kill_ms else ""))
    rows = [r if isinstance(r, LevRow) else LevRow(*r) for r in (levs or [])]
    roll = next((r.roll for r in rows if r.lev == p.leverage and r.roll), None)
    if roll:
        L.append(f"  Her ayın 1'inde başlayan {roll['n']} hesap (12 ay, kill-switch uygulanır): ortanca "
                 f"{_pct(roll['median'])} | %60'ı aşan düşüş {roll['dd60']} | kill-switch {roll['kills']}")
    fixed = ev.trades
    if fixed:
        n = len(fixed)
        share = {k: sum(tr.reason == k for tr in fixed) / n * 100 for k in ("TP", "BE", "SL", "TIME")}
        per_year = f"{n / max(days / 365.25, 1e-9):,.0f}".replace(",", ".")
        L.append(f"  Strateji (C): yılda ~{per_year} işlem, işlem başı net {ev.avg_move:+.3f}%, kazanma "
                 f"%{ev.win_rate:.1f} | çıkış: Kâr Al %{share['TP']:.0f}, başabaş %{share['BE']:.0f}, zarar kes "
                 f"%{share['SL']:.0f}, zaman stopu %{share['TIME']:.0f}")
    fill = next((x for x in (live or []) if "Giriş gecikmesi (anlık tarama" in x or
                 ("Giriş gecikmesi" in x and "anlık tarama" not in " ".join(live or []))), None)
    if fill and "dolum, kapanış fiyatına göre ortalama" in fill:
        part = fill.split("dolum, kapanış fiyatına göre ")[1]
        L.append(f"  Canlı girişler: dolum {part}")
    return L + [""]


def sl_streak_line(p: SimParams, trades: Sequence[FutTrade]) -> str:
    """En uzun art arda zarar kes serisinin (C, giriş sırasıyla) ilk pozisyon boyunda bakiyeye etkisi: her zarar kes
    teminatın stop mesafesi + kayma + komisyon × kaldıraç kadarını götürür; düşüşte küçültme açıksa eşikten sonra küçük boy."""
    run = best = 0
    for tr in trades:
        run = run + 1 if tr.reason == "SL" else 0
        best = max(best, run)
    best = max(best, 1)
    share = p.margin_usage_pct * (slot_share(p, 0) if p.max_positions > 1 else 1.0)
    per_loss = (p.sl_pct + 2 * p.slippage_pct + p.stop_slip_pct + 2 * p.taker_fee * 100) * p.leverage * share / 100
    equity = peak = 100.0
    for _ in range(best):
        equity *= 1 - per_loss / 100 * dd_scale_factor(p.dd_scale_pct, p.dd_scale_size_pct, equity, peak)
    scaled = " (düşüşte küçültmeyle)" if p.dd_scale_pct > 0 and equity < peak * (1 - p.dd_scale_pct / 100) else ""
    size = "ilk pozisyon boyunda" if p.max_positions > 1 else "tam boyda"
    return (f"En uzun zarar kes serisi {best} işlem: {p.leverage}x'te {size} (bakiyenin "
            f"{_pct_of(share, 1)} teminat) art arda {best} zarar kes bakiyenin ~{_pct_case(100 - equity, 'acc')} götürür"
            f"{scaled}; her biri ~%{per_loss:.1f}.")


def format_report(cfg, data: FutData, ev: Evaluation, funnel: Funnel, levs=None, grid=None, variants=None,
                  live: Optional[List[str]] = None, draws: Optional[Sequence[float]] = None,
                  risk: Optional[dict] = None) -> str:
    res = ev.chained
    p = res.params
    st = stats_of(res)
    dd, dd_a, dd_b = max_drawdown(res.curve)
    loss_streak, win_streak, streak_drop = streaks(res.trades)
    room = futures_liq_distance_pct(p.leverage, p.mmr_pct)
    days = (data.end_ms - data.start_ms) / DAY_MS
    fixed = ev.trades
    size = p.start_capital * p.margin_usage_pct / 100 * p.leverage / max(1, p.max_positions)
    L = [f"📊 VADELİ SCALPING BACKTESTİ — {_date(data.start_ms)} → {_date(data.end_ms)} ({days:.0f} gün)", "━" * 30]
    if data.universe_note:
        last = data.segments[-1].coins if data.segments else ()
        L.append(f"Coinler: {data.universe_note}; son ay: {','.join(last[:20])}" + (" ..." if len(last) > 20 else ""))
    else:
        L.append("Coinler: " + "; ".join(f"{_date(s.start_ms)}'den: {','.join(s.coins)}" for s in data.segments))
    margin_text = (f"serbest bakiyenin {_pct_of(p.margin_usage_pct, 1)} (bileşik)" if p.max_positions <= 1 else
                   f"pozisyon başına bakiyenin {_pct_of(p.margin_usage_pct / p.max_positions, 1)} (en fazla "
                   f"{p.max_positions} pozisyon, bileşik)" if p.extra_size_pct >= 100 else
                   f"ilk pozisyon bakiyenin {_pct_of(p.margin_usage_pct * slot_share(p, 0), 1)}, ek pozisyonlar "
                   f"{_pct_of(p.margin_usage_pct * slot_share(p, 1), 1)} (en fazla {p.max_positions} pozisyon, bileşik)")
    L.append(f"Kaldıraç {p.leverage}x isolated | teminat: {margin_text} | başlangıç {p.start_capital:g} USDT")
    L.extend(depth_lines(p, data, res))
    if p.dd_scale_pct > 0:
        small = sum(1 for tr in res.trades if tr.dd_scaled)
        L.append(f"📉 Düşüşte küçültme: bakiye zirvesinin %{p.dd_scale_pct:g} altındayken yeni pozisyonlar "
                 f"%{p.dd_scale_size_pct:g} boy (A ve B'de; C sabit tutarla). A'da {small} / {len(res.trades)} işlem "
                 f"küçük boyla açıldı")
    if p.bear_size_pct < 100:
        bears = sum(1 for tr in res.trades if tr.bear)
        L.append(f"🐻 Ayı piyasası: BTC'nin girişten önceki günkü kapanışı {p.bear_sma_days} günlük ortalamasının "
                 f"altındayken yeni pozisyonlar %{p.bear_size_pct:g} boy (A, B ve C'de). A'da {bears} / "
                 f"{len(res.trades)} işlem bu boyla açıldı")
    if p.bounce_calm_pct > 0 or p.bounce_boost_pct > 0:
        calm = sum(1 for tr in res.trades if tr.bounced == 1)
        big = sum(1 for tr in res.trades if tr.bounced == 2)
        parts = []
        if p.bounce_calm_pct > 0 and p.recent_size_pct < 100:
            parts.append(f"fiyat son 15 dk'nın dibinden en az %{p.bounce_calm_pct:g} yukarıdaysa çöküş durulmuş sayılmaz "
                         f"(A'da {calm} işlem)")
        if p.bounce_boost_pct > 0:
            parts.append(f"en az %{p.bounce_boost_pct:g} yukarıdaysa pozisyon %{p.bounce_boost_size_pct:g} boy (boştaki "
                         f"teminatla sınırlı; A'da {big} işlem)")
        if parts:
            L.append("↩️ V dönüşü: " + "; ".join(parts))
    if p.support_size_pct < 100:
        sup = sum(1 for tr in res.trades if tr.support)
        L.append(f"📐 Destek bölgesi: fiyat coinin önceki {p.support_days} günlük dibinin (sinyalden en az 1 saat önce "
                 f"kapanmış 1 saatlik mumlar) en fazla %{p.support_zone_pct:g} altındayken yeni pozisyonlar "
                 f"%{p.support_size_pct:g} boy (A, B ve C'de). A'da {sup} / {len(res.trades)} işlem bu boyla açıldı")
    if p.cushion_x > 0:
        big = sum(1 for tr in res.trades if tr.cushioned)
        L.append(f"🛏️ Kâr yastığı: bakiye başlangıç sermayesinin {p.cushion_x:g} katına çıkınca yeni pozisyonlar "
                 f"%{p.cushion_size_pct:g} boy (boştaki teminatla sınırlı; A ve B'de, C sabit tutarla). A'da {big} / "
                 f"{len(res.trades)} işlem bu boyla açıldı")
    if p.guard_pct > 0:
        guarded = sum(1 for tr in res.trades if tr.guarded)
        L.append(f"🛡️ Sermaye koruması: bakiye başlangıç sermayesinin %{p.guard_pct:g} altındayken (toplam zarar) yeni "
                 f"pozisyonlar %{p.guard_size_pct:g} boy (A ve B'de; C sabit tutarla). A'da {guarded} / "
                 f"{len(res.trades)} işlem bu boyla açıldı")
    if p.day_brake_pct > 0:
        braked = sum(1 for tr in res.trades if tr.braked)
        L.append(f"🚦 24 saat freni: bakiye son 24 saatteki en yüksek değerinin %{p.day_brake_pct:g} altındayken yeni "
                 f"pozisyonlar %{p.day_brake_size_pct:g} boy (A ve B'de; C sabit tutarla). A'da {braked} / "
                 f"{len(res.trades)} işlem frenle açıldı")
    if p.basis_size_pct != 100:
        big = sum(1 for tr in res.trades if tr.basis == tr.basis and tr.basis <= -p.basis_deep_pct)
        known = sum(1 for tr in res.trades if tr.basis == tr.basis)
        L.append(f"⚖️ Vadeli-spot farkı: girişte vadeli fiyat spotun %{tr_from_num(p.basis_deep_pct)} fazla altındaysa "
                 f"(zorla satış izi) pozisyon %{p.basis_size_pct:g} boy (boştaki teminatla sınırlı). A'da {big} / "
                 f"{len(res.trades)} işlem bu boyla açıldı" + (f"; {len(res.trades) - known} işlemde spot fiyatı "
                                                                 f"bulunamadı (boy değişmedi)" if known < len(res.trades)
                                                                 else ""))
    if p.quality_size_pct < 100:
        cut = QUALITY_CUTS.get(int(round(p.quality_cut_pct)), QUALITY_CUTS[60])
        small = sum(1 for tr in res.trades if tr.quality == tr.quality and tr.quality <= cut)
        L.append(f"🧮 Sinyal kalitesi: yedi ölçülü puan (çöküş şiddeti, vadeli-spot farkı, son 15 dk piyasa alış "
                 f"payı, son 5 / 15 dk düşüş, hacim ve satış hacmi oranı) 2020-2023 sinyallerinin en zayıf "
                 f"%{p.quality_cut_pct:g}'ındaysa pozisyon %{p.quality_size_pct:g} boy. A'da {small} / "
                 f"{len(res.trades)} işlem bu boyla açıldı")
    if p.big_account_usdt > 0 and p.max_positions > 1:
        extra = sum(1 for tr in res.trades if tr.slot > p.max_positions)
        skipped = res.skipped.get("büyük hesap: zayıf sinyal, son fonlama ≤ 0", 0)
        L.append(f"🏦 Büyük hesap kuralları: gerçekleşen bakiye ≥{_num(p.big_account_usdt, 0)} USDT iken kalite puanı "
                 f"en güçlü %20'lik sinyaller {BIG_EXTRA_SLOTS} ek pozisyon açabilir (en fazla "
                 f"{p.max_positions + BIG_EXTRA_SLOTS}); zayıf sinyale (en zayıf %60) son ödenen fonlama ≤ 0 ise "
                 f"girilmez. A'da {extra} işlem ek yuvadan açıldı, {skipped} sinyal fonlama yüzünden atlandı")
    if p.quality_roundup:
        n_round = sum(1 for tr in res.trades if tr.rounded)
        L.append(f"🔼 En küçük emre yuvarlama: kalite puanı en güçlü %40'taki sinyal Binance'in en küçük emir tutarının "
                 f"altında kalırsa boşta teminat yetiyorsa o tutarla açılır. A'da {n_round} işlem yuvarlandı")
    L.append(f"Kâr Al +%{p.tp_pct:g} ({'limit, maker' if p.tp_limit else 'piyasa'}) | Zarar Kes -%{p.sl_pct:g} | "
             + (f"başabaş +%{p.be_trigger_pct:g} → stop +%{p.be_floor_pct:g}" if p.be_enabled else "başabaş kapalı")
             + (f" | izleyen stop +%{p.trail_act_pct:g}'de, tepenin %{p.trail_gap_pct:g} gerisinden"
                if p.trail_enabled else "")
             + f" | zaman stopu {p.hold_min} dk (fiyat %, girişten)"
             + (f"; Faz 9: {phase9_label(p)}" if phase9_label(p) else ""))
    if p.trail_enabled:
        L.append("⚠️ İzleyen stop açık: backtest bu çıkışı iyimser hesaplar (1 dk mumda dakika içi geri çekilme görünmez). "
                 "Saniyelik veriyle kalibrasyonda (Kasım 2026, 1.935 işlem) izleyen stop işlem başı getiriyi %0.04-0.13 "
                 "düşürdü; gerçek sonuç bu rapordan belirgin düşük olur.")
    L.append(f"Komisyon: piyasa %{p.taker_fee * 100:.3f} / limit %{p.maker_fee * 100:.3f}, kayma %{p.slippage_pct:g}, "
             + ("ortalama fonlama oranı (8 saatte %0.01)" if data.loader is not None else "gerçek fonlama oranları")
             + f"; çıkışlar {'1 dk' if data.exit_interval == '1m' else '5 dk'} mumlarla")
    real = realism_label(p)
    if real:
        L.append(real)
    L.append(f"Likidasyon ({p.leverage}x): fiyat ~%{room:.1f} ters giderse; zarar kes bundan {room - p.sl_pct:.1f} "
             f"puan önce çalışır")
    crash = cfg.futures_tactic == "crash"
    if crash:
        L.append(f"Taktik: ÇÖKÜŞ DÖNÜŞÜ — {rule_label(cfg)}, long" + (" (+ likidasyon dalgası şartı)"
                                                                     if cfg.futures_crash_liq else "")
                 + cooldown_text(p))
        if funnel.between:
            L.append("⏱️ Her dakika tarama (FUTURES_MINUTE_SCAN): kural 5 dk kapanışlarının yanında aradaki dakikalarda da "
                     "değerlendirilir (1 dk mumlardan üretilen, o dakikada kapanan 5 dk mumlarla; eşikler aynı)")
        elif getattr(cfg, "futures_minute_scan", False):
            L.append("⏱️ FUTURES_MINUTE_SCAN açık ama bu ayarlarla / bu veriyle kullanılamadı: 5 dk'da bir tarandı")
        mods = futures_modules(cfg)
        if mods:
            parts = {"pump": f"pompa short ({cfg.futures_pump_rules})", "breakout": "hacimli kırılım long",
                     "bb": f"Bollinger + RSI ({cfg.futures_bb})", "funding": "fonlama sıkışması long",
                     "pullback": "trend içi düzeltme long"}
            L.append("Ek modüller (VEYA): " + ", ".join(parts[m] for m in mods)
                     + "; öncelik çöküş > fonlama > pompa > düzeltme > kırılım > Bollinger")
        if p.max_positions > 1 or p.isolated_size_pct < 100 or p.reentry_breadth > 0 or p.recent_size_pct < 100:
            L.extend(cascade_lines(p))
        wave = futures_wave_rules(cfg)
        if wave:
            L.append(f"Dalga eşikleri (Faz 6): son 1 saatte ≥{cfg.futures_wave_breadth} coin %3+ düşerken "
                     + " veya ".join(f"{h} saatte %{q:g}" for h, q in wave) + " düşüş de sinyal")
    else:
        L.append("Taktik: 4'lü onay (likidasyon + hacim + RSI uyumsuzluğu + BTC 1 saatlik trendi)")
    L.append("")
    L.extend(summary_lines(p, res, ev, levs, live, days))
    per_month = funnel.signals / max(days / 30.4, 1e-9)
    if crash:
        L.append("🔎 Sinyal hunisi (aktif dönemdeki 5 dk mumlar):")
        L.append(f"  {_num(funnel.candles, 0)} mum → çöküş kuralını ({rule_label(cfg).split(' · ')[0]}) geçen "
                 f"{_num(funnel.liq, 0)} (bunların likidasyon dalgalı olanı: {_num(funnel.liq_vol, 0)}) → "
                 f"{_num(funnel.signals, 0)} sinyal mumu" + (f" + {_num(funnel.wave, 0)} dalga eşiği sinyali" if funnel.wave else "")
                 + (f" + {_num(funnel.between, 0)} ara dakika sinyali" if funnel.between else "")
                 + f" ({'tek pozisyon' if p.max_positions <= 1 else 'pozisyon sınırı'} ve bekleme "
                 f"kurallarıyla işleme dönüşen {len(fixed)})")
    else:
        L.append("🔎 4'lü onay hunisi (aktif dönemdeki 5 dk mumlar):")
        L.append(f"  {_num(funnel.candles, 0)} mum → likidasyon dalgası {_num(funnel.liq, 0)} → + hacim "
                 f"≥{cfg.futures_volume_ratio:g}x {_num(funnel.liq_vol, 0)} → + BTC 1s trend yönünde "
                 f"{_num(funnel.liq_vol_trend, 0)} → + RSI uyumsuzluğu {_num(funnel.signals, 0)} sinyal "
                 f"(ayda ~{per_month:.0f}; tek pozisyon kuralıyla işleme dönüşen {len(fixed)})")
    L.append("")
    if live:
        L.extend(live)
        L.append("")
    L.append(f"💰 A) {p.start_capital:g} USDT ile {_date(data.start_ms)}'de başlayıp para eklemeseydiniz (tek hesap)")
    L.append(f"  Son bakiye: {_num(res.final_equity)} USDT ({_pct(res.net_pct)}) | BTC al-tut {_pct(btc_hold_pct(data))}")
    if draws:
        vals = sorted(draws)
        L.append(f"  Çekiliş duyarlılığı: aynı ayar, mum içi başabaşın {len(vals)} farklı çekilişiyle → son bakiye en düşük "
                 f"{_num(vals[0], 0)}, ortanca {_num(float(np.median(vals)), 0)}, en yüksek {_num(vals[-1], 0)} USDT. "
                 f"İki raporun son bakiyesi arasındaki fark bu aralığın içindeyse şans olabilir.")
    L.append(f"  İşlem {st.trades} | kazanan {st.wins} | kazanma %{st.win_rate:.1f} | kâr faktörü {_pf(st.profit_factor)}"
             f" | işlem başı ROE {st.avg_roe:+.2f}%")
    if p.depth_impact and data.depth is not None and fixed and st.win_rate < ev.win_rate - 15:
        L.append(f"  (Kazanma oranı C'dekinden (%{ev.win_rate:.1f}) düşük: hesap büyüdükçe emir defteri kayması başabaş "
                 f"çıkışlarını (net ~%0) küçük zarara çevirir; kâr Kâr Al ve zaman stopundan gelir.)")
    L.append(f"  Maks. düşüş %{dd:.1f}" + (f" ({_date(dd_a)} → {_date(dd_b)})" if dd_a else "")
             + f" | en uzun kayıp serisi {loss_streak} işlem (bakiye -%{streak_drop:.1f}) | kazanç serisi {win_streak}")
    skipped = ", ".join(f"{k}: {v}" for k, v in res.skipped.most_common())
    if skipped:
        L.append(f"  Atlanan sinyal: {skipped}")
    if crash and data.breadth is not None and fixed:
        L.append("  " + breadth_breakdown(fixed, p))
        first_line = first_breadth_line(fixed, p)
        if first_line:
            L.append("  " + first_line)
    recent_line = recent_drop_line(fixed, p) if crash and fixed else None
    if recent_line:
        L.append("  " + recent_line)
    basis_line = basis_breakdown_line(fixed, p) if crash and fixed else None
    if basis_line:
        L.append("  " + basis_line)
    quality_line = quality_breakdown_line(fixed, p) if crash and fixed else None
    if quality_line:
        L.append("  " + quality_line)
    if res.kill_ms:
        L.append(f"  ⚠️ Kill-switch (-%{p.kill_pct:g}) canlıda {_date(res.kill_ms)} tarihinde botu durdururdu")
    if res.ruined_ms:
        L.append(f"  🛑 HESAP ERİDİ: {_date(res.ruined_ms)} tarihinde bakiye en küçük emrin altına indi; sonrası bu "
                 f"senaryoda hiç işlem görmedi (B ve C'ye bakın)")
    L.append("")
    L.extend(half_year_lines(res, p.start_capital, data.start_ms, data.end_ms, p.kill_pct))
    L.append("")
    L.append(f"📅 B) Her yıl {p.start_capital:g} USDT ile yeniden başlasaydınız")
    L.append(f"  {'Yıl':4s}  {'İşlem':>5s}  {'Kazanma':>7s}  {'Getiri':>10s}  {'Maks.düşüş':>10s}  "
             f"{'Kayıp serisi':>12s}  {'Likid.':>6s}  {'BTC al-tut':>10s}")
    for y, r in ev.years:
        s2 = stats_of(r)
        mark = f"  🛑 eridi {_date(r.ruined_ms)[5:]}" if s2.ruined else ""
        L.append(f"  {str(y):4s}  {s2.trades:5d}  {_pct_cell(s2.win_rate, 7)}  {_pct(s2.net_pct):>10s}  "
                 f"{_pct_cell(s2.max_dd, 10)}  {s2.loss_streak:12d}  {s2.liquidations:6d}  "
                 f"{_pct(btc_hold_pct(data, r.start_ms, r.end_ms)):>10s}{mark}")
    L.append(f"  Kârlı yıl: {ev.profitable_years}/{len(ev.years)}")
    L.append("")
    L.append(f"📈 C) Stratejinin kendisi: her sinyal sabit {size:g} USDT'lik pozisyonla (bakiye etkisi ve erime yok)")
    L.append(f"  {'Yıl':4s}  {'İşlem':>5s}  {'Kazanma':>7s}  {'İşlem başı net':>14s}  {'Toplam (fiyat %)':>16s}  "
             f"{'Kâr faktörü':>11s}  {'Kayıp serisi':>12s}")
    for y, group in ev.fixed_years():
        w = sum(tr.pnl > 0 for tr in group)
        gain = sum(tr.pnl for tr in group if tr.pnl > 0)
        loss = -sum(tr.pnl for tr in group if tr.pnl <= 0)
        moves = [net_move_pct(tr) for tr in group]
        L.append(f"  {str(y):4s}  {len(group):5d}  {_pct_cell(w / len(group) * 100, 7)}  {np.mean(moves):+13.3f}%  "
                 f"{np.sum(moves):+15.1f}%  {_pf(gain / loss if loss > 0 else float('inf')):>11s}  "
                 f"{streaks(group)[0]:12d}")
    if fixed:
        moves = [net_move_pct(tr) for tr in fixed]
        gross = [m + tr.fees / tr.notional * 100 + tr.funding / tr.notional * 100 for m, tr in zip(moves, fixed)]
        roe = np.mean(moves) * p.leverage
        L.append(f"  Toplam {len(fixed)} işlem | kazanma %{ev.win_rate:.1f} | işlem başı net {np.mean(moves):+.3f}% "
                 f"(komisyon ve fonlama hariç {np.mean(gross):+.3f}%) | {p.leverage}x'te teminata göre "
                 f"{'+' if roe >= 0 else '−'}%{abs(roe):.2f}")
        L.append("")
        total = sum(tr.pnl for tr in fixed)
        L.append("📌 Çıkış nedenleri (C; net = komisyon, kayma ve fonlama dahil fiyat hareketi):")
        L.append("  Çıkış           İşlem  (pay) | net ort. | toplam kârdaki payı | ort. ROE | ort. süre")
        for reason in ("TP", "TRAIL", "BE", "SL", "TIME", "LIQ", "END"):
            group = [tr for tr in fixed if tr.reason == reason]
            if group:
                part = sum(tr.pnl for tr in group) / total * 100 if total else 0.0
                x = len(group) / len(fixed) * 100
                pay = f"(%{x:.1f})" if x < 99.95 else "(%100)"          # Sütun 7 karakter: tek çıkış türünde de hizalı
                L.append(f"  {REASON_TR[reason]:14s} {len(group):5d} {pay:>7s} | "
                         f"{np.mean([net_move_pct(tr) for tr in group]):+7.3f}% | "
                         f"{('+' if part >= 0 else '−') + '%' + f'{abs(part):.0f}':>19s} | "
                         f"{np.mean([tr.roe_pct for tr in group]):+7.2f}% | "
                         f"{np.mean([(tr.exit_ms - tr.entry_ms) / MIN_MS for tr in group]):5.0f} dk")
        if any(tr.reason == "BE" for tr in fixed):
            L.append("  (Başabaş bir kâr çıkışı değil: dönüşü sönen işlemi zarar kese gitmeden kapatır ve yuvayı yeni "
                     "sinyale açar; kâr Kâr Al ve zaman stopundan gelir.)")
        for side, name in (("long", "LONG"), ("short", "SHORT")):
            group = [tr for tr in fixed if tr.side == side]
            if group and len(group) < len(fixed):           # Tek yönlüyse satır toplamın aynısı: yazılmaz
                w = sum(tr.pnl > 0 for tr in group)
                L.append(f"  {name:5s}: {len(group)} işlem, kazanma %{w / len(group) * 100:.1f}, işlem başı "
                         f"{np.mean([net_move_pct(tr) for tr in group]):+.3f}%")
        if len({tr.kind for tr in fixed}) > 1:                 # Ek modüller açıksa modül modül
            for code, name in ((1.0, "Çöküş"), (2.0, "Pompa short"), (3.0, "Bollinger"), (4.0, "Kırılım")):
                group = [tr for tr in fixed if tr.kind == code]
                if group:
                    w = sum(tr.pnl > 0 for tr in group)
                    L.append(f"  {name:11s}: {len(group)} işlem, kazanma %{w / len(group) * 100:.1f}, işlem başı "
                             f"{np.mean([net_move_pct(tr) for tr in group]):+.3f}%, likidasyon "
                             f"{sum(tr.liquidated for tr in group)}")
        L.append("")
        L.append("⚠️ MARJİN ÇAĞRISI / LİKİDASYON RİSKİ (C'deki tüm işlemler)")
        L.append(f"  Likidasyon: {sum(tr.liquidated for tr in fixed)} | stopun ötesinde açılan mumla kapanan: "
                 f"{sum(tr.gap for tr in fixed)}")
        worst = min(fixed, key=lambda tr: tr.roe_pct)
        L.append(f"  En kötü işlem: {worst.symbol} {worst.side.upper()} {_date(worst.entry_ms)}: ROE {worst.roe_pct:+.1f}%"
                 f" ({REASON_TR[worst.reason]}, fiyat %{worst.mae_pct:.2f} ters)")
        wicks = sorted((tr.mae_pct - p.sl_pct for tr in fixed if tr.reason in ("SL", "BE", "TRAIL")), reverse=True)
        if wicks and wicks[0] > 0:
            big = sum(1 for w in wicks if w >= 2)
            L.append(f"  Zarar kes mumunda fiyatın stopun ötesine sarktığı en uzun fitil: %{wicks[0]:.1f} (≥%2 fitilli "
                     f"stop: {big}). Backtest stopu stop fiyatından kayma + %{p.stop_slip_pct:g} kötü doldurur "
                     f"(saniyelik veride ölçülen ortalama); tek tük ani çöküşte gerçek dolum daha kötü olabilir (en kötü "
                     f"durum: 'Kötümser stop dolumu' satırı).")
        wm = np.array([tr.window_mae_pct for tr in fixed])
        L.append(f"  Zarar kes OLMASAYDI: {p.hold_min} dk içinde fiyatın likidasyon mesafesini aştığı işlem:")
        L.append("    " + " | ".join(f"{lev}x (%{futures_liq_distance_pct(lev, p.mmr_pct):.1f}): "
                                   f"{int((wm >= futures_liq_distance_pct(lev, p.mmr_pct)).sum())}"
                                   for lev in sorted({*LEVERAGES, p.leverage})))
        L.append("  " + sl_streak_line(p, fixed))
        cluster_lines = monthly_cluster_lines(monthly_clusters(ev, data))
        if cluster_lines:
            L.append("")
            L.extend(cluster_lines)
    names = ([f"TP +%{tp:g} / SL -%{sl:g} ◀ şimdiki" for tp, sl, _e in (grid or [])]
             + [n for n, e in (variants or []) if e is not None])
    width = min(max([24] + [len(n) for n in names]), 72)
    head_row = f"    {'Ayar':{width}s} " + EVAL_HEAD
    band = _noise_band(draws)
    band_note = (f"  ▲ / ▼: A'daki son bakiye mevcut ayarınkinden çekiliş aralığından fazla yüksek / düşük (−%"
                 f"{(1 - band[0]) * 100:.0f} … +%{(band[1] - 1) * 100:.0f} şans payı; ▼ eriyen de); işaretsiz satırlardaki "
                 f"fark şans olabilir. C (sabit pozisyon) bileşik büyümeden bağımsız ölçüdür." if band else "")
    if levs:
        rows = [r if isinstance(r, LevRow) else LevRow(*r) for r in levs]
        n = next((r.roll["n"] for r in rows if r.roll), None)
        L.append("")
        L.append("🎚️ KALDIRAÇ DUYARLILIĞI (aynı sinyaller ve çıkışlar)")
        limit = ("emir defteri kayması ve derinliğe göre boy büyük pozisyonları sınırladığı için"
                 if p.depth_impact and data.depth is not None else
                 f"pozisyon tutarı en fazla {_num(p.max_notional, 0)} USDT kabul edildiği için")
        L.append((f"  Sol taraf: her ayın 1'inde başlayan {n} hesabın ilk 12 ayı (kill-switch uygulanır); bugün başlayan "
                  f"biri için asıl ölçü bu. " if n else "  ")
                 + f"A tek bir başlangıç tarihidir; hesap büyüyünce {limit} kaldıraçtan neredeyse bağımsızdır.")
        L.append(LEV_HEAD)
        for r in rows:
            label = r.label or (f"{r.lev}x" + (" ◀ şimdiki" if r.lev == p.leverage else ""))
            if r.ev is None:
                L.append(f"  {label:{LEV_LABEL_W}s}  — ({r.note})")
            else:
                L.append(f"  {label:{LEV_LABEL_W}s}" + _lev_line(r.ev, r.roll) + (f"  {r.note}" if r.note else ""))
        if any(r.label and "fren" in r.label for r in rows):
            L.append("  (fren satırları: 24 saat freni (FUTURES_DAY_BRAKE_PCT) açık / kapalı aynı hesap; fren tek başına "
                     "kârı düşürür, düşüşü azaltır; daha yüksek kaldıraçla aynı düşüşte daha çok kâr verir)")
        if any(r.label and "koruma" in r.label for r in rows):
            L.append("  (koruma satırı: sermaye koruması (FUTURES_GUARD_PCT) açık / kapalı aynı hesap; yalnız zarardaki "
                     "hesabı küçültür, farkı kill-switch sütununda görünür; tek çekilişte bu sayı oynaktır)")
    if risk:
        L.append("")
        L.extend(leverage_risk_lines(risk))
    if grid:
        L.append("")
        L.append(f"🎯 KÂR AL / ZARAR KES (şimdiki ayarın çevresi, {p.leverage}x)")
        if band_note:
            L.append(band_note)
        L.append(head_row)
        for tp, sl, ev2 in grid:
            name = f"TP +%{tp:g} / SL -%{sl:g}" + (" ◀ şimdiki" if _same(tp, p.tp_pct) and _same(sl, p.sl_pct) else "")
            L.append(f"  {_mark(ev2, res.final_equity, band) or '  '}{name:{width}s} " + _eval_line(ev2))
    if variants:
        L.append("")
        L.append("🧪 TEK KURAL DEĞİŞİNCE (diğer her şey aynı)")
        if band_note and not grid:
            L.append(band_note)
        L.append(head_row)
        ref5 = next((e for n, e in variants if n == SCAN5_REF and e is not None), None)
        for name, ev2 in variants:
            if ev2 is None:                         # Grup başlığı
                L.append(f"  ▸ {name}")
                continue
            five = ref5 is not None and name.endswith("5 dk taramayla")   # 5 dk taramalı satırlar kendi kıyasıyla
            mk = _mark(ev2, ref5.chained.final_equity if five else res.final_equity, band) or "  "
            L.append(f"  {mk}{name:{width}s} " + _eval_line(ev2))
            if name in ROW_NOTES and (name != FEE2_ROW or p.be_enabled):
                L.append(f"    ({ROW_NOTES[name]})")
    L.append("")
    if crash:
        L.append("⚠️ Çöküşte alım riskleri: ani çöküşlerde (ör. 10 Ekim 2025) zarar kes emri stop fiyatının çok ötesinde "
                 "dolabilir ve borsa gecikebilir; listedeki bir coin tamamen çökerse (LUNA, FTT gibi) art arda kayıp "
                 "yaşanır (test listelerinde böyle bir coin yoktu). Geçmiş sonuç gelecek garantisi değildir.")
    else:
        L.append("ℹ️ Likidasyon dalgası mumlardan ölçülür (zorla satış/alış hacmi ≥ ortalamanın "
                 f"{cfg.futures_liq_ratio:g} katı + son 1 saatin dibi/zirvesi kırıldı + ≥1 ATR itiş); Binance geçmiş "
                 "USDⓈ-M likidasyon kaydı yayımlamıyor. Geçmiş sonuç gelecek garantisi değildir.")
    for note in data.notes:
        L.append(f"ℹ️ {note}")
    return "\n".join(L)


def save_trades_csv(trades: Sequence[FutTrade], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["giris_utc", "cikis_utc", "coin", "yon", "kaldirac", "giris", "cikis", "neden", "teminat_usdt",
                    "pozisyon_usdt", "komisyon_usdt", "fonlama_usdt", "kz_usdt", "roe_%", "net_fiyat_%",
                    "bakiye_sonra", "en_kotu_hareket_%", "likidasyon", "bosluk"])
        for tr in trades:
            w.writerow([datetime.fromtimestamp(tr.entry_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        datetime.fromtimestamp(tr.exit_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        tr.symbol, tr.side.upper(), tr.leverage, f"{tr.entry:.8g}", f"{tr.exit:.8g}",
                        REASON_TR[tr.reason], f"{tr.margin:.4f}", f"{tr.notional:.4f}", f"{tr.fees:.4f}",
                        f"{tr.funding:.4f}", f"{tr.pnl:.4f}", f"{tr.roe_pct:.2f}", f"{net_move_pct(tr):.3f}",
                        f"{tr.equity_after:.4f}", f"{tr.mae_pct:.3f}", "evet" if tr.liquidated else "",
                        "evet" if tr.gap else ""])
    return path


def save_chart(ev: Evaluation, path: Path, title: str) -> Optional[Path]:
    """4 panel: tek hesabın bakiyesi (log), yıllık getiri (her yıl yeniden), işlem başı net sonucun birikimi
    (stratejinin avantajı var mı?), yıllık kazanma oranı + işlem sayısı."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    if not ev.trades and not ev.chained.trades:
        return None
    res = ev.chained
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5))
    ax = axes[0][0]
    times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t, _e in res.curve]
    eq = [max(e, 1e-3) for _t, e in res.curve]
    ax.plot(times, eq, color="#2563eb", lw=1.2, drawstyle="steps-post")
    ax.axhline(res.params.start_capital, color="#9ca3af", lw=0.8, ls="--")
    if res.ruined_ms:
        ax.axvline(datetime.fromtimestamp(res.ruined_ms / 1000, tz=timezone.utc), color="#dc2626", lw=1, ls=":")
    ax.set_yscale("log")
    ax.set_title(f"A) Tek hesap: {res.params.start_capital:g} USDT'nin seyri (log)")
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=6))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m.%Y"))
    ax.grid(alpha=0.3)
    labels = [str(y) for y, _r in ev.years]
    nets = [r.net_pct for _y, r in ev.years]
    shown = [max(min(v, 500.0), -100.0) for v in nets]
    ax = axes[0][1]
    ax.bar(labels, shown, color=["#16a34a" if v >= 0 else "#dc2626" for v in nets])
    for i, v in enumerate(nets):
        ax.text(i, shown[i], f"{v:+.0f}%", ha="center", va="bottom" if shown[i] >= 0 else "top", fontsize=8)
    ax.axhline(0, color="#374151", lw=0.8)
    ax.set_title(f"B) Yıllık getiri: her yıl {res.params.start_capital:g} USDT ile yeniden (%)")
    ax.grid(alpha=0.3, axis="y")
    ax = axes[1][0]
    fixed = ev.trades
    if fixed:
        tt = [datetime.fromtimestamp(tr.exit_ms / 1000, tz=timezone.utc) for tr in fixed]
        ax.plot(tt, np.cumsum([net_move_pct(tr) for tr in fixed]), color="#7c3aed", lw=1.2, label="net")
        ax.plot(tt, np.cumsum([net_move_pct(tr) + (tr.fees + tr.funding) / tr.notional * 100 for tr in fixed]),
                color="#9ca3af", lw=1.0, ls="--", label="komisyon ve fonlama hariç")
        ax.legend(loc="best", fontsize=8)
    ax.axhline(0, color="#374151", lw=0.8)
    ax.set_title("C) Sabit pozisyon: işlem sonuçlarının toplamı (kaldıraçsız %)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.grid(alpha=0.3)
    ax = axes[1][1]
    fy = ev.fixed_years()
    ylabels = [str(y) for y, _g in fy]
    wins = [sum(tr.pnl > 0 for tr in g) / len(g) * 100 for _y, g in fy]
    ax.bar(ylabels, wins, color="#0891b2", alpha=0.8)
    for i, (_y, g) in enumerate(fy):
        ax.text(i, wins[i], f"%{wins[i]:.0f}\n{len(g)} işl.", ha="center", va="bottom", fontsize=8)
    ax.set_ylim(0, 100)
    ax.set_title("C) Kazanma oranı (%) ve işlem sayısı")
    ax.grid(alpha=0.3, axis="y")
    fig.suptitle(title, fontsize=12)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


# ===================================================================== komut satırı
def period(cfg, days: Optional[int], end_ms: Optional[int], full_history: bool) -> Tuple[int, int, List[Segment]]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    end = min(end_ms or now_ms, now_ms) // DAY_MS * DAY_MS     # Bitmemiş gün yok (arşivde günlük dosya)
    if full_history:
        segments = history_segments(end)
    else:
        start = max(end - int(days or 365) * DAY_MS, _ms(HISTORY_SEGMENTS[0][0]))
        segments = [Segment(start, end, futures_coin_list(cfg))]
    return segments[0].start_ms, end, segments


def attach_depth(cfg, data: FutData, signals: Sequence[FutSignal],
                 progress: Optional[Callable[[str], None]] = None) -> Optional[DepthBook]:
    """Derinliğe göre boy ya da emir defteri kayması açıksa (FUTURES_DEPTH_CAP_PCT / FUTURES_BACKTEST_DEPTH) derinlik
    deposunu kurar ve sinyal günlerinin dosyalarını (2023'ten itibaren; ilk seferde arşivden) önceden okur."""
    p = params_from_cfg(cfg)
    if not (p.depth_impact or p.depth_cap_pct > 0) or data.archive is None:
        return None
    if data.depth is None:
        data.depth = DepthBook(data.archive.book_depth, daily_volume_fn(data.archive))
    data.depth.prefetch([(s.symbol, s.close_ms) for s in signals], progress=progress)
    return data.depth


BASIS_WORKERS = 8          # Spot 1 dk günlük dosyaları paralel indirilir (ilk çalıştırmada)


def attach_basis(cfg, data: FutData, signals: Sequence[FutSignal],
                 progress: Optional[Callable[[str], None]] = None) -> int:
    """Çöküş taktiğinde her sinyalin giriş anındaki vadeli / spot fiyat farkı (%) → metrics["basis"] (vadeli: sinyalden
    sonraki mumun açılışı, spot: aynı dakikanın 1 dk açılışı; spot arşivi, ilk seferde indirilir). Spot karşılığı ya da
    o dakika yoksa ölçü yazılmaz (boy çarpanı 1). Ölçülen sinyal sayısı döner."""
    if getattr(cfg, "futures_tactic", "crash") != "crash" or data.archive is None or not signals:
        return 0
    groups: Dict[Tuple[str, int], List[FutSignal]] = {}
    for s in signals:
        if "basis" not in s.metrics:
            groups.setdefault((s.symbol, s.close_ms // DAY_MS), []).append(s)
    if not groups:
        return 0
    if progress:
        progress(f"⚖️ Vadeli-spot farkı: {len(groups)} gün için spot fiyatları okunuyor (ilk seferde indirilir)...")

    def load(key: Tuple[str, int]) -> Tuple[Tuple[str, int], Optional[Tuple[np.ndarray, np.ndarray, float]]]:
        symbol, day = key
        when = datetime.fromtimestamp(day * DAY_MS / 1000, tz=timezone.utc)
        for spot, mult in spot_pairs(symbol):
            try:
                arr = data.archive.spot_day(spot, when)
            except Exception:  # noqa: BLE001 - spot verisi alınamadı: ölçü yok, boy çarpanı 1
                continue
            if len(arr["t"]):
                return key, (arr["t"], arr["o"], mult)
        return key, None

    done = 0
    with ThreadPoolExecutor(BASIS_WORKERS) as pool:
        for key, got in pool.map(load, list(groups)):
            if got is None:
                continue
            t, o, mult = got
            for s in groups[key]:
                i = int(np.searchsorted(t, s.close_ms))
                if i < len(t) and int(t[i]) == s.close_ms:
                    b = basis_pct(s.entry_open, float(o[i]), mult)
                    if b == b:
                        s.metrics["basis"] = b
                        done += 1
    return done


FUNDING_STALE_MS = 12 * 3_600_000      # Son ödenmiş fonlama bundan eskiyse (arşiv boşluğu) ölçü yazılmaz


def attach_funding(cfg, data: FutData, signals: Sequence[FutSignal],
                   progress: Optional[Callable[[str], None]] = None) -> int:
    """Çöküş taktiğinde her sinyalin girişten önceki son ödenmiş fonlama oranı (%) → metrics["last_funding"] (büyük
    hesap kuralları, FUTURES_BIG_ACCOUNT_USDT; canlı: FuturesExchange.funding_history). Aylık fonlama arşivi ilk
    seferde indirilir. Ölçülen sinyal sayısı döner."""
    if getattr(cfg, "futures_tactic", "crash") != "crash" or data.archive is None or not signals:
        return 0
    by_symbol: Dict[str, List[FutSignal]] = {}
    for s in signals:
        if "last_funding" not in s.metrics:
            by_symbol.setdefault(s.symbol, []).append(s)
    if not by_symbol:
        return 0
    if progress:
        progress(f"🏦 Fonlama oranları: {len(by_symbol)} coin okunuyor (ilk seferde indirilir)...")

    def load(symbol: str) -> Tuple[str, Optional[Tuple[np.ndarray, np.ndarray]]]:
        group = by_symbol[symbol]
        try:
            t, r = data.archive.funding(symbol, min(x.close_ms for x in group) - 2 * DAY_MS,
                                        max(x.close_ms for x in group) + 1)
        except Exception:  # noqa: BLE001 - fonlama alınamadı: ölçü yok, kural uygulanmaz
            return symbol, None
        return symbol, (t, r) if len(t) else None

    done = 0
    with ThreadPoolExecutor(BASIS_WORKERS) as pool:
        for symbol, got in pool.map(load, list(by_symbol)):
            if got is None:
                continue
            t, r = got
            for s in by_symbol[symbol]:
                i = int(np.searchsorted(t, s.close_ms, side="left")) - 1
                if i >= 0 and s.close_ms - int(t[i]) <= FUNDING_STALE_MS:
                    s.metrics["last_funding"] = float(r[i]) * 100
                    done += 1
    return done


def btc_daily_closes(data: FutData, end_ms: int) -> Tuple[np.ndarray, np.ndarray]:
    """BTCUSDT vadeli günlük kapanışları (5 dk mumların gün sonu; arşivden, Eylül 2019'dan end_ms ayına): (gün
    numaraları, kapanışlar). Okunamayan aylar atlanır."""
    days: List[int] = []
    closes: List[float] = []
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    y, m = 2019, 9
    while (y, m) <= (end.year, end.month):
        try:
            k = data.archive.month("BTCUSDT", "5m", y, m)
        except Exception:  # noqa: BLE001 - o ay yok / indirilemedi
            k = None
        if k is not None and len(k["t"]):
            d = (np.asarray(k["t"]) // DAY_MS).astype(np.int64)
            last = np.r_[np.nonzero(np.diff(d))[0], len(d) - 1]
            days.extend(int(x) for x in d[last])
            closes.extend(float(x) for x in np.asarray(k["c"])[last])
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    order = np.argsort(np.asarray(days, dtype=np.int64), kind="stable")
    return np.asarray(days, dtype=np.int64)[order], np.asarray(closes, dtype=float)[order]


def attach_regime(cfg, data: FutData, signals: Sequence[FutSignal],
                  progress: Optional[Callable[[str], None]] = None) -> int:
    """Çöküş taktiğinde her sinyale BTC'nin günlük rejimi (ayı piyasası küçültmesi, FUTURES_BEAR_SIZE_PCT): girişten
    önceki günün kapanışı N günlük ortalamanın altındaysa metrics["bearN"] = 1, değilse 0 (strategy.bear_regime ile aynı
    kural; N = BEAR_SMA_CHOICES ve ayardaki gün). Yeterli geçmiş yoksa ölçü yazılmaz (boy değişmez). Ölçülen sinyal
    sayısı döner."""
    if getattr(cfg, "futures_tactic", "crash") != "crash" or data.archive is None or not signals:
        return 0
    todo = [s for s in signals if not any(k.startswith("bear") for k in s.metrics)]
    if not todo:
        return 0
    if progress:
        progress("🐻 BTC günlük rejimi (ayı piyasası) hesaplanıyor...")
    day_no, closes = btc_daily_closes(data, max(s.close_ms for s in todo))
    if not len(closes):
        return 0
    cs = np.r_[0.0, np.cumsum(closes)]
    flags: Dict[int, np.ndarray] = {}
    for n in sorted(set(BEAR_SMA_CHOICES) | {int(getattr(cfg, "futures_bear_sma_days", 100))}):
        f = np.full(len(closes), np.nan)
        if len(closes) >= n:
            sma = (cs[n:] - cs[:-n]) / n
            f[n - 1:] = (closes[n - 1:] < sma).astype(float)
        flags[n] = f
    done = 0
    for s in todo:
        i = int(np.searchsorted(day_no, s.close_ms // DAY_MS, side="left")) - 1     # Dünün kapanışı (bugün bilinmez)
        if i < 0:
            continue
        got = False
        for n, f in flags.items():
            if f[i] == f[i]:
                s.metrics[f"bear{n}"] = float(f[i])
                got = True
        done += got
    return done


def attach_support(cfg, data: FutData, signals: Sequence[FutSignal],
                   progress: Optional[Callable[[str], None]] = None) -> int:
    """Çöküş taktiğinde her sinyale coinin destek dibine uzaklığı (destek bölgesi, FUTURES_SUPPORT_SIZE_PCT):
    metrics["lowNh"] = giriş fiyatı / dip − 1 (%), dip = sinyalden en az 1 saat önce kapanmış, son N gün içinde açılmış 1
    saatlik mumların en düşüğü (strategy.support_low ile aynı kural; mumlar coinin 5 dk geçmişinden toplanır; N =
    SUPPORT_DAY_CHOICES ve ayardaki gün). Pencerenin yarısından azı varsa ölçü yazılmaz (boy değişmez). Ölçülen sinyal
    sayısı döner."""
    if getattr(cfg, "futures_tactic", "crash") != "crash" or not signals:
        return 0
    todo = [s for s in signals if not any(k.startswith("low") and k.endswith("h") for k in s.metrics)]
    if not todo:
        return 0
    if progress:
        progress("📐 Destek dipleri (1 saatlik mumlar) hesaplanıyor...")
    by_sym: Dict[str, List[FutSignal]] = {}
    for s in todo:
        by_sym.setdefault(s.symbol, []).append(s)
    ns = sorted(set(SUPPORT_DAY_CHOICES) | {int(getattr(cfg, "futures_support_days", 30))})
    done = 0
    for sym, sigs in by_sym.items():
        k = data.k5.get(sym) if data.loader is None else data.loader(sym)
        if k is None or not len(k["t"]):
            continue
        t = np.asarray(k["t"], dtype=np.int64)
        hour = t // FUT_HOUR_MS
        hours, first = np.unique(hour, return_index=True)
        lows = np.minimum.reduceat(np.asarray(k["l"], dtype=np.float64), first)
        for s in sigs:
            got = False
            h_max = s.close_ms // FUT_HOUR_MS - 2                  # Kapanışı ≤ sinyal − 1 saat olan son mum
            for n in ns:
                h_min = -(-(s.close_ms - n * DAY_MS) // FUT_HOUR_MS)  # Açılışı ≥ sinyal − N gün
                a = int(np.searchsorted(hours, h_min, side="left"))
                b = int(np.searchsorted(hours, h_max, side="right"))
                if b - a >= n * 12 and s.entry_open > 0:
                    low = float(lows[a:b].min())
                    if low > 0:                                    # Fiyat: girişteki açılış (≈ sinyal kapanışı)
                        s.metrics[f"low{n}h"] = (s.entry_open / low - 1) * 100
                        got = True
            done += got
    return done


def run_cli(cfg, days: Optional[int], end_ms: Optional[int], out_dir: Path, chart: bool = True,
            full_history: bool = False, exit_interval: str = "1m", sensitivity: bool = True,
            progress: Callable[[str], None] = print) -> Tuple[str, Optional[Path], Evaluation]:
    """Backtesti çalıştırır; (rapor, grafik yolu, değerlendirme). İşlemler CSV olarak `out_dir`'e yazılır. İlerleme
    mesajları "⏳" ile başlar: Telegram'daki durum satırında görünür, rapor dosyasına girmez (backtest_runner)."""
    out = progress

    def progress(msg: str) -> None:             # noqa: F811 - ilerleme mesajlarını işaretleyen sarmalayıcı
        text = str(msg).strip()
        out(text if text.startswith("⏳") else f"⏳ {text}")
    start, end, segments = period(cfg, days, end_ms, full_history)
    data = prepare(cfg, start, end, segments, exit_interval=exit_interval, progress=progress)
    progress("🔎 Sinyaller aranıyor ("
             + ("çöküş dönüşü" if cfg.futures_tactic == "crash" else "4'lü onay") + ")...")
    # İlerleme yalnızca dakikalık taramada gerekir (1 dk mumların indirilmesi uzun sürer)
    signals, funnel = find_signals(cfg, data, progress) if futures_scan_shifts(cfg) else find_signals(cfg, data)
    progress(f"   {len(signals)} sinyal; çıkışlar için mumlar okunuyor...")
    windows = load_windows(data, signals, window_minutes(cfg), progress)
    attach_depth(cfg, data, signals, progress)
    attach_basis(cfg, data, signals, progress)
    attach_funding(cfg, data, signals, progress)
    attach_regime(cfg, data, signals, progress)
    attach_support(cfg, data, signals, progress)
    ev = evaluate(params_from_cfg(cfg), data, signals, windows)
    levs = grid = variants = None
    if sensitivity:
        progress("🎚️ Kaldıraç, Kâr Al / Zarar Kes ve kural duyarlılığı hesaplanıyor (uzun sürer)...")
        levs = leverage_table(cfg, data, signals, windows, progress=progress)
        progress("⏳ Kâr Al / Zarar Kes ızgarası hesaplanıyor...")
        grid = exit_grid(cfg, data, signals, windows)
        variants = variant_rows(cfg, data, signals, windows, progress=progress)
    risk = None
    if sensitivity:
        progress("🧯 Kaldıraç riski hesaplanıyor (yıl içi dipler, her ay başlayan hesaplar, kill-switch)...")
        risk = leverage_risk(cfg, data, signals, windows, ev.trades)
    draws = draw_spread(params_from_cfg(cfg), data, signals, windows) if sensitivity else []
    report = format_report(cfg, data, ev, funnel, levs, grid, variants, live=live_comparison(cfg, data, ev.chained.trades),
                           **({"draws": draws} if draws else {}), **({"risk": risk} if risk else {}))
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    try:
        csv_path = save_trades_csv(ev.trades, out_dir / f"vadeli_islemler_{stamp}.csv")
        report += f"\n📄 Tüm işlemler (C: sabit pozisyon): {csv_path}"
    except OSError as exc:
        report += f"\n⚠️ İşlem dosyası yazılamadı: {exc}"
    chart_path = None
    if chart:
        try:
            chart_path = save_chart(ev, out_dir / f"vadeli_grafik_{stamp}.png",
                                    f"Vadeli scalping {ev.chained.params.leverage}x — {_date(data.start_ms)} → "
                                    f"{_date(data.end_ms)} | {ev.profitable_years}/{len(ev.years)} yıl kârlı, "
                                    f"işlem başı {ev.avg_move:+.3f}%")
        except Exception as exc:  # noqa: BLE001 - grafik hatası raporu engellemesin
            report += f"\n⚠️ Grafik çizilemedi: {exc}"
    return report, chart_path, ev
