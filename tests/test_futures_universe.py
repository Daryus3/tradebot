"""Vadeli: en hacimli N coin evreni (FUTURES_UNIVERSE), izleyen stop (canlı + backtest) ve ilgili Telegram ayarları."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from commands import CALLBACK, Command
from futures_exchange import ccxt_symbol
from futures_universe import DAY_MS, market_eligible
from risk import ExitReason
from test_futures import BASE_MS, FakeCcxt, fcfg, make_fbot, params, sig, signal, window  # noqa: F401

NOW_MS = BASE_MS + 3_000


class UniCcxt(FakeCcxt):
    """24 saatlik özetler ve parite bilgileri (onboardDate, deliveryDate, underlyingType) olan sahte borsa."""

    def __init__(self, tickers, meta=None, **kw):
        super().__init__(prices={s: v[0] for s, v in tickers.items()}, **kw)
        self.tickers = tickers                       # parite → (son fiyat, 24 saatlik tepe, USDT hacmi)
        self.meta = meta or {}

    def market(self, sym):
        m = super().market(sym)
        base = sym.split("/")[0]
        m["base"] = base
        m["info"] = {"contractType": "PERPETUAL", "status": "TRADING", "underlyingType": "COIN",
                     "onboardDate": str(NOW_MS - 2000 * DAY_MS), "deliveryDate": "4133404800000",
                     **self.meta.get(base, {})}
        return m

    @property
    def markets(self):
        return {ccxt_symbol(s): self.market(ccxt_symbol(s)) for s in self.prices}

    def fapiPublicGetTicker24hr(self, params=None):
        self.calls.append(("ticker24hr",))
        return [{"symbol": s, "lastPrice": str(v[0]), "highPrice": str(v[1]), "lowPrice": str(v[0] * 0.9),
                 "quoteVolume": str(v[2])} for s, v in self.tickers.items()]

    def fapiPublicGetKlines(self, params):
        self.calls.append(("klines", params["symbol"], params["interval"]))
        if params["interval"] != "1d":
            return []
        qv = self.tickers[params["symbol"]][2]             # Günlük USDT hacmi = 24 saatlik özet
        day = 86_400_000
        start = NOW_MS // day * day - 3 * day
        return [[start + i * day, "1", "1", "1", "1", "1", start + (i + 1) * day - 1, str(qv), "1", "1", "1", "0"]
                for i in range(3)]


TICKERS = {"BTCUSDT": (100.0, 101.0, 5e9), "ETHUSDT": (88.0, 100.0, 3e9), "SOLUSDT": (99.0, 100.0, 2e9),
           "USDCUSDT": (1.0, 1.0, 9e9), "TSLAUSDT": (300.0, 310.0, 8e9), "NEWUSDT": (1.0, 1.3, 7e9),
           "OLDUSDT": (1.0, 1.4, 6e9), "IDXUSDT": (50.0, 60.0, 4e9), "MIDUSDT": (1.0, 1.0, 4.5e9),
           "DOGEUSDT": (0.1, 0.1, 1e9)}
META = {"NEW": {"onboardDate": str(NOW_MS - 5 * DAY_MS)},          # 5 günlük: çok yeni
        "MID": {"onboardDate": str(NOW_MS - 500 * DAY_MS)},       # 500 günlük: 3 yıllık yaş filtresine takılır
        "OLD": {"deliveryDate": str(NOW_MS + 10 * DAY_MS)},       # 10 gün sonra kaldırılacak
        "IDX": {"underlyingType": "INDEX"}}


@pytest.fixture
def ucfg(fcfg):
    return replace(fcfg, futures_universe=3, futures_universe_with_list=False)


def test_market_eligibility_rules():
    fake = UniCcxt(TICKERS, META)
    ok = {s for s in TICKERS if market_eligible(fake.market(ccxt_symbol(s)), NOW_MS)}
    assert ok == {"BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "MIDUSDT"}
    m = fake.market(ccxt_symbol("BTCUSDT"))
    assert not market_eligible({**m, "linear": False}, NOW_MS)
    assert not market_eligible({**m, "info": {**m["info"], "status": "SETTLING"}}, NOW_MS)
    assert not market_eligible({**m, "info": {**m["info"], "underlyingType": "TRADIFI"}}, NOW_MS)


def test_seasoned_ranking_and_crash_prefilter_scan(ucfg, tmp_path):
    fake = UniCcxt(TICKERS, META)
    bot, fake, notes, clock = make_fbot(ucfg, tmp_path, fake=fake)
    # Stabil / hisse / yeni / kalkacak / endeks yok; 500 günlük MID 3 yıllık yaş filtresine takıldı
    assert bot.universe == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert "en hacimli 3" in notes.messages[0]
    fake.calls.clear()
    bot.scan_and_enter(clock.t)
    asked = [c[1] for c in fake.calls if c[0] == "klines" and c[2] == "5m"]
    assert asked == ["ETHUSDT"]                     # 24 saatlik tepesinin ≥%9 altında olan tek coin (çöküş adayı)
    assert not [c for c in fake.calls if c[0] == "klines" and c[2] == "1d"]      # Sıralama günde bir
    assert bot.last_scan.rejects["çöküş yok (24 saatlik tepeye yakın)"] == 2 and bot.last_scan.candidates == 3
    # Yaş filtresi değişince sıralama hemen yenilenir: 500 günlük MID artık evrende
    bot.apply_config(replace(bot.cfg, futures_universe_min_age_days=365))
    bot.scan_and_enter(clock.t)
    assert bot.universe == ["BTCUSDT", "MIDUSDT", "ETHUSDT"]
    # 4'lü onay (liq4): ön eleme yok, evrendeki her coinin mumları istenir
    bot.apply_config(replace(bot.cfg, futures_tactic="liq4", take_profit_pct=1.2, stop_loss_pct=0.9,
                             trailing_activation_pct=1.0, trailing_floor_pct=0.5, breakeven_trigger_pct=0.5,
                             breakeven_floor_pct=0.2))
    fake.calls.clear()
    bot.scan_and_enter(clock.t)
    assert sorted(c[1] for c in fake.calls if c[0] == "klines" and c[2] == "5m") == ["BTCUSDT", "ETHUSDT", "MIDUSDT"]


def test_ranking_refreshes_daily_and_survives_errors(ucfg, tmp_path):
    fake = UniCcxt(TICKERS, META)
    bot, fake, _notes, clock = make_fbot(ucfg, tmp_path, fake=fake)
    fake.calls.clear()
    bot._refresh_universe(clock.t + 3600)
    assert not [c for c in fake.calls if c[0] == "klines"]
    bot._refresh_universe(clock.t + 25 * 3600)                          # 24 saat geçti: yeniden sıralanır
    assert [c for c in fake.calls if c[0] == "klines" and c[2] == "1d"]
    fake.fapiPublicGetTicker24hr = lambda params=None: (_ for _ in ()).throw(Exception("ağ"))
    assert bot._scan_symbols() == (["BTCUSDT", "ETHUSDT", "SOLUSDT"], 0)   # Özet yoksa ön eleme yapılmaz


def test_list_mode_unchanged(fcfg, tmp_path):
    bot, fake, _notes, clock = make_fbot(fcfg, tmp_path)
    assert bot.universe == [] and bot._scan_symbols()[0][:2] == ["BTCUSDT", "ETHUSDT"]


# ------------------------------------------------------------------ izleyen stop: canlı motor
@pytest.fixture
def tcfg(fcfg):
    return replace(fcfg, futures_trailing=True, futures_trailing_act_pct=3.0, futures_trailing_gap_pct=1.5)


def test_live_trailing_moves_exchange_stop_and_exits(tcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(tcfg, tmp_path, live=True)
    pos = bot.open_futures(signal(), clock.t)
    fake.prices["BTCUSDT"] = 102.0                                         # Başabaş (+%1.5): stop 100.3
    bot.manage_positions(clock.t + 10)
    assert pos.breakeven_active and not pos.trailing_active and pos.sl_price == pytest.approx(100.3)
    fake.prices["BTCUSDT"] = 104.0                                         # +%4 ≥ +%3: izleyen stop devrede
    bot.manage_positions(clock.t + 20)
    assert pos.trailing_active and pos.sl_price == pytest.approx(102.44) and pos.peak_price == 104.0
    stops = [c for c in fake.calls if c[0] == "create_order" and c[2] == "STOP_MARKET"]
    assert stops[-1][6]["stopPrice"] == pytest.approx(102.44)
    assert any("izleyen stop devrede" in m for m in notes.messages)
    fake.prices["BTCUSDT"] = 105.0                                         # 10 sn dolmadan taşınmaz...
    bot.manage_positions(clock.t + 25)
    assert pos.sl_price == pytest.approx(102.44) and pos.peak_price == 105.0
    bot.manage_positions(clock.t + 31)                                     # ...sonra taşınır
    assert pos.sl_price == pytest.approx(103.42)
    fake.prices["BTCUSDT"] = 103.0                                         # Seviyenin belirgin altı: bot kapatır
    bot.manage_positions(clock.t + 40)
    assert not bot.state.positions and any("İZLEYEN STOP" in m for m in notes.messages)
    assert bot.trade_log  # işlem kaydı yazıldı


def test_live_trailing_soft_exit_before_exchange_move(tcfg, tmp_path):
    """Binance stopu henüz taşınmadıysa bot seviyeyi kendisi uygular (canlıdaki fiyat aralığında kayıp büyümez)."""
    bot, fake, notes, clock = make_fbot(tcfg, tmp_path, live=True)
    pos = bot.open_futures(signal(), clock.t)
    fake.prices["BTCUSDT"] = 104.0
    bot.manage_positions(clock.t + 10)
    fake.prices["BTCUSDT"] = 106.0                                          # Yeni tepe; taşıma 10 sn bekliyor
    bot.manage_positions(clock.t + 12)
    assert pos.sl_price == pytest.approx(102.44)
    fake.prices["BTCUSDT"] = 104.2                                          # 106 × 0.985 = 104.41 altına indi
    bot.manage_positions(clock.t + 14)
    assert not bot.state.positions
    closes = [c for c in fake.calls if c[0] == "create_order" and c[2] == "market" and c[6].get("reduceOnly")]
    assert closes, "piyasa emriyle kapatılmalıydı"


def test_paper_trailing_fill_reason(tcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(tcfg, tmp_path, live=False)
    pos = bot.open_futures(signal(), clock.t)
    fake.prices["BTCUSDT"] = 104.0
    bot.manage_positions(clock.t + 10)
    assert pos.trailing_active
    fake.prices["BTCUSDT"] = 102.0                                          # Sanal stop (102.44) çalıştı
    bot.manage_positions(clock.t + 20)
    assert not bot.state.positions and any("İZLEYEN STOP" in m for m in notes.messages)


def test_trailing_off_keeps_old_behaviour(fcfg, tmp_path):
    bot, fake, _notes, clock = make_fbot(fcfg, tmp_path, live=True)
    pos = bot.open_futures(signal(), clock.t)
    fake.prices["BTCUSDT"] = 104.0
    bot.manage_positions(clock.t + 10)
    assert not pos.trailing_active and pos.sl_price == pytest.approx(100.3) and pos.peak_price == 104.0


# ------------------------------------------------------------------ izleyen stop: backtest
TRAIL_ROWS = [(100, 101, 99.9, 100.8),        # Yükselen: başabaş (+%0.5) → stop 100.2
              (100.8, 104, 100.7, 103.8),     # Yükselen: tepe 104 ≥ +%3 → izleyen stop 104 × 0.99 = 102.96
              (103.8, 104.5, 102.0, 102.2)]   # Düşen: önce tepe 104.5 (stop 103.455), sonra dip 102 → çıkış


def test_backtest_trailing_exit():
    p = params(tp_pct=10.0, trail_enabled=True, trail_act_pct=3.0, trail_gap_pct=1.0)
    tr = fb.run_trade(p, sig(), window(TRAIL_ROWS), margin=10.0, notional=100.0)
    assert tr.reason == "TRAIL" and tr.exit == pytest.approx(104.5 * 0.99)
    off = fb.run_trade(params(tp_pct=10.0), sig(), window(TRAIL_ROWS), margin=10.0, notional=100.0)
    assert off.reason != "TRAIL"
    # Aynı mumda tepe + kapanışa iniş (yükselen mum): tepeden sonra kapanış yeni stopun altında
    rows = [(100, 100.2, 99.9, 100.1), (100.1, 105, 100.0, 103.5)]
    tr = fb.run_trade(p, sig(), window(rows), margin=10.0, notional=100.0)
    assert tr.reason == "TRAIL" and tr.exit == pytest.approx(105 * 0.99)


def test_backtest_trailing_never_lowers_stop():
    p = params(tp_pct=20.0, be_enabled=True, be_trigger_pct=4.0, be_floor_pct=3.5, trail_enabled=True,
               trail_act_pct=3.0, trail_gap_pct=3.0)
    rows = [(100, 104.2, 99.9, 104.0), (104.0, 104.1, 103.2, 103.3)]      # Başabaş stopu 103.5 > izleyen 101.07
    tr = fb.run_trade(p, sig(), window(rows), margin=10.0, notional=100.0)
    assert tr.reason == "BE" and tr.exit == pytest.approx(103.5)


def test_params_from_cfg_reads_trailing(tcfg):
    p = fb.params_from_cfg(tcfg)
    assert (p.trail_enabled, p.trail_act_pct, p.trail_gap_pct) == (True, 3.0, 1.5)


# ------------------------------------------------------------------ Telegram ayarları
def press(bot, data):
    bot.commands.handle(Command(CALLBACK, data.split(":"), kind="callback", message_id=9))


def data_of(notes):
    return [d for row in notes.menus[-1][1] for _l, d in row]


def test_protection_page_and_universe_buttons(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(fcfg, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fb")
    data = data_of(notes)
    assert "kv:fb:BREAKEVEN_TRIGGER_PCT:5" in data and "kv:fb:BREAKEVEN_TRIGGER_PCT:4" in data
    assert "kv:fb:FUTURES_TRAILING:t" in data and "kv:fb:FUTURES_TRAILING_ACT_PCT:3" not in data   # Kapalıyken gizli
    press(bot, "kv:fb:BREAKEVEN_TRIGGER_PCT:5")
    press(bot, "kv:fb:BREAKEVEN_FLOOR_PCT:3")
    assert (bot.cfg.breakeven_trigger_pct, bot.cfg.breakeven_floor_pct) == (5, 3)
    assert "kv:fb:BREAKEVEN_TRIGGER_PCT:1.5" not in data_of(notes)         # Tabandan (3) küçük tetik gösterilmez
    press(bot, "kv:fb:BREAKEVEN_TRIGGER_PCT:+")
    assert bot.cfg.breakeven_trigger_pct == 5.5
    press(bot, "kv:fb:FUTURES_TRAILING:t")
    press(bot, "kv:fb:FUTURES_TRAILING_ACT_PCT:4")
    press(bot, "kv:fb:FUTURES_TRAILING_GAP_PCT:2")
    assert (bot.cfg.futures_trailing, bot.cfg.futures_trailing_act_pct, bot.cfg.futures_trailing_gap_pct) == (
        True, 4, 2)
    text = env.read_text(encoding="utf-8")
    assert "FUTURES_TRAILING=true" in text and "BREAKEVEN_TRIGGER_PCT=5.5" in text
    press(bot, "kv:fb:FUTURES_TRAILING_ACT_PCT:6")                         # Kâr Al'a (6) eşit: uyarı
    assert "hiç devreye girmez" in notes.menus[-1][0]
    press(bot, "pg:fc")
    assert "fc:BTC" in data_of(notes) and "kv:fc:FUTURES_UNIVERSE:40" in data_of(notes)
    press(bot, "kv:fc:FUTURES_UNIVERSE:40")
    assert bot.cfg.futures_universe == 40 and "FUTURES_UNIVERSE=40" in env.read_text(encoding="utf-8")
    assert "fc:BTC" in data_of(notes) and "en hacimli <b>40</b>" in notes.menus[-1][0]   # Liste de taranıyor
    press(bot, "cfg")
    assert any("en hacimli 40" in label for row in notes.menus[-1][1] for label, _d in row)
    press(bot, "kv:fc:FUTURES_UNIVERSE:0")
    assert bot.cfg.futures_universe == 0 and "fc:BTC" in data_of(notes)


def test_trailing_exit_reason_helpers(tcfg, tmp_path):
    from futures_trader import _stop_reason
    bot, fake, _notes, clock = make_fbot(tcfg, tmp_path, live=True)
    pos = bot.open_futures(signal(), clock.t)
    assert _stop_reason(pos) == ExitReason.STOP_LOSS
    pos.breakeven_active = True
    assert _stop_reason(pos) == ExitReason.BREAKEVEN
    pos.trailing_active = True
    assert _stop_reason(pos) == ExitReason.TRAILING_STOP


# ------------------------------------------------------------------ backtest: en hacimli N köklü coin
import numpy as np  # noqa: E402

import futures_universe as fu  # noqa: E402
from futures_data import slice_range  # noqa: E402
from strategy import FUT_BAR_MS  # noqa: E402
from test_futures import crash_series  # noqa: E402


def _months(a, b):
    out, k = [], a
    while k <= b:
        out.append(k)
        k = fu.month_add(k, 1)
    return out


def test_rank_month_age_filter_average_and_fallback():
    available = {"OLDUSDT": _months("2020-01", "2024-06"), "MIDUSDT": _months("2021-03", "2024-06"),
                 "NEWUSDT": _months("2024-01", "2024-06"), "GONEUSDT": _months("2020-01", "2024-04")}
    vols = {"OLDUSDT": {m: 10.0 for m in available["OLDUSDT"]}, "MIDUSDT": {m: 20.0 for m in available["MIDUSDT"]},
            "NEWUSDT": {m: 999.0 for m in available["NEWUSDT"]}, "GONEUSDT": {m: 50.0 for m in available["GONEUSDT"]}}
    assert fu.rank_month("2024-06", 5, 36, 6, available, vols) == ["MID", "OLD"]   # Yeni ve kaldırılmış yok
    assert fu.rank_month("2024-06", 2, 0, 6, available, vols) == ["NEW", "MID"]
    assert fu.rank_month("2020-01", 5, 36, 6, available, vols) == ["GONE", "OLD"]  # Arşivin ilk ayı: eski sayılır
    vols["MIDUSDT"].update({"2024-05": 0.0})                                       # Önceki ay işlem yok → dışarıda
    assert fu.rank_month("2024-06", 5, 36, 6, available, vols) == ["OLD"]
    late = {s: {m: v for m, v in ms.items() if m < "2024-05"} for s, ms in vols.items()}
    assert fu.rank_month("2024-06", 5, 36, 6, available, late) == ["GONE", "MID", "OLD"]   # Ay başı: bir önceki ay
    assert fu.month_add("2024-12", 1) == "2025-01" and fu.month_diff("2025-02", "2024-11") == 3


def test_universe_months_builds_monthly_segments(fcfg, monkeypatch):
    available = {"AAAUSDT": _months("2020-01", "2026-10"), "BBBUSDT": _months("2020-01", "2026-10"),
                 "CCCUSDT": _months("2025-06", "2026-10")}
    vols = {"AAAUSDT": {m: 5.0 for m in available["AAAUSDT"]}, "BBBUSDT": {m: 1.0 for m in available["BBBUSDT"]},
            "CCCUSDT": {m: 99.0 for m in available["CCCUSDT"]}}
    vols["BBBUSDT"]["2026-08"] = 500.0                                   # Ağustos'ta hacim patlaması (ort. 6 ay)
    monkeypatch.setattr(fu.VolumeHistory, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(fu.VolumeHistory, "symbols", lambda self: available)
    monkeypatch.setattr(fu.VolumeHistory, "volumes", lambda self, months, avail: vols)
    cfg = replace(fcfg, futures_universe=1)
    segs = fu.universe_months(cfg, fu.month_ms("2026-08") + 5 * DAY_MS, fu.month_ms("2026-10") + 3 * DAY_MS, None)
    assert [s[2] for s in segs] == [("AAA",), ("BBB",), ("BBB",)]       # CCC 3 yıllık değil
    assert segs[0][0] == fu.month_ms("2026-08") + 5 * DAY_MS and segs[-1][1] == fu.month_ms("2026-10") + 3 * DAY_MS


class FakeArchive:
    """Sentetik arşiv: 5 dk çöküş serisi ve girişten sonra +%7'ye yükselen 1 dk mumlar."""

    def __init__(self, k5, entry_ms, cache_dir):
        self.k5, self.entry_ms, self.cache_dir = k5, entry_ms, cache_dir
        self.day_calls = []

    def klines(self, symbol, a, b, interval="5m"):
        return slice_range(self.k5, a, b)

    def day(self, symbol, interval, day):
        self.day_calls.append((symbol, interval, day))
        start = int(day.timestamp() * 1000)
        t = start + np.arange(1440, dtype=np.int64) * 60_000
        base = getattr(self, "base", self.k5["c"][-1])
        c = np.where(t < self.entry_ms, base, base * (1 + np.minimum((t - self.entry_ms) / 60_000, 20) * 0.005))
        arr = {"t": t, "o": c, "h": c * 1.0005, "l": c * 0.9995, "c": c}
        for k in ("v", "qv", "n", "tb"):
            arr[k] = np.ones(1440)
        return arr


def test_lean_universe_backtest_pipeline(fcfg, tmp_path):
    k = crash_series(12.0, n=600)
    close_ms = int(k["t"][-1]) + FUT_BAR_MS
    ext = {key: np.concatenate((val, val[-1:])) for key, val in k.items()}   # Girişi sağlayan bir sonraki mum
    ext["t"][-1] = close_ms
    archive = FakeArchive(ext, close_ms, tmp_path)
    cfg = replace(fcfg, futures_universe=20)
    seg = [fb.Segment(int(k["t"][0]), close_ms + 1, ("SOL",))]
    data = fb.prepare_universe(cfg, int(k["t"][0]), close_ms + 2 * FUT_BAR_MS, seg, archive)
    assert data.k5 == {} and data.lean_symbols == ["SOLUSDT"] and "en hacimli 20 köklü" in data.universe_note
    signals, _funnel = fb.find_signals(cfg, data)
    assert signals and signals[-1].close_ms == close_ms
    archive.entry_ms, archive.base = signals[0].close_ms, signals[0].entry_open   # Fiyat ilk girişten sonra yükselir
    windows = fb.load_windows(data, signals, 250)
    assert isinstance(windows, fb.LazyWindows) and not archive.day_calls          # Pencereler henüz okunmadı
    res = fb.simulate(fb.params_from_cfg(cfg), data, signals, windows)
    assert res.trades and res.trades[0].reason == "TP" and archive.day_calls
    assert archive.day_calls[0][1] == "1m"


# ------------------------------------------------------------------ liste ∪ köklü N, önerilen ayarlar, profil
def test_universe_plus_list_live_scan(ucfg, tmp_path):
    fake = UniCcxt(TICKERS, META)
    cfg = replace(ucfg, futures_universe_with_list=True, futures_coins="DOGE,BTC")
    bot, fake, notes, clock = make_fbot(cfg, tmp_path, fake=fake)
    assert bot._universe_symbols() == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"]   # Liste coini eklendi
    assert "+ listeniz" in notes.messages[0]
    fake.tickers["DOGEUSDT"] = (0.08, 0.1, 1e9)                         # DOGE de 24 saatlik tepesinin %20 altında
    fake.calls.clear()
    bot.scan_and_enter(clock.t)
    assert sorted(c[1] for c in fake.calls if c[0] == "klines" and c[2] == "5m") == ["DOGEUSDT", "ETHUSDT"]


def test_backtest_with_list_merges_period_lists():
    month = fu.month_ms("2023-11")
    segs = [fb.Segment(month - 30 * DAY_MS, month, ("SOL", "BTC")), fb.Segment(month, month + 30 * DAY_MS, ("SOL",))]
    lists = [fb.Segment(0, month, ("BTC", "XLM")), fb.Segment(month, month + 99 * DAY_MS, ("BTC", "DOGE"))]
    merged = fb.with_list(segs, lists)
    assert [s.coins for s in merged] == [("SOL", "BTC", "XLM"), ("SOL", "BTC", "DOGE")]


def test_futures_profile_cooldowns_and_spot_restore(cfg):
    from config import with_strategy
    spot = replace(cfg, symbol_cooldown_minutes=30, loss_streak_limit=3)
    fut, _ = with_strategy(spot, "futures")
    assert (fut.symbol_cooldown_minutes, fut.loss_streak_limit, fut.loss_cooldown_minutes) == (0, 0, 240)
    back, _ = with_strategy(fut, "rebound")                             # Vadelinin beklemesizliği spota taşınmaz
    assert (back.symbol_cooldown_minutes, back.loss_streak_limit) == (30, 3)


def test_recommended_futures_settings_button(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(replace(fcfg, symbol_cooldown_minutes=30, loss_streak_limit=3,
                                                futures_universe=0), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "cfg")
    assert "frec" in data_of(notes)
    press(bot, "frec")
    pending = bot.commands.pending
    assert pending.action == "setting" and "FUTURES_UNIVERSE=20" in notes.menus[-1][0]
    assert bot.cfg.futures_universe == 0                                 # Onaydan önce değişmez
    press(bot, f"yes:{pending.token}")
    cfg = bot.cfg
    assert (cfg.futures_universe, cfg.futures_universe_with_list, cfg.symbol_cooldown_minutes,
            cfg.loss_streak_limit, cfg.loss_cooldown_minutes) == (20, True, 0, 0, 240)
    text = env.read_text(encoding="utf-8")
    assert "SYMBOL_COOLDOWN_MINUTES=0" in text and "FUTURES_UNIVERSE=20" in text and "BINANCE_API_KEY=gizli" in text
    press(bot, "frec")
    assert "Zaten önerilen" in notes.menus[-1][0] and bot.commands.pending is None
    press(bot, "risk")
    assert "kv:risk:SYMBOL_COOLDOWN_MINUTES:0" in data_of(notes) and "yok (hemen tekrar" in notes.menus[-1][0]
    press(bot, "pg:fc")                                                  # Liste de taranıyor: coin ızgarası görünür
    data = data_of(notes)
    assert "kv:fc:FUTURES_UNIVERSE_WITH_LIST:t" in data and "fc:BTC" in data
    press(bot, "kv:fc:FUTURES_UNIVERSE_WITH_LIST:t")
    assert not bot.cfg.futures_universe_with_list and "fc:BTC" not in data_of(notes)
