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
import heapq
import json
import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from config import (DATA_DIR, format_crash_rules, futures_coin_list, futures_crash_rules, futures_funding_rules,
                    futures_liq_distance_pct, futures_long_windows, futures_modules, futures_wave_cfg,
                    futures_wave_rules, parse_crash_rules, scan_log_path)
from futures_data import FuturesArchive, concat, resample, slice_range
from strategy import (FUT_BAR_MS, FUT_HOUR_MS, breadth_flags, btc_trend_1h, crash_state, funding_per_bar,
                      futures_candidates, futures_entries, futures_features, isolated_factor, pick_signal, trend_at)

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
LEVERAGES = (1, 3, 5, 10, 15, 20)
TP_GRID = (1.0, 1.2, 1.5)
SL_GRID = (0.8, 0.9, 1.0)
CRASH_TP_GRID = (3.0, 4.0, 6.0, 8.0)
CRASH_SL_GRID = (3.0, 4.0, 5.0)
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
                   loader=loader, lean_symbols=sorted(months), universe_note=note)


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
                   exit_interval, notes)


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

    def __init__(self, data: FutData):
        self.data = data
        self.t0 = data.start_ms // FUT_BAR_MS * FUT_BAR_MS
        self.need = data.breadth is None
        self.counts = np.zeros((data.end_ms - self.t0) // FUT_BAR_MS + 2, dtype=np.int32) if self.need else None

    def add(self, f: Dict[str, np.ndarray], close: np.ndarray, coin: str) -> None:
        if not self.need:
            return
        idx = (close - self.t0) // FUT_BAR_MS
        ok = (idx >= 0) & (idx < len(self.counts)) & _active_mask(close, coin, self.data.segments) & breadth_flags(f)
        np.add.at(self.counts, idx[ok], 1)

    def finish(self, signals: Sequence[FutSignal]) -> None:
        if self.need:
            self.data.breadth = (self.t0, self.counts)
        for s in signals:
            s.breadth = breadth_at(self.data, s.close_ms)


def breadth_at(data: FutData, close_ms: int) -> int:
    if data.breadth is None:
        return 0
    t0, counts = data.breadth
    i = (int(close_ms) - t0) // FUT_BAR_MS
    return int(counts[i]) if 0 <= i < len(counts) else 0


def find_signals(cfg, data: FutData) -> Tuple[List[FutSignal], Funnel]:
    """Canlı botun her 5 dk kapanışında yaptığı değerlendirmenin tüm geçmişe uygulanışı."""
    signals: List[FutSignal] = []
    wave_sigs: List[FutSignal] = []
    funnel = Funnel()
    breadth = BreadthCounter(data)
    wave_cfg = futures_wave_cfg(cfg)            # Faz 6 dalga eşikleri (kapalıysa None)
    for symbol, k in _series(data):
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
    funnel.signals = len(signals)
    breadth.finish(signals + wave_sigs)
    if wave_sigs:                   # Canlıdaki gibi: dalga eşikleri yalnızca genişlik ≥ FUTURES_WAVE_BREADTH iken
        need = int(getattr(cfg, "futures_wave_breadth", 12))
        kept = [s for s in wave_sigs if s.breadth >= need]
        funnel.wave = len(kept)
        signals += kept
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
        first = close_ms // DAY_MS * DAY_MS
        parts = [self._day(symbol, d) for d in range(first, close_ms + self.span, DAY_MS)]
        cols = [np.concatenate([p[i] for p in parts]) for i in range(5)]
        t = cols[0]
        i, j = np.searchsorted(t, close_ms), np.searchsorted(t, close_ms + self.span)
        win = (self.bar, t[i:j], cols[1][i:j], cols[2][i:j], cols[3][i:j], cols[4][i:j]) \
            if j > i and int(t[i]) == close_ms else None
        dict.__setitem__(self, key, win)
        return win if win is not None else default


# ===================================================================== simülasyon
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
    # Faz 6: ek pozisyonların büyüklüğü (ilk pozisyonun %'si; 100 = eşit, Faz 5) ve dalgada yeniden giriş (genişlik
    # ≥ reentry_breadth iken zarar sonrası bekleme atlanır; 0 = kapalı)
    extra_size_pct: float = 100.0
    reentry_breadth: int = 0
    # Faz 8: ek pozisyonların (başka pozisyon açıkken açılanlar) başabaş tetiği (fiyat %; 0 = be_trigger_pct ile aynı)
    extra_be_trigger_pct: float = 0.0


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
                  extra_size_pct=float(getattr(cfg, "futures_extra_size_pct", 100.0)),
                  reentry_breadth=int(getattr(cfg, "futures_reentry_breadth", 0)),
                  extra_be_trigger_pct=float(getattr(cfg, "futures_extra_be_trigger_pct", 0.0)))
    return replace(p, **over) if over else p


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


def run_trade(p: SimParams, s: FutSignal, window: tuple, margin: float, notional: float,
              data: Optional[FutData] = None) -> Optional[FutTrade]:
    """Tek işlem: giriş, başabaş, izleyen stop, Kâr Al / Zarar Kes / zaman stopu, likidasyon (yalıtılmış teminat).
    Limit girişte emir süresinde dolmazsa None (işlem yok)."""
    bar_ms, t, o, h, l, c = window
    side = 1 if s.side == "long" else -1
    slip = p.slippage_pct / 100
    start, entry_ms, maker = 0, s.close_ms, False
    if p.entry_limit:
        got = limit_fill(p, s, window)
        if got is not None:
            (start, entry), maker = got, True
        elif p.limit_fallback:                          # Süre doldu: kalan fırsat piyasa emriyle (taker)
            start = int(np.searchsorted(t, s.close_ms + p.limit_ttl_min * MIN_MS))
            if start >= len(t):
                return None
            entry = o[start] * (1 + side * slip)
        else:
            return None
        entry_ms = int(t[start])
    else:
        entry = s.entry_open * (1 + side * slip)
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
                exit_price, reason, exit_ms = stop * (1 - side * slip), kind, tj
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
                exit_price, reason, exit_ms = stop * (1 - side * slip), kind, tj
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
            exit_price, reason, exit_ms = fill * (1 - side * slip), kind, tj
            break
        if reached(fj, tp):
            exit_price = tp if p.tp_limit else tp * (1 - side * slip)
            reason, exit_ms = "TP", tj
            break
        favorable(fj)
        after = cj if adverse_first else aj                 # Tepeden sonra görülen en kötü fiyat
        if worse(after, stop):
            fill = (stop + after) / 2 if p.harsh_stops and not adverse_first else stop
            exit_price, reason, exit_ms = fill * (1 - side * slip), kind, tj
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
                    int(getattr(s, "breadth", 0)))


def slot_share(p: SimParams, n_open: int) -> float:
    """Yeni pozisyonun teminatı, kullanılabilir bakiyenin kaçta kaçı (config.futures_slot_share ile aynı kural): tek
    pozisyonda 1; dalga modunda ilk pozisyon 1, ek pozisyonlar x = extra_size_pct / 100 ağırlıkta, toplam 1 + (N-1)·x."""
    slots = max(1, int(p.max_positions))
    if slots <= 1:
        return 1.0
    x = max(0.0, min(1.0, p.extra_size_pct / 100))
    return (1.0 if n_open <= 0 else x) / (1 + (slots - 1) * x)


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
    for s in signals:
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
        factor = isolated_factor(p.isolated_size_pct, p.isolated_breadth, s.breadth)   # Yalnız çöküş: küçük
        if fixed_notional:
            notional = fixed_notional * factor
            margin = notional / p.leverage
        else:
            margin = equity * usage * factor
            notional = min(margin * p.leverage, p.max_notional)
            margin = notional / p.leverage
        if not fixed_notional and notional < min_notional(s.symbol):
            skipped["minimum emir tutarının altında"] += 1
            if equity * usage * p.leverage < DEFAULT_MIN_NOTIONAL:
                ruined = s.close_ms
                break
            continue
        window = windows.get((s.symbol, s.close_ms))
        if window is None or not len(window[1]):
            skipped["çıkış verisi yok"] += 1
            continue
        tr = run_trade(p, s, window, margin, notional, data)
        if tr is None:                                  # Limit emir süresinde dolmadı: iptal, işlem yok
            skipped[f"limit emir {p.limit_ttl_min} dk içinde dolmadı"] += 1
            busy_until = s.close_ms + p.limit_ttl_min * MIN_MS
            last_entry = s.close_ms
            continue
        equity = equity + tr.pnl if fixed_notional else max(0.0, equity + tr.pnl)
        tr.equity_after = equity
        tr.reentry = reentry
        tr.wave_rule = bool(s.metrics.get("wave_rule"))
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
        if equity * usage * p.leverage < DEFAULT_MIN_NOTIONAL:
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
    # Faz 8: ek pozisyonlar (başka pozisyon açıkken açılanlar) kendi başabaş tetiğiyle (config.futures_be_trigger)
    p_extra = (replace(p, be_trigger_pct=p.extra_be_trigger_pct)
               if p.be_enabled and p.extra_be_trigger_pct > 0 and p.extra_be_trigger_pct != p.be_trigger_pct else p)

    def realize(until: int) -> None:
        nonlocal equity, used, freeze_until, sl_streak, day_key, day_pnl, kill_ms, ruined, stop
        while heap and heap[0][0] <= until and not stop:
            _free, _q, tr, margin = heapq.heappop(heap)
            used -= margin
            open_syms.pop(tr.symbol, None)
            equity = equity + tr.pnl if fixed_notional else max(0.0, equity + tr.pnl)
            tr.equity_after = equity
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
            if not heap and equity * usage * p.leverage * first_share < DEFAULT_MIN_NOTIONAL:
                ruined, stop = tr.exit_ms, True

    for s in signals:
        realize(s.close_ms)
        if stop:
            break
        if s.close_ms == limit_bar:
            skipped["aynı mumda limit emir denendi"] += 1
            continue
        if len(heap) >= slots:
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
        factor = isolated_factor(p.isolated_size_pct, p.isolated_breadth, s.breadth)
        share = slot_share(p, len(heap))
        if fixed_notional:                  # fixed_notional = toplam / N (evaluate): eşit büyüklükte değişmez
            notional = fixed_notional * slots * share * factor
            margin = notional / p.leverage
        else:
            margin = min(equity * usage * share * factor, max(0.0, equity * usage - used))
            notional = min(margin * p.leverage, p.max_notional)
            margin = notional / p.leverage
            if notional < min_notional(s.symbol):
                skipped["minimum emir tutarının altında"] += 1
                if not heap and equity * usage * p.leverage * first_share < DEFAULT_MIN_NOTIONAL:
                    ruined = s.close_ms
                    break
                continue
        window = windows.get((s.symbol, s.close_ms))
        if window is None or not len(window[1]):
            skipped["çıkış verisi yok"] += 1
            continue
        tr = run_trade(p_extra if heap else p, s, window, margin, notional, data)
        if p.entry_limit:
            limit_bar = s.close_ms
        if tr is None:
            skipped[f"limit emir {p.limit_ttl_min} dk içinde dolmadı"] += 1
            continue
        tr.slot = len(heap) + 1
        tr.reentry = reentry
        tr.wave_rule = bool(s.metrics.get("wave_rule"))
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


def btc_hold_pct(data: FutData, start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> Optional[float]:
    t, c = data.btc_close
    if start_ms is not None:
        i, j = np.searchsorted(t, start_ms), np.searchsorted(t, end_ms or data.end_ms) - 1
        if j <= i:
            return None
        return (float(c[j]) / float(c[i]) - 1) * 100
    return (float(c[-1]) / float(c[0]) - 1) * 100 if len(c) > 1 else None


# ===================================================================== duyarlılık
def leverage_table(cfg, data: FutData, signals, windows) -> List[Tuple[int, Optional[Evaluation], str]]:
    rows = []
    for lev in LEVERAGES:
        room = futures_liq_distance_pct(lev, cfg.futures_mmr_pct)
        if cfg.stop_loss_pct >= room - 0.5:
            rows.append((lev, None, f"zarar kes (%{cfg.stop_loss_pct:g}) likidasyona (%{room:.1f}) çok yakın"))
            continue
        rows.append((lev, evaluate(params_from_cfg(cfg, leverage=lev), data, signals, windows), ""))
    return rows


def exit_grid(cfg, data: FutData, signals, windows) -> List[Tuple[float, float, Evaluation]]:
    tps, sls = (CRASH_TP_GRID, CRASH_SL_GRID) if cfg.futures_tactic == "crash" else (TP_GRID, SL_GRID)
    return [(tp, sl, evaluate(params_from_cfg(cfg, tp_pct=tp, sl_pct=sl), data, signals, windows))
            for tp in tps for sl in sls]


PHASE6_SIM = dict(max_positions=4, leverage=3, extra_breadth=8, isolated_size_pct=50.0, isolated_breadth=2,
                  extra_size_pct=16.7, reentry_breadth=3, loss_cooldown_min=240,       # config.FUTURES_PHASE6_PRESET
                  extra_be_trigger_pct=0.0)                                            # (Faz 8 kapalı: saf Faz 6)
PHASE6_SINGLE_SIM = dict(max_positions=1, leverage=2, isolated_size_pct=50.0, isolated_breadth=2, extra_size_pct=100.0,
                         reentry_breadth=3, loss_cooldown_min=240)               # config.FUTURES_PHASE6_SINGLE_PRESET
PHASE6_WAVE = {"futures_wave_rules": "1:5,4:8", "futures_wave_breadth": 12}
PHASE6_OFF_SIM = dict(extra_size_pct=100.0, reentry_breadth=0)
PHASE8_BE = 0.5                                 # config.FUTURES_PHASE8_BE_TRIGGER: ek pozisyonların başabaş tetiği


def phase6_on(cfg) -> bool:
    """Faz 6 eklerinden biri açık mı (dalgada yeniden giriş, dalga eşikleri, küçük ek pozisyonlar)."""
    return (int(getattr(cfg, "futures_reentry_breadth", 0)) > 0 or bool(futures_wave_rules(cfg))
            or (int(getattr(cfg, "futures_max_positions", 1)) > 1
                and float(getattr(cfg, "futures_extra_size_pct", 100.0)) < 100))


def no_wave(signals: Sequence[FutSignal]) -> List[FutSignal]:
    """Dalga eşiğiyle (Faz 6) gelen sinyaller çıkarılmış liste."""
    return [s for s in signals if not s.metrics.get("wave_rule")]


def cascade_rows(cfg, ev, signals=None) -> List[Tuple[str, "Evaluation"]]:
    """Dalga modu karşılaştırması: kapalıysa Faz 5 seti ve yalnız çöküş yarım; açıksa tek pozisyon (2x). Faz 6 ekleri
    açıksa bu satırlar Faz 6'sız hesaplanır (Faz 5 tam olarak Faz 5; Faz 6 satırları: phase6_rows)."""
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
                         extra_be_trigger_pct=0.0)))
    else:
        rows.append(("Tek pozisyon (dalga modu" + (" ve Faz 6" if on else "") + " kapalı, 2x)",
                     ev0(max_positions=1, leverage=2, isolated_size_pct=100.0)))
    if base.isolated_size_pct >= 100:
        rows.append(("Yalnız çöküş yarım büyüklük (<2 coin)", ev(isolated_size_pct=50.0, isolated_breadth=2)))
    return rows


def phase6_rows(cfg, ev, signals=None, phase6=None) -> List[Tuple[str, "Evaluation"]]:
    """Faz 6 karşılaştırması: açık eklerin tek tek kapatılması, kapalıysa dalgada yeniden girişin tek başına etkisi ve
    ⭐ Faz 6 setleri (`phase6(**over)`: dalga eşikli 1:5,4:8 / ≥12 sinyallerle değerlendirme, variant_rows verir)."""
    base = params_from_cfg(cfg)
    rows = []
    if base.reentry_breadth > 0:
        rows.append(("Dalgada yeniden giriş kapalı (zarar sonrası bekleme hep)", ev(reentry_breadth=0)))
    elif base.loss_cooldown_min > base.symbol_cooldown_min:
        rows.append(("Dalgada yeniden giriş (≥3 coin düşerken zarar sonrası bekleme yok)", ev(reentry_breadth=3)))
    if futures_wave_rules(cfg) and signals is not None:
        rows.append(("Dalga eşikleri kapalı", ev(no_wave(signals))))
    if base.max_positions > 1 and base.extra_size_pct < 100:
        rows.append(("Ek pozisyonlar eşit büyüklükte (Faz 5 gibi)", ev(extra_size_pct=100.0)))
    if phase6 is not None:
        wave = futures_wave_rules(cfg) == ((1, 5.0), (4, 8.0)) and cfg.futures_wave_breadth == 12
        cur = (base.max_positions, base.leverage, base.extra_size_pct, base.reentry_breadth, base.isolated_size_pct,
               base.loss_cooldown_min)
        if not (wave and cur == (4, 3, 16.7, 3, 50.0, 240) and not phase8_active(base)):
            rows.append(("⭐ Faz 6 seti (4 pozisyon: ilk tam + 3 × %16.7, 3x, dalgada yeniden giriş, dalga eşikleri)",
                         phase6(**PHASE6_SIM)))
        if not (wave and cur[:2] == (1, 2) and cur[3:] == (3, 50.0, 240)):
            rows.append(("⭐ Faz 6 tek pozisyon (dalgada yeniden giriş, dalga eşikleri, yalnız çöküş %50)",
                         phase6(**PHASE6_SINGLE_SIM)))
    return rows


def phase8_active(p: SimParams) -> bool:
    """Faz 8 etkin mi: dalga modunda ek pozisyonlar ana ayardan farklı bir başabaş tetiği kullanıyor."""
    return (p.max_positions > 1 and p.be_enabled and p.extra_be_trigger_pct > 0
            and p.extra_be_trigger_pct != p.be_trigger_pct)


def phase8_rows(cfg, ev) -> List[Tuple[str, "Evaluation"]]:
    """Faz 8 karşılaştırması (dalga modunda, başabaş açıkken): kapalıysa ek pozisyonlarda başabaş +%0.5, açıksa ana
    ayarla aynı tetik. Tek pozisyonda ek pozisyon olmadığı için satır yok."""
    base = params_from_cfg(cfg)
    if base.max_positions <= 1 or not base.be_enabled:
        return []
    if phase8_active(base):
        return [(f"Ek pozisyon başabaşı ana ayarla aynı (+%{base.be_trigger_pct:g}; Faz 8 kapalı)",
                 ev(extra_be_trigger_pct=0.0))]
    if base.be_trigger_pct <= PHASE8_BE:
        return []
    return [(f"Ek pozisyonlarda başabaş +%{PHASE8_BE:g} (Faz 8)", ev(extra_be_trigger_pct=PHASE8_BE))]


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


def variant_rows(cfg, data: FutData, signals, windows) -> List[Tuple[str, Evaluation]]:
    """Tek bir kuralı değiştirmenin etkisi (diğer her şey aynı)."""
    def ev(sigs=signals, **over):
        return evaluate(params_from_cfg(cfg, **over), data, sigs, windows)
    if cfg.futures_tactic == "crash":
        def other(**changes):
            c2 = replace(cfg, **changes)
            sigs, _f = find_signals(c2, data)
            load_windows(data, sigs, max(MAX_WINDOW_MIN, cfg.max_hold_minutes + 10), cache=windows)
            return evaluate(params_from_cfg(c2), data, sigs, windows)
        wave_cache: List[List[FutSignal]] = []

        def phase6(**over):     # Faz 6 setleri: dalga eşikli sinyaller (ayar zaten aynıysa mevcut sinyaller)
            if not wave_cache:
                if (futures_wave_rules(cfg) == ((1, 5.0), (4, 8.0))
                        and int(getattr(cfg, "futures_wave_breadth", 12)) == PHASE6_WAVE["futures_wave_breadth"]):
                    wave_cache.append(list(signals))
                else:
                    sigs, _f = find_signals(replace(cfg, **PHASE6_WAVE), data)
                    load_windows(data, sigs, max(MAX_WINDOW_MIN, cfg.max_hold_minutes + 10), cache=windows)
                    wave_cache.append(sigs)
            return evaluate(params_from_cfg(cfg, **over), data, wave_cache[0], windows)
        base = params_from_cfg(cfg)
        trail = (("İzleyen stop kapalı", ev(trail_enabled=False)) if base.trail_enabled else
                 ("İzleyen stop +%3 / %1.5", ev(trail_enabled=True, trail_act_pct=3.0, trail_gap_pct=1.5)))
        return ([("Başabaş kalkanı kapalı", ev(be_enabled=False)),
                trail,
                ("Başabaş +%5 → +%2", ev(be_enabled=True, be_trigger_pct=5.0, be_floor_pct=2.0)),
                ("Zaman stopu 60 dk", ev(hold_min=60)),
                ("Zaman stopu 120 dk", ev(hold_min=120)),
                ("Zarardan sonra bekleme yok", ev(loss_cooldown_min=0)),
                ("Kayma 5 kat (%0.10)", ev(slippage_pct=0.10)),
                ("Komisyon 2 kat", ev(taker_fee=params_from_cfg(cfg).taker_fee * 2,
                                      maker_fee=params_from_cfg(cfg).maker_fee * 2)),
                ("Kâr Al piyasa emriyle (taker)", ev(tp_limit=False)),
                ("Kötümser stop dolumu (fitilin yarısı)", ev(harsh_stops=True)),
                (f"Çöküş eşiği %{cfg.futures_crash_drop_pct - 2:g}", other(futures_crash_drop_pct=cfg.futures_crash_drop_pct - 2)),
                (f"Çöküş eşiği %{cfg.futures_crash_drop_pct + 2:g}", other(futures_crash_drop_pct=cfg.futures_crash_drop_pct + 2)),
                ("+likidasyon dalgası şartı" if not cfg.futures_crash_liq else "Likidasyon şartı kapalı",
                 other(futures_crash_liq=not cfg.futures_crash_liq))] + long_window_rows(cfg, other)
                + cascade_rows(cfg, ev, signals) + phase6_rows(cfg, ev, signals, phase6) + phase8_rows(cfg, ev))
    out = [("Başabaş kalkanı kapalı", ev(be_enabled=False)),
           ("Zaman stopu 30 dk", ev(hold_min=30)),
           ("Zaman stopu 120 dk", ev(hold_min=120)),
           ("Kâr Al piyasa emriyle (taker)", ev(tp_limit=False)),
           ("Komisyonsuz + kaymasız (kıyas)", ev(taker_fee=0.0, maker_fee=0.0, slippage_pct=0.0)),
           ("Yalnızca long", ev([s for s in signals if s.side == "long"])),
           ("Yalnızca short", ev([s for s in signals if s.side == "short"]))]
    return out


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
    hold = max([c.max_hold_minutes for c in cfgs] + [int(o.get("hold_min", 0)) for _n, o in exits])
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
            L.append("   Yıl   İşlem  Kazanma  Getiri (her yıl baştan)  Maks.düşüş")
            for y, res in ev.years:
                st = stats_of(res)
                L.append(f"   {y}  {st.trades:5d}  %{st.win_rate:5.1f}  {_pct(st.net_pct):>12s}            "
                         f"%{st.max_dd:4.0f}")
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
            windows = load_windows(data, sigs, max(MAX_WINDOW_MIN, p2.hold_min + 10), cache=windows)
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
            windows = load_windows(data, sigs, max(MAX_WINDOW_MIN, c2.max_hold_minutes + 10), cache=windows)
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
            windows = load_windows(data, sigs, max(MAX_WINDOW_MIN, c2.max_hold_minutes + 10), cache=windows)
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
    if abs(v) >= 1e5:
        return f"{v:+.1e}%" if sign else f"{v:.1e}%"
    return f"{v:+.1f}%" if sign else f"{v:.1f}%"


def _pf(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def _num(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _eval_line(ev: Evaluation) -> str:
    ch = ev.chained
    ruined = f"eridi {_date(ch.ruined_ms)}" if ch.ruined_ms else _pct(ch.net_pct)
    return (f"{ruined:>16s} | {ev.profitable_years}/{len(ev.years)} | {len(ev.trades):5d} işlem, kazanma "
            f"%{ev.win_rate:4.1f}, işlem başı {ev.avg_move:+.3f}%")


def cascade_label(p: SimParams) -> str:
    """Dalga modu / yalnız çöküş büyüklüğü / Faz 6 dalgada yeniden giriş özeti (rapor satırı)."""
    parts = []
    if p.max_positions > 1 and p.extra_size_pct >= 100:
        parts.append(f"🌊 Dalga modu: en fazla {p.max_positions} pozisyon (her biri bakiyenin "
                     f"%{p.margin_usage_pct / p.max_positions:.1f}'i teminat × {p.leverage}x); ek pozisyon yalnızca son 1 "
                     f"saatte ≥{p.extra_breadth} coin %3+ düşerken")
    elif p.max_positions > 1:
        first, each = p.margin_usage_pct * slot_share(p, 0), p.margin_usage_pct * slot_share(p, 1)
        parts.append(f"🌊 Dalga modu: en fazla {p.max_positions} pozisyon (ilk pozisyon bakiyenin %{first:.1f}'i teminat "
                     f"× {p.leverage}x, ek pozisyonlar %{each:.1f}'i: ilkinin %{p.extra_size_pct:g}'i); ek pozisyon "
                     f"yalnızca son 1 saatte ≥{p.extra_breadth} coin %3+ düşerken")
    if p.isolated_size_pct < 100:
        parts.append(f"yalnız çöküşte (≤{p.isolated_breadth - 1} coin düşüyor) pozisyon %{p.isolated_size_pct:g}")
    if p.reentry_breadth > 0:
        parts.append(f"dalgada yeniden giriş: ≥{p.reentry_breadth} coin düşerken zarar sonrası bekleme yok")
    if phase8_active(p):
        parts.append(f"ek pozisyonlarda başabaş +%{p.extra_be_trigger_pct:g} (Faz 8; ilk pozisyonda "
                     f"+%{p.be_trigger_pct:g})")
    return "; ".join(parts)


def breadth_breakdown(trades: Sequence[FutTrade], p: SimParams) -> str:
    """Girişteki genişliğe göre işlem başı sonuç (sabit tutar): yalnız çöküş / ara / piyasa geneli dalga."""
    groups = (("yalnız çöküş (0-1 coin)", lambda b: b < 2), ("2-7 coin", lambda b: 2 <= b < 8),
              ("dalga (8+ coin)", lambda b: b >= 8))
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
                          f"bitmiş günler var): karşılaştırma yarınki backtestte görünür."]
        return [head, "  Bu backtest döneminde canlı tarama kaydı yok."]
    by_t = {int(r["t"]): r for r in journal}
    first, last = min(by_t), max(by_t)
    expected = (last - first) // FUT_BAR_MS + 1
    skips = Counter(str(r["skip"]).split(" (")[0] for r in journal if "skip" in r)
    scanned = sum(1 for r in journal if "skip" not in r)
    L = [f"🔍 CANLI BOT KARŞILAŞTIRMASI ({mode} bot kaydı: {when(first)} → {when(last)})",
         f"  Kayıtlı 5 dk mum: {len(by_t)} / {expected} (bot kapalıyken kayıt olmaz) | taranan {scanned}"
         + (" | taranmayan: " + ", ".join(f"{k}: {v}" for k, v in skips.most_common(4)) if skips else "")]
    last_u = next((r["u"] for r in reversed(journal) if r.get("u") is not None), None)
    if last_u is not None:
        bt = [c for c in (data.segments[-1].coins if data.segments else ())]
        missing = [c for c in bt if c not in last_u]
        L.append(f"  Canlı evren (son kayıt): {len(last_u)} coin | backtest evreni: {len(bt)} coin"
                 + (f" | backtestte olup canlıda olmayan: {', '.join(missing[:12])}" + (" …" if len(missing) > 12 else "")
                    if missing else ""))
    inside = [tr for tr in trades if first <= tr.entry_ms <= last]
    if not inside:
        L.append("  Bu aralıkta backtestte işlem yok.")
        return L
    counts: Counter = Counter()
    rows = []
    for tr in inside:
        # Sinyal mumunun kapanışı: piyasa girişinde giriş anının kendisi, limit girişte (dolum birkaç dk sonra) o mum
        key, text = live_status(tr.symbol, by_t.get(int(tr.entry_ms) // FUT_BAR_MS * FUT_BAR_MS))
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


def format_report(cfg, data: FutData, ev: Evaluation, funnel: Funnel, levs=None, grid=None, variants=None,
                  live: Optional[List[str]] = None) -> str:
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
    margin_text = (f"serbest bakiyenin %{p.margin_usage_pct:g}'i (bileşik)" if p.max_positions <= 1 else
                   f"pozisyon başına bakiyenin %{p.margin_usage_pct / p.max_positions:.1f}'i (en fazla "
                   f"{p.max_positions} pozisyon, bileşik)" if p.extra_size_pct >= 100 else
                   f"ilk pozisyon bakiyenin %{p.margin_usage_pct * slot_share(p, 0):.1f}'i, ek pozisyonlar "
                   f"%{p.margin_usage_pct * slot_share(p, 1):.1f}'i (en fazla {p.max_positions} pozisyon, bileşik)")
    L.append(f"Kaldıraç {p.leverage}x isolated | teminat: {margin_text} | başlangıç {p.start_capital:g} USDT")
    L.append(f"Kâr Al +%{p.tp_pct:g} ({'limit, maker' if p.tp_limit else 'piyasa'}) | Zarar Kes -%{p.sl_pct:g} | "
             + (f"başabaş +%{p.be_trigger_pct:g} → stop +%{p.be_floor_pct:g}" if p.be_enabled else "başabaş kapalı")
             + (f" | izleyen stop +%{p.trail_act_pct:g}'de, tepenin %{p.trail_gap_pct:g} gerisinden"
                if p.trail_enabled else "")
             + f" | zaman stopu {p.hold_min} dk (fiyat %, girişten)")
    L.append(f"Komisyon: piyasa %{p.taker_fee * 100:.3f} / limit %{p.maker_fee * 100:.3f}, kayma %{p.slippage_pct:g}, "
             + ("ortalama fonlama oranı (8 saatte %0.01)" if data.loader is not None else "gerçek fonlama oranları")
             + f"; çıkışlar {'1 dk' if data.exit_interval == '1m' else '5 dk'} mumlarla")
    L.append(f"Likidasyon ({p.leverage}x): fiyat ~%{room:.1f} ters giderse; zarar kes bundan {room - p.sl_pct:.1f} "
             f"puan önce çalışır")
    crash = cfg.futures_tactic == "crash"
    if crash:
        L.append(f"Taktik: ÇÖKÜŞ DÖNÜŞÜ — {rule_label(cfg)}, long" + (" (+ likidasyon dalgası şartı)"
                                                                     if cfg.futures_crash_liq else "")
                 + f"; aynı coine işlemden sonra {p.symbol_cooldown_min} dk, zarardan sonra "
                 f"{max(p.symbol_cooldown_min, p.loss_cooldown_min)} dk girilmez")
        mods = futures_modules(cfg)
        if mods:
            parts = {"pump": f"pompa short ({cfg.futures_pump_rules})", "breakout": "hacimli kırılım long",
                     "bb": f"Bollinger + RSI ({cfg.futures_bb})", "funding": "fonlama sıkışması long",
                     "pullback": "trend içi düzeltme long"}
            L.append("Ek modüller (VEYA): " + ", ".join(parts[m] for m in mods)
                     + "; öncelik çöküş > fonlama > pompa > düzeltme > kırılım > Bollinger")
        if p.max_positions > 1 or p.isolated_size_pct < 100 or p.reentry_breadth > 0:
            L.append(cascade_label(p))
        wave = futures_wave_rules(cfg)
        if wave:
            L.append(f"Dalga eşikleri (Faz 6): son 1 saatte ≥{cfg.futures_wave_breadth} coin %3+ düşerken "
                     + " veya ".join(f"{h} saatte %{q:g}" for h, q in wave) + " düşüş de sinyal")
    else:
        L.append("Taktik: 4'lü onay (likidasyon + hacim + RSI uyumsuzluğu + BTC 1 saatlik trendi)")
    L.append("")
    per_month = funnel.signals / max(days / 30.4, 1e-9)
    if crash:
        L.append("🔎 Sinyal hunisi (aktif dönemdeki 5 dk mumlar):")
        L.append(f"  {_num(funnel.candles, 0)} mum → {cfg.futures_crash_hours} saatte ≥%{cfg.futures_crash_drop_pct:g} "
                 f"düşüş {_num(funnel.liq, 0)} (likidasyon dalgalı: {_num(funnel.liq_vol, 0)}) → {_num(funnel.signals, 0)} "
                 f"sinyal mumu" + (f" + {_num(funnel.wave, 0)} dalga eşiği sinyali" if funnel.wave else "")
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
    L.append(f"  İşlem {st.trades} | kazanan {st.wins} | kazanma %{st.win_rate:.1f} | kâr faktörü {_pf(st.profit_factor)}"
             f" | işlem başı ROE {st.avg_roe:+.2f}%")
    L.append(f"  Maks. düşüş %{dd:.1f}" + (f" ({_date(dd_a)} → {_date(dd_b)})" if dd_a else "")
             + f" | en uzun kayıp serisi {loss_streak} işlem (bakiye -%{streak_drop:.1f}) | kazanç serisi {win_streak}")
    skipped = ", ".join(f"{k}: {v}" for k, v in res.skipped.most_common())
    if skipped:
        L.append(f"  Atlanan sinyal: {skipped}")
    if crash and data.breadth is not None and fixed:
        L.append("  " + breadth_breakdown(fixed, p))
    if res.kill_ms:
        L.append(f"  ⚠️ Kill-switch (-%{p.kill_pct:g}) canlıda {_date(res.kill_ms)} tarihinde botu durdururdu")
    if res.ruined_ms:
        L.append(f"  🛑 HESAP ERİDİ: {_date(res.ruined_ms)} tarihinde bakiye en küçük emrin altına indi; sonrası bu "
                 f"senaryoda hiç işlem görmedi (B ve C'ye bakın)")
    L.append("")
    L.append(f"📅 B) Her yıl {p.start_capital:g} USDT ile yeniden başlasaydınız")
    L.append("  Yıl   İşlem  Kazanma  Getiri      Maks.düşüş  Kayıp serisi  Likid.  BTC al-tut")
    for y, r in ev.years:
        s2 = stats_of(r)
        mark = f"  🛑 eridi {_date(r.ruined_ms)[5:]}" if s2.ruined else ""
        L.append(f"  {y}  {s2.trades:5d}  %{s2.win_rate:5.1f}  {_pct(s2.net_pct):>10s}  %{s2.max_dd:5.1f}      "
                 f"{s2.loss_streak:3d}       {s2.liquidations:3d}   {_pct(btc_hold_pct(data, r.start_ms, r.end_ms)):>8s}"
                 f"{mark}")
    L.append(f"  Kârlı yıl: {ev.profitable_years}/{len(ev.years)}")
    L.append("")
    L.append(f"📈 C) Stratejinin kendisi: her sinyal sabit {size:g} USDT'lik pozisyonla (bakiye etkisi ve erime yok)")
    L.append("  Yıl   İşlem  Kazanma  İşlem başı net  Toplam (fiyat %)  Kâr faktörü  Kayıp serisi")
    for y, group in ev.fixed_years():
        w = sum(tr.pnl > 0 for tr in group)
        gain = sum(tr.pnl for tr in group if tr.pnl > 0)
        loss = -sum(tr.pnl for tr in group if tr.pnl <= 0)
        moves = [net_move_pct(tr) for tr in group]
        L.append(f"  {y}  {len(group):5d}  %{w / len(group) * 100:5.1f}  {np.mean(moves):+13.3f}%  {np.sum(moves):+14.1f}%"
                 f"   {_pf(gain / loss if loss > 0 else float('inf')):>9s}   {streaks(group)[0]:6d}")
    if fixed:
        moves = [net_move_pct(tr) for tr in fixed]
        gross = [m + tr.fees / tr.notional * 100 + tr.funding / tr.notional * 100 for m, tr in zip(moves, fixed)]
        L.append(f"  Toplam {len(fixed)} işlem | kazanma %{ev.win_rate:.1f} | işlem başı net {np.mean(moves):+.3f}% "
                 f"(komisyon ve fonlama hariç {np.mean(gross):+.3f}%) | "
                 f"{p.leverage}x'te teminatın {np.mean(moves) * p.leverage:+.2f}%'i")
        L.append("")
        L.append("📌 Çıkış nedenleri (C):")
        for reason in ("TP", "TRAIL", "BE", "SL", "TIME", "LIQ", "END"):
            group = [tr for tr in fixed if tr.reason == reason]
            if group:
                L.append(f"  {REASON_TR[reason]:14s} {len(group):5d} (%{len(group) / len(fixed) * 100:4.1f}) | ort. ROE "
                         f"{np.mean([tr.roe_pct for tr in group]):+6.2f}% | ort. süre "
                         f"{np.mean([(tr.exit_ms - tr.entry_ms) / MIN_MS for tr in group]):3.0f} dk")
        for side, name in (("long", "LONG"), ("short", "SHORT")):
            group = [tr for tr in fixed if tr.side == side]
            if group:
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
                     f"stop: {big}). Backtest stopu stop fiyatından doldurur; böyle anlarda gerçek dolum daha kötü "
                     f"olabilir (bkz. 'Kötümser stop dolumu' satırı).")
        wm = np.array([tr.window_mae_pct for tr in fixed])
        L.append(f"  Zarar kes OLMASAYDI: {p.hold_min} dk içinde fiyatın likidasyon mesafesini aştığı işlem:")
        L.append("    " + " | ".join(f"{lev}x (%{futures_liq_distance_pct(lev, p.mmr_pct):.1f}): "
                                   f"{int((wm >= futures_liq_distance_pct(lev, p.mmr_pct)).sum())}" for lev in LEVERAGES))
        streak = max(streaks(fixed)[0], 1)
        per_loss = (p.sl_pct + 2 * p.slippage_pct + 2 * p.taker_fee * 100) * p.leverage * p.margin_usage_pct / 100
        L.append(f"  En uzun kayıp serisi {streak} işlem: {p.leverage}x'te art arda {streak} zarar kes bakiyenin "
                 f"~%{(1 - (1 - per_loss / 100) ** streak) * 100:.0f}'ini götürür (işlem başı ~%{per_loss:.1f}).")
    head_row = "  Ayar                        A) tek hesap | B) kârlı yıl | C) sabit pozisyon"
    if levs:
        L.append("")
        L.append("🎚️ KALDIRAÇ DUYARLILIĞI (aynı sinyaller ve çıkışlar)")
        L.append(head_row)
        for lev, ev2, note in levs:
            L.append(f"  {f'{lev}x':26s} " + (_eval_line(ev2) if ev2 is not None else f"— ({note})"))
    if grid:
        L.append("")
        L.append(f"🎯 KÂR AL / ZARAR KES DUYARLILIĞI ({p.leverage}x)")
        L.append(head_row)
        for tp, sl, ev2 in grid:
            L.append(f"  {f'TP +%{tp:g} / SL -%{sl:g}':26s} " + _eval_line(ev2))
    if variants:
        L.append("")
        L.append("🧪 TEK KURAL DEĞİŞİNCE")
        L.append(head_row)
        for name, ev2 in variants:
            L.append(f"  {name:26s} " + _eval_line(ev2))
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


def run_cli(cfg, days: Optional[int], end_ms: Optional[int], out_dir: Path, chart: bool = True,
            full_history: bool = False, exit_interval: str = "1m", sensitivity: bool = True,
            progress: Callable[[str], None] = print) -> Tuple[str, Optional[Path], Evaluation]:
    """Backtesti çalıştırır; (rapor, grafik yolu, değerlendirme). İşlemler CSV olarak `out_dir`'e yazılır."""
    start, end, segments = period(cfg, days, end_ms, full_history)
    data = prepare(cfg, start, end, segments, exit_interval=exit_interval, progress=progress)
    progress("🔎 Sinyaller aranıyor ("
             + ("çöküş dönüşü" if cfg.futures_tactic == "crash" else "4'lü onay") + ")...")
    signals, funnel = find_signals(cfg, data)
    progress(f"   {len(signals)} sinyal; çıkışlar için mumlar okunuyor...")
    windows = load_windows(data, signals, max(MAX_WINDOW_MIN, cfg.max_hold_minutes + 10), progress)
    ev = evaluate(params_from_cfg(cfg), data, signals, windows)
    levs = grid = variants = None
    if sensitivity:
        progress("🎚️ Kaldıraç, Kâr Al / Zarar Kes ve kural duyarlılığı hesaplanıyor...")
        levs = leverage_table(cfg, data, signals, windows)
        grid = exit_grid(cfg, data, signals, windows)
        variants = variant_rows(cfg, data, signals, windows)
    report = format_report(cfg, data, ev, funnel, levs, grid, variants, live=live_comparison(cfg, data, ev.chained.trades))
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
