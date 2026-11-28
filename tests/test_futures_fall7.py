"""Düşen bıçak (FUTURES_FALL7_PCT / FUTURES_FALL7_SIZE_PCT; rapor 33 araştırması, 28 Kasım): coin son 7 günde (dünkü
günlük kapanışa göre) %X'ten fazla düştüyse yeni pozisyon küçük boy. Ortak kural (strategy), ayarlar, backtest
(attach_fall7, boy çarpanı, işlem bayrağı, rapor satırları), canlı / test modunda günlük kapanışların günde bir alınması ve
teminat, Telegram satırları. Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, load_config
from runtime_settings import EDITABLE
from strategy import FUT_BAR_MS, FuturesSignal, fall7_size_factor, ret7_pct
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, crashing, flat, live_cfg, press
from test_futures_day_brake import LOSS, WIN, bt_data, bt_sig

DAY = 86_400_000


# ====================================================================== ortak kural ve ayarlar
def test_shared_rule():
    closes = [100.0, 90.0, 95.0, 92.0, 91.0, 88.0, 85.0, 80.0]          # son: dünkü kapanış, 7 gün önce 100
    assert ret7_pct(closes) == pytest.approx(-20.0)
    assert ret7_pct([50.0] + closes) == pytest.approx(-20.0)               # Yalnız son 8 gün sayılır
    assert ret7_pct(closes[1:]) is None                                    # 8 günden az
    assert fall7_size_factor(15.0, 60.0, -20.0) == pytest.approx(0.6)
    assert fall7_size_factor(15.0, 60.0, -15.0) == 1.0                     # Tam −%15: sert düşüş sayılmaz
    assert fall7_size_factor(15.0, 60.0, 5.0) == 1.0
    assert fall7_size_factor(0.0, 60.0, -40.0) == 1.0                      # Kapalı
    assert fall7_size_factor(15.0, 60.0, None) == 1.0 and fall7_size_factor(15.0, 60.0, float("nan")) == 1.0


def test_settings_defaults_validation_env(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_fall7_pct, fcfg.futures_fall7_size_pct) == (0.0, 60.0)
    replace(fcfg, futures_fall7_pct=15.0).validate()
    for bad in (dict(futures_fall7_pct=3.0), dict(futures_fall7_pct=90.0), dict(futures_fall7_size_pct=5.0),
                dict(futures_fall7_size_pct=120.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    monkeypatch.setenv("FUTURES_FALL7_PCT", "15")
    monkeypatch.setenv("FUTURES_FALL7_SIZE_PCT", "50")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_fall7_pct, loaded.futures_fall7_size_pct) == (15.0, 50.0)
    p = fb.params_from_cfg(loaded)
    assert (p.fall7_pct, p.fall7_size_pct) == (15.0, 50.0)
    for key in ("FUTURES_FALL7_PCT", "FUTURES_FALL7_SIZE_PCT"):
        assert EDITABLE[key].attr == key.lower()


# ====================================================================== backtest
def k5_daily(close_ms, day_closes):
    """close_ms'in gününden önceki günler için 5 dk mumlar; her günün son mumunun kapanışı day_closes'tan."""
    d0 = close_ms // DAY
    t, c = [], []
    for i, dc in enumerate(day_closes):
        day = d0 - len(day_closes) + i
        for b in range(288):
            t.append(day * DAY + b * FUT_BAR_MS)
            c.append(dc if b == 287 else dc * 1.01)
    for b in range(int((close_ms % DAY) // FUT_BAR_MS)):              # bugün (sinyalden önce): sayılmamalı
        t.append(d0 * DAY + b * FUT_BAR_MS)
        c.append(10.0)
    t = np.array(t, dtype=np.int64)
    c = np.array(c, dtype=float)
    return {"t": t, "o": c, "h": c, "l": c, "c": c, "v": np.ones(len(t)), "qv": c}


def test_attach_fall7_matches_shared_rule(fcfg):
    close = BASE_MS
    k = k5_daily(close, [100.0, 98.0, 97.0, 95.0, 92.0, 90.0, 88.0, 84.0, 80.0])   # dün 80, 7 gün önce (8. gün) 98
    data = fb.FutData(close - 10 * DAY, close + DAY, [], {"SOLUSDT": k}, None, (np.zeros(0), np.zeros(0)), {}, None, "1m")
    s = fb.FutSignal(close, "SOLUSDT", "long", 1.0, 70.0)
    cfg = replace(fcfg, futures_fall7_pct=15.0)
    assert fb.attach_fall7(cfg, data, [s]) == 1
    assert s.metrics["ret7"] == pytest.approx((80.0 / 98.0 - 1) * 100)            # bugünün mumları sayılmaz
    assert fb.attach_fall7(cfg, data, [s]) == 0                                    # Zaten ölçülmüş
    p = fb.params_from_cfg(cfg)
    assert fb.size_factor(p, s) == pytest.approx(0.6)
    assert fb.size_factor(fb.params_from_cfg(fcfg), s) == 1.0                      # Kapalı
    short = fb.FutSignal(close, "SOLUSDT", "long", 1.0, 70.0)
    data2 = fb.FutData(close - 10 * DAY, close + DAY, [], {"SOLUSDT": k5_daily(close, [90.0, 80.0, 70.0])}, None,
                       (np.zeros(0), np.zeros(0)), {}, None, "1m")
    assert fb.attach_fall7(cfg, data2, [short]) == 0 and "ret7" not in short.metrics   # 8 günden az: ölçü yok


def test_engine_flags_and_header_line():
    sigs = [bt_sig("AAAUSDT", BASE_MS), bt_sig("BBBUSDT", BASE_MS + 10 * fb.MIN_MS)]
    sigs[0].metrics["ret7"] = -25.0
    sigs[1].metrics["ret7"] = -5.0
    w = {(s.symbol, s.close_ms): window(r, start=s.close_ms) for s, r in zip(sigs, [WIN, LOSS])}
    p = params(tp_pct=3.0, sl_pct=4.0, be_enabled=False, fall7_pct=15.0, fall7_size_pct=60.0)
    res = fb.simulate(p, bt_data(), sigs, w)
    a, b = res.trades
    off = fb.simulate(replace(p, fall7_pct=0.0), bt_data(), sigs, w).trades
    assert a.fall7 and a.margin == pytest.approx(off[0].margin * 0.6) and not b.fall7
    line = fb.fall7_line(p, res)
    assert line.startswith("🔪 Düşen bıçak: coin son 7 günde %15'ten fazla düştüyse") and line.endswith("A'da 1 / 2 işlem bu "
                                                                                                       "boyla açıldı")
    assert fb.fall7_line(replace(p, fall7_pct=0.0), res) == ""


def test_variant_rows(fcfg):
    rows = dict(fb.fall7_variant_rows(fb.params_from_cfg(fcfg), lambda **kw: kw, True))
    assert rows == {"Coin son 7 günde %15+ düştüyse boy %60": {"fall7_pct": 15.0, "fall7_size_pct": 60.0},
                    "Düşen bıçak %15 / %60, düşüş ≥ eşik ×2 muaf": {"fall7_pct": 15.0, "fall7_size_pct": 60.0,
                                                                    "fall7_exempt_x": 2.0},
                    "Düşen bıçak %15 / %60, ×2 muaf, BTC 7 günde %10+ düştüyse hafife girme": {
                        "fall7_pct": 15.0, "fall7_size_pct": 60.0, "fall7_exempt_x": 2.0, "fall7_btc_pct": 10.0}}
    on = fb.params_from_cfg(replace(fcfg, futures_fall7_pct=15.0, futures_fall7_size_pct=60.0))
    got = dict(fb.fall7_variant_rows(on, lambda **kw: kw, True))
    assert got["Düşen bıçak kuralı kapalı"] == {"fall7_pct": 0.0}
    assert got["Düşen bıçak: 7 günde %20+ (şimdi %15)"] == {"fall7_pct": 20.0}
    assert got["Düşen bıçakta boy %50 (şimdi %60)"] == {"fall7_size_pct": 50.0}
    assert got["Bıçakta sert çöküş muafiyeti: düşüş ≥ eşik ×2 (şimdi kapalı)"] == {"fall7_exempt_x": 2.0} and len(got) == 7
    ex = fb.params_from_cfg(replace(fcfg, futures_fall7_pct=15.0, futures_fall7_exempt_x=2.0))
    got = dict(fb.fall7_variant_rows(ex, lambda **kw: kw, True))
    assert got["Bıçakta sert çöküş muafiyeti kapalı"] == {"fall7_exempt_x": 0.0}
    assert got["Bıçakta sert çöküş muafiyeti: düşüş ≥ eşik ×3 (şimdi 2)"] == {"fall7_exempt_x": 3.0}
    assert got["Bıçakta BTC 7 günde %10+ düştüyse hafif çöküşe girme (şimdi kapalı)"] == {"fall7_btc_pct": 10.0}
    assert len(got) == 10                                                   # Piyasa düşüşü satırları muafiyetle
    bt = fb.params_from_cfg(replace(fcfg, futures_fall7_pct=15.0, futures_fall7_exempt_x=2.0, futures_fall7_btc_pct=10.0))
    got = dict(fb.fall7_variant_rows(bt, lambda **kw: kw, True))
    assert got["Bıçakta piyasa düşüşü kuralı kapalı"] == {"fall7_btc_pct": 0.0}
    assert got["Bıçakta BTC 7 günde %15+ düştüyse hafif çöküşe girme (şimdi %10)"] == {"fall7_btc_pct": 15.0}
    assert fb.fall7_variant_rows(on, lambda **kw: kw, False) == []


# ====================================================================== canlı / test modu
def daily_klines(day, closes):
    """Bugünün (day) mumu dahil günlük mumlar: kapanışlar closes + bugün (kesinleşmemiş)."""
    n = len(closes) + 1
    return {"t": [(day - n + 1 + i) * DAY for i in range(n)], "c": list(closes) + [1.0]}


def test_live_refresh_and_margin(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_leverage=2, futures_fall7_pct=15.0, futures_fall7_size_pct=60.0)
    closes = {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}
    bot, fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(closes))
    day = int(clock.t // 86400)
    calls = []

    def klines(symbol, interval, limit):
        calls.append((symbol, interval, limit))
        return daily_klines(day, [100.0, 98.0, 97.0, 95.0, 92.0, 90.0, 88.0, 80.0])  # dün 80 / 7 gün önce 100
    bot.exchange.klines = klines
    for _ in range(3):
        bot._refresh_fall7(clock.t)
    assert all(c[1] == "1d" for c in calls) and len({c[0] for c in calls}) == len(calls)   # coin başına günde bir
    sig = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0})
    assert bot.ret7_of(sig) == pytest.approx(-20.0)
    m = bot._entry_margin(sig, 30.0)
    assert m == pytest.approx(30.0 * 0.95 * 0.6) and sig.metrics["fall7_size_pct"] == 60.0
    assert "Düşen bıçak: coin son 7 günde %20.0 düştü, pozisyon %60 boy" in sig.note()
    bot._day_closes["SOLUSDT"] = (day - 1, bot._day_closes["SOLUSDT"][1])          # dünkü kayıt: bugün geçersiz
    sig2 = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0})
    assert bot.ret7_of(sig2) is None and bot._entry_margin(sig2, 30.0) == pytest.approx(30.0 * 0.95)


# ====================================================================== Telegram
def test_risk_page_rows_and_status(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash"), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    assert "🔪 Düşen bıçak: kapalı" in text
    data = [d for row in keyboard for _l, d in row]
    assert any(d.endswith("FUTURES_FALL7_PCT:15") for d in data) and not any("FUTURES_FALL7_SIZE_PCT" in d for d in data)
    press(bot, next(d for d in data if d.endswith("FUTURES_FALL7_PCT:15")))
    assert bot.cfg.futures_fall7_pct == 15.0 and "FUTURES_FALL7_PCT=15" in env.read_text()
    text = notes.menus[-1][0]
    assert "🔪 Düşen bıçak: coin son 7 günde %15'ten fazla düştüyse pozisyon %60 boy" in text
