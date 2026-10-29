"""Zararı azaltma ayarları: takılan işlem çıkışı (STALE_EXIT_MINUTES), zarardan sonra coine bekleme
(LOSS_COOLDOWN_MINUTES) ve önceki trend filtresi (PRIOR_TREND_FILTER) — canlı bot, backtest, A/B ve menüler."""
import time
from dataclasses import replace

import pytest

import backtest
from entry_filters import NO_PRIOR_TREND, prior_trend_ok, prior_trend_reject
from risk import ExitReason, Position, cooldown_minutes, evaluate_exit

from test_backtest import (DIP_AT, END_TIME_MS, DAYS, btc_flat, dip_and_bounce, flat, make_client,  # noqa: F401
                           no_pause, run, scenario, to_raw, trend)
from test_bot_flow import make_bot, tick
from test_smart import last_buttons, press


# ------------------------------------------------------------------ saf kurallar
def test_stale_exit_rule_and_loss_cooldown(cfg):
    on = replace(cfg, stale_exit_minutes=60, stale_exit_peak_pct=1.0)
    t0 = 1_000_000.0
    pos = Position("X", "X", 1, 1, 10, t0, peak_pnl_pct=0.4)
    assert evaluate_exit(pos, -0.5, t0 + 59 * 60, on) is None                 # Süre dolmadı
    assert evaluate_exit(pos, -0.5, t0 + 60 * 60, on) == ExitReason.STALE
    assert evaluate_exit(pos, -0.5, t0 + 60 * 60, cfg) is None                # Kapalı
    pos.peak_pnl_pct = 1.2                                                    # Toparlanma başladı: kural uygulanmaz
    assert evaluate_exit(pos, 0.3, t0 + 600 * 60, on) is None
    added = Position("X", "X", 1, 1, 10, t0, peak_pnl_pct=0.0, adds=1)       # DCA ile eklenmiş: uygulanmaz
    assert evaluate_exit(added, -3.0, t0 + 600 * 60, on) is None
    assert cooldown_minutes(cfg, -1.0) == cfg.symbol_cooldown_minutes         # Kapalı: her çıkışta normal bekleme
    longer = replace(cfg, loss_cooldown_minutes=720)
    assert cooldown_minutes(longer, -0.01) == 720 and cooldown_minutes(longer, 0.5) == cfg.symbol_cooldown_minutes
    assert cooldown_minutes(replace(cfg, loss_cooldown_minutes=1), -1.0) == cfg.symbol_cooldown_minutes  # Kısaltmaz


def test_prior_trend_rule_and_live_check():
    assert prior_trend_ok(1.0, 1.1) and prior_trend_ok(1.0, 1.0) and not prior_trend_ok(1.2, 1.0)
    assert prior_trend_ok(0.0, 1.0)                                           # Veri yok: engellemez

    def hours(week_ago, day_ago, n=170):
        rows = [[i, "1", "1", "1", str(week_ago if i < n - 26 else day_ago), "0", i, "0", 0, "0", "0", "0"]
                for i in range(n)]
        return rows

    class Client:
        def __init__(self, rows, fail=False):
            self.rows, self.fail, self.calls = rows, fail, []

        def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
            self.calls.append((symbol, interval, limit))
            if self.fail:
                raise ConnectionError("ağ yok")
            return self.rows[-limit:]

    from types import SimpleNamespace
    on = SimpleNamespace(prior_trend_filter=True)
    down = Client(hours(1.2, 1.0))
    assert prior_trend_reject(on, down, "ABCUSDT") == NO_PRIOR_TREND and down.calls == [("ABCUSDT", "1h", 170)]
    assert prior_trend_reject(on, Client(hours(1.0, 1.1)), "ABCUSDT") == ""
    assert prior_trend_reject(on, Client(hours(1.2, 1.0, n=100)), "ABCUSDT") == ""   # Yeni coin: geçmiş yetmiyor
    assert prior_trend_reject(on, Client([], fail=True), "ABCUSDT") == ""             # Veri alınamadı: engellemez
    assert prior_trend_reject(SimpleNamespace(prior_trend_filter=False), down, "X") == ""


# ------------------------------------------------------------------ backtest
def test_backtest_stale_exit_cuts_trades_that_never_recover(cfg, make_client, tmp_path):
    client = make_client({"ABCUSDT": scenario(lambda p: (flat(30, p), p)), "BTCUSDT": btc_flat()})
    base = run(cfg, client, tmp_path)
    assert base.trades and base.trades[0].reason != ExitReason.STALE.value and base.trades[0].peak_pct < 2
    on = replace(cfg, stale_exit_minutes=60, stale_exit_peak_pct=2.0)   # İşlem en fazla ~+%1.2 gördü
    stale = run(on, client, tmp_path)
    t = stale.trades[0]
    assert t.reason == ExitReason.STALE.value and 3600 <= t.duration_sec <= 3600 + 300
    assert "🧊 Takılan işlem" in backtest.format_backtest_report(stale, on)
    assert run(replace(on, stale_exit_peak_pct=1.0), client, tmp_path).trades[0].reason != ExitReason.STALE.value
    up = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    winner = run(replace(cfg, stale_exit_minutes=60), up, tmp_path / "u")
    assert winner.trades[0].reason == ExitReason.TAKE_PROFIT.value          # Toparlanan işleme dokunmaz


def _two_dips():
    """Düşüş + toparlanma → çöküş (zarar kes) → 2 saat yatay → yeni düşüş + toparlanma → yükseliş."""
    bars = flat(DIP_AT, 1.0)
    dip, p = dip_and_bounce(1.0)
    bars += dip
    crash, p = trend(p, 0.97, 8)
    bars += crash + flat(24, p)
    dip2, p = dip_and_bounce(p)
    bars += dip2
    up, p = trend(p, 1.03, 8)
    bars += up
    return to_raw(bars + flat(2000, p))


def test_backtest_loss_cooldown_blocks_reentry_after_a_loss(cfg, make_client, tmp_path):
    client = make_client({"ABCUSDT": _two_dips(), "BTCUSDT": btc_flat()})
    loose = replace(cfg, max_24h_drop_pct=60.0)            # Çöküşten sonra da 24 saatlik düşüş aralığında kalsın
    base = run(loose, client, tmp_path)
    assert len(base.trades) == 2 and base.trades[0].pnl < 0, [(t.reason, t.pnl) for t in base.trades]
    cooled = run(replace(loose, loss_cooldown_minutes=720), client, tmp_path)
    assert len(cooled.trades) == 1                          # Zarardan sonra 12 saat aynı coine girilmedi


def test_backtest_prior_trend_filter_skips_falling_knives(cfg, make_client, tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "PRIOR_TREND_DAYS", 2)    # Test verisi 3.5 gün: "7 gün" yerine 2 gün
    falling = scenario(lambda p: trend(p, 1.03, 8))
    for row in falling[:300]:                              # İki gün önce fiyat daha yüksekti (düşüş trendi)
        for i in (1, 2, 3, 4):
            row[i] = str(float(row[i]) * 1.2)
    client = make_client({"ABCUSDT": falling, "BTCUSDT": btc_flat()})
    assert run(cfg, client, tmp_path).trades                # Filtre kapalıyken alınır
    blocked = run(replace(cfg, prior_trend_filter=True), client, tmp_path)
    assert not blocked.trades and blocked.stats.get(f"filtre:{NO_PRIOR_TREND}", 0) > 0
    rising = make_client({"ABCUSDT": scenario(lambda p: trend(p, 1.03, 8)), "BTCUSDT": btc_flat()})
    assert run(replace(cfg, prior_trend_filter=True), rising, tmp_path / "r").trades   # Yatay / yükselen: geçer


def test_ab_test_tries_the_new_rows(cfg):
    keys = {k for st, _ in backtest.ab_combos(cfg) for k in st}
    assert {"PRIOR_TREND_FILTER", "LOSS_COOLDOWN_MINUTES", "STALE_EXIT_MINUTES"} <= keys
    stale = [st["STALE_EXIT_MINUTES"] for st, _ in backtest.ab_combos(cfg) if "STALE_EXIT_MINUTES" in st]
    assert stale == [60, 120, 240]
    on = [st["STALE_EXIT_MINUTES"] for st, _ in backtest.ab_combos(replace(cfg, stale_exit_minutes=120))
          if "STALE_EXIT_MINUTES" in st]
    assert on == [60, 240, 0]
    assert backtest._ab_label(cfg, {"STALE_EXIT_MINUTES": 120}) == "Takılan işlem çıkışı: kapalı → 120 dk"
    assert backtest._ab_label(cfg, {"LOSS_COOLDOWN_MINUTES": 720}) == "Zarardan sonra coine bekleme: kapalı → 720 dk"
    rot, _ = __import__("config").with_strategy(cfg, "rotation")
    assert not {"STALE_EXIT_MINUTES", "PRIOR_TREND_FILTER"} & {k for st, _ in backtest.ab_combos(rot) for k in st}


# ------------------------------------------------------------------ canlı bot ve menüler
def test_live_stale_exit_and_loss_cooldown(cfg, fake_client, tmp_path):
    bot, client, notes = make_bot(replace(cfg, stale_exit_minutes=60, loss_cooldown_minutes=720), fake_client,
                                  tmp_path)
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    start = pos.opened_at
    bot.time_fn = lambda: start + 30 * 60
    tick(bot)
    assert "ABCUSDT" in bot.state.positions                 # 30 dk: henüz değil
    bot.time_fn = lambda: start + 61 * 60
    tick(bot)
    assert "ABCUSDT" not in bot.state.positions and any("TAKILAN İŞLEM" in m for m in notes.messages)
    # Fiyat yatay: komisyonla zararda kapandı → 12 saat aynı coine girilmez
    assert bot.state.cooldowns["ABCUSDT"] == pytest.approx(start + 61 * 60 + 720 * 60, abs=1)


def test_menus_offer_the_new_settings(cfg, fake_client, tmp_path):
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    bot.settings_path = tmp_path / ".env"
    press(bot, "smart")
    assert "Takılan işlem çıkışı" in notes.menus[-1][0] and "stx:120" in last_buttons(notes)
    press(bot, "stx:120")
    assert bot.cfg.stale_exit_minutes == 120 and "STALE_EXIT_MINUTES=120" in (tmp_path / ".env").read_text("utf-8")
    press(bot, "risk")
    assert "Zarardan sonra bekleme" in notes.menus[-1][0] and "lcd:720" in last_buttons(notes)
    press(bot, "lcd:720")
    assert bot.cfg.loss_cooldown_minutes == 720
    press(bot, "flt")
    press(bot, "tg:PRIOR_TREND_FILTER")
    assert bot.cfg.prior_trend_filter
    from backtest_runner import PRESETS, TOGGLES
    assert "stale" in PRESETS and "lcool" in PRESETS and "PRIOR_TREND_FILTER" in dict(TOGGLES)


def test_live_scan_applies_prior_trend_filter(cfg, fake_client):
    """Canlı tarama sinyal veren coinde 1 saatlik mumlarla önceki trende bakar (backtest'teki kuralın aynısı)."""
    from exchange import parse_symbol_rules
    from strategy import make_strategy
    from test_mean_revert import dip_rows
    rows = dip_rows()
    price = float(rows[-1][4])

    class Client(fake_client):
        def __init__(self, week_ago):
            super().__init__({"ABCUSDT": price, "BTCUSDT": 30_000.0})
            self.week_ago = week_ago

        def tickers_24h(self):
            return [{"symbol": "ABCUSDT", "lastPrice": str(price), "priceChangePercent": "-3",
                     "quoteVolume": "90000000", "highPrice": str(price * 1.08), "lowPrice": str(price * 0.99)}]

        def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
            if symbol == "BTCUSDT":
                return [[i * 300_000, "30000", "30000", "30000", "30000", "0", 0, "0", 0, "0", "0", "0"]
                        for i in range(limit)]
            if limit == 170:            # Önceki trend sorgusu: 7 gün önce bu fiyat, 1 gün önce şimdiki fiyat
                return [[i, "1", "1", "1", str(self.week_ago if i < 144 else price), "0", i, "0", 0, "0", "0", "0"]
                        for i in range(170)]
            return rows[-limit:]

    on = replace(cfg, strategy="mean_revert", regime_filter=False, mr_universe=1, prior_trend_filter=True)
    for week_ago, expected in ((price * 1.3, []), (price * 0.8, ["ABCUSDT"])):
        client = Client(week_ago)
        rules = parse_symbol_rules(client.exchange_info(), "USDT")
        result = make_strategy(client, on).scan(rules, set(), 0.0)
        assert [s.symbol for s in result.signals] == expected, result.rejects
    assert result.rejects.get(NO_PRIOR_TREND, 0) == 0
    blocked = make_strategy(Client(price * 1.3), on).scan(rules, set(), 0.0)
    assert blocked.rejects[NO_PRIOR_TREND] == 1
