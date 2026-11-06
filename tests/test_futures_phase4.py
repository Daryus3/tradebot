"""Faz 4: limit (maker) giriş, fonlama sıkışması, trend içi düzeltme (1 saatlik EMA + 5 dk RSI), günlük hacim evreni;
canlı / backtest aynılığı, bekleyen limit emrin yaşam döngüsü (test modu ve canlı), kapanışta iptal."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, futures_funding_rules, futures_modules, load_config
from futures_data import resample
from strategy import (FUT_BAR_MS, FUT_HOUR_MS, FUT_PRIORITY, FuturesScalpStrategy, FuturesSignal, entry_masks,
                      funding_per_bar, funding_state, futures_features, futures_kline_limit, h1_trend,
                      pick_signal, pullback_state)
from test_futures import BASE_MS, FakeCcxt, fcfg, make_fbot, params, sig, window  # noqa: F401 - pytest fikstürü

MIN = 60_000


def series(closes, start=BASE_MS, volumes=None):
    c = np.asarray(closes, dtype=float)
    o = np.concatenate(([c[0]], c[:-1]))
    v = np.full(len(c), 10.0) if volumes is None else np.asarray(volumes, dtype=float)
    t = start + np.arange(len(c), dtype=np.int64) * FUT_BAR_MS
    return {"t": t, "o": o, "h": np.maximum(o, c) * 1.0005, "l": np.minimum(o, c) * 0.9995, "c": c, "v": v,
            "tb": v / 2, "qv": v * c, "n": v}


def zigzag(n, amp=0.001, base=100.0):
    return base * (1 + amp * np.where(np.arange(n) % 2, 1.0, -1.0))


def crash_tail(n=200, pct=5.5):
    """Son 1 saatte (12 mum) %pct düşüş: fonlama modülünün çöküş şartı (ana kural 4 saatte %10 sağlanmaz)."""
    c = zigzag(n)
    c[-12:] = np.linspace(c[-13], c[-13] * (1 - pct / 100), 13)[1:]
    return series(c)


# ====================================================================== ayarlar
def test_phase4_settings_defaults_and_validation(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_entry_order, fcfg.futures_universe_mode) == ("market", "avg")
    assert not fcfg.futures_funding and not fcfg.futures_pullback and futures_modules(fcfg) == ()
    c = replace(fcfg, futures_funding=True, futures_pullback=True, futures_pump_rules="1:6")
    assert futures_modules(c) == ("funding", "pump", "pullback")            # Öncelik sırası
    assert futures_funding_rules(replace(fcfg, futures_funding_rules="4:10,1:5")) == ((1, 5.0), (4, 10.0))
    for bad in (dict(futures_entry_order="post"), dict(futures_limit_offset_pct=2.0), dict(futures_limit_ttl_min=0),
                dict(futures_funding_rate_pct=0.1), dict(futures_funding=True, futures_funding_rules=""),
                dict(futures_funding_rules="1:x"), dict(futures_pullback_rsi=60.0), dict(futures_pullback_ema=10),
                dict(futures_universe_mode="weekly")):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    for key, value in (("FUTURES_ENTRY_ORDER", "LIMIT"), ("FUTURES_LIMIT_OFFSET_PCT", "0.1"),
                       ("FUTURES_LIMIT_TTL_MIN", "5"), ("FUTURES_FUNDING", "true"),
                       ("FUTURES_FUNDING_RATE_PCT", "-0.05"), ("FUTURES_FUNDING_RULES", "1s:%5 ; 4:10"),
                       ("FUTURES_PULLBACK", "true"), ("FUTURES_PULLBACK_RSI", "20"), ("FUTURES_PULLBACK_EMA", "100"),
                       ("FUTURES_UNIVERSE_MODE", "daily")):
        monkeypatch.setenv(key, value)
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_entry_order, loaded.futures_limit_offset_pct, loaded.futures_limit_ttl_min) == ("limit", 0.1, 5)
    assert loaded.futures_funding and loaded.futures_funding_rate_pct == -0.05
    assert loaded.futures_funding_rules == "1:5,4:10" and loaded.futures_universe_mode == "daily"
    assert loaded.futures_pullback and (loaded.futures_pullback_rsi, loaded.futures_pullback_ema) == (20.0, 100)


# ====================================================================== fonlama
def test_funding_per_bar_is_causal_and_scaled_to_8h():
    h = FUT_HOUR_MS
    ft = np.array([0, 8 * h, 12 * h, 13 * h], dtype=np.int64) + 1            # Binance zamanı: +1 ms
    fr = np.array([-0.001, -0.001, -0.001, -0.0005])
    close = np.array([-5 * MIN, 5 * MIN, 8 * h, 8 * h + 5 * MIN, 12 * h + 5 * MIN, 13 * h + 5 * MIN])
    out = funding_per_bar(close, ft, fr)
    assert np.isnan(out[0])                                                 # Henüz ödeme yok
    assert out[1] == pytest.approx(-0.1)                                    # 8 saatlik: %0.10
    assert out[2] == pytest.approx(-0.1)          # 8. saatteki ödeme o dakikada kapanan mumda henüz sayılmaz
    assert out[3] == pytest.approx(-0.1)
    assert out[4] == pytest.approx(-0.2)                                    # 4 saatlik ödeme: × 2
    assert out[5] == pytest.approx(-0.4)                                    # 1 saatlik ödeme: × 8


def test_funding_module_needs_negative_funding_and_crash(fcfg):
    c = replace(fcfg, futures_funding=True, futures_funding_rate_pct=-0.10, futures_funding_rules="1:5")
    k = crash_tail()
    n = len(k["t"])
    f = futures_features(k, c)
    assert not funding_state(f, c)[0].any()                                 # Fonlama verisi yok → sinyal yok
    for rate, expect in ((-0.12, True), (-0.05, False)):
        kk = dict(k, fr=np.full(n, rate))
        f = futures_features(kk, c)
        trig, fund = funding_state(f, c)[:2]
        assert bool(trig[-1]) is expect and fund[-1] == pytest.approx(rate)
        assert not trig[:-13].any()                                         # Çöküş yokken fonlama tek başına yetmez
    kk = dict(k, fr=np.full(n, -0.3))
    f = futures_features(kk, c)
    assert [name for name, _m in entry_masks(f, c)["long"]] == ["crash", "funding"]
    found = pick_signal(f, n - 1, c, entry_masks(f, c)["long"][1][1], np.zeros(n, dtype=bool))
    assert found["tactic"] == 5.0 and found["priority"] == FUT_PRIORITY["funding"]
    assert found["score"] == pytest.approx(30.0) and found["funding_pct"] == pytest.approx(-0.3)
    note = FuturesSignal("SOLUSDT", "long", 1.0, 0, 1.0, {k2: v for k2, v in found.items() if k2 != "side"}).note()
    assert "Fonlama sıkışması" in note and "%-0.300" in note
    assert futures_kline_limit(replace(c, futures_funding_rules="24:20")) >= 24 * 12


def test_funding_live_matches_backtest_and_fetches_only_on_crash(fcfg, tmp_path):
    c = replace(fcfg, futures_funding=True, futures_funding_rate_pct=-0.10, futures_funding_rules="1:5")
    k = crash_tail()
    t_close = k["t"] + FUT_BAR_MS
    ft, fr = [int(t_close[0]) - 4 * FUT_HOUR_MS, int(t_close[-60])], [-0.0012, -0.0015]

    class FundCcxt(FakeCcxt):
        def __init__(self):
            super().__init__(prices={"SOLUSDT": 95.0, "BTCUSDT": 100.0})
            self.fund_calls = []

        def fapiPublicGetFundingRate(self, params):
            self.fund_calls.append(params["symbol"])
            return [{"fundingTime": str(t), "fundingRate": str(r)} for t, r in zip(ft, fr)]
    bot, fake, _notes, _clock = make_fbot(c, tmp_path, fake=FundCcxt())
    flat = series(zigzag(200))
    klines = {"SOLUSDT": dict(k), "BTCUSDT": flat}
    bot._attach_extras(klines, int(t_close[-1]))
    assert fake.fund_calls == ["SOLUSDT"] and "fr" not in klines["BTCUSDT"]      # Çöküş yoksa istek yok
    sigs, _rej = FuturesScalpStrategy(None, c).evaluate({"SOLUSDT": klines["SOLUSDT"]}, None, int(t_close[-1]) + 3000)
    assert sigs and sigs[0].metrics["tactic"] == 5.0
    # Backtest aynı oranlarla aynı mumda aynı sinyali bulur
    data = fb.FutData(int(k["t"][0]), int(t_close[-1]) + FUT_BAR_MS, [fb.Segment(0, 2 ** 62, ("SOL",))],
                      {"SOLUSDT": k}, ((), (), ()), (np.zeros(0), np.zeros(0)),
                      {"SOLUSDT": (np.asarray(ft, dtype=np.int64), np.asarray(fr))}, None)
    k_bt = fb.with_extras(dict(k, o=np.r_[k["o"]]), "SOLUSDT", data, [c])
    f = futures_features(k_bt, c)
    assert funding_state(f, c)[0][-1] and f["funding"][-1] == pytest.approx(sigs[0].metrics["funding_pct"])


# ====================================================================== trend içi düzeltme
def h1_bars(closes, start):
    c = np.asarray(closes, dtype=float)
    return {"t": start + np.arange(len(c), dtype=np.int64) * FUT_HOUR_MS, "c": c, "o": c, "h": c, "l": c,
            "v": np.ones(len(c)), "qv": np.ones(len(c)), "n": np.ones(len(c)), "tb": np.ones(len(c))}


def test_h1_trend_uses_last_completed_hour(fcfg):
    k = series(zigzag(36), start=BASE_MS)                  # 3 saatlik 5 dk mum
    h1 = h1_bars([1.0, 2.0, 3.0, 4.0], BASE_MS - FUT_HOUR_MS)   # Saatler: -1, 0, 1, 2 (son saat sürüyor)
    f = futures_features(dict(k, h1=h1), fcfg)
    close_at, ema_at = h1_trend(f, 2)
    assert close_at[0] == 1.0                              # İlk 5 dk mum kapanınca biten son saat: -1
    assert close_at[10] == 1.0 and close_at[11] == 2.0     # 0. saat 12. mumun kapanışında biter
    assert close_at[22] == 2.0 and close_at[23] == 3.0     # 1. saat 24. mumun kapanışında biter
    assert close_at[-1] == 4.0                             # Son 5 dk mum kapanınca 2. saat de tamamlandı
    assert np.isnan(ema_at[0]) and ema_at[11] == pytest.approx(1.5)


def test_pullback_module_needs_uptrend_and_oversold_rsi(fcfg):
    c = replace(fcfg, futures_pullback=True, futures_pullback_rsi=25.0, futures_pullback_ema=20)
    n = 240
    closes = zigzag(n, 0.0005)
    closes[-6:] = closes[-7] * np.cumprod(np.full(6, 0.994))           # Son 30 dk sert düşüş: RSI < 25
    k = series(closes)
    start = int(k["t"][0]) - 30 * FUT_HOUR_MS
    up = h1_bars(np.linspace(80, 100, 50), start)                     # 1 saatlik yükseliş trendi (EMA altında değil)
    down = h1_bars(np.linspace(120, 100, 50), start)
    f_up = futures_features(dict(k, h1=up), c)
    trig, dist = pullback_state(f_up, c)
    assert trig[-1] and dist[-1] > 0
    assert not pullback_state(futures_features(dict(k, h1=down), c), c)[0][-1]   # Trend aşağı: sinyal yok
    assert not pullback_state(futures_features(k, c), c)[0].any()               # 1 saatlik veri yok: sinyal yok
    long_ok = entry_masks(f_up, c)["long"][1][1]
    found = pick_signal(f_up, n - 1, c, long_ok, np.zeros(n, dtype=bool))
    assert found["tactic"] == 6.0 and found["priority"] == FUT_PRIORITY["pullback"] and found["rsi"] < 25
    note = FuturesSignal("ETHUSDT", "long", 1.0, 0, 1.0, {k2: v for k2, v in found.items() if k2 != "side"}).note()
    assert "Trend içi düzeltme" in note and "EMA20" in note
    # Backtest: 1 saatlik mumlar 5 dk mumlardan (canlıda borsadan) aynı kuralla. 1446 mum: düşüş süren (bitmemiş)
    # saatte; son tamamlanan saat yükseliş trendinde
    rise = np.linspace(80, 100, 1446)
    rise[-6:] = rise[-7] * np.cumprod(np.full(6, 0.994))
    k_long = series(rise, start=start)
    data = fb.FutData(0, 0, [], {}, ((), (), ()), (np.zeros(0), np.zeros(0)), {}, None)
    kb = fb.with_extras(k_long, "ETHUSDT", data, [c])
    assert set(kb) >= {"h1"} and len(kb["h1"]["t"]) == len(resample(k_long, FUT_HOUR_MS)["t"])
    assert pullback_state(futures_features(kb, c), c)[0][-1]


def test_pullback_live_fetches_hourly_once_per_hour(fcfg, tmp_path):
    c = replace(fcfg, futures_pullback=True)

    class H1Ccxt(FakeCcxt):
        def __init__(self):
            super().__init__(prices={"SOLUSDT": 95.0, "BTCUSDT": 100.0})
            self.h1 = 0

        def fapiPublicGetKlines(self, params):
            if params["interval"] == "1h":
                self.h1 += 1
                return [[BASE_MS - (3 - i) * FUT_HOUR_MS, "1", "1", "1", "1", "1",
                         BASE_MS - (2 - i) * FUT_HOUR_MS - 1, "1", "1", "1", "1", "0"] for i in range(3)]
            return []
    bot, fake, _n, _c = make_fbot(c, tmp_path, fake=H1Ccxt())
    kl = {"SOLUSDT": series(zigzag(50)), "BTCUSDT": series(zigzag(50))}
    bot._attach_extras(kl, BASE_MS)
    assert fake.h1 == 2 and len(kl["SOLUSDT"]["h1"]["t"]) == 3
    kl2 = {"SOLUSDT": series(zigzag(50))}
    bot._attach_extras(kl2, BASE_MS + 5 * MIN)                          # Aynı saat: önbellekten
    assert fake.h1 == 2 and "h1" in kl2["SOLUSDT"]
    bot._attach_extras({"SOLUSDT": series(zigzag(50))}, BASE_MS + FUT_HOUR_MS)
    assert fake.h1 == 3


# ====================================================================== limit (maker) giriş: backtest
def test_limit_fill_needs_trade_through_within_ttl():
    p = params(entry_limit=True, limit_offset_pct=0.1, limit_ttl_min=3, limit_through_pct=0.02)
    s = sig()                                                         # Referans fiyat 100 → limit 99.9
    touch = window([(100, 100.1, 99.9, 100.0)] * 5)                    # Yalnızca dokunuyor: dolmaz
    assert fb.limit_fill(p, s, touch) is None
    late = window([(100, 100.1, 99.95, 100.0)] * 3 + [(99.9, 99.9, 99.5, 99.6)])   # 4. dakikada: süre doldu
    assert fb.limit_fill(p, s, late) is None
    j, price = fb.limit_fill(p, s, window([(100, 100.1, 99.95, 100), (100, 100, 99.8, 99.9)]))
    assert (j, price) == (1, pytest.approx(99.9))
    short = fb.limit_fill(p, sig("short"), window([(100, 100.2, 99.9, 100.1)]))
    assert short == (0, pytest.approx(100.1))


def test_run_trade_limit_entry_costs_and_fill_bar():
    rows = [(100, 100.05, 99.95, 100.0), (100, 100, 99.8, 99.85)] + [(99.9, 100.0, 99.85, 99.95)] * 60
    base = dict(hold_min=30, taker_fee=0.0005, maker_fee=0.0002, slippage_pct=0.02, be_enabled=False)
    mk = fb.run_trade(params(**base), sig(), window(rows), margin=10.0, notional=100.0)
    lm = fb.run_trade(params(**base, entry_limit=True, limit_offset_pct=0.1), sig(), window(rows), margin=10.0,
                      notional=100.0)
    assert mk.entry == pytest.approx(100.02) and mk.fees == pytest.approx(0.05 + 100 / 100.02 * mk.exit * 0.0005)
    assert lm.entry == pytest.approx(99.9) and lm.entry_ms == BASE_MS + MIN          # Dolduğu dakika
    assert lm.fees == pytest.approx(0.02 + 100 / 99.9 * lm.exit * 0.0005)            # Girişte maker, kayma yok
    assert lm.reason == "TIME" and lm.exit_ms == BASE_MS + MIN + 30 * MIN           # Süre dolumdan sayılır
    assert lm.pnl > mk.pnl
    # Dolum mumunda dip zarar kesin altına inerse o mumda zarar kes
    crash = [(100, 100, 99.95, 100), (100, 100, 94.0, 95.0)] + [(95, 96, 94, 95)] * 5
    tr = fb.run_trade(params(sl_pct=5.0, entry_limit=True, limit_offset_pct=0.1), sig(), window(crash), 10.0, 100.0)
    assert tr.reason == "SL" and tr.exit == pytest.approx(99.9 * 0.95)
    # Yükselen dolum mumunda dipten sonra tepe görülür (standart sıra): başabaş tetiği o mumda
    green = [(100, 100, 99.95, 100), (99.95, 101.5, 99.8, 101.2), (100.8, 100.85, 99.0, 99.1)]
    be = dict(tp_pct=5.0, be_trigger_pct=1.0, be_floor_pct=0.3, entry_limit=True, limit_offset_pct=0.1)
    tr = fb.run_trade(params(**be), sig(), window(green), 10.0, 100.0)
    assert tr.reason == "BE" and tr.exit == pytest.approx(99.9 * 1.003)
    strict = fb.run_trade(params(**be, limit_strict=True), sig(), window(green), 10.0, 100.0)
    assert strict.reason == "SL"


def test_simulate_counts_unfilled_limits_and_frees_slot():
    s1, s2 = sig(close_ms=BASE_MS), sig(close_ms=BASE_MS + 5 * MIN)
    flat = window([(100, 100.05, 99.97, 100.0)] * 80)
    dip = window([(100, 100.0, 99.5, 99.6)] + [(99.6, 99.7, 99.55, 99.6)] * 80, start=BASE_MS + 5 * MIN)
    data = fb.FutData(BASE_MS, BASE_MS + 10 * fb.DAY_MS, [], {}, ((), (), ()), (np.zeros(0), np.zeros(0)), {}, None)
    p = params(entry_limit=True, limit_offset_pct=0.1, hold_min=30, start_capital=100.0)
    res = fb.simulate(p, data, [s1, s2], {("BTCUSDT", s1.close_ms): flat, ("BTCUSDT", s2.close_ms): dip})
    assert [t.entry_ms for t in res.trades] == [BASE_MS + 5 * MIN]               # İlki dolmadı, ikincisi doldu
    assert res.skipped["limit emir 3 dk içinde dolmadı"] == 1
    fallback = fb.simulate(replace(p, limit_fallback=True), data, [s1, s2],
                           {("BTCUSDT", s1.close_ms): flat, ("BTCUSDT", s2.close_ms): dip})
    assert fallback.trades[0].entry_ms == BASE_MS + 3 * MIN                    # Süre dolunca piyasa emriyle


def test_params_from_cfg_reads_entry_order(fcfg):
    p = fb.params_from_cfg(replace(fcfg, futures_entry_order="limit", futures_limit_offset_pct=0.1,
                                   futures_limit_ttl_min=5))
    assert p.entry_limit and p.limit_offset_pct == 0.1 and p.limit_ttl_min == 5
    assert not fb.params_from_cfg(fcfg).entry_limit


# ====================================================================== limit (maker) giriş: canlı / test modu
def limit_cfg(fcfg, **over):
    c = replace(fcfg, futures_entry_order="limit", futures_limit_offset_pct=0.1, futures_limit_ttl_min=3, **over)
    c.validate()
    return c


def test_paper_limit_entry_waits_fills_on_price_and_uses_maker_fee(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(limit_cfg(fcfg), tmp_path)
    assert bot.open_futures(FuturesSignal("BTCUSDT", "long", 100.0, BASE_MS - FUT_BAR_MS, 5.0,
                                          {"tactic": 1.0, "drop_pct": 11.0, "hours": 4.0}), clock.t) is None
    pe = bot.state.pending_entry
    assert pe["price"] == pytest.approx(99.9) and not bot.state.positions and bot.free_slots() == 0
    bal = bot.exchange.balances["USDT"]
    clock.t += 20
    bot._check_pending_entry(clock.t)                       # Fiyat 100: dolmadı
    assert bot.state.pending_entry and not bot.state.positions
    fake.prices["BTCUSDT"] = 99.8
    clock.t += 10
    bot._check_pending_entry(clock.t)
    pos = bot.state.positions["BTCUSDT"]
    assert not bot.state.pending_entry and pos.entry_price == pytest.approx(99.9)
    assert pos.buy_fee_usdt == pytest.approx(pos.qty * 99.9 * fcfg.futures_maker_fee)
    assert bot.exchange.balances["USDT"] == pytest.approx(bal - pos.buy_fee_usdt)
    assert pos.sl_price == pytest.approx(99.9 * (1 - fcfg.stop_loss_pct / 100), abs=0.01)
    assert "Çöküş" in pos.signal_note and any("POZİSYON" in m.upper() or "LONG" in m for m in notes.messages)


def test_paper_limit_entry_expires_and_restart_keeps_waiting(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(limit_cfg(fcfg), tmp_path)
    bot.open_futures(FuturesSignal("BTCUSDT", "long", 100.0, BASE_MS - FUT_BAR_MS, 5.0, {"tactic": 1.0}), clock.t)
    bot.store.save(bot.state)
    loaded = bot.store.load()
    assert loaded.pending_entry["symbol"] == "BTCUSDT"                    # Yeniden başlayınca da sürer
    clock.t = BASE_MS / 1000 + 3 * 60 + 70
    bot._check_pending_entry(clock.t)
    assert not bot.state.pending_entry and not bot.state.positions
    assert any("dolmadı" in m for m in notes.messages)


def test_live_limit_entry_post_only_fill_partial_and_requote(fcfg, tmp_path):
    cfg = limit_cfg(fcfg)
    bot, fake, notes, clock = make_fbot(cfg, tmp_path, live=True)
    bot.open_futures(FuturesSignal("BTCUSDT", "long", 100.0, BASE_MS - FUT_BAR_MS, 5.0, {"tactic": 1.0}), clock.t)
    order = [c for c in fake.calls if c[0] == "create_order"][-1]
    assert order[2:4] == ("limit", "buy") and float(order[5]) == pytest.approx(99.9)
    assert order[6]["timeInForce"] == "GTX"
    oid = bot.state.pending_entry["order_id"]
    clock.t += 30
    bot._check_pending_entry(clock.t)
    assert bot.state.pending_entry                                       # Hâlâ açık
    fake.orders[oid].update(status="closed", filled=0.285, average=99.9)
    fake.positions["BTCUSDT"] = {"side": "long", "contracts": 0.285, "entryPrice": 99.9}
    bot._check_pending_entry(clock.t + 2)
    pos = bot.state.positions["BTCUSDT"]
    assert pos.qty == pytest.approx(0.285) and pos.stop_order_id and not bot.state.pending_entry
    # Kısmen dolup süresi biten emir iptal edilir, dolan kadarı pozisyon olur
    bot2, fake2, _n2, clock2 = make_fbot(cfg, tmp_path / "b", live=True)
    bot2.open_futures(FuturesSignal("ETHUSDT", "long", 100.0, BASE_MS - FUT_BAR_MS, 5.0, {"tactic": 1.0}), clock2.t)
    oid2 = bot2.state.pending_entry["order_id"]
    real_cancel = fake2.cancel_order

    def cancel(oid, sym, params=None):
        fake2.orders[oid] = {"status": "canceled", "filled": 0.1, "average": 99.9}
        fake2.calls.append(("cancel_order", oid, dict(params or {})))
    fake2.cancel_order = cancel
    clock2.t = BASE_MS / 1000 + 3 * 60 + 10
    bot2._check_pending_entry(clock2.t)
    assert ("cancel_order", oid2, {}) in fake2.calls and bot2.state.positions["ETHUSDT"].qty == pytest.approx(0.1)
    fake2.cancel_order = real_cancel
    # Post-only reddedilirse (fiyat limiti geçti) güncel fiyatla yeniden dizilir
    bot3, fake3, _n3, clock3 = make_fbot(cfg, tmp_path / "c", live=True)
    real_create = fake3.create_order
    rejected = []

    def create(sym, type_, side, amount, price=None, params=None):
        if type_ == "limit" and not rejected:
            rejected.append(price)
            fake3.prices["BTCUSDT"] = 99.0
            raise Exception('binance {"code":-5022,"msg":"Due to the order could not be executed as maker"}')
        return real_create(sym, type_, side, amount, price, params)
    fake3.create_order = create
    bot3.open_futures(FuturesSignal("BTCUSDT", "long", 100.0, BASE_MS - FUT_BAR_MS, 5.0, {"tactic": 1.0}), clock3.t)
    assert [float(x) for x in rejected] == [pytest.approx(99.9)]
    assert bot3.state.pending_entry["price"] == pytest.approx(98.9)


def test_shutdown_cancels_pending_live_limit(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(limit_cfg(fcfg), tmp_path, live=True)
    bot.open_futures(FuturesSignal("BTCUSDT", "long", 100.0, BASE_MS - FUT_BAR_MS, 5.0, {"tactic": 1.0}), clock.t)
    oid = bot.state.pending_entry["order_id"]
    bot._on_shutdown()
    assert any(c[0] == "cancel_order" and c[1] == oid for c in fake.calls)
    assert not bot.state.pending_entry and not bot.state.positions


def test_scan_waits_while_limit_pending(fcfg, tmp_path):
    bot, fake, _notes, clock = make_fbot(limit_cfg(fcfg), tmp_path)
    bot.open_futures(FuturesSignal("BTCUSDT", "long", 100.0, BASE_MS - FUT_BAR_MS, 5.0, {"tactic": 1.0}), clock.t)
    scans = []
    bot.scan_and_enter = lambda now: scans.append(now)
    bot.next_scan_at = clock.t
    bot.tick()
    assert scans == [] and bot._next_sleep() <= 2.0


# ====================================================================== günlük hacim evreni
class DailyArchive:
    """futures_universe.daily_volumes'un kullandığı arşiv: parite → ay → günlük mumlar."""

    def __init__(self, days):
        self.days = days                                  # parite → [(gün ms, USDT hacmi)]

    def month(self, symbol, interval, y, m):
        from datetime import datetime, timezone
        a = datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000
        b = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc).timestamp() * 1000
        rows = [(t, q) for t, q in self.days.get(symbol, []) if a <= t < b]
        return {"t": np.array([t for t, _q in rows], dtype=np.int64), "qv": np.array([q for _t, q in rows])}


def test_daily_universe_ranks_previous_day_volume_with_age(fcfg, monkeypatch):
    import futures_universe as fu
    day = fb.DAY_MS
    d0 = fb._ms("2024-03-10")
    vols = {"BTCUSDT": [(d0 - 2 * day, 10.0), (d0 - day, 10.0), (d0, 10.0)],
            "AAAUSDT": [(d0 - 2 * day, 1.0), (d0 - day, 50.0), (d0, 1.0)],          # Dün patladı
            "NEWUSDT": [(d0 - day, 99.0), (d0, 99.0)]}                                # 1 günlük: yaşa takılır
    arc = DailyArchive(vols)
    available = {"BTCUSDT": ["2020-01", "2024-03"], "AAAUSDT": ["2023-01", "2024-03"], "NEWUSDT": ["2024-03"]}
    vols["AAAUSDT"].insert(0, (fb._ms("2023-01-05"), 1.0))
    monkeypatch.setattr(fu.VolumeHistory, "symbols", lambda self, max_age_days=7.0: available)
    monkeypatch.setattr(fu.VolumeHistory, "__init__", lambda self, archive, **kw: None)
    c = replace(fcfg, futures_universe=2, futures_universe_mode="daily", futures_universe_min_age_days=30)
    out = fu.universe_days(c, d0, d0 + day, arc)
    assert out == [(d0, d0 + day, ("AAA", "BTC"))]                          # Bir önceki günün hacmine göre
    old = replace(c, futures_universe_min_age_days=1095)
    assert fu.universe_days(old, d0, d0 + day, arc)[0][2] == ("BTC",)       # 3 yıl: yalnızca arşiv başındaki


def test_active_mask_matches_segment_rule():
    segs = [fb.Segment(10, 30, ("A",)), fb.Segment(30, 50, ("B",)), fb.Segment(50, 70, ("A", "B"))]
    t = np.arange(0, 80, 5, dtype=np.int64)
    ref = np.zeros(len(t), dtype=bool)
    for s in segs:
        if "A" in s.coins:
            ref |= (t > s.start_ms) & (t <= s.end_ms)
    assert (fb._active_mask(t, "A", segs) == ref).all()


def test_live_daily_universe_refreshes_each_utc_day(fcfg, tmp_path):
    from test_futures_universe import META, NOW_MS, TICKERS, UniCcxt
    from test_futures import Clock
    c = replace(fcfg, futures_universe=2, futures_universe_mode="daily", futures_universe_min_age_days=30,
                futures_universe_with_list=False)
    clock = Clock(NOW_MS / 1000)
    bot, fake, _n, _c = make_fbot(c, tmp_path, fake=UniCcxt(TICKERS, META), clock=clock)
    first = list(bot.universe)
    assert len(first) == 2
    calls = sum(1 for x in fake.calls if x[0] == "klines")
    clock.t += 3600
    bot._refresh_universe(clock.t)
    assert sum(1 for x in fake.calls if x[0] == "klines") == calls        # Aynı gün: yenilenmez
    clock.t = (NOW_MS // fb.DAY_MS + 1) * fb.DAY_MS / 1000 + 10
    bot._refresh_universe(clock.t)
    assert sum(1 for x in fake.calls if x[0] == "klines") > calls         # Yeni UTC günü: yeniden sıralanır


# ====================================================================== Telegram ve Faz 4 ızgarası
def press(bot, data, message_id=7):
    from commands import CALLBACK, Command
    bot.commands.handle(Command(CALLBACK, data.split(":"), kind="callback", message_id=message_id))


def buttons(notes):
    return [d for row in notes.menus[-1][1] for _l, d in row]


def test_phase4_buttons_and_env(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(fcfg, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fm")
    data = buttons(notes)
    assert "kv:fm:FUTURES_FUNDING:t" in data and "kv:fm:FUTURES_PULLBACK:t" in data
    assert all(len(d.encode()) <= 64 for d in data)
    press(bot, "kv:fm:FUTURES_FUNDING:t")
    assert bot.cfg.futures_funding and "FUTURES_FUNDING=true" in env.read_text(encoding="utf-8")
    assert "kv:fm:FUTURES_FUNDING_RATE_PCT:-0.2" in buttons(notes) and "kv:fm:FUTURES_FUNDING_RULES:1h3" in buttons(notes)
    press(bot, "kv:fm:FUTURES_FUNDING_RATE_PCT:-0.2")
    assert bot.cfg.futures_funding_rate_pct == pytest.approx(-0.2)               # Eksi değer, azaltma değil
    press(bot, "kv:fm:FUTURES_FUNDING_RULES:1h3")
    assert bot.cfg.futures_funding_rules == "1:3"
    press(bot, "kv:fm:FUTURES_PULLBACK:t")
    assert bot.cfg.futures_pullback and "kv:fm:FUTURES_PULLBACK_RSI:20" in buttons(notes)
    text = notes.menus[-1][0]
    assert "Fonlama sıkışması" in text and "Trend içi düzeltme" in text and "EMA200" in text
    press(bot, "pg:fe")
    assert any("Ek modüller: fonlama, düzeltme" in label for row in notes.menus[-1][1] for label, _d in row)
    press(bot, "pg:fx")
    assert "kv:fx:FUTURES_ENTRY_ORDER:limit" in buttons(notes)
    press(bot, "kv:fx:FUTURES_ENTRY_ORDER:limit")
    assert bot.cfg.futures_entry_order == "limit" and "kv:fx:FUTURES_LIMIT_TTL_MIN:5" in buttons(notes)
    assert "post-only" in notes.menus[-1][0]
    press(bot, "kv:fc:FUTURES_UNIVERSE:30")
    assert bot.cfg.futures_universe == 30
    assert "kv:fc:FUTURES_UNIVERSE_MODE:daily" in buttons(notes) and "kv:fc:FUTURES_UNIVERSE_MIN_AGE_DAYS:30" in buttons(notes)
    press(bot, "kv:fc:FUTURES_UNIVERSE_MODE:daily")
    assert bot.cfg.futures_universe_mode == "daily" and "00:00 UTC" in notes.menus[-1][0]


def test_phase4_grid_cli_report(fcfg, tmp_path, monkeypatch):
    rng = np.random.default_rng(7)
    coins = {}
    for sym in ("SOLUSDT", "ADAUSDT"):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.008, 2500)))
        coins[sym] = series(c, volumes=rng.uniform(5, 15, 2500))
    t0, end = int(BASE_MS), int(BASE_MS) + 2500 * FUT_BAR_MS
    data = fb.FutData(t0, end, [fb.Segment(t0 + 300 * FUT_BAR_MS, end, ("SOL", "ADA"))], coins,
                      ((), (), ()), (coins["SOLUSDT"]["t"], coins["SOLUSDT"]["c"]), {}, None, "5m")
    data.funding = {s: (np.array([t0 - FUT_HOUR_MS], dtype=np.int64), np.array([-0.002])) for s in coins}
    monkeypatch.setattr(fb, "period", lambda *_a, **_k: (t0, end, data.segments))
    monkeypatch.setattr(fb, "prepare", lambda *_a, **_k: data)
    monkeypatch.setattr(fb, "GRID_SPLIT", fb._date(t0 + 1500 * FUT_BAR_MS))
    report, unis, path = fb.run_phase4_grid_cli(fcfg, None, None, tmp_path, universes=(("liste", {}),),
                                                progress=lambda _m: None)
    assert path is not None and path.read_text(encoding="utf-8").startswith("⚡ FAZ 4 IZGARASI")
    assert len(unis) == 1 and len(unis[0].rows) == len(fb.phase4_variants(fcfg))
    assert all(len(g) == 4 for g in unis[0].rows) and len(unis[0].verified) == 3
    for part in ("Yalnız fonlama", "Yalnız düzeltme", "limit %0.05", "piyasa", "1 dk", "yalnızca LONG"):
        assert part in report, part
    over = dict(unis[0].rows[0][0].variant.over)
    assert over["futures_sides"] == "long" and over["futures_funding"] is False and over["futures_pullback"] is False
    assert dict(unis[0].rows[1][0].variant.over)["futures_funding"] is True
