"""Backtest: sahte istemci + sentetik 5 dk mumlarla (internet gerekmez)."""
from dataclasses import replace

import pytest

import backtest
from backtest import (END_REASON, HOUR_MS, INTERVAL_MS, cache_path, format_backtest_report, load_klines,
                      run_backtest, select_symbols)
from exchange import parse_symbol_rules
from risk import ExitReason

END_TIME_MS = 1_700_000_000_000          # Saate yuvarlanınca 1_699_999_200_000
DAYS = 2
END_MS = END_TIME_MS // HOUR_MS * HOUR_MS
START_MS = END_MS - 84 * HOUR_MS          # (2 + 1.5 gün ısınma) × 24 saat
N_BARS = (END_MS - START_MS) // INTERVAL_MS
SIM_START = N_BARS - DAYS * 288           # Simülasyonun başladığı mum
DIP_AT = 600                              # Düşüşün başladığı mum (simülasyon içinde)


# ---------------------------------------------------------------- sentetik veri
def flat(n, price, vol=20_000.0):
    return [(price, price * 1.003, price * 0.997, price, vol, vol * 0.5) for _ in range(n)]


def dip_and_bounce(price):
    """Dalgalı sert düşüş (~%6.5, aşırı satım) → hacimli toparlanma başlangıcı (5 mum)."""
    bars = []
    for i in range(16):
        o = price
        price *= 0.988 if i % 2 == 0 else 1.004
        bars.append((o, max(o, price) * 1.001, min(o, price) * 0.998, price, 30_000.0, 15_000.0))
    for _ in range(5):
        o = price
        price *= 1.006
        bars.append((o, price * 1.001, o * 0.999, price, 60_000.0, 40_000.0))
    return bars, price


def trend(price, factor, n, vol=40_000.0):
    bars = []
    for _ in range(n):
        o = price
        price *= factor
        bars.append((o, max(o, price) * 1.002, min(o, price) * 0.999, price, vol, vol * 0.5))
    return bars, price


def to_raw(bars):
    """(o, h, l, c, quote_hacim, taker_quote) listesi → ortak zaman çizelgesinde ham Binance mumları."""
    assert len(bars) >= N_BARS
    rows = []
    for k, (o, h, low, c, qv, tbq) in enumerate(bars[:N_BARS]):
        t = START_MS + k * INTERVAL_MS
        rows.append([t, str(o), str(h), str(low), str(c), "0", t + INTERVAL_MS - 1, str(qv), 10, "0",
                     str(tbq), "0"])
    return rows


def scenario(after_bounce):
    """Yatay → düşüş + toparlanma → `after_bounce(fiyat)` ile verilen devam → yatay."""
    bars = flat(DIP_AT, 1.0)
    dip, price = dip_and_bounce(1.0)
    bars += dip
    tail, price = after_bounce(price)
    bars += tail
    bars += flat(N_BARS - len(bars), price)
    return to_raw(bars)


def aggregate(rows, step_ms):
    """Ham 5 dk mumları daha uzun aralıklı mumlara birleştirir (açılış zamanı step_ms'e hizalı)."""
    buckets = {}
    for r in rows:
        buckets.setdefault(r[0] // step_ms * step_ms, []).append(r)
    out = []
    for t in sorted(buckets):
        b = buckets[t]
        out.append([t, b[0][1], str(max(float(x[2]) for x in b)), str(min(float(x[3]) for x in b)), b[-1][4], "0",
                    t + step_ms - 1, str(sum(float(x[7]) for x in b)), 0, "0", str(sum(float(x[10]) for x in b)),
                    "0"])
    return out


def btc_flat():
    return to_raw(flat(N_BARS, 30_000.0, vol=50_000_000.0))


# ---------------------------------------------------------------- sahte istemci
@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(backtest, "REQUEST_PAUSE_SEC", 0.0)


@pytest.fixture
def make_client(fake_client):
    class KlineClient(fake_client):
        def __init__(self, series, volumes=None):
            super().__init__({s: 1.0 for s in series})
            self.series = series
            self.volumes = volumes or {}
            self.kline_calls = 0

        def tickers_24h(self):
            return [{"symbol": s, "quoteVolume": str(self.volumes.get(s, 5_000_000))} for s in self.prices]

        def _request(self, method, path, params=None, signed=False):
            assert method == "GET" and path == "/api/v3/klines"
            self.kline_calls += 1
            rows = self.series[params["symbol"]]
            if params["interval"] != "5m":   # 4s / 1g: 5 dk mumlardan birleştir (piyasa rejimi için)
                rows = aggregate(rows, {"4h": 4 * HOUR_MS, "1d": 24 * HOUR_MS}[params["interval"]])
            out = [r for r in rows if params["startTime"] <= r[0] <= params["endTime"]]
            return out[: params["limit"]]

    return KlineClient


def run(cfg, client, tmp_path, **kw):
    kw.setdefault("symbols", ["ABCUSDT"])
    return run_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "cache", progress=None,
                        end_time_ms=END_TIME_MS, **kw)


# ---------------------------------------------------------------- testler
def test_dip_rebound_take_profit(cfg, make_client, tmp_path):
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    result = run(cfg, client, tmp_path)

    assert result.trades, result.stats
    t = result.trades[0]
    assert t.symbol == "ABCUSDT"
    assert t.reason == ExitReason.TAKE_PROFIT.value
    assert t.pnl > 0 and t.pnl_pct >= cfg.take_profit_pct - 0.01
    assert START_MS + (DIP_AT + 16) * INTERVAL_MS < t.open_time_ms < t.close_time_ms
    assert t.cost <= cfg.start_capital and t.score > 0
    # Spread: alış piyasa fiyatının üstünde, satış altında
    assert t.entry_price > 0.95 and t.exit_price > t.entry_price
    assert result.end_capital == pytest.approx(cfg.start_capital + result.realized_pnl)
    assert result.realized_pnl == pytest.approx(sum(x.pnl for x in result.trades))
    assert result.total_fees > 0 and not result.killed
    assert result.symbols_used == ["ABCUSDT"]
    assert len(result.equity_curve) >= DAYS * 288 - 1
    assert result.equity_curve[-1][1] == pytest.approx(result.end_capital)


def test_trailing_stop_after_peak(cfg, make_client, tmp_path):
    def rally_then_fade(p):
        up, p = trend(p, 1.03, 4)          # ~+%12.5 → izleyen stop aktif, TP (+%15) görülmez
        down, p = trend(p, 0.99, 12)
        return up + down, p

    client = make_client({"ABCUSDT": scenario(rally_then_fade), "BTCUSDT": btc_flat()})
    result = run(cfg, client, tmp_path)

    assert result.trades
    t = result.trades[0]
    assert t.reason == ExitReason.TRAILING_STOP.value
    assert t.pnl_pct == pytest.approx(cfg.trailing_floor_pct, abs=0.05)  # Taban seviyesinden satıldı


def test_same_candle_tp_and_sl_assumes_stop_loss(cfg, make_client, tmp_path):
    def wide_candle(p):
        # Toparlanmanın hemen ardından: tepesi TP'nin, dibi SL'nin çok ötesinde olan tek mum
        return [(p, p * 1.30, p * 0.75, p, 50_000.0, 25_000.0)], p

    client = make_client({"ABCUSDT": scenario(wide_candle), "BTCUSDT": btc_flat()})
    result = run(cfg, client, tmp_path)

    assert result.trades
    t = result.trades[0]
    assert t.reason == ExitReason.STOP_LOSS.value
    assert t.pnl_pct == pytest.approx(-cfg.stop_loss_pct, abs=0.01)  # SL eşik fiyatından çıkış


def test_gap_below_stop_exits_at_open(cfg, make_client, tmp_path):
    def gap_down(p):
        g = p * 0.70  # Mum SL'nin çok altında açılıyor
        return [(g, g * 1.01, g * 0.99, g, 50_000.0, 25_000.0)], g

    client = make_client({"ABCUSDT": scenario(gap_down), "BTCUSDT": btc_flat()})
    result = run(cfg, client, tmp_path, spread_pct=0.1)

    t = result.trades[0]
    assert t.reason == ExitReason.STOP_LOSS.value
    gap_open = float(client.series["ABCUSDT"][DIP_AT + 21][1])
    assert t.exit_price == pytest.approx(gap_open * (1 - 0.1 / 200))
    assert t.pnl_pct < -25


def test_gap_below_stop_with_trailing_active_is_stop_loss(cfg, make_client, tmp_path):
    def rally_then_gap(p):
        up, p = trend(p, 1.03, 4)          # İzleyen stop aktifleşir
        g = p * 0.70
        return up + [(g, g * 1.01, g * 0.99, g, 50_000.0, 25_000.0)], g

    client = make_client({"ABCUSDT": scenario(rally_then_gap), "BTCUSDT": btc_flat()})
    t = run(cfg, client, tmp_path).trades[0]
    assert t.reason == ExitReason.STOP_LOSS.value  # Canlı evaluate_exit gibi: SL, izleyen stoptan önce
    assert t.pnl_pct < -cfg.stop_loss_pct


def test_position_closed_when_symbol_data_ends(cfg, make_client, tmp_path):
    rows = scenario(lambda p: ([], p))[: DIP_AT + 40]  # Toparlanmadan kısa süre sonra veri bitiyor
    client = make_client({"ABCUSDT": rows, "BTCUSDT": btc_flat()})
    result = run(cfg, client, tmp_path)

    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.reason == END_REASON
    assert t.close_time_ms == START_MS + (DIP_AT + 40) * INTERVAL_MS
    assert result.end_capital == pytest.approx(result.equity_curve[-1][1])


def test_crash_triggers_kill_switch(cfg, make_client, tmp_path):
    cfg = replace(cfg, max_total_loss_pct=0.5 / 11 * 100)

    def crash_then_new_setup(p):
        c = p * 0.94  # SL'ye değmeyen ama toplam zararı limitin altına iten kapanış
        bars = [(p, p, c, c, 80_000.0, 20_000.0)]
        bars += flat(100, c)
        dip, p2 = dip_and_bounce(c)          # Sonradan yeni bir fırsat: kill-switch sonrası alınmamalı
        bars += dip
        rally, p2 = trend(p2, 1.03, 8)
        return bars + rally, p2

    client = make_client({"ABCUSDT": scenario(crash_then_new_setup), "BTCUSDT": btc_flat()})
    result = run(cfg, client, tmp_path)

    assert result.killed
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.reason == ExitReason.KILL_SWITCH.value
    crash_close_ms = START_MS + (DIP_AT + 22) * INTERVAL_MS
    assert result.killed_at_ms == crash_close_ms == t.close_time_ms
    assert result.realized_pnl <= -0.5
    assert result.equity_curve[-1][0] == crash_close_ms  # Sonrası simüle edilmedi
    report = format_backtest_report(result, cfg)
    assert "TETİKLENDİ" in report


def test_btc_drop_blocks_entries(cfg, make_client, tmp_path):
    btc = flat(DIP_AT + 6, 30_000.0, vol=50_000_000.0)
    down, p = trend(30_000.0, 0.997, 20, vol=50_000_000.0)  # BTC 1 saatte ~%3.5 düşüyor
    btc += down + flat(N_BARS, p, vol=50_000_000.0)
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": to_raw(btc)})
    result = run(cfg, client, tmp_path)

    assert result.trades == []
    assert result.stats.get("btc_engel", 0) > 0


def test_second_run_uses_cache(cfg, make_client, tmp_path):
    series = {"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()}
    first = make_client(series)
    r1 = run(cfg, first, tmp_path)
    assert first.kline_calls == 6  # 1008 mum → sembol başına 2 sayfa (1000 + 8) + BTC 4s ve 1g (rejim)
    assert cache_path(tmp_path / "cache", "ABCUSDT", START_MS, END_MS).exists()

    second = make_client(series)
    r2 = run(cfg, second, tmp_path)
    assert second.kline_calls == 0  # Hepsi önbellekten
    assert [(t.symbol, t.open_time_ms, t.reason, round(t.pnl, 10)) for t in r1.trades] == \
           [(t.symbol, t.open_time_ms, t.reason, round(t.pnl, 10)) for t in r2.trades]


def test_load_klines_pages_and_filters_range(make_client, tmp_path):
    client = make_client({"ABCUSDT": btc_flat()})
    rows, cached = load_klines(client, "ABCUSDT", START_MS, END_MS, tmp_path / "c")
    assert not cached and len(rows) == N_BARS and client.kline_calls == 2
    assert rows[0][0] == START_MS and rows[-1][0] == END_MS - INTERVAL_MS
    assert len(rows[0]) == 7
    rows2, cached2 = load_klines(client, "ABCUSDT", START_MS, END_MS, tmp_path / "c")
    assert cached2 and rows2 == rows and client.kline_calls == 2


def test_select_symbols_by_volume(cfg, make_client, tmp_path):
    client = make_client({s: [] for s in ["AAAUSDT", "BBBUSDT", "USDCUSDT", "ETHDOWNUSDT", "CCCUSDT"]})
    rules = parse_symbol_rules(client.exchange_info(), "USDT")
    tickers = [
        {"symbol": "AAAUSDT", "quoteVolume": "1000"},
        {"symbol": "BBBUSDT", "quoteVolume": "9000"},
        {"symbol": "USDCUSDT", "quoteVolume": "99999"},     # Stabil coin
        {"symbol": "ETHDOWNUSDT", "quoteVolume": "50000"},  # Kaldıraçlı token
        {"symbol": "XYZUSDT", "quoteVolume": "70000"},      # Kuralı yok
        {"symbol": "CCCUSDT", "quoteVolume": "5000"},
    ]
    assert select_symbols(cfg, tickers, rules, 2) == ["BBBUSDT", "CCCUSDT"]
    assert select_symbols(cfg, tickers, rules, 10) == ["BBBUSDT", "CCCUSDT", "AAAUSDT"]


def test_auto_symbol_selection_includes_btc_for_filter(cfg, make_client, tmp_path):
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()},
                         volumes={"ABCUSDT": 5_000_000, "BTCUSDT": 9_000_000_000})
    result = run(cfg, client, tmp_path, symbols=None, max_symbols=1)
    assert result.symbols_used == ["BTCUSDT"]  # En hacimli tek parite
    assert client.kline_calls == 4             # BTC 5 dk ayrıca indirilmedi (+ rejim için BTC 4s ve 1g)
    assert result.trades == []


def test_period_end_closes_open_positions_and_report(cfg, make_client, tmp_path):
    # Toparlanmadan sonra fiyat yatay kalıyor → pozisyon dönem sonuna kadar açık
    client = make_client({"ABCUSDT": scenario(lambda p: ([], p)), "BTCUSDT": btc_flat()})
    result = run(cfg, client, tmp_path)

    assert result.trades and result.trades[-1].reason == END_REASON
    assert result.trades[-1].close_time_ms == END_MS
    assert any("Dönem sonunda" in n for n in result.notes)
    report = format_backtest_report(result, cfg)
    for text in ("BACKTEST RAPORU", "İşlem sayısı", "Kazanma oranı", "Maks. düşüş", "Dönem Sonu",
                 "Kill-switch: tetiklenmedi", "Hayatta kalma"):
        assert text in report


def test_max_hold_closes_at_candle_close(cfg, make_client, tmp_path):
    cfg = replace(cfg, max_hold_minutes=30)
    client = make_client({"ABCUSDT": scenario(lambda p: ([], p)), "BTCUSDT": btc_flat()})
    result = run(cfg, client, tmp_path)

    t = result.trades[0]
    assert t.reason == ExitReason.MAX_HOLD.value
    assert t.close_time_ms - t.open_time_ms == 30 * 60_000


def test_report_without_trades(cfg):
    empty = backtest.BacktestResult(
        trades=[], start_capital=11.0, end_capital=11.0, realized_pnl=0.0, total_fees=0.0,
        max_drawdown_usdt=0.0, max_drawdown_pct=0.0, killed=False, killed_at_ms=None, symbols_used=[],
        days=30.0, notes=[], equity_curve=[])
    text = format_backtest_report(empty, cfg)
    assert "hiç işlem açmadı" in text and "İşlem sayısı: 0" in text


def test_min_signal_score_respected(cfg, make_client, tmp_path):
    from dataclasses import replace
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    result = run(replace(cfg, min_signal_score=10_000), client, tmp_path)
    assert not result.trades and result.stats["puan_dusuk"] > 0


def test_breakeven_simulated_like_live(cfg, make_client, tmp_path):
    def small_rally_then_fade(p):
        up, p = trend(p, 1.02, 3)          # ~+%6 → başabaş (tetik %5) aktif; izleyen stop (+%10) ve TP değil
        down, p = trend(p, 0.99, 12)
        return up + down, p

    client = make_client({"ABCUSDT": scenario(small_rally_then_fade), "BTCUSDT": btc_flat()})
    assert cfg.breakeven_enabled                       # Varsayılan açık
    result = run(cfg, client, tmp_path)
    t = result.trades[0]
    assert t.reason == ExitReason.BREAKEVEN.value
    assert t.pnl_pct == pytest.approx(cfg.breakeven_floor_pct, abs=0.05)
    report = format_backtest_report(result, cfg)
    assert "🔒 Başabaş: 1 işlem" in report and "Başabaş: +%5 → +%0.3" in report

    off = replace(cfg, breakeven_enabled=False)
    result_off = run(off, client, tmp_path)
    assert result_off.trades[0].reason != ExitReason.BREAKEVEN.value
    assert result_off.trades[0].pnl < t.pnl


# ---------------------------------------------------------------- teşhis / rejim / tarama
def _btc_slow_crash():
    """BTC 20 saatte ~%8 düşüyor (1 saatlik düşüş küçük → BTC 1s filtresi değil, rejim 'çöküş' engeller)."""
    bars = flat(300, 30_000.0, vol=50_000_000.0)
    down, p = trend(30_000.0, 0.99965, 240, vol=50_000_000.0)
    bars += down
    bars += flat(N_BARS - len(bars), p, vol=50_000_000.0)
    return to_raw(bars)


def test_trades_carry_diagnostics_and_report_explains(cfg, make_client, tmp_path):
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    result = run(cfg, client, tmp_path)
    t = result.trades[0]
    assert t.peak_pct >= cfg.take_profit_pct - 0.01 and t.trough_pct < 0      # Girişte -(komisyon+spread)
    assert t.move_pct > t.pnl_pct > 0                                         # Maliyet hareketten düşülür
    assert t.fee_usdt == pytest.approx(result.total_fees)
    assert t.room_pct is not None and t.room_pct > 0
    report = format_backtest_report(result, cfg)
    assert "🔬 TEŞHİS" in report and "komisyon+spread" in report and "1. yarısı" in report
    assert "--tara" in report


def test_summarize_and_halves():
    from backtest import BacktestTrade, split_halves, summarize

    def tr(pnl, move, t):
        return BacktestTrade("X", t, t + 1, 1, 1, 10, 10 + pnl, pnl, pnl * 10, "R", 0, move_pct=move)
    trades = [tr(1.0, 11.0, 0), tr(-0.5, -4.0, 10), tr(0.2, 2.5, 60)]
    sm = summarize(trades)
    assert sm.count == 3 and sm.net == pytest.approx(0.7) and sm.profit_factor == pytest.approx(2.4)
    assert sm.mean_net_pct == pytest.approx((10 - 5 + 2) / 3)
    assert sm.cost_pct == pytest.approx((11 - 4 + 2.5) / 3 - (10 - 5 + 2) / 3)
    first, second = split_halves(trades, 0, 100)
    assert [t.open_time_ms for t in first] == [0, 10] and [t.open_time_ms for t in second] == [60]
    assert summarize([]).count == 0


def test_regime_crash_blocks_entries_like_live(cfg, make_client, tmp_path):
    series = {"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": _btc_slow_crash()}
    blocked = run(cfg, make_client(series), tmp_path)
    assert blocked.trades == [] and blocked.stats.get("rejim_cokus", 0) > 0
    assert blocked.stats.get("btc_engel", 0) == 0
    assert "çöküş nedeniyle" in format_backtest_report(blocked, cfg)
    allowed = run(replace(cfg, regime_filter=False), make_client(series), tmp_path)
    assert allowed.trades


def test_regime_sim_matches_live_classify():
    from backtest import _RegimeSim, _Series
    from market_regime import COKUS, NORMAL, ZAYIF, classify
    rows = [[START_MS + k * INTERVAL_MS] + list(map(float, r[1:5])) + [5e7, 2e7]
            for k, r in enumerate(_btc_slow_crash())]
    btc = _Series("BTCUSDT", rows, START_MS, N_BARS)
    flat_4h = [[START_MS - (60 - i) * 4 * HOUR_MS, 30_000, 30_000, 30_000, 30_000, 0, 0] for i in range(60)]
    flat_1d = [[START_MS - (30 - i) * 24 * HOUR_MS, 30_000, 30_000, 30_000, 30_000, 0, 0] for i in range(30)]
    sim = _RegimeSim(type("C", (), {"min_quote_volume_24h": 1})(), btc, {}, flat_4h, flat_1d)
    k = 560                                            # BTC 24 saatte ~%8 aşağıda
    t_close = START_MS + (k + 1) * INTERVAL_MS
    btc_24h = (btc.c[k] - btc.o[k - 287]) / btc.o[k - 287] * 100
    assert btc_24h < -6
    assert sim.at(k, t_close) == COKUS == classify(btc.c[k], [30_000] * 59 + [btc.c[k]],
                                                   [30_000] * 29 + [btc.c[k]], btc_24h, None).regime
    assert sim.at(100, START_MS + 101 * INTERVAL_MS) == NORMAL
    assert sim.cache[k] == COKUS and ZAYIF != COKUS


def test_room_filter_applied_in_backtest(cfg, make_client, tmp_path):
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    result = run(replace(cfg, min_room_ratio=50), client, tmp_path)   # İmkânsız derecede geniş alan iste
    assert result.trades == [] and result.stats.get("alan_dar", 0) > 0


def test_signal_cache_reused_and_results_identical(cfg, make_client, tmp_path):
    from backtest import prepare_backtest, simulate
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    data = prepare_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                            symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)
    cache = {}
    a = simulate(cfg, data, signal_cache=cache)
    filled = len(cache)
    b = simulate(cfg, data, signal_cache=cache)
    assert filled > 0 and len(cache) == filled
    assert [(t.open_time_ms, t.reason, t.pnl) for t in a.trades] == [(t.open_time_ms, t.reason, t.pnl) for t in b.trades]


def test_sweep_compares_settings_on_same_data(cfg, make_client, tmp_path):
    from backtest import format_sweep_report, prepare_backtest, run_sweep, sweep_combos
    combos = sweep_combos(cfg, tps=(3.0, 15.0), sls=(2.0,), trails=(2.0, 10.0), holds=(120,), rooms=(0.0,))
    assert combos[0][1] is True                                        # İlk satır mevcut ayarlar
    assert all(st["TRAILING_ACTIVATION_PCT"] < st["TAKE_PROFIT_PCT"] for st, _ in combos[1:])
    assert all(st["TRAILING_FLOOR_PCT"] == st["TRAILING_ACTIVATION_PCT"] / 2 for st, _ in combos[1:])
    assert len(combos) == 1 + 3                                         # (3,2,2) (15,2,2) (15,2,10)
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    data = prepare_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                            symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)
    rows = run_sweep(cfg, data, combos=combos)
    assert len(rows) == 4 and rows[0].baseline
    assert all(r.trades == 1 for r in rows)
    tp3 = next(r for r in rows if r.settings["TAKE_PROFIT_PCT"] == 3.0)
    assert tp3.net < next(r for r in rows if r.baseline).net          # Daha küçük hedef → daha az kâr (bu veride)
    text = format_sweep_report(rows, cfg, data)
    assert "PARAMETRE TARAMASI" in text and "⭐" in text and "kill-switch kapalıdır" in text


def test_cli_sweep_and_capital_override(cfg, make_client, tmp_path, monkeypatch, capsys):
    import binance_client
    import main
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()},
                         volumes={"ABCUSDT": 9_000_000_000, "BTCUSDT": 1})
    monkeypatch.setattr(binance_client, "BinanceClient", lambda *a, **k: client)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)               # Tarama CSV'si proje klasörüne yazılmasın
    real_prepare = backtest.prepare_backtest
    seen = {}

    def prepare(c, cl, days, max_symbols, end_time_ms=None):
        seen["capital"], seen["end"] = c.start_capital, end_time_ms
        return real_prepare(c, cl, days, max_symbols, cache_dir=tmp_path / "c", progress=None,
                            symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)
    monkeypatch.setattr(backtest, "prepare_backtest", prepare)
    args = main.parse_args(["--backtest", "--gun", "2", "--tara", "--sermaye", "25", "--tp", "3,15",
                            "--sl", "2", "--iz", "2", "--sure", "0,120", "--alan", "0"])
    assert main.run_backtest_cli(cfg, args) == main.EXIT_OK
    out = capsys.readouterr().out
    assert seen["capital"] == 25 and seen["end"] is None and "PARAMETRE TARAMASI" in out and "sermaye 25.00" in out
    assert "--bitis" in out                                                # Bağımsız dönem önerisi
    args2 = main.parse_args(["--backtest", "--gun", "2", "--tara", "--bitis", "2023-11-14", "--tp", "15"])
    assert main.run_backtest_cli(cfg, args2) == main.EXIT_OK
    from datetime import datetime as _dt
    assert seen["end"] == int(_dt(2023, 11, 14).timestamp() * 1000)
    for wrong in ("14.11.2023", "2999-01-01"):
        assert main.run_backtest_cli(cfg, main.parse_args(["--backtest", "--bitis", wrong])) == main.EXIT_CONFIG
    bad = main.parse_args(["--backtest", "--tara", "--tp", "abc"])
    assert main.run_backtest_cli(cfg, bad) == main.EXIT_CONFIG
    assert main.run_backtest_cli(cfg, main.parse_args(["--backtest", "--sermaye", "0"])) == main.EXIT_CONFIG
    assert main._number_list("0,120", "sure", int, allow_zero=True) == [0, 120]
    import pytest as _pytest
    with _pytest.raises(ValueError):
        main._number_list("0", "tp")


def test_few_identical_trades_are_not_called_evidence(cfg):
    from backtest import BacktestResult, BacktestTrade, diagnostics_lines

    def tp_trade(t):
        return BacktestTrade("X", t, t + 1, 1, 1.15, 10, 11.5, 1.5, 15.0, ExitReason.TAKE_PROFIT.value, 0,
                             move_pct=15.3, peak_pct=15, trough_pct=-0.2)
    res = BacktestResult(trades=[tp_trade(0), tp_trade(10)], start_capital=11, end_capital=14, realized_pnl=3,
                         total_fees=0.1, max_drawdown_usdt=0, max_drawdown_pct=0, killed=False, killed_at_ms=None,
                         symbols_used=["X"], days=1, notes=[], equity_curve=[], start_ms=0, end_ms=100)
    text = "\n".join(diagnostics_lines(res, cfg))
    assert "şans eseri olma ihtimali düşük" not in text and "✅" not in text
    assert "30'dan az" in text and "kanıt yetersiz" in text


def test_sweep_suggestion_is_pasteable_env_lines(cfg, tmp_path, monkeypatch):
    from backtest import BacktestData, SweepRow, format_sweep_report
    def st(tp, sl, trail, floor, hold, room):
        return {"TAKE_PROFIT_PCT": tp, "STOP_LOSS_PCT": sl, "TRAILING_ACTIVATION_PCT": trail,
                "TRAILING_FLOOR_PCT": floor, "MAX_HOLD_MINUTES": hold, "MIN_ROOM_RATIO": room}
    row = SweepRow(st(3, 2, 2, 1, 120, 1), False, 40, 60, 2.5, 0.3, 5, 1.0, 1.5, 1.4, se_net_pct=0.05)
    base = SweepRow(st(5, 3.5, 2, 1, 240, 0), True, 40, 40, -1.0, -0.2, 9, -0.5, -0.5, 0.8)
    data = BacktestData(series={}, btc=None, regime=None, min_notional=5, start_ms=0, end_ms=10 * 86_400_000,
                        n=2880, k_start=288, days=9, notes=[])
    text = format_sweep_report([base, row], cfg, data)
    lines = [ln.strip() for ln in text.splitlines()]
    for key in ("TAKE_PROFIT_PCT=3", "STOP_LOSS_PCT=2", "TRAILING_FLOOR_PCT=1", "MAX_HOLD_MINUTES=120"):
        assert key in lines                      # Her ayar kendi satırında → .env'e yapıştırılabilir
    env = tmp_path / "aday.env"
    env.write_text("\n".join(ln for ln in lines if "=" in ln and ln.split("=")[0].isupper()), encoding="utf-8")
    from dotenv import dotenv_values             # load_config os.environ'u kalıcı değiştirirdi
    values = dotenv_values(env)
    assert values["TAKE_PROFIT_PCT"] == "3" and values["MAX_HOLD_MINUTES"] == "120"
    assert values["MIN_ROOM_RATIO"] == "1" and values["TRAILING_FLOOR_PCT"] == "1"
    assert "--bitis" in text and "bağımsız kanıt sayılmaz" in text


def _row(settings, baseline=False, trades=40, net=0.0, per=0.0, first=0.0, second=0.0):
    from backtest import SweepRow
    return SweepRow(settings, baseline, trades, 50.0, net, per, 5.0, first, second, 1.0)


def test_sweep_ranking_puts_few_trade_rows_last(cfg):
    from backtest import BacktestData, format_sweep_report
    data = BacktestData(series={}, btc=None, regime=None, min_notional=5, start_ms=0, end_ms=10 * 86_400_000,
                        n=2880, k_start=288, days=9, notes=[])
    st = {"TAKE_PROFIT_PCT": 5, "STOP_LOSS_PCT": 3.5, "TRAILING_ACTIVATION_PCT": 2, "TRAILING_FLOOR_PCT": 1,
          "MAX_HOLD_MINUTES": 240}
    rows = [_row({**st, "MIN_ROOM_RATIO": 0}, True, trades=108, net=-2.45, per=-0.32),
            _row({**st, "MIN_ROOM_RATIO": 1}, trades=5, net=-0.35, per=-0.99),       # Az kaybeden ama 5 işlem
            _row({**st, "TAKE_PROFIT_PCT": 3, "MIN_ROOM_RATIO": 0}, trades=150, net=-2.9, per=-0.20)]
    text = format_sweep_report(rows, cfg, data)
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if "İşl.başı" in ln) + 1
    table = lines[start:start + len(rows)]
    assert "-0.20%" in table[0]                    # En iyi işlem başı sonuç, yeterli işlemle
    assert table[-1].startswith("· ")             # 5 işlemlik satır en altta, işaretli
    assert "en alta alındı" in text and "En iyi işlem başı sonuç" in text and "--tara-giris" in text


def test_entry_sweep_varies_filters_and_reuses_cache(cfg, make_client, tmp_path):
    from backtest import entry_combos, format_sweep_report, prepare_backtest, run_entry_sweep, save_sweep_csv
    combos = entry_combos(cfg, [0.0, 20.0])
    assert combos[0][1] is True and len({tuple(sorted(c.items())) for c, _ in combos}) == len(combos)
    assert {c["REGIME_SKIP_WEAK"] for c, _ in combos} == {False, True}
    assert {c["MAX_24H_DROP_PCT"] for c, _ in combos} == {cfg.max_24h_drop_pct, 15.0, 8.0}
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    data = prepare_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                            symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)
    rows = run_entry_sweep(cfg, data)
    assert rows[0].baseline and rows[0].trades == 1
    narrow = [r for r in rows if r.settings["MAX_24H_DROP_PCT"] == 8.0 and r.settings["MIN_ROOM_RATIO"] == 0]
    assert narrow and all(r.trades == 1 for r in narrow)      # 24s düşüş ~%4.6, 8'in içinde: yine alınır
    roomy = [r for r in rows if r.settings["MIN_ROOM_RATIO"] == 1.0]
    assert roomy and all(r.trades == 0 for r in roomy)        # TP %15 için eski zirveye alan yetmez
    text = format_sweep_report(rows, cfg, data, kind="giris")
    assert "GİRİŞ TARAMASI" in text and "MaxDüş" in text and "--tara-giris --strateji rebound --bitis" in text
    path = save_sweep_csv(rows, tmp_path / "t.csv")
    content = path.read_text(encoding="utf-8-sig").splitlines()
    assert content[0].startswith("REGIME_SKIP_WEAK;MAX_24H_DROP_PCT") and len(content) == len(rows) + 1


def test_max_24h_drop_applied_at_simulation_time(cfg, make_client, tmp_path):
    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    assert run(cfg, client, tmp_path).trades
    tight = replace(cfg, min_24h_drop_pct=1.0, max_24h_drop_pct=2.0)          # ~%6.5 düşüş aralık dışı
    assert run(tight, make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()}),
               tmp_path).trades == []


def test_regime_skip_weak_blocks_entries(cfg, make_client, tmp_path):
    from backtest import _RegimeSim
    series = {"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()}
    original = _RegimeSim.at
    try:
        _RegimeSim.at = lambda self, k, t: "ZAYIF"
        strict = run(cfg, make_client(series), tmp_path)
        skipped = run(replace(cfg, regime_skip_weak=True), make_client(series), tmp_path)
    finally:
        _RegimeSim.at = original
    assert skipped.trades == [] and skipped.stats.get("rejim_zayif", 0) > 0
    assert "alım yapılmayan" in format_backtest_report(skipped, replace(cfg, regime_skip_weak=True))
    assert strict.stats.get("rejim_zayif", 0) > 0


def test_sweep_candidate_needs_multiple_testing_significance(cfg):
    from backtest import BacktestData, SweepRow, format_sweep_report, sweep_z
    assert sweep_z(1) == pytest.approx(1.96, abs=0.01) and sweep_z(60) > 3
    data = BacktestData(series={}, btc=None, regime=None, min_notional=5, start_ms=0, end_ms=10 * 86_400_000,
                        n=2880, k_start=288, days=9, notes=[])
    st = {"REGIME_SKIP_WEAK": False, "MAX_24H_DROP_PCT": 15.0, "MIN_SIGNAL_SCORE": 0.0, "MIN_ROOM_RATIO": 1.0}
    # Rastgele veride görülen türden: 43 işlem, iki yarıda da kârlı ama işlem başı +%0.14 ± 0.15
    lucky = SweepRow(st, False, 43, 72, 0.5, 0.14, 6.7, 0.1, 0.4, 1.2, se_net_pct=0.15)
    base = SweepRow({**st, "MIN_ROOM_RATIO": 0.0}, True, 111, 58, -5.6, -0.65, 51, -5.6, 0.0, 0.6, se_net_pct=0.2)
    fillers = [SweepRow({**st, "MIN_SIGNAL_SCORE": float(i)}, False, 40, 50, -1.0, -0.3, 10, -0.5, -0.5, 0.8,
                        se_net_pct=0.2) for i in range(1, 53)]
    rows = [base, lucky] + fillers
    assert not lucky.proven(len(rows))
    text = format_sweep_report(rows, cfg, data, kind="giris")
    assert "✅ En tutarlı aday" not in text and "şans eleğinden" in text and "≈ işaretli 1 ayar" in text
    strong = SweepRow(st, False, 120, 70, 6.0, 0.9, 5, 3.0, 3.0, 2.0, se_net_pct=0.1)
    assert strong.proven(len(rows))
    text2 = format_sweep_report([base, strong] + fillers, cfg, data, kind="giris")
    assert "✅ En tutarlı aday" in text2 and "MIN_ROOM_RATIO=1" in text2


def test_sweep_critical_value_uses_student_t_for_few_trades():
    from backtest import sweep_critical, sweep_z
    assert sweep_z(60) == pytest.approx(3.34, abs=0.01)
    assert sweep_critical(54, 30) == pytest.approx(3.69, abs=0.03)    # t(29) Bonferroni 54
    assert sweep_critical(54, 50) == pytest.approx(3.53, abs=0.03)
    assert sweep_z(54) < sweep_critical(54, 1000) < sweep_critical(54, 100) < sweep_critical(54, 30)


def test_lucky_mark_requires_positive_per_trade_result(cfg):
    from backtest import BacktestData, SweepRow, format_sweep_report
    data = BacktestData(series={}, btc=None, regime=None, min_notional=5, start_ms=0, end_ms=10 * 86_400_000,
                        n=2880, k_start=288, days=9, notes=[])
    st = {"REGIME_SKIP_WEAK": False, "MAX_24H_DROP_PCT": 15.0, "MIN_SIGNAL_SCORE": 0.0, "MIN_ROOM_RATIO": 0.5}
    # USDT toplamı pozitif ama işlem başı ortalama negatif (pozisyon büyüklüğü değiştiği için)
    odd = SweepRow(st, False, 56, 60, 0.45, -0.03, 8, 0.2, 0.25, 1.1, se_net_pct=0.1)
    base = SweepRow({**st, "MIN_ROOM_RATIO": 0.0}, True, 111, 58, -5.6, -0.65, 51, -5.6, 0.0, 0.6, se_net_pct=0.2)
    text = format_sweep_report([base, odd], cfg, data, kind="giris")
    assert "kârlı: 0" in text and "≈" not in text.split("━━━━━━━━━━━━━━━━")[3] and "HİÇBİRİ kârlı değil" in text


def test_sweep_csv_is_turkish_excel_friendly(cfg, tmp_path):
    import csv
    from backtest import SweepRow, save_sweep_csv
    st = {"TAKE_PROFIT_PCT": 3.5, "STOP_LOSS_PCT": 2.0, "TRAILING_ACTIVATION_PCT": 1.0, "TRAILING_FLOOR_PCT": 0.5,
          "MAX_HOLD_MINUTES": 120, "MIN_ROOM_RATIO": 0.0}
    path = save_sweep_csv([SweepRow(st, True, 40, 61.3, -1.5345, -0.287, 12.5, -0.5, -1.0, 1.234, 0.11)],
                          tmp_path / "t.csv")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        header, row = list(csv.reader(fh, delimiter=";"))
    cells = dict(zip(header, row))
    assert cells["TAKE_PROFIT_PCT"] == "3,5" and cells["MAX_HOLD_MINUTES"] == "120"
    assert cells["islem_basi_net_yuzde"] == "-0,287" and cells["kar_faktoru"] == "1,234"
    assert cells["net_usdt"] == "-1,5345" and cells["mevcut"] == "evet"
    assert not any("." in v for v in row)                     # Hiçbir sayı noktayla yazılmadı


def test_entry_sweep_regime_variants_and_own_thresholds(cfg, make_client, tmp_path, monkeypatch):
    import backtest as bt
    from backtest import entry_combos, prepare_backtest, run_entry_sweep
    from strategy import Signal
    no_regime = replace(cfg, regime_filter=False)
    assert {c["REGIME_SKIP_WEAK"] for c, _ in entry_combos(no_regime, [0.0])} == {False}
    assert {c["REGIME_SKIP_WEAK"] for c, _ in entry_combos(cfg, [0.0], regime_available=False)} == {False}
    from config import _setting_notes
    assert any("REGIME_SKIP_WEAK" in n for n in _setting_notes(replace(no_regime, regime_skip_weak=True)))

    client = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    data = prepare_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                            symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)
    shared = {("rebound", "ZZZUSDT", i, False): Signal("ZZZUSDT", 1.0, 99.0, {}) for i in range(50)}  # Önceki --tara
    seen = {}
    real = bt.score_thresholds

    def spy(cache):
        seen["keys"] = set(cache)
        return real(cache)
    monkeypatch.setattr(bt, "score_thresholds", spy)
    run_entry_sweep(cfg, data, signal_cache=shared)
    assert seen["keys"] and not any(k[1] == "ZZZUSDT" for k in seen["keys"])   # Eşikler yalnızca kendi sinyallerinden
    assert any(k[1] == "ABCUSDT" for k in shared)                              # Ortak önbelleğe de eklendi
