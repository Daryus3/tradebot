"""coin_filters.CoinSafetyFilter testleri (sahte istemci, internet gerektirmez)."""
from dataclasses import replace

import requests

from binance_client import BinanceAPIError
from coin_filters import DELIST_PATH, CoinSafetyFilter

DAY_MS = 86_400_000
NOW = 1_760_000_000.0  # Sabit test saati (sn)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def day(i, o, h, l, c):
    return [i * DAY_MS, str(o), str(h), str(l), str(c), "1000", i * DAY_MS + DAY_MS - 1, "1000", 10, "500", "500", "0"]


def flat_days(n, price=1.0):
    return [day(i, price, price * 1.02, price * 0.98, price) for i in range(n)]


class FilterClient:
    """Delist takvimi ve günlük mumlar sunan sahte Binance istemcisi."""

    def __init__(self, api_key="key", schedule=None, daily=None):
        self.api_key = api_key
        self.schedule = schedule if schedule is not None else []
        self.daily = dict(daily or {})
        self.requests = []
        self.kline_calls = []
        self.fail_delist = False
        self.fail_klines = False

    def _request(self, method, path, params=None, signed=False):
        self.requests.append((method, path, signed))
        if self.fail_delist:
            raise BinanceAPIError(500, None, "sunucu hatası")
        return self.schedule

    def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
        self.kline_calls.append((symbol, interval, limit))
        if self.fail_klines:
            raise requests.ConnectionError("ağ yok")
        rows = self.daily.get(symbol, flat_days(60))
        return rows[-limit:]


def make(cfg, client, clock=None, **kw):
    return CoinSafetyFilter(client, cfg, time_fn=clock or Clock(), **kw)


# ------------------------------------------------------------------ delist takvimi
def test_delist_schedule_with_key(cfg):
    when = int((NOW + 3 * 86400) * 1000)
    client = FilterClient(schedule=[{"delistTime": when, "symbols": ["ABCUSDT", "xyzusdt"]}])
    f = make(cfg, client)
    f.refresh_delist()
    assert client.requests == [("GET", DELIST_PATH, False)]
    assert f.delisting == {"ABCUSDT": when, "XYZUSDT": when}
    assert f.check("ABCUSDT") == (False, "delist takviminde")
    assert client.kline_calls == []  # Delist olan coin için mum isteği yapılmaz
    assert f.check("DEFUSDT") == (True, "")


def test_delist_keeps_earliest_time_for_duplicates(cfg):
    client = FilterClient(schedule=[{"delistTime": 2000, "symbols": ["ABCUSDT"]},
                                    {"delistTime": 1000, "symbols": ["ABCUSDT"]}])
    f = make(cfg, client)
    f.refresh_delist()
    assert f.delisting == {"ABCUSDT": 1000}


def test_delist_without_key_is_skipped(cfg):
    client = FilterClient(api_key="", schedule=[{"delistTime": 1, "symbols": ["ABCUSDT"]}])
    f = make(cfg, client)
    f.refresh_delist(force=True)
    assert client.requests == [] and f.delisting == {}
    assert f.check("ABCUSDT") == (True, "")


def test_delist_refresh_is_throttled(cfg):
    clock = Clock()
    client = FilterClient(schedule=[{"delistTime": 1, "symbols": ["ABCUSDT"]}])
    f = make(cfg, client, clock, delist_refresh_sec=3600)
    f.refresh_delist()
    f.refresh_delist()
    assert len(client.requests) == 1
    clock.t += 3599
    f.refresh_delist()
    assert len(client.requests) == 1
    f.refresh_delist(force=True)
    assert len(client.requests) == 2
    clock.t += 3600
    f.refresh_delist()
    assert len(client.requests) == 3


def test_delist_error_keeps_old_list(cfg):
    clock = Clock()
    client = FilterClient(schedule=[{"delistTime": 1, "symbols": ["ABCUSDT"]}])
    f = make(cfg, client, clock)
    f.refresh_delist()
    client.fail_delist = True
    f.refresh_delist(force=True)
    assert f.delisting == {"ABCUSDT": 1}
    # Beklenmeyen yanıt biçimi de eski listeyi bozmaz
    client.fail_delist = False
    client.schedule = {"code": -1, "msg": "bozuk"}
    f.refresh_delist(force=True)
    assert f.delisting == {"ABCUSDT": 1}
    # Hata sonrası kısa sürede (10 dk) yeniden denenir, 6 saat beklenmez
    client.schedule = []
    clock.t += 601
    f.refresh_delist()
    assert f.delisting == {}


def test_delisting_property_is_a_copy(cfg):
    client = FilterClient(schedule=[{"delistTime": 1, "symbols": ["ABCUSDT"]}])
    f = make(cfg, client)
    f.refresh_delist()
    f.delisting.clear()
    assert f.delisting == {"ABCUSDT": 1}


# ------------------------------------------------------------------ yeni listeleme / pompa
def test_new_listing_rejected(cfg):
    client = FilterClient(daily={"NEWUSDT": flat_days(10)})
    f = make(cfg, client)
    assert f.check("NEWUSDT") == (False, "yeni listelendi (10 gün)")
    limit = max(cfg.min_listing_days, cfg.pump_lookback_days * 2) + 2
    assert client.kline_calls == [("NEWUSDT", "1d", limit)]


def test_old_enough_coin_passes(cfg):
    client = FilterClient(daily={"OLDUSDT": flat_days(cfg.min_listing_days)})
    assert make(cfg, client).check("OLDUSDT") == (True, "")


def test_pump_rejected(cfg):
    rows = flat_days(17)
    # Son 3 günde 1.0 → 1.6 (önceki 3 günün dibi 0.98)
    rows += [day(17, 1.0, 1.3, 1.0, 1.3), day(18, 1.3, 1.6, 1.25, 1.4), day(19, 1.4, 1.45, 1.1, 1.15)]
    f = make(cfg, FilterClient(daily={"PMPUSDT": rows}))
    ok, why = f.check("PMPUSDT")
    assert not ok and why == "pompa sonrası (+%63)"
    assert why.split(" (")[0] == "pompa sonrası"  # Tarama raporundaki eleme kategorisi


def test_moderate_rise_passes_and_pump_threshold_configurable(cfg):
    rows = flat_days(17) + [day(17, 1.0, 1.2, 1.0, 1.2), day(18, 1.2, 1.3, 1.15, 1.25), day(19, 1.25, 1.28, 1.2, 1.2)]
    client = FilterClient(daily={"RISEUSDT": rows})
    assert make(cfg, client).check("RISEUSDT") == (True, "")  # +%32.7 < %50
    strict = replace(cfg, pump_filter_pct=30.0)
    ok, why = make(strict, client).check("RISEUSDT")
    assert not ok and why.startswith("pompa sonrası")
    off = replace(cfg, pump_filter_pct=0.0)
    assert make(off, client).check("RISEUSDT") == (True, "")


def test_pump_only_looks_at_recent_window(cfg):
    # Pompa 10 gün önceydi; son 6 gün sakin → elenmez
    rows = flat_days(8) + [day(8, 1.0, 3.0, 1.0, 2.0)] + flat_days(11, price=2.0)
    assert make(cfg, FilterClient(daily={"OLDPUSDT": rows})).check("OLDPUSDT") == (True, "")


# ------------------------------------------------------------------ önbellek / hata
def test_daily_klines_are_cached(cfg):
    clock = Clock()
    client = FilterClient()
    f = make(cfg, client, clock, kline_cache_sec=600)
    assert f.check("ABCUSDT") == (True, "")
    assert f.check("ABCUSDT") == (True, "")
    assert len(client.kline_calls) == 1  # İkinci çağrıda istek yok
    f.check("DEFUSDT")
    assert len(client.kline_calls) == 2
    clock.t += 600
    f.check("ABCUSDT")
    assert len(client.kline_calls) == 3


def test_network_error_fails_open(cfg):
    client = FilterClient()
    client.fail_klines = True
    f = make(cfg, client)
    assert f.check("ABCUSDT") == (True, "")
    client.fail_klines = False
    client.daily["ABCUSDT"] = flat_days(5)
    # Hata önbelleğe alınmaz: veri gelince kontrol yapılır
    assert f.check("ABCUSDT") == (False, "yeni listelendi (5 gün)")


def test_filter_disabled_always_passes(cfg):
    off = replace(cfg, delist_filter=False)
    client = FilterClient(schedule=[{"delistTime": 1, "symbols": ["ABCUSDT"]}], daily={"ABCUSDT": flat_days(2)})
    f = make(off, client)
    f.refresh_delist(force=True)
    assert f.check("ABCUSDT") == (True, "")
    assert client.requests == [] and client.kline_calls == []


# ------------------------------------------------------------------ açık pozisyon uyarısı
def test_open_position_warnings(cfg):
    soon = int((NOW + 2 * 86400 + 3 * 3600 + 120) * 1000)
    past = int((NOW - 3600) * 1000)
    client = FilterClient(schedule=[{"delistTime": soon, "symbols": ["ABCUSDT"]},
                                    {"delistTime": past, "symbols": ["OLDUSDT"]}])
    f = make(cfg, client)
    warnings = f.open_position_warnings(["ABCUSDT", "ETHUSDT", "OLDUSDT"])  # Takvimi kendisi yükler
    assert len(warnings) == 2
    abc, old = warnings
    assert abc.startswith("ABCUSDT ") and "delist" in abc and "2 gün 3 sa kaldı" in abc and "/sat ABCUSDT" in abc
    assert old.startswith("OLDUSDT ") and "geçti" in old
    assert f.open_position_warnings(["ETHUSDT"]) == []
    assert len(client.requests) == 1  # Yenileme yine de seyreltilir


def test_open_position_warnings_without_key(cfg):
    f = make(cfg, FilterClient(api_key=""))
    assert f.open_position_warnings(["ABCUSDT"]) == []
