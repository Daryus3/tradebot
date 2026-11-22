"""Sinyal kalitesine göre boy (FUTURES_QUALITY_SIZE_PCT / FUTURES_QUALITY_CUT_PCT; rapor 18 araştırması, 22 Kasım):
yedi ölçülü birleşik puan sinyallerin en zayıf kesimindeyse pozisyon küçük açılır. Ortak ölçü (strategy), backtest,
canlı bot (sahte borsa, gerçek istek yok), ayarlar, Telegram ve rapor satırları."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
import futures_trader
from config import ConfigError, _setting_notes, load_config
from runtime_settings import EDITABLE
from strategy import (FUT_BAR_MS, QUALITY_CUTS, QUALITY_SIGNS, QUALITY_TABLES, FuturesScalpStrategy, quality_rank,
                      quality_score, quality_size_factor, taker_share)
from test_futures import BASE_MS, fcfg, make_fbot, params  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, bt_data, bt_sig, crashing, flat, live_cfg, press, tp_window

MIN = 60_000
STRONG = {"basis": -1.5, "taker15": 0.30, "drop5": 6.0, "drop15": 10.0, "vol_ratio": 9.0, "liq_ratio": 10.0}
WEAK = {"basis": 0.3, "taker15": 0.58, "drop5": -2.0, "drop15": -1.0, "vol_ratio": 0.3, "liq_ratio": 0.3}


# ====================================================================== ortak ölçü
def test_quality_tables_ranks_score_and_factor():
    assert set(QUALITY_TABLES) == set(QUALITY_SIGNS) and len(QUALITY_SIGNS) == 7
    for name, tab in QUALITY_TABLES.items():
        assert len(tab) == 101 and all(a <= b for a, b in zip(tab, tab[1:])), name
    tab = QUALITY_TABLES["taker15"]
    assert quality_rank("taker15", tab[0] - 1) == 0.0 and quality_rank("taker15", tab[-1] + 1) == 1.0
    assert quality_rank("taker15", tab[50]) == pytest.approx(0.5)
    assert quality_rank("taker15", (tab[50] + tab[51]) / 2) == pytest.approx(0.505)     # Tablo arası doğrusal
    assert quality_rank("taker15", None) == 0.5 and quality_rank("taker15", float("nan")) == 0.5
    assert quality_rank("vol_ratio", 0.0) == 0.0                                        # Eşit değerli dilim: başı
    # Güçlü sinyal (şiddetli, vadeli spotun altında, satıcılar baskın, hızlı düşüş, yüksek hacim) → 1'e yakın
    assert quality_score(40.0, STRONG) > 0.95 and quality_score(10.0, WEAK) < 0.1
    # Ölçü yoksa 0.5: en düşük şiddet (0) + 6 × 0.5 → 3 / 7
    assert quality_score(10.0, {}) == pytest.approx(3 / 7)
    assert quality_size_factor(60.0, 60, 0.5) == pytest.approx(0.6)                     # 0.5 ≤ 0.5311
    assert quality_size_factor(60.0, 60, QUALITY_CUTS[60]) == pytest.approx(0.6)         # Eşikte küçük
    assert quality_size_factor(60.0, 60, 0.6) == 1.0
    assert quality_size_factor(60.0, 40, 0.5) == 1.0 and quality_size_factor(60.0, 40, 0.4) == pytest.approx(0.6)
    assert quality_size_factor(100.0, 60, 0.1) == 1.0                                   # Kapalı
    assert quality_size_factor(60.0, 60, None) == 1.0 and quality_size_factor(60.0, 60, float("nan")) == 1.0


def test_taker_share_and_crash_signal_carries_new_measures(fcfg):
    f = {"v": np.array([10.0, 10, 10, 20, 0, 0]), "tb": np.array([5.0, 5, 2, 4, 0, 0])}
    got = taker_share(f)
    assert np.isnan(got[:2]).all() and got[2:] == pytest.approx([0.4, 0.275, 0.2, 0.2])
    assert np.isnan(taker_share({"v": np.ones(5)})).all()                              # Alış hacmi yoksa NaN
    cfg = replace(fcfg, futures_crash_extra="1:6")
    c = crashing(drop=8.0)
    n = len(c)
    o = np.concatenate(([c[0]], c[:-1]))
    tb = np.full(n, 5.0)
    tb[-3:] = [2.0, 3.0, 4.0]                                                           # Son 15 dk: satıcılar baskın
    k = {"t": BASE_MS - FUT_BAR_MS * n + np.arange(n) * FUT_BAR_MS, "o": o, "h": np.maximum(o, c) * 1.0005,
         "l": np.minimum(o, c) * 0.9995, "c": c, "v": np.full(n, 10.0), "tb": tb}
    sigs, _rej = FuturesScalpStrategy(None, cfg).evaluate({"SOLUSDT": k}, None, BASE_MS)
    assert len(sigs) == 1
    m = sigs[0].metrics
    assert m["drop5"] == pytest.approx((1 - c[-1] / c[-2]) * 100)
    assert m["taker15"] == pytest.approx(9 / 30)


# ====================================================================== backtest
def _sig(symbol, close_ms, breadth, metrics, score=30.0):
    s = bt_sig(symbol, close_ms, breadth, score=score)
    s.metrics = {"tactic": 1.0, **metrics}
    return s


def test_backtest_sizes_weak_signals_down_in_both_simulators():
    data = bt_data()
    a = _sig("AAAUSDT", BASE_MS, 9, WEAK, score=10.0)          # Zayıf: küçük
    b = _sig("BBBUSDT", BASE_MS + 120 * MIN, 9, STRONG)       # Güçlü: olağan boy
    w = {(s.symbol, s.close_ms): tp_window(s.close_ms) for s in (a, b)}
    p = params(start_capital=100.0, be_enabled=False, hold_min=60, margin_usage_pct=95.0)
    slots = replace(p, max_positions=4, extra_breadth=8, extra_size_pct=16.7)
    share = fb.slot_share(slots, 0)
    off = fb.simulate(slots, data, [a, b], w)
    assert off.trades[0].margin == pytest.approx(100 * 0.95 * share)
    assert off.trades[0].quality == pytest.approx(quality_score(10.0, a.metrics))       # Kapalıyken de kaydedilir
    on = fb.simulate(replace(slots, quality_size_pct=60.0, quality_cut_pct=60.0), data, [a, b], w)
    assert on.trades[0].margin == pytest.approx(100 * 0.95 * share * 0.6)
    eq = on.trades[1].equity_after - on.trades[1].pnl
    assert on.trades[1].margin == pytest.approx(eq * 0.95 * share)                      # Güçlü: olağan boy
    single = fb.simulate(replace(p, quality_size_pct=50.0), data, [a], w)
    assert single.trades[0].margin == pytest.approx(100 * 0.95 * 0.5)                   # Tek pozisyonda da küçülür
    fixed = fb.simulate(replace(slots, quality_size_pct=60.0), data, [a], w, fixed_notional=25.0)
    assert fixed.trades[0].notional == pytest.approx(25.0 * 4 * share * 0.6)
    other = _sig("CCCUSDT", BASE_MS, 9, {**WEAK, "tactic": 2.0}, score=10.0)            # Ek modül: puan uygulanmaz
    w[(other.symbol, other.close_ms)] = tp_window(other.close_ms)
    got = fb.simulate(replace(slots, quality_size_pct=60.0), data, [other], w)
    assert got.trades[0].margin == pytest.approx(100 * 0.95 * share) and got.trades[0].quality != got.trades[0].quality


def test_report_lines_and_variant_rows_for_the_quality_rule(fcfg):
    p = replace(fb.params_from_cfg(fcfg), quality_size_pct=60.0, quality_cut_pct=60.0)
    trades = [fb.FutTrade("A", "long", 0, 1, 1, 1, "TP", 2, 1, 2, 0, 0, pnl, 1, 0, 0, 0, False, False, MIN, quality=q)
              for q, pnl in ((0.30, -0.001), (0.50, 0.0), (0.60, 0.002), (0.80, 0.005), (float("nan"), 0.5))]
    line = fb.quality_breakdown_line(trades, p)
    assert line == ("Sinyal kalitesi puanına göre (sabit tutar; en zayıf %60'ta boy %60): en zayıf %40: 1 işlem, ort. "
                    "-0.050% | %40-60: 1 işlem, ort. +0.000% | %60-80: 1 işlem, ort. +0.100% | en güçlü %20: 1 işlem, "
                    "ort. +0.250%")
    assert "hepsi aynı boy" in fb.quality_breakdown_line(trades, replace(p, quality_size_pct=100.0))
    assert fb.quality_breakdown_line(trades[-1:], p) is None                            # Puan yok: satır yok
    names = [n for n, _e in fb.quality_variant_rows(p, lambda **kw: kw)]
    assert names == ["Kalite puanına göre boy kapalı", "Kalite puanı boyu %40 (şimdi %60)",
                     "Kalite puanı: en zayıf %40 (şimdi %60)"]
    assert fb.quality_variant_rows(replace(p, quality_size_pct=100.0), lambda **kw: kw) == [
        ("Kalite puanı en zayıf %60 → boy %60", {"quality_size_pct": 60.0, "quality_cut_pct": 60.0})]


# ====================================================================== canlı bot (sahte borsa, test modu)
CLOSES = {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}


def _bot(cfg, path, quality, monkeypatch):
    monkeypatch.setattr(futures_trader, "quality_score", lambda score, metrics: quality)
    bot, _fake, _notes, clock = make_fbot(cfg, path, fake=KlineCcxt(CLOSES))
    calls = []

    def spot_prices():
        calls.append(1)
        return {"SOLUSDT": float(CLOSES["SOLUSDT"][-1])}
    bot.exchange.spot_prices = spot_prices                                               # Gerçek istek yok
    return bot, clock, calls


def test_paper_bot_opens_weak_signals_small(fcfg, tmp_path, monkeypatch):
    cfg = live_cfg(fcfg, futures_quality_size_pct=60.0, futures_quality_cut_pct=60.0, futures_leverage=2)
    bot, clock, calls = _bot(cfg, tmp_path, 0.42, monkeypatch)
    bot.scan_and_enter(clock.t)
    p = bot.state.positions["SOLUSDT"]
    assert calls and p.margin_usdt == pytest.approx(30 * 0.95 * 0.6, rel=0.02)        # Fark puanın ölçüsü: spot istenir
    assert "Sinyal kalitesi zayıf (puan 0.42, en zayıf %60): pozisyon %60" in p.signal_note
    bot2, clock2, _c2 = _bot(cfg, tmp_path / "b", 0.70, monkeypatch)                    # Güçlü: tam boy
    bot2.scan_and_enter(clock2.t)
    p2 = bot2.state.positions["SOLUSDT"]
    assert p2.margin_usdt == pytest.approx(30 * 0.95, rel=0.02) and "Sinyal kalitesi" not in p2.signal_note
    bot3, clock3, calls3 = _bot(replace(cfg, futures_quality_size_pct=100.0), tmp_path / "c", 0.1, monkeypatch)
    bot3.scan_and_enter(clock3.t)                                                       # Kapalı (varsayılan): eskisi gibi
    assert not calls3 and bot3.state.positions["SOLUSDT"].margin_usdt == pytest.approx(30 * 0.95, rel=0.02)


# ====================================================================== ayarlar ve Telegram
def test_quality_settings_defaults_validation_env_runtime_and_notes(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_quality_size_pct, fcfg.futures_quality_cut_pct) == (100.0, 60.0)
    for bad in (dict(futures_quality_size_pct=10.0), dict(futures_quality_size_pct=120.0),
                dict(futures_quality_cut_pct=50.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    monkeypatch.setenv("FUTURES_QUALITY_SIZE_PCT", "60")
    monkeypatch.setenv("FUTURES_QUALITY_CUT_PCT", "40")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_quality_size_pct, loaded.futures_quality_cut_pct) == (60.0, 40.0)
    assert EDITABLE["FUTURES_QUALITY_SIZE_PCT"].attr == "futures_quality_size_pct"
    assert EDITABLE["FUTURES_QUALITY_CUT_PCT"].kind == "float"
    notes = " ".join(_setting_notes(replace(fcfg, strategy="futures", futures_quality_size_pct=60.0)))
    assert "FUTURES_QUALITY_SIZE_PCT=60" in notes and "en zayıf %60'ında" in notes
    assert not any("FUTURES_QUALITY_SIZE_PCT" in n for n in _setting_notes(replace(fcfg, strategy="futures")))
    p = fb.params_from_cfg(replace(fcfg, futures_quality_size_pct=60.0, futures_quality_cut_pct=40.0))
    assert (p.quality_size_pct, p.quality_cut_pct) == (60.0, 40.0)


def test_wave_page_quality_rows_buttons_and_text(fcfg, tmp_path):
    import settings_menu as sm
    rows = {r.key: r for r in sm.PAGES["fw"].rows if isinstance(r, sm.Row)}
    size, cut = rows["FUTURES_QUALITY_SIZE_PCT"], rows["FUTURES_QUALITY_CUT_PCT"]
    on = replace(fcfg, strategy="futures", futures_tactic="crash", futures_quality_size_pct=60.0)
    off = replace(on, futures_quality_size_pct=100.0)
    assert size.show(off) and not cut.show(off) and cut.show(on)
    assert size.fmt(100) == "Kalite puanı: kapalı" and size.fmt(60) == "Kalite puanı: %60"
    assert cut.fmt(60) == "en zayıf %60"
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash"), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert "🧮 Kalite puanına göre boy kapalı." in text and "kv:fw:FUTURES_QUALITY_SIZE_PCT:60" in data
    assert all(len(d.encode()) <= 64 for d in data)
    press(bot, "kv:fw:FUTURES_QUALITY_SIZE_PCT:60")
    assert bot.cfg.futures_quality_size_pct == 60.0 and "FUTURES_QUALITY_SIZE_PCT=60" in env.read_text()
    assert "sinyallerin en zayıf %60'ındaysa pozisyon <b>%60</b> büyüklükte" in notes.menus[-1][0]
