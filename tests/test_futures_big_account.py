"""Büyük hesap kuralları (FUTURES_BIG_ACCOUNT_USDT; rapor 19 araştırması, 23 Kasım): gerçekleşen bakiye eşiği aşınca
kalite puanı en güçlü %20'lik sinyaller 2 ek pozisyon açabilir, zayıf sinyale son fonlama ≤ 0 ise girilmez. Ortak
kurallar (strategy), backtest, fonlama arşivi, rapor satırları, canlı bot (sahte borsa, gerçek istek yok), ayarlar."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, load_config
from runtime_settings import EDITABLE
from strategy import (BIG_EXTRA_SLOTS, QUALITY_CUTS, FuturesSignal, big_extra_slots, big_funding_skip, big_rules_on,
                      quality_score)
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import bt_data, bt_sig, press
from test_futures_quality import STRONG, WEAK

MIN = 60_000


# ====================================================================== ortak kurallar
def test_big_account_rule_helpers():
    assert BIG_EXTRA_SLOTS == 2 and QUALITY_CUTS[80] > QUALITY_CUTS[60]
    assert big_rules_on(5000, 5000) and not big_rules_on(5000, 4999) and not big_rules_on(0, 1e9)
    strong, weak = quality_score(40.0, STRONG), quality_score(10.0, WEAK)
    assert big_extra_slots(5000, 6000, strong) == 2 and big_extra_slots(5000, 4000, strong) == 0
    assert big_extra_slots(5000, 6000, QUALITY_CUTS[80]) == 0                           # Eşikte değil, üstünde
    assert big_extra_slots(5000, 6000, None) == 0 and big_extra_slots(5000, 6000, float("nan")) == 0
    assert big_funding_skip(5000, 6000, weak, -0.01) and big_funding_skip(5000, 6000, weak, 0.0)
    assert not big_funding_skip(5000, 6000, weak, 0.01)                                 # Fonlama artı: girilir
    assert not big_funding_skip(5000, 6000, strong, -0.05)                              # Güçlü sinyal: girilir
    assert not big_funding_skip(5000, 4000, weak, -0.05)                                # Eşik altı
    assert not big_funding_skip(5000, 6000, weak, None) and not big_funding_skip(0, 6000, weak, -0.05)


# ====================================================================== backtest
def _sig(symbol, close_ms, metrics, score, funding=None):
    s = bt_sig(symbol, close_ms, 9, score=score)
    s.metrics = {"tactic": 1.0, **metrics}
    if funding is not None:
        s.metrics["last_funding"] = funding
    return s


def _flat(start, bars=60):
    return window([(100, 100.05, 99.95, 100.0)] * bars, start=start)        # Çıkış yok: zaman stopuna kadar açık


def _run(p, sigs):
    w = {(s.symbol, s.close_ms): _flat(s.close_ms) for s in sigs}
    return fb.simulate(p, bt_data(), sigs, w)


def test_backtest_extra_slots_and_funding_skip_above_the_threshold():
    four = [_sig(f"S{i}USDT", BASE_MS + i * MIN, WEAK, 10.0) for i in range(4)]         # Dört küçük pozisyon açık
    top = _sig("TOPUSDT", BASE_MS + 5 * MIN, STRONG, 40.0)                              # En güçlü %20: ek yuva
    mid = _sig("MIDUSDT", BASE_MS + 6 * MIN, {**STRONG, "taker15": 0.6, "drop5": -1.0}, 12.0)
    p = params(start_capital=10_000.0, be_enabled=False, hold_min=30, margin_usage_pct=95.0, max_positions=4,
               extra_breadth=8, extra_size_pct=16.7, quality_size_pct=50.0)    # Zayıflar yarım boy: teminat boşta
    assert quality_score(mid.score, mid.metrics) <= QUALITY_CUTS[80] < quality_score(top.score, top.metrics)
    off = _run(p, four + [top, mid])
    assert len(off.trades) == 4 and off.skipped["açık pozisyon vardı"] == 2
    on = _run(replace(p, big_account_usdt=5000.0), four + [top, mid])
    assert [t.symbol for t in on.trades][-1] == "TOPUSDT" and len(on.trades) == 5 and on.trades[-1].slot == 5
    assert on.skipped["açık pozisyon vardı"] == 1                                       # Orta kalite: ek yuva yok
    below = _run(replace(p, big_account_usdt=50_000.0), four + [top])                   # Bakiye eşiğin altında
    assert len(below.trades) == 4
    single = _run(replace(p, max_positions=1, big_account_usdt=5000.0), four[:1] + [top])
    assert len(single.trades) == 1                                                      # Tek pozisyonda etkisiz
    # Zayıf sinyal + son fonlama ≤ 0: girilmez; fonlama artıysa ya da bilinmiyorsa girilir
    weak_neg = _sig("WNUSDT", BASE_MS, WEAK, 10.0, funding=-0.01)
    weak_pos = _sig("WPUSDT", BASE_MS + 40 * MIN, WEAK, 10.0, funding=0.01)
    weak_none = _sig("WXUSDT", BASE_MS + 80 * MIN, WEAK, 10.0)
    strong_neg = _sig("SNUSDT", BASE_MS + 120 * MIN, STRONG, 40.0, funding=-0.05)
    got = _run(replace(p, big_account_usdt=5000.0), [weak_neg, weak_pos, weak_none, strong_neg])
    assert [t.symbol for t in got.trades] == ["WPUSDT", "WXUSDT", "SNUSDT"]
    assert got.skipped["büyük hesap: zayıf sinyal, son fonlama ≤ 0"] == 1
    assert len(_run(p, [weak_neg]).trades) == 1                                         # Kapalıyken girilir


class _Archive:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def funding(self, symbol, start_ms, end_ms):
        self.calls.append(symbol)
        t, r = self.rows.get(symbol, ([], []))
        return np.array(t, dtype=np.int64), np.array(r, dtype=float)


def test_attach_funding_reads_the_last_settled_rate(fcfg):
    data = bt_data()
    h8 = 8 * 3_600_000
    data.archive = _Archive({"AAAUSDT": ([BASE_MS - h8, BASE_MS], [0.0001, -0.0002]),
                             "BBBUSDT": ([BASE_MS - 3 * h8], [0.0003])})
    a1 = bt_sig("AAAUSDT", BASE_MS, 9)                         # Tam ödeme anında: bir önceki ödeme sayılır
    a2 = bt_sig("AAAUSDT", BASE_MS + MIN, 9)
    b = bt_sig("BBBUSDT", BASE_MS, 9)                          # Son ödeme 24 sa önce: bayat, ölçü yok
    c = bt_sig("CCCUSDT", BASE_MS, 9)                          # Arşivde yok
    cfg = replace(fcfg, futures_tactic="crash")
    assert fb.attach_funding(cfg, data, [a1, a2, b, c]) == 2
    assert a1.metrics["last_funding"] == pytest.approx(0.01) and a2.metrics["last_funding"] == pytest.approx(-0.02)
    assert "last_funding" not in b.metrics and "last_funding" not in c.metrics
    assert fb.attach_funding(replace(cfg, futures_tactic="liq4"), data, [bt_sig("AAAUSDT", BASE_MS, 9)]) == 0


def test_report_lines_and_variant_rows_for_big_account_rules(fcfg):
    p = replace(fb.params_from_cfg(fcfg), max_positions=4, big_account_usdt=5000.0)
    names = [n for n, _e in fb.big_variant_rows(p, lambda **kw: kw, True)]
    assert names == ["Büyük hesap kuralları kapalı", "Büyük hesap kuralları: bakiye ≥2.000 USDT (şimdi ≥5.000)",
                     "Büyük hesap kuralları: bakiye ≥10.000 USDT (şimdi ≥5.000)"]
    assert fb.big_variant_rows(replace(p, big_account_usdt=0.0), lambda **kw: kw, True) == [
        ("Büyük hesap kuralları: bakiye ≥5.000 USDT", {"big_account_usdt": 5000.0})]
    assert fb.big_variant_rows(p, lambda **kw: kw, False) == []                         # Fonlama verisi yok
    assert fb.big_variant_rows(replace(p, max_positions=1), lambda **kw: kw, True) == []


# ====================================================================== canlı bot (sahte borsa, test modu)
def test_live_scan_guard_extra_slot_choice_and_funding_skip(fcfg, tmp_path):
    cfg = replace(fcfg, futures_tactic="crash", futures_max_positions=4, futures_big_account_usdt=5000.0)
    bot, _fake, _notes, _clock = make_fbot(cfg, tmp_path)
    bot.state.positions = {f"S{i}USDT": object() for i in range(4)}                     # Dört yuva dolu
    bot.state.realized_pnl = 0.0
    bot.state.start_capital = 6000.0
    assert bot.state.capital >= 5000 and bot._big_on()
    assert bot._scan_block_reason(bot.time_fn()) is None                                # Ek yuva için taranır
    bot.state.start_capital = 1000.0
    assert not bot._big_on() and "boş yer yok" in bot._scan_block_reason(bot.time_fn())
    bot.state.start_capital = 6000.0
    calls = []

    def funding_history(symbol, limit=5):
        calls.append(symbol)
        return [BASE_MS], [-0.0001]
    bot.exchange.funding_history = funding_history                                       # Gerçek istek yok
    weak = FuturesSignal("WEAKUSDT", "long", 100.0, BASE_MS, 10.0, {"tactic": 1.0, **WEAK})
    strong = FuturesSignal("TOPUSDT", "long", 100.0, BASE_MS, 40.0, {"tactic": 1.0, **STRONG})
    assert bot._big_funding_skip(weak) and weak.metrics["last_funding"] == pytest.approx(-0.01)
    assert not bot._big_funding_skip(strong) and calls == ["WEAKUSDT"]                  # Güçlüde fonlama istenmez
    bot.exchange.funding_history = lambda symbol, limit=5: ([BASE_MS], [0.0001])
    assert not bot._big_funding_skip(FuturesSignal("W2USDT", "long", 100.0, BASE_MS, 10.0, {"tactic": 1.0, **WEAK}))
    bot2, _f2, _n2, _c2 = make_fbot(replace(cfg, futures_big_account_usdt=0.0), tmp_path / "b")
    bot2.state.positions = {f"S{i}USDT": object() for i in range(4)}
    assert not bot2._big_on() and not bot2._big_funding_skip(weak)


# ====================================================================== ayarlar ve Telegram
def test_big_account_settings_defaults_validation_env_runtime_and_notes(fcfg, monkeypatch, tmp_path):
    assert fcfg.futures_big_account_usdt == 0.0
    for bad in (50.0, -1.0):
        with pytest.raises(ConfigError):
            replace(fcfg, futures_big_account_usdt=bad).validate()
    monkeypatch.setenv("FUTURES_BIG_ACCOUNT_USDT", "5000")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert loaded.futures_big_account_usdt == 5000.0
    assert EDITABLE["FUTURES_BIG_ACCOUNT_USDT"].attr == "futures_big_account_usdt"
    notes = " ".join(_setting_notes(replace(fcfg, strategy="futures", futures_big_account_usdt=5000.0)))
    assert "FUTURES_BIG_ACCOUNT_USDT=5.000" in notes and "+%1.097 → +%1.423" in notes
    assert not any("FUTURES_BIG_ACCOUNT_USDT" in n for n in _setting_notes(replace(fcfg, strategy="futures")))
    assert fb.params_from_cfg(replace(fcfg, futures_big_account_usdt=5000.0)).big_account_usdt == 5000.0


def test_wave_page_big_account_row_button_and_text(fcfg, tmp_path):
    import settings_menu as sm
    rows = {r.key: r for r in sm.PAGES["fw"].rows if isinstance(r, sm.Row)}
    big = rows["FUTURES_BIG_ACCOUNT_USDT"]
    wave = replace(fcfg, strategy="futures", futures_tactic="crash", futures_max_positions=4)
    assert big.show(wave) and not big.show(replace(wave, futures_max_positions=1))
    assert big.fmt(0) == "Büyük hesap: kapalı" and big.fmt(5000) == "Büyük hesap: ≥5 bin"
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash", futures_max_positions=4), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert "🏦 Büyük hesap kuralları kapalı." in text and "kv:fw:FUTURES_BIG_ACCOUNT_USDT:5000" in data
    assert all(len(d.encode()) <= 64 for d in data)
    press(bot, "kv:fw:FUTURES_BIG_ACCOUNT_USDT:5000")
    assert bot.cfg.futures_big_account_usdt == 5000.0 and "FUTURES_BIG_ACCOUNT_USDT=5000" in env.read_text()
    assert "🏦 Büyük hesap kuralları: bakiye ≥5.000 USDT iken en güçlü sinyaller 2 ek pozisyon" in notes.menus[-1][0]
