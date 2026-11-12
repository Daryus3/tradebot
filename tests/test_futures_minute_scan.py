"""Dakikalık tarama (FUTURES_MINUTE_SCAN; varsayılan kapalı): çöküş kuralı 5 dk mum kapanışlarının yanında aradaki
dakikalarda da değerlendirilir. Ara dakikada coinin 1 dk mumlarından "o dakikada kapanan" 5 dk mumlar üretilir
(futures_data.resample, shift) ve aynı strateji fonksiyonlarından geçirilir: canlı bot ve backtest ortak. 5 dk
sınırlarında hiçbir şey değişmez; kapalıyken davranış eskisiyle aynıdır."""
import json
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import (ConfigError, _setting_notes, futures_close_only, futures_minute_limit, futures_scan_shifts,
                    load_config)
from futures_data import FIELDS, resample
from runtime_settings import EDITABLE
from strategy import FUT_BAR_MS, futures_kline_limit
from test_futures import BASE_MS, Clock, FakeCcxt, fcfg, make_fbot  # noqa: F401 - pytest fikstürü
from test_futures_cascade import live_cfg

MIN = 60_000
T0 = BASE_MS + 3 * fb.DAY_MS                     # Dibin yaşandığı 5 dk mumun açılışı (5 dk sınırı)
N1 = 3 * 1440 + 30                                # 1 dk mum sayısı: BASE_MS'ten T0 + 30 dk'ya


def minute_closes(dip=7.0):
    """BASE_MS'ten başlayan 1 dk kapanışlar: yatay; T0'dan sonraki 3. dakikada %dip düşüp 5. dakikada toparlanır.
    5 dk kapanışları (T0, T0+5 dk) düşüşü görmez; 1 dk kapanışı T0+3 dk'da görür."""
    c = np.full(N1, 100.0) * (1 + 0.0002 * np.where(np.arange(N1) % 2, 1.0, -1.0))
    i = 3 * 1440                                  # T0'da açılan 1 dk mum
    low = 100.0 * (1 - dip / 100)
    c[i], c[i + 1], c[i + 2], c[i + 3], c[i + 4] = 98.5, 96.0, low, 97.5, 99.6
    return c


def k1_of(closes, start=BASE_MS):
    c = np.asarray(closes, dtype=float)
    o = np.concatenate(([c[0]], c[:-1]))
    v = np.full(len(c), 2.0)
    t = start + np.arange(len(c), dtype=np.int64) * MIN
    return {"t": t, "o": o, "h": np.maximum(o, c) * 1.0002, "l": np.minimum(o, c) * 0.9998, "c": c, "v": v,
            "tb": v / 2, "qv": v * c, "n": v}


def bt_data(k1):
    """Bellekteki 5 dk mumlar (1 dk'dan üretilmiş) + 1 dk yükleyici: evren modu olmayan backtest verisi."""
    k5 = {s: resample(k, FUT_BAR_MS) for s, k in k1.items()}
    end = BASE_MS + N1 * MIN
    coins = tuple(s[:-4] for s in k1)
    return fb.FutData(BASE_MS, end, [fb.Segment(BASE_MS - 1, end, coins)], k5, ((), (), ()),
                      (np.zeros(0), np.zeros(0)), {}, None, "5m", minute_loader=lambda sym: k1[sym])


def scan_cfg(fcfg, **over):
    return live_cfg(fcfg, futures_minute_scan=True, **over)


# ====================================================================== ayar
def test_setting_is_off_by_default_and_supported_only_for_close_only_rules(fcfg, tmp_path, monkeypatch):
    assert fcfg.futures_minute_scan is False and futures_scan_shifts(fcfg) == ()
    assert EDITABLE["FUTURES_MINUTE_SCAN"].attr == "futures_minute_scan"
    on = scan_cfg(fcfg)
    assert futures_close_only(on) and futures_scan_shifts(on) == (60_000, 120_000, 180_000, 240_000)
    note = [n for n in _setting_notes(on) if "FUTURES_MINUTE_SCAN" in n]
    assert len(note) == 1 and "1.662" in note[0] and "3 kat kaymada" in note[0] and "test modunda" in note[0]
    # Sinyal yalnızca kapanışlara bağlı değilse, limit girişte ya da çok uzun pencerede: 5 dk taramaya düşer ve söylenir
    for over in (dict(futures_crash_liq=True), dict(futures_crash_z=2.0), dict(futures_pump_rules="1:8"),
                 dict(futures_confirm="green"), dict(futures_entry_order="limit")):
        off = replace(on, **over)
        assert futures_scan_shifts(off) == (), over
        assert any("kullanılamıyor" in n for n in _setting_notes(off)), over
    assert futures_scan_shifts(replace(on, futures_crash_extra="1:6,24:20")) != ()        # 24 saat sığar
    monkeypatch.setenv("FUTURES_MINUTE_SCAN", "true")
    assert load_config(env_file=str(tmp_path / "x.env"), mode_override="paper").futures_minute_scan is True
    assert futures_minute_limit(on) == 485 and futures_kline_limit(on) >= 96               # 4 sa: (48 + 40 → 96) × 5 + 5
    assert futures_minute_limit(replace(on, futures_crash_extra="1:6,24:20")) == 1500       # Binance sınırı


def test_shifted_resample_builds_candles_closing_at_any_minute():
    k1 = k1_of(np.arange(1, 38, dtype=float), start=BASE_MS)
    plain = resample(k1, FUT_BAR_MS)
    assert np.array_equal(resample(k1, FUT_BAR_MS, shift_ms=0)["c"], plain["c"]) and len(plain["t"]) == 7
    last_close = int(k1["t"][-1]) + MIN                                 # 37. dakikanın sonu: sınırdan 2 dk sonra
    k = resample(k1, FUT_BAR_MS, shift_ms=last_close % FUT_BAR_MS)
    assert int(k["t"][-1]) + FUT_BAR_MS == last_close and set(k) == set(FIELDS)
    assert k["c"][-1] == 37 and k["o"][-1] == k1["o"][-5] and k["v"][-1] == 10.0
    assert k["h"][-1] == k1["h"][-5:].max() and k["l"][-1] == k1["l"][-5:].min()
    assert list(k["c"][-3:]) == [27.0, 32.0, 37.0]                      # Her 5 dakikada bir, aynı kaymayla


# ====================================================================== backtest
def test_backtest_finds_the_dip_only_with_minute_scan(fcfg):
    k1 = {"SOLUSDT": k1_of(minute_closes()), "ADAUSDT": k1_of(minute_closes(dip=0.0) * 0 + 100.0)}
    off, on = live_cfg(fcfg, futures_coins="SOL,ADA"), scan_cfg(fcfg, futures_coins="SOL,ADA")
    sigs, funnel = fb.find_signals(off, bt_data(k1))
    assert sigs == [] and funnel.between == 0                           # 5 dk kapanışları dibi görmedi
    data = bt_data(k1)
    sigs, funnel = fb.find_signals(on, data)
    assert [(s.symbol, s.close_ms) for s in sigs] == [("SOLUSDT", T0 + 3 * MIN)] and funnel.between == 1
    s = sigs[0]
    assert s.metrics["hours"] == 1.0 and s.metrics["drop_pct"] == pytest.approx(7.0, abs=0.05)
    assert s.entry_open == pytest.approx(93.0)                           # Sonraki dakikanın açılışı = dip kapanışı
    assert funnel.signals == 0 and data.monthly_windows is True
    # Açılan işlem dakikalık pencereyle yürür: +%1.1'de başabaş, sonra zaman stopu
    win = fb.slice_range(k1["SOLUSDT"], s.close_ms, s.close_ms + 20 * MIN)
    window = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    res = fb.simulate(fb.params_from_cfg(on), data, sigs, {(s.symbol, s.close_ms): window})
    assert len(res.trades) == 1 and res.trades[0].entry_ms == T0 + 3 * MIN and res.trades[0].pnl > 0


def test_backtest_boundary_signals_are_identical_and_breadth_is_per_minute(fcfg):
    """Dip tam 5 dk sınırında kapanırsa iki modda da aynı sinyal; ara dakikada genişlik o dakikanın kendi sayımı."""
    deep = minute_closes(dip=8.0)
    k1 = {"SOLUSDT": k1_of(deep), "ADAUSDT": k1_of(minute_closes(dip=4.0)), "XRPUSDT": k1_of(minute_closes(dip=3.5)),
          "DOGEUSDT": k1_of(minute_closes(dip=0.0) * 0 + 100.0)}
    i = 3 * 1440
    k1["SOLUSDT"]["c"][i + 4] = 93.5                                    # SOL sınırda da (T0+5 dk) %6.5 aşağıda
    k1["SOLUSDT"]["o"][i + 5] = 93.5
    off, on = live_cfg(fcfg, futures_max_positions=2, futures_extra_breadth=3), \
        scan_cfg(fcfg, futures_max_positions=2, futures_extra_breadth=3)
    a, _f = fb.find_signals(off, bt_data(k1))
    data = bt_data(k1)
    b, funnel = fb.find_signals(on, data)
    edge = [(s.symbol, s.close_ms, s.score, s.breadth) for s in b if not s.close_ms % FUT_BAR_MS]
    assert edge == [(s.symbol, s.close_ms, s.score, s.breadth) for s in a] and len(a) == 1
    mid = {s.close_ms: s for s in b if s.symbol == "SOLUSDT" and s.close_ms % FUT_BAR_MS}
    assert (T0 + 3 * MIN) in mid and mid[T0 + 3 * MIN].breadth == 3     # O dakikada SOL, ADA, XRP %3+ aşağıda
    assert fb.breadth_at(data, T0 + 3 * MIN) == 3 and fb.breadth_at(data, T0 + 5 * MIN) == a[0].breadth
    assert set(data.breadth_shift) == {60_000, 120_000, 180_000, 240_000}


def test_report_rows_and_lines_for_minute_scan(fcfg):
    k1 = {"SOLUSDT": k1_of(minute_closes()), "ADAUSDT": k1_of(minute_closes(dip=0.0) * 0 + 100.0)}
    on = scan_cfg(fcfg, futures_coins="SOL,ADA")
    data = bt_data(k1)
    sigs, funnel = fb.find_signals(on, data)
    windows = {}
    for s in sigs:
        win = fb.slice_range(k1[s.symbol], s.close_ms, s.close_ms + 60 * MIN)
        windows[(s.symbol, s.close_ms)] = (MIN, win["t"], win["o"], win["h"], win["l"], win["c"])
    rows = dict(fb.variant_rows(on, data, sigs, windows))
    base = rows["5 dk'da bir tarama (dakikalık tarama kapalı)"]
    assert len(base.fixed.trades) == 0                                   # 5 dk taramada bu işlem yoktu
    assert any(name.startswith("Çöküş eşiği") and name.endswith("· 5 dk taramayla") for name in rows)
    ev = fb.evaluate(fb.params_from_cfg(on), data, sigs, windows)
    text = fb.format_report(on, data, ev, funnel)
    assert "⏱️ Her dakika tarama (FUTURES_MINUTE_SCAN)" in text and "+ 1 ara dakika sinyali" in text
    plain = dict(fb.variant_rows(live_cfg(fcfg, futures_coins="SOL,ADA"), bt_data(k1), [], {}))
    assert not any("5 dk'da bir tarama" in name or "5 dk taramayla" in name for name in plain)


def test_live_comparison_reads_minute_rows(fcfg, tmp_path):
    path = tmp_path / "futures_scans_paper.jsonl"
    rows = [{"t": T0, "u": ["SOL"], "a": ["SOL"]}, {"t": T0 + MIN, "a": ["SOL"]}, {"t": T0 + 2 * MIN, "a": ["SOL"]},
            {"t": T0 + 3 * MIN, "a": ["SOL"], "s": {"SOL": "açıldı"}}]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    data = fb.FutData(T0, T0 + 10 * MIN, [fb.Segment(T0 - 1, T0 + 10 * MIN, ("SOL",))], {}, ((), (), ()), (None, None),
                      {}, None, "5m")
    trade = fb.FutTrade("SOLUSDT", "long", T0 + 3 * MIN, T0 + 9 * MIN, 93.0, 94.0, "TIME", 3, 10.0, 30.0, 0.0, 0.0, 0.3,
                        3.0, 0.0, 0.0, 0.0, False, False, MIN)
    text = "\n".join(fb.live_comparison(fcfg, data, [trade], path=path))
    assert "Kayıtlı tarama (dakikada bir): 4 / 4" in text and "canlı da açtı: 1" in text


# ====================================================================== canlı bot
class MinuteCcxt(FakeCcxt):
    """1 dk kapanış dizilerinden hem 1 dk hem (saat hizalı) 5 dk mum veren sahte borsa; saatin ilerisindeki mumlar yok."""

    def __init__(self, closes, clock, **kw):
        super().__init__(prices={s: float(c[-1]) for s, c in closes.items()}, **kw)
        self.k1 = {s: k1_of(c) for s, c in closes.items()}
        self.clock = clock
        self.asked = []

    def fapiPublicGetKlines(self, params):
        sym, interval, limit = params["symbol"], params["interval"], int(params["limit"])
        self.asked.append((sym, interval, limit))
        if sym not in self.k1:
            return []
        now_ms = int(self.clock() * 1000)
        k = self.k1[sym]
        seen = {f: v[k["t"] <= now_ms] for f, v in k.items()}             # Açılmış mumlar (sonuncusu sürüyor olabilir)
        step = MIN
        if interval == "5m":
            closed = {f: v[seen["t"] + MIN <= now_ms] for f, v in seen.items()}
            seen, step = resample(closed, FUT_BAR_MS), FUT_BAR_MS
        return [[int(t), str(o), str(h), str(lo), str(c), str(v), int(t) + step - 1, str(v * c), "5", str(v / 2), "0", "0"]
                for t, o, h, lo, c, v in zip(seen["t"], seen["o"], seen["h"], seen["l"], seen["c"], seen["v"])][-limit:]


def journal(bot):
    return [json.loads(line) for line in bot.scan_log_path.read_text(encoding="utf-8").splitlines()]


CLOSES = {"SOLUSDT": minute_closes(), "ADAUSDT": minute_closes(dip=0.0) * 0 + 100.0,
          "XRPUSDT": minute_closes(dip=0.0) * 0 + 100.0, "DOGEUSDT": minute_closes(dip=0.0) * 0 + 100.0}


def test_live_bot_scans_every_minute_and_opens_at_the_dip(fcfg, tmp_path):
    clock = Clock((T0 + 2 * MIN) / 1000 + 10)                            # T0 + 2 dk 10 sn
    fake = MinuteCcxt(CLOSES, clock)
    bot, fake, _notes, clock = make_fbot(scan_cfg(fcfg), tmp_path, fake=fake, clock=clock)
    assert bot._scan_step_ms() == MIN and bot.next_scan_at == (T0 + 3 * MIN) / 1000 + 3     # Sıradaki dakika + 3 sn
    fake.asked.clear()
    clock.t = bot.next_scan_at
    bot.tick()
    limit = futures_minute_limit(bot.cfg)
    assert {(i, n) for _s, i, n in fake.asked} == {("1m", limit)}        # Ara dakika: yalnızca 1 dk mumlar
    assert set(bot.state.positions) == {"SOLUSDT"}
    row = journal(bot)[-1]
    assert row["t"] == T0 + 3 * MIN and row["s"] == {"SOL": "açıldı"} and sorted(row["a"]) == ["ADA", "DOGE", "SOL", "XRP"]
    pos = bot.state.positions["SOLUSDT"]
    assert pos.metrics["drop_pct"] == pytest.approx(7.0, abs=0.05) and pos.metrics["hours"] == 1.0
    assert "Her dakika tarama açık" in bot.scan_report()
    # Aynı veride backtest de aynı dakikada, aynı ölçülerle sinyal verir (ortak fonksiyonlar)
    sigs, _f = fb.find_signals(bot.cfg, bt_data({s: k1_of(c) for s, c in CLOSES.items()}))
    assert [(s.symbol, s.close_ms) for s in sigs] == [("SOLUSDT", T0 + 3 * MIN)]
    assert sigs[0].metrics["drop_pct"] == pytest.approx(pos.metrics["drop_pct"]) and sigs[0].score == pytest.approx(pos.score)
    # 5 dk sınırında eskisi gibi 5 dk mumlar istenir
    bot.state.positions.clear()
    bot.state.cooldowns.clear()
    fake.asked.clear()
    clock.t = (T0 + 5 * MIN) / 1000 + 3
    bot.next_scan_at = clock.t
    bot.tick()
    assert {i for _s, i, _n in fake.asked} == {"5m"} and journal(bot)[-1]["t"] == T0 + 5 * MIN
    assert bot.state.positions == {}                                     # Sınırda fiyat toparlanmıştı: sinyal yok


def test_live_bot_without_the_setting_still_scans_every_five_minutes(fcfg, tmp_path):
    clock = Clock((T0 + 2 * MIN) / 1000 + 10)
    fake = MinuteCcxt(CLOSES, clock)
    bot, fake, _notes, clock = make_fbot(live_cfg(fcfg), tmp_path, fake=fake, clock=clock)
    assert bot._scan_step_ms() == FUT_BAR_MS and bot.next_scan_at == (T0 + 5 * MIN) / 1000 + 3
    clock.t = bot.next_scan_at
    bot.tick()
    assert bot.state.positions == {} and {i for _s, i, _n in fake.asked} == {"5m"}        # Dip kaçtı (eski davranış)
    assert "Her dakika tarama" not in bot.scan_report()
    # Ayar Telegram'dan açılınca sıradaki tarama bir sonraki dakikaya alınır; desteklenmeyen ayarda 5 dk'da kalır
    bot.apply_config(scan_cfg(fcfg))
    assert bot._scan_step_ms() == MIN and bot.next_scan_at == (T0 + 6 * MIN) / 1000 + 3
    bot.apply_config(scan_cfg(fcfg, futures_crash_liq=True))
    assert bot._scan_step_ms() == FUT_BAR_MS and "kullanılamıyor" in bot.scan_report()


def test_fast_entry_works_on_minute_scans_too(fcfg, tmp_path):
    """Hızlı giriş + dakikalık tarama: ara dakikada da tarama kapanıştan 1 sn sonra, yeni 1 dk mum kontrolüyle."""
    clock = Clock((T0 + 2 * MIN) / 1000 + 10)
    fake = MinuteCcxt(CLOSES, clock)
    bot, fake, _notes, clock = make_fbot(scan_cfg(fcfg, futures_fast_entry=True), tmp_path, fake=fake, clock=clock)
    assert bot.next_scan_at == (T0 + 3 * MIN) / 1000 + 1
    bot.sleep_fn = lambda seconds: setattr(clock, "t", clock.t + seconds)
    fake.asked.clear()
    clock.t = bot.next_scan_at
    bot.tick()
    row = journal(bot)[-1]
    assert row["t"] == T0 + 3 * MIN and row["s"] == {"SOL": "açıldı"} and row["fast"] == 1 and row["ms"][0] == 1000
    assert {i for _s, i, _n in fake.asked} == {"1m"} and "kt" not in row      # Yeni 1 dk mum yanıtta: bekleme yok
    assert bot.next_scan_at == (T0 + 4 * MIN) / 1000 + 1


def test_telegram_page_has_the_switch(fcfg):
    from settings_menu import MINUTE_SCAN_HINT, PAGES
    rows = [getattr(r, "key", None) for item in PAGES["fx"].rows for r in (item if isinstance(item, tuple) else (item,))]
    assert "FUTURES_MINUTE_SCAN" in rows and rows.index("FUTURES_MINUTE_SCAN") == rows.index("FUTURES_FAST_ENTRY") + 1
    assert "1.662" in MINUTE_SCAN_HINT and "3 kat kaymada" in MINUTE_SCAN_HINT


def test_config_rejects_nothing_new_and_validates(fcfg):
    scan_cfg(fcfg).validate()
    with pytest.raises(ConfigError):
        replace(fcfg, futures_leverage=0).validate()
