"""BNB ile komisyon indirimi takibi: oran okuma, FEE_RATE karşılaştırması, az BNB uyarısı, komisyon tahmini.

Bot yalnızca izler ve uyarır; BNB almaz ve Binance'teki "BNB ile komisyon öde" ayarını değiştirmez.
"""
import time
from dataclasses import replace

import pytest

from commands import Command
from config import ConfigError, load_config
from exchange import FeeInfo, LiveExchange, parse_fee_info
from state import BotState, StateStore, TradeLog
from trader import FEE_CHECK_SEC, FEE_WARN_COOLDOWN_SEC, TradingBot

from fake_binance import FakeBinance
from test_bot_flow import CaptureNotifier
from test_live_features import OneShotStrategy


def make_fake(bnb=0.05, enabled=True, with_price=True):
    """BNB indirimi açık (enabled=True), kapalı (False) veya yanıtta hiç yok (None) bir sahte hesap."""
    prices = {"ABCUSDT": 1.0}
    if with_price:
        prices["BNBUSDT"] = 600.0
    fake = FakeBinance(prices)
    if bnb:
        fake.free["BNB"] = bnb
    if enabled is not None:
        fake.set_bnb_discount(account=enabled)
    return fake


def live_bot(cfg, tmp_path, fake, fee_rate=0.001, startup=True, **changes):
    cfg_live = replace(cfg, trading_mode="live", fee_rate=fee_rate, **changes)
    notes = CaptureNotifier()
    bot = TradingBot(cfg_live, LiveExchange(fake, fee_rate=fee_rate), OneShotStrategy(fake, ["ABCUSDT"]), notes,
                     StateStore(tmp_path / "state.json"), BotState.new("live", 11.0), TradeLog(tmp_path / "trades.csv"))
    if startup:
        bot.startup()
    bot.running = True
    return bot, notes


def fee_msgs(notes):
    return [m for m in notes.messages if "BNB" in m or "FEE_RATE" in m]


# ------------------------------------------------------------------ fee_info
def test_fee_info_discount_active():
    fake = make_fake(bnb=0.05)
    ex = LiveExchange(fake)
    info = ex.fee_info("BTCUSDT")
    assert info.taker == pytest.approx(0.001)
    assert info.discount == pytest.approx(0.25)
    assert info.bnb_enabled and info.bnb_active
    assert info.bnb_free == pytest.approx(0.05)
    assert info.bnb_usdt == pytest.approx(600.0)
    assert info.effective_rate == pytest.approx(0.00075)     # %0.1 × (1 − 0.25)
    assert ex.last_fee_info is info
    # 30 USDT'lik BNB, 11 USDT'lik al-satta (2 × 11 × %0.075 ≈ 0.0165 USDT) ≈ 1818 işleme yeter
    assert info.bnb_trades(11.0) == pytest.approx(30 / (2 * 11 * 0.00075))


def test_fee_info_without_discount_block():
    info = LiveExchange(make_fake(enabled=None)).fee_info()
    assert info.discount == 0 and not info.bnb_enabled
    assert info.effective_rate == pytest.approx(0.001)


@pytest.mark.parametrize("account,symbol", [(False, True), (True, False), ("false", "true")])
def test_fee_info_discount_disabled_flags(account, symbol):
    fake = make_fake()
    fake.set_bnb_discount(account=account, symbol=symbol)
    info = LiveExchange(fake).fee_info()
    assert not info.bnb_enabled and not info.bnb_active
    assert info.effective_rate == pytest.approx(0.001)


def test_fee_info_non_bnb_discount_asset_is_ignored():
    fake = make_fake()
    fake.set_bnb_discount(asset="XYZ")
    info = LiveExchange(fake).fee_info()
    assert info.discount == 0 and not info.bnb_enabled
    assert info.effective_rate == pytest.approx(0.001)


def test_fee_info_zero_bnb_falls_back_to_full_rate():
    info = LiveExchange(make_fake(bnb=0.0)).fee_info()
    assert info.bnb_enabled and not info.bnb_active
    assert info.bnb_free == 0 and info.bnb_usdt == 0
    assert info.effective_rate == pytest.approx(0.001)       # Binance BNB yoksa tam oranı keser
    assert info.bnb_trades(11.0) == 0


def test_fee_info_unknown_bnb_price():
    info = LiveExchange(make_fake(bnb=0.05, with_price=False)).fee_info()
    assert info.bnb_usdt == 0
    assert info.effective_rate == pytest.approx(0.00075)
    assert info.bnb_trades(11.0) is None                     # Fiyat yoksa tahmin edilmez


def test_parse_fee_info_bad_discount_value():
    data = {"standardCommission": {"taker": "0.001"},
            "discount": {"enabledForAccount": True, "enabledForSymbol": True, "discountAsset": "BNB",
                         "discount": "abc"}}
    info = parse_fee_info(data, bnb_free=1.0, bnb_usdt=600.0)
    assert info.discount == 0
    assert info.effective_rate == pytest.approx(0.001)


def test_fmt_rate_shows_bnb_rate_exactly(cfg):
    from telegram_bot import Messages, fmt_rate
    assert fmt_rate(0.001) == "%0.10"
    assert fmt_rate(0.00075) == "%0.075"                     # %.2f ile "%0.07" görünürdü
    assert "Komisyon: %0.075" in Messages.started(replace(cfg, fee_rate=0.00075), 11.0, 0.0, 100)


def test_fee_rate_status():
    info = FeeInfo(0.001, 0.25, True, 1.0, 600.0)
    assert info.fee_rate_status(0.00075) == "ok"
    assert info.fee_rate_status(0.001) == "conservative"
    assert info.fee_rate_status(0.0005) == "optimistic"


# ------------------------------------------------------------------ açılış kontrolü
@pytest.mark.parametrize("fee_rate,bnb_on,expected", [
    (0.001, True, "info"),       # BNB indirimi var, bot %0.1 ile temkinli hesaplıyor → yalnızca bilgi
    (0.001, False, None),        # Standart: her şey doğru
    (0.00075, True, None),       # BNB ile ödüyor ve FEE_RATE buna göre: doğru
    (0.00075, False, "warn"),    # FEE_RATE indirimli ama indirim yok → hesap fazla iyimser
])
def test_startup_fee_check(cfg, tmp_path, fee_rate, bnb_on, expected):
    bot, notes = live_bot(cfg, tmp_path, make_fake(bnb=0.05, enabled=bnb_on), fee_rate=fee_rate)
    msgs = fee_msgs(notes)
    if expected is None:
        assert msgs == []
    elif expected == "info":
        assert len(msgs) == 1
        assert msgs[0].startswith("ℹ️") and "Uyarı" not in msgs[0]
        assert "temkinli" in msgs[0] and "FEE_RATE=0.00075" in msgs[0]
    else:
        assert len(msgs) == 1
        assert "Uyarı" in msgs[0] and "iyimser" in msgs[0]
        assert "FEE_RATE=0.001" in msgs[0] and "BNB ile komisyon ödeme kapalı" in msgs[0]
    # Tekrar kontrol (örn. saatlik) aynı mesajı hemen tekrarlamaz
    bot._check_commission()
    assert len(fee_msgs(notes)) == len(msgs)
    assert bot.fee_info is not None


def test_existing_commission_mismatch_still_reported(cfg, tmp_path):
    fake = make_fake(enabled=None)
    fake.taker = 0.00075                 # Hesabın kendi oranı düşük (BNB'siz)
    _, notes = live_bot(cfg, tmp_path, fake)
    msgs = fee_msgs(notes)
    assert len(msgs) == 1 and "FEE_RATE=0.00075" in msgs[0]


def test_bnb_ran_out_with_discounted_fee_rate(cfg, tmp_path):
    _, notes = live_bot(cfg, tmp_path, make_fake(bnb=0.0, enabled=True), fee_rate=0.00075)
    msgs = fee_msgs(notes)
    assert len(msgs) == 1                # Tek uyarı: ayrıca "BNB az" uyarısı gelmez
    assert "BNB bakiyeniz bitmiş" in msgs[0] and "FEE_RATE=0.001" in msgs[0]


def test_bnb_enabled_but_empty_with_standard_fee_rate(cfg, tmp_path):
    _, notes = live_bot(cfg, tmp_path, make_fake(bnb=0.0, enabled=True), fee_rate=0.001)
    msgs = fee_msgs(notes)
    assert len(msgs) == 1
    assert "BNB yok" in msgs[0] and "otomatik BNB almaz" in msgs[0]


# ------------------------------------------------------------------ az BNB uyarısı
def test_low_bnb_warning_is_rate_limited(cfg, tmp_path):
    fake = make_fake(bnb=0.0005)         # 0.30 USDT: 11 USDT'lik al-satta ≈18 işlem (< 20)
    bot, notes = live_bot(cfg, tmp_path, fake, fee_rate=0.00075)

    def low():
        return [m for m in notes.messages if "işlemlik" in m]

    assert len(low()) == 1
    assert "yaklaşık 18 işlemlik" in low()[0]
    assert "%0.10'a döner" in low()[0] and "otomatik BNB almaz" in low()[0]

    # Saatlik kontrol çalışır ama 6 saat dolmadan aynı uyarı tekrar gönderilmez
    calls = fake.commission_calls
    bot._periodic(time.time() + 60)
    assert fake.commission_calls == calls            # Henüz saati gelmedi
    bot._periodic(time.time() + FEE_CHECK_SEC + 1)
    assert fake.commission_calls == calls + 1
    assert len(low()) == 1

    # Bekleme süresi dolunca tekrar hatırlatılır
    bot.notifier._last_error_sent["bnb-low"] -= FEE_WARN_COOLDOWN_SEC + 1
    bot._check_commission()
    assert len(low()) == 2


def test_enough_bnb_or_disabled_threshold_no_warning(cfg, tmp_path):
    _, notes = live_bot(cfg, tmp_path, make_fake(bnb=0.0005), fee_rate=0.00075, bnb_min_trades=0)
    assert fee_msgs(notes) == []
    _, notes = live_bot(cfg, tmp_path, make_fake(bnb=0.0005), fee_rate=0.00075, bnb_min_trades=10)
    assert fee_msgs(notes) == []                      # ≈18 işlem ≥ 10


# ------------------------------------------------------------------ kapalı / test modu / hatalar
def test_paper_mode_is_noop(cfg, fake_client, tmp_path):
    from test_bot_flow import make_bot
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)   # FakeClient'ta komisyon uç noktası bile yok
    bot._check_commission()
    bot._periodic(time.time() + FEE_CHECK_SEC + 1)
    assert bot.fee_info is None
    assert fee_msgs(notes) == []
    assert "test modunda" in bot.fee_status_line()


def test_bnb_fee_check_disabled(cfg, tmp_path):
    fake = make_fake(bnb=0.0, enabled=True)
    bot, notes = live_bot(cfg, tmp_path, fake, fee_rate=0.00075, bnb_fee_check=False)
    bot._periodic(time.time() + FEE_CHECK_SEC + 1)
    assert fee_msgs(notes) == []
    assert "kapalı" in bot.fee_status_line()
    assert fake.commission_calls == 0                 # Binance'e hiç sorulmadı


def test_fee_check_errors_never_raise(cfg, tmp_path, monkeypatch):
    fake = make_fake()

    def broken(symbol):
        raise RuntimeError("bağlantı yok")

    fake.commission_rates = broken
    bot, notes = live_bot(cfg, tmp_path, fake)        # Açılış hatasız tamamlanmalı
    assert bot.fee_info is None and fee_msgs(notes) == []

    fake2 = make_fake(bnb=0.0005)
    bot2, _ = live_bot(cfg, tmp_path, fake2, fee_rate=0.00075, startup=False)
    monkeypatch.setattr(bot2, "position_size", lambda: 1 / 0)
    bot2._check_commission()                          # Bildirim hesabındaki hata da yutulur


# ------------------------------------------------------------------ BNB komisyonunun USDT karşılığı
def test_bnb_commission_converted_with_price():
    fake = make_fake(bnb=0.05)
    fake.bnb_fees = True
    ex = LiveExchange(fake, fee_rate=0.001)
    ex.refresh_rules()
    buy = ex.market_buy("ABCUSDT", 10.0)
    assert buy.qty == pytest.approx(10.0)             # Komisyon coinden değil BNB'den kesildi
    assert buy.fee_usdt == pytest.approx(10 * 0.00075)
    assert buy.usdt == pytest.approx(10 + 10 * 0.00075)


def test_bnb_commission_estimated_when_price_lookup_fails():
    fake = make_fake(bnb=0.05, with_price=False)      # BNBUSDT fiyatı alınamıyor
    fake.bnb_fees = True
    ex = LiveExchange(fake, fee_rate=0.001)
    ex.refresh_rules()

    buy = ex.market_buy("ABCUSDT", 10.0)
    assert buy.fee_usdt == pytest.approx(10 * 0.001)  # 0 değil: FEE_RATE ile tahmin
    assert buy.usdt == pytest.approx(10.01)
    order = next(iter(fake.orders.values()))
    recovered = ex.recover_order("ABCUSDT", order["clientOrderId"], "BUY")   # İşlem geçmişinden
    assert recovered.fee_usdt == pytest.approx(10 * 0.001)

    ex.fee_info()                                     # İndirim açık → indirimli oranla tahmin
    sell = ex.market_sell("ABCUSDT", buy.qty)
    assert sell.fee_usdt == pytest.approx(10 * 0.00075)
    assert sell.usdt == pytest.approx(10 - 10 * 0.00075)
    external = ex.find_sell_fills("ABCUSDT", 0)       # Bot dışı satış hesabı da aynı tahmini kullanır
    assert external.fee_usdt == pytest.approx(10 * 0.00075)


# ------------------------------------------------------------------ self-test ve /ayarlar
def _selftest(cfg, fake, fee_rate):
    from selftest import _check_api_keys
    keyed = replace(cfg, binance_api_key="k", binance_api_secret="s", fee_rate=fee_rate)
    return _check_api_keys(keyed, fake)


@pytest.mark.parametrize("fee_rate,fake_args,status,parts,absent", [
    (0.001, {"bnb": 0.05}, "OK",
     ["BNB indirimi: açık (%0.075, BNB ≈ 30.00 USDT ≈ 1818 işlem)", "FEE_RATE=0.00075"], []),
    (0.00075, {"bnb": 0.05}, "OK", ["BNB indirimi: açık (%0.075"], ["FEE_RATE="]),
    (0.00075, {"bnb": 0.05, "enabled": False}, "WARN", ["BNB indirimi: kapalı", "iyimser", "FEE_RATE=0.001"], []),
    (0.001, {"bnb": 0.05, "enabled": False}, "OK", ["komisyon %0.10", "BNB indirimi: kapalı"], ["FEE_RATE="]),
    (0.00075, {"bnb": 0.0005}, "WARN", ["~18 işleme", "otomatik BNB almaz"], []),
])
def test_selftest_bnb_line(cfg, fee_rate, fake_args, status, parts, absent):
    import selftest
    result, _, detail = _selftest(cfg, make_fake(**fake_args), fee_rate)
    assert result == getattr(selftest, status), detail
    for part in parts:
        assert part in detail
    for part in absent:
        assert part not in detail


def test_ayarlar_uses_cached_bnb_status(cfg, tmp_path):
    fake = make_fake(bnb=0.05)
    bot, notes = live_bot(cfg, tmp_path, fake, fee_rate=0.00075)
    calls = fake.commission_calls
    bot.commands.handle(Command("ayarlar"))
    assert fake.commission_calls == calls             # Son kontrolün sonucu kullanıldı
    assert "BNB indirimi: açık (%0.075, BNB ≈ 30.00 USDT ≈ 1818 işlem)" in notes.messages[-1]

    bot.fee_info = None                               # Hiç okunamamışsa en fazla bir kez sorulur
    bot.commands.handle(Command("ayarlar"))
    assert fake.commission_calls == calls + 1
    assert "BNB indirimi: açık" in notes.messages[-1]


def test_ayarlar_bnb_disabled_status(cfg, tmp_path):
    bot, notes = live_bot(cfg, tmp_path, make_fake(bnb=0.05, enabled=False))
    bot.commands.handle(Command("ayarlar"))
    assert "BNB indirimi: kapalı (komisyon %0.10)" in notes.messages[-1]


# ------------------------------------------------------------------ ayarlar
def test_bnb_config(cfg, monkeypatch, tmp_path):
    assert cfg.bnb_fee_check is True and cfg.bnb_min_trades == 20
    monkeypatch.setenv("BNB_FEE_CHECK", "false")
    monkeypatch.setenv("BNB_MIN_TRADES", "5")
    cfg = load_config(env_file=str(tmp_path / "yok.env"), mode_override="paper")
    assert cfg.bnb_fee_check is False and cfg.bnb_min_trades == 5
    monkeypatch.setenv("BNB_MIN_TRADES", "-1")
    with pytest.raises(ConfigError):
        load_config(env_file=str(tmp_path / "yok.env"), mode_override="paper")


def test_bot_does_not_trade_bnb_while_fees_are_paid_in_bnb(cfg, tmp_path):
    fake = make_fake(bnb=5.0)
    fake.prices["BNBUSDT"] = 1.0          # Ucuz fiyat: toz filtresi değil, BNB dışlaması test edilsin
    bot, _ = live_bot(cfg, tmp_path, fake)
    bot.strategy = OneShotStrategy(fake, ["BNBUSDT", "ABCUSDT"])
    bot.next_scan_at = 0
    bot.tick()
    assert "BNBUSDT" not in bot.state.positions and "ABCUSDT" in bot.state.positions

    off = make_fake(bnb=5.0, enabled=False)
    off.prices["BNBUSDT"] = 1.0
    bot2, _ = live_bot(cfg, tmp_path / "off", off)
    bot2.strategy = OneShotStrategy(off, ["BNBUSDT"])
    bot2.next_scan_at = 0
    bot2.tick()
    assert "BNBUSDT" in bot2.state.positions       # İndirim kapalıyken BNB normal bir coin
