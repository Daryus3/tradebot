"""24 saat freni (FUTURES_DAY_BRAKE_PCT / FUTURES_DAY_BRAKE_SIZE_PCT; rapor 22 araştırması, 23 Kasım): sermaye son 24
saatteki en yüksek değerinin eşik kadar altındayken yeni pozisyonlar küçük boyla açılır. Ayar ve ortak kural, backtest
motoru (tek pozisyon / dalga modu, sabit tutar), rapor satırları, canlı / test modunda teminat, kapanış kayıtları
(/sermayeekle, durum dosyası) ve Telegram'daki 🧯 Risk sigortaları satırları. Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, futures_brake_factor, load_config, window_peak
from runtime_settings import EDITABLE
from state import BotState, StateStore
from strategy import FUT_BAR_MS, FuturesSignal
from test_futures import BASE_MS, fcfg, make_fbot, params, signal, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, crashing, flat, live_cfg, press
from test_settings_menu import buttons, env_text, fut, spot  # noqa: F401 - pytest fikstürü

MIN = fb.MIN_MS
HOUR = 60 * MIN


# ====================================================================== ayar ve ortak kural
def test_settings_defaults_validation_env_notes_and_rule(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_day_brake_pct, fcfg.futures_day_brake_size_pct) == (0.0, 50.0)     # Varsayılan: kapalı
    for bad in (dict(futures_day_brake_pct=-1.0), dict(futures_day_brake_pct=60.0),
                dict(futures_day_brake_size_pct=5.0), dict(futures_day_brake_size_pct=120.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    replace(fcfg, futures_day_brake_pct=10.0, futures_day_brake_size_pct=70.0).validate()
    monkeypatch.setenv("FUTURES_DAY_BRAKE_PCT", "10")
    monkeypatch.setenv("FUTURES_DAY_BRAKE_SIZE_PCT", "60")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_day_brake_pct, loaded.futures_day_brake_size_pct) == (10.0, 60.0)
    assert EDITABLE["FUTURES_DAY_BRAKE_PCT"].attr == "futures_day_brake_pct"
    assert EDITABLE["FUTURES_DAY_BRAKE_SIZE_PCT"].attr == "futures_day_brake_size_pct"
    notes = " ".join(_setting_notes(replace(fcfg, futures_day_brake_pct=10.0)))
    assert "FUTURES_DAY_BRAKE_PCT=10, FUTURES_DAY_BRAKE_SIZE_PCT=50 (24 saat freni)" in notes
    assert "+%1.426 → +%1.841" in notes
    assert not any("FUTURES_DAY_BRAKE_PCT" in n for n in _setting_notes(fcfg))
    on = replace(fcfg, futures_day_brake_pct=10.0, futures_day_brake_size_pct=70.0)
    assert futures_brake_factor(on, 90.0, 100.0) == pytest.approx(0.7)              # Tam %10 aşağıda
    assert futures_brake_factor(on, 90.01, 100.0) == 1.0 and futures_brake_factor(fcfg, 10.0, 100.0) == 1.0


def test_window_peak_counts_only_the_last_mark_before_the_window():
    marks = [(0, 120.0), (10, 100.0), (50, 90.0), (70, 95.0)]
    # Pencere 30-80: pencere başındaki sermaye (10'daki kayıt: 100), içindekiler (90, 95) ve şimdiki
    assert window_peak(marks, 80, 50, 95.0) == pytest.approx(100.0)
    assert window_peak(marks, 80, 75, 95.0) == pytest.approx(120.0)                 # 0'daki kayıt da pencere başı
    assert window_peak(marks, 200, 50, 95.0) == pytest.approx(95.0)                 # Hepsi eski: son kayıt = şimdiki
    assert window_peak([], 10, 5, 42.0) == pytest.approx(42.0)
    assert window_peak(marks, 80, 50, 130.0) == pytest.approx(130.0)                # Şimdiki daha yüksek


def test_state_keeps_window_marks_and_first_close_records_capital_before():
    st = BotState.new("paper", 100.0)
    st.realized_pnl = -10.0
    st.note_capital(1000.0, 100.0, before=100.0)            # İlk kayıt: kapanıştan önceki sermaye de yazılır
    assert st.capital_marks == [[1000.0, 100.0], [1000.0, 90.0]]
    st.realized_pnl = -5.0
    st.note_capital(1050.0, 100.0, before=90.0)
    assert st.capital_marks[-1] == [1050.0, 95.0] and len(st.capital_marks) == 3
    st.note_capital(1200.0, 100.0, before=95.0)             # Pencere 1100-1200: 1050'deki kayıt pencere başı
    assert st.capital_marks == [[1050.0, 95.0], [1200.0, 95.0]]


# ====================================================================== backtest motoru
def bt_data(hours=30):
    return fb.FutData(BASE_MS, BASE_MS + hours * HOUR, [], {}, None, (np.zeros(0), np.zeros(0)), {}, None, "1m")


def bt_sig(symbol, close_ms, breadth=9):
    s = fb.FutSignal(close_ms, symbol, "long", 1.0, 100.0)
    s.breadth = breadth
    return s


LOSS = [(100, 100.1, 95.0, 95.5)]          # Zarar kes (%4): 10x'te teminatın %40'ı
WIN = [(100, 103.5, 99.9, 103.0)]          # Kâr Al (%3): 10x'te teminatın %30'u


def brake_params(**over):
    base = dict(tp_pct=3.0, sl_pct=4.0, be_enabled=False, day_brake_pct=15.0, day_brake_size_pct=70.0)
    base.update(over)
    return params(**base)


def test_single_slot_brakes_within_24_hours_and_returns_to_full_size():
    sigs = [bt_sig("AAAUSDT", BASE_MS), bt_sig("BBBUSDT", BASE_MS + 10 * MIN), bt_sig("CCCUSDT", BASE_MS + 25 * HOUR)]
    w = {(s.symbol, s.close_ms): window(LOSS if i == 0 else WIN, start=s.close_ms) for i, s in enumerate(sigs)}
    res = fb.simulate(brake_params(), bt_data(), sigs, w)
    a, b, c = res.trades
    assert a.margin == pytest.approx(100.0) and a.pnl == pytest.approx(-40.0) and not a.braked
    # Bakiye 60: son 24 saatin zirvesinin (100) %40 altında → teminat 60 × %70 = 42
    assert b.margin == pytest.approx(42.0) and b.braked and b.pnl == pytest.approx(12.6)
    # 25 saat sonra pencerede zarar yok (pencere başındaki bakiye 72.6): tam boy
    assert c.margin == pytest.approx(72.6) and not c.braked
    off = fb.simulate(brake_params(day_brake_pct=0.0), bt_data(), sigs, w)
    assert off.trades[1].margin == pytest.approx(60.0) and not any(t.braked for t in off.trades)
    # Sabit tutarlı C senaryosu: fren uygulanmaz
    fixed = fb.simulate(brake_params(), bt_data(), sigs, w, fixed_notional=1000.0)
    assert [t.notional for t in fixed.trades] == pytest.approx([1000.0] * 3) and not any(t.braked for t in fixed.trades)


def test_slots_brake_multiplies_the_position_share_and_stacks_with_dd_scale():
    sigs = [bt_sig("AAAUSDT", BASE_MS), bt_sig("BBBUSDT", BASE_MS + 10 * MIN)]
    w = {("AAAUSDT", sigs[0].close_ms): window(LOSS, start=sigs[0].close_ms),
         ("BBBUSDT", sigs[1].close_ms): window(WIN, start=sigs[1].close_ms)}
    p = brake_params(max_positions=2, extra_breadth=8)
    a, b = fb.simulate(p, bt_data(), sigs, w).trades
    assert a.margin == pytest.approx(50.0) and a.pnl == pytest.approx(-20.0)          # Yuva payı: 100 × 1/2
    assert b.margin == pytest.approx(80.0 * 0.5 * 0.7) and b.braked                    # %20 aşağıda: fren
    both = fb.simulate(replace(p, dd_scale_pct=20.0, dd_scale_size_pct=50.0), bt_data(), sigs, w).trades[1]
    assert both.margin == pytest.approx(80.0 * 0.5 * 0.7 * 0.5) and both.braked and both.dd_scaled
    light = fb.simulate(replace(p, day_brake_pct=25.0), bt_data(), sigs, w).trades[1]
    assert light.margin == pytest.approx(40.0) and not light.braked                    # Eşiğin üstünde: tam boy


def test_params_report_header_and_variant_rows(fcfg):
    p = fb.params_from_cfg(replace(fcfg, futures_day_brake_pct=10.0, futures_day_brake_size_pct=60.0))
    assert (p.day_brake_pct, p.day_brake_size_pct) == (10.0, 60.0)
    assert fb.params_from_cfg(fcfg).day_brake_pct == 0.0
    rows = [n for n, _kw in fb.brake_variant_rows(fb.params_from_cfg(fcfg), lambda **kw: kw)]
    assert rows == ["24 saat freni: son 24 saatin zirvesinden %10 düşünce %50 boy"]
    on = fb.params_from_cfg(replace(fcfg, futures_day_brake_pct=10.0))
    got = dict(fb.brake_variant_rows(on, lambda **kw: kw))
    assert got["24 saat freni kapalı"] == {"day_brake_pct": 0.0}
    assert "24 saat freni eşiği %15 (şimdi %10)" in got and "24 saat freni boyu %70 (şimdi %50)" in got
    from test_futures_minute_scan import bt_data as m_data, k1_of, minute_closes, scan_cfg
    k1 = {"SOLUSDT": k1_of(minute_closes())}
    scan = scan_cfg(fcfg, futures_coins="SOL")
    data = m_data(k1)
    sigs, funnel = fb.find_signals(scan, data)
    windows = {}
    for s in sigs:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * MIN)
        windows[(s.symbol, s.close_ms)] = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    brake_on = replace(scan, futures_day_brake_pct=10.0)
    ev = fb.evaluate(fb.params_from_cfg(brake_on), data, sigs, windows)
    text = fb.format_report(brake_on, data, ev, funnel)
    n, braked = len(ev.chained.trades), sum(t.braked for t in ev.chained.trades)
    assert n and (f"🚦 24 saat freni: bakiye son 24 saatteki en yüksek değerinin %10 altındayken yeni pozisyonlar %50 "
                  f"boy (A ve B'de; C sabit tutarla). A'da {braked} / {n} işlem frenle açıldı") in text
    assert "24 saat freni" not in fb.format_report(scan, data, fb.evaluate(fb.params_from_cfg(scan), data, sigs,
                                                                             windows), funnel)


def test_leverage_table_adds_brake_rows(fcfg):
    from test_futures_minute_scan import bt_data as m_data, k1_of, minute_closes, scan_cfg
    k1 = {"SOLUSDT": k1_of(minute_closes())}
    scan = replace(scan_cfg(fcfg, futures_coins="SOL"), futures_max_positions=4, futures_leverage=3)
    data = m_data(k1)
    sigs, _funnel = fb.find_signals(scan, data)
    windows = {}
    for s in sigs:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * MIN)
        windows[(s.symbol, s.close_ms)] = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    rooms = {lev: 100.0 for lev in range(1, 21)}
    off = fb.brake_lev_rows(scan, data, sigs, windows, rooms)
    assert [r.label for r in off] == ["3x + 24 sa fren", "4x + 24 sa fren", "5x + 24 sa fren"]
    assert all(r.ev for r in off)                      # Kısa test verisinde aylık başlangıç özeti yok
    on = fb.brake_lev_rows(replace(scan, futures_day_brake_pct=10.0), data, sigs, windows, rooms)
    assert [r.label for r in on] == ["1x, fren yok", "2x, fren yok", "3x, fren yok"]
    assert fb.brake_lev_rows(replace(scan, futures_max_positions=1), data, sigs, windows, rooms) == []


# ====================================================================== canlı / test modu
def crash_closes():
    return {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}


def test_live_entry_margin_brakes_only_within_24_hours(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_leverage=2, futures_day_brake_pct=10.0)
    bot, _fake, notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(crash_closes()))
    bot.state.capital_marks = [[clock.t - 3 * 3600, 40.0], [clock.t - 2 * 3600, 30.0]]    # 2 saat önce 40 → 30
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions["SOLUSDT"]
    assert pos.margin_usdt == pytest.approx(30 * 0.95 * 0.5, rel=0.02)
    assert "24 saat freni: sermaye son 24 saatin zirvesinin %25 altında, pozisyon %50 boy" in pos.signal_note
    assert any("24 saat freni" in m for m in notes.messages)          # Pozisyon mesajında
    # Zarar 24 saatten eski, düşüş eşiğin altında ya da ayar kapalı: tam boy
    day = 24 * 3600
    cases = (("eski", {}, [[clock.t - day - 7200, 40.0], [clock.t - day - 3600, 30.0]]),
             ("az", {}, [[clock.t - 3600, 32.0], [clock.t - 1800, 30.0]]),
             ("kapalı", {"futures_day_brake_pct": 0.0}, [[clock.t - 3600, 40.0], [clock.t - 1800, 30.0]]))
    for name, over, marks in cases:
        c = live_cfg(fcfg, futures_leverage=2, **({"futures_day_brake_pct": 10.0} | over))
        b2, _f, _n, clock2 = make_fbot(c, tmp_path / name, fake=KlineCcxt(crash_closes()))
        b2.state.capital_marks = marks
        b2.scan_and_enter(clock2.t)
        p2 = b2.state.positions["SOLUSDT"]
        assert p2.margin_usdt == pytest.approx(30 * 0.95, rel=0.02) and "24 saat freni" not in p2.signal_note


def test_live_wave_mode_margin_formula(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_max_positions=2, futures_day_brake_pct=10.0, futures_day_brake_size_pct=50.0)
    bot, _fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(crash_closes()))
    sig = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0, "breadth": 9.0})
    assert bot._entry_margin(sig, 30.0) == pytest.approx(30 * 0.95 / 2)
    bot.state.capital_marks = [[clock.t - 600, 45.0], [clock.t - 60, 30.0]]          # %33 aşağıda
    assert bot._entry_margin(sig, 30.0) == pytest.approx(30 * 0.95 / 2 * 0.5)
    assert sig.metrics["brake_size_pct"] == 50.0 and sig.metrics["brake_pct"] == pytest.approx(33.3)


def test_closes_record_marks_added_capital_and_state_file(fcfg, tmp_path):
    cfg = replace(fcfg, futures_day_brake_pct=1.0)
    bot, fake, _notes, clock = make_fbot(cfg, tmp_path)
    assert bot.state.capital_marks == []
    bot.open_futures(signal("ETHUSDT", "short"), clock.t)
    fake.prices["ETHUSDT"] = 104.1                                     # Zarar kes
    bot.manage_positions(clock.t + 120)
    capital = bot.state.capital
    marks = bot.state.capital_marks
    assert capital < 29.0 and [m[1] for m in marks] == pytest.approx([30.0, capital])   # Önce / sonra
    sig = signal("ETHUSDT", "long")
    assert bot._brake_factor(sig) == pytest.approx(0.5) and sig.metrics["brake_size_pct"] == 50.0
    bot.add_capital(10.0)                                               # Yatırılan para kayıtlara da eklenir
    assert [m[1] for m in bot.state.capital_marks] == pytest.approx([40.0, capital + 10.0])
    assert StateStore(tmp_path / "s.json").load().capital_marks == bot.state.capital_marks
    legacy = BotState.from_dict({"mode": "paper", "start_capital": 25.0, "realized_pnl": -5.0})
    assert legacy.capital_marks == []                                   # Eski durum dosyası: alan yok


# ====================================================================== Telegram
def test_risk_page_rows_status_and_env(fut, tmp_path):
    bot, notes = fut
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    data = buttons(keyboard)
    assert "🚦 24 saat freni: kapalı (son 24 saatin zirvesi 30" in text and "%0.0 aşağıda" in text
    assert "kv:risk:FUTURES_DAY_BRAKE_PCT:10" in data and not any("FUTURES_DAY_BRAKE_SIZE_PCT" in d for d in data)
    press(bot, "kv:risk:FUTURES_DAY_BRAKE_PCT:10")
    assert bot.cfg.futures_day_brake_pct == 10.0 and "FUTURES_DAY_BRAKE_PCT=10" in env_text(bot)
    text, keyboard, _mid = notes.menus[-1]
    assert "kv:risk:FUTURES_DAY_BRAKE_SIZE_PCT:70" in buttons(keyboard)
    press(bot, "kv:risk:FUTURES_DAY_BRAKE_SIZE_PCT:70")
    assert bot.cfg.futures_day_brake_size_pct == 70.0 and "FUTURES_DAY_BRAKE_SIZE_PCT=70" in env_text(bot)
    text = notes.menus[-1][0]
    assert "son 24 saatin zirvesinden %10 düşünce pozisyon %70 boy" in text and "sıradaki pozisyon tam boy" in text
    now = bot.time_fn()
    bot.state.capital_marks = [[now - 600, 40.0], [now - 60, 30.0]]
    press(bot, "risk")
    assert "şu an %25.0 aşağıda → sıradaki pozisyon <b>%70 boy</b>" in notes.menus[-1][0]


def test_risk_page_has_no_brake_rows_for_spot(spot):
    bot, notes = spot
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    assert "24 saat freni" not in text and not any("FUTURES_DAY_BRAKE" in d for d in buttons(keyboard))
