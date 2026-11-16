"""Backtest raporunun kaldıraç riski bölümü (🧯 KALDIRAÇ RİSKİ): ayardaki kaldıraç ve bir alt kıyas kaldıracı için yıl
içindeki dipler (tarihleriyle), kill-switch'in hangi yıllarda botu durduracağı, en kötü günler, her ayın 1'inde başlayan
hesapların ilk 12 ayı, zarar kes kayarsa likidasyon bölgesine giren işlemler ve kayma stresi. Rapordaki A / B tabloları
stopun tam stop fiyatından dolduğunu varsayar ve kill-switch'i uygulamaz; bu bölüm ikisini de gösterir."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

import futures_backtest as fb
from config import futures_liq_distance_pct
from test_futures import fcfg  # noqa: F401 - pytest fikstürü
from test_futures_minute_scan import MIN, bt_data, k1_of, minute_closes, scan_cfg

DAY = fb.DAY_MS
Y24, Y25, Y26 = fb._ms("2024-01-01"), fb._ms("2025-01-01"), fb._ms("2026-01-01")


def trade(mae, reason="SL", entry=Y24 + 5 * DAY, symbol="DOTUSDT"):
    return fb.FutTrade(symbol, "long", entry, entry + MIN, 1.0, 0.95, reason, 10, 1.0, 10.0, 0.0, 0.0, -0.5, -50.0, 0.0,
                       mae, mae, False, False, MIN)


# ====================================================================== yardımcılar
def test_reference_leverage_and_curve_helpers():
    assert [fb.risk_ref_leverage(x) for x in (1, 2, 3, 4, 5, 7, 10, 20)] == [None, 1, 2, 3, 3, 5, 5, 5]
    t = [Y24 + i * DAY for i in range(5)]
    curve = [(t[0], 100.0), (t[1], 150.0), (t[2], 30.0), (t[3], 200.0), (t[4], 120.0)]
    st = fb.curve_stats(curve, 100.0, kill_pct=45.0)
    assert st["dd"] == pytest.approx(80.0) and (st["peak_ms"], st["peak"], st["trough_ms"], st["trough"]) == (t[1], 150.0, t[2], 30.0)
    assert (st["low_ms"], st["low"], st["final"]) == (t[2], 30.0, 120.0)
    assert st["kill_ms"] == t[2]                                           # 30 ≤ 100 × (1 − %45): bot o anda dururdu
    assert fb.curve_stats(curve, 100.0)["kill_ms"] is None                 # Kill-switch yoksa
    assert fb.curve_stats([(t[0], 100.0), (t[1], 60.0)], 100.0, 45.0)["kill_ms"] is None
    # Gün içi en düşük bakiye ve gün sonu, bir önceki günün son bakiyesine göre
    day = [(Y24, 100.0), (Y24 + 3_600_000, 40.0), (Y24 + 7_200_000, 80.0), (Y24 + DAY + 60_000, 20.0), (Y24 + DAY + 120_000, 160.0)]
    lows = fb.day_lows(day, 100.0)
    assert lows[Y24 // DAY] == (pytest.approx(-60.0), pytest.approx(-20.0))
    assert lows[Y24 // DAY + 1] == (pytest.approx(-75.0), pytest.approx(100.0))
    # Her ayın 1'i; 12 aylık pencere dönem sonunu aşmamalı, ay ortasından başlayan dönemde sıradaki ayın 1'i
    starts = fb._month_starts(Y24, Y26, 12)
    assert starts[0] == Y24 and starts[-1] == Y25 and len(starts) == 13
    assert fb._month_starts(Y24 + 10 * DAY, Y26, 12)[0] == fb._ms("2024-02-01")
    assert fb._month_starts(Y24, Y24 + 200 * DAY, 12) == []


# ====================================================================== ölçüler ve satırlar
def fake_simulate(p, data, signals, windows, kill_switch=False, start_ms=None, end_ms=None, fixed_notional=None):
    """Kaldıraca göre bakiye eğrisi: 10x'te zirveden %96 düşüş, 5x'te %50; kayma 4 katına çıkınca 10x erir."""
    a = data.start_ms if start_ms is None else start_ms
    b = data.end_ms if end_ms is None else end_ms
    cap = p.start_capital
    if start_ms is None and p.leverage == 10 and p.slippage_pct >= 0.08 - 1e-9:
        curve = [(a, cap), (a + 30 * DAY, cap * 0.05)]
        return SimpleNamespace(curve=curve, ruined_ms=a + 30 * DAY, final_equity=cap * 0.05)
    trough = 0.04 if p.leverage == 10 else 0.5
    curve = [(a, cap), (a + 10 * DAY, cap * 2), (a + 20 * DAY, cap * 2 * trough), (b - DAY, cap * 3)]
    return SimpleNamespace(curve=curve, ruined_ms=None, final_equity=cap * 3)


def risk_cfg(fcfg):
    return replace(fcfg, futures_leverage=10, max_total_loss_pct=45.0, start_capital=28.99)


def test_leverage_risk_measures_and_report_lines(fcfg, monkeypatch):
    monkeypatch.setattr(fb, "simulate", fake_simulate)
    data = fb.FutData(Y24, Y26 + 10 * DAY, [], {}, None, (None, None), {}, None, "1m")
    trades = [trade(12.0), trade(25.0, entry=Y25 + 40 * DAY, symbol="CRVUSDT"), trade(3.0), trade(30.0, reason="TP")]
    risk = fb.leverage_risk(risk_cfg(fcfg), data, [], {}, trades)
    assert (risk["lev"], risk["ref"], risk["levs"], risk["kill"], risk["start"]) == (10, 5, [10, 5], 45.0, 28.99)
    assert [y for y, _st, _r in risk["yearly"][10]] == [2024, 2025]          # 2026'nın 10 günü: yıl satırı yok
    st = risk["yearly"][10][0][1]
    assert st["dd"] == pytest.approx(96.0) and st["trough_ms"] == Y24 + 20 * DAY and st["kill_ms"] == Y24 + 20 * DAY
    assert risk["yearly"][5][0][1]["dd"] == pytest.approx(50.0) and risk["yearly"][5][0][1]["kill_ms"] is None
    assert len(risk["roll"][10]) == len(risk["roll"][5]) == 13
    assert risk["sl"] == 3 and [t.mae_pct for t in risk["beyond"][10]] == [25.0, 12.0] and len(risk["beyond"][5]) == 1
    assert risk["liq"][10] == pytest.approx(futures_liq_distance_pct(10, fcfg.futures_mmr_pct))
    assert [r is not None for _k, r, _e in risk["stress"][10]] == [False, False, True, True]     # Kayma ×4 ve ×5: erir
    assert all(r is None for _k, r, _e in risk["stress"][5])
    text = "\n".join(fb.leverage_risk_lines(risk))
    assert text.startswith("🧯 KALDIRAÇ RİSKİ (10x, kıyas 5x; aynı sinyaller ve çıkışlar, 28.99 USDT ile)")
    assert "kill-switch'i uygulamaz" in text
    assert "2024: 10x +200.0% | −%96.0 (2024-01-21) | 2.32 USDT   ·   5x +200.0% | −%50.0 (2024-01-21) | 28.99 USDT" in text
    assert ("Kill-switch (toplam zarar sermayenin %45'i) uygulansaydı 1 Ocak başlangıçlı hesabı durdururdu: 10x: 2024 "
            "(2024-01-21), 2025 (2025-01-21) | 5x: hiçbir yılda") in text
    assert ("En kötü günler (gün içi en düşük bakiye, bir önceki günün sonuna göre; 10x / 5x): 2024-01-21 −%96 / −%50, "
            "2025-01-21 −%96 / −%50") in text                              # Kazançlı günler listelenmez
    assert "Her ayın 1'inde başlayan 13 hesap (2024-01-01 → 2025-01-01), ilk 12 ayda:" in text
    assert ("10x: %80'i aşan düşüş 13, %90'ı aşan 13, bakiye 5 USDT'nin altına inen 13, eriyen 0, 3 ay sonra zararda 13, "
            "12 ay sonra zararda 0, ortanca 12 ay getirisi +200.0%, kill-switch 13 (ortanca 20. gün)") in text
    assert "5x: %80'i aşan düşüş 0, %90'ı aşan 0" in text and "kill-switch 0" in text
    assert "12 ay sonunda 10x'in 5x'ten az bakiyeyle bittiği başlangıç: 0 / 13" in text
    assert ("Zarar kes kayarsa: 3 zarar kes işleminin 2'inde stop dakikasında fiyat girişin %9.5'inden fazla altına indi "
            "(10x'te stop o dibe kayarsa likidasyon; 5x için %19.5: 1 işlem). En derinleri: 2025-02-10 CRV −%25.0, "
            "2024-01-06 DOT −%12.0") in text
    assert "Kayma katlanırsa (tek hesap, son bakiye USDT): 10x ×2: 86.97, ×3: 86.97, ×4: ERİDİ 2024-01-31" in text
    # 1x'te kıyas yok; kill-switch kapalıysa o satır yok; sonuç yoksa bölüm boş
    one = fb.leverage_risk(replace(risk_cfg(fcfg), futures_leverage=1), data, [], {}, trades)
    assert one["levs"] == [1] and "kıyas" not in fb.leverage_risk_lines(one)[0]
    assert fb.leverage_risk_lines(None) == []


def test_section_follows_the_leverage_table_and_run_cli_computes_it(fcfg, tmp_path, monkeypatch):
    k1 = {"SOLUSDT": k1_of(minute_closes()), "ADAUSDT": k1_of(minute_closes(dip=0.0) * 0 + 100.0)}
    on = scan_cfg(fcfg, futures_coins="SOL,ADA")
    data = bt_data(k1)
    sigs, funnel = fb.find_signals(on, data)
    windows = {}
    for s in sigs:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * MIN)
        windows[(s.symbol, s.close_ms)] = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    ev = fb.evaluate(fb.params_from_cfg(on), data, sigs, windows)
    monkeypatch.setattr(fb, "leverage_risk_lines", lambda risk: ["🧯 KALDIRAÇ RİSKİ (deneme)"])
    text = fb.format_report(on, data, ev, funnel, levs=[(5, None, "deneme")], risk={"lev": 3})
    assert text.index("🎚️ KALDIRAÇ DUYARLILIĞI") < text.index("🧯 KALDIRAÇ RİSKİ (deneme)")
    assert "🧯" not in fb.format_report(on, data, ev, funnel)
    # run_cli: duyarlılık açıkken bölüm hesaplanır ve rapora verilir (veri ve hesaplar sahte)
    seen = {}

    class Ev:
        trades = ["sabit boy işlemleri"]

        class chained:
            trades = []
            params = SimpleNamespace(leverage=10)
    monkeypatch.setattr(fb, "prepare", lambda *a, **k: data)
    monkeypatch.setattr(fb, "find_signals", lambda *a, **k: ([], fb.Funnel()))
    monkeypatch.setattr(fb, "load_windows", lambda *a, **k: {})
    monkeypatch.setattr(fb, "evaluate", lambda *a, **k: Ev)
    for name in ("leverage_table", "exit_grid", "variant_rows", "draw_spread"):
        monkeypatch.setattr(fb, name, lambda *a, **k: [])
    monkeypatch.setattr(fb, "live_comparison", lambda *a, **k: [])
    monkeypatch.setattr(fb, "leverage_risk", lambda cfg, d, s, w, fixed: seen.setdefault("args", fixed) and {"lev": 10})

    def fake_report(cfg, d, ev, funnel, levs, grid, variants, live=None, risk=None):
        seen["risk"] = risk
        return "RAPOR"
    monkeypatch.setattr(fb, "format_report", fake_report)
    monkeypatch.setattr(fb, "save_trades_csv", lambda trades, path: path)
    messages = []
    fb.run_cli(fcfg, 7, None, tmp_path, chart=False, sensitivity=True, progress=messages.append)
    assert seen["risk"] == {"lev": 10} and seen["args"] == ["sabit boy işlemleri"]
    assert any(m.startswith("🧯 Kaldıraç riski hesaplanıyor") for m in messages)
