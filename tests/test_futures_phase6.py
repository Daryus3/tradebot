"""Faz 6: dalgada yeniden giriş (zarar sonrası bekleme dalgada atlanır), dalga eşikleri (genişlik yüksekken düşük çöküş
eşikleri), küçük ek pozisyonlar (ilk pozisyon #2 boyunda). Ayarlar, strateji, backtest motoru, canlı / test modu ve
Telegram setleri; Faz 5 seti ve varsayılanlarda eski davranış aynen."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import (FUTURES_CASCADE_PRESET, FUTURES_PHASE6_PRESET, FUTURES_PHASE6_SINGLE_PRESET, ConfigError,
                    _setting_notes, futures_breadth_on, futures_crash_rules, futures_slot_share, futures_wave_cfg,
                    futures_wave_rules, load_config)
from risk import ExitReason
from runtime_settings import EDITABLE, format_value
from strategy import FUT_BAR_MS, FuturesScalpStrategy, FuturesSignal, futures_kline_limit
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, bt_data, crashing, flat, live_cfg, press, series, tp_window

MIN = 60_000


# ====================================================================== ayarlar ve ortak kurallar
def test_phase6_settings_defaults_validation_env_notes(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_extra_size_pct, fcfg.futures_reentry_breadth) == (100.0, 0)
    assert (fcfg.futures_wave_rules, fcfg.futures_wave_breadth) == ("", 12)
    assert not futures_breadth_on(fcfg) and futures_wave_cfg(fcfg) is None
    for bad in (dict(futures_extra_size_pct=2.0), dict(futures_extra_size_pct=150.0), dict(futures_reentry_breadth=-1),
                dict(futures_wave_breadth=0), dict(futures_wave_rules="abc")):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    for key, value in (("FUTURES_EXTRA_SIZE_PCT", "16.7"), ("FUTURES_REENTRY_BREADTH", "3"),
                       ("FUTURES_WAVE_RULES", "1h:%5 ; 4:8"), ("FUTURES_WAVE_BREADTH", "15")):
        monkeypatch.setenv(key, value)
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_extra_size_pct, loaded.futures_reentry_breadth) == (16.7, 3)
    assert (loaded.futures_wave_rules, loaded.futures_wave_breadth) == ("1:5,4:8", 15)
    p6 = replace(fcfg, futures_max_positions=4, futures_leverage=3, futures_extra_size_pct=16.7,
                 futures_reentry_breadth=3, futures_wave_rules="1:5,4:8", futures_isolated_size_pct=50.0)
    p6.validate()
    notes = " ".join(_setting_notes(p6))
    assert "Faz 6 büyüklükleri" in notes and "1.90 katı" in notes and "dalgada yeniden giriş" in notes
    assert "FUTURES_WAVE_RULES=1:5,4:8" in notes and "3 ve üstü" not in notes       # Eşit büyüklük uyarısı değil
    assert "etkisiz" in " ".join(_setting_notes(replace(p6, loss_cooldown_minutes=0)))
    assert "%75" in " ".join(_setting_notes(replace(p6, futures_max_positions=5, futures_extra_size_pct=50.0)))
    assert "#2'den küçük" in " ".join(_setting_notes(replace(p6, futures_leverage=2)))
    assert not any("Faz 6" in n for n in _setting_notes(fcfg))
    # Faz 5 seti (eşit büyüklük) notu aynen
    f5 = replace(fcfg, futures_max_positions=2, futures_leverage=3)
    assert any("2 pozisyon, 3x" in n and "Düzeltilmiş testte" in n for n in _setting_notes(f5))


def test_slot_share_wave_cfg_and_kline_limit(fcfg):
    p6 = replace(fcfg, futures_max_positions=4, futures_extra_size_pct=16.7)
    first, extra = futures_slot_share(p6, 0), futures_slot_share(p6, 2)
    assert first + 3 * extra == pytest.approx(1.0) and first / extra == pytest.approx(100 / 16.7)
    assert first * 0.95 * 3 == pytest.approx(1.9, abs=0.002)            # 3x'te ilk pozisyon #2 boyunda
    f5 = replace(fcfg, futures_max_positions=2)
    assert futures_slot_share(f5, 0) == futures_slot_share(f5, 1) == 0.5   # Faz 5: 1/N, eskisi gibi
    assert futures_slot_share(fcfg, 0) == 1.0
    w = replace(fcfg, futures_crash_extra="1:6", futures_wave_rules="1:5,4:8")
    wc = futures_wave_cfg(w)
    assert futures_crash_rules(wc) == ((1, 5.0), (4, 8.0)) and wc.futures_crash_drop_pct == 8.0
    assert futures_crash_rules(w) == ((1, 6.0), (4, 10.0)) and futures_breadth_on(w)
    assert futures_wave_rules(replace(w, futures_crash_z=3.0)) == ()   # Oynaklık eşiğinde kullanılmaz
    assert futures_kline_limit(replace(fcfg, futures_wave_rules="24:20")) >= 24 * 12


def test_evaluate_wave_rules_only_in_wave(fcfg):
    c = replace(fcfg, futures_crash_extra="1:6", futures_wave_rules="1:5,4:8", futures_wave_breadth=3)
    klines = {"SOLUSDT": series(crashing(drop=5.5)), "ADAUSDT": series(crashing(drop=7.0)),
              "XRPUSDT": series(crashing(drop=4.0)), "DOGEUSDT": series(flat())}
    now = int(klines["SOLUSDT"]["t"][-1]) + FUT_BAR_MS + 3000
    sigs, _r = FuturesScalpStrategy(None, c).evaluate(klines, None, now)
    by = {s.symbol: s for s in sigs}
    assert set(by) == {"SOLUSDT", "ADAUSDT"}                            # SOL: %5.5 (dalga eşiği %5), ADA: %7 (ana)
    assert by["SOLUSDT"].metrics.get("wave_rule") == 1.0 and "wave_rule" not in by["ADAUSDT"].metrics
    assert "Dalga eşiği (Faz 6)" in by["SOLUSDT"].note() and by["ADAUSDT"].score > by["SOLUSDT"].score
    sigs, _r = FuturesScalpStrategy(None, c).evaluate(klines, None, now, breadth=2)   # Dalga yok
    assert [s.symbol for s in sigs] == ["ADAUSDT"]
    plain = replace(c, futures_wave_rules="")
    sigs, _r = FuturesScalpStrategy(None, plain).evaluate(klines, None, now)
    assert [s.symbol for s in sigs] == ["ADAUSDT"]


# ====================================================================== backtest motoru
def bt_sig(symbol, close_ms, breadth, score=1.0):
    s = fb.FutSignal(close_ms, symbol, "long", score, 100.0)
    s.breadth = breadth
    return s


def sl_window(start, bars=20):
    """3 dk yatay, sonra zarar kese (-%0.9) inen 1 dk mumlar."""
    rows = [(100, 100.05, 99.95, 100.0)] * 3 + [(100, 100.0, 98.8, 98.9)] + [(98.9, 99.0, 98.8, 98.9)] * bars
    return window(rows, start=start)


def test_reentry_in_wave_and_loss_window_persists():
    data = bt_data()
    a = bt_sig("AAAUSDT", BASE_MS, 0)                                   # Zararla kapanır
    b = bt_sig("AAAUSDT", BASE_MS + 30 * MIN, 5)                        # Dalgada: yeniden giriş (kârla)
    c = bt_sig("AAAUSDT", BASE_MS + 90 * MIN, 0)                        # Dalga yok: önceki zararın beklemesi sürüyor
    d = bt_sig("AAAUSDT", BASE_MS + 300 * MIN, 0)                       # Bekleme bitti
    w = {("AAAUSDT", a.close_ms): sl_window(a.close_ms)}
    for s in (b, c, d):
        w[("AAAUSDT", s.close_ms)] = tp_window(s.close_ms)
    p = params(start_capital=100.0, be_enabled=False, hold_min=30, loss_cooldown_min=240)
    old = fb.simulate(p, data, [a, b, c, d], w)                         # Kapalı: eskisi gibi
    assert [t.entry_ms for t in old.trades] == [a.close_ms, d.close_ms]
    assert old.skipped["coin beklemede (önceki işlemden sonra)"] == 2
    on = fb.simulate(replace(p, reentry_breadth=3), data, [a, b, c, d], w)
    assert [t.entry_ms for t in on.trades] == [a.close_ms, b.close_ms, d.close_ms]
    assert [t.reentry for t in on.trades] == [False, True, False] and on.trades[1].pnl > 0
    assert on.skipped["coin beklemede (zarar sonrası; dalga yok, genişlik < 3)"] == 1
    # Çoklu pozisyonda aynı kural
    slots = fb.simulate(replace(p, reentry_breadth=3, max_positions=2), data, [a, b, c, d], w)
    assert [t.entry_ms for t in slots.trades] == [a.close_ms, b.close_ms, d.close_ms]
    # Bekleme kapalıyken (LOSS_COOLDOWN 0) ayarın etkisi yok
    zero = fb.simulate(replace(p, reentry_breadth=3, loss_cooldown_min=0), data, [a, b, c, d], w)
    assert len(zero.trades) == 4 and not any(t.reentry for t in zero.trades)


def test_slot_weights_and_equal_sizes_unchanged():
    data = bt_data()
    a, b, c = (bt_sig(s, BASE_MS + i * 5 * MIN, 9) for i, s in enumerate(("AAAUSDT", "BBBUSDT", "CCCUSDT")))
    w = {(s.symbol, s.close_ms): tp_window(s.close_ms) for s in (a, b, c)}
    p = params(start_capital=100.0, be_enabled=False, hold_min=60, max_positions=3, extra_breadth=8)
    eq = fb.simulate(p, data, [a, b, c], w)
    assert [t.margin for t in eq.trades] == pytest.approx([100 / 3] * 3)    # Faz 5: 1/N
    small = fb.simulate(replace(p, extra_size_pct=25.0), data, [a, b, c], w)
    assert [t.margin for t in small.trades] == pytest.approx([100 / 1.5, 25 / 1.5, 25 / 1.5])
    fx = fb.simulate(replace(p, extra_size_pct=25.0), data, [a, b, c], w, fixed_notional=300.0 / 3)
    assert [t.notional for t in fx.trades] == pytest.approx([200.0, 50.0, 50.0])
    assert fb.slot_share(p, 0) == fb.slot_share(p, 2) == pytest.approx(1 / 3)
    assert fb.slot_share(params(), 0) == 1.0


def test_find_signals_adds_wave_rule_signals_only_in_wave(fcfg):
    c = replace(fcfg, futures_crash_extra="1:6", futures_wave_rules="1:5,4:8", futures_wave_breadth=3)
    coins = {"SOLUSDT": series(crashing(drop=5.5)), "ADAUSDT": series(crashing(drop=7.0)),
             "XRPUSDT": series(crashing(drop=4.0)), "DOGEUSDT": series(flat())}
    for k in coins.values():                                            # Sinyal mumunun ardından bir mum (giriş)
        for key in k:
            k[key] = np.concatenate((k[key], k[key][-1:] + (FUT_BAR_MS if key == "t" else 0)))
    t0, end = int(BASE_MS), int(coins["SOLUSDT"]["t"][-1]) + FUT_BAR_MS
    close = int(coins["SOLUSDT"]["t"][-2]) + FUT_BAR_MS

    funnels = []

    def run(cfg):
        data = fb.FutData(t0, end, [fb.Segment(t0 - 1, end, ("SOL", "ADA", "XRP", "DOGE"))], dict(coins), ((), (), ()),
                          (np.zeros(0), np.zeros(0)), {}, None, "5m")
        sigs, funnel = fb.find_signals(cfg, data)
        funnels.append(funnel)
        return [s for s in sigs if s.close_ms == close]
    got = {s.symbol: s for s in run(c)}
    assert funnels[-1].wave >= 1                                         # Hunide ayrı sayılır
    assert set(got) == {"SOLUSDT", "ADAUSDT"} and got["SOLUSDT"].metrics["wave_rule"] == 1.0
    assert got["SOLUSDT"].breadth == 3 and "wave_rule" not in got["ADAUSDT"].metrics
    assert {s.symbol for s in run(replace(c, futures_wave_breadth=4))} == {"ADAUSDT"}
    assert {s.symbol for s in run(replace(c, futures_wave_rules=""))} == {"ADAUSDT"}


def test_phase6_rows_and_phase5_rows_without_phase6(fcfg):
    p6 = replace(fcfg, futures_max_positions=4, futures_leverage=3, futures_extra_size_pct=16.7,
                 futures_reentry_breadth=3, futures_wave_rules="1:5,4:8", futures_isolated_size_pct=50.0)
    sigs = [bt_sig("AAAUSDT", BASE_MS, 9), bt_sig("BBBUSDT", BASE_MS, 12)]
    sigs[1].metrics["wave_rule"] = 1.0
    calls = []

    def ev(s=None, **over):
        calls.append((None if s is None else [x.symbol for x in s], over))
        return over
    rows = fb.cascade_rows(p6, ev, sigs)                                # Faz 6 açıkken: Faz 6'sız #2
    assert rows[0][0] == "Tek pozisyon (dalga modu ve Faz 6 kapalı, 2x)"
    assert calls[0] == (["AAAUSDT"], dict(extra_size_pct=100.0, reentry_breadth=0, max_positions=1, leverage=2,
                                          isolated_size_pct=100.0, wave_hold_min=0))          # Faz 9 da kapalı
    labels = [r[0] for r in fb.phase6_rows(p6, ev, sigs, phase6=lambda **o: o)]
    assert labels[:3] == ["Dalgada yeniden giriş kapalı (zarar sonrası bekleme hep)", "Dalga eşikleri kapalı",
                          "Ek pozisyonlar eşit büyüklükte (Faz 5 gibi)"]
    assert not any("⭐ Faz 6 seti" in x for x in labels)                  # Zaten Faz 6 seti
    plain_rows = fb.phase6_rows(fcfg, ev, sigs, phase6=lambda **o: o)
    assert [r[0] for r in plain_rows][0].startswith("Dalgada yeniden giriş (≥3")
    assert plain_rows[1][1] == fb.PHASE6_SIM and plain_rows[2][1] == fb.PHASE6_SINGLE_SIM
    p = fb.params_from_cfg(p6)
    label = fb.cascade_label(p)
    assert "ilk pozisyon bakiyenin %63.3" in label and "dalgada yeniden giriş" in label
    trades = [fb.FutTrade("A", "long", 0, 1, 1, 1, "TP", 2, 1, 2, 0, 0, 0.01, 1, 0, 0, 0, False, False, MIN,
                          breadth=12, slot=s, reentry=r, wave_rule=wr) for s, r, wr in ((1, True, False), (2, False, True))]
    text = fb.breadth_breakdown(trades, p)
    assert "dalgada yeniden giriş: 1 işlem" in text and "dalga eşiği sinyali: 1 işlem" in text


# ====================================================================== canlı / test modu
def wave_closes():
    return {"SOLUSDT": crashing(), "ADAUSDT": crashing(drop=7.0), "XRPUSDT": crashing(drop=6.5),
            "DOGEUSDT": flat(), "BTCUSDT": flat()}


def stop_out(bot, fake, clock, symbol="SOLUSDT"):
    """Pozisyonu açıp zarar kesle kapatır (zarar sonrası bekleme başlar)."""
    pos = bot.open_futures(FuturesSignal(symbol, "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0}), clock.t)
    fake.prices[symbol] = pos.sl_price * 0.99
    bot.manage_positions(clock.t + 20)
    assert symbol not in bot.state.positions and bot.state.realized_pnl < 0
    fake.prices[symbol] = float(fake.closes[symbol][-1])


def test_paper_reentry_in_wave_after_loss(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_reentry_breadth=3, futures_leverage=2)
    assert cfg.loss_cooldown_minutes == 240 and cfg.futures_max_positions == 1
    bot, fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(wave_closes()))
    stop_out(bot, fake, clock)
    assert "SOLUSDT" in bot.state.cooldowns and bot.state.wave_reentry["SOLUSDT"][1] == bot.state.cooldowns["SOLUSDT"]
    bot.scan_and_enter(clock.t + 30)                                    # 3 coin düşüyor: dalga, bekleme atlanır
    pos = bot.state.positions
    assert list(pos) == ["SOLUSDT"] and "Dalgada yeniden giriş" in pos["SOLUSDT"].signal_note
    # Kapalıyken (eskisi gibi): beklemedeki coine girilmez, sıradaki coin alınır
    off = live_cfg(fcfg, futures_leverage=2)
    bot2, fake2, _n2, clock2 = make_fbot(off, tmp_path / "b", fake=KlineCcxt(wave_closes()))
    stop_out(bot2, fake2, clock2)
    assert not bot2.state.wave_reentry
    bot2.scan_and_enter(clock2.t + 30)
    assert list(bot2.state.positions) == ["ADAUSDT"]


def test_paper_reentry_needs_wave_and_respects_blacklist(fcfg, tmp_path):
    closes = {"SOLUSDT": crashing(), "ADAUSDT": crashing(drop=2.0), "XRPUSDT": flat(), "DOGEUSDT": flat(),
              "BTCUSDT": flat()}
    cfg = live_cfg(fcfg, futures_reentry_breadth=3, futures_leverage=2)
    bot, fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(closes))
    stop_out(bot, fake, clock)
    bot.scan_and_enter(clock.t + 30)                                    # Yalnız 1 coin %3+ düşüyor: dalga yok
    assert not bot.state.positions and bot.last_breadth == 1
    bot3, fake3, _n3, clock3 = make_fbot(cfg, tmp_path / "c", fake=KlineCcxt(wave_closes()))
    stop_out(bot3, fake3, clock3)
    bot3.state.cooldowns["SOLUSDT"] += 24 * 3600                        # Kara liste (bekleme uzatıldı): atlanmaz
    bot3.scan_and_enter(clock3.t + 30)
    assert "SOLUSDT" not in bot3.state.positions and "ADAUSDT" in bot3.state.positions


def test_reentry_win_keeps_loss_window(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_reentry_breadth=3, futures_leverage=2)
    bot, fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(wave_closes()))
    stop_out(bot, fake, clock)
    until = bot.state.cooldowns["SOLUSDT"]
    bot.scan_and_enter(clock.t + 30)
    pos = bot.state.positions["SOLUSDT"]
    fake.prices["SOLUSDT"] = pos.entry_price * 1.03                     # Yeniden giriş kârla kapanır
    pnl_before = bot.state.realized_pnl
    bot.close_position(pos, ExitReason.MANUAL)
    assert "SOLUSDT" not in bot.state.positions and bot.state.realized_pnl > pnl_before
    assert bot.state.cooldowns["SOLUSDT"] == pytest.approx(until)       # Önceki zararın beklemesi sürer
    assert bot.state.wave_reentry["SOLUSDT"][1] == pytest.approx(until)


def test_paper_phase6_slot_sizes(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_max_positions=4, futures_extra_breadth=3, futures_extra_size_pct=16.7,
                   futures_leverage=3)
    bot, _fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(wave_closes()))
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions
    assert len(pos) == 3
    first = pos["SOLUSDT"]                                              # En güçlü sinyal: ilk pozisyon
    share = futures_slot_share(cfg, 0)
    assert first.margin_usdt == pytest.approx(30 * 0.95 * share, rel=0.02)
    extras = [p for s, p in pos.items() if s != "SOLUSDT"]
    assert all(p.margin_usdt == pytest.approx(30 * 0.95 * futures_slot_share(cfg, 1), rel=0.05) for p in extras)
    assert all("ek pozisyon" in p.signal_note for p in extras)


def test_scan_report_shows_phase6(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_reentry_breadth=3, futures_wave_rules="1:5,4:8", futures_wave_breadth=12)
    bot, _fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(wave_closes()))
    assert bot._cascade_on()
    bot.scan_and_enter(clock.t)
    report = bot.scan_report()
    assert "dalgada yeniden giriş ≥3" in report and "dalga eşikleri ≥12" in report


# ====================================================================== Telegram
def test_phase6_presets_and_phase5_stays_phase5(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(fcfg, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert {"fcm:on", "fcm:p6", "fcm:p6s"} <= set(data) and all(len(d.encode()) <= 64 for d in data)
    assert "kv:fw:FUTURES_REENTRY_BREADTH:3" in data and "Faz 6 testi" in text
    press(bot, "fcm:p6")
    pending = bot.commands.pending
    assert pending is not None and bot.cfg.futures_max_positions == 1          # Onaydan önce değişmez
    press(bot, f"yes:{pending.token}")
    c = bot.cfg
    assert (c.futures_max_positions, c.futures_leverage, c.futures_extra_size_pct) == (4, 3, 16.7)
    assert (c.futures_reentry_breadth, c.futures_wave_rules, c.futures_wave_breadth) == (3, "1:5,4:8", 12)
    assert c.loss_cooldown_minutes == 240 and "FUTURES_WAVE_RULES=1:5,4:8" in env.read_text()
    press(bot, "pg:fw")
    labels = [label for row in notes.menus[-1][1] for label, _d in row]
    assert any(label.startswith("✅ ⭐ Faz 6 seti") for label in labels)
    assert not any(label.startswith("✅ ⭐ Faz 5") for label in labels)
    assert any("Faz 6'yı" in label for label in labels)
    # Faz 5 seti: Faz 6 ekleri kapanır, Faz 5 tam olarak Faz 5
    press(bot, "fcm:on")
    press(bot, f"yes:{bot.commands.pending.token}")
    c = bot.cfg
    for key, value in FUTURES_CASCADE_PRESET.items():
        assert format_value(getattr(c, EDITABLE[key].attr)) == format_value(value)
    assert (c.futures_extra_size_pct, c.futures_reentry_breadth, c.futures_wave_rules) == (100.0, 0, "")
    press(bot, "pg:fw")
    assert any(label.startswith("✅ ⭐ Faz 5 seti") for row in notes.menus[-1][1] for label, _d in row)
    # Faz 6 tek pozisyon, sonra hepsini kapat
    press(bot, "fcm:p6s")
    press(bot, f"yes:{bot.commands.pending.token}")
    c = bot.cfg
    assert (c.futures_max_positions, c.futures_leverage, c.futures_reentry_breadth) == (1, 2, 3)
    assert c.futures_wave_rules == "1:5,4:8" and c.futures_isolated_size_pct == 50.0
    assert "FUTURES_REENTRY_BREADTH=3" in env.read_text()
    assert all(format_value(getattr(c, EDITABLE[k].attr)) == format_value(v) for k, v in FUTURES_PHASE6_SINGLE_PRESET.items())
    press(bot, "fcm:off")
    press(bot, f"yes:{bot.commands.pending.token}")
    c = bot.cfg
    assert (c.futures_max_positions, c.futures_isolated_size_pct, c.futures_reentry_breadth, c.futures_wave_rules) \
        == (1, 100.0, 0, "")
    assert set(FUTURES_PHASE6_PRESET) >= {"FUTURES_EXTRA_SIZE_PCT", "FUTURES_REENTRY_BREADTH", "FUTURES_WAVE_RULES"}
