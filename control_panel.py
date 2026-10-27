"""
Telegram kontrol paneli: /menu ile açılan düğmeli menü ve alt menüleri.

Düğmeler YALNIZCA .env'deki TELEGRAM_CHAT_ID sohbetinde çalışır (bkz. commands.TelegramCommandListener).
Ayar değişiklikleri doğrulanır, hemen uygulanır ve sunucudaki .env'ye kaydedilir (bkz. runtime_settings).
Satış / yeniden başlatma / strateji değişikliği gibi işlemler yine onay ister (COMMAND_PIN ayarlıysa /evet PIN).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from config import (LOG_DIR, PROFILE_ATTRS, PROFILES, STRATEGY_NAMES, Config, ConfigError, is_rotation,
                    strategy_family, with_strategy)
from logger import get_logger, redact
from runtime_settings import EDITABLE, apply_settings, env_values, find_key, format_value, write_env_values
from telegram_bot import LINE, Messages, esc, fmt_duration, fmt_signed, fmt_usdt

if TYPE_CHECKING:  # pragma: no cover
    from commands import CommandProcessor

log = get_logger()

RESTART_EXIT_CODE = 3          # systemd servisi (deploy/tradebot.service) bu çıkış kodunda botu ~15 sn sonra yeniden açar
LOG_TAIL_LINES = 15
BLACKLIST_HOURS = 24
# Tarama sürerken de hemen çalışan (yalnızca bilgi gösteren / menüde gezinen) düğmeler
READ_ONLY_CALLBACKS = frozenset({"menu", "st", "cfg", "cap", "strat", "ntf", "flt", "risk", "scan", "lat", "log",
                                 "ip", "x", "rst", "smart", "board", "dust", "bt"})
NOTIFY_LABELS = {"all": "Tümü", "closes": "Sadece Kapanışlar", "silent": "Sessiz Mod"}
NOTIFY_HELP = {"all": "her alım, satış, izleyen stop ve rapor",
               "closes": "yalnızca pozisyon kapanışları (alım ve periyodik raporlar gelmez)",
               "silent": "yalnızca acil durumlar (zarar kes, kill-switch, hatalar)"}
# Ayar düğmeleri: kısa ad → (ayar, adım)
STEPS = {"TP": ("TAKE_PROFIT_PCT", 0.5), "SL": ("STOP_LOSS_PCT", 0.5), "TR": ("TRAILING_ACTIVATION_PCT", 0.5),
         "POS": ("MAX_OPEN_POSITIONS", 1)}
CAPITAL_CHOICES = (100, 75, 50, 33)
FILTER_KEYS = ("BTC_FILTER", "SPREAD_FILTER", "FAKEOUT_FILTER", "VWAP_FILTER", "OVERHEAT_FILTER",
               "OB_WALL_FILTER", "OB_IMBALANCE_FILTER", "TAPE_FILTER")
SMART_BOOL_KEYS = ("ATR_TARGETS", "TP_EXTEND", "TRAILING_BUY", "LIQ_FEED", "RSI_DIV_ALERTS")
SMART_MODE_KEYS = ("DCA_MODE", "PYRAMID_MODE", "TIME_EXIT_MODE", "ESCAPE_MODE", "SIGNAL_APPROVAL")
MODE_LABELS = {"off": "kapalı", "ask": "sorarak", "auto": "otomatik", "volatile": "oynak piyasada", "all": "her sinyalde"}
DAILY_LIMIT_CHOICES = (0, 2, 3, 5)
STREAK_CHOICES = (0, 2, 3, 4)
STRATEGY_SHORT = {"rebound": "Rebound (5 dk dip)", "trend": "Trend (5 dk)", "breakout": "Breakout (5 dk)",
                  "swing": "Swing kırılım (1 saat)", "swing_pullback": "Swing geri çekilme (1 saat)",
                  "mean_revert": "Ortalamaya dönüş (1 saat)", "rotation": "Rotasyon (haftalık)"}


def _on(flag: bool) -> str:
    return "✅" if flag else "⬜"


def under_systemd() -> bool:
    """Bot systemd servisi olarak mı çalışıyor? (systemd her servise INVOCATION_ID verir)"""
    return bool(os.environ.get("INVOCATION_ID"))


def sudo_restart(service: str) -> Tuple[bool, str]:
    """`sudo -n systemctl --no-block restart SERVİS`. Şifresiz sudo izni yoksa (bkz. VPS_KURULUM.md) False döner."""
    systemctl = "/usr/bin/systemctl" if os.path.exists("/usr/bin/systemctl") else (shutil.which("systemctl")
                                                                                   or "systemctl")
    try:
        done = subprocess.run(["sudo", "-n", systemctl, "--no-block", "restart", service],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = (done.stderr or done.stdout or "").strip().splitlines()
    return done.returncode == 0, (out[0][:200] if out else f"çıkış kodu {done.returncode}")


def log_file_path() -> Path:
    for handler in get_logger().handlers + logging.getLogger().handlers:
        if isinstance(handler, RotatingFileHandler):
            return Path(handler.baseFilename)
    return LOG_DIR / "bot.log"


def tail_lines(path: Path, n: int, max_bytes: int = 64_000) -> List[str]:
    """Dosyanın son n satırı (büyük dosyada yalnızca sonu okunur)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [line for line in data.splitlines() if line.strip()]
    if size > max_bytes and lines:
        lines = lines[1:]   # İlk satır yarım okunmuş olabilir
    return lines[-n:]


class ControlPanel:
    def __init__(self, proc: "CommandProcessor"):
        self.proc = proc
        self._scan_thread: Optional[threading.Thread] = None
        from backtest_runner import BacktestMenu
        from market_scan import scan_market
        self.scan_fn = scan_market               # Testlerde değiştirilebilir
        self.backtest = BacktestMenu(proc)       # /backtest düğmeleri

    @property
    def bot(self):
        return self.proc.bot

    def _client_for_scan(self, cfg):
        from binance_client import BinanceClient
        return BinanceClient("", "", cfg.binance_base_url)   # Yalnızca herkese açık veri: anahtar gerekmez

    # ================================================================ düğme yönlendirme
    def handle_callback(self, cmd) -> None:
        action, args = cmd.args[0], cmd.args[1:]
        mid = cmd.message_id or None
        arg = args[0] if args else ""
        simple = {
            "menu": lambda: self.show_main(mid),
            "st": lambda: self.proc.cmd_durum([]),
            "pause": lambda: self._pause(True, mid),
            "resume": lambda: self._pause(False, mid),
            "cfg": lambda: self.show_settings(mid),
            "cap": lambda: self.show_capital(mid),
            "strat": lambda: self.show_strategies(mid),
            "ntf": lambda: self.show_notify(mid),
            "flt": lambda: self.show_filters(mid),
            "risk": lambda: self.show_risk(mid),
            "scan": self.market_scan,
            "lat": self.latency,
            "log": self.logs,
            "ip": self.ip_info,
            "rst": self.ask_restart,
            "x": lambda: self.proc.reply_menu("🎛️ Menü kapatıldı. Tekrar açmak için /menu", [], mid),
            "yes": lambda: self._confirm(arg, True),
            "no": lambda: self._confirm(arg, False),
            "unfreeze": lambda: self._unfreeze(mid),
            "smart": lambda: self.show_smart(mid),
            "board": lambda: self.bot.smart.board_off(mid) if arg == "off" else self.bot.smart.board_on(),
            "dust": self.bot.smart.show_dust,
            "q": lambda: self.bot.smart.answer(arg, args[1] if len(args) > 1 else "", mid),
            "bt": lambda: self.backtest.handle(args, mid),
        }
        if action in simple:
            simple[action]()
        elif action == "adj" and len(args) == 2:
            self.adjust(arg, args[1], mid)
        elif action == "capset":
            self._choose("CAPITAL_USAGE_PCT", arg, self.show_capital, mid)
        elif action == "ntfset":
            self._choose("NOTIFY_MODE", arg, self.show_notify, mid)
        elif action == "tg" and arg in FILTER_KEYS + SMART_BOOL_KEYS:
            view = self.show_smart if arg in SMART_BOOL_KEYS else self.show_filters
            self._choose(arg, format_value(not getattr(self.bot.cfg, EDITABLE[arg].attr)), view, mid)
        elif action == "cy" and arg in SMART_MODE_KEYS:
            choices = EDITABLE[arg].choices
            current = getattr(self.bot.cfg, EDITABLE[arg].attr)
            self._choose(arg, choices[(choices.index(current) + 1) % len(choices)], self.show_smart, mid)
        elif action == "dl":
            self._choose("DAILY_LOSS_LIMIT_PCT", arg, self.show_risk, mid)
        elif action == "ls":
            self._choose("LOSS_STREAK_LIMIT", arg, self.show_risk, mid)
        elif action == "stp":
            self.ask_strategy(arg)
        elif action in ("bl", "nobl"):
            self.blacklist(arg, action == "bl", mid)
        else:
            self.proc.reply("❓ Bu düğme artık geçerli değil. Güncel menü için /menu")

    # ================================================================ ana menü
    def show_main(self, mid: Optional[int] = None) -> None:
        bot, cfg = self.bot, self.bot.cfg
        state = bot.state
        if state.paused:
            status = "⏸️ Yeni alımlar durdurulmuş"
        elif bot.freeze_note():
            status = bot.freeze_note()
        else:
            status = "▶️ Alımlar açık"
        text = (f"🎛️ <b>Kontrol Paneli</b>\n{LINE}\n"
                f"Mod: {'🧪 TEST' if cfg.is_paper else '💵 CANLI'} | 🧭 {esc(STRATEGY_SHORT.get(cfg.strategy, cfg.strategy))}\n"
                f"{status}\n"
                f"📦 Açık pozisyon: {len(state.positions)}/{bot.target_slots()} | 💼 Sermaye: "
                f"{fmt_usdt(state.capital)} {cfg.quote_asset}\n"
                f"🔔 Bildirim: {NOTIFY_LABELS.get(cfg.notify_mode, cfg.notify_mode)}")
        keyboard = [
            [("📊 Durum", "st"), ("▶️ Devam", "resume") if state.paused else ("⏸️ Durdur", "pause")],
            [("⚙️ Ayarlar", "cfg"), ("💰 Sermaye", "cap")],
            [("🧭 Strateji", "strat"), ("🔔 Bildirim", "ntf")],
            [("🛡️ Filtreler", "flt"), ("🧯 Risk", "risk")],
            [("🧠 Akıllı Yönetim", "smart"), ("📈 Canlı Pano", "board")],
            [("🧪 Backtest", "bt")],
            [("🔎 Piyasayı Tara", "scan"), ("📶 Gecikme", "lat")],
            [("📜 Son Loglar", "log"), ("🌐 IP", "ip")],
            [("🧹 Toz → BNB", "dust"), ("🔄 Sunucu Reset", "rst")],
            [("✖️ Kapat", "x")],
        ]
        self.proc.reply_menu(text, keyboard, mid)

    def _confirm(self, token: str, yes: bool) -> None:
        """Onay düğmesi: yalnızca hâlâ bekleyen AYNI işlemi onaylar / iptal eder."""
        pending = self.proc.pending
        if pending is None or token != pending.token or self.proc.time_fn() > pending.expires:
            self.proc.reply("ℹ️ Bu onayın süresi doldu veya yerini başka bir işlem aldı.")
            return
        (self.proc.cmd_evet if yes else self.proc.cmd_hayir)([])

    def _pause(self, paused: bool, mid: Optional[int]) -> None:
        (self.proc.cmd_durdur if paused else self.proc.cmd_devam)([])
        self.show_main(mid)

    @staticmethod
    def _back() -> List[Tuple[str, str]]:
        return [("⬅️ Menü", "menu")]

    # ================================================================ ayar değiştirme
    def set_values(self, values: Dict[str, object]) -> Tuple[bool, str]:
        """Ayarları doğrular, bota uygular ve .env'ye yazar. (başarılı mı, mesaj)"""
        bot = self.bot
        try:
            new, notes = apply_settings(bot.cfg, values)
        except ConfigError as exc:
            return False, f"⚠️ {esc(str(exc).replace('Ayar hataları:', '').strip())}"
        saved = ""
        if bot.settings_path is not None:
            try:
                write_env_values(bot.settings_path, env_values(new, list(values)))
            except OSError as exc:
                saved = f"\n⚠️ .env dosyasına yazılamadı ({esc(type(exc).__name__)}); değişiklik yalnızca bot " \
                        f"yeniden başlayana kadar geçerli."
        else:
            saved = "\n<i>(Ayar dosyası yok: değişiklik bot yeniden başlayana kadar geçerli.)</i>"
        bot.apply_config(new)
        shown = ", ".join(f"{k}={format_value(getattr(new, EDITABLE[k].attr))}" for k in values)
        log.info("Ayar değiştirildi (Telegram): %s", shown)
        msg = f"✅ {esc(shown)}{saved}"
        if notes:
            msg += "\n" + "\n".join(f"⚠️ {esc(n)}" for n in notes)
        return True, msg

    def _choose(self, key: str, value: str, view, mid: Optional[int]) -> None:
        _ok, msg = self.set_values({key: value})
        view(mid, msg)

    def set_from_text(self, args) -> None:
        if len(args) < 2:
            keys = ", ".join(EDITABLE)
            self.proc.reply("Kullanım: /ayar ANAHTAR DEĞER  (örn. /ayar tp 2.5, /ayar FAKEOUT_FILTER acik)\n"
                            f"Değiştirilebilen ayarlar: {esc(keys)}\nStrateji için /menu → Strateji.")
            return
        key = find_key(args[0])
        if key is None:
            self.proc.reply(f"❓ {esc(args[0])} Telegram'dan değiştirilemez. Liste için /ayar")
            return
        _ok, msg = self.set_values({key: " ".join(args[1:])})
        self.proc.reply(msg)

    def adjust(self, short: str, direction: str, mid: Optional[int]) -> None:
        if short not in STEPS or direction not in ("+", "-"):
            self.show_settings(mid)
            return
        key, step = STEPS[short]
        current = getattr(self.bot.cfg, EDITABLE[key].attr)
        value = current + step if direction == "+" else current - step
        value = round(value, 4) if isinstance(step, float) else int(value)
        _ok, msg = self.set_values({key: value})
        self.show_settings(mid, msg)

    def show_settings(self, mid: Optional[int] = None, note: str = "") -> None:
        cfg = self.bot.cfg
        hold = fmt_duration(cfg.max_hold_minutes * 60, exact=True) if cfg.max_hold_minutes else "sınırsız"
        rot = "\n<i>Rotasyon stratejisinde Kâr Al ve izleyen stop kullanılmaz.</i>" if is_rotation(cfg.strategy) else ""
        text = (f"⚙️ <b>Dinamik Ayarlar</b>\n{LINE}\n"
                f"🎯 Kâr Al: +%{cfg.take_profit_pct:g}\n🛑 Zarar Kes: -%{cfg.stop_loss_pct:g}\n"
                f"📈 İzleyen stop: +%{cfg.trailing_activation_pct:g} → +%{cfg.trailing_floor_pct:g}\n"
                f"📦 En fazla pozisyon: {cfg.max_open_positions} (şu an {self.bot.target_slots()})\n"
                f"⏱️ Maks. süre: {hold}{rot}\n{LINE}\n"
                "<i>Değişiklik hemen uygulanır (açık pozisyonların hedefleri de) ve sunucudaki .env'ye kaydedilir. "
                "Diğer ayarlar için: /ayar ANAHTAR DEĞER</i>" + (f"\n\n{note}" if note else ""))
        keyboard = [
            [("🎯 TP −0.5", "adj:TP:-"), ("🎯 TP +0.5", "adj:TP:+")],
            [("🛑 SL −0.5", "adj:SL:-"), ("🛑 SL +0.5", "adj:SL:+")],
            [("📈 İz. −0.5", "adj:TR:-"), ("📈 İz. +0.5", "adj:TR:+")],
            [("📦 Poz. −1", "adj:POS:-"), ("📦 Poz. +1", "adj:POS:+")],
            self._back(),
        ]
        self.proc.reply_menu(text, keyboard, mid)

    def show_capital(self, mid: Optional[int] = None, note: str = "") -> None:
        bot, cfg = self.bot, self.bot.cfg
        pct = cfg.capital_usage_pct
        slots = bot.slots()
        per = bot.state.capital / slots if slots else 0.0
        reserve = ("kapalı: her pozisyona ayrılan payın tamamıyla girilir" if pct >= 100 else
                   f"açık: her pozisyona ayrılan payın %{pct:g} kadarıyla girilir, kalanı (%{100 - pct:g}) USDT olarak "
                   f"yedekte bekler")
        text = (f"💰 <b>Sermaye Yönetimi (Yedek Akçe)</b>\n{LINE}\n"
                f"İlk giriş yüzdesi: <b>%{pct:g}</b> — {reserve}\n"
                f"💼 Sermaye {fmt_usdt(bot.state.capital)} {cfg.quote_asset} | pozisyon hakkı {slots} | pozisyon "
                f"başına pay ~{fmt_usdt(per)}, girişte ~{fmt_usdt(per * pct / 100)} {cfg.quote_asset}\n"
                "<i>Yedek, riski azaltır. Yüzde düştükçe pozisyon tutarı küçülür; Binance'in en küçük emir tutarının "
                "(~5 USDT) altına inecekse bot pozisyon sayısını azaltır.</i>" + (f"\n\n{note}" if note else ""))
        row = [(("✅ " if abs(pct - c) < 0.01 else "") + ("Kapalı (%100)" if c == 100 else f"%{c}"), f"capset:{c}")
               for c in CAPITAL_CHOICES]
        self.proc.reply_menu(text, [row[:2], row[2:], self._back()], mid)

    def show_notify(self, mid: Optional[int] = None, note: str = "") -> None:
        mode = self.bot.cfg.notify_mode
        text = (f"🔔 <b>Bildirim Ayarı</b>\n{LINE}\n"
                + "\n".join(f"{'✅' if m == mode else '▫️'} <b>{NOTIFY_LABELS[m]}</b>: {NOTIFY_HELP[m]}"
                            for m in NOTIFY_LABELS)
                + "\n<i>Komut cevapları, uyarılar ve hatalar her modda gelir. Susturulan mesajlar loglara yazılır.</i>"
                + (f"\n\n{note}" if note else ""))
        keyboard = [[(("✅ " if m == mode else "") + label, f"ntfset:{m}")] for m, label in NOTIFY_LABELS.items()]
        self.proc.reply_menu(text, keyboard + [self._back()], mid)

    def show_filters(self, mid: Optional[int] = None, note: str = "") -> None:
        cfg = self.bot.cfg
        info = {
            "BTC_FILTER": f"BTC son 1 saatte sert düştüyse altcoin alınmaz (sınır -%{cfg.btc_max_1h_drop_pct:g})",
            "SPREAD_FILTER": f"alış-satış farkı fazlaysa alınmaz (sınır %{cfg.max_spread_pct:g})",
            "FAKEOUT_FILTER": "son mumun üst fitili gövdesinden uzunsa alınmaz",
            "VWAP_FILTER": f"fiyat VWAP'ın (son {cfg.vwap_bars} mum) altındaysa alınmaz",
            "OVERHEAT_FILTER": f"son {cfg.overheat_minutes} dk'da çok yükseldiyse alınmaz (sınır +%{cfg.overheat_pct:g})",
            "OB_WALL_FILTER": f"ilk +%{cfg.ob_band_pct:g}'de dev satış duvarı varsa alınmaz (canlı)",
            "OB_IMBALANCE_FILTER": f"defterde satış emirleri alışların {cfg.ob_imbalance_ratio:g} katıysa alınmaz "
                                   f"(canlı)",
            "TAPE_FILTER": f"son {cfg.tape_seconds} sn'de piyasa alımları zayıfsa alınmaz (canlı)",
        }
        lines = [f"{_on(getattr(cfg, EDITABLE[k].attr))} <b>{EDITABLE[k].label}</b>: {info[k]}" for k in FILTER_KEYS]
        text = (f"🛡️ <b>Giriş Filtreleri</b>\n{LINE}\n" + "\n".join(lines) +
                f"\n{LINE}\n<i>Filtre eklemek işlem sayısını azaltır. Hangisinin kârı artırdığını görmek için "
                f"sunucuda: python main.py --backtest --tara-ab (canlı filtreler geçmiş veriyle denenemez).</i>"
                + (f"\n\n{note}" if note else ""))
        buttons = [(f"{_on(getattr(cfg, EDITABLE[k].attr))} {EDITABLE[k].label.split(' (')[0]}", f"tg:{k}")
                   for k in FILTER_KEYS]
        keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)] + [self._back()]
        self.proc.reply_menu(text, keyboard, mid)

    def show_risk(self, mid: Optional[int] = None, note: str = "") -> None:
        bot, cfg = self.bot, self.bot.cfg
        q = cfg.quote_asset
        day_pnl = bot.today_pnl()
        daily = (f"%{cfg.daily_loss_limit_pct:g} (≈{fmt_usdt(bot.daily_loss_usdt())} {q})"
                 if cfg.daily_loss_limit_pct > 0 else "kapalı")
        target = f"%{cfg.daily_profit_target_pct:g}" if cfg.daily_profit_target_pct > 0 else "kapalı"
        streak = (f"üst üste {cfg.loss_streak_limit} zarar kes → {cfg.loss_streak_pause_hours:g} saat alım yok "
                  f"(şu an seri: {bot.state.sl_streak})" if cfg.loss_streak_limit > 0 else "kapalı")
        karne = (f"aynı coinde üst üste {cfg.coin_sl_limit} zarar kes → {BLACKLIST_HOURS} saat kara liste teklifi"
                 if cfg.coin_sl_limit > 0 else "kapalı")
        slip = (f"son 10 alımın ortalaması sınırı aşarsa uyarır (sınır %{cfg.slippage_alert_pct:g})"
                if cfg.slippage_alert_pct > 0 else "kapalı")
        frozen = bot.freeze_note()
        text = (f"🧯 <b>Risk Sigortaları</b>\n{LINE}\n"
                f"📉 Günlük zarar limiti: {daily} | bugün: {fmt_signed(day_pnl)} {q}\n"
                f"🎯 Günlük kâr hedefi: {target}\n🔁 Zarar serisi sigortası: {streak}\n"
                f"📒 Coin karnesi: {karne}\n💧 Kayma alarmı: {slip}\n"
                f"{frozen or '▶️ Alımlar dondurulmamış'}\n{LINE}\n"
                "<i>Sigortalar yalnızca YENİ alımları durdurur; açık pozisyonların takibi sürer. Diğerleri için: "
                "/ayar DAILY_PROFIT_TARGET_PCT 3, /ayar COIN_SL_LIMIT 0 ...</i>" + (f"\n\n{note}" if note else ""))
        dl = [(("✅ " if abs(cfg.daily_loss_limit_pct - c) < 0.01 else "") + ("Limit yok" if c == 0 else f"Gün -%{c}"),
               f"dl:{c}") for c in DAILY_LIMIT_CHOICES]
        ls = [(("✅ " if cfg.loss_streak_limit == c else "") + ("Seri yok" if c == 0 else f"{c} SL"), f"ls:{c}")
              for c in STREAK_CHOICES]
        keyboard = [dl, ls]
        if frozen:
            keyboard.append([("▶️ Dondurmayı kaldır", "unfreeze")])
        self.proc.reply_menu(text, keyboard + [self._back()], mid)

    def show_smart(self, mid: Optional[int] = None, note: str = "") -> None:
        cfg = self.bot.cfg
        mode = lambda key: MODE_LABELS.get(getattr(cfg, EDITABLE[key].attr), "?")  # noqa: E731
        reserve_note = ("\n⚠️ Yedek akçe kapalı (ilk giriş %100): DCA ve piramit için /menu → Sermaye'den ilk giriş "
                        "yüzdesini düşürün." if cfg.capital_usage_pct >= 100
                        and (cfg.dca_mode != "off" or cfg.pyramid_mode != "off") else "")
        liq = (f", uyarı eşiği {cfg.liq_alert_usdt:,.0f} USDT".replace(",", ".") if cfg.liq_alert_usdt > 0
               else ", uyarı kapalı")
        text = (f"🧠 <b>Akıllı Yönetim</b>\n{LINE}\n"
                f"{_on(cfg.atr_targets)} <b>ATR hedefleri</b>: Kâr Al = 1 saatlik ATR × {cfg.atr_tp_mult:g}, Zarar Kes = "
                f"ATR × {cfg.atr_sl_mult:g} (en fazla -%{cfg.stop_loss_pct:g})\n"
                f"{_on(cfg.tp_extend)} <b>Hedef uzatma</b>: Kâr Al'da momentum güçlüyse satma, zirveden "
                f"%{cfg.tp_extend_gap_pct:g} dönünce sat\n"
                f"➕ <b>DCA</b> ({mode('DCA_MODE')}): net -%{cfg.dca_trigger_pct:g}'de yedek akçeyle ekle\n"
                f"🔺 <b>Piramit</b> ({mode('PYRAMID_MODE')}): net +%{cfg.pyramid_trigger_pct:g} ve momentum güçlüyse "
                f"yedekle ekle, sonra zarara döndürmeden sat\n"
                f"⏳ <b>Süre sorusu</b> ({mode('TIME_EXIT_MODE')}): {cfg.time_exit_minutes} dk dolunca yataysa "
                f"başabaşta sat\n"
                f"🏃 <b>Erken kaçış</b> ({mode('ESCAPE_MODE')}): net -%{cfg.escape_trigger_pct:g}'de hacimli satış "
                f"baskısı varsa zarar kesi beklemeden çık\n"
                f"{_on(cfg.trailing_buy)} <b>Kademeli alım</b>: sinyalden sonra fiyat düştükçe bekle, dipten "
                f"%{cfg.trailing_buy_pct:g} dönünce al (en fazla {cfg.trailing_buy_minutes} dk)\n"
                f"🔔 <b>Sinyal onayı</b> ({mode('SIGNAL_APPROVAL')}): almadan önce Telegram'dan sor\n"
                f"{_on(cfg.liq_feed)} <b>Likidasyon akışı</b> (vadeli işlemler){liq}\n"
                f"{_on(cfg.rsi_div_alerts)} <b>RSI uyumsuzluğu bildirimi</b> (en hacimli 40 coin, 1 saatlik)"
                f"{reserve_note}\n{LINE}\n"
                "<i>sorarak = Telegram'dan sorar; otomatik = sormadan yapar. Otomatik hallerin geçmiş veriyle etkisi: "
                "python main.py --backtest --tara-ab</i>" + (f"\n\n{note}" if note else ""))
        mode_btn = lambda key, icon, name: (f"{icon} {name}: {mode(key)}", f"cy:{key}")  # noqa: E731
        bool_btn = lambda key, name: (f"{_on(getattr(cfg, EDITABLE[key].attr))} {name}", f"tg:{key}")  # noqa: E731
        keyboard = [
            [bool_btn("ATR_TARGETS", "ATR hedefleri"), bool_btn("TP_EXTEND", "Hedef uzatma")],
            [mode_btn("DCA_MODE", "➕", "DCA"), mode_btn("PYRAMID_MODE", "🔺", "Piramit")],
            [mode_btn("TIME_EXIT_MODE", "⏳", "Süre"), mode_btn("ESCAPE_MODE", "🏃", "Kaçış")],
            [bool_btn("TRAILING_BUY", "Kademeli alım"), mode_btn("SIGNAL_APPROVAL", "🔔", "Onay")],
            [bool_btn("LIQ_FEED", "Likidasyon"), bool_btn("RSI_DIV_ALERTS", "RSI uyumsuzluk")],
            self._back(),
        ]
        self.proc.reply_menu(text, keyboard, mid)

    def _unfreeze(self, mid: Optional[int]) -> None:
        self.bot.unfreeze()
        self.show_risk(mid, "▶️ Dondurma kaldırıldı; bir sonraki taramada alım yapılabilir.")

    # ================================================================ strateji seçimi
    def show_strategies(self, mid: Optional[int] = None) -> None:
        cfg = self.bot.cfg
        text = (f"🧭 <b>Strateji Seç</b>\n{LINE}\nŞu an: <b>{esc(STRATEGY_SHORT.get(cfg.strategy, cfg.strategy))}</b>\n"
                "Seçtiğiniz strateji onaydan sonra .env'ye kaydedilir ve bot yeniden başlatılır (~20 sn).\n"
                "<i>Farklı gruba (5 dk / 1 saat / rotasyon) geçerken o grubun çıkış ayarları da (Kâr Al, Zarar Kes...) "
                "yüklenir; eski grubun ayarları saklanır, geri dönünce aynen geri gelir. Hangi stratejinin kârlı "
                "olduğunu görmek için sunucuda: python main.py --backtest --turnuva</i>")
        keyboard = [[(("✅ " if name == cfg.strategy else "") + STRATEGY_SHORT.get(name, name), f"stp:{name}")]
                    for name in STRATEGY_NAMES]
        self.proc.reply_menu(text, keyboard + [self._back()], mid)

    def _strategy_plan(self, name: str) -> Tuple[Config, Dict[str, str], bool]:
        """(yeni ayarlar, .env'ye yazılacak değerler, kayıtlı ayarlar geri yüklendi mi). Geçersizse ConfigError."""
        bot, cfg = self.bot, self.bot.cfg
        new, _ = with_strategy(cfg, name)
        values = {"STRATEGY": name}
        restored = False
        if strategy_family(name) != strategy_family(cfg.strategy):
            saved = bot.state.family_settings.get(strategy_family(name))
            if saved:
                types = Config.__dataclass_fields__
                changes = {}
                for key, text in saved.items():
                    attr = PROFILE_ATTRS.get(key)
                    if attr is None:
                        continue
                    changes[attr] = int(float(text)) if types[attr].type == "int" else float(text)
                from dataclasses import replace
                new = replace(new, **changes)
                restored = True
            values.update({k: format_value(getattr(new, attr)) for k, attr in PROFILE_ATTRS.items()})
        new.validate()
        return new, values, restored

    def ask_strategy(self, name: str) -> None:
        bot = self.bot
        cfg = bot.cfg
        if name not in STRATEGY_NAMES:
            self.proc.reply("❓ Bilinmeyen strateji.")
            return
        if name == cfg.strategy:
            self.proc.reply(f"ℹ️ Zaten {esc(STRATEGY_SHORT.get(name, name))} kullanılıyor.")
            return
        if bot.settings_path is None:
            self.proc.reply("⚠️ Bu çalıştırmada ayar dosyası (.env) yok; strateji Telegram'dan değiştirilemez.")
            return
        if strategy_family(name) != strategy_family(cfg.strategy) and bot.state.positions:
            self.proc.reply("⚠️ Açık pozisyon varken farklı gruptaki (5 dk / 1 saat / rotasyon) bir stratejiye "
                            "geçilemez: açık pozisyonlar yeni stratejinin kurallarıyla yönetilirdi. Önce pozisyonların "
                            "kapanmasını bekleyin (veya /hepsinisat).")
            return
        try:
            new, values, restored = self._strategy_plan(name)
        except ConfigError as exc:
            self.proc.reply(f"⚠️ Bu stratejiye geçilemez: {esc(exc)}")
            return
        changed = [f"{k}={v}" for k, v in values.items() if k != "STRATEGY"
                   and v != format_value(getattr(cfg, PROFILE_ATTRS[k]))]
        detail = (f"\n🧭 {esc(STRATEGY_SHORT.get(cfg.strategy, cfg.strategy))} → "
                  f"<b>{esc(STRATEGY_SHORT.get(name, name))}</b>")
        if changed:
            source = "önceden saklanan ayarlar" if restored else "bu grubun önerilen ayarları"
            detail += f"\n⚙️ Çıkış ayarları ({source}): {esc(', '.join(changed))}"
        elif strategy_family(name) != strategy_family(cfg.strategy) and strategy_family(name) not in PROFILES:
            detail += ("\n⚠️ Bu grup için saklanmış çıkış ayarı yok; mevcut Kâr Al / Zarar Kes aynen kalır. Geçişten "
                       "sonra /menu → Ayarlar'dan kontrol edin.")
        detail += "\n🔄 Bot yeniden başlatılacak (~20 sn); açık pozisyonlar takipte kalır."
        from commands import PendingConfirm
        self.proc._ask(PendingConfirm("strategy", name), detail, "Strateji değiştirilsin mi?")

    def apply_strategy(self, name: str) -> None:
        bot, cfg = self.bot, self.bot.cfg
        if name == cfg.strategy or name not in STRATEGY_NAMES or bot.settings_path is None:
            return
        if strategy_family(name) != strategy_family(cfg.strategy) and bot.state.positions:
            self.proc.reply("⚠️ Bu arada pozisyon açıldı; farklı gruptaki stratejiye geçilmedi.")
            return
        try:
            _new, values, _restored = self._strategy_plan(name)
        except ConfigError as exc:
            self.proc.reply(f"⚠️ Strateji değiştirilemedi: {esc(exc)}")
            return
        old_family = strategy_family(cfg.strategy)
        if old_family != strategy_family(name):   # Geri dönünce aynı ayarlar gelsin
            bot.state.family_settings[old_family] = {k: format_value(getattr(cfg, attr))
                                                     for k, attr in PROFILE_ATTRS.items()}
            bot.store.save(bot.state)
        try:
            write_env_values(bot.settings_path, values)
        except OSError as exc:
            self.proc.reply(Messages.error(f".env dosyasına yazılamadı, strateji değişmedi: {type(exc).__name__}"))
            return
        log.info("Strateji değiştirildi (Telegram): %s → %s", cfg.strategy, name)
        self.proc.reply(f"✅ Strateji <b>{esc(STRATEGY_SHORT.get(name, name))}</b> olarak kaydedildi.")
        self.restart(f"strateji değişti: {name}")

    # ================================================================ yeniden başlatma
    def ask_restart(self) -> None:
        from commands import PendingConfirm
        open_note = (f"\n📦 {len(self.bot.state.positions)} açık pozisyon var: yeniden başlarken Binance'teki yedek "
                     f"stop emirleriyle korunur, bot açılınca takibe devam eder." if self.bot.state.positions else "")
        how = ("" if under_systemd() else
               "\n⚠️ Bot systemd servisi olarak çalışmıyor: yalnızca kapanır, elle açmanız gerekir.")
        self.proc._ask(PendingConfirm("restart"), open_note + how,
                       f"Bot servisi (<code>{esc(self.bot.cfg.service_name)}</code>) yeniden başlatılsın mı?")

    def restart(self, reason: str) -> None:
        bot = self.bot
        if not under_systemd():
            self.proc.reply("ℹ️ Bot systemd servisi olarak çalışmadığı için kendini yeniden başlatamaz. Yeni ayarlar "
                            "bot bir sonraki açılışta okunur: botu kapatıp (/kapat) yeniden açın.")
            return
        ok, detail = sudo_restart(bot.cfg.service_name)
        if ok:
            log.info("Servis yeniden başlatılıyor (sudo systemctl): %s", reason)
            self.proc.reply("🔄 Servis yeniden başlatılıyor (sudo systemctl restart). Bot ~20 sn içinde açılış "
                            "mesajı gönderir.")
            return
        log.info("sudo ile yeniden başlatılamadı (%s); bot %d çıkış koduyla kapanıp systemd tarafından yeniden "
                 "açılacak: %s", detail, RESTART_EXIT_CODE, reason)
        self.proc.reply("🔄 Bot yeniden başlatılıyor: kapanıp systemd tarafından ~15 sn sonra tekrar açılacak.")
        bot.stop(exit_code=RESTART_EXIT_CODE)

    # ================================================================ bilgi araçları
    def latency(self) -> None:
        bot = self.bot
        client = bot.exchange.client
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            try:
                client.ping()
            except Exception as exc:  # noqa: BLE001
                self.proc.reply(Messages.error(f"Binance'e ulaşılamadı: {exc}"))
                return
            samples.append((time.perf_counter() - started) * 1000)
        stream = bot.price_stream
        ws = ("⚡ Anlık fiyat akışı (WebSocket): bağlı" if stream is not None and stream.connected
              else "🔁 Anlık fiyat akışı yok: fiyatlar REST ile sorgulanıyor")
        offset = getattr(client, "time_offset_ms", 0)
        self.proc.reply(f"📶 <b>Ağ Gecikmesi</b>\nBinance API (3 ölçüm): en iyi <b>{min(samples):.0f} ms</b> | "
                        f"ortalama {sum(samples) / len(samples):.0f} ms | en kötü {max(samples):.0f} ms\n"
                        f"{ws}\n🕒 Sunucu saati farkı: {offset} ms\n"
                        "<i>Bu sunucudan Binance'e gidiş-dönüş süresi: 50 ms altı çok iyi, 300 ms üstü yavaş.</i>")

    def logs(self, n: int = LOG_TAIL_LINES) -> None:
        lines = tail_lines(log_file_path(), n)
        if not lines:
            self.proc.reply("📜 Log dosyası boş veya bulunamadı.")
            return
        body: List[str] = []
        size = 0
        for line in reversed(lines):   # Mesaj sınırına sığmazsa en eski satırlar atılır
            text = esc(redact(line))[:240]
            if size + len(text) > 3300:
                break
            body.insert(0, text)
            size += len(text) + 1
        self.proc.reply(f"📜 <b>Son {len(body)} log satırı</b>\n<pre>" + "\n".join(body) + "</pre>")

    def ip_info(self) -> None:
        from ip_monitor import IpMonitor
        monitor = self.bot.ip_monitor or IpMonitor(0)
        found = monitor.check_once()
        known = self.bot.state.public_ips
        lines = [f"{family}: <code>{esc(found.get(family) or known.get(family) or 'yok')}</code>"
                 for family in ("IPv4", "IPv6")]
        watch = (f"her {self.bot.cfg.ip_check_minutes} dk'da kontrol ediliyor, değişirse alarm gelir"
                 if self.bot.ip_monitor is not None else "izleme kapalı (IP_CHECK_MINUTES=0)")
        self.proc.reply("🌐 <b>Sunucunun dış IP adresi</b>\n" + "\n".join(lines) + f"\n<i>{watch}. Binance API "
                        "anahtarında IP kısıtlaması varsa sunucunun Binance'e bağlandığı adres orada ekli olmalı.</i>")

    def market_scan(self) -> None:
        if self._scan_thread is not None and self._scan_thread.is_alive():
            self.proc.reply("🔎 Piyasa taraması zaten sürüyor; sonuç birazdan gelecek.")
            return
        bot = self.bot
        cfg, rules, notifier = bot.cfg, dict(bot.exchange.rules), bot.notifier
        client = self._client_for_scan(cfg)
        scan_fn = self.scan_fn

        def run() -> None:
            try:
                text = scan_fn(client, rules, cfg)
            except Exception as exc:  # noqa: BLE001
                log.warning("Piyasa taraması başarısız: %s", exc)
                text = Messages.error(f"Piyasa taraması yapılamadı: {exc}")
            notifier.send(text)

        self.proc.reply("🔎 Hacme göre ilk 40 coin taranıyor (~15-30 sn). Bu sırada bot normal çalışmaya devam eder.")
        self._scan_thread = threading.Thread(target=run, name="market-scan", daemon=True)
        self._scan_thread.start()

    # ================================================================ coin karnesi
    def blacklist(self, symbol: str, yes: bool, mid: Optional[int]) -> None:
        bot = self.bot
        symbol = symbol.upper()
        if not symbol:
            return
        if not yes:
            self.proc.reply_menu(f"👍 {esc(symbol)} listede kalıyor.", [], mid)
            return
        until = bot.time_fn() + BLACKLIST_HOURS * 3600
        bot.state.cooldowns[symbol] = max(bot.state.cooldowns.get(symbol, 0.0), until)
        bot.store.save(bot.state)
        log.info("%s %d saat kara listeye alındı (Telegram)", symbol, BLACKLIST_HOURS)
        self.proc.reply_menu(f"⛔ <b>{esc(symbol)}</b> {BLACKLIST_HOURS} saat kara listeye alındı "
                             f"(bitiş {datetime.fromtimestamp(until):%d.%m %H:%M}; bu süre alınmayacak).", [], mid)
