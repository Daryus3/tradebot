"""Backtest raporunun genişletilmiş bölümleri: kaldıraç duyarlılığı 1x-20x (düşüş sütunları, canlı botun açmadığı
kaldıraçlar), gruplanmış "tek kural değişince" satırları (mevcut ayarın çevresi), ▲ / ▼ şans bandı işaretleri ve yıl yıl
aylık işlem yığılması. Küçük / sahte verilerle; gerçek emir yok."""
from collections import Counter
from dataclasses import replace

import pytest

import futures_backtest as fb
from test_futures import fcfg, params  # noqa: F401 - pytest fikstürü
from test_futures_minute_scan import MIN, bt_data, k1_of, minute_closes, scan_cfg

DAY = fb.DAY_MS
Y24, Y25 = fb._ms("2024-01-01"), fb._ms("2025-01-01")


def small_inputs(fcfg, **over):
    k1 = {"SOLUSDT": k1_of(minute_closes()), "ADAUSDT": k1_of(minute_closes(dip=0.0) * 0 + 100.0)}
    on = scan_cfg(fcfg, futures_coins="SOL,ADA", **over)
    data = bt_data(k1)
    sigs, funnel = fb.find_signals(on, data)
    windows = {}
    for s in sigs:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * MIN)
        windows[(s.symbol, s.close_ms)] = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    return on, data, sigs, funnel, windows


# ====================================================================== kaldıraç 1x-20x
def test_leverage_table_covers_every_leverage_from_1_to_20(fcfg, monkeypatch):
    cfg = replace(fcfg, stop_loss_pct=4.75, futures_leverage=10, futures_mmr_pct=0.5)
    monkeypatch.setattr(fb, "evaluate", lambda p, *a, **k: p.leverage)
    said = []
    rows = fb.leverage_table(cfg, None, [], {}, progress=said.append)
    assert [r[0] for r in rows] == list(range(1, 21))
    assert said == [f"⏳ Kaldıraç 1x-20x: {k} / 19 ayar hesaplandı" for k in (5, 10, 15, 19)]   # 20x hesaplanmaz
    assert all(ev == lev and note == "" for lev, ev, note in rows[:17])
    # 18x / 19x: zarar kes likidasyona 0.5 puandan yakın: hesaplanır, canlı bot açmaz
    assert [ev for _l, ev, _n in rows[17:19]] == [18, 19]
    assert all(note.startswith("⛔ canlı bot açmaz") for _l, _e, note in rows[17:19])
    # 20x: zarar kes (%4.75) likidasyonun (%4.5) ötesinde: hesaplanmaz
    assert rows[19][1] is None and "likidasyonun (%4.5) ötesinde" in rows[19][2]


def test_leverage_rows_show_drawdowns_current_marker_and_notes(fcfg):
    on, data, sigs, funnel, windows = small_inputs(fcfg)
    ev = fb.evaluate(fb.params_from_cfg(on), data, sigs, windows)
    levs = [(1, ev, ""), (on.futures_leverage, ev, ""), (19, ev, "⛔ canlı bot açmaz: deneme"),
            (20, None, "zarar kes deneme")]
    lines = fb.format_report(on, data, ev, funnel, levs=levs).splitlines()
    assert any("A) tek hesap | B) kârlı yıl | A'da en büyük düşüş | B'de en kötü yıl içi düşüş | C'de likidasyon | "
               "kill-switch (%" in x for x in lines)
    cur = next(x for x in lines if x.strip().startswith(f"{on.futures_leverage}x"))
    assert "◀ şimdiki" in cur and "düşüş %" in cur and "en kötü yıl %" in cur
    assert cur.endswith("likidasyon 0 | kill-switch yok") or "| kill-switch 20" in cur
    stopped = fb.Evaluation(replace(ev.chained, kill_ms=fb._ms("2020-03-12")), ev.years, ev.fixed)
    assert fb._lev_line(stopped).endswith("| kill-switch 2020-03-12")
    assert any(x.strip().startswith("19x") and x.endswith("| ⛔ canlı bot açmaz: deneme") for x in lines)
    assert any(x.strip().startswith("20x") and x.endswith("— (zarar kes deneme)") for x in lines)
    assert any("C'nin işlem başı sonucu her kaldıraçta aynıdır" in x and "en fazla 1.000.000 USDT kabul edildiği" in x
               for x in lines)


# ====================================================================== tek kural değişince
def rich_cfg(fcfg, **over):
    base = dict(futures_max_positions=4, futures_extra_size_pct=16.7, futures_isolated_size_pct=25.0,
                futures_reentry_breadth=3, breakeven_enabled=True, breakeven_trigger_pct=1.1, breakeven_floor_pct=0.25,
                futures_extra_be_trigger_pct=0.4, futures_extra_hold_minutes=35, futures_wave_hold_minutes=60,
                futures_dd_scale_pct=30.0, loss_cooldown_minutes=240, symbol_cooldown_minutes=0, loss_streak_limit=0,
                take_profit_pct=6.0, stop_loss_pct=4.75, futures_leverage=10, futures_margin_usage_pct=95.0,
                futures_slippage_pct=0.02, futures_tp_order="limit", max_hold_minutes=45, futures_extra_breadth=8)
    base.update(over)
    return small_inputs(fcfg, **base)


def test_variant_rows_are_grouped_and_cover_settings_around_the_current_ones(fcfg, monkeypatch):
    on, data, sigs, _funnel, windows = rich_cfg(fcfg)
    monkeypatch.setattr(fb, "evaluate", lambda p, *a, **k: p)             # Satır → o satırın SimParams'ı
    said = []
    rows = fb.variant_rows(on, data, sigs, windows, progress=said.append)
    n = sum(1 for _n, e in rows if e is not None)
    assert said[0] == f"⏳ Tek kural değişince: 10 / {n} ayar hesaplandı" and said[-1].endswith(f"{n} / {n} ayar hesaplandı")
    assert len(said) == n // 10 + (1 if n % 10 else 0)
    assert [n for n, e in rows if e is None] == ["Çıkışlar ve başabaş", "Pozisyon büyüklüğü ve dalga modu",
                                                 "Giriş kuralı, tarama ve bekleme", "Maliyet ve gerçekçilik"]
    got = {n: e for n, e in rows if e is not None}
    # Çıkışlar
    assert got["Başabaş tetiği +%0.8 (şimdi +%1.1)"].be_trigger_pct == 0.8
    assert got["Başabaş tabanı +%0.35 (şimdi +%0.25)"].be_floor_pct == 0.35
    assert not any(n.startswith("Başabaş tabanı +%0.5") for n in got)    # Faz 8 ek tetiğinin (+%0.4) üstü atlanır
    assert got["Kâr Al +%5 (şimdi +%6)"].tp_pct == 5 and got["Kâr Al +%7 (şimdi +%6)"].tp_pct == 7
    assert got["Zarar Kes -%5.25 (şimdi -%4.75)"].sl_pct == 5.25
    assert {"Zaman stopu 30 dk", "Zaman stopu 60 dk", "Zaman stopu 120 dk"} <= set(got)
    assert got["Ek pozisyon başabaşı +%0.6 (şimdi +%0.4)"].extra_be_trigger_pct == 0.6
    assert got["Ek pozisyon zaman stopu 25 dk (şimdi 35 dk)"].extra_hold_min == 25
    assert got["Dalgada ilk pozisyon 75 dk (şimdi 60 dk)"].wave_hold_min == 75
    assert not got["Kâr Al piyasa emriyle (taker)"].tp_limit
    # Büyüklük ve dalga modu
    assert got["Teminat kullanımı %71 (şimdi %95)"].margin_usage_pct == 71
    assert got["Teminat kullanımı %57 (şimdi %95)"].margin_usage_pct == 57
    assert any(n.startswith("En fazla 2 pozisyon (ilk pozisyon bakiyenin %") and e.max_positions == 2
               for n, e in got.items())
    assert any(n.startswith("Ek pozisyon boyu ilkinin %8'i") for n in got)
    assert got["Ek pozisyon: ≥6 coin düşerken (şimdi ≥8)"].extra_breadth == 6
    assert got["Yalnız çöküş tam boy (şimdi %25)"].isolated_size_pct == 100
    assert got["Yalnız çöküş eşiği <5 coin (şimdi <2)"].isolated_breadth == 5
    assert got["Yalnız çöküş eşiği <3 coin (şimdi <2)"].isolated_breadth == 3
    assert not any(n.startswith("Yalnız çöküş eşiği <2 coin") for n in got)     # Şimdiki eşik denenmez
    assert got["Dalgada yeniden giriş ≥5 coin (şimdi ≥3)"].reentry_breadth == 5
    assert got["Düşüşte küçültme kapalı"].dd_scale_pct == 0
    assert got["Düşüşte küçültme boyu %50 (şimdi %60)"].dd_scale_size_pct == 50
    # Giriş kuralı (5 dk taramayla yeni sinyaller), tarama ve bekleme
    main = f"{on.futures_crash_hours} sa %{on.futures_crash_drop_pct:g}"
    assert {"Ek pencere 1 sa %5 · 5 dk taramayla", "Ek pencere 1 sa %7 · 5 dk taramayla",
            f"Ek pencere yok (yalnız {main}) · 5 dk taramayla", "5 dk'da bir tarama (dakikalık tarama kapalı)"} <= set(got)
    assert got["Zarardan sonra bekleme 2 saat (şimdi 4 saat)"].loss_cooldown_min == 120
    assert got["Her işlemden sonra aynı coine 15 dk bekleme"].symbol_cooldown_min == 15
    assert got["Üst üste 3 zarar keste 4 saat mola"].loss_streak_limit == 3
    # Maliyet
    assert got["Kayma 2 kat (%0.04)"].slippage_pct == pytest.approx(0.04)
    assert got["Kayma 5 kat (%0.10)"].slippage_pct == pytest.approx(0.10)
    assert got["Kötümser stop dolumu (fitilin yarısı)"].harsh_stops


def test_variant_rows_follow_the_current_settings(fcfg, monkeypatch):
    monkeypatch.setattr(fb, "evaluate", lambda p, *a, **k: p)
    on, data, sigs, _f, windows = rich_cfg(fcfg, futures_tp_order="market", futures_dd_scale_pct=0.0,
                                           breakeven_enabled=False, loss_streak_limit=3, symbol_cooldown_minutes=30)
    got = {n: e for n, e in fb.variant_rows(on, data, sigs, windows) if e is not None}
    assert got["Kâr Al limit emirle (maker)"].tp_limit and "Kâr Al piyasa emriyle (taker)" not in got
    assert got["Düşüşte küçültme: zirveden %30 düşünce %60 boy"].dd_scale_pct == 30
    assert got["Başabaş kalkanı açık (+%1 → +%0.3)"].be_enabled and "Başabaş kalkanı kapalı" not in got
    assert not any(n.startswith("Başabaş tetiği") for n in got)          # Kalkan kapalıyken tetik / taban denenmez
    assert got["Zarar serisi molası kapalı"].loss_streak_limit == 0 and got["Aynı coine bekleme yok"].symbol_cooldown_min == 0


# ====================================================================== şans bandı ve rapor
def test_noise_band_marks_and_group_headers_in_report(fcfg):
    assert fb._noise_band([10.0, 11.0]) is None
    lo, hi = fb._noise_band([90.0, 100.0, 110.0])
    assert (lo, hi) == (pytest.approx(0.9), pytest.approx(1.1))
    on, data, sigs, funnel, windows = small_inputs(fcfg)
    ev = fb.evaluate(fb.params_from_cfg(on), data, sigs, windows)
    base = ev.chained.final_equity
    up = fb.Evaluation(replace(ev.chained, curve=ev.chained.curve + [(data.end_ms, base * 1.2)]), ev.years, ev.fixed)
    down = fb.Evaluation(replace(ev.chained, curve=ev.chained.curve + [(data.end_ms, base * 0.8)]), ev.years, ev.fixed)
    ruined = fb.Evaluation(replace(ev.chained, ruined_ms=data.end_ms), ev.years, ev.fixed)
    band = (0.9, 1.1)
    assert [fb._mark(x, base, band) for x in (up, down, ruined, ev)] == ["▲ ", "▼ ", "▼ ", "  "]
    assert fb._mark(up, base, None) == ""
    variants = [("Çıkışlar ve başabaş", None), ("Daha iyi", up), ("Daha kötü", down)]
    text = fb.format_report(on, data, ev, funnel, variants=variants, draws=[90.0, 100.0, 110.0])
    assert "🧪 TEK KURAL DEĞİŞİNCE (diğer her şey aynı)" in text and "  ▸ Çıkışlar ve başabaş" in text
    assert "▲ / ▼: A'daki son bakiye mevcut ayarınkinden çekiliş aralığından fazla yüksek / düşük (−%10 … +%10" in text
    assert "  ▲ Daha iyi" in text and "  ▼ Daha kötü" in text
    plain = fb.format_report(on, data, ev, funnel, variants=variants)
    assert "▲ / ▼" not in plain and "  Daha iyi" in plain                # Çekiliş yoksa işaret de yok


# ====================================================================== aylık işlem yığılması
def tr_at(ms, move_pct):
    """Sabit tutarlı (100 USDT) işlem: net sonucu fiyatın move_pct'i."""
    return fb.FutTrade("SOLUSDT", "long", ms, ms + MIN, 100.0, 101.0, "TP", 10, 10.0, 100.0, 0.0, 0.0, move_pct, 10.0,
                       0.0, 0.0, 0.0, False, False, MIN)


def cluster_eval():
    trades = []
    for m in range(1, 13):                                  # 2024: her ay 10 işlem (+%0.1), Mart'ta 40 (+%0.5)
        start = fb._ms(f"2024-{m:02d}-02")
        n, move = (40, 0.5) if m == 3 else (10, 0.1)
        trades += [tr_at(start + i * 3_600_000, move) for i in range(n)]
    trades += [tr_at(fb._ms("2025-01-05") + i * DAY, 0.2) for i in range(6)]     # 2025 Ocak-Şubat: yığılma yok
    trades += [tr_at(fb._ms("2025-02-05") + i * DAY, 0.2) for i in range(5)]
    p = params(start_capital=100.0)
    b24 = fb.FutResult(p, [], [(Y24, 100.0), (fb._ms("2024-02-15"), 110.0), (fb._ms("2024-03-10"), 165.0),
                               (fb._ms("2024-03-20"), 132.0), (fb._ms("2024-03-30"), 220.0),
                               (fb._ms("2024-12-30"), 242.0)], Counter(), Y24, Y25)
    b25 = fb.FutResult(p, [], [(Y25, 100.0), (fb._ms("2025-02-10"), 105.0)], Counter(), Y25, fb._ms("2025-03-01"))
    fixed = fb.FutResult(p, trades, [], Counter(), Y24, fb._ms("2025-03-01"))
    data = fb.FutData(Y24, fb._ms("2025-03-01"), [], {}, None, (None, None), {}, None, "1m")
    return fb.Evaluation(b24, [(2024, b24), (2025, b25)], fixed), data


def test_monthly_clusters_measure_share_profit_and_b_month():
    ev, data = cluster_eval()
    rows = fb.monthly_clusters(ev, data)
    y24, y25 = rows
    assert (y24["year"], y24["n"]) == (2024, 150) and [mo["m"] for mo in y24["clusters"]] == [3]
    mar = y24["clusters"][0]
    assert mar["n"] == 40 and mar["sum"] == pytest.approx(20.0) and mar["x"] == pytest.approx((40 / 31) / (150 / 366))
    assert mar["ret"] == pytest.approx(100.0) and mar["dd"] == pytest.approx(20.0)  # 110 → 220; 165 → 132 düşüş
    jan, feb = (next(mo for mo in y24["months"] if mo["m"] == m) for m in (1, 2))
    assert jan["ret"] == pytest.approx(0.0) and feb["ret"] == pytest.approx(10.0) and not feb["cluster"]
    assert y25["clusters"] == [] and len(y25["months"]) == 2                     # Veri 1 Mart'ta bitiyor


def test_breadth_lines_follow_the_isolated_threshold_and_split_first_positions(fcfg):
    p = replace(fb.params_from_cfg(fcfg), max_positions=4, isolated_breadth=5, isolated_size_pct=50.0)
    trades = [fb.FutTrade("A", "long", 0, 1, 1, 1, "TP", 2, 1, 2, 0, 0, pnl, 1, 0, 0, 0, False, False, MIN,
                          breadth=b, slot=s)
              for b, s, pnl in ((1, 1, -0.01), (3, 1, 0.002), (4, 1, 0.004), (6, 1, 0.01), (9, 1, 0.02), (12, 2, 0.01))]
    text = fb.breadth_breakdown(trades, p)
    assert "yalnız çöküş (0-4 coin): 3 işlem" in text and "5-7 coin: 1 işlem" in text
    assert "dalga (8+ coin): 2 işlem" in text and "ek pozisyonlar: 1 işlem" in text
    line = fb.first_breadth_line(trades, p)
    assert line == ("İlk pozisyonlar genişliğe göre (sabit tutar; 5'ten az coin düşerken boy %50): "
                    "0-1 coin: 1 işlem, ort. -0.500% | 2-4 coin: 2 işlem, ort. +0.150% | 5-7 coin: 1 işlem, ort. "
                    "+0.500% | 8+ coin: 1 işlem, ort. +1.000%")                   # Ek pozisyon (yuva 2) sayılmaz
    assert "hepsi tam boy" in fb.first_breadth_line(trades, replace(p, isolated_size_pct=100.0))
    assert fb.first_breadth_line(trades, replace(p, max_positions=1)) is None        # Tek pozisyonda satır yok
    old = fb.breadth_breakdown(trades, replace(p, isolated_breadth=2))
    assert "yalnız çöküş (0-1 coin): 1 işlem" in old and "2-7 coin: 3 işlem" in old


def test_turkish_ablative_suffix_for_numbers():
    from config import tr_from
    assert [tr_from(n) for n in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100, 1000,
                                 1_000_000)] == [
        "0'dan", "1'den", "2'den", "3'ten", "4'ten", "5'ten", "6'dan", "7'den", "8'den", "9'dan", "10'dan", "12'den",
        "15'ten", "20'den", "30'dan", "40'tan", "50'den", "60'tan", "70'ten", "80'den", "90'dan", "100'den", "1000'den",
        "1000000'dan"]


def test_turkish_percent_suffixes():
    assert [fb._pct_of(v) for v in (27, 65, 60, 25, 100, 40, 30, 6, 3, 0, 50, 90, 1000, -20.4)] == [
        "%27'si", "%65'i", "%60'ı", "%25'i", "%100'ü", "%40'ı", "%30'u", "%6'sı", "%3'ü", "%0'ı", "%50'si", "%90'ı",
        "%1000'i", "−%20'si"]
    assert [fb._pct_of(v, 1) for v in (81.43, 57.0, 33.3, 76.6, 8.0, 63.25)] == [
        "%81.4'ü", "%57'si", "%33.3'ü", "%76.6'sı", "%8'i", "%63.2'si"]


def test_monthly_cluster_lines_explain_the_effect():
    ev, data = cluster_eval()
    text = "\n".join(fb.monthly_cluster_lines(fb.monthly_clusters(ev, data)))
    assert text.startswith("📆 AYLIK İŞLEM YIĞILMASI (C: sabit pozisyon işlemleri; B: her yıl yeniden başlayan hesap)")
    assert "2024: 150 işlem (ayda ort. 12) | yığılma: Mart 40 (×3.1) → işlemlerin %27'si, C kârının %65'i" in text
    assert "Mart: işlem başı +0.500%, kazanma %100, C +20.0%, B ay +100.0% (düşüş %20)" in text
    assert ("Yığılma ayları olmadan: C +11.0% (yerine +31.0%), işlem başı +0.100% (yığılma aylarında +0.500%) | B "
            "(bu aylar yatay geçseydi) +21.0% (yerine +142.0%)") in text
    assert "2025: 11 işlem (ayda ort. 6) | yığılma yok; en yoğun ay Ocak 6 işlem (×1.0)" in text
    assert ("Tüm yıllar: 1 / 14 ayda yığılma; bu aylarda işlemlerin %25'i, C kârının %60'ı | işlem başı yığılma "
            "aylarında +0.500%, diğer aylarda +0.109%") in text
    assert fb.monthly_cluster_lines([]) == []


def test_report_places_the_cluster_section_before_the_leverage_table(fcfg):
    on, data, sigs, funnel, windows = small_inputs(fcfg)
    ev = fb.evaluate(fb.params_from_cfg(on), data, sigs, windows)
    text = fb.format_report(on, data, ev, funnel, levs=[(20, None, "deneme")])
    assert "📆 AYLIK İŞLEM YIĞILMASI" in text
    assert text.index("⚠️ MARJİN ÇAĞRISI") < text.index("📆 AYLIK İŞLEM YIĞILMASI") < text.index("🎚️ KALDIRAÇ DUYARLILIĞI")
