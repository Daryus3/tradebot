"""Uzun vadeli (swing) stratejiler: 1 saatlik mumlar, çok günlük trend, 1-5 gün tutulan işlemler."""
from dataclasses import replace

import pytest

import backtest
from backtest import (HOUR_MS, BacktestError, entry_combos, prepare_backtest, run_sweep, simulate,
                      strategy_combos, sweep_combos)
from config import SWING_EXIT_PROFILE, ConfigError, _setting_notes, load_config, with_strategy
from risk import ExitReason
from strategy import SwingBreakoutStrategy, SwingPullbackStrategy, make_strategy
from telegram_bot import fmt_duration

from test_backtest import aggregate, no_pause  # noqa: F401 - 4s/1g birleştirme, istek beklemesi kapalı

H = HOUR_MS
END_TIME_MS = 1_700_000_000_000
END_MS = END_TIME_MS // H * H
DAYS = 10
START_MS = END_MS - (DAYS + 18) * 24 * H      # 18 gün ısınma (400 saatlik analiz geçmişi)
N_BARS = (DAYS + 18) * 24
BREAKOUT_AT = 522                             # Simülasyon 432. mumda başlar


def k(t, o, h, l, c, vol=200_000.0, taker=None):
    taker = vol * 0.5 if taker is None else taker
    return [t, str(o), str(h), str(l), str(c), "0", t + H - 1, str(vol), 10, "0", str(taker), "0"]


def uptrend(n, drift=1.002, t=0, p=100.0, vol=200_000.0):
    """Dalgalı yükseliş trendi (saatlik ATR ≈ %1.5)."""
    rows = []
    for i in range(n):
        o = p
        p *= drift * (1.008 if i % 2 == 0 else 0.993)
        rows.append(k(t, o, max(o, p) * 1.004, min(o, p) * 0.996, p, vol))
        t += H
    return rows, t, p


def flat(n, t, p, vol=200_000.0, swing=0.006):
    rows = []
    base = p
    for i in range(n):
        o = p
        p = base * (1 + swing if i % 2 == 0 else 1 - swing)
        rows.append(k(t, o, max(o, p) * 1.003, min(o, p) * 0.997, p, vol))
        t += H
    return rows, t, p


def breakout_rows(vol=800_000.0, jump=1.012, n=320, t=0, p0=100.0):
    """Çok günlük yükseliş → 3 gün yatay → son kapanan mumda hacimli zirve kırılımı → açık mum."""
    rows, t, p = uptrend(n, t=t, p=p0)
    more, t, p = flat(72, t, p)
    rows += more
    hi = max(float(r[2]) for r in rows[-72:])
    o, p = p, hi * jump
    rows.append(k(t, o, p * 1.002, o * 0.998, p, vol=vol, taker=vol * 0.7))
    t += H
    rows.append(k(t, p, p * 1.001, p * 0.999, p * 1.001, vol=30_000))
    return rows


def pullback_rows(pull=0.992, bars=8, up=1.03):
    """Güçlü yükseliş → ~%7 geri çekilme (EMA50'ye) → hacimli dönüş mumu → açık mum."""
    rows, t, p = uptrend(320, drift=1.003)
    for _ in range(bars):
        o = p
        p *= pull
        rows.append(k(t, o, o * 1.002, p * 0.997, p, vol=160_000))
        t += H
    o = p
    p = max(float(rows[-1][2]) * 1.004, p * up)
    rows.append(k(t, o, p * 1.002, o * 0.999, p, vol=500_000, taker=350_000))
    t += H
    rows.append(k(t, p, p * 1.001, p * 0.999, p * 1.0005, vol=30_000))
    return rows


def mirror(rows):
    out = []
    for r in rows:
        o, h, lo, c = (400 - float(x) for x in r[1:5])
        out.append([r[0], str(o), str(lo), str(h), str(c)] + r[5:])
    return out


# ---------------------------------------------------------------- sinyaller
def test_swing_breakout_signal(cfg):
    strat = SwingBreakoutStrategy(None, cfg)
    sig, why = strat.analyze("ABCUSDT", breakout_rows()[-400:])
    assert sig is not None, why
    m = sig.metrics
    assert m["kind"] == "swing" and m["breakout_pct"] > 0 and m["momentum_pct"] > 0 and m["lookback_hours"] == 72
    assert "room_pct" not in m and "72 saatlik zirve kırılımı" in sig.note()


def test_swing_breakout_rejections(cfg):
    strat = SwingBreakoutStrategy(None, cfg)
    assert strat.analyze("X", breakout_rows(vol=250_000)[-400:])[1] == "hacim zayıf"
    assert strat.analyze("X", breakout_rows(jump=0.99)[-400:])[1] == "kırılım yok"
    assert strat.analyze("X", mirror(breakout_rows())[-400:])[0] is None          # Düşüş trendinde alım yok
    assert strat.analyze("X", breakout_rows()[-200:])[1] == "veri az"
    # Kırılımdan bir saat sonra (bir önceki mum da zirvenin üstünde kapanmışsa) artık taze değil
    rows = breakout_rows()
    p = float(rows[-2][4])
    late = rows[:-1] + [k(rows[-1][0], p, p * 1.01, p * 0.999, p * 1.006, vol=800_000),
                        k(rows[-1][0] + H, p * 1.006, p * 1.007, p * 1.005, p * 1.006)]
    assert strat.analyze("X", late[-400:])[1] == "kırılım eski"
    # Zirve penceresi analizi değiştirir: backtest önbelleği farklı pencereleri ayrı tutmalı
    long_cfg = replace(cfg, swing_lookback_hours=168)
    assert SwingBreakoutStrategy(None, long_cfg).analysis_key() != strat.analysis_key()


def test_swing_pullback_signal_and_rejections(cfg):
    strat = SwingPullbackStrategy(None, cfg)
    sig, why = strat.analyze("ABCUSDT", pullback_rows()[-400:])
    assert sig is not None, why
    m = sig.metrics
    assert m["kind"] == "swing_pullback" and 4 <= m["pullback_pct"] <= 12 and m["room_pct"] > 0
    assert m["rsi_min"] <= 45 < m["rsi"] and "Zirveden geri çekilme" in sig.note()
    assert strat.analyze("X", pullback_rows(pull=0.998)[-400:])[1] == "geri çekilme az"
    assert strat.analyze("X", pullback_rows(pull=0.98, bars=10)[-400:])[1] == "geri çekilme derin"
    assert strat.analyze("X", mirror(pullback_rows())[-400:])[0] is None


def test_swing_universe_and_live_scan_uses_hourly_klines(cfg, fake_client):
    swing = make_strategy(None, replace(cfg, strategy="swing"))
    pb = make_strategy(None, replace(cfg, strategy="swing_pullback"))
    assert swing.interval == swing.bar_interval == "1h" and swing.kline_limit == 400
    assert swing.candidate_limit == 3 * cfg.max_candidates and not swing.uses_room and pb.uses_room
    assert swing.change_ok(3.0) and not swing.change_ok(-1.0) and not swing.change_ok(cfg.max_24h_gain_pct + 1)
    assert pb.change_ok(-8.0) and not pb.change_ok(-cfg.swing_pullback_max_pct - 1)
    assert pb.candidate_key(-8.0) < pb.candidate_key(2.0)       # En çok geri çekilen önce incelenir

    rows = breakout_rows()

    class Client(fake_client):
        def __init__(self):
            super().__init__({"ABCUSDT": float(rows[-1][4]), "BTCUSDT": 30_000.0})
            self.calls = []

        def tickers_24h(self):
            p = float(rows[-1][4])
            # 24 saatlik aralık %4'ün altında: 5 dk stratejileri elerdi, swing elemez (sıkışma sonrası kırılım)
            return [{"symbol": "ABCUSDT", "lastPrice": str(p), "priceChangePercent": "2.5", "quoteVolume": "9000000",
                     "highPrice": str(p * 1.001), "lowPrice": str(p * 0.98)}]

        def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
            self.calls.append((symbol, interval, limit))
            if symbol == "BTCUSDT":
                return [k(i * 300_000, 30_000, 30_000, 30_000, 30_000) for i in range(limit)]
            return rows[-limit:]

    client = Client()
    from exchange import parse_symbol_rules
    rules = parse_symbol_rules(client.exchange_info(), "USDT")
    live = make_strategy(client, replace(cfg, strategy="swing", regime_filter=False))
    result = live.scan(rules, set(), 0.0)    # 0 = toz kontrolü yok (sentetik fiyat ~200, LOT adımı 0.01)
    assert ("ABCUSDT", "1h", 400) in client.calls
    assert [s.symbol for s in result.signals] == ["ABCUSDT"], result.rejects
    # Aynı veriyle 5 dk stratejisi 24 saatlik aralık filtresinde eler
    assert make_strategy(client, replace(cfg, strategy="breakout")).prefilter(
        client.tickers_24h(), rules, set(), 0.0) == []


# ---------------------------------------------------------------- ayarlar
def _load(tmp_path, monkeypatch, text):
    """Ayarları ortam değişkeni olarak verir (monkeypatch test sonunda geri alır; .env okumak os.environ'u kirletir)."""
    for line in text.strip().splitlines():
        key, value = line.split("=", 1)
        monkeypatch.setenv(key, value)
    try:
        return load_config(env_file=str(tmp_path / "yok.env"), mode_override="paper")
    finally:
        for line in text.strip().splitlines():
            monkeypatch.delenv(line.split("=", 1)[0], raising=False)


def test_swing_config_validation_and_notes(tmp_path, monkeypatch):
    c = _load(tmp_path, monkeypatch, "STRATEGY=swing_pullback\nSWING_LOOKBACK_HOURS=120\n")
    assert c.strategy == "swing_pullback" and c.swing_lookback_hours == 120
    with pytest.raises(ConfigError, match="SWING_LOOKBACK_HOURS"):
        _load(tmp_path, monkeypatch, "SWING_LOOKBACK_HOURS=12\n")
    with pytest.raises(ConfigError, match="SWING_PULLBACK_MIN_PCT"):
        _load(tmp_path, monkeypatch, "SWING_PULLBACK_MIN_PCT=8\nSWING_PULLBACK_MAX_PCT=6\n")
    with pytest.raises(ConfigError, match="STRATEGY"):
        _load(tmp_path, monkeypatch, "STRATEGY=scalp\n")
    # Uzun vadeli strateji + kısa vadeli çıkışlar → açılışta uyarı (önerilen değerlerle)
    short = _load(tmp_path, monkeypatch, "STRATEGY=swing\nTAKE_PROFIT_PCT=5\nSTOP_LOSS_PCT=3.5\n"
                                         "TRAILING_ACTIVATION_PCT=2\nTRAILING_FLOOR_PCT=1\nMAX_HOLD_MINUTES=240\n"
                                         "BREAKEVEN_TRIGGER_PCT=1\n")
    note = next(n for n in short.notes if "STRATEGY=swing" in n)
    assert "MAX_HOLD_MINUTES=240" in note and "TAKE_PROFIT_PCT=12" in note and "BREAKEVEN_TRIGGER_PCT=1 " not in note
    good, _ = with_strategy(replace(short, strategy="rebound"), "swing")
    assert not any("STRATEGY=swing" in n for n in _setting_notes(good))


def test_with_strategy_applies_exit_profile_only_when_horizon_changes(cfg):
    short = replace(cfg, take_profit_pct=5, stop_loss_pct=3.5, max_hold_minutes=240, min_signal_score=30)
    swing, applied = with_strategy(short, "swing")
    assert applied and swing.strategy == "swing"
    assert (swing.take_profit_pct, swing.stop_loss_pct, swing.max_hold_minutes) == (12.0, 6.0, 7200)
    assert swing.min_signal_score == 0 and isinstance(swing.max_hold_minutes, int)
    # Zaten uzun vadeli bir .env: çıkışlar kullanıcınındır, yalnızca strateji değişir
    custom = replace(swing, take_profit_pct=9.0)
    pb, applied = with_strategy(custom, "swing_pullback")
    assert not applied and pb.take_profit_pct == 9.0 and pb.strategy == "swing_pullback"
    assert with_strategy(short, "trend") == (replace(short, strategy="trend"), False)
    assert with_strategy(short, "rebound") == (short, False)


# ---------------------------------------------------------------- backtest (1 saatlik mumlar)
class HourClient:
    def __init__(self, fake_client, series):
        class C(fake_client):
            def tickers_24h(inner):
                return [{"symbol": s, "quoteVolume": "5000000"} for s in inner.prices]

            def _request(inner, method, path, params=None, signed=False):
                inner.intervals.append(params["interval"])
                if params["startTime"] == 0:   # Listelenme günü: series'in ilk mumu (yoksa çok eski)
                    first = inner.listed.get(params["symbol"], 1_500_000_000_000)
                    return [[first, "1", "1", "1", "1", "0", first + 24 * H - 1, "0", 0, "0", "0", "0"]]
                rows = series[params["symbol"]]
                if params["interval"] != "1h":
                    rows = aggregate(rows, {"4h": 4 * H, "1d": 24 * H}[params["interval"]])
                out = [r for r in rows if params["startTime"] <= r[0] <= params["endTime"]]
                return out[: params["limit"]]
        self.client = C({s: 1.0 for s in series})
        self.client.intervals = []
        self.client.listed = {}


def _swing_series():
    rows = breakout_rows(n=BREAKOUT_AT - 72, t=START_MS, p0=1.0)   # Düşük fiyat: LOT adımı (0.01) toz yaratmasın
    rows = rows[:-1]                                  # Sentetik açık mumu at
    t, p = rows[-1][0] + H, float(rows[-1][4])
    for _ in range(16):                               # Kırılımdan sonra yükseliş: Kâr Al (+%12)
        o = p
        p *= 1.01
        rows.append(k(t, o, p * 1.002, o * 0.999, p))
        t += H
    while len(rows) < N_BARS:
        rows.append(k(t, p, p * 1.003, p * 0.997, p))
        t += H
    btc = [k(START_MS + i * H, 30_000, 30_030, 29_970, 30_000, vol=5e8) for i in range(N_BARS)]
    return {"ABCUSDT": rows, "BTCUSDT": btc}


def _prepare(cfg, client, tmp_path, **kw):
    return prepare_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                            symbols=["ABCUSDT"], end_time_ms=END_TIME_MS, **kw)


def test_swing_backtest_uses_hourly_data_and_exit_profile(cfg, fake_client, tmp_path):
    swing, _ = with_strategy(replace(cfg, max_hold_minutes=240, take_profit_pct=5, stop_loss_pct=3.5,
                                     trailing_activation_pct=2, trailing_floor_pct=1), "swing")
    hc = HourClient(fake_client, _swing_series())
    data = _prepare(swing, hc.client, tmp_path)
    assert data.interval == "1h" and data.step_ms == H and data.n == N_BARS and data.k_start == N_BARS - DAYS * 24
    assert data.start_ms == START_MS and "5m" not in hc.client.intervals and "1h" in hc.client.intervals
    assert backtest.cache_path(tmp_path, "ABCUSDT", START_MS, END_MS, "1h").name.startswith("ABCUSDT_1h_")

    result = simulate(swing, data)
    assert result.trades, result.stats
    t = result.trades[0]
    assert t.open_time_ms == START_MS + (BREAKOUT_AT + 1) * H        # Kırılım mumunun kapanışında alındı
    assert t.reason == ExitReason.TAKE_PROFIT.value and t.pnl_pct >= 11.99
    assert t.duration_sec >= 10 * 3600 and result.interval == "1h"
    report = backtest.format_backtest_report(result, swing)
    assert "1 saatlik mumlar" in report and "zirve penceresi 72 saat" in report and "Maks. süre 5 gün" in report

    # 1 saatlik veri 5 dk stratejisiyle simüle edilemez (yanlış sonuç yerine açık hata)
    with pytest.raises(BacktestError, match="1h"):
        simulate(replace(swing, strategy="rebound"), data)


def test_swing_sweeps_use_long_horizon_grids(cfg, fake_client, tmp_path):
    swing, _ = with_strategy(cfg, "swing")
    rows = sweep_combos(swing)
    tps = {c["TAKE_PROFIT_PCT"] for c, _ in rows[1:]}
    holds = {c["MAX_HOLD_MINUTES"] for c, _ in rows[1:]}
    assert tps == {8.0, 12.0, 15.0} and holds == {2880, 7200}
    assert {c["MIN_ROOM_RATIO"] for c, _ in rows} == {0.0}            # Kırılımda alan filtresi denenmez
    assert len(rows) == 31 and rows[0][1]
    assert {c["TAKE_PROFIT_PCT"] for c, _ in sweep_combos(cfg)[1:]} == {2.0, 3.0, 5.0}   # 5 dk aynen

    entries = entry_combos(swing, [0.0, 20.0])
    assert {c["SWING_LOOKBACK_HOURS"] for c, _ in entries} == {48, 72, 168}
    assert "MAX_24H_DROP_PCT" not in entries[0][0] and "MAX_24H_GAIN_PCT" not in entries[0][0]
    assert [st["STRATEGY"] for st, _ in strategy_combos(swing)] == ["swing", "swing_pullback"]
    assert [st["STRATEGY"] for st, _ in strategy_combos(cfg)] == ["rebound", "trend", "breakout"]

    # Farklı zirve pencereleri ortak önbellekte birbirinin sinyalini kullanmaz
    data = _prepare(swing, HourClient(fake_client, _swing_series()).client, tmp_path)
    cache = {}
    res = run_sweep(swing, data, combos=[({"SWING_LOOKBACK_HOURS": 72}, True), ({"SWING_LOOKBACK_HOURS": 240}, False)],
                    signal_cache=cache)
    assert len({key[1] for key in cache}) == 2 and res[0].trades >= 1


def test_swing_sweep_advice_includes_all_exit_lines(cfg):
    from backtest import BacktestData, SweepRow, format_sweep_report
    swing, _ = with_strategy(replace(cfg, backup_stop_pct=7.0), "swing")
    data = BacktestData(series={}, btc=None, regime=None, min_notional=5, start_ms=0, end_ms=400 * 86_400_000,
                        n=9600, k_start=432, days=365, notes=[], interval="1h")
    st = {"TAKE_PROFIT_PCT": 15.0, "STOP_LOSS_PCT": 8.0, "TRAILING_ACTIVATION_PCT": 10.0, "TRAILING_FLOOR_PCT": 5.0,
          "MAX_HOLD_MINUTES": 2880, "MIN_ROOM_RATIO": 0.0}
    strong = SweepRow(st, False, 150, 60, 30.0, 2.5, 10, 15.0, 15.0, 2.0, se_net_pct=0.2)
    base = SweepRow({**st, "TAKE_PROFIT_PCT": 12.0}, True, 100, 50, -1, -0.2, 9, -0.5, -0.5, 0.8, se_net_pct=0.2)
    text = format_sweep_report([base, strong], swing, data)
    lines = [ln.strip() for ln in text.splitlines()]
    for line in ("STRATEGY=swing", "TAKE_PROFIT_PCT=15", "MAX_HOLD_MINUTES=2880", "BREAKEVEN_TRIGGER_PCT=5",
                 "BACKUP_STOP_PCT=12"):
        assert line in lines, line
    assert "1 saatlik mumlar" in text and "--gun 365 --strateji swing --bitis" in text
    assert "1440 = 1 gün" in text


def test_cli_swing_defaults_to_one_year_with_profile(cfg, fake_client, tmp_path, monkeypatch, capsys):
    import binance_client
    import main
    monkeypatch.setattr(binance_client, "BinanceClient", lambda *a, **k: object())
    seen = {}

    def fake_run(c, client, days, max_symbols, end_time_ms=None):
        seen.update(strategy=c.strategy, days=days, tp=c.take_profit_pct, hold=c.max_hold_minutes)
        raise RuntimeError("durdur")
    monkeypatch.setattr(backtest, "run_backtest", fake_run)
    short = replace(cfg, take_profit_pct=5, max_hold_minutes=240)
    main.run_backtest_cli(short, main.parse_args(["--backtest", "--strateji", "swing"]))
    out = capsys.readouterr().out
    assert seen == {"strategy": "swing", "days": 365, "tp": 12.0, "hold": 7200}
    assert "önerilen çıkışlarla" in out and "MAX_HOLD_MINUTES=7200" in out
    main.run_backtest_cli(short, main.parse_args(["--backtest"]))
    assert seen["days"] == 30 and seen["strategy"] == "rebound"


def test_fmt_duration_days():
    assert fmt_duration(45 * 60) == "45 dk"
    assert fmt_duration(125 * 60) == "2 sa 5 dk"
    assert fmt_duration(7200 * 60) == "5 gün 0 sa"
    assert fmt_duration((1440 + 90) * 60) == "1 gün 1 sa"


def test_profile_keys_are_known_settings():
    from backtest import SWEEP_FIELDS
    from config import PROFILE_ATTRS
    assert set(SWING_EXIT_PROFILE) == set(PROFILE_ATTRS) and set(SWING_EXIT_PROFILE) <= set(SWEEP_FIELDS)
    assert all(SWEEP_FIELDS[k][0] == PROFILE_ATTRS[k] for k in SWING_EXIT_PROFILE)


# ---------------------------------------------------------------- inceleme düzeltmeleri
def test_live_does_not_chase_price_after_hourly_close(cfg):
    rows = breakout_rows()
    p = float(rows[-2][4])
    chased = rows[:-1] + [k(rows[-1][0], p, p * 1.03, p, p * 1.02, vol=30_000)]   # Açık mum kapanıştan %2 yukarıda
    assert SwingBreakoutStrategy(None, cfg).analyze("X", chased[-400:])[1] == "fiyat kaçtı"
    rows = pullback_rows()
    p = float(rows[-2][4])
    chased = rows[:-1] + [k(rows[-1][0], p, p * 1.03, p, p * 1.02, vol=30_000)]
    assert SwingPullbackStrategy(None, cfg).analyze("X", chased[-400:])[1] == "fiyat kaçtı"


def _late_listing_series(listed_at):
    """Coin dönemin ortasında listelenir (öncesinde mum yok); listelendikten ~200 saat sonra kırılım."""
    rows = breakout_rows(n=128, t=START_MS + listed_at * H, p0=1.0)[:-1]
    t, p = rows[-1][0] + H, float(rows[-1][4])
    for _ in range(16):
        o = p
        p *= 1.01
        rows.append(k(t, o, p * 1.002, o * 0.999, p))
        t += H
    while t < END_MS:
        rows.append(k(t, p, p * 1.003, p * 0.997, p))
        t += H
    series = _swing_series()
    series["ABCUSDT"] = rows
    return series


def test_backtest_never_feeds_pre_listing_bars_to_strategy(cfg, fake_client, tmp_path):
    swing, _ = with_strategy(replace(cfg, delist_filter=False), "swing")
    listed_at = 440                                    # Simülasyon 432'de başlar
    data = _prepare(swing, HourClient(fake_client, _late_listing_series(listed_at)).client, tmp_path)
    s = data.series["ABCUSDT"]
    assert s.first == listed_at
    window = s.window(listed_at + 30, 400)
    assert len(window) == 32 and float(window[0][4]) > 0       # Yalnızca gerçek mumlar + açık mum
    # Kırılım listelendikten ~200 saat sonra: canlıda 260 saatlik geçmiş olmadığı için "veri az" → işlem yok
    assert simulate(swing, data).trades == []


def test_backtest_applies_listing_age_filter(cfg, fake_client, tmp_path):
    from backtest import LISTING_CACHE_FILE, _Simulator, listing_times
    swing, _ = with_strategy(cfg, "swing")
    hc = HourClient(fake_client, _late_listing_series(440))
    listed = START_MS + 440 * H
    hc.client.listed = {"ABCUSDT": listed}
    data = _prepare(swing, hc.client, tmp_path)
    s = data.series["ABCUSDT"]
    assert s.listed_ms == listed
    sim = _Simulator(swing, data.series, data.btc, data.min_notional, 0.05, None)
    day = 24 * H
    assert not sim._listing_ok(s, listed // day * day + 12 * day)             # 13. gün: 13 günlük mum
    assert sim._listing_ok(s, listed // day * day + 13 * day)                 # 14. gün
    assert _Simulator(replace(swing, delist_filter=False), data.series, data.btc, 5, 0.05, None)._listing_ok(s, listed)
    assert simulate(swing, data).stats.get("yeni_coin", 0) > 0
    # Listelenme günü kalıcı önbellekte: ikinci hazırlıkta yeniden sorulmaz
    assert (tmp_path / "c" / LISTING_CACHE_FILE).exists()
    hc.client.listed = {}
    assert listing_times(hc.client, ["ABCUSDT"], END_MS, tmp_path / "c") == {"ABCUSDT": listed}


def test_listing_filter_uses_real_listing_day_not_data_gaps(cfg, fake_client, tmp_path):
    """Eski bir coinin verisi dönem başında eksikse (işlem durması) yeni coin sayılmaz; dönemden kısa süre önce
    listelenen coin ise verisi dönem başından başlasa da canlıdaki gibi elenir."""
    swing, _ = with_strategy(cfg, "swing")
    series = _swing_series()
    series["ABCUSDT"] = series["ABCUSDT"][5:]          # İlk 5 saat yok (durma), ama coin yıllardır listede
    hc = HourClient(fake_client, series)
    data = _prepare(swing, hc.client, tmp_path)
    assert data.series["ABCUSDT"].first == 5
    result = simulate(swing, data)
    assert result.trades and not result.stats.get("yeni_coin")

    fresh = HourClient(fake_client, _swing_series())
    fresh.client.listed = {"ABCUSDT": END_MS - (DAYS + 2) * 24 * H}   # Simülasyon başlamadan ~2 gün önce
    data = _prepare(swing, fresh.client, tmp_path / "x")
    assert data.series["ABCUSDT"].first == 0
    assert simulate(swing, data).stats.get("yeni_coin", 0) > 0


def test_notes_flag_only_entry_filters_that_block_swing(cfg):
    from config import short_entry_filters
    pb = replace(cfg, strategy="swing_pullback", take_profit_pct=12.0, min_room_ratio=1.2, max_24h_gain_pct=6.0,
                 min_signal_score=25.0)
    found = " ".join(short_entry_filters(pb))
    assert "MIN_ROOM_RATIO=1.2" in found and "hiç alım yapamaz" in found and "MAX_24H_GAIN_PCT=6" in found
    assert "MIN_SIGNAL_SCORE" not in found               # Swing --tara-giris de önerebilir: uyarılmaz
    assert any("gereksiz yere eleyebilir" in n for n in _setting_notes(pb))
    # swing --tara-giris'in önerebileceği değerler (alan 0.5 / 1, puan eşiği) uyarı üretmez
    assert short_entry_filters(replace(pb, min_room_ratio=1.0, max_24h_gain_pct=25.0)) == []
    assert short_entry_filters(replace(pb, strategy="swing", max_24h_gain_pct=25.0)) == []
    assert not any("eleyebilir" in n for n in _setting_notes(replace(pb, strategy="rebound")))
    full = next(n for n in _setting_notes(replace(cfg, strategy="swing", take_profit_pct=5)) if "Önerilen" in n)
    assert "MIN_SIGNAL_SCORE=0" in full and "MIN_ROOM_RATIO=0" in full and "MAX_24H_GAIN_PCT=25" in full


def test_profile_widens_5m_gain_cap_but_keeps_wider_one(cfg):
    narrow, _ = with_strategy(replace(cfg, strategy="trend", max_24h_gain_pct=6.0), "swing")
    wide, _ = with_strategy(replace(cfg, strategy="trend", max_24h_gain_pct=40.0), "swing")
    assert narrow.max_24h_gain_pct == 25.0 and wide.max_24h_gain_pct == 40.0
    from backtest import SweepRow, _advice_lines
    row = SweepRow({"STRATEGY": "swing", "MIN_SIGNAL_SCORE": 0.0, "MIN_ROOM_RATIO": 0.0}, True, 50, 50, 1, 1, 1, 1, 1, 1)
    assert "MAX_24H_GAIN_PCT=25" in _advice_lines(narrow, row)


def test_configured_hold_is_shown_exactly():
    assert fmt_duration(1530 * 60, exact=True) == "1 gün 1 sa 30 dk"
    assert fmt_duration(7200 * 60, exact=True) == "5 gün"
    assert fmt_duration(90 * 60, exact=True) == "1 sa 30 dk"


def test_rebound_suggestion_keeps_strategy_family(cfg):
    from backtest import BacktestData, SweepRow, format_sweep_report
    data = BacktestData(series={}, btc=None, regime=None, min_notional=5, start_ms=0, end_ms=10 * 86_400_000,
                        n=2880, k_start=288, days=9, notes=[])
    st = {"TAKE_PROFIT_PCT": 3.0, "STOP_LOSS_PCT": 2.0, "TRAILING_ACTIVATION_PCT": 2.0, "TRAILING_FLOOR_PCT": 1.0,
          "MAX_HOLD_MINUTES": 120, "MIN_ROOM_RATIO": 0.0}
    rows = [SweepRow(st, True, 100, 40, -2, -0.3, 9, -1, -1, 0.7, se_net_pct=0.1)]
    text = format_sweep_report(rows, replace(cfg, strategy="rebound"), data)
    assert "--strateji-karsilastir --strateji rebound" in text
    assert "--strateji swing --strateji-karsilastir" in text      # 5 dk başarısızsa uzun vade önerilir


def test_listing_lookup_stops_on_rate_limit(fake_client):
    from backtest import listing_times
    from binance_client import RateLimitError

    class Banned(fake_client):
        calls = 0

        def _request(self, method, path, params=None, signed=False):
            Banned.calls += 1
            raise RateLimitError(418, 0, "banned")

    import backtest
    saved = backtest.RATE_LIMIT_RETRIES
    backtest.RATE_LIMIT_RETRIES = 0
    try:
        out = listing_times(Banned({}), [f"S{i}USDT" for i in range(40)], END_MS)
    finally:
        backtest.RATE_LIMIT_RETRIES = saved
    assert Banned.calls == 1 and out == {}       # İlk yasakta durur, kalanlar için istek atmaz


def test_compare_warning_ignores_room_filter(cfg, capsys, monkeypatch):
    import binance_client
    import main
    monkeypatch.setattr(binance_client, "BinanceClient", lambda *a, **k: object())
    monkeypatch.setattr(backtest, "prepare_backtest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dur")))
    env = replace(cfg, strategy="swing", take_profit_pct=12.0, min_room_ratio=2.0)
    main.run_backtest_cli(env, main.parse_args(["--backtest", "--strateji", "swing_pullback", "--strateji-karsilastir"]))
    assert "MIN_ROOM_RATIO" not in capsys.readouterr().out
    main.run_backtest_cli(env, main.parse_args(["--backtest", "--strateji", "swing_pullback", "--tara"]))
    assert "MIN_ROOM_RATIO=2" in capsys.readouterr().out
