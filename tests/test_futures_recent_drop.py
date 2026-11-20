"""Durulmuş çöküş (FUTURES_RECENT_SIZE_PCT / FUTURES_RECENT_DROP_PCT; rapor 14 araştırması): girişten önceki 15 dk'da
fiyat eşikten az düştüyse pozisyon küçük açılır. Ortak ölçü (strategy), backtest, canlı bot (sahte borsa), ayarlar,
Telegram ve rapor satırları."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, load_config
from runtime_settings import EDITABLE
from strategy import (FUT_BAR_MS, FUT_RECENT_DROP_MINUTES, FuturesScalpStrategy, futures_features, recent_drop_pct,
                      recent_size_factor)
from test_futures import BASE_MS, fcfg, make_fbot, params  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, bt_data, bt_sig, crashing, flat, live_cfg, tp_window

MIN = 60_000


# ====================================================================== ortak ölçü
def test_recent_drop_is_close_to_close_over_three_bars_and_factor_rules():
    assert FUT_RECENT_DROP_MINUTES == 15
    c = np.array([100.0, 100.0, 100.0, 100.0, 97.0, 96.0, 95.0, 96.0])
    d = recent_drop_pct({"c": c})
    assert np.isnan(d[:3]).all()
    assert d[3] == pytest.approx(0.0) and d[6] == pytest.approx(5.0)             # 95 / 100: %5 düştü
    assert d[7] == pytest.approx((1 - 96 / 97) * 100)
    assert recent_drop_pct({"c": c}, 5)[1] == pytest.approx(0.0)                   # 5 dk = 1 mum geri
    assert recent_size_factor(100.0, 2.4, 0.5) == 1.0                             # Kapalı
    assert recent_size_factor(50.0, 2.4, 1.0) == 0.5                              # Durulmuş: küçük
    assert recent_size_factor(50.0, 2.4, -0.8) == 0.5                             # 15 dk'da yükselmiş: küçük
    assert recent_size_factor(50.0, 2.4, 2.4) == 1.0                              # Eşikte: tam boy
    assert recent_size_factor(33.0, 2.4, None) == 1.0 and recent_size_factor(33.0, 2.4, float("nan")) == 1.0


def test_crash_signal_carries_the_fifteen_minute_drop(fcfg):
    cfg = replace(fcfg, futures_crash_extra="1:6")
    c = crashing(drop=8.0)                         # Son 1 saatte doğrusal %8: son 15 dk ~%2.13
    n = len(c)
    o = np.concatenate(([c[0]], c[:-1]))
    k = {"t": BASE_MS - FUT_BAR_MS * n + np.arange(n) * FUT_BAR_MS, "o": o, "h": np.maximum(o, c) * 1.0005,
         "l": np.minimum(o, c) * 0.9995, "c": c, "v": np.full(n, 10.0), "tb": np.full(n, 5.0)}
    sigs, _rej = FuturesScalpStrategy(None, cfg).evaluate({"SOLUSDT": k}, None, BASE_MS)
    assert len(sigs) == 1
    expect = (1 - c[-1] / c[-4]) * 100
    assert sigs[0].metrics["drop15"] == pytest.approx(expect) and 2.0 < expect < 2.4
    assert "Son 15 dk -2.1%" in sigs[0].note()
    f = futures_features(k, cfg)
    assert recent_drop_pct(f)[-1] == pytest.approx(expect)


# ====================================================================== backtest
def _sig(symbol, close_ms, breadth, drop15):
    s = bt_sig(symbol, close_ms, breadth)
    s.metrics = {"tactic": 1.0, "drop15": drop15}
    return s


def test_backtest_sizes_calm_crashes_down_in_both_simulators():
    data = bt_data()
    a = _sig("AAAUSDT", BASE_MS, 9, 1.0)                       # Durulmuş (son 15 dk %1)
    b = _sig("BBBUSDT", BASE_MS + 120 * MIN, 9, 5.0)           # Hâlâ sert düşüyor
    w = {(s.symbol, s.close_ms): tp_window(s.close_ms) for s in (a, b)}
    p = params(start_capital=100.0, be_enabled=False, hold_min=60)
    off = fb.simulate(p, data, [a, b], w)
    assert [t.margin for t in off.trades][0] == pytest.approx(100.0)
    on = fb.simulate(replace(p, recent_size_pct=50.0, recent_drop_pct=2.4), data, [a, b], w)
    assert on.trades[0].margin == pytest.approx(50.0) and on.trades[0].drop15 == pytest.approx(1.0)
    assert on.trades[1].margin == pytest.approx(on.trades[1].equity_after - on.trades[1].pnl)   # Tam boy
    assert fb.simulate(replace(p, recent_size_pct=50.0), data, [a], w, fixed_notional=100.0).trades[0].notional \
        == pytest.approx(50.0)
    # Dalga modu (simulate_slots): yalnız çöküş çarpanıyla çarpılır
    slots = replace(p, max_positions=2, extra_breadth=8, recent_size_pct=50.0, isolated_size_pct=50.0,
                    isolated_breadth=5)
    a.breadth = 2
    res = fb.simulate(slots, data, [a], w)
    assert res.trades[0].margin == pytest.approx(100.0 * 0.5 * 0.5 * 0.5)        # Yuva payı × yalnız × durulmuş
    a.metrics.pop("drop15")                                                      # Ölçü yoksa küçültülmez
    assert fb.simulate(replace(p, recent_size_pct=50.0), data, [a], w).trades[0].margin == pytest.approx(100.0)


def test_report_lines_for_the_recent_drop_rule(fcfg):
    p = replace(fb.params_from_cfg(fcfg), recent_size_pct=50.0, recent_drop_pct=2.4)
    trades = [fb.FutTrade("A", "long", 0, 1, 1, 1, "TP", 2, 1, 2, 0, 0, pnl, 1, 0, 0, 0, False, False, MIN, drop15=d)
              for d, pnl in ((0.5, -0.002), (1.5, 0.001), (3.0, 0.004), (7.0, 0.02), (float("nan"), 0.5))]
    line = fb.recent_drop_line(trades, p)
    assert line == ("Girişten önceki 15 dk'daki düşüşe göre (sabit tutar; %2.4'ten az düşüşte boy %50): %1'den az: "
                    "1 işlem, ort. -0.100% | %1-2.4: 1 işlem, ort. +0.050% | %2.4-5.5: 1 işlem, ort. +0.200% | "
                    "%5.5+: 1 işlem, ort. +1.000%")
    assert "hepsi tam boy" in fb.recent_drop_line(trades, replace(p, recent_size_pct=100.0))
    assert fb.recent_drop_line(trades[-1:], p) is None                            # Ölçü yok: satır yok
    assert "çöküş durulmuşsa (girişten önceki 15 dk'da düşüş %2.4'ten az) pozisyon %50" in fb.cascade_label(p)
    names = [n for n, _e in fb.recent_variant_rows(p, lambda **kw: kw)]
    assert names == ["Çöküş durulmuşsa boy %33 (şimdi %50)", "Çöküş durulmuşsa da tam boy (kural kapalı) (şimdi %50)",
                     "Çöküş durulmuş sayılan: son 15 dk düşüş < %2 (şimdi < %2.4)",
                     "Çöküş durulmuş sayılan: son 15 dk düşüş < %3 (şimdi < %2.4)"]
    rows = fb.recent_variant_rows(replace(p, recent_size_pct=100.0), lambda **kw: kw)
    assert rows == [("Çöküş durulmuşsa (son 15 dk düşüş < %2.4) boy %50", {"recent_size_pct": 50.0,
                                                                            "recent_drop_pct": 2.4})]


# ====================================================================== canlı bot (sahte borsa, test modu)
def test_paper_bot_opens_calm_crash_at_reduced_size(fcfg, tmp_path):
    closes = {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}
    cfg = live_cfg(fcfg, futures_recent_size_pct=50.0, futures_recent_drop_pct=2.4, futures_leverage=2)
    bot, _fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(closes))
    bot.scan_and_enter(clock.t)
    p = bot.state.positions["SOLUSDT"]
    assert p.margin_usdt == pytest.approx(30 * 0.95 * 0.5, rel=0.02)
    assert "Çöküş durulmuş (son 15 dk -2.1%, gereken −%2.4): pozisyon %50" in p.signal_note
    # Eşik %2: aynı coin hâlâ yeterince düşüyor sayılır, tam boy
    cfg2 = replace(cfg, futures_recent_drop_pct=2.0)
    bot2, _f2, _n2, clock2 = make_fbot(cfg2, tmp_path / "b", fake=KlineCcxt(closes))
    bot2.scan_and_enter(clock2.t)
    p2 = bot2.state.positions["SOLUSDT"]
    assert p2.margin_usdt == pytest.approx(30 * 0.95, rel=0.02) and "Çöküş durulmuş" not in p2.signal_note
    # Kapalıyken (varsayılan) eskisi gibi
    bot3, _f3, _n3, clock3 = make_fbot(replace(cfg, futures_recent_size_pct=100.0), tmp_path / "c",
                                       fake=KlineCcxt(closes))
    bot3.scan_and_enter(clock3.t)
    assert bot3.state.positions["SOLUSDT"].margin_usdt == pytest.approx(30 * 0.95, rel=0.02)


# ====================================================================== ayarlar ve Telegram
def test_recent_settings_defaults_validation_env_and_runtime(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_recent_size_pct, fcfg.futures_recent_drop_pct) == (100.0, 2.4)
    for bad in (dict(futures_recent_size_pct=5.0), dict(futures_recent_size_pct=120.0),
                dict(futures_recent_drop_pct=0.0), dict(futures_recent_drop_pct=25.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    monkeypatch.setenv("FUTURES_RECENT_SIZE_PCT", "50")
    monkeypatch.setenv("FUTURES_RECENT_DROP_PCT", "2")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_recent_size_pct, loaded.futures_recent_drop_pct) == (50.0, 2.0)
    assert EDITABLE["FUTURES_RECENT_SIZE_PCT"].attr == "futures_recent_size_pct"
    assert EDITABLE["FUTURES_RECENT_DROP_PCT"].kind == "float"
    from config import _setting_notes
    notes = " ".join(_setting_notes(replace(fcfg, strategy="futures", futures_recent_size_pct=50.0)))
    assert "FUTURES_RECENT_SIZE_PCT=50" in notes
    assert not any("FUTURES_RECENT_SIZE_PCT" in n for n in _setting_notes(replace(fcfg, strategy="futures")))


def test_wave_page_rows_and_text(fcfg, tmp_path):
    import settings_menu as sm
    rows = {r.key: r for r in sm.PAGES["fw"].rows if isinstance(r, sm.Row)}
    size, drop = rows["FUTURES_RECENT_SIZE_PCT"], rows["FUTURES_RECENT_DROP_PCT"]
    on = replace(fcfg, strategy="futures", futures_tactic="crash", futures_recent_size_pct=50.0)
    off = replace(on, futures_recent_size_pct=100.0)
    assert size.show(off) and not drop.show(off) and drop.show(on)
    assert size.fmt(100) == "Durulmuş çöküş: tam" and size.fmt(50) == "Durulmuş çöküş: %50"
    assert drop.fmt(2.4) == "son 15 dk < %2.4"
