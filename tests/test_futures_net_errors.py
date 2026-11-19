"""Taramada ağ hatası (zaman aşımı, bağlantı kopması): tek tük coinin mumu alınamayınca o coin atlanır, ötekiler taranır
(eskiden tek coinin hatası taramanın tamamını düşürüp Telegram'a hata yazdırıyordu). Çok coinde ya da hepsinde ağ hatası
ağ kesintisi sayılır (ana döngü bekler); Binance'in istek sınırı (429 / 418) eskisi gibi taramayı durdurur. Sahte borsayla;
gerçek emir yok."""
import ccxt
import requests

import futures_trader as ft
from futures_exchange import rate_limited
from test_futures import fcfg  # noqa: F401 - pytest fikstürü
from test_futures import Clock, make_fbot
from test_futures_cascade import live_cfg
from test_futures_fast_entry import CLOSE, CLOSES, FastCcxt, fast_cfg, journal, stepping
from test_futures_snapshot_scan import SnapCcxt, run_until_prepared, start


def timeout_error(sym):
    return ccxt.RequestTimeout(f"binance GET https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=5m&limit=300")


class FlakyCcxt(FastCcxt):
    """FastCcxt + mum isteğinde ağ hatası: `fail` parite → kaç istek daha zaman aşımı verecek, `limited` parite →
    istek sınırı hatası (429)."""

    def __init__(self, closes, fail=None, limited=(), **kw):
        super().__init__(closes, **kw)
        self.fail = dict(fail or {})
        self.limited = set(limited)

    def fapiPublicGetKlines(self, params):
        sym = params["symbol"]
        if sym in self.limited:
            self.calls.append(("klines", sym, params["interval"]))
            raise ccxt.RateLimitExceeded('binance {"code":-1003,"msg":"Too many requests"}')
        if self.fail.get(sym, 0) > 0:
            self.fail[sym] -= 1
            self.calls.append(("klines", sym, params["interval"]))
            raise timeout_error(sym)
        return super().fapiPublicGetKlines(params)


def fast_bot(fcfg, tmp_path, fast=True, **kw):
    clock = Clock(CLOSE + (1 if fast else 3))
    fake = FlakyCcxt(CLOSES, clock=clock, **kw)
    cfg = fast_cfg(fcfg) if fast else live_cfg(fcfg)
    bot, fake, notes, clock = make_fbot(cfg, tmp_path, fake=fake, clock=clock)
    stepping(bot, clock)
    fake.calls.clear()
    bot.next_scan_at = clock.t
    return bot, fake, notes


def asked(fake, symbol):
    return sum(1 for c in fake.calls if c[0] == "klines" and c[1] == symbol)


def test_one_network_error_is_retried_once_and_the_scan_goes_on(fcfg, tmp_path):
    bot, fake, notes = fast_bot(fcfg, tmp_path, fail={"ADAUSDT": 1})
    bot.tick()                                                             # Hata yükselmez
    row = journal(bot)[-1]
    assert asked(fake, "ADAUSDT") == 2 and "e" not in row and "ADA" in row["a"]
    assert row["s"] == {"SOL": "açıldı"} and set(bot.state.positions) == {"SOLUSDT"}
    assert not any("ConnectionError" in m for m in notes.messages)


def test_persistent_network_error_on_one_coin_skips_only_that_coin(fcfg, tmp_path, caplog):
    bot, fake, notes = fast_bot(fcfg, tmp_path, fail={"ADAUSDT": 99})
    bot.tick()
    row = journal(bot)[-1]
    assert asked(fake, "ADAUSDT") == 1 + ft.KLINE_NET_RETRIES                # Bir kez yeniden istenir, sonra bırakılır
    assert row["e"] == 1 and "ADA" not in row["a"] and row["s"] == {"SOL": "açıldı"}
    assert not any("ConnectionError" in m for m in notes.messages)          # Telegram'a hata yazılmaz
    assert "1 coinin mumları alınamadı (ADAUSDT: ADAUSDT mumları alınamadı: binance GET" in caplog.text
    assert bot.last_scan.rejects["veri alınamadı"] == 1


def test_network_errors_on_many_coins_still_count_as_an_outage(fcfg, tmp_path):
    fail = {s: 99 for s in ("ADAUSDT", "XRPUSDT", "DOGEUSDT")}              # KLINE_NET_MAX_MISSING (2) aşıldı
    bot, fake, _notes = fast_bot(fcfg, tmp_path, fail=fail)
    try:
        bot.tick()
        raised = False
    except requests.ConnectionError:
        raised = True                                                       # Ana döngü ağ kesintisi gibi ele alır
    assert raised and not bot.state.positions
    assert journal(bot)[-1]["skip"].startswith("tarama hatası (ConnectionError: ")


def test_rate_limit_error_stops_the_scan_without_retrying(fcfg, tmp_path):
    bot, fake, _notes = fast_bot(fcfg, tmp_path, limited={"ADAUSDT"})
    try:
        bot.tick()
        raised = None
    except requests.ConnectionError as exc:
        raised = exc
    assert raised is not None and rate_limited(raised) and asked(fake, "ADAUSDT") == 1
    assert not bot.state.positions
    plain = requests.ConnectionError("x")
    assert not rate_limited(plain) and not rate_limited(ValueError("x"))


def test_old_path_skips_the_failing_coin_without_retrying(fcfg, tmp_path):
    bot, fake, _notes = fast_bot(fcfg, tmp_path, fast=False, fail={"XRPUSDT": 99})
    bot.tick()
    row = journal(bot)[-1]
    assert asked(fake, "XRPUSDT") == 1 and row["e"] == 1 and "XRP" not in row["a"]
    assert set(bot.state.positions) == {"SOLUSDT"}


class FlakySnapCcxt(SnapCcxt):
    """SnapCcxt + geçmiş mum isteğinde ağ hatası (ön hazırlıkta)."""

    def __init__(self, closes, clock, fail=(), **kw):
        super().__init__(closes, clock, **kw)
        self.fail = set(fail)

    def fapiPublicGetKlines(self, params):
        if params["symbol"] in self.fail:
            self.calls.append(("klines", params["symbol"], params["interval"]))
            raise timeout_error(params["symbol"])
        return super().fapiPublicGetKlines(params)


def test_snapshot_prepare_counts_a_network_error_as_one_missing_coin(fcfg, tmp_path, monkeypatch):
    monkeypatch.setattr("test_futures_snapshot_scan.SnapCcxt", FlakySnapCcxt)
    bot, fake, _notes, clock = start(fcfg, tmp_path)
    fake.fail = {"DOGEUSDT"}                                                 # Ön hazırlıkta DOGE'nin geçmişi alınamaz
    run_until_prepared(bot, clock)
    assert set(bot._snap["k"]) == {"SOLUSDT", "ADAUSDT", "XRPUSDT"}         # Öteki coinlerin geçmişi hazır
    clock.t = bot.next_scan_at
    bot.tick()
    row = journal(bot)[-1]
    assert "sn" in row and row["e"] == 1 and sorted(row["a"]) == ["ADA", "SOL", "XRP"]
    assert row["s"] == {"SOL": "açıldı"}
    # Üç coinde ağ hatası (SNAP_MAX_MISSING = 2 aşıldı): anlık tarama kurulmaz, neden kayda yazılır
    (tmp_path / "b").mkdir()
    bot2, fake2, _n2, clock2 = start(fcfg, tmp_path / "b")
    fake2.fail = {"ADAUSDT", "XRPUSDT", "DOGEUSDT"}
    for _ in range(400):
        bot2.tick()
        if bot2._snap_miss:
            break
        clock2.t += bot2._next_sleep()
    assert bot2._snap is None and bot2._snap_miss == "geçmiş mumlar 1 / 4 coinde hazır (ağ hatası: 3 coin)"
