"""Ayı piyasası küçültmesi (FUTURES_BEAR_SIZE_PCT / FUTURES_BEAR_SMA_DAYS; rapor 25 araştırması, 25 Kasım): BTC'nin dünkü
kapanışı N günlük ortalamasının altındayken yeni pozisyonlar küçük boyla açılır. Ortak kural, ayarlar, backtest (BTC
rejiminin sinyallere eklenmesi, boy çarpanı, rapor), canlı bot (günlük mumlar, önbellek, hata) ve Telegram satırları.
Sahte borsa / arşivle; gerçek istek yok."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, load_config
from runtime_settings import EDITABLE
from strategy import FUT_BAR_MS, FuturesSignal, bear_regime, bear_size_factor
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, crashing, flat, live_cfg, press
from test_settings_menu import buttons, env_text, fut, spot  # noqa: F401 - pytest fikstürü

DAY = 86_400_000
MIN = fb.MIN_MS


# ====================================================================== ortak kural ve ayarlar
def test_regime_rule_and_factor():
    assert bear_regime([10, 10, 10], 4) is None                       # Yeterli gün yok
    assert bear_regime([10, 12, 14, 9], 4) is True                    # 9 < ortalama 11.25
    assert bear_regime([10, 12, 14, 16], 4) is False
    assert bear_regime([1, 100, 100, 100, 100], 4) is False           # Yalnız son 4 gün sayılır
    assert bear_size_factor(75, True) == pytest.approx(0.75)
    assert bear_size_factor(75, False) == 1.0 and bear_size_factor(75, None) == 1.0
    assert bear_size_factor(100, True) == 1.0


def test_settings_defaults_validation_env_and_notes(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_bear_size_pct, fcfg.futures_bear_sma_days) == (100.0, 100)       # Varsayılan: kapalı
    for bad in (dict(futures_bear_size_pct=5.0), dict(futures_bear_size_pct=120.0), dict(futures_bear_sma_days=10),
                dict(futures_bear_sma_days=500)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    monkeypatch.setenv("FUTURES_BEAR_SIZE_PCT", "75")
    monkeypatch.setenv("FUTURES_BEAR_SMA_DAYS", "50")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_bear_size_pct, loaded.futures_bear_sma_days) == (75.0, 50)
    assert EDITABLE["FUTURES_BEAR_SIZE_PCT"].attr == "futures_bear_size_pct"
    assert EDITABLE["FUTURES_BEAR_SMA_DAYS"].kind == "int"
    assert not _setting_notes(replace(fcfg, futures_bear_size_pct=75.0))  # Açılış notu yok (rapor 34: bilgilendirme notları kaldırıldı)
    assert not any("FUTURES_BEAR_SIZE_PCT" in n for n in _setting_notes(fcfg))


# ====================================================================== backtest
def bt_data(hours=10, archive=None):
    data = fb.FutData(BASE_MS, BASE_MS + hours * 3600_000, [], {}, None, (np.zeros(0), np.zeros(0)), {}, None, "1m")
    data.archive = archive
    return data


def bt_sig(symbol, close_ms, bear=None, breadth=9):
    s = fb.FutSignal(close_ms, symbol, "long", 1.0, 100.0)
    s.breadth = breadth
    if bear is not None:
        s.metrics["bear100"] = float(bear)
    return s


WIN = [(100, 103.5, 99.9, 103.0)]          # Kâr Al (%3)


def test_size_factor_and_trade_flag():
    p = params(tp_pct=3.0, sl_pct=4.0, be_enabled=False, bear_size_pct=75.0)
    assert fb.size_factor(p, bt_sig("A", BASE_MS, bear=1)) == pytest.approx(0.75)
    assert fb.size_factor(p, bt_sig("A", BASE_MS, bear=0)) == 1.0
    assert fb.size_factor(p, bt_sig("A", BASE_MS)) == 1.0                      # Ölçü yok: boy değişmez
    assert fb.size_factor(replace(p, bear_size_pct=100.0), bt_sig("A", BASE_MS, bear=1)) == 1.0
    assert fb.size_factor(replace(p, bear_sma_days=50), bt_sig("A", BASE_MS, bear=1)) == 1.0   # Başka gün sayısı
    sigs = [bt_sig("AAAUSDT", BASE_MS, bear=1), bt_sig("BBBUSDT", BASE_MS + 10 * MIN, bear=0)]
    w = {(s.symbol, s.close_ms): window(WIN, start=s.close_ms) for s in sigs}
    a, b = fb.simulate(p, bt_data(), sigs, w).trades
    assert a.margin == pytest.approx(75.0) and a.bear and not b.bear


class FakeArchive:
    """BTCUSDT 5 dk mumlarını aylık veren sahte arşiv: günlük kapanış dizisinden üretilir."""

    def __init__(self, first_day, closes):
        self.first_day, self.closes = first_day, closes

    def month(self, symbol, interval, y, m):
        from datetime import datetime, timezone
        assert symbol == "BTCUSDT" and interval == "5m"
        start = datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000 // DAY
        end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc).timestamp() * 1000 // DAY
        t, c = [], []
        for i, close in enumerate(self.closes):
            d = self.first_day + i
            if start <= d < end:
                t += [d * DAY + k * FUT_BAR_MS for k in (0, 287)]       # Günün ilk ve son 5 dk mumu
                c += [close * 1.01, close]                              # Gün sonu kapanışı = close
        return {"t": np.array(t, dtype=np.int64), "c": np.array(c, dtype=float)}


def test_attach_regime_uses_yesterdays_close(fcfg):
    today = BASE_MS // DAY
    first = today - 60
    closes = [100.0 + i for i in range(59)] + [50.0]          # Dün (59. gün) sert düşüş: ayı
    data = bt_data(archive=FakeArchive(first, closes))
    up = [100.0 + i for i in range(60)]                        # Hep yükseliş
    cfg = replace(fcfg, futures_bear_sma_days=50)
    s1 = bt_sig("SOLUSDT", BASE_MS)
    assert fb.attach_regime(cfg, data, [s1]) == 1
    assert s1.metrics["bear50"] == 1.0 and "bear100" not in s1.metrics       # 100 gün yok: ölçü yazılmaz
    s2 = bt_sig("SOLUSDT", BASE_MS)
    fb.attach_regime(cfg, bt_data(archive=FakeArchive(first, up)), [s2])
    assert s2.metrics["bear50"] == 0.0
    s3 = bt_sig("SOLUSDT", (first + 59) * DAY + 5 * MIN)      # Sert düşüşün olduğu gün: dünkü kapanış hâlâ yüksek
    fb.attach_regime(cfg, data, [s3])
    assert s3.metrics["bear50"] == 0.0


def test_report_header_and_variant_rows(fcfg):
    base = fb.params_from_cfg(fcfg)
    assert fb.bear_variant_rows(base, lambda **kw: kw, False) == []
    rows = dict(fb.bear_variant_rows(base, lambda **kw: kw, True))
    assert rows["Ayı piyasasında (BTC < 100 günlük ort.) boy %75"] == {"bear_size_pct": 75.0, "bear_sma_days": 100}
    on = fb.params_from_cfg(replace(fcfg, futures_bear_size_pct=75.0))
    got = dict(fb.bear_variant_rows(on, lambda **kw: kw, True))
    assert got["Ayı piyasası küçültmesi kapalı"] == {"bear_size_pct": 100.0}
    assert "Ayı piyasası boyu %65 (şimdi %75)" in got and "Ayı piyasası: BTC 50 günlük ort. (şimdi 100)" in got
    from test_futures_minute_scan import bt_data as m_data, k1_of, minute_closes, scan_cfg
    k1 = {"SOLUSDT": k1_of(minute_closes())}
    scan = scan_cfg(fcfg, futures_coins="SOL")
    data = m_data(k1)
    sigs, funnel = fb.find_signals(scan, data)
    for s in sigs:
        s.metrics["bear100"] = 1.0
    windows = {}
    for s in sigs:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * MIN)
        windows[(s.symbol, s.close_ms)] = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    bear_on = replace(scan, futures_bear_size_pct=75.0)
    ev = fb.evaluate(fb.params_from_cfg(bear_on), data, sigs, windows)
    n = len(ev.chained.trades)
    assert n and (f"🐻 Ayı piyasası: BTC'nin girişten önceki günkü kapanışı 100 günlük ortalamasının altındayken yeni "
                  f"pozisyonlar %75 boy (A, B ve C'de). A'da {n} / {n} işlem bu boyla açıldı") in \
        fb.format_report(bear_on, data, ev, funnel)


# ====================================================================== canlı / test modu
class DailyCcxt(KlineCcxt):
    """5 dk mumlara ek olarak BTCUSDT'nin günlük mumlarını veren sahte borsa."""

    def __init__(self, closes, daily, **kw):
        super().__init__(closes, **kw)
        self.daily = daily
        self.daily_calls = 0
        self.daily_error = None

    def fapiPublicGetKlines(self, params):
        if params["interval"] != "1d":
            return super().fapiPublicGetKlines(params)
        self.daily_calls += 1
        if self.daily_error:
            raise Exception(self.daily_error)
        today = BASE_MS // DAY
        n = len(self.daily)
        rows = []
        for i, c in enumerate(self.daily + [self.daily[-1]]):          # Son satır: süren (bugünkü) mum
            t = (today - n + i) * DAY
            rows.append([t, str(c), str(c), str(c), str(c), "1", t + DAY - 1, "1", "1", "1", "0", "0"])
        return rows[-int(params["limit"]):]


def crash_closes():
    return {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}


BEARISH = [100.0] * 99 + [80.0]            # Dünkü kapanış 100 günlük ortalamanın altında
BULLISH = [100.0] * 99 + [120.0]


def test_live_entry_is_smaller_in_a_bear_market(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_leverage=2, futures_bear_size_pct=75.0)
    fake = DailyCcxt(crash_closes(), BEARISH)
    bot, _fake, notes, clock = make_fbot(cfg, tmp_path, fake=fake)
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions["SOLUSDT"]
    assert pos.margin_usdt == pytest.approx(30 * 0.95 * 0.75, rel=0.02)
    assert "Ayı piyasası (BTC 100 günlük ortalamasının altında): pozisyon %75 boy" in pos.signal_note
    assert any("Ayı piyasası" in m for m in notes.messages)
    for name, over, daily in (("boğa", {}, BULLISH), ("kapalı", {"futures_bear_size_pct": 100.0}, BEARISH)):
        c = live_cfg(fcfg, futures_leverage=2, **({"futures_bear_size_pct": 75.0} | over))
        b2, f2, _n, clock2 = make_fbot(c, tmp_path / name, fake=DailyCcxt(crash_closes(), daily))
        b2.scan_and_enter(clock2.t)
        p2 = b2.state.positions["SOLUSDT"]
        assert p2.margin_usdt == pytest.approx(30 * 0.95, rel=0.02) and "Ayı piyasası" not in p2.signal_note


def test_live_regime_is_cached_per_day_and_errors_leave_size_unchanged(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_bear_size_pct=75.0)
    fake = DailyCcxt(crash_closes(), BEARISH)
    bot, _f, _n, clock = make_fbot(cfg, tmp_path, fake=fake)
    assert bot.bear_regime() is True and bot.bear_regime() is True and fake.daily_calls == 1     # Günde bir istek
    sig = FuturesSignal("SOLUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0, "breadth": 9.0})
    assert bot._bear_factor(sig) == pytest.approx(0.75) and sig.metrics["bear_days"] == 100
    b2, f2, _n2, clock2 = make_fbot(cfg, tmp_path / "hata", fake=DailyCcxt(crash_closes(), BEARISH))
    f2.daily_error = "timeout"
    assert b2.bear_regime() is None and b2._bear_factor() == 1.0
    assert b2.bear_regime() is None and f2.daily_calls == 1                 # 10 dk dolmadan yeniden istenmez
    clock2.t += 601
    f2.daily_error = None
    assert b2.bear_regime() is True and f2.daily_calls == 2
    short = make_fbot(replace(cfg, futures_bear_sma_days=200), tmp_path / "kisa",
                      fake=DailyCcxt(crash_closes(), BEARISH))[0]
    assert short.bear_regime() is None                                      # 200 gün yok: bilinmiyor


# ====================================================================== Telegram
def test_risk_page_rows_and_status(fut, tmp_path):
    bot, notes = fut
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    data = buttons(keyboard)
    assert "🐻 Ayı piyasası küçültmesi: kapalı (henüz ölçülmedi)" in text
    assert "kv:risk:FUTURES_BEAR_SIZE_PCT:75" in data and not any("FUTURES_BEAR_SMA_DAYS" in d for d in data)
    press(bot, "kv:risk:FUTURES_BEAR_SIZE_PCT:75")
    assert bot.cfg.futures_bear_size_pct == 75.0 and "FUTURES_BEAR_SIZE_PCT=75" in env_text(bot)
    text, keyboard, _mid = notes.menus[-1]
    assert "kv:risk:FUTURES_BEAR_SMA_DAYS:50" in buttons(keyboard)
    bot._bear_cache = (0, 100, True)
    press(bot, "risk")
    assert "pozisyon %75 boy | BTC şu an 100 günlük ortalamasının altında (ayı)" in notes.menus[-1][0]


def test_no_bear_rows_for_spot(spot):
    bot, notes = spot
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    assert "Ayı piyasası" not in text and not any("FUTURES_BEAR" in d for d in buttons(keyboard))
