"""Uzun ek çöküş penceresi (≥8 saat, ör. 12 saatte %15): ayar notu, backtest raporunda 'penceresi kapalı' satırı ve
dalga modu sayfasında ek pozisyon kapısı ≥10 coin düğmesi (Kasım 2026, rapor_13 testi)."""
from dataclasses import replace

import futures_backtest as fb
from config import _setting_notes, futures_long_windows
from test_futures import fcfg, make_fbot  # noqa: F401 - pytest fikstürü
from test_futures_cascade import press


def test_long_window_helper_and_note(fcfg):
    c = replace(fcfg, futures_tactic="crash", futures_crash_extra="1:6,12:15", futures_crash_z=0.0)
    assert futures_long_windows(c) == ((12, 15.0),)
    assert futures_long_windows(replace(c, futures_crash_extra="1:6,8:12")) == ((8, 12.0),)
    assert futures_long_windows(replace(c, futures_crash_extra="1:6,2:8")) == ()
    assert futures_long_windows(replace(c, futures_crash_extra="abc")) == ()       # Hatalı yazım: not yok, hata yok
    notes = [n for n in _setting_notes(c) if "uzun pencere" in n]
    assert len(notes) == 1 and "12 saatte %15" in notes[0] and "+ 1 sa %6" in notes[0]
    assert not any("uzun pencere" in n for n in _setting_notes(replace(c, futures_crash_extra="1:6")))
    assert not any("uzun pencere" in n for n in _setting_notes(replace(c, futures_crash_z=3.0)))
    assert not any("uzun pencere" in n for n in _setting_notes(replace(c, futures_tactic="liq4")))


def test_long_window_report_row(fcfg):
    calls = []

    def other(**changes):
        calls.append(changes)
        return "ev"
    c = replace(fcfg, futures_tactic="crash", futures_crash_extra="1:6,12:15", futures_crash_z=0.0)
    rows = fb.long_window_rows(c, other)
    assert rows == [("12 sa %15 penceresi kapalı (ek: 1 sa %6)", "ev")] and calls[-1] == {"futures_crash_extra": "1:6"}
    rows = fb.long_window_rows(replace(c, futures_crash_extra="12:15"), other)
    assert rows[0][0] == "12 sa %15 penceresi kapalı (ek pencere yok)" and calls[-1] == {"futures_crash_extra": ""}
    assert fb.long_window_rows(replace(c, futures_crash_extra="1:6"), other) == []
    assert fb.long_window_rows(replace(c, futures_crash_z=3.0), other) == []
    assert len(calls) == 2


def test_wave_page_offers_extra_gate_10(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash", futures_max_positions=4), tmp_path)
    press(bot, "pg:fw")
    data = [d for row in notes.menus[-1][1] for _l, d in row]
    assert {f"kv:fw:FUTURES_EXTRA_BREADTH:{v}" for v in (5, 8, 10, 12)} <= set(data)
    assert all(len(d.encode()) <= 64 for d in data)
