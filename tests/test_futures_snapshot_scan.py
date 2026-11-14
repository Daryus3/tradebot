"""Anlık tarama (FUTURES_SNAPSHOT_SCAN; varsayılan kapalı; hızlı giriş açıkken). Binance az önce kapanan mumu 0.3-2.5 sn
sonra kesinleştirir; bot bunu beklemek yerine geçmiş mumları kapanıştan hemen önce alır, kapanıştan ~0.15 sn sonra tek
istekle son işlem fiyatlarını okur ve kapanış olarak onu kullanır (kural yalnızca kapanışlara bağlıyken). Giriş emri
bakiye / emir defteri isteği beklemeden gider, açık pozisyon varken tarama pozisyon kontrolünü beklemez. Her anlık tarama
sonradan kesinleşen mumlarla doğrulanır; fiyat alınamazsa ya da veri bozuksa olağan hızlı yola dönülür. Kapalıyken
davranış eskisiyle aynıdır (tests/test_futures_fast_entry.py)."""
import time
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
import futures_trader as ft
from commands import CALLBACK, Command
from config import _setting_notes, futures_minute_limit, futures_snapshot_on, load_config
from futures_data import FIELDS, history_mismatch, merge_tail, split_history, with_bar
from futures_exchange import FuturesError, FuturesExchange
from runtime_settings import EDITABLE
from strategy import FUT_BAR_MS, futures_kline_limit
from test_futures import BASE_MS, Clock, fcfg, make_fbot  # noqa: F401 - pytest fikstürü
from test_futures_cascade import live_cfg
from test_futures_fast_entry import RealClock, run_until_scan, stepping
from test_futures_minute_scan import CLOSES, MIN, T0, MinuteCcxt, journal, k1_of, minute_closes

DIP = T0 + 3 * MIN                                 # SOL'un dibinin kapanışı (ara dakika; 5 dk kapanışları görmez)
LEV = 3                                            # live_cfg kaldıracı


class SnapCcxt(MinuteCcxt):
    """MinuteCcxt + hızlı girişin ve anlık taramanın kullandığı uçlar: sunucu saati, parite ayarları, toplu son işlem
    fiyatı (ticker/price) ve pozisyonlarla azalan serbest bakiye. Son işlem fiyatı = o ana kadar kapanmış son 1 dk mumun
    kapanışı (× istenirse `drift`: kapanıştan sonraki işlemler); emirler de o fiyattan dolar."""

    def __init__(self, closes, clock, **kw):
        super().__init__(closes, clock, **kw)
        self.settings = {}
        self.enableRateLimit = True
        self.drift = {}                            # parite → kapanıştan sonraki fiyat çarpanı (ör. 1.002)
        self.ticker_age_ms = 20                    # En yeni işlemin yaşı (ms)
        self.ticker_error = None
        self.ticker_time = True                    # False: yanıtta işlem zamanı alanı yok
        self.rtt = 0.0                             # > 0: her istek o kadar (gerçek) saniye sürer

    def _wait(self):
        if self.rtt:
            time.sleep(self.rtt)

    def last(self, symbol):
        now_ms = int(self.clock() * 1000)
        k = self.k1[symbol]
        closed = k["c"][k["t"] + MIN <= now_ms]
        return float(closed[-1]) * self.drift.get(symbol, 1.0)

    def fapiPublicGetKlines(self, params):
        self._wait()
        self.calls.append(("klines", params["symbol"], params["interval"]))
        return super().fapiPublicGetKlines(params)

    def fapiPublicGetTime(self, params=None):
        self._wait()
        self.calls.append(("time",))
        return {"serverTime": str(int(self.clock() * 1000))}

    def fapiPrivateGetSymbolConfig(self, params=None):
        self._wait()
        self.calls.append(("symbol_config",))
        return [{"symbol": s, "marginType": m, "leverage": lev} for s, (m, lev) in self.settings.items()]

    def fapiPublicV2GetTickerPrice(self, params=None):
        self._wait()
        self.calls.append(("ticker_price",))
        if self.ticker_error:
            raise Exception(self.ticker_error)
        now_ms = int(self.clock() * 1000)
        rows = [{"symbol": s, "price": str(self.last(s)), **({"time": now_ms - self.ticker_age_ms}
                                                           if self.ticker_time else {})} for s in self.k1]
        return rows + [{"symbol": "BOZUKUSDT", "price": "x"}]

    def set_margin_mode(self, mode, sym):
        super().set_margin_mode(mode, sym)
        symbol = sym.split("/")[0] + "USDT"
        self.settings[symbol] = ("ISOLATED", self.settings.get(symbol, ("CROSSED", 20))[1])

    def set_leverage(self, lev, sym):
        super().set_leverage(lev, sym)
        symbol = sym.split("/")[0] + "USDT"
        self.settings[symbol] = (self.settings.get(symbol, ("CROSSED", 20))[0], lev)

    def fetch_balance(self):
        self._wait()
        self.calls.append(("fetch_balance",))
        used = sum(p["contracts"] * p["entryPrice"] / LEV for p in self.positions.values())
        return {"free": {"USDT": self.free - used}, "total": {"USDT": self.free}}

    def fetch_order_book(self, sym, limit=5):
        self._wait()
        self.calls.append(("fetch_order_book", sym))
        p = self.last(sym.split("/")[0] + "USDT")
        return {"bids": [[p, 1]], "asks": [[p, 1]]}

    def fetch_ticker(self, sym):
        self._wait()
        self.calls.append(("fetch_ticker", sym))
        return {"last": self.last(sym.split("/")[0] + "USDT")}

    def fetch_positions(self, symbols=None):
        self._wait()
        self.calls.append(("fetch_positions",))
        return super().fetch_positions(symbols)

    def create_order(self, sym, type_, side, amount, price=None, params=None):
        self._wait()
        symbol = sym.split("/")[0] + "USDT"
        self.prices[symbol] = self.last(symbol)    # Piyasa emri o anki son işlem fiyatından dolar
        return super().create_order(sym, type_, side, amount, price, params)


def snap_cfg(fcfg, **over):
    base = dict(futures_fast_entry=True, futures_minute_scan=True, futures_snapshot_scan=True)
    base.update(over)
    return live_cfg(fcfg, **base)


def start(fcfg, tmp_path, closes=CLOSES, at=DIP - MIN + 10_000, live=True, **over):
    """Bot, dibin kapanışından 50 sn önce başlar (ön hazırlık ve taramalar döngüyle yürütülür)."""
    clock = Clock(at / 1000)
    fake = SnapCcxt(closes, clock)
    bot, fake, notes, clock = make_fbot(snap_cfg(fcfg, **over), tmp_path, live=live, fake=fake, clock=clock)
    stepping(bot, clock)
    return bot, fake, notes, clock


def names(fake):
    return [c[0] for c in fake.calls]


# ====================================================================== ayar
def test_setting_is_off_by_default_and_needs_fast_entry_and_close_only_rule(fcfg, tmp_path, monkeypatch):
    assert fcfg.futures_snapshot_scan is False and not futures_snapshot_on(fcfg)
    assert EDITABLE["FUTURES_SNAPSHOT_SCAN"].attr == "futures_snapshot_scan"
    on = snap_cfg(fcfg)
    assert futures_snapshot_on(on) and futures_snapshot_on(replace(on, futures_minute_scan=False))
    note = [n for n in _setting_notes(on) if "FUTURES_SNAPSHOT_SCAN" in n]
    assert len(note) == 1 and "~0.15 sn" in note[0] and "kendisi doğrular" in note[0] and "test modunda" in note[0]
    # Hızlı giriş kapalıyken, sinyal yalnızca kapanışlara bağlı değilken ya da limit girişte: çalışmaz ve söylenir
    for over in (dict(futures_fast_entry=False), dict(futures_crash_liq=True), dict(futures_crash_z=2.0),
                 dict(futures_pump_rules="1:8"), dict(futures_confirm="green"), dict(futures_entry_order="limit")):
        off = replace(on, **over)
        assert not futures_snapshot_on(off), over
        assert any("FUTURES_SNAPSHOT_SCAN=true ama" in n for n in _setting_notes(off)), over
    assert not any("FUTURES_SNAPSHOT_SCAN" in n for n in _setting_notes(replace(on, futures_snapshot_scan=False)))
    monkeypatch.setenv("FUTURES_SNAPSHOT_SCAN", "true")
    assert load_config(env_file=str(tmp_path / "x.env"), mode_override="paper").futures_snapshot_scan is True
    # Kapalıyken (ya da kullanılamıyorken) tarama anı hızlı girişinkiyle aynı; açıkken kapanıştan 0.15 sn sonra
    bot, _fake, _notes, _clock = start(fcfg, tmp_path, futures_snapshot_scan=False)
    assert bot._scan_delay() == ft.FAST_SCAN_DELAY_SEC and bot.next_scan_at == DIP / 1000 + 1
    bot.apply_config(replace(bot.cfg, futures_snapshot_scan=True))
    assert bot._scan_delay() == ft.SNAP_SCAN_DELAY_SEC and bot.next_scan_at == pytest.approx(DIP / 1000 + 0.15)
    bot.apply_config(replace(bot.cfg, futures_fast_entry=False))           # Hızlı giriş kapanınca kullanılamaz: eski yol
    assert bot.next_scan_at == pytest.approx(DIP / 1000 + 3)
    run_until_scan(bot, _clock, _clock.t)
    assert "sn" not in journal(bot)[-1] and "Anlık tarama açık ama bu ayarlarla kullanılamıyor" in bot.scan_report()


def test_telegram_row_and_page_text(fcfg, tmp_path):
    bot, _fake, notes, _clock = start(fcfg, tmp_path, live=False, futures_snapshot_scan=False)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    bot.commands.handle(Command(CALLBACK, ["pg", "fx"], kind="callback", message_id=7))
    text, keyboard, _mid = notes.menus[-1]
    assert "kv:fx:FUTURES_SNAPSHOT_SCAN:t" in [d for row in keyboard for _l, d in row]
    assert "🚀 Anlık tarama: kapalı" in text and "Giriş 1 sn'de dolsaydı" in text
    bot.commands.handle(Command(CALLBACK, ["kv", "fx", "FUTURES_SNAPSHOT_SCAN", "t"], kind="callback", message_id=7))
    assert bot.cfg.futures_snapshot_scan and "FUTURES_SNAPSHOT_SCAN=true" in env.read_text(encoding="utf-8")
    assert "🚀 Anlık tarama: açık: tarama mum kapanışından ~0.15 sn sonra" in notes.menus[-1][0]
    assert bot.next_scan_at == pytest.approx(DIP / 1000 + 0.15)             # Sıradaki tarama yeni gecikmeyle


# ====================================================================== borsa katmanı ve mum tamamlama
def test_last_prices_and_forming_candle_helpers(fcfg):
    clock = Clock(DIP / 1000 - 1.1)                                       # Kapanıştan 1.1 sn önce
    fake = SnapCcxt(CLOSES, clock)
    ex = FuturesExchange(fcfg, exchange=fake, time_fn=clock)
    k, forming = ex.klines_open("SOLUSDT", "1m", 485)
    assert int(k["t"][-1]) + 2 * MIN == DIP and len(k["t"]) == 484          # Kapanmış mumlar: dipten önceki dakikaya dek
    assert forming["t"] == DIP - MIN and forming["c"] == pytest.approx(93.0) and set(forming) == set(FIELDS)
    assert ex.klines_state("SOLUSDT", "1m", 485)[1] == DIP - MIN            # Eski çağrı: aynı ayrım
    prices = ex.last_prices()
    assert set(prices) == set(CLOSES) and prices["SOLUSDT"] == (pytest.approx(96.0), DIP - 1100 - 20)
    fake.ticker_time = False
    assert ex.last_prices()["ADAUSDT"] == (100.0, 0)                        # İşlem zamanı yoksa 0
    fake.ticker_error = 'binance {"code":-1003,"msg":"Too many requests"}'
    with pytest.raises(FuturesError):
        ex.last_prices()
    fake.fapiPublicV2GetTickerPrice = None                                  # Eski CCXT: v1 ucu; o da yoksa açık hata
    fake.fapiPublicGetTickerPrice = lambda params=None: [{"symbol": "SOLUSDT", "price": "5", "time": "7"}]
    assert ex.last_prices() == {"SOLUSDT": (5.0, 7)}
    fake.fapiPublicGetTickerPrice = None
    with pytest.raises(FuturesError) as err:
        ex.last_prices()
    assert err.value.code == "NO_TICKER"
    # Süren mum anlık fiyatla tamamlanır: kapanış o fiyat, tepe / dip onu kapsar, diğer alanlar yanıttaki gibi
    done = with_bar(k, DIP - MIN, 92.5, forming)
    assert len(done["t"]) == 485 and int(done["t"][-1]) == DIP - MIN and done["t"].dtype == np.int64
    assert done["c"][-1] == 92.5 and done["l"][-1] == 92.5 and done["o"][-1] == forming["o"]
    assert done["h"][-1] == forming["h"] and done["v"][-1] == forming["v"] and np.array_equal(done["c"][:-1], k["c"])
    # Yanıtta süren mum yoksa (ya da başka muma aitse): önceki kapanıştan düz, hacimsiz mum
    for other in (None, dict(forming, t=DIP)):
        flat = with_bar(k, DIP - MIN, 97.0, other)
        prev = float(k["c"][-1])
        assert flat["o"][-1] == prev and flat["h"][-1] == max(prev, 97.0) and flat["l"][-1] == min(prev, 97.0)
        assert flat["c"][-1] == 97.0 and flat["v"][-1] == 0.0 and set(flat) == set(FIELDS)


# ====================================================================== anlık tarama
def test_scan_runs_right_after_the_close_with_one_price_request_and_orders_at_once(fcfg, tmp_path):
    bot, fake, notes, clock = start(fcfg, tmp_path)
    assert bot.next_scan_at == pytest.approx(DIP / 1000 + ft.SNAP_SCAN_DELAY_SEC)
    fake.calls.clear()
    run_until_scan(bot, clock, clock.t)
    calls = names(fake)
    snap_at = calls.index("ticker_price")
    # Mum kapanmadan: teminat modu / kaldıraç önceden ayarlandı ve doğrulandı, bakiye okundu, geçmiş 1 dk mumlar alındı
    before = calls[:snap_at]
    assert before.count("set_margin_mode") == 4 and "symbol_config" in before and before.count("fetch_balance") == 1
    assert [c[1:] for c in fake.calls[:snap_at] if c[0] == "klines"] == [(s, "1m") for s in CLOSES]
    assert before.index("fetch_balance") < before.index("klines")
    # Kapanıştan 0.15 sn sonra: tek fiyat isteği → emir. Mum, bakiye ve emir defteri isteği yok
    order_at = calls.index("create_order")
    assert calls[snap_at:order_at + 1] == ["ticker_price", "create_order"]
    assert "klines" not in calls[snap_at:] and "fetch_order_book" not in calls and calls.count("ticker_price") == 1
    row = journal(bot)[-1]
    assert row["t"] == DIP and row["s"] == {"SOL": "açıldı"} and sorted(row["a"]) == ["ADA", "DOGE", "SOL", "XRP"]
    assert row["ms"] == [150, 150, 150] and row["sn"] == [150, 130] and row["fast"] == 1 and "sx" not in row
    assert row["f"]["SOL"]["o"] == 150 and row["f"]["SOL"]["p"] == 0.0     # Emir kapanıştan 0.15 sn sonra, kapanış fiyatından
    opened = [m for m in notes.messages if "POZİSYON AÇILDI" in m][-1]
    assert "Giriş gecikmesi: mum kapanışından 0.1 sn sonra doldu; kapanış fiyatından alındı" in opened
    assert "ucuza" not in opened and "%0.000" not in opened                 # Fark sıfırken "%0.000 ucuza" yazılmaz
    pos = bot.state.positions["SOLUSDT"]
    assert pos.entry_price == pytest.approx(93.0) and pos.stop_order_id and pos.leverage == LEV
    assert pos.metrics["drop_pct"] == pytest.approx(7.0, abs=0.05)         # Kural: 1 saatte %6 (aynı kural, aynı ölçü)
    report = bot.scan_report()
    assert "⚡ Anlık tarama: bu taramada kullanıldı (fiyatlar mum kapanışından 0.15 sn sonra alındı" in report
    assert "tarama 0.15 sn'de başladı, anlık fiyatlar 0.15 sn'de alındı, sinyaller 0.15 sn'de hazırdı" in report
    assert bot._snap_live is False and bot._free_cache is None             # Kısa yol yalnızca o taramanın girişlerinde


def test_snapshot_gives_the_same_entry_as_the_settled_candle_path(fcfg, tmp_path):
    """Aynı dip, anlık tarama kapalıyken (mumların kesinleşmesi beklenerek) ve açıkken: aynı coin, aynı miktar, aynı
    sinyal ölçüleri; yalnızca emir daha erken gider."""
    got = {}
    for name, on in (("settled", False), ("snap", True)):
        (tmp_path / name).mkdir()
        bot, fake, _notes, clock = start(fcfg, tmp_path / name, futures_snapshot_scan=on)
        run_until_scan(bot, clock, clock.t)
        pos, row = bot.state.positions["SOLUSDT"], journal(bot)[-1]
        got[name] = (pos.qty, pos.entry_price, pos.margin_usdt, pos.metrics["drop_pct"], pos.metrics["hours"],
                     pos.score, row["s"], row["a"], row.get("b"))
        got[name + "_sent"] = row["f"]["SOL"]["o"]
    assert got["settled"] == got["snap"]
    assert got["settled_sent"] == 1000 and got["snap_sent"] == 150


def test_snapshot_path_is_measurably_faster_with_and_without_an_open_position(fcfg, tmp_path):
    """Gecikmeli sahte borsa (istek başına 20 ms) ve gerçek geçen süreyle ilerleyen saat. Mum kapanışından emrin yanıtına
    kadar: olağan hızlı yolda 1 sn + mumlar + bakiye + emir defteri + emir (açık pozisyon varken üstüne pozisyon
    kontrolü); anlık taramada 0.15 sn + fiyat + emir, açık pozisyon olsa da."""
    ada = minute_closes(dip=0.0) * 0 + 100.0
    i = 3 * 1440
    ada[i + 3], ada[i + 4:] = 93.0, 99.0                                   # ADA bir dakika sonra dip yapar (SOL açıkken)
    sol = minute_closes()
    sol[i + 3:] = 93.5
    closes = dict(CLOSES, SOLUSDT=sol, ADAUSDT=ada)
    lags = {}
    for name, on in (("fast", False), ("snap", True)):
        (tmp_path / name).mkdir()
        clock = RealClock(DIP / 1000 - 30)
        fake = SnapCcxt(closes, clock)
        fake.settings = {s: ("ISOLATED", LEV) for s in closes}             # Teminat modu / kaldıraç zaten ayarlı
        cfg = snap_cfg(fcfg, futures_snapshot_scan=on, futures_max_positions=2, futures_extra_breadth=2)
        bot, fake, _notes, clock = make_fbot(cfg, tmp_path / name, live=True, fake=fake, clock=clock)
        fake.rtt = 0.02
        for close in (DIP, DIP + MIN):                                     # İlk pozisyon (SOL), sonra ek pozisyon (ADA)
            clock.set(bot.next_scan_at - bot._scan_lead())
            bot.tick()                                                     # Ön hazırlık (mum kapanmadan)
            # (Sahte borsanın saati isteğin sonunda okunur: ölçülen saat farkı +10 ms, tarama anı o kadar erken)
            assert bot.next_scan_at == pytest.approx(close / 1000 + (0.15 if on else 1.0), abs=0.02)
            clock.set(bot.next_scan_at)
            bot.tick()
            row = journal(bot)[-1]
            assert row["t"] == close and "açıldı" in row["s"].values()
            lags[name, close] = next(iter(row["f"].values()))["r"]
        assert set(bot.state.positions) == {"SOLUSDT", "ADAUSDT"}
    # Olağan hızlı yol: 1 sn + 1 dalga mum + bakiye + emir defteri + emir; ek pozisyonda önce pozisyon kontrolü (2 istek)
    assert lags["fast", DIP] >= 1000 + 4 * 20 and lags["fast", DIP + MIN] >= 1000 + 6 * 20
    # Anlık tarama: 0.15 sn + fiyat + emir; açık pozisyon taramayı geciktirmez
    for close in (DIP, DIP + MIN):
        assert 130 + 2 * 20 <= lags["snap", close] < 450, lags
        assert lags["fast", close] - lags["snap", close] > 800, lags


def test_boundary_scan_uses_five_minute_candles(fcfg, tmp_path):
    c = minute_closes(dip=0.0) * 0 + 100.0
    i = 3 * 1440
    c[i:i + 5] = [98.5, 96.0, 94.0, 93.5, 93.0]                           # 5 dk kapanışı (T0 + 5 dk) düşüşü görür
    c[i + 5:] = 93.2
    closes = dict(CLOSES, SOLUSDT=c)
    close = T0 + 5 * MIN
    for minute in (True, False):                                           # Dakikalık taramayla ve 5 dk'da bir taramayla
        folder = tmp_path / str(minute)
        folder.mkdir()
        bot, fake, _notes, clock = start(fcfg, folder, closes=closes, at=close - 50_000, futures_minute_scan=minute)
        assert bot.next_scan_at == pytest.approx(close / 1000 + 0.15)
        fake.calls.clear()
        run_until_scan(bot, clock, clock.t)
        asked = {(c[2], c[1]) for c in fake.calls if c[0] == "klines"}
        assert asked == {("5m", s) for s in closes}                        # Sınırda geçmiş 5 dk mumlar (eski yoldaki gibi)
        assert fake.asked[-1][2] == futures_kline_limit(bot.cfg)
        row = journal(bot)[-1]
        assert row["t"] == close and row["s"] == {"SOL": "açıldı"} and row["sn"][0] == 150
        assert bot.state.positions["SOLUSDT"].entry_price == pytest.approx(93.0)


def test_paper_mode_scans_with_snapshot_too(fcfg, tmp_path):
    bot, fake, _notes, clock = start(fcfg, tmp_path, live=False)
    fake.calls.clear()
    run_until_scan(bot, clock, clock.t)
    calls = names(fake)
    assert "fetch_balance" not in calls and "symbol_config" not in calls   # Test modu: hesap yok
    assert calls.count("ticker_price") == 1 and "klines" not in calls[calls.index("ticker_price"):]
    row = journal(bot)[-1]
    assert row["s"] == {"SOL": "açıldı"} and row["sn"][0] == 150
    assert row["f"]["SOL"]["p"] == pytest.approx(bot.cfg.futures_slippage_pct, abs=1e-3)   # Sanal dolum: fiyat + kayma


# ====================================================================== geri dönüş yolları
@pytest.mark.parametrize("fault, why", [
    ("error", "anlık fiyatlar alınamadı"), ("stale", "fiyatlar kapanıştan 1850 ms önceye ait"),
    ("late", "tarama geç başladı"), ("no_prepare", "ön hazırlık yapılamadı"), ("missing", "3 coinin verisi eksik")])
def test_falls_back_to_the_settled_candle_path(fcfg, tmp_path, fault, why):
    bot, fake, _notes, clock = start(fcfg, tmp_path)
    if fault == "error":
        fake.ticker_error = 'binance {"code":-1003,"msg":"Too many requests"}'
    elif fault == "stale":
        fake.ticker_age_ms = 2000                                          # En yeni fiyat kapanıştan 1.85 sn önce
    if fault == "no_prepare":
        bot._warm_for = bot.next_scan_at                                   # Ön hazırlık yapılamadı (ör. bot yeni açıldı)
        clock.t = bot.next_scan_at
        bot.tick()
    elif fault == "late":
        run_until_prepared(bot, clock)
        clock.t = DIP / 1000 + 0.7                                         # Döngü gecikti: kapanıştan 0.7 sn sonra
        bot.tick()
    elif fault == "missing":
        run_until_prepared(bot, clock)
        for symbol in ("ADAUSDT", "XRPUSDT", "DOGEUSDT"):
            fake.k1.pop(symbol)                                            # Fiyat yanıtında üç coin yok
        clock.t = bot.next_scan_at
        bot.tick()
        fake.k1.update({s: k1_of(CLOSES[s]) for s in ("ADAUSDT", "XRPUSDT", "DOGEUSDT")})
    else:
        run_until_scan(bot, clock, clock.t)
    row = journal(bot)[-1]
    assert row["sx"].startswith(why) and "sn" not in row
    if fault == "missing":
        return                                                             # (Sahte borsada mumlar da yoktu)
    # Olağan hızlı yol: mumlar kapanıştan 1 sn sonra istendi, sinyal yine bulundu ve açıldı
    assert row["ms"][1] == 1000 and row["s"] == {"SOL": "açıldı"}
    calls = names(fake)
    order_at = calls.index("create_order")
    # ...mumlar kapanıştan sonra yeniden istendi; girişte bakiye + emir defteri (kısa yol yalnızca anlık taramada)
    last_klines = max(i for i, n in enumerate(calls[:order_at]) if n == "klines")
    entry = calls[last_klines + 1:order_at]
    if fault == "no_prepare":                                              # Doğrulanmamış coin: teminat modu + kaldıraç da
        assert entry == ["set_margin_mode", "set_leverage", "fetch_balance", "fetch_order_book"]
    else:
        assert entry == ["fetch_balance", "fetch_order_book"]
    assert calls.count("ticker_price") == (1 if fault in ("error", "stale") else 0)
    assert f"bu taramada kullanılamadı ({why}" in bot.scan_report()


def run_until_prepared_or_moved(bot, clock):
    """Döngüyü, sıradaki taramanın ön hazırlığı tarama anını değiştirene kadar yürütür."""
    target = bot.next_scan_at
    for _ in range(400):
        bot.tick()
        if bot.next_scan_at != target:
            return
        clock.t += bot._next_sleep()
    raise AssertionError("tarama anı değişmedi")


def run_until_prepared(bot, clock):
    """Döngüyü, sıradaki taramanın ön hazırlığı (geçmiş mumlar) yapılana kadar yürütür."""
    for _ in range(400):
        bot.tick()
        if bot._snap is not None:
            return
        clock.t += bot._next_sleep()
    raise AssertionError("ön hazırlık yapılmadı")


def test_one_missing_coin_does_not_disable_the_snapshot(fcfg, tmp_path):
    bot, fake, _notes, clock = start(fcfg, tmp_path)
    run_until_prepared(bot, clock)
    fake.k1.pop("DOGEUSDT")                                                # Fiyat yanıtında bir coin yok: o coin taranmaz
    clock.t = bot.next_scan_at
    bot.tick()
    row = journal(bot)[-1]
    assert row["sn"][0] == 150 and row["e"] == 1 and sorted(row["a"]) == ["ADA", "SOL", "XRP"]
    assert row["s"] == {"SOL": "açıldı"}


def test_scan_time_follows_the_clock_offset(fcfg, tmp_path):
    bot, fake, _notes, clock = start(fcfg, tmp_path)
    for offset, delay in ((0.0, 0.15), (-300.0, 0.45), (100.0, 0.05), (400.0, 0.02), (-9000.0, 0.65), (None, 0.15)):
        bot._clock_offset_ms = offset                                      # Binance saati − sunucu saati
        assert bot._scan_delay() == pytest.approx(delay), offset
    # Ön hazırlıkta ölçülen saat farkı sıradaki tarama anına yansır (sunucu saati 300 ms ileride: o kadar geç)
    fake.fapiPublicGetTime = lambda params=None: {"serverTime": str(int(clock() * 1000) - 300)}
    run_until_prepared(bot, clock)
    assert bot._clock_offset_ms == pytest.approx(-300.0) and bot.next_scan_at == pytest.approx(DIP / 1000 + 0.45)
    fake.calls.clear()
    clock.t += bot._next_sleep()
    bot.tick()
    assert journal(bot)[-1]["sn"][0] == 450 and "ticker_price" in names(fake)


# ====================================================================== açık pozisyon varken: tarama kontrolü beklemez
def two_slot(fcfg, tmp_path, closes=CLOSES, **over):
    return start(fcfg, tmp_path, closes=closes, futures_max_positions=2, futures_extra_breadth=2, **over)


def test_scan_does_not_wait_for_the_position_check(fcfg, tmp_path):
    ada = minute_closes(dip=0.0) * 0 + 100.0
    i = 3 * 1440
    ada[i + 3] = 93.0                                                      # ADA bir dakika sonra (T0 + 4 dk) dip yapar
    ada[i + 4:] = 99.0
    sol = minute_closes()
    sol[i + 3:] = 93.5                                                     # SOL dipte kalır: genişlik 2 (dalga)
    closes = dict(CLOSES, SOLUSDT=sol, ADAUSDT=ada)
    bot, fake, _notes, clock = two_slot(fcfg, tmp_path, closes=closes)
    run_until_scan(bot, clock, clock.t)
    assert set(bot.state.positions) == {"SOLUSDT"}
    # Sıradaki dakika: SOL açık. Ön hazırlıktan taramaya kadar pozisyon kontrolü yapılmaz; tarama anında önce tarama
    # (fiyat → emir), pozisyon kontrolü hemen ardından aynı döngü adımında
    fake.calls.clear()
    target = bot.next_scan_at
    marks = []
    for _ in range(400):
        mark = len(fake.calls)
        bot.tick()
        marks.append((clock.t, mark))
        if bot.next_scan_at != target:
            break
        clock.t += bot._next_sleep()
    calls = names(fake)
    scan_tick = marks[-1][1]
    in_scan = calls[scan_tick:]
    assert in_scan[:2] == ["ticker_price", "create_order"]
    assert "fetch_positions" in in_scan and in_scan.index("fetch_positions") > in_scan.index("create_order")
    prepare_tick = next(m for t, m in marks if t >= target - ft.SNAP_LEAD_SEC - 1e-9)
    assert "fetch_positions" not in calls[prepare_tick:scan_tick]          # Ön hazırlık → tarama arası: kontrol yok
    assert "fetch_positions" in calls[:prepare_tick]                       # Öncesinde 2 sn'de bir kontrol sürüyordu
    assert set(bot.state.positions) == {"SOLUSDT", "ADAUSDT"}
    row = journal(bot)[-1]
    assert row["t"] == DIP + MIN and row["s"] == {"ADA": "açıldı"} and row["ms"][0] == 150 and row["b"] == 2


def test_position_check_is_deferred_only_when_a_snapshot_scan_will_run(fcfg, tmp_path):
    bot, fake, _notes, clock = two_slot(fcfg, tmp_path)
    run_until_scan(bot, clock, clock.t)
    assert set(bot.state.positions) == {"SOLUSDT"}
    lead = ft.SNAP_LEAD_SEC
    at = bot.next_scan_at
    armed = {"closed_ms": DIP + MIN}
    # Ön hazırlıktan önce: kontrol her zamanki gibi; yalnızca ön hazırlığa sarkacaksa ertelenir
    assert not bot._hold_poll(at - 30, lead)
    assert bot._hold_poll(at - lead - 0.04, lead)
    bot._manage_sec = 0.6                                                  # Kontrol 0.6 sn sürüyorsa 0.8 sn önceden
    assert bot._hold_poll(at - lead - 0.79, lead) and not bot._hold_poll(at - lead - 0.81, lead)
    bot._manage_sec = 30.0                                                 # ...ama en çok SNAP_POLL_GUARD_SEC erken
    assert not bot._hold_poll(at - lead - ft.SNAP_POLL_GUARD_SEC - 0.01, lead)
    bot._snap_armed = False                                                # Son ön hazırlık kurulamadıysa hiç ertelenmez
    assert not bot._hold_poll(at - lead - 0.04, lead)
    bot._snap_armed = True
    # Ön hazırlık yapıldı ve anlık tarama kuruldu: taramaya kadar (ve tarama anında) bekle
    bot._warm_for, bot._snap = at, armed
    assert bot._hold_poll(at - 1.0, lead) and bot._hold_poll(at, lead) and bot._hold_poll(at + 0.3, lead)
    # Kurulamadıysa eski sıra: önce pozisyon kontrolü, sonra (olağan yoldan) tarama
    bot._snap = None
    assert not bot._hold_poll(at - 1.0, lead) and not bot._hold_poll(at, lead)
    bot._snap = armed
    bot.state.paused = True                                                # Tarama yapılmayacaksa ertelenmez
    assert not bot._hold_poll(at, lead)
    bot.state.paused = False
    assert bot._hold_poll(at, lead)
    bot.apply_config(replace(bot.cfg, futures_max_positions=1))            # Boş yer yok: tarama yok, erteleme yok
    bot._snap = armed
    assert not bot._hold_poll(bot.next_scan_at, lead)
    # Ön hazırlık uzun sürüyorsa (yavaş bağlantı) sıradaki o kadar erken başlar; en çok SNAP_LEAD_MAX_SEC
    assert bot._scan_lead() == ft.SNAP_LEAD_SEC
    bot._prep_sec = 2.5
    assert bot._scan_lead() == pytest.approx(2.5 * 1.3 + 0.4)
    bot._prep_sec = 60.0
    assert bot._scan_lead() == ft.SNAP_LEAD_MAX_SEC


def test_unexpected_errors_in_the_new_path_never_stop_the_scan(fcfg, tmp_path):
    """Ön hazırlıkta ya da anlık fiyatla mum kurulurken beklenmeyen bir hata: tarama olağan hızlı yoldan yapılır,
    sinyal kaçmaz, hata ana döngüye çıkmaz."""
    bot, fake, _notes, clock = start(fcfg, tmp_path)
    real = bot._check_snapshot
    bot._check_snapshot = lambda *a, **k: 1 / 0                            # Ön hazırlığın ortasında beklenmeyen hata
    run_until_scan(bot, clock, clock.t)
    row = journal(bot)[-1]
    assert row["sx"].startswith("ön hazırlık hatası (ZeroDivisionError") and row["s"] == {"SOL": "açıldı"}
    assert bot._snap_armed is False and "sn" not in row
    bot._check_snapshot = real
    # Anlık fiyat yanıtı bozuk (fiyat yerine anlamsız değerler taşıyan mum): kurulamaz → olağan yol
    (tmp_path / "b").mkdir()
    bot2, fake2, _n2, clock2 = start(fcfg, tmp_path / "b")
    run_until_prepared(bot2, clock2)
    k, _forming = bot2._snap["k"]["SOLUSDT"]
    bot2._snap["k"]["SOLUSDT"] = (k, {"t": DIP - MIN, "o": "bozuk"})        # Süren mumun alanları eksik / bozuk
    clock2.t = bot2.next_scan_at
    bot2.tick()
    row = journal(bot2)[-1]
    assert row["sx"].startswith("mumlar kurulamadı (") and row["s"] == {"SOL": "açıldı"} and row["ms"][1] == 1000


# ====================================================================== bakiye: girişte istenmez
def test_entries_use_the_balance_read_before_the_close(fcfg, tmp_path):
    """Aynı taramada iki giriş (dalga): bakiye yalnızca kapanıştan önce okunur; ikinci girişte ilk pozisyonun teminatı
    ve komisyonu düşülür. Sonuç, bakiyenin her girişte yeniden okunduğu olağan yolla aynıdır."""
    ada = minute_closes(dip=6.5)
    closes = dict(CLOSES, ADAUSDT=ada)
    sizes = {}
    for name, on in (("settled", False), ("snap", True)):
        (tmp_path / name).mkdir()
        bot, fake, _notes, clock = two_slot(fcfg, tmp_path / name, closes=closes, futures_snapshot_scan=on)
        fake.calls.clear()
        run_until_scan(bot, clock, clock.t)
        assert set(bot.state.positions) == {"SOLUSDT", "ADAUSDT"}
        calls = names(fake)
        first = calls.index("create_order")
        sizes[name] = {s: (p.qty, round(p.margin_usdt, 6)) for s, p in bot.state.positions.items()}
        if on:
            assert calls.count("fetch_balance") == 1 and calls.index("fetch_balance") < calls.index("ticker_price")
            assert "fetch_order_book" not in calls
        else:
            assert calls[first:].count("fetch_balance") == 1               # İkinci giriş bakiyeyi yeniden okur
    assert sizes["snap"] == sizes["settled"]
    assert sizes["snap"]["SOLUSDT"][0] > sizes["snap"]["ADAUSDT"][0] > 0


def test_stale_balance_is_dropped_after_a_margin_rejection(fcfg, tmp_path):
    bot, fake, _notes, clock = start(fcfg, tmp_path)
    run_until_prepared(bot, clock)
    assert bot._free_cache is not None and bot._free_cache[0] == pytest.approx(30.0)
    # Önbellek yalnızca anlık taramanın girişlerinde ve tazeyken kullanılır
    before = names(fake).count("fetch_balance")
    assert bot._available_margin() == pytest.approx(30.0) and names(fake).count("fetch_balance") == before + 1
    bot._snap_live = True
    assert bot._available_margin() == pytest.approx(30.0) and names(fake).count("fetch_balance") == before + 1
    bot._free_cache = (30.0, clock.t - ft.SNAP_BALANCE_MAX_AGE_SEC - 1)
    bot._available_margin()
    assert names(fake).count("fetch_balance") == before + 2
    bot._snap_live = False
    # Emir "teminat yetersiz" (-2019) ile reddedilirse önbellek atılır, miktar küçültülüp bir kez daha denenir
    bot._free_cache = (30.0, clock.t)
    real = fake.create_order
    state = {"n": 0}

    def create(sym, type_, side, amount, price=None, params=None):
        state["n"] += 1
        if state["n"] == 1:
            raise Exception('binance {"code":-2019,"msg":"Margin is insufficient."}')
        return real(sym, type_, side, amount, price, params)
    fake.create_order = create
    clock.t = bot.next_scan_at
    bot.tick()
    assert set(bot.state.positions) == {"SOLUSDT"} and state["n"] >= 2 and bot._free_cache is None


# ====================================================================== doğrulama
def test_each_snapshot_scan_is_verified_against_the_settled_candles(fcfg, tmp_path):
    bot, fake, _notes, clock = two_slot(fcfg, tmp_path)
    fake.drift = {"XRPUSDT": 1.0004}                                       # Kapanıştan sonraki işlemler: XRP %0.04 yukarıda
    run_until_scan(bot, clock, clock.t)
    fake.drift = {}
    assert journal(bot)[-1]["s"] == {"SOL": "açıldı"} and "sv" not in journal(bot)[-1]
    used = bot._snap_pending[-1]
    assert used["closed_ms"] == DIP and used["sig"] == ["SOLUSDT"] and used["scan"] == sorted(CLOSES)
    run_until_scan(bot, clock, clock.t)                                    # Sıradaki dakika: ön hazırlıkta doğrulanır
    row = journal(bot)[-1]
    assert row["t"] == DIP + MIN
    assert row["sv"] == [{"t": DIP, "n": 4, "av": 0.01, "mx": 0.04, "sg": 1, "ok": 1}]     # "sg": o taramada 1 sinyal vardı
    # Ara dakikanın (T0 + 4 dk) kapanışı sıradaki sınır taramasının 5 dk mumlarıyla doğrulanamaz: bir sonraki 1 dk ön
    # hazırlığını bekler ve sınırdaki taramayla birlikte doğrulanır. Hiçbir anlık tarama doğrulamasız kalmaz
    run_until_scan(bot, clock, clock.t)
    assert journal(bot)[-1]["t"] == T0 + 5 * MIN and "sv" not in journal(bot)[-1] and len(bot._snap_pending) == 2
    run_until_scan(bot, clock, clock.t)
    assert journal(bot)[-1]["sv"] == [{"t": DIP + MIN, "n": 4, "av": 0.0, "mx": 0.0, "ok": 1},
                                      {"t": T0 + 5 * MIN, "n": 4, "av": 0.0, "mx": 0.0, "ok": 1}]
    assert len(bot._snap_pending) == 1
    # Rapor satırı
    text = "\n".join(fb.live_timing(bot.cfg, journal(bot)))
    assert ("Tarama zamanlaması (anlık tarama: mum kapanışından sonra, ortanca): başlangıç 0.15 sn → anlık fiyatlar "
            "alındı 0.15 sn → sinyaller hazır 0.15 sn (4 tarama") in text
    assert ("Anlık tarama doğrulaması (3 tarama, sonradan kesinleşen mumlarla): anlık fiyat ile mum kapanışı arasındaki "
            "fark ortalama %0.003, en çok %0.040 | kesinleşmiş mumlarla aynı sinyaller: 3 / 3 tarama") in text
    assert "Giriş gecikmesi (anlık tarama: mum kapanışı → dolum): ortalama 0.1 sn" in text


def test_verification_reports_a_signal_that_settled_candles_would_not_give(fcfg, tmp_path):
    """Eşiği kıl payı geçen sinyal: kesin kapanışta düşüş %5.97 (sinyal yok), anlık fiyat %0.06 aşağıda (%6.03: sinyal).
    Bot girer (o anki fiyatla kural sağlanıyor); doğrulama farkı kayda ve rapora yazar."""
    sol = minute_closes(dip=5.97)
    bot, fake, _notes, clock = two_slot(fcfg, tmp_path, closes=dict(CLOSES, SOLUSDT=sol))
    fake.drift = {"SOLUSDT": 0.9994}
    run_until_scan(bot, clock, clock.t)
    fake.drift = {}
    assert journal(bot)[-1]["s"] == {"SOL": "açıldı"}
    run_until_scan(bot, clock, clock.t)
    check = journal(bot)[-1]["sv"][0]
    assert check["ok"] == 0 and check["mx"] == pytest.approx(0.06, abs=0.001) and check["t"] == DIP
    text = "\n".join(fb.live_snapshot_check(journal(bot)))
    assert ("aynı sinyaller: 0 / 1 tarama (sinyal çıkan tarama: 1) (fark, eşiği kıl payı geçen / geçemeyen "
            "sinyallerdedir)") in text


def test_bad_snapshot_data_switches_back_to_the_settled_path(fcfg, tmp_path):
    names5 = ("SOLUSDT", "ADAUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT")
    closes = {s: (minute_closes() if s == "SOLUSDT" else minute_closes(dip=0.0) * 0 + 100.0) for s in names5}
    bot, fake, notes, clock = two_slot(fcfg, tmp_path, closes=closes, futures_coins="SOL,ADA,XRP,DOGE,BNB")
    fake.drift = {s: 1.005 for s in names5}                                # Fiyat ucu bozuk: her coin %0.5 farklı
    run_until_scan(bot, clock, clock.t)
    fake.drift = {}
    notes.messages.clear()
    run_until_prepared_or_moved(bot, clock)                                # Ön hazırlık: doğrulama veriyi bozuk buldu
    warned = [m for m in notes.messages if "Anlık tarama 60 dakikalığına kapatıldı" in m]
    assert len(warned) == 1 and "ortanca %0.50" in warned[0] and "olağan hızlı" in warned[0]
    assert bot.next_scan_at == pytest.approx((DIP + MIN) / 1000 + 1)       # Bu tarama da olağan hızlı yoldan (1 sn sonra)
    run_until_scan(bot, clock, clock.t)
    row = journal(bot)[-1]
    assert row["sv"][0]["mx"] == pytest.approx(0.5, abs=0.01) and "sn" not in row and row["ms"][0] == 1000
    assert not bot._snapshot_on() and bot._scan_delay() == ft.FAST_SCAN_DELAY_SEC
    assert "geçici olarak kapalı" in bot.scan_report()
    clock.t = bot._snap_off_until + 1                                      # Süre dolunca yeniden denenir
    assert bot._snapshot_on() and bot._scan_delay() == ft.SNAP_SCAN_DELAY_SEC
    # Tek coindeki sıçrama (dalgada olağan) kapatmaz: ortanca küçük kalır
    assert ft.SNAP_BAD_MIN_COINS == 5 and ft.SNAP_BAD_PCT == 0.3


def test_unverifiable_scans_are_dropped_when_too_old(fcfg, tmp_path):
    bot, _fake, _notes, clock = two_slot(fcfg, tmp_path)
    run_until_scan(bot, clock, clock.t)
    assert len(bot._snap_pending) == 1
    bot._check_snapshot({}, MIN, DIP + ft.SNAP_CHECK_MAX_AGE_MS + MIN)     # Uzun süre tarama yapılmadı: eski kayıt bırakılır
    assert bot._snap_pending == [] and bot._snap_checks == []
    bot._snap_pending = [{"closed_ms": DIP, "shift": DIP % FUT_BAR_MS, "px": {}}]   # Taraması yarıda kalmış (sinyalsiz) kayıt
    bot._check_snapshot({}, MIN, DIP + MIN)
    assert bot._snap_pending == []


# ====================================================================== rapor
def test_report_lines_for_snapshot_scans(fcfg):
    t0 = BASE_MS
    rows = [{"t": t0, "ms": [1000, 2400, 2450], "fast": 1},
            {"t": t0 + MIN, "ms": [160, 420, 430], "fast": 1, "sn": [410, 180], "co": -12,
             "f": {"SOL": {"o": 440, "r": 700, "x": 590, "p": 0.012}}},
            {"t": t0 + 2 * MIN, "ms": [150, 380, 390], "fast": 1, "sn": [370, 160],
             "sv": [{"t": t0 + MIN, "n": 24, "av": 0.004, "mx": 0.031, "ok": 1}]},
            {"t": t0 + 3 * MIN, "ms": [150, 1900, 1950], "fast": 1, "sx": "anlık fiyatlar alınamadı: zaman aşımı",
             "sv": [{"t": t0 + 2 * MIN, "n": 24, "av": 0.002, "mx": 0.011, "sg": 2, "ok": 0}, {"t": 1, "n": 0}]},
            {"t": t0 + 4 * MIN, "ms": [150, 2000, 2010], "fast": 1, "sx": "geçmiş mumlar 6 / 24 coinde hazır"},
            {"t": t0 + 5 * MIN, "ms": [150, 2000, 2010], "fast": 1, "sx": "geçmiş mumlar 0 / 24 coinde hazır"}]
    text = "\n".join(fb.live_timing(fcfg, rows))
    assert ("Tarama zamanlaması (hızlı giriş açık: mum kapanışından sonra, ortanca): başlangıç 0.1 sn → mumlar alındı "
            "2.0 sn → sinyaller hazır 2.0 sn (4 tarama)") in text
    assert ("Tarama zamanlaması (anlık tarama: mum kapanışından sonra, ortanca): başlangıç 0.15 sn → anlık fiyatlar "
            "alındı 0.40 sn → sinyaller hazır 0.41 sn (2 tarama; Binance ile saat farkı ortanca 12 ms)") in text
    assert "Giriş gecikmesi (anlık tarama: mum kapanışı → dolum): ortalama 0.6 sn" in text and "+0.012%" in text
    assert ("Anlık tarama doğrulaması (2 tarama, sonradan kesinleşen mumlarla): anlık fiyat ile mum kapanışı arasındaki "
            "fark ortalama %0.003, en çok %0.031 | kesinleşmiş mumlarla aynı sinyaller: 1 / 2 tarama (sinyal çıkan "
            "tarama: 1)") in text
    # Kullanılamama nedenleri sayılardan arındırılıp gruplanır (6 / 24 ile 0 / 24 aynı neden)
    assert ("Anlık fiyat 3 taramada kullanılamadı (olağan hızlı yoldan tarandı): geçmiş mumlar N / N coinde hazır: 2; "
            "anlık fiyatlar alınamadı: 1") in text
    assert fb.live_snapshot_check([{"t": t0, "ms": [3000, 3100, 3100]}]) == []
    # Anlık tarama hiç kullanılmadıysa eski satırlar aynen
    old = fb.live_timing(fcfg, [{"t": t0, "ms": [3000, 3100, 3100]}])
    assert len(old) == 1 and old[0].startswith("  Tarama zamanlaması (mum kapanışından sonra, ortanca): başlangıç 3.0 sn")


def test_report_timing_lines_cover_the_journal_after_the_backtest_data_ends(fcfg, tmp_path):
    """Backtest verisi bitmiş günde kalır; bot kaydı ise şu ana kadar sürer. Güncellemeden sonraki saatlerin zamanlaması
    ve anlık tarama doğrulaması, işlem karşılaştırması o saatleri kapsamasa da aynı gün raporda görünür."""
    import json
    t0 = BASE_MS
    end = t0 + 10 * FUT_BAR_MS                                             # Backtest verisinin bittiği an
    rows = [{"t": t0, "u": ["SOL", "ADA"], "a": ["ADA", "SOL"], "ms": [1000, 2000, 2010], "fast": 1},
            {"t": t0 + FUT_BAR_MS, "a": ["ADA", "SOL"], "ms": [1000, 2100, 2110], "fast": 1},
            # Veri bittikten sonra (bot güncellendi, anlık tarama açıldı)
            {"t": end + 60 * MIN, "a": ["ADA", "SOL"], "ms": [150, 410, 420], "fast": 1, "sn": [400, 170]},
            {"t": end + 61 * MIN, "a": ["ADA", "SOL"], "ms": [150, 390, 400], "fast": 1, "sn": [380, 150],
             "sv": [{"t": end + 60 * MIN, "n": 2, "av": 0.002, "mx": 0.004, "ok": 1}],
             "s": {"SOL": "açıldı"}, "f": {"SOL": {"o": 400, "r": 660, "x": 530, "p": 0.011}}}]
    path = tmp_path / "futures_scans_paper.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    data = fb.FutData(t0, end, [fb.Segment(t0 - 1, end, ("SOL", "ADA"))], {}, ((), (), ()), (None, None), {}, None, "5m")
    text = "\n".join(fb.live_comparison(fcfg, data, [], path=path))
    assert "Kayıtlı 5 dk mum: 2 / 2" in text                              # İşlem karşılaştırması: veri aralığındaki satırlar
    assert "Tarama zamanlaması (hızlı giriş açık: mum kapanışından sonra, ortanca): başlangıç 1.0 sn" in text
    assert "Tarama zamanlaması (anlık tarama: mum kapanışından sonra, ortanca): başlangıç 0.15 sn" in text
    assert "Giriş gecikmesi (anlık tarama: mum kapanışı → dolum): ortalama 0.5 sn" in text and "+0.011%" in text
    assert "Anlık tarama doğrulaması (1 tarama" in text and "aynı sinyaller: 1 / 1 tarama" in text
    assert "(Zamanlama ve doğrulama satırları kaydın tamamından," in text and "backtest verisinin bittiği" in text
    # Kayıt veri aralığını aşmıyorsa not yok
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[:2]), encoding="utf-8")
    assert "kaydın tamamından" not in "\n".join(fb.live_comparison(fcfg, data, [], path=path))
    # Kayıt tümüyle veri bittikten sonra başlıyorsa: karşılaştırma yarın, zamanlama bugün
    path.write_text("".join(json.dumps(dict(r, u=["SOL", "ADA"]), ensure_ascii=False) + "\n" for r in rows[2:]),
                    encoding="utf-8")
    late = "\n".join(fb.live_comparison(fcfg, data, [], path=path))
    assert "yarınki backtestte görünür" in late and "Tarama zamanlaması (anlık tarama:" in late


# ====================================================================== geçmiş mum belleği
def test_history_helpers_split_merge_and_compare():
    k = k1_of(np.arange(1.0, 21.0), start=BASE_MS)                        # 20 adet 1 dk mum (kapanışlar 1..20)
    close = BASE_MS + 20 * MIN                                             # 20. mumun kapanışı
    row = dict({f: float(k[f][19]) for f in FIELDS if f != "t"}, t=int(k["t"][19]))
    # Yanıt kapanıştan ÖNCE okundu: 20. mum süren mum olarak ayrı gelir; geçmiş olduğu gibi
    hist, forming = split_history({f: v[:19] for f, v in k.items()}, row, close, MIN)
    assert len(hist["t"]) == 19 and forming is row
    # Yanıt kapanıştan SONRA okundu (rapor 22: 46 taramada): 20. mum "kapanmış" satırlar arasında gelir ama henüz
    # kesinleşmemiş olabilir → geçmişe alınmaz, süren mum olarak döner
    late, forming = split_history(k, None, close, MIN)
    assert len(late["t"]) == 19 and int(late["t"][-1]) + 2 * MIN == close
    assert forming["t"] == close - MIN and forming["c"] == 20.0 and set(forming) == set(FIELDS)
    # Daha da geç (bir sonraki mum da kapanmış görünüyor): yine yalnızca o kapanıştan önceki mumlar
    later, forming = split_history(k, None, close - MIN, MIN)
    assert len(later["t"]) == 18 and forming["c"] == 19.0
    assert split_history({f: v[:19] for f, v in k.items()}, None, close, MIN)[1] is None      # Süren mum yanıtta yok
    # Bellek + son mumlar: ortak mumlar aynı, yeniler kesintisiz devam → eklenir; en yeni `keep` mum kalır
    base = {f: v[:15] for f, v in k.items()}
    merged = merge_tail(base, {f: v[12:19] for f, v in k.items()}, MIN, 485)
    assert np.array_equal(merged["t"], k["t"][:19]) and np.array_equal(merged["c"], k["c"][:19])
    assert merged["t"].dtype == np.int64 and set(merged) == set(FIELDS)
    assert np.array_equal(merge_tail(base, {f: v[12:19] for f, v in k.items()}, MIN, 10)["t"], k["t"][9:19])
    assert np.array_equal(merge_tail(base, {f: v[10:15] for f, v in k.items()}, MIN, 485)["t"], base["t"])   # Yeni mum yok
    assert np.array_equal(merge_tail(base, {f: v[15:19] for f, v in k.items()}, MIN, 485)["t"], k["t"][:19])  # Tam devamı
    # Güvenilmez durumlar: tam geçmiş yeniden istenir (None)
    assert merge_tail(base, {f: v[16:19] for f, v in k.items()}, MIN, 485) is None          # Arada eksik mum
    assert merge_tail(base, {f: np.concatenate([v[12:14], v[15:19]]) for f, v in k.items()}, MIN, 485) is None
    changed = {f: v[12:19].copy() for f, v in k.items()}
    changed["c"][0] += 0.5                                                 # Ortak mum farklı görünüyor
    assert merge_tail(base, changed, MIN, 485) is None
    assert merge_tail(base, {f: v[:0] for f, v in k.items()}, MIN, 485) is None
    assert merge_tail({f: v[:0] for f, v in k.items()}, base, MIN, 485) is None
    # Bellek ile tam yenilemenin karşılaştırması
    other = {f: v.copy() for f, v in k.items()}
    other["c"][3] *= 1.01
    assert history_mismatch(k, k) == 0 and history_mismatch(k, other) == 1
    assert history_mismatch(k, {f: v[:0] for f, v in k.items()}) == 0


def test_history_is_kept_in_memory_and_only_the_last_candles_are_requested(fcfg, tmp_path):
    bot, fake, _notes, clock = two_slot(fcfg, tmp_path, at=DIP - 3 * MIN + 10_000)
    limit = futures_minute_limit(bot.cfg)
    fake.asked.clear()
    run_until_scan(bot, clock, clock.t)                                    # İlk tarama: tam geçmiş (coin başına 485 mum)
    assert {(i, n) for _s, i, n in fake.asked} == {("1m", limit)} and len(fake.asked) == 4
    assert set(bot._hist) == {(s, "1m") for s in CLOSES}
    # Tam yenilemeler zamana yayılır: coinlerin hepsi aynı dakikada yeniden istenmez
    due = sorted(v["full_at"] for v in bot._hist.values())
    assert due[0] < due[-1] and due[-1] - due[0] < ft.SNAP_FULL_REFRESH_SEC and len(set(due)) == 4
    for _ in range(2):                                                     # Sonraki taramalar: coin başına yalnızca son 6 mum
        fake.asked.clear()
        run_until_scan(bot, clock, clock.t)
        assert {(i, n) for _s, i, n in fake.asked} == {("1m", ft.SNAP_TAIL_BARS)} and len(fake.asked) == 4
    row = journal(bot)[-1]
    assert row["t"] == DIP and row["s"] == {"SOL": "açıldı"} and row["sn"][0] == 150      # Sinyal bellekteki geçmişle bulundu
    assert bot.state.positions["SOLUSDT"].metrics["drop_pct"] == pytest.approx(7.0, abs=0.05)
    # Bellek, borsanın tam geçmişiyle birebir aynı (aynı mumlar, aynı sayıda)
    for symbol in CLOSES:
        mem, full = bot._hist[(symbol, "1m")]["k"], fake.k1[symbol]
        assert len(mem["t"]) == limit - 1 and int(mem["t"][-1]) == DIP - 2 * MIN
        keep = (full["t"] >= mem["t"][0]) & (full["t"] <= mem["t"][-1])
        assert np.array_equal(mem["t"], full["t"][keep]) and np.array_equal(mem["c"], full["c"][keep])
    # Ayar değişince bellek atılır (pencere / mum sayısı değişmiş olabilir)
    bot.apply_config(replace(bot.cfg, futures_extra_breadth=3))
    assert bot._hist == {} and bot._snap is None


def test_prefetch_that_overruns_the_close_still_arms_the_snapshot(fcfg, tmp_path):
    """Rapor 22: 898 taramanın 46'sında ön hazırlık mum kapanışına sarktı (yanıtlar kapanıştan sonra okundu) ve anlık
    tarama o mum için düşürüldü ("geçmiş mumlar 0 / 24 coinde hazır"). Kapanıştan önceki mumlar ne zaman okunursa
    okunsun kesindir: geç okunan yanıt artık anlık taramayı düşürmez."""
    for cached in (False, True):                                           # Tam geçmiş yoluyla ve bellek yoluyla
        folder = tmp_path / str(cached)
        folder.mkdir()
        bot, fake, _notes, clock = start(fcfg, folder)
        run_until_prepared(bot, clock)
        if not cached:
            bot._hist = {}
        fake.asked.clear()
        clock.t = DIP / 1000 + 0.05                                        # Yanıtlar kapanıştan 0.05 sn sonra okunuyor
        bot._prefetch()
        assert {n for _s, _i, n in fake.asked} == {ft.SNAP_TAIL_BARS if cached else futures_minute_limit(bot.cfg)}
        assert bot._snap is not None and bot._snap_miss == "" and len(bot._snap["k"]) == 4
        k, forming = bot._snap["k"]["SOLUSDT"]
        assert int(k["t"][-1]) + 2 * MIN == DIP                            # Az önce kapanan mum geçmişe alınmadı...
        assert forming["t"] == DIP - MIN and forming["c"] == pytest.approx(93.0)      # ...süren mum sayıldı
        clock.t = bot.next_scan_at
        bot.tick()
        row = journal(bot)[-1]
        assert row["s"] == {"SOL": "açıldı"} and "sx" not in row and row["sn"][0] == 150


def test_gap_in_memory_falls_back_to_the_full_history(fcfg, tmp_path):
    bot, fake, _notes, clock = start(fcfg, tmp_path, at=DIP - 23 * MIN + 10_000)        # Taramalar ara dakikalarda
    limit = futures_minute_limit(bot.cfg)
    run_until_scan(bot, clock, clock.t)
    assert len(bot._hist) == 4
    # Bot 11 dakika tarama yapamadı (ör. boş yer yoktu): son 6 mum belleğin devamı değil → tam geçmiş yeniden istenir
    clock.t += 11 * 60
    bot.next_scan_at, bot._warm_for = bot._next_close(clock.t) + bot._scan_delay(), 0.0
    fake.asked.clear()
    run_until_scan(bot, clock, clock.t)
    asked = [(i, n) for _s, i, n in fake.asked]
    # Önce son mumlar denenir, belleğe eklenemeyince tam geçmiş istenir (dördüncü coinin dönemsel yenilemesi zaten gelmişti)
    assert asked.count(("1m", ft.SNAP_TAIL_BARS)) == 3 and asked.count(("1m", limit)) == 4
    assert journal(bot)[-1]["sn"][0] == 150
    for symbol in CLOSES:                                                  # Bellek yine kesintisiz ve güncel
        t = bot._hist[(symbol, "1m")]["k"]["t"]
        assert np.all(np.diff(t) == MIN) and int(t[-1]) + 2 * MIN == journal(bot)[-1]["t"]


def test_memory_is_refreshed_periodically_and_checked_against_the_exchange(fcfg, tmp_path):
    bot, fake, notes, clock = start(fcfg, tmp_path, at=DIP - 23 * MIN + 10_000)         # Taramalar ara dakikalarda
    limit = futures_minute_limit(bot.cfg)
    run_until_scan(bot, clock, clock.t)
    key = ("XRPUSDT", "1m")
    good = bot._hist[key]["k"]["c"].copy()
    # Süresi dolan coinin tam geçmişi yeniden istenir; bellekle aynıysa uyarı yok
    bot._hist[key]["full_at"] = clock.t - ft.SNAP_FULL_REFRESH_SEC - 1
    notes.messages.clear()
    fake.asked.clear()
    run_until_scan(bot, clock, clock.t)
    assert sorted((s, n) for s, _i, n in fake.asked) == sorted(
        [("XRPUSDT", limit)] + [(s, ft.SNAP_TAIL_BARS) for s in CLOSES if s != "XRPUSDT"])
    assert not [m for m in notes.messages if "bellekte tutulan" in m]
    assert bot._hist[key]["full_at"] == pytest.approx(clock.t, abs=5)
    # Bellekteki bir mum bozulmuşsa (olmamalı) tam yenilemede fark edilir: bellek yenilenir, bir kez uyarı gelir
    for n in range(2):
        bot._hist[key]["k"]["c"][-10] *= 1.01
        bot._hist[key]["full_at"] = clock.t - ft.SNAP_FULL_REFRESH_SEC - 1
        run_until_scan(bot, clock, clock.t)
        assert history_mismatch(bot._hist[key]["k"], {"t": fake.k1["XRPUSDT"]["t"], "c": fake.k1["XRPUSDT"]["c"]}) == 0
    warned = [m for m in notes.messages if "bellekte tutulan geçmiş mumlar" in m]
    assert len(warned) == 1 and "XRPUSDT" in warned[0] and "1 mumda farklı" in warned[0]     # Saatte en çok bir uyarı
    assert good[-1] in bot._hist[key]["k"]["c"]
