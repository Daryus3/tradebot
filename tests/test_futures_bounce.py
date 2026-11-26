"""V dönüşü (FUTURES_BOUNCE_CALM_PCT / FUTURES_BOUNCE_BOOST_PCT / FUTURES_BOUNCE_BOOST_SIZE_PCT; rapor 27 araştırması,
26 Kasım): fiyat son 15 dk'nın en düşüğünden %X yukarıdaysa (son dakikalarda sert satış ve toparlanma) çöküş durulmuş
sayılmaz; %Y yukarıdaysa pozisyon büyük boy. Ortak ölçü (strategy), ayarlar, backtest (sinyal ölçüsü, boy çarpanı, rapor
satırları), canlı / test modunda teminat ve pozisyon mesajı, Telegram satırları. Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, load_config
from futures_data import resample
from runtime_settings import EDITABLE
from strategy import (FUT_BAR_MS, FuturesSignal, bounce_boost_factor, bounce_from_low_pct, recent_size_factor)
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, live_cfg, press
from test_futures_day_brake import WIN, bt_data, bt_sig, crash_closes

MIN = fb.MIN_MS


# ====================================================================== ortak ölçü ve ayarlar
def test_bounce_measure_and_factors():
    c = np.array([100.0, 99.0, 96.0, 90.0, 95.0, 97.0])
    low = np.array([99.5, 98.0, 95.0, 88.0, 92.0, 96.0])
    b = bounce_from_low_pct({"c": c, "l": low})
    assert np.isnan(b[0]) and np.isnan(b[1])                                       # 3 mum (15 dk) yok
    assert b[5] == pytest.approx((97.0 / 88.0 - 1) * 100)                          # Son 3 mumun en düşüğü 88
    assert b[3] == pytest.approx((90.0 / 88.0 - 1) * 100)
    # Durulmuş (son 15 dk düşüş < %2.4) ama V dönüşü: küçültme yok; dönüş eşiğin altında ya da kural kapalı: küçük
    assert recent_size_factor(33.0, 2.4, 1.0) == pytest.approx(0.33)
    assert recent_size_factor(33.0, 2.4, 1.0, 5.0, 3.0) == 1.0
    assert recent_size_factor(33.0, 2.4, 1.0, 2.9, 3.0) == pytest.approx(0.33)
    assert recent_size_factor(33.0, 2.4, 1.0, 5.0, 0.0) == pytest.approx(0.33)
    assert recent_size_factor(33.0, 2.4, 1.0, float("nan"), 3.0) == pytest.approx(0.33)
    assert recent_size_factor(33.0, 2.4, 5.0, 0.0, 3.0) == 1.0                     # Durulmamış: zaten tam boy
    assert bounce_boost_factor(8.0, 125.0, 8.0) == pytest.approx(1.25)
    assert bounce_boost_factor(8.0, 125.0, 7.9) == 1.0 and bounce_boost_factor(0.0, 125.0, 20.0) == 1.0
    assert bounce_boost_factor(8.0, 125.0, None) == 1.0


def test_settings_defaults_validation_env(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_bounce_calm_pct, fcfg.futures_bounce_boost_pct, fcfg.futures_bounce_boost_size_pct) == \
        (0.0, 0.0, 150.0)
    for bad in (dict(futures_bounce_calm_pct=0.2), dict(futures_bounce_calm_pct=25.0), dict(futures_bounce_boost_pct=0.5),
                dict(futures_bounce_boost_pct=40.0), dict(futures_bounce_boost_size_pct=90.0),
                dict(futures_bounce_boost_size_pct=250.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    replace(fcfg, futures_bounce_calm_pct=3.0, futures_bounce_boost_pct=8.0).validate()
    for key, val in (("FUTURES_BOUNCE_CALM_PCT", "3"), ("FUTURES_BOUNCE_BOOST_PCT", "8"),
                     ("FUTURES_BOUNCE_BOOST_SIZE_PCT", "125")):
        monkeypatch.setenv(key, val)
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_bounce_calm_pct, loaded.futures_bounce_boost_pct, loaded.futures_bounce_boost_size_pct) == \
        (3.0, 8.0, 125.0)
    for key in ("FUTURES_BOUNCE_CALM_PCT", "FUTURES_BOUNCE_BOOST_PCT", "FUTURES_BOUNCE_BOOST_SIZE_PCT"):
        assert EDITABLE[key].attr == key.lower()


# ====================================================================== backtest
def test_signal_measure_on_shifted_candles_matches_one_minute_lows(fcfg):
    from test_futures_minute_scan import bt_data as m_data, k1_of, minute_closes, scan_cfg
    k1 = {"SOLUSDT": k1_of(minute_closes(dip=9.0))}
    scan = scan_cfg(fcfg, futures_coins="SOL")
    sigs, _f = fb.find_signals(scan, m_data(k1))
    k = k1["SOLUSDT"]
    close_t = k["t"] + MIN
    assert sigs and all("bounce15" in s.metrics for s in sigs)
    for s in sigs:
        i = int(np.searchsorted(close_t, s.close_ms))
        assert close_t[i] == s.close_ms
        want = (k["c"][i] / k["l"][i - 14:i + 1].min() - 1) * 100       # Son 15 adet 1 dk mumun en düşüğü
        assert s.metrics["bounce15"] == pytest.approx(want)
    assert resample is not None


def test_size_factor_engine_flags_header_and_variant_rows(fcfg):
    p = params(recent_size_pct=33.0, recent_drop_pct=2.4, bounce_calm_pct=3.0, bounce_boost_pct=8.0,
               bounce_boost_size_pct=125.0)
    calm = fb.FutSignal(BASE_MS, "AAAUSDT", "long", 1.0, 100.0, {"drop15": 1.0, "bounce15": 4.0})
    plain = fb.FutSignal(BASE_MS, "BBBUSDT", "long", 1.0, 100.0, {"drop15": 1.0, "bounce15": 1.0})
    strong = fb.FutSignal(BASE_MS, "CCCUSDT", "long", 1.0, 100.0, {"drop15": 6.0, "bounce15": 9.0})
    assert fb.size_factor(p, calm) == 1.0 and fb.bounce_flag(p, calm) == 1
    assert fb.size_factor(p, plain) == pytest.approx(0.33) and fb.bounce_flag(p, plain) == 0
    assert fb.size_factor(p, strong) == pytest.approx(1.25) and fb.bounce_flag(p, strong) == 2
    off = replace(p, bounce_calm_pct=0.0, bounce_boost_pct=0.0)
    assert fb.size_factor(off, calm) == pytest.approx(0.33) and fb.size_factor(off, strong) == 1.0
    # Yuva motoru: büyütme boştaki teminatla sınırlı; işlem bayrağı
    sigs = [bt_sig("AAAUSDT", BASE_MS)]
    sigs[0].metrics.update(drop15=6.0, bounce15=9.0)
    w = {("AAAUSDT", BASE_MS): window(WIN, start=BASE_MS)}
    tr = fb.simulate(replace(p, max_positions=2, extra_breadth=8, tp_pct=3.0, be_enabled=False), bt_data(), sigs,
                     w).trades[0]
    assert tr.margin == pytest.approx(100.0 * 0.5 * 1.25) and tr.bounced == 2
    rows = dict(fb.bounce_variant_rows(fb.params_from_cfg(fcfg), lambda **kw: kw))
    assert list(rows) == ["V dönüşü: son 15 dk'nın dibinden %3+ yukarıdaysa durulmuş sayılmaz, %8+ ise %150 boy"]
    on = fb.params_from_cfg(replace(fcfg, futures_bounce_calm_pct=3.0, futures_bounce_boost_pct=8.0))
    got = dict(fb.bounce_variant_rows(on, lambda **kw: kw))
    assert got["V dönüşü kuralları kapalı"] == {"bounce_calm_pct": 0.0, "bounce_boost_pct": 0.0}
    assert "V dönüşünde durulmuş muafiyeti %4+ (şimdi %3)" in got and "Güçlü V dönüşü %10+ (şimdi %8)" in got
    assert "Güçlü V dönüşünde boy %125 (şimdi %150)" in got
    from test_futures_minute_scan import bt_data as m_data, k1_of, minute_closes, scan_cfg
    k1 = {"SOLUSDT": k1_of(minute_closes(dip=9.0))}
    scan = replace(scan_cfg(fcfg, futures_coins="SOL"), futures_bounce_boost_pct=1.0, futures_bounce_calm_pct=1.0,
                   futures_recent_size_pct=33.0)
    data = m_data(k1)
    found, funnel = fb.find_signals(scan, data)
    windows = {}
    for s in found:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * MIN)
        windows[(s.symbol, s.close_ms)] = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    ev = fb.evaluate(fb.params_from_cfg(scan), data, found, windows)
    text = fb.format_report(scan, data, ev, funnel)
    assert "↩️ V dönüşü: fiyat son 15 dk'nın dibinden en az %1 yukarıdaysa çöküş durulmuş sayılmaz" in text
    assert "en az %1 yukarıdaysa pozisyon %150 boy" in text


# ====================================================================== canlı / test modu
def test_live_margin_boost_and_calm_exemption(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_max_positions=2, futures_recent_size_pct=33.0, futures_bounce_calm_pct=3.0,
                   futures_bounce_boost_pct=8.0, futures_bounce_boost_size_pct=125.0)
    bot, _fake, _notes, _clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(crash_closes()))

    def sig(**m):
        return FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0, "breadth": 9.0, **m})
    base = 30 * 0.95 / 2
    s1 = sig(drop15=6.0, bounce15=9.0)
    assert bot._entry_margin(s1, 30.0) == pytest.approx(base * 1.25) and s1.metrics["bounce_size_pct"] == 125.0
    assert "V dönüşü: fiyat son 15 dk'nın dibinden %9.0 yukarıda, pozisyon %125 boy" in s1.note()
    s2 = sig(drop15=1.0, bounce15=4.0)
    assert bot._entry_margin(s2, 30.0) == pytest.approx(base) and s2.metrics["bounce_calm"] == 1.0
    assert "V dönüşü: fiyat son 15 dk'nın dibinden %4.0 yukarıda, durulmuş küçültmesi yok" in s2.note()
    s3 = sig(drop15=1.0, bounce15=1.0)
    assert bot._entry_margin(s3, 30.0) == pytest.approx(base * 0.33) and "bounce_calm" not in s3.metrics
    off, _f, _n, _c = make_fbot(live_cfg(fcfg, futures_max_positions=2, futures_recent_size_pct=33.0), tmp_path / "k",
                                fake=KlineCcxt(crash_closes()))
    s4 = sig(drop15=1.0, bounce15=9.0)
    assert off._entry_margin(s4, 30.0) == pytest.approx(base * 0.33) and "V dönüşü" not in s4.note()


# ====================================================================== Telegram
def test_wave_page_rows_and_status(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash", futures_recent_size_pct=33.0), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert "↩️ V dönüşüne göre boy kapalı." in text
    assert "kv:fw:FUTURES_BOUNCE_CALM_PCT:3" in data and "kv:fw:FUTURES_BOUNCE_BOOST_PCT:8" in data
    assert not any("FUTURES_BOUNCE_BOOST_SIZE_PCT" in d for d in data)
    press(bot, "kv:fw:FUTURES_BOUNCE_CALM_PCT:3")
    press(bot, "kv:fw:FUTURES_BOUNCE_BOOST_PCT:8")
    assert (bot.cfg.futures_bounce_calm_pct, bot.cfg.futures_bounce_boost_pct) == (3.0, 8.0)
    assert "FUTURES_BOUNCE_CALM_PCT=3" in env.read_text() and "FUTURES_BOUNCE_BOOST_PCT=8" in env.read_text()
    text = notes.menus[-1][0]
    assert ("↩️ V dönüşü: fiyat son 15 dk'nın dibinden %3+ yukarıdaysa durulmuş sayılmaz; %8+ yukarıdaysa pozisyon "
            "<b>%150</b> büyüklükte.") in text
    assert "kv:fw:FUTURES_BOUNCE_BOOST_SIZE_PCT:125" in [d for row in notes.menus[-1][1] for _l, d in row]
