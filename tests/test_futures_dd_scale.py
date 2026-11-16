"""Düşüşte küçültme (FUTURES_DD_SCALE_PCT / FUTURES_DD_SCALE_SIZE_PCT): sermaye ulaştığı en yüksek seviyenin eşik kadar
altındayken yeni pozisyonlar küçük boyla açılır. Ayar ve ortak kural, backtest motoru (tek pozisyon / dalga modu, sabit
tutar, erime), rapor satırı, canlı / test modunda teminat, zirvenin kaydı (kapanış, /sermayeekle, durum dosyası) ve
Telegram'daki 🧯 Risk sigortaları satırları. Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, dd_scale_factor, futures_dd_factor, load_config
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
    assert (fcfg.futures_dd_scale_pct, fcfg.futures_dd_scale_size_pct) == (0.0, 60.0)      # Varsayılan: kapalı
    for bad in (dict(futures_dd_scale_pct=-1.0), dict(futures_dd_scale_pct=95.0), dict(futures_dd_scale_size_pct=4.0),
                dict(futures_dd_scale_size_pct=120.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    replace(fcfg, futures_dd_scale_pct=30.0, futures_dd_scale_size_pct=60.0).validate()
    monkeypatch.setenv("FUTURES_DD_SCALE_PCT", "30")
    monkeypatch.setenv("FUTURES_DD_SCALE_SIZE_PCT", "50")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_dd_scale_pct, loaded.futures_dd_scale_size_pct) == (30.0, 50.0)
    assert EDITABLE["FUTURES_DD_SCALE_PCT"].attr == "futures_dd_scale_pct"
    assert EDITABLE["FUTURES_DD_SCALE_SIZE_PCT"].attr == "futures_dd_scale_size_pct"
    notes = " ".join(_setting_notes(replace(fcfg, futures_dd_scale_pct=30.0)))
    assert "FUTURES_DD_SCALE_PCT=30, FUTURES_DD_SCALE_SIZE_PCT=60 (düşüşte küçültme)" in notes
    assert not any("FUTURES_DD_SCALE_PCT" in n for n in _setting_notes(fcfg))
    # Kural: zirvenin eşik kadar (ya da daha fazla) altındaysa boy küçülür; kapalıyken / zirve yokken hep tam boy
    assert dd_scale_factor(30, 60, 70.0, 100.0) == pytest.approx(0.6)                 # Tam %30 aşağıda
    assert dd_scale_factor(30, 60, 40.0, 100.0) == pytest.approx(0.6)
    assert dd_scale_factor(30, 60, 70.01, 100.0) == 1.0
    assert dd_scale_factor(30, 60, 120.0, 100.0) == 1.0                                # Zirvenin üstünde
    assert dd_scale_factor(0, 60, 10.0, 100.0) == 1.0 and dd_scale_factor(30, 60, 10.0, 0.0) == 1.0
    on = replace(fcfg, futures_dd_scale_pct=25.0, futures_dd_scale_size_pct=50.0)
    assert futures_dd_factor(on, 74.0, 100.0) == pytest.approx(0.5) and futures_dd_factor(fcfg, 10.0, 100.0) == 1.0


# ====================================================================== backtest motoru
def bt_data(hours=10):
    return fb.FutData(BASE_MS, BASE_MS + hours * HOUR, [], {}, None, (np.zeros(0), np.zeros(0)), {}, None, "1m")


def bt_sig(symbol, close_ms, breadth=9):
    s = fb.FutSignal(close_ms, symbol, "long", 1.0, 100.0)
    s.breadth = breadth
    return s


LOSS = [(100, 100.1, 95.0, 95.5)]          # Zarar kes (%4): 10x'te teminatın %40'ı
WIN = [(100, 103.5, 99.9, 103.0)]          # Kâr Al (%3): 10x'te teminatın %30'u


def dd_params(**over):
    base = dict(tp_pct=3.0, sl_pct=4.0, be_enabled=False, dd_scale_pct=30.0, dd_scale_size_pct=60.0)
    base.update(over)
    return params(**base)


def test_single_slot_scales_after_drawdown_and_returns_to_full_size():
    sigs = [bt_sig("AAAUSDT", BASE_MS), bt_sig("BBBUSDT", BASE_MS + 10 * MIN), bt_sig("CCCUSDT", BASE_MS + 20 * MIN)]
    w = {("AAAUSDT", sigs[0].close_ms): window(LOSS, start=sigs[0].close_ms),
         ("BBBUSDT", sigs[1].close_ms): window(WIN, start=sigs[1].close_ms),
         ("CCCUSDT", sigs[2].close_ms): window(WIN, start=sigs[2].close_ms)}
    res = fb.simulate(dd_params(), bt_data(), sigs, w)
    a, b, c = res.trades
    assert a.margin == pytest.approx(100.0) and a.pnl == pytest.approx(-40.0) and not a.dd_scaled
    # Bakiye 60: zirvenin (100) %40 altında → teminat 60 × %60 = 36
    assert b.margin == pytest.approx(36.0) and b.dd_scaled and b.pnl == pytest.approx(10.8)
    # Bakiye 70.8: zirvenin %29.2 altında → yine tam boy
    assert c.margin == pytest.approx(70.8) and not c.dd_scaled
    off = fb.simulate(dd_params(dd_scale_pct=0.0), bt_data(), sigs, w)
    assert off.trades[1].margin == pytest.approx(60.0) and not any(t.dd_scaled for t in off.trades)
    # Sabit tutarlı C senaryosu: küçültme uygulanmaz
    fixed = fb.simulate(dd_params(), bt_data(), sigs, w, fixed_notional=1000.0)
    assert [t.notional for t in fixed.trades] == pytest.approx([1000.0] * 3) and not any(t.dd_scaled for t in fixed.trades)


def test_slots_scale_with_realized_peak():
    sigs = [bt_sig("AAAUSDT", BASE_MS), bt_sig("BBBUSDT", BASE_MS + 10 * MIN)]
    w = {("AAAUSDT", sigs[0].close_ms): window(LOSS, start=sigs[0].close_ms),
         ("BBBUSDT", sigs[1].close_ms): window(WIN, start=sigs[1].close_ms)}
    p = dd_params(max_positions=2, extra_breadth=8)
    res = fb.simulate(p, bt_data(), sigs, w)
    a, b = res.trades
    assert a.margin == pytest.approx(50.0) and a.pnl == pytest.approx(-20.0)          # Yuva payı: 100 × 1/2
    assert b.margin == pytest.approx(80.0 * 0.5) and b.dd_scaled is False             # %20 aşağıda: tam boy
    deep = fb.simulate(replace(p, dd_scale_pct=20.0, dd_scale_size_pct=50.0), bt_data(), sigs, w)
    assert deep.trades[1].margin == pytest.approx(80.0 * 0.5 * 0.5) and deep.trades[1].dd_scaled


def test_account_counts_as_ruined_when_even_the_scaled_order_is_too_small():
    sigs = [bt_sig("AAAUSDT", BASE_MS), bt_sig("BBBUSDT", BASE_MS + 10 * MIN)]
    w = {(s.symbol, s.close_ms): window(LOSS, start=s.close_ms) for s in sigs}
    # 1.2 USDT → zarar kesle 0.72: tam boy 7.2 USDT (açılabilir), %60 boy 4.32 USDT (5 USDT'lik en küçük emrin altında)
    off = fb.simulate(dd_params(start_capital=1.2, dd_scale_pct=0.0), bt_data(), sigs, w)
    assert len(off.trades) == 2 and off.ruined_ms == off.trades[1].exit_ms         # İkinci zararda (0.43 USDT) erir
    on = fb.simulate(dd_params(start_capital=1.2), bt_data(), sigs, w)
    assert len(on.trades) == 1 and on.ruined_ms == on.trades[0].exit_ms


def test_params_from_cfg_and_report_header_line(fcfg):
    p = fb.params_from_cfg(replace(fcfg, futures_dd_scale_pct=30.0, futures_dd_scale_size_pct=50.0))
    assert (p.dd_scale_pct, p.dd_scale_size_pct) == (30.0, 50.0)
    assert fb.params_from_cfg(fcfg).dd_scale_pct == 0.0
    from test_futures_minute_scan import bt_data as m_data, k1_of, minute_closes, scan_cfg
    k1 = {"SOLUSDT": k1_of(minute_closes())}
    on = scan_cfg(fcfg, futures_coins="SOL")
    data = m_data(k1)
    sigs, funnel = fb.find_signals(on, data)
    windows = {}
    for s in sigs:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * MIN)
        windows[(s.symbol, s.close_ms)] = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    dd_on = replace(on, futures_dd_scale_pct=30.0)
    ev = fb.evaluate(fb.params_from_cfg(dd_on), data, sigs, windows)
    text = fb.format_report(dd_on, data, ev, funnel)
    n, small = len(ev.chained.trades), sum(t.dd_scaled for t in ev.chained.trades)
    assert n and (f"📉 Düşüşte küçültme: bakiye zirvesinin %30 altındayken yeni pozisyonlar %60 boy (A ve B'de; C "
                  f"sabit tutarla). A'da {small} / {n} işlem küçük boyla açıldı") in text
    assert "Düşüşte küçültme" not in fb.format_report(on, data, fb.evaluate(fb.params_from_cfg(on), data, sigs, windows),
                                                       funnel)


# ====================================================================== canlı / test modu
def crash_closes():
    return {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}


def test_live_entry_margin_scales_only_in_drawdown(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_leverage=2, futures_dd_scale_pct=30.0)
    bot, _fake, notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(crash_closes()))
    assert bot.state.peak_capital == pytest.approx(30.0)                # Açılışta zirve = o anki sermaye
    bot.state.peak_capital = 50.0                                       # Sermaye 30: zirvenin %40 altında
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions["SOLUSDT"]
    assert pos.margin_usdt == pytest.approx(30 * 0.95 * 0.6, rel=0.02)
    assert "Düşüşte küçültme: sermaye zirvenin %40 altında, pozisyon %60 boy" in pos.signal_note
    assert any("Düşüşte küçültme" in m for m in notes.messages)        # Pozisyon mesajında
    # Düşüş eşiğin altında ya da ayar kapalı: tam boy
    for name, over, peak in (("az", {}, 40.0), ("kapalı", {"futures_dd_scale_pct": 0.0}, 50.0)):
        c = live_cfg(fcfg, futures_leverage=2, **({"futures_dd_scale_pct": 30.0} | over))
        b2, _f, _n, clock2 = make_fbot(c, tmp_path / name, fake=KlineCcxt(crash_closes()))
        b2.state.peak_capital = peak
        b2.scan_and_enter(clock2.t)
        p2 = b2.state.positions["SOLUSDT"]
        assert p2.margin_usdt == pytest.approx(30 * 0.95, rel=0.02) and "Düşüşte küçültme" not in p2.signal_note


def test_live_wave_mode_margin_formula(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_max_positions=2, futures_dd_scale_pct=30.0, futures_dd_scale_size_pct=50.0)
    bot, _fake, _notes, _clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(crash_closes()))
    sig = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0, "breadth": 9.0})
    assert bot._entry_margin(sig, 30.0) == pytest.approx(30 * 0.95 / 2)
    bot.state.peak_capital = 45.0                                       # %33 aşağıda
    assert bot._entry_margin(sig, 30.0) == pytest.approx(30 * 0.95 / 2 * 0.5)
    assert sig.metrics["dd_size_pct"] == 50.0 and sig.metrics["dd_pct"] == pytest.approx(33.3)


def test_peak_follows_closed_trades_added_capital_and_state_file(fcfg, tmp_path):
    bot, fake, _notes, clock = make_fbot(fcfg, tmp_path)
    assert bot.state.peak_capital == pytest.approx(30.0)
    bot.open_futures(signal("ETHUSDT", "short"), clock.t)
    fake.prices["ETHUSDT"] = 104.1                                     # Zarar kes
    bot.manage_positions(clock.t + 120)
    capital = bot.state.capital
    assert capital < 29.0 and bot.state.peak_capital == pytest.approx(30.0)          # Zarar zirveyi düşürmez
    bot.add_capital(10.0)                                               # Yatırılan para zirveye de eklenir
    assert bot.state.peak_capital == pytest.approx(40.0) and bot.state.capital == pytest.approx(capital + 10.0)
    assert StateStore(tmp_path / "s.json").load().peak_capital == pytest.approx(40.0)
    fake.prices["ETHUSDT"] = 100.0
    bot.open_futures(signal("ETHUSDT", "long"), clock.t + 600)
    fake.prices["ETHUSDT"] = 120.0                                      # Kâr Al: sermaye zirveyi geçer
    bot.manage_positions(clock.t + 720)
    assert bot.state.realized_pnl > 0 and bot.state.peak_capital == pytest.approx(bot.state.capital)
    # Eski durum dosyası (alan yok): ilk kayıtta o anki sermaye
    legacy = BotState.from_dict({"mode": "paper", "start_capital": 25.0, "realized_pnl": -5.0})
    assert legacy.peak_capital == 0.0 and legacy.note_peak() == pytest.approx(20.0)


# ====================================================================== Telegram
def test_risk_page_rows_status_and_env(fut, tmp_path):
    bot, notes = fut
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    data = buttons(keyboard)
    assert "📉 Düşüşte küçültme: kapalı (sermaye zirvesi 30" in text and "%0.0 aşağıda" in text
    assert "kv:risk:FUTURES_DD_SCALE_PCT:30" in data and not any("FUTURES_DD_SCALE_SIZE_PCT" in d for d in data)
    press(bot, "kv:risk:FUTURES_DD_SCALE_PCT:30")
    assert bot.cfg.futures_dd_scale_pct == 30.0 and "FUTURES_DD_SCALE_PCT=30" in env_text(bot)
    text, keyboard, _mid = notes.menus[-1]
    assert "kv:risk:FUTURES_DD_SCALE_SIZE_PCT:50" in buttons(keyboard)
    press(bot, "kv:risk:FUTURES_DD_SCALE_SIZE_PCT:50")
    assert bot.cfg.futures_dd_scale_size_pct == 50.0 and "FUTURES_DD_SCALE_SIZE_PCT=50" in env_text(bot)
    text = notes.menus[-1][0]
    assert "zirveden %30 düşünce pozisyon %50 boy" in text and "sıradaki pozisyon tam boy" in text
    bot.state.peak_capital = 50.0
    press(bot, "risk")
    assert "şu an %40.0 aşağıda → sıradaki pozisyon <b>%50 boy</b>" in notes.menus[-1][0]


def test_risk_page_has_no_dd_rows_for_spot(spot):
    bot, notes = spot
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    assert "Düşüşte küçültme" not in text and not any("FUTURES_DD" in d for d in buttons(keyboard))
