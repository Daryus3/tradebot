"""Büyük coin trendi (ROTATION_COINS) ve BNB ile ödenen komisyon (BNB_FEES).

Küçük sermayede BTC / ETH'nin LOT adımı sermayenin %0.5'inden (MAX_DUST_PCT) büyüktür: komisyon alınan coinden
kesilirse satışta bir adım altı "toz" kalır, bot bu yüzden almaz. Komisyon BNB'den ödeniyorsa coin azalmaz, toz
kalmaz: BNB_FEES=true ile canlı, test modu ve backtest aynı şekilde alır.
"""
from dataclasses import replace
from decimal import Decimal

import pytest

import backtest
from config import ConfigError, fee_from_coin, rotation_coin_list
from exchange import PaperExchange, parse_symbol_rules
from risk import plan_entry
from strategy import make_strategy

from conftest import FakeClient
from test_bnb_fees import live_bot, make_fake
from test_rotation import DailyClient, _prepare, _series, rcfg  # noqa: F401 - rcfg fikstürü
from test_rotation import DAYS, END_TIME_MS
from test_risk import make_rules


def btc_rules():
    d = Decimal
    rules = make_rules(step="0.00001")
    return replace(rules, symbol="BTCUSDT", base_asset="BTC", tick_size=d("0.01"))


def test_settings_parse_and_validate(cfg):
    assert rotation_coin_list(replace(cfg, rotation_coins=" btc, ETHUSDT ,BTC")) == ("BTC", "ETH")
    assert rotation_coin_list(cfg) == ()
    assert fee_from_coin(cfg) and not fee_from_coin(replace(cfg, bnb_fees=True))
    with pytest.raises(ConfigError):
        replace(cfg, rotation_coins="BTC;ETH").validate()


def test_dust_rule_only_when_the_fee_comes_from_the_coin(cfg):
    """34 USDT ile 78.000 $'lık BTC: bir LOT adımı (0.00001 BTC = 0.78 $) sermayenin %2.3'ü."""
    small = replace(cfg, start_capital=34.0, capital_usage_pct=100.0)
    refused = plan_entry(34.0, btc_rules(), 78_000.0, small)
    assert not refused.ok and refused.reason.startswith(backtest.DUST_REASON)
    assert plan_entry(34.0, btc_rules(), 78_000.0, replace(small, bnb_fees=True)).ok


def test_paper_buy_with_bnb_fees_keeps_the_whole_coin(cfg):
    client = FakeClient({"BTCUSDT": 78_000.0})
    coin = PaperExchange(client, {"USDT": 34.0}, "USDT", 0.00075)
    bnb = PaperExchange(client, {"USDT": 34.0}, "USDT", 0.00075, bnb_fees=True)
    for ex in (coin, bnb):
        ex.rules = {"BTCUSDT": btc_rules()}
    a = coin.market_buy("BTCUSDT", 34.0)
    assert a.qty == pytest.approx(0.00043 * (1 - 0.00075))          # Komisyon coinden: 0.00042'si satılabilir
    assert coin.sellable_qty("BTCUSDT", a.qty) == pytest.approx(0.00042)
    b = bnb.market_buy("BTCUSDT", 34.0)
    assert b.qty == pytest.approx(0.00043) and bnb.sellable_qty("BTCUSDT", b.qty) == pytest.approx(0.00043)
    assert b.usdt == pytest.approx(0.00043 * 78_000 * 1.00075)      # Maliyet komisyonu içerir (canlı BNB dolumu gibi)
    assert b.fee_usdt == pytest.approx(0.00043 * 78_000 * 0.00075)
    assert bnb.free_balance("USDT") == pytest.approx(34.0 - b.usdt) and bnb.free_balance("USDT") >= 0
    sell = bnb.market_sell("BTCUSDT", b.qty)
    assert sell.qty == pytest.approx(0.00043)                        # Tamamı satıldı: toz yok
    assert bnb.free_balance("BTC") == pytest.approx(0.0)


def test_backtest_buys_expensive_coins_only_with_bnb_fees(rcfg, fake_client, tmp_path):
    """Toz sınırı her alımı reddederken (sermaye coinin LOT adımına küçük) BNB_FEES ile alım yapılır."""
    tight = replace(rcfg, rotation_top_n=1, rotation_lookback_days=14, start_capital=11.0, max_dust_pct=0.0001)
    data = _prepare(tight, DailyClient(fake_client, _series()).client, tmp_path)
    assert not backtest.simulate(tight, data).trades
    paid = backtest.simulate(replace(tight, bnb_fees=True), data)
    assert paid.trades and not backtest.plan_rejections(paid.stats)
    first = paid.trades[0]
    assert first.cost <= tight.start_capital + 1e-9                  # Komisyon da aynı bütçeden ödendi
    normal = backtest.simulate(replace(tight, max_dust_pct=0.5), data)
    assert [t.symbol for t in normal.trades] == [t.symbol for t in paid.trades]   # Aynı işlemler, toz yok


def test_rotation_with_a_fixed_coin_list(rcfg, fake_client, tmp_path):
    """ROTATION_COINS: yalnızca seçilen coinler indirilir ve sıralanır (en güçlüsü başka olsa da)."""
    cfg = replace(rcfg, rotation_top_n=1, rotation_lookback_days=14, start_capital=11.0, rotation_coins="NEXT")
    client = DailyClient(fake_client, _series()).client
    data = backtest.prepare_backtest(cfg, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                                     end_time_ms=END_TIME_MS)
    assert list(data.series) == ["NEXTUSDT"]
    result = backtest.simulate(cfg, data)
    assert result.trades and {t.symbol for t in result.trades} == {"NEXTUSDT"}
    free = backtest.simulate(replace(cfg, rotation_coins=""), _prepare(cfg, client, tmp_path / "all"))
    assert free.trades[0].symbol == "LEADUSDT"                        # Listesiz: en güçlü coin (LEAD) alınır


def test_live_rotation_universe_uses_the_coin_list(cfg):
    prices = {"BTCUSDT": 78_000.0, "ETHUSDT": 3_000.0, "SOLUSDT": 150.0}
    client = FakeClient(prices)
    rules = parse_symbol_rules(client.exchange_info(), "USDT")
    tickers = [{"symbol": s, "lastPrice": str(p), "quoteVolume": str(v)}
               for (s, p), v in zip(prices.items(), (9e9, 5e9, 7e9))]
    rot = replace(cfg, strategy="rotation")
    assert make_strategy(client, rot).universe(tickers, rules, set()) == ["BTCUSDT", "SOLUSDT", "ETHUSDT"]
    big = replace(rot, rotation_coins="BTC,ETH", rotation_universe=1)   # Liste varken evren sayısı uygulanmaz
    assert make_strategy(client, big).universe(tickers, rules, set()) == ["BTCUSDT", "ETHUSDT"]


def test_tournament_has_the_big_coin_trend_rows():
    rows = backtest.TOURNAMENT_ROTATIONS
    assert rows["BTC trendi"]["ROTATION_COINS"] == "BTC" and rows["BTC/ETH trendi"]["ROTATION_COINS"] == "BTC,ETH"
    assert rows["haftalık rotasyon"] == {"ROTATION_COINS": ""}        # .env'de liste olsa da en hacimliler


def test_live_warns_when_bnb_fees_is_set_but_binance_takes_the_coin(cfg, tmp_path):
    _bot, notes = live_bot(cfg, tmp_path, make_fake(bnb=0.05, enabled=False), bnb_fees=True)
    assert any("BNB_FEES=true ama" in m for m in notes.messages)
    _bot, ok = live_bot(cfg, tmp_path / "ok", make_fake(bnb=0.05, enabled=True), bnb_fees=True)
    assert not any("BNB_FEES=true ama" in m for m in ok.messages)


def test_coin_list_caps_how_many_coins_are_held(rcfg, fake_client, tmp_path, cfg):
    """Listede tek coin varken ROTATION_TOP_N=3 sermayenin üçte ikisini boşta bekletirdi: en fazla listedeki kadar
    coin tutulur (canlı, backtest ve tarama aynı)."""
    from config import _setting_notes, rotation_hold_count
    one = replace(rcfg, rotation_coins="LEAD", rotation_top_n=3, rotation_lookback_days=14, start_capital=11.0)
    assert rotation_hold_count(one) == 1 and rotation_hold_count(replace(one, rotation_coins="")) == 3
    assert any("ROTATION_COINS'te 1 coin" in n for n in _setting_notes(one))
    client = DailyClient(fake_client, _series()).client
    data = backtest.prepare_backtest(one, client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                                     end_time_ms=END_TIME_MS)
    three = backtest.simulate(one, data)
    single = backtest.simulate(replace(one, rotation_top_n=1), data)
    assert three.trades and [(t.symbol, t.cost) for t in three.trades] == [(t.symbol, t.cost) for t in single.trades]
    assert three.trades[0].cost == pytest.approx(one.start_capital, rel=0.01)   # Sermayenin tamamı
    tops = {st["ROTATION_TOP_N"] for st, _ in backtest.entry_combos(replace(one, rotation_coins="BTC,ETH",
                                                                            rotation_top_n=1), [0.0])}
    assert tops == {1, 2}                                     # 2 coinlik listede 3 coin tutmak denenmez
    assert "coinler: BTC, ETH" in backtest._rotation_summary(replace(one, rotation_coins="BTC,ETH"))
    assert "ilk giriş %50" in backtest._rotation_summary(replace(one, capital_usage_pct=50.0))   # Yarısı yatırılır
    assert "ilk giriş" not in backtest._rotation_summary(replace(one, capital_usage_pct=100.0))


def _small_live_bot(rcfg, fake_client, tmp_path, bnb_fees):
    """34 USDT, gerçek LOT adımlarıyla (BTC 0.00001, ETH 0.0001) BTC/ETH trend rotasyonu, test modunda."""
    from state import BotState, StateStore, TradeLog
    from strategy import RotationStrategy
    from test_bot_flow import CaptureNotifier
    from test_rotation import T0, LiveDaily, rising
    from trader import TradingBot
    cfg = replace(rcfg, start_capital=34.0, rotation_coins="BTC,ETH", rotation_top_n=2, max_open_positions=2,
                  rotation_lookback_days=56, bnb_fees=bnb_fees, fee_rate=0.00075)
    closes = {"BTCUSDT": rising(n=120, f=1.003, start=60_000.0), "ETHUSDT": rising(n=120, f=1.004, start=2_000.0),
              "SOLUSDT": rising(n=120, f=1.02, start=100.0)}          # En güçlüsü ama listede yok
    feed = LiveDaily(fake_client, closes)
    plain = feed.client.exchange_info

    def info():
        data = plain()
        for s in data["symbols"]:
            step = {"BTCUSDT": "0.00001000", "ETHUSDT": "0.00010000"}.get(s["symbol"])
            for f in s["filters"]:
                if step and f["filterType"] == "LOT_SIZE":
                    f["stepSize"] = f["minQty"] = step
        return data
    feed.client.exchange_info = info
    state = BotState.new("paper", cfg.start_capital)
    exchange = PaperExchange(feed.client, state.paper_balances, fee_rate=cfg.fee_rate, bnb_fees=cfg.bnb_fees)
    clock = [T0]
    bot = TradingBot(cfg, exchange, RotationStrategy(feed.client, cfg), CaptureNotifier(),
                     StateStore(tmp_path / "s.json"), state, TradeLog(tmp_path / "t.csv"), time_fn=lambda: clock[0])
    bot.startup()
    bot.running = True
    bot.tick()
    return bot


def test_live_big_coin_trend_buys_btc_and_eth_with_small_capital(rcfg, fake_client, tmp_path):
    """Önerilen ayarlarla (BTC+ETH, 2 coin, 56 gün, BNB komisyonu) 34 USDT'lik test modu botu ilk dengelemede iki
    coini de yarı yarıya alır, satışta toz kalmaz; BNB komisyonu kapalıyken toz sınırı yüzünden hiçbirini alamaz."""
    bot = _small_live_bot(rcfg, fake_client, tmp_path / "bnb", bnb_fees=True)
    assert set(bot.state.positions) == {"BTCUSDT", "ETHUSDT"}
    for pos in bot.state.positions.values():
        assert 15.0 <= pos.cost_usdt <= 17.05
        assert bot.exchange.sellable_qty(pos.symbol, pos.qty) == pytest.approx(pos.qty)   # Tamamı satılabilir
    assert not _small_live_bot(rcfg, fake_client, tmp_path / "coin", bnb_fees=False).state.positions
