"""Küçük çöküşte Kâr Al (FUTURES_SMALL_TP_X / FUTURES_SMALL_TP_PCT; rapor 29 araştırması, 27 Kasım): çöküş sinyalinde
düşüş, çöküş eşiğinin X katından azsa (eşik zar zor geçildi) Kâr Al fiyat +%Y, diğerlerinde TAKE_PROFIT_PCT. Ortak
kural (config), ayarlar, backtest (işlem başına Kâr Al, iki motor, rapor satırları), canlı / test modunda Binance'e konan
Kâr Al ve pozisyon mesajı, Telegram satırları. Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from config import ConfigError, futures_tp_pct, load_config, small_crash_tp_pct
from runtime_settings import EDITABLE
from strategy import FuturesSignal
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import press
from test_futures_day_brake import bt_data

MIN = fb.MIN_MS


def crash(drop, thr=10.0, **extra):
    return {"tactic": 1.0, "drop_pct": drop, "thr_pct": thr, **extra}


# ====================================================================== ortak kural ve ayarlar
def test_shared_rule():
    assert small_crash_tp_pct(1.5, 5.0, 6.0, crash(12.0)) == 5.0           # 12 / 10 = 1.2 < 1.5: küçük çöküş
    assert small_crash_tp_pct(1.5, 5.0, 6.0, crash(15.0)) == 6.0           # Tam 1.5 kat: küçük sayılmaz
    assert small_crash_tp_pct(1.5, 5.0, 6.0, crash(25.0)) == 6.0
    assert small_crash_tp_pct(0.0, 5.0, 6.0, crash(12.0)) == 6.0           # Kapalı
    assert small_crash_tp_pct(1.5, 5.0, 6.0, {**crash(12.0), "tactic": 2.0}) == 6.0   # Ek modül (pompa) sinyali
    for m in ({}, None, {"tactic": 1.0}, crash(12.0, thr=0.0), crash(float("nan")), crash(12.0, thr=float("nan")),
              {"tactic": 1.0, "drop_pct": "x", "thr_pct": 10.0}):
        assert small_crash_tp_pct(1.5, 5.0, 6.0, m) == 6.0                 # Ölçü yok / bozuk: ana Kâr Al


def test_settings_defaults_validation_env(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_small_tp_x, fcfg.futures_small_tp_pct) == (0.0, 5.0)
    assert futures_tp_pct(fcfg, crash(12.0)) == fcfg.take_profit_pct
    on = replace(fcfg, futures_small_tp_x=1.5, futures_small_tp_pct=4.5)
    on.validate()
    assert futures_tp_pct(on, crash(12.0)) == 4.5 and futures_tp_pct(on, crash(20.0)) == on.take_profit_pct
    for bad in (dict(futures_small_tp_x=1.0), dict(futures_small_tp_x=0.5), dict(futures_small_tp_x=12.0),
                dict(futures_small_tp_pct=0.2), dict(futures_small_tp_pct=60.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    monkeypatch.setenv("FUTURES_SMALL_TP_X", "1.5")
    monkeypatch.setenv("FUTURES_SMALL_TP_PCT", "5.5")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_small_tp_x, loaded.futures_small_tp_pct) == (1.5, 5.5)
    for key in ("FUTURES_SMALL_TP_X", "FUTURES_SMALL_TP_PCT"):
        assert EDITABLE[key].attr == key.lower()


# ====================================================================== backtest
ROWS = [(100, 105.5, 99.9, 105.0)] + [(105.0, 105.2, 104.0, 104.5)] * 5     # +%5.5'e çıkıp geri döner


@pytest.mark.parametrize("slots", [1, 2])
def test_engines_use_per_trade_tp(slots):
    small = fb.FutSignal(BASE_MS, "AAAUSDT", "long", 1.0, 100.0, crash(12.0))
    big = fb.FutSignal(BASE_MS + 30 * MIN, "BBBUSDT", "long", 1.0, 100.0, crash(25.0))
    w = {(s.symbol, s.close_ms): window(ROWS, start=s.close_ms) for s in (small, big)}
    p = params(tp_pct=6.0, be_enabled=False, hold_min=3, small_tp_x=1.5, small_tp_pct=5.0, max_positions=slots)
    a, b = fb.simulate(p, bt_data(), [small, big], w).trades
    assert (a.reason, a.small_tp) == ("TP", True) and a.exit == pytest.approx(105.0)
    assert b.reason == "TIME" and not b.small_tp                       # Büyük çöküşte hedef +%6: görülmedi
    off = fb.simulate(replace(p, small_tp_x=0.0), bt_data(), [small, big], w).trades
    assert [t.reason for t in off] == ["TIME", "TIME"] and not any(t.small_tp for t in off)
    if slots == 1:                                                     # Faz 9 zaman stopu da korunur (tek pozisyon)
        long_hold = replace(p, wave_hold_min=5, wave_hold_breadth=0)
        t = fb.simulate(long_hold, bt_data(), [big], w).trades[0]
        assert t.reason == "TIME" and t.exit_ms == big.close_ms + 5 * MIN


def test_params_cache_and_variant_rows(fcfg):
    p = params(tp_pct=6.0, small_tp_x=1.5, small_tp_pct=5.0)
    cache = {}
    small = fb.FutSignal(BASE_MS, "AAAUSDT", "long", 1.0, 100.0, crash(12.0))
    big = fb.FutSignal(BASE_MS, "BBBUSDT", "long", 1.0, 100.0, crash(25.0))
    got = fb.small_tp_params(p, small, cache)
    assert got.tp_pct == 5.0 and got is fb.small_tp_params(p, small, cache)     # Aynı pp: tek kopya
    assert fb.small_tp_params(p, big, cache) is p
    assert fb.small_tp_params(replace(p, small_tp_x=0.0), small, cache).tp_pct == 6.0
    rows = dict(fb.small_tp_variant_rows(fb.params_from_cfg(fcfg), lambda **kw: kw))
    assert rows == {"Küçük çöküşte (düşüş < eşik ×1.5) Kâr Al %5": {"small_tp_x": 1.5, "small_tp_pct": 5.0}}
    on = replace(fcfg, futures_small_tp_x=1.5, futures_small_tp_pct=5.0)
    got = dict(fb.small_tp_variant_rows(fb.params_from_cfg(on), lambda **kw: kw))
    assert got["Küçük çöküşte Kâr Al kapalı"] == {"small_tp_x": 0.0}
    assert got["Küçük çöküş: düşüş < eşik ×1.25 (şimdi ×1.5)"] == {"small_tp_x": 1.25}
    assert "Küçük çöküş: düşüş < eşik ×2 (şimdi ×1.5)" in got and "Küçük çöküş: düşüş < eşik ×1.5 (şimdi ×1.5)" not in got
    assert got["Küçük çöküşte Kâr Al %4.5 (şimdi %5)"] == {"small_tp_pct": 4.5} and len(got) == 5
    names = [n for n, _e in fb.exit_variant_rows(on, lambda **kw: kw)]
    assert "Küçük çöküşte Kâr Al kapalı" in names


def test_report_header_and_settings_line(fcfg):
    from test_futures_minute_scan import bt_data as m_data, k1_of, minute_closes, scan_cfg
    k1 = {"SOLUSDT": k1_of(minute_closes(dip=9.0))}
    scan = replace(scan_cfg(fcfg, futures_coins="SOL"), futures_small_tp_x=10.0, futures_small_tp_pct=4.5)
    data = m_data(k1)
    found, funnel = fb.find_signals(scan, data)
    assert found and all("drop_pct" in s.metrics and "thr_pct" in s.metrics for s in found)
    windows = {}
    for s in found:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * MIN)
        windows[(s.symbol, s.close_ms)] = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    p = fb.params_from_cfg(scan)
    assert (p.small_tp_x, p.small_tp_pct) == (10.0, 4.5)
    ev = fb.evaluate(p, data, found, windows)
    text = fb.format_report(scan, data, ev, funnel)
    assert ("🎯 Küçük çöküşte Kâr Al: çöküş düşüşü eşiğinin 10 katından azsa (eşik zar zor geçildi) Kâr Al +%4.5, "
            f"diğerlerinde +%{scan.take_profit_pct:g}.") in text
    assert f"Kâr Al +%{scan.take_profit_pct:g} (küçük çöküşte +%4.5)" in text


# ====================================================================== canlı / test modu
def test_paper_open_places_small_tp_and_message(fcfg, tmp_path):
    cfg = replace(fcfg, futures_tactic="crash", futures_small_tp_x=1.5, futures_small_tp_pct=5.0)
    cfg.validate()
    bot, _fake, notes, clock = make_fbot(cfg, tmp_path)
    lev = cfg.futures_leverage
    pos = bot.open_futures(FuturesSignal("BTCUSDT", "long", 100.0, BASE_MS, 5.0, crash(12.0)), clock.t)
    assert pos.tp_price == pytest.approx(pos.entry_price * 1.05, abs=0.011)    # Fiyat adımına yuvarlı
    assert pos.metrics["tp_pct"] == 5.0 and pos.tp_pct is None         # Spot alanı (ROE yorumu) kullanılmaz
    assert any(f"(fiyat +%5 → ROE ~+%{5 * lev:.0f}, küçük çöküş)" in m for m in notes.messages)
    bot2, _f2, notes2, clock2 = make_fbot(cfg, tmp_path / "b")          # Bakiye ilk pozisyonda: ayrı bot
    big = bot2.open_futures(FuturesSignal("ETHUSDT", "long", 100.0, BASE_MS, 5.0, crash(25.0)), clock2.t)
    assert big.tp_price == pytest.approx(big.entry_price * (1 + cfg.take_profit_pct / 100), abs=0.011)
    assert "tp_pct" not in big.metrics
    assert any(f"(fiyat +%{cfg.take_profit_pct:g} → ROE ~+%{cfg.take_profit_pct * lev:.0f})" in m
               for m in notes2.messages)


def test_live_tp_order_price(fcfg, tmp_path):
    cfg = replace(fcfg, futures_tactic="crash", futures_small_tp_x=1.5, futures_small_tp_pct=5.0)
    bot, fake, _notes, clock = make_fbot(cfg, tmp_path, live=True)
    fake.calls.clear()
    pos = bot.open_futures(FuturesSignal("BTCUSDT", "long", 100.0, BASE_MS, 5.0, crash(12.0)), clock.t)
    tp_orders = [c for c in fake.calls if c[0] == "create_order" and c[2] == "limit"]
    assert len(tp_orders) == 1 and pos.tp_price == pytest.approx(pos.entry_price * 1.05, abs=0.011)
    assert any(isinstance(v, float) and v == pytest.approx(pos.tp_price) for v in tp_orders[0])   # Binance'e konan


# ====================================================================== Telegram
def test_exits_page_rows_and_status(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash"), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fx")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert "Küçük çöküşte Kâr Al:" not in text.split("<i>")[0]
    assert "kv:fx:FUTURES_SMALL_TP_X:1.5" in data and not any("FUTURES_SMALL_TP_PCT" in d for d in data)
    press(bot, "kv:fx:FUTURES_SMALL_TP_X:1.5")
    assert bot.cfg.futures_small_tp_x == 1.5 and "FUTURES_SMALL_TP_X=1.5" in env.read_text()
    text, keyboard, _mid = notes.menus[-1]
    lev = bot.cfg.futures_leverage
    assert (f"🎯 Küçük çöküşte Kâr Al: düşüş çöküş eşiğinin 1.5 katından azsa fiyat +%5 → ROE ~+%{5 * lev:.0f}"
            in text)
    assert "kv:fx:FUTURES_SMALL_TP_PCT:5.5" in [d for row in keyboard for _l, d in row]
    press(bot, "kv:fx:FUTURES_SMALL_TP_PCT:5.5")
    assert bot.cfg.futures_small_tp_pct == 5.5 and "FUTURES_SMALL_TP_PCT=5.5" in env.read_text()
