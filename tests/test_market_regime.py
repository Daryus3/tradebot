"""market_regime testleri: saf sınıflandırma tablosu, piyasa genişliği, önbellek ve hata durumları."""
from dataclasses import replace

import pytest
import requests

from exchange import parse_symbol_rules
from market_regime import COKUS, NORMAL, ZAYIF, MarketRegime, classify, compute_breadth
from strategy import ReboundStrategy

UP_4H = [100.0] * 60     # EMA20 = EMA50 = 100
UP_1D = [100.0] * 30     # EMA20 = 100
HIGH_4H = [200.0] * 60   # EMA20 = EMA50 = 200


# ------------------------------------------------------------------ classify (saf)
@pytest.mark.parametrize("price, c4, c1, btc24, breadth, expected", [
    # Çöküş: BTC 24 saatte -%6 veya daha kötü
    (150, UP_4H, UP_1D, -6.0, 30.0, COKUS),
    (150, UP_4H, UP_1D, -9.5, None, COKUS),
    (150, UP_4H, UP_1D, -5.9, 30.0, NORMAL),
    # Çöküş: genişlik ≥ %85 ve BTC 4s EMA20 altında
    (99, UP_4H, [50.0] * 30, -1.0, 85.0, COKUS),
    (101, UP_4H, UP_1D, -1.0, 90.0, ZAYIF),       # EMA20 üstünde → sadece zayıf (genişlik ≥ 70)
    # Zayıf: BTC hem 4s EMA50 hem günlük EMA20 altında
    (90, UP_4H, UP_1D, 0.5, 40.0, ZAYIF),
    (150, HIGH_4H, UP_1D, 0.5, 40.0, NORMAL),     # Sadece 4s EMA50 altında → normal
    (150, UP_4H, [200.0] * 30, 0.5, 40.0, NORMAL),  # Sadece günlük EMA20 altında → normal
    # Zayıf: genişlik ≥ %70
    (150, UP_4H, UP_1D, 1.0, 70.0, ZAYIF),
    (150, UP_4H, UP_1D, 1.0, 69.9, NORMAL),
    # Eksik veri: eldeki verilerle karar
    (90, [], UP_1D, None, None, ZAYIF),           # Yalnızca günlük trend
    (90, UP_4H, [], None, None, ZAYIF),           # Yalnızca 4s trend
    (None, [], [], -7.0, None, COKUS),            # Yalnızca 24s değişim
    (None, [], [], None, 75.0, ZAYIF),            # Yalnızca genişlik
    (None, [], [], None, 90.0, ZAYIF),            # Genişlik yüksek ama EMA20 yok → çöküş teyit edilemez
    (None, [], [], None, None, NORMAL),           # Hiç veri yok
])
def test_classify_table(price, c4, c1, btc24, breadth, expected):
    res = classify(price, c4, c1, btc24, breadth)
    assert res.regime == expected
    assert res.btc_24h_pct == btc24 and res.breadth_down_pct == breadth
    assert res.note  # Her zaman açıklama olmalı


def test_classify_notes_are_turkish_and_informative():
    assert "BTC 24 saatte %-7.0" in classify(150, UP_4H, UP_1D, -7.0, None).note
    crash = classify(99, UP_4H, UP_1D, 0.0, 88.0).note
    assert "düşen coin oranı %88" in crash and "EMA20" in crash
    weak = classify(90, UP_4H, UP_1D, None, None).note
    assert "4s EMA50" in weak and "günlük EMA20" in weak
    assert "veri yetersiz" in classify(None, [], [], None, None).note
    assert classify(150, UP_4H, UP_1D, 1.25, 40.0).note == "BTC 24s %+1.2 | düşen coin %40"


def test_classify_uses_last_close_when_price_missing():
    falling_4h = [100.0] * 59 + [80.0]
    assert classify(None, falling_4h, [], None, None).regime == ZAYIF
    assert classify(None, [100.0] * 59 + [120.0], [], None, None).regime == NORMAL


def test_classify_short_series_has_no_ema():
    # 20'den az mum: EMA hesaplanamaz → trend koşulu yok sayılır
    assert classify(50, [100.0] * 10, [100.0] * 10, None, None).regime == NORMAL


# ------------------------------------------------------------------ piyasa genişliği
def make_rules(fake_client, symbols):
    return parse_symbol_rules(fake_client({s: 1.0 for s in symbols}).exchange_info())


def ticker(symbol, change, volume=10_000_000, price=1.0):
    return {"symbol": symbol, "lastPrice": str(price), "priceChangePercent": str(change),
            "quoteVolume": str(volume), "highPrice": str(price), "lowPrice": str(price)}


def coin_names(n):
    return [f"C{i:02d}USDT" for i in range(n)]


def test_breadth_counts_liquid_usdt_pairs(cfg, fake_client):
    names = coin_names(25)
    rules = make_rules(fake_client, names + ["USDCUSDT", "THINUSDT"])
    tickers = [ticker(s, -2.0 if i < 20 else 1.5) for i, s in enumerate(names)]
    tickers += [
        ticker("USDCUSDT", -0.01),                 # Hariç tutulan varlık
        ticker("THINUSDT", -5.0, volume=1000),     # Hacim düşük
        ticker("NORULEUSDT", -5.0),                # rules'ta yok
        {"symbol": "C99USDT"},                     # Eksik alan (rules'ta da yok)
    ]
    assert compute_breadth(tickers, rules, cfg) == pytest.approx(80.0)


def test_breadth_needs_twenty_pairs(cfg, fake_client):
    names = coin_names(19)
    rules = make_rules(fake_client, names)
    assert compute_breadth([ticker(s, -1) for s in names], rules, cfg) is None
    names = coin_names(20)
    rules = make_rules(fake_client, names)
    assert compute_breadth([ticker(s, -1) for s in names], rules, cfg) == 100.0


def test_breadth_zero_change_is_not_down(cfg, fake_client):
    names = coin_names(20)
    rules = make_rules(fake_client, names)
    assert compute_breadth([ticker(s, 0) for s in names], rules, cfg) == 0.0


# ------------------------------------------------------------------ MarketRegime (önbellek / hata)
class Clock:
    def __init__(self, t=1_760_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def candle(i, close):
    return [i * 1000, str(close), str(close), str(close), str(close), "1", i * 1000 + 999, "1", 1, "0", "0", "0"]


class RegimeClient:
    def __init__(self, btc_4h=None, btc_1d=None):
        self.btc_4h = btc_4h if btc_4h is not None else UP_4H
        self.btc_1d = btc_1d if btc_1d is not None else UP_1D
        self.calls = []
        self.fail = False

    def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
        self.calls.append((symbol, interval, limit))
        if self.fail:
            raise requests.ConnectionError("ağ yok")
        if interval == "5m":  # strategy.market_ok için: düz BTC
            return [candle(i, 150.0) for i in range(limit)]
        closes = self.btc_4h if interval == "4h" else self.btc_1d
        return [candle(i, c) for i, c in enumerate(closes[-limit:])]

    def regime_calls(self):
        return [c for c in self.calls if c[1] in ("4h", "1d")]


def market(btc_price=150.0, btc_change=1.0, down=8, total=25):
    out = [ticker("BTCUSDT", btc_change, price=btc_price)]
    out += [ticker(s, -2.0 if i < down else 2.0) for i, s in enumerate(coin_names(total))]
    return out


@pytest.fixture
def rules(fake_client):
    return make_rules(fake_client, coin_names(25) + ["BTCUSDT"])


def test_evaluate_uses_tickers_and_klines(cfg, rules):
    client = RegimeClient()
    res = MarketRegime(client, cfg, time_fn=Clock()).evaluate(market(btc_change=-1.0, down=10), rules)
    assert res.regime == NORMAL
    assert res.btc_24h_pct == -1.0
    assert res.breadth_down_pct == pytest.approx(11 / 26 * 100)  # BTCUSDT (-%1) da likit bir USDT paritesi
    assert sorted(client.calls) == [("BTCUSDT", "1d", 30), ("BTCUSDT", "4h", 60)]


def test_evaluate_detects_weak_and_crash(cfg, rules):
    regime = MarketRegime(RegimeClient(), cfg, time_fn=Clock())
    assert regime.evaluate(market(btc_price=90.0), rules).regime == ZAYIF
    assert regime.evaluate(market(btc_change=-8.0), rules).regime == COKUS
    assert regime.evaluate(market(btc_price=99.0, down=23), rules).regime == COKUS  # %88 düşüşte + EMA20 altı
    assert regime.last.regime == COKUS


def test_btc_klines_are_cached(cfg, rules):
    clock = Clock()
    client = RegimeClient()
    regime = MarketRegime(client, cfg, time_fn=clock, cache_sec=900)
    regime.evaluate(market(), rules)
    regime.evaluate(market(), rules)
    assert len(client.regime_calls()) == 2  # İkinci çağrıda istek yok (4h + 1d bir kez)
    clock.t += 899
    regime.evaluate(market(), rules)
    assert len(client.regime_calls()) == 2
    clock.t += 1
    regime.evaluate(market(), rules)
    assert len(client.regime_calls()) == 4


def test_regime_filter_disabled_is_always_normal(cfg, rules):
    client = RegimeClient()
    res = MarketRegime(client, replace(cfg, regime_filter=False)).evaluate(market(btc_change=-20.0, down=25), rules)
    assert res.regime == NORMAL and client.calls == []


def test_kline_error_without_data_is_normal(cfg, rules):
    clock = Clock()
    client = RegimeClient()
    client.fail = True
    regime = MarketRegime(client, cfg, time_fn=clock)
    res = regime.evaluate([], rules)  # Ticker da yok
    assert res.regime == NORMAL and "veri alınamadı" in res.note
    regime.evaluate([], rules)
    assert len(client.calls) == 1  # Hata sonrası hemen tekrar denenmez
    clock.t += 61
    client.fail = False
    res = regime.evaluate(market(), rules)
    assert len(client.regime_calls()) == 3 and "alınamadı" not in res.note


def test_kline_error_still_detects_crash_from_tickers(cfg, rules):
    client = RegimeClient()
    client.fail = True
    res = MarketRegime(client, cfg, time_fn=Clock()).evaluate(market(btc_change=-7.5), rules)
    assert res.regime == COKUS and "veri alınamadı" in res.note


def test_kline_error_uses_stale_cache(cfg, rules):
    clock = Clock()
    client = RegimeClient()
    regime = MarketRegime(client, cfg, time_fn=clock, cache_sec=900)
    regime.evaluate(market(), rules)
    clock.t += 1000
    client.fail = True
    res = regime.evaluate(market(btc_price=90.0), rules)
    assert res.regime == ZAYIF and "eski veri" in res.note


def test_unexpected_error_falls_back_to_normal(cfg):
    res = MarketRegime(RegimeClient(), cfg, time_fn=Clock()).evaluate(market(btc_change=-9.0), None)
    assert res.regime == NORMAL and res.note == "veri alınamadı"


# ------------------------------------------------------------------ strateji entegrasyonu
class ScanMarketClient(RegimeClient):
    def __init__(self, tickers, **kw):
        super().__init__(**kw)
        self.tickers = tickers

    def tickers_24h(self):
        return self.tickers


def test_strategy_scan_blocks_on_crash(cfg, rules):
    client = ScanMarketClient(market(btc_change=-8.0))
    result = ReboundStrategy(client, cfg, regime=MarketRegime(client, cfg, time_fn=Clock())).scan(rules, set(), 11)
    assert not result.market_ok and "çöküş" in result.market_note and result.regime == COKUS


def test_strategy_scan_reports_weak_regime(cfg, rules):
    client = ScanMarketClient(market(btc_price=90.0))
    result = ReboundStrategy(client, cfg, regime=MarketRegime(client, cfg, time_fn=Clock())).scan(rules, set(), 11)
    assert result.market_ok and result.regime == ZAYIF and "EMA" in result.regime_note
