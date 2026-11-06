"""
Telegram menüsünden (/menu, /ayar) değiştirilebilen ayarlar.

- Yalnızca `EDITABLE` listesindeki ayarlar değiştirilebilir. API anahtarları, Telegram token'ı, çalışma modu ve
  sermaye Telegram'dan DEĞİŞTİRİLEMEZ. Kill-switch limiti (MAX_TOTAL_LOSS_PCT) değiştirilebilir ama gevşetmek onay
  ister ve şu anki zararın altına indirilemez (bkz. control_panel.ControlPanel.request_change).
- Yeni değer önce doğrulanır (Config.validate); geçerliyse çalışan bota hemen uygulanır ve sunucudaki .env
  dosyasına yazılır, böylece bot yeniden başlasa da kalır (.env, GitHub'daki ayarlar.env'yi ezer).
- .env'ye yazma atomiktir (önce geçici dosya, sonra yer değiştirme) ve dosya izinleri korunur. Dosyadaki diğer
  satırlara (API anahtarları dahil) dokunulmaz; içerik hiçbir yere yazdırılmaz / loglanmaz.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from config import (ACTION_MODES, FUTURES_CONFIRMS, FUTURES_LIQ_SOURCES, FUTURES_SIDES, FUTURES_TACTICS,
                    FUTURES_TP_ORDERS, NOTIFY_MODES, SIGNAL_APPROVAL_MODES, Config, ConfigError, _setting_notes,
                    format_crash_rules, parse_crash_rules)


@dataclass(frozen=True)
class Setting:
    attr: str           # Config alanı
    kind: str           # float | int | bool | choice | str | time (SS:DD) | trange (SS:DD-SS:DD) | rules (saat:%,…)
    label: str          # Telegram'da görünen ad
    choices: Tuple[str, ...] = ()


EDITABLE: Dict[str, Setting] = {
    # Çıkışlar ve pozisyon sayısı
    "TAKE_PROFIT_PCT": Setting("take_profit_pct", "float", "Kâr Al (%)"),
    "STOP_LOSS_PCT": Setting("stop_loss_pct", "float", "Zarar Kes (%)"),
    "TRAILING_ACTIVATION_PCT": Setting("trailing_activation_pct", "float", "İzleyen stop başlangıcı (%)"),
    "TRAILING_FLOOR_PCT": Setting("trailing_floor_pct", "float", "İzleyen stop tabanı (%)"),
    "MAX_HOLD_MINUTES": Setting("max_hold_minutes", "int", "Maks. tutma süresi (dk, 0 = sınırsız)"),
    "MAX_OPEN_POSITIONS": Setting("max_open_positions", "int", "En fazla pozisyon"),
    # Sermaye yönetimi (yedek akçe): her pozisyona ayrılan payın bu yüzdesiyle girilir, kalanı yedekte bekler
    "CAPITAL_USAGE_PCT": Setting("capital_usage_pct", "float", "İlk giriş yüzdesi (%)"),
    # Bildirimler
    "NOTIFY_MODE": Setting("notify_mode", "choice", "Bildirim modu", NOTIFY_MODES),
    # Giriş filtreleri
    "BTC_FILTER": Setting("btc_filter", "bool", "BTC sigortası"),
    "SPREAD_FILTER": Setting("spread_filter", "bool", "Spread kalkanı"),
    "FAKEOUT_FILTER": Setting("fakeout_filter", "bool", "Sahte yükseliş (fitil) filtresi"),
    "VWAP_FILTER": Setting("vwap_filter", "bool", "VWAP filtresi"),
    "OVERHEAT_FILTER": Setting("overheat_filter", "bool", "Aşırı ısınma filtresi"),
    "RSI_DIV_FILTER": Setting("rsi_div_filter", "bool", "RSI uyumsuzluğu teyidi"),
    "PRIOR_TREND_FILTER": Setting("prior_trend_filter", "bool", "Önceki trend (düşen bıçak) filtresi"),
    "OB_WALL_FILTER": Setting("ob_wall_filter", "bool", "Satış duvarı radarı"),
    "OB_IMBALANCE_FILTER": Setting("ob_imbalance_filter", "bool", "Emir defteri dengesizliği"),
    "TAPE_FILTER": Setting("tape_filter", "bool", "Tape okuma teyidi"),
    "MAX_SPREAD_PCT": Setting("max_spread_pct", "float", "Maks. spread (%)"),
    "BTC_MAX_1H_DROP_PCT": Setting("btc_max_1h_drop_pct", "float", "BTC 1 saatlik düşüş sınırı (%)"),
    "OVERHEAT_PCT": Setting("overheat_pct", "float", "Aşırı ısınma eşiği (%)"),
    "MAX_RETRACE": Setting("max_retrace", "float", "Rebound: düşüşün en fazla ne kadarı geri alınmış olabilir (0-1)"),
    "MIN_ROOM_RATIO": Setting("min_room_ratio", "float", "Hedef alanı oranı (zirveye kalan alan ÷ Kâr Al, 0 = kapalı)"),
    "MAX_24H_DROP_PCT": Setting("max_24h_drop_pct", "float", "24 saatte en fazla düşüş (%)"),
    "MIN_SIGNAL_SCORE": Setting("min_signal_score", "float", "En düşük sinyal puanı (0 = kapalı)"),
    # Risk sigortaları
    "DAILY_LOSS_LIMIT_PCT": Setting("daily_loss_limit_pct", "float", "Günlük zarar limiti (%, 0 = kapalı)"),
    "DAILY_PROFIT_TARGET_PCT": Setting("daily_profit_target_pct", "float", "Günlük kâr hedefi (%, 0 = kapalı)"),
    "LOSS_STREAK_LIMIT": Setting("loss_streak_limit", "int", "Üst üste zarar kes sigortası (0 = kapalı)"),
    "LOSS_STREAK_PAUSE_HOURS": Setting("loss_streak_pause_hours", "float", "Sigorta bekleme süresi (saat)"),
    "COIN_SL_LIMIT": Setting("coin_sl_limit", "int", "Coin karnesi: kara liste teklifi (0 = kapalı)"),
    "SLIPPAGE_ALERT_PCT": Setting("slippage_alert_pct", "float", "Kayma alarmı (ortalama %, 0 = kapalı)"),
    "LOSS_COOLDOWN_MINUTES": Setting("loss_cooldown_minutes", "int",
                                     "Zarardan sonra aynı coine bekleme (dk, 0 = normal bekleme)"),
    "SYMBOL_COOLDOWN_MINUTES": Setting("symbol_cooldown_minutes", "int", "Her çıkıştan sonra aynı coine bekleme (dk)"),
    # Kill-switch: gevşetmek (limiti büyütmek) onay ister; şu anki toplam zararın altına indirilemez
    "MAX_TOTAL_LOSS_PCT": Setting("max_total_loss_pct", "float", "Kill-switch: toplam zarar limiti (sermayenin %'si)"),
    # Çıkış ayrıntıları ve Binance'teki yedek stop
    "BREAKEVEN_ENABLED": Setting("breakeven_enabled", "bool", "Başabaş kalkanı"),
    "BACKUP_STOP_PCT": Setting("backup_stop_pct", "float", "Binance yedek stop (net zarar %, yeni pozisyonlarda)"),
    # Raporlar ve sessiz saatler
    "STATUS_REPORT_MINUTES": Setting("status_report_minutes", "int", "Durum raporu aralığı (dk, 0 = kapalı)"),
    "DAILY_REPORT_TIME": Setting("daily_report_time", "time", "Günlük özet saati (SS:DD, kapalı = gönderme)"),
    "WEEKLY_REPORT": Setting("weekly_report", "bool", "Haftalık özet (pazar)"),
    "QUIET_HOURS": Setting("quiet_hours", "trange", "Sessiz saatler (SS:DD-SS:DD, kapalı = yok)"),
    # Akıllı pozisyon yönetimi ve giriş
    "ATR_TARGETS": Setting("atr_targets", "bool", "ATR hedefleri"),
    "ATR_TP_MULT": Setting("atr_tp_mult", "float", "ATR Kâr Al çarpanı"),
    "ATR_SL_MULT": Setting("atr_sl_mult", "float", "ATR Zarar Kes çarpanı"),
    "TP_EXTEND": Setting("tp_extend", "bool", "Hedef uzatma"),
    "TP_EXTEND_GAP_PCT": Setting("tp_extend_gap_pct", "float", "Hedef uzatma geri dönüş payı (%)"),
    "DCA_MODE": Setting("dca_mode", "choice", "DCA (ortalama düşürme)", ACTION_MODES),
    "DCA_TRIGGER_PCT": Setting("dca_trigger_pct", "float", "DCA tetiği (net zarar %)"),
    "PYRAMID_MODE": Setting("pyramid_mode", "choice", "Piramit", ACTION_MODES),
    "PYRAMID_TRIGGER_PCT": Setting("pyramid_trigger_pct", "float", "Piramit tetiği (net kâr %)"),
    "TIME_EXIT_MODE": Setting("time_exit_mode", "choice", "Süre sorusu", ACTION_MODES),
    "TIME_EXIT_MINUTES": Setting("time_exit_minutes", "int", "Süre sorusu (dk)"),
    "ESCAPE_MODE": Setting("escape_mode", "choice", "Erken kaçış", ACTION_MODES),
    "ESCAPE_TRIGGER_PCT": Setting("escape_trigger_pct", "float", "Erken kaçış tetiği (net zarar %)"),
    "TRAILING_BUY": Setting("trailing_buy", "bool", "Kademeli alım"),
    "STALE_EXIT_MINUTES": Setting("stale_exit_minutes", "int", "Takılan işlem çıkışı (dk, 0 = kapalı)"),
    "SL_EXTEND": Setting("sl_extend", "bool", "Zarar kes uzatma (dönüş işaretinde bir kez)"),
    "SL_EXTEND_PCT": Setting("sl_extend_pct", "float", "Zarar kes uzatma payı (%)"),
    "STALE_EXIT_PEAK_PCT": Setting("stale_exit_peak_pct", "float", "Takılan işlem: beklenen en az net kâr (%)"),
    "TRAILING_BUY_PCT": Setting("trailing_buy_pct", "float", "Kademeli alım dönüş payı (%)"),
    "SIGNAL_APPROVAL": Setting("signal_approval", "choice", "Sinyal onayı", SIGNAL_APPROVAL_MODES),
    "LIQ_FEED": Setting("liq_feed", "bool", "Likidasyon akışı"),
    "LIQ_ALERT_USDT": Setting("liq_alert_usdt", "float", "Likidasyon uyarı eşiği (USDT, 0 = kapalı)"),
    "RSI_DIV_ALERTS": Setting("rsi_div_alerts", "bool", "RSI uyumsuzluğu bildirimi"),
    "RSI_DIV_TRADE": Setting("rsi_div_trade", "choice", "RSI uyumsuzluğu alımı (ana stratejinin yanında)",
                             ACTION_MODES),
    "RSI_DIV_TP_PCT": Setting("rsi_div_tp_pct", "float", "Uyumsuzluk alımı Kâr Al (%, 0 = genel ayar)"),
    "RSI_DIV_SL_PCT": Setting("rsi_div_sl_pct", "float", "Uyumsuzluk alımı Zarar Kes (%, 0 = genel ayar)"),
    "RSI_DIV_OVERSOLD": Setting("rsi_div_oversold", "float", "Uyumsuzluk: ilk dipte RSI en fazla"),
    "RSI_DIV_TREND_FILTER": Setting("rsi_div_trend_filter", "bool", "Uyumsuzlukta trend filtresi (EMA200 üstü)"),
    "BNB_TOPUP_USDT": Setting("bnb_topup_usdt", "float", "BNB takviye tutarı (USDT, 0 = sorma)"),
    # Gün içi al-sat (STRATEGY=intraday)
    "INTRADAY_TACTIC": Setting("intraday_tactic", "str",
                               "Gün içi taktik (vwap, bollinger, rsi2, orb, ema_cross, squeeze, donchian, pullback, dip)"),
    "INTRADAY_COINS": Setting("intraday_coins", "str", "Gün içi coinler (virgülle, ör. BTC,ETH,XRP)"),
    "INTRADAY_TREND_FILTER": Setting("intraday_trend_filter", "bool", "Gün içi trend filtresi (EMA50 > EMA200)"),
    "INTRADAY_BAND_STD": Setting("intraday_band_std", "float", "Gün içi: VWAP / Bollinger bandı (standart sapma)"),
    "INTRADAY_RSI2_MAX": Setting("intraday_rsi2_max", "float", "Gün içi: RSI(2) eşiği"),
    "INTRADAY_ORB_MINUTES": Setting("intraday_orb_minutes", "int", "Gün içi: açılış aralığı (dk)"),
    "INTRADAY_BREAKOUT_BARS": Setting("intraday_breakout_bars", "int", "Gün içi: kırılacak zirve (mum)"),
    "INTRADAY_VOLUME_RATIO": Setting("intraday_volume_ratio", "float", "Gün içi: kırılım hacmi (ortalamanın katı)"),
    "INTRADAY_MIN_ATR_PCT": Setting("intraday_min_atr_pct", "float", "Gün içi: en düşük 5 dk oynaklık (ATR %)"),
    # Vadeli işlem scalping (STRATEGY=futures)
    "FUTURES_TACTIC": Setting("futures_tactic", "choice", "Vadeli taktik (crash = çöküş dönüşü / liq4 = 4'lü onay)",
                              FUTURES_TACTICS),
    "FUTURES_CRASH_DROP_PCT": Setting("futures_crash_drop_pct", "float", "Çöküş eşiği (%, FUTURES_CRASH_HOURS içinde)"),
    "FUTURES_CRASH_HOURS": Setting("futures_crash_hours", "int", "Çöküşün ölçüldüğü süre (saat)"),
    "FUTURES_CRASH_LIQ": Setting("futures_crash_liq", "bool", "Çöküşte likidasyon dalgası da şart"),
    "FUTURES_CRASH_EXTRA": Setting("futures_crash_extra", "rules",
                                   "Ek çöküş pencereleri (saat:%, ör. 1:6 ya da 1:5,12:15; kapalı = yok)"),
    "FUTURES_CRASH_Z": Setting("futures_crash_z", "float", "Oynaklık eşiği (standart sapma; 0 = sabit %)"),
    "FUTURES_CRASH_Z_MIN_PCT": Setting("futures_crash_z_min_pct", "float", "Oynaklık eşiğinde en az düşüş (%)"),
    "FUTURES_CONFIRM": Setting("futures_confirm", "choice",
                               "Çöküş teyidi (close = kapanış / volume = hacim zirvesi / green = yeşil mum / either)",
                               FUTURES_CONFIRMS),
    "FUTURES_CONFIRM_BARS": Setting("futures_confirm_bars", "int", "Teyit için izleme süresi (5 dk mum)"),
    "FUTURES_CONFIRM_VOLUME_RATIO": Setting("futures_confirm_volume_ratio", "float",
                                            "Hacim zirvesi teyidi (önceki 20 mum ortalamasının katı)"),
    "FUTURES_LEVERAGE": Setting("futures_leverage", "int", "Vadeli kaldıraç (1-20x; sonraki işlemde uygulanır)"),
    "FUTURES_COINS": Setting("futures_coins", "str", "Vadeli coinler (virgülle, ör. BTC,ETH,SOL)"),
    "FUTURES_SIDES": Setting("futures_sides", "choice", "Vadeli yön (both / long / short)", FUTURES_SIDES),
    "FUTURES_MARGIN_USAGE_PCT": Setting("futures_margin_usage_pct", "float", "Vadeli: teminata giden bakiye (%)"),
    "FUTURES_VOLUME_RATIO": Setting("futures_volume_ratio", "float", "Vadeli: hacim onayı (ortalamanın katı)"),
    "FUTURES_LIQ_RATIO": Setting("futures_liq_ratio", "float", "Vadeli: likidasyon dalgası eşiği (ortalamanın katı)"),
    "FUTURES_LIQ_SOURCE": Setting("futures_liq_source", "choice", "Vadeli: likidasyon ölçümü (proxy / feed / both)",
                                  FUTURES_LIQ_SOURCES),
    "FUTURES_LIQ_MIN_USDT": Setting("futures_liq_min_usdt", "float", "Vadeli: canlı akışta en az likidasyon (USDT)"),
    "FUTURES_BTC_TREND": Setting("futures_btc_trend", "bool", "Vadeli: BTC 1 saatlik trend filtresi"),
    "FUTURES_TP_ORDER": Setting("futures_tp_order", "choice", "Vadeli: Kâr Al emri (limit / market)",
                                FUTURES_TP_ORDERS),
    "FUTURES_EXCHANGE_STOP": Setting("futures_exchange_stop", "bool",
                                     "Vadeli: zarar kes Binance'e konamazsa pozisyonu hemen kapat"),
    "FUTURES_UNIVERSE": Setting("futures_universe", "int", "Vadeli: en hacimli N köklü coin (0 = FUTURES_COINS listesi)"),
    "FUTURES_UNIVERSE_WITH_LIST": Setting("futures_universe_with_list", "bool",
                                          "Vadeli: en hacimli N'e FUTURES_COINS listesini de ekle"),
    "FUTURES_UNIVERSE_MIN_AGE_DAYS": Setting("futures_universe_min_age_days", "int",
                                             "Vadeli: en hacimli N için en az listelenme süresi (gün)"),
    "FUTURES_TRAILING": Setting("futures_trailing", "bool", "Vadeli: izleyen stop"),
    "FUTURES_TRAILING_ACT_PCT": Setting("futures_trailing_act_pct", "float", "Vadeli: izleyen stop başlangıcı (fiyat %)"),
    "FUTURES_TRAILING_GAP_PCT": Setting("futures_trailing_gap_pct", "float", "Vadeli: izleyen stop mesafesi (fiyat %)"),
    "BREAKEVEN_TRIGGER_PCT": Setting("breakeven_trigger_pct", "float", "Başabaş tetiği (%)"),
    "BREAKEVEN_FLOOR_PCT": Setting("breakeven_floor_pct", "float", "Başabaş stopu (%)"),
}

_TRUE = {"1", "true", "yes", "on", "evet", "e", "acik", "açık"}
_FALSE = {"0", "false", "no", "off", "hayir", "hayır", "h", "kapali", "kapalı"}
_NONE = {"", "0", "-", "off", "none", "yok", "kapali", "kapalı"}     # Saat ayarlarında "kapalı"
_HHMM = r"([01]?\d|2[0-3]):?([0-5]\d)"
_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def find_key(text: str) -> Optional[str]:
    """'tp', 'take_profit_pct' veya 'TAKE_PROFIT_PCT' → 'TAKE_PROFIT_PCT'."""
    key = text.strip().upper()
    aliases = {"TP": "TAKE_PROFIT_PCT", "SL": "STOP_LOSS_PCT", "POZISYON": "MAX_OPEN_POSITIONS",
               "GIRIS": "CAPITAL_USAGE_PCT", "BILDIRIM": "NOTIFY_MODE", "KALDIRAC": "FUTURES_LEVERAGE",
               "KALDIRAÇ": "FUTURES_LEVERAGE", "LEVERAGE": "FUTURES_LEVERAGE", "KILL": "MAX_TOTAL_LOSS_PCT",
               "KILLSWITCH": "MAX_TOTAL_LOSS_PCT", "KILL_SWITCH": "MAX_TOTAL_LOSS_PCT"}
    key = aliases.get(key, key)
    return key if key in EDITABLE else None


def parse_value(key: str, raw) -> object:
    """Metni ayarın türüne çevirir; geçersizse ConfigError."""
    s = EDITABLE[key]
    return cast_value(s.kind, raw, key, s.choices)


def cast_value(kind: str, raw, key: str, choices: Tuple[str, ...] = ()) -> object:
    """Metni türüne (float | int | bool | choice) çevirir; geçersizse ConfigError."""
    if not isinstance(raw, str):
        return raw
    if kind == "str":                  # Serbest metin (ör. ROTATION_COINS=BTC,ETH): virgül ondalık sayılmaz
        return raw.strip()
    if kind == "rules":                # Çöküş pencereleri: "1:5,12:15" / düğmeden "1h5,12h15" / "kapalı" → ""
        if raw.strip().lower() in _NONE:
            return ""
        try:
            return format_crash_rules(parse_crash_rules(raw))
        except ValueError as exc:
            raise ConfigError(f"{key}: {exc}") from None
    if kind in ("time", "trange"):     # "23:55" / "2355" / "kapalı" → "23:55" / "" (biçimi Config doğrular)
        text = raw.strip().replace(" ", "")
        if text.lower() in _NONE:
            return ""
        pattern = _HHMM if kind == "time" else f"{_HHMM}-{_HHMM}"
        m = re.fullmatch(pattern, text)
        if m is None:
            return text
        parts = [f"{int(m.group(i)):02d}:{m.group(i + 1)}" for i in range(1, m.lastindex, 2)]
        return "-".join(parts)
    text = raw.strip().replace(",", ".")
    try:
        if kind == "float":
            value = float(text)
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(text)
            return value
        if kind == "int":
            return int(float(text))
    except ValueError:
        raise ConfigError(f"{key} için sayı girin (girilen: {raw!r})") from None
    if kind == "bool":
        low = text.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"{key} için açık / kapalı (true / false) yazın")
    low = text.lower()
    if low not in choices:
        raise ConfigError(f"{key} şunlardan biri olmalı: {', '.join(choices)}")
    return low


# ================================================================ yalnızca backtest için ayar değişikliği
# (python main.py --backtest --ayar KEY=DEĞER ...; Telegram → /backtest → Test ayarları). Botun ayarları değişmez.
OVERRIDE_BLOCKED = frozenset({
    "binance_api_key", "binance_api_secret", "binance_base_url", "binance_ws_url", "telegram_bot_token",
    "telegram_chat_id", "trading_mode", "log_level", "command_pin", "service_name", "notes", "legacy_loss_usdt",
    "excluded_assets", "strategy", "kline_interval", "quote_asset",
})
OVERRIDE_CHOICES = {"dca_mode": ACTION_MODES, "pyramid_mode": ACTION_MODES, "time_exit_mode": ACTION_MODES,
                    "escape_mode": ACTION_MODES, "signal_approval": SIGNAL_APPROVAL_MODES, "notify_mode": NOTIFY_MODES,
                    "futures_sides": FUTURES_SIDES, "futures_liq_source": FUTURES_LIQ_SOURCES,
                    "futures_tactic": FUTURES_TACTICS, "futures_confirm": FUTURES_CONFIRMS,
                    "futures_tp_order": FUTURES_TP_ORDERS}
# Serbest metin olarak değiştirilebilenler (doğrulamayı Config yapar)
OVERRIDE_TEXT = frozenset({"rotation_coins", "intraday_coins", "intraday_tactic", "futures_coins",
                           "futures_crash_extra"})


def override_spec(key: str) -> Optional[Tuple[str, str, Tuple[str, ...]]]:
    """Backtest'te değiştirilebilen ayarın (Config alanı, tür, seçenekler); değiştirilemiyorsa None."""
    attr = key.strip().lower()
    f = Config.__dataclass_fields__.get(attr)
    if f is None or attr in OVERRIDE_BLOCKED:
        return None
    if f.type in ("float", "int", "bool"):
        return attr, f.type, ()
    if f.type == "str" and attr in OVERRIDE_CHOICES:
        return attr, "choice", OVERRIDE_CHOICES[attr]
    if attr == "futures_crash_extra":
        return attr, "rules", ()
    if f.type == "str" and attr in OVERRIDE_TEXT:
        return attr, "str", ()
    return None


def apply_overrides(cfg: Config, values: Dict[str, object]) -> Config:
    """Backtest'e özel ayar değişikliklerini uygular ve doğrular (geçersizse ConfigError)."""
    changes = {}
    for key, raw in values.items():
        spec = override_spec(key)
        if spec is None:
            raise ConfigError(f"{key} backtest ayarı olarak değiştirilemez")
        attr, kind, choices = spec
        changes[attr] = cast_value(kind, raw, key.upper(), choices)
    new = replace(cfg, **changes)
    new.validate()
    return new


def parse_override_args(items: Sequence[str]) -> Dict[str, str]:
    """['TAKE_PROFIT_PCT=3', 'VWAP_FILTER=true'] → {'TAKE_PROFIT_PCT': '3', ...}; biçim hatalıysa ConfigError.

    Serbest metin ayarları boş olabilir (ör. ROTATION_COINS= → liste yok, en hacimli coinler)."""
    out: Dict[str, str] = {}
    for item in items or ():
        key, sep, value = str(item).partition("=")
        text_key = key.strip().lower() in OVERRIDE_TEXT
        if not sep or not key.strip() or (not value.strip() and not text_key):
            raise ConfigError(f"--ayar ANAHTAR=DEĞER biçiminde olmalı (girilen: {item!r})")
        out[key.strip().upper()] = value.strip()
    return out


def format_value(value) -> str:
    """.env'ye yazılacak metin: True → 'true', 2.50 → '2.5'."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def apply_settings(cfg: Config, values: Dict[str, object]) -> Tuple[Config, Tuple[str, ...]]:
    """Değerleri uygulanmış ve doğrulanmış yeni Config + yeni çıkan ayar uyarıları. Geçersizse ConfigError."""
    changes = {}
    for key, raw in values.items():
        if key not in EDITABLE:
            raise ConfigError(f"{key} Telegram'dan değiştirilemez")
        changes[EDITABLE[key].attr] = parse_value(key, raw)
    if "max_total_loss_pct" in changes:
        changes["legacy_loss_usdt"] = None     # Yüzdelik limit, eski sabit USDT ayarının (MAX_TOTAL_LOSS_USDT) yerine geçer
    new = replace(cfg, **changes)
    new.validate()
    old_notes = set(_setting_notes(cfg))
    return new, tuple(n for n in _setting_notes(new) if n not in old_notes)


def env_values(cfg: Config, keys: Sequence[str]) -> Dict[str, str]:
    """Config'teki güncel değerlerin .env satırı karşılıkları."""
    return {k: format_value(getattr(cfg, EDITABLE[k].attr)) for k in keys}


def write_env_values(path, values: Dict[str, Optional[str]]) -> None:
    """KEY=değer satırlarını .env'de günceller (yoksa sona ekler, değer None ise satırı siler).

    Aynı anahtar birden çok kez yazılmışsa hepsi güncellenir (dosyada son yazılan geçerli olduğu için). Diğer
    satırlar, yorumlar ve dosya izinleri aynen korunur. Yazma atomiktir: yarıda kalırsa eski dosya bozulmaz.
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written = set()
    out = []
    for line in lines:
        m = _KEY_RE.match(line)
        key = m.group(1) if m else None
        if key in values:
            if values[key] is None:
                continue
            out.append(f"{key}={values[key]}")
            written.add(key)
        else:
            out.append(line)
    new_keys = [k for k, v in values.items() if v is not None and k not in written]
    if new_keys:
        marker = "# Telegram menüsünden değiştirilen ayarlar (ayarlar.env'deki değeri ezer)"
        if marker not in out:
            if out and out[-1].strip():
                out.append("")
            out.append(marker)
        out.extend(f"{k}={values[k]}" for k in new_keys)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
