"""
Tehlikeli coin filtresi.

Toparlanma stratejisi için riskli olan coinleri alımdan önce eler:
  - Binance delist (listeden çıkarma) takvimindeki pariteler,
  - Çok yeni listelenmiş coinler (günlük mum sayısı MIN_LISTING_DAYS'ten az),
  - Kısa sürede aşırı pompalanmış coinler (pompa sonrası sönüş genelde toparlanmaz).

Ağ hatalarında filtre "açık" davranır (fail-open): veri alınamazsa coin elenmez,
sadece log'a yazılır. Böylece Binance'in tek bir uç noktası bozulsa da bot durmaz.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from binance_client import RateLimitError
from logger import get_logger

log = get_logger()

DELIST_PATH = "/sapi/v1/spot/delist-schedule"
DELIST_RETRY_SEC = 600   # Delist takvimi alınamazsa bu kadar sonra tekrar dene


def _fmt_left(seconds: float) -> str:
    """Kalan süreyi '2 gün 3 sa' / '5 sa 10 dk' / '12 dk' biçiminde yazar."""
    minutes = max(0, int(seconds // 60))
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} gün {hours} sa"
    if hours:
        return f"{hours} sa {mins} dk"
    return f"{mins} dk"


class CoinSafetyFilter:
    """Delist takvimi, yeni listeleme ve pompa kontrolü (önbellekli)."""

    def __init__(self, client, cfg, time_fn: Callable[[], float] = time.time,
                 kline_cache_sec: float = 21600, delist_refresh_sec: float = 21600):
        self.client = client
        self.cfg = cfg
        self.time_fn = time_fn
        self.kline_cache_sec = kline_cache_sec
        self.delist_refresh_sec = delist_refresh_sec
        self._delist: Dict[str, int] = {}                 # parite -> delist zamanı (ms)
        self._next_delist_at: float = 0.0                 # Bir sonraki takvim yenileme zamanı
        self._daily: Dict[str, Tuple[float, List[list]]] = {}  # parite -> (alınma zamanı, günlük mumlar)

    # ------------------------------------------------------------ delist takvimi
    @property
    def delisting(self) -> Dict[str, int]:
        """Delist takvimindeki pariteler: {sembol: delist zamanı (ms)} (kopya)."""
        return dict(self._delist)

    def refresh_delist(self, force: bool = False) -> None:
        """Binance delist takvimini en fazla `delist_refresh_sec` saniyede bir yeniler.

        API anahtarı yoksa (uç nokta anahtar ister) sessizce atlanır. Hata olursa
        eldeki liste korunur ve kısa süre sonra yeniden denenir.
        """
        if not self.cfg.delist_filter or not getattr(self.client, "api_key", ""):
            return
        now = self.time_fn()
        if not force and now < self._next_delist_at:
            return
        try:
            data = self.client._request("GET", DELIST_PATH)
            schedule = self._parse_schedule(data)
        except RateLimitError:
            raise  # Binance istek limiti: tarama durmalı, döngü beklesin
        except Exception as exc:  # noqa: BLE001 - takvim alınamazsa eski liste kullanılır
            log.debug("Delist takvimi alınamadı (eski liste korunuyor): %s", exc)
            self._next_delist_at = now + min(DELIST_RETRY_SEC, self.delist_refresh_sec)
            return
        added = sorted(set(schedule) - set(self._delist))
        if added:
            log.info("Delist takvimine giren pariteler: %s", ", ".join(added))
        self._delist = schedule
        self._next_delist_at = now + self.delist_refresh_sec

    @staticmethod
    def _parse_schedule(data) -> Dict[str, int]:
        """[{"delistTime": ms, "symbols": [...]}, ...] → {sembol: en erken delist zamanı}."""
        if not isinstance(data, list):
            raise ValueError(f"beklenmeyen delist yanıtı: {str(data)[:100]}")
        out: Dict[str, int] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                when = int(item.get("delistTime") or 0)
            except (TypeError, ValueError):
                when = 0
            for sym in item.get("symbols") or []:
                if not isinstance(sym, str) or not sym:
                    continue
                key = sym.upper()
                prev = out.get(key)
                if prev is None or (when and (not prev or when < prev)):  # En erken bilinen zaman
                    out[key] = when
        return out

    # ------------------------------------------------------------ günlük mumlar
    def _daily_klines(self, symbol: str) -> List[list]:
        cfg = self.cfg
        now = self.time_fn()
        cached = self._daily.get(symbol)
        if cached is not None and now - cached[0] < self.kline_cache_sec:
            return cached[1]
        limit = max(cfg.min_listing_days, cfg.pump_lookback_days * 2) + 2
        rows = self.client.klines(symbol, "1d", limit=limit) or []
        # Süresi dolmuş kayıtları temizle (bellek sınırsız büyümesin)
        for sym in [s for s, (at, _) in self._daily.items() if now - at >= self.kline_cache_sec]:
            del self._daily[sym]
        self._daily[symbol] = (now, rows)
        return rows

    def _pump_pct(self, rows: List[list]) -> Optional[float]:
        """Son N gündeki en yüksek fiyatın, önceki N gündeki en düşük fiyata göre yükselişi (%)."""
        n = self.cfg.pump_lookback_days
        if n <= 0 or len(rows) < 2 * n:
            return None
        recent, before = rows[-n:], rows[-2 * n:-n]
        top = max(float(r[2]) for r in recent)
        bottom = min(float(r[3]) for r in before)
        if bottom <= 0:
            return None
        return (top / bottom - 1) * 100

    # ------------------------------------------------------------ ana kontrol
    def check(self, symbol: str) -> Tuple[bool, str]:
        """(güvenli_mi, neden). Neden metni tarama raporunda eleme nedeni olarak görünür."""
        cfg = self.cfg
        if not cfg.delist_filter:
            return True, ""
        if symbol.upper() in self._delist:
            return False, "delist takviminde"
        try:
            rows = self._daily_klines(symbol)
            if cfg.min_listing_days > 0 and len(rows) < cfg.min_listing_days:
                return False, f"yeni listelendi ({len(rows)} gün)"
            pump = self._pump_pct(rows) if cfg.pump_filter_pct > 0 else None
        except RateLimitError:
            raise  # Binance istek limiti: tarama durmalı, döngü beklesin
        except Exception as exc:  # noqa: BLE001 - veri yoksa coini eleme (fail-open)
            log.debug("%s günlük mumları alınamadı, güvenlik filtresi atlandı: %s", symbol, exc)
            return True, ""
        if pump is not None and pump >= cfg.pump_filter_pct:
            return False, f"pompa sonrası (+%{pump:.0f})"
        return True, ""

    # ------------------------------------------------------------ açık pozisyon uyarısı
    def open_position_warnings(self, symbols: Iterable[str]) -> List[str]:
        """Açık pozisyonlardan delist takvimine girenler için Telegram uyarı metinleri (düz metin).

        Metin parite adıyla başlar; böylece bildirim anahtarı (ilk 20 karakter) pariteye özgü olur.
        """
        try:
            self.refresh_delist()
        except RateLimitError:
            raise  # Binance istek limiti: tarama durmalı, döngü beklesin
        except Exception as exc:  # noqa: BLE001
            log.debug("Delist takvimi yenilenemedi: %s", exc)
        now = self.time_fn()
        out: List[str] = []
        for symbol in symbols:
            sym = str(symbol).upper()
            when_ms = self._delist.get(sym)
            if when_ms is None:
                continue
            if when_ms > 0:
                when_sec = when_ms / 1000
                when_txt = datetime.fromtimestamp(when_sec).strftime("%d.%m.%Y %H:%M")
                left = when_sec - now
                timing = (f"Delist zamanı: {when_txt} ({_fmt_left(left)} kaldı)." if left > 0
                          else f"Delist zamanı: {when_txt} (geçti!).")
            else:
                timing = "Delist zamanı açıklanmadı."
            out.append(f"{sym} Binance delist takviminde! {timing} İşlemler durmadan önce pozisyonu "
                       f"kapatmanız önerilir (/sat {sym}).")
        return out
