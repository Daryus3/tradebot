"""
Vadeli işlemde "en hacimli N coin" evreni (FUTURES_UNIVERSE).

Canlı bot her taramada Binance USDⓈ-M'nin 24 saatlik özetlerinden (tek istek) en yüksek USDT hacimli N kripto
USDT süresiz paritesini seçer (FuturesExchange.top_volume). Backtest aynı kuralı geçmişe uygular: her ay, bir önceki
ayın USDT hacmine göre ilk N parite (Binance'in herkese açık arşivi; sonradan kaldırılan pariteler dahil, böylece
"bugün hayatta olanlar" yanılgısı oluşmaz).

Evrene girmeyenler (canlı ve backtestte aynı):
  - Stabil coinler ve endeks / sepet kontratları (USDC, BTCDOM, DEFI ...),
  - Kripto olmayan pariteler: Binance 2026'da hisse, ETF ve emtia vadelileri açtı (TSLA, NVDA, SPY, CL, XAU ...);
    çöküş dönüşü bunlarda test edilmedi (canlıda Binance'in underlyingType alanı da kontrol edilir),
  - Çok yeni pariteler (canlı: 30 günden yeni; backtest: önceki ay içinde listelenenler),
  - Kaldırılması planlanan pariteler (canlı: teslim / kapanış tarihi 30 günden yakın).
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

DAY_MS = 86_400_000
UNIVERSE_MIN_AGE_DAYS = 30           # Canlı: bundan yeni pariteler evrene girmez (listeleme sonrası aşırı oynaklık)
DELIST_GUARD_DAYS = 30               # Canlı: kapanış (teslim) tarihi bundan yakın pariteler evrene girmez
STABLE_OR_INDEX = frozenset({
    "USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "USDE", "RLUSD", "USD1", "EUR", "GBP", "AUD",
    "BTCDOM", "DEFI", "FOOTBALL", "BLUEBIRD", "BTCST",
})
NON_CRYPTO = frozenset({                # Hisse / ETF / emtia vadelileri (Binance TradFi kontratları)
    "XAU", "XAG", "XPT", "XPD", "PAXG", "XAUT", "CL", "BZ", "NATGAS",
    "TSLA", "COIN", "CRCL", "HOOD", "INTC", "MSTR", "PLTR", "GOOGL", "META", "NVDA", "AAPL", "AMZN", "MSFT", "NFLX",
    "BABA", "GME", "MU", "SNDK", "AMD", "ARM", "CBRS", "DRAM", "LITE", "MRVL", "NBIS", "ORCL", "RKLB", "AAOI",
    "AXTI", "DELL", "GLW", "SAMSUNG", "SKHYNIX", "SKHY", "ZHIPU", "UNITREE",
    "EWY", "QQQ", "SPY", "TQQQ", "SOXL", "SOXS", "SPCX", "KORU", "MVLL", "MUU", "SNXX",
})
EXCLUDED_BASES = STABLE_OR_INDEX | NON_CRYPTO
NO_DELIVERY_MS = 4_133_404_800_000   # Binance: süresiz kontratlarda "teslim tarihi" 2100 yılıdır


def base_of(symbol: str, quote: str = "USDT") -> str:
    return symbol[:-len(quote)] if symbol.endswith(quote) and len(symbol) > len(quote) else symbol


def eligible_base(base: str) -> bool:
    return bool(base) and base.upper() not in EXCLUDED_BASES


def _int(value) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def market_eligible(m: dict, now_ms: int, quote: str = "USDT") -> bool:
    """CCXT market kaydı (Binance USDⓈ-M) en hacimli N evrenine girebilir mi?"""
    info = m.get("info") or {}
    if not (m.get("swap") and m.get("linear") and m.get("quote") == quote and m.get("active", True)):
        return False
    if (info.get("contractType") or "PERPETUAL") != "PERPETUAL" or (info.get("status") or "TRADING") != "TRADING":
        return False
    if (info.get("underlyingType") or "COIN").upper() != "COIN":     # INDEX, hisse / emtia kontratları
        return False
    if not eligible_base(str(m.get("base") or info.get("baseAsset") or "")):
        return False
    created = _int(m.get("created")) or _int(info.get("onboardDate"))
    if created and now_ms - created < UNIVERSE_MIN_AGE_DAYS * DAY_MS:
        return False
    delivery = _int(info.get("deliveryDate"))
    if delivery and delivery < NO_DELIVERY_MS and delivery - now_ms < DELIST_GUARD_DAYS * DAY_MS:
        return False
    return True


# ===================================================================== backtest: geçmiş hacim sıralaması
LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"   # Arşivin dosya listesi (S3)
SETTLE_DAYS = 3                     # Biten ayın aylık dosyası birkaç gün içinde yayımlanır
ARCHIVE_FIRST_MONTH = "2020-01"     # Arşivdeki ilk ay: o ay listelenmiş görünen coinler aslında daha eski
DAYS_PER_MONTH = 30.44


def month_add(key: str, n: int) -> str:
    y, m = int(key[:4]), int(key[5:])
    i = y * 12 + m - 1 + n
    return f"{i // 12}-{i % 12 + 1:02d}"


def month_diff(a: str, b: str) -> int:
    """a - b (ay)."""
    return (int(a[:4]) * 12 + int(a[5:])) - (int(b[:4]) * 12 + int(b[5:]))


def months_between(start_ms: int, end_ms: int) -> List[str]:
    """[start, end) aralığına dokunan takvim ayları ('2024-03')."""
    a = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    key, out = f"{a.year}-{a.month:02d}", []
    while month_ms(key) < end_ms:
        out.append(key)
        key = month_add(key, 1)
    return out


def _month_of(ms: float) -> str:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f"{d.year}-{d.month:02d}"


def month_ms(key: str) -> int:
    return int(datetime(int(key[:4]), int(key[5:]), 1, tzinfo=timezone.utc).timestamp() * 1000)


class VolumeHistory:
    """Binance arşivindeki tüm USDⓈ-M USDT süresiz paritelerinin (kaldırılanlar dahil) aylık USDT hacmi.

    Kaynak: her paritenin günlük mumlarının aylık dosyaları (data/futures/um/monthly/klines/<P>/1d/). Sonuçlar
    önbelleğe yazılır (<cache>/_universe/): ikinci çalıştırmada yalnızca yeni aylar indirilir."""

    def __init__(self, archive, listing_url: str = LISTING_URL, workers: int = 16,
                 progress: Optional[Callable[[str], None]] = None):
        self.archive = archive
        self.listing_url = listing_url.rstrip("/")
        self.workers = workers
        self.say = progress or (lambda _m: None)
        self.dir = Path(archive.cache_dir) / "_universe"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _xml(self, params: str) -> str:
        content = self.archive._get(f"{self.listing_url}?{params}")
        if content is None:
            raise RuntimeError(f"arşiv listesi alınamadı ({params[:60]})")
        return content.decode("utf-8", errors="replace")

    def _listing(self, prefix: str, delimiter: bool) -> List[str]:
        out, marker = [], ""
        while True:
            xml = self._xml(f"prefix={prefix}&marker={marker}" + ("&delimiter=/" if delimiter else ""))
            items = re.findall(r"<Prefix>([^<]*)</Prefix>" if delimiter else r"<Key>([^<]*)</Key>", xml)
            out += [x for x in items if x != prefix]
            if "<IsTruncated>true</IsTruncated>" not in xml:
                return out
            nxt = re.search(r"<NextMarker>([^<]*)</NextMarker>", xml)
            marker = nxt.group(1) if nxt else (items[-1] if items else "")
            if not marker:
                return out

    def symbols(self, max_age_days: float = 7.0) -> Dict[str, List[str]]:
        """Parite → arşivdeki aylar (günlük mum dosyası olan). Liste 7 günde bir yenilenir."""
        path = self.dir / "months.json"
        if path.exists() and time.time() - path.stat().st_mtime < max_age_days * 86400:
            return json.loads(path.read_text(encoding="utf-8"))
        self.say("📥 Vadeli parite listesi (arşiv) okunuyor...")
        dirs = self._listing("data/futures/um/monthly/klines/", delimiter=True)
        names = sorted({d.rstrip("/").split("/")[-1] for d in dirs})
        names = [s for s in names if s.endswith("USDT") and "_" not in s and eligible_base(base_of(s))]

        def months_of(sym: str):
            keys = self._listing(f"data/futures/um/monthly/klines/{sym}/1d/", delimiter=False)
            return sym, sorted(k.rsplit("-1d-", 1)[1][:7] for k in keys if k.endswith(".zip"))

        with ThreadPoolExecutor(self.workers) as pool:
            result = {s: ms for s, ms in pool.map(months_of, names) if ms}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(result), encoding="utf-8")
        tmp.replace(path)
        return result

    def volumes(self, months: Sequence[str], available: Dict[str, List[str]]) -> Dict[str, Dict[str, float]]:
        """Parite → {ay: USDT hacmi} (yalnızca istenen aylar)."""
        path = self.dir / "volumes.json"
        cache: Dict[str, Dict[str, float]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        want = set(months)
        jobs = [(s, m) for s, ms in available.items() for m in ms if m in want and m not in cache.get(s, {})]
        if jobs:
            self.say(f"📥 Aylık hacimler: {len(jobs)} dosya (ilk seferde birkaç dakika sürebilir)")

            def load(job):
                s, m = job
                arr = self.archive.month(s, "1d", int(m[:4]), int(m[5:]))
                return s, m, float(arr["qv"].sum()) if len(arr["t"]) else 0.0

            fresh: Dict[str, Dict[str, float]] = {}
            with ThreadPoolExecutor(self.workers) as pool:
                for s, m, qv in pool.map(load, jobs):
                    fresh.setdefault(s, {})[m] = qv
            settled = month_add(_month_of(time.time() * 1000 - SETTLE_DAYS * DAY_MS), -1)
            for s, ms in fresh.items():          # Henüz yayımlanmamış olabilecek aylar kalıcı önbelleğe yazılmaz
                cache.setdefault(s, {}).update({m: v for m, v in ms.items() if m <= settled})
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            tmp.replace(path)
            for s, ms in fresh.items():
                cache.setdefault(s, {}).update(ms)
        return {s: {m: v for m, v in ms.items() if m in want} for s, ms in cache.items()}


def rank_month(key: str, n: int, min_age_months: int, avg_months: int, available: Dict[str, List[str]],
               vols: Dict[str, Dict[str, float]]) -> List[str]:
    """O ayın evreni: en az min_age_months aydır listelenen (arşiv başlangıcındakiler eski sayılır) ve önceki ay
    işlem gören paritelerden, önceki avg_months ayın ortalama USDT hacmine göre ilk n."""
    prev = month_add(key, -1) if key > ARCHIVE_FIRST_MONTH else key
    if not any(v.get(prev, 0.0) > 0 for v in vols.values()) and prev > ARCHIVE_FIRST_MONTH:
        prev = month_add(prev, -1)                   # Ay başı: önceki ayın dosyaları henüz yayımlanmamış
    rows = []
    for s, ms in available.items():
        first = ms[0]
        if first != ARCHIVE_FIRST_MONTH and month_diff(key, first) < min_age_months:
            continue
        v = vols.get(s, {})
        if v.get(prev, 0.0) <= 0:
            continue
        window = [v[m] for m in (month_add(prev, -i) for i in range(avg_months)) if v.get(m, 0.0) > 0]
        rows.append((sum(window) / len(window), s))
    rows.sort(reverse=True)
    return [base_of(s) for _q, s in rows[:n]]


def universe_months(cfg, start_ms: int, end_ms: int, archive,
                    progress: Optional[Callable[[str], None]] = None) -> List[Tuple[int, int, Tuple[str, ...]]]:
    """Backtest evreni: [(başlangıç ms, bitiş ms, coinler)] — her takvim ayı için ayrı liste."""
    hist = VolumeHistory(archive, progress=progress)
    available = hist.symbols()
    keys = months_between(start_ms, end_ms)
    age = max(0, round(cfg.futures_universe_min_age_days / DAYS_PER_MONTH))
    avg = max(1, round(cfg.futures_universe_avg_days / DAYS_PER_MONTH))
    need = sorted({month_add(k, -i) for k in keys for i in range(1, avg + 2)} | set(keys[:1]))
    vols = hist.volumes(need, available)
    out = []
    for k in keys:
        a, b = max(start_ms, month_ms(k)), min(end_ms, month_ms(month_add(k, 1)))
        if a < b:
            out.append((a, b, tuple(rank_month(k, cfg.futures_universe, age, avg, available, vols))))
    return out


def daily_volumes(archive, start_ms: int, end_ms: int, available: Dict[str, List[str]], workers: int = 16):
    """Tüm paritelerin günlük USDT hacimleri [start_ms - 1 gün, end_ms): (gün başı ms dizisi, pariteler, hacim matrisi
    gün × parite, paritenin arşivdeki ilk işlem günü ms; arşivin ilk ayında görünenler 0). Kaynak: günlük mumların
    aylık dosyaları (önbellekte)."""
    import numpy as np
    first_day = start_ms // DAY_MS * DAY_MS - DAY_MS
    days = np.arange(first_day, end_ms, DAY_MS, dtype=np.int64)
    keys = set(months_between(first_day, end_ms))
    symbols = sorted(available)
    qv = np.zeros((len(days), len(symbols)), dtype=np.float64)
    born = np.full(len(symbols), np.iinfo(np.int64).max, dtype=np.int64)

    def load(j: int):
        symbol = symbols[j]
        months = available[symbol]
        arrs = [archive.month(symbol, "1d", int(m[:4]), int(m[5:])) for m in months if m in keys]
        if months[0] == ARCHIVE_FIRST_MONTH:            # Arşivin ilk ayında görünenler daha eskidir
            return j, arrs, 0
        first = archive.month(symbol, "1d", int(months[0][:4]), int(months[0][5:]))
        return j, arrs, int(first["t"][0]) if len(first["t"]) else None

    with ThreadPoolExecutor(workers) as pool:
        for j, arrs, start in pool.map(load, range(len(symbols))):
            if start is not None:
                born[j] = start
            for a in arrs:
                if not len(a["t"]):
                    continue
                idx = (a["t"] - first_day) // DAY_MS
                ok = (idx >= 0) & (idx < len(days))
                qv[idx[ok], j] = a["qv"][ok]
    return days, symbols, qv, born


def universe_days(cfg, start_ms: int, end_ms: int, archive,
                  progress: Optional[Callable[[str], None]] = None) -> List[Tuple[int, int, Tuple[str, ...]]]:
    """FUTURES_UNIVERSE_MODE=daily backtest evreni: her UTC günü için bir önceki günün USDT hacmine göre ilk N parite
    (canlı: her gün 00:00 UTC'de, kapanmış günlük mumlardan). En az FUTURES_UNIVERSE_MIN_AGE_DAYS gündür işlem gören
    pariteler (arşivin ilk ayında, 2020-01'de görünenler eski sayılır); stabil coin / endeks / hisse pariteleri hariç."""
    import numpy as np
    say = progress or (lambda _m: None)
    hist = VolumeHistory(archive, progress=progress)
    available = hist.symbols()
    say(f"📥 Günlük hacimler: {len(available)} parite (önbellekten)")
    days, symbols, qv, born = daily_volumes(archive, start_ms, end_ms, available)
    n, age_ms = int(cfg.futures_universe), int(cfg.futures_universe_min_age_days) * DAY_MS
    bases = [base_of(s) for s in symbols]
    out = []
    for i in range(1, len(days)):
        a, b = max(start_ms, int(days[i])), min(end_ms, int(days[i]) + DAY_MS)
        if a >= b:
            continue
        prev = qv[i - 1]
        ok = (prev > 0) & (born <= int(days[i]) - age_ms)
        cand = np.flatnonzero(ok)
        top = cand[np.argsort(-prev[cand], kind="stable")[:n]]
        out.append((a, b, tuple(bases[j] for j in top)))
    return out
