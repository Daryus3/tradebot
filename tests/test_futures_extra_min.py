"""Ek pozisyonda küçük çöküş (FUTURES_EXTRA_MIN_X; rapor 30 araştırması, 27 Kasım): dalga modunda başka pozisyon
açıkken, düşüşü çöküş eşiğinin X katından az olan çöküş sinyaline girilmez (yuva daha derin çöküşe kalır). Ortak kural
(config), ayarlar, backtest (yuva motoru, atlama sayımı, rapor satırları), canlı / test modunda tarama, Telegram
satırı. Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from config import ConfigError, crash_ratio, extra_depth_skip, load_config
from runtime_settings import EDITABLE
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, crashing, flat, live_cfg, press
from test_futures_day_brake import bt_data, bt_sig

MIN = fb.MIN_MS


def crash(drop, thr=10.0, **extra):
    return {"tactic": 1.0, "drop_pct": drop, "thr_pct": thr, **extra}


# ====================================================================== ortak kural ve ayarlar
def test_shared_rule():
    assert crash_ratio(crash(12.0)) == pytest.approx(1.2)
    for m in ({}, None, {"tactic": 2.0, "drop_pct": 5.0, "thr_pct": 4.0}, crash(12.0, thr=0.0), crash(float("nan"))):
        r = crash_ratio(m)
        assert r != r                                                   # Çöküş dışı / ölçü yok: NaN
    assert extra_depth_skip(1.25, 1, crash(12.0))                       # Ek pozisyon, 1.2 < 1.25: girilmez
    assert not extra_depth_skip(1.25, 1, crash(12.5))                   # Tam 1.25 kat: girilir
    assert not extra_depth_skip(1.25, 0, crash(10.5))                   # İlk pozisyon: kural yok
    assert not extra_depth_skip(0.0, 3, crash(10.5))                    # Kapalı
    assert not extra_depth_skip(1.25, 2, {"tactic": 3.0})               # Ek modül sinyali (Bollinger ...)
    assert not extra_depth_skip(1.25, 2, {})                            # Ölçü yok: eskisi gibi girilir


def test_settings_defaults_validation_env(fcfg, monkeypatch, tmp_path):
    assert fcfg.futures_extra_min_x == 0.0
    replace(fcfg, futures_extra_min_x=1.1).validate()
    for bad in (1.0, 0.5, 6.0):
        with pytest.raises(ConfigError):
            replace(fcfg, futures_extra_min_x=bad).validate()
    monkeypatch.setenv("FUTURES_EXTRA_MIN_X", "1.25")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert loaded.futures_extra_min_x == 1.25
    assert EDITABLE["FUTURES_EXTRA_MIN_X"].attr == "futures_extra_min_x"
    assert fb.params_from_cfg(loaded).extra_min_x == 1.25


# ====================================================================== backtest
FLAT = [(100, 100.2, 99.8, 100.0)] * 40                                  # Ne hedef ne stop: zaman stopuna kadar açık


def test_slots_engine_skips_shallow_extra_positions():
    first = bt_sig("AAAUSDT", BASE_MS)
    first.metrics.update(crash(11.0))                                   # 1.1: ilk pozisyon olduğu için girilir
    shallow = bt_sig("BBBUSDT", BASE_MS + MIN)
    shallow.metrics.update(crash(11.5))                                 # 1.15 < 1.25: ek pozisyon olarak girilmez
    deep = bt_sig("CCCUSDT", BASE_MS + 2 * MIN)
    deep.metrics.update(crash(20.0))
    sigs = [first, shallow, deep]
    w = {(s.symbol, s.close_ms): window(FLAT, start=s.close_ms) for s in sigs}
    p = params(tp_pct=3.0, be_enabled=False, hold_min=30, max_positions=3, extra_min_x=1.25)
    res = fb.simulate(p, bt_data(), sigs, w)
    assert [t.symbol for t in res.trades] == ["AAAUSDT", "CCCUSDT"]
    assert res.skipped[fb.extra_skip_reason(1.25)] == 1
    assert res.skipped["ek pozisyon: küçük çöküş (düşüş < eşik ×1.25)"] == 1
    off = fb.simulate(replace(p, extra_min_x=0.0), bt_data(), sigs, w)
    assert [t.symbol for t in off.trades] == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    lower = fb.simulate(replace(p, extra_min_x=1.1), bt_data(), sigs, w)
    assert len(lower.trades) == 3
    single = fb.simulate(replace(p, max_positions=1), bt_data(), sigs, w)  # Tek pozisyonda ek pozisyon yok
    assert [t.symbol for t in single.trades] == ["AAAUSDT"] and not single.skipped.get(fb.extra_skip_reason(1.25))


def test_variant_rows_and_report_header(fcfg):
    wave = replace(fcfg, futures_max_positions=4, futures_leverage=3)
    rows = dict(fb.extra_min_rows(fb.params_from_cfg(wave), lambda **kw: kw))
    assert rows == {"Ek pozisyonda küçük çöküşe (düşüş < eşik ×1.35) girme": {"extra_min_x": 1.35}}
    on = fb.params_from_cfg(replace(wave, futures_extra_min_x=1.35))
    got = dict(fb.extra_min_rows(on, lambda **kw: kw))
    assert got["Ek pozisyonda küçük çöküş kuralı kapalı"] == {"extra_min_x": 0.0}
    assert got["Ek pozisyonda çöküş ≥ eşik ×1.2 (şimdi ×1.35)"] == {"extra_min_x": 1.2}
    assert "Ek pozisyonda çöküş ≥ eşik ×1.5 (şimdi ×1.35)" in got and len(got) == 3
    assert fb.extra_min_rows(fb.params_from_cfg(fcfg), lambda **kw: kw) == []     # Tek pozisyon: satır yok
    first = bt_sig("AAAUSDT", BASE_MS)
    first.metrics.update(crash(20.0))
    shallow = bt_sig("BBBUSDT", BASE_MS + MIN)
    shallow.metrics.update(crash(11.0))
    w = {(s.symbol, s.close_ms): window(FLAT, start=s.close_ms) for s in (first, shallow)}
    p = params(tp_pct=3.0, be_enabled=False, hold_min=30, max_positions=2, extra_min_x=1.25)
    res = fb.simulate(p, bt_data(), [first, shallow], w)
    line = fb.extra_min_line(p, res)
    assert line.startswith("🧩 Ek pozisyonda küçük çöküş: başka pozisyon açıkken düşüşü çöküş eşiğinin 1.25 katından")
    assert line.endswith("A'da 1 sinyal bu yüzden atlandı")
    assert fb.extra_min_line(replace(p, extra_min_x=0.0), res) == ""
    assert fb.extra_min_line(replace(p, max_positions=1), res) == ""


# ====================================================================== canlı / test modu
def scan_bot(fcfg, tmp_path, x, name):
    closes = {"SOLUSDT": crashing(), "ADAUSDT": crashing(drop=7.0), "XRPUSDT": crashing(drop=6.5),
              "DOGEUSDT": flat(), "BTCUSDT": flat()}
    cfg = live_cfg(fcfg, futures_max_positions=2, futures_extra_breadth=3, futures_extra_min_x=x)
    bot, _fake, _notes, clock = make_fbot(cfg, tmp_path / name, fake=KlineCcxt(closes))
    bot.scan_and_enter(clock.t)
    return bot


def test_paper_scan_skips_shallow_extra_position(fcfg, tmp_path):
    # Düşüş / eşik (1 sa %6): SOL 8 / 6 = 1.33 (en güçlü, ilk pozisyon), ADA 7 / 6 = 1.17, XRP 6.5 / 6 = 1.08
    assert set(scan_bot(fcfg, tmp_path, 0.0, "off").state.positions) == {"SOLUSDT", "ADAUSDT"}
    assert set(scan_bot(fcfg, tmp_path, 1.25, "x125").state.positions) == {"SOLUSDT"}
    assert set(scan_bot(fcfg, tmp_path, 1.1, "x11").state.positions) == {"SOLUSDT", "ADAUSDT"}
    assert set(scan_bot(fcfg, tmp_path, 1.5, "x15").state.positions) == {"SOLUSDT"}   # İlk pozisyona kural yok


# ====================================================================== Telegram
def test_wave_page_row_and_status(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash", futures_max_positions=4,
                                                  futures_leverage=3), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    assert "🧩 Ek pozisyonda çöküş derinliği şartı yok." in text
    assert "kv:fw:FUTURES_EXTRA_MIN_X:1.35" in [d for row in keyboard for _l, d in row]
    press(bot, "kv:fw:FUTURES_EXTRA_MIN_X:1.35")
    assert bot.cfg.futures_extra_min_x == 1.35 and "FUTURES_EXTRA_MIN_X=1.35" in env.read_text()
    assert "🧩 Ek pozisyon yalnızca çöküş düşüşü eşiğin en az <b>1.35</b> katıysa" in notes.menus[-1][0]
    single, _f, notes1, _c = make_fbot(replace(fcfg, futures_tactic="crash"), tmp_path / "s")
    press(single, "pg:fw")
    data = [d for row in notes1.menus[-1][1] for _l, d in row]
    assert not any("FUTURES_EXTRA_MIN_X" in d for d in data)                 # Tek pozisyonda satır yok
