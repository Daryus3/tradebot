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
    cfg = replace(cfg, max_total_loss=0.5)

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
    assert first.kline_calls == 4  # 1008 mum → sembol başına 2 sayfa (1000 + 8)
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
    assert client.kline_calls == 2             # BTC filtre için ayrıca indirilmedi
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
