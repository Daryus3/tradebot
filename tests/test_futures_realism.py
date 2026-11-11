"""Backtest gerçekçilik payları (Kasım 2026, saniyelik işlem verisiyle ayarlandı): piyasa emriyle girişte gecikme payı
ve başabaşın tetiklendiği mumda fiyatın tabana geri dönme olasılığı. Paylar kapalıyken motor eskisi gibi; Faz 8 ek
tetiği tabanın altında kalırsa (duyarlılık satırları) ana tetik kullanılır."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from test_futures import BASE_MS, fcfg, params, sig, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import bt_data, bt_sig

MIN = 60_000


# ====================================================================== giriş gecikmesi
def test_entry_lag_applies_to_market_entries_only(fcfg):
    assert fb.ENTRY_LAG_PCT == 0.05 and fb.params_from_cfg(fcfg).entry_lag_pct == 0.05   # Varsayılan: açık
    rows = [(100, 100.2, 99.9, 100.1)] * 5
    off = fb.run_trade(params(hold_min=3), sig(), window(rows), margin=10.0, notional=100.0)
    on = fb.run_trade(params(hold_min=3, entry_lag_pct=0.05), sig(), window(rows), margin=10.0, notional=100.0)
    assert off.entry == pytest.approx(100.0) and on.entry == pytest.approx(100.05)       # Long: daha pahalıya
    assert on.exit == off.exit == pytest.approx(100.0)                                   # Aynı çıkış (zaman stopu)
    assert off.pnl == pytest.approx(0.0) and on.pnl == pytest.approx(-100.0 / 100.05 * 0.05)     # Fark: pahalı giriş
    short = fb.run_trade(params(hold_min=3, entry_lag_pct=0.05), sig("short"), window(rows), margin=10.0, notional=100.0)
    assert short.entry == pytest.approx(99.95)                                           # Short: daha ucuza
    # Kayma ile toplanır; limit (maker) girişte uygulanmaz
    both = fb.run_trade(params(hold_min=3, entry_lag_pct=0.05, slippage_pct=0.02), sig(), window(rows), 10.0, 100.0)
    assert both.entry == pytest.approx(100.07)
    lim = params(hold_min=3, entry_lag_pct=0.05, entry_limit=True, limit_offset_pct=0.05, limit_ttl_min=3)
    filled = fb.run_trade(lim, sig(), window(rows), margin=10.0, notional=100.0)
    assert filled is not None and filled.entry == pytest.approx(99.95)


# ====================================================================== mum içi başabaş
def test_be_retouch_probability_shape():
    q = fb.be_retouch_prob
    base = q(1, 101.0, 100.1, 100.6, 100.5, 100.3)                 # tetik 100.5, taban 100.3, mum 100.1-101.0
    assert 0 < base < 1
    assert q(1, 101.0, 100.1, 100.6, 100.4, 100.3) > base          # Dar aralık: olasılık yüksek
    assert q(1, 101.0, 100.1, 100.6, 101.0, 100.3) < base          # Geniş aralık: düşük
    assert q(1, 101.0, 100.1, 100.35, 100.5, 100.3) > base         # Kapanış tabana yakın: yüksek
    assert q(1, 102.0, 99.5, 100.6, 100.5, 100.3) > base           # Geniş (oynak) mum: yüksek
    assert q(1, 101.0, 100.1, 100.6, 100.5, 100.3, k=0.0) == 1.0 and q(1, 101.0, 100.1, 100.6, 100.3, 100.3) == 1.0
    assert q(-1, 99.0, 99.9, 99.4, 99.5, 99.7) == pytest.approx(base)          # Short: ayna
    assert fb.BE_RETOUCH_K == 45.0


def test_trade_uniform_is_stable_and_spread():
    u = fb.trade_uniform("BTCUSDT", BASE_MS)
    assert u == fb.trade_uniform("BTCUSDT", BASE_MS) and 0 <= u < 1
    vals = [fb.trade_uniform(f"C{i}USDT", BASE_MS + i * MIN) for i in range(400)]
    assert 0.4 < sum(vals) / len(vals) < 0.6 and min(vals) < 0.05 and max(vals) > 0.95


# Yükselen mum (kapanış > açılış): dip 100.1 (taban 100.2'nin altında), tepe 100.9 (tetik 100.5'in üstünde), kapanış
# 100.8 (tabanın üstünde). Sonra fiyat yukarıda kalır ve 3 dk'da zaman stopuyla +%0.8'den çıkılır.
EVENT = [(100.3, 100.9, 100.1, 100.8)] + [(100.8, 100.85, 100.75, 100.8)] * 6


def _event_trade(**over):
    return fb.run_trade(params(hold_min=3, tp_pct=5.0, sl_pct=3.0, **over), sig(), window(EVENT), 10.0, 100.0)


def test_intrabar_breakeven_event_off_forced_and_recorded():
    old = _event_trade()                                            # Pay kapalı: eski varsayım, işlem sürer
    assert old.reason == "TIME" and old.exit == pytest.approx(100.8) and old.be_bar == () and old.be_q == -1.0
    stay = _event_trade(be_intrabar=True, be_force=False)           # Çekiliş "kal": aynı sonuç, olay kaydedilir
    assert stay.reason == "TIME" and stay.pnl == pytest.approx(old.pnl)
    assert stay.be_bar == (100.3, 100.9, 100.1, 100.8)
    assert stay.be_q == pytest.approx(fb.be_retouch_prob(1, 100.9, 100.1, 100.8, 100.5, 100.2)) and 0 < stay.be_q < 1
    out = _event_trade(be_intrabar=True, be_force=True)             # Çekiliş "çık": o mumda tabandan
    assert out.reason == "BE" and out.exit == pytest.approx(100.2) and out.exit_ms == BASE_MS and out.pnl < old.pnl


def test_intrabar_breakeven_draw_is_per_trade():
    q = fb.be_retouch_prob(1, 100.9, 100.1, 100.8, 100.5, 100.2)
    names = [f"C{i}USDT" for i in range(60)]
    low = next(n for n in names if fb.trade_uniform(n, BASE_MS) < q)
    high = next(n for n in names if fb.trade_uniform(n, BASE_MS) >= q)
    p = params(hold_min=3, tp_pct=5.0, sl_pct=3.0, be_intrabar=True)
    a = fb.run_trade(p, sig(symbol=low), window(EVENT), 10.0, 100.0)
    b = fb.run_trade(p, sig(symbol=high), window(EVENT), 10.0, 100.0)
    assert (a.reason, b.reason) == ("BE", "TIME") and a.be_q == pytest.approx(q) == b.be_q
    assert fb.run_trade(p, sig(symbol=low), window(EVENT), 10.0, 100.0).pnl == a.pnl      # Tekrarlanabilir


@pytest.mark.parametrize("rows, reason", [
    # Dip tabanın üstünde: fiyat o mumda tabana hiç inmedi → olay yok
    ([(100.3, 100.9, 100.25, 100.8)] + [(100.8, 100.85, 100.75, 100.8)] * 6, "TIME"),
    # Tetik görülmedi → olay yok
    ([(100.3, 100.45, 100.1, 100.4)] + [(100.4, 100.45, 100.35, 100.4)] * 6, "TIME"),
    # Düşen mum (tepe → dip): eskiden de tabandan çıkılırdı
    ([(100.6, 100.9, 100.1, 100.4)] + [(100.4, 100.45, 100.35, 100.4)] * 6, "BE"),
    # Kapanış tabanın altında: eskiden de tabandan çıkılırdı
    ([(100.1, 100.9, 100.05, 100.15)] + [(100.15, 100.2, 100.1, 100.15)] * 6, "BE"),
])
def test_intrabar_breakeven_only_changes_the_ambiguous_bar(rows, reason):
    p = params(hold_min=3, tp_pct=5.0, sl_pct=3.0)
    old = fb.run_trade(p, sig(), window(rows), 10.0, 100.0)
    new = fb.run_trade(replace(p, be_intrabar=True, be_force=True), sig(), window(rows), 10.0, 100.0)
    assert old.reason == new.reason == reason and new.pnl == pytest.approx(old.pnl) and new.be_bar == ()


def test_intrabar_default_on_and_not_applied_without_breakeven(fcfg):
    assert fb.params_from_cfg(fcfg).be_intrabar is True
    off = _event_trade(be_intrabar=True, be_force=True, be_enabled=False)
    assert off.reason == "TIME" and off.be_bar == ()


def test_draw_seed_changes_only_the_draw_and_spread_is_reported(fcfg):
    """Çekiliş tohumu: 0 = eski çekiliş (sonuçlar değişmez); başka tohum aynı işlemi farklı çekebilir. draw_spread aynı
    ayarı farklı çekilişlerle çalıştırır ve rapor aralığı yazar."""
    assert fb.trade_uniform("BTCUSDT", BASE_MS, 0) == fb.trade_uniform("BTCUSDT", BASE_MS)
    assert fb.trade_uniform("BTCUSDT", BASE_MS, 1) != fb.trade_uniform("BTCUSDT", BASE_MS, 2)
    vals = [fb.trade_uniform("BTCUSDT", BASE_MS, seed) for seed in range(1, 401)]
    assert 0.4 < sum(vals) / len(vals) < 0.6
    q = fb.be_retouch_prob(1, 100.9, 100.1, 100.8, 100.5, 100.2)
    out_seed = next(i for i in range(1, 60) if fb.trade_uniform("BTCUSDT", BASE_MS, i) < q)
    stay_seed = next(i for i in range(1, 60) if fb.trade_uniform("BTCUSDT", BASE_MS, i) >= q)
    assert _event_trade(be_intrabar=True, be_seed=out_seed).reason == "BE"
    assert _event_trade(be_intrabar=True, be_seed=stay_seed).reason == "TIME"
    assert fb.SimParams.__dataclass_fields__["be_seed"].default == 0
    # draw_spread: tek hesabın son bakiyesi tohum 1..N ile; başabaş ya da pay kapalıysa boş
    data = bt_data()
    s1 = bt_sig("BTCUSDT", BASE_MS, 12)
    w = {(s1.symbol, s1.close_ms): window(EVENT, start=s1.close_ms)}
    p = params(start_capital=100.0, hold_min=3, tp_pct=5.0, sl_pct=3.0, be_intrabar=True)
    spread = fb.draw_spread(p, data, [s1], w, seeds=59)
    assert len(spread) == 59 and len({round(v, 6) for v in spread}) == 2          # Yalnızca iki sonuç: çık / kal
    assert spread[out_seed - 1] < spread[stay_seed - 1]                           # Tohum sırası 1..N
    assert fb.draw_spread(replace(p, be_intrabar=False), data, [s1], w) == []
    assert fb.draw_spread(replace(p, be_enabled=False), data, [s1], w) == []
    assert fb.draw_spread(p, data, [s1], w, seeds=0) == []
    # Rapor satırı
    ev = fb.evaluate(p, data, [s1], w)
    text = fb.format_report(fcfg, data, ev, fb.Funnel(), draws=[150.0, 90.0, 120.0])
    assert "Çekiliş duyarlılığı" in text and "en düşük 90" in text and "ortanca 120" in text and "en yüksek 150" in text
    assert "Çekiliş duyarlılığı" not in fb.format_report(fcfg, data, ev, fb.Funnel())


# ====================================================================== Faz 8 tetiği tabanın altında kalırsa
def test_extra_trigger_below_floor_falls_back_to_main_trigger():
    """Rapor 15'teki "Başabaş +%5 → +%2" satırı: ek tetik (+%0.5) tabanın (+%2) altında kalınca stop fiyatın üstüne
    konmuş sayılıyor, ek pozisyonlar +%0.5'i görür görmez +%2 kârla kapanmış görünüyordu."""
    data = bt_data()
    a, b = bt_sig("AAAUSDT", BASE_MS, 12), bt_sig("BBBUSDT", BASE_MS + 5 * MIN, 12)
    rows = [(100, 100.05, 99.95, 100.0)] * 2 + [(100, 100.65, 99.98, 100.6)] + [(100.6, 100.65, 100.55, 100.6)] * 40
    w = {(s.symbol, s.close_ms): window(rows, start=s.close_ms) for s in (a, b)}
    p = params(start_capital=100.0, tp_pct=8.0, sl_pct=5.0, be_trigger_pct=5.0, be_floor_pct=2.0, hold_min=30,
               max_positions=2, extra_breadth=8, extra_be_trigger_pct=0.5)
    assert not fb.phase8_active(p) and fb.phase8_active(replace(p, be_trigger_pct=1.0, be_floor_pct=0.3))
    res = fb.simulate(p, data, [a, b], w)
    assert [(t.slot, t.reason) for t in res.trades] == [(1, "TIME"), (2, "TIME")]
    assert all(t.exit == pytest.approx(100.6) for t in res.trades)             # +%2'den hayali çıkış yok
    same = fb.simulate(replace(p, extra_be_trigger_pct=0.0), data, [a, b], w)
    assert [t.pnl for t in res.trades] == pytest.approx([t.pnl for t in same.trades])


# ====================================================================== rapor
def test_realism_label_and_comparison_row(fcfg):
    p = fb.params_from_cfg(fcfg)
    text = fb.realism_label(p)
    assert "Gerçekçilik payları" in text and "~5 sn" in text and "%0.05" in text and "tabana geri dönme" in text
    assert fb.realism_label(replace(p, be_intrabar=False, entry_lag_pct=0.0)) == ""
    assert "tabana" not in fb.realism_label(replace(p, be_enabled=False)) and "~5 sn" in fb.realism_label(
        replace(p, be_enabled=False))
    assert "~5 sn" not in fb.realism_label(replace(p, entry_limit=True))       # Limit girişte gecikme payı yok
    seen = {}

    def ev(**over):
        seen.update(over)
        return "E"
    rows = fb.realism_rows(fcfg, ev)
    assert len(rows) == 1 and "Eski (iyimser) hesap" in rows[0][0] and seen == {"be_intrabar": False, "entry_lag_pct": 0.0}
