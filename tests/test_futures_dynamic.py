"""Dinamik çöküş kuralları: çok pencere (VEYA), oynaklık eşiği (σ), teyit / izleme modu, ızgara testi."""
from dataclasses import replace

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, futures_crash_rules, parse_crash_rules
from strategy import (FUT_BAR_MS, FUT_KLINE_LIMIT, FUT_Z_LOOKBACK_BARS, FuturesScalpStrategy, crash_entries,
                      crash_scores, crash_state, futures_candidates, futures_features, futures_kline_limit,
                      window_sigma)
from test_futures import BASE_MS, btc_hourly, fcfg, make_fbot  # noqa: F401 - pytest fikstürü
from test_futures_universe import UniCcxt


def series(closes, opens=None, volumes=None):
    """Kapanışlardan 5 dk mumlar (açılış = önceki kapanış, yoksa verilen)."""
    c = np.asarray(closes, dtype=float)
    o = np.concatenate(([c[0]], c[:-1])) if opens is None else np.asarray(opens, dtype=float)
    v = np.full(len(c), 10.0) if volumes is None else np.asarray(volumes, dtype=float)
    t = BASE_MS + np.arange(len(c), dtype=np.int64) * FUT_BAR_MS
    return {"t": t, "o": o, "h": np.maximum(o, c) + 0.01, "l": np.minimum(o, c) - 0.01, "c": c, "v": v,
            "tb": v / 2, "qv": v * c, "n": v}


def drop_then(n, start_drop_at, bars, pct, tail=()):
    """`n` mum yatay (100), `start_drop_at`'tan itibaren `bars` mumda %pct düşüş, sonra `tail` kapanışları."""
    c = [100.0] * start_drop_at
    c += list(np.linspace(100.0, 100.0 * (1 - pct / 100), bars + 1)[1:])
    c += list(tail)
    c += [c[-1]] * (n - len(c))
    return np.array(c[:n])


def evaluate(cfg, k):
    close_ms = int(k["t"][-1]) + FUT_BAR_MS
    return FuturesScalpStrategy(None, cfg).evaluate({"SOLUSDT": k}, None, close_ms + 3000)


# ====================================================================== ayarlar
def test_parse_crash_rules_and_or_merge(fcfg):
    assert parse_crash_rules("") == ()
    assert parse_crash_rules("1:5, 12s:%15; 2h:7.5%") == ((1, 5.0), (12, 15.0), (2, 7.5))
    for bad in ("5", "0:5", "25:5", "1:1", "1:60", "x:5"):
        with pytest.raises(ValueError):
            parse_crash_rules(bad)
    assert futures_crash_rules(fcfg) == ((4, 10.0),)                   # Varsayılan: yalnızca ana kural
    c = replace(fcfg, futures_crash_extra="12:15,1:5,4:8")
    assert futures_crash_rules(c) == ((1, 5.0), (4, 8.0), (12, 15.0))  # Aynı saatte küçük eşik (VEYA)
    assert futures_crash_rules(replace(fcfg, futures_crash_extra="bozuk")) == ((4, 10.0),)


def test_dynamic_settings_validation(fcfg):
    for over in ({"futures_crash_extra": "1:1"}, {"futures_crash_extra": ",".join(["1:5"] * 7)},
                 {"futures_crash_z": 0.5}, {"futures_crash_z": 11}, {"futures_crash_z_min_pct": 0.1},
                 {"futures_confirm": "rsi"}, {"futures_confirm_bars": 0}, {"futures_confirm_bars": 25},
                 {"futures_confirm_volume_ratio": 0.5}):
        with pytest.raises(ConfigError):
            replace(fcfg, **over).validate()
    replace(fcfg, futures_crash_extra="1:5,12:15", futures_crash_z=3, futures_confirm="either").validate()


def test_kline_limit_grows_with_windows(fcfg):
    assert futures_kline_limit(fcfg) == FUT_KLINE_LIMIT                       # Varsayılan değişmez (300)
    long = replace(fcfg, futures_crash_extra="24:20", futures_confirm="green", futures_confirm_bars=24,
                   futures_crash_z=3)
    assert futures_kline_limit(long) == 24 * 12 + 24 + FUT_Z_LOOKBACK_BARS + 40
    assert futures_kline_limit(long) <= 1500


# ====================================================================== çok pencere (VEYA)
def test_extra_window_adds_micro_crash(fcfg):
    k = series(drop_then(200, 188, 12, 6.0))          # Son 1 saatte %6 (4 saatte de %6): ana kural (4s %10) yetmez
    sigs, rejects = evaluate(fcfg, k)
    assert not sigs and rejects["çöküş yok"] == 1
    c = replace(fcfg, futures_crash_extra="1:5,12:15")
    sigs, _ = evaluate(c, k)
    assert len(sigs) == 1 and sigs[0].metrics["hours"] == 1 and sigs[0].metrics["drop_pct"] == pytest.approx(6.0)
    # Puan: eşiği aşma oranı × ana kuralın yüzdesi (6 / 5 × 10)
    assert sigs[0].score == pytest.approx(12.0)


def test_single_rule_behaviour_unchanged(fcfg):
    k = series(drop_then(200, 152, 48, 12.0))
    f = futures_features(k, fcfg)
    old = f["crash"] <= -fcfg.futures_crash_drop_pct        # İlk sürümün kuralı
    assert np.array_equal(futures_candidates(f, fcfg)[0], old)
    idx = np.flatnonzero(old)
    assert np.allclose(crash_scores(f, fcfg, idx), -f["crash"][idx])


# ====================================================================== oynaklık eşiği (σ)
def test_window_sigma_uses_only_history_before_window():
    rng = np.random.default_rng(3)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, 900)))
    sig = window_sigma(c, 48, 288)
    i = 800
    r = np.diff(np.log(c))
    expect = np.std(r[i - 48 - 288:i - 48]) * np.sqrt(48)    # Pencereden (i-48 → i) önceki 288 getiri
    assert sig[i] == pytest.approx(expect, rel=1e-9)
    c2 = c.copy()
    c2[i - 47:] *= 0.5                                       # Pencere içindeki çöküş ölçüyü değiştirmez
    assert window_sigma(c2, 48, 288)[i] == pytest.approx(expect, rel=1e-9)
    assert np.isnan(sig[48 + 288 - 1])


def zigzag(n, amp):
    """Sabit oynaklık: kapanışlar 100 × (1 ± amp) arasında gidip gelir."""
    return 100.0 * (1 + amp * np.where(np.arange(n) % 2, 1.0, -1.0))


def test_z_threshold_adapts_to_volatility(fcfg):
    n = 400
    calm = zigzag(n, 0.001)                     # 5 dk'da ~%0.2: 4 saatlik olağan hareket ~%1.4
    calm[-48:] = np.linspace(calm[-49], calm[-49] * 0.94, 48)    # Son 4 saatte %6
    wild = zigzag(n, 0.012)                     # 5 dk'da ~%2.4: 4 saatlik olağan hareket ~%17
    wild[-48:] = np.linspace(wild[-49], wild[-49] * 0.88, 48)    # Son 4 saatte %12
    zc = replace(fcfg, futures_crash_z=3.0, futures_crash_z_min_pct=3.0)
    assert not evaluate(fcfg, series(calm))[0] and evaluate(fcfg, series(wild))[0]    # Sabit %10: tersi
    sigs, _ = evaluate(zc, series(calm))
    assert len(sigs) == 1 and sigs[0].metrics["z"] == 3.0 and sigs[0].metrics["thr_pct"] < 6
    assert "standart sapma" in sigs[0].note()
    assert not evaluate(zc, series(wild))[0]                     # Oynak coinde %12 olağan
    floor = replace(zc, futures_crash_z_min_pct=7.0)
    assert not evaluate(floor, series(calm))[0]                  # En az düşüş şartı


# ====================================================================== teyit / izleme modu
def test_volume_confirmation_waits_for_capitulation(fcfg):
    c = drop_then(200, 152, 48, 12.0, tail=[87.5, 87.0, 87.2])     # Çöküş 199. mumdan önce başlıyor
    vols = np.full(200, 10.0)
    k = series(c, volumes=vols)
    vc = replace(fcfg, futures_confirm="volume", futures_confirm_volume_ratio=3.0)
    sigs, rejects = evaluate(vc, k)
    assert not sigs and rejects["teyit bekleniyor (hacim zirvesi)"] == 1
    vols[-1] = 40.0                                                  # Son mumda 4 kat hacim: kapitülasyon
    sigs, _ = evaluate(vc, series(c, volumes=vols))
    assert len(sigs) == 1 and sigs[0].metrics["confirm"] == 1.0 and sigs[0].metrics["confirm_vol"] >= 3
    assert "hacim zirvesi" in sigs[0].note()


def test_green_confirmation_and_watch_expiry(fcfg):
    c = drop_then(200, 100, 48, 12.0)                # Düşüş 148. mumda biter, sonra yatay
    o = np.concatenate(([c[0]], c[:-1]))
    o[160] = c[160] - 0.5                            # 160. mum yeşil (kapanış > açılış)
    k = series(c, opens=o)
    gc = replace(fcfg, futures_confirm="green", futures_confirm_bars=12)
    f = futures_features(k, gc)
    trig = crash_state(f, gc)[0]
    last_trig = int(np.flatnonzero(trig)[-1])
    entries = crash_entries(f, gc, trig)
    assert entries[160] == (160 - last_trig <= 12)
    assert not entries[last_trig + 13:].any()        # İzleme süresi doldu
    green_bars = np.flatnonzero(entries)
    assert all(c[i] > o[i] for i in green_bars)
    # Yeşil mum hemen gelebilir: ölçüler izlemedeki en derin çöküş mumundan (1 mum önce, -%13)
    sigs, _ = evaluate(gc, series(drop_then(200, 151, 48, 13.0, tail=[87.5])))
    assert len(sigs) == 1 and sigs[0].metrics["confirm"] == 2.0 and sigs[0].metrics["confirm_bars"] == 1
    assert sigs[0].metrics["drop_pct"] == pytest.approx(13.0) and sigs[0].price == 87.5
    assert "ilk yeşil mum (çöküşten 5 dk sonra)" in sigs[0].note()
    sigs, rejects = evaluate(gc, series(drop_then(200, 151, 49, 13.0)))      # Hâlâ düşüyor: teyit yok
    assert not sigs and rejects["teyit bekleniyor (yeşil mum)"] == 1


def test_watch_mode_backtest_matches_live(fcfg):
    c = drop_then(240, 150, 48, 12.0, tail=[87.6, 87.4])
    vols = np.full(240, 10.0)
    vols[200] = 50.0
    k = series(c, volumes=vols)
    vc = replace(fcfg, futures_confirm="either", futures_crash_extra="1:5", futures_confirm_bars=24)
    close = k["t"] + FUT_BAR_MS
    data = fb.FutData(int(k["t"][0]), int(close[-1]) + FUT_BAR_MS, [fb.Segment(int(k["t"][0]), int(close[-1]) + 1,
                                                                               ("SOL",))],
                      {"SOLUSDT": k}, fb.btc_trend_1h(btc_hourly(True, int(close[-1]))), (k["t"], k["c"]), {}, None,
                      "5m")
    signals, _funnel = fb.find_signals(vc, data)
    assert signals
    for s in signals[:5]:                                        # Her backtest sinyali canlıda da aynı mumda
        i = int(np.searchsorted(close, s.close_ms))
        sub = {key: val[:i + 1] for key, val in k.items()}
        live, _ = evaluate(vc, sub)
        assert live and live[0].score == pytest.approx(s.score)


# ====================================================================== canlı ön eleme
def test_prefilter_uses_lowest_rule_and_24h_low(fcfg, tmp_path):
    ucfg = replace(fcfg, futures_universe=3, futures_universe_with_list=False)
    fake = UniCcxt({"SOLUSDT": (94.5, 100.0, 2e9), "ADAUSDT": (99.5, 100.0, 1e9)})
    bot, fake, _notes, _clock = make_fbot(ucfg, tmp_path, fake=fake)
    assert bot.universe == ["SOLUSDT", "ADAUSDT"]
    tickers = {"SOLUSDT": {"last": 94.5, "high": 100.0, "low": 94.0, "qv": 2e9},     # %5.5 aşağıda
               "ADAUSDT": {"last": 99.5, "high": 100.0, "low": 93.5, "qv": 1e9}}     # toparlanmış, dip %6.5
    bot.exchange.tickers_24h = lambda: tickers
    assert bot._scan_symbols() == ([], 2)                                # 4s %10: ikisi de uzak
    bot.cfg = replace(ucfg, futures_crash_extra="1:5")
    assert bot._scan_symbols()[0] == ["SOLUSDT"]                         # En küçük eşik %5 (pay %1)
    bot.cfg = replace(ucfg, futures_crash_extra="1:5", futures_confirm="green")
    assert bot._scan_symbols()[0] == ["SOLUSDT", "ADAUSDT"]              # İzlemede 24 saatlik dibe bakılır
    bot.cfg = replace(ucfg, futures_crash_z=3.0, futures_crash_z_min_pct=3.0)
    assert bot._scan_symbols()[0] == ["SOLUSDT"]                         # σ modunda en az düşüş şartı (%3)
    bot.cfg = replace(ucfg, futures_crash_extra="24:20", futures_confirm="green")
    assert bot._scan_symbols() == (["SOLUSDT", "ADAUSDT"], 0)            # 24 saati aşan izleme: ön eleme yok


# ====================================================================== ızgara testi
def grid_data(cfg):
    coins = {}
    for sym, pct, start, bars in (("SOLUSDT", 12.0, 600, 48), ("ADAUSDT", 6.0, 640, 12), ("XRPUSDT", 16.0, 900, 48)):
        c = drop_then(1400, start, bars, pct, tail=list(np.linspace(100 * (1 - pct / 100), 100, 60)))
        coins[sym] = series(c)
    t0 = int(BASE_MS)
    end = t0 + 1400 * FUT_BAR_MS
    seg = [fb.Segment(t0 + 400 * FUT_BAR_MS, end, ("SOL", "ADA", "XRP"))]
    return fb.FutData(t0, end, seg, coins, fb.btc_trend_1h(btc_hourly(True, end)), (coins["SOLUSDT"]["t"],
                      coins["SOLUSDT"]["c"]), {}, None, "5m")


def test_grid_variants_cover_requested_grid(fcfg):
    vs = fb.grid_variants(fcfg)
    assert vs[0].label.startswith("Mevcut ayarınız") and not vs[0].over
    assert len(vs) == 1 + 4 * 4 * 3 + 4 + 4 * 3 * 3 + len(fb.GRID_EXTRAS)
    labels = {v.label for v in vs}
    assert len(labels) == len(vs)                                          # Tekrar yok
    assert "1s %5 · sadece kapanış" in labels and "8s %15 · ilk yeşil mum (60 dk)" in labels
    assert any("1s %5 | 4s %10 | 12s %15" in x for x in labels) and any("3σ" in x for x in labels)
    assert "1s %6 | 4s %10 · sadece kapanış" in labels and "1s %6 | 4s %10 | 12s %15 · sadece kapanış" in labels
    env = dict(vs[1].settings(fcfg))
    assert env["FUTURES_CRASH_HOURS"] == 1 and env["FUTURES_CONFIRM"] == "close"
    assert "FUTURES_CONFIRM_BARS" not in env and "FUTURES_CRASH_Z_MIN_PCT" not in env


def test_grid_search_matches_find_signals(fcfg):
    cfg = replace(fcfg, loss_cooldown_minutes=0, max_hold_minutes=30)
    data = grid_data(cfg)
    variants = [fb.GridVariant("taban"),
                fb.GridVariant("1s %5", (("futures_crash_hours", 1), ("futures_crash_drop_pct", 5.0))),
                fb.GridVariant("yeşil", (("futures_confirm", "green"),))]
    rows = fb.grid_search(cfg, data, variants, split_ms=int(BASE_MS) + 1000 * FUT_BAR_MS)
    assert [r.variant.label for r in rows] == ["taban", "1s %5", "yeşil"]
    for v, row in zip(variants, rows):
        c2 = v.cfg(cfg)
        sigs, _ = fb.find_signals(c2, data)
        assert row.signals == len(sigs)
        windows = fb.load_windows(data, sigs, 40)
        fixed = fb.simulate(fb.params_from_cfg(c2), data, sigs, windows,
                            fixed_notional=cfg.start_capital * cfg.futures_margin_usage_pct / 100 * cfg.futures_leverage)
        assert row.whole.trades == len(fixed.trades)
        assert row.whole.sum_move == pytest.approx(sum(fb.net_move_pct(tr) for tr in fixed.trades))
        assert sum(p.trades for p in row.parts) == row.whole.trades
    syms = [{x.symbol for x in fb.find_signals(v.cfg(cfg), data)[0]} for v in variants[:2]]
    assert syms[0] == {"SOLUSDT", "XRPUSDT"} and "ADAUSDT" in syms[1]   # ADA'nın 1 saatlik %6'sı yalnızca 1s %5'te


def test_grid_windows_slices():
    k = series(np.linspace(100, 110, 50))
    w = fb.GridWindows(8)
    w.add("SOLUSDT", k, np.array([10, 45, 49]))
    bar, t, o, h, low, c = w.get(("SOLUSDT", int(k["t"][10]) + FUT_BAR_MS))
    assert bar == FUT_BAR_MS and len(t) == 8 and t[0] == k["t"][11] and o[0] == k["o"][11]
    assert len(w.get(("SOLUSDT", int(k["t"][45]) + FUT_BAR_MS))[1]) == 4      # Veri sonuna kadar
    assert w.get(("SOLUSDT", int(k["t"][49]) + FUT_BAR_MS)) is None             # Sonraki mum yok
    assert w.get(("SOLUSDT", 123)) is None and w.get(("ADAUSDT", 1)) is None


# ====================================================================== Telegram ve ayar dosyası
def press(bot, data, message_id=7):
    from commands import CALLBACK, Command
    bot.commands.handle(Command(CALLBACK, data.split(":"), kind="callback", message_id=message_id))


def data_of(notes):
    return [d for row in notes.menus[-1][1] for _label, d in row]


@pytest.fixture
def fbot(fcfg, tmp_path):
    bot, _fake, notes, _clock = make_fbot(fcfg, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    return bot, notes, env


def test_rules_setting_kind_round_trip():
    from runtime_settings import EDITABLE, parse_value
    assert EDITABLE["FUTURES_CRASH_EXTRA"].kind == "rules"
    assert parse_value("FUTURES_CRASH_EXTRA", "1h6,12h15") == "1:6,12:15"           # Düğme verisi
    assert parse_value("FUTURES_CRASH_EXTRA", " 1s:%5 ; 12:15 ") == "1:5,12:15"       # Elle yazım
    assert parse_value("FUTURES_CRASH_EXTRA", "kapalı") == "" and parse_value("FUTURES_CRASH_EXTRA", "off") == ""
    with pytest.raises(ConfigError):
        parse_value("FUTURES_CRASH_EXTRA", "1:1")
    assert parse_value("FUTURES_CONFIRM", "Volume") == "volume"
    with pytest.raises(ConfigError):
        parse_value("FUTURES_CONFIRM", "rsi")


def test_entry_page_rows_and_env_persistence(fbot):
    bot, notes, env = fbot
    press(bot, "pg:fe")
    data = data_of(notes)
    assert "kv:fe:FUTURES_CRASH_EXTRA:1h6,12h15" in data and "kv:fe:FUTURES_CONFIRM:green" in data
    assert "kv:fe:FUTURES_CRASH_Z:3" in data and "fgp:1" in data and "fgp:2" in data
    assert "kv:fe:FUTURES_CONFIRM_BARS:12" not in data                     # Teyit kapalıyken izleme süresi gizli
    assert all(len(d.encode()) <= 64 for d in data)
    press(bot, "kv:fe:FUTURES_CRASH_EXTRA:1h6,12h15")
    assert bot.cfg.futures_crash_extra == "1:6,12:15" and "FUTURES_CRASH_EXTRA=1:6,12:15" in env.read_text()
    assert "1 sa %6 + 12 sa %15" in notes.menus[-1][0]
    press(bot, "kv:fe:FUTURES_CONFIRM:volume")
    data = data_of(notes)
    assert bot.cfg.futures_confirm == "volume" and "kv:fe:FUTURES_CONFIRM_BARS:6" in data
    assert "kv:fe:FUTURES_CONFIRM_VOLUME_RATIO:4" in data and "hacim zirvesi" in notes.menus[-1][0]
    press(bot, "kv:fe:FUTURES_CRASH_Z:3")
    assert bot.cfg.futures_crash_z == 3 and "kv:fe:FUTURES_CRASH_Z_MIN_PCT:5" in data_of(notes)
    assert "3 standart sapma" in notes.menus[-1][0]
    press(bot, "kv:fe:FUTURES_CRASH_EXTRA:off")
    assert bot.cfg.futures_crash_extra == "" and "FUTURES_CRASH_EXTRA=\n" in env.read_text()


def test_grid_preset_buttons_need_confirmation(fbot):
    from config import FUTURES_GRID_PRESETS
    bot, notes, env = fbot
    press(bot, "fgp:1")
    pending = bot.commands.pending
    assert pending is not None and pending.action == "setting" and "Izgara #1" in notes.menus[-1][0]
    assert bot.cfg.futures_crash_extra == ""                              # Onaydan önce değişmez
    press(bot, f"yes:{pending.token}")
    want = FUTURES_GRID_PRESETS[0]["values"]
    assert bot.cfg.futures_crash_extra == want["FUTURES_CRASH_EXTRA"]
    assert bot.cfg.breakeven_trigger_pct == want["BREAKEVEN_TRIGGER_PCT"]
    assert bot.cfg.max_hold_minutes == want["MAX_HOLD_MINUTES"]
    assert f"FUTURES_CRASH_EXTRA={want['FUTURES_CRASH_EXTRA']}" in env.read_text()
    press(bot, "pg:fe")
    labels = [label for row in notes.menus[-1][1] for label, _d in row]
    assert any(label.startswith("✅ 🏆 Izgara #1") for label in labels)
    press(bot, "fgp:1")
    assert "zaten kullanılıyor" in notes.menus[-1][0]
    press(bot, "fgp:9")
    assert "geçerli değil" in notes.messages[-1]


def test_grid_presets_are_valid_configs(fcfg):
    from config import FUTURES_GRID_PRESETS
    from runtime_settings import apply_settings
    for preset in FUTURES_GRID_PRESETS:
        new, _notes = apply_settings(fcfg, {k: str(v) if not isinstance(v, bool) else ("true" if v else "false")
                                            for k, v in preset["values"].items()})
        assert new.futures_tactic == "crash" and futures_crash_rules(new)[0][0] >= 1


def test_izgara_flag_and_runner_kind():
    import main
    from backtest_runner import KINDS
    args = main.parse_args(["--backtest", "--strateji", "futures", "--izgara"])
    assert args.izgara and KINDS["izgara"][1] == ("--izgara",)


def test_run_grid_cli_report(fcfg, tmp_path, monkeypatch):
    cfg = replace(fcfg, loss_cooldown_minutes=0)
    data = grid_data(cfg)
    monkeypatch.setattr(fb, "period", lambda *_a, **_k: (data.start_ms, data.end_ms, data.segments))
    monkeypatch.setattr(fb, "prepare", lambda *_a, **_k: data)
    small = [fb.GridVariant("Mevcut ayarınız: " + fb.rule_label(cfg)),
             fb.GridVariant("1s %5", (("futures_crash_hours", 1), ("futures_crash_drop_pct", 5.0),
                                      ("futures_crash_extra", ""), ("futures_crash_z", 0.0),
                                      ("futures_confirm", "close"))),
             fb.GridVariant("ek 1:5", (("futures_crash_extra", "1:5"),))]
    monkeypatch.setattr(fb, "grid_variants", lambda _cfg: small)
    monkeypatch.setattr(fb, "grid_exits", lambda _cfg: [("ayarlarınız", {}), ("TP4", {"tp_pct": 4.0})])
    monkeypatch.setattr(fb, "GRID_SPLIT", fb._date(int(BASE_MS) + 1000 * FUT_BAR_MS))
    report, rows, path = fb.run_grid_cli(cfg, None, None, tmp_path, progress=lambda _m: None)
    assert path is not None and path.exists() and path.read_text(encoding="utf-8").startswith("🧮 IZGARA")
    for part in ("1) GİRİŞ KURALLARI", "2) ÇIKIŞ DENEMELERİ", "3) 1 DK MUMLARLA DOĞRULAMA", "EN İYİ 2", "kayma ×3",
                 "FUTURES_CRASH_HOURS=", "Mevcut:"):
        assert part in report, part
    stage2 = rows[len(small):]                      # 2. aşama: mevcut + kârı yeterli adaylar, her biri 2 çıkışla
    assert len(stage2) >= 2 and len(stage2) % 2 == 0 and stage2[0].variant.label == small[0].label
