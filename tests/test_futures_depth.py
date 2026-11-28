"""Emir defteri derinliği (likidite araştırması, Kasım 2026): bookDepth dosyalarının okunması ve önbelleği, fiyat kayması
hesabı, derinlik deposu (ölçülen / hacimden tahmin), backtest motorunda derinliğe göre boy + kapasite önceliği + kayma,
rapor satırları, canlı botta girişten önce emir defteri ve Telegram ayarları. Sahte borsa ve verilerle; gerçek emir yok."""
import io
import math
import zipfile
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, load_config
from futures_data import FuturesArchive, parse_book_depth
from futures_depth import (DEPTH_CHECK_MIN_NOTIONAL, IMPACT_MAX_PCT, DepthBook, daily_volume_fn, depth_within,
                           impact_pct)
from runtime_settings import EDITABLE
from strategy import FUT_BAR_MS, FuturesSignal
from test_futures import BASE_MS, FakeCcxt, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, crashing, flat, live_cfg, press
from test_settings_menu import buttons, env_text, fut  # noqa: F401 - pytest fikstürü

MIN = fb.MIN_MS
DAY = fb.DAY_MS
D24 = fb._ms("2024-03-05")


# ====================================================================== dosya ve hesap
CSV = ("timestamp,percentage,depth,notional\n"
       + "".join(f"2024-03-05 00:00:07,{p},{abs(p)},{abs(p) * 1000.0}\n" for p in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5))
       + "".join(f"2024-03-05 00:00:37,{p},{abs(p)},{abs(p) * 2000.0}\n" for p in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5))
       + "2024-03-05 00:01:07,1,1,500.0\n")                       # Eksik seviyeli görüntü atılır


def test_parse_book_depth_groups_snapshots_and_drops_incomplete_ones():
    d = parse_book_depth(CSV.encode())
    assert list(d["t"]) == [D24 + 7_000, D24 + 37_000]
    assert d["ask"].shape == (2, 5) and list(d["ask"][0]) == [1000, 2000, 3000, 4000, 5000]
    assert list(d["bid"][1]) == [2000, 4000, 6000, 8000, 10000]
    numeric = parse_book_depth("".join(f"{D24},{p},1,{abs(p)}\n" for p in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5)).encode())
    assert list(numeric["t"]) == [D24] and list(numeric["ask"][0]) == [1, 2, 3, 4, 5]
    assert len(parse_book_depth(b"timestamp,percentage,depth,notional\n")["t"]) == 0


def test_impact_walks_the_book_and_is_bounded():
    cum = [1000, 2000, 3000, 4000, 5000]
    assert impact_pct(0, cum) == 0.0
    assert impact_pct(500, cum) == pytest.approx(0.25)                 # İlk %1 içinde düzgün dağılım: ortalama yarısı
    assert impact_pct(1500, cum) == pytest.approx((1000 * 0.5 + 500 * 1.25) / 1500)
    assert impact_pct(6000, cum) == pytest.approx((1000 * (0.5 + 1.5 + 2.5 + 3.5 + 4.5) + 1000 * 5) / 6000)
    assert impact_pct(1e9, cum) == pytest.approx(IMPACT_MAX_PCT, rel=1e-3)
    assert impact_pct(1000, [5, 5, 5, 5, 5]) == pytest.approx(IMPACT_MAX_PCT, rel=0.01)   # Boş defter: %5'ten dolar


def test_depth_within_sums_both_sides_inside_one_percent():
    book = {"bids": [[99.9, 10], [99.5, 10], [98.0, 100]], "asks": [[100.1, 5], [100.9, 5], [102.0, 100]]}
    bid, ask, ask_n, bid_n = depth_within(book, 1.0)
    assert (bid, ask) == (99.9, 100.1)
    assert ask_n == pytest.approx(100.1 * 5 + 100.9 * 5) and bid_n == pytest.approx(99.9 * 10 + 99.5 * 10)
    assert depth_within({"bids": [], "asks": [[1, 1]]}) == (0.0, 0.0, 0.0, 0.0)


# ====================================================================== depo ve arşiv
def snaps(day_ms, values, step=30_000, start=0):
    """Gün içinde `start` ms'den itibaren her 30 sn'de bir görüntü: values[i] = (%1 satış, %1 alış)."""
    t = np.array([day_ms + start + i * step for i in range(len(values))], dtype=np.int64)
    ask = np.array([[a * k for k in (1, 2, 3, 4, 5)] for a, _b in values], dtype=np.float32)
    bid = np.array([[b * k for k in (1, 2, 3, 4, 5)] for _a, b in values], dtype=np.float32)
    return {"t": t, "ask": ask, "bid": bid}


def test_depth_book_uses_measured_snapshots_causally_and_falls_back_to_volume():
    day = D24 // DAY * DAY
    files = {("DOTUSDT", day): snaps(day, [(100_000, 50_000)] * 4 + [(1_000, 1_000)] * 4, start=3_600_000)}
    calls = []

    def loader(sym, dt):
        calls.append((sym, dt))
        return files.get((sym, int(dt.timestamp() * 1000)), {"t": np.zeros(0, dtype=np.int64),
                                                              "ask": np.zeros((0, 5)), "bid": np.zeros((0, 5))})
    book = DepthBook(loader, lambda sym, ms: 1e9)
    ms = day + 3_600_000 + 3 * 30_000 + 1_000                         # 4. görüntüden 1 sn sonra (derinlik düşmeden önce)
    assert book.cap_depth("DOTUSDT", ms) == pytest.approx(50_000)     # Karar: yalnızca önceki 2 dk (alış tarafı küçük)
    assert book.profile("DOTUSDT", ms, "ask")[0] == pytest.approx(np.median([100_000] * 4 + [1_000] * 4))  # ±2 dk
    assert book.measured == 3 and book.proxied == 0
    # Veri yok: 2023 öncesi ya da dosya yok → 7 günlük hacimden tahmin (2023 oranı, ortanca şekil)
    old = fb._ms("2021-05-19")
    assert book.profile("DOTUSDT", old, "ask")[0] == pytest.approx(0.0018 * 1e9)
    assert book.profile("DOTUSDT", old, "bid")[2] == pytest.approx(0.0024 * 1e9 * 2.41)
    assert book.proxied == 2 and not any(dt.year < 2023 for _s, dt in calls)       # 2023 öncesi dosya istenmez
    assert DepthBook(loader, None).cap_depth("DOTUSDT", old) == math.inf            # Ne ölçüm ne tahmin: sınır yok
    assert book.impact("DOTUSDT", ms, "ask", 0) == 0.0
    # Ön yükleme: sinyal günü + çıkış için sonraki saatler (gün değişirse ertesi gün de)
    late = day + DAY - 3_600_000
    loaded = DepthBook(loader).prefetch([("DOTUSDT", late), ("DOTUSDT", old)])
    assert loaded == 2


class FakeSession:
    def __init__(self, files):
        self.files, self.urls = files, []

    def get(self, url, timeout=None):
        self.urls.append(url)
        body = self.files.get(url.rsplit("/", 1)[1])
        return type("R", (), {"status_code": 200 if body is not None else 404, "content": body or b"",
                              "raise_for_status": lambda self: None})()


def zipped(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name.replace(".zip", ".csv"), text)
    return buf.getvalue()


def test_archive_downloads_and_caches_book_depth(tmp_path):
    name = "DOTUSDT-bookDepth-2024-03-05.zip"
    session = FakeSession({name: zipped(name, CSV)})
    archive = FuturesArchive(tmp_path, "https://example.test", session=session, now_fn=lambda: D24 / 1000 + 30 * 86400)
    day = datetime(2024, 3, 5, tzinfo=timezone.utc)
    got = archive.book_depth("DOTUSDT", day)
    assert len(got["t"]) == 2 and session.urls[-1].endswith("/data/futures/um/daily/bookDepth/DOTUSDT/" + name)
    again = archive.book_depth("DOTUSDT", day)                         # Önbellekten (istek yok)
    assert len(session.urls) == 1 and list(again["ask"][0]) == list(got["ask"][0])
    missing = archive.book_depth("DOTUSDT", datetime(2024, 3, 6, tzinfo=timezone.utc))
    assert len(missing["t"]) == 0
    archive.book_depth("DOTUSDT", datetime(2024, 3, 6, tzinfo=timezone.utc))
    assert len(session.urls) == 2                                      # Yayımlanmış olması gereken eksik gün: bir daha sorulmaz


def test_daily_volume_proxy_averages_the_previous_seven_days():
    class Arch:
        def klines(self, sym, a, b, interval):
            assert interval == "1d"
            t = np.arange(a, b, DAY, dtype=np.int64)
            return {"t": t, "qv": (t // DAY % 10 + 1).astype(float) * 1e6}
    adv = daily_volume_fn(Arch())
    ms = fb._ms("2024-03-15") + 5 * 3_600_000
    days = range(ms // DAY - 7, ms // DAY)
    assert adv("DOTUSDT", ms) == pytest.approx(np.mean([(d % 10 + 1) * 1e6 for d in days]))


# ====================================================================== backtest motoru
class FakeDepth:
    """DepthBook yerine: coin başına sabit sınır derinliği ve kayma (%)."""

    def __init__(self, caps, impact=0.0):
        self.caps, self.imp, self.calls = caps, impact, []

    def cap_depth(self, sym, ms):
        self.calls.append(sym)
        return self.caps.get(sym, math.inf)

    def impact(self, sym, ms, side, q):
        return self.imp

    def prefetch(self, keys, workers=8, progress=None):
        return 0


def bt_data(depth=None, hours=10):
    d = fb.FutData(BASE_MS, BASE_MS + hours * 3_600_000, [], {}, None, (np.zeros(0), np.zeros(0)), {}, None, "1m")
    d.depth = depth
    return d


def bt_sig(symbol, close_ms, score=1.0, breadth=9):
    s = fb.FutSignal(close_ms, symbol, "long", score, 100.0)
    s.breadth = breadth
    return s


WIN = [(100, 103.5, 99.9, 103.0)]           # Kâr Al (%3)


def test_engine_caps_position_by_depth_and_charges_impact():
    s1 = bt_sig("AAAUSDT", BASE_MS)
    w = {("AAAUSDT", BASE_MS): window(WIN, start=BASE_MS)}
    p = params(tp_pct=3.0, sl_pct=4.0, be_enabled=False, start_capital=1_000.0, depth_cap_pct=10.0)   # İstenen 10.000
    capped = fb.simulate(p, bt_data(FakeDepth({"AAAUSDT": 40_000})), [s1], w).trades[0]
    assert capped.notional == pytest.approx(4_000) and capped.depth_capped and capped.pnl == pytest.approx(120.0)
    roomy = fb.simulate(p, bt_data(FakeDepth({"AAAUSDT": 1e9})), [s1], w).trades[0]
    assert roomy.notional == pytest.approx(10_000) and not roomy.depth_capped
    small = fb.simulate(replace(p, start_capital=50.0), bt_data(FakeDepth({"AAAUSDT": 1.0})), [s1], w).trades[0]
    assert small.notional == pytest.approx(500) and not small.depth_capped          # 1.000 USDT altında bakılmaz
    assert fb.simulate(p, bt_data(None), [s1], w).trades[0].notional == pytest.approx(10_000)    # Veri yok: sınır yok
    # Kayma: giriş + çıkış, kârdan düşülür; zarar teminatı aşamaz
    imp = fb.simulate(replace(p, depth_cap_pct=0.0, depth_impact=True), bt_data(FakeDepth({}, impact=0.5)), [s1], w)
    tr = imp.trades[0]
    assert tr.impact_pct == pytest.approx(1.0) and tr.pnl == pytest.approx(300.0 - 100.0)
    assert imp.final_equity == pytest.approx(1_200.0)
    huge = fb.simulate(replace(p, depth_cap_pct=0.0, depth_impact=True), bt_data(FakeDepth({}, impact=60.0)), [s1], w)
    assert huge.trades[0].pnl == pytest.approx(-1_000.0)                            # -teminat (likidasyon)
    off = fb.simulate(replace(p, depth_cap_pct=0.0, depth_impact=False), bt_data(FakeDepth({}, impact=0.5)), [s1], w)
    assert off.trades[0].pnl == pytest.approx(300.0) and off.trades[0].impact_pct == 0.0


def test_capacity_priority_picks_the_deeper_coin_only_when_capped():
    a, b = bt_sig("THINUSDT", BASE_MS, score=2.0), bt_sig("DEEPUSDT", BASE_MS, score=1.0)
    w = {(s.symbol, BASE_MS): window(WIN, start=BASE_MS) for s in (a, b)}
    p = params(tp_pct=3.0, sl_pct=4.0, be_enabled=False, start_capital=1_000.0, depth_cap_pct=10.0)
    depth = FakeDepth({"THINUSDT": 20_000, "DEEPUSDT": 1e9})                # İnce: 2.000 sınır < 10.000 istenen
    one = fb.simulate(p, bt_data(depth), [a, b], w)
    assert [t.symbol for t in one.trades] == ["DEEPUSDT"] and one.skipped["açık pozisyon vardı"] == 1
    roomy = fb.simulate(p, bt_data(FakeDepth({"THINUSDT": 1e9, "DEEPUSDT": 1e9})), [a, b], w)
    assert [t.symbol for t in roomy.trades] == ["THINUSDT"]                 # Takılmıyorsa puan sırası
    tiny = fb.simulate(replace(p, start_capital=50.0), bt_data(FakeDepth({"THINUSDT": 1.0, "DEEPUSDT": 1e9})), [a, b], w)
    assert [t.symbol for t in tiny.trades] == ["THINUSDT"]                  # 1.000 USDT altında defter okunmaz
    off = fb.simulate(replace(p, depth_cap_pct=0.0), bt_data(depth), [a, b], w)
    assert [t.symbol for t in off.trades] == ["THINUSDT"]
    # Dalga modu: ilk yuva derin coine, ikinci yuva (istenen tutar küçüldü) ince coine
    slots = fb.simulate(replace(p, max_positions=2, extra_breadth=1), bt_data(depth), [a, b], w)
    assert [t.symbol for t in slots.trades] == ["DEEPUSDT", "THINUSDT"]
    assert slots.trades[1].notional == pytest.approx(2_000) and slots.trades[1].depth_capped


def test_params_report_lines_variant_rows_and_attach(fcfg, monkeypatch, tmp_path):
    on = replace(fcfg, futures_depth_cap_pct=10.0, futures_backtest_depth=True)
    p = fb.params_from_cfg(on)
    assert (p.depth_cap_pct, p.depth_impact) == (10.0, True)
    assert fb.params_from_cfg(replace(fcfg, futures_backtest_depth=False)).depth_impact is False
    s1 = bt_sig("AAAUSDT", BASE_MS)
    w = {("AAAUSDT", BASE_MS): window(WIN, start=BASE_MS)}
    res = fb.simulate(replace(params(tp_pct=3.0, sl_pct=4.0, be_enabled=False, start_capital=1_000.0),
                              depth_cap_pct=10.0, depth_impact=True), bt_data(FakeDepth({"AAAUSDT": 40_000}, 0.5)),
                      [s1], w)
    lines = fb.depth_lines(res.params, bt_data(FakeDepth({})), res)
    assert lines[0].startswith("📏 Derinliğe göre boy: pozisyon, girişten önceki 2 dk'da fiyatın ±%1 içindeki")
    assert lines[0].endswith("A'da 1 / 1 işlem sınıra takıldı")
    assert lines[1].startswith("📚 Emir defteri kayması:") and lines[1].endswith("A'da toplam kayma 40 USDT")
    warn = fb.depth_lines(replace(res.params, depth_impact=False, depth_cap_pct=0.0), bt_data(None), res)
    assert warn and warn[0].startswith("⚠️ Kayma pozisyon büyüklüğünden bağımsız sayıldı (FUTURES_BACKTEST_DEPTH=false)")
    assert fb.depth_lines(replace(res.params, depth_cap_pct=0.0), bt_data(None), res) == []
    # Kaldıraç tablosu dipnotu: kayma açıkken tavan değil kayma anlatılır
    from test_futures_report_sections import small_inputs
    on2, data2, sigs2, funnel2, windows2 = small_inputs(fcfg)
    ev2 = fb.evaluate(fb.params_from_cfg(on2), data2, sigs2, windows2)
    data2.depth = FakeDepth({})
    text = fb.format_report(on2, data2, ev2, funnel2, levs=[(1, ev2, "")])
    assert "emir defteri kayması ve derinliğe göre boy büyük pozisyonları sınırladığı için" in text
    assert "1.000.000 USDT" not in text
    data2.depth = None
    assert "pozisyon tutarı en fazla 1.000.000 USDT kabul edildiği için" in fb.format_report(on2, data2, ev2, funnel2,
                                                                                          levs=[(1, ev2, "")])
    # Tek kural satırları: derinlik verisi varsa
    monkeypatch.setattr(fb, "evaluate", lambda p, *a, **k: p)
    got = dict(fb.size_variant_rows(on, lambda **o: fb.params_from_cfg(on, **o), depth=True))
    assert got["Derinliğe göre boy kapalı"].depth_cap_pct == 0
    assert got["Derinliğe göre boy %7.5 (şimdi %10)"].depth_cap_pct == 7.5 and "Derinliğe göre boy %10 (şimdi %10)" not in got
    off = dict(fb.size_variant_rows(fcfg, lambda **o: fb.params_from_cfg(fcfg, **o), depth=True))
    assert off["Derinliğe göre boy %10 + kapasite önceliği"].depth_cap_pct == 10
    assert not any("Derinliğe" in n for n in dict(fb.size_variant_rows(fcfg, lambda **o: o)))
    cost = dict(fb.cost_variant_rows(on, lambda **o: fb.params_from_cfg(on, **o), depth=True))
    assert not any("kayması yok" in n for n in cost)                       # Gerçek dışı kıyas satırı artık yok
    # Derinlik deposu: ayar açıksa ve arşiv varsa kurulur
    data = bt_data(None)
    assert fb.attach_depth(on, data, [s1]) is None                          # Arşiv yok (bellekteki veri)
    data.archive = FuturesArchive(tmp_path, "https://example.test", session=FakeSession({}))
    assert fb.attach_depth(replace(fcfg, futures_backtest_depth=False), data, [s1]) is None
    book = fb.attach_depth(on, data, [s1])
    assert isinstance(book, DepthBook) and data.depth is book


# ====================================================================== canlı bot
class DepthCcxt(KlineCcxt):
    """Coin başına emir defteri: ±%1 içinde `depth` USDT (alış ve satış), defter istekleri sayılır."""

    def __init__(self, closes, depth, **kw):
        super().__init__(closes, **kw)
        self.depth, self.book_calls = depth, []

    def fetch_order_book(self, sym, limit=5):
        p = self.prices[sym.split("/")[0] + "USDT"]
        self.book_calls.append((sym, limit))
        d = self.depth.get(sym.split("/")[0] + "USDT", 1e9)
        return {"bids": [[p * 0.999, d / (p * 0.999)], [p * 0.95, 1e9]],
                "asks": [[p * 1.001, d / (p * 1.001)], [p * 1.05, 1e9]]}


def depth_closes():
    return {"SOLUSDT": crashing(), "ADAUSDT": crashing(drop=7.0), "XRPUSDT": flat(), "DOGEUSDT": flat(),
            "BTCUSDT": flat()}


def test_live_entry_is_capped_by_order_book_depth(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_leverage=2, futures_depth_cap_pct=10.0)
    fake = DepthCcxt(depth_closes(), {"SOLUSDT": 20_000, "ADAUSDT": 20_000}, free=3_000.0)
    bot, _fake, notes, clock = make_fbot(cfg, tmp_path, fake=fake)
    bot.add_capital(2_970.0)                                           # Bot sermayesi 3.000: pozisyon ~5.700 USDT
    bot.scan_and_enter(clock.t)
    pos = next(iter(bot.state.positions.values()))
    assert pos.margin_usdt * 2 == pytest.approx(2_000, rel=0.02)       # ±%1'in küçük tarafının %10'u
    assert "Derinliğe göre boy: pozisyon 2.000 USDT ile sınırlı" in pos.signal_note
    assert any(limit == 500 for _s, limit in fake.book_calls)
    # Küçük hesap: 1.000 USDT altında defter okunmaz
    small = DepthCcxt(depth_closes(), {"SOLUSDT": 1.0})
    bot2, _f, _n, clock2 = make_fbot(cfg, tmp_path / "small", fake=small)
    bot2.scan_and_enter(clock2.t)
    assert bot2.state.positions and not any(limit == 500 for _s, limit in small.book_calls)
    # Kapalıyken defter okunmaz, boy eskisi gibi
    off = DepthCcxt(depth_closes(), {"SOLUSDT": 20_000})
    bot3, _f3, _n3, clock3 = make_fbot(replace(cfg, futures_depth_cap_pct=0.0), tmp_path / "off", fake=off)
    bot3.add_capital(2_970.0)
    bot3.scan_and_enter(clock3.t)
    assert not any(limit == 500 for _s, limit in off.book_calls)


def test_live_capacity_priority_and_unreadable_book(fcfg, tmp_path):
    cfg = live_cfg(fcfg, futures_leverage=2, futures_depth_cap_pct=10.0)
    fake = DepthCcxt(depth_closes(), {"SOLUSDT": 5_000, "ADAUSDT": 1e9}, free=3_000.0)
    bot, _fake, _notes, clock = make_fbot(cfg, tmp_path, fake=fake)
    bot.add_capital(2_970.0)
    sol = FuturesSignal("SOLUSDT", "long", 95.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0})
    ada = FuturesSignal("ADAUSDT", "long", 92.0, BASE_MS - FUT_BAR_MS, 9.0, {"tactic": 1.0})
    assert bot._next_signal([sol, ada]) is ada                          # SOL ince (sınır 500 < ~5.700 istenen)
    fake.depth["SOLUSDT"] = 1e9
    bot._depth_cache.clear()
    assert bot._next_signal([sol, ada]) is sol                          # Takılmıyorsa en güçlü
    assert bot._next_signal([sol]) is sol

    def broken(sym, limit=5):
        raise RuntimeError("bağlantı yok")
    bot._depth_cache.clear()
    fake.fetch_order_book = broken
    margin = bot._apply_depth_cap(sol, 3_000.0, 2)                     # Defter okunamadı: sınır uygulanmaz
    assert margin == 3_000.0 and "depth_cap" not in sol.metrics


# ====================================================================== ayar ve Telegram
def test_settings_validation_env_notes(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_depth_cap_pct, fcfg.futures_backtest_depth) == (0.0, True)
    for bad in (0.5, 60.0, -1.0):
        with pytest.raises(ConfigError):
            replace(fcfg, futures_depth_cap_pct=bad).validate()
    replace(fcfg, futures_depth_cap_pct=10.0).validate()
    monkeypatch.setenv("FUTURES_DEPTH_CAP_PCT", "12.5")
    monkeypatch.setenv("FUTURES_BACKTEST_DEPTH", "false")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_depth_cap_pct, loaded.futures_backtest_depth) == (12.5, False)
    assert EDITABLE["FUTURES_DEPTH_CAP_PCT"].attr == "futures_depth_cap_pct"
    assert EDITABLE["FUTURES_BACKTEST_DEPTH"].kind == "bool"
    assert not _setting_notes(replace(fcfg, futures_depth_cap_pct=10.0))  # Açılış notu yok (rapor 34: bilgilendirme notları kaldırıldı)
    assert DEPTH_CHECK_MIN_NOTIONAL == 1_000.0


def test_risk_page_depth_rows_and_status(fut):
    bot, notes = fut
    press(bot, "risk")
    text, keyboard, _mid = notes.menus[-1]
    data = buttons(keyboard)
    assert "🧱 Derinliğe göre boy: kapalı" in text and "📚 Backtestte emir defteri kayması: açık" in text
    assert "kv:risk:FUTURES_DEPTH_CAP_PCT:10" in data and "kv:risk:FUTURES_BACKTEST_DEPTH:t" in data
    press(bot, "kv:risk:FUTURES_DEPTH_CAP_PCT:10")
    assert bot.cfg.futures_depth_cap_pct == 10.0 and "FUTURES_DEPTH_CAP_PCT=10" in env_text(bot)
    assert "küçük olanın %10'iyle sınırlı" in notes.menus[-1][0]
    press(bot, "kv:risk:FUTURES_BACKTEST_DEPTH:t")
    assert bot.cfg.futures_backtest_depth is False and "FUTURES_BACKTEST_DEPTH=false" in env_text(bot)
