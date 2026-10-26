"""
Telegram menüsünden (/menu, /ayar) değiştirilebilen ayarlar.

- Yalnızca `EDITABLE` listesindeki ayarlar değiştirilebilir. API anahtarları, Telegram token'ı, çalışma modu,
  sermaye ve kill-switch gibi ayarlar Telegram'dan DEĞİŞTİRİLEMEZ.
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

from config import NOTIFY_MODES, Config, ConfigError, _setting_notes


@dataclass(frozen=True)
class Setting:
    attr: str           # Config alanı
    kind: str           # float | int | bool | choice
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
    "OB_WALL_FILTER": Setting("ob_wall_filter", "bool", "Satış duvarı radarı"),
    "OB_IMBALANCE_FILTER": Setting("ob_imbalance_filter", "bool", "Emir defteri dengesizliği"),
    "TAPE_FILTER": Setting("tape_filter", "bool", "Tape okuma teyidi"),
    "MAX_SPREAD_PCT": Setting("max_spread_pct", "float", "Maks. spread (%)"),
    "BTC_MAX_1H_DROP_PCT": Setting("btc_max_1h_drop_pct", "float", "BTC 1 saatlik düşüş sınırı (%)"),
    "OVERHEAT_PCT": Setting("overheat_pct", "float", "Aşırı ısınma eşiği (%)"),
    # Risk sigortaları
    "DAILY_LOSS_LIMIT_PCT": Setting("daily_loss_limit_pct", "float", "Günlük zarar limiti (%, 0 = kapalı)"),
    "DAILY_PROFIT_TARGET_PCT": Setting("daily_profit_target_pct", "float", "Günlük kâr hedefi (%, 0 = kapalı)"),
    "LOSS_STREAK_LIMIT": Setting("loss_streak_limit", "int", "Üst üste zarar kes sigortası (0 = kapalı)"),
    "LOSS_STREAK_PAUSE_HOURS": Setting("loss_streak_pause_hours", "float", "Sigorta bekleme süresi (saat)"),
    "COIN_SL_LIMIT": Setting("coin_sl_limit", "int", "Coin karnesi: kara liste teklifi (0 = kapalı)"),
    "SLIPPAGE_ALERT_PCT": Setting("slippage_alert_pct", "float", "Kayma alarmı (ortalama %, 0 = kapalı)"),
}

_TRUE = {"1", "true", "yes", "on", "evet", "e", "acik", "açık"}
_FALSE = {"0", "false", "no", "off", "hayir", "hayır", "h", "kapali", "kapalı"}
_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def find_key(text: str) -> Optional[str]:
    """'tp', 'take_profit_pct' veya 'TAKE_PROFIT_PCT' → 'TAKE_PROFIT_PCT'."""
    key = text.strip().upper()
    aliases = {"TP": "TAKE_PROFIT_PCT", "SL": "STOP_LOSS_PCT", "POZISYON": "MAX_OPEN_POSITIONS",
               "GIRIS": "CAPITAL_USAGE_PCT", "BILDIRIM": "NOTIFY_MODE"}
    key = aliases.get(key, key)
    return key if key in EDITABLE else None


def parse_value(key: str, raw) -> object:
    """Metni ayarın türüne çevirir; geçersizse ConfigError."""
    s = EDITABLE[key]
    if not isinstance(raw, str):
        return raw
    text = raw.strip().replace(",", ".")
    try:
        if s.kind == "float":
            value = float(text)
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(text)
            return value
        if s.kind == "int":
            return int(float(text))
    except ValueError:
        raise ConfigError(f"{key} için sayı girin (girilen: {raw!r})") from None
    if s.kind == "bool":
        low = text.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"{key} için açık / kapalı (true / false) yazın")
    low = text.lower()
    if low not in s.choices:
        raise ConfigError(f"{key} şunlardan biri olmalı: {', '.join(s.choices)}")
    return low


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
