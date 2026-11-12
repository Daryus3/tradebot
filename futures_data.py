"""
Binance USDⓈ-M vadeli işlem geçmiş verisi (Binance'in herkese açık arşivi data.binance.vision).

Vadeli backtest (futures_backtest.py) 5 dakikalık mumları buradan alır: aylık zip dosyaları (bitmiş aylar) +
günlük zip dosyaları (içinde bulunulan ay). İndirilen her dosya önbelleğe (.npz) yazılır; ikinci çalıştırma
internetsiz ve hızlıdır. Arşiv Ocak 2020'de başlar (BTCUSDT vadeli Eylül 2019'da açıldı ama arşivde 2020'den
önceki ay yok).

Her mum: açılış zamanı (ms), açılış / en yüksek / en düşük / kapanış fiyatı, hacim (coin), USDT hacmi,
işlem sayısı ve "taker buy" hacmi (piyasa emriyle ALAN tarafın hacmi). Satan taraf = hacim - taker buy:
likidasyonlar piyasa emriyle kapatıldığı için zorla satış dalgaları burada görünür.
"""
from __future__ import annotations

import io
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import requests

DEFAULT_DATA_URL = "https://data.binance.vision"
FIELDS = ("t", "o", "h", "l", "c", "v", "qv", "n", "tb")
_USECOLS = (0, 1, 2, 3, 4, 5, 7, 8, 9)      # Binance kline CSV sütunları → FIELDS
FIRST_MONTH = (2020, 1)                     # Arşivdeki ilk aylık dosya
MISSING_GRACE_DAYS = 7                      # Biteli bu kadar gün olmuş bir ayın dosyası yoksa coin o ay işlemde değildir
DAY_MS = 86_400_000
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": DAY_MS}


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def empty() -> Dict[str, np.ndarray]:
    out = {k: np.zeros(0, dtype=np.float64) for k in FIELDS}
    out["t"] = np.zeros(0, dtype=np.int64)
    return out


def parse_csv(raw: bytes) -> Dict[str, np.ndarray]:
    """Binance kline CSV (başlıklı ya da başlıksız) → sütun dizileri."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return empty()
    if not text[0].isdigit():                       # 2022 sonrası dosyalarda başlık satırı var
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if not text:
            return empty()
    data = np.loadtxt(io.StringIO(text), delimiter=",", usecols=_USECOLS, dtype=np.float64, ndmin=2)
    out = {k: data[:, i].copy() for i, k in enumerate(FIELDS)}
    t = data[:, 0].astype(np.int64)
    if len(t) and t[0] > 10 ** 14:                  # Mikro saniye ile yazılmış dosya
        t //= 1000
    out["t"] = t
    return out


def concat(parts: List[Dict[str, np.ndarray]]) -> Dict[str, np.ndarray]:
    parts = [p for p in parts if len(p["t"])]
    if not parts:
        return empty()
    out = {k: np.concatenate([p[k] for p in parts]) for k in FIELDS}
    order = np.argsort(out["t"], kind="stable")
    t = out["t"][order]
    keep = np.ones(len(t), dtype=bool)
    keep[1:] = t[1:] != t[:-1]                      # Aynı mum iki dosyada varsa bir kez al
    return {k: v[order][keep] for k, v in out.items()}


def slice_range(arrs: Dict[str, np.ndarray], start_ms: int, end_ms: int) -> Dict[str, np.ndarray]:
    t = arrs["t"]
    i, j = np.searchsorted(t, start_ms, "left"), np.searchsorted(t, end_ms, "left")
    return {k: v[i:j] for k, v in arrs.items()}


def resample(arrs: Dict[str, np.ndarray], interval_ms: int, shift_ms: int = 0) -> Dict[str, np.ndarray]:
    """Küçük mumları büyük muma çevirir (örn. 5 dk → 1 saat). Yalnızca TAMAMLANMIŞ büyük mumlar döner:
    son grup eksikse atılır (canlıda henüz kapanmamış mum kullanılmasın). `shift_ms` > 0: büyük mumlar saat
    başına göre o kadar kaydırılır (ör. 1 dk → 5 dk, shift 2 dk: ...:02-:07, :07-:12 mumları); dakikalık taramada
    ara dakikalarda "o dakikada kapanan 5 dk mumlar" böyle üretilir (canlı bot ve backtest ortak)."""
    t = arrs["t"]
    if not len(t):
        return empty()
    step = int(t[1] - t[0]) if len(t) > 1 else interval_ms
    shift_ms = int(shift_ms) % interval_ms
    bucket = (t - shift_ms) // interval_ms
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    ends = np.r_[starts[1:], len(t)]
    out = {"t": bucket[starts] * interval_ms + shift_ms,
           "o": arrs["o"][starts],
           "h": np.maximum.reduceat(arrs["h"], starts),
           "l": np.minimum.reduceat(arrs["l"], starts),
           "c": arrs["c"][ends - 1]}
    for k in ("v", "qv", "n", "tb"):
        out[k] = np.add.reduceat(arrs[k], starts)
    complete = (t[ends - 1] + step) >= (out["t"] + interval_ms)
    return {k: v[complete] for k, v in out.items()}


class FuturesArchive:
    """data.binance.vision'dan vadeli mum indirir ve önbelleğe yazar."""

    def __init__(self, cache_dir, base_url: str = DEFAULT_DATA_URL, session: Optional[requests.Session] = None,
                 workers: int = 8, now_fn=time.time, timeout: float = 30.0):
        self.cache_dir = Path(cache_dir)
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.workers = max(1, workers)
        self.now_fn = now_fn
        self.timeout = timeout

    # ---------------------------------------------------------------- dosya
    def _get(self, url: str) -> Optional[bytes]:
        """Dosyayı indirir; yoksa (404) None. Geçici hatalarda birkaç kez dener."""
        delay = 2.0
        for attempt in range(5):
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 404 or resp.status_code == 403 and b"NoSuchKey" in resp.content[:500]:
                return None
            if resp.status_code == 200:
                return resp.content
            if attempt == 4:
                resp.raise_for_status()
            time.sleep(delay)
            delay *= 2
        return None

    @staticmethod
    def _unzip(content: bytes) -> bytes:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            return zf.read(zf.namelist()[0])

    def _cached(self, path: Path, url: str, missing_ok_forever: bool) -> Dict[str, np.ndarray]:
        if path.exists():
            with np.load(path) as data:
                return {k: data[k] for k in FIELDS}
        content = self._get(url)
        if content is None:
            if missing_ok_forever:            # Coin o tarihte listelenmemiş / kaldırılmış: tekrar sorma
                self._save(path, empty())
            return empty()
        arrs = parse_csv(self._unzip(content))
        self._save(path, arrs)
        return arrs

    @staticmethod
    def _save(path: Path, arrs: Dict[str, np.ndarray]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp.npz")   # Eşzamanlı yazımlar
        np.savez_compressed(tmp, **arrs)
        tmp.replace(path)

    def month(self, symbol: str, interval: str, year: int, month: int) -> Dict[str, np.ndarray]:
        name = f"{symbol}-{interval}-{year}-{month:02d}"
        url = f"{self.base_url}/data/futures/um/monthly/klines/{symbol}/{interval}/{name}.zip"
        nxt = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
        settled = self.now_fn() - nxt.timestamp() > MISSING_GRACE_DAYS * 86400
        return self._cached(self.cache_dir / symbol / interval / f"{name}.npz", url, settled)

    def day(self, symbol: str, interval: str, day: datetime) -> Dict[str, np.ndarray]:
        name = f"{symbol}-{interval}-{day:%Y-%m-%d}"
        url = f"{self.base_url}/data/futures/um/daily/klines/{symbol}/{interval}/{name}.zip"
        settled = self.now_fn() - (day.timestamp() + 86400) > MISSING_GRACE_DAYS * 86400
        return self._cached(self.cache_dir / symbol / interval / "daily" / f"{name}.npz", url, settled)

    # ---------------------------------------------------------------- aralık
    def plan(self, start_ms: int, end_ms: int) -> List[Tuple[str, Tuple]]:
        """İndirilecek dosyalar: bitmiş aylar aylık, son (bitmemiş ya da henüz yayımlanmamış) ay günlük."""
        start = max(_utc(start_ms), datetime(*FIRST_MONTH, 1, tzinfo=timezone.utc))
        end = _utc(end_ms)
        now = _utc(int(self.now_fn() * 1000))
        jobs: List[Tuple[str, Tuple]] = []
        y, m = start.year, start.month
        while datetime(y, m, 1, tzinfo=timezone.utc) < end:
            nxt = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
            if (now - nxt).days >= 2:          # Aylık dosya ay bittikten 1-2 gün sonra yayımlanır
                jobs.append(("month", (y, m)))
            else:
                d = max(datetime(y, m, 1, tzinfo=timezone.utc), start.replace(hour=0, minute=0, second=0,
                                                                              microsecond=0))
                while d < min(nxt, end) and d + timedelta(days=1) <= now:
                    jobs.append(("day", (d,)))
                    d += timedelta(days=1)
            y, m = nxt.year, nxt.month
        return jobs

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> Tuple[np.ndarray, np.ndarray]:
        """Fonlama oranları (8 saatte bir; long'lar pozitif oranda öder): (zaman ms, oran). Aylık dosyalardan; bitmemiş
        ay için dosya yoksa boş döner (çağıran varsayılan oranı kullanır)."""
        times, rates = [], []
        start = max(_utc(start_ms), datetime(*FIRST_MONTH, 1, tzinfo=timezone.utc))
        y, m = start.year, start.month
        while datetime(y, m, 1, tzinfo=timezone.utc) < _utc(end_ms):
            name = f"{symbol}-fundingRate-{y}-{m:02d}"
            path = self.cache_dir / symbol / "funding" / f"{name}.npz"
            nxt = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
            if path.exists():
                with np.load(path) as data:
                    t, r = data["t"], data["r"]
            else:
                url = f"{self.base_url}/data/futures/um/monthly/fundingRate/{symbol}/{name}.zip"
                content = self._get(url)
                t = np.zeros(0, dtype=np.int64)
                r = np.zeros(0, dtype=np.float64)
                if content is not None:
                    lines = [ln for ln in self._unzip(content).decode().splitlines() if ln and ln[0].isdigit()]
                    if lines:
                        t = np.array([int(ln.split(",")[0]) for ln in lines], dtype=np.int64)
                        r = np.array([float(ln.split(",")[-1]) for ln in lines], dtype=np.float64)
                if content is not None or self.now_fn() - nxt.timestamp() > MISSING_GRACE_DAYS * 86400:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp.npz")
                    np.savez_compressed(tmp, t=t, r=r)
                    tmp.replace(path)
            times.append(t)
            rates.append(r)
            y, m = nxt.year, nxt.month
        if not times:
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64)
        t, r = np.concatenate(times), np.concatenate(rates)
        keep = (t >= start_ms) & (t < end_ms)
        return t[keep], r[keep]

    def klines(self, symbol: str, start_ms: int, end_ms: int, interval: str = "5m",
               progress: Optional[Callable[[str], None]] = None) -> Dict[str, np.ndarray]:
        jobs = self.plan(start_ms, end_ms)

        def run(job):
            kind, args = job
            if kind == "day":
                return self.day(symbol, interval, *args)
            arrs = self.month(symbol, interval, *args)
            y, m = args
            nxt = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
            if not len(arrs["t"]) and self.now_fn() - nxt.timestamp() <= MISSING_GRACE_DAYS * 86400:
                # Aylık dosya henüz yayımlanmamış: aynı ayı günlük dosyalardan topla
                days, d = [], datetime(y, m, 1, tzinfo=timezone.utc)
                while d < nxt:
                    days.append(self.day(symbol, interval, d))
                    d += timedelta(days=1)
                arrs = concat(days)
            return arrs

        if progress:
            progress(f"{symbol} {interval}: {len(jobs)} dosya")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            parts = list(pool.map(run, jobs))
        return slice_range(concat(parts), start_ms, end_ms)
