"""Dalga modu (Faz 5): piyasa genişliği, çoklu pozisyon (ek pozisyon yalnızca dalgada), yalnız çöküşte küçük pozisyon;
backtest motoru (tek pozisyonda eski davranış aynen), canlı / test modu tarama ve büyüklük, Telegram sayfası ve seti."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import FUTURES_CASCADE_PRESET, ConfigError, _setting_notes, load_config
from strategy import (FUT_BAR_MS, FuturesScalpStrategy, FuturesSignal, breadth_flags, futures_features,
                      isolated_factor, market_breadth)
from test_futures import BASE_MS, FakeCcxt, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü

MIN = 60_000


def series(closes, start=BASE_MS):
    c = np.asarray(closes, dtype=float)
    o = np.concatenate(([c[0]], c[:-1]))
    v = np.full(len(c), 10.0)
    t = start + np.arange(len(c), dtype=np.int64) * FUT_BAR_MS
    return {"t": t, "o": o, "h": np.maximum(o, c) * 1.0005, "l": np.minimum(o, c) * 0.9995, "c": c, "v": v,
            "tb": v / 2, "qv": v * c, "n": v}


def crashing(n=300, drop=8.0):
    """Son 1 saatte (12 mum) %drop düşen dizi: 1 sa %6 ek kuralı tetiklenir, genişliğe sayılır."""
    c = np.full(n, 100.0) * (1 + 0.0005 * np.where(np.arange(n) % 2, 1.0, -1.0))
    c[-12:] = np.linspace(c[-13], c[-13] * (1 - drop / 100), 13)[1:]
    return c


def flat(n=300):
    return np.full(n, 100.0) * (1 + 0.0005 * np.where(np.arange(n) % 2, 1.0, -1.0))


# ====================================================================== ayarlar ve ortak ölçüler
def test_cascade_settings_defaults_validation_env_notes(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_max_positions, fcfg.futures_extra_breadth) == (1, 8)
    assert (fcfg.futures_isolated_size_pct, fcfg.futures_isolated_breadth) == (100.0, 2)
    for bad in (dict(futures_max_positions=0), dict(futures_max_positions=6), dict(futures_extra_breadth=0),
                dict(futures_isolated_size_pct=5.0), dict(futures_isolated_size_pct=120.0),
                dict(futures_isolated_breadth=0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    for key, value in (("FUTURES_MAX_POSITIONS", "2"), ("FUTURES_EXTRA_BREADTH", "5"),
                       ("FUTURES_ISOLATED_SIZE_PCT", "50"), ("FUTURES_ISOLATED_BREADTH", "3")):
        monkeypatch.setenv(key, value)
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_max_positions, loaded.futures_extra_breadth) == (2, 5)
    assert (loaded.futures_isolated_size_pct, loaded.futures_isolated_breadth) == (50.0, 3)
    on = replace(fcfg, futures_max_positions=3, futures_isolated_size_pct=50.0, futures_universe=0)
    notes = " ".join(_setting_notes(on))
    assert "dalga modu" in notes and "3 ve üstü" in notes and "listesinde" in notes and "FUTURES_ISOLATED_SIZE_PCT" in notes
    assert not any("dalga modu" in n for n in _setting_notes(fcfg))
    preset = replace(fcfg, futures_max_positions=FUTURES_CASCADE_PRESET["FUTURES_MAX_POSITIONS"],
                     futures_leverage=FUTURES_CASCADE_PRESET["FUTURES_LEVERAGE"])
    preset.validate()


def test_breadth_helpers():
    k = {"A": series(crashing(drop=4.0)), "B": series(crashing(drop=2.0)), "C": series(flat()),
         "D": series(crashing(drop=8.0))}
    assert market_breadth(k) == 2                                       # %3 ve üstü düşenler: A, D
    flags = breadth_flags({"c": k["D"]["c"]})
    assert flags[-1] and not flags[:-12].any()
    assert isolated_factor(100, 2, 0) == 1.0 and isolated_factor(50, 2, 1) == 0.5 and isolated_factor(50, 2, 2) == 1.0


def test_evaluate_attaches_breadth_only_in_cascade_mode(fcfg):
    c = replace(fcfg, futures_crash_extra="1:6")
    klines = {"SOLUSDT": series(crashing()), "ADAUSDT": series(crashing(drop=7.0)), "XRPUSDT": series(flat())}
    now = int(klines["SOLUSDT"]["t"][-1]) + FUT_BAR_MS + 3000
    sigs, _r = FuturesScalpStrategy(None, c).evaluate(klines, None, now)
    assert sigs and all("breadth" not in s.metrics for s in sigs)
    on = replace(c, futures_max_positions=2)
    sigs, _r = FuturesScalpStrategy(None, on).evaluate(klines, None, now)
    assert [s.metrics["breadth"] for s in sigs] == [2.0, 2.0] and "Dalga: 2 coin" in sigs[0].note()
    sigs, _r = FuturesScalpStrategy(None, on).evaluate({"SOLUSDT": klines["SOLUSDT"]}, None, now, breadth=11)
    assert sigs[0].metrics["breadth"] == 11.0                           # Canlı: tüm coinlerden verilen genişlik
    s = FuturesSignal("SOLUSDT", "long", 1.0, 0, 1.0, {"tactic": 1.0, "breadth": 1.0, "size_pct": 50.0})
    assert "Pozisyon %50 (yalnız çöküş)" in s.note()


# ====================================================================== backtest motoru
def bt_data(start=BASE_MS, days=3):
    return fb.FutData(start, start + days * fb.DAY_MS, [], {}, ((), (), ()), (np.zeros(0), np.zeros(0)), {}, None)


def bt_sig(symbol, close_ms, breadth, score=1.0):
    s = fb.FutSignal(close_ms, symbol, "long", score, 100.0)
    s.breadth = breadth
    return s


def tp_window(start, bars=40):
    """Önce 15 dk yatay, sonra TP'ye giden (+%1.2) 1 dk mumlar."""
    rows = [(100, 100.05, 99.95, 100.0)] * 15 + [(100, 101.3, 99.98, 101.2)] + [(101.2, 101.3, 101.1, 101.2)] * bars
    return window(rows, start=start)


def test_simulate_single_slot_unchanged_and_isolated_size():
    data = bt_data()
    a, b = bt_sig("AAAUSDT", BASE_MS, 9), bt_sig("BBBUSDT", BASE_MS + 5 * MIN, 9)
    w = {("AAAUSDT", a.close_ms): tp_window(a.close_ms), ("BBBUSDT", b.close_ms): tp_window(b.close_ms)}
    p = params(start_capital=100.0, be_enabled=False, hold_min=60)
    one = fb.simulate(p, data, [a, b], w)
    assert [t.symbol for t in one.trades] == ["AAAUSDT"] and one.skipped["açık pozisyon vardı"] == 1
    assert one.trades[0].margin == pytest.approx(100.0) and one.trades[0].breadth == 9
    a.breadth = 1                                                       # Yalnız çöküş: yarım büyüklük
    half = fb.simulate(replace(p, isolated_size_pct=50.0, isolated_breadth=2), data, [a, b], w)
    assert half.trades[0].margin == pytest.approx(50.0)
    assert fb.simulate(replace(p, isolated_size_pct=50.0), data, [a, b], w, fixed_notional=100.0).trades[0].notional \
        == pytest.approx(50.0)


def test_simulate_slots_gate_sizing_and_realization():
    data = bt_data()
    a, b = bt_sig("AAAUSDT", BASE_MS, 9), bt_sig("BBBUSDT", BASE_MS + 5 * MIN, 9)
    c = bt_sig("CCCUSDT", BASE_MS + 10 * MIN, 3)                        # Dalga yok: ek pozisyon açılmaz
    w = {(s.symbol, s.close_ms): tp_window(s.close_ms) for s in (a, b, c)}
    p = params(start_capital=100.0, be_enabled=False, hold_min=60, max_positions=2, extra_breadth=8)
    res = fb.simulate(p, data, [a, b, c], w)
    assert [t.symbol for t in res.trades] == ["AAAUSDT", "BBBUSDT"] and [t.slot for t in res.trades] == [1, 2]
    assert res.trades[0].margin == pytest.approx(50.0) and res.trades[1].margin == pytest.approx(50.0)
    assert res.skipped["açık pozisyon vardı"] == 1
    assert [x[0] for x in res.curve] == sorted(x[0] for x in res.curve)
    assert res.final_equity == pytest.approx(100.0 + sum(t.pnl for t in res.trades))
    gate = fb.simulate(replace(p, extra_breadth=10), data, [a, b, c], w)
    assert [t.symbol for t in gate.trades] == ["AAAUSDT"]
    assert gate.skipped["ek pozisyon: dalga yok (genişlik < 10)"] == 2
    same = fb.simulate(p, data, [a, bt_sig("AAAUSDT", BASE_MS + 5 * MIN, 9)], {**w, ("AAAUSDT", BASE_MS + 5 * MIN):
                                                                              tp_window(BASE_MS + 5 * MIN)})
    assert len(same.trades) == 1 and same.skipped["bu coinde açık pozisyon var"] == 1
    # Yalnız çöküşte ilk pozisyon yarım (yuva payının yarısı)
    a.breadth = 0
    iso = fb.simulate(replace(p, isolated_size_pct=50.0, isolated_breadth=2), data, [a, b], w)
    assert iso.trades[0].margin == pytest.approx(25.0) and iso.trades[1].margin == pytest.approx(50.0)


def test_simulate_slots_limit_entry_one_attempt_per_bar():
    data = bt_data()
    a, b = bt_sig("AAAUSDT", BASE_MS, 9, score=2.0), bt_sig("BBBUSDT", BASE_MS, 9, score=1.0)
    dip = window([(100, 100.0, 99.5, 99.6)] + [(99.6, 99.7, 99.55, 99.6)] * 80, start=BASE_MS)
    p = params(start_capital=100.0, max_positions=2, extra_breadth=8, entry_limit=True, limit_offset_pct=0.1,
               hold_min=30)
    res = fb.simulate(p, data, [a, b], {("AAAUSDT", BASE_MS): dip, ("BBBUSDT", BASE_MS): dip})
    assert len(res.trades) == 1 and res.skipped["aynı mumda limit emir denendi"] == 1


def test_find_signals_counts_breadth_over_universe(fcfg):
    c = replace(fcfg, futures_crash_extra="1:6")
    coins = {"SOLUSDT": series(crashing()), "ADAUSDT": series(crashing(drop=4.0)), "XRPUSDT": series(flat())}
    for k in coins.values():                                            # Sinyal mumunun ardından bir mum (giriş)
        for key in k:
            k[key] = np.concatenate((k[key], k[key][-1:] + (FUT_BAR_MS if key == "t" else 0)))
    t0, end = int(BASE_MS), int(coins["SOLUSDT"]["t"][-1]) + FUT_BAR_MS
    data = fb.FutData(t0, end, [fb.Segment(t0 - 1, end, ("SOL", "ADA", "XRP"))], coins, ((), (), ()),
                      (np.zeros(0), np.zeros(0)), {}, None, "5m")
    sigs, _f = fb.find_signals(c, data)
    last = [s for s in sigs if s.symbol == "SOLUSDT" and s.close_ms == int(coins["SOLUSDT"]["t"][-2]) + FUT_BAR_MS]
    assert last and last[0].breadth == 2 and data.breadth is not None
    assert fb.breadth_at(data, last[0].close_ms) == 2


def test_cascade_rows_and_report_lines(fcfg):
    calls = []
    rows = fb.cascade_rows(fcfg, lambda **over: calls.append(over) or over)
    assert rows[0][0].startswith("🌊 Dalga modu") and calls[0]["max_positions"] == 2 and calls[0]["leverage"] == 3
    assert rows[1][1]["isolated_size_pct"] == 50.0
    on = replace(fcfg, futures_max_positions=2, futures_isolated_size_pct=50.0)
    rows = fb.cascade_rows(on, lambda **over: over)
    assert rows[0][0].startswith("Tek pozisyon") and len(rows) == 1
    p = fb.params_from_cfg(on)
    assert "en fazla 2 pozisyon" in fb.cascade_label(p) and "%50" in fb.cascade_label(p)
    trades = [fb.FutTrade("A", "long", 0, 1, 1, 1, "TP", 2, 1, 2, 0, 0, 0.01, 1, 0, 0, 0, False, False, MIN,
                          breadth=b, slot=s) for b, s in ((0, 1), (9, 2), (12, 1))]
    text = fb.breadth_breakdown(trades, p)
    assert "yalnız çöküş (0-1 coin): 1 işlem" in text and "dalga (8+ coin): 2 işlem" in text and "ek pozisyonlar: 1" in text


# ====================================================================== canlı / test modu
class KlineCcxt(FakeCcxt):
    """Coin başına 5 dk mum dizisi döndüren sahte borsa (son mum az önce kapanan)."""

    def __init__(self, closes, **kw):
        super().__init__(prices={s: float(c[-1]) for s, c in closes.items()}, **kw)
        self.closes = closes
        self.kline_calls = []

    def fapiPublicGetKlines(self, params):
        sym, interval = params["symbol"], params["interval"]
        self.kline_calls.append(sym)
        if interval != "5m" or sym not in self.closes:
            return []
        c = self.closes[sym]
        n = len(c)
        start = BASE_MS - n * FUT_BAR_MS
        rows = []
        for i in range(n):
            o = c[i - 1] if i else c[0]
            t = start + i * FUT_BAR_MS
            rows.append([t, str(o), str(max(o, c[i]) * 1.0005), str(min(o, c[i]) * 0.9995), str(c[i]), "10",
                         t + FUT_BAR_MS - 1, str(10 * c[i]), "5", "5", "0", "0"])
        return rows[-int(params["limit"]):]


def live_cfg(fcfg, **over):
    base = dict(futures_crash_extra="1:6", futures_coins="SOL,ADA,XRP,DOGE", futures_universe=0, futures_leverage=3)
    base.update(over)
    c = replace(fcfg, **base)
    c.validate()
    return c


def test_paper_cascade_opens_second_position_only_in_wave(fcfg, tmp_path):
    closes = {"SOLUSDT": crashing(), "ADAUSDT": crashing(drop=7.0), "XRPUSDT": crashing(drop=6.5),
              "DOGEUSDT": flat(), "BTCUSDT": flat()}
    cfg = live_cfg(fcfg, futures_max_positions=2, futures_extra_breadth=3)
    bot, fake, notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(closes))
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions
    assert len(pos) == 2 and bot.last_breadth == 3 and bot.free_slots() == 0
    margins = sorted(p.margin_usdt for p in pos.values())
    assert margins[0] == pytest.approx(margins[1], rel=0.02) and margins[1] == pytest.approx(30 * 0.95 / 2, rel=0.02)
    assert all("Dalga: 3 coin" in p.signal_note for p in pos.values())
    # Aynı coinler dalga eşiğinin altında: tek pozisyon
    bot2, _f2, _n2, clock2 = make_fbot(replace(cfg, futures_extra_breadth=4), tmp_path / "b", fake=KlineCcxt(closes))
    bot2.scan_and_enter(clock2.t)
    assert len(bot2.state.positions) == 1 and bot2.free_slots() == 1


def test_paper_isolated_crash_half_size_and_breadth_counts_held_coins(fcfg, tmp_path):
    closes = {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}
    cfg = live_cfg(fcfg, futures_isolated_size_pct=50.0, futures_isolated_breadth=2, futures_leverage=2)
    bot, fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(closes))
    bot.scan_and_enter(clock.t)
    p = bot.state.positions["SOLUSDT"]
    assert p.margin_usdt == pytest.approx(30 * 0.95 * 0.5, rel=0.02) and "Pozisyon %50" in p.signal_note
    # Dalga modu: açık pozisyondaki coin de genişliğe sayılır (mumları istenir, sinyal aranmaz)
    closes2 = {"SOLUSDT": crashing(), "ADAUSDT": crashing(drop=7.0), "XRPUSDT": flat(), "DOGEUSDT": flat(),
               "BTCUSDT": flat()}
    cfg2 = live_cfg(fcfg, futures_max_positions=2, futures_extra_breadth=2)
    bot2, fake2, _n2, clock2 = make_fbot(cfg2, tmp_path / "c", fake=KlineCcxt(closes2))
    sig = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0, "breadth": 2.0})
    bot2.open_futures(sig, clock2.t)
    fake2.kline_calls.clear()
    bot2.scan_and_enter(clock2.t)
    assert "SOLUSDT" in fake2.kline_calls and bot2.last_breadth == 2
    assert set(bot2.state.positions) == {"SOLUSDT", "ADAUSDT"}


def test_single_slot_behaviour_unchanged(fcfg, tmp_path):
    closes = {"SOLUSDT": crashing(), "ADAUSDT": crashing(drop=7.0), "XRPUSDT": crashing(drop=6.5),
              "DOGEUSDT": flat(), "BTCUSDT": flat()}
    cfg = live_cfg(fcfg, futures_leverage=2)
    bot, fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(closes))
    assert bot.slots() == 1 and not bot._cascade_on()
    bot.scan_and_enter(clock.t)
    assert list(bot.state.positions) == ["SOLUSDT"]                     # En güçlü sinyal, tek pozisyon
    assert bot.state.positions["SOLUSDT"].margin_usdt == pytest.approx(30 * 0.95, rel=0.02)
    assert bot.free_slots() == 0 and bot.last_breadth is None


def test_scan_continues_while_cascade_position_open(fcfg, tmp_path):
    closes = {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}
    cfg = live_cfg(fcfg, futures_max_positions=2)
    bot, fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(closes))
    bot.open_futures(FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0}), clock.t)
    scans = []
    bot.scan_and_enter = lambda now: scans.append(now)
    bot.next_scan_at = clock.t
    bot.tick()
    assert scans                                                        # Boş yer var: tarama sürer
    one = live_cfg(fcfg)
    bot1, _f, _n, clock1 = make_fbot(one, tmp_path / "d", fake=KlineCcxt(closes))
    bot1.open_futures(FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0}), clock1.t)
    scans1 = []
    bot1.scan_and_enter = lambda now: scans1.append(now)
    bot1.next_scan_at = clock1.t
    bot1.tick()
    assert not scans1                                                   # Tek pozisyonda eskisi gibi beklenir


# ====================================================================== Telegram
def press(bot, data, message_id=7):
    from commands import CALLBACK, Command
    bot.commands.handle(Command(CALLBACK, data.split(":"), kind="callback", message_id=message_id))


def test_cascade_page_preset_and_off(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(fcfg, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fe")
    assert "pg:fw" in [d for row in notes.menus[-1][1] for _l, d in row]
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert "fcm:on" in data and "kv:fw:FUTURES_MAX_POSITIONS:2" in data and all(len(d.encode()) <= 64 for d in data)
    assert "Kapalı" in text and "+%0.46" in text
    press(bot, "fcm:on")
    pending = bot.commands.pending
    assert pending is not None and bot.cfg.futures_max_positions == 1          # Onaydan önce değişmez
    press(bot, f"yes:{pending.token}")
    assert (bot.cfg.futures_max_positions, bot.cfg.futures_leverage, bot.cfg.futures_extra_breadth) == (2, 3, 8)
    assert bot.cfg.futures_isolated_size_pct == 50.0 and "FUTURES_MAX_POSITIONS=2" in env.read_text()
    press(bot, "pg:fw")
    labels = [label for row in notes.menus[-1][1] for label, _d in row]
    assert any(label.startswith("✅ ⭐ Faz 5 seti") for label in labels) and "fcm:off" in [
        d for row in notes.menus[-1][1] for _l, d in row]
    press(bot, "fcm:off")
    press(bot, f"yes:{bot.commands.pending.token}")
    assert (bot.cfg.futures_max_positions, bot.cfg.futures_leverage, bot.cfg.futures_isolated_size_pct) == (1, 2, 100.0)
    press(bot, "fcm:x")
    assert "geçerli değil" in notes.messages[-1]
