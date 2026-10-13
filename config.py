"""
Ayarlar modülü.

.env dosyasındaki değerleri okur, varsayılanları uygular ve tutarlılığını
kontrol eder. Botun tüm parametreleri tek bir `Config` nesnesinde toplanır.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import FrozenSet, List, Optional, Tuple

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


def parse_hhmm(text: str) -> Optional[int]:
    """'23:55' → gece yarısından itibaren dakika; hatalıysa None."""
    try:
        hh, mm = text.strip().split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def parse_time_range(text: str) -> Optional[Tuple[int, int]]:
    """'23:00-08:00' → (1380, 480); boş/hatalıysa None."""
    if not text or "-" not in text:
        return None
    start, end = (parse_hhmm(x) for x in text.split("-", 1))
    if start is None or end is None or start == end:
        return None
    return start, end


def in_time_range(minute_of_day: int, window: Optional[Tuple[int, int]]) -> bool:
    """Gece yarısını aşan aralıkları da (23:00-08:00) doğru değerlendirir."""
    if window is None:
        return False
    start, end = window
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


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
    max_total_loss_pct: float     # Kill-switch: toplam zarar limiti (botun sermayesinin %'si)
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

    # ================= Ek özellikler (varsayılanlı) =================
    # 1) Binance'te yedek zarar-kes emri (bot ulaşılamazsa devreye girer)
    backup_stop_enabled: bool = True
    backup_stop_pct: float = 12.0          # Net zarar % (bot normalde STOP_LOSS_PCT'de kendisi satar)
    backup_stop_limit_gap_pct: float = 1.0  # STOP_LOSS_LIMIT gerekiyorsa limit fiyatın stop altındaki payı
    # 4) Başabaş koruması (isteğe bağlı)
    breakeven_enabled: bool = True
    breakeven_trigger_pct: float = 5.0     # Net kâr bu seviyeyi görünce koruma başlar
    breakeven_floor_pct: float = 0.3       # Sonra net kâr bu seviyeye inerse satılır
    # 5) BTC çökerken açık pozisyonlardan erken çıkış (isteğe bağlı)
    btc_crash_exit: bool = False
    btc_crash_drop_pct: float = 2.0        # BTC son X dakikadaki zirvesinden bu kadar düşerse
    btc_crash_window_min: int = 15
    # 6) Anlık fiyat akışı (WebSocket)
    use_websocket: bool = True
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"
    ws_position_check_sec: float = 1.0     # Akış canlıyken pozisyon kontrol aralığı
    # 12) Kesinti uyarısı
    outage_alert_sec: int = 120            # Döngü bu kadar saniye durursa "bot çalışmadı" uyarısı
    # 13) İnternet kesintisi
    wait_for_network: bool = True          # Açılışta internet yoksa kapanmak yerine bekle
    telegram_queue_max: int = 300          # Gönderilemeyen mesaj kuyruğu üst sınırı
    # 16-20) Telegram komutları
    telegram_commands: bool = True
    command_pin: str = ""                  # Doluysa riskli komut onayı "/evet PIN" ister
    show_try: bool = True                  # Bakiyeyi TL karşılığıyla da göster
    # 21) Günlük / haftalık rapor
    daily_report_time: str = "23:55"       # "" = kapalı
    weekly_report: bool = True             # Pazar günü haftalık özet
    # 24) Sessiz saatler ("23:00-08:00", boş = kapalı)
    quiet_hours: str = ""
    # 28) Spread filtresi
    max_spread_pct: float = 0.3
    slippage_warn_pct: float = 0.5
    # 29) Tehlikeli coin filtresi
    delist_filter: bool = True
    min_listing_days: int = 14
    pump_filter_pct: float = 50.0          # Son PUMP_LOOKBACK_DAYS günde bu kadar yükselmişse ele
    pump_lookback_days: int = 3
    # 30) Sinyal kalitesi
    min_signal_score: float = 0.0          # 0 = kapalı
    signal_confirm_scans: int = 1          # 1 = kapalı (2 = sinyal art arda 2 taramada görülmeli)
    # 31) Oynaklık (ATR) filtresi — 5 dk ATR(14) / fiyat (%)
    min_atr_pct: float = 0.25
    max_atr_pct: float = 4.0
    # 33) Piyasa rejimi
    regime_filter: bool = True
    # 37) Ayrıntılı kayıt
    equity_log_minutes: int = 15           # 0 = kapalı
    # 38) BNB ile komisyon indirimi takibi: yalnızca izler ve uyarır (BNB ALMAZ, Binance ayarını değiştirmez)
    bnb_fee_check: bool = True             # false = komisyon/BNB kontrolü tamamen kapalı
    bnb_min_trades: int = 20               # BNB bundan az al-sat işlemine yetecekse uyar (0 = uyarma)
    # Eski sabit USDT kill-switch ayarı (MAX_TOTAL_LOSS_USDT). Yalnızca MAX_TOTAL_LOSS_PCT yazılmamışsa dolu.
    legacy_loss_usdt: Optional[float] = None
    # Ayar okunurken oluşan uyarılar (eski ayar adı, işe yaramayan değer...). Bot açılırken gösterilir.
    notes: Tuple[str, ...] = ()

    @property
    def quiet_window(self) -> Optional[Tuple[int, int]]:
        """Sessiz saatler (başlangıç, bitiş) gece yarısından itibaren dakika olarak; kapalıysa None."""
        return parse_time_range(self.quiet_hours)

    def loss_limit(self, base_capital: float, capital_added: float = 0.0) -> float:
        """Kill-switch'in USDT karşılığı: botun sermayesi × MAX_TOTAL_LOSS_PCT.

        `base_capital` botun yönettiği toplam sermayedir (başlangıç + /sermayeekle ile eklenenler,
        `capital_added` bunların sonradan eklenen kısmı). Hesaba para eklendikçe limit de aynı oranda büyür.

        Eski MAX_TOTAL_LOSS_USDT ayarı hâlâ kullanılıyorsa limit, ilk sermayede tam o USDT tutarıdır
        (koruma ne sıkılaşır ne gevşer); sonradan sermaye eklenirse aynı oranda büyür.
        """
        base = max(0.0, base_capital)
        if self.legacy_loss_usdt is not None:
            original = base - max(0.0, capital_added)
            return self.legacy_loss_usdt * base / original if original > 0 else self.legacy_loss_usdt
        return base * self.max_total_loss_pct / 100

    def loss_pct(self, base_capital: float, capital_added: float = 0.0) -> float:
        """Kill-switch limitinin sermayeye oranı (%) — mesajlarda göstermek için."""
        if base_capital <= 0:
            return self.max_total_loss_pct
        return self.loss_limit(base_capital, capital_added) / base_capital * 100

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
        if self.legacy_loss_usdt is not None:
            if self.legacy_loss_usdt <= 0:
                errors.append("MAX_TOTAL_LOSS_USDT sıfırdan büyük olmalı (yeni ayar: MAX_TOTAL_LOSS_PCT)")
        elif not 0 < self.max_total_loss_pct < 100:
            errors.append("MAX_TOTAL_LOSS_PCT 0 ile 100 arasında olmalı (örn. 45 = sermayenin %45'i)")
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
        if not 0 < self.backup_stop_pct < 100:
            errors.append("BACKUP_STOP_PCT 0-100 arasında olmalı")
        if self.backup_stop_enabled and self.backup_stop_pct <= self.stop_loss_pct:
            errors.append("BACKUP_STOP_PCT, STOP_LOSS_PCT'den büyük olmalı (yedek emir botun zarar kesinin altında beklemeli)")
        if self.breakeven_enabled and not 0 <= self.breakeven_floor_pct < self.breakeven_trigger_pct:
            errors.append("BREAKEVEN_FLOOR_PCT, BREAKEVEN_TRIGGER_PCT'den küçük olmalı")
        if self.signal_confirm_scans < 1:
            errors.append("SIGNAL_CONFIRM_SCANS en az 1 olmalı")
        if self.min_atr_pct >= self.max_atr_pct:
            errors.append("MIN_ATR_PCT, MAX_ATR_PCT'den küçük olmalı")
        if self.ws_position_check_sec <= 0:
            errors.append("WS_POSITION_CHECK_SEC sıfırdan büyük olmalı")
        if self.daily_report_time and parse_hhmm(self.daily_report_time) is None:
            errors.append("DAILY_REPORT_TIME SS:DD biçiminde olmalı (örn. 23:55) veya boş")
        if self.quiet_hours and parse_time_range(self.quiet_hours) is None:
            errors.append("QUIET_HOURS SS:DD-SS:DD biçiminde olmalı (örn. 23:00-08:00) veya boş")
        if self.bnb_min_trades < 0:
            errors.append("BNB_MIN_TRADES 0 veya daha büyük olmalı (0 = az BNB uyarısı kapalı)")
        if errors:
            raise ConfigError("Ayar hataları:\n  - " + "\n  - ".join(errors))


def load_config(env_file: Optional[str] = None, mode_override: Optional[str] = None) -> Config:
    """.env dosyasını okuyup doğrulanmış bir Config döndürür."""
    env_path = Path(env_file) if env_file else BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    mode = (mode_override or _env_str("TRADING_MODE", "paper")).lower()
    loss_pct, legacy_usdt, notes = _loss_pct_setting(_env_float("START_CAPITAL_USDT", 11.0))

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
        max_total_loss_pct=loss_pct,
        legacy_loss_usdt=legacy_usdt,
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
        backup_stop_enabled=_env_bool("BACKUP_STOP_ENABLED", True),
        backup_stop_pct=_env_float("BACKUP_STOP_PCT", 12.0),
        backup_stop_limit_gap_pct=_env_float("BACKUP_STOP_LIMIT_GAP_PCT", 1.0),
        breakeven_enabled=_env_bool("BREAKEVEN_ENABLED", True),
        breakeven_trigger_pct=_env_float("BREAKEVEN_TRIGGER_PCT", 5.0),
        breakeven_floor_pct=_env_float("BREAKEVEN_FLOOR_PCT", 0.3),
        btc_crash_exit=_env_bool("BTC_CRASH_EXIT", False),
        btc_crash_drop_pct=_env_float("BTC_CRASH_DROP_PCT", 2.0),
        btc_crash_window_min=_env_int("BTC_CRASH_WINDOW_MIN", 15),
        use_websocket=_env_bool("USE_WEBSOCKET", True),
        binance_ws_url=_env_str("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws"),
        ws_position_check_sec=_env_float("WS_POSITION_CHECK_SEC", 1.0),
        outage_alert_sec=_env_int("OUTAGE_ALERT_SEC", 120),
        wait_for_network=_env_bool("WAIT_FOR_NETWORK", True),
        telegram_queue_max=_env_int("TELEGRAM_QUEUE_MAX", 300),
        telegram_commands=_env_bool("TELEGRAM_COMMANDS", True),
        command_pin=_env_str("COMMAND_PIN"),
        show_try=_env_bool("SHOW_TRY", True),
        daily_report_time=_env_str("DAILY_REPORT_TIME", "23:55"),
        weekly_report=_env_bool("WEEKLY_REPORT", True),
        quiet_hours=_env_str("QUIET_HOURS"),
        max_spread_pct=_env_float("MAX_SPREAD_PCT", 0.3),
        slippage_warn_pct=_env_float("SLIPPAGE_WARN_PCT", 0.5),
        delist_filter=_env_bool("DELIST_FILTER", True),
        min_listing_days=_env_int("MIN_LISTING_DAYS", 14),
        pump_filter_pct=_env_float("PUMP_FILTER_PCT", 50.0),
        pump_lookback_days=_env_int("PUMP_LOOKBACK_DAYS", 3),
        min_signal_score=_env_float("MIN_SIGNAL_SCORE", 0.0),
        signal_confirm_scans=_env_int("SIGNAL_CONFIRM_SCANS", 1),
        min_atr_pct=_env_float("MIN_ATR_PCT", 0.25),
        max_atr_pct=_env_float("MAX_ATR_PCT", 4.0),
        regime_filter=_env_bool("REGIME_FILTER", True),
        equity_log_minutes=_env_int("EQUITY_LOG_MINUTES", 15),
        bnb_fee_check=_env_bool("BNB_FEE_CHECK", True),
        bnb_min_trades=_env_int("BNB_MIN_TRADES", 20),
    )
    cfg.validate()
    notes += _setting_notes(cfg)
    return replace(cfg, notes=tuple(notes)) if notes else cfg


DEFAULT_MAX_TOTAL_LOSS_PCT = 45.0  # 11 USDT sermayede ≈ 5 USDT


def _loss_pct_setting(start_capital: float) -> Tuple[float, Optional[float], List[str]]:
    """Kill-switch ayarını okur: (yüzde, eski USDT değeri veya None, uyarılar).

    - MAX_TOTAL_LOSS_PCT yazılıysa o geçerlidir (eski USDT satırı yok sayılır).
    - Yalnızca eski MAX_TOTAL_LOSS_USDT varsa o USDT tutarı aynen korunur (bkz. Config.loss_limit);
      yüzde değeri yalnızca sermaye bilinmeyen yerlerde (backtest, self-test) gösterim içindir.
    - Hiçbiri yoksa varsayılan %45.
    """
    notes: List[str] = []
    has_pct = (os.getenv("MAX_TOTAL_LOSS_PCT") or "").strip() != ""
    has_usdt = (os.getenv("MAX_TOTAL_LOSS_USDT") or "").strip() != ""
    if has_pct:
        if has_usdt:
            notes.append("MAX_TOTAL_LOSS_USDT artık kullanılmıyor (MAX_TOTAL_LOSS_PCT geçerli); "
                         ".env'den bu satırı silebilirsiniz.")
        return _env_float("MAX_TOTAL_LOSS_PCT", DEFAULT_MAX_TOTAL_LOSS_PCT), None, notes
    if has_usdt:
        usdt = _env_float("MAX_TOTAL_LOSS_USDT", 0.0)
        pct = round(usdt / start_capital * 100, 2) if start_capital > 0 else DEFAULT_MAX_TOTAL_LOSS_PCT
        notes.append(f"Eski ayar MAX_TOTAL_LOSS_USDT={usdt:g} kullanılıyor: kill-switch şimdilik sabit "
                     f"{usdt:g} USDT (sermaye eklenirse aynı oranda büyür). Yüzdeye geçmek için .env'de bu "
                     f"satırı silip MAX_TOTAL_LOSS_PCT=45 gibi bir değer yazın "
                     f"({start_capital:g} USDT'de {usdt:g} USDT ≈ %{pct:g}).")
        return pct, usdt, notes
    return DEFAULT_MAX_TOTAL_LOSS_PCT, None, notes


def _setting_notes(cfg: Config) -> List[str]:
    """Hata sayılmayan ama işe yaramayan ayar birleşimleri için uyarılar."""
    notes: List[str] = []
    from risk import breakeven_effective  # risk config'i içe aktarmaz; döngüsel bağımlılık yok
    if cfg.breakeven_enabled:
        if not breakeven_effective(cfg):
            first_exit = min(cfg.take_profit_pct, cfg.trailing_activation_pct)
            hint = round(first_exit / 2, 1)
            tip = (f" Örn. BREAKEVEN_TRIGGER_PCT={hint:g} yapın." if hint > cfg.breakeven_floor_pct else "")
            notes.append(f"Başabaş koruması açık ama BREAKEVEN_TRIGGER_PCT ({cfg.breakeven_trigger_pct:g}), "
                         f"izleyen stop / kâr al seviyesinden (+%{first_exit:g}) küçük olmadığı için hiç "
                         f"devreye girmez.{tip}")
    return notes
