"""Alternatif giriş stratejileri: trend (yükselişte geri çekilme) ve breakout (zirve kırılımı)."""
from dataclasses import replace

import pytest

from config import ConfigError, load_config
from strategy import (BreakoutStrategy, ReboundStrategy, Signal, TrendPullbackStrategy, make_strategy,
                      room_ok)

from test_backtest import make_client, no_pause  # noqa: F401 - backtest fikstürleri (istek beklemesi kapalı)
from test_strategy import kline


def uptrend_pullback(n_trend=280, drift=1.0007, pull=(0.997, 8), up=(1.004, 4), t0=0):
    """Yükselen trend → ~%2.5 geri çekilme (EMA20'ye) → hacimli yeniden yükseliş → açık mum."""
    rows, t, p = [], t0, 100.0
    for i in range(n_trend):
        o = p
        p *= drift * (1.0015 if i % 2 == 0 else 0.9992)
        rows.append(kline(t, o, max(o, p) * 1.0015, min(o, p) * 0.9985, p))
        t += 300_000
    f, k = pull
    for _ in range(k):
        o = p
        p *= f
        rows.append(kline(t, o, o * 1.001, p * 0.999, p, vol=900))
        t += 300_000
    f, k = up
    for _ in range(k):
        o = p
        p *= f
        rows.append(kline(t, o, p * 1.001, o * 0.999, p, vol=2200, taker=1500))
        t += 300_000
    rows.append(kline(t, p, p * 1.001, p * 0.9995, p * 1.0005, vol=300))
    return rows


def breakout_rows(n=290, lookback=48, vol=4000.0, jump=1.006):
    """Yükselen trend → son 4 saat yatay → hacimli zirve kırılımı → açık mum."""
    rows, t, p = [], 0, 100.0
    for i in range(n):
        o = p
        p *= 1.0007 * (1.0015 if i % 2 == 0 else 0.9992)
        if i >= n - lookback:
            p = o * (1.002 if i % 2 == 0 else 0.998)
        rows.append(kline(t, o, max(o, p) * 1.0015, min(o, p) * 0.9985, p))
        t += 300_000
    hi = max(float(r[2]) for r in rows[-lookback:])
    o, p = p, hi * jump
    rows.append(kline(t, o, p * 1.001, o * 0.999, p, vol=vol, taker=vol * 0.75))
    t += 300_000
    rows.append(kline(t, p, p * 1.001, p * 0.9995, p * 1.0005, vol=300))
    return rows


def mirror(rows):
    """Fiyatları ters çevirir (yükseliş → düşüş) — trend yokken sinyal çıkmamalı."""
    out = []
    for r in rows:
        o, h, lo, c = (200 - float(x) for x in r[1:5])
        out.append([r[0], str(o), str(max(h, lo)), str(min(h, lo)), str(c), *r[5:]])
    return out


# ------------------------------------------------------------------ trend
def test_trend_pullback_signal(cfg):
    signal, why = TrendPullbackStrategy(None, cfg).analyze("ABCUSDT", uptrend_pullback())
    assert signal is not None, why
    m = signal.metrics
    assert m["kind"] == "trend" and m["trend_pct"] > 0 and 1 <= m["pullback_pct"] <= 5 and m["room_pct"] > 0
    assert "Geri çekilme" in signal.note() and "Trend" in signal.note()


@pytest.mark.parametrize("rows, reason", [
    (lambda: mirror(uptrend_pullback()), "trend yok"),
    (lambda: uptrend_pullback(pull=(0.990, 8)), "geri çekilme derin"),
    (lambda: uptrend_pullback(pull=(0.9995, 8)), "geri çekilme az"),
    (lambda: uptrend_pullback()[:-5], "son mum kırmızı"),
    (lambda: uptrend_pullback()[:120], "veri az"),
])
def test_trend_pullback_rejections(cfg, rows, reason):
    signal, why = TrendPullbackStrategy(None, cfg).analyze("ABCUSDT", rows())
    assert signal is None
    if reason == "son mum kırmızı":   # Geri çekilme hâlâ sürüyor: farklı ret nedenleri olabilir
        assert why in {"son mum kırmızı", "geri çekilme sürüyor", "momentum yok", "RSI yükselmiyor"}
    else:
        assert why == reason


def test_trend_strict_mode_needs_more(cfg):
    weak_volume = replace(cfg, min_volume_ratio=1.8)    # x1.3 sıkı modda 2.34 > ~2.16
    assert TrendPullbackStrategy(None, weak_volume).analyze("ABCUSDT", uptrend_pullback())[0] is not None
    signal, why = TrendPullbackStrategy(None, weak_volume).analyze("ABCUSDT", uptrend_pullback(), strict=True)
    assert signal is None and why == "hacim zayıf"


# ------------------------------------------------------------------ breakout
def test_breakout_signal(cfg):
    signal, why = BreakoutStrategy(None, cfg).analyze("ABCUSDT", breakout_rows())
    assert signal is not None, why
    m = signal.metrics
    assert m["kind"] == "breakout" and m["breakout_pct"] > 0 and m["vol_ratio"] >= 2
    assert "room_pct" not in m and "Kırılım" in signal.note() and "240 dk" in signal.note()
    assert room_ok(signal, replace(cfg, min_room_ratio=5))      # Kırılımda hedef alanı filtresi uygulanmaz


@pytest.mark.parametrize("kwargs, reason", [
    ({"vol": 1200.0}, "hacim zayıf"),
    ({"jump": 0.998}, "kırılım yok"),
    ({"jump": 1.05}, "aşırı alım"),
])
def test_breakout_rejections(cfg, kwargs, reason):
    signal, why = BreakoutStrategy(None, cfg).analyze("ABCUSDT", breakout_rows(**kwargs))
    assert signal is None and why == reason


def test_breakout_does_not_chase_extended_moves(cfg, monkeypatch):
    import strategy
    monkeypatch.setattr(strategy, "BREAKOUT_MAX_RSI", 101.0)        # RSI engelini kaldır, uzama kontrolü kalsın
    signal, why = BreakoutStrategy(None, cfg).analyze("ABCUSDT", breakout_rows(jump=1.05))
    assert signal is None and why == "fazla uzamış"


def test_breakout_stale_and_downtrend(cfg):
    rows = breakout_rows()
    last_closed = rows[-2]
    p = float(last_closed[4])
    stale = rows[:-1] + [kline(last_closed[0] + 300_000, p, p * 1.004, p * 0.999, p * 1.003, vol=4000),
                         kline(last_closed[0] + 600_000, p * 1.003, p * 1.004, p * 1.002, p * 1.003)]
    signal, why = BreakoutStrategy(None, cfg).analyze("ABCUSDT", stale)
    assert signal is None and why in {"kırılım eski", "hacim zayıf"}
    assert BreakoutStrategy(None, cfg).analyze("ABCUSDT", mirror(breakout_rows()))[1] == "trend yok"


# ------------------------------------------------------------------ evren / fabrika / ayarlar
def test_universe_and_ordering(cfg, fake_client):
    from exchange import parse_symbol_rules
    rules = parse_symbol_rules(fake_client({s: 1.0 for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")})
                               .exchange_info())

    def t(sym, change):
        return {"symbol": sym, "lastPrice": "1.0", "priceChangePercent": str(change), "quoteVolume": "9000000",
                "highPrice": "1.1", "lowPrice": "0.95"}
    tickers = [t("AAAUSDT", -8), t("BBBUSDT", 5), t("CCCUSDT", 15), t("DDDUSDT", 40)]
    reb = [x["symbol"] for x in ReboundStrategy(None, cfg).prefilter(tickers, rules, set(), 11)]
    trend = [x["symbol"] for x in TrendPullbackStrategy(None, cfg).prefilter(tickers, rules, set(), 11)]
    assert reb == ["AAAUSDT"]
    assert trend == ["CCCUSDT", "BBBUSDT"]               # +%40 aşırı pompalanmış: MAX_24H_GAIN_PCT=25 dışında


def test_factory_and_config(cfg, monkeypatch, tmp_path):
    assert type(make_strategy(None, cfg)) is ReboundStrategy
    assert type(make_strategy(None, replace(cfg, strategy="trend"))) is TrendPullbackStrategy
    assert type(make_strategy(None, replace(cfg, strategy="breakout"))) is BreakoutStrategy
    assert TrendPullbackStrategy.kline_limit == BreakoutStrategy.kline_limit == 300
    monkeypatch.setenv("STRATEGY", "Trend")
    assert load_config(env_file=str(tmp_path / "x.env"), mode_override="paper").strategy == "trend"
    for key, value in (("STRATEGY", "scalp"), ("MIN_24H_GAIN_PCT", "30"), ("TREND_PULLBACK_MAX_PCT", "0.5"),
                       ("BREAKOUT_LOOKBACK_BARS", "5")):
        monkeypatch.setenv(key, value)
        with pytest.raises(ConfigError):
            load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
        monkeypatch.delenv(key)
    monkeypatch.delenv("STRATEGY", raising=False)


def test_live_scan_uses_strategy_kline_limit(cfg, fake_client):
    from test_new_features import scan_rules

    class Client:
        def __init__(self):
            self.limits = []

        def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
            if symbol == "BTCUSDT":
                return [kline(i * 300_000, 100, 100, 100, 100) for i in range(13)]
            self.limits.append(limit)
            return uptrend_pullback()

        def tickers_24h(self):
            return [{"symbol": "ABCUSDT", "lastPrice": "1.0", "priceChangePercent": "6", "quoteVolume": "9000000",
                     "highPrice": "1.06", "lowPrice": "0.97"}]
    client = Client()
    result = make_strategy(client, replace(cfg, strategy="trend")).scan(scan_rules(fake_client), set(), 11)
    assert client.limits == [300]
    assert [s.symbol for s in result.signals] == ["ABCUSDT"] and result.signals[0].metrics["kind"] == "trend"


def test_signal_note_falls_back_to_rebound():
    assert "Düşüş" in Signal("X", 1, 1, {"dip_pct": 5}).note()


# ------------------------------------------------------------------ backtest
def _trend_series(drift=1.0003):
    """test_backtest zaman çizelgesinde: uzun yükseliş → geri çekilme → yeniden yükseliş → yükselişin devamı."""
    from test_backtest import N_BARS, to_raw, trend
    rows = uptrend_pullback(n_trend=600, drift=drift)[:-1]          # Açık mum hariç
    # Fiyat ~1 USDT'ye ölçeklenir (100 USDT'lik coinde 0.01'lik LOT adımı toz filtresine takılırdı)
    bars = [(float(r[1]) / 100, float(r[2]) / 100, float(r[3]) / 100, float(r[4]) / 100, float(r[7]) * 40,
             float(r[10]) * 40) for r in rows]
    tail, _ = trend(bars[-1][3], 1.004, N_BARS - len(bars), vol=60_000.0)
    return to_raw(bars + tail)


def test_backtest_runs_trend_strategy_and_comparison(cfg, make_client, tmp_path):
    from backtest import format_backtest_report, format_sweep_report, prepare_backtest, run_sweep, strategy_combos
    from test_backtest import DAYS, END_TIME_MS, btc_flat, run
    series = {"ABCUSDT": _trend_series(), "BTCUSDT": btc_flat()}
    trend_cfg = replace(cfg, strategy="trend")
    result = run(trend_cfg, make_client(series), tmp_path)
    assert result.trades, result.stats
    assert "Strateji: trend" in format_backtest_report(result, trend_cfg)
    assert run(cfg, make_client(series), tmp_path).trades == []     # rebound yükselen coine bakmaz

    data = prepare_backtest(cfg, make_client(series), days=DAYS, cache_dir=tmp_path / "c", progress=None,
                            symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)
    combos = strategy_combos(cfg)
    assert [st["STRATEGY"] for st, _ in combos] == ["rebound", "trend", "breakout"] and combos[0][1]
    cache = {}
    rows = run_sweep(cfg, data, combos=combos, signal_cache=cache)
    by = {r.settings["STRATEGY"]: r for r in rows}
    assert by["trend"].trades >= 1 and by["rebound"].trades == 0
    assert {k[0] for k in cache} <= {"rebound", "trend", "breakout"}   # Önbellek stratejiye göre ayrı
    text = format_sweep_report(rows, cfg, data, kind="strateji")
    assert "STRATEJİ KARŞILAŞTIRMASI" in text and "Strateji" in text and "--strateji-karsilastir --bitis" in text


def test_entry_sweep_uses_gain_range_for_trend(cfg):
    from backtest import entry_combos, entry_keys
    trend_cfg = replace(cfg, strategy="trend")
    assert "MAX_24H_GAIN_PCT" in entry_keys(trend_cfg) and "MAX_24H_DROP_PCT" not in entry_keys(trend_cfg)
    combos = entry_combos(trend_cfg, [0.0])
    assert {c["MAX_24H_GAIN_PCT"] for c, _ in combos} == {25.0, 12.0, 6.0}
    brk = entry_combos(replace(cfg, strategy="breakout"), [0.0])
    assert {c["MIN_ROOM_RATIO"] for c, _ in brk} == {0.0}             # Kırılımda alan filtresi denenmez


def test_sweep_suggests_strategy_line_when_overridden(cfg):
    from backtest import BacktestData, SweepRow, format_sweep_report
    data = BacktestData(series={}, btc=None, regime=None, min_notional=5, start_ms=0, end_ms=10 * 86_400_000,
                        n=2880, k_start=288, days=9, notes=[])
    st = {"TAKE_PROFIT_PCT": 3.0, "STOP_LOSS_PCT": 2.0, "TRAILING_ACTIVATION_PCT": 2.0, "TRAILING_FLOOR_PCT": 1.0,
          "MAX_HOLD_MINUTES": 120, "MIN_ROOM_RATIO": 0.0}
    strong = SweepRow(st, False, 150, 70, 8.0, 0.9, 5, 4.0, 4.0, 2.0, se_net_pct=0.1)
    base = SweepRow({**st, "TAKE_PROFIT_PCT": 5.0}, True, 100, 50, -1, -0.2, 9, -0.5, -0.5, 0.8, se_net_pct=0.2)
    text = format_sweep_report([base, strong], replace(cfg, strategy="breakout"), data)
    lines = [ln.strip() for ln in text.splitlines()]
    assert "STRATEGY=breakout" in lines and "TAKE_PROFIT_PCT=3" in lines
    assert "--tara --strateji breakout --bitis" in text


def test_cli_strategy_override_and_comparison(cfg, make_client, tmp_path, monkeypatch, capsys):
    import backtest
    import binance_client
    import main
    from test_backtest import END_TIME_MS, btc_flat
    client = make_client({"ABCUSDT": _trend_series(), "BTCUSDT": btc_flat()})
    monkeypatch.setattr(binance_client, "BinanceClient", lambda *a, **k: client)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    real_prepare, seen = backtest.prepare_backtest, {}

    def prepare(c, cl, days, max_symbols, end_time_ms=None):
        seen["strategy"] = c.strategy
        return real_prepare(c, cl, days, max_symbols, cache_dir=tmp_path / "c", progress=None,
                            symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)
    monkeypatch.setattr(backtest, "prepare_backtest", prepare)
    args = main.parse_args(["--backtest", "--gun", "2", "--strateji", "trend", "--strateji-karsilastir"])
    assert main.run_backtest_cli(cfg, args) == main.EXIT_OK
    out = capsys.readouterr().out
    assert seen["strategy"] == "trend" and "STRATEJİ KARŞILAŞTIRMASI" in out
    assert list(tmp_path.glob("tarama_strateji_*.csv"))
    assert main.parse_args(["--backtest"]).strateji is None
