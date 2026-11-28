"""Düşen bıçak + piyasa düşüşü (FUTURES_FALL7_BTC_PCT; rapor 34 araştırması, 28 Kasım): coin son 7 günde sert düştü, BTC
de son 7 günde %X'ten fazla düştü ve çöküş hafifse (düşüş / eşik < FUTURES_FALL7_EXEMPT_X) girilmez. Ortak kural
(strategy), ayarlar, backtest (BTC ölçüsü, iki motorda atlama, rapor satırı), canlı / test modunda BTC günlük kapanışları ve
tarama, Telegram satırı. Sahte borsalarla; gerçek emir yok."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, load_config
from runtime_settings import EDITABLE
from strategy import FALL7_BTC_REASON, fall7_btc_skip
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, crashing, flat, live_cfg, press
from test_futures_day_brake import bt_data, bt_sig
from test_futures_fall7 import k5_daily

NAN = float("nan")
MIN = fb.MIN_MS
FLAT = [(100, 100.2, 99.8, 100.0)] * 40                                  # Ne hedef ne stop: zaman stopuna kadar açık


# ====================================================================== ortak kural ve ayarlar
def test_shared_rule():
    assert fall7_btc_skip(15.0, 10.0, 2.0, -20.0, -12.0, 1.5)                      # Bıçak + BTC düşüşte + hafif
    assert fall7_btc_skip(15.0, 10.0, 2.0, -20.0, -10.0, 1.5)                      # Tam −%10 sayılır
    assert fall7_btc_skip(15.0, 10.0, 2.0, -20.0, -12.0, NAN)                      # Oran bilinmiyor: hafif
    assert not fall7_btc_skip(15.0, 10.0, 2.0, -20.0, -12.0, 2.0)                  # Sert çöküş: girilir (tam boy)
    assert not fall7_btc_skip(15.0, 10.0, 2.0, -20.0, -9.9, 1.5)                   # BTC yeterince düşmedi
    assert not fall7_btc_skip(15.0, 10.0, 2.0, -15.0, -12.0, 1.5)                  # Coin bıçak değil (tam −%15)
    assert not fall7_btc_skip(15.0, 10.0, 2.0, None, -12.0, 1.5)
    assert not fall7_btc_skip(15.0, 10.0, 2.0, -20.0, None, 1.5)
    assert not fall7_btc_skip(15.0, 0.0, 2.0, -20.0, -12.0, 1.5)                   # Kural kapalı
    assert not fall7_btc_skip(15.0, 10.0, 0.0, -20.0, -12.0, 1.5)                  # Sert çöküş sınırı yok: kural çalışmaz
    assert not fall7_btc_skip(0.0, 10.0, 2.0, -20.0, -12.0, 1.5)                   # Düşen bıçak kapalı


def test_settings_defaults_validation_env_note(fcfg, monkeypatch, tmp_path):
    assert fcfg.futures_fall7_btc_pct == 0.0
    on = replace(fcfg, futures_fall7_pct=15.0, futures_fall7_exempt_x=2.0, futures_fall7_btc_pct=10.0)
    on.validate()
    for bad in (1.0, 60.0, -5.0):
        with pytest.raises(ConfigError):
            replace(on, futures_fall7_btc_pct=bad).validate()
    monkeypatch.setenv("FUTURES_FALL7_PCT", "15")
    monkeypatch.setenv("FUTURES_FALL7_EXEMPT_X", "2")
    monkeypatch.setenv("FUTURES_FALL7_BTC_PCT", "10")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert loaded.futures_fall7_btc_pct == 10.0 and fb.params_from_cfg(loaded).fall7_btc_pct == 10.0
    assert EDITABLE["FUTURES_FALL7_BTC_PCT"].attr == "futures_fall7_btc_pct"
    assert not _setting_notes(on)                                                   # Açılış notu yok
    for off in (dict(futures_fall7_exempt_x=0.0), dict(futures_fall7_pct=0.0)):
        note = [n for n in _setting_notes(replace(on, **off)) if "FUTURES_FALL7_BTC_PCT" in n]
        assert len(note) == 1 and "etkisiz" in note[0]


# ====================================================================== backtest
def test_attach_fall7_adds_btc_return(fcfg):
    close = BASE_MS
    coin = k5_daily(close, [100.0, 98.0, 97.0, 95.0, 92.0, 90.0, 88.0, 84.0, 80.0])     # dün 80, 7 gün önce 98
    btc = k5_daily(close, [100.0, 100.0, 99.0, 97.0, 96.0, 95.0, 93.0, 91.0, 88.0])     # dün 88, 7 gün önce 100
    data = fb.FutData(close - 10 * fb.DAY_MS, close + fb.DAY_MS, [], {"SOLUSDT": coin, "BTCUSDT": btc}, None,
                      (np.zeros(0), np.zeros(0)), {}, None, "1m")
    s = fb.FutSignal(close, "SOLUSDT", "long", 1.0, 70.0)
    cfg = replace(fcfg, futures_fall7_pct=15.0)
    assert fb.attach_fall7(cfg, data, [s]) == 1
    assert s.metrics["btc7"] == pytest.approx((88.0 / 100.0 - 1) * 100)
    nobtc = fb.FutData(close - 10 * fb.DAY_MS, close + fb.DAY_MS, [], {"SOLUSDT": coin}, None,
                       (np.zeros(0), np.zeros(0)), {}, None, "1m")
    s2 = fb.FutSignal(close, "SOLUSDT", "long", 1.0, 70.0)
    assert fb.attach_fall7(cfg, nobtc, [s2]) == 1 and "btc7" not in s2.metrics     # BTC verisi yok: ölçü yok


def knife(symbol, close_ms, drop, btc7, thr=10.0):
    s = bt_sig(symbol, close_ms)
    s.metrics.update({"tactic": 1.0, "drop_pct": drop, "thr_pct": thr, "ret7": -25.0, "btc7": btc7})
    return s


def test_engines_skip_mild_knife_in_btc_down_week():
    mild = knife("AAAUSDT", BASE_MS, 15.0, -12.0)                           # oran 1.5 < 2: girilmez
    hard = knife("BBBUSDT", BASE_MS + MIN, 25.0, -12.0)                     # oran 2.5: tam boy
    calm = knife("CCCUSDT", BASE_MS + 2 * MIN, 15.0, -5.0)                  # BTC −%5: girilir (küçük boy)
    sigs = [mild, hard, calm]
    w = {(s.symbol, s.close_ms): window(FLAT, start=s.close_ms) for s in sigs}
    p = params(tp_pct=3.0, be_enabled=False, hold_min=30, max_positions=3, fall7_pct=15.0, fall7_size_pct=60.0,
               fall7_exempt_x=2.0, fall7_btc_pct=10.0)
    res = fb.simulate(p, bt_data(), sigs, w)
    assert [t.symbol for t in res.trades] == ["BBBUSDT", "CCCUSDT"] and res.skipped[FALL7_BTC_REASON] == 1
    by = {t.symbol: t for t in res.trades}
    assert not by["BBBUSDT"].fall7 and by["CCCUSDT"].fall7
    off = fb.simulate(replace(p, fall7_btc_pct=0.0), bt_data(), sigs, w)
    assert [t.symbol for t in off.trades] == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    single = fb.simulate(replace(p, max_positions=1), bt_data(), sigs, w)        # Tek pozisyonlu motor da atlar
    assert single.trades[0].symbol == "BBBUSDT" and single.skipped[FALL7_BTC_REASON] == 1
    module = knife("DDDUSDT", BASE_MS, 15.0, -12.0)
    module.metrics["tactic"] = 2.0                                           # Ek modül sinyali: kural dışı
    assert not fb.fall7_btc_block(p, module) and fb.fall7_btc_block(p, mild)
    line = fb.fall7_line(p, res)
    assert "BTC de son 7 günde %10'ten fazla düştüyse hafif çöküşe girilmez" in line
    assert line.endswith("A'da 1 / 2 işlem bu boyla açıldı, 1 sinyal BTC düşüşü yüzünden atlandı")
    assert "hafif çöküşe" not in fb.fall7_line(replace(p, fall7_btc_pct=0.0), res)


# ====================================================================== canlı / test modu
def scan_bot(fcfg, tmp_path, btc_pct, btc_closes, name):
    # Düşüş / eşik (1 sa %6): SOL 8 / 6 = 1.33 (hafif), ADA 7 / 6 = 1.17
    closes = {"SOLUSDT": crashing(), "ADAUSDT": crashing(drop=7.0), "DOGEUSDT": flat(), "XRPUSDT": flat(),
              "BTCUSDT": flat()}
    cfg = live_cfg(fcfg, futures_max_positions=2, futures_extra_breadth=2, futures_fall7_pct=15.0,
                   futures_fall7_exempt_x=2.0, futures_fall7_btc_pct=btc_pct)
    bot, _fake, _notes, clock = make_fbot(cfg, tmp_path / name, fake=KlineCcxt(closes))
    day = int(clock.t // 86400)
    bot._day_closes["SOLUSDT"] = (day, [100.0, 98.0, 97.0, 95.0, 92.0, 90.0, 88.0, 80.0])     # 7 günde −%20
    bot._day_closes["ADAUSDT"] = (day, [100.0] * 8)
    bot._day_closes["BTCUSDT"] = (day, btc_closes)
    bot.scan_and_enter(clock.t)
    return bot


BTC_DOWN = [100.0, 99.0, 98.0, 96.0, 95.0, 93.0, 91.0, 88.0]          # 7 günde −%12
BTC_FLAT = [100.0, 99.0, 98.0, 99.0, 97.0, 98.0, 96.0, 95.0]          # 7 günde −%5


def test_paper_scan_skips_mild_knife_when_btc_falls(fcfg, tmp_path):
    bot = scan_bot(fcfg, tmp_path, 10.0, BTC_DOWN, "down")
    assert bot.btc7() == pytest.approx(-12.0) and "SOLUSDT" not in bot.state.positions
    assert "ADAUSDT" in bot.state.positions                                    # Bıçak olmayan coin etkilenmez
    assert "SOLUSDT" in scan_bot(fcfg, tmp_path, 10.0, BTC_FLAT, "flat").state.positions   # BTC −%5: girilir
    assert "SOLUSDT" in scan_bot(fcfg, tmp_path, 0.0, BTC_DOWN, "off").state.positions     # Kural kapalı


def test_refresh_fetches_btc_daily_closes(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_coins="SOL,ADA", futures_fall7_pct=15.0, futures_fall7_exempt_x=2.0,
                   futures_fall7_btc_pct=10.0)
    closes = {"SOLUSDT": crashing(), "ADAUSDT": flat(), "BTCUSDT": flat()}
    bot, _fake, _notes, clock = make_fbot(cfg, tmp_path, fake=KlineCcxt(closes))
    asked = []

    def klines(symbol, interval, limit):
        asked.append(symbol)
        n = 9
        day = int(clock.t // 86400)
        return {"t": [(day - n + 1 + i) * fb.DAY_MS for i in range(n)], "c": BTC_DOWN + [1.0]}
    bot.exchange.klines = klines
    bot._refresh_fall7(clock.t)
    assert asked[0] == "BTCUSDT" and bot.btc7() == pytest.approx(-12.0)        # BTC listede olmasa da alınır
    asked.clear()
    bot.apply_config(replace(bot.cfg, futures_fall7_btc_pct=0.0))
    bot._day_closes.clear()
    bot._refresh_fall7(clock.t)
    assert "BTCUSDT" not in asked                                              # Kural kapalıyken BTC istenmez


# ====================================================================== Telegram
def test_risk_page_row(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash", futures_fall7_pct=15.0), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "risk")
    data = [d for row in notes.menus[-1][1] for _l, d in row]
    assert not any("FUTURES_FALL7_BTC_PCT" in d for d in data)                  # Sert çöküş sınırı yokken satır yok
    press(bot, next(d for d in data if d.endswith("FUTURES_FALL7_EXEMPT_X:2")))
    data = [d for row in notes.menus[-1][1] for _l, d in row]
    press(bot, next(d for d in data if d.endswith("FUTURES_FALL7_BTC_PCT:10")))
    assert bot.cfg.futures_fall7_btc_pct == 10.0 and "FUTURES_FALL7_BTC_PCT=10" in env.read_text()
    text = notes.menus[-1][0]
    assert "BTC de 7 günde %10'ten fazla düştüyse hafif çöküşe girilmez" in text and len(text) <= 4000
