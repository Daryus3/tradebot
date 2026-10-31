"""Zarar kes uzatma (SL_EXTEND): zarar kese gelindiğinde dönüş işareti varsa zarar kes bir kez SL_EXTEND_PCT
genişler ve dönüş beklenir; uzatılmış zarar kese gelinirse satılır. Canlı ve backtest aynı kuralı kullanır."""
from dataclasses import replace

import pytest

import backtest
from config import ConfigError
from position_rules import REVERSAL_ROWS, reversal_signal
from risk import ExitReason, Position, position_sl

from test_backtest import btc_flat, make_client, no_pause, run, scenario, trend  # noqa: F401
from test_bot_flow import make_bot, tick
from test_smart import row


def falling_then(last_open, last_close, buyers=0.6, n=REVERSAL_ROWS):
    """Uzun düşüş (RSI aşırı satımda) + son mum: (açılış, kapanış), alıcı payı `buyers`."""
    rows, p = [], 100.0
    for i in range(n - 1):
        o, p = p, p * 0.995
        rows.append(row(o, o * 1.001, p * 0.999, p, t=i))
    rows.append(row(last_open * p, max(last_open, last_close) * p * 1.001, min(last_open, last_close) * p * 0.999,
                    last_close * p, qv=1000.0, tb=1000.0 * buyers, t=n))
    return rows


def test_reversal_signal_rules():
    assert "aşırı satımdan alıcılı dönüş" in reversal_signal(falling_then(1.0, 1.01), 1.5)
    assert reversal_signal(falling_then(1.0, 0.99), 1.5) is None              # Son mum kırmızı
    assert reversal_signal(falling_then(1.0, 1.01, buyers=0.4), 1.5) is None  # Satıcılar baskın
    assert reversal_signal(falling_then(1.0, 1.01)[-30:], 1.5) is None        # Veri az
    heavy = falling_then(1.0, 0.97, buyers=0.2)
    heavy[-1][7] = "9000"                                                     # Hacimli satış baskısı
    assert reversal_signal(heavy, 1.5) is None


def test_position_sl_includes_the_extension(cfg):
    pos = Position("X", "X", 1, 1, 10, 0.0)
    assert position_sl(pos, cfg) == cfg.stop_loss_pct
    pos.sl_extra_pct = 2.0
    assert position_sl(pos, cfg) == cfg.stop_loss_pct + 2.0


def test_config_keeps_the_extended_stop_above_the_backup_stop(cfg):
    replace(cfg, sl_extend=True, stop_loss_pct=4.0, sl_extend_pct=2.0, backup_stop_pct=12.0).validate()
    with pytest.raises(ConfigError):
        replace(cfg, sl_extend=True, stop_loss_pct=10.0, sl_extend_pct=2.0, backup_stop_pct=12.0).validate()
    with pytest.raises(ConfigError):
        replace(cfg, sl_extend_pct=0.0).validate()
    replace(cfg, sl_extend=False, stop_loss_pct=10.0, sl_extend_pct=2.0, backup_stop_pct=12.0).validate()


# ------------------------------------------------------------------ canlı
def live(cfg, fake_client, tmp_path, signal):
    bot, client, notes = make_bot(replace(cfg, sl_extend=True, stop_loss_pct=5.0, sl_extend_pct=2.0), fake_client,
                                  tmp_path)
    bot.smart.reversal = lambda symbol: signal
    tick(bot)
    assert bot.state.positions
    return bot, client, notes


def test_live_extends_once_and_sells_at_the_wider_stop(cfg, fake_client, tmp_path):
    bot, client, notes = live(cfg, fake_client, tmp_path, "aşırı satımdan alıcılı dönüş (test)")
    client.prices["ABCUSDT"] = 0.945                     # Net ≈ -%5.6: zarar kes (-%5) aşıldı
    tick(bot)
    pos = bot.state.positions["ABCUSDT"]
    assert pos.sl_extra_pct == 2.0 and any("ZARAR KES UZATILDI" in m and "-%7" in m for m in notes.messages)
    client.prices["ABCUSDT"] = 0.94
    tick(bot)
    assert bot.state.positions                           # Uzatılmış zarar kese (-%7) gelmedi: dönüş bekleniyor
    client.prices["ABCUSDT"] = 0.925                     # Net ≈ -%7.6
    tick(bot)
    assert not bot.state.positions
    assert sum("ZARAR KES UZATILDI" in m for m in notes.messages) == 1   # İkinci kez uzatılmaz


def test_live_without_a_reversal_sells_at_the_normal_stop(cfg, fake_client, tmp_path):
    bot, client, notes = live(cfg, fake_client, tmp_path, None)
    client.prices["ABCUSDT"] = 0.945
    tick(bot)
    assert not bot.state.positions and not any("ZARAR KES UZATILDI" in m for m in notes.messages)


def test_live_gap_below_the_wider_stop_is_not_extended(cfg, fake_client, tmp_path):
    bot, client, notes = live(cfg, fake_client, tmp_path, "test")
    client.prices["ABCUSDT"] = 0.90                      # Net ≈ -%10: uzatılmış seviyenin de altında
    tick(bot)
    assert not bot.state.positions and not any("ZARAR KES UZATILDI" in m for m in notes.messages)


# ------------------------------------------------------------------ backtest
def dip_then(rally):
    def path(p):
        down, p = trend(p, 0.99, 5)                        # Girişe göre ≈ -%4: zarar kes (-%3) aşılır, -%5 değil
        up, p = trend(p, 1.03, 8) if rally else trend(p, 0.99, 8)
        return down + up, p
    return path


def test_backtest_extension_waits_for_the_rebound(cfg, make_client, tmp_path, monkeypatch):
    base = replace(cfg, stop_loss_pct=3.0, take_profit_pct=5.0, breakeven_enabled=False,
                   trailing_activation_pct=4.9, trailing_floor_pct=4.0)
    client = make_client({"ABCUSDT": scenario(dip_then(rally=True)), "BTCUSDT": btc_flat()})
    stopped = run(base, client, tmp_path / "a")
    assert stopped.trades[0].reason == ExitReason.STOP_LOSS.value
    monkeypatch.setattr(backtest, "reversal_signal", lambda rows, ratio: None)
    assert run(replace(base, sl_extend=True), client, tmp_path / "b").trades[0].reason == ExitReason.STOP_LOSS.value
    seen = []
    monkeypatch.setattr(backtest, "reversal_signal", lambda rows, ratio: seen.append(len(rows)) or "test")
    held = run(replace(base, sl_extend=True), client, tmp_path / "c")
    assert held.trades[0].reason == ExitReason.TAKE_PROFIT.value and held.stats["zarar_kes_uzatma"] == 1
    assert seen == [REVERSAL_ROWS]                          # Canlıdaki gibi son 100 kapanmış mum, bir kez
    assert "zarar kes uzatma: 1" in backtest.format_backtest_report(held, replace(base, sl_extend=True))


def test_backtest_extension_costs_more_when_the_fall_continues(cfg, make_client, tmp_path, monkeypatch):
    base = replace(cfg, stop_loss_pct=3.0, take_profit_pct=5.0, breakeven_enabled=False,
                   trailing_activation_pct=4.9, trailing_floor_pct=4.0, sl_extend_pct=2.0)
    client = make_client({"ABCUSDT": scenario(dip_then(rally=False)), "BTCUSDT": btc_flat()})
    normal = run(base, client, tmp_path / "a").trades[0]
    monkeypatch.setattr(backtest, "reversal_signal", lambda rows, ratio: "test")
    wider = run(replace(base, sl_extend=True), client, tmp_path / "b").trades[0]
    assert wider.reason == normal.reason == ExitReason.STOP_LOSS.value
    assert wider.pnl_pct < normal.pnl_pct - 1.5                # Dönüş gelmedi: zarar ~2 puan daha büyük


def test_ab_test_has_the_row_and_fits_the_backup_stop(cfg):
    rows = [st for st, _ in backtest.ab_combos(replace(cfg, stop_loss_pct=4.0))]
    assert {"SL_EXTEND": True} in rows
    tight = replace(cfg, stop_loss_pct=10.0, backup_stop_pct=12.0, sl_extend_pct=2.0)
    fitted = next(st for st, _ in backtest.ab_combos(tight) if st.get("SL_EXTEND"))
    assert fitted == {"SL_EXTEND_PCT": 1.0, "SL_EXTEND": True}
    assert backtest._ab_label(tight, fitted) == "Zarar kes uzatma (dönüş işaretinde): kapalı → açık (+%1)"


def test_smart_menu_toggle_and_ayar(cfg, fake_client, tmp_path):
    """Varsayılan ayarlarla (zarar kes -%10, yedek stop -%12, pay %1.5) Telegram'dan açılıp kapanabilmeli."""
    from commands import Command
    from test_smart import last_buttons, press
    bot, _, notes = make_bot(cfg, fake_client, tmp_path)
    bot.settings_path = tmp_path / ".env"
    press(bot, "smart")
    assert "Zarar kes uzatma" in notes.menus[-1][0] and "tg:SL_EXTEND" in last_buttons(notes)
    assert "-%11.5" in notes.menus[-1][0]
    press(bot, "tg:SL_EXTEND")
    assert bot.cfg.sl_extend and "SL_EXTEND=true" in (tmp_path / ".env").read_text(encoding="utf-8")
    bot.commands.handle(Command("ayar", ["SL_EXTEND_PCT", "3"]))        # -%13: yedek stopun (-%12) altında
    assert bot.cfg.sl_extend_pct == 1.5 and "BACKUP_STOP_PCT" in notes.messages[-1]
    bot.commands.handle(Command("ayar", ["SL_EXTEND_PCT", "1"]))
    assert bot.cfg.sl_extend_pct == 1.0


def test_rotation_never_extends_its_disaster_stop(fake_client, tmp_path, monkeypatch, cfg):
    """Rotasyonda zarar kes yalnızca felaket korumasıdır: SL_EXTEND açık olsa da backtest canlıdaki gibi uzatmaz."""
    from test_rotation import N, DailyClient, _prepare, _start_ms, day_rows, path
    start = _start_ms()
    crash = path((211, 1.004), (N - 211, 0.95))          # İlk dengelemede alınır, sonra günde %5 düşer
    flat = path((N, 1.0))
    series = {"LEADUSDT": day_rows(crash, start), "NEXTUSDT": day_rows(flat, start),
              "FLATUSDT": day_rows(flat, start), "BTCUSDT": day_rows(path((N, 1.001), start=30_000.0), start, vol=5e9)}
    rot = replace(cfg, strategy="rotation", rotation_top_n=1, rotation_lookback_days=14, start_capital=11.0,
                  stop_loss_pct=10.0, backup_stop_pct=25.0)
    data = _prepare(rot, DailyClient(fake_client, series).client, tmp_path)
    normal = backtest.simulate(rot, data)
    assert any(t.reason == ExitReason.STOP_LOSS.value for t in normal.trades)      # Çöküşte zarar kes çalışır
    monkeypatch.setattr(backtest, "reversal_signal", lambda rows, ratio: "test")
    extended = backtest.simulate(replace(rot, sl_extend=True, sl_extend_pct=5.0), data)
    key = lambda r: [(t.symbol, t.open_time_ms, t.close_time_ms, t.reason, round(t.pnl, 8)) for t in r.trades]  # noqa
    assert key(extended) == key(normal) and not extended.stats.get("zarar_kes_uzatma")
