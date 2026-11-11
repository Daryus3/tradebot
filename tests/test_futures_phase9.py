"""Faz 9: pozisyona göre zaman stopu. Dalga modunda ek pozisyonlar (FUTURES_EXTRA_HOLD_MINUTES) ve dalgada açılan ilk
pozisyon (FUTURES_WAVE_HOLD_MINUTES, genişlik ≥ FUTURES_WAVE_HOLD_BREADTH) ana ayardan farklı sürede kapatılır. Ayar,
backtest motoru, rapor, canlı / test modu ve Telegram; varsayılanda (0 / 0) eski davranış aynen."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from config import (FUTURES_PHASE9_EXTRA_HOLD, FUTURES_PHASE9_OFF, FUTURES_PHASE9_WAVE_HOLD, ConfigError, _setting_notes,
                    futures_breadth_on, futures_hold_minutes, load_config)
from runtime_settings import apply_settings
from test_futures import BASE_MS, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, bt_data, bt_sig, live_cfg, press
from test_futures_phase8 import p6cfg, wave_bot, wave_closes

MIN = 60_000
FLAT = [(100, 100.05, 99.95, 100.0)] * 90          # Hiçbir çıkışa değmeyen 1 dk mumlar: işlem zaman stopuyla kapanır


# ====================================================================== ayar
def test_phase9_settings_defaults_validation_env_helper_notes(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_extra_hold_minutes, fcfg.futures_wave_hold_minutes, fcfg.futures_wave_hold_breadth) == (0, 0, 8)
    assert FUTURES_PHASE9_OFF == {"FUTURES_EXTRA_HOLD_MINUTES": 0, "FUTURES_WAVE_HOLD_MINUTES": 0}
    base = fcfg.max_hold_minutes
    assert futures_hold_minutes(fcfg, 0, 20) == futures_hold_minutes(fcfg, 2, 20) == base       # Kapalı: hep ana ayar
    on = p6cfg(fcfg, futures_extra_hold_minutes=35, futures_wave_hold_minutes=60)
    on.validate()
    assert futures_hold_minutes(on, 0, 8) == futures_hold_minutes(on, 0, 30) == 60              # İlk pozisyon, dalgada
    assert futures_hold_minutes(on, 0, 7) == futures_hold_minutes(on, 0, None) == base          # Dalga yok / bilinmiyor
    assert futures_hold_minutes(on, 1, 30) == futures_hold_minutes(on, 3, 0) == 35              # Ek pozisyon
    single = replace(on, futures_max_positions=1)
    assert futures_hold_minutes(single, 1, 30) == base and futures_hold_minutes(single, 0, 12) == 60
    assert futures_hold_minutes(replace(on, futures_wave_hold_breadth=12), 0, 10) == base
    for bad in (dict(futures_extra_hold_minutes=3), dict(futures_extra_hold_minutes=2000),
                dict(futures_wave_hold_minutes=4), dict(futures_wave_hold_minutes=-5), dict(futures_wave_hold_breadth=0)):
        with pytest.raises(ConfigError):
            replace(on, **bad).validate()
    for key, value in (("FUTURES_EXTRA_HOLD_MINUTES", "35"), ("FUTURES_WAVE_HOLD_MINUTES", "60"),
                       ("FUTURES_WAVE_HOLD_BREADTH", "10")):
        monkeypatch.setenv(key, value)
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_extra_hold_minutes, loaded.futures_wave_hold_minutes, loaded.futures_wave_hold_breadth) \
        == (35, 60, 10)
    new, _ = apply_settings(fcfg, {"FUTURES_WAVE_HOLD_MINUTES": "70"})
    assert new.futures_wave_hold_minutes == 70
    notes = [n for n in _setting_notes(on) if "Faz 9" in n]
    assert len(notes) == 1 and "ek pozisyonlar 35 dk'da" in notes[0] and "ilk pozisyon 60 dk'da" in notes[0]
    assert "2025'te geride" in notes[0] and "hiçbir yılda geride değil" in notes[0]
    assert "etkisiz" in " ".join(n for n in _setting_notes(replace(fcfg, futures_extra_hold_minutes=35)) if "Faz 9" in n)
    assert not any("Faz 9" in n for n in _setting_notes(p6cfg(fcfg)))
    # Dalga kuralı tek pozisyonda da genişlik ölçümünü açar (canlıda sinyale genişlik eklenir)
    assert not futures_breadth_on(fcfg) and futures_breadth_on(replace(fcfg, futures_wave_hold_minutes=60))
    assert (FUTURES_PHASE9_EXTRA_HOLD, FUTURES_PHASE9_WAVE_HOLD) == (fb.PHASE9_EXTRA, fb.PHASE9_WAVE) == (35, 60)


def test_backtest_and_live_use_the_same_hold_rule(fcfg):
    """config.futures_hold_minutes (canlı) ile futures_backtest.hold_minutes (backtest) her durumda aynı sonucu verir."""
    for over in (dict(), dict(futures_extra_hold_minutes=35), dict(futures_wave_hold_minutes=60),
                 dict(futures_extra_hold_minutes=30, futures_wave_hold_minutes=70, futures_wave_hold_breadth=12),
                 dict(futures_extra_hold_minutes=35, futures_wave_hold_minutes=60, futures_max_positions=1)):
        cfg = p6cfg(fcfg, **over)
        p = fb.params_from_cfg(cfg)
        assert (p.extra_hold_min, p.wave_hold_min, p.wave_hold_breadth) == (
            cfg.futures_extra_hold_minutes, cfg.futures_wave_hold_minutes, cfg.futures_wave_hold_breadth)
        for n_open in (0, 1, 3):
            for breadth in (0, 7, 8, 11, 12, 40):
                assert fb.hold_minutes(p, n_open, breadth) == futures_hold_minutes(cfg, n_open, breadth)


# ====================================================================== backtest motoru
def test_backtest_time_stop_by_slot_and_breadth():
    data = bt_data()
    a, b = bt_sig("AAAUSDT", BASE_MS, 12), bt_sig("BBBUSDT", BASE_MS + 5 * MIN, 12)
    w = {(s.symbol, s.close_ms): window(FLAT, start=s.close_ms) for s in (a, b)}
    p = params(start_capital=100.0, tp_pct=5.0, sl_pct=3.0, be_trigger_pct=1.0, be_floor_pct=0.3, hold_min=45,
               max_positions=2, extra_breadth=8)

    def minutes(res):
        return [(t.slot, t.reason, (t.exit_ms - t.entry_ms) // MIN) for t in res.trades]
    assert minutes(fb.simulate(p, data, [a, b], w)) == [(1, "TIME", 45), (2, "TIME", 45)]            # Kapalı: hep 45
    on = replace(p, extra_hold_min=30, wave_hold_min=60)
    assert minutes(fb.simulate(on, data, [a, b], w)) == [(1, "TIME", 60), (2, "TIME", 30)]           # İlk 60, ek 30
    assert minutes(fb.simulate(on, data, [a, b], w, fixed_notional=50.0)) == [(1, "TIME", 60), (2, "TIME", 30)]
    assert minutes(fb.simulate(replace(p, extra_hold_min=30), data, [a, b], w)) == [(1, "TIME", 45), (2, "TIME", 30)]
    assert minutes(fb.simulate(replace(p, wave_hold_min=60), data, [a, b], w)) == [(1, "TIME", 60), (2, "TIME", 45)]
    # Genişlik eşiğin altındaysa ilk pozisyon ana sürede; ek pozisyon kuralı genişlikten bağımsız
    high = replace(on, wave_hold_breadth=13)
    assert minutes(fb.simulate(high, data, [a, b], w)) == [(1, "TIME", 45), (2, "TIME", 30)]
    # Tek pozisyon: ek pozisyon yok, dalga kuralı geçerli (ilk işlem 60 dk sürerken ikinci sinyal atlanır)
    single = replace(on, max_positions=1)
    assert minutes(fb.simulate(single, data, [a, b], w)) == [(1, "TIME", 60)]
    quiet = bt_sig("AAAUSDT", BASE_MS, 3)
    assert minutes(fb.simulate(single, data, [quiet], {("AAAUSDT", BASE_MS): window(FLAT)})) == [(1, "TIME", 45)]
    # Ek pozisyon erken kapanınca yuvası boşalır: 30. dakikadaki üçüncü sinyal alınır (45 dk'da alınamazdı)
    c = bt_sig("CCCUSDT", BASE_MS + 40 * MIN, 12)
    w[(c.symbol, c.close_ms)] = window(FLAT, start=c.close_ms)
    assert len(fb.simulate(replace(p, extra_hold_min=30), data, [a, b, c], w).trades) == 3
    assert len(fb.simulate(p, data, [a, b, c], w).trades) == 2
    assert fb.phase9_active(on) and not fb.phase9_active(p) and not fb.phase9_active(replace(p, extra_hold_min=45))
    assert not fb.phase9_active(replace(p, extra_hold_min=30, max_positions=1))


def test_report_label_rows_and_window_length(fcfg):
    on = p6cfg(fcfg, futures_extra_hold_minutes=35, futures_wave_hold_minutes=60)
    label = fb.phase9_label(fb.params_from_cfg(on))
    assert "ek pozisyonlarda 35 dk" in label and "≥8 coin %3+ düşerken açılan ilk pozisyonda 60 dk" in label
    assert fb.phase9_label(fb.params_from_cfg(p6cfg(fcfg))) == ""
    calls = []

    def ev(s=None, **over):
        calls.append(over)
        return over
    rows = fb.phase9_rows(on, ev)
    assert len(rows) == 1 and "Faz 9 kapalı" in rows[0][0] and rows[0][1] == {"extra_hold_min": 0, "wave_hold_min": 0}
    rows = fb.phase9_rows(p6cfg(fcfg), ev)                               # Kapalıyken: iki deneme satırı
    assert [r[1] for r in rows] == [{"wave_hold_min": 60, "wave_hold_breadth": 8},
                                    {"extra_hold_min": 35, "wave_hold_min": 60, "wave_hold_breadth": 8}]
    assert len(fb.phase9_rows(fcfg, ev)) == 1                             # Tek pozisyon: yalnız dalga satırı
    # Faz 5 / Faz 6 karşılaştırma satırları Faz 9'suz hesaplanır
    assert fb.PHASE6_SIM["extra_hold_min"] == fb.PHASE6_SIM["wave_hold_min"] == 0
    assert fb.PHASE6_SINGLE_SIM["wave_hold_min"] == 0
    # Çıkış penceresi en uzun zaman stopunu kapsar
    assert fb.window_minutes(replace(fcfg, max_hold_minutes=45)) == fb.MAX_WINDOW_MIN
    assert fb.window_minutes(replace(fcfg, futures_wave_hold_minutes=600)) == 610
    assert fb.window_minutes(replace(fcfg, futures_extra_hold_minutes=400)) == 410
    # Rapor başlığı
    data = bt_data()
    s1 = bt_sig("BTCUSDT", BASE_MS, 12)
    w = {(s1.symbol, s1.close_ms): window(FLAT, start=s1.close_ms)}
    ev1 = fb.evaluate(fb.params_from_cfg(on), data, [s1], w)
    text = fb.format_report(on, data, ev1, fb.Funnel())
    assert "Faz 9: ek pozisyonlarda 35 dk" in text
    assert "Faz 9" not in fb.format_report(p6cfg(fcfg), data, fb.evaluate(fb.params_from_cfg(p6cfg(fcfg)), data, [s1], w),
                                           fb.Funnel())


# ====================================================================== canlı / test modu
def test_paper_positions_close_at_their_own_time_stop(fcfg, tmp_path):
    bot, fake, notes, clock = wave_bot(fcfg, tmp_path, futures_extra_hold_minutes=35, futures_wave_hold_minutes=60,
                                       futures_wave_hold_breadth=3, max_hold_minutes=45)
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions
    assert len(pos) == 3 and bot.last_breadth == 3
    first, extra = pos["SOLUSDT"], pos["ADAUSDT"]                       # En güçlü sinyal ilk pozisyon
    assert first.metrics["hold_min"] == 60.0 and extra.metrics["hold_min"] == 35.0
    assert pos["XRPUSDT"].metrics["hold_min"] == 35.0
    assert (bot._hold_minutes(first), bot._hold_minutes(extra)) == (60, 35)
    opened = [m for m in notes.messages if "POZİSYON AÇILDI" in m]
    assert sum("Zaman stopu: 35 dk (Faz 9; ana ayar 45 dk)" in m for m in opened) == 2
    assert sum("Zaman stopu: 60 dk (Faz 9; ana ayar 45 dk)" in m for m in opened) == 1
    bot.manage_positions(clock.t + 34 * 60)
    assert len(bot.state.positions) == 3                                # Henüz kimsenin süresi dolmadı
    bot.manage_positions(clock.t + 36 * 60)
    assert set(bot.state.positions) == {"SOLUSDT"}                      # Ek pozisyonlar 35 dk'da kapandı
    assert sum("Zaman stopu" in m or "zaman stopu" in m.lower() for m in notes.messages if "KAPANDI" in m.upper()) >= 2
    bot.manage_positions(clock.t + 46 * 60)
    assert set(bot.state.positions) == {"SOLUSDT"}                      # İlk pozisyon ana sürede (45) kapanmaz
    bot.manage_positions(clock.t + 61 * 60)
    assert not bot.state.positions
    report = bot.scan_report()
    assert "ek pozisyon zaman stopu 35 dk" in report and "≥3 coin düşerken ilk pozisyon 60 dk" in report


def test_default_keeps_one_time_stop_and_bad_metric_falls_back(fcfg, tmp_path):
    bot, fake, notes, clock = wave_bot(fcfg, tmp_path, max_hold_minutes=45)
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions
    assert len(pos) == 3 and not any("hold_min" in p.metrics for p in pos.values())
    assert all("Zaman stopu: 45 dk |" in m for m in notes.messages if "POZİSYON AÇILDI" in m)
    first = pos["SOLUSDT"]
    first.metrics["hold_min"] = "bozuk"                                 # Okunamayan kayıt: ana ayar
    assert bot._hold_minutes(first) == 45
    bot.manage_positions(clock.t + 44 * 60)
    assert len(bot.state.positions) == 3
    bot.manage_positions(clock.t + 46 * 60)
    assert not bot.state.positions                                      # Hepsi 45 dk'da
    assert "zaman stopu" not in bot.scan_report().split("Genişlik")[-1].split("\n")[0]


def test_wave_rule_in_single_position_mode_uses_breadth(fcfg, tmp_path):
    """Tek pozisyonda da (dalga modu kapalı) dalga kuralı çalışır: genişlik ölçülür, eşik sağlanırsa uzun tutulur."""
    def single(folder, need):
        cfg = live_cfg(fcfg, futures_max_positions=1, futures_wave_hold_minutes=60, futures_wave_hold_breadth=need,
                       max_hold_minutes=45)
        return make_fbot(cfg, folder, fake=KlineCcxt(wave_closes()))
    bot, fake, _notes, clock = single(tmp_path, 3)
    assert bot.slots() == 1 and bot._cascade_on()                       # Dalga kuralı genişlik ölçümünü açar
    bot.scan_and_enter(clock.t)
    (pos,) = bot.state.positions.values()
    assert bot.last_breadth == 3 and pos.metrics["hold_min"] == 60.0
    (tmp_path / "b").mkdir()
    bot2, _f, _n, clock2 = single(tmp_path / "b", 4)
    bot2.scan_and_enter(clock2.t)
    (pos2,) = bot2.state.positions.values()
    assert "hold_min" not in pos2.metrics                                # Genişlik 3 < 4: ana süre


# ====================================================================== Telegram
def test_wave_page_rows_hint_and_presets_turn_phase9_off(fcfg, tmp_path):
    cfg = replace(fcfg, futures_tactic="crash", futures_max_positions=4, futures_extra_size_pct=16.7)
    bot, _fake, notes, _clock = make_fbot(cfg, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert {f"kv:fw:FUTURES_EXTRA_HOLD_MINUTES:{v}" for v in (0, 30, 35)} <= set(data)
    assert {f"kv:fw:FUTURES_WAVE_HOLD_MINUTES:{v}" for v in (0, 60, 70)} <= set(data)
    assert "kv:fw:FUTURES_EXTRA_BE_TRIGGER_PCT:0.4" in data                       # Araştırmadaki ek başabaş değeri
    assert all(len(d.encode()) <= 64 for d in data) and "Faz 9 testi" in text and len(text) < 4096
    for key, value in (("FUTURES_EXTRA_HOLD_MINUTES", 35), ("FUTURES_WAVE_HOLD_MINUTES", 60)):
        press(bot, f"kv:fw:{key}:{value}")
        if bot.commands.pending is not None:
            press(bot, f"yes:{bot.commands.pending.token}")
    assert (bot.cfg.futures_extra_hold_minutes, bot.cfg.futures_wave_hold_minutes) == (35, 60)
    assert "FUTURES_EXTRA_HOLD_MINUTES=35" in env.read_text() and "FUTURES_WAVE_HOLD_MINUTES=60" in env.read_text()
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    labels = [label for row in keyboard for label, _d in row]
    assert any(x.startswith("✅") and "ek zaman stopu: 35 dk" in x for x in labels)
    assert any(x.startswith("✅") and "dalgada ilk: 60 dk" in x for x in labels)
    assert "Zaman stopu (Faz 9): ek pozisyonlar 35 dk, ≥8 coin düşerken açılan ilk pozisyon 60 dk" in text
    assert len(text) < 4096
    # ⭐ Faz 6 seti saf Faz 6'dır: Faz 9'u da kapatır
    press(bot, "fcm:p6")
    press(bot, f"yes:{bot.commands.pending.token}")
    assert (bot.cfg.futures_extra_hold_minutes, bot.cfg.futures_wave_hold_minutes) == (0, 0)
    # Tek pozisyonda ek zaman stopu satırı gizli, dalga satırı görünür
    press(bot, "fcm:off")
    press(bot, f"yes:{bot.commands.pending.token}")
    press(bot, "pg:fw")
    data = [d for row in notes.menus[-1][1] for _l, d in row]
    assert not any("FUTURES_EXTRA_HOLD_MINUTES" in d for d in data)
    assert any("FUTURES_WAVE_HOLD_MINUTES" in d for d in data)
