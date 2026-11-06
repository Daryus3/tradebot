"""Faz 3 giriş modülleri: pompa dönüşü (short), Bollinger + RSI (long / short), hacimli kırılım (long); öncelik,
canlı / backtest aynılığı, canlı ön eleme ve paralel mum isteği."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, futures_modules, futures_pump_rules
from strategy import (FUT_BAR_MS, FUT_PRIORITY, FuturesScalpStrategy, futures_candidates, futures_entries,
                      futures_features, pick_signal)
from test_futures import BASE_MS, btc_hourly, fcfg, make_fbot  # noqa: F401 - pytest fikstürü
from test_futures_universe import UniCcxt


def series(closes, volumes=None, opens=None):
    c = np.asarray(closes, dtype=float)
    o = np.concatenate(([c[0]], c[:-1])) if opens is None else np.asarray(opens, dtype=float)
    v = np.full(len(c), 10.0) if volumes is None else np.asarray(volumes, dtype=float)
    t = BASE_MS + np.arange(len(c), dtype=np.int64) * FUT_BAR_MS
    return {"t": t, "o": o, "h": np.maximum(o, c) * 1.0005, "l": np.minimum(o, c) * 0.9995, "c": c, "v": v,
            "tb": v / 2, "qv": v * c, "n": v}


def zigzag(n, amp=0.001, base=100.0):
    return base * (1 + amp * np.where(np.arange(n) % 2, 1.0, -1.0))


def evaluate(cfg, k):
    close_ms = int(k["t"][-1]) + FUT_BAR_MS
    return FuturesScalpStrategy(None, cfg).evaluate({"SOLUSDT": k}, None, close_ms + 3000)


def pump_series(pct=12.0, n=200):
    c = zigzag(n)
    c[-48:] = np.linspace(c[-49], c[-49] * (1 + pct / 100), 48)
    return series(c)


def bb_series(direction=-1, n=240):
    """Yatay dalgalanma + son 6 mumda %0.6'lık adımlarla tek yönlü hareket (bant dışı kapanış, RSI aşırı)."""
    c = zigzag(n)
    for j in range(6):
        c[n - 6 + j] = c[n - 7 + j] * (1 + direction * 0.006)
    return series(c)


def breakout_series(n=200, ratio=5.0):
    c = zigzag(n, 0.002)
    c[-1] = c[-2] * 1.015                                  # Son 4 saatin zirvesinin üstünde kapanış
    v = np.full(n, 10.0)
    v[-1] = 10.0 * ratio
    return series(c, volumes=v)


# ====================================================================== ayarlar
def test_module_settings_and_validation(fcfg):
    assert futures_modules(fcfg) == () and futures_pump_rules(fcfg) == ()
    c = replace(fcfg, futures_pump_rules="12:15,1:6,4:10", futures_bb="both", futures_breakout=True)
    assert futures_pump_rules(c) == ((1, 6.0), (4, 10.0), (12, 15.0))
    assert futures_modules(c) == ("pump", "breakout", "bb")
    assert futures_modules(replace(c, futures_tactic="liq4")) == ()
    for over in ({"futures_pump_rules": "1:1"}, {"futures_bb": "yes"}, {"futures_bb_std": 0.5},
                 {"futures_bb_rsi_low": 60}, {"futures_bb_rsi_high": 40}, {"futures_breakout_volume_ratio": 0.5},
                 {"futures_breakout_hours": 30}):
        with pytest.raises(ConfigError):
            replace(fcfg, **over).validate()
    c.validate()


# ====================================================================== modüller
def test_pump_short_signal(fcfg):
    k = pump_series(12.0)
    assert not evaluate(fcfg, k)[0]                                        # Modül kapalı: yalnızca çöküş
    sigs, _ = evaluate(replace(fcfg, futures_pump_rules="1:6,4:10,12:15"), k)
    assert len(sigs) == 1 and sigs[0].side == "short" and sigs[0].metrics["tactic"] == 2.0
    assert sigs[0].metrics["hours"] == 4 and sigs[0].metrics["rise_pct"] == pytest.approx(12.0, abs=0.3)
    assert sigs[0].priority == FUT_PRIORITY["pump"] and "Pompa dönüşü (short)" in sigs[0].note()
    assert not evaluate(replace(fcfg, futures_pump_rules="4:15"), k)[0]   # Eşik yüksek
    assert not evaluate(replace(fcfg, futures_pump_rules="4:10", futures_sides="long"), k)[0]   # Yön kapalı


def test_bollinger_rsi_long_and_short(fcfg):
    on = replace(fcfg, futures_bb="both")
    sigs, _ = evaluate(on, bb_series(-1))
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].metrics["tactic"] == 3.0
    assert sigs[0].metrics["rsi"] < 25 and sigs[0].metrics["band_pct"] < 0 and "alt bandın" in sigs[0].note()
    sigs, _ = evaluate(on, bb_series(+1))
    assert len(sigs) == 1 and sigs[0].side == "short" and sigs[0].metrics["rsi"] > 75
    assert not evaluate(replace(on, futures_bb="long"), bb_series(+1))[0]
    assert not evaluate(replace(on, futures_bb_rsi_low=5), bb_series(-1))[0]          # RSI şartı
    assert not evaluate(fcfg, bb_series(-1))[0]                                         # Modül kapalı


def test_volume_breakout_long(fcfg):
    on = replace(fcfg, futures_breakout=True)
    sigs, _ = evaluate(on, breakout_series(ratio=5.0))
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].metrics["tactic"] == 4.0
    assert sigs[0].metrics["vol_ratio"] == pytest.approx(5.0) and sigs[0].metrics["break_pct"] > 0
    assert "Hacimli kırılım" in sigs[0].note()
    assert not evaluate(on, breakout_series(ratio=2.5))[0]                              # Hacim yetmez
    assert not evaluate(replace(on, futures_breakout_hours=24), breakout_series())[0]   # 24 saatlik geçmiş yok


def test_priority_crash_over_bollinger_and_pump_over_bb(fcfg):
    c = zigzag(240)
    c[-48:] = np.linspace(c[-49], c[-49] * 0.85, 48)                                    # %15 çöküş: bant da kırık
    k = series(c)
    both = replace(fcfg, futures_bb="both")
    sigs, _ = evaluate(both, k)
    assert sigs[0].metrics["tactic"] == 1.0 and sigs[0].priority == 0                   # Çöküş önce
    f = futures_features(k, both)
    long_ok, _short = futures_candidates(f, both)
    assert long_ok[-1]
    up = pump_series(14.0, 240)
    pb = replace(fcfg, futures_bb="both", futures_pump_rules="4:10")
    sigs, _ = evaluate(pb, up)
    assert sigs[0].side == "short" and sigs[0].metrics["tactic"] == 2.0                 # Pompa, Bollinger'den önce


def test_cross_coin_ranking_uses_priority(fcfg):
    cfg = replace(fcfg, futures_bb="both", futures_pump_rules="4:10")
    strat = FuturesScalpStrategy(None, cfg)
    a, b = bb_series(-1, 240), pump_series(14.0, 240)
    close_ms = int(a["t"][-1]) + FUT_BAR_MS
    sigs, _ = strat.evaluate({"AAAUSDT": a, "BBBUSDT": b}, None, close_ms + 3000)
    assert [s.symbol for s in sigs] == ["BBBUSDT", "AAAUSDT"]                           # Pompa (öncelik -1) önce


def test_vector_entries_match_per_bar_pick(fcfg):
    rng = np.random.default_rng(7)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.006, 3000)))
    v = rng.uniform(5, 15, 3000)
    v[rng.integers(100, 3000, 40)] *= 6
    k = series(c, volumes=v)
    cfg = replace(fcfg, futures_bb="both", futures_breakout=True, futures_pump_rules="1:3,4:6",
                  futures_crash_extra="1:3", futures_crash_drop_pct=6.0)
    f = futures_features(k, cfg)
    idx, side, prio, score = futures_entries(f, cfg)
    assert len(idx) > 50 and set(side) == {1, -1}
    long_ok, short_ok = futures_candidates(f, cfg)
    for j, i in enumerate(idx):
        found = pick_signal(f, int(i), cfg, long_ok, short_ok)
        assert found["side"] == ("long" if side[j] > 0 else "short")
        assert found.get("priority", 0) == prio[j] and found["score"] == pytest.approx(score[j])
    assert len(idx) == int((long_ok | short_ok).sum())


def test_backtest_matches_live_for_modules(fcfg):
    cfg = replace(fcfg, futures_bb="both", futures_breakout=True, futures_pump_rules="1:6,4:10,12:15")
    rng = np.random.default_rng(3)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.007, 1500)))
    v = rng.uniform(5, 15, 1500)
    v[rng.integers(300, 1500, 25)] *= 6
    k = series(c, volumes=v)
    close = k["t"] + FUT_BAR_MS
    data = fb.FutData(int(k["t"][0]), int(close[-1]) + FUT_BAR_MS,
                      [fb.Segment(int(k["t"][0]), int(close[-1]) + 1, ("SOL",))], {"SOLUSDT": k},
                      fb.btc_trend_1h(btc_hourly(True, int(close[-1]))), (k["t"], k["c"]), {}, None, "5m")
    signals, _funnel = fb.find_signals(cfg, data)
    kinds = {s.metrics.get("tactic") for s in signals}
    assert len(signals) > 20 and {2.0, 3.0} <= kinds
    for s in signals[::max(1, len(signals) // 15)]:
        i = int(np.searchsorted(close, s.close_ms))
        live, _ = evaluate(cfg, {key: val[:i + 1] for key, val in k.items()})
        assert live and live[0].side == s.side and live[0].priority == s.priority
        assert live[0].score == pytest.approx(s.score) and live[0].metrics["tactic"] == s.metrics["tactic"]


def test_short_trade_simulation_profit_on_fall():
    data = fb.FutData(BASE_MS, BASE_MS + 10 * 3_600_000, [], {}, None, (np.zeros(0), np.zeros(0)), {}, None, "1m")
    p = fb.SimParams(leverage=2, tp_pct=8, sl_pct=5, be_enabled=True, be_trigger_pct=1, be_floor_pct=0.3,
                     hold_min=45, margin_usage_pct=95, taker_fee=0.0005, maker_fee=0.0002, slippage_pct=0.02,
                     tp_limit=True, mmr_pct=0.5, start_capital=30, kill_pct=0)
    t = BASE_MS + np.arange(60, dtype=np.int64) * 60_000
    prices = np.linspace(100, 85, 60)                                  # Short lehine düşüş: 32 dk'da Kâr Al -%8
    win = (60_000, t, prices, prices * 1.001, prices * 0.999, prices)
    s = fb.FutSignal(BASE_MS, "SOLUSDT", "short", 1.0, 100.0)
    tr = fb.run_trade(p, s, win, 28.5, 57.0, data)
    assert tr.reason == "TP" and tr.pnl > 0 and tr.exit < tr.entry


# ====================================================================== canlı: ön eleme ve paralel mumlar
def test_prefilter_with_modules(fcfg, tmp_path):
    ucfg = replace(fcfg, futures_universe=3, futures_universe_with_list=False)
    fake = UniCcxt({"SOLUSDT": (94.5, 100.0, 3e9), "ADAUSDT": (110.0, 111.0, 2e9), "XRPUSDT": (99.5, 100.0, 1e9)})
    bot, fake, _notes, _clock = make_fbot(ucfg, tmp_path, fake=fake)
    tickers = {"SOLUSDT": {"last": 94.5, "high": 100.0, "low": 94.0, "qv": 3e9},
               "ADAUSDT": {"last": 110.0, "high": 111.0, "low": 100.0, "qv": 2e9},     # 24 saatte +%10
               "XRPUSDT": {"last": 99.5, "high": 100.0, "low": 99.0, "qv": 1e9}}
    bot.exchange.tickers_24h = lambda: tickers
    assert bot._scan_symbols()[0] == []                                   # Yalnızca çöküş (4s %10): hiçbiri
    bot.cfg = replace(ucfg, futures_pump_rules="4:10")
    assert bot._scan_symbols()[0] == ["ADAUSDT"]                          # Pompa adayı (dibinin %9+ üstünde)
    bot.cfg = replace(ucfg, futures_bb="both")
    assert sorted(bot._scan_symbols()[0]) == ["ADAUSDT", "SOLUSDT", "XRPUSDT"]   # Bollinger: ön eleme yok
    bot.cfg = replace(ucfg, futures_breakout=True)
    assert bot._scan_symbols()[1] == 0


def test_many_symbols_fetched_in_parallel(fcfg, tmp_path, monkeypatch):
    import futures_trader
    names = [f"C{i:02d}USDT" for i in range(20)]
    tick = {s: (1.0, 1.0, 1e9 - i) for i, s in enumerate(names)}
    fake = UniCcxt(tick)
    cfg = replace(fcfg, futures_universe=20, futures_universe_with_list=False, futures_bb="both")
    bot, fake, _notes, clock = make_fbot(cfg, tmp_path, fake=fake)
    used = []

    class Pool:
        def __init__(self, max_workers):
            used.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def map(self, fn, items):
            return [fn(x) for x in items]

    monkeypatch.setattr(futures_trader, "ThreadPoolExecutor", Pool)
    fake.calls.clear()
    bot.scan_and_enter(clock.t)
    asked = [c[1] for c in fake.calls if c[0] == "klines" and c[2] == "5m"]
    assert used == [futures_trader.KLINE_WORKERS] and sorted(asked) == sorted(names)


def test_grid_search_handles_short_modules(fcfg):
    cfg = replace(fcfg, loss_cooldown_minutes=0)
    rng = np.random.default_rng(11)
    coins = {}
    for sym in ("SOLUSDT", "ADAUSDT"):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.007, 2000)))
        v = rng.uniform(5, 15, 2000)
        coins[sym] = series(c, volumes=v)
    t0, end = int(BASE_MS), int(BASE_MS) + 2000 * FUT_BAR_MS
    data = fb.FutData(t0, end, [fb.Segment(t0 + 300 * FUT_BAR_MS, end, ("SOL", "ADA"))], coins,
                      fb.btc_trend_1h(btc_hourly(True, end)), (coins["SOLUSDT"]["t"], coins["SOLUSDT"]["c"]), {},
                      None, "5m")
    variants = [fb.GridVariant("taban"),
                fb.GridVariant("tümü", (("futures_bb", "both"), ("futures_pump_rules", "1:3,4:5"),
                                        ("futures_breakout", True)))]
    rows = fb.grid_search(cfg, data, variants, split_ms=t0 + 1200 * FUT_BAR_MS)
    for v, row in zip(variants, rows):
        sigs, _ = fb.find_signals(v.cfg(cfg), data)
        assert row.signals == len(sigs)
        windows = fb.load_windows(data, sigs, 60)
        fixed = fb.simulate(fb.params_from_cfg(v.cfg(cfg)), data, sigs, windows,
                            fixed_notional=cfg.start_capital * cfg.futures_margin_usage_pct / 100 * cfg.futures_leverage)
        assert row.whole.trades == len(fixed.trades)
        assert row.whole.sum_move == pytest.approx(sum(fb.net_move_pct(tr) for tr in fixed.trades))
    assert rows[1].signals > rows[0].signals


# ====================================================================== Telegram ve modül ızgarası
def press(bot, data, message_id=7):
    from commands import CALLBACK, Command
    bot.commands.handle(Command(CALLBACK, data.split(":"), kind="callback", message_id=message_id))


def test_module_page_buttons_and_env(fcfg, tmp_path):
    from runtime_settings import EDITABLE, parse_value
    bot, _fake, notes, _clock = make_fbot(fcfg, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fe")
    assert "pg:fm" in [d for row in notes.menus[-1][1] for _l, d in row]
    press(bot, "pg:fm")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert "kv:fm:FUTURES_PUMP_RULES:1h6,4h10,12h15" in data and "kv:fm:FUTURES_BB:both" in data
    assert "kv:fm:FUTURES_BREAKOUT:t" in data and all(len(d.encode()) <= 64 for d in data)
    assert "zarar etti" in text and "<" not in text.replace("<b>", "").replace("</b>", "").replace(
        "<i>", "").replace("</i>", "")                                    # HTML: < ve > kaçışlı
    press(bot, "kv:fm:FUTURES_PUMP_RULES:1h6,4h10,12h15")
    assert bot.cfg.futures_pump_rules == "1:6,4:10,12:15" and "FUTURES_PUMP_RULES=1:6,4:10,12:15" in env.read_text()
    press(bot, "kv:fm:FUTURES_BB:both")
    assert bot.cfg.futures_bb == "both" and "kv:fm:FUTURES_BB_RSI_LOW:20" in [
        d for row in notes.menus[-1][1] for _l, d in row]
    press(bot, "kv:fm:FUTURES_BREAKOUT:t")
    assert bot.cfg.futures_breakout and "kv:fm:FUTURES_BREAKOUT_VOLUME_RATIO:5" in [
        d for row in notes.menus[-1][1] for _l, d in row]
    press(bot, "pg:fe")
    assert any("Ek modüller: pompa short, kırılım, Bollinger" in label for row in notes.menus[-1][1]
               for label, _d in row)
    assert EDITABLE["FUTURES_PUMP_RULES"].kind == "rules" and parse_value("FUTURES_BB", "LONG") == "long"
    press(bot, "pg:fc")
    assert "kv:fc:FUTURES_UNIVERSE:150" in [d for row in notes.menus[-1][1] for _l, d in row]


def test_module_grid_cli_report(fcfg, tmp_path, monkeypatch):
    rng = np.random.default_rng(5)
    coins = {}
    for sym in ("SOLUSDT", "ADAUSDT"):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.008, 2500)))
        coins[sym] = series(c, volumes=rng.uniform(5, 15, 2500))
    t0, end = int(BASE_MS), int(BASE_MS) + 2500 * FUT_BAR_MS
    data = fb.FutData(t0, end, [fb.Segment(t0 + 300 * FUT_BAR_MS, end, ("SOL", "ADA"))], coins,
                      fb.btc_trend_1h(btc_hourly(True, end)), (coins["SOLUSDT"]["t"], coins["SOLUSDT"]["c"]), {},
                      None, "5m")
    monkeypatch.setattr(fb, "period", lambda *_a, **_k: (t0, end, data.segments))
    monkeypatch.setattr(fb, "prepare", lambda *_a, **_k: data)
    monkeypatch.setattr(fb, "GRID_SPLIT", fb._date(t0 + 1500 * FUT_BAR_MS))
    report, unis, path = fb.run_module_grid_cli(fcfg, None, None, tmp_path, universes=(20,),
                                                progress=lambda _m: None)
    assert path is not None and path.read_text(encoding="utf-8").startswith("🧩 MODÜL IZGARASI")
    assert len(unis) == 1 and len(unis[0].rows) == len(fb.module_variants(fcfg))
    for part in ("Yalnız pompa short", "HEPSİ", "kayma ×3", "🏁", "1 dk"):
        assert part in report, part
    labels = [r.variant.label for r, _x in unis[0].rows]
    assert labels[0].startswith("Çöküş (ayarlarınız)") and len(set(labels)) == len(labels)
    base = dict(unis[0].rows[0][0].variant.over)
    assert base["futures_pump_rules"] == "" and base["futures_bb"] == "off" and base["futures_breakout"] is False
