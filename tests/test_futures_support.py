"""Destek bölgesi (FUTURES_SUPPORT_SIZE_PCT / _DAYS / _ZONE_PCT; rapor 26 araştırması, 26 Kasım): fiyat coinin önceki N
günlük dibinin (sinyalden en az 1 saat önce kapanmış 1 saatlik mumlar) en fazla %Z altındayken yeni pozisyonlar küçük boyla
açılır. Ortak kural (strategy), ayarlar, backtest (attach_support, boy çarpanı, rapor satırları), canlı / test modunda 1
saatlik mumların saatte bir alınması ve teminat, Telegram satırları. Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, load_config
from runtime_settings import EDITABLE
from strategy import FUT_BAR_MS, HOUR_MS, FuturesSignal, support_dist_pct, support_low, support_size_factor
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, live_cfg, press
from test_futures_day_brake import LOSS, WIN, bt_data, bt_sig, crash_closes
from test_settings_menu import buttons, env_text, fut, spot  # noqa: F401 - pytest fikstürü

DAY = 24 * HOUR_MS


# ====================================================================== ortak kural ve ayarlar
def hourly(close_ms, days, base=100.0, lows=None):
    """close_ms'ten önceki days gün + 3 saatin 1 saatlik mumları (açılış, en düşük); lows = {saat önce: değer}."""
    h0 = close_ms // HOUR_MS
    opens = [(h0 - k) * HOUR_MS for k in range(days * 24 + 3, -1, -1)]
    vals = [base] * len(opens)
    for back, v in (lows or {}).items():
        vals[opens.index((h0 - back) * HOUR_MS)] = v
    return opens, vals


def test_support_low_window_and_rule():
    close = BASE_MS
    h0 = close // HOUR_MS
    opens, lows = hourly(close, 30, lows={3: 90.0, 1: 50.0, 0: 40.0})
    # Son 1 saatte kapanan (ve süren) mumlar sayılmaz: açılışı ≤ kapanış − 2 saat olanlar
    eligible = [v for t, v in zip(opens, lows) if t <= close - 2 * HOUR_MS and t >= close - 30 * DAY]
    assert support_low(opens, lows, close, 30) == pytest.approx(min(eligible))
    assert (h0 - 1) * HOUR_MS > close - 2 * HOUR_MS or min(eligible) == 50.0       # Kapanış saate denk gelmiyorsa
    assert support_low(opens, lows, close, 30) in (90.0, 50.0)
    # 30 günden eski dip sayılmaz
    far = hourly(close, 40, lows={35 * 24: 10.0})
    assert support_low(*far, close, 30) == pytest.approx(100.0)
    # Pencerenin yarısından azı: bilinmiyor
    assert support_low(opens[-100:], lows[-100:], close, 30) is None
    assert support_dist_pct(98.0, 100.0) == pytest.approx(-2.0) and support_dist_pct(98.0, None) is None
    assert support_size_factor(60.0, 5.0, -2.0) == pytest.approx(0.6)               # Bölgede
    assert support_size_factor(60.0, 5.0, -5.0) == pytest.approx(0.6)               # Sınırda (dahil)
    assert support_size_factor(60.0, 5.0, -5.01) == 1.0                             # Derin kırılım: tam boy
    assert support_size_factor(60.0, 5.0, 0.0) == 1.0 and support_size_factor(60.0, 5.0, 3.0) == 1.0
    assert support_size_factor(100.0, 5.0, -2.0) == 1.0 and support_size_factor(60.0, 5.0, None) == 1.0


def test_settings_defaults_validation_env_notes(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_support_size_pct, fcfg.futures_support_days, fcfg.futures_support_zone_pct) == (100.0, 30, 5.0)
    for bad in (dict(futures_support_size_pct=5.0), dict(futures_support_size_pct=120.0), dict(futures_support_days=2),
                dict(futures_support_days=60), dict(futures_support_zone_pct=0.1), dict(futures_support_zone_pct=30.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    replace(fcfg, futures_support_size_pct=60.0, futures_support_days=14, futures_support_zone_pct=3.0).validate()
    for key, val in (("FUTURES_SUPPORT_SIZE_PCT", "60"), ("FUTURES_SUPPORT_DAYS", "14"), ("FUTURES_SUPPORT_ZONE_PCT", "3")):
        monkeypatch.setenv(key, val)
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_support_size_pct, loaded.futures_support_days, loaded.futures_support_zone_pct) == (60.0, 14, 3.0)
    for key in ("FUTURES_SUPPORT_SIZE_PCT", "FUTURES_SUPPORT_DAYS", "FUTURES_SUPPORT_ZONE_PCT"):
        assert EDITABLE[key].attr == key.lower()
    assert not _setting_notes(replace(fcfg, futures_support_size_pct=60.0))  # Açılış notu yok (rapor 34: bilgilendirme notları kaldırıldı)
    assert not any("FUTURES_SUPPORT_SIZE_PCT" in n for n in _setting_notes(fcfg))


# ====================================================================== backtest
def k5_with_low(close_ms, days=31, low_at_hours=10, low=90.0, price=88.0):
    """close_ms'e kadar 5 dk mumlar: hep 100, low_at_hours saat önce bir mumun en düşüğü `low`, son kapanış `price`."""
    n = days * 288
    t = close_ms - (np.arange(n, 0, -1)) * FUT_BAR_MS
    c = np.full(n, 100.0)
    lo = np.full(n, 99.9)
    lo[np.searchsorted(t, close_ms - low_at_hours * HOUR_MS)] = low
    c[-1] = price
    lo[-1] = price
    z = np.zeros(n)
    return {"t": t.astype(np.int64), "o": c.copy(), "h": c + 0.1, "l": lo, "c": c, "v": z + 1, "n": z, "tb": z, "qv": z}


def test_attach_support_matches_the_shared_rule(fcfg):
    close = BASE_MS
    k = k5_with_low(close)
    data = fb.FutData(close - 31 * DAY, close + HOUR_MS, [], {"SOLUSDT": k}, None, (np.zeros(0), np.zeros(0)), {}, None,
                      "1m")
    s = fb.FutSignal(close, "SOLUSDT", "long", 1.0, 88.0)          # Giriş fiyatı 88
    cfg = replace(fcfg, futures_support_size_pct=60.0)
    assert fb.attach_support(cfg, data, [s]) == 1
    hours = k["t"] // HOUR_MS
    opens = sorted(set(int(h) * HOUR_MS for h in hours))
    lows = [float(k["l"][hours == o // HOUR_MS].min()) for o in opens]
    for n in (7, 14, 30):
        want = support_dist_pct(88.0, support_low(opens, lows, close, n))
        assert s.metrics[f"low{n}h"] == pytest.approx(want)
    assert s.metrics["low30h"] == pytest.approx((88.0 / 90.0 - 1) * 100)          # 10 saat önceki dip
    assert fb.attach_support(cfg, data, [s]) == 0                                    # Zaten ölçülmüş
    p = fb.params_from_cfg(cfg)
    assert (p.support_size_pct, p.support_days, p.support_zone_pct) == (60.0, 30, 5.0)
    assert fb.size_factor(p, s) == pytest.approx(0.6)
    assert fb.size_factor(replace(p, support_zone_pct=2.0), s) == 1.0                # %2.2 altında: bölge dışı
    assert fb.size_factor(fb.params_from_cfg(fcfg), s) == 1.0                        # Kapalı


def test_engine_marks_support_trades_and_applies_in_fixed_mode():
    sigs = [bt_sig("AAAUSDT", BASE_MS), bt_sig("BBBUSDT", BASE_MS + 10 * fb.MIN_MS)]
    sigs[0].metrics["low30h"] = -2.0                     # Destek bölgesinde
    sigs[1].metrics["low30h"] = -12.0                    # Derin kırılım
    w = {(s.symbol, s.close_ms): window(r, start=s.close_ms) for s, r in zip(sigs, [WIN, LOSS])}
    p = params(tp_pct=3.0, sl_pct=4.0, be_enabled=False, support_size_pct=60.0)
    a, b = fb.simulate(p, bt_data(), sigs, w).trades
    assert a.margin == pytest.approx(60.0) and a.support and not b.support
    fixed = fb.simulate(p, bt_data(), sigs, w, fixed_notional=1000.0).trades
    assert [t.notional for t in fixed] == pytest.approx([600.0, 1000.0]) and fixed[0].support


def test_report_header_and_variant_rows(fcfg):
    base = fb.params_from_cfg(fcfg)
    assert fb.support_variant_rows(base, lambda **kw: kw, False) == []
    rows = [n for n, _kw in fb.support_variant_rows(base, lambda **kw: kw, True)]
    assert rows == ["Destek bölgesinde (30 g dibin 0-%5 altı) boy %60"]
    on = fb.params_from_cfg(replace(fcfg, futures_support_size_pct=60.0))
    got = dict(fb.support_variant_rows(on, lambda **kw: kw, True))
    assert got["Destek bölgesi kuralı kapalı"] == {"support_size_pct": 100.0}
    assert "Destek bölgesi boyu %45 (şimdi %60)" in got and "Destek bölgesi: dibin 0-%8 altı (şimdi %5)" in got
    assert "Destek bölgesi: 14 günlük dip (şimdi 30)" in got
    from test_futures_minute_scan import bt_data as m_data, k1_of, minute_closes, scan_cfg
    k1 = {"SOLUSDT": k1_of(minute_closes())}
    scan = replace(scan_cfg(fcfg, futures_coins="SOL"), futures_support_size_pct=60.0)
    data = m_data(k1)
    sigs, funnel = fb.find_signals(scan, data)
    windows = {}
    for s in sigs:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * fb.MIN_MS)
        windows[(s.symbol, s.close_ms)] = (fb.MIN_MS, win["t"], win["o"], win["h"], win["l"], win["c"])
    ev = fb.evaluate(fb.params_from_cfg(scan), data, sigs, windows)
    text = fb.format_report(scan, data, ev, funnel)
    n, sup = len(ev.chained.trades), sum(t.support for t in ev.chained.trades)
    assert n and (f"📐 Destek bölgesi: fiyat coinin önceki 30 günlük dibinin (sinyalden en az 1 saat önce kapanmış 1 "
                  f"saatlik mumlar) en fazla %5 altındayken yeni pozisyonlar %60 boy (A, B ve C'de). A'da {sup} / {n} "
                  f"işlem bu boyla açıldı") in text


# ====================================================================== canlı / test modu
class HourlyCcxt(KlineCcxt):
    """5 dk mumlara ek olarak 1 saatlik mumları (her coinde aynı en düşükler) veren sahte borsa."""

    def __init__(self, closes, lows=None, **kw):
        super().__init__(closes, **kw)
        self.lows = lows or {}
        self.hour_calls = []
        self.hour_error = None

    def fapiPublicGetKlines(self, params):
        if params["interval"] != "1h":
            return super().fapiPublicGetKlines(params)
        self.hour_calls.append(params["symbol"])
        if self.hour_error:
            raise Exception(self.hour_error)
        opens, vals = hourly(BASE_MS, 31, lows=self.lows)
        rows = [[t, "100", "100", str(v), "100", "1", t + HOUR_MS - 1, "1", "1", "1", "0", "0"]
                for t, v in zip(opens, vals)]
        return rows[-int(params["limit"]):]


def test_live_refresh_hourly_and_size_the_entry(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_leverage=2, futures_support_size_pct=60.0)
    fake = HourlyCcxt(crash_closes(), lows={10: 94.0})          # SOL 92'ye düştü: 10 saat önceki dibin %2.1 altında
    bot, _fake, notes, clock = make_fbot(cfg, tmp_path, fake=fake)
    bot._refresh_support(clock.t)
    assert sorted(fake.hour_calls) == ["ADAUSDT", "DOGEUSDT", "SOLUSDT", "XRPUSDT"]
    bot._refresh_support(clock.t + 60)                          # Aynı saat: yeniden istenmez
    assert len(fake.hour_calls) == 4
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions["SOLUSDT"]
    assert pos.margin_usdt == pytest.approx(30 * 0.95 * 0.6, rel=0.02)
    assert "Destek bölgesi: fiyat 30 günlük dibin %2.1 altında (dip kırılırken), pozisyon %60 boy" in pos.signal_note
    assert any("Destek bölgesi" in m for m in notes.messages)


def test_live_full_size_outside_zone_missing_or_stale_data(fcfg, tmp_path):
    for name, lows, fail in (("derin", {10: 110.0}, None), ("ustunde", {10: 80.0}, None), ("yok", {10: 94.0}, "ağ")):
        cfg = live_cfg(fcfg, futures_leverage=2, futures_support_size_pct=60.0)
        fake = HourlyCcxt(crash_closes(), lows=lows)
        fake.hour_error = fail
        bot, _f, _n, clock = make_fbot(cfg, tmp_path / name, fake=fake)
        bot._refresh_support(clock.t)
        bot.scan_and_enter(clock.t)
        p = bot.state.positions["SOLUSDT"]
        assert p.margin_usdt == pytest.approx(30 * 0.95, rel=0.02) and "Destek bölgesi" not in p.signal_note, name
    # Bir saatten eski kayıt kullanılmaz; kapalıyken hiç istenmez
    cfg = live_cfg(fcfg, futures_leverage=2, futures_support_size_pct=60.0)
    bot, fake, _n, clock = make_fbot(cfg, tmp_path / "eski", fake=HourlyCcxt(crash_closes(), lows={10: 94.0}))
    bot._refresh_support(clock.t)
    sig = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0})
    assert bot.support_dist(sig) == pytest.approx((92.0 / 94.0 - 1) * 100)
    bot.time_fn = lambda: clock.t + 2 * 3600 + 5
    assert bot.support_dist(sig) is None and bot._support_factor(sig) == 1.0
    off, f2, _n2, c2 = make_fbot(live_cfg(fcfg), tmp_path / "kapali", fake=HourlyCcxt(crash_closes()))
    off._refresh_support(c2.t)
    assert f2.hour_calls == []


# ====================================================================== Telegram
def test_risk_page_rows_status_and_env(fut):
    bot, notes = fut
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    data = buttons(keyboard)
    assert "📐 Destek bölgesi: kapalı" in text
    assert "kv:risk:FUTURES_SUPPORT_SIZE_PCT:60" in data and not any("FUTURES_SUPPORT_ZONE_PCT" in d for d in data)
    press(bot, "kv:risk:FUTURES_SUPPORT_SIZE_PCT:60")
    assert bot.cfg.futures_support_size_pct == 60.0 and "FUTURES_SUPPORT_SIZE_PCT=60" in env_text(bot)
    text, keyboard, _mid = notes.menus[-1]
    data = buttons(keyboard)
    assert "kv:risk:FUTURES_SUPPORT_ZONE_PCT:8" in data and "kv:risk:FUTURES_SUPPORT_DAYS:14" in data
    assert "fiyat coinin 30 günlük dibinin en fazla %5 altındayken pozisyon %60 boy" in text
    press(bot, "kv:risk:FUTURES_SUPPORT_DAYS:14")
    assert bot.cfg.futures_support_days == 14 and "FUTURES_SUPPORT_DAYS=14" in env_text(bot)


def test_risk_page_has_no_support_rows_for_spot(spot):
    bot, notes = spot
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    assert "Destek bölgesi" not in text and not any("FUTURES_SUPPORT" in d for d in buttons(keyboard))
