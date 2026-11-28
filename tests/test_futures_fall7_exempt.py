"""Düşen bıçakta sert çöküş muafiyeti (FUTURES_FALL7_EXEMPT_X; rapor 34 araştırması, 28 Kasım): coin son 7 günde sert
düşmüş olsa da çöküşün düşüşü eşiğin X katı ve üstündeyse düşen bıçak küçültmesi uygulanmaz. Ortak kural (strategy),
ayarlar, backtest (boy çarpanı, işlem bayrağı, rapor satırı), canlı / test modunda teminat ve pozisyon notu, Telegram satırı.
Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, load_config
from runtime_settings import EDITABLE
from strategy import FUT_BAR_MS, FuturesSignal, fall7_size_factor
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, crashing, flat, live_cfg, press
from test_futures_day_brake import LOSS, WIN, bt_data, bt_sig
from test_futures_fall7 import daily_klines

NAN = float("nan")


# ====================================================================== ortak kural ve ayarlar
def test_shared_rule():
    assert fall7_size_factor(15.0, 60.0, -20.0, 2.5, 2.0) == 1.0                # Sert çöküş: tam boy
    assert fall7_size_factor(15.0, 60.0, -20.0, 2.0, 2.0) == 1.0                # Tam eşik muaf
    assert fall7_size_factor(15.0, 60.0, -20.0, 1.99, 2.0) == pytest.approx(0.6)
    assert fall7_size_factor(15.0, 60.0, -20.0, 5.0, 0.0) == pytest.approx(0.6)  # Muafiyet kapalı
    assert fall7_size_factor(15.0, 60.0, -20.0, NAN, 2.0) == pytest.approx(0.6)  # Oran bilinmiyor: küçük boy
    assert fall7_size_factor(15.0, 60.0, -20.0, None, 2.0) == pytest.approx(0.6)
    assert fall7_size_factor(15.0, 60.0, -10.0, 1.0, 2.0) == 1.0                # Bıçak değil
    assert fall7_size_factor(0.0, 60.0, -40.0, 1.0, 2.0) == 1.0                 # Düşen bıçak kapalı


def test_settings_defaults_validation_env_note(fcfg, monkeypatch, tmp_path):
    assert fcfg.futures_fall7_exempt_x == 0.0
    on = replace(fcfg, futures_fall7_pct=15.0, futures_fall7_exempt_x=2.0)
    on.validate()
    for bad in (1.0, 11.0, -1.0):
        with pytest.raises(ConfigError):
            replace(on, futures_fall7_exempt_x=bad).validate()
    monkeypatch.setenv("FUTURES_FALL7_PCT", "15")
    monkeypatch.setenv("FUTURES_FALL7_EXEMPT_X", "2")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert loaded.futures_fall7_exempt_x == 2.0 and fb.params_from_cfg(loaded).fall7_exempt_x == 2.0
    assert EDITABLE["FUTURES_FALL7_EXEMPT_X"].attr == "futures_fall7_exempt_x"
    assert not _setting_notes(on)                                               # Açılış notu yok
    note = [n for n in _setting_notes(replace(on, futures_fall7_pct=0.0)) if "FUTURES_FALL7_EXEMPT_X" in n]
    assert len(note) == 1 and "etkisiz" in note[0]                              # Bıçak kapalıyken muafiyet etkisiz


# ====================================================================== backtest
def crash_sig(symbol, close_ms, drop, thr=10.0):
    s = bt_sig(symbol, close_ms)
    s.metrics.update({"ret7": -25.0, "drop_pct": drop, "thr_pct": thr})
    return s


def test_engine_size_flags_and_header_line():
    sigs = [crash_sig("AAAUSDT", BASE_MS, 25.0), crash_sig("BBBUSDT", BASE_MS + 10 * fb.MIN_MS, 15.0)]   # oran 2.5 / 1.5
    w = {(s.symbol, s.close_ms): window(r, start=s.close_ms) for s, r in zip(sigs, [WIN, LOSS])}
    p = params(tp_pct=3.0, sl_pct=4.0, be_enabled=False, fall7_pct=15.0, fall7_size_pct=60.0, fall7_exempt_x=2.0)
    full = fb.simulate(replace(p, fall7_pct=0.0), bt_data(), sigs, w).trades
    a, b = fb.simulate(p, bt_data(), sigs, w).trades
    assert not a.fall7 and a.margin == pytest.approx(full[0].margin)            # Sert çöküş: tam boy
    shrunk = fb.simulate(replace(p, fall7_exempt_x=0.0), bt_data(), sigs, w).trades
    assert shrunk[0].fall7 and shrunk[0].margin == pytest.approx(full[0].margin * 0.6)
    assert b.fall7                                                              # Hafif çöküş: küçük boy
    assert fb.fall7_factor(p, sigs[0]) == 1.0 and fb.fall7_factor(p, sigs[1]) == pytest.approx(0.6)
    line = fb.fall7_line(p, fb.simulate(p, bt_data(), sigs, w))
    assert "düşüşü eşiğin 2 katı ve üstündeki sert çöküşler tam boy" in line and line.endswith("A'da 1 / 2 işlem bu "
                                                                                               "boyla açıldı")
    other = crash_sig("CCCUSDT", BASE_MS, 25.0)
    other.metrics["tactic"] = 2.0                                               # Çöküş dışı sinyal: oran yok
    assert fb.fall7_factor(p, other) == pytest.approx(0.6)


# ====================================================================== canlı / test modu
def test_live_margin_and_note(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_leverage=2, futures_fall7_pct=15.0, futures_fall7_size_pct=60.0,
                   futures_fall7_exempt_x=2.0)
    closes = {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}
    bot, _fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(closes))
    day = int(clock.t // 86400)
    bot.exchange.klines = lambda symbol, interval, limit: daily_klines(day, [100.0, 98.0, 97.0, 95.0, 92.0, 90.0, 88.0,
                                                                             80.0])
    bot._refresh_fall7(clock.t)
    hard = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0,
                         {"tactic": 1.0, "drop_pct": 25.0, "thr_pct": 10.0})
    assert bot._entry_margin(hard, 30.0) == pytest.approx(30.0 * 0.95)          # Sert çöküş: tam boy
    assert hard.metrics["fall7_exempt"] == 2.5 and "fall7_size_pct" not in hard.metrics
    assert "Düşen bıçak: coin son 7 günde %20.0 düştü ama çöküş sert (düşüş eşiğin 2.5 katı), tam boy" in hard.note()
    mild = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0,
                         {"tactic": 1.0, "drop_pct": 15.0, "thr_pct": 10.0})
    assert bot._entry_margin(mild, 30.0) == pytest.approx(30.0 * 0.95 * 0.6) and "fall7_exempt" not in mild.metrics
    flat_sig = FuturesSignal("ADAUSDT", "long", 1.0, BASE_MS - FUT_BAR_MS, 9.0,
                             {"tactic": 1.0, "drop_pct": 25.0, "thr_pct": 10.0})
    bot._day_closes["ADAUSDT"] = (day, [100.0] * 8)                             # Bıçak değil: not yok
    assert bot._entry_margin(flat_sig, 30.0) == pytest.approx(30.0 * 0.95) and "fall7_exempt" not in flat_sig.metrics


# ====================================================================== Telegram
def test_risk_page_row(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash"), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "risk")
    data = [d for row in notes.menus[-1][1] for _l, d in row]
    assert not any("FUTURES_FALL7_EXEMPT_X" in d for d in data)                 # Bıçak kapalıyken satır yok
    press(bot, next(d for d in data if d.endswith("FUTURES_FALL7_PCT:15")))
    data = [d for row in notes.menus[-1][1] for _l, d in row]
    press(bot, next(d for d in data if d.endswith("FUTURES_FALL7_EXEMPT_X:2")))
    assert bot.cfg.futures_fall7_exempt_x == 2.0 and "FUTURES_FALL7_EXEMPT_X=2" in env.read_text()
    text = notes.menus[-1][0]
    assert "(düşüşü eşiğin 2 katı ve üstü sert çöküşte tam boy)" in text and len(text) <= 4000
