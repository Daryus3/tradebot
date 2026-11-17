"""
Emir defteri derinliği ve büyük pozisyonların fiyat kayması (Kasım 2026, likidite araştırması; README "Likidite tavanı").

Stratejinin işlem başı kazancı küçüktür (~%0.27). Büyük bir piyasa emri emir defterini tüketip fiyatı kaydırır; ölçülen
derinlikle (Binance bookDepth, 2023-2026) pozisyon onlarca bin USDT'yi geçince kayma bu kazancı yer. Bu modül:

- Canlı: borsanın emir defterinden fiyatın ±%1 içindeki alış / satış emirlerinin USDT tutarı (depth_within).
- Backtest (DepthBook): günlük bookDepth dosyaları (2023'ten beri; ~30 sn'de bir, fiyatın ±%1..±%5 içindeki kümülatif
  emir tutarı). Dosya yoksa (2023 öncesi, eksik gün) derinlik o coinin 7 günlük ortalama günlük hacminden tahmin edilir
  (PROXY_RATIO / PROXY_SHAPE: 2023-2026 ölçümlerinin ortancaları).
- Fiyat kayması (impact_pct): piyasa emri defteri sırayla tüketir; %0 → %1 arasındaki emirler düzgün dağılmış, sonraki
  seviyeler arasında doğrusal kabul edilir; %5'in ötesine taşan kısım %5'ten dolar (ölçüm aralığı dışı uzatılmaz).
- Derinliğe göre boy (FUTURES_DEPTH_CAP_PCT): pozisyon tutarı, girişten önceki 2 dk'da (canlıda girişten hemen önce)
  fiyatın ±%1 içindeki alış ve satış emirlerinden küçük olanın yüzdesiyle sınırlı. Pozisyon DEPTH_CHECK_MIN_NOTIONAL'ın
  altındaysa defter okunmaz (bu boyda sınır bağlamaz; canlıda gecikme eklenmez).
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, Optional, Sequence, Tuple

import numpy as np

DAY_MS = 86_400_000
DEPTH_WINDOW_MS = 120_000               # Anlık boşalmalara karşı ±2 dk'nın (karar için önceki 2 dk'nın) ortancası
DEPTH_FIRST_DAY = datetime(2023, 1, 1, tzinfo=timezone.utc)    # Arşivde bookDepth'in başladığı yer (öncesi tahmin)
DEPTH_CHECK_MIN_NOTIONAL = 1_000.0      # Bundan küçük pozisyonda defter okunmaz, sınır uygulanmaz
DEPTH_BOOK_LIMIT = 500                  # Canlı: emir defterinden istenen seviye sayısı (Binance ağırlığı 10)
DEPTH_EXIT_PAD_MS = 4 * 3_600_000       # Çıkış derinliği için sinyal gününden sonra da indirilecek süre
IMPACT_MAX_PCT = 5.0                    # Kayma tek yönde en fazla (ölçüm aralığı)
# Tahmin: %1 içindeki derinlik / 7 günlük ortalama günlük hacim (yıl yıl ortanca; 2023 öncesi için 2023 değeri)
PROXY_RATIO = {"ask": {2023: 0.0018, 2024: 0.0018, 2025: 0.0030, 2026: 0.0035},
               "bid": {2023: 0.0024, 2024: 0.0022, 2025: 0.0023, 2026: 0.0038}}
# Tahmin: %1..5 kümülatif derinliğin %1'e oranı (ortanca şekil)
PROXY_SHAPE = {"ask": (1.0, 1.63, 2.2, 2.62, 2.89), "bid": (1.0, 1.77, 2.41, 2.84, 3.18)}


def impact_pct(q: float, cum: Sequence[float]) -> float:
    """q USDT'lik piyasa emrinin ortalama fiyat kayması (%). cum: fiyatın %1..%5'i içindeki kümülatif emir tutarı."""
    if q <= 0:
        return 0.0
    ns = [0.0]
    for c in cum:
        ns.append(max(float(c) if math.isfinite(float(c)) else 0.0, ns[-1] + 1.0))   # Azalmasın, sıfır olmasın
    cost, rem = 0.0, float(q)
    for k in range(1, len(ns)):
        seg = ns[k] - ns[k - 1]
        if rem <= seg:
            x_end = (k - 1) + rem / seg
            return (cost + rem * ((k - 1) + x_end) / 2) / q
        cost += seg * ((k - 1) + k) / 2
        rem -= seg
    return min(IMPACT_MAX_PCT, (cost + rem * IMPACT_MAX_PCT) / q)


def depth_within(book: dict, pct: float = 1.0) -> Tuple[float, float, float, float]:
    """Emir defteri (ccxt biçimi: bids / asks = [[fiyat, miktar], ...]) → (en iyi alış, en iyi satış, orta fiyatın
    +pct% içindeki satış emirleri USDT, -pct% içindeki alış emirleri USDT). Defter boşsa sıfırlar."""
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return 0.0, 0.0, 0.0, 0.0
    bid, ask = float(bids[0][0]), float(asks[0][0])
    mid = (bid + ask) / 2
    hi, lo = mid * (1 + pct / 100), mid * (1 - pct / 100)
    ask_n = sum(float(row[0]) * float(row[1]) for row in asks if float(row[0]) <= hi)
    bid_n = sum(float(row[0]) * float(row[1]) for row in bids if float(row[0]) >= lo)
    return bid, ask, ask_n, bid_n


def _day_of(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // DAY_MS * DAY_MS / 1000, tz=timezone.utc)


def daily_volume_fn(archive) -> Callable[[str, int], Optional[float]]:
    """(parite, ms) → ms'den önceki 7 tam günün ortalama günlük USDT hacmi (arşivin günlük mumlarından; yoksa None)."""
    cache: Dict[Tuple[str, int, int], Dict[str, np.ndarray]] = {}

    def month(sym: str, y: int, m: int) -> Dict[str, np.ndarray]:
        key = (sym, y, m)
        if key not in cache:
            a = datetime(y, m, 1, tzinfo=timezone.utc)
            b = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
            cache[key] = archive.klines(sym, int(a.timestamp() * 1000), int(b.timestamp() * 1000), "1d")
        return cache[key]

    def adv(sym: str, ms: int) -> Optional[float]:
        d = int(ms // DAY_MS)
        days = {}
        for day in range(d - 7, d):
            dt = datetime.fromtimestamp(day * 86400, tz=timezone.utc)
            k = month(sym, dt.year, dt.month)
            if len(k["t"]):
                hit = np.nonzero(k["t"] // DAY_MS == day)[0]
                if len(hit):
                    days[day] = float(k["qv"][hit[0]])
        return float(np.mean(list(days.values()))) if len(days) >= 3 else None
    return adv


class DepthBook:
    """Backtest için derinlik: ölçülen (bookDepth) ya da tahmin. `loader(parite, gün)` → {t, ask, bid} (arşiv; dosya
    yoksa boş), `volume_fn(parite, ms)` → 7 günlük ortalama günlük hacim. Sayaçlar raporda kullanılır."""

    def __init__(self, loader: Callable[[str, datetime], Dict[str, np.ndarray]],
                 volume_fn: Optional[Callable[[str, int], Optional[float]]] = None, window_ms: int = DEPTH_WINDOW_MS):
        self.loader = loader
        self.volume_fn = volume_fn
        self.window_ms = window_ms
        self._days: Dict[Tuple[str, int], Dict[str, np.ndarray]] = {}
        self._cache: Dict[tuple, Optional[np.ndarray]] = {}
        self.measured = 0
        self.proxied = 0

    # ---------------------------------------------------------------- veri
    def _day(self, sym: str, day: int) -> Dict[str, np.ndarray]:
        key = (sym, day)
        if key not in self._days:
            dt = datetime.fromtimestamp(day * 86400, tz=timezone.utc)
            self._days[key] = self.loader(sym, dt) if dt >= DEPTH_FIRST_DAY else None
        return self._days[key]

    def prefetch(self, keys: Iterable[Tuple[str, int]], workers: int = 8,
                 progress: Optional[Callable[[str], None]] = None) -> int:
        """(parite, sinyal ms) listesinin günlerini (ve çıkış için sonrasını) paralel indirir / okur; gün sayısı."""
        days = set()
        for sym, ms in keys:
            for t in (ms, ms + DEPTH_EXIT_PAD_MS):
                if _day_of(t) >= DEPTH_FIRST_DAY:
                    days.add((sym, int(t // DAY_MS)))
        todo = sorted(d for d in days if d not in self._days)
        if todo and progress:
            progress(f"⏳ Emir defteri derinliği: {len(todo)} gün okunuyor (ilk seferde Binance arşivinden indirilir)...")
        if todo:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for key, arrs in zip(todo, pool.map(lambda k: self._load(*k), todo)):
                    self._days[key] = arrs
        return len(todo)

    def _load(self, sym: str, day: int) -> Optional[Dict[str, np.ndarray]]:
        dt = datetime.fromtimestamp(day * 86400, tz=timezone.utc)
        return self.loader(sym, dt) if dt >= DEPTH_FIRST_DAY else None

    def _measured(self, sym: str, ms: int, side: str, causal: bool) -> Optional[np.ndarray]:
        a, b = ms - self.window_ms, ms if causal else ms + self.window_ms
        parts = []
        for day in range(int(a // DAY_MS), int(b // DAY_MS) + 1):
            arrs = self._day(sym, day)
            if arrs is None or not len(arrs["t"]):
                continue
            t = arrs["t"]
            i, j = int(np.searchsorted(t, a)), int(np.searchsorted(t, b, side="right"))
            if j > i:
                parts.append(arrs[side][i:j])
        if not parts:
            return None
        return np.median(np.concatenate(parts), axis=0)

    def _proxy(self, sym: str, ms: int, side: str) -> Optional[np.ndarray]:
        adv = self.volume_fn(sym, ms) if self.volume_fn else None
        if not adv:
            return None
        year = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year
        ratios = PROXY_RATIO[side]
        ratio = ratios.get(year, ratios[min(ratios)] if year < min(ratios) else ratios[max(ratios)])
        return np.array(PROXY_SHAPE[side]) * ratio * adv

    def profile(self, sym: str, ms: int, side: str, causal: bool = False) -> Optional[np.ndarray]:
        """Fiyatın %1..5'i içindeki kümülatif emir tutarı (5,) ya da None (ne ölçüm ne tahmin)."""
        key = (sym, int(ms), side, causal)
        if key not in self._cache:
            got = self._measured(sym, ms, side, causal)
            if got is not None:
                self.measured += 1
            else:
                got = self._proxy(sym, ms, side)
                if got is not None:
                    self.proxied += 1
            self._cache[key] = got
        return self._cache[key]

    # ---------------------------------------------------------------- kullanım
    def cap_depth(self, sym: str, ms: int) -> float:
        """Girişten önceki 2 dk'da fiyatın ±%1 içindeki alış ve satış emirlerinden küçük olanı (USDT); yoksa sonsuz."""
        ask, bid = self.profile(sym, ms, "ask", causal=True), self.profile(sym, ms, "bid", causal=True)
        if ask is None or bid is None:
            return math.inf
        return float(min(ask[0], bid[0]))

    def impact(self, sym: str, ms: int, side: str, q: float) -> float:
        """q USDT'lik piyasa emrinin o andaki defterde kayması (%); derinlik bilinmiyorsa 0."""
        prof = self.profile(sym, ms, side)
        return impact_pct(q, prof) if prof is not None else 0.0
