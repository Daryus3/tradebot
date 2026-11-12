"""Faz 8: dalga modunda ek pozisyonların kendi başabaş tetiği (FUTURES_EXTRA_BE_TRIGGER_PCT). Ayar, backtest motoru,
rapor satırları, canlı / test modu ve Telegram; varsayılanda (0) ve tek pozisyonda eski davranış aynen, Faz 5 / Faz 6
setleri Faz 8'i kapatır."""
from dataclasses import replace

import pytest

import futures_backtest as fb
from config import (FUTURES_PHASE8_BE_TRIGGER, FUTURES_PHASE8_OFF, ConfigError, _setting_notes, futures_be_trigger,
                    load_config)
from risk import ExitReason
from test_futures import BASE_MS, FakeCcxt, fcfg, make_fbot, params, window  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, bt_data, bt_sig, crashing, flat, live_cfg, press

MIN = 60_000


def p6cfg(fcfg, **over):
    base = dict(futures_max_positions=4, futures_leverage=3, futures_extra_size_pct=16.7, futures_reentry_breadth=3,
                futures_wave_rules="1:5,4:8", futures_isolated_size_pct=50.0, breakeven_trigger_pct=1.0,
                breakeven_floor_pct=0.3)
    base.update(over)
    return replace(fcfg, **base)


# ====================================================================== ayar
def test_phase8_setting_defaults_validation_env_helper_notes(fcfg, monkeypatch, tmp_path):
    assert fcfg.futures_extra_be_trigger_pct == 0.0 and FUTURES_PHASE8_OFF == {"FUTURES_EXTRA_BE_TRIGGER_PCT": 0}
    assert futures_be_trigger(fcfg, 0) == futures_be_trigger(fcfg, 3) == fcfg.breakeven_trigger_pct
    on = p6cfg(fcfg, futures_extra_be_trigger_pct=0.5)
    on.validate()
    assert futures_be_trigger(on, 0) == 1.0 and futures_be_trigger(on, 1) == futures_be_trigger(on, 3) == 0.5
    assert futures_be_trigger(replace(on, futures_max_positions=1), 1) == 1.0          # Tek pozisyon: ek pozisyon yok
    for bad in (-0.5, 11.0, 0.3, 0.2):                                                 # 0.3 / 0.2: tabandan büyük değil
        with pytest.raises(ConfigError):
            replace(on, futures_extra_be_trigger_pct=bad).validate()
    monkeypatch.setenv("FUTURES_EXTRA_BE_TRIGGER_PCT", "0.5")
    assert load_config(env_file=str(tmp_path / "x.env"), mode_override="paper").futures_extra_be_trigger_pct == 0.5
    notes = [n for n in _setting_notes(on) if "Faz 8" in n]
    assert len(notes) == 1 and "+%0.5" in notes[0] and "925" in notes[0] and "DÜZELTME" in notes[0]
    assert "kâr artmaz" in notes[0] and "Daha çok işlem isteniyorsa açık" in notes[0]   # Rapor 15 + çok çekilişli test
    assert "etkisiz" in " ".join(_setting_notes(replace(on, futures_max_positions=1)))
    assert "etkisiz" in " ".join(_setting_notes(replace(on, breakeven_enabled=False)))
    assert "küçük değil" in " ".join(_setting_notes(replace(on, futures_extra_be_trigger_pct=1.5)))
    assert not any("Faz 8" in n for n in _setting_notes(p6cfg(fcfg)))
    assert FUTURES_PHASE8_BE_TRIGGER == fb.PHASE8_BE == 0.5


# ====================================================================== backtest motoru
def dip_window(start, bars=50):
    """+%0.6'yı görüp (başabaş +%0.5 tetiklenir, +%1 tetiklenmez) -%1'e inen ve orada kalan 1 dk mumlar."""
    rows = [(100, 100.05, 99.95, 100.0)] * 2 + [(100, 100.65, 99.98, 100.6), (100.6, 100.6, 99.0, 99.1)] \
        + [(99.1, 99.15, 99.05, 99.1)] * bars
    return window(rows, start=start)


def test_backtest_extra_slot_uses_own_breakeven_trigger():
    data = bt_data()
    a, b = bt_sig("AAAUSDT", BASE_MS, 12), bt_sig("BBBUSDT", BASE_MS + 5 * MIN, 12)
    w = {(s.symbol, s.close_ms): dip_window(s.close_ms) for s in (a, b)}
    p = params(start_capital=100.0, tp_pct=5.0, sl_pct=3.0, be_trigger_pct=1.0, be_floor_pct=0.3, hold_min=30,
               max_positions=2, extra_breadth=8)
    off = fb.simulate(p, data, [a, b], w)
    assert [(t.slot, t.reason) for t in off.trades] == [(1, "TIME"), (2, "TIME")] and all(t.pnl < 0 for t in off.trades)
    on = fb.simulate(replace(p, extra_be_trigger_pct=0.5), data, [a, b], w)
    first, extra = on.trades
    assert (first.slot, first.reason, extra.slot, extra.reason) == (1, "TIME", 2, "BE")
    assert first.pnl == pytest.approx(off.trades[0].pnl) and extra.exit == pytest.approx(100.3) and extra.pnl > 0
    # Sabit tutarda da aynı kural; başabaş kapalıyken ve tek pozisyonda etkisiz
    fixed = fb.simulate(replace(p, extra_be_trigger_pct=0.5), data, [a, b], w, fixed_notional=50.0)
    assert [t.reason for t in fixed.trades] == ["TIME", "BE"]
    no_be = fb.simulate(replace(p, extra_be_trigger_pct=0.5, be_enabled=False), data, [a, b], w)
    assert [t.reason for t in no_be.trades] == ["TIME", "TIME"]
    single = replace(p, max_positions=1)
    assert fb.simulate(replace(single, extra_be_trigger_pct=0.5), data, [a, b], w).final_equity \
        == fb.simulate(single, data, [a, b], w).final_equity
    assert fb.phase8_active(replace(p, extra_be_trigger_pct=0.5)) and not fb.phase8_active(p)
    assert not fb.phase8_active(replace(p, extra_be_trigger_pct=1.0))                 # Ana tetikle aynı: etkisiz


def test_params_from_cfg_and_report_rows(fcfg):
    on = p6cfg(fcfg, futures_extra_be_trigger_pct=0.5)
    assert fb.params_from_cfg(on).extra_be_trigger_pct == 0.5 and fb.params_from_cfg(fcfg).extra_be_trigger_pct == 0.0
    calls = []

    def ev(s=None, **over):
        calls.append(over)
        return over
    rows = fb.phase8_rows(p6cfg(fcfg), ev)
    assert rows == [("Ek pozisyonlarda başabaş +%0.5 (Faz 8)", {"extra_be_trigger_pct": 0.5})]
    rows = fb.phase8_rows(on, ev)
    assert rows[0][0].startswith("Ek pozisyon başabaşı ana ayarla aynı (+%1") and rows[0][1] == {"extra_be_trigger_pct": 0.0}
    assert fb.phase8_rows(fcfg, ev) == []                                              # Tek pozisyon: satır yok
    assert fb.phase8_rows(p6cfg(fcfg, breakeven_enabled=False), ev) == []
    assert fb.phase8_rows(p6cfg(fcfg, breakeven_trigger_pct=0.5), ev) == []            # Ana tetik zaten ≤ 0.5
    label = fb.cascade_label(fb.params_from_cfg(on))
    assert "ek pozisyonlarda başabaş +%0.5 (Faz 8; ilk pozisyonda +%1)" in label
    assert "Faz 8" not in fb.cascade_label(fb.params_from_cfg(p6cfg(fcfg)))
    # Faz 5 / Faz 6 satırları saf kalır: Faz 8 kapalı hesaplanır; Faz 8 açıkken "⭐ Faz 6 seti" satırı yine gösterilir
    assert fb.PHASE6_SIM["extra_be_trigger_pct"] == 0.0
    labels = [r[0] for r in fb.phase6_rows(on, ev, [], phase6=lambda **o: o)]
    assert any(x.startswith("⭐ Faz 6 seti") for x in labels)
    pure = [r[0] for r in fb.phase6_rows(p6cfg(fcfg), ev, [], phase6=lambda **o: o)]
    assert not any(x.startswith("⭐ Faz 6 seti") for x in pure)
    calls.clear()
    fb.cascade_rows(replace(fcfg, futures_extra_be_trigger_pct=0.5), lambda **over: calls.append(over) or over)
    assert calls[0]["max_positions"] == 2 and calls[0]["extra_be_trigger_pct"] == 0.0


# ====================================================================== canlı / test modu
def wave_closes():
    return {"SOLUSDT": crashing(), "ADAUSDT": crashing(drop=7.0), "XRPUSDT": crashing(drop=6.5),
            "DOGEUSDT": flat(), "BTCUSDT": flat()}


def wave_bot(fcfg, tmp_path, live=False, fake=None, **over):
    cfg = live_cfg(fcfg, futures_max_positions=4, futures_extra_breadth=3, futures_extra_size_pct=16.7,
                   breakeven_trigger_pct=1.0, breakeven_floor_pct=0.3, **over)
    return make_fbot(cfg, tmp_path, live=live, fake=fake or KlineCcxt(wave_closes()))


def test_paper_extra_positions_move_stop_at_own_trigger(fcfg, tmp_path):
    bot, fake, notes, clock = wave_bot(fcfg, tmp_path, futures_extra_be_trigger_pct=0.5)
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions
    assert len(pos) == 3
    first, extra = pos["SOLUSDT"], pos["ADAUSDT"]                       # En güçlü sinyal ilk pozisyon
    assert "be_trigger" not in first.metrics and extra.metrics["be_trigger"] == 0.5
    assert pos["XRPUSDT"].metrics["be_trigger"] == 0.5
    assert bot.levels(extra)["trail_on"] == pytest.approx(extra.entry_price * 1.005)
    assert bot.levels(first)["trail_on"] == pytest.approx(first.entry_price * 1.01)
    opened = [m for m in notes.messages if "POZİSYON AÇILDI" in m]
    assert sum("(+%0.5, ek pozisyon)" in m for m in opened) == 2 and sum("(+%1)" in m for m in opened) == 1
    # Fiyat +%0.6: ek pozisyonun stopu girişin +%0.3'üne taşınır, ilk pozisyonunki yerinde kalır
    for sym in ("SOLUSDT", "ADAUSDT"):
        fake.prices[sym] = pos[sym].entry_price * 1.006
    bot.manage_positions(clock.t + 5)
    assert extra.breakeven_active and extra.sl_price == pytest.approx(extra.entry_price * 1.003, rel=1e-3)
    assert not first.breakeven_active and first.sl_price < first.entry_price
    fake.prices["SOLUSDT"] = first.entry_price * 1.011                  # İlk pozisyon kendi tetiğinde (+%1)
    bot.manage_positions(clock.t + 10)
    assert first.breakeven_active
    # Ek pozisyon stopa dönünce başabaşla kapanır
    fake.prices["ADAUSDT"] = extra.entry_price * 1.002
    bot.manage_positions(clock.t + 15)
    assert "ADAUSDT" not in bot.state.positions and bot.state.realized_pnl > 0
    assert "ek pozisyon başabaşı +%0.5" in bot.scan_report()


def test_default_keeps_one_trigger_for_all_positions(fcfg, tmp_path):
    bot, fake, _notes, clock = wave_bot(fcfg, tmp_path)
    bot.scan_and_enter(clock.t)
    pos = bot.state.positions
    assert len(pos) == 3 and not any("be_trigger" in p.metrics for p in pos.values())
    fake.prices["ADAUSDT"] = pos["ADAUSDT"].entry_price * 1.006
    bot.manage_positions(clock.t + 5)
    assert not pos["ADAUSDT"].breakeven_active
    assert "ek pozisyon başabaşı" not in bot.scan_report()


class RejectingCcxt(KlineCcxt):
    """Stop emri fiyatın gerisine konmak istenirse Binance'in -2021 hatasını verir (emir hemen tetiklenirdi)."""

    def create_order(self, sym, type_, side, amount, price=None, params=None):
        params = params or {}
        symbol = sym.split("/")[0] + "USDT"
        if type_ == "STOP_MARKET" and side == "sell" and float(params.get("stopPrice", 0)) >= self.prices[symbol]:
            self.calls.append(("rejected_stop", symbol, params.get("stopPrice")))
            raise Exception('binance {"code":-2021,"msg":"Order would immediately trigger."}')
        return super().create_order(sym, type_, side, amount, price, params)


def test_live_breakeven_stop_already_passed_closes_at_market(fcfg, tmp_path):
    """Dar aralık: stop konurken fiyat tabanın altına inmişse (Binance -2021) backtestteki gibi beklemeden çıkılır."""
    bot, fake, notes, clock = wave_bot(fcfg, tmp_path, live=True, fake=RejectingCcxt(wave_closes()),
                                       futures_extra_be_trigger_pct=0.5)
    bot.scan_and_enter(clock.t)
    extra = bot.state.positions["ADAUSDT"]
    old_stop = extra.sl_price
    entry = extra.entry_price
    real_last = bot.exchange.last_price
    seen = []

    def falling_last(symbol):                       # Bot +%0.6'yı görür; emir giderken fiyat +%0.1'e düşmüştür
        seen.append(symbol)
        price = real_last(symbol)
        if symbol == "ADAUSDT":
            fake.prices["ADAUSDT"] = entry * 1.001
        return price
    fake.prices["ADAUSDT"] = entry * 1.006
    bot.exchange.last_price = falling_last
    bot.manage_positions(clock.t + 5)
    assert "ADAUSDT" not in bot.state.positions                          # Piyasa emriyle kapandı
    assert any(c[0] == "rejected_stop" for c in fake.calls)
    closes = [c for c in fake.calls if c[0] == "create_order" and c[2] == "market" and c[6].get("reduceOnly")]
    assert len(closes) == 1 and old_stop < entry
    assert not any("taşınamadı" in m for m in notes.messages)            # "eski zarar kes duruyor" uyarısı yok
    assert bot._stop_passed == set()


# ====================================================================== Telegram
def test_wave_page_row_hint_and_presets_turn_phase8_off(fcfg, tmp_path):
    cfg = replace(fcfg, futures_tactic="crash", futures_max_positions=4, futures_extra_size_pct=16.7)
    bot, _fake, notes, _clock = make_fbot(cfg, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert {f"kv:fw:FUTURES_EXTRA_BE_TRIGGER_PCT:{v}" for v in ("0", "0.5", "0.6", "0.75")} <= set(data)
    assert all(len(d.encode()) <= 64 for d in data) and "DÜZELTME" in text and "kâr artmaz" in text and len(text) < 4096
    press(bot, "kv:fw:FUTURES_EXTRA_BE_TRIGGER_PCT:0.5")
    if bot.commands.pending is not None:
        press(bot, f"yes:{bot.commands.pending.token}")
    assert bot.cfg.futures_extra_be_trigger_pct == 0.5 and "FUTURES_EXTRA_BE_TRIGGER_PCT=0.5" in env.read_text()
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    labels = [label for row in keyboard for label, _d in row]
    assert any(label.startswith("✅") and "ek başabaş: +%0.5" in label for label in labels)
    assert "Ek pozisyon başabaşı (Faz 8)" in text
    # ⭐ Faz 6 seti saf Faz 6'dır: Faz 8'i kapatır; Faz 5 seti ve "kapat" da
    press(bot, "fcm:p6")
    press(bot, f"yes:{bot.commands.pending.token}")
    assert bot.cfg.futures_extra_be_trigger_pct == 0.0 and bot.cfg.futures_max_positions == 4
    press(bot, "pg:fw")
    assert any(label.startswith("✅ ⭐ Faz 6 seti") for row in notes.menus[-1][1] for label, _d in row)
    press(bot, "kv:fw:FUTURES_EXTRA_BE_TRIGGER_PCT:0.5")
    if bot.commands.pending is not None:
        press(bot, f"yes:{bot.commands.pending.token}")
    press(bot, "pg:fw")
    assert not any(label.startswith("✅ ⭐ Faz 6 seti") for row in notes.menus[-1][1] for label, _d in row)
    press(bot, "fcm:on")
    press(bot, f"yes:{bot.commands.pending.token}")
    assert bot.cfg.futures_extra_be_trigger_pct == 0.0 and bot.cfg.futures_max_positions == 2
    # Tek pozisyonda satır gizli
    press(bot, "fcm:off")
    press(bot, f"yes:{bot.commands.pending.token}")
    press(bot, "pg:fw")
    assert not any("FUTURES_EXTRA_BE_TRIGGER_PCT" in d for row in notes.menus[-1][1] for _l, d in row)
    assert ExitReason.BREAKEVEN is not None
