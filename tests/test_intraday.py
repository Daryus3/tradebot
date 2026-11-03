"""Gün içi al-sat (STRATEGY=intraday): yalnızca en büyük coinlerde 5 dk mumlarla, seçilebilen giriş taktikleri."""
import random
from dataclasses import replace

import pytest

import backtest
from backtest import (INTRADAY_KEYS, SweepRow, _advice_lines, ab_combos, entry_combos, simulate, strategy_combos,
                      sweep_combos)
from config import (DEFAULT_INTRADAY_COINS, INTRADAY_PROFILE, INTRADAY_TACTICS, ConfigError, _setting_notes,
                    intraday_coin_list, intraday_tactic_list, strategy_family, with_strategy)
from risk import ExitReason, _backup_floor
from strategy import INTRADAY_TACTIC_LABELS, IntradayStrategy, Signal, make_strategy

from test_backtest import DAYS, END_TIME_MS, N_BARS, flat, make_client, no_pause, to_raw  # noqa: F401 - fikstürler
from test_strategy import kline

DAY = 86_400_000
MIDNIGHT = 1_699_920_000_000          # 2023-11-14 00:00 UTC


def day_cfg(cfg, **kw):
    day, _ = with_strategy(cfg, "intraday")
    return replace(day, **kw)


def walk(n=6000, seed=7, t0=MIDNIGHT):
    """Tekrarlanabilir rastgele yürüyüş (5 dk), hareketli mumlarda hacim artar."""
    rnd = random.Random(seed)
    rows, t, p = [], t0, 30_000.0
    for _ in range(n):
        o = p
        p *= 1 + rnd.gauss(0.00002, 0.0025)
        h = max(o, p) * (1 + abs(rnd.gauss(0, 0.001)))
        lo = min(o, p) * (1 - abs(rnd.gauss(0, 0.001)))
        v = rnd.uniform(5e5, 3e6) * (3 if abs(p / o - 1) > 0.004 else 1)
        rows.append(kline(t, o, h, lo, p, vol=v, taker=v / 2))
        t += 300_000
    return rows


def window(rows, k, limit=400):
    """k. mum kapandığında canlı botun göreceği pencere: son (limit-1) kapanmış mum + açık mum (fiyat = kapanış)."""
    win = rows[max(0, k - limit + 2):k + 1]
    t, p = rows[k][0] + 300_000, rows[k][4]
    return win + [kline(t, p, p, p, p, vol=0.0, taker=0.0)]


# ------------------------------------------------------------------ ayarlar
def test_intraday_config_defaults_profile_and_validation(cfg, monkeypatch, tmp_path):
    assert "intraday" in backtest.STRATEGIES and strategy_family("intraday") == "intraday"
    assert cfg.intraday_coins == DEFAULT_INTRADAY_COINS and len(intraday_coin_list(cfg)) == 10
    assert intraday_tactic_list(cfg) == ("vwap",)
    assert intraday_tactic_list(replace(cfg, intraday_tactic="RSI2, vwap ,rsi2")) == ("rsi2", "vwap")
    assert intraday_coin_list(replace(cfg, intraday_coins="btcusdt, eth,ETH")) == ("BTC", "ETH")
    day, applied = with_strategy(replace(cfg, take_profit_pct=5.0, max_hold_minutes=120), "intraday")
    assert applied and day.take_profit_pct == INTRADAY_PROFILE["TAKE_PROFIT_PCT"] == 1.5
    assert day.stop_loss_pct == 1.5 and day.max_hold_minutes == 360 and day.max_24h_gain_pct == 25
    assert with_strategy(day, "intraday") == (day, False)
    assert _backup_floor(day) == 1.0          # 5 dk grubu gibi: yedek stop boyutlandırmaya katılmaz
    assert not _setting_notes(day)
    for bad in ({"intraday_tactic": "vwap,foo"}, {"intraday_tactic": " "}, {"intraday_coins": "BTC;ETH"},
                {"intraday_orb_minutes": 7}, {"intraday_band_std": 0.1}, {"intraday_breakout_bars": 5},
                {"intraday_min_atr_pct": 9.0}):
        with pytest.raises(ConfigError):
            replace(day, **bad).validate()
    monkeypatch.setenv("INTRADAY_TACTIC", "ORB,donchian")
    monkeypatch.setenv("INTRADAY_COINS", "BTC,ETH")
    from config import load_config
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert intraday_tactic_list(loaded) == ("orb", "donchian") and intraday_coin_list(loaded) == ("BTC", "ETH")


def test_intraday_setting_notes(cfg):
    day = day_cfg(cfg)
    assert any("VWAP_FILTER" in n for n in _setting_notes(replace(day, vwap_filter=True)))
    assert not _setting_notes(replace(day, vwap_filter=True, intraday_tactic="orb"))   # Kırılımda engel değil
    assert any("MAX_HOLD_MINUTES" in n for n in _setting_notes(replace(day, max_hold_minutes=0)))
    assert any("%5" in n for n in _setting_notes(replace(day, take_profit_pct=8.0, trailing_activation_pct=4.0,
                                                         trailing_floor_pct=2.0)))


# ------------------------------------------------------------------ taktikler
def _scan(day, rows, start=400, step=1):
    st = IntradayStrategy(None, day)
    out = []
    for k in range(start, len(rows) - 1, step):
        sig, _why = st.analyze("BTCUSDT", window(rows, k))
        if sig is not None:
            out.append(sig)
    return out


@pytest.mark.parametrize("tactic", INTRADAY_TACTICS)
def test_every_tactic_signals_and_respects_its_rule(cfg, tactic):
    day = day_cfg(cfg, intraday_tactic=tactic, intraday_trend_filter=False)
    signals = _scan(day, walk())
    assert signals, f"{tactic}: rastgele yürüyüşte hiç sinyal yok"
    for s in signals:
        m = s.metrics
        assert m["kind"] == "intraday" and m["tactic"] == tactic and m["atr_pct"] >= day.intraday_min_atr_pct
        assert s.note().startswith(INTRADAY_TACTIC_LABELS[tactic])
        if tactic == "vwap":
            assert m["z"] <= -day.intraday_band_std and m["room_pct"] > 0
        elif tactic in ("bollinger", "rsi2"):
            assert m["room_pct"] > 0 and (tactic == "bollinger" or m["rsi2"] <= day.intraday_rsi2_max)
        elif tactic in ("orb", "squeeze", "donchian"):
            assert m["breakout_pct"] > 0 and m["vol_ratio"] >= day.intraday_volume_ratio
        elif tactic == "ema_cross":
            assert 50 <= m["rsi"] <= 70
        elif tactic == "pullback":
            assert m["trend_pct"] > 0          # Geri çekilme taktiği trendi her zaman ister
        elif tactic == "dip":
            assert 0.15 <= m["retrace"] <= 0.6
    if tactic != "pullback":                    # Trend filtresi açıkken sinyaller yalnızca yükselen trendde
        trended = _scan(replace(day, intraday_trend_filter=True), walk())
        assert len(trended) <= len(signals) and all(s.metrics["trend_pct"] > 0 for s in trended)


def test_session_based_tactics_wait_for_the_day(cfg):
    rows = walk(800)
    early = next(k for k in range(400, 800) if (rows[k][0] // DAY) * DAY == rows[k][0] - 300_000 * 10)  # 11. mum
    st = IntradayStrategy(None, day_cfg(cfg, intraday_tactic="vwap"))
    assert st.analyze("BTCUSDT", window(rows, early)) == (None, "gün yeni başladı")
    orb = IntradayStrategy(None, day_cfg(cfg, intraday_tactic="orb"))
    assert orb.analyze("BTCUSDT", window(rows, early)) == (None, "açılış aralığı oluşmadı")
    assert st.analyze("BTCUSDT", window(rows, 300)[:200]) == (None, "veri az")


def test_tactic_list_takes_the_first_signal_in_order(cfg, monkeypatch):
    st = IntradayStrategy(None, day_cfg(cfg, intraday_tactic="rsi2,vwap"))
    rows = window(walk(500), 450)
    fake = Signal("BTCUSDT", 1.0, 3.0, {"kind": "intraday", "tactic": "vwap"})
    monkeypatch.setattr(st, "_t_rsi2", lambda *a: (None, "RSI(2) yüksek"))
    monkeypatch.setattr(st, "_t_vwap", lambda *a: (fake, ""))
    assert st.analyze("BTCUSDT", rows) == (fake, "")
    monkeypatch.setattr(st, "_t_vwap", lambda *a: (None, "VWAP'tan sapma az"))
    assert st.analyze("BTCUSDT", rows) == (None, "RSI(2) yüksek")   # İlk taktiğin nedeni


# ------------------------------------------------------------------ evren (hangi coinler)
def test_prefilter_keeps_only_the_listed_big_coins(cfg, fake_client):
    from exchange import parse_symbol_rules
    client = fake_client({"BTCUSDT": 30_000.0, "ETHUSDT": 2000.0, "PEPEUSDT": 1.0, "XRPUSDT": 0.6})
    rules = parse_symbol_rules(client.exchange_info(), "USDT")
    tickers = [{"symbol": s, "lastPrice": str(p), "priceChangePercent": ch, "quoteVolume": vol,
                "highPrice": str(p * 1.005), "lowPrice": str(p * 0.995)}   # 24 saat aralığı yalnızca %1
               for s, p, ch, vol in (("BTCUSDT", 30_000.0, "-1", "900000000"), ("ETHUSDT", 2000.0, "2", "400000000"),
                                     ("PEPEUSDT", 1.0, "9", "990000000"), ("XRPUSDT", 0.6, "-35", "300000000"))]
    st = make_strategy(client, day_cfg(cfg, intraday_coins="BTC,ETH,XRP"))
    assert st.bar_interval == "5m" and st.kline_limit == 400 and st.candidate_limit == 3
    # PEPE listede yok, XRP 24 saatte %35 düşmüş (MAX_24H_DROP_PCT dışı); 24 saat aralık filtresi uygulanmaz
    assert [t["symbol"] for t in st.prefilter(tickers, rules, set(), 0.0)] == ["BTCUSDT", "ETHUSDT"]
    auto = make_strategy(client, day_cfg(cfg, intraday_coins="", intraday_universe=2))
    assert auto.candidate_limit == 2
    assert [t["symbol"] for t in auto.prefilter(tickers, rules, set(), 0.0)] == ["PEPEUSDT", "BTCUSDT"]


def test_live_scan_requests_5m_klines_of_listed_coins(cfg, fake_client):
    rows = walk(600)

    class Client(fake_client):
        def __init__(self):
            super().__init__({"BTCUSDT": 30_000.0, "ETHUSDT": 30_000.0, "DOGEUSDT": 30_000.0})
            self.calls = []

        def tickers_24h(self):
            return [{"symbol": s, "lastPrice": "30000", "priceChangePercent": "0", "quoteVolume": "900000000",
                     "highPrice": "30300", "lowPrice": "29700"} for s in ("BTCUSDT", "ETHUSDT", "DOGEUSDT")]

        def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
            self.calls.append((symbol, interval, limit))
            return rows[-limit:]

    client = Client()
    from exchange import parse_symbol_rules
    st = make_strategy(client, day_cfg(cfg, intraday_coins="BTC,ETH", regime_filter=False, btc_filter=False))
    st.scan(parse_symbol_rules(client.exchange_info(), "USDT"), set(), 0.0)
    assert ("BTCUSDT", "5m", 400) in client.calls and ("ETHUSDT", "5m", 400) in client.calls
    assert not any(c[0] == "DOGEUSDT" for c in client.calls)


# ------------------------------------------------------------------ backtest
def _breakout_series():
    """Yatay (küçük dalga) → hacimli zirve kırılımı → yükseliş (+%0.4 / mum) → yatay."""
    bars = [(1.0, 1.003, 0.997, 1.0 if i % 2 else 0.999, 20_000.0, 10_000.0) for i in range(700)]
    bars.append((0.999, 1.0065, 0.999, 1.006, 60_000.0, 45_000.0))
    p = 1.006
    for _ in range(12):
        o, p = p, p * 1.004
        bars.append((o, p * 1.001, o * 0.999, p, 40_000.0, 25_000.0))
    bars += flat(N_BARS - len(bars), p)
    return to_raw(bars)


def test_backtest_downloads_and_trades_only_intraday_coins(cfg, make_client, tmp_path):
    series = {"AAAUSDT": _breakout_series(), "BBBUSDT": to_raw(flat(N_BARS, 1.0)),
              "ZZZUSDT": _breakout_series(), "BTCUSDT": to_raw(flat(N_BARS, 30_000.0, vol=50_000_000.0))}
    client = make_client(series)
    day = day_cfg(cfg, intraday_coins="AAA,BBB", intraday_tactic="donchian", intraday_trend_filter=False,
                  regime_filter=False, start_capital=100.0)
    data = backtest.prepare_backtest(day, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                                     end_time_ms=END_TIME_MS)
    assert set(data.series) == {"AAAUSDT", "BBBUSDT"} and data.btc is not None and data.interval == "5m"
    res = simulate(day, data)
    assert [t.symbol for t in res.trades] == ["AAAUSDT"]
    assert res.trades[0].reason in (ExitReason.TAKE_PROFIT.value, ExitReason.TRAILING_STOP.value)
    assert res.trades[0].pnl > 0
    # Simülasyon da canlı ön eleme gibi yalnızca INTRADAY_COINS'e bakar (veride başka coin olsa bile)
    assert not simulate(replace(day, intraday_coins="BBB"), data).trades


def test_intraday_sweeps_compare_tactics(cfg):
    day = day_cfg(cfg)
    exits = sweep_combos(day)
    assert {c["TAKE_PROFIT_PCT"] for c, _ in exits[1:]} == {0.8, 1.2, 2.0}
    assert {c["MAX_HOLD_MINUTES"] for c, _ in exits[1:]} == {120, 360, 720}
    entries = entry_combos(day, [3.0, 7.0])
    assert {c["INTRADAY_TACTIC"] for c, _ in entries} == set(INTRADAY_TACTICS)
    assert {c["MIN_SIGNAL_SCORE"] for c, _ in entries} == {day.min_signal_score}   # Puan ölçeği taktiğe özel
    combos = strategy_combos(day)
    assert [c["INTRADAY_TACTIC"] for c, _ in combos] == list(INTRADAY_TACTICS) and combos[0][1]
    assert any(c == {"INTRADAY_TREND_FILTER": False} for c, _ in ab_combos(day))
    assert not any("INTRADAY_TREND_FILTER" in c for c, _ in ab_combos(cfg))         # Yalnızca gün içinde
    row = SweepRow({"TAKE_PROFIT_PCT": 1.2}, False, 40, 55.0, 3.0, 0.1, 5.0, 1.0, 2.0, 1.2)
    lines = _advice_lines(replace(day, intraday_tactic="orb"), row)
    assert "STRATEGY=intraday" in lines and "INTRADAY_TACTIC=orb" in lines and "TAKE_PROFIT_PCT=1.2" in lines
    assert all(any(line.startswith(k + "=") for line in lines) for k in INTRADAY_KEYS)


# ------------------------------------------------------------------ Telegram / ayar değişikliği
def test_intraday_settings_can_be_changed_from_telegram_and_backtest_menu(cfg):
    from backtest_runner import PRESET_NAMES, PRESETS
    from control_panel import STRATEGY_SHORT
    from runtime_settings import apply_overrides, apply_settings, find_key
    day = day_cfg(cfg)
    new, _notes = apply_settings(day, {"INTRADAY_TACTIC": "rsi2,orb", "INTRADAY_TREND_FILTER": "kapalı"})
    assert intraday_tactic_list(new) == ("rsi2", "orb") and not new.intraday_trend_filter
    with pytest.raises(ConfigError):
        apply_settings(day, {"INTRADAY_TACTIC": "yok"})
    assert find_key("intraday_coins") == "INTRADAY_COINS"
    assert apply_overrides(day, {"INTRADAY_COINS": "BTC,ETH", "INTRADAY_TACTIC": "squeeze"}).intraday_coins == "BTC,ETH"
    assert "intraday" in STRATEGY_SHORT
    assert [o["INTRADAY_TACTIC"] for o in PRESETS["itac"][1]] == list(INTRADAY_TACTICS)
    for group in ("icoins", "itrend"):
        assert len(PRESET_NAMES[group]) == len(PRESETS[group][1])
