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
# coini tutar, her hafta yeniden dengeler; bkz. strategy.py ve rotation.py). intraday yalnızca en büyük coinlerde
# (INTRADAY_COINS) 5 dk mumlarla gün içi al-sat yapar; giriş taktiği INTRADAY_TACTIC ile seçilir.
STRATEGY_NAMES = ("rebound", "trend", "breakout", "intraday", "swing", "swing_pullback", "mean_revert",
                  "rsi_divergence", "rotation", "futures")
LONG_HORIZON_STRATEGIES = frozenset({"swing", "swing_pullback"})
MEAN_REVERSION_STRATEGIES = frozenset({"mean_revert", "rsi_divergence"})
ROTATION_STRATEGIES = frozenset({"rotation"})
INTRADAY_STRATEGIES = frozenset({"intraday"})
# Vadeli işlem scalping (Binance USDⓈ-M, kaldıraçlı long / short; ayrı motor: futures_trader.FuturesBot). Giriş: son
# kapanan 5 dk mumda likidasyon dalgası + hacim + RSI uyumsuzluğu + BTC 1 saatlik trendi aynı anda (bkz. strategy.py)
FUTURES_STRATEGIES = frozenset({"futures"})
# Vadeli giriş taktiği:
#   crash = çöküş dönüşü: son FUTURES_CRASH_HOURS saatte en az FUTURES_CRASH_DROP_PCT düşen coinde (likidasyon
#           şelalesi) 5 dk mum kapanışında long. 2020-2026 testinde seçim ve doğrulama döneminde kârlı (bkz. README).
#   liq4  = ilk sürüm: likidasyon dalgası + hacim + RSI uyumsuzluğu + BTC 1 saatlik trendi (long / short). Aynı testte
#           7 yılın 7'sinde zarar etti; yalnızca karşılaştırma için.
FUTURES_TACTICS = ("crash", "liq4")
# Çöküş teyidi (izleme modu): close = çöküş mumunun kapanışında hemen al (ilk sürüm) | volume = çöküşten sonraki
# FUTURES_CONFIRM_BARS mum içinde hacmi ortalamanın FUTURES_CONFIRM_VOLUME_RATIO katına çıkan mumda (kapitülasyon) |
# green = aynı süre içinde ilk yeşil (kapanışı açılışından yüksek) mumda | either = ikisinden hangisi önce gelirse
FUTURES_CONFIRMS = ("close", "volume", "green", "either")
FUTURES_MAX_CRASH_EXTRA = 6             # FUTURES_CRASH_EXTRA: en fazla 6 ek pencere
# Çöküş taktiğine VEYA ile eklenen giriş modülleri (Faz 3; hepsi varsayılan kapalı):
#   pump     = pompa dönüşü SHORT: son H saatte en az %X YÜKSELEN coinde 5 dk mum kapanışında short (FUTURES_PUMP_RULES)
#   bb       = Bollinger (20, FUTURES_BB_STD) + RSI(14): kapanış alt bandın altında ve RSI < FUTURES_BB_RSI_LOW → long,
#              üst bandın üstünde ve RSI > FUTURES_BB_RSI_HIGH → short (FUTURES_BB: off | both | long | short)
#   breakout = hacimli kırılım LONG: hacim önceki 20 mumun FUTURES_BREAKOUT_VOLUME_RATIO katından fazla ve kapanış son
#              FUTURES_BREAKOUT_HOURS saatin en yükseğinin üstünde
# Faz 4 (Kasım 2026; hepsi varsayılan kapalı, yalnızca LONG):
#   funding  = fonlama sıkışması: son ödenen fonlama oranı (8 saate oranlanmış) FUTURES_FUNDING_RATE_PCT veya altında
#              (ör. -0.10 = short'lar long'lara 8 saatte %0.10 ödüyor) VE FUTURES_FUNDING_RULES çöküşü (ör. 1:5)
#   pullback = trend içi düzeltme: son tamamlanan 1 saatlik kapanış 1 saatlik EMA(FUTURES_PULLBACK_EMA) üstünde ve
#              5 dk RSI(14) < FUTURES_PULLBACK_RSI
# Aynı anda birden çok sinyal varsa öncelik: çöküş > fonlama > pompa > düzeltme > kırılım > Bollinger (tek pozisyon).
FUTURES_BB_MODES = ("off", "both", "long", "short")
# Giriş emri: market = piyasa emri (taker) | limit = alışta en iyi alış (bid) fiyatının FUTURES_LIMIT_OFFSET_PCT
# altına post-only limit emir (maker); FUTURES_LIMIT_TTL_MIN dakikada dolmazsa iptal (fırsat kaçar)
FUTURES_ENTRY_ORDERS = ("market", "limit")
# Evren sıralaması: avg = son FUTURES_UNIVERSE_AVG_DAYS günün ortalama hacmi | daily = bir önceki UTC gününün USDT
# hacmi (her gün 00:00 UTC'de yenilenir: "son 24 saatte en çok hacim yapan N coin")
FUTURES_UNIVERSE_MODES = ("avg", "daily")
# Dalga modu (Kasım 2026, Faz 5 araştırması; varsayılan kapalı). "Genişlik" = taranan coinlerden son 1 saatte en az
# %3 düşenlerin sayısı (o 5 dk mumun kapanışında). Çöküş dönüşünün kârı piyasa geneli likidasyon dalgalarından geliyor:
# 8+ coin birlikte düşerken işlem başı ~+%0.46, tek coin düşerken ~%0. Buna göre:
#   FUTURES_MAX_POSITIONS = aynı anda en fazla bu kadar pozisyon (1 = eskisi gibi tek pozisyon); her pozisyonun teminatı
#       bakiyenin FUTURES_MARGIN_USAGE_PCT / FUTURES_MAX_POSITIONS'ı. 2. ve sonraki pozisyon yalnızca genişlik
#       FUTURES_EXTRA_BREADTH veya üstündeyken açılır.
#   FUTURES_ISOLATED_SIZE_PCT = genişlik FUTURES_ISOLATED_BREADTH'in altındaysa ("yalnız çöküş") pozisyon büyüklüğü (%);
#       100 = tam (kapalı), 50 = yarım.
FUTURES_MAX_POSITIONS_LIMIT = 5
# Telegram → 🌊 Dalga modu → Faz 5 seti (2020-2026, 1 dk): 2 pozisyon, 3x, ek pozisyon ≥8 coin, yalnız çöküş yarım.
# #2'nin giriş / çıkış ayarlarıyla birlikte kullanılır (onlar değişmez). Daha çok işlem ve kâr, ama daha yüksek risk
# (kötümser stop dolumunda Mayıs 2021'de eridi); ayrıntı README.
FUTURES_CASCADE_PRESET = {"FUTURES_MAX_POSITIONS": 2, "FUTURES_LEVERAGE": 3, "FUTURES_EXTRA_BREADTH": 8,
                          "FUTURES_ISOLATED_SIZE_PCT": 50, "FUTURES_ISOLATED_BREADTH": 2}
# Faz 6 (Kasım 2026; hepsi varsayılan kapalı, Faz 5 ayarları ve seti değişmez):
#   FUTURES_REENTRY_BREADTH = genişlik bu değer veya üstündeyken zarar sonrası bekleme (LOSS_COOLDOWN_MINUTES)
#       uygulanmaz: dalgada zarar kesen coine yeniden girilebilir (SYMBOL_COOLDOWN_MINUTES yine geçerli). 0 = kapalı.
#       Testte dalgada zarar sonrası yeniden girişler en kârlı işlemlerdi (12+ coin: işlem başı +%1.10, sıradan dalga
#       işlemi +%0.69); yalnız çöküşte ~%0 (orada bekleme sürer).
#   FUTURES_WAVE_RULES + FUTURES_WAVE_BREADTH = genişlik FUTURES_WAVE_BREADTH veya üstündeyken ek çöküş pencereleri
#       ("saat:%", ör. 1:5,4:8 = son 1 saatte %5 VEYA 4 saatte %8 düşüş de sinyal); boş = kapalı.
#   FUTURES_EXTRA_SIZE_PCT = dalga modunda ek pozisyonların (2., 3. ...) büyüklüğü, ilk pozisyonun yüzdesi olarak;
#       100 = hepsi eşit (Faz 5, eskisi gibi). Teminat ağırlıkları 1, x, x, ... (x = %/100): ilk pozisyon kullanılabilir
#       bakiyenin 1 / (1 + (N-1)·x)'i, her ek pozisyon x / (1 + (N-1)·x)'i. 3x ve (N-1)·x = 0.5 iken ilk pozisyon #2 ile
#       aynı büyüklükte (bakiyenin 1.9 katı) ve hepsi açıkken toplam Faz 5 kadar (2.85 katı).
# Telegram → 🌊 Dalga modu → ⭐ Faz 6 seti (2020-2026, 1 dk; #2'nin giriş / çıkışlarıyla): 4 pozisyon (ilk tam + 3 ek ×
# 1/6), 3x, dalgada yeniden giriş, dalga eşikleri. ⭐ Faz 6 tek pozisyon: aynı ekler tek pozisyonla. Ayrıntı README.
FUTURES_PHASE6_PRESET = {"FUTURES_MAX_POSITIONS": 4, "FUTURES_LEVERAGE": 3, "FUTURES_EXTRA_BREADTH": 8,
                         "FUTURES_ISOLATED_SIZE_PCT": 50, "FUTURES_ISOLATED_BREADTH": 2, "FUTURES_EXTRA_SIZE_PCT": 16.7,
                         "FUTURES_REENTRY_BREADTH": 3, "FUTURES_WAVE_RULES": "1:5,4:8", "FUTURES_WAVE_BREADTH": 12,
                         "LOSS_COOLDOWN_MINUTES": 240}
FUTURES_PHASE6_SINGLE_PRESET = {"FUTURES_MAX_POSITIONS": 1, "FUTURES_LEVERAGE": 2, "FUTURES_ISOLATED_SIZE_PCT": 50,
                                "FUTURES_ISOLATED_BREADTH": 2, "FUTURES_EXTRA_SIZE_PCT": 100,
                                "FUTURES_REENTRY_BREADTH": 3, "FUTURES_WAVE_RULES": "1:5,4:8",
                                "FUTURES_WAVE_BREADTH": 12, "LOSS_COOLDOWN_MINUTES": 240}
# Faz 6 eklerinin kapalı hâli (Faz 5 seti ve "dalga modunu kapat" bunları da yazar: Faz 5 tam olarak Faz 5 kalır)
FUTURES_PHASE6_OFF = {"FUTURES_EXTRA_SIZE_PCT": 100, "FUTURES_REENTRY_BREADTH": 0, "FUTURES_WAVE_RULES": ""}
# Faz 8 (Kasım 2026, rapor_14 araştırması; varsayılan kapalı, Faz 5 / Faz 6 ayarları ve setleri değişmez):
#   FUTURES_EXTRA_BE_TRIGGER_PCT = dalga modunda EK pozisyonların (başka pozisyon açıkken açılanlar) başabaş tetiği
#       (fiyat %); 0 = ana ayarla (BREAKEVEN_TRIGGER_PCT) aynı. Taban (BREAKEVEN_FLOOR_PCT) ortaktır.
#       Dalgada ek yuvalar hep doludur (sinyallerin çoğu "açık pozisyon vardı" diye atlanır): ek pozisyon küçük kârı
#       erken kilitleyip kapanınca yuvası hemen yeni bir sinyale geçer. İlk (büyük) pozisyonda aynı şey kârı düşürür:
#       o kapanınca yerine gelen sinyal küçük ek pozisyon olarak açılır. İlk test (1 dk mumlu eski motor) kârın da
#       arttığını gösterdi; DÜZELTME (rapor 15): o artış motorun iyimserliğiydi (1 dk mum, dar aralıkta fiyatın tabana
#       geri dönüşünü görmüyordu). Saniyelik veriyle düzeltilmiş testte (Faz 6 seti): yılda ~780 → ~925 işlem, yıllık
#       getiri aynı (+%270 / +%166 → +%246 / +%168), düşüş ve maliyet hassasiyeti daha yüksek. Öneri: kapalı.
FUTURES_PHASE8_BE_TRIGGER = 0.5
FUTURES_PHASE8_OFF = {"FUTURES_EXTRA_BE_TRIGGER_PCT": 0}
FUTURES_SIDES = ("both", "long", "short")
FUTURES_LIQ_SOURCES = ("proxy", "feed", "both")
FUTURES_TP_ORDERS = ("limit", "market")
FUTURES_MAX_LEVERAGE = 20
FUTURES_MAX_UNIVERSE = 150              # FUTURES_UNIVERSE: en hacimli en fazla 150 parite taranır
# Kasım 2023'te piyasa değeri en büyük 10 coin (stabil coinler, TON ve MATIC/POL hariç; vadeli testin son dönemi)
DEFAULT_FUTURES_COINS = "BTC,ETH,BNB,XRP,SOL,ADA,DOGE,TRX,LINK,DOT"
# Gün içi (intraday) giriş taktikleri; INTRADAY_TACTIC virgülle birden çoğunu alabilir (sıradaki ilk sinyal alınır):
#   vwap      : fiyat gün başından (UTC 00:00) beri oluşan VWAP'ın INTRADAY_BAND_STD standart sapma altına inip yeşil
#               mumla dönünce (ortalamaya dönüş; hedef alanı = VWAP'a kalan mesafe)
#   bollinger : önceki mum Bollinger alt bandının (20 mum, INTRADAY_BAND_STD) altında kapanıp son mum bandın içine
#               yeşil dönünce (hedef alanı = orta bant)
#   rsi2      : 2 periyotluk RSI INTRADAY_RSI2_MAX'ın altına inince (kısa süreli aşırı satım; hedef alanı = 5 mum ort.)
#   orb       : açılış aralığı kırılımı: gün başının ilk INTRADAY_ORB_MINUTES dakikasının zirvesi hacimle kırılınca
#   ema_cross : 5 dk EMA9, EMA21'i yukarı kesince (RSI 50-70, fiyat EMA21'den fazla uzaklaşmamış)
#   squeeze   : Bollinger bantları son 10 saatin en darına yakınken (sıkışma) üst bant hacimle kırılınca
#   donchian  : son INTRADAY_BREAKOUT_BARS mumun zirvesi hacimle kırılınca (taze kırılım)
#   pullback  : yükselen trendde (EMA50 > EMA200) EMA20'ye geri çekilip önceki mumun zirvesinin üstünde kapanınca
#   dip       : son 3 saatte ATR'nin en az 4 katı düşüş, RSI 30 altı, dönüş başlamış (düşüşün %15-60'ı geri alınmış)
INTRADAY_TACTICS = ("vwap", "bollinger", "rsi2", "orb", "ema_cross", "squeeze", "donchian", "pullback", "dip")
INTRADAY_BELOW_VWAP_TACTICS = frozenset({"vwap", "bollinger", "rsi2", "dip"})   # Düşüşte (ortalamanın altında) alanlar
# Dönem başında piyasa değeri en büyük 10 coin (stabil coinler ve BNB hariç; BNB komisyon için tutulur), Kasım 2023
DEFAULT_INTRADAY_COINS = "BTC,ETH,XRP,SOL,ADA,DOGE,TRX,LINK,DOT,LTC"
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
# Gün içi (intraday): büyük coinler 5 dk'da az oynar; küçük hedef, işlem gün içinde kapanır (6 saat). Değerler 9 yıllık
# testte (2017-2026, her dönem kendi başındaki ilk 10 coinle) varsayılan taktiğin (vwap) üç dönemde de en iyi çıkışı.
# Kâr Al / Zarar Kes net (komisyon düşülmüş) yüzdedir: +%1.2 için fiyat ~%1.4 yükselmelidir. Uyarı: o testte hiçbir
# taktik kâr etmedi (en iyisi vwap + trend filtresi, 9 yılda -%28); işlem başı maliyet (~%0.2) avantajdan büyük.
INTRADAY_PROFILE = {
    "TAKE_PROFIT_PCT": 1.2,
    "STOP_LOSS_PCT": 2.5,
    "TRAILING_ACTIVATION_PCT": 1.0,
    "TRAILING_FLOOR_PCT": 0.5,
    "MAX_HOLD_MINUTES": 360,
    "BREAKEVEN_TRIGGER_PCT": 0.6,
    "BREAKEVEN_FLOOR_PCT": 0.1,
    "MIN_SIGNAL_SCORE": 0.0,           # Puan ölçeği taktiğe özeldir
    "MIN_ROOM_RATIO": 0.0,
    "MAX_24H_GAIN_PCT": 25.0,          # 5 dk trend/breakout için daraltılmış üst sınır büyük coinleri eleyebilir
    "BACKUP_STOP_PCT": 12.0,
}
# Vadeli (çöküş dönüşü taktiği): Kâr Al / Zarar Kes / başabaş FİYAT hareketi yüzdesidir (net K/Z değil): 2x
# kaldıraçta fiyat +%6 = teminatın ~+%12'si. Başabaş: fiyat +%1.5'e gelince zarar kes girişin +%0.3 üstüne çekilir;
# zaman stopu 30 dk; zararla kapanan coine 4 saat (240 dk) tekrar girilmez. Değerler 2020-2023 (seçim dönemi) testinde
# seçildi, 2023-2026'da (doğrulama) aynen denendi. İzleyen stop vadeli motorda kullanılmaz (değerler yalnızca ayar
# doğrulaması için TP'nin altında tutulur).
FUTURES_PROFILE = {
    "TAKE_PROFIT_PCT": 6.0,
    "STOP_LOSS_PCT": 4.0,
    "TRAILING_ACTIVATION_PCT": 5.0,
    "TRAILING_FLOOR_PCT": 2.5,
    "MAX_HOLD_MINUTES": 30,
    "BREAKEVEN_TRIGGER_PCT": 1.5,
    "BREAKEVEN_FLOOR_PCT": 0.3,
    "MIN_SIGNAL_SCORE": 0.0,
    "MIN_ROOM_RATIO": 0.0,
    "MAX_24H_GAIN_PCT": 25.0,
    "BACKUP_STOP_PCT": 8.0,
    "LOSS_COOLDOWN_MINUTES": 240,
    # İşlem sayısı araştırması (Kasım 2026): kârla kapanan coine hemen tekrar girilebilir (çöküş sürüyorsa yeni sinyal
    # alınır), zarar serisi molası yok. 1 dk çıkışlarla iki dönemde de işlem sayısını ve getiriyi artırdı.
    "SYMBOL_COOLDOWN_MINUTES": 0,
    "LOSS_STREAK_LIMIT": 0,
}
# Vadeliden spot bir stratejiye dönülürken (o grubun saklanmış ayarı yoksa) bu iki risk ayarı spot varsayılanına döner
SPOT_RISK_DEFAULTS = {"SYMBOL_COOLDOWN_MINUTES": 30, "LOSS_STREAK_LIMIT": 3}
# Telegram → ⚙️ Ayarlar → ⭐ Önerilen: araştırmada iki dönemde de en iyi sonucu veren vadeli ayar seti (tek düğme)
FUTURES_RECOMMENDED = {
    "FUTURES_TACTIC": "crash", "FUTURES_CRASH_HOURS": 4, "FUTURES_CRASH_DROP_PCT": 10, "FUTURES_CRASH_LIQ": False,
    "FUTURES_LEVERAGE": 2, "FUTURES_MARGIN_USAGE_PCT": 95, "TAKE_PROFIT_PCT": 6, "STOP_LOSS_PCT": 4,
    "TRAILING_ACTIVATION_PCT": 5, "TRAILING_FLOOR_PCT": 2.5, "BREAKEVEN_ENABLED": True,
    "BREAKEVEN_TRIGGER_PCT": 1.5, "BREAKEVEN_FLOOR_PCT": 0.3, "MAX_HOLD_MINUTES": 30, "FUTURES_TRAILING": False,
    "LOSS_COOLDOWN_MINUTES": 240, "SYMBOL_COOLDOWN_MINUTES": 0, "LOSS_STREAK_LIMIT": 0, "FUTURES_UNIVERSE": 20,
    "FUTURES_UNIVERSE_WITH_LIST": True, "FUTURES_UNIVERSE_MIN_AGE_DAYS": 1095,
}
# Telegram → 🧭 Giriş sinyali → 🏆 Izgara #1 / #2: --izgara testinin (Kasım 2026; 2020-2026, seçim / doğrulama dönemi,
# 1 dk mumlarla doğrulandı) en iyi iki yapılandırması. Test ⭐ önerilen ayarların üzerine yapıldığı için set onları da
# içerir; yalnızca giriş kuralı ve çıkışlar farklıdır (onaydan sonra birlikte yazılır).
FUTURES_GRID_PRESETS = (
    {"icon": "🏆", "name": "4s %10 + 1s %6 + 12s %15 (yılda ~410 işlem)",
     "detail": "Çöküş: son 4 saatte %10 VEYA 1 saatte %6 VEYA 12 saatte %15. Çıkış: TP +8, SL -5, başabaş +1 → +0.3, "
               "45 dk. Test (2020-2026, 1 dk): yılda ~410 işlem (şimdiki ~195), kazanma %80 (şimdiki %70), toplam kâr "
               "+%38; yıllık getiri seçim döneminde +%721, doğrulamada +%146 (şimdiki +%385 / +%99); en büyük düşüş "
               "%66 / %54 (şimdiki %57 / %30). Zayıf yanı: 2022 ve 2023 yaklaşık başabaş / hafif zarar (-%1, -%11). "
               "Kasım 2026 testi: 12 saat penceresi doğrulamada kârı düşürdü (tek pozisyonda yıllık +%213 yerine "
               "+%146, Faz 6 setinde 3x'te +%352 yerine +%209); 🥈 #2 girişi önerilir.",
     "values": {**FUTURES_RECOMMENDED, "FUTURES_CRASH_EXTRA": "1:6,12:15", "FUTURES_CRASH_Z": 0,
                "FUTURES_CONFIRM": "close", "TAKE_PROFIT_PCT": 8, "STOP_LOSS_PCT": 5, "BREAKEVEN_ENABLED": True,
                "BREAKEVEN_TRIGGER_PCT": 1, "BREAKEVEN_FLOOR_PCT": 0.3, "MAX_HOLD_MINUTES": 45}},
    {"icon": "🥈", "name": "4s %10 + 1s %6 (dengeli, 7/7 yıl)",
     "detail": "Çöküş: son 4 saatte %10 VEYA 1 saatte %6. Çıkış: TP +8, SL -5, başabaş +1 → +0.3, 45 dk. Test: yılda "
               "~345 işlem, kazanma %81, toplam kâr +%37; yıllık getiri +%587 / +%213 (şimdiki +%385 / +%99), en "
               "büyük düşüş %52 / %39; 7 yılın 7'si kârlı ve kayma 3 katına çıksa da şimdiki ayardan iyi.",
     "values": {**FUTURES_RECOMMENDED, "FUTURES_CRASH_EXTRA": "1:6", "FUTURES_CRASH_Z": 0, "FUTURES_CONFIRM": "close",
                "TAKE_PROFIT_PCT": 8, "STOP_LOSS_PCT": 5, "BREAKEVEN_ENABLED": True, "BREAKEVEN_TRIGGER_PCT": 1,
                "BREAKEVEN_FLOOR_PCT": 0.3, "MAX_HOLD_MINUTES": 45}},
)
# Telegram'da vadeli taktik değişince (onaydan sonra) o taktiğin çıkışları da yüklenir: çöküş dönüşü saatlerce süren
# büyük toparlanmayı, 4'lü onay ise ilk sürümdeki %1-1.5'lik hızlı scalping hareketini hedefler.
FUTURES_TACTIC_EXITS = {
    "crash": {k: FUTURES_PROFILE[k] for k in ("TAKE_PROFIT_PCT", "STOP_LOSS_PCT", "TRAILING_ACTIVATION_PCT",
                                              "TRAILING_FLOOR_PCT", "MAX_HOLD_MINUTES", "BREAKEVEN_TRIGGER_PCT",
                                              "BREAKEVEN_FLOOR_PCT", "LOSS_COOLDOWN_MINUTES")},
    "liq4": {"TAKE_PROFIT_PCT": 1.2, "STOP_LOSS_PCT": 0.9, "TRAILING_ACTIVATION_PCT": 1.0, "TRAILING_FLOOR_PCT": 0.5,
             "MAX_HOLD_MINUTES": 60, "BREAKEVEN_TRIGGER_PCT": 0.5, "BREAKEVEN_FLOOR_PCT": 0.2,
             "LOSS_COOLDOWN_MINUTES": 0},
}
PROFILE_ATTRS = {
    "TAKE_PROFIT_PCT": "take_profit_pct", "STOP_LOSS_PCT": "stop_loss_pct",
    "TRAILING_ACTIVATION_PCT": "trailing_activation_pct", "TRAILING_FLOOR_PCT": "trailing_floor_pct",
    "MAX_HOLD_MINUTES": "max_hold_minutes", "BREAKEVEN_TRIGGER_PCT": "breakeven_trigger_pct",
    "BREAKEVEN_FLOOR_PCT": "breakeven_floor_pct", "MIN_SIGNAL_SCORE": "min_signal_score",
    "MIN_ROOM_RATIO": "min_room_ratio", "MAX_24H_GAIN_PCT": "max_24h_gain_pct",
    "BACKUP_STOP_PCT": "backup_stop_pct", "LOSS_COOLDOWN_MINUTES": "loss_cooldown_minutes",
    "SYMBOL_COOLDOWN_MINUTES": "symbol_cooldown_minutes", "LOSS_STREAK_LIMIT": "loss_streak_limit",
}
PROFILES = {"swing": SWING_EXIT_PROFILE, "mr": MEAN_REVERSION_PROFILE, "rotation": ROTATION_PROFILE,
            "intraday": INTRADAY_PROFILE, "futures": FUTURES_PROFILE}


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


def is_intraday(strategy: str) -> bool:
    """Yalnızca en büyük coinlerde (INTRADAY_COINS) gün içi al-sat yapan strateji mi?"""
    return strategy in INTRADAY_STRATEGIES


def is_futures(strategy: str) -> bool:
    """Binance vadeli işlemlerde (USDⓈ-M) kaldıraçlı long / short scalping mi? (ayrı motor: FuturesBot)"""
    return strategy in FUTURES_STRATEGIES


def _coin_list(text: str, quote: str) -> Tuple[str, ...]:
    out: List[str] = []
    for raw in (text or "").replace(",", " ").split():
        coin = raw.upper()
        if len(coin) > len(quote) and coin.endswith(quote):
            coin = coin[: -len(quote)]
        if coin not in out:
            out.append(coin)
    return tuple(out)


def rotation_coin_list(cfg) -> Tuple[str, ...]:
    """ROTATION_COINS → ("BTC", "ETH"): büyük harf, tekrarsız; "BTCUSDT" yazıldıysa parite eki atılır. Boş = sınır yok
    (en hacimli ROTATION_UNIVERSE coin sıralanır)."""
    return _coin_list(getattr(cfg, "rotation_coins", ""), getattr(cfg, "quote_asset", "USDT").upper())


def intraday_coin_list(cfg) -> Tuple[str, ...]:
    """INTRADAY_COINS → ("BTC", "ETH", ...) (ROTATION_COINS ile aynı yazım). Boş = en hacimli INTRADAY_UNIVERSE coin."""
    return _coin_list(getattr(cfg, "intraday_coins", ""), getattr(cfg, "quote_asset", "USDT").upper())


def futures_coin_list(cfg) -> Tuple[str, ...]:
    """FUTURES_COINS → ("BTC", "ETH", ...) (ROTATION_COINS ile aynı yazım)."""
    return _coin_list(getattr(cfg, "futures_coins", ""), getattr(cfg, "quote_asset", "USDT").upper())


def parse_crash_rules(text: str) -> Tuple[Tuple[int, float], ...]:
    """FUTURES_CRASH_EXTRA "1:5, 12:15" → ((1, 5.0), (12, 15.0)): "saat:yüzde" çiftleri (saat 1-24, yüzde 2-50;
    "1s:%5", "1h:5%" ve Telegram düğmesindeki "1h5" de olur). Boş metin → (). Hatalı yazımda ValueError."""
    out: List[Tuple[int, float]] = []
    for raw in (text or "").replace(";", ",").split(","):
        tok = raw.strip().lower().replace(" ", "").replace("%", "")
        if not tok:
            continue
        if ":" not in tok and "h" in tok:              # Düğme verisinde ":" kullanılamaz: "1h5" = 1:5
            tok = tok.replace("h", ":", 1)
        if ":" not in tok:
            raise ValueError(f"'{raw.strip()}' saat:yüzde biçiminde değil (ör. 1:5)")
        h_txt, p_txt = tok.split(":", 1)
        for suffix in ("saat", "sa", "s", "h"):
            if h_txt.endswith(suffix):
                h_txt = h_txt[:-len(suffix)]
                break
        try:
            hours, pct = int(h_txt), float(p_txt.replace(",", "."))
        except ValueError:
            raise ValueError(f"'{raw.strip()}' okunamadı (ör. 1:5 = 1 saatte %5)") from None
        if not 1 <= hours <= 24 or not 2 <= pct <= 50:
            raise ValueError(f"'{raw.strip()}': saat 1-24, yüzde 2-50 arasında olmalı")
        out.append((hours, pct))
    return tuple(out)


def format_crash_rules(rules) -> str:
    """((1, 5.0), (12, 15.0)) → "1:5,12:15" (FUTURES_CRASH_EXTRA'nın kayıt biçimi)."""
    return ",".join(f"{h}:{p:g}" for h, p in rules)


def canonical_crash_extra(text: str) -> str:
    """FUTURES_CRASH_EXTRA yazımını tek biçime getirir ("1h:%5 ; 12:15" → "1:5,12:15"); hatalıysa olduğu gibi
    bırakır (doğrulama hatayı gösterir)."""
    try:
        return format_crash_rules(parse_crash_rules(text))
    except ValueError:
        return (text or "").strip()


def futures_crash_rules(cfg) -> Tuple[Tuple[int, float], ...]:
    """Çöküş kuralları (saat, %): ana kural FUTURES_CRASH_HOURS / FUTURES_CRASH_DROP_PCT + FUTURES_CRASH_EXTRA, saat
    sırasıyla. Herhangi biri sağlanınca çöküş sayılır (VEYA); aynı saat iki kez yazıldıysa küçük eşik geçerlidir.
    Hatalı ek pencere yazımı atlanır (ayar doğrulaması zaten uyarır)."""
    rules = {int(cfg.futures_crash_hours): float(cfg.futures_crash_drop_pct)}
    try:
        extra = parse_crash_rules(getattr(cfg, "futures_crash_extra", ""))
    except ValueError:
        extra = ()
    for hours, pct in extra:
        rules[hours] = min(pct, rules.get(hours, pct))
    return tuple(sorted(rules.items()))


def scan_log_path(folder, mode: str) -> Path:
    """Vadeli canlı tarama kaydı (FuturesBot yazar, vadeli backtest raporu karşılaştırır): mod başına bir dosya."""
    return Path(folder) / f"futures_scans_{mode}.jsonl"


LONG_CRASH_WINDOW_H = 8                 # Bu saat ve üstü ek pencere "uzun" (ör. 12 saatte %15; rapor_13 testi)


def futures_long_windows(cfg) -> Tuple[Tuple[int, float], ...]:
    """FUTURES_CRASH_EXTRA'daki uzun ek pencereler (≥ LONG_CRASH_WINDOW_H saat), ör. ((12, 15.0),). Kasım 2026
    testinde (rapor_13) yalnız bu pencerenin bulduğu işlemler işlem başı ~%0 getirdi ve tam boy ilk pozisyonu
    kapattı; not ve backtest satırı bunları gösterir. Hatalı yazımda ()."""
    try:
        extra = parse_crash_rules(getattr(cfg, "futures_crash_extra", ""))
    except ValueError:
        return ()
    return tuple(r for r in extra if r[0] >= LONG_CRASH_WINDOW_H)


def futures_pump_rules(cfg) -> Tuple[Tuple[int, float], ...]:
    """Pompa (short) kuralları (saat, %): FUTURES_PUMP_RULES, saat sırasıyla; boş = modül kapalı. Aynı saat iki kez
    yazıldıysa küçük eşik geçerli (VEYA). Hatalı yazım boş sayılır (ayar doğrulaması uyarır)."""
    try:
        parsed = parse_crash_rules(getattr(cfg, "futures_pump_rules", ""))
    except ValueError:
        return ()
    rules: dict = {}
    for hours, pct in parsed:
        rules[hours] = min(pct, rules.get(hours, pct))
    return tuple(sorted(rules.items()))


def futures_funding_rules(cfg) -> Tuple[Tuple[int, float], ...]:
    """Fonlama modülünün çöküş kuralları (saat, %): FUTURES_FUNDING_RULES (VEYA; aynı saatte küçük eşik)."""
    try:
        parsed = parse_crash_rules(getattr(cfg, "futures_funding_rules", ""))
    except ValueError:
        return ()
    rules: dict = {}
    for hours, pct in parsed:
        rules[hours] = min(pct, rules.get(hours, pct))
    return tuple(sorted(rules.items()))


def futures_wave_rules(cfg) -> Tuple[Tuple[int, float], ...]:
    """Faz 6 dalga eşikleri (saat, %): FUTURES_WAVE_RULES, saat sırasıyla; boş / hatalı = kapalı. Yalnızca çöküş
    taktiğinde ve sabit yüzdelerle (FUTURES_CRASH_Z açıkken eşikler oynaklıktan gelir, dalga eşikleri kullanılmaz)."""
    if getattr(cfg, "futures_tactic", "crash") != "crash" or float(getattr(cfg, "futures_crash_z", 0.0) or 0.0) > 0:
        return ()
    try:
        parsed = parse_crash_rules(getattr(cfg, "futures_wave_rules", ""))
    except ValueError:
        return ()
    rules: dict = {}
    for hours, pct in parsed:
        rules[hours] = min(pct, rules.get(hours, pct))
    return tuple(sorted(rules.items()))


def futures_wave_cfg(cfg):
    """Dalga anındaki çöküş ayarı: ana kural + FUTURES_CRASH_EXTRA + FUTURES_WAVE_RULES (aynı saatte küçük eşik).
    Ana saatin eşiği de düşerse ana yüzde de düşer: sıralama puanı (şiddet × ana yüzde) düşüş derinliğini ölçmeye
    devam eder. Dalga eşikleri kapalıysa None. Canlı bot (FuturesScalpStrategy.evaluate) ve backtest (find_signals)
    aynı ayarı kullanır."""
    rules = futures_wave_rules(cfg)
    if not rules:
        return None
    try:
        base_extra = parse_crash_rules(getattr(cfg, "futures_crash_extra", ""))
    except ValueError:
        base_extra = ()
    main_h = int(cfg.futures_crash_hours)
    main_pct = min([float(cfg.futures_crash_drop_pct)] + [p for h, p in rules if h == main_h])
    return replace(cfg, futures_crash_drop_pct=main_pct,
                   futures_crash_extra=format_crash_rules(tuple(base_extra) + tuple(rules)))


def futures_breadth_on(cfg) -> bool:
    """Genişlik (son 1 saatte %3+ düşen coin sayısı) gerekiyor mu: dalga modu, yalnız çöküş küçültme (Faz 5) ya da
    dalgada yeniden giriş / dalga eşikleri (Faz 6)."""
    return (int(getattr(cfg, "futures_max_positions", 1)) > 1
            or float(getattr(cfg, "futures_isolated_size_pct", 100.0)) < 100
            or int(getattr(cfg, "futures_reentry_breadth", 0)) > 0 or bool(futures_wave_rules(cfg)))


def futures_be_trigger(cfg, n_open: int) -> float:
    """Yeni pozisyonun başabaş tetiği (fiyat %): ana ayar; Faz 8 açıksa (FUTURES_EXTRA_BE_TRIGGER_PCT > 0) ve pozisyon
    dalga modunda başka pozisyon açıkken açılıyorsa (ek pozisyon, n_open ≥ 1) o değer. Canlı bot ve backtest ortak."""
    extra = float(getattr(cfg, "futures_extra_be_trigger_pct", 0.0))
    if extra > 0 and n_open >= 1 and int(getattr(cfg, "futures_max_positions", 1)) > 1:
        return extra
    return float(cfg.breakeven_trigger_pct)


def futures_slot_share(cfg, n_open: int) -> float:
    """Yeni pozisyonun teminatı, kullanılabilir bakiyenin (bakiye × FUTURES_MARGIN_USAGE_PCT) kaçta kaçı: tek
    pozisyonda 1; dalga modunda ağırlıklar 1 (ilk pozisyon, açık pozisyon yokken) ve x = FUTURES_EXTRA_SIZE_PCT / 100
    (ek pozisyonlar), toplam 1 + (N - 1)·x'e bölünür. x = 1 (varsayılan) → her biri 1/N (Faz 5)."""
    slots = max(1, int(getattr(cfg, "futures_max_positions", 1)))
    if slots <= 1:
        return 1.0
    x = max(0.0, min(1.0, float(getattr(cfg, "futures_extra_size_pct", 100.0)) / 100))
    total = 1 + (slots - 1) * x
    return (1.0 if n_open <= 0 else x) / total


def futures_modules(cfg) -> Tuple[str, ...]:
    """Çöküş taktiğinde açık olan ek giriş modülleri (öncelik sırasıyla): funding, pump, pullback, breakout, bb."""
    if getattr(cfg, "futures_tactic", "crash") != "crash":
        return ()
    out = []
    if getattr(cfg, "futures_funding", False) and futures_funding_rules(cfg):
        out.append("funding")
    if futures_pump_rules(cfg):
        out.append("pump")
    if getattr(cfg, "futures_pullback", False):
        out.append("pullback")
    if getattr(cfg, "futures_breakout", False):
        out.append("breakout")
    if getattr(cfg, "futures_bb", "off") != "off":
        out.append("bb")
    return tuple(out)


def futures_liq_distance_pct(leverage: float, mmr_pct: float) -> float:
    """Yalıtılmış (isolated) teminatta likidasyona kalan yaklaşık FİYAT hareketi (%): 100/kaldıraç - bakım oranı.
    10x → ~%9.5, 20x → ~%4.5."""
    return 100.0 / max(1.0, leverage) - mmr_pct


def intraday_tactic_list(cfg) -> Tuple[str, ...]:
    """INTRADAY_TACTIC → ("vwap", "rsi2"): küçük harf, tekrarsız, yazıldığı sırayla (ilk sinyal veren alınır)."""
    out: List[str] = []
    for raw in (getattr(cfg, "intraday_tactic", "") or "").replace(",", " ").split():
        name = raw.strip().lower()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def rotation_hold_count(cfg) -> int:
    """Rotasyonda aynı anda en fazla kaç coin tutulur: ROTATION_TOP_N; ROTATION_COINS verildiyse listedeki coin
    sayısından fazla olamaz (yalnızca BTC varken TOP_N=3, sermayenin üçte ikisini hep boşta bekletirdi)."""
    coins = rotation_coin_list(cfg)
    return min(cfg.rotation_top_n, len(coins)) if coins else cfg.rotation_top_n


def fee_from_coin(cfg) -> bool:
    """Alış komisyonu alınan coinden mi kesilir? BNB_FEES=true ise BNB'den ödenir: coin miktarı azalmaz, satışta LOT
    adımı yüzünden toz kalmaz; toz sınırı (MAX_DUST_PCT) gerekmez, küçük sermayeyle BTC / ETH de alınabilir."""
    return not getattr(cfg, "bnb_fees", False)


def divergence_trading(cfg) -> bool:
    """RSI_DIV_TRADE ana stratejinin yanında çalışıyor mu? (Rotasyonda ve rsi_divergence'ın kendisinde çalışmaz.)"""
    return (getattr(cfg, "rsi_div_trade", "off") != "off" and not is_rotation(cfg.strategy)
            and cfg.strategy != "rsi_divergence")


def strategy_family(strategy: str) -> str:
    """Aynı çıkış mantığını ve mum aralığını paylaşan strateji grubu: 'short' (5 dk), 'intraday' (5 dk, en büyük
    coinler, küçük hedef), 'swing' (1 saat, günler), 'mr' (1 saat, ortalamaya dönüş / RSI uyumsuzluğu), 'rotation'
    (günlük), 'futures' (vadeli işlemler, 5 dk, kaldıraçlı long / short)."""
    if is_futures(strategy):
        return "futures"
    if is_rotation(strategy):
        return "rotation"
    if is_intraday(strategy):
        return "intraday"
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
    rotation_coins: str = ""               # Yalnızca bu coinler (ör. "BTC,ETH": büyük coin trendi); boş = en hacimliler
    # Ortalamaya dönüş (STRATEGY=mean_revert; 1 saatlik mumlar)
    mr_band_std: float = 2.0               # Kapanış 20 saatlik ortalamanın en az bu kadar standart sapma altında olmalı
    mr_universe: int = 20                  # Yalnızca en hacimli bu kadar coin (büyük, likit coinler)
    # Gün içi al-sat (STRATEGY=intraday; 5 dk mumlar, yalnızca en büyük coinler)
    intraday_coins: str = DEFAULT_INTRADAY_COINS   # Boş = en hacimli INTRADAY_UNIVERSE coin
    intraday_universe: int = 10            # INTRADAY_COINS boşken taranacak en hacimli coin sayısı
    intraday_tactic: str = "vwap"          # Giriş taktiği (bkz. INTRADAY_TACTICS); virgülle birden çok
    intraday_band_std: float = 2.0         # vwap / bollinger: kaç standart sapma altı aşırı satım sayılır
    intraday_rsi2_max: float = 10.0        # rsi2: RSI(2) bunun altına inince
    intraday_orb_minutes: int = 60         # orb: gün başından (UTC 00:00) itibaren açılış aralığının süresi
    intraday_breakout_bars: int = 48       # donchian: kırılacak zirvenin bakıldığı mum sayısı (48 × 5 dk = 4 saat)
    intraday_volume_ratio: float = 1.5     # orb / squeeze / donchian: kırılım mumunun hacmi ortalamanın en az katı
    intraday_trend_filter: bool = True     # Yalnızca 5 dk EMA50 > EMA200 ve fiyat EMA200 üstündeyken al
    intraday_min_atr_pct: float = 0.1      # 5 dk ATR / fiyat (%) bundan düşükse alma (hedefe gidemeyecek kadar durgun)
    # Vadeli işlem scalping (STRATEGY=futures; Binance USDⓈ-M, 5 dk mumlar, kaldıraçlı long / short, isolated)
    futures_coins: str = DEFAULT_FUTURES_COINS   # İşlem yapılacak vadeli coinler (USDT pariteleri)
    futures_tactic: str = "crash"          # crash (çöküş dönüşü) | liq4 (4'lü onay, ilk sürüm)
    futures_crash_hours: int = 4           # crash: düşüşün ölçüldüğü süre (saat)
    futures_crash_drop_pct: float = 10.0   # crash: bu sürede en az bu kadar (%) düşmüş olmalı
    futures_crash_liq: bool = False        # crash: çöküş mumunda likidasyon dalgası da şart (daha az ama daha isabetli)
    # Dinamik çöküş (Kasım 2026; hepsi varsayılan kapalı, ana kural yukarıdaki iki ayardır):
    # FUTURES_CRASH_EXTRA: ana kurala VEYA ile eklenen pencereler "saat:%" (ör. "1:5,12:15" = son 1 saatte %5 ya da
    # son 12 saatte %15 düşüş de yeter; ana kural 4:10 ile mikro / standart / makro çöküş)
    futures_crash_extra: str = ""
    # FUTURES_CRASH_Z > 0: sabit yüzdeler yerine oynaklık eşiği. Her pencerede düşüş, coinin o süredeki olağan
    # hareketinin (pencereden önceki 24 saatin 5 dk getirilerinin standart sapması × √mum sayısı) en az Z katı olmalı
    # (ör. 3 = "son 4 saatte 3 standart sapma düştüyse"); yine de en az FUTURES_CRASH_Z_MIN_PCT (%) düşüş aranır.
    futures_crash_z: float = 0.0
    futures_crash_z_min_pct: float = 3.0
    # Teyit / izleme modu (bkz. FUTURES_CONFIRMS): çöküş görülünce FUTURES_CONFIRM_BARS mum (12 × 5 dk = 1 saat)
    # boyunca teyit beklenir; hacim zirvesi = mum hacmi önceki 20 mumun ortalamasının FUTURES_CONFIRM_VOLUME_RATIO katı
    futures_confirm: str = "close"
    futures_confirm_bars: int = 12
    futures_confirm_volume_ratio: float = 3.0
    # Ek giriş modülleri (Faz 3, bkz. FUTURES_BB_MODES üstündeki açıklama; hepsi varsayılan kapalı)
    futures_pump_rules: str = ""           # Pompa dönüşü SHORT, "saat:%" yükseliş (ör. 1:6,4:10,12:15); boş = kapalı
    futures_bb: str = "off"                # Bollinger + RSI: off | both | long | short
    futures_bb_std: float = 2.0            # Bant genişliği (standart sapma; 20 mumluk ortalama)
    futures_bb_rsi_low: float = 25.0       # Long: RSI(14) bunun altında
    futures_bb_rsi_high: float = 75.0      # Short: RSI(14) bunun üstünde
    futures_breakout: bool = False         # Hacimli kırılım LONG
    futures_breakout_volume_ratio: float = 3.0
    futures_breakout_hours: int = 4
    # Faz 4 (bkz. FUTURES_ENTRY_ORDERS üstündeki açıklama; hepsi varsayılan kapalı)
    futures_funding: bool = False          # Fonlama sıkışması LONG
    futures_funding_rate_pct: float = -0.10   # Son ödenen fonlama (8 saate oranlanmış, %) bu değer veya altında
    futures_funding_rules: str = "1:5"     # ...ve bu çöküşlerden biri ("saat:%"; 1:5 = son 1 saatte %5)
    futures_pullback: bool = False         # Trend içi düzeltme LONG
    futures_pullback_rsi: float = 25.0     # 5 dk RSI(14) bunun altında
    futures_pullback_ema: int = 200        # ...ve son 1 saatlik kapanış 1 saatlik EMA(bu)'nun üstünde
    futures_entry_order: str = "market"    # market | limit (maker; bkz. FUTURES_ENTRY_ORDERS)
    futures_limit_offset_pct: float = 0.05   # limit: alışta bid'in, satışta ask'ın bu kadar (%) gerisine
    futures_limit_ttl_min: int = 3         # limit: bu kadar dakikada dolmazsa iptal
    # Dalga modu (bkz. FUTURES_MAX_POSITIONS_LIMIT üstündeki açıklama; varsayılanlar = kapalı, eskisi gibi)
    futures_max_positions: int = 1         # Aynı anda en fazla pozisyon (her biri bakiyenin 1/N'i teminat)
    futures_extra_breadth: int = 8         # 2. ve sonraki pozisyon: son 1 saatte %3+ düşen coin sayısı en az bu kadar
    futures_isolated_size_pct: float = 100.0   # Yalnız çöküşte (genişlik < FUTURES_ISOLATED_BREADTH) pozisyon büyüklüğü %
    futures_isolated_breadth: int = 2      # Bundan az coin düşüyorsa çöküş "yalnız" sayılır
    # Faz 6 (bkz. FUTURES_PHASE6_PRESET üstündeki açıklama; varsayılanlar = kapalı, eskisi gibi)
    futures_extra_size_pct: float = 100.0  # Ek pozisyonların büyüklüğü, ilk pozisyonun %'si (100 = eşit, Faz 5)
    futures_reentry_breadth: int = 0       # Genişlik ≥ bu iken zarar sonrası bekleme atlanır (0 = kapalı)
    futures_wave_rules: str = ""           # Genişlik ≥ FUTURES_WAVE_BREADTH iken ek çöküş pencereleri ("saat:%")
    futures_wave_breadth: int = 12
    # Faz 8: ek pozisyonların başabaş tetiği (fiyat %; 0 = BREAKEVEN_TRIGGER_PCT ile aynı)
    futures_extra_be_trigger_pct: float = 0.0
    futures_leverage: int = 2             # Kaldıraç 1-20; her girişten hemen önce Binance'te ayarlanır
    futures_margin_usage_pct: float = 95.0  # Serbest bakiyenin teminata giden yüzdesi (bileşik; kalan pay komisyon için)
    futures_volume_ratio: float = 2.5      # Hacim onayı: sinyal mumu hacmi önceki 20 mumun ortalamasının en az bu katı
    futures_liq_source: str = "proxy"      # proxy (mumlardan, backtest ile aynı) | feed (canlı akış) | both (ikisi)
    futures_liq_ratio: float = 3.0         # Vekil: zorla satış / alış hacmi önceki 20 mumun ortalamasının en az katı
    futures_liq_min_usdt: float = 100_000.0  # feed / both: son 5 dk'da bu kadar USDT'lik likidasyon
    futures_div_oversold: float = 40.0     # Uyumsuzluk: önceki dipte RSI ≤ bu (tepede ≥ 100 - bu)
    futures_div_rise: float = 3.0          # ...yeni dipte RSI en az bu kadar puan yüksek (tepede düşük)
    futures_btc_trend: bool = True         # İşlem yönü BTC'nin 1 saatlik trendiyle aynı olmalı
    futures_sides: str = "both"            # both | long | short
    futures_tp_order: str = "limit"        # limit: Kâr Al Binance'te bekleyen limit emir (maker komisyon) | market
    futures_taker_fee: float = 0.0005      # Piyasa emri komisyonu (Binance vadeli VIP0: %0.05)
    futures_maker_fee: float = 0.0002      # Limit emir komisyonu (%0.02)
    futures_slippage_pct: float = 0.02     # Test modu / backtest: piyasa emirlerinde kayma (%)
    futures_mmr_pct: float = 0.5           # Bakım teminatı oranı (likidasyon fiyatı tahmini için, %)
    futures_exchange_stop: bool = True     # Girişten hemen sonra Binance'e STOP_MARKET; konamazsa pozisyon kapatılır
    # 0 = yalnızca FUTURES_COINS listesi; N = "en hacimli N köklü coin": en az FUTURES_UNIVERSE_MIN_AGE_DAYS gündür
    # listelenen kripto USDT süresiz paritelerinden son FUTURES_UNIVERSE_AVG_DAYS günün ortalama USDT hacmine göre ilk N
    # (stabil coin, endeks, hisse / emtia ve kaldırılması planlanan pariteler hariç; sıralama günde bir yenilenir).
    # Test (2020-2026): yaş filtresi olmadan her N'de hesap eridi; köklü ilk 10-20 kârlı, 40 ve üstü zayıf / zararlı.
    futures_universe: int = 0
    futures_universe_with_list: bool = True     # En hacimli N'e FUTURES_COINS listesi de eklenir (liste ∪ köklü N)
    futures_universe_min_age_days: int = 1095
    futures_universe_avg_days: int = 180
    futures_universe_mode: str = "avg"     # avg | daily (bir önceki UTC gününün hacmi, her gün yenilenir)
    # İzleyen stop: fiyat girişten +FUTURES_TRAILING_ACT_PCT gidince zarar kes, görülen en iyi fiyatın
    # FUTURES_TRAILING_GAP_PCT gerisine taşınır (yalnızca lehte yönde; Binance'teki stop emri de taşınır)
    futures_trailing: bool = False
    futures_trailing_act_pct: float = 3.0
    futures_trailing_gap_pct: float = 1.5
    futures_data_url: str = "https://data.binance.vision"   # Backtest geçmiş verisi (Binance'in herkese açık arşivi)
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
    bnb_fees: bool = False                 # Komisyon BNB ile ödeniyor: alınan coinden kesilmez, satışta toz kalmaz
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
    # 45) Zararı azaltma (hepsi varsayılan kapalı; etkileri --tara-ab ile ölçülür)
    stale_exit_minutes: int = 0            # Takılan işlem: bu sürede net kâr hiç STALE_EXIT_PEAK_PCT'yi görmediyse sat
    stale_exit_peak_pct: float = 1.0       # (0 = kapalı; DCA ile eklenmiş pozisyona uygulanmaz)
    loss_cooldown_minutes: int = 0         # Zararla kapanan coine bu kadar dakika tekrar girme (0 = SYMBOL_COOLDOWN_MINUTES)
    prior_trend_filter: bool = False       # Coin son düşüşten önce yükselişte olmalı (1 gün önceki fiyat ≥ 7 gün önceki)
    sl_extend: bool = False                # Zarar kese gelindi ama dönüş işareti var: zarar kes bir kez genişletilir
    sl_extend_pct: float = 1.5             # ...bu kadar (net zarar %; ör. zarar kes -%4 → -%5.5)
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
        # Vadeli motor izleyen stop ve spot yedek stopu kullanmaz (zarar kes Binance'te STOP_MARKET olarak bekler):
        # bu iki kural vadelide Kâr Al / Zarar Kes seçimini gereksiz yere kısıtlamasın
        if self.trailing_activation_pct >= self.take_profit_pct and not is_futures(self.strategy):
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
        if self.backup_stop_enabled and self.backup_stop_pct <= self.stop_loss_pct and not is_futures(self.strategy):
            errors.append("BACKUP_STOP_PCT, STOP_LOSS_PCT'den büyük olmalı (yedek emir botun zarar kesinin altında beklemeli)")
        if self.breakeven_enabled and not 0 <= self.breakeven_floor_pct < self.breakeven_trigger_pct:
            errors.append("BREAKEVEN_FLOOR_PCT, BREAKEVEN_TRIGGER_PCT'den küçük olmalı")
        if self.min_room_ratio < 0:
            errors.append("MIN_ROOM_RATIO negatif olamaz (0 = kapalı)")
        if self.max_24h_drop_pct <= 0:
            errors.append("MAX_24H_DROP_PCT sıfırdan büyük olmalı")
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
        bad = [c for c in self.rotation_coins.replace(",", " ").split() if not c.isalnum()]
        if bad:
            errors.append("ROTATION_COINS virgülle ayrılmış coin adları olmalı (ör. BTC,ETH); hatalı: "
                          + ", ".join(bad))
        if not 0.5 <= self.mr_band_std <= 5:
            errors.append("MR_BAND_STD 0.5 ile 5 arasında olmalı (örn. 2 = ortalamanın 2 standart sapma altı)")
        if not 1 <= self.mr_universe <= 200:
            errors.append("MR_UNIVERSE 1 ile 200 arasında olmalı")
        bad = [c for c in self.intraday_coins.replace(",", " ").split() if not c.isalnum()]
        if bad:
            errors.append("INTRADAY_COINS virgülle ayrılmış coin adları olmalı (ör. BTC,ETH,XRP); hatalı: "
                          + ", ".join(bad))
        if not 1 <= self.intraday_universe <= 50:
            errors.append("INTRADAY_UNIVERSE 1 ile 50 arasında olmalı")
        tactics = intraday_tactic_list(self)
        unknown = [t for t in tactics if t not in INTRADAY_TACTICS]
        if not tactics or unknown:
            errors.append("INTRADAY_TACTIC şunlardan biri (veya virgülle birkaçı) olmalı: " + ", ".join(INTRADAY_TACTICS)
                          + (f" (hatalı: {', '.join(unknown)})" if unknown else ""))
        if not 0.5 <= self.intraday_band_std <= 5:
            errors.append("INTRADAY_BAND_STD 0.5 ile 5 arasında olmalı")
        if not 1 <= self.intraday_rsi2_max <= 50:
            errors.append("INTRADAY_RSI2_MAX 1 ile 50 arasında olmalı")
        if not 15 <= self.intraday_orb_minutes <= 240 or self.intraday_orb_minutes % 5:
            errors.append("INTRADAY_ORB_MINUTES 15 ile 240 arasında ve 5'in katı olmalı (5 dk mumlar)")
        if not 12 <= self.intraday_breakout_bars <= 288:
            errors.append("INTRADAY_BREAKOUT_BARS 12 ile 288 (24 saat) arasında olmalı")
        if self.intraday_volume_ratio <= 0:
            errors.append("INTRADAY_VOLUME_RATIO sıfırdan büyük olmalı")
        if not 0 <= self.intraday_min_atr_pct < self.max_atr_pct:
            errors.append("INTRADAY_MIN_ATR_PCT 0 veya pozitif ve MAX_ATR_PCT'den küçük olmalı")
        bad = [c for c in self.futures_coins.replace(",", " ").split() if not c.isalnum()]
        if bad or not self.futures_coins.strip():
            errors.append("FUTURES_COINS virgülle ayrılmış coin adları olmalı (ör. BTC,ETH,SOL)"
                          + (f"; hatalı: {', '.join(bad)}" if bad else ""))
        if not 1 <= self.futures_leverage <= FUTURES_MAX_LEVERAGE:
            errors.append(f"FUTURES_LEVERAGE 1 ile {FUTURES_MAX_LEVERAGE} arasında olmalı")
        if not 10 <= self.futures_margin_usage_pct <= 100:
            errors.append("FUTURES_MARGIN_USAGE_PCT 10 ile 100 arasında olmalı")
        if self.futures_volume_ratio <= 0 or self.futures_liq_ratio <= 0 or self.futures_liq_min_usdt < 0:
            errors.append("FUTURES_VOLUME_RATIO ve FUTURES_LIQ_RATIO sıfırdan büyük, FUTURES_LIQ_MIN_USDT negatif "
                          "olmamalı")
        if not 0 <= self.futures_universe <= FUTURES_MAX_UNIVERSE:
            errors.append(f"FUTURES_UNIVERSE 0 (FUTURES_COINS listesi) ile {FUTURES_MAX_UNIVERSE} arasında olmalı")
        if not 30 <= self.futures_universe_min_age_days <= 3650 or not 30 <= self.futures_universe_avg_days <= 365:
            errors.append("FUTURES_UNIVERSE_MIN_AGE_DAYS 30-3650, FUTURES_UNIVERSE_AVG_DAYS 30-365 gün olmalı")
        if not 0 < self.futures_trailing_act_pct <= 50 or not 0 < self.futures_trailing_gap_pct <= 20:
            errors.append("FUTURES_TRAILING_ACT_PCT 0-50, FUTURES_TRAILING_GAP_PCT 0-20 arasında olmalı (fiyat %)")
        if self.futures_tactic not in FUTURES_TACTICS:
            errors.append("FUTURES_TACTIC şunlardan biri olmalı: " + ", ".join(FUTURES_TACTICS))
        if not 1 <= self.futures_crash_hours <= 24 or not 2 <= self.futures_crash_drop_pct <= 50:
            errors.append("FUTURES_CRASH_HOURS 1-24, FUTURES_CRASH_DROP_PCT 2-50 arasında olmalı")
        try:
            extra = parse_crash_rules(self.futures_crash_extra)
            if len(extra) > FUTURES_MAX_CRASH_EXTRA:
                errors.append(f"FUTURES_CRASH_EXTRA en fazla {FUTURES_MAX_CRASH_EXTRA} pencere alabilir")
        except ValueError as exc:
            errors.append(f"FUTURES_CRASH_EXTRA hatalı: {exc}")
        if not (self.futures_crash_z == 0 or 1 <= self.futures_crash_z <= 10):
            errors.append("FUTURES_CRASH_Z 0 (kapalı) ya da 1-10 standart sapma olmalı")
        if not 0.5 <= self.futures_crash_z_min_pct <= 50:
            errors.append("FUTURES_CRASH_Z_MIN_PCT 0.5-50 arasında olmalı (%)")
        try:
            parse_crash_rules(self.futures_pump_rules)
        except ValueError as exc:
            errors.append(f"FUTURES_PUMP_RULES hatalı: {exc}")
        if self.futures_bb not in FUTURES_BB_MODES:
            errors.append("FUTURES_BB şunlardan biri olmalı: " + ", ".join(FUTURES_BB_MODES))
        if not 1 <= self.futures_bb_std <= 4 or not 5 <= self.futures_bb_rsi_low <= 50 \
                or not 50 <= self.futures_bb_rsi_high <= 95:
            errors.append("FUTURES_BB_STD 1-4, FUTURES_BB_RSI_LOW 5-50, FUTURES_BB_RSI_HIGH 50-95 arasında olmalı")
        if not 1 <= self.futures_breakout_volume_ratio <= 20 or not 1 <= self.futures_breakout_hours <= 24:
            errors.append("FUTURES_BREAKOUT_VOLUME_RATIO 1-20 kat, FUTURES_BREAKOUT_HOURS 1-24 saat olmalı")
        try:
            if not parse_crash_rules(self.futures_funding_rules) and self.futures_funding:
                errors.append("FUTURES_FUNDING_RULES boş olamaz (ör. 1:5 = son 1 saatte %5 çöküş)")
        except ValueError as exc:
            errors.append(f"FUTURES_FUNDING_RULES hatalı: {exc}")
        if not -2 <= self.futures_funding_rate_pct <= 0:
            errors.append("FUTURES_FUNDING_RATE_PCT -2 ile 0 arasında olmalı (%, 8 saatlik; ör. -0.10)")
        if not 5 <= self.futures_pullback_rsi <= 50 or not 20 <= self.futures_pullback_ema <= 500:
            errors.append("FUTURES_PULLBACK_RSI 5-50, FUTURES_PULLBACK_EMA 20-500 arasında olmalı")
        if self.futures_entry_order not in FUTURES_ENTRY_ORDERS:
            errors.append("FUTURES_ENTRY_ORDER şunlardan biri olmalı: " + ", ".join(FUTURES_ENTRY_ORDERS))
        if not 0 <= self.futures_limit_offset_pct <= 1 or not 1 <= self.futures_limit_ttl_min <= 15:
            errors.append("FUTURES_LIMIT_OFFSET_PCT 0-1 (%), FUTURES_LIMIT_TTL_MIN 1-15 dakika olmalı")
        if not 1 <= self.futures_max_positions <= FUTURES_MAX_POSITIONS_LIMIT:
            errors.append(f"FUTURES_MAX_POSITIONS 1-{FUTURES_MAX_POSITIONS_LIMIT} arasında olmalı")
        if not 1 <= self.futures_extra_breadth <= 100 or not 1 <= self.futures_isolated_breadth <= 100:
            errors.append("FUTURES_EXTRA_BREADTH ve FUTURES_ISOLATED_BREADTH 1-100 coin olmalı")
        if not 10 <= self.futures_isolated_size_pct <= 100:
            errors.append("FUTURES_ISOLATED_SIZE_PCT 10-100 arasında olmalı (%; 100 = tam büyüklük)")
        if not 5 <= self.futures_extra_size_pct <= 100:
            errors.append("FUTURES_EXTRA_SIZE_PCT 5-100 arasında olmalı (%; ilk pozisyona göre, 100 = eşit)")
        if not 0 <= self.futures_reentry_breadth <= 100 or not 1 <= self.futures_wave_breadth <= 100:
            errors.append("FUTURES_REENTRY_BREADTH 0-100 (0 = kapalı), FUTURES_WAVE_BREADTH 1-100 coin olmalı")
        try:
            parse_crash_rules(self.futures_wave_rules)
        except ValueError as exc:
            errors.append(f"FUTURES_WAVE_RULES hatalı: {exc}")
        if not 0 <= self.futures_extra_be_trigger_pct <= 10:
            errors.append("FUTURES_EXTRA_BE_TRIGGER_PCT 0-10 arasında olmalı (fiyat %; 0 = ana başabaş tetiğiyle aynı)")
        elif 0 < self.futures_extra_be_trigger_pct <= self.breakeven_floor_pct and is_futures(self.strategy):
            errors.append(f"FUTURES_EXTRA_BE_TRIGGER_PCT ({self.futures_extra_be_trigger_pct:g}) başabaş tabanından "
                          f"(BREAKEVEN_FLOOR_PCT={self.breakeven_floor_pct:g}) büyük olmalı")
        if self.futures_universe_mode not in FUTURES_UNIVERSE_MODES:
            errors.append("FUTURES_UNIVERSE_MODE şunlardan biri olmalı: " + ", ".join(FUTURES_UNIVERSE_MODES))
        if self.futures_confirm not in FUTURES_CONFIRMS:
            errors.append("FUTURES_CONFIRM şunlardan biri olmalı: " + ", ".join(FUTURES_CONFIRMS))
        if not 1 <= self.futures_confirm_bars <= 24 or not 1 <= self.futures_confirm_volume_ratio <= 20:
            errors.append("FUTURES_CONFIRM_BARS 1-24 mum, FUTURES_CONFIRM_VOLUME_RATIO 1-20 kat olmalı")
        if self.futures_liq_source not in FUTURES_LIQ_SOURCES:
            errors.append("FUTURES_LIQ_SOURCE şunlardan biri olmalı: " + ", ".join(FUTURES_LIQ_SOURCES))
        if self.futures_sides not in FUTURES_SIDES:
            errors.append("FUTURES_SIDES şunlardan biri olmalı: " + ", ".join(FUTURES_SIDES))
        if self.futures_tp_order not in FUTURES_TP_ORDERS:
            errors.append("FUTURES_TP_ORDER şunlardan biri olmalı: " + ", ".join(FUTURES_TP_ORDERS))
        if not 10 <= self.futures_div_oversold <= 50 or not 0 <= self.futures_div_rise <= 30:
            errors.append("FUTURES_DIV_OVERSOLD 10 ile 50, FUTURES_DIV_RISE 0 ile 30 arasında olmalı")
        if not (0 <= self.futures_taker_fee < 0.01 and 0 <= self.futures_maker_fee < 0.01
                and 0 <= self.futures_slippage_pct < 2 and 0 <= self.futures_mmr_pct < 5):
            errors.append("FUTURES_TAKER_FEE / FUTURES_MAKER_FEE 0-0.01, FUTURES_SLIPPAGE_PCT 0-2, FUTURES_MMR_PCT 0-5 "
                          "arasında olmalı")
        if is_futures(self.strategy):
            room = futures_liq_distance_pct(self.futures_leverage, self.futures_mmr_pct)
            if self.stop_loss_pct >= room - 0.5:
                errors.append(f"Vadeli: STOP_LOSS_PCT ({self.stop_loss_pct:g}) likidasyona kalan mesafeden "
                              f"({self.futures_leverage}x'te fiyat ~%{room:.1f}) en az 0.5 puan küçük olmalı: zarar kes "
                              f"likidasyondan önce çalışmalı. Kaldıracı düşürün veya zarar kesi daraltın.")
            if self.max_hold_minutes <= 0:
                errors.append("Vadeli: MAX_HOLD_MINUTES (zaman stopu) sıfırdan büyük olmalı (örn. 60)")
        if self.status_report_minutes < 0 or self.symbol_cooldown_minutes < 0 or self.max_hold_minutes < 0:
            errors.append("STATUS_REPORT_MINUTES, SYMBOL_COOLDOWN_MINUTES ve MAX_HOLD_MINUTES 0 veya pozitif olmalı")
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
        if self.stale_exit_minutes < 0 or not 0 < self.stale_exit_peak_pct <= 50 or self.loss_cooldown_minutes < 0:
            errors.append("STALE_EXIT_MINUTES ve LOSS_COOLDOWN_MINUTES 0 (kapalı) veya pozitif, STALE_EXIT_PEAK_PCT "
                          "0-50 arasında olmalı")
        if not 0 < self.sl_extend_pct <= 20:
            errors.append("SL_EXTEND_PCT 0-20 arasında olmalı (zarar kesin kaç puan genişletileceği)")
        elif (self.sl_extend and self.backup_stop_enabled and not is_rotation(self.strategy)
              and self.stop_loss_pct + self.sl_extend_pct >= self.backup_stop_pct):
            errors.append("STOP_LOSS_PCT + SL_EXTEND_PCT, BACKUP_STOP_PCT'den küçük olmalı (yoksa Binance'teki yedek "
                          "stop uzatılmış zarar kesten önce satar)")
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
        rotation_coins=_env_str("ROTATION_COINS", ""),
        mr_band_std=_env_float("MR_BAND_STD", 2.0),
        mr_universe=_env_int("MR_UNIVERSE", 20),
        intraday_coins=_env_str("INTRADAY_COINS", DEFAULT_INTRADAY_COINS),
        intraday_universe=_env_int("INTRADAY_UNIVERSE", 10),
        intraday_tactic=_env_str("INTRADAY_TACTIC", "vwap").lower(),
        intraday_band_std=_env_float("INTRADAY_BAND_STD", 2.0),
        intraday_rsi2_max=_env_float("INTRADAY_RSI2_MAX", 10.0),
        intraday_orb_minutes=_env_int("INTRADAY_ORB_MINUTES", 60),
        intraday_breakout_bars=_env_int("INTRADAY_BREAKOUT_BARS", 48),
        intraday_volume_ratio=_env_float("INTRADAY_VOLUME_RATIO", 1.5),
        intraday_trend_filter=_env_bool("INTRADAY_TREND_FILTER", True),
        intraday_min_atr_pct=_env_float("INTRADAY_MIN_ATR_PCT", 0.1),
        futures_coins=_env_str("FUTURES_COINS", DEFAULT_FUTURES_COINS),
        futures_tactic=_env_str("FUTURES_TACTIC", "crash").lower(),
        futures_crash_hours=_env_int("FUTURES_CRASH_HOURS", 4),
        futures_crash_drop_pct=_env_float("FUTURES_CRASH_DROP_PCT", 10.0),
        futures_crash_liq=_env_bool("FUTURES_CRASH_LIQ", False),
        futures_crash_extra=canonical_crash_extra(_env_str("FUTURES_CRASH_EXTRA", "")),
        futures_crash_z=_env_float("FUTURES_CRASH_Z", 0.0),
        futures_crash_z_min_pct=_env_float("FUTURES_CRASH_Z_MIN_PCT", 3.0),
        futures_confirm=_env_str("FUTURES_CONFIRM", "close").lower(),
        futures_confirm_bars=_env_int("FUTURES_CONFIRM_BARS", 12),
        futures_confirm_volume_ratio=_env_float("FUTURES_CONFIRM_VOLUME_RATIO", 3.0),
        futures_pump_rules=canonical_crash_extra(_env_str("FUTURES_PUMP_RULES", "")),
        futures_bb=_env_str("FUTURES_BB", "off").lower(),
        futures_bb_std=_env_float("FUTURES_BB_STD", 2.0),
        futures_bb_rsi_low=_env_float("FUTURES_BB_RSI_LOW", 25.0),
        futures_bb_rsi_high=_env_float("FUTURES_BB_RSI_HIGH", 75.0),
        futures_breakout=_env_bool("FUTURES_BREAKOUT", False),
        futures_breakout_volume_ratio=_env_float("FUTURES_BREAKOUT_VOLUME_RATIO", 3.0),
        futures_breakout_hours=_env_int("FUTURES_BREAKOUT_HOURS", 4),
        futures_funding=_env_bool("FUTURES_FUNDING", False),
        futures_funding_rate_pct=_env_float("FUTURES_FUNDING_RATE_PCT", -0.10),
        futures_funding_rules=canonical_crash_extra(_env_str("FUTURES_FUNDING_RULES", "1:5")),
        futures_pullback=_env_bool("FUTURES_PULLBACK", False),
        futures_pullback_rsi=_env_float("FUTURES_PULLBACK_RSI", 25.0),
        futures_pullback_ema=_env_int("FUTURES_PULLBACK_EMA", 200),
        futures_entry_order=_env_str("FUTURES_ENTRY_ORDER", "market").lower(),
        futures_limit_offset_pct=_env_float("FUTURES_LIMIT_OFFSET_PCT", 0.05),
        futures_limit_ttl_min=_env_int("FUTURES_LIMIT_TTL_MIN", 3),
        futures_max_positions=_env_int("FUTURES_MAX_POSITIONS", 1),
        futures_extra_breadth=_env_int("FUTURES_EXTRA_BREADTH", 8),
        futures_isolated_size_pct=_env_float("FUTURES_ISOLATED_SIZE_PCT", 100.0),
        futures_isolated_breadth=_env_int("FUTURES_ISOLATED_BREADTH", 2),
        futures_extra_size_pct=_env_float("FUTURES_EXTRA_SIZE_PCT", 100.0),
        futures_reentry_breadth=_env_int("FUTURES_REENTRY_BREADTH", 0),
        futures_wave_rules=canonical_crash_extra(_env_str("FUTURES_WAVE_RULES", "")),
        futures_wave_breadth=_env_int("FUTURES_WAVE_BREADTH", 12),
        futures_extra_be_trigger_pct=_env_float("FUTURES_EXTRA_BE_TRIGGER_PCT", 0.0),
        futures_leverage=_env_int("FUTURES_LEVERAGE", 2),
        futures_margin_usage_pct=_env_float("FUTURES_MARGIN_USAGE_PCT", 95.0),
        futures_volume_ratio=_env_float("FUTURES_VOLUME_RATIO", 2.5),
        futures_liq_source=_env_str("FUTURES_LIQ_SOURCE", "proxy").lower(),
        futures_liq_ratio=_env_float("FUTURES_LIQ_RATIO", 3.0),
        futures_liq_min_usdt=_env_float("FUTURES_LIQ_MIN_USDT", 100_000.0),
        futures_div_oversold=_env_float("FUTURES_DIV_OVERSOLD", 40.0),
        futures_div_rise=_env_float("FUTURES_DIV_RISE", 3.0),
        futures_btc_trend=_env_bool("FUTURES_BTC_TREND", True),
        futures_sides=_env_str("FUTURES_SIDES", "both").lower(),
        futures_tp_order=_env_str("FUTURES_TP_ORDER", "limit").lower(),
        futures_taker_fee=_env_float("FUTURES_TAKER_FEE", 0.0005),
        futures_maker_fee=_env_float("FUTURES_MAKER_FEE", 0.0002),
        futures_slippage_pct=_env_float("FUTURES_SLIPPAGE_PCT", 0.02),
        futures_mmr_pct=_env_float("FUTURES_MMR_PCT", 0.5),
        futures_exchange_stop=_env_bool("FUTURES_EXCHANGE_STOP", True),
        futures_universe=_env_int("FUTURES_UNIVERSE", 0),
        futures_universe_with_list=_env_bool("FUTURES_UNIVERSE_WITH_LIST", True),
        futures_universe_min_age_days=_env_int("FUTURES_UNIVERSE_MIN_AGE_DAYS", 1095),
        futures_universe_avg_days=_env_int("FUTURES_UNIVERSE_AVG_DAYS", 180),
        futures_universe_mode=_env_str("FUTURES_UNIVERSE_MODE", "avg").lower(),
        futures_trailing=_env_bool("FUTURES_TRAILING", False),
        futures_trailing_act_pct=_env_float("FUTURES_TRAILING_ACT_PCT", 3.0),
        futures_trailing_gap_pct=_env_float("FUTURES_TRAILING_GAP_PCT", 1.5),
        futures_data_url=_env_str("FUTURES_DATA_URL", "https://data.binance.vision"),
        signal_confirm_scans=_env_int("SIGNAL_CONFIRM_SCANS", 1),
        min_atr_pct=_env_float("MIN_ATR_PCT", 0.25),
        max_atr_pct=_env_float("MAX_ATR_PCT", 4.0),
        regime_filter=_env_bool("REGIME_FILTER", True),
        regime_skip_weak=_env_bool("REGIME_SKIP_WEAK", False),
        equity_log_minutes=_env_int("EQUITY_LOG_MINUTES", 15),
        bnb_fee_check=_env_bool("BNB_FEE_CHECK", True),
        bnb_min_trades=_env_int("BNB_MIN_TRADES", 20),
        bnb_fees=_env_bool("BNB_FEES", False),
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
        stale_exit_minutes=_env_int("STALE_EXIT_MINUTES", 0),
        stale_exit_peak_pct=_env_float("STALE_EXIT_PEAK_PCT", 1.0),
        loss_cooldown_minutes=_env_int("LOSS_COOLDOWN_MINUTES", 0),
        prior_trend_filter=_env_bool("PRIOR_TREND_FILTER", False),
        sl_extend=_env_bool("SL_EXTEND", False),
        sl_extend_pct=_env_float("SL_EXTEND_PCT", 1.5),
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
    # Büyük Kâr Al (ör. turnuvadaki "trend" çıkışları: +%12 / 5 gün) uyarılmaz: iki ayrı 3 yıllık dönemde
    # ortalamaya dönüşte küçük hedeften iyi sonuç verdi. Süre sınırının hiç olmaması ise dönmeyen düşüşte bekletir.
    if is_mean_reversion(cfg.strategy) and cfg.max_hold_minutes <= 0:
        notes.append(f"STRATEGY={cfg.strategy} düşüş sonrası dönüşü saatler-günler içinde bekler; MAX_HOLD_MINUTES=0 "
                     "(süre sınırı yok) dönmeyen düşüşte pozisyonu süresiz tutar. Önerilen: " + " ".join(
                         f"{k}={v:g}" for k, v in MEAN_REVERSION_PROFILE.items()))
    if is_intraday(cfg.strategy):
        if cfg.max_hold_minutes <= 0 or cfg.max_hold_minutes > 1440:
            notes.append(f"STRATEGY=intraday gün içi al-sat içindir; MAX_HOLD_MINUTES={cfg.max_hold_minutes} işlemi "
                         f"günlerce tutabilir. Önerilen: MAX_HOLD_MINUTES={INTRADAY_PROFILE['MAX_HOLD_MINUTES']:g}")
        if cfg.take_profit_pct > 5 or cfg.stop_loss_pct > 5:
            notes.append(f"STRATEGY=intraday en büyük coinlerde işlem yapar; bunlar gün içinde nadiren %5 oynar "
                         f"(TAKE_PROFIT_PCT={cfg.take_profit_pct:g}, STOP_LOSS_PCT={cfg.stop_loss_pct:g}). Önerilen: "
                         + " ".join(f"{k}={v:g}" for k, v in INTRADAY_PROFILE.items()))
        below = [t for t in intraday_tactic_list(cfg) if t in INTRADAY_BELOW_VWAP_TACTICS]
        if cfg.vwap_filter and below:
            notes.append(f"VWAP_FILTER=true fiyat VWAP'ın altındayken alımı engeller; INTRADAY_TACTIC={','.join(below)} "
                         f"tam da düşüşte (VWAP altında) alır, bu yüzden çok az sinyal kalır. VWAP_FILTER=false önerilir.")
    if is_futures(cfg.strategy):
        if cfg.futures_tactic == "liq4":
            notes.append("FUTURES_TACTIC=liq4 (4'lü onay) 2020-2026 backtestinde 7 yılın 7'sinde de zarar etti: işlem başı "
                         "net -%0.14, 10x ve 30 USDT ile hesap 10 ayda eridi. Kârlı bulunan taktik: FUTURES_TACTIC=crash.")
        elif cfg.futures_leverage > 3:
            notes.append(f"FUTURES_TACTIC=crash, FUTURES_LEVERAGE={cfg.futures_leverage}: zarar kes %{cfg.stop_loss_pct:g} "
                         f"teminatın ~%{cfg.stop_loss_pct * cfg.futures_leverage:.0f}'i demek; testte 3x üstünde düşüşler "
                         f"%60-90'a çıktı. Önerilen kaldıraç 2x (en fazla 3x).")
        if cfg.futures_universe > 20:
            notes.append(f"FUTURES_UNIVERSE={cfg.futures_universe}: 2023-2026 testinde 20'den fazla köklü coin sonucu "
                         f"zayıflattı (40: ~%0-35/yıl, 60-80: zarar; Faz 3: 120-150 coinde yılda 1.000+ işlem ama işlem "
                         f"başı ~%0, hesap eridi); 10-20 coin kârlıydı. Önce 🧪 Backtest.")
        if cfg.futures_universe > 0 and cfg.futures_universe_min_age_days < 730:
            notes.append(f"FUTURES_UNIVERSE_MIN_AGE_DAYS={cfg.futures_universe_min_age_days}: yaş filtresi kısa; testte "
                         f"yeni / hype coinler (TRUMP, FARTCOIN, WIF...) çöküşten toparlanmadı, hesap eridi. Önerilen ≥ 1095.")
        if cfg.futures_trailing:
            notes.append("FUTURES_TRAILING=true: izleyen stop 2023-2026 testinde bu stratejinin sonucunu iyileştirmedi "
                         "(sabit listede çoğu ayarda yıllık getiri +%62'den +%27-48'e indi).")
        if cfg.futures_tactic == "crash":
            if cfg.futures_confirm in ("green", "either"):
                notes.append(f"FUTURES_CONFIRM={cfg.futures_confirm}: ızgara testinde (2020-2026) ilk yeşil mum teyidi "
                             f"her pencerede işlem başı kârı düşürdü (ör. 4 saat %10: +%0.46 → +%0.05). Önce 🧪 Backtest.")
            if cfg.futures_crash_z > 0:
                notes.append("FUTURES_CRASH_Z: ızgara testinde oynaklık (σ) eşiği aynı işlem sayısında sabit yüzde "
                             "eşiğinden kötü çıktı. Önce 🧪 Backtest.")
            long_w = futures_long_windows(cfg)
            if long_w and not cfg.futures_crash_z:
                names = " + ".join(f"{h} saatte %{p:g}" for h, p in long_w)
                notes.append(f"FUTURES_CRASH_EXTRA={cfg.futures_crash_extra}: uzun pencere ({names}) Kasım 2026 "
                             f"testinde (2020-2026, 1 dk) kârı düşürdü: yalnız bu pencerenin bulduğu işlemler (yılda "
                             f"~100) işlem başı ~%0 getirdi (diğerleri +%0.37) ve çoğu tam boy ilk pozisyonu kapattı. "
                             f"Kapalıyken (yalnız 1 saatte %6) doğrulama dönemi yıllık getirisi, düzeltilmiş testte "
                             f"(giriş gecikmesi ve mum içi başabaş payıyla) Faz 6 setinde (3x) +%100 → +%166 (en büyük "
                             f"düşüş %55 → %45), 5x'te +%133 → +%279 (%76 → %66). Telegram → 🧭 Giriş sinyali → ➕ uzun "
                             f"pencere olmadan (ör. '+ 1 sa %6').")
            if (cfg.futures_crash_extra and cfg.breakeven_enabled
                    and (cfg.breakeven_trigger_pct > 1.2 or cfg.max_hold_minutes < 45)):
                notes.append("FUTURES_CRASH_EXTRA: testte ek pencereler başabaş +1 → +0.3 ve 45-60 dk zaman stopuyla "
                             "kârlı oldu; şimdiki çıkışlarla (başabaş +%g, %d dk) kâr düşebilir. Telegram → 🧭 Giriş "
                             "sinyali → 🏆 / 🥈 Izgara setleri çıkışları da yazar." % (cfg.breakeven_trigger_pct,
                                                                                    cfg.max_hold_minutes))
            mods = futures_modules(cfg)
            names = {"pump": "pompa short", "bb": "Bollinger + RSI", "breakout": "hacimli kırılım"}
            phase3 = [m for m in mods if m in names]
            if phase3:
                notes.append("Ek giriş modülleri açık (" + ", ".join(names[m] for m in phase3) + "): Faz 3 testinde "
                             "(2020-2026) üçü de tek başına ve çöküşle birlikte zarar etti; işlem sayısı artar ama "
                             "işlem başı sonuç maliyetin altında kalır (README). Yalnızca test modunda deneyin.")
            if "pullback" in mods:
                notes.append("FUTURES_PULLBACK=true (trend içi düzeltme): Faz 4 testinde (2020-2026, 1 dk) çöküşle "
                             "birlikte yılda ~1.240 işlem ama işlem başı -%0.01, kazanma %56; 2022-2026 her yıl zarar, "
                             "hesap 30 USDT'den 3 USDT'ye indi. Yalnızca test modunda deneyin.")
            if "funding" in mods:
                notes.append("FUTURES_FUNDING=true (fonlama sıkışması): Faz 4 testinde çöküş kuralına yılda yalnızca "
                             "~4 işlem ekledi (fonlama sıkışmalarının çoğu zaten çöküş kuralına giriyor); sonuç "
                             "neredeyse aynı kaldı. Tek başına yılda ~45 işlem, işlem başı +%0.16 (kayma ×3: +%0.02).")
        if cfg.futures_entry_order == "limit":
            notes.append("FUTURES_ENTRY_ORDER=limit: Faz 4 testinde (2020-2026, 1 dk) limit giriş maliyeti düşürdü "
                         "(komisyon + kayma işlem başı ~%0.05 az) ama emirlerin ~%11'i dolmadı ve kaçanlar en hızlı "
                         "toparlanan işlemlerdi (ortalama +%1.06, %93'ü kârlı): işlem başı +%0.31 → +%0.23, yıllık "
                         "getiri +%587 / +%213 → +%234 / +%105. Kayma 3 katına çıksa da piyasa emri önde.")
        if cfg.futures_max_positions > 1:
            x = cfg.futures_extra_size_pct / 100
            if x >= 1:                   # Eşit büyüklükler (Faz 5)
                per = cfg.futures_margin_usage_pct / cfg.futures_max_positions
                notes.append(f"FUTURES_MAX_POSITIONS={cfg.futures_max_positions} (🌊 dalga modu): her pozisyon bakiyenin "
                             f"%{per:.1f}'i teminat × {cfg.futures_leverage}x; 2. ve sonraki pozisyon yalnızca son 1 "
                             f"saatte ≥{cfg.futures_extra_breadth} coin %3+ düşerken. Düzeltilmiş testte (Kasım 2026: "
                             f"giriş gecikmesi ve mum içi başabaş payıyla; 2 pozisyon, 3x, ≥8 coin) yılda ~445 işlem (tek "
                             f"pozisyonda ~340), işlem başı +%0.21, yıllık getiri +%177 / +%90, en büyük düşüş %76 / %52 "
                             f"(tek pozisyon 2x: +%193 / +%90, %72 / %60); zarar kes emirleri fitilin yarısından dolarsa "
                             f"(kötümser senaryo) ikisinde de hesap eridi. Önce test modunda deneyin.")
                if cfg.futures_max_positions > 2:
                    notes.append("FUTURES_MAX_POSITIONS 3 ve üstü: testte düşüşü %83-95'e çıkardı (aynı dalgadaki "
                                 "pozisyonlar birlikte zarar kesiyor). Önerilen 2.")
            else:                        # Faz 6: ilk pozisyon büyük, ek pozisyonlar küçük
                lev = cfg.futures_leverage
                first = cfg.futures_margin_usage_pct * futures_slot_share(cfg, 0)
                each = cfg.futures_margin_usage_pct * futures_slot_share(cfg, 1)
                notes.append(f"FUTURES_MAX_POSITIONS={cfg.futures_max_positions}, FUTURES_EXTRA_SIZE_PCT="
                             f"{cfg.futures_extra_size_pct:g} (🌊 dalga modu, Faz 6 büyüklükleri): ilk pozisyon "
                             f"bakiyenin %{first:.1f}'i teminat × {lev}x (bakiyenin {first * lev / 100:.2f} katı; #2: 1.90), "
                             f"son 1 saatte ≥{cfg.futures_extra_breadth} coin %3+ düşerken açılan her ek pozisyon "
                             f"%{each:.1f}'i ({each * lev / 100:.2f} katı). Düzeltilmiş testte (Kasım 2026: giriş "
                             f"gecikmesi ve mum içi başabaş payıyla) Faz 6 setinde (4 pozisyon, 3x, ek %16.7, dalgada "
                             f"yeniden giriş ve dalga eşikleriyle) yılda ~780 işlem, işlem başı +%0.22, yıllık getiri "
                             f"+%270 / +%166, en büyük düşüş %66 / %45 (#2: 341 işlem, +%193 / +%90, %72 / %60; Faz 6 tek "
                             f"pozisyon 2x: 387 işlem, +%379 / +%159, %59 / %45); kötümser stop dolumunda 30 USDT → 12 "
                             f"USDT (#2: eridi). 5x'te +%412 / +%279 ama düşüş %87 / %66 ve kötümser stopta hesap eridi. "
                             f"Eski raporlardaki rakamlar (+%1.001 / +%353) iyimserdi. Önce test modunda deneyin.")
                if first * lev / 100 < 1.8:
                    notes.append(f"İlk pozisyon #2'den küçük (bakiyenin {first * lev / 100:.2f} katı, #2: 1.90): işlemlerin "
                                 f"çoğu tek pozisyon olduğundan getiri düşer. Faz 6 seti 3x ve (pozisyon sayısı - 1) × "
                                 f"ek büyüklük = %50 ile ilk pozisyonu #2 boyunda tutar.")
                if (cfg.futures_max_positions - 1) * x > 0.75:
                    notes.append("Ek pozisyonların toplamı ilk pozisyonun %75'inden büyük: düzeltilmiş testte (ör. 5 "
                                 "pozisyon, ekler ilkinin %25'i, 4x) yıllık getiri +%266 / +%145, en büyük düşüş %79 / "
                                 "%58, kötümser stopta hesap eridi. Önerilen: 4 pozisyon, ek %16.7.")
            if cfg.futures_universe <= 0:
                notes.append("Dalga modu yalnızca FUTURES_COINS listesinde: genişlik eşikleri (≥8 coin) liste + en hacimli "
                             "20 köklü coin (~25 coin) için seçildi; 10 coinlik listede ek pozisyon çok seyrek açılır.")
        if cfg.futures_isolated_size_pct < 100:
            notes.append(f"FUTURES_ISOLATED_SIZE_PCT={cfg.futures_isolated_size_pct:g}: son 1 saatte "
                         f"{cfg.futures_isolated_breadth}'den az coin %3+ düşerken açılan (yalnız çöküş) pozisyonlar "
                         f"%{cfg.futures_isolated_size_pct:g} büyüklükte. Düzeltilmiş testte (Kasım 2026) yalnız çöküşler "
                         f"işlem başı ~%0 getirdi; yarım büyüklük işlem sayısını değiştirmeden #2'de yıllık getiriyi "
                         f"+%193 / +%90'dan +%228 / +%103'e çıkardı, doğrulama dönemi düşüşünü %60'tan %46'ya indirdi.")
        if cfg.futures_reentry_breadth > 0:
            if cfg.loss_cooldown_minutes <= cfg.symbol_cooldown_minutes:
                notes.append(f"FUTURES_REENTRY_BREADTH={cfg.futures_reentry_breadth}: LOSS_COOLDOWN_MINUTES="
                             f"{cfg.loss_cooldown_minutes} olduğu için zarar sonrası ayrı bir bekleme yok, bu ayar etkisiz "
                             f"(Faz 6 240 dk ile test edildi; ⭐ Faz 6 setleri 240 yazar).")
            else:
                notes.append(f"FUTURES_REENTRY_BREADTH={cfg.futures_reentry_breadth} (Faz 6, dalgada yeniden giriş): son 1 "
                             f"saatte ≥{cfg.futures_reentry_breadth} coin %3+ düşerken zarar sonrası "
                             f"{cfg.loss_cooldown_minutes} dk bekleme uygulanmaz (yalnız çöküşte sürer). Düzeltilmiş "
                             f"testte (Kasım 2026) dalgada zarar sonrası yeniden girişler işlem başı +%0.62 kazandırdı "
                             f"(12+ coin; sıradan dalga işlemi +%0.53); #2 + yalnız çöküş %50'de yılda 341 → 369 işlem, "
                             f"yıllık getiri +%228 / +%103 → +%367 / +%147, en büyük düşüş %71 / %46 → %61 / %45.")
        wave = futures_wave_rules(cfg)
        if wave:
            desc = " veya ".join(f"{h} saatte %{p:g}" for h, p in wave)
            notes.append(f"FUTURES_WAVE_RULES={format_crash_rules(wave)} (Faz 6, dalga eşikleri): son 1 saatte "
                         f"≥{cfg.futures_wave_breadth} coin %3+ düşerken {desc} düşüş de sinyal. Düzeltilmiş testte "
                         f"(Kasım 2026; 1:5,4:8, ≥12) tek pozisyonda yılda 369 → 387 işlem, yıllık getiri +%367 / +%147 "
                         f"→ +%379 / +%159; Faz 6 setinde (4 pozisyon) 682 → 780 işlem, +%274 / +%149 → +%270 / +%166. "
                         f"Bu sinyallerin kendi kazancı ince (işlem başı +%0.08).")
        elif (cfg.futures_wave_rules or "").strip():
            notes.append("FUTURES_WAVE_RULES yazılı ama kullanılmıyor: dalga eşikleri yalnızca çöküş taktiğinde ve sabit "
                         "yüzdelerle (FUTURES_CRASH_Z=0) çalışır.")
        xbe = cfg.futures_extra_be_trigger_pct
        if xbe > 0:
            if cfg.futures_max_positions <= 1 or not cfg.breakeven_enabled:
                notes.append(f"FUTURES_EXTRA_BE_TRIGGER_PCT={xbe:g} (Faz 8) etkisiz: ek pozisyon yalnızca dalga modunda "
                             f"(FUTURES_MAX_POSITIONS > 1) açılır ve başabaş kalkanı açık olmalı.")
            elif xbe >= cfg.breakeven_trigger_pct:
                notes.append(f"FUTURES_EXTRA_BE_TRIGGER_PCT={xbe:g} ana başabaş tetiğinden (BREAKEVEN_TRIGGER_PCT="
                             f"{cfg.breakeven_trigger_pct:g}) küçük değil: Faz 8 testindeki kazanç ek pozisyonlarda "
                             f"tetiği erkene (+%{FUTURES_PHASE8_BE_TRIGGER:g}) almaktan geldi; bu değer test edilmedi.")
            else:
                gap = xbe - cfg.breakeven_floor_pct
                notes.append(f"FUTURES_EXTRA_BE_TRIGGER_PCT={xbe:g} (Faz 8): dalga modunda ek pozisyonların başabaş "
                             f"kalkanı fiyat +%{xbe:g}'te devreye girer (ilk pozisyonda +%{cfg.breakeven_trigger_pct:g}); "
                             f"yuvaları daha çabuk boşalıp yeni sinyale geçer. DÜZELTME (Kasım 2026, rapor 15): ilk "
                             f"testteki kâr artışı backtestin iyimserliğiydi; 1 dk mumlar, tetik ile stop arası bu kadar "
                             f"darken (%{gap:g}) fiyatın aynı dakikada tabana geri dönüşünü görmüyordu. Saniyelik işlem "
                             f"verisiyle düzeltilmiş testte (+%0.5; Faz 6 seti) yılda ~780 → ~925 işlem, işlem başı "
                             f"+%0.22 → +%0.15; yıllık getiri değişmedi (+%270 / +%166 → +%246 / +%168), en büyük düşüş "
                             f"%66 / %45 → %67 / %53, 3 kat kaymada +%142 / +%92 → +%82 / +%53, 5 kat kaymada 30 USDT "
                             f"243 yerine 38 USDT'ye çıktı. Yani işlem sayısı artar, kâr artmaz, maliyete dayanıklılık "
                             f"azalır. Öneri: kapalı (0).")
        if cfg.futures_universe > 0 and cfg.futures_universe_mode == "daily":
            notes.append("FUTURES_UNIVERSE_MODE=daily (dünün en hacimli coinleri): Faz 4 testinde günlük en hacimli 30 "
                         "coin, en az 3 yıllık olanlarla bile 2024-2026'da her yıl -%66 ile -%91 arası zarar etti; "
                         "dünün hacim patlaması yapan coinleri çöküşten toparlanmıyor. Önerilen: avg (6 ay ortalama).")
            if cfg.breakeven_enabled and cfg.breakeven_floor_pct < 0.3:
                notes.append(f"BREAKEVEN_FLOOR_PCT={cfg.breakeven_floor_pct:g}: başabaş stopu maliyetlere (komisyon + "
                             f"kayma ≈ %0.14) çok yakın; testte kayma biraz artınca bu küçük kazançlar zarara döndü. "
                             f"Önerilen en az +0.3.")
        if cfg.futures_liq_source != "proxy":
            notes.append(f"FUTURES_LIQ_SOURCE={cfg.futures_liq_source}: canlı likidasyon akışı kullanılır; bu ölçüm "
                         f"backtest edilemez (backtest mumlardan ölçer), sonuçlar backtestten farklı olabilir.")
        if cfg.futures_leverage > 10:
            notes.append(f"FUTURES_LEVERAGE={cfg.futures_leverage}: likidasyon ~%{futures_liq_distance_pct(cfg.futures_leverage, cfg.futures_mmr_pct):.1f} "
                         f"fiyat hareketinde; ani fitillerde zarar kes kayabilir. Backtestte kaldıraç arttıkça hesap daha "
                         f"hızlı eridi.")
        return notes   # Spot'a özgü (izleyen stop, rejim...) uyarılar vadeli motorda anlamsız
    if is_rotation(cfg.strategy):
        if cfg.stop_loss_pct < 10:
            notes.append(f"STRATEGY=rotation coinleri haftalarca tutar; STOP_LOSS_PCT={cfg.stop_loss_pct:g} haftalık "
                         f"dalgalanmada bile satar. Rotasyonda zarar kes yalnızca felaket korumasıdır, önerilen: "
                         + " ".join(f"{k}={v:g}" for k, v in ROTATION_PROFILE.items()))
        if cfg.rotation_top_n > cfg.max_open_positions:
            notes.append(f"ROTATION_TOP_N={cfg.rotation_top_n} ama MAX_OPEN_POSITIONS={cfg.max_open_positions}: "
                         f"en fazla {cfg.max_open_positions} coin tutulur.")
        if rotation_hold_count(cfg) < cfg.rotation_top_n:
            notes.append(f"ROTATION_TOP_N={cfg.rotation_top_n} ama ROTATION_COINS'te {rotation_hold_count(cfg)} coin "
                         f"var: en fazla {rotation_hold_count(cfg)} coin tutulur, sermaye onlara bölünür.")
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
    if strategy_family(cfg.strategy) == "futures" and family != "futures":
        # Vadeli profilinin bekleme / seri sigortası değerleri spot stratejiye taşınmasın
        changes.update({PROFILE_ATTRS[k]: v for k, v in SPOT_RISK_DEFAULTS.items()
                        if k not in PROFILES.get(family, {})})
    if profile:
        # Profildeki türle (1.5 → float, 2880 → int): mevcut değerin türüne çevirmek 1.5'i 1'e kırpabilirdi
        changes.update({PROFILE_ATTRS[k]: v for k, v in PROFILES[family].items()})
        if family in ("swing", "mr"):
            # Üst sınır: .env'deki daha geniş değer korunur (yalnızca 5 dk için daraltılmışsa genişletilir; turnuva
            # da 1 saatlik stratejileri bu sınırla dener, normal backtest aynı sonucu versin)
            changes["max_24h_gain_pct"] = max(cfg.max_24h_gain_pct, SWING_EXIT_PROFILE["MAX_24H_GAIN_PCT"])
    return replace(cfg, **changes), profile
