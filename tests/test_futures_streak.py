"""Seri (FUTURES_STREAK_*; rapor 35 araştırması, 28 Kasım): dalga modunda ek pozisyonun boyu son kapanan işlemlerin işlem
başı net ortalamasına göre: soğuk seride (zararlar sürüyor) küçük, sıcak seride (dalga dönüyor) büyük. Ortak kural
(strategy), ayarlar, durum kaydı, backtest (yuvalı motor, işlem bayrağı, rapor satırları), canlı / test modunda teminat ve
pozisyon notu, Telegram satırları. Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, load_config
from runtime_settings import EDITABLE
from state import RECENT_TRADES_KEEP, BotState
from strategy import FUT_BAR_MS, FuturesSignal, streak_avg, streak_factor
from test_futures import BASE_MS, fcfg, make_fbot, params, signal, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import press
from test_futures_day_brake import LOSS, WIN, bt_data, bt_sig

MIN = fb.MIN_MS
FLAT = [(100, 100.2, 99.8, 100.0)] * 40                                  # Ne hedef ne stop: zaman stopuna kadar açık
COLD = dict(futures_streak_cold_size_pct=50.0)
HOT = dict(futures_streak_hot_size_pct=125.0)


# ====================================================================== ortak kural, ayarlar, durum
def test_shared_rule():
    assert streak_avg([1.0, 2.0, 3.0], 10) is None                              # 10 işlemde en az 5 gerekir
    assert streak_avg([-1.0] * 5 + [1.0, 2.0, 3.0, 4.0, 5.0], 10) == pytest.approx(1.0)
    assert streak_avg([9.0] + [0.5] * 10, 10) == pytest.approx(0.5)              # Yalnız son 10
    assert streak_avg([1.0, 2.0, 3.0], 5) == pytest.approx(2.0)                  # 5 işlemde en az 3
    assert streak_factor(-1.5, -1.0, 50.0, 0.8, 125.0) == pytest.approx(0.5)
    assert streak_factor(-1.0, -1.0, 50.0, 0.8, 125.0) == 1.0                    # Tam eşik soğuk değil
    assert streak_factor(0.8, -1.0, 50.0, 0.8, 125.0) == pytest.approx(1.25)     # Tam eşik sıcak
    assert streak_factor(0.3, -1.0, 50.0, 0.8, 125.0) == 1.0
    assert streak_factor(None, -1.0, 50.0, 0.8, 125.0) == 1.0
    assert streak_factor(-3.0, -1.0, 100.0, 0.8, 125.0) == 1.0                   # Soğuk taraf kapalı
    assert streak_factor(3.0, -1.0, 50.0, 0.8, 100.0) == 1.0                     # Sıcak taraf kapalı


def test_settings_defaults_validation_env_note(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_streak_trades, fcfg.futures_streak_cold_pct, fcfg.futures_streak_cold_size_pct,
            fcfg.futures_streak_hot_pct, fcfg.futures_streak_hot_size_pct) == (10, -1.0, 100.0, 0.8, 100.0)
    wave = replace(fcfg, futures_max_positions=4, futures_leverage=3, **COLD, **HOT)
    wave.validate()
    for bad in (dict(futures_streak_trades=2), dict(futures_streak_cold_pct=0.5), dict(futures_streak_cold_size_pct=5.0),
                dict(futures_streak_hot_pct=-1.0), dict(futures_streak_hot_size_pct=250.0),
                dict(futures_streak_hot_size_pct=90.0)):
        with pytest.raises(ConfigError):
            replace(wave, **bad).validate()
    for key, val in (("FUTURES_STREAK_TRADES", "20"), ("FUTURES_STREAK_COLD_PCT", "-1.5"),
                     ("FUTURES_STREAK_COLD_SIZE_PCT", "50"), ("FUTURES_STREAK_HOT_PCT", "0.5"),
                     ("FUTURES_STREAK_HOT_SIZE_PCT", "125")):
        monkeypatch.setenv(key, val)
        assert EDITABLE[key].attr == key.lower()
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    p = fb.params_from_cfg(loaded)
    assert (p.streak_trades, p.streak_cold_pct, p.streak_cold_size_pct, p.streak_hot_pct, p.streak_hot_size_pct) == (
        20, -1.5, 50.0, 0.5, 125.0)
    assert not any("STREAK" in n for n in _setting_notes(wave))                 # Açılış notu yok
    note = [n for n in _setting_notes(replace(wave, futures_max_positions=1)) if "STREAK" in n]
    assert len(note) == 1 and "etkisiz" in note[0]                              # Tek pozisyonda ek pozisyon yok


def test_state_keeps_last_trades_and_survives_restart():
    st = BotState.new("paper", 100.0)
    for i in range(RECENT_TRADES_KEEP + 5):
        st.note_trade(1000.0 + i, float(i))
    assert len(st.recent_trades) == RECENT_TRADES_KEEP and st.recent_trades[-1] == [1000.0 + RECENT_TRADES_KEEP + 4,
                                                                                    RECENT_TRADES_KEEP + 4.0]
    again = BotState.from_dict(st.to_dict())
    assert again.recent_trades == st.recent_trades
    old = st.to_dict()
    old.pop("recent_trades")                                                     # Eski sürümün durum dosyası
    assert BotState.from_dict(old).recent_trades == []


# ====================================================================== backtest
def streak_case(over, outcome):
    """3 işlem kapanır (outcome penceresiyle), sonra ilk pozisyon (açık kalır) ve ek pozisyon gelir."""
    sigs = [bt_sig(f"P{i}USDT", BASE_MS + i * 10 * MIN) for i in range(3)]
    first = bt_sig("AAAUSDT", BASE_MS + 40 * MIN)
    extra = bt_sig("BBBUSDT", BASE_MS + 41 * MIN)
    sigs += [first, extra]
    w = {(s.symbol, s.close_ms): window(outcome if s.symbol.startswith("P") else FLAT, start=s.close_ms) for s in sigs}
    p = params(tp_pct=3.0, sl_pct=4.0, be_enabled=False, hold_min=30, max_positions=3, streak_trades=3, **over)
    return p, fb.simulate(p, bt_data(), sigs, w)


def test_slots_engine_sizes_extra_by_recent_results():
    p_off, off = streak_case({}, LOSS)
    p_cold, cold = streak_case(dict(streak_cold_size_pct=50.0), LOSS)
    assert [t.symbol for t in cold.trades] == [t.symbol for t in off.trades] == ["P0USDT", "P1USDT", "P2USDT",
                                                                                 "AAAUSDT", "BBBUSDT"]
    by_off, by_cold = {t.symbol: t for t in off.trades}, {t.symbol: t for t in cold.trades}
    assert by_cold["AAAUSDT"].streak == 1.0                                     # İlk pozisyon: kural yok
    assert by_cold["BBBUSDT"].streak == pytest.approx(0.5)
    assert by_cold["BBBUSDT"].margin / by_cold["AAAUSDT"].margin == pytest.approx(
        0.5 * by_off["BBBUSDT"].margin / by_off["AAAUSDT"].margin)
    _p, hot = streak_case(dict(streak_hot_size_pct=125.0), WIN)
    _p, hot_off = streak_case({}, WIN)
    h, ho = {t.symbol: t for t in hot.trades}, {t.symbol: t for t in hot_off.trades}
    assert h["BBBUSDT"].streak == pytest.approx(1.25)
    assert h["BBBUSDT"].margin / h["AAAUSDT"].margin == pytest.approx(1.25 * ho["BBBUSDT"].margin / ho["AAAUSDT"].margin)
    _p, mild = streak_case(dict(streak_cold_size_pct=50.0), WIN)                # Kazançlı seride soğuk taraf işlemez
    assert {t.symbol: t for t in mild.trades}["BBBUSDT"].streak == 1.0
    line = fb.streak_line(p_cold, cold)
    assert line.startswith("🔥 Seri: son 3 kapanan işlemin işlem başı ortalaması −%1'in altındaysa ek pozisyonlar %50 boy")
    assert line.endswith("A'da 1 ek pozisyon küçük, 0 ek pozisyon büyük açıldı")
    assert fb.streak_line(p_off, off) == "" and fb.streak_line(replace(p_cold, max_positions=1), cold) == ""


def test_variant_rows(fcfg):
    wave = replace(fcfg, futures_max_positions=4, futures_leverage=3)
    rows = dict(fb.streak_rows(fb.params_from_cfg(wave), lambda **kw: kw))
    assert rows == {"Seri: son 10 işlem < −%1 → ek %50, ≥ +%0.8 → ek %125": {"streak_cold_size_pct": 50.0,
                                                                               "streak_hot_size_pct": 125.0}}
    on = fb.params_from_cfg(replace(wave, **COLD, **HOT))
    got = dict(fb.streak_rows(on, lambda **kw: kw))
    assert got["Seride soğuk taraf kapalı"] == {"streak_cold_size_pct": 100.0}
    assert got["Soğuk seri: ortalama < −%1.5 (şimdi −%1)"] == {"streak_cold_pct": -1.5}
    assert got["Soğuk seride ek boy %35 (şimdi %50)"] == {"streak_cold_size_pct": 35.0}
    assert got["Seride sıcak taraf kapalı"] == {"streak_hot_size_pct": 100.0}
    assert got["Sıcak seride ek boy %150 (şimdi %125)"] == {"streak_hot_size_pct": 150.0} and len(got) == 7
    assert fb.streak_rows(fb.params_from_cfg(fcfg), lambda **kw: kw) == []        # Tek pozisyonda satır yok


# ====================================================================== canlı / test modu
def live_bot(fcfg, tmp_path, nets, **over):
    cfg = replace(fcfg, futures_max_positions=4, futures_leverage=3, futures_extra_size_pct=100.0, **over)
    bot, fake, notes, clock = make_fbot(cfg, tmp_path)
    for i, n in enumerate(nets):
        bot.state.note_trade(clock.t - 1000 + i, n)
    return bot, fake, notes, clock


def test_live_margin_and_note(fcfg, tmp_path):
    bot, _fake, _notes, clock = live_bot(fcfg, tmp_path / "cold", [-2.0] * 6, **COLD)
    assert bot.streak_recent_avg() == pytest.approx(-2.0)
    first = signal("ETHUSDT")
    base_first = bot._entry_margin(first, 30.0)
    assert "streak_size_pct" not in first.metrics                               # Açık pozisyon yok: kural yok
    bot.open_futures(signal("BTCUSDT"), clock.t)
    extra = signal("ETHUSDT")
    off_bot, *_ = live_bot(fcfg, tmp_path / "off", [-2.0] * 6)
    off_bot.open_futures(signal("BTCUSDT"), clock.t)
    plain = signal("ETHUSDT")
    assert bot._entry_margin(extra, 30.0) == pytest.approx(off_bot._entry_margin(plain, 30.0) * 0.5)
    assert extra.metrics["streak_size_pct"] == 50.0 and base_first > 0
    assert "Seri: son 10 işlemin ortalaması %-2.00 (zararlar sürüyor), ek pozisyon %50 boy" in extra.note()
    hot, *_ = live_bot(fcfg, tmp_path / "hot", [1.0] * 6, **HOT)
    hot.open_futures(signal("BTCUSDT"), clock.t)
    sig = signal("ETHUSDT")
    hot._entry_margin(sig, 30.0)
    assert sig.metrics["streak_size_pct"] == 125.0 and "(dalga dönüyor)" in sig.note()
    few, *_ = live_bot(fcfg, tmp_path / "few", [-2.0] * 3, **COLD)                # 10 işlemde en az 5 gerekir
    assert few.streak_recent_avg() is None and few._streak_factor() == 1.0


def test_close_records_trade_net(fcfg, tmp_path):
    bot, fake, _notes, clock = make_fbot(fcfg, tmp_path, live=False)
    pos = bot.open_futures(signal("ETHUSDT", "short"), clock.t)
    notional = pos.qty * pos.entry_price
    fake.prices["ETHUSDT"] = 104.1                                              # Short'ta stop
    bot.manage_positions(clock.t + 120)
    assert not bot.state.positions and len(bot.state.recent_trades) == 1
    assert bot.state.recent_trades[0][1] == pytest.approx(bot.state.realized_pnl / notional * 100, abs=1e-3)


# ====================================================================== Telegram
def test_wave_page_rows_and_status(fcfg, tmp_path):
    bot, _fake, notes, _clock = live_bot(replace(fcfg, futures_tactic="crash"), tmp_path, [-2.0] * 6)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert "🔥 Seri" not in text and "kv:fw:FUTURES_STREAK_COLD_SIZE_PCT:50" in data
    press(bot, "kv:fw:FUTURES_STREAK_COLD_SIZE_PCT:50")
    assert bot.cfg.futures_streak_cold_size_pct == 50.0 and "FUTURES_STREAK_COLD_SIZE_PCT=50" in env.read_text()
    press(bot, "kv:fw:FUTURES_STREAK_HOT_SIZE_PCT:125")
    text = notes.menus[-1][0]
    assert "🔥 Seri: son 10 işlemin ortalaması < %-1 → ek <b>%50</b>, ≥ +%0.8 → ek <b>%125</b> | şu an %-2.00" in text
    assert len(text) <= 4000
