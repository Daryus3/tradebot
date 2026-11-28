"""Sermaye koruması (FUTURES_GUARD_PCT / FUTURES_GUARD_SIZE_PCT; rapor 26 araştırması, 26 Kasım): sermaye bota verilen
sermayenin (ilk sermaye + /sermayeekle) eşik kadar altındayken (toplam zarar) yeni pozisyonlar küçük boyla açılır;
kârdaki hesabı etkilemez. Ayar ve ortak kural, backtest motoru (tek pozisyon / dalga modu, sabit tutar), rapor satırları,
canlı / test modunda teminat, /sermayeekle ve Telegram'daki 🧯 Risk sigortaları satırları. Sahte borsalarla; gerçek emir
yok."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, futures_guard_factor, load_config
from runtime_settings import EDITABLE
from strategy import FUT_BAR_MS, FuturesSignal
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, live_cfg, press
from test_futures_day_brake import LOSS, WIN, bt_data, bt_sig, crash_closes
from test_settings_menu import buttons, env_text, fut, spot  # noqa: F401 - pytest fikstürü

MIN = fb.MIN_MS
HOUR = 60 * MIN


# ====================================================================== ayar ve ortak kural
def test_settings_defaults_validation_env_notes_and_rule(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_guard_pct, fcfg.futures_guard_size_pct) == (0.0, 60.0)          # Varsayılan: kapalı
    for bad in (dict(futures_guard_pct=-1.0), dict(futures_guard_pct=95.0),
                dict(futures_guard_size_pct=5.0), dict(futures_guard_size_pct=120.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    replace(fcfg, futures_guard_pct=30.0, futures_guard_size_pct=50.0).validate()
    monkeypatch.setenv("FUTURES_GUARD_PCT", "30")
    monkeypatch.setenv("FUTURES_GUARD_SIZE_PCT", "70")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_guard_pct, loaded.futures_guard_size_pct) == (30.0, 70.0)
    assert EDITABLE["FUTURES_GUARD_PCT"].attr == "futures_guard_pct"
    assert EDITABLE["FUTURES_GUARD_SIZE_PCT"].attr == "futures_guard_size_pct"
    assert not _setting_notes(replace(fcfg, futures_guard_pct=30.0))  # Açılış notu yok (rapor 34: bilgilendirme notları kaldırıldı)
    assert not any("FUTURES_GUARD_PCT" in n for n in _setting_notes(fcfg))
    on = replace(fcfg, futures_guard_pct=30.0, futures_guard_size_pct=60.0)
    assert futures_guard_factor(on, 70.0, 100.0) == pytest.approx(0.6)                # Tam %30 zararda
    assert futures_guard_factor(on, 70.01, 100.0) == 1.0
    assert futures_guard_factor(on, 500.0, 1000.0) == pytest.approx(0.6)
    assert futures_guard_factor(fcfg, 10.0, 100.0) == 1.0                             # Kapalı


# ====================================================================== backtest motoru
def guard_params(**over):
    base = dict(tp_pct=3.0, sl_pct=4.0, be_enabled=False, guard_pct=30.0, guard_size_pct=60.0)
    base.update(over)
    return params(**base)


def test_single_slot_guard_by_start_capital_not_peak():
    # Zarar kes ×2 (bakiye 100 → 60 → 36), sonra kazançlar
    sigs = [bt_sig(f"C{i}USDT", BASE_MS + i * 10 * MIN) for i in range(4)]
    rows = [LOSS, WIN, WIN, WIN]
    w = {(s.symbol, s.close_ms): window(rows[i], start=s.close_ms) for i, s in enumerate(sigs)}
    res = fb.simulate(guard_params(), bt_data(), sigs, w)
    a, b, c, d = res.trades
    assert a.margin == pytest.approx(100.0) and not a.guarded and a.pnl == pytest.approx(-40.0)
    assert b.margin == pytest.approx(60.0 * 0.6) and b.guarded                         # Toplam zarar %40 ≥ %30
    after_b = 60.0 + b.pnl
    assert c.margin == pytest.approx(after_b * (0.6 if after_b < 70.0 else 1.0))
    off = fb.simulate(guard_params(guard_pct=0.0), bt_data(), sigs, w)
    assert off.trades[1].margin == pytest.approx(60.0) and not any(t.guarded for t in off.trades)
    # Kârdaki hesap: zirveden %40 düşse de başlangıç sermayesinin üstünde → tam boy (düşüşte küçültmeden farkı)
    rich = [bt_sig("W1USDT", BASE_MS), bt_sig("W2USDT", BASE_MS + 10 * MIN), bt_sig("L1USDT", BASE_MS + 20 * MIN),
            bt_sig("X1USDT", BASE_MS + 30 * MIN)]
    rw = {(s.symbol, s.close_ms): window(r, start=s.close_ms) for s, r in zip(rich, [WIN, WIN, LOSS, WIN])}
    t = fb.simulate(guard_params(), bt_data(), rich, rw).trades
    assert t[2].pnl < 0 and not t[3].guarded and t[3].margin == pytest.approx(t[2].equity_after)
    # Sabit tutarlı C senaryosu: uygulanmaz
    fixed = fb.simulate(guard_params(), bt_data(), sigs, w, fixed_notional=1000.0)
    assert [x.notional for x in fixed.trades] == pytest.approx([1000.0] * 4) and not any(x.guarded for x in fixed.trades)


def test_slots_guard_multiplies_the_share_and_stacks_with_the_brake():
    sigs = [bt_sig("AAAUSDT", BASE_MS), bt_sig("BBBUSDT", BASE_MS + 10 * MIN)]
    w = {("AAAUSDT", sigs[0].close_ms): window(LOSS, start=sigs[0].close_ms),
         ("BBBUSDT", sigs[1].close_ms): window(WIN, start=sigs[1].close_ms)}
    p = guard_params(max_positions=2, extra_breadth=8, guard_pct=15.0)
    a, b = fb.simulate(p, bt_data(), sigs, w).trades
    assert a.margin == pytest.approx(50.0) and a.pnl == pytest.approx(-20.0)           # Yuva payı: 100 × 1/2
    assert b.margin == pytest.approx(80.0 * 0.5 * 0.6) and b.guarded                   # %20 zarar ≥ %15
    both = fb.simulate(replace(p, day_brake_pct=10.0, day_brake_size_pct=50.0), bt_data(), sigs, w).trades[1]
    assert both.margin == pytest.approx(80.0 * 0.5 * 0.6 * 0.5) and both.guarded and both.braked
    light = fb.simulate(replace(p, guard_pct=25.0), bt_data(), sigs, w).trades[1]
    assert light.margin == pytest.approx(40.0) and not light.guarded                   # Eşiğin altında: tam boy


def test_params_report_header_variant_and_leverage_rows(fcfg):
    p = fb.params_from_cfg(replace(fcfg, futures_guard_pct=30.0, futures_guard_size_pct=50.0))
    assert (p.guard_pct, p.guard_size_pct) == (30.0, 50.0)
    assert fb.params_from_cfg(fcfg).guard_pct == 0.0
    rows = [n for n, _kw in fb.guard_variant_rows(fb.params_from_cfg(fcfg), lambda **kw: kw)]
    assert rows == ["Sermaye koruması: toplam zarar %30 iken %60 boy"]
    got = dict(fb.guard_variant_rows(fb.params_from_cfg(replace(fcfg, futures_guard_pct=30.0)), lambda **kw: kw))
    assert got["Sermaye koruması kapalı"] == {"guard_pct": 0.0}
    assert "Sermaye koruması eşiği %20 (şimdi %30)" in got and "Sermaye koruması boyu %50 (şimdi %60)" in got
    assert "Sermaye koruması eşiği %30 (şimdi %30)" not in got
    from test_futures_minute_scan import bt_data as m_data, k1_of, minute_closes, scan_cfg
    k1 = {"SOLUSDT": k1_of(minute_closes())}
    scan = scan_cfg(fcfg, futures_coins="SOL")
    data = m_data(k1)
    sigs, funnel = fb.find_signals(scan, data)
    windows = {}
    for s in sigs:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * MIN)
        windows[(s.symbol, s.close_ms)] = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    guard_on = replace(scan, futures_guard_pct=30.0)
    ev = fb.evaluate(fb.params_from_cfg(guard_on), data, sigs, windows)
    text = fb.format_report(guard_on, data, ev, funnel)
    n, guarded = len(ev.chained.trades), sum(t.guarded for t in ev.chained.trades)
    assert n and (f"🛡️ Sermaye koruması: bakiye başlangıç sermayesinin %30 altındayken (toplam zarar) yeni pozisyonlar "
                  f"%60 boy (A ve B'de; C sabit tutarla). A'da {guarded} / {n} işlem bu boyla açıldı") in text
    assert "Sermaye koruması" not in fb.format_report(scan, data, fb.evaluate(fb.params_from_cfg(scan), data, sigs,
                                                                                windows), funnel)
    wave = replace(scan, futures_max_positions=4, futures_leverage=3)
    off = fb.guard_lev_rows(wave, data, sigs, windows)
    assert [r.label for r in off] == ["3x + koruma"] and off[0].ev is not None
    on = fb.guard_lev_rows(replace(wave, futures_guard_pct=30.0), data, sigs, windows)
    assert [r.label for r in on] == ["3x, koruma yok"]
    assert fb.guard_lev_rows(replace(wave, futures_max_positions=1), data, sigs, windows) == []


# ====================================================================== canlı / test modu
def test_live_entry_margin_guard_by_given_capital(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_leverage=2, futures_guard_pct=30.0)
    bot, _fake, notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(crash_closes()))
    bot.state.realized_pnl = -12.0                                    # 30 USDT verildi → sermaye 18 (toplam zarar %40)
    bot.state.paper_balances["USDT"] = 18.0
    bot.exchange.balances["USDT"] = 18.0
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions["SOLUSDT"]
    assert pos.margin_usdt == pytest.approx(18 * 0.95 * 0.6, rel=0.02)
    assert "Sermaye koruması: bota verilen sermayeye göre toplam zarar %40, pozisyon %60 boy" in pos.signal_note
    assert any("Sermaye koruması" in m for m in notes.messages)        # Pozisyon mesajında
    # Zarar eşiğin altında ya da ayar kapalı: tam boy
    for name, over, pnl in (("az", {}, -6.0), ("kapalı", {"futures_guard_pct": 0.0}, -12.0)):
        c = live_cfg(fcfg, futures_leverage=2, **({"futures_guard_pct": 30.0} | over))
        b2, _f, _n, clock2 = make_fbot(c, tmp_path / name, fake=KlineCcxt(crash_closes()))
        b2.state.realized_pnl = pnl
        b2.state.paper_balances["USDT"] = 30.0 + pnl
        b2.exchange.balances["USDT"] = 30.0 + pnl
        b2.scan_and_enter(clock2.t)
        p2 = b2.state.positions["SOLUSDT"]
        assert p2.margin_usdt == pytest.approx((30 + pnl) * 0.95, rel=0.02) and "Sermaye koruması" not in p2.signal_note


def test_live_wave_mode_formula_and_added_capital(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_max_positions=2, futures_guard_pct=30.0, futures_guard_size_pct=50.0)
    bot, _fake, _notes, _clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(crash_closes()))
    sig = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0, "breadth": 9.0})
    assert bot._entry_margin(sig, 30.0) == pytest.approx(30 * 0.95 / 2)
    bot.state.realized_pnl = -10.0                                    # Toplam zarar %33
    assert bot._entry_margin(sig, 20.0) == pytest.approx(20 * 0.95 / 2 * 0.5)
    assert sig.metrics["guard_size_pct"] == 50.0 and sig.metrics["guard_loss_pct"] == pytest.approx(33.3)
    bot.add_capital(30.0)                                             # Verilen 60, sermaye 50: zarar %16.7 → tam boy
    fresh = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0, "breadth": 9.0})
    assert bot._guard_factor(fresh) == 1.0 and "guard_size_pct" not in fresh.metrics


# ====================================================================== Telegram
def test_risk_page_rows_status_and_env(fut):
    bot, notes = fut
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    data = buttons(keyboard)
    assert "🛡️ Sermaye koruması: kapalı (bota verilen sermaye 30" in text and "toplam zarar %0.0" in text
    assert "kv:risk:FUTURES_GUARD_PCT:30" in data and not any("FUTURES_GUARD_SIZE_PCT" in d for d in data)
    press(bot, "kv:risk:FUTURES_GUARD_PCT:30")
    assert bot.cfg.futures_guard_pct == 30.0 and "FUTURES_GUARD_PCT=30" in env_text(bot)
    text, keyboard, _mid = notes.menus[-1]
    assert "kv:risk:FUTURES_GUARD_SIZE_PCT:50" in buttons(keyboard)
    press(bot, "kv:risk:FUTURES_GUARD_SIZE_PCT:50")
    assert bot.cfg.futures_guard_size_pct == 50.0 and "FUTURES_GUARD_SIZE_PCT=50" in env_text(bot)
    text = notes.menus[-1][0]
    assert "toplam zarar %30 olunca pozisyon %50 boy" in text and "sıradaki pozisyon tam boy" in text
    bot.state.realized_pnl = -12.0
    press(bot, "risk")
    assert "toplam zarar %40.0 → sıradaki pozisyon <b>%50 boy</b>" in notes.menus[-1][0]


def test_risk_page_has_no_guard_rows_for_spot(spot):
    bot, notes = spot
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    assert "Sermaye koruması" not in text and not any("FUTURES_GUARD" in d for d in buttons(keyboard))


# ====================================================================== kâr yastığı (FUTURES_CUSHION_X)
def test_cushion_settings_rule_and_engines(fcfg, monkeypatch, tmp_path):
    from config import cushion_factor, futures_cushion_factor
    assert (fcfg.futures_cushion_x, fcfg.futures_cushion_size_pct) == (0.0, 112.5)        # Varsayılan: kapalı
    for bad in (dict(futures_cushion_x=1.0), dict(futures_cushion_x=30.0), dict(futures_cushion_size_pct=90.0),
                dict(futures_cushion_size_pct=160.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    monkeypatch.setenv("FUTURES_CUSHION_X", "2")
    monkeypatch.setenv("FUTURES_CUSHION_SIZE_PCT", "125")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_cushion_x, loaded.futures_cushion_size_pct) == (2.0, 125.0)
    assert EDITABLE["FUTURES_CUSHION_X"].attr == "futures_cushion_x"
    assert not _setting_notes(replace(fcfg, futures_cushion_x=2.0))  # Açılış notu yok (rapor 34: bilgilendirme notları kaldırıldı)
    assert cushion_factor(2.0, 112.5, 200.0, 100.0) == pytest.approx(1.125)
    assert cushion_factor(2.0, 112.5, 199.9, 100.0) == 1.0 and cushion_factor(0.0, 112.5, 900.0, 100.0) == 1.0
    assert futures_cushion_factor(replace(fcfg, futures_cushion_x=1.5), 150.0, 100.0) == pytest.approx(1.125)
    # Dalga modu: büyütme boştaki teminatla sınırlı; tek pozisyonda kullanım payını aşmaz
    sigs = [bt_sig("AAAUSDT", BASE_MS), bt_sig("BBBUSDT", BASE_MS + 10 * MIN)]
    w = {("AAAUSDT", sigs[0].close_ms): window(WIN, start=sigs[0].close_ms),
         ("BBBUSDT", sigs[1].close_ms): window(WIN, start=sigs[1].close_ms)}
    p = guard_params(max_positions=2, extra_breadth=8, guard_pct=0.0, cushion_x=1.1, cushion_size_pct=125.0)
    a, b = fb.simulate(p, bt_data(), sigs, w).trades
    assert a.margin == pytest.approx(50.0) and not a.cushioned                          # Bakiye 100: yastık yok
    assert b.margin == pytest.approx(115.0 * 0.5 * 1.25) and b.cushioned                # 115 ≥ 1.1 × 100
    single = fb.simulate(replace(p, max_positions=1), bt_data(), sigs, w).trades
    assert single[1].margin == pytest.approx(130.0) and single[1].cushioned             # Tek pozisyon: en çok bakiye
    assert fb.params_from_cfg(replace(fcfg, futures_cushion_x=2.0)).cushion_x == 2.0
    rows = dict(fb.cushion_variant_rows(fb.params_from_cfg(replace(fcfg, futures_max_positions=4)), lambda **kw: kw))
    assert rows == {"Kâr yastığı: başlangıcın 2 katında %112.5 boy": {"cushion_x": 2.0, "cushion_size_pct": 112.5}}
    on = fb.params_from_cfg(replace(fcfg, futures_max_positions=4, futures_cushion_x=2.0))
    got = dict(fb.cushion_variant_rows(on, lambda **kw: kw))
    assert got["Kâr yastığı kapalı"] == {"cushion_x": 0.0} and "Kâr yastığı 3 katında (şimdi 2)" in got
    assert "Kâr yastığı boyu %125 (şimdi %112.5)" in got
    assert fb.cushion_variant_rows(fb.params_from_cfg(fcfg), lambda **kw: kw) == []        # Tek pozisyon


def test_cushion_live_margin_message_and_page(fcfg, tmp_path, fut):
    cfg = live_cfg(fcfg, futures_max_positions=2, futures_cushion_x=2.0)
    bot, _fake, _notes, _clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(crash_closes()))
    sig = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0, "breadth": 9.0})
    assert bot._entry_margin(sig, 30.0) == pytest.approx(30 * 0.95 / 2)
    bot.state.realized_pnl = 30.0                                     # Sermaye 60 = verilenin 2 katı
    assert bot._entry_margin(sig, 60.0) == pytest.approx(60 * 0.95 / 2 * 1.125)
    assert sig.metrics["cushion_size_pct"] == 112.5 and sig.metrics["cushion_x"] == 2.0
    assert "Kâr yastığı: sermaye bota verilenin 2 katı, pozisyon %112.5 boy" in sig.note()
    single = live_cfg(fcfg, futures_cushion_x=2.0)
    b1, _f, _n, _c = make_fbot(single, tmp_path / "tek", fake=KlineCcxt(crash_closes()))
    b1.state.realized_pnl = 30.0
    assert b1._entry_margin(sig, 60.0) == pytest.approx(60 * 0.95)    # Tek pozisyon: kullanım payını aşmaz
    tbot, notes = fut
    press(tbot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    assert "🛏️ Kâr yastığı: kapalı (sermaye verilenin 1.00 katı)" in text
    assert "kv:risk:FUTURES_CUSHION_X:2" in buttons(keyboard)
    press(tbot, "kv:risk:FUTURES_CUSHION_X:2")
    assert tbot.cfg.futures_cushion_x == 2.0 and "FUTURES_CUSHION_X=2" in env_text(tbot)
    tbot.state.realized_pnl = tbot.state.start_capital * 1.5
    press(tbot, "risk")
    assert "şu an 2.50 katı → sıradaki pozisyon <b>%112.5 boy</b>" in notes.menus[-1][0]
