"""Telegram ayrıntılı ayar sayfaları: vadeli işlem, kill-switch, risk sigortaları, raporlar (settings_menu)."""
import os
from dataclasses import replace

import pytest

import settings_menu as sm
from commands import CALLBACK, Command
from config import ConfigError, with_strategy
from control_panel import READ_ONLY_CALLBACKS
from risk import cooldown_minutes
from runtime_settings import EDITABLE, apply_settings, parse_value
from test_bot_flow import make_bot, tick
from test_futures import make_fbot

LEGACY = {"dl": "DAILY_LOSS_LIMIT_PCT", "ls": "LOSS_STREAK_LIMIT", "lcd": "LOSS_COOLDOWN_MINUTES",
          "ntfset": "NOTIFY_MODE", "lev": "FUTURES_LEVERAGE"}


def press(bot, data, message_id=7):
    bot.commands.handle(Command(CALLBACK, data.split(":"), kind="callback", message_id=message_id))


def buttons(keyboard):
    return [data for row in keyboard for _label, data in row]


def env_text(bot):
    return bot.settings_path.read_text(encoding="utf-8")


@pytest.fixture
def spot(cfg, fake_client, tmp_path):
    bot, _client, notes = make_bot(cfg, fake_client, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    os.chmod(env, 0o600)
    bot.settings_path = env
    return bot, notes


@pytest.fixture
def fut(cfg, tmp_path):
    fcfg, _ = with_strategy(replace(cfg, start_capital=30.0), "futures")
    fcfg.validate()
    bot, _fake, notes, _clock = make_fbot(fcfg, tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    return bot, notes


# ------------------------------------------------------------------ sayfalar ve düğmeler
@pytest.mark.parametrize("tactic", ["crash", "liq4"])
def test_every_page_renders_with_valid_buttons(fut, spot, tactic):
    for bot, notes in (fut, spot):
        if bot is fut[0] and tactic == "liq4":
            bot.apply_config(replace(bot.cfg, futures_tactic="liq4", futures_liq_source="both"))
        for pid in sm.PAGES:
            press(bot, f"pg:{pid}")
            text, keyboard, mid = notes.menus[-1]
            assert mid == 7 and sm.PAGES[pid].title.split(" ", 1)[1].split(" —")[0] in text
            for data in buttons(keyboard):
                assert len(data.encode()) <= 64, data           # Telegram callback_data sınırı
                if data.startswith("kv:"):
                    _, page, key, _raw = data.split(":")
                    assert page == pid and key in EDITABLE
                    assert key in {r.key for r in sm.page_rows(sm.PAGES[pid])}


def test_pressing_every_preset_applies_or_asks(fut):
    """Hazır değer düğmelerinin hepsi (vadeli, varsayılan ayarlarda) ya hemen uygulanır ya da onay ister."""
    bot, notes = fut
    original = bot.cfg
    seen = 0
    for pid in ("fut", "fe", "fx", "ks", "risk", "ntf"):
        press(bot, f"pg:{pid}")
        for data in buttons(notes.menus[-1][1]):
            head, *rest = data.split(":")
            if head == "kv" and rest[2] not in ("+", "-", "t", "n"):
                key = rest[1]
            elif head in LEGACY:
                key = LEGACY[head]
            else:
                continue
            bot.apply_config(original)
            bot.commands.pending = None
            press(bot, data)
            seen += 1
            text = notes.messages[-1]
            if data == "lev:20":            # 20x'te likidasyon ~%4.5: zarar kes %4 çok yakın (doğrulama reddeder)
                assert "likidasyon" in text and bot.cfg.futures_leverage == 2
            elif bot.commands.pending is not None:
                assert bot.commands.pending.action == "setting" and key in bot.commands.pending.values
                assert data in ("kv:ks:MAX_TOTAL_LOSS_PCT:50", "kv:ks:MAX_TOTAL_LOSS_PCT:60")
            else:
                assert f"✅ {key}=" in text, (data, text[-300:])
    bot.apply_config(original)
    assert seen > 50


def test_read_only_split():
    assert "pg" in READ_ONLY_CALLBACKS
    assert not {"kv", "fc", "ftac"} & READ_ONLY_CALLBACKS   # Ayar değiştirenler tarama bitince çalışır


def test_stale_or_foreign_buttons_are_rejected(fut):
    bot, notes = fut
    for data in ("kv:fx:BINANCE_API_KEY:x", "kv:ks:TAKE_PROFIT_PCT:5", "pg:yok", "kv:yok:TAKE_PROFIT_PCT:5",
                 "kv:fx:FUTURES_TACTIC:+", "ftac:xyz", "fc:B-T"):
        press(bot, data)
        assert "artık geçerli değil" in notes.messages[-1], data
    press(bot, "kv:ntf:QUIET_HOURS:9999-0000")
    assert "QUIET_HOURS" in notes.messages[-1] and bot.cfg.quiet_hours == ""
    assert "BINANCE_API_KEY=gizli" in env_text(bot) and "QUIET_HOURS" not in env_text(bot)


# ------------------------------------------------------------------ kill-switch
def test_kill_switch_tighten_loosen_and_refuse(spot):
    bot, notes = spot
    assert bot.cfg.max_total_loss_pct == 45 and bot.loss_limit == pytest.approx(4.95)
    press(bot, "kv:ks:MAX_TOTAL_LOSS_PCT:30")                   # Sıkılaştırma: hemen
    assert bot.cfg.max_total_loss_pct == 30 and "MAX_TOTAL_LOSS_PCT=30" in env_text(bot)
    assert bot.commands.pending is None and "−3.30" in notes.menus[-1][0]
    press(bot, "kv:ks:MAX_TOTAL_LOSS_PCT:50")                   # Gevşetme: onay ister
    pending = bot.commands.pending
    assert pending.action == "setting" and bot.cfg.max_total_loss_pct == 30
    assert "gevşetiliyor" in notes.menus[-1][0] and "−5.50" in notes.menus[-1][0]
    press(bot, f"yes:{pending.token}")
    assert bot.cfg.max_total_loss_pct == 50 and "MAX_TOTAL_LOSS_PCT=50" in env_text(bot)
    assert "Kill-switch" in notes.menus[-1][0] and "✅ MAX_TOTAL_LOSS_PCT=50" in notes.menus[-1][0]
    # Toplam zarar 2 USDT (%18): %10'luk limit hemen tetiklenirdi → reddedilir
    bot.state.realized_pnl = -2.0
    press(bot, "kv:ks:MAX_TOTAL_LOSS_PCT:10")
    assert "seçilemez" in notes.messages[-1] and bot.cfg.max_total_loss_pct == 50 and not bot.state.killed
    press(bot, "kv:ks:MAX_TOTAL_LOSS_PCT:-")                    # 50 → 45 (zarar 2 < 4.95)
    assert bot.cfg.max_total_loss_pct == 45
    bot.commands.handle(Command("ayar", ["kill", "80"]))        # /ayar ile gevşetme de onay ister
    assert bot.commands.pending.values == {"MAX_TOTAL_LOSS_PCT": "80"}
    bot.commands.handle(Command("hayir"))
    assert bot.cfg.max_total_loss_pct == 45
    bot.state.killed = True
    press(bot, "kv:ks:MAX_TOTAL_LOSS_PCT:40")
    assert "zaten tetiklenmiş" in notes.messages[-1] and bot.cfg.max_total_loss_pct == 45


def test_tightened_kill_switch_takes_effect_next_tick(spot):
    bot, notes = spot
    press(bot, "kv:ks:MAX_TOTAL_LOSS_PCT:20")                   # 11 USDT'nin %20'si = 2.2 USDT
    bot.state.realized_pnl = -2.3
    tick(bot)
    assert bot.state.killed and any("KILL" in m.upper() for m in notes.messages[-3:])


def test_kill_switch_confirm_rechecks_state(spot):
    """Onay beklerken zarar büyüdüyse onay da yeni limiti reddeder (hemen tetiklenmesin)."""
    bot, notes = spot
    press(bot, "kv:ks:MAX_TOTAL_LOSS_PCT:20")
    press(bot, "kv:ks:MAX_TOTAL_LOSS_PCT:30")
    token = bot.commands.pending.token
    bot.state.realized_pnl = -3.5                               # 11 USDT'nin %30'u = 3.3
    press(bot, f"yes:{token}")
    assert "seçilemez" in notes.messages[-1] and bot.cfg.max_total_loss_pct == 20


def test_legacy_usdt_kill_switch_converted(spot):
    bot, notes = spot
    bot.apply_config(replace(bot.cfg, legacy_loss_usdt=5.0))
    bot.settings_path.write_text("BINANCE_API_KEY=gizli\nMAX_TOTAL_LOSS_USDT=5\n", encoding="utf-8")
    press(bot, "pg:ks")
    assert "MAX_TOTAL_LOSS_USDT=5" in notes.menus[-1][0]
    press(bot, "kv:ks:MAX_TOTAL_LOSS_PCT:30")                   # 3.3 < 5 USDT: sıkılaştırma
    assert bot.cfg.legacy_loss_usdt is None and bot.loss_limit == pytest.approx(3.3)
    text = env_text(bot)
    assert "MAX_TOTAL_LOSS_USDT" not in text and "MAX_TOTAL_LOSS_PCT=30" in text and "BINANCE_API_KEY=gizli" in text


# ------------------------------------------------------------------ vadeli
def test_tactic_switch_asks_and_loads_exits(fut):
    bot, notes = fut
    press(bot, "ftac:liq4")
    pending = bot.commands.pending
    assert pending.action == "setting" and pending.values["TAKE_PROFIT_PCT"] == "1.2"
    assert bot.cfg.futures_tactic == "crash" and "7 yılın 7'sinde" in notes.menus[-1][0]
    press(bot, f"yes:{pending.token}")
    cfg = bot.cfg
    assert (cfg.futures_tactic, cfg.take_profit_pct, cfg.stop_loss_pct, cfg.max_hold_minutes,
            cfg.breakeven_trigger_pct, cfg.breakeven_floor_pct, cfg.loss_cooldown_minutes) == (
        "liq4", 1.2, 0.9, 60, 0.5, 0.2, 0)
    text, keyboard, _ = notes.menus[-1]
    assert "4'lü onay" in text and "kv:fe:FUTURES_SIDES:long" in buttons(keyboard)
    assert "FUTURES_TACTIC=liq4" in env_text(bot) and "TAKE_PROFIT_PCT=1.2" in env_text(bot)
    press(bot, "kv:fx:TAKE_PROFIT_PCT:+")                       # liq4: 0.1'lik adım
    assert bot.cfg.take_profit_pct == pytest.approx(1.3)
    press(bot, "ftac:crash")
    press(bot, f"yes:{bot.commands.pending.token}")
    assert (bot.cfg.futures_tactic, bot.cfg.take_profit_pct, bot.cfg.stop_loss_pct) == ("crash", 6.0, 4.0)
    press(bot, "kv:fx:TAKE_PROFIT_PCT:+")                       # crash: 0.5'lik adım
    assert bot.cfg.take_profit_pct == pytest.approx(6.5)
    press(bot, "ftac:crash")
    assert "zaten seçili" in notes.menus[-1][0] and bot.commands.pending is None


def test_exchange_stop_disable_needs_confirmation(fut):
    bot, notes = fut
    press(bot, "kv:fx:FUTURES_EXCHANGE_STOP:t")
    assert bot.commands.pending is not None and "korumasız" in notes.menus[-1][0]
    press(bot, f"no:{bot.commands.pending.token}")
    assert bot.cfg.futures_exchange_stop
    press(bot, "kv:fx:FUTURES_EXCHANGE_STOP:t")
    press(bot, f"yes:{bot.commands.pending.token}")
    assert not bot.cfg.futures_exchange_stop and "FUTURES_EXCHANGE_STOP=false" in env_text(bot)
    press(bot, "kv:fx:FUTURES_EXCHANGE_STOP:t")                 # Açmak (daha güvenli) onaysız
    assert bot.cfg.futures_exchange_stop and bot.commands.pending is None


def test_coin_toggles(fut):
    bot, notes = fut
    press(bot, "fc:AVAX")
    press(bot, "fc:BTC")
    coins = bot.cfg.futures_coins.split(",")
    assert "AVAX" in coins and "BTC" not in coins and f"FUTURES_COINS={bot.cfg.futures_coins}" in env_text(bot)
    text, keyboard, _ = notes.menus[-1]
    assert "(10 seçili)" in text and "fc:default" in buttons(keyboard)
    press(bot, "fc:default")
    assert bot.cfg.futures_coins == sm.DEFAULT_FUTURES_COINS
    bot.apply_config(replace(bot.cfg, futures_coins="PEPE"))    # Listede olmayan coin de gösterilir
    press(bot, "pg:fc")
    assert "fc:PEPE" in buttons(notes.menus[-1][1])
    press(bot, "fc:PEPE")
    assert "En az bir coin" in notes.menus[-1][0] and bot.cfg.futures_coins == "PEPE"


def test_leverage_and_margin_on_futures_hub(fut):
    bot, notes = fut
    press(bot, "cfg")
    data = buttons(notes.menus[-1][1])
    assert {"lev:2", "lev:20", "adj:LEV:+", "pg:fe", "pg:fx", "pg:fc", "pg:ks", "risk"} <= set(data)
    press(bot, "kv:fut:FUTURES_MARGIN_USAGE_PCT:50")
    assert bot.cfg.futures_margin_usage_pct == 50 and "FUTURES_MARGIN_USAGE_PCT=50" in env_text(bot)
    assert "Dinamik Ayarlar — Vadeli" in notes.menus[-1][0]
    press(bot, "lev:3")
    assert bot.cfg.futures_leverage == 3 and "3x" in notes.menus[-1][0]


def test_futures_validation_ignores_spot_only_rules(cfg):
    fcfg, _ = with_strategy(cfg, "futures")
    replace(fcfg, take_profit_pct=3.0).validate()               # İzleyen stop (5) vadelide kullanılmaz
    replace(fcfg, stop_loss_pct=9.0).validate()                 # Spot yedek stopu (8) da
    with pytest.raises(ConfigError):
        replace(cfg, take_profit_pct=cfg.trailing_activation_pct).validate()
    with pytest.raises(ConfigError):
        replace(cfg, stop_loss_pct=cfg.backup_stop_pct).validate()


# ------------------------------------------------------------------ bekleme, raporlar, spot çıkışları
def test_symbol_cooldown_button_feeds_cooldown_rule(spot):
    bot, _ = spot
    press(bot, "kv:risk:SYMBOL_COOLDOWN_MINUTES:15")
    assert bot.cfg.symbol_cooldown_minutes == 15 and cooldown_minutes(bot.cfg, 1.0) == 15
    press(bot, "kv:risk:COIN_SL_LIMIT:0")
    press(bot, "kv:risk:DAILY_PROFIT_TARGET_PCT:5")
    assert bot.cfg.coin_sl_limit == 0 and bot.cfg.daily_profit_target_pct == 5
    with pytest.raises(ConfigError):
        apply_settings(bot.cfg, {"SYMBOL_COOLDOWN_MINUTES": "-5"})


def test_reports_and_quiet_hours_apply_live(spot):
    bot, notes = spot
    press(bot, "kv:ntf:QUIET_HOURS:2300-0800")
    assert bot.cfg.quiet_hours == "23:00-08:00" and bot.notifier.quiet_window == (1380, 480)
    assert "QUIET_HOURS=23:00-08:00" in env_text(bot)
    press(bot, "kv:ntf:DAILY_REPORT_TIME:off")
    assert bot.cfg.daily_report_time == "" and "DAILY_REPORT_TIME=\n" in env_text(bot)
    press(bot, "kv:ntf:STATUS_REPORT_MINUTES:0")
    assert bot.next_status_at == float("inf")
    press(bot, "kv:ntf:STATUS_REPORT_MINUTES:240")
    assert bot.next_status_at == pytest.approx(bot.time_fn() + 240 * 60, abs=5)
    press(bot, "kv:ntf:WEEKLY_REPORT:t")
    assert not bot.cfg.weekly_report
    bot.commands.handle(Command("ayar", ["DAILY_REPORT_TIME", "2130"]))
    assert bot.cfg.daily_report_time == "21:30"
    assert parse_value("QUIET_HOURS", "kapalı") == "" and parse_value("DAILY_REPORT_TIME", "7:05") == "07:05"


def test_spot_exit_details(spot):
    bot, notes = spot
    press(bot, "cfg")
    assert "pg:sx" in buttons(notes.menus[-1][1]) and "pg:ks" in buttons(notes.menus[-1][1])
    press(bot, "kv:sx:MAX_HOLD_MINUTES:240")
    press(bot, "kv:sx:MAX_HOLD_MINUTES:+")
    assert bot.cfg.max_hold_minutes == 300
    bot.apply_config(replace(bot.cfg, max_hold_minutes=0))
    press(bot, "kv:sx:MAX_HOLD_MINUTES:-")                      # 0'ın altına inmez
    assert "sınırda" in notes.menus[-1][0] and bot.cfg.max_hold_minutes == 0
    press(bot, "kv:sx:BACKUP_STOP_PCT:+")
    assert bot.cfg.backup_stop_pct == 13 and "BACKUP_STOP_PCT=13" in env_text(bot)
    bot.apply_config(replace(bot.cfg, backup_stop_pct=bot.cfg.stop_loss_pct + 1))
    press(bot, "kv:sx:BACKUP_STOP_PCT:-")                       # Zarar kesten büyük kalmalı
    assert "BACKUP_STOP_PCT" in notes.menus[-1][0] and bot.cfg.backup_stop_pct == bot.cfg.stop_loss_pct + 1
    press(bot, "kv:sx:BREAKEVEN_ENABLED:t")
    assert not bot.cfg.breakeven_enabled
    assert "kv:sx:BREAKEVEN_TRIGGER_PCT:+" not in buttons(notes.menus[-1][1])   # Kapalıyken ayarı gizli
