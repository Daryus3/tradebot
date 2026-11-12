"""Canlı giriş gecikmesi: ölçüm (tarama kaydı, pozisyon, açılış mesajı, backtest raporu) ve hızlı giriş
(FUTURES_FAST_ENTRY: tarama mum kapanışından 1 sn sonra, yeni mum kontrolü, tek dalgada mumlar, teminat modu + kaldıracın
önceden ayarlanıp doğrulanması). Varsayılan kapalı: kapalıyken davranış eskisiyle aynı."""
import json
import time
from dataclasses import replace

import pytest

import futures_backtest as fb
import futures_trader as ft
from commands import CALLBACK, Command
from config import _setting_notes, load_config
from futures_exchange import FFill, FuturesError, FuturesExchange, _order_ts
from runtime_settings import apply_settings
from strategy import FUT_BAR_MS, FuturesSignal
from test_futures import BASE_MS, Clock, fcfg, make_fbot  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, crashing, flat, live_cfg
from test_futures_universe import META, TICKERS, UniCcxt

CLOSE = BASE_MS / 1000                       # Sahte borsanın son mumunun kapanış anı (sn)
CLOSES = {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}


class FastCcxt(KlineCcxt):
    """KlineCcxt + Binance'in hızlı girişte kullanılan uçları: yanıttaki süren (yeni) mum, parite ayarları
    (teminat modu / kaldıraç), sunucu saati, emir yanıtında işlem zamanı. `rtt` > 0 ise her istek o kadar (gerçek)
    saniye sürer: paralel istekler üst üste biner, sıralı istekler toplanır."""

    def __init__(self, closes, clock=None, rtt=0.0, **kw):
        super().__init__(closes, **kw)
        self.clock = clock
        self.rtt = rtt
        self.forming = True                  # Yanıtta yeni mum da var (Binance o mumu kapatmış)
        self.forming_after = {}              # parite → yeni mum kaçıncı istekten itibaren görünür
        self.kline_count = {}
        self.settings = {}                   # parite → (teminat modu, kaldıraç)
        self.settings_error = None
        self.exchange_lag_ms = None          # Emir yanıtındaki updateTime: saat − bu kadar ms (None = alan yok)
        self.enableRateLimit = True
        self.throttle_seen = []

    def _wait(self):
        if self.rtt:
            time.sleep(self.rtt)

    def fapiPublicGetKlines(self, params):
        self._wait()
        self.throttle_seen.append(self.enableRateLimit)
        sym = params["symbol"]
        self.calls.append(("klines", sym, params["interval"]))
        rows = super().fapiPublicGetKlines(params)
        self.kline_count[sym] = n = self.kline_count.get(sym, 0) + 1
        if rows and self.forming and n >= self.forming_after.get(sym, 1):
            last = rows[-1]
            rows = rows[1:] + [[BASE_MS, last[4], last[4], last[4], last[4], "1", BASE_MS + FUT_BAR_MS - 1, "1", "1",
                                "1", "0", "0"]]
        return rows

    def fapiPublicGetTime(self, params=None):
        self._wait()
        self.calls.append(("time",))
        return {"serverTime": str(int(self.clock() * 1000) + 120)}

    def fapiPrivateGetSymbolConfig(self, params=None):
        self._wait()
        self.calls.append(("symbol_config",))
        if self.settings_error:
            raise Exception(self.settings_error)
        return [{"symbol": s, "marginType": m, "leverage": lev, "isAutoAddMargin": "false"}
                for s, (m, lev) in self.settings.items()]

    def set_margin_mode(self, mode, sym):
        self._wait()
        super().set_margin_mode(mode, sym)
        symbol = sym.split("/")[0] + "USDT"
        self.settings[symbol] = ("ISOLATED", self.settings.get(symbol, ("CROSSED", 20))[1])

    def set_leverage(self, lev, sym):
        self._wait()
        super().set_leverage(lev, sym)
        symbol = sym.split("/")[0] + "USDT"
        self.settings[symbol] = (self.settings.get(symbol, ("CROSSED", 20))[0], lev)

    def fetch_balance(self):
        self._wait()
        return super().fetch_balance()

    def fetch_order_book(self, sym, limit=5):
        self._wait()
        self.calls.append(("fetch_order_book", sym))
        return super().fetch_order_book(sym, limit)

    def create_order(self, sym, type_, side, amount, price=None, params=None):
        self._wait()
        order = super().create_order(sym, type_, side, amount, price, params)
        if type_ == "market" and self.exchange_lag_ms is not None:
            order["info"] = {"updateTime": str(int(self.clock() * 1000) - self.exchange_lag_ms)}
        return order


class RealClock:
    """Verilen andan başlayıp gerçek geçen süre kadar ilerleyen saat (gecikmeli sahte borsayla kullanılır)."""

    def __init__(self, t):
        self.set(t)

    def set(self, t):
        self.base, self.t0 = t, time.monotonic()

    def __call__(self):
        return self.base + (time.monotonic() - self.t0)


def fast_cfg(fcfg, **over):
    return live_cfg(fcfg, futures_fast_entry=True, **over)


def journal(bot):
    return [json.loads(line) for line in bot.scan_log_path.read_text(encoding="utf-8").splitlines()]


def stepping(bot, clock):
    """Kısa beklemeler sahte saati ilerletir (gerçekte beklenmez)."""
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        clock.t += seconds
    bot.sleep_fn = sleep
    return waits


def run_until_scan(bot, clock, start):
    """Döngüyü sahte saatle, bot.run'daki gibi (_next_sleep kadar bekleyerek) sıradaki tarama bitene kadar yürütür."""
    clock.t = start
    target = bot.next_scan_at
    for _ in range(400):
        bot.tick()
        if bot.next_scan_at != target:
            return
        clock.t += bot._next_sleep()
    raise AssertionError("tarama yapılmadı")


# ====================================================================== ayar
def test_fast_entry_is_off_by_default_and_editable(fcfg, tmp_path, monkeypatch):
    assert fcfg.futures_fast_entry is False
    assert not any("FUTURES_FAST_ENTRY" in n for n in _setting_notes(fcfg))
    new, _ = apply_settings(fcfg, {"FUTURES_FAST_ENTRY": "true"})
    assert new.futures_fast_entry and any("FUTURES_FAST_ENTRY=true" in n for n in _setting_notes(new))
    monkeypatch.setenv("FUTURES_FAST_ENTRY", "true")
    assert load_config(env_file=str(tmp_path / "x.env"), mode_override="paper").futures_fast_entry
    bot, _fake, notes, _clock = make_fbot(fcfg, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    assert bot._scan_delay() == ft.SCAN_DELAY_SEC and bot.next_scan_at == CLOSE + FUT_BAR_MS / 1000 + 3
    bot.commands.handle(Command(CALLBACK, ["pg", "fx"], kind="callback", message_id=7))
    text, keyboard, _mid = notes.menus[-1]
    assert "kv:fx:FUTURES_FAST_ENTRY:t" in [d for row in keyboard for _l, d in row]
    assert "⚡ Hızlı giriş: kapalı" in text and "5 sn'de %0.05" in text
    bot.commands.handle(Command(CALLBACK, ["kv", "fx", "FUTURES_FAST_ENTRY", "t"], kind="callback", message_id=7))
    assert bot.cfg.futures_fast_entry and bot.commands.pending is None           # Onay gerekmez
    assert "FUTURES_FAST_ENTRY=true" in env.read_text(encoding="utf-8")
    assert "⚡ Hızlı giriş: açık" in notes.menus[-1][0]
    # Çalışırken açılınca sıradaki tarama yeni gecikmeyle (3 → 1 sn), kapatılınca eskisiyle
    assert bot._scan_delay() == ft.FAST_SCAN_DELAY_SEC and bot.next_scan_at == CLOSE + FUT_BAR_MS / 1000 + 1
    bot.apply_config(replace(bot.cfg, futures_fast_entry=False))
    assert bot.next_scan_at == CLOSE + FUT_BAR_MS / 1000 + 3


# ====================================================================== ölçüm (her zaman açık)
def test_entry_lag_is_measured_in_journal_position_and_message(fcfg, tmp_path):
    clock = Clock(CLOSE + 3)
    fake = FastCcxt(CLOSES, clock=clock)
    fake.exchange_lag_ms = 400                                   # Binance emri, yanıt gelmeden 400 ms önce işlemiş
    bot, fake, notes, clock = make_fbot(live_cfg(fcfg), tmp_path, live=True, fake=fake, clock=clock)
    fake.prices["SOLUSDT"] = float(CLOSES["SOLUSDT"][-1]) * 1.001   # Dolum, sinyal mumunun kapanışından %0.1 yukarıda
    bot.next_scan_at = clock.t
    bot.tick()
    row = journal(bot)[-1]
    assert row["ms"] == [3000, 3000, 3000] and "fast" not in row and "kt" not in row
    assert row["s"] == {"SOL": "açıldı"} and row["f"] == {"SOL": {"o": 3000, "r": 3000, "x": 2600, "p": 0.1}}
    assert "e" not in row and row["a"] == ["ADA", "DOGE", "SOL", "XRP"]          # Eski alanlar aynen
    pos = bot.state.positions["SOLUSDT"]
    assert pos.metrics["lag_ms"] == 2600.0 and pos.metrics["slip_pct"] == pytest.approx(0.1)
    opened = [m for m in notes.messages if "POZİSYON AÇILDI" in m][-1]
    assert "Giriş gecikmesi: mum kapanışından 2.6 sn sonra doldu; kapanış fiyatından %0.100 pahalıya alındı" in opened
    assert bot.last_timing["ms"] == [3000, 3000, 3000]
    assert "Zamanlama (mum kapanışından sonra): tarama 3.0 sn'de başladı" in bot.scan_report()
    assert "hızlı giriş kapalı" in bot.scan_report()


def test_entry_lag_without_exchange_time_uses_response_time_and_paper_works(fcfg, tmp_path):
    bot, fake, notes, clock = make_fbot(live_cfg(fcfg), tmp_path, fake=FastCcxt(CLOSES))      # Test modu
    bot.next_scan_at = clock.t
    bot.tick()
    row = journal(bot)[-1]
    assert set(row["f"]["SOL"]) == {"o", "r", "p"} and row["f"]["SOL"]["r"] == 3000
    assert row["f"]["SOL"]["p"] == pytest.approx(bot.cfg.futures_slippage_pct, abs=1e-3)   # Sanal dolum: ask + kayma
    assert bot.state.positions["SOLUSDT"].metrics["lag_ms"] == 3000.0
    # Elle / tarama dışı çağrıda saçma gecikme (saat atlaması) ölçüm sayılmaz
    sig = FuturesSignal("ADAUSDT", "long", 100.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0})
    bot._note_entry_lag(sig, FFill(100.0, 1.0, 0.0), clock.t, clock.t + 3600, clock.t + 3600)
    assert "lag_ms" not in sig.metrics


def test_order_time_and_kline_state_helpers(fcfg):
    assert _order_ts({"info": {"updateTime": "1700000000123"}}) == 1700000000123
    assert _order_ts({"info": {}, "lastTradeTimestamp": None, "timestamp": 5}) == 5
    assert _order_ts({"info": None}) == 0 and _order_ts({"info": {"updateTime": "x"}}) == 0
    clock = Clock(CLOSE + 1)
    fake = FastCcxt(CLOSES, clock=clock)
    ex = FuturesExchange(fcfg, exchange=fake, time_fn=clock)
    k, open_ms = ex.klines_state("SOLUSDT", "5m", 300)
    assert int(k["t"][-1]) + FUT_BAR_MS == BASE_MS and open_ms == BASE_MS and len(k["t"]) == 299
    assert len(ex.klines("SOLUSDT", "5m", 300)["t"]) == 299                    # Süren mum eskisi gibi atılır
    fake.forming = False
    assert ex.klines_state("SOLUSDT", "5m", 300)[1] == 0
    assert ex.klines_state("YOKUSDT", "5m", 300)[1] == 0
    # Parite ayarları: tek istek, parite → (ISOLATED mı, kaldıraç)
    fake.settings = {"SOLUSDT": ("ISOLATED", 3), "ADAUSDT": ("CROSSED", 20)}
    assert ex.symbol_settings() == {"SOLUSDT": (True, 3), "ADAUSDT": (False, 20)}
    fake.settings_error = 'binance {"code":-2015,"msg":"Invalid API-key"}'
    with pytest.raises(FuturesError):
        ex.symbol_settings()
    # CCXT'nin istekler arası beklemesi yalnızca blok içinde kapalı; hata olsa da geri açılır
    with ex.unthrottled():
        assert fake.enableRateLimit is False
    assert fake.enableRateLimit is True
    with pytest.raises(ValueError):
        with ex.unthrottled():
            raise ValueError("x")
    assert fake.enableRateLimit is True
    assert ex.warm(4) == pytest.approx(120.0) and [c for c in fake.calls if c == ("time",)] == [("time",)] * 4


# ====================================================================== hızlı yol
def test_fast_scan_starts_one_second_after_close_and_skips_prepare(fcfg, tmp_path):
    clock = Clock(CLOSE - 60)
    fake = FastCcxt(CLOSES, clock=clock)
    fake.exchange_lag_ms = 30
    bot, fake, notes, clock = make_fbot(fast_cfg(fcfg), tmp_path, live=True, fake=fake, clock=clock)
    assert bot.next_scan_at == CLOSE + 1                         # Kapanıştan 1 sn sonra (kapalıyken 3)
    fake.calls.clear()
    run_until_scan(bot, clock, CLOSE - 60)
    names = [c[0] for c in fake.calls]
    # Taramalar arasında: 4 coinin teminat modu + kaldıracı önceden ayarlandı; taramadan hemen önce doğrulandı
    assert names.count("set_margin_mode") == 4 and names.count("set_leverage") == 4
    assert names.count("symbol_config") == 2 and names.count("time") == 4
    scan_at = names.index("klines")
    assert max(i for i, n in enumerate(names) if n in ("set_margin_mode", "set_leverage", "symbol_config", "time")) \
        < scan_at                                                # ...hepsi mum kapanmadan (taramadan) önce
    # Sinyal anında: mumlar → bakiye → fiyat → emir. Teminat modu / kaldıraç isteği yok, 24 saatlik özet yok
    entry = names[scan_at:names.index("create_order") + 1]
    assert sorted(set(entry)) == ["create_order", "fetch_balance", "fetch_order_book", "klines"]
    assert all(seen is False for seen in fake.throttle_seen)     # Mumlar CCXT beklemesi olmadan istendi
    row = journal(bot)[-1]
    assert row["ms"] == [1000, 1000, 1000] and row["fast"] == 1 and row["co"] == 120
    assert row["f"]["SOL"] == {"o": 1000, "r": 1000, "x": 970, "p": 0.0}
    pos = bot.state.positions["SOLUSDT"]
    assert pos.leverage == 3 and pos.stop_order_id and pos.metrics["lag_ms"] == 970.0
    assert "hızlı giriş açık" in bot.scan_report()


def test_fast_path_is_measurably_faster_than_old_path(fcfg, tmp_path):
    """Gecikmeli sahte borsa (istek başına 20 ms) ve gerçek geçen süreyle ilerleyen saat: mum kapanışından emrin
    yanıtına kadar geçen süre eski yolda 3 sn + sıralı istekler, hızlı yolda 1 sn + daha az sayıda istek."""
    lags, entry_calls = {}, {}
    for name, cfg in (("old", live_cfg(fcfg)), ("fast", fast_cfg(fcfg))):
        clock = RealClock(CLOSE - 30)
        fake = FastCcxt(CLOSES, clock=clock)
        fake.settings = {s: ("ISOLATED", 3) for s in ("SOLUSDT", "ADAUSDT", "XRPUSDT", "DOGEUSDT")}   # Zaten ayarlı
        (tmp_path / name).mkdir()
        bot, fake, _notes, clock = make_fbot(cfg, tmp_path / name, live=True, fake=fake, clock=clock)
        if name == "fast":
            clock.set(bot.next_scan_at - ft.FAST_WARM_LEAD_SEC)
            bot.tick()                                           # Ön hazırlık: bağlantılar + doğrulama
        fake.rtt = 0.02
        fake.calls.clear()
        clock.set(bot.next_scan_at)
        bot.tick()
        row = journal(bot)[-1]
        assert row["s"] == {"SOL": "açıldı"}
        lags[name] = row["f"]["SOL"]["r"]
        names = [c[0] for c in fake.calls]
        entry_calls[name] = names[:names.index("create_order") + 1]
    assert entry_calls["old"].count("set_margin_mode") == 1 and entry_calls["old"].count("set_leverage") == 1
    assert "set_margin_mode" not in entry_calls["fast"] and "set_leverage" not in entry_calls["fast"]
    assert lags["old"] >= 3000 + 9 * 20                          # 5 mum (sıralı) + 2 hazırlık + bakiye + fiyat + emir
    assert 1000 + 4 * 20 <= lags["fast"] < lags["old"] - 2000    # 1 dalga mum + bakiye + fiyat + emir


NEAR = dict(CLOSES, ADAUSDT=crashing(drop=5.5))      # ADA 1 sa %6 eşiğine 0.5 puan uzakta: sinyal yok ama karara yakın


def test_fast_scan_retries_until_new_candle_shows(fcfg, tmp_path):
    # 1) Karara yakın bir coinde (düşüşü eşiğe 1 puandan yakın) yeni mum hiç görünmüyor, diğerlerinde var: yalnızca o
    #    coin 0.25 sn arayla yeniden istenir; başka coinlerde göründü diye erken kabul edilmez (kapanan mumu henüz
    #    kesinleşmemiş olabilir), eski tarama anında (kapanış + 3 sn) eldeki mumla devam edilir
    clock = Clock(CLOSE + 1)
    fake = FastCcxt(NEAR, clock=clock)
    fake.forming_after = {"ADAUSDT": 99}
    bot, fake, _notes, clock = make_fbot(fast_cfg(fcfg), tmp_path, fake=fake, clock=clock)
    waits = stepping(bot, clock)
    fake.calls.clear()
    bot.next_scan_at = clock.t
    bot.tick()
    asked = [c[1] for c in fake.calls if c[0] == "klines"]
    assert asked.count("ADAUSDT") == 9 and asked.count("SOLUSDT") == 1 and waits == [ft.FAST_SETTLE_WAIT_SEC] * 8
    row = journal(bot)[-1]
    assert row["kt"] == 9 and "ADA" in row["a"] and row["s"] == {"SOL": "açıldı"} and row["ms"] == [1000, 3000, 3000]
    # Geciken coin iki denemede yetişirse tarama o kadar bekler
    clock1 = Clock(CLOSE + 1)
    fake1 = FastCcxt(NEAR, clock=clock1)
    fake1.forming_after = {"ADAUSDT": 3}
    (tmp_path / "a").mkdir()
    bot1, fake1, _n1, clock1 = make_fbot(fast_cfg(fcfg), tmp_path / "a", fake=fake1, clock=clock1)
    waits1 = stepping(bot1, clock1)
    fake1.calls.clear()
    bot1.next_scan_at = clock1.t
    bot1.tick()
    asked1 = [c[1] for c in fake1.calls if c[0] == "klines"]
    assert asked1.count("ADAUSDT") == 3 and asked1.count("XRPUSDT") == 1 and journal(bot1)[-1]["ms"][1] == 1500
    assert waits1 == [ft.FAST_SETTLE_WAIT_SEC] * 2
    # 2) Hiçbirinde yeni mum yok (sunucu saati ileride / veri yerleşmedi): en geç eski tarama anına (kapanış + 3 sn)
    #    kadar yeniden denenir, sonra eski davranış: az önce kapanan mum yanıttaysa yeterli
    clock2 = Clock(CLOSE + 1)
    fake2 = FastCcxt(CLOSES, clock=clock2)
    fake2.forming = False
    (tmp_path / "b").mkdir()
    bot2, fake2, _n2, clock2 = make_fbot(fast_cfg(fcfg), tmp_path / "b", fake=fake2, clock=clock2)
    waits2 = stepping(bot2, clock2)
    bot2.next_scan_at = clock2.t
    bot2.tick()
    row2 = journal(bot2)[-1]
    assert sum(waits2) == pytest.approx(2.0) and row2["kt"] == len(waits2) + 1 and row2["ms"][1] == 3000
    assert row2["s"] == {"SOL": "açıldı"} and sorted(row2["a"]) == ["ADA", "DOGE", "SOL", "XRP"]
    asked2 = [c[1] for c in fake2.calls if c[0] == "klines"]
    assert asked2.count("SOLUSDT") == row2["kt"] and asked2.count("ADAUSDT") == 1 and row2["kq"] == 3   # Yalnız aday bekler
    # 3) Yeni mum ikinci denemede görünür: beklemeden devam
    clock3 = Clock(CLOSE + 1)
    fake3 = FastCcxt(CLOSES, clock=clock3)
    fake3.forming_after = {s: 2 for s in CLOSES}
    (tmp_path / "c").mkdir()
    bot3, fake3, _n3, clock3 = make_fbot(fast_cfg(fcfg), tmp_path / "c", fake=fake3, clock=clock3)
    waits3 = stepping(bot3, clock3)
    bot3.next_scan_at = clock3.t
    bot3.tick()
    assert waits3 == [ft.FAST_SETTLE_WAIT_SEC] and journal(bot3)[-1]["kt"] == 2


def test_quiet_coins_do_not_delay_the_scan(fcfg, tmp_path):
    """Sakin coin kuralı (rapor 18: 103 canlı taramanın 61'i tek tük coinin yeni mumunu 3 sn'ye kadar bekledi): yeni mumu
    görünmeyen coinin kapanışı hiçbir eşiğe yakın değilse beklenmez; tarama ve giriş 1 sn'de yapılır, sonuç aynıdır."""
    clock = Clock(CLOSE + 1)
    fake = FastCcxt(CLOSES, clock=clock)
    fake.forming_after = {"ADAUSDT": 99, "XRPUSDT": 99}          # İki sakin coinde yeni mum hiç görünmüyor
    bot, fake, _notes, clock = make_fbot(fast_cfg(fcfg), tmp_path, fake=fake, clock=clock)
    waits = stepping(bot, clock)
    fake.calls.clear()
    bot.next_scan_at = clock.t
    bot.tick()
    asked = [c[1] for c in fake.calls if c[0] == "klines"]
    assert asked.count("ADAUSDT") == 1 and asked.count("XRPUSDT") == 1 and not waits
    row = journal(bot)[-1]
    assert "kt" not in row and row["kq"] == 2 and row["ms"] == [1000, 1000, 1000] and row["s"] == {"SOL": "açıldı"}
    assert sorted(row["a"]) == ["ADA", "DOGE", "SOL", "XRP"]                 # Sakin coinler de analiz edildi
    # Aday coinin (SOL) kendi yeni mumu görünmüyorsa eskisi gibi beklenir: kesinleşmemiş mumla sinyal verilmez
    clock1 = Clock(CLOSE + 1)
    fake1 = FastCcxt(CLOSES, clock=clock1)
    fake1.forming_after = {"SOLUSDT": 4, "ADAUSDT": 99}
    (tmp_path / "a").mkdir()
    bot1, fake1, _n1, clock1 = make_fbot(fast_cfg(fcfg), tmp_path / "a", fake=fake1, clock=clock1)
    waits1 = stepping(bot1, clock1)
    fake1.calls.clear()
    bot1.next_scan_at = clock1.t
    bot1.tick()
    asked1 = [c[1] for c in fake1.calls if c[0] == "klines"]
    assert asked1.count("SOLUSDT") == 4 and asked1.count("ADAUSDT") == 1 and len(waits1) == 3
    assert journal(bot1)[-1]["ms"][1] == 1750 and journal(bot1)[-1]["kq"] == 1


def test_quiet_rule_thresholds_and_when_it_is_off(fcfg, tmp_path):
    bot, _fake, _notes, _clock = make_fbot(fast_cfg(fcfg), tmp_path)
    rules = bot._quiet_rules()
    assert rules == ((12, 6.0), (48, 10.0))                                  # 1 sa %6 (ek kural), 4 sa %10 (ana kural)
    k = lambda closes: {"c": closes}                                         # noqa: E731
    assert bot._near_decision(k(flat()), rules) is False
    assert bot._near_decision(k(crashing(drop=4.9)), rules) is False        # Eşiğe 1.1 puan uzak
    assert bot._near_decision(k(crashing(drop=5.1)), rules) is True         # Eşiğe 0.9 puan uzak: bekler
    assert bot._near_decision(k(crashing(drop=8.0)), rules) is True         # Eşiğin üstünde (aday): bekler
    assert bot._near_decision(k(flat(n=30)), rules) is True                 # Yetersiz geçmiş: temkinli
    # Dalga eşikleri (1 sa %5) daha düşük eşik getirir; genişlik ölçülüyorsa %3'e 0.5 puan yakın coin de bekler
    wave = fast_cfg(fcfg, futures_wave_rules="1:5", futures_max_positions=2, futures_extra_breadth=3)
    (tmp_path / "w").mkdir()
    wbot, _f, _n, _c = make_fbot(wave, tmp_path / "w")
    wrules = wbot._quiet_rules()
    assert wrules == ((12, 5.0), (48, 10.0))
    assert wbot._near_decision(k(crashing(drop=4.1)), wrules) is True       # 5 - 1 = 4'ün üstünde
    assert wbot._near_decision(k(crashing(drop=3.3)), wrules) is True       # Genişlik eşiğine (%3) 0.3 puan
    assert wbot._near_decision(k(crashing(drop=2.6)), wrules) is True
    assert wbot._near_decision(k(crashing(drop=3.8)), wrules) is False      # %3'ü açık farkla geçmiş, eşiğe uzak
    assert wbot._near_decision(k(crashing(drop=2.0)), wrules) is False
    assert bot._near_decision(k(crashing(drop=3.3)), rules) is False        # Genişlik ölçülmüyorsa bu şart yok
    # Sinyal yalnızca kapanışlara bağlı değilse kural kapalı: her coin yeni mumunu bekler
    for over in (dict(futures_crash_liq=True), dict(futures_crash_z=2.0), dict(futures_pump_rules="1:8")):
        strict = fast_cfg(fcfg, **over)
        (tmp_path / str(len(over) + sum(map(ord, next(iter(over)))))).mkdir()
        sbot, _f2, _n2, _c2 = make_fbot(strict, tmp_path / str(len(over) + sum(map(ord, next(iter(over))))))
        assert sbot._quiet_rules() is None, over
    clock = Clock(CLOSE + 1)
    fake = FastCcxt(CLOSES, clock=clock)
    fake.forming_after = {"ADAUSDT": 99}
    (tmp_path / "s").mkdir()
    sbot, fake, _n3, clock = make_fbot(fast_cfg(fcfg, futures_crash_z=2.0), tmp_path / "s", fake=fake, clock=clock)
    stepping(sbot, clock)
    fake.calls.clear()
    sbot.next_scan_at = clock.t
    sbot.tick()
    assert [c[1] for c in fake.calls if c[0] == "klines"].count("ADAUSDT") == 9 and "kq" not in journal(sbot)[-1]


def test_old_path_asks_each_coin_once_without_new_candle_check(fcfg, tmp_path):
    clock = Clock(CLOSE + 3)
    fake = FastCcxt(CLOSES, clock=clock)
    fake.forming = False
    bot, fake, _notes, clock = make_fbot(live_cfg(fcfg), tmp_path, fake=fake, clock=clock)
    waits = stepping(bot, clock)
    fake.calls.clear()
    bot.next_scan_at = clock.t
    bot.tick()
    assert sorted(c[1] for c in fake.calls if c[0] == "klines") == ["ADAUSDT", "DOGEUSDT", "SOLUSDT", "XRPUSDT"]
    assert not waits and all(seen is True for seen in fake.throttle_seen)      # CCXT beklemesi eskisi gibi açık
    assert "kt" not in journal(bot)[-1] and set(bot.state.positions) == {"SOLUSDT"}
    assert not [c for c in fake.calls if c[0] in ("symbol_config", "time")]


# ====================================================================== teminat modu / kaldıraç: önceden ayarla + doğrula
def test_prepared_symbol_is_trusted_only_right_after_verification(fcfg, tmp_path):
    clock = Clock(CLOSE - 2.5)
    fake = FastCcxt(CLOSES, clock=clock)
    fake.settings = {"SOLUSDT": ("ISOLATED", 3), "ADAUSDT": ("ISOLATED", 10), "XRPUSDT": ("CROSSED", 3)}
    bot, fake, _notes, clock = make_fbot(fast_cfg(fcfg), tmp_path, live=True, fake=fake, clock=clock)
    assert bot.next_scan_at == CLOSE + 1
    clock.t = bot.next_scan_at - ft.FAST_WARM_LEAD_SEC
    bot.tick()                                                   # Ön hazırlık: doğrulama
    assert bot._prepared == {"SOLUSDT"} and bot._prepare_todo == 3            # ADA (kaldıraç), XRP (çapraz), DOGE (yok)

    def opened_with_prepare(symbol, at):
        fake.calls.clear()
        clock.t = at
        bot.open_futures(FuturesSignal(symbol, "long", 100.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0}), at)
        bot.close_position(bot.state.positions[symbol], ft.ExitReason.TAKE_PROFIT)
        return [c[0] for c in fake.calls if c[0] in ("set_margin_mode", "set_leverage")]

    assert opened_with_prepare("SOLUSDT", CLOSE + 1) == []                    # Doğrulandı: yeniden ayarlanmaz
    assert opened_with_prepare("ADAUSDT", CLOSE + 1) == ["set_margin_mode", "set_leverage"]   # Kaldıraç farklı
    assert opened_with_prepare("XRPUSDT", CLOSE + 1) == ["set_margin_mode", "set_leverage"]   # Çapraz teminat
    # Doğrulama eskidiyse (ön hazırlık bu tarama için yapılamadıysa) eski yol
    assert opened_with_prepare("SOLUSDT", CLOSE + 1 + ft.FAST_VERIFY_MAX_AGE_SEC + 5) == ["set_margin_mode",
                                                                                         "set_leverage"]
    # Kaldıraç ayarı değişince eski doğrulama geçersiz
    bot.apply_config(replace(bot.cfg, futures_leverage=2))
    assert opened_with_prepare("SOLUSDT", CLOSE + 1) == ["set_margin_mode", "set_leverage"]
    # Hızlı giriş kapalıyken her girişte ayarlanır (eski davranış)
    bot.apply_config(replace(bot.cfg, futures_fast_entry=False, futures_leverage=3))
    bot._prepared, bot._prepared_lev, bot._verified_at = {"SOLUSDT"}, 3, CLOSE + 1
    assert opened_with_prepare("SOLUSDT", CLOSE + 1) == ["set_margin_mode", "set_leverage"]


def test_verification_failure_falls_back_to_prepare_at_entry(fcfg, tmp_path):
    clock = Clock(CLOSE - 2.5)
    fake = FastCcxt(CLOSES, clock=clock)
    fake.settings = {s: ("ISOLATED", 3) for s in CLOSES}
    fake.settings_error = 'binance {"code":-1003,"msg":"Too many requests"}'
    bot, fake, _notes, clock = make_fbot(fast_cfg(fcfg), tmp_path, live=True, fake=fake, clock=clock)
    clock.t = bot.next_scan_at - ft.FAST_WARM_LEAD_SEC
    bot.tick()
    assert bot._prepared == set() and bot._verified_at == 0.0 and bot._prepare_todo == 0
    fake.calls.clear()
    clock.t = bot.next_scan_at
    bot.tick()
    names = [c[0] for c in fake.calls]
    assert names.index("set_margin_mode") < names.index("set_leverage") < names.index("create_order")
    assert set(bot.state.positions) == {"SOLUSDT"}


def test_prepare_ahead_is_incremental_skips_open_positions_and_backs_off(fcfg, tmp_path):
    clock = Clock(CLOSE - 200)
    fake = FastCcxt(CLOSES, clock=clock)
    bot, fake, _notes, clock = make_fbot(fast_cfg(fcfg), tmp_path, live=True, fake=fake, clock=clock)
    fake.margin_error = 'binance {"code":-4047,"msg":"Margin type cannot be changed if there exists open orders."}'
    fake.calls.clear()
    bot.tick()
    tried = [c[2] for c in fake.calls if c[0] == "set_margin_mode"]
    assert len(tried) == ft.FAST_PREPARE_BATCH and bot._prepared == set()      # Adım başına en fazla 2 coin
    assert bot._next_sleep() == ft.FAST_PREPARE_POLL_SEC                      # Kalanlar için döngü hızlı döner
    clock.t += 1
    bot.tick()
    assert len([c for c in fake.calls if c[0] == "set_margin_mode"]) == 4 and bot._prepare_todo == 0
    clock.t += 1
    bot.tick()                                                   # Ayarlanamayanlar bir saat denenmez
    assert len([c for c in fake.calls if c[0] == "set_margin_mode"]) == 4
    assert bot._next_sleep() == 30.0
    # Taramaya az kala (20 sn) ön hazırlık ve parite yenilemesi yapılmaz
    fake.margin_error = None
    bot._prepare_retry.clear()
    bot._verified_at = 0.0
    clock.t = bot.next_scan_at - 10
    fake.calls.clear()
    bot.exchange.markets_loaded_at = 0.0
    bot.tick()
    assert not [c for c in fake.calls if c[0] in ("set_margin_mode", "symbol_config", "load_markets")]
    # Test modunda hesap ayarı yok: önceden ayarlama / doğrulama isteği atılmaz
    (tmp_path / "p").mkdir()
    pclock = Clock(CLOSE - 200)
    paper, pfake, _n, pclock = make_fbot(fast_cfg(fcfg), tmp_path / "p", fake=FastCcxt(CLOSES, clock=pclock),
                                        clock=pclock)
    pfake.calls.clear()
    run_until_scan(paper, pclock, CLOSE - 200)
    names = [c[0] for c in pfake.calls]
    assert "symbol_config" not in names and "set_leverage" not in names and names.count("time") == 4
    assert set(paper.state.positions) == {"SOLUSDT"} and journal(paper)[-1]["ms"][0] == 1000


def test_self_prepared_symbol_that_binance_shows_different_is_not_retried_forever(fcfg, tmp_path):
    clock = Clock(CLOSE - 200)
    fake = FastCcxt(CLOSES, clock=clock)
    bot, fake, _notes, clock = make_fbot(fast_cfg(fcfg), tmp_path, live=True, fake=fake, clock=clock)
    bot.tick()
    clock.t += 1
    bot.tick()
    assert bot._prepared == {"SOLUSDT", "ADAUSDT", "XRPUSDT", "DOGEUSDT"}
    fake.settings["ADAUSDT"] = ("ISOLATED", 1)                    # Binance kaldıracı farklı gösteriyor
    bot._verify_prepared(clock.t + 2)
    assert "ADAUSDT" not in bot._prepared and bot._prepare_retry["ADAUSDT"] > clock.t and bot._prepare_todo == 0


# ====================================================================== zamanlama
def test_fast_loop_wakes_exactly_at_warm_and_scan_time(fcfg, tmp_path):
    clock = Clock(CLOSE - 100)
    bot, fake, _notes, clock = make_fbot(fast_cfg(fcfg), tmp_path, fake=FastCcxt(CLOSES, clock=clock), clock=clock)
    assert bot._next_sleep() == 30.0
    clock.t = CLOSE - 10
    assert bot._next_sleep() == pytest.approx(10 + 1 - ft.FAST_WARM_LEAD_SEC)   # Önce ön hazırlık anına
    clock.t = bot.next_scan_at - ft.FAST_WARM_LEAD_SEC
    bot.tick()
    assert bot._warm_for == bot.next_scan_at and [c for c in fake.calls if c == ("time",)]
    clock.t += 0.4
    assert bot._next_sleep() == pytest.approx(ft.FAST_WARM_LEAD_SEC - 0.4)     # Sonra tam tarama anına
    clock.t = bot.next_scan_at + 0.3
    assert bot._next_sleep() == ft.FAST_MIN_SLEEP_SEC
    # Açık pozisyon varken de tarama anı kaçmaz (eskiden 2 sn'lik uykunun denk gelişine bağlıydı)
    bot.open_futures(FuturesSignal("ADAUSDT", "long", 100.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0}), clock.t)
    bot._warm_for = bot.next_scan_at
    clock.t = bot.next_scan_at - 0.7
    assert bot._next_sleep() == pytest.approx(0.7)
    clock.t = bot.next_scan_at - 30
    assert bot._next_sleep() == ft.POSITION_POLL_SEC
    # Ön hazırlık anı kaçtıysa (taramaya 1 sn'den az kaldı) atlanır: taramayı geciktirmez
    bot.close_position(bot.state.positions["ADAUSDT"], ft.ExitReason.TAKE_PROFIT)
    bot._warm_for = 0.0
    fake.calls.clear()
    clock.t = bot.next_scan_at - 0.5
    bot.tick()
    assert bot._warm_for == bot.next_scan_at and not [c for c in fake.calls if c == ("time",)]
    # Kapalıyken eski uyku süreleri
    bot.apply_config(replace(bot.cfg, futures_fast_entry=False))
    clock.t = bot.next_scan_at - 0.4
    assert bot._next_sleep() == 1.0


def test_fast_skips_24h_prefilter_only_for_small_universe(fcfg, tmp_path, monkeypatch):
    ucfg = replace(fcfg, futures_universe=3, futures_universe_with_list=False)
    bot, fake, _notes, clock = make_fbot(ucfg, tmp_path, fake=UniCcxt(TICKERS, META))
    fake.calls.clear()
    assert bot._scan_symbols() == (["ETHUSDT"], 2) and ("ticker24hr",) in fake.calls      # Kapalı: ön eleme var
    bot.apply_config(replace(bot.cfg, futures_fast_entry=True))
    fake.calls.clear()
    assert bot._scan_symbols() == (["BTCUSDT", "ETHUSDT", "SOLUSDT"], 0) and ("ticker24hr",) not in fake.calls
    monkeypatch.setattr(ft, "FAST_PREFILTER_MIN", 2)             # Büyük evren: ön eleme sürer
    assert bot._scan_symbols() == (["ETHUSDT"], 2) and ("ticker24hr",) in fake.calls


def test_clock_offset_warning_is_logged_once_an_hour(fcfg, tmp_path, caplog):
    clock = Clock(CLOSE - 2.5)
    fake = FastCcxt(CLOSES, clock=clock)
    fake.fapiPublicGetTime = lambda params=None: {"serverTime": str(int(clock() * 1000) - 2000)}
    bot, fake, _notes, clock = make_fbot(fast_cfg(fcfg), tmp_path, fake=fake, clock=clock)
    caplog.set_level("WARNING")
    bot._pre_scan(clock.t)
    bot._pre_scan(clock.t + 300)
    assert bot._clock_offset_ms == pytest.approx(-2000.0)
    assert sum("Sunucu saati Binance'in 2000 ms ilerisinde" in r.getMessage() for r in caplog.records) == 1


# ====================================================================== backtest raporu
def test_report_shows_measured_scan_timing_and_entry_lag(fcfg, tmp_path):
    t0 = BASE_MS
    path = tmp_path / "futures_scans_paper.jsonl"
    rows = [{"t": t0, "u": ["BTC", "ETH"], "a": ["ETH"], "ms": [3400, 4900, 5000], "s": {"ETH": "açıldı"},
             "f": {"ETH": {"o": 5600, "r": 5900, "x": 5800, "p": 0.06}}},
            {"t": t0 + FUT_BAR_MS, "a": ["ETH"], "ms": [3200, 4700, 4800]},
            {"t": t0 + 2 * FUT_BAR_MS, "a": ["BTC"], "ms": [1000, 1400, 1450], "fast": 1, "co": -40, "kt": 2,
             "s": {"BTC": "açıldı"}, "f": {"BTC": {"o": 1700, "r": 1950, "p": 0.02}}},
            {"t": t0 + 3 * FUT_BAR_MS, "skip": "boş yer yok (1/1 pozisyon açık)"}]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    data = fb.FutData(t0, t0 + 6 * FUT_BAR_MS, [fb.Segment(t0 - 1, t0 + 6 * FUT_BAR_MS, ("BTC", "ETH"))], {},
                      ((), (), ()), (None, None), {}, None, "5m")
    text = "\n".join(fb.live_comparison(fcfg, data, [], path=path))
    assert ("Tarama zamanlaması (hızlı giriş kapalı: mum kapanışından sonra, ortanca): başlangıç 3.3 sn → mumlar "
            "alındı 4.8 sn → sinyaller hazır 4.9 sn (2 tarama)") in text
    assert ("Giriş gecikmesi (hızlı giriş kapalı: mum kapanışı → dolum): ortalama 5.8 sn, ortanca 5.8 sn, en uzun "
            "5.8 sn (1 giriş) | dolum, kapanış fiyatına göre ortalama +0.060% (backtest varsayımı +%0.07") in text
    assert ("Tarama zamanlaması (hızlı giriş açık: mum kapanışından sonra, ortanca): başlangıç 1.0 sn → mumlar "
            "alındı 1.4 sn → sinyaller hazır 1.4 sn (1 tarama; 1 taramada mumlar yeniden istendi; Binance ile saat "
            "farkı ortanca 40 ms)") in text
    assert "Giriş gecikmesi (hızlı giriş açık: mum kapanışı → dolum): ortalama 1.9 sn" in text and "+0.020%" in text
    assert text.index("Tarama zamanlaması") < text.index("Bu aralıkta backtestte işlem yok")
    # Ölçümsüz (eski) kayıt: satır eklenmez
    path.write_text(json.dumps({"t": t0, "u": ["BTC"], "a": ["BTC"]}) + "\n", encoding="utf-8")
    assert "zamanlaması" not in "\n".join(fb.live_comparison(fcfg, data, [], path=path))
    assert fb.live_timing(fcfg, [{"t": t0, "ms": [3000, 3100, 3100]}])[0].startswith(
        "  Tarama zamanlaması (mum kapanışından sonra, ortanca): başlangıç 3.0 sn")
    quiet = fb.live_timing(fcfg, [{"t": t0, "ms": [1000, 1500, 1550], "fast": 1, "kq": 14},
                                  {"t": t0 + FUT_BAR_MS, "ms": [1000, 3600, 3650], "fast": 1, "kt": 9, "kq": 12}])[0]
    assert "(2 tarama; 1 taramada mumlar yeniden istendi; 2 taramada sakin coinlerin yeni mumu beklenmedi)" in quiet


# ====================================================================== gerçek CCXT nesnesi (ağ yok: istekler sahte)
def test_real_ccxt_endpoints_pool_and_pacing(fcfg):
    """Kullanılan Binance uçları kurulu CCXT sürümünde var; bağlantı havuzu büyütülür; CCXT'nin istekler arası
    beklemesi (mum isteği 100 ms, 24 saatlik özet 2 sn) `unthrottled` bloğunda uygulanmaz."""
    ccxt = pytest.importorskip("ccxt")
    import futures_exchange as fx
    raw = ccxt.binance({"enableRateLimit": True, "apiKey": "x", "secret": "y",
                        "options": {"defaultType": "future"}})
    seen = []

    def fetch(url, method="GET", headers=None, body=None):
        seen.append(url)
        if "symbolConfig" in url:
            return [{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": "5"},
                    {"symbol": "ETHUSDT", "marginType": "CROSSED", "leverage": 20}]
        if url.endswith("/time"):
            return {"serverTime": int(time.time() * 1000) + 250}
        return []
    raw.fetch = fetch
    ex = FuturesExchange(replace(fcfg, trading_mode="live"), exchange=raw)
    assert raw.session.adapters["https://"]._pool_maxsize == fx.POOL_SIZE
    assert ex.symbol_settings() == {"BTCUSDT": (True, 5), "ETHUSDT": (False, 20)}
    assert "/fapi/v1/symbolConfig" in seen[-1] and "signature=" in seen[-1]
    offset = ex.warm(6)
    assert offset == pytest.approx(250, abs=150) and sum(u.endswith("/fapi/v1/time") for u in seen) == 6
    t0 = time.monotonic()
    ex.klines("BTCUSDT", "5m", 300)
    ex.klines("ETHUSDT", "5m", 300)                              # CCXT: önceki istekten 100 ms sonra
    paced = time.monotonic() - t0
    with ex.unthrottled():
        t0 = time.monotonic()
        for _ in range(10):
            ex.klines_state("BTCUSDT", "5m", 300)
        free = time.monotonic() - t0
    assert paced >= 0.09 and free < 0.09 and raw.enableRateLimit is True


def test_measurement_failure_never_blocks_the_entry_or_its_stop(fcfg, tmp_path):
    """Emir dolduktan sonra ölçümde hata olsa bile pozisyon kaydedilir ve zarar kes Binance'e konur."""
    bot, fake, _notes, clock = make_fbot(live_cfg(fcfg), tmp_path, live=True, fake=FastCcxt(CLOSES))

    def boom(*a, **k):
        raise RuntimeError("ölçüm bozuk")
    bot._note_entry_lag = boom
    bot.next_scan_at = clock.t
    bot.tick()
    pos = bot.state.positions["SOLUSDT"]
    assert pos.stop_order_id and "lag_ms" not in pos.metrics
    row = journal(bot)[-1]
    assert row["s"] == {"SOL": "açıldı"} and "f" not in row and len(row["ms"]) == 3
    assert _order_ts("bozuk") == 0
    bot._journal({"t": 1, "x": {1, 2}})                          # Yazılamayan kayıt botu durdurmaz
