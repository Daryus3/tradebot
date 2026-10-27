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


# Giriş stratejileri. İlk üçü 5 dakikalık mumlarla dakikalar-saatler süren işlemler içindir; swing stratejileri
# 1 saatlik mumlarla 1-5 gün süren işlemler içindir; mean_revert 1 saatlik mumlarla en hacimli coinlerde trend içi
# aşırı satımı alıp küçük hedefle (saatler-2 gün) satar; rsi_divergence aynı coinlerde pozitif RSI uyumsuzluğunu
# dönüş teyidiyle alır (aynı çıkış grubu); rotation günlük mumlarla haftalık momentum rotasyonudur (en güçlü birkaç
# coini tutar, her hafta yeniden dengeler; bkz. strategy.py ve rotation.py).
STRATEGY_NAMES = ("rebound", "trend", "breakout", "swing", "swing_pullback", "mean_revert", "rsi_divergence",
                  "rotation")
LONG_HORIZON_STRATEGIES = frozenset({"swing", "swing_pullback"})
MEAN_REVERSION_STRATEGIES = frozenset({"mean_revert", "rsi_divergence"})
ROTATION_STRATEGIES = frozenset({"rotation"})
# Telegram bildirim modu: all = hepsi | closes = yalnızca pozisyon kapanışları | silent = yalnızca acil durumlar.
# Komut cevapları, hatalar ve kritik uyarılar (zarar kes, kill-switch...) her modda gelir.
NOTIFY_MODES = ("all", "closes", "silent")
# Akıllı pozisyon yönetimi modları: off = kapalı | ask = Telegram'dan sorar | auto = sormadan uygular
# (backtest yalnızca "auto"yu simüle edebilir; "ask" orada kapalı sayılır)
ACTION_MODES = ("off", "ask", "auto")
SIGNAL_APPROVAL_MODES = ("off", "volatile", "all")

# Uzun vadeli (swing) stratejiler için önerilen çıkış ayarları ve stratejiye özel giriş eşikleri (.env anahtarı →
# değer). 5 dk stratejilerine göre ayarlanmış çıkışlar (örn. 4 saatlik maks. süre, +%1 başabaş) swing işlemlerini
# daha başlamadan kapatır. Backtest'te .env'deki 5 dk stratejisinden --strateji ile swing'e geçilince bunlar kullanılır.
SWING_EXIT_PROFILE = {
    "TAKE_PROFIT_PCT": 12.0,
    "STOP_LOSS_PCT": 6.0,
    "TRAILING_ACTIVATION_PCT": 8.0,
    "TRAILING_FLOOR_PCT": 4.0,
    "MAX_HOLD_MINUTES": 7200,          # 5 gün
    "BREAKEVEN_TRIGGER_PCT": 5.0,
    "BREAKEVEN_FLOOR_PCT": 1.0,
    "MIN_SIGNAL_SCORE": 0.0,           # Puan ölçeği stratejiye özeldir: 5 dk stratejisi için seçilen eşik uymaz
    "MIN_ROOM_RATIO": 0.0,
    "MAX_24H_GAIN_PCT": 25.0,          # En az bu (5 dk trend/breakout taraması 12 / 6 önerebilir; kırılımları eler)
}
# Momentum rotasyonu: pozisyon sıralamadan düşünce veya BTC trend filtresiyle satılır; Kâr Al / izleyen stop /
# başabaş / maks. süre kullanılmaz. Zarar kes yalnızca felaket korumasıdır (haftalık dalgalanmada satmasın).
ROTATION_PROFILE = {
    "STOP_LOSS_PCT": 20.0,
    "BACKUP_STOP_PCT": 25.0,           # Binance'teki yedek stop, zarar kesin altında beklemeli
    "MIN_SIGNAL_SCORE": 0.0,
    "MIN_ROOM_RATIO": 0.0,
}
# Ortalamaya dönüş (mean_revert, rsi_divergence): küçük hedef + daha geniş zarar kes + süre sınırı. Kazanma oranı
# yüksek olur ama kâr ancak kazananlar kaybedenleri karşılıyorsa çıkar (backtest'te işlem başı sonuca bakın).
MEAN_REVERSION_PROFILE = {
    "TAKE_PROFIT_PCT": 1.5,
    "STOP_LOSS_PCT": 5.0,
    "TRAILING_ACTIVATION_PCT": 1.0,
    "TRAILING_FLOOR_PCT": 0.5,
    "MAX_HOLD_MINUTES": 2880,          # 2 gün: dönmeyen düşüşte beklenmez
    "BREAKEVEN_TRIGGER_PCT": 0.7,
    "BREAKEVEN_FLOOR_PCT": 0.2,
    "MIN_SIGNAL_SCORE": 0.0,
    "MIN_ROOM_RATIO": 0.0,
    "BACKUP_STOP_PCT": 8.0,
}
PROFILE_ATTRS = {
    "TAKE_PROFIT_PCT": "take_profit_pct", "STOP_LOSS_PCT": "stop_loss_pct",
    "TRAILING_ACTIVATION_PCT": "trailing_activation_pct", "TRAILING_FLOOR_PCT": "trailing_floor_pct",
    "MAX_HOLD_MINUTES": "max_hold_minutes", "BREAKEVEN_TRIGGER_PCT": "breakeven_trigger_pct",
    "BREAKEVEN_FLOOR_PCT": "breakeven_floor_pct", "MIN_SIGNAL_SCORE": "min_signal_score",
    "MIN_ROOM_RATIO": "min_room_ratio", "MAX_24H_GAIN_PCT": "max_24h_gain_pct",
    "BACKUP_STOP_PCT": "backup_stop_pct",
}
PROFILES = {"swing": SWING_EXIT_PROFILE, "mr": MEAN_REVERSION_PROFILE, "rotation": ROTATION_PROFILE}


def is_long_horizon(strategy: str) -> bool:
    """1 saatlik mumlarla günlerce tutulan (swing) bir strateji mi?"""
    return strategy in LONG_HORIZON_STRATEGIES


def is_mean_reversion(strategy: str) -> bool:
    """1 saatlik mumlarla en hacimli coinlerde düşüş sonrası dönüşü alan (ortalamaya dönüş / RSI uyumsuzluğu), küçük
    hedefli strateji mi?"""
    return strategy in MEAN_REVERSION_STRATEGIES


def is_rotation(strategy: str) -> bool:
    """Haftalık momentum rotasyonu mu? (sinyal + kâr al yerine sıralamaya göre tut / sat)"""
    return strategy in ROTATION_STRATEGIES


def divergence_trading(cfg) -> bool:
    """RSI_DIV_TRADE ana stratejinin yanında çalışıyor mu? (Rotasyonda ve rsi_divergence'ın kendisinde çalışmaz.)"""
    return (getattr(cfg, "rsi_div_trade", "off") != "off" and not is_rotation(cfg.strategy)
            and cfg.strategy != "rsi_divergence")


def strategy_family(strategy: str) -> str:
    """Aynı çıkış mantığını ve mum aralığını paylaşan strateji grubu: 'short' (5 dk), 'swing' (1 saat, günler),
    'mr' (1 saat, ortalamaya dönüş / RSI uyumsuzluğu), 'rotation' (günlük)."""
    if is_rotation(strategy):
        return "rotation"
    if is_mean_reversion(strategy):
        return "mr"
    return "swing" if is_long_horizon(strategy) else "short"


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
    # Hedef alanı: düşüş öncesi zirveye kalan alan ≥ Kâr Al için gereken hareket × bu oran (0 = kapalı)
    min_room_ratio: float = 0.0
    # Giriş stratejisi: rebound (düşenlerde dip) | trend (yükselen trendde geri çekilme) | breakout (kırılım)
    strategy: str = "rebound"
    min_24h_gain_pct: float = 2.0          # trend / breakout: son 24 saatte en az bu kadar yükselmiş
    max_24h_gain_pct: float = 25.0         # ...ama bundan fazla değil (aşırı pompalanmış coinler hariç)
    trend_pullback_min_pct: float = 1.0    # trend: zirveden geri çekilme en az
    trend_pullback_max_pct: float = 5.0    # trend: ...en fazla (daha derini trend bozulması sayılır)
    breakout_lookback_bars: int = 48       # breakout: kırılacak zirvenin bakıldığı mum sayısı (48 × 5 dk = 4 saat)
    breakout_volume_ratio: float = 2.0     # breakout: kırılım mumunun hacmi ortalamanın en az bu katı
    # Uzun vadeli stratejiler (1 saatlik mumlar, 1-5 gün)
    swing_lookback_hours: int = 72         # swing: kırılacak zirvenin bakıldığı süre / swing_pullback: zirve penceresi
    swing_volume_ratio: float = 1.5        # swing: kırılım mumunun hacmi son 24 saatin ortalamasının en az bu katı
    swing_pullback_min_pct: float = 4.0    # swing_pullback: zirveden geri çekilme en az
    swing_pullback_max_pct: float = 12.0   # swing_pullback: ...en fazla
    # Haftalık momentum rotasyonu (STRATEGY=rotation; günlük mumlar)
    rotation_top_n: int = 3                # Aynı anda tutulacak en güçlü coin sayısı (bakiye yetmezse azalır)
    rotation_lookback_days: int = 28       # Momentum: son kaç günün getirisine göre sıralanır
    rotation_days: int = 7                 # Kaç günde bir yeniden dengelenir
    rotation_btc_ma_days: int = 50         # BTC bu günlük ortalamanın altına inince her şey satılıp USDT'de beklenir (0 = kapalı)
    rotation_universe: int = 30            # Sıralamaya giren en hacimli coin sayısı
    # Ortalamaya dönüş (STRATEGY=mean_revert; 1 saatlik mumlar)
    mr_band_std: float = 2.0               # Kapanış 20 saatlik ortalamanın en az bu kadar standart sapma altında olmalı
    mr_universe: int = 20                  # Yalnızca en hacimli bu kadar coin (büyük, likit coinler)
    signal_confirm_scans: int = 1          # 1 = kapalı (2 = sinyal art arda 2 taramada görülmeli)
    # 31) Oynaklık (ATR) filtresi — 5 dk ATR(14) / fiyat (%)
    min_atr_pct: float = 0.25
    max_atr_pct: float = 4.0
    # 33) Piyasa rejimi
    regime_filter: bool = True
    regime_skip_weak: bool = False         # true: piyasa ZAYIF iken hiç alım yapma (yalnızca sıkılaştırmak yerine)
    # 37) Ayrıntılı kayıt
    equity_log_minutes: int = 15           # 0 = kapalı
    # 38) BNB ile komisyon indirimi takibi: yalnızca izler ve uyarır (BNB ALMAZ, Binance ayarını değiştirmez)
    bnb_fee_check: bool = True             # false = komisyon/BNB kontrolü tamamen kapalı
    bnb_min_trades: int = 20               # BNB bundan az al-sat işlemine yetecekse uyar (0 = uyarma)
    # 39) Telegram kontrol paneli (/menu) ve bildirim modu
    notify_mode: str = "all"               # all | closes | silent (bkz. NOTIFY_MODES)
    service_name: str = "tradebot.service"  # Menüdeki "Sunucu Reset" düğmesinin yeniden başlattığı systemd servisi
    ip_check_minutes: int = 10             # Sunucunun dış IP adresi bu aralıkla kontrol edilir, değişirse alarm (0 = kapalı)
    # 40) Ek giriş filtreleri (hepsi varsayılan kapalı; --tara-ab ile etkileri ölçülebilir)
    spread_filter: bool = True             # false: MAX_SPREAD_PCT kontrolü yapılmaz
    fakeout_filter: bool = False           # Son kapanmış mumun üst fitili gövdesinden uzunsa alma
    vwap_filter: bool = False              # Fiyat VWAP'ın (son VWAP_BARS mum) altındaysa alma
    vwap_bars: int = 48                    # 48 × 5 dk = 4 saat (1 saatlik stratejilerde 2 gün)
    overheat_filter: bool = False          # Son OVERHEAT_MINUTES dakikada dipten OVERHEAT_PCT'den fazla yükseldiyse alma
    overheat_pct: float = 10.0
    overheat_minutes: int = 60
    ob_wall_filter: bool = False           # Canlı: başabaş bandında (+OB_BAND_PCT) dev satış duvarı varsa alma
    ob_band_pct: float = 1.0
    ob_wall_ratio: float = 10.0            # Duvar = tek fiyat seviyesi, defterdeki ortanca seviyenin en az bu katı
    ob_imbalance_filter: bool = False      # Canlı: ±OB_BAND_PCT bandında satış emirleri alışların bu katıysa alma
    ob_imbalance_ratio: float = 5.0
    tape_filter: bool = False              # Canlı: son TAPE_SECONDS sn'deki piyasa alımlarının payı düşükse alma
    tape_seconds: int = 60
    tape_min_buy_ratio: float = 0.55
    # 41) Risk sigortaları (yeni alımları dondurur; açık pozisyonların takibi sürer)
    daily_loss_limit_pct: float = 0.0      # Günlük net zarar sermayenin bu %'sine ulaşınca gün sonuna kadar alım yok
    daily_profit_target_pct: float = 0.0   # Günlük net kâr bu %'ye ulaşınca gün sonuna kadar alım yok
    loss_streak_limit: int = 0             # Üst üste bu kadar zarar kes olursa LOSS_STREAK_PAUSE_HOURS alım yok
    loss_streak_pause_hours: float = 4.0
    coin_sl_limit: int = 3                 # Aynı coinde üst üste bu kadar zarar kes olursa 24 saat kara liste teklifi
    slippage_alert_pct: float = 0.3        # Son 10 alımın ortalama kayması bunu aşarsa uyar (0 = kapalı)
    # 42) Akıllı pozisyon yönetimi (hepsi varsayılan kapalı; --tara-ab ile "auto" halleri denenebilir)
    atr_targets: bool = False              # Kâr Al / Zarar Kes'i coinin 1 saatlik ATR'sine göre belirle
    atr_tp_mult: float = 3.0               # Kâr Al = ATR% × bu (ATR_TP_MIN_PCT…ATR_TP_MAX_PCT arası)
    atr_sl_mult: float = 2.0               # Zarar Kes = ATR% × bu (ATR_SL_MIN_PCT…STOP_LOSS_PCT arası: yalnızca daraltır)
    atr_tp_min_pct: float = 1.0
    atr_tp_max_pct: float = 15.0
    atr_sl_min_pct: float = 1.0
    tp_extend: bool = False                # Kâr Al'da momentum güçlüyse satma, zirveden TP_EXTEND_GAP_PCT izle
    tp_extend_gap_pct: float = 0.5
    dca_mode: str = "off"                  # Zarardaki pozisyona yedek akçeyle ekleme (ortalama düşürme)
    dca_trigger_pct: float = 5.0           # Net zarar bu yüzdeye inince
    pyramid_mode: str = "off"              # Kârdaki pozisyona yedek akçeyle ekleme
    pyramid_trigger_pct: float = 3.0       # Net kâr bu yüzdeye çıkınca (momentum da güçlü olmalı)
    time_exit_mode: str = "off"            # TIME_EXIT_MINUTES dolunca pozisyon yataysa "başabaşta satılsın mı?"
    time_exit_minutes: int = 180
    time_exit_band_pct: float = 0.5        # Net K/Z ±bu aralıktaysa "yatay"
    escape_mode: str = "off"               # Hacimli satış baskısında zarar kesi beklemeden çıkış
    escape_trigger_pct: float = 4.0        # Net zarar bu yüzdeye inmiş olmalı
    escape_volume_ratio: float = 1.5       # Son kırmızı mumun hacmi ortalamanın en az bu katı
    # 43) Akıllı giriş
    trailing_buy: bool = False             # Sinyalden sonra fiyat düştükçe bekle, dipten TRAILING_BUY_PCT dönünce al
    trailing_buy_pct: float = 0.3
    trailing_buy_minutes: int = 15         # Bu sürede dönüş olmazsa sinyal iptal
    signal_approval: str = "off"           # off | volatile (yalnızca oynak / likidasyonlu piyasada sor) | all
    approval_atr_pct: float = 3.0          # 1 saatlik ATR% bunu aşarsa "oynak"
    approval_liq_usdt: float = 100_000.0   # Son 5 dk'da coinde bu kadar long likidasyonu varsa "oynak"
    approval_timeout_sec: int = 120        # Onay bu sürede gelmezse alım yapılmaz
    liq_feed: bool = False                 # Binance vadeli işlemler likidasyon akışını dinle (WebSocket)
    liq_alert_usdt: float = 0.0            # Bir coinde 5 dk'da bu kadar long likidasyonu olursa bildir (0 = kapalı)
    rsi_div_alerts: bool = False           # En hacimli coinlerde 1 saatlik pozitif RSI uyumsuzluğu bildirimi
    rsi_div_scan_minutes: int = 60
    # Pozitif RSI uyumsuzluğuyla alım: STRATEGY=rsi_divergence (ana strateji), RSI_DIV_TRADE (ana stratejinin yanında)
    # ve RSI_DIV_FILTER (diğer stratejilere teyit). Uyumsuzluk eşikleri bildirim için de geçerlidir.
    rsi_div_oversold: float = 35.0         # İlk dipte RSI en fazla bu olmalı
    rsi_div_rise: float = 3.0              # İkinci (daha düşük) dipte RSI en az bu kadar yüksek olmalı
    rsi_div_universe: int = 30             # rsi_divergence / RSI_DIV_TRADE: yalnızca en hacimli bu kadar coin
    rsi_div_trend_filter: bool = False     # true: yalnızca fiyat 1 saatlik EMA200'ün (≈ 8 gün) üstündeyken al
    rsi_div_trade: str = "off"             # Ana stratejinin yanında uyumsuzluk + teyitle alım: off | ask | auto
    rsi_div_tp_pct: float = 0.0            # RSI_DIV_TRADE işlemlerinin Kâr Al'ı (0 = TAKE_PROFIT_PCT)
    rsi_div_sl_pct: float = 0.0            # ...ve Zarar Kes'i (0 = STOP_LOSS_PCT; ondan büyük olamaz)
    rsi_div_filter: bool = False           # Diğer stratejilere teyit: yakın zamanda pozitif uyumsuzluk yoksa alma
    # 44) Araçlar
    pnl_board_sec: int = 5                 # /pano: canlı K/Z mesajı bu aralıkla güncellenir
    pnl_board_minutes: int = 30            # ...ve bu süre sonra kendiliğinden durur
    bnb_topup_usdt: float = 6.0            # BNB azalınca "şu kadar USDT'lik BNB alınsın mı?" (0 = sorma)
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
        if self.min_room_ratio < 0:
            errors.append("MIN_ROOM_RATIO negatif olamaz (0 = kapalı)")
        if self.strategy not in STRATEGY_NAMES:
            errors.append("STRATEGY şunlardan biri olmalı: " + ", ".join(STRATEGY_NAMES))
        if not 0 <= self.min_24h_gain_pct < self.max_24h_gain_pct:
            errors.append("MIN_24H_GAIN_PCT, MAX_24H_GAIN_PCT'den küçük olmalı (ikisi de 0 veya pozitif)")
        if not 0 < self.trend_pullback_min_pct < self.trend_pullback_max_pct:
            errors.append("TREND_PULLBACK_MIN_PCT, TREND_PULLBACK_MAX_PCT'den küçük ve sıfırdan büyük olmalı")
        if not 12 <= self.breakout_lookback_bars <= 200:
            errors.append("BREAKOUT_LOOKBACK_BARS 12 ile 200 arasında olmalı")
        if self.breakout_volume_ratio <= 0:
            errors.append("BREAKOUT_VOLUME_RATIO sıfırdan büyük olmalı")
        if not 24 <= self.swing_lookback_hours <= 240:
            errors.append("SWING_LOOKBACK_HOURS 24 ile 240 (10 gün) arasında olmalı")
        if self.swing_volume_ratio <= 0:
            errors.append("SWING_VOLUME_RATIO sıfırdan büyük olmalı")
        if not 0 < self.swing_pullback_min_pct < self.swing_pullback_max_pct < 50:
            errors.append("SWING_PULLBACK_MIN_PCT, SWING_PULLBACK_MAX_PCT'den küçük olmalı (ikisi de 0-50 arası)")
        if not 1 <= self.rotation_top_n <= 10:
            errors.append("ROTATION_TOP_N 1 ile 10 arasında olmalı")
        if not 7 <= self.rotation_lookback_days <= 180:
            errors.append("ROTATION_LOOKBACK_DAYS 7 ile 180 arasında olmalı")
        if not 1 <= self.rotation_days <= 30:
            errors.append("ROTATION_DAYS 1 ile 30 arasında olmalı")
        if not (self.rotation_btc_ma_days == 0 or 10 <= self.rotation_btc_ma_days <= 200):
            errors.append("ROTATION_BTC_MA_DAYS 0 (kapalı) veya 10 ile 200 arasında olmalı")
        if not self.rotation_top_n <= self.rotation_universe <= 200:
            errors.append("ROTATION_UNIVERSE, ROTATION_TOP_N'den küçük olmamalı (en fazla 200)")
        if not 0.5 <= self.mr_band_std <= 5:
            errors.append("MR_BAND_STD 0.5 ile 5 arasında olmalı (örn. 2 = ortalamanın 2 standart sapma altı)")
        if not 1 <= self.mr_universe <= 200:
            errors.append("MR_UNIVERSE 1 ile 200 arasında olmalı")
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
        if self.notify_mode not in NOTIFY_MODES:
            errors.append("NOTIFY_MODE şunlardan biri olmalı: " + ", ".join(NOTIFY_MODES))
        if self.ip_check_minutes < 0:
            errors.append("IP_CHECK_MINUTES 0 (kapalı) veya pozitif olmalı")
        if not 5 <= self.vwap_bars <= 500:
            errors.append("VWAP_BARS 5 ile 500 arasında olmalı")
        if self.overheat_pct <= 0 or self.overheat_minutes <= 0:
            errors.append("OVERHEAT_PCT ve OVERHEAT_MINUTES sıfırdan büyük olmalı")
        if not 0 < self.ob_band_pct <= 10:
            errors.append("OB_BAND_PCT 0-10 arasında olmalı")
        if self.ob_wall_ratio <= 1 or self.ob_imbalance_ratio <= 1:
            errors.append("OB_WALL_RATIO ve OB_IMBALANCE_RATIO 1'den büyük olmalı")
        if not 5 <= self.tape_seconds <= 600 or not 0 < self.tape_min_buy_ratio < 1:
            errors.append("TAPE_SECONDS 5-600 arasında, TAPE_MIN_BUY_RATIO 0 ile 1 arasında olmalı")
        if not 0 <= self.daily_loss_limit_pct < 100 or self.daily_profit_target_pct < 0:
            errors.append("DAILY_LOSS_LIMIT_PCT 0-100 arasında, DAILY_PROFIT_TARGET_PCT 0 veya pozitif olmalı")
        if self.loss_streak_limit < 0 or self.coin_sl_limit < 0 or self.loss_streak_pause_hours <= 0:
            errors.append("LOSS_STREAK_LIMIT ve COIN_SL_LIMIT 0 (kapalı) veya pozitif, LOSS_STREAK_PAUSE_HOURS "
                          "sıfırdan büyük olmalı")
        if self.slippage_alert_pct < 0:
            errors.append("SLIPPAGE_ALERT_PCT 0 (kapalı) veya pozitif olmalı")
        for name in ("dca_mode", "pyramid_mode", "time_exit_mode", "escape_mode"):
            if getattr(self, name) not in ACTION_MODES:
                errors.append(f"{name.upper()} şunlardan biri olmalı: " + ", ".join(ACTION_MODES))
        if self.signal_approval not in SIGNAL_APPROVAL_MODES:
            errors.append("SIGNAL_APPROVAL şunlardan biri olmalı: " + ", ".join(SIGNAL_APPROVAL_MODES))
        if not (0 < self.atr_tp_min_pct < self.atr_tp_max_pct and 0 < self.atr_sl_min_pct
                and self.atr_tp_mult > 0 and self.atr_sl_mult > 0):
            errors.append("ATR_TP_MIN_PCT < ATR_TP_MAX_PCT, ATR_SL_MIN_PCT ve ATR çarpanları sıfırdan büyük olmalı")
        if self.tp_extend_gap_pct <= 0:
            errors.append("TP_EXTEND_GAP_PCT sıfırdan büyük olmalı")
        # Tetik zarar kesten önce gelmeli (yalnızca özellik açıkken: kapalıyken eski ayarlar hata vermesin)
        if self.dca_mode != "off" and not 0 < self.dca_trigger_pct < self.stop_loss_pct:
            errors.append("DCA_TRIGGER_PCT sıfırdan büyük ve STOP_LOSS_PCT'den küçük olmalı")
        if self.escape_mode != "off" and not 0 < self.escape_trigger_pct < self.stop_loss_pct:
            errors.append("ESCAPE_TRIGGER_PCT sıfırdan büyük ve STOP_LOSS_PCT'den küçük olmalı")
        if self.escape_volume_ratio <= 0:
            errors.append("ESCAPE_VOLUME_RATIO sıfırdan büyük olmalı")
        if self.pyramid_trigger_pct <= 0 or self.time_exit_minutes <= 0 or self.time_exit_band_pct < 0:
            errors.append("PYRAMID_TRIGGER_PCT ve TIME_EXIT_MINUTES sıfırdan büyük, TIME_EXIT_BAND_PCT negatif "
                          "olmamalı")
        if not 0 < self.trailing_buy_pct < 10 or self.trailing_buy_minutes <= 0:
            errors.append("TRAILING_BUY_PCT 0-10 arasında, TRAILING_BUY_MINUTES sıfırdan büyük olmalı")
        if self.approval_atr_pct <= 0 or self.approval_liq_usdt < 0 or not 10 <= self.approval_timeout_sec <= 3600:
            errors.append("APPROVAL_ATR_PCT pozitif, APPROVAL_LIQ_USDT negatif olmayan, APPROVAL_TIMEOUT_SEC 10-3600 "
                          "arasında olmalı")
        if self.liq_alert_usdt < 0 or self.rsi_div_scan_minutes < 5:
            errors.append("LIQ_ALERT_USDT negatif olamaz, RSI_DIV_SCAN_MINUTES en az 5 olmalı")
        if not 10 <= self.rsi_div_oversold <= 50 or not 0 <= self.rsi_div_rise <= 30:
            errors.append("RSI_DIV_OVERSOLD 10 ile 50, RSI_DIV_RISE 0 ile 30 arasında olmalı")
        if not 1 <= self.rsi_div_universe <= 200:
            errors.append("RSI_DIV_UNIVERSE 1 ile 200 arasında olmalı")
        if self.rsi_div_trade not in ACTION_MODES:
            errors.append("RSI_DIV_TRADE şunlardan biri olmalı: " + ", ".join(ACTION_MODES))
        # Yalnızca özellik açıkken (kapalıyken eski ayarlar hata vermesin); zarar kes yalnızca daralabilir: pozisyon
        # boyutu ve Binance yedek stop'u STOP_LOSS_PCT'ye göre hesaplanır
        if self.rsi_div_trade != "off" and (self.rsi_div_tp_pct < 0
                                            or not 0 <= self.rsi_div_sl_pct <= self.stop_loss_pct):
            errors.append("RSI_DIV_TP_PCT negatif olamaz; RSI_DIV_SL_PCT 0 (= STOP_LOSS_PCT) ile STOP_LOSS_PCT "
                          "arasında olmalı")
        if self.pnl_board_sec < 3 or self.pnl_board_minutes <= 0:
            errors.append("PNL_BOARD_SEC en az 3, PNL_BOARD_MINUTES sıfırdan büyük olmalı")
        if self.bnb_topup_usdt < 0:
            errors.append("BNB_TOPUP_USDT 0 (kapalı) veya pozitif olmalı")
        if errors:
            raise ConfigError("Ayar hataları:\n  - " + "\n  - ".join(errors))


SETTINGS_FILE = "ayarlar.env"   # GitHub'daki ayarlar (gizli bilgi İÇERMEZ); aynı klasördeki .env bunları ezer


def load_config(env_file: Optional[str] = None, mode_override: Optional[str] = None) -> Config:
    """Ayarları okuyup doğrulanmış bir Config döndürür.

    Öncelik: ortam değişkenleri > .env (sunucuda; API anahtarı ve Telegram token'ı burada, GitHub'a yüklenmez) >
    aynı klasördeki ayarlar.env (GitHub'daki strateji / risk ayarları) > koddaki varsayılanlar.
    """
    env_path = Path(env_file) if env_file else BASE_DIR / ".env"
    for path in (env_path, env_path.parent / SETTINGS_FILE):
        if path.exists():
            load_dotenv(path, override=False)   # Önce yüklenen kazanır

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
        min_room_ratio=_env_float("MIN_ROOM_RATIO", 0.0),
        strategy=_env_str("STRATEGY", "rebound").lower(),
        min_24h_gain_pct=_env_float("MIN_24H_GAIN_PCT", 2.0),
        max_24h_gain_pct=_env_float("MAX_24H_GAIN_PCT", 25.0),
        trend_pullback_min_pct=_env_float("TREND_PULLBACK_MIN_PCT", 1.0),
        trend_pullback_max_pct=_env_float("TREND_PULLBACK_MAX_PCT", 5.0),
        breakout_lookback_bars=_env_int("BREAKOUT_LOOKBACK_BARS", 48),
        breakout_volume_ratio=_env_float("BREAKOUT_VOLUME_RATIO", 2.0),
        swing_lookback_hours=_env_int("SWING_LOOKBACK_HOURS", 72),
        swing_volume_ratio=_env_float("SWING_VOLUME_RATIO", 1.5),
        swing_pullback_min_pct=_env_float("SWING_PULLBACK_MIN_PCT", 4.0),
        swing_pullback_max_pct=_env_float("SWING_PULLBACK_MAX_PCT", 12.0),
        rotation_top_n=_env_int("ROTATION_TOP_N", 3),
        rotation_lookback_days=_env_int("ROTATION_LOOKBACK_DAYS", 28),
        rotation_days=_env_int("ROTATION_DAYS", 7),
        rotation_btc_ma_days=_env_int("ROTATION_BTC_MA_DAYS", 50),
        rotation_universe=_env_int("ROTATION_UNIVERSE", 30),
        mr_band_std=_env_float("MR_BAND_STD", 2.0),
        mr_universe=_env_int("MR_UNIVERSE", 20),
        signal_confirm_scans=_env_int("SIGNAL_CONFIRM_SCANS", 1),
        min_atr_pct=_env_float("MIN_ATR_PCT", 0.25),
        max_atr_pct=_env_float("MAX_ATR_PCT", 4.0),
        regime_filter=_env_bool("REGIME_FILTER", True),
        regime_skip_weak=_env_bool("REGIME_SKIP_WEAK", False),
        equity_log_minutes=_env_int("EQUITY_LOG_MINUTES", 15),
        bnb_fee_check=_env_bool("BNB_FEE_CHECK", True),
        bnb_min_trades=_env_int("BNB_MIN_TRADES", 20),
        notify_mode=_env_str("NOTIFY_MODE", "all").lower(),
        service_name=_env_str("SERVICE_NAME", "tradebot.service"),
        ip_check_minutes=_env_int("IP_CHECK_MINUTES", 10),
        spread_filter=_env_bool("SPREAD_FILTER", True),
        fakeout_filter=_env_bool("FAKEOUT_FILTER", False),
        vwap_filter=_env_bool("VWAP_FILTER", False),
        vwap_bars=_env_int("VWAP_BARS", 48),
        overheat_filter=_env_bool("OVERHEAT_FILTER", False),
        overheat_pct=_env_float("OVERHEAT_PCT", 10.0),
        overheat_minutes=_env_int("OVERHEAT_MINUTES", 60),
        ob_wall_filter=_env_bool("OB_WALL_FILTER", False),
        ob_band_pct=_env_float("OB_BAND_PCT", 1.0),
        ob_wall_ratio=_env_float("OB_WALL_RATIO", 10.0),
        ob_imbalance_filter=_env_bool("OB_IMBALANCE_FILTER", False),
        ob_imbalance_ratio=_env_float("OB_IMBALANCE_RATIO", 5.0),
        tape_filter=_env_bool("TAPE_FILTER", False),
        tape_seconds=_env_int("TAPE_SECONDS", 60),
        tape_min_buy_ratio=_env_float("TAPE_MIN_BUY_RATIO", 0.55),
        daily_loss_limit_pct=_env_float("DAILY_LOSS_LIMIT_PCT", 0.0),
        daily_profit_target_pct=_env_float("DAILY_PROFIT_TARGET_PCT", 0.0),
        loss_streak_limit=_env_int("LOSS_STREAK_LIMIT", 0),
        loss_streak_pause_hours=_env_float("LOSS_STREAK_PAUSE_HOURS", 4.0),
        coin_sl_limit=_env_int("COIN_SL_LIMIT", 3),
        slippage_alert_pct=_env_float("SLIPPAGE_ALERT_PCT", 0.3),
        atr_targets=_env_bool("ATR_TARGETS", False),
        atr_tp_mult=_env_float("ATR_TP_MULT", 3.0),
        atr_sl_mult=_env_float("ATR_SL_MULT", 2.0),
        atr_tp_min_pct=_env_float("ATR_TP_MIN_PCT", 1.0),
        atr_tp_max_pct=_env_float("ATR_TP_MAX_PCT", 15.0),
        atr_sl_min_pct=_env_float("ATR_SL_MIN_PCT", 1.0),
        tp_extend=_env_bool("TP_EXTEND", False),
        tp_extend_gap_pct=_env_float("TP_EXTEND_GAP_PCT", 0.5),
        dca_mode=_env_str("DCA_MODE", "off").lower(),
        dca_trigger_pct=_env_float("DCA_TRIGGER_PCT", 5.0),
        pyramid_mode=_env_str("PYRAMID_MODE", "off").lower(),
        pyramid_trigger_pct=_env_float("PYRAMID_TRIGGER_PCT", 3.0),
        time_exit_mode=_env_str("TIME_EXIT_MODE", "off").lower(),
        time_exit_minutes=_env_int("TIME_EXIT_MINUTES", 180),
        time_exit_band_pct=_env_float("TIME_EXIT_BAND_PCT", 0.5),
        escape_mode=_env_str("ESCAPE_MODE", "off").lower(),
        escape_trigger_pct=_env_float("ESCAPE_TRIGGER_PCT", 4.0),
        escape_volume_ratio=_env_float("ESCAPE_VOLUME_RATIO", 1.5),
        trailing_buy=_env_bool("TRAILING_BUY", False),
        trailing_buy_pct=_env_float("TRAILING_BUY_PCT", 0.3),
        trailing_buy_minutes=_env_int("TRAILING_BUY_MINUTES", 15),
        signal_approval=_env_str("SIGNAL_APPROVAL", "off").lower(),
        approval_atr_pct=_env_float("APPROVAL_ATR_PCT", 3.0),
        approval_liq_usdt=_env_float("APPROVAL_LIQ_USDT", 100_000.0),
        approval_timeout_sec=_env_int("APPROVAL_TIMEOUT_SEC", 120),
        liq_feed=_env_bool("LIQ_FEED", False),
        liq_alert_usdt=_env_float("LIQ_ALERT_USDT", 0.0),
        rsi_div_alerts=_env_bool("RSI_DIV_ALERTS", False),
        rsi_div_scan_minutes=_env_int("RSI_DIV_SCAN_MINUTES", 60),
        rsi_div_oversold=_env_float("RSI_DIV_OVERSOLD", 35.0),
        rsi_div_rise=_env_float("RSI_DIV_RISE", 3.0),
        rsi_div_universe=_env_int("RSI_DIV_UNIVERSE", 30),
        rsi_div_trend_filter=_env_bool("RSI_DIV_TREND_FILTER", False),
        rsi_div_trade=_env_str("RSI_DIV_TRADE", "off").lower(),
        rsi_div_tp_pct=_env_float("RSI_DIV_TP_PCT", 0.0),
        rsi_div_sl_pct=_env_float("RSI_DIV_SL_PCT", 0.0),
        rsi_div_filter=_env_bool("RSI_DIV_FILTER", False),
        pnl_board_sec=_env_int("PNL_BOARD_SEC", 5),
        pnl_board_minutes=_env_int("PNL_BOARD_MINUTES", 30),
        bnb_topup_usdt=_env_float("BNB_TOPUP_USDT", 6.0),
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
    if cfg.rsi_div_trade != "off" and (is_rotation(cfg.strategy) or cfg.strategy == "rsi_divergence"):
        why = ("rotasyon pozisyonları haftalık sıralamaya göre yönetir" if is_rotation(cfg.strategy)
               else "strateji zaten uyumsuzlukla alıyor")
        notes.append(f"RSI_DIV_TRADE={cfg.rsi_div_trade} ana stratejinin YANINDA uyumsuzluk alımı içindir; "
                     f"STRATEGY={cfg.strategy} iken etkisi yok ({why}).")
    if is_long_horizon(cfg.strategy):
        short = short_exit_settings(cfg)
        if short:
            notes.append(f"STRATEGY={cfg.strategy} 1 saatlik mumlarla 1-5 gün süren işlemler içindir ama çıkış "
                         f"ayarlarınız kısa vadeli: {', '.join(short)}. Bu ayarlar işlemleri hareket başlamadan "
                         f"kapatır. Önerilen: " + " ".join(
                             f"{k}={max(v, cfg.max_24h_gain_pct) if k == 'MAX_24H_GAIN_PCT' else v:g}"
                             for k, v in SWING_EXIT_PROFILE.items()))
        filters = short_entry_filters(cfg)
        if filters:
            notes.append(f"STRATEGY={cfg.strategy} ile şu giriş filtreleri sinyalleri gereksiz yere eleyebilir: "
                         f"{'; '.join(filters)}.")
    if is_mean_reversion(cfg.strategy):
        odd = []
        if cfg.take_profit_pct > 5:
            odd.append(f"TAKE_PROFIT_PCT={cfg.take_profit_pct:g}")
        if cfg.max_hold_minutes <= 0:
            odd.append("MAX_HOLD_MINUTES=0 (süre sınırı yok)")
        if odd:
            notes.append(f"STRATEGY={cfg.strategy} düşüş sonrası dönüşü küçük hedefle (saatler-2 gün) alır; şu "
                         f"ayarlar ona uymuyor: {', '.join(odd)}. Önerilen: " + " ".join(
                             f"{k}={v:g}" for k, v in MEAN_REVERSION_PROFILE.items()))
    if is_rotation(cfg.strategy):
        if cfg.stop_loss_pct < 10:
            notes.append(f"STRATEGY=rotation coinleri haftalarca tutar; STOP_LOSS_PCT={cfg.stop_loss_pct:g} haftalık "
                         f"dalgalanmada bile satar. Rotasyonda zarar kes yalnızca felaket korumasıdır, önerilen: "
                         + " ".join(f"{k}={v:g}" for k, v in ROTATION_PROFILE.items()))
        if cfg.rotation_top_n > cfg.max_open_positions:
            notes.append(f"ROTATION_TOP_N={cfg.rotation_top_n} ama MAX_OPEN_POSITIONS={cfg.max_open_positions}: "
                         f"en fazla {cfg.max_open_positions} coin tutulur.")
        return notes   # Kâr al / izleyen stop / başabaş rotasyonda kullanılmaz: onlarla ilgili uyarılar anlamsız
    if cfg.regime_skip_weak and not cfg.regime_filter:
        notes.append("REGIME_SKIP_WEAK=true yalnızca REGIME_FILTER=true iken çalışır; şu an etkisi yok.")
    if cfg.breakeven_enabled:
        if not breakeven_effective(cfg):
            first_exit = min(cfg.take_profit_pct, cfg.trailing_activation_pct)
            hint = round(first_exit / 2, 1)
            tip = (f" Örn. BREAKEVEN_TRIGGER_PCT={hint:g} yapın." if hint > cfg.breakeven_floor_pct else "")
            notes.append(f"Başabaş koruması açık ama BREAKEVEN_TRIGGER_PCT ({cfg.breakeven_trigger_pct:g}), "
                         f"izleyen stop / kâr al seviyesinden (+%{first_exit:g}) küçük olmadığı için hiç "
                         f"devreye girmez.{tip}")
    return notes


def short_exit_settings(cfg) -> List[str]:
    """Uzun vadeli strateji için fazla kısa vadeli görünen çıkış ayarları (boş liste = sorun yok)."""
    out: List[str] = []
    if 0 < cfg.max_hold_minutes < 1440:
        out.append(f"MAX_HOLD_MINUTES={cfg.max_hold_minutes} (1 günden kısa)")
    if cfg.take_profit_pct < 6:
        out.append(f"TAKE_PROFIT_PCT={cfg.take_profit_pct:g}")
    if cfg.trailing_activation_pct < 3:
        out.append(f"TRAILING_ACTIVATION_PCT={cfg.trailing_activation_pct:g}")
    if cfg.breakeven_enabled and cfg.breakeven_trigger_pct < 3:
        out.append(f"BREAKEVEN_TRIGGER_PCT={cfg.breakeven_trigger_pct:g}")
    return out


def short_entry_filters(cfg) -> List[str]:
    """Uzun vadeli stratejide sinyalleri gereksiz yere eleyen (çoğu zaman 5 dk stratejisinden kalmış) giriş filtreleri.

    Yalnızca kesin sorunlar: MIN_SIGNAL_SCORE gibi swing taramasının da önerebileceği değerler uyarılmaz.
    """
    out: List[str] = []
    if cfg.strategy == "swing_pullback" and cfg.min_room_ratio > 0:
        need = ((1 + cfg.take_profit_pct / 100) / (1 - cfg.fee_rate) ** 2 - 1) * 100   # strategy.required_move_pct
        max_room = cfg.swing_pullback_max_pct / (100 - cfg.swing_pullback_max_pct) * 100
        if cfg.min_room_ratio * need > max_room:
            out.append(f"MIN_ROOM_RATIO={cfg.min_room_ratio:g} (en derin geri çekilmede bile zirveye kalan alan "
                       f"~%{max_room:.1f}, gereken %{cfg.min_room_ratio * need:.1f}: swing_pullback hiç alım yapamaz)")
    if cfg.max_24h_gain_pct < 15:
        out.append(f"MAX_24H_GAIN_PCT={cfg.max_24h_gain_pct:g} (24 saatte bundan fazla yükselen coinler alınmaz; "
                   f"5 dk stratejisi için seçildiyse 25 yapın)")
    return out


def with_strategy(cfg: Config, strategy: str) -> Tuple[Config, bool]:
    """Backtest'te --strateji: stratejiyi değiştirir. (yeni ayarlar, çıkış profili uygulandı mı)

    .env'deki strateji başka bir gruptansa (5 dk / swing / rotasyon) .env'deki çıkış ayarları o gruba göredir; yeni
    grubun profili (SWING_EXIT_PROFILE / ROTATION_PROFILE) kullanılır. 5 dk grubunun profili yoktur. Aynı gruptan bir
    stratejiye geçilirse (çıkışlar ona göre ayarlanmış sayılır) yalnızca strateji değişir.
    """
    if strategy == cfg.strategy:
        return cfg, False
    changes = {"strategy": strategy}
    family = strategy_family(strategy)
    profile = family != strategy_family(cfg.strategy) and family in PROFILES
    if profile:
        # Profildeki türle (1.5 → float, 2880 → int): mevcut değerin türüne çevirmek 1.5'i 1'e kırpabilirdi
        changes.update({PROFILE_ATTRS[k]: v for k, v in PROFILES[family].items()})
        if family == "swing":
            # Üst sınır: .env'deki daha geniş değer korunur (yalnızca 5 dk için daraltılmışsa genişletilir)
            changes["max_24h_gain_pct"] = max(cfg.max_24h_gain_pct, SWING_EXIT_PROFILE["MAX_24H_GAIN_PCT"])
    return replace(cfg, **changes), profile
