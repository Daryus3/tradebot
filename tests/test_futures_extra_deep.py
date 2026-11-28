"""Derin ek pozisyon (FUTURES_EXTRA_DEEP_X / FUTURES_EXTRA_DEEP_SIZE_PCT; rapor 31 araştırması, 27 Kasım): dalga modunda
başka pozisyon açıkken düşüşü çöküş eşiğinin X katı ya da fazlası olan çöküşte ek pozisyon büyük boy. Ortak kural
(config), ayarlar, backtest (yuva motoru, işlem bayrağı, rapor satırları), canlı / test modunda teminat ve pozisyon
mesajı, Telegram satırları. Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from config import ConfigError, extra_deep_factor, load_config
from runtime_settings import EDITABLE
from strategy import FUT_BAR_MS, FuturesSignal
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, crashing, flat, live_cfg, press
from test_futures_day_brake import bt_data, bt_sig

MIN = fb.MIN_MS


def crash(drop, thr=10.0, **extra):
    return {"tactic": 1.0, "drop_pct": drop, "thr_pct": thr, **extra}


# ====================================================================== ortak kural ve ayarlar
def test_shared_rule():
    assert extra_deep_factor(3.0, 150.0, 1, crash(30.0)) == pytest.approx(1.5)          # Tam 3 kat: büyük boy
    assert extra_deep_factor(3.0, 150.0, 2, crash(29.0)) == 1.0                         # 2.9 kat: normal
    assert extra_deep_factor(3.0, 150.0, 0, crash(40.0)) == 1.0                         # İlk pozisyon: kural yok
    assert extra_deep_factor(0.0, 150.0, 1, crash(40.0)) == 1.0                         # Kapalı
    assert extra_deep_factor(3.0, 200.0, 1, {"tactic": 2.0, "drop_pct": 40.0, "thr_pct": 10.0}) == 1.0   # Ek modül
    assert extra_deep_factor(3.0, 200.0, 1, {}) == 1.0                                 # Ölçü yok


def test_settings_defaults_validation_env(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_extra_deep_x, fcfg.futures_extra_deep_size_pct) == (0.0, 150.0)
    replace(fcfg, futures_extra_deep_x=3.0).validate()
    for bad in (dict(futures_extra_deep_x=1.2), dict(futures_extra_deep_x=12.0), dict(futures_extra_deep_size_pct=90.0),
                dict(futures_extra_deep_size_pct=350.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    monkeypatch.setenv("FUTURES_EXTRA_DEEP_X", "3")
    monkeypatch.setenv("FUTURES_EXTRA_DEEP_SIZE_PCT", "200")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_extra_deep_x, loaded.futures_extra_deep_size_pct) == (3.0, 200.0)
    p = fb.params_from_cfg(loaded)
    assert (p.extra_deep_x, p.extra_deep_size_pct) == (3.0, 200.0)
    for key in ("FUTURES_EXTRA_DEEP_X", "FUTURES_EXTRA_DEEP_SIZE_PCT"):
        assert EDITABLE[key].attr == key.lower()


# ====================================================================== backtest
FLAT = [(100, 100.2, 99.8, 100.0)] * 40


def test_slots_engine_boosts_deep_extra_positions():
    first = bt_sig("AAAUSDT", BASE_MS)
    first.metrics.update(crash(40.0))                                   # Derin ama ilk pozisyon: normal boy
    deep = bt_sig("BBBUSDT", BASE_MS + MIN)
    deep.metrics.update(crash(35.0))                                    # 3.5 kat, ek pozisyon: büyük boy
    plain = bt_sig("CCCUSDT", BASE_MS + 2 * MIN)
    plain.metrics.update(crash(20.0))
    sigs = [first, deep, plain]
    w = {(s.symbol, s.close_ms): window(FLAT, start=s.close_ms) for s in sigs}
    p = params(tp_pct=3.0, be_enabled=False, hold_min=30, max_positions=4, extra_size_pct=16.7, extra_breadth=8,
               extra_deep_x=3.0, extra_deep_size_pct=150.0)
    res = fb.simulate(p, bt_data(), sigs, w)
    a, b, c = res.trades
    off = fb.simulate(replace(p, extra_deep_x=0.0), bt_data(), sigs, w).trades
    assert a.margin == pytest.approx(off[0].margin) and not a.deep_extra
    assert b.margin == pytest.approx(off[1].margin * 1.5) and b.deep_extra
    assert c.margin == pytest.approx(off[2].margin) and not c.deep_extra
    line = fb.extra_deep_line(p, res)
    assert line.startswith("🔱 Derin ek pozisyon: başka pozisyon açıkken düşüşü çöküş eşiğinin 3 katı ya da fazlası")
    assert line.endswith("A'da 1 / 3 işlem") and fb.extra_deep_line(replace(p, max_positions=1), res) == ""


def test_variant_rows(fcfg):
    wave = replace(fcfg, futures_max_positions=4, futures_leverage=3)
    rows = dict(fb.extra_deep_rows(fb.params_from_cfg(wave), lambda **kw: kw))
    assert rows == {"Derin ek pozisyonda (düşüş ≥ eşik ×3) boy %150": {"extra_deep_x": 3.0, "extra_deep_size_pct": 150.0}}
    on = fb.params_from_cfg(replace(wave, futures_extra_deep_x=3.0, futures_extra_deep_size_pct=150.0))
    got = dict(fb.extra_deep_rows(on, lambda **kw: kw))
    assert got["Derin ek pozisyon büyütmesi kapalı"] == {"extra_deep_x": 0.0}
    assert got["Derin ek pozisyon: düşüş ≥ eşik ×2.5 (şimdi ×3)"] == {"extra_deep_x": 2.5}
    assert got["Derin ek pozisyonda boy %200 (şimdi %150)"] == {"extra_deep_size_pct": 200.0} and len(got) == 4
    assert fb.extra_deep_rows(fb.params_from_cfg(fcfg), lambda **kw: kw) == []
    names = [n for n, _e in fb.phase6_rows(replace(wave, futures_extra_deep_x=3.0), lambda *a, **kw: kw)]
    i = names.index("Ek pozisyonda küçük çöküşe (düşüş < eşik ×1.35) girme")
    assert names[i + 1:i + 5] == ["Derin ek pozisyon büyütmesi kapalı", "Derin ek pozisyon: düşüş ≥ eşik ×2.5 (şimdi ×3)",
                                  "Derin ek pozisyon: düşüş ≥ eşik ×4 (şimdi ×3)", "Derin ek pozisyonda boy %200 (şimdi %150)"]
    assert names[i + 5:] == ["Seri: son 10 işlem < −%1 → ek %50, ≥ +%0.8 → ek %125"]     # Seri satırı en sonda


# ====================================================================== canlı / test modu
def test_live_margin_boost_and_note(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_max_positions=2, futures_extra_size_pct=16.7, futures_extra_deep_x=3.0,
                   futures_extra_deep_size_pct=150.0)
    closes = {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}
    bot, _fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(closes))

    def sig(drop, sym="ADAUSDT"):
        return FuturesSignal(sym, "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"breadth": 12.0, **crash(drop, thr=6.0)})
    first = sig(30.0, "SOLUSDT")
    base_first = bot._entry_margin(first, 30.0)
    assert "deep_size_pct" not in first.metrics                        # İlk pozisyon: kural yok
    bot.open_futures(first, clock.t)
    plain, deep = sig(12.0), sig(18.0)                                 # 2 kat / 3 kat (eşik %6)
    m_plain = bot._entry_margin(plain, 30.0)
    m_deep = bot._entry_margin(deep, 30.0)
    assert m_deep == pytest.approx(m_plain * 1.5) and deep.metrics["deep_size_pct"] == 150.0
    assert "Derin ek pozisyon: düşüş çöküş eşiğinin 3 katı, pozisyon %150 boy" in deep.note()
    assert "deep_size_pct" not in plain.metrics and base_first > 0


# ====================================================================== Telegram
def test_wave_page_rows_and_status(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash", futures_max_positions=4,
                                                  futures_leverage=3), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    data = [d for row in notes.menus[-1][1] for _l, d in row]
    assert "kv:fw:FUTURES_EXTRA_DEEP_X:3" in data and not any("FUTURES_EXTRA_DEEP_SIZE_PCT" in d for d in data)
    press(bot, "kv:fw:FUTURES_EXTRA_DEEP_X:3")
    assert bot.cfg.futures_extra_deep_x == 3.0 and "FUTURES_EXTRA_DEEP_X=3" in env.read_text()
    text, keyboard, _mid = notes.menus[-1]
    assert ("🔱 Derin ek pozisyon: çöküş düşüşü eşiğin 3 katı ya da fazlasıysa ek pozisyon <b>%150</b> büyüklükte"
            in text)
    assert "kv:fw:FUTURES_EXTRA_DEEP_SIZE_PCT:200" in [d for row in keyboard for _l, d in row]
