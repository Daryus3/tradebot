"""Pozitif RSI uyumsuzluğuyla alım: rsi_divergence stratejisi (backtest + canlı), ana stratejinin yanında uyumsuzluk
alımı (RSI_DIV_TRADE) ve diğer stratejilere teyit filtresi (RSI_DIV_FILTER)."""
import time
from dataclasses import replace

import pytest

import backtest
from backtest import TOURNAMENT_ENTRIES, ab_combos, entry_combos, simulate, strategy_combos
from config import MEAN_REVERSION_PROFILE, ConfigError, _setting_notes, divergence_trading, load_config, \
    strategy_family, with_strategy
from entry_filters import NO_DIVERGENCE, candle_filters_on, candle_reject
from market_scan import format_divergences
from position_rules import bullish_divergence
from risk import ExitReason
from strategy import make_strategy

from test_backtest import no_pause  # noqa: F401 - istek beklemesi kapalı
from test_bot_flow import make_bot
from test_smart import answer, last_buttons, press
from test_swing import DAYS, END_TIME_MS, H, N_BARS, START_MS, HourClient, k, uptrend


def div_rows(confirm=1.02, vol=600_000.0, n=320, t=0, p0=100.0, base_vol=200_000.0):
    """Yükseliş trendi → sert düşüş (RSI çok düşük, ilk dip) → tepki → yavaş ve dalgalı daha düşük dip (RSI daha
    yüksek) → dönüş mumu (dibin üstünden açılıp `confirm` kadar yükselir) → açık mum."""
    rows, t, p = uptrend(n, drift=1.002, t=t, p=p0, vol=base_vol)

    def bar(o, c, v=base_vol):
        nonlocal t
        rows.append(k(t, o, max(o, c) * 1.002, min(o, c) * 0.998, c, vol=v))
        t += H
    for _ in range(10):
        o = p
        p *= 0.985
        bar(o, p)
    for _ in range(8):
        o = p
        p *= 1.006
        bar(o, p)
    for i in range(11):
        o = p
        p *= 0.988 if i % 2 == 0 else 1.003
        bar(o, p)
    o = p * 1.002
    p = o * confirm
    bar(o, p, vol)
    rows.append(k(t, p, p * 1.001, p * 0.999, p, vol=30_000))
    return rows


def strat(cfg, **kw):
    return make_strategy(None, replace(cfg, strategy="rsi_divergence", **kw))


# ------------------------------------------------------------------ uyumsuzluk kuralı
def test_bullish_divergence_reports_structure_and_recency():
    rows = div_rows()[:-1]
    found = bullish_divergence(rows)
    assert found and found["bars_since_low"] == 1 and found["price_drop_pct"] < 0
    assert found["low"] == pytest.approx(float(rows[-2][3])) and found["peak"] > found["low"] * 1.03
    assert bullish_divergence(rows, oversold=20) is None                  # İlk dipte RSI ~24: eşik daha sıkı
    assert bullish_divergence(rows, rise=10) is None                       # RSI yalnızca ~6 puan yükseldi
    later = rows + [k(rows[-1][0] + i * H, 1, 1e9, 1e9, 1e9) for i in range(1, 7)]   # Dip 7 mum geride kaldı
    assert bullish_divergence(later) is None and bullish_divergence(later, recent=12)


# ------------------------------------------------------------------ strateji
def test_divergence_strategy_signal_and_rejections(cfg):
    st = strat(cfg)
    assert st.interval == st.bar_interval == "1h" and st.uses_room and st.candidate_limit == cfg.rsi_div_universe
    sig, why = st.analyze("ABCUSDT", div_rows())
    assert why == "" and sig.metrics["kind"] == "rsi_divergence"
    assert sig.metrics["rsi_low"] > sig.metrics["rsi_prev"] and 0 < sig.metrics["retrace"] <= 0.6
    assert sig.metrics["room_pct"] > 0 and sig.metrics["vol_ratio"] >= 1
    assert "RSI uyumsuzluğu" in sig.note() and "saat arayla" in sig.note()
    assert st.analyze("ABCUSDT", div_rows(confirm=1.0))[1] == "son mum kırmızı"
    assert st.analyze("ABCUSDT", div_rows(confirm=1.005))[1] == "dönüş teyidi yok"
    assert st.analyze("ABCUSDT", div_rows(confirm=1.04))[1] == "toparlanma bitmiş"
    assert st.analyze("ABCUSDT", div_rows(vol=150_000))[1] == "hacim zayıf"
    assert st.analyze("ABCUSDT", uptrend(400)[0])[1] == "uyumsuzluk yok"
    assert st.analyze("ABCUSDT", div_rows()[:200])[1] == "veri az"
    # Açık mum uyumsuz dibin altına indiyse / fiyat dönüş mumundan kaçtıysa alınmaz
    rows = div_rows()
    low = float(rows[-3][3])                                               # Uyumsuz dip (dönüş mumundan önceki)
    rows[-1] = k(rows[-1][0], low, low, low * 0.99, low * 0.995)
    assert st.analyze("ABCUSDT", rows)[1] == "dip sürüyor"
    rows = div_rows()
    p = float(rows[-2][4]) * 1.015
    rows[-1] = k(rows[-1][0], p, p, p, p)
    assert st.analyze("ABCUSDT", rows)[1] == "fiyat kaçtı"
    # Zayıf piyasada daha derin aşırı satım istenir; trend filtresi yalnızca açıkken uygulanır
    assert strat(cfg, rsi_div_oversold=26).analyze("ABCUSDT", div_rows())[1] == ""
    assert strat(cfg, rsi_div_oversold=26).analyze("ABCUSDT", div_rows(), strict=True)[1] == "uyumsuzluk yok"
    assert sig.metrics["trend_pct"] > 0
    assert strat(cfg, rsi_div_trend_filter=True).analyze("ABCUSDT", div_rows())[1] == ""
    e200_break = [r[:] for r in div_rows(n=320)]
    top = max(float(r[2]) for r in e200_break[:320])
    for r in e200_break[:320]:            # Önceki yükselişi çok yukarı taşı: fiyat EMA200'ün altında kalsın
        for i in (1, 2, 3, 4):
            r[i] = str(float(r[i]) + top)
    assert strat(cfg, rsi_div_trend_filter=True).analyze("ABCUSDT", e200_break)[1] == "trend yok"
    assert strat(cfg, rsi_div_trend_filter=False).analyze("ABCUSDT", e200_break)[1] == ""


def test_divergence_config_family_and_validation(cfg, tmp_path, monkeypatch):
    assert strategy_family("rsi_divergence") == "mr"
    div, applied = with_strategy(replace(cfg, take_profit_pct=50), "rsi_divergence")
    assert applied and div.take_profit_pct == MEAN_REVERSION_PROFILE["TAKE_PROFIT_PCT"] and div.max_hold_minutes == 2880
    assert not _setting_notes(div)
    assert "STRATEGY=rsi_divergence" in _setting_notes(replace(div, take_profit_pct=12))[0]
    for key, value in (("RSI_DIV_OVERSOLD", "5"), ("RSI_DIV_RISE", "-1"), ("RSI_DIV_UNIVERSE", "0"),
                       ("RSI_DIV_TRADE", "maybe")):
        monkeypatch.setenv(key, value)
        with pytest.raises(ConfigError):
            load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
        monkeypatch.delenv(key)
    # Zarar kes yalnızca daralabilir (özellik açıkken); kapalıyken eski değer hata vermez
    replace(cfg, rsi_div_sl_pct=50).validate()
    with pytest.raises(ConfigError):
        replace(cfg, rsi_div_trade="auto", rsi_div_sl_pct=cfg.stop_loss_pct + 1).validate()
    replace(cfg, rsi_div_trade="auto", rsi_div_sl_pct=cfg.stop_loss_pct, rsi_div_tp_pct=2).validate()
    # Ana stratejinin yanında çalışır; rotasyonda ve stratejinin kendisinde etkisizdir (uyarı verilir)
    assert divergence_trading(replace(cfg, rsi_div_trade="ask"))
    assert not divergence_trading(cfg) and not divergence_trading(replace(div, rsi_div_trade="auto"))
    rot, _ = with_strategy(cfg, "rotation")
    assert not divergence_trading(replace(rot, rsi_div_trade="auto"))
    assert any("RSI_DIV_TRADE=auto" in n for n in _setting_notes(replace(rot, rsi_div_trade="auto")))


# ------------------------------------------------------------------ backtest
def _div_series(confirm_at=470):
    def coin(vol, p0):
        rows = div_rows(n=confirm_at - 29, t=START_MS, p0=p0, vol=vol * 3, base_vol=vol)[:-1]   # Açık mumu at
        t, p = rows[-1][0] + H, float(rows[-1][4])
        for _ in range(4):                    # Dönüş: +%1 / saat → Kâr Al (+%1.5)
            o = p
            p *= 1.01
            rows.append(k(t, o, p * 1.002, o * 0.999, p, vol=vol))
            t += H
        while len(rows) < N_BARS:
            rows.append(k(t, p, p * 1.003, p * 0.997, p, vol=vol))
            t += H
        return rows
    btc = [k(START_MS + i * H, 30_000, 30_030, 29_970, 30_000, vol=5e8) for i in range(N_BARS)]
    return {"ABCUSDT": coin(400_000.0, 0.5), "BTCUSDT": btc}


def test_divergence_backtest_trades_the_confirmed_divergence(cfg, fake_client, tmp_path):
    div, _ = with_strategy(replace(cfg, regime_filter=False), "rsi_divergence")
    hc = HourClient(fake_client, _div_series())
    data = backtest.prepare_backtest(div, hc.client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                                     symbols=["ABCUSDT"], end_time_ms=END_TIME_MS)
    assert data.interval == "1h"
    res = simulate(div, data)
    assert [t.symbol for t in res.trades] == ["ABCUSDT"] and res.trades[0].pnl > 0
    assert res.trades[0].reason in (ExitReason.TAKE_PROFIT.value, ExitReason.TRAILING_STOP.value)
    assert res.trades[0].room_pct and res.trades[0].room_pct > 0          # Tepeye (iki dip arası) kalan alan
    # Aynı veri trend filtresiyle de denenebilir (A/B), sinyal önbelleği ayarlara göre ayrılır
    cache: dict = {}
    simulate(div, data, signal_cache=cache)
    simulate(replace(div, rsi_div_trend_filter=True), data, signal_cache=cache)
    assert len({key[1] for key in cache}) == 2


def test_divergence_sweeps_ab_and_tournament(cfg):
    div, _ = with_strategy(cfg, "rsi_divergence")
    entries = entry_combos(div, [0.0])
    assert {c["RSI_DIV_OVERSOLD"] for c, _ in entries} == {30.0, 35.0, 40.0}
    assert "MR_BAND_STD" not in entries[0][0]
    assert [st["STRATEGY"] for st, _ in strategy_combos(div)] == ["rsi_divergence", "mean_revert"]
    assert "rsi_divergence" in TOURNAMENT_ENTRIES
    keys = lambda c: {k for st, _ in ab_combos(c) for k in st}  # noqa: E731
    assert "RSI_DIV_TREND_FILTER" in keys(div) and "RSI_DIV_FILTER" not in keys(div)
    mr, _ = with_strategy(cfg, "mean_revert")
    assert "RSI_DIV_FILTER" in keys(mr) and "RSI_DIV_TREND_FILTER" not in keys(mr)
    assert "RSI_DIV_FILTER" in keys(cfg)                                  # 5 dk stratejisine de teyit olarak
    label = backtest._ab_label(div, {"RSI_DIV_TREND_FILTER": True})
    assert "trend" in label.lower()


# ------------------------------------------------------------------ teyit filtresi
def test_rsi_div_filter_requires_recent_divergence(cfg):
    on = replace(cfg, rsi_div_filter=True)
    assert candle_filters_on(on) and not candle_filters_on(cfg)
    assert candle_reject(on, div_rows()) == ""
    assert candle_reject(on, uptrend(120)[0]) == NO_DIVERGENCE
    assert candle_reject(cfg, uptrend(120)[0]) == ""


# ------------------------------------------------------------------ canlı: ana stratejinin yanında uyumsuzluk alımı
def div_bot(cfg, fake_client, tmp_path, capital=None, **kw):
    rows = div_rows(p0=0.5)
    price = float(rows[-1][4])

    class Client(fake_client):
        calls = []

        def tickers_24h(self):
            return [{"symbol": "DIVUSDT", "lastPrice": str(price), "priceChangePercent": "-2",
                     "quoteVolume": "90000000", "highPrice": str(price * 1.05), "lowPrice": str(price * 0.97)}]

        def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
            self.calls.append((symbol, interval, limit))
            if symbol == "DIVUSDT" and interval == "1h":
                return rows[-limit:]
            return super().klines(symbol, interval, limit, start_time, end_time)

    bot, client, notes = make_bot(replace(cfg, **kw), lambda prices: Client(prices), tmp_path, capital=capital,
                                  prices={"ABCUSDT": 1.0, "DIVUSDT": price, "BTCUSDT": 30_000.0})
    bot.strategy.symbols = []              # Ana strateji (sahte) sinyal vermesin: yalnızca uyumsuzluk taraması
    return bot, client, notes


def _hour_start(offset=120):
    return (int(time.time() // 3600) + 1) * 3600 + offset


def test_divergence_trade_auto_buys_once_per_hour(cfg, fake_client, tmp_path):
    bot, client, notes = div_bot(cfg, fake_client, tmp_path, rsi_div_trade="auto", rsi_div_tp_pct=2.0)
    now = _hour_start()
    bot.divergence_tick(now - 90)                                         # Mum kapanalı 1 dk olmadı: beklenir
    assert not client.calls
    bot.divergence_tick(now)
    pos = bot.state.positions["DIVUSDT"]
    assert pos.tp_pct == 2.0 and pos.sl_pct is None and "RSI uyumsuzluğu" in pos.signal_note
    assert any("ALIM YAPILDI" in m and "RSI uyumsuzluğu" in m for m in notes.messages)
    assert "Uyumsuzluk alımı</b> (otomatik" in bot.scan_report() and "DIVUSDT" in bot.scan_report()
    calls = len(client.calls)
    bot.divergence_tick(now + 600)                                        # Aynı saat: yeniden taranmaz
    assert len(client.calls) == calls
    bot.state.positions.clear()
    bot.divergence_tick(now + 3600)                                       # Sonraki saat: yine taranır
    assert len(client.calls) > calls


def test_tick_runs_hourly_divergence_scan_before_main_scan(cfg, fake_client, tmp_path):
    bot, client, notes = div_bot(cfg, fake_client, tmp_path, capital=40.0, rsi_div_trade="auto")
    bot.strategy.symbols = ["ABCUSDT"]                                    # Ana strateji de sinyal veriyor
    now = _hour_start()
    bot.time_fn = lambda: now
    bot.next_scan_at = 0
    bot.tick()                                                            # Bu döngü: yalnızca uyumsuzluk taraması
    assert "DIVUSDT" in bot.state.positions and "ABCUSDT" not in bot.state.positions
    assert bot._next_sleep() <= 5
    bot.tick()                                                            # Sonraki döngü: ana tarama
    assert "ABCUSDT" in bot.state.positions
    calls = len(client.calls)
    bot.tick()
    assert not any(c[0] == "DIVUSDT" and c[1] == "1h" for c in client.calls[calls:])   # Saatte bir kez


def test_divergence_scan_error_is_retried_within_the_hour(cfg, fake_client, tmp_path):
    bot, client, notes = div_bot(cfg, fake_client, tmp_path, rsi_div_trade="auto")
    real = client.tickers_24h
    client.tickers_24h = lambda: (_ for _ in ()).throw(ConnectionError("ağ yok"))
    now = _hour_start()
    with pytest.raises(ConnectionError):
        bot.divergence_tick(now)
    client.tickers_24h = real
    assert not bot.divergence_tick(now + 60)                              # Hemen değil (istek yağmuru olmasın)
    assert bot.divergence_tick(now + 360) and "DIVUSDT" in bot.state.positions


def test_divergence_trade_ask_waits_for_approval(cfg, fake_client, tmp_path):
    bot, client, notes = div_bot(cfg, fake_client, tmp_path, rsi_div_trade="ask")
    bot.divergence_tick(_hour_start())
    assert not bot.state.positions and "DIVUSDT" in bot.smart.entries
    assert "SİNYAL ONAYI" in notes.menus[-1][0] and "RSI_DIV_TRADE=ask" in notes.menus[-1][0]
    assert "15 dk" in notes.menus[-1][0]                                   # Saatlik sinyal: en az 15 dk beklenir
    answer(bot, notes, "yes")
    assert "DIVUSDT" in bot.state.positions and not bot.smart.entries


def test_divergence_trade_skips_when_paused_or_full(cfg, fake_client, tmp_path):
    bot, client, notes = div_bot(cfg, fake_client, tmp_path, rsi_div_trade="auto")
    bot.set_paused(True)
    bot.divergence_tick(_hour_start())
    assert not client.calls and not bot.state.positions
    bot.set_paused(False)
    bot.divergence_tick(_hour_start(300))                                 # Aynı saat içinde, durdurma kalkınca
    assert "DIVUSDT" in bot.state.positions
    rot, _ = div_bot(cfg, fake_client, tmp_path / "r", rsi_div_trade="auto")[:2]
    rot.cfg = replace(rot.cfg, strategy="rotation")
    rot.tick()                                                            # Rotasyonda uyumsuzluk taraması yok
    assert not any(c[1] == "1h" and c[0] == "DIVUSDT" for c in rot.exchange.client.calls)


def test_divergence_alert_text_and_menus(cfg, fake_client, tmp_path):
    hit = ("ETHUSDT", {"price_drop_pct": -1.2, "rsi_prev": 22.0, "rsi_now": 31.0, "bars_apart": 12}, 2000.0)
    assert "Uyumsuzluk alımı" in format_divergences([hit], cfg) and "STRATEGY=rsi_divergence" in format_divergences([hit])
    assert "alım için sorar" in format_divergences([hit], replace(cfg, rsi_div_trade="ask"))
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    bot.settings_path = tmp_path / ".env"
    press(bot, "smart")
    assert "Uyumsuzluk alımı" in notes.menus[-1][0] and "cy:RSI_DIV_TRADE" in last_buttons(notes)
    for expected in ("ask", "auto", "off"):
        press(bot, "cy:RSI_DIV_TRADE")
        assert bot.cfg.rsi_div_trade == expected
    assert "RSI_DIV_TRADE=off" in (tmp_path / ".env").read_text(encoding="utf-8")
    press(bot, "flt")
    assert "tg:RSI_DIV_FILTER" in last_buttons(notes)
    press(bot, "tg:RSI_DIV_FILTER")
    assert bot.cfg.rsi_div_filter
    press(bot, "strat")
    assert "stp:rsi_divergence" in last_buttons(notes)
