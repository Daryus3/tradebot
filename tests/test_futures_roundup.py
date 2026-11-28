"""Güçlü sinyalde en küçük emre yuvarlama (FUTURES_QUALITY_ROUNDUP; rapor 20 araştırması, 23 Kasım): kalite puanı en
güçlü %40'taki sinyal Binance'in en küçük emir tutarının altında kalırsa boşta teminat yetiyorsa o tutarla açılır.
Ortak kural, backtest, canlı bot (sahte borsa, gerçek istek yok), ayarlar ve Telegram."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from config import _setting_notes, load_config
from runtime_settings import EDITABLE
from strategy import QUALITY_CUTS, FuturesSignal, quality_roundup_ok, quality_score
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import bt_data, bt_sig, press
from test_futures_quality import STRONG, WEAK

MIN = 60_000


def test_roundup_rule_helper():
    strong, weak = quality_score(40.0, STRONG), quality_score(10.0, WEAK)
    assert quality_roundup_ok(True, strong) and not quality_roundup_ok(True, weak)
    assert not quality_roundup_ok(False, strong)                                        # Kapalı
    assert not quality_roundup_ok(True, QUALITY_CUTS[60]) and quality_roundup_ok(True, QUALITY_CUTS[60] + 1e-6)
    assert not quality_roundup_ok(True, None) and not quality_roundup_ok(True, float("nan"))


def _sig(symbol, close_ms, metrics, score):
    s = bt_sig(symbol, close_ms, 9, score=score)
    s.metrics = {"tactic": 1.0, **metrics}
    return s


def test_backtest_rounds_strong_small_positions_up_to_the_minimum():
    flat = [(100, 100.05, 99.95, 100.0)] * 60
    # 10 USDT × %95 × ilk pozisyon payı (2/3) × 3x → ~19 USDT ETH pozisyonu; ETH'nin en küçük tutarı 20 USDT, boşta
    # teminat 9.5 USDT × 3x = 28.5 USDT'ye yetiyor
    strong = _sig("ETHUSDT", BASE_MS, STRONG, 40.0)
    weak = _sig("ETHUSDT", BASE_MS + 90 * MIN, WEAK, 10.0)
    w = {(s.symbol, s.close_ms): window(flat, start=s.close_ms) for s in (strong, weak)}
    p = params(start_capital=10.0, leverage=3, be_enabled=False, hold_min=30, margin_usage_pct=95.0, max_positions=4,
               extra_breadth=8, extra_size_pct=16.7)
    assert fb.min_notional("ETHUSDT") == 20.0
    off = fb.simulate(p, bt_data(), [strong, weak], w)
    assert not off.trades and off.skipped["minimum emir tutarının altında"] == 2
    on = fb.simulate(replace(p, quality_roundup=True), bt_data(), [strong, weak], w)
    assert len(on.trades) == 1 and on.trades[0].symbol == "ETHUSDT" and on.trades[0].rounded
    assert on.trades[0].notional == pytest.approx(20.0) and on.skipped["minimum emir tutarının altında"] == 1
    poor = fb.simulate(replace(p, quality_roundup=True, start_capital=6.0), bt_data(), [strong], w)
    assert not poor.trades                                                              # 6 × %95 × 3x < 20: pas


def test_variant_row_toggles_the_roundup_rule(fcfg):
    p = fb.params_from_cfg(replace(fcfg, futures_quality_roundup=True))
    assert p.quality_roundup
    rows = fb.size_variant_rows(replace(fcfg, futures_quality_roundup=True), lambda **kw: kw)
    assert ("Güçlü sinyalde en küçük emre yuvarlama kapalı", {"quality_roundup": False}) in rows
    rows = fb.size_variant_rows(fcfg, lambda **kw: kw)
    assert ("Güçlü sinyalde en küçük emre yuvarlama açık", {"quality_roundup": True}) in rows


def test_live_roundup_only_for_strong_signals_with_free_margin(fcfg, tmp_path):
    cfg = replace(fcfg, futures_tactic="crash", futures_quality_roundup=True)
    bot, _fake, _notes, _clock = make_fbot(cfg, tmp_path)

    class Ex:
        def min_notional(self, symbol):
            return 20.0

        def min_qty(self, symbol):
            return 0.001

        def amount_to_precision(self, symbol, qty):
            return round(qty, 3)
    bot.exchange = Ex()
    strong = FuturesSignal("ETHUSDT", "long", 2000.0, BASE_MS, 40.0, {"tactic": 1.0, **STRONG})
    weak = FuturesSignal("ETHUSDT", "long", 2000.0, BASE_MS, 10.0, {"tactic": 1.0, **WEAK})
    got = bot._roundup_qty(strong, "ETHUSDT", 0.007, 2000.0, 6, free=10.0)              # 14 USDT → en küçük 20 × 1.05
    assert got == pytest.approx(0.011) and strong.metrics["rounded_up"] == pytest.approx(22.0)
    assert "en küçük emir tutarına yuvarlandı (22.00 USDT)" in strong.note()
    assert bot._roundup_qty(weak, "ETHUSDT", 0.007, 2000.0, 6, free=10.0) == 0.007      # Zayıf: pas geçilir
    assert bot._roundup_qty(strong, "ETHUSDT", 0.007, 2000.0, 6, free=3.0) == 0.007     # Teminat yetmiyor
    assert bot._roundup_qty(strong, "ETHUSDT", 0.02, 2000.0, 6, free=10.0) == 0.02      # Zaten yeterli
    bot.cfg = replace(cfg, futures_quality_roundup=False)
    assert bot._roundup_qty(strong, "ETHUSDT", 0.007, 2000.0, 6, free=10.0) == 0.007    # Kapalı (varsayılan)


def test_roundup_setting_env_runtime_notes_and_menu(fcfg, monkeypatch, tmp_path):
    assert fcfg.futures_quality_roundup is False
    monkeypatch.setenv("FUTURES_QUALITY_ROUNDUP", "true")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert loaded.futures_quality_roundup is True
    assert EDITABLE["FUTURES_QUALITY_ROUNDUP"].kind == "bool"
    assert not _setting_notes(replace(fcfg, strategy="futures", futures_quality_roundup=True))  # Açılış notu yok (rapor 34: bilgilendirme notları kaldırıldı)
    import settings_menu as sm
    rows = {r.key: r for r in sm.PAGES["fw"].rows if isinstance(r, sm.Row)}
    assert rows["FUTURES_QUALITY_ROUNDUP"].show(replace(fcfg, strategy="futures", futures_tactic="crash"))
    bot, _fake, notes_, _clock = make_fbot(replace(fcfg, futures_tactic="crash"), tmp_path / "b")
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    data = [d for row in notes_.menus[-1][1] for _l, d in row]
    toggle = [d for d in data if "FUTURES_QUALITY_ROUNDUP" in d]
    assert toggle and all(len(d.encode()) <= 64 for d in data)
    press(bot, toggle[0])
    assert bot.cfg.futures_quality_roundup is True and "FUTURES_QUALITY_ROUNDUP=true" in env.read_text()
    assert "🔼 Güçlü sinyal (kalite puanı en güçlü %40)" in notes_.menus[-1][0]
