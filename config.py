"""
Ayarlar modülü.

.env dosyasındaki değerleri okur, varsayılanları uygular ve tutarlılığını
kontrol eder. Botun tüm parametreleri tek bir `Config` nesnesinde toplanır.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

# Stabil coinler, fiat paralar ve altın gibi düşük oynaklıklı varlıklar
# "toparlanma" stratejisine uygun olmadığı için taramadan çıkarılır.
DEFAULT_EXCLUDED_ASSETS = frozenset({
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "USDD", "UST", "USTC",
    "AEUR", "EURI", "EUR", "GBP", "AUD", "TRY", "BRL", "BIDR", "IDRT", "UAH",
    "ZAR", "RUB", "NGN", "PLN", "RON", "ARS", "JPY", "MXN", "COP", "CZK",
    "PAXG", "XUSD", "USD1", "BFUSD", "RLUSD", "USDE", "USDS", "PYUSD",
})


class ConfigError(Exception):
    """Ayar dosyasındaki hatalı/eksik değerler için."""


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{name} sayısal bir değer olmalı (girilen: {raw!r})") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw.strip()))
    except ValueError as exc:
        raise ConfigError(f"{name} tam sayı olmalı (girilen: {raw!r})") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "evet", "e"}


def _env_set(name: str) -> FrozenSet[str]:
    raw = _env_str(name)
    return frozenset(x.strip().upper() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    # --- API bilgileri ---
    binance_api_key: str
    binance_api_secret: str
    binance_base_url: str
    telegram_bot_token: str
    telegram_chat_id: str

    # --- Çalışma modu ---
    trading_mode: str  # "paper" (sanal bakiye) veya "live" (gerçek para)
    log_level: str

    # --- Sermaye ve risk ---
    quote_asset: str
    start_capital: float          # Botun yöneteceği sermaye (USDT)
    capital_usage_pct: float      # Sermayenin işleme girilecek yüzdesi
    max_open_positions: int       # Aynı anda en fazla kaç coin (bakiye yetmezse otomatik azalır)
    max_total_loss: float         # Kill-switch: toplam zarar limiti (USDT)
    take_profit_pct: float        # Net kâr hedefi (%)
    stop_loss_pct: float          # Net zarar limiti (%)
    trailing_activation_pct: float  # İzleyen stop'un devreye girdiği net kâr (%)
    trailing_floor_pct: float     # İzleyen stop aktifken satış yapılacak net kâr (%)
    trailing_dynamic: bool        # True: taban = max(floor, zirve kârın yarısı)
    fee_rate: float               # İşlem başına komisyon oranı (0.001 = %0.1)
    max_hold_minutes: int         # 0 = kapalı. >0 ise bu süre dolunca pozisyon kapatılır
    min_notional_safety: float    # Stop-loss seviyesinde bile min. emir tutarının bu katı kalmalı
    max_dust_pct: float           # LOT adımı yüzünden satılamayacak "toz" kaybının üst sınırı (%)
    symbol_cooldown_minutes: int  # Bir coinden çıktıktan sonra tekrar girmeden önce bekleme

    # --- Zamanlama ---
    scan_interval_sec: int        # Piyasa tarama aralığı
    position_check_sec: int       # Açık pozisyon fiyat kontrol aralığı
    status_report_minutes: int    # Telegram durum raporu aralığı (0 = kapalı)

    # --- Strateji (düşüş sonrası toparlanma) ---
    min_quote_volume_24h: float
    min_24h_drop_pct: float
    max_24h_drop_pct: float
    min_24h_range_pct: float
    max_candidates: int
    kline_interval: str
    min_dip_pct: float
    min_retrace: float
    max_retrace: float
    rsi_period: int
    rsi_oversold: float
    rsi_entry_min: float
    rsi_entry_max: float
    min_volume_ratio: float
    btc_filter: bool
    btc_max_1h_drop_pct: float
    excluded_assets: FrozenSet[str]

    @property
    def is_paper(self) -> bool:
        return self.trading_mode == "paper"

    @property
    def has_binance_keys(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def validate(self) -> None:
        errors = []
        if self.trading_mode not in {"paper", "live"}:
            errors.append("TRADING_MODE 'paper' veya 'live' olmalı")
        if self.start_capital <= 0:
            errors.append("START_CAPITAL_USDT sıfırdan büyük olmalı")
        if not 1 <= self.max_open_positions <= 10:
            errors.append("MAX_OPEN_POSITIONS 1 ile 10 arasında olmalı")
        if not 0 < self.capital_usage_pct <= 100:
            errors.append("CAPITAL_USAGE_PCT 0-100 arasında olmalı")
        if self.max_total_loss <= 0:
            errors.append("MAX_TOTAL_LOSS_USDT sıfırdan büyük olmalı")
        if self.max_total_loss >= self.start_capital:
            errors.append("MAX_TOTAL_LOSS_USDT başlangıç sermayesinden küçük olmalı")
        if not 0 < self.stop_loss_pct < 100:
            errors.append("STOP_LOSS_PCT 0-100 arasında olmalı")
        if self.take_profit_pct <= 0:
            errors.append("TAKE_PROFIT_PCT sıfırdan büyük olmalı")
        if not 0 <= self.trailing_floor_pct < self.trailing_activation_pct:
            errors.append("TRAILING_FLOOR_PCT, TRAILING_ACTIVATION_PCT'den küçük olmalı")
        if self.trailing_activation_pct >= self.take_profit_pct:
            errors.append("TRAILING_ACTIVATION_PCT, TAKE_PROFIT_PCT'den küçük olmalı")
        if not 0 <= self.fee_rate < 0.01:
            errors.append("FEE_RATE 0 ile 0.01 arasında olmalı (örn. 0.001 = %0.1)")
        if self.scan_interval_sec < 5:
            errors.append("SCAN_INTERVAL_SEC en az 5 olmalı")
        if self.position_check_sec < 1:
            errors.append("POSITION_CHECK_SEC en az 1 olmalı")
        if not 0 <= self.min_retrace < self.max_retrace <= 1:
            errors.append("MIN_RETRACE < MAX_RETRACE ve ikisi 0-1 arasında olmalı")
        if self.min_notional_safety < 1:
            errors.append("MIN_NOTIONAL_SAFETY en az 1.0 olmalı")
        if self.max_candidates < 1:
            errors.append("MAX_CANDIDATES en az 1 olmalı")
        if errors:
            raise ConfigError("Ayar hataları:\n  - " + "\n  - ".join(errors))


def load_config(env_file: Optional[str] = None, mode_override: Optional[str] = None) -> Config:
    """.env dosyasını okuyup doğrulanmış bir Config döndürür."""
    env_path = Path(env_file) if env_file else BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    mode = (mode_override or _env_str("TRADING_MODE", "paper")).lower()

    cfg = Config(
        binance_api_key=_env_str("BINANCE_API_KEY"),
        binance_api_secret=_env_str("BINANCE_API_SECRET"),
        binance_base_url=_env_str("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/"),
        telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env_str("TELEGRAM_CHAT_ID"),
        trading_mode=mode,
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        quote_asset=_env_str("QUOTE_ASSET", "USDT").upper(),
        start_capital=_env_float("START_CAPITAL_USDT", 11.0),
        capital_usage_pct=_env_float("CAPITAL_USAGE_PCT", 100.0),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", 3),
        max_total_loss=_env_float("MAX_TOTAL_LOSS_USDT", 5.0),
        take_profit_pct=_env_float("TAKE_PROFIT_PCT", 15.0),
        stop_loss_pct=_env_float("STOP_LOSS_PCT", 10.0),
        trailing_activation_pct=_env_float("TRAILING_ACTIVATION_PCT", 10.0),
        trailing_floor_pct=_env_float("TRAILING_FLOOR_PCT", 5.0),
        trailing_dynamic=_env_bool("TRAILING_DYNAMIC", False),
        fee_rate=_env_float("FEE_RATE", 0.001),
        max_hold_minutes=_env_int("MAX_HOLD_MINUTES", 0),
        min_notional_safety=_env_float("MIN_NOTIONAL_SAFETY", 1.05),
        max_dust_pct=_env_float("MAX_DUST_PCT", 0.5),
        symbol_cooldown_minutes=_env_int("SYMBOL_COOLDOWN_MINUTES", 60),
        scan_interval_sec=_env_int("SCAN_INTERVAL_SEC", 30),
        position_check_sec=_env_int("POSITION_CHECK_SEC", 5),
        status_report_minutes=_env_int("STATUS_REPORT_MINUTES", 60),
        min_quote_volume_24h=_env_float("MIN_QUOTE_VOLUME_24H", 3_000_000),
        min_24h_drop_pct=_env_float("MIN_24H_DROP_PCT", 2.0),
        max_24h_drop_pct=_env_float("MAX_24H_DROP_PCT", 30.0),
        min_24h_range_pct=_env_float("MIN_24H_RANGE_PCT", 4.0),
        max_candidates=_env_int("MAX_CANDIDATES", 15),
        kline_interval=_env_str("KLINE_INTERVAL", "5m"),
        min_dip_pct=_env_float("MIN_DIP_PCT", 3.0),
        min_retrace=_env_float("MIN_RETRACE", 0.15),
        max_retrace=_env_float("MAX_RETRACE", 0.60),
        rsi_period=_env_int("RSI_PERIOD", 14),
        rsi_oversold=_env_float("RSI_OVERSOLD", 35.0),
        rsi_entry_min=_env_float("RSI_ENTRY_MIN", 40.0),
        rsi_entry_max=_env_float("RSI_ENTRY_MAX", 65.0),
        min_volume_ratio=_env_float("MIN_VOLUME_RATIO", 1.0),
        btc_filter=_env_bool("BTC_FILTER", True),
        btc_max_1h_drop_pct=_env_float("BTC_MAX_1H_DROP_PCT", 1.5),
        excluded_assets=DEFAULT_EXCLUDED_ASSETS | _env_set("EXCLUDED_ASSETS"),
    )
    cfg.validate()
    return cfg
