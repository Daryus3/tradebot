"""
Telegram'daki ayrıntılı, düğmeli ayar sayfaları (/menu → ⚙️ Ayarlar, 🧯 Risk / Kill-switch, 🔔 Bildirim).

- Vadeli işlem: kaldıraç ve teminat (ana sayfa), giriş sinyali (taktik ve eşikleri), çıkışlar ve emirler, coinler.
- Kill-switch (toplam zarar limiti), risk sigortaları ve bekleme süreleri, raporlar ve sessiz saatler, spot çıkış
  ayrıntıları (izleyen stop tabanı, maks. süre, başabaş, Binance yedek stopu).

Her sayfa satırlardan oluşur; bir satır bir ayarın hazır değerlerini (seçili olan ✅), ± adım düğmelerini ya da
aç / kapa düğmesini gösterir. Düğme verisi (Telegram sınırı 64 bayt):
  pg:<sayfa>                       sayfayı göster
  kv:<sayfa>:<AYAR>:<değer>        ayarı bu değere getir (saatlerde ":" yazılmaz: 2355 = 23:55, off = kapalı)
  kv:<sayfa>:<AYAR>:+ | - | t | n  bir adım artır / azalt, aç-kapa, sıradaki seçenek
  fc:<COIN> | fc:default           vadeli coin listesine ekle / çıkar, varsayılan listeye dön
  ftac:<taktik>                    vadeli taktik (onaydan sonra o taktiğin çıkış ayarları da yüklenir)
  fgp:<n>                          ızgara testinin n. en iyi giriş kuralı (onaydan sonra birlikte yazılır)
  fcm:on | fcm:off                 🌊 dalga modunun önerilen seti / kapatma (onaydan sonra birlikte yazılır)
  fcm:p6 | fcm:p6s                 ⭐ Faz 6 seti / Faz 6 tek pozisyon (onaydan sonra birlikte yazılır)
Değişiklik ControlPanel.request_change ile doğrulanır, hemen uygulanır ve .env'ye yazılır. Kill-switch'i gevşetmek
ve Binance'teki zarar kes güvencesini kapatmak onay ister; şu anki zararın altında bir kill-switch seçilemez.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from config import (futures_scan_shifts, futures_snapshot_on, DEFAULT_FUTURES_COINS, FUTURES_BB_MODES, FUTURES_CASCADE_PRESET, FUTURES_CONFIRMS,
                    FUTURES_ENTRY_ORDERS, FUTURES_PHASE6_OFF, FUTURES_PHASE6_PRESET, FUTURES_PHASE6_SINGLE_PRESET,
                    FUTURES_PHASE8_OFF, FUTURES_PHASE9_OFF, FUTURES_GRID_PRESETS, FUTURES_LIQ_SOURCES, FUTURES_RECOMMENDED, FUTURES_SIDES,
                    FUTURES_TACTIC_EXITS, FUTURES_TACTICS, FUTURES_TP_ORDERS, FUTURES_UNIVERSE_MODES, futures_be_trigger,
                    futures_hold_minutes,
                    futures_coin_list, futures_crash_rules,
                    futures_funding_rules, futures_liq_distance_pct, futures_modules, futures_pump_rules,
                    futures_slot_share, futures_wave_rules, is_futures, is_rotation, parse_crash_rules)
from runtime_settings import EDITABLE, format_value
from telegram_bot import LINE, esc, fmt_duration, fmt_signed, fmt_usdt

if TYPE_CHECKING:  # pragma: no cover
    from control_panel import ControlPanel

Button = Tuple[str, str]
NOTIFY_LABELS = {"all": "Tümü", "closes": "Sadece Kapanışlar", "silent": "Sessiz Mod"}
NOTIFY_HELP = {"all": "her alım, satış, izleyen stop ve rapor",
               "closes": "yalnızca pozisyon kapanışları (alım ve periyodik raporlar gelmez)",
               "silent": "yalnızca acil durumlar (zarar kes, kill-switch, hatalar)"}
TACTIC_LABELS = {"crash": "💥 Çöküş dönüşü", "liq4": "🧩 4'lü onay"}
SIDE_LABELS = {"both": "↕️ İki yön", "long": "📈 Yalnız long", "short": "📉 Yalnız short"}
SOURCE_LABELS = {"proxy": "📊 Mumlardan", "feed": "📡 Canlı akış", "both": "🔗 İkisi"}
TP_ORDER_LABELS = {"limit": "📌 Kâr Al: limit emir", "market": "⚡ Kâr Al: piyasa"}
CONFIRM_LABELS = {"close": "⚡ Sadece kapanış", "volume": "📊 Hacim zirvesi", "green": "🟢 İlk yeşil mum",
                  "either": "🔀 Hacim veya yeşil"}
CONFIRM_TEXT = {"close": "yok — çöküş mumunun kapanışında hemen alınır",
                "volume": "hacim zirvesi (kapitülasyon: mum hacmi önceki 20 mumun {ratio:g} katı)",
                "green": "ilk yeşil mum (kapanış > açılış)",
                "either": "hacim zirvesi ({ratio:g}x) ya da ilk yeşil mum, hangisi önce gelirse"}
# Ek giriş modülleri (Faz 3)
PUMP_CHOICES = ("", "1:6,4:10,12:15", "1:10,4:15,12:25")
BB_LABELS = {"off": "〰️ Bant kapalı", "both": "〰️ Long + short", "long": "〰️ Yalnız long", "short": "〰️ Yalnız short"}
MODULE_LABELS = {"pump": "pompa short", "bb": "Bollinger", "breakout": "kırılım", "funding": "fonlama",
                 "pullback": "düzeltme"}
# Faz 4: fonlama sıkışması, trend içi düzeltme, limit (maker) giriş, günlük hacim sıralaması
FUNDING_RULE_CHOICES = ("1:5", "1:3", "1:6")
ENTRY_ORDER_LABELS = {"market": "⚡ Giriş: piyasa emri", "limit": "📌 Giriş: limit (maker)"}
UNIVERSE_MODE_LABELS = {"avg": "📊 Ortalama hacim", "daily": "📅 Dünün hacmi (her gün)"}
# Dalga modu (Faz 5): kapatınca tek pozisyon, tam büyüklük ve 🥈 #2'nin kaldıracı (Faz 6 ekleri de kapanır)
CASCADE_OFF = {"FUTURES_MAX_POSITIONS": 1, "FUTURES_ISOLATED_SIZE_PCT": 100, "FUTURES_LEVERAGE": 2}
# Faz 6 setleri (Faz 5 seti bunlardan bağımsız; Faz 5 düğmesi Faz 6 eklerini kapatır, Faz 5 tam olarak Faz 5 kalır)
PHASE6_PRESETS = {"p6": FUTURES_PHASE6_PRESET, "p6s": FUTURES_PHASE6_SINGLE_PRESET}
WAVE_RULE_CHOICES = ("", "1:5,4:8")
# Faz 8: ek pozisyonların başabaş tetiği (0 = ana ayar). Faz 5 / Faz 6 setleri ve "kapat" düğmesi bunu da kapatır.
EXTRA_BE_CHOICES = (0, 0.4, 0.5, 0.6, 0.75)
# Faz 9: pozisyona göre zaman stopu (0 = ana ayar). Faz 5 / Faz 6 setleri ve "kapat" düğmesi bunları da kapatır.
EXTRA_HOLD_CHOICES = (0, 30, 35)
WAVE_HOLD_CHOICES = (0, 60, 70)
PHASE9_HINT = ("Faz 9 testi (rapor 17 araştırması; 2020-2026, 1 dk, 10 çekilişin ortancası): dalgada (≥8 coin düşerken) "
               "açılan ilk pozisyonu 45 yerine 60 dk tutmak, mevcut ayarın üstünde işlem sayısını değiştirmeden bileşiksiz "
               "getiriyi +%332 / +%212'den +%341 / +%222'ye, son 12 ayı +%81'den +%120'ye çıkardı ve hiçbir yılda geride "
               "kalmadı. İlk başabaş 1.1 + ek başabaş 0.4 + yalnız çöküş %25 + ek pozisyon 35 dk ile birlikte yılda 888 → "
               "929 işlem, +%379 / +%236, son 12 ay +%152; 2025'te geride. Ek pozisyonda 25 dk ve altı, dalgada 50-55 dk "
               "testte kötüydü; düşüş ve kötümser stop riski değişmiyor.")
PHASE8_HINT = ("Faz 8 (ek pozisyonlarda başabaş +%0.5): küçük pozisyon kârını erken kilitleyip kapanır, yuvası yeni "
               "sinyale geçer. DÜZELTME (Kasım 2026, rapor 15): ilk testteki kâr artışı backtestin iyimserliğiydi. "
               "Saniyelik işlem verisiyle düzeltilmiş testte Faz 6 setinde yılda ~780 → ~925 işlem, ama yıllık getiri "
               "aynı (+%270 / +%166 → +%246 / +%168), en büyük düşüş %66 / %45 → %67 / %53, 3 kat kaymada +%142 / +%92 "
               "→ +%82 / +%53. İşlem sayısı artar, kâr artmaz, maliyete dayanıklılık azalır. Çok çekilişli testte kapatmak "
               "işlem sayısını %15-20 düşürdü ve getiriyi artırmadı (Faz 9 setinde +%379 / +%236 → +%376 / +%226, son 12 ay "
               "+%152 → +%36): daha çok işlem için açık (0.4), maliyete dayanıklılık için kapalı.")
MINUTE_SCAN_HINT = ("Her dakika tarama: çöküş kuralı yalnızca 5 dk kapanışlarında değil her dakika değerlendirilir. "
                    "Testte (Faz 9 seti, 10 çekiliş) yılda 929 → 1.662 işlem, yıllık getiri +%391 / +%235 → +%536 / "
                    "+%278; ama doğrulamada düşüş %68 → %77, 3 kat kaymada +%144 → +%56, 2022 ve 2024'te geride. "
                    "Backtest ilk seferde ~2 GB 1 dk veri indirir ve birkaç kat uzun sürer.")
FAST_ENTRY_HINT = ("Hızlı giriş: saniyelik veride çöküş mumunun kapanışından sonra fiyat ortalama 1 sn'de %0.015, 3 sn'de "
                   "%0.04, 5 sn'de %0.05, 10 sn'de %0.08 yükseliyor; backtest 5 sn (%0.05) varsayar. Kurallar ve "
                   "pozisyon boyları değişmez, yalnızca emir daha erken gider. Açıkken taranan coinlerin hepsinde "
                   "teminat modu ve kaldıraç önceden ayarlanır. Ölçülen gecikme her pozisyon mesajında ve backtest "
                   "raporunda (canlı bot karşılaştırması) görünür: önce kapalıyken ölçüp sonra açarak karşılaştırın.")
SNAPSHOT_HINT = ("Anlık tarama (hızlı giriş açıkken): Binance az önce kapanan mumu 0.3-2.5 sn sonra kesinleştirir; bot "
                 "bunu beklemek yerine geçmiş mumları kapanıştan hemen önce alır, kapanıştan ~0.15 sn sonra tek istekle "
                 "son işlem fiyatlarını okur ve kapanış olarak onu kullanır. Emir de bakiye / emir defteri isteği "
                 "beklemeden gider, açık pozisyon varken tarama pozisyon kontrolünü beklemez. Kural ve eşikler aynıdır. "
                 "Backtestte giriş payı %0.05 → %0.02 olunca bileşiksiz yıllık getiri +%558 / +%305 → +%598 / +%345 "
                 "(rapordaki 'Giriş 1 sn'de dolsaydı' satırı). Bot her anlık taramayı sonradan kesinleşen mumlarla "
                 "kendisi doğrular; sonuç raporda 'Anlık tarama doğrulaması' satırındadır.")
PHASE6_HINT = ("Faz 6 testi (2020-2026, 1 dk, liste + en hacimli 20 köklü coin, 🥈 #2 giriş / çıkışları, 30 USDT): "
               "dalgada zarar kesen coin genelde düşmeye devam edip daha derinden dönüyor; zarar sonrası 240 dk "
               "bekleme bu en kârlı ikinci girişi kaçırıyordu (12+ coin düşerken işlem başı +%1.10). Faz 6 seti (4 "
               "pozisyon: ilk #2 boyunda + dalgada 3 küçük ek, 3x; dalgada yeniden giriş ≥3 coin; 12+ coin düşerken "
               "1 saatte %5 / 4 saatte %8 de sinyal): yılda ~800 işlem (#2: 343), yıllık getiri iki dönemde de #2'den "
               "yüksek, en büyük düşüş #2 kadar. Faz 6 tek pozisyon: yılda ~400 işlem, düşüş #2 kadar. Rakamlar README'de "
               "(Faz 6). Kötümser stop dolumunda ve kaymada da #2'den iyi ama ikisi birlikte her ayar zarar eder. Önce "
               "test modunda (paper) izleyin.")
CASCADE_HINT = ("Faz 5 testi (2020-2026, 1 dk, liste + en hacimli 20 köklü coin, 🥈 #2 çıkışları): kâr piyasa geneli "
                "dalgalardan geliyor (8+ coin düşerken işlem başı ~+%0.46, tek coin düşerken ~%0). Faz 5 seti (2 "
                "pozisyon, 3x, ek pozisyon ≥8 coin, yalnız çöküş %50): yılda ~450 işlem (343 yerine), kazanma %81.7, "
                "işlem başı +%0.34 (+%0.31 yerine), yıllık getiri +%690 / +%233 (+%587 / +%212 yerine), kayma 3 kat "
                "olsa da #2'den iyi; bedeli en büyük düşüş %71 / %42 (%52 / %39 yerine) ve zarar kes emirleri büyük "
                "fitillerde kötü dolarsa (kötümser senaryo) Mayıs 2021'de hesabın erimesi (#2 erimedi). Aynı set 2x'te "
                "daha güvenli ama #2'den az kâr. Yalnızca 'yalnız çöküş %50' (tek pozisyon): işlem sayısı aynı, her "
                "senaryoda düşüş daha az (%46 / %34). Önce test modunda (paper) izleyin.")
# Ek çöküş pencereleri (ana kurala VEYA ile): boş, ızgara testinin öne çıkanları, mikro + makro
CRASH_EXTRA_CHOICES = ("", "1:7", "1:6", "1:6,12:15", "1:7,12:15", "1:5,12:15")
LEVERAGE_CHOICES = (1, 2, 3, 5, 10, 15, 20)
UNIVERSE_CHOICES = (0, 10, 20, 30, 40, 60, 80, 100, 120, 150)     # 0 = FUTURES_COINS listesi
# En hacimli N evreninin backtest özeti (Kasım 2026 araştırması; ayrıntı README)
UNIVERSE_HINT = ("En iyi sonuç: 📋 liste + en hacimli 20 köklü coin (beklemesiz): doğrulamada yılda 137 işlem, +%99/yıl "
                 "(yalnızca liste: 53 işlem, +%62). Test (doğrulama 2023-11 → 2026-10, 2x, en az 3 yıllık coinler, 6 ay "
                 "ort. hacim, liste olmadan): sabit liste "
                 "+%62/yıl; ilk 10 +%55, ilk 20 +%81 (en büyük düşüş %33), ilk 40 +%11 (düşüş %78), ilk 60 -%8, "
                 "ilk 80 -%42. Yaş filtresi olmadan (yeni / hype coinler dahil) her N'de hesap eridi.")
TRAIL_HINT = ("Test (2023-11 → 2026-10): izleyen stop bu stratejide sonucu iyileştirmedi (sabit listede çoğu ayarda "
              "+%62 → +%27-48/yıl); başabaş +1.5 → +0.3 en düşük düşüşü verdi, +4 / +5 tetik getiri kazandırmadan "
              "düşüşü %25'ten %39'a çıkardı. Backtest izleyen stopu olduğundan iyi gösterir: saniyelik veriyle "
              "kalibrasyonda (Kasım 2026, 1.935 işlem) izleyen stop işlem başı getiriyi %0.04-0.13 düşürdü.")
MODULES_HINT = ("Faz 3 testi (2020-2026, TP 8 / SL 5 / başabaş +1 → +0.3 / 45 dk, 1 dk doğrulama): üç modül de zarar "
                "etti. Pompa short yılda ~740 işlem, kazanma %71 ama işlem başı -%0.11; çöküşle birlikte yılda ~1.000 "
                "işlem ama hesap eridi. Bollinger ve kırılım binlerce işlem, işlem başı -%0.1 civarı; hepsi birlikte "
                "yılda 5.000+ işlem, 2025'te -%91. Faz 4: fonlama sıkışması çöküşe yılda yalnızca ~4 işlem ekledi "
                "(sonuç aynı); trend içi düzeltme çöküşle yılda ~1.240 işlem ama işlem başı -%0.01, 2022-2026 her yıl "
                "zarar (ayrıntı README). Yalnızca test modunda deneyin.")
# Coin sayfasındaki seçenekler: varsayılan 10 coin (backtestte kullanılanlar) + vadelisi eski ve hacimli büyük coinler
FUTURES_COIN_CHOICES = tuple(DEFAULT_FUTURES_COINS.split(",")) + ("AVAX", "LTC", "BCH", "XLM", "NEAR", "ATOM")


# ================================================================ satır tanımları
@dataclass(frozen=True)
class Row:
    """Bir ayarın düğmeleri: hazır değerler (values), ± adım (step) ya da (ikisi de yoksa) aç-kapa / sıradaki seçenek."""
    key: str                                         # EDITABLE anahtarı
    icon: str = ""                                   # İlk hazır değer düğmesinin başına
    name: str = ""                                   # Aç-kapa ve ± düğmelerindeki ad
    values: object = ()                              # Hazır değerler: demet ya da cfg → demet
    step: object = 0.0                               # ± adım: sayı ya da cfg → sayı (0 = yok)
    unit: str = ""                                   # ± düğmesindeki birim (ör. "x", " dk")
    fmt: Optional[Callable[[object], str]] = None    # Değer → düğme yazısı
    show: Optional[Callable[[object], bool]] = None  # cfg → bu satır gösterilsin mi
    lo: Optional[float] = None                       # ± ile en düşük / en yüksek değer
    hi: Optional[float] = None
    cb: str = ""                                     # Hazır değerlerin eski düğme biçimi: "<cb>:<değer>" (ör. dl:3)
    step_cb: str = ""                                # ± düğmesinin eski biçimi: "<step_cb>:+" (ör. adj:LEV)
    per_row: int = 4

    def visible(self, cfg) -> bool:
        return self.show is None or bool(self.show(cfg))

    def presets(self, cfg) -> tuple:
        return tuple(self.values(cfg)) if callable(self.values) else tuple(self.values)

    def step_of(self, cfg) -> float:
        return float(self.step(cfg)) if callable(self.step) else float(self.step)


@dataclass(frozen=True)
class Link:
    """Başka bir sayfaya / işleme giden tek düğme."""
    label: Callable[["SettingsMenu"], str]
    data: str
    show: Optional[Callable[["SettingsMenu"], bool]] = None


@dataclass(frozen=True)
class Page:
    title: str
    rows: tuple = ()                                 # Row | Link | (Row / Link, ...) = aynı düğme satırında
    back: Tuple[Button, ...] = (("⬅️ Ayarlar", "cfg"), ("🏠 Menü", "menu"))
    futures: bool = False                            # Vadeli işleme özgü sayfa


def _crash(cfg) -> bool:
    return cfg.futures_tactic == "crash"


def _liq4(cfg) -> bool:
    return cfg.futures_tactic != "crash"


def _by_tactic(crash, liq4):
    return lambda cfg: crash if _crash(cfg) else liq4


def _be_triggers(cfg) -> tuple:
    """Başabaş tetiği seçenekleri (tabandan büyük olanlar; çöküş dönüşünde %5'e kadar)."""
    base = (1, 1.5, 2, 3, 4, 5) if _crash(cfg) else (0.3, 0.5, 0.7, 1.0, 1.5, 2.0)
    return tuple(v for v in base if v > cfg.breakeven_floor_pct)


def _be_floors(cfg) -> tuple:
    """Başabaş stopu (kilitlenen kâr) seçenekleri: tetikten küçük olanlar."""
    return tuple(v for v in (0.1, 0.3, 0.5, 1, 2, 3) if v < cfg.breakeven_trigger_pct)


def _pct(v) -> str:
    return f"%{v:g}"


def _x(v) -> str:
    return f"{v:g}x"


def _mins(v) -> str:
    v = int(v)
    if v and v % 1440 == 0:
        return f"{v // 1440} gün"
    if v >= 60 and v % 60 == 0:
        return f"{v // 60} sa"
    return f"{v} dk"


def _off(text: str, fmt: Callable[[object], str]) -> Callable[[object], str]:
    """0 / boş değer için `text`, diğerleri için `fmt`."""
    return lambda v: text if not v else fmt(v)


def _hours_range(v: str) -> str:
    """'23:00-08:00' → '23-08' (düğmede kısa)."""
    return "-".join(part[:2] if part.endswith(":00") else part for part in v.split("-")) if v else ""


def _rules_label(rules) -> str:
    """((1, 6.0), (12, 15.0)) → '1 sa %6 + 12 sa %15'."""
    return " + ".join(f"{h} sa %{p:g}" for h, p in rules)


def _extra_label(v: str) -> str:
    try:
        rules = parse_crash_rules(v)
    except ValueError:
        return v
    return f"+ {_rules_label(rules)}" if rules else "Ek pencere yok"


def _wave_label(v: str) -> str:
    try:
        rules = parse_crash_rules(v)
    except ValueError:
        return v
    return "Dalga eşikleri: " + " · ".join(f"{h} sa %{p:g}" for h, p in rules) if rules else "Dalga eşikleri: kapalı"


def _pump_label(v: str) -> str:
    try:
        rules = parse_crash_rules(v)
    except ValueError:
        return v
    return "📈 " + " · ".join(f"{h} sa +%{p:g}" for h, p in rules) if rules else "📈 Pompa short kapalı"


def _funding_rule_label(v: str) -> str:
    try:
        rules = parse_crash_rules(v)
    except ValueError:
        return v
    return "💸 " + " · ".join(f"{h} sa -%{p:g}" for h, p in rules) if rules else "💸 Çöküş şartı yok"


def _age_label(v) -> str:
    return f"≥{int(v)} gün" if int(v) < 365 else f"≥{int(v) // 365} yıl"


def _watching(cfg) -> bool:
    return _crash(cfg) and cfg.futures_confirm != "close"


def _thousands(v: float) -> str:
    return f"{v:,.0f}".replace(",", ".")


def _on(flag: bool) -> str:
    return "✅" if flag else "⬜"


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-9
    return a == b


def encode(value, kind: str = "") -> str:
    """Değerin düğme verisindeki yazımı: ':' düğme verisini böldüğü için saatlerde atılır, çöküş pencerelerinde
    'h' olur (1:5 → 1h5), boş = 'off'."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return (value.replace(":", "h") if kind == "rules" else value.replace(":", "")) or "off"
    return format_value(value)


PAGES: Dict[str, Page] = {
    # ------------------------------------------------------------ vadeli işlem
    "fut": Page("⚙️ Dinamik Ayarlar — Vadeli", rows=(
        Row("FUTURES_LEVERAGE", icon="⚡", values=LEVERAGE_CHOICES, fmt=lambda v: f"{v}x", cb="lev",
            step=1, step_cb="adj:LEV", name="⚡", unit="x"),
        Row("FUTURES_MARGIN_USAGE_PCT", icon="💵", values=(25, 50, 75, 95), fmt=_pct),
        (Link(lambda m: "🧭 Giriş sinyali", "pg:fe"), Link(lambda m: "🎯 Çıkış ve emirler", "pg:fx")),
        (Link(lambda m: m.protect_label(), "pg:fb"), Link(lambda m: m.coins_label(), "pg:fc")),
        (Link(lambda m: "☠️ Kill-switch", "pg:ks"), Link(lambda m: "🧯 Risk sigortaları", "risk")),
        (Link(lambda m: "🔔 Bildirim ve raporlar", "ntf"), Link(lambda m: "⭐ Önerilen ayarlar", "frec")),
    ), back=(("⬅️ Menü", "menu"),), futures=True),
    "fe": Page("🧭 Vadeli Giriş Sinyali", rows=(
        Row("FUTURES_TACTIC", values=FUTURES_TACTICS, fmt=TACTIC_LABELS.get, cb="ftac", per_row=2),
        Row("FUTURES_CRASH_DROP_PCT", icon="📉", values=(6, 8, 10, 12, 15), fmt=_pct, show=_crash, per_row=5),
        Row("FUTURES_CRASH_HOURS", icon="🕓", values=(1, 2, 4, 8, 12, 24), fmt=lambda v: f"{v} sa", show=_crash,
            per_row=3),
        Row("FUTURES_CRASH_EXTRA", icon="➕", values=CRASH_EXTRA_CHOICES, fmt=_extra_label, show=_crash, per_row=2),
        Row("FUTURES_CRASH_Z", icon="📐", values=(0, 2.5, 3, 4), fmt=_off("Sabit %", lambda v: f"{v:g}σ"),
            show=_crash),
        Row("FUTURES_CRASH_Z_MIN_PCT", icon="🔻", values=(2, 3, 5, 8), fmt=lambda v: f"≥%{v:g}",
            show=lambda cfg: _crash(cfg) and cfg.futures_crash_z > 0),
        Row("FUTURES_CONFIRM", values=FUTURES_CONFIRMS, fmt=CONFIRM_LABELS.get, show=_crash, per_row=2),
        Row("FUTURES_CONFIRM_BARS", icon="⏳", values=(3, 6, 12, 24), fmt=lambda v: _mins(int(v) * 5),
            show=_watching),
        Row("FUTURES_CONFIRM_VOLUME_RATIO", icon="📊", values=(2, 3, 4, 5), fmt=_x,
            show=lambda cfg: _watching(cfg) and cfg.futures_confirm in ("volume", "either")),
        Row("FUTURES_CRASH_LIQ", name="💥 Likidasyon dalgası şartı", show=_crash),
        Link(lambda m: m.modules_label(), "pg:fm", show=lambda m: _crash(m.cfg)),
        Link(lambda m: m.cascade_label(), "pg:fw", show=lambda m: _crash(m.cfg)),
        Link(lambda m: m.grid_label(1), "fgp:1", show=lambda m: _crash(m.cfg) and len(FUTURES_GRID_PRESETS) >= 1),
        Link(lambda m: m.grid_label(2), "fgp:2", show=lambda m: _crash(m.cfg) and len(FUTURES_GRID_PRESETS) >= 2),
        Row("FUTURES_SIDES", values=FUTURES_SIDES, fmt=SIDE_LABELS.get, show=_liq4, per_row=3),
        Row("FUTURES_VOLUME_RATIO", icon="📊", values=(1.5, 2, 2.5, 3), fmt=_x, show=_liq4),
        Row("FUTURES_LIQ_RATIO", icon="💥", values=(2, 3, 4, 5), fmt=_x, show=_liq4),
        Row("FUTURES_LIQ_SOURCE", values=FUTURES_LIQ_SOURCES, fmt=SOURCE_LABELS.get, show=_liq4, per_row=3),
        Row("FUTURES_LIQ_MIN_USDT", icon="💵", values=(50_000, 100_000, 250_000, 500_000),
            fmt=lambda v: f"{v / 1000:g}K", show=lambda cfg: _liq4(cfg) and cfg.futures_liq_source != "proxy"),
        Row("FUTURES_BTC_TREND", name="₿ BTC 1 saatlik trend filtresi", show=_liq4),
    ), back=(("⬅️ Vadeli ayarlar", "cfg"), ("🏠 Menü", "menu")), futures=True),
    "fm": Page("🧩 Ek Giriş Modülleri", rows=(
        Row("FUTURES_PUMP_RULES", values=PUMP_CHOICES, fmt=_pump_label, show=_crash, per_row=1),
        Row("FUTURES_BB", values=FUTURES_BB_MODES, fmt=BB_LABELS.get, show=_crash, per_row=2),
        Row("FUTURES_BB_RSI_LOW", icon="🔻", values=(20, 25, 30), fmt=lambda v: f"RSI < {v:g}",
            show=lambda cfg: _crash(cfg) and cfg.futures_bb in ("both", "long"), per_row=3),
        Row("FUTURES_BB_RSI_HIGH", icon="🔺", values=(70, 75, 80), fmt=lambda v: f"RSI > {v:g}",
            show=lambda cfg: _crash(cfg) and cfg.futures_bb in ("both", "short"), per_row=3),
        Row("FUTURES_BB_STD", icon="↔️", values=(2, 2.5, 3), fmt=lambda v: f"{v:g}σ bant",
            show=lambda cfg: _crash(cfg) and cfg.futures_bb != "off", per_row=3),
        Row("FUTURES_BREAKOUT", name="🚀 Hacimli kırılım (long)", show=_crash),
        Row("FUTURES_BREAKOUT_VOLUME_RATIO", icon="📊", values=(3, 4, 5), fmt=_x,
            show=lambda cfg: _crash(cfg) and cfg.futures_breakout, per_row=3),
        Row("FUTURES_BREAKOUT_HOURS", icon="🕓", values=(4, 12, 24), fmt=lambda v: f"{v} sa zirve",
            show=lambda cfg: _crash(cfg) and cfg.futures_breakout, per_row=3),
        Row("FUTURES_FUNDING", name="💸 Fonlama sıkışması (long)", show=_crash),
        Row("FUTURES_FUNDING_RATE_PCT", icon="💸", values=(-0.05, -0.10, -0.20), fmt=lambda v: f"fonlama ≤ %{v:g}",
            show=lambda cfg: _crash(cfg) and cfg.futures_funding, per_row=3),
        Row("FUTURES_FUNDING_RULES", values=FUNDING_RULE_CHOICES, fmt=_funding_rule_label,
            show=lambda cfg: _crash(cfg) and cfg.futures_funding, per_row=3),
        Row("FUTURES_PULLBACK", name="📐 Trend içi düzeltme (long)", show=_crash),
        Row("FUTURES_PULLBACK_RSI", icon="🔻", values=(15, 20, 25, 30), fmt=lambda v: f"RSI < {v:g}",
            show=lambda cfg: _crash(cfg) and cfg.futures_pullback, per_row=4),
        Row("FUTURES_PULLBACK_EMA", icon="📈", values=(100, 200), fmt=lambda v: f"1 sa EMA{v}",
            show=lambda cfg: _crash(cfg) and cfg.futures_pullback, per_row=2),
    ), back=(("🧭 Giriş sinyali", "pg:fe"), ("⬅️ Vadeli ayarlar", "cfg"), ("🏠 Menü", "menu")), futures=True),
    "fw": Page("🌊 Dalga Modu", rows=(
        Link(lambda m: m.cascade_preset_label(), "fcm:on", show=lambda m: _crash(m.cfg)),
        Link(lambda m: m.phase6_preset_label("p6"), "fcm:p6", show=lambda m: _crash(m.cfg)),
        Link(lambda m: m.phase6_preset_label("p6s"), "fcm:p6s", show=lambda m: _crash(m.cfg)),
        Row("FUTURES_MAX_POSITIONS", values=(1, 2, 3, 4), fmt=lambda v: "1 pozisyon (kapalı)" if int(v) == 1 else
            f"en fazla {v} pozisyon", show=_crash, per_row=4),
        Row("FUTURES_EXTRA_BREADTH", icon="🌊", values=(5, 8, 10, 12), fmt=lambda v: f"ek: ≥{v} coin",
            show=lambda cfg: _crash(cfg) and cfg.futures_max_positions > 1, per_row=4),
        Row("FUTURES_LEVERAGE", icon="⚡", values=(2, 3, 4), fmt=lambda v: f"{v}x",
            show=lambda cfg: _crash(cfg) and cfg.futures_max_positions > 1, per_row=3),
        Row("FUTURES_EXTRA_SIZE_PCT", icon="📏", values=(100, 50, 25, 16.7), fmt=lambda v: "ek: eşit" if float(v) >= 100
            else f"ek: %{float(v):g}", show=lambda cfg: _crash(cfg) and cfg.futures_max_positions > 1, per_row=4),
        Row("FUTURES_ISOLATED_SIZE_PCT", values=(100, 50, 25), fmt=lambda v: "Yalnız çöküş: tam" if float(v) >= 100
            else f"Yalnız çöküş: %{float(v):g}", show=_crash, per_row=3),
        Row("FUTURES_ISOLATED_BREADTH", icon="🔎", values=(2, 3), fmt=lambda v: f"yalnız: <{v} coin",
            show=lambda cfg: _crash(cfg) and cfg.futures_isolated_size_pct < 100, per_row=2),
        Row("FUTURES_REENTRY_BREADTH", icon="🔁", values=(0, 3, 8), fmt=lambda v: "yeniden giriş: kapalı" if int(v) == 0
            else f"yeniden giriş: ≥{v} coin", show=_crash, per_row=3),
        Row("FUTURES_WAVE_RULES", values=WAVE_RULE_CHOICES, fmt=lambda v: _wave_label(v), show=_crash, per_row=2),
        Row("FUTURES_WAVE_BREADTH", icon="🌊", values=(8, 12, 15), fmt=lambda v: f"eşikler: ≥{v} coin",
            show=lambda cfg: _crash(cfg) and bool(futures_wave_rules(cfg)), per_row=3),
        Row("FUTURES_EXTRA_BE_TRIGGER_PCT", icon="🔒", values=EXTRA_BE_CHOICES,
            fmt=lambda v: "ek başabaş: ana ayar" if float(v) <= 0 else f"ek başabaş: +%{float(v):g}",
            show=lambda cfg: _crash(cfg) and cfg.futures_max_positions > 1 and cfg.breakeven_enabled, per_row=2),
        Row("FUTURES_EXTRA_HOLD_MINUTES", icon="⏱️", values=EXTRA_HOLD_CHOICES,
            fmt=lambda v: "ek zaman stopu: ana ayar" if int(v) <= 0 else f"ek zaman stopu: {int(v)} dk",
            show=lambda cfg: _crash(cfg) and cfg.futures_max_positions > 1, per_row=3),
        Row("FUTURES_WAVE_HOLD_MINUTES", icon="🌊", values=WAVE_HOLD_CHOICES,
            fmt=lambda v: "dalgada ilk: ana ayar" if int(v) <= 0 else f"dalgada ilk: {int(v)} dk",
            show=_crash, per_row=3),
        Link(lambda m: "⛔ Dalga modunu kapat (tek pozisyon, 2x)" + (" ve Faz 6'yı" if m.phase6_on() else ""),
             "fcm:off", show=lambda m: (m.cfg.futures_max_positions > 1 or m.cfg.futures_isolated_size_pct < 100
                                        or m.phase6_on())),
    ), back=(("🧭 Giriş sinyali", "pg:fe"), ("⬅️ Vadeli ayarlar", "cfg"), ("🏠 Menü", "menu")), futures=True),
    "fx": Page("🎯 Vadeli Çıkışlar ve Emirler", rows=(
        Row("TAKE_PROFIT_PCT", icon="🎯", values=_by_tactic((3, 4, 6, 8), (1.0, 1.2, 1.5, 2.0)), fmt=_pct),
        Row("STOP_LOSS_PCT", icon="🛑", values=_by_tactic((3, 4, 5), (0.8, 0.9, 1.0)), fmt=_pct),
        (Row("TAKE_PROFIT_PCT", step=_by_tactic(0.5, 0.1), name="🎯"),
         Row("STOP_LOSS_PCT", step=_by_tactic(0.5, 0.1), name="🛑")),
        Row("MAX_HOLD_MINUTES", icon="⏱️", values=(15, 30, 60, 120, 240), fmt=_mins, per_row=5),
        Link(lambda m: m.protect_label(), "pg:fb"),
        Row("FUTURES_EXCHANGE_STOP", name="🛡️ Stop güvencesi (stop konamazsa pozisyonu kapat)"),
        Row("FUTURES_TP_ORDER", values=FUTURES_TP_ORDERS, fmt=TP_ORDER_LABELS.get, per_row=2),
        Row("FUTURES_ENTRY_ORDER", values=FUTURES_ENTRY_ORDERS, fmt=ENTRY_ORDER_LABELS.get, per_row=2),
        Row("FUTURES_LIMIT_OFFSET_PCT", icon="📌", values=(0, 0.02, 0.05, 0.1), fmt=lambda v: f"bid -%{v:g}",
            show=lambda cfg: cfg.futures_entry_order == "limit", per_row=4),
        Row("FUTURES_LIMIT_TTL_MIN", icon="⏳", values=(1, 3, 5), fmt=lambda v: f"{v} dk bekle",
            show=lambda cfg: cfg.futures_entry_order == "limit", per_row=3),
        Row("FUTURES_FAST_ENTRY", name="⚡ Hızlı giriş (mum kapanışından 1 sn sonra tara)"),
        Row("FUTURES_MINUTE_SCAN", name="⏱️ Her dakika tara (5 dk kapanışını bekleme)"),
        Row("FUTURES_SNAPSHOT_SCAN", name="🚀 Anlık tarama (mumun kesinleşmesini bekleme)"),
    ), back=(("⬅️ Vadeli ayarlar", "cfg"), ("🏠 Menü", "menu")), futures=True),
    "fb": Page("🔒 Başabaş ve İzleyen Stop", rows=(
        Row("BREAKEVEN_ENABLED", name="🔒 Başabaş kalkanı"),
        Row("BREAKEVEN_TRIGGER_PCT", icon="🔒", values=_be_triggers, fmt=lambda v: f"+%{v:g}", per_row=3,
            show=lambda cfg: cfg.breakeven_enabled),
        Row("BREAKEVEN_FLOOR_PCT", icon="🧷", values=_be_floors, fmt=lambda v: f"+%{v:g}", per_row=3,
            show=lambda cfg: cfg.breakeven_enabled),
        (Row("BREAKEVEN_TRIGGER_PCT", step=0.5, name="🔒", show=lambda cfg: cfg.breakeven_enabled),
         Row("BREAKEVEN_FLOOR_PCT", step=0.1, name="🧷", lo=0, show=lambda cfg: cfg.breakeven_enabled)),
        Row("FUTURES_TRAILING", name="📈 İzleyen stop"),
        Row("FUTURES_TRAILING_ACT_PCT", icon="📈", values=(1, 2, 3, 4, 5, 6), fmt=lambda v: f"+%{v:g}", per_row=3,
            show=lambda cfg: cfg.futures_trailing),
        Row("FUTURES_TRAILING_GAP_PCT", icon="↩️", values=(0.5, 1, 1.5, 2, 3), fmt=_pct, per_row=5,
            show=lambda cfg: cfg.futures_trailing),
        (Row("FUTURES_TRAILING_ACT_PCT", step=0.5, name="📈", lo=0.5, show=lambda cfg: cfg.futures_trailing),
         Row("FUTURES_TRAILING_GAP_PCT", step=0.25, name="↩️", lo=0.25, show=lambda cfg: cfg.futures_trailing)),
    ), back=(("🎯 Çıkışlar", "pg:fx"), ("⬅️ Vadeli ayarlar", "cfg"), ("🏠 Menü", "menu")), futures=True),
    "fc": Page("🪙 Vadeli Coinler", rows=(
        Row("FUTURES_UNIVERSE", values=UNIVERSE_CHOICES, fmt=lambda v: f"🔝 {v}" if v else "📋 Liste", per_row=4),
        Row("FUTURES_UNIVERSE_MODE", values=FUTURES_UNIVERSE_MODES, fmt=UNIVERSE_MODE_LABELS.get,
            show=lambda cfg: cfg.futures_universe > 0, per_row=2),
        Row("FUTURES_UNIVERSE_MIN_AGE_DAYS", icon="🎂", values=(30, 365, 730, 1095, 1460),
            fmt=_age_label, show=lambda cfg: cfg.futures_universe > 0),
        Row("FUTURES_UNIVERSE_WITH_LIST", name="📋 Listemi de tara (aşağıdaki coinler)",
            show=lambda cfg: cfg.futures_universe > 0),
    ), back=(("⬅️ Vadeli ayarlar", "cfg"), ("🏠 Menü", "menu")), futures=True),
    # ------------------------------------------------------------ kill-switch ve risk sigortaları
    "ks": Page("☠️ Kill-switch", rows=(
        Row("MAX_TOTAL_LOSS_PCT", icon="☠️", values=(10, 20, 30, 40, 50, 60), fmt=_pct, per_row=3),
        Row("MAX_TOTAL_LOSS_PCT", step=5, name="☠️", unit=" puan", lo=5, hi=95),
    ), back=(("🧯 Risk", "risk"), ("⬅️ Ayarlar", "cfg"), ("🏠 Menü", "menu"))),
    "risk": Page("🧯 Risk Sigortaları", rows=(
        Link(lambda m: m.kill_label(), "pg:ks"),
        Row("DAILY_LOSS_LIMIT_PCT", values=(0, 2, 3, 5), cb="dl", fmt=_off("Limit yok", lambda v: f"Gün -%{v:g}")),
        Row("DAILY_PROFIT_TARGET_PCT", icon="🎯", values=(0, 3, 5, 10), fmt=_off("Hedef yok", lambda v: f"+%{v:g}")),
        Row("LOSS_STREAK_LIMIT", values=(0, 2, 3, 4), cb="ls", fmt=_off("Seri yok", lambda v: f"{v} SL")),
        Row("LOSS_STREAK_PAUSE_HOURS", icon="⏸️", values=(2, 4, 8, 24), fmt=lambda v: f"{v:g} sa",
            show=lambda cfg: cfg.loss_streak_limit > 0),
        Row("SYMBOL_COOLDOWN_MINUTES", icon="⏳", values=(0, 15, 30, 60, 120), fmt=_off("Yok", _mins), per_row=5),
        Row("LOSS_COOLDOWN_MINUTES", values=(0, 240, 720, 1440), cb="lcd",
            fmt=_off("Zarar sonrası: normal", lambda v: f"{int(v) // 60} sa")),
        Row("COIN_SL_LIMIT", icon="📒", values=(0, 2, 3, 5), fmt=_off("Karne yok", lambda v: f"{v} SL")),
        Row("SLIPPAGE_ALERT_PCT", icon="💧", values=(0, 0.2, 0.3, 0.5), fmt=_off("Alarm yok", _pct),
            show=lambda cfg: not is_futures(cfg.strategy)),           # Vadeli motor alış kaymasını izlemez
        Link(lambda m: "▶️ Dondurmayı kaldır", "unfreeze", show=lambda m: bool(m.bot.freeze_note())),
    ), back=(("⬅️ Menü", "menu"),)),
    # ------------------------------------------------------------ bildirim ve raporlar
    "ntf": Page("🔔 Bildirim Ayarı", rows=(
        Row("NOTIFY_MODE", values=tuple(NOTIFY_LABELS), fmt=NOTIFY_LABELS.get, cb="ntfset", per_row=1),
        Row("STATUS_REPORT_MINUTES", icon="📊", values=(0, 60, 240, 720), fmt=_off("Rapor yok", _mins)),
        Row("DAILY_REPORT_TIME", icon="📅", values=("", "09:00", "21:00", "23:55"), fmt=lambda v: v or "Özet yok"),
        Row("QUIET_HOURS", icon="🌙", values=("", "23:00-08:00", "00:00-07:00", "22:00-09:00"),
            fmt=lambda v: _hours_range(v) or "Sessiz yok"),
        Row("WEEKLY_REPORT", name="🗓️ Haftalık özet (pazar)"),
    ), back=(("⬅️ Menü", "menu"),)),
    # ------------------------------------------------------------ spot çıkış ayrıntıları
    "sx": Page("🔧 Çıkış Ayrıntıları", rows=(
        Row("TRAILING_FLOOR_PCT", step=0.5, name="📈 Taban", show=lambda cfg: not is_rotation(cfg.strategy)),
        Row("MAX_HOLD_MINUTES", icon="⏱️", values=(0, 240, 1440, 7200), fmt=_off("Sınırsız", _mins)),
        Row("MAX_HOLD_MINUTES", step=60, name="⏱️", unit=" dk", lo=0),
        Row("BREAKEVEN_ENABLED", name="🔒 Başabaş kalkanı"),
        Row("BREAKEVEN_TRIGGER_PCT", step=0.5, name="🔒 Tetik", lo=0.1, show=lambda cfg: cfg.breakeven_enabled),
        Row("BREAKEVEN_FLOOR_PCT", step=0.1, name="🧷 Taban", lo=0, show=lambda cfg: cfg.breakeven_enabled),
        Row("BACKUP_STOP_PCT", step=1, name="🧱 Yedek stop", lo=1, hi=95, show=lambda cfg: cfg.backup_stop_enabled),
    )),
}


def page_rows(page: Page) -> List[Row]:
    """Sayfadaki tüm ayar satırları (aynı düğme satırında birleştirilenler dahil)."""
    out: List[Row] = []
    for item in page.rows:
        for part in (item if isinstance(item, tuple) else (item,)):
            if isinstance(part, Row):
                out.append(part)
    return out


# ================================================================ sayfalar
class SettingsMenu:
    def __init__(self, panel: "ControlPanel"):
        self.panel = panel

    @property
    def proc(self):
        return self.panel.proc

    @property
    def bot(self):
        return self.panel.bot

    @property
    def cfg(self):
        return self.panel.bot.cfg

    def _view(self, pid: str):
        return lambda mid, note="": self.show(pid, mid, note)

    def _invalid(self) -> None:
        self.proc.reply("❓ Bu düğme artık geçerli değil. Güncel menü için /menu")

    # ------------------------------------------------------------ gösterme
    def show(self, pid: str, mid: Optional[int] = None, note: str = "") -> None:
        page = PAGES.get(pid)
        if page is None:
            self._invalid()
            return
        text = getattr(self, f"_text_{pid}")()
        if page.futures and not is_futures(self.cfg.strategy):
            text += ("\n\nℹ️ Şu an vadeli strateji seçili değil: bu ayarlar 🧭 Strateji → Vadeli seçilince geçerli "
                     "olur.")
        if note:
            text += f"\n\n{note}"
        cfg = self.cfg
        grid = pid == "fc" and (cfg.futures_universe <= 0 or cfg.futures_universe_with_list)
        keyboard = (self._coin_buttons() if grid else []) + self.keyboard(pid)
        self.proc.reply_menu(text, keyboard, mid)

    def keyboard(self, pid: str) -> List[List[Button]]:
        page, cfg = PAGES[pid], self.cfg
        out: List[List[Button]] = []
        for item in page.rows:
            if isinstance(item, tuple):                    # Aynı düğme satırında birleşenler
                merged = [b for part in item for line in self._render(pid, part, cfg) for b in line]
                if merged:
                    out.append(merged)
            else:
                out.extend(self._render(pid, item, cfg))
        out.append(list(page.back))
        return out

    def _render(self, pid: str, item, cfg) -> List[List[Button]]:
        if isinstance(item, Link):
            return [[(item.label(self), item.data)]] if item.show is None or item.show(self) else []
        if not item.visible(cfg):
            return []
        setting = EDITABLE[item.key]
        current = getattr(cfg, setting.attr)
        lines: List[List[Button]] = []
        presets = item.presets(cfg)
        if presets:
            row: List[Button] = []
            for i, value in enumerate(presets):
                label = item.fmt(value) if item.fmt else format_value(value)
                if i == 0 and item.icon:
                    label = f"{item.icon} {label}"
                if _same(value, current):
                    label = f"✅ {label}"
                code = encode(value, setting.kind)
                data = f"{item.cb}:{code}" if item.cb else f"kv:{pid}:{item.key}:{code}"
                row.append((label, data))
            lines += [row[i:i + item.per_row] for i in range(0, len(row), item.per_row)]
        step = item.step_of(cfg)
        if step > 0:
            prefix = item.step_cb or f"kv:{pid}:{item.key}"
            text = f"{step:g}{item.unit}"
            lines.append([(f"{item.name} −{text}", f"{prefix}:-"), (f"{item.name} +{text}", f"{prefix}:+")])
        if not presets and step <= 0:
            if setting.kind == "bool":
                lines.append([(f"{_on(bool(current))} {item.name}", f"kv:{pid}:{item.key}:t")])
            elif setting.kind == "choice":
                shown = item.fmt(current) if item.fmt else current
                lines.append([(f"{item.name}: {shown}" if item.name else str(shown), f"kv:{pid}:{item.key}:n")])
        return lines

    # ------------------------------------------------------------ düğmeler
    def set(self, pid: str, key: str, raw: str, mid: Optional[int]) -> None:
        """kv:<sayfa>:<AYAR>:<değer | + | - | t | n> düğmesi."""
        page = PAGES.get(pid)
        rows = [r for r in page_rows(page) if r.key == key] if page else []
        setting = EDITABLE.get(key)
        if not rows or setting is None:
            self._invalid()
            return
        cfg = self.cfg
        current = getattr(cfg, setting.attr)
        if raw in ("+", "-"):
            steps = [r for r in rows if r.step_of(cfg) > 0]
            if not steps or setting.kind not in ("int", "float"):
                self._invalid()
                return
            row = next((r for r in steps if r.visible(cfg)), steps[0])
            step = row.step_of(cfg)
            value = current + step if raw == "+" else current - step
            if row.lo is not None:
                value = max(row.lo, value)
            if row.hi is not None:
                value = min(row.hi, value)
            value = int(round(value)) if setting.kind == "int" else round(value, 4)
            if _same(value, current):
                self.show(pid, mid, f"ℹ️ {esc(setting.label)} zaten sınırda ({esc(format_value(current))}).")
                return
        elif raw == "t" and setting.kind == "bool":
            value = not current
        elif raw == "n" and setting.kind == "choice":
            choices = setting.choices
            value = choices[(choices.index(current) + 1) % len(choices)] if current in choices else choices[0]
        else:
            value = raw                                    # Türüne çevirip doğrulamayı apply_settings yapar
        self.panel.request_change({key: value}, self._view(pid), mid, origin=pid)

    def toggle_coin(self, coin: str, mid: Optional[int]) -> None:
        """fc:<COIN>: coini vadeli listeye ekler / çıkarır; fc:default varsayılan 10 coine döner."""
        coin = coin.strip().upper()
        coins = list(futures_coin_list(self.cfg))
        if coin == "DEFAULT":
            value = DEFAULT_FUTURES_COINS
        elif not coin.isalnum():
            self._invalid()
            return
        elif coin in coins:
            if len(coins) == 1:
                self.show("fc", mid, "⚠️ En az bir coin seçili kalmalı.")
                return
            coins.remove(coin)
            value = ",".join(coins)
        else:
            value = ",".join(coins + [coin])
        self.panel.request_change({"FUTURES_COINS": value}, self._view("fc"), mid, origin="fc")

    def ask_tactic(self, tactic: str, mid: Optional[int]) -> None:
        """ftac:<taktik>: taktik + o taktiğin önerilen çıkışları (onaydan sonra birlikte yazılır)."""
        cfg = self.cfg
        if tactic not in FUTURES_TACTICS:
            self._invalid()
            return
        if tactic == cfg.futures_tactic:
            self.show("fe", mid, f"ℹ️ {TACTIC_LABELS[tactic]} zaten seçili.")
            return
        values: Dict[str, object] = {"FUTURES_TACTIC": tactic}
        values.update({k: format_value(v) for k, v in FUTURES_TACTIC_EXITS[tactic].items()})
        changed = [f"{k}={v}" for k, v in values.items()
                   if k != "FUTURES_TACTIC" and v != format_value(getattr(cfg, EDITABLE[k].attr))]
        detail = f"\n🧭 {TACTIC_LABELS[cfg.futures_tactic]} → <b>{TACTIC_LABELS[tactic]}</b>"
        if changed:
            detail += f"\n⚙️ Bu taktiğin çıkış ayarları da yüklenir: {esc(', '.join(changed))}"
        if tactic == "liq4":
            detail += "\n⚠️ 4'lü onay 2020-2026 testinde 7 yılın 7'sinde zarar etti; önce test modunda deneyin."
        if self.bot.state.positions:
            detail += "\n📦 Açık pozisyon kendi Kâr Al / Zarar Kes emirleriyle devam eder."
        self.panel.request_change(values, self._view("fe"), mid, origin="fe", ask=detail,
                                  question="Vadeli taktik değiştirilsin mi?")

    def modules_label(self) -> str:
        mods = futures_modules(self.cfg)
        return "🧩 Ek modüller: " + (", ".join(MODULE_LABELS[m] for m in mods) if mods else "kapalı")

    def cascade_label(self) -> str:
        cfg = self.cfg
        p6 = " + Faz 6" if self.phase6_on() else ""
        if cfg.futures_max_positions > 1:
            return (f"🌊 Dalga modu: {cfg.futures_max_positions} pozisyon, ek ≥{cfg.futures_extra_breadth} coin"
                    + (f", yalnız çöküş %{cfg.futures_isolated_size_pct:g}" if cfg.futures_isolated_size_pct < 100 else "")
                    + p6)
        if cfg.futures_isolated_size_pct < 100:
            return f"🌊 Dalga modu: kapalı (yalnız çöküş %{cfg.futures_isolated_size_pct:g}){p6}"
        return "🌊 Dalga modu: kapalı" + p6

    def _matches(self, values: Dict[str, object]) -> bool:
        return all(format_value(getattr(self.cfg, EDITABLE[k].attr)) == format_value(v) for k, v in values.items())

    def phase6_on(self) -> bool:
        """Faz 6 eklerinden biri açık mı (dalgada yeniden giriş, dalga eşikleri, küçük ek pozisyonlar)."""
        cfg = self.cfg
        return (cfg.futures_reentry_breadth > 0 or bool(futures_wave_rules(cfg))
                or (cfg.futures_max_positions > 1 and cfg.futures_extra_size_pct < 100))

    def cascade_preset_label(self) -> str:
        on = self._matches({**FUTURES_CASCADE_PRESET, **FUTURES_PHASE6_OFF, **FUTURES_PHASE8_OFF, **FUTURES_PHASE9_OFF})
        return f"{'✅ ' if on else ''}⭐ Faz 5 seti: 2 pozisyon, 3x, ek ≥8 coin, yalnız çöküş %50"

    def phase6_preset_label(self, key: str) -> str:
        on = self._matches({**PHASE6_PRESETS[key], **FUTURES_PHASE8_OFF, **FUTURES_PHASE9_OFF})
        name = ("Faz 6 seti: 4 pozisyon (ilk tam + 3 küçük), 3x, dalgada yeniden giriş, dalga eşikleri" if key == "p6"
                else "Faz 6 tek pozisyon: dalgada yeniden giriş, dalga eşikleri, yalnız çöküş %50")
        return f"{'✅ ' if on else ''}⭐ {name}"

    def ask_cascade(self, arg: str, mid: Optional[int]) -> None:
        """fcm:on → dalga modunun Faz 5 seti; fcm:p6 / fcm:p6s → Faz 6 seti / Faz 6 tek pozisyon; fcm:off → tek
        pozisyon, tam büyüklük, 2x (Faz 6 ekleri de kapanır). Onaydan sonra birlikte yazılır."""
        if arg not in ("on", "off", "p6", "p6s"):
            self._invalid()
            return
        if arg in PHASE6_PRESETS:
            self.ask_phase6(arg, mid)
            return
        cfg = self.cfg
        preset = FUTURES_CASCADE_PRESET if arg == "on" else CASCADE_OFF
        values = {k: format_value(v) for k, v in {**preset, **FUTURES_PHASE6_OFF, **FUTURES_PHASE8_OFF,
                                                  **FUTURES_PHASE9_OFF}.items()}
        changed = [f"{k}={v}" for k, v in values.items() if v != format_value(getattr(cfg, EDITABLE[k].attr))]
        if not changed:
            self.show("fw", mid, "✅ Zaten bu ayarlar kullanılıyor.")
            return
        if arg == "on":
            detail = ("\n🌊 <b>Dalga modu (Faz 5 seti)</b>\nAynı anda en fazla 2 pozisyon; ikincisi yalnızca son 1 saatte "
                      "8+ coin %3+ düşerken açılır. Her pozisyon bakiyenin %47.5'i teminat × 3x; başka coin düşmezken "
                      "açılan (yalnız çöküş) pozisyonlar yarım büyüklükte. Test: yılda ~450 işlem (343 yerine), işlem "
                      "başı +%0.34, yıllık getiri iki dönemde de daha yüksek; en büyük düşüş %71 / %42 (%52 / %39 yerine)."
                      "\n⚠️ Zarar kes emirleri büyük fitillerde kötü dolarsa (kötümser senaryo) Mayıs 2021 çöküşünde "
                      "hesap eridi; tek pozisyonlu #2 erimedi."
                      f"\n⚙️ Değişecekler: {esc(', '.join(changed))}\n🧪 Önce test modunda (paper) izleyin.")
            question = "Dalga modu açılsın mı?"
        else:
            detail = f"\n⚙️ Değişecekler: {esc(', '.join(changed))}"
            question = "Dalga modu kapatılsın mı (tek pozisyon, tam büyüklük, 2x)?"
        if self.bot.state.positions:
            detail += "\n📦 Açık pozisyonlar kendi emirleriyle devam eder."
        self.panel.request_change(values, self._view("fw"), mid, origin="fw", ask=detail, question=question)

    def ask_phase6(self, arg: str, mid: Optional[int]) -> None:
        """fcm:p6 → Faz 6 seti (4 pozisyon); fcm:p6s → Faz 6 tek pozisyon. Faz 5 seti değişmez."""
        cfg = self.cfg
        values = {k: format_value(v) for k, v in {**PHASE6_PRESETS[arg], **FUTURES_PHASE8_OFF,
                                                  **FUTURES_PHASE9_OFF}.items()}
        changed = [f"{k}={v}" for k, v in values.items() if v != format_value(getattr(cfg, EDITABLE[k].attr))]
        if not changed:
            self.show("fw", mid, "✅ Zaten bu ayarlar kullanılıyor.")
            return
        if arg == "p6":
            detail = ("\n⭐ <b>Faz 6 seti</b>\nAynı anda en fazla 4 pozisyon: ilk pozisyon #2 ile aynı büyüklükte "
                      "(bakiyenin %63.3'ü teminat × 3x = bakiyenin 1.9 katı); son 1 saatte 8+ coin %3+ düşerken açılan "
                      "3 ek pozisyonun her biri bunun 1/6'sı (toplam en fazla bakiyenin 2.85 katı, Faz 5 kadar). Dalgada "
                      "(3+ coin) zarar sonrası 240 dk bekleme atlanır; 12+ coin düşerken 1 saatte %5 / 4 saatte %8 düşüş "
                      "de sinyal; yalnız çöküşte pozisyon yarım. Test (2020-2026, 1 dk, 30 USDT): yılda ~800 işlem "
                      "(#2: 343), işlem başı +%0.35, yıllık getiri iki dönemde de #2'den yüksek, en büyük düşüş #2 "
                      "kadar (%56 / %40; #2 %52 / %39).")
            question = "Faz 6 seti açılsın mı?"
        else:
            detail = ("\n⭐ <b>Faz 6 tek pozisyon</b>\nEskisi gibi tek pozisyon (2x); dalgada (3+ coin) zarar sonrası "
                      "240 dk bekleme atlanır, 12+ coin düşerken 1 saatte %5 / 4 saatte %8 düşüş de sinyal, yalnız "
                      "çöküşte pozisyon yarım. Test: yılda ~400 işlem (#2: 343), yıllık getiri iki dönemde de #2'den "
                      "yüksek, en büyük düşüş #2 kadar.")
            question = "Faz 6 tek pozisyon açılsın mı?"
        detail += ("\n🥈 #2'nin giriş / çıkış ayarlarıyla test edildi (onlar değişmez). ⚠️ Kötümser stop dolumu ve 3 kat "
                   "kayma birlikte olursa her ayar zarar eder."
                   f"\n⚙️ Değişecekler: {esc(', '.join(changed))}\n🧪 Önce test modunda (paper) izleyin.")
        if self.bot.state.positions:
            detail += "\n📦 Açık pozisyonlar kendi emirleriyle devam eder."
        self.panel.request_change(values, self._view("fw"), mid, origin="fw", ask=detail, question=question)

    def grid_label(self, n: int) -> str:
        preset = FUTURES_GRID_PRESETS[n - 1]
        on = all(format_value(getattr(self.cfg, EDITABLE[k].attr)) == format_value(v)
                 for k, v in preset["values"].items())
        return f"{'✅ ' if on else ''}{preset['icon']} Izgara #{n}: {preset['name']}"

    def ask_grid_preset(self, arg: str, mid: Optional[int]) -> None:
        """fgp:<n>: ızgara testinin n. en iyi yapılandırması (giriş kuralı + çıkışlar + testteki evren / beklemeler)."""
        try:
            preset = FUTURES_GRID_PRESETS[int(arg) - 1]
        except (ValueError, IndexError):
            self._invalid()
            return
        cfg = self.cfg
        values = {k: format_value(v) for k, v in preset["values"].items()}
        changed = [f"{k}={v or 'kapalı'}" for k, v in values.items()
                   if v != format_value(getattr(cfg, EDITABLE[k].attr))]
        if not changed:
            self.show("fe", mid, f"✅ Izgara #{arg} zaten kullanılıyor.")
            return
        detail = (f"\n{preset['icon']} <b>Izgara #{arg}: {esc(preset['name'])}</b>\n{esc(preset['detail'])}"
                  f"\n⚙️ Değişecekler: {esc(', '.join(changed))}"
                  "\n🧪 Önce test modunda (paper) izleyin.")
        if self.bot.state.positions:
            detail += "\n📦 Açık pozisyon kendi emirleriyle devam eder."
        self.panel.request_change(values, self._view("fe"), mid, origin="fe", ask=detail,
                                  question=f"Izgara #{arg} ayarları yüklensin mi?")

    def ask_recommended(self, mid: Optional[int]) -> None:
        """⭐ Önerilen vadeli ayarlar: araştırmada iki dönemde de en iyi sonucu veren set (onaydan sonra yazılır)."""
        cfg = self.cfg
        values = {k: format_value(v) for k, v in FUTURES_RECOMMENDED.items()}
        changed = [f"{k}={v}" for k, v in values.items() if v != format_value(getattr(cfg, EDITABLE[k].attr))]
        if not changed:
            self.show("fut", mid, "✅ Zaten önerilen ayarlar kullanılıyor.")
            return
        detail = ("\n⭐ Kasım 2026 işlem sayısı araştırmasında (2020-2026, seçim / doğrulama dönemi) en iyi sonucu veren "
                  "set: liste + en hacimli 20 köklü coin, kârla kapanan coine hemen tekrar giriş, zarar sonrası 4 saat, "
                  "zarar serisi molası yok. Test: yılda ~140-240 işlem (şimdikinin 1.5-2.5 katı), getiri iki dönemde "
                  "de daha yüksek, en büyük düşüş %30-57 (şimdiki %25-56)."
                  f"\n⚙️ Değişecekler: {esc(', '.join(changed))}")
        if self.bot.state.positions:
            detail += "\n📦 Açık pozisyon kendi emirleriyle devam eder."
        self.panel.request_change(values, self._view("fut"), mid, origin="fut", ask=detail,
                                  question="Önerilen vadeli ayarlar yüklensin mi?")

    def _coin_buttons(self) -> List[List[Button]]:
        chosen = futures_coin_list(self.cfg)
        names = list(FUTURES_COIN_CHOICES) + [c for c in chosen if c not in FUTURES_COIN_CHOICES]
        buttons = [(f"{_on(c in chosen)} {c}", f"fc:{c}") for c in names]
        rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
        return rows + [[("↺ Varsayılan 10 coin", "fc:default")]]

    # ------------------------------------------------------------ metinler
    def protect_label(self) -> str:
        cfg = self.cfg
        be = f"+%{cfg.breakeven_trigger_pct:g}" if cfg.breakeven_enabled else "kapalı"
        trail = f"+%{cfg.futures_trailing_act_pct:g}" if cfg.futures_trailing else "kapalı"
        return f"🔒 Başabaş {be} | 📈 İzleyen {trail}"

    def coins_label(self) -> str:
        cfg = self.cfg
        if cfg.futures_universe > 0:
            return f"🪙 Coinler (en hacimli {cfg.futures_universe})"
        return f"🪙 Coinler ({len(futures_coin_list(cfg))})"

    def kill_label(self) -> str:
        bot = self.bot
        return f"☠️ Kill-switch: %{bot.loss_pct:.4g} (−{fmt_usdt(bot.loss_limit)} {self.cfg.quote_asset}) ›"

    def kill_line(self) -> str:
        bot, q = self.bot, self.cfg.quote_asset
        if bot.state.killed:
            return "☠️ Kill-switch: ⛔ TETİKLENDİ"
        left = bot.loss_limit + bot.state.realized_pnl + bot.unrealized()
        return (f"☠️ Kill-switch: %{bot.loss_pct:.4g} = −{fmt_usdt(bot.loss_limit)} {q} "
                f"(limite kalan {fmt_usdt(max(0.0, left))} {q})")

    def _text_fut(self) -> str:
        cfg = self.cfg
        lev = cfg.futures_leverage
        room = futures_liq_distance_pct(lev, cfg.futures_mmr_pct)
        tactic = (f"çöküş dönüşü ({self.rules_text()}, long)"
                  if _crash(cfg) else "4'lü onay (likidasyon + hacim + RSI uyumsuzluğu + BTC trendi)")
        be = (f"+%{cfg.breakeven_trigger_pct:g} → stop +%{cfg.breakeven_floor_pct:g}" if cfg.breakeven_enabled
              else "kapalı")
        sides = "" if _crash(cfg) else f" | yön: {SIDE_LABELS.get(cfg.futures_sides, cfg.futures_sides)}"
        return (f"⚙️ <b>Dinamik Ayarlar — Vadeli</b>\n{LINE}\n"
                f"🧭 Taktik: {tactic}\n"
                f"⚡ Kaldıraç: <b>{lev}x</b> (isolated) | likidasyon ~%{room:.1f} fiyat hareketinde\n"
                f"🎯 Kâr Al: fiyat +%{cfg.take_profit_pct:g} → ROE ~+%{cfg.take_profit_pct * lev:.0f}\n"
                f"🛑 Zarar Kes: fiyat -%{cfg.stop_loss_pct:g} → ROE ~-%{cfg.stop_loss_pct * lev:.0f}\n"
                f"🔒 Başabaş: {be} | ⏱️ Zaman stopu: {cfg.max_hold_minutes} dk\n"
                + (f"💵 Teminat: serbest bakiyenin %{cfg.futures_margin_usage_pct:g} kadarı{sides} | 🪙 "
                   if cfg.futures_max_positions <= 1 else
                   f"🌊 Dalga modu: en fazla {cfg.futures_max_positions} pozisyon, her biri bakiyenin "
                   f"%{cfg.futures_margin_usage_pct / cfg.futures_max_positions:.1f}'i teminat | 🪙 ")
                + (f"en hacimli {cfg.futures_universe} köklü coin" if cfg.futures_universe > 0
                   else f"{len(futures_coin_list(cfg))} coin") + "\n"
                f"🛡️ Stop güvencesi: {'açık' if cfg.futures_exchange_stop else '⚠️ kapalı'} | Kâr Al emri: "
                f"{'limit' if cfg.futures_tp_order == 'limit' else 'piyasa'}\n"
                f"{self.kill_line()}\n{LINE}\n"
                "<i>Kaldıraç değişikliği hemen kaydedilir ve SONRAKİ işlemde (girişten hemen önce Binance'te) "
                "uygulanır; açık pozisyonun kaldıracı değişmez. Zarar kes likidasyondan en az 0.5 puan önce "
                "olmalı. Ayrıntılı ayarlar aşağıdaki sayfalarda; diğerleri: /ayar ANAHTAR DEĞER</i>")

    def rules_text(self) -> str:
        """Çöküş kuralları kısaca: '4 sa ≥%10 veya 1 sa ≥%6' / '4 sa ≥3σ (en az %3)' (+ teyit)."""
        cfg = self.cfg
        rules = futures_crash_rules(cfg)
        if cfg.futures_crash_z:
            text = " veya ".join(f"{h} sa" for h, _p in rules) + (f" ≥{cfg.futures_crash_z:g}σ "
                                                                  f"(en az %{cfg.futures_crash_z_min_pct:g})")
        else:
            text = " veya ".join(f"{h} sa ≥%{p:g}" for h, p in rules)
        if cfg.futures_confirm != "close":
            text += f" + {CONFIRM_LABELS[cfg.futures_confirm].split(' ', 1)[1].lower()} teyidi"
        return text

    def _text_fe(self) -> str:
        cfg = self.cfg
        if _crash(cfg):
            liq = "açık (giriş mumunda likidasyon dalgası da şart)" if cfg.futures_crash_liq else "kapalı"
            rules = futures_crash_rules(cfg)
            extra = [r for r in rules if r != (cfg.futures_crash_hours, cfg.futures_crash_drop_pct)]
            body = (f"Taktik: <b>{TACTIC_LABELS['crash']}</b> — coin aşağıdaki pencerelerden <b>herhangi birinde</b> "
                    f"yeterince düştüyse 5 dk mum kapanışında <b>LONG</b> açılır; aynı anda birden çok coin çökerse "
                    f"eşiğini en çok aşan alınır.\n"
                    f"📉 Ana kural: son {cfg.futures_crash_hours} saatte ≥%{cfg.futures_crash_drop_pct:g}\n"
                    f"➕ Ek pencereler (VEYA): {_rules_label(extra) if extra else 'yok'}\n")
            if cfg.futures_crash_z:
                body += (f"📐 Oynaklık eşiği: <b>{cfg.futures_crash_z:g} standart sapma</b> — sabit yüzdeler yerine "
                         f"coinin o süredeki olağan hareketinin {cfg.futures_crash_z:g} katı (önceki 24 saatten), en "
                         f"az %{cfg.futures_crash_z_min_pct:g}\n")
            else:
                body += "📐 Oynaklık eşiği: kapalı (sabit yüzdeler)\n"
            confirm = CONFIRM_TEXT[cfg.futures_confirm].format(ratio=cfg.futures_confirm_volume_ratio)
            if cfg.futures_confirm != "close":
                confirm += f"; çöküşten sonra en fazla {_mins(cfg.futures_confirm_bars * 5)} beklenir"
            body += f"✅ Teyit (izleme modu): {confirm}\n💥 Likidasyon dalgası şartı: {liq}"
            hint = ("Izgara testi (2020-2026, 1 dk çıkışlarla doğrulandı; ayrıntı README): '1 sa %6' ek penceresi "
                    "başabaş +1 → +0.3 ve SL 5 ile işlem sayısını 1.8-2.1 katına çıkarıp kazanma oranını ve kârı "
                    "artırdı (🏆 / 🥈 düğmeleri bu iki seti yükler); mevcut çıkışlarla ek pencere kârı düşürür. "
                    "'🟢 İlk yeşil mum' teyidi kârı düşürdü, '📊 Hacim zirvesi' işlemi azalttı, oynaklık (σ) eşiği "
                    "sabit yüzdeden kötü çıktı. Önce test modunda deneyin; komut satırında: python main.py --backtest "
                    "--strateji futures --izgara. Taktik değişince o taktiğin çıkış ayarları da yüklenir (onay ister).")
        else:
            source = SOURCE_LABELS.get(cfg.futures_liq_source, cfg.futures_liq_source)
            if cfg.futures_liq_source != "proxy":
                source += f" (en az {_thousands(cfg.futures_liq_min_usdt)} USDT / 5 dk)"
            body = (f"Taktik: <b>{TACTIC_LABELS['liq4']}</b> — likidasyon dalgası + hacim + RSI uyumsuzluğu + BTC "
                    f"1 saatlik trendi aynı anda.\n"
                    f"{SIDE_LABELS.get(cfg.futures_sides, cfg.futures_sides)} | 📊 Hacim onayı: "
                    f"{cfg.futures_volume_ratio:g}x | 💥 Likidasyon eşiği: {cfg.futures_liq_ratio:g}x\n"
                    f"Likidasyon ölçümü: {source} | ₿ BTC trendi: "
                    f"{'açık' if cfg.futures_btc_trend else 'kapalı'}\n"
                    "⚠️ 2020-2026 testinde 7 yılın 7'sinde zarar etti.")
            hint = ("Taktik değişince o taktiğin çıkış ayarları da yüklenir (onay ister). Canlı likidasyon akışı "
                    "backtest edilemez.")
        return f"🧭 <b>Vadeli Giriş Sinyali</b>\n{LINE}\n{body}\n{LINE}\n<i>{hint}</i>"

    def _text_fw(self) -> str:
        cfg, bot = self.cfg, self.bot
        if not _crash(cfg):
            return (f"🌊 <b>Dalga Modu</b>\n{LINE}\nYalnızca 💥 Çöküş dönüşü taktiğinde çalışır (🧭 Giriş sinyali → "
                    f"taktik).")
        k = cfg.futures_max_positions
        if k > 1 and cfg.futures_extra_size_pct >= 100:
            mode = (f"Açık: aynı anda en fazla <b>{k}</b> pozisyon; her biri bakiyenin "
                    f"%{cfg.futures_margin_usage_pct / k:.1f}'i teminat × {cfg.futures_leverage}x. İlk pozisyon her "
                    f"sinyalde, sonrakiler yalnızca son 1 saatte <b>≥{cfg.futures_extra_breadth}</b> coin %3+ düşerken "
                    f"(piyasa geneli likidasyon dalgası) açılır.")
        elif k > 1:
            first = cfg.futures_margin_usage_pct * futures_slot_share(cfg, 0)
            each = cfg.futures_margin_usage_pct * futures_slot_share(cfg, 1)
            mode = (f"Açık: aynı anda en fazla <b>{k}</b> pozisyon. İlk pozisyon her sinyalde, bakiyenin "
                    f"%{first:.1f}'i teminat × {cfg.futures_leverage}x (bakiyenin {first * cfg.futures_leverage / 100:.2f}"
                    f" katı); ek pozisyonlar yalnızca son 1 saatte <b>≥{cfg.futures_extra_breadth}</b> coin %3+ düşerken, "
                    f"her biri %{each:.1f}'i (ilkinin %{cfg.futures_extra_size_pct:g}'i).")
        else:
            mode = "Kapalı: aynı anda tek pozisyon (tüm bakiye)."
        size = (f"Yalnız çöküş (son 1 saatte {cfg.futures_isolated_breadth}'den az coin %3+ düşüyor): pozisyon "
                f"<b>%{cfg.futures_isolated_size_pct:g}</b> büyüklükte." if cfg.futures_isolated_size_pct < 100
                else "Yalnız çöküşte pozisyon tam büyüklükte.")
        lines = ["🌊 <b>Dalga Modu</b>", LINE, mode, size]
        if cfg.futures_reentry_breadth > 0:
            lines.append(f"🔁 Dalgada yeniden giriş: son 1 saatte ≥{cfg.futures_reentry_breadth} coin %3+ düşerken zarar "
                         f"sonrası bekleme ({cfg.loss_cooldown_minutes} dk) atlanır."
                         + (" ⚠️ LOSS_COOLDOWN_MINUTES bekleme vermiyor, etkisiz." if
                            cfg.loss_cooldown_minutes <= cfg.symbol_cooldown_minutes else ""))
        wave = futures_wave_rules(cfg)
        if wave:
            lines.append(f"📉 Dalga eşikleri: son 1 saatte ≥{cfg.futures_wave_breadth} coin %3+ düşerken "
                         + " veya ".join(f"{h} saatte %{p:g}" for h, p in wave) + " düşüş de sinyal.")
        if k > 1 and cfg.breakeven_enabled and futures_be_trigger(cfg, 1) != cfg.breakeven_trigger_pct:
            lines.append(f"🔒 Ek pozisyon başabaşı (Faz 8): fiyat +%{cfg.futures_extra_be_trigger_pct:g}'e gelince stop "
                         f"girişin +%{cfg.breakeven_floor_pct:g} üstüne (ilk pozisyonda +%{cfg.breakeven_trigger_pct:g}).")
        if futures_hold_minutes(cfg, 1, None) != cfg.max_hold_minutes or cfg.futures_wave_hold_minutes > 0:
            parts = []
            if futures_hold_minutes(cfg, 1, None) != cfg.max_hold_minutes:
                parts.append(f"ek pozisyonlar {cfg.futures_extra_hold_minutes} dk")
            if cfg.futures_wave_hold_minutes > 0:
                parts.append(f"≥{cfg.futures_wave_hold_breadth} coin düşerken açılan ilk pozisyon "
                             f"{cfg.futures_wave_hold_minutes} dk")
            lines.append(f"⏱️ Zaman stopu (Faz 9): {', '.join(parts)} (diğerleri {cfg.max_hold_minutes} dk).")
        breadth = getattr(bot, "last_breadth", None)
        if breadth is not None:
            lines.append(f"Son tarama: {breadth} coin son 1 saatte %3+ düştü.")
        if k > 1 and cfg.futures_universe <= 0:
            lines.append("⚠️ Yalnızca FUTURES_COINS listesi taranıyor: eşikler ~25 coinlik evren (liste + en hacimli 20) "
                         "için seçildi, ek pozisyon çok seyrek açılır.")
        lines += [LINE, f"<i>{esc(CASCADE_HINT)}</i>", f"<i>{esc(PHASE6_HINT)}</i>", f"<i>{esc(PHASE8_HINT)}</i>",
                  f"<i>{esc(PHASE9_HINT)}</i>"]
        return "\n".join(lines)

    def _text_fm(self) -> str:
        cfg = self.cfg
        if not _crash(cfg):
            return (f"🧩 <b>Ek Giriş Modülleri</b>\n{LINE}\nModüller yalnızca 💥 Çöküş dönüşü taktiğinde çalışır "
                    f"(🧭 Giriş sinyali → taktik).")
        pump = futures_pump_rules(cfg)
        bb = cfg.futures_bb
        lines = ["🧩 <b>Ek Giriş Modülleri</b> (çöküş kuralına VEYA ile eklenir)", LINE,
                 "📈 Pompa dönüşü (SHORT): " + (" veya ".join(f"son {h} saatte +%{p:g}" for h, p in pump)
                                              + " yükselen coinde 5 dk kapanışında short" if pump else "kapalı"),
                 "〰️ Bollinger + RSI: " + ("kapalı" if bb == "off" else esc(
                     f"{BB_LABELS[bb].split(' ', 1)[1].lower()} — kapanış 20 mumluk {cfg.futures_bb_std:g}σ bandın "
                     f"dışında ve RSI(14) < {cfg.futures_bb_rsi_low:g} (long) / > {cfg.futures_bb_rsi_high:g} "
                     f"(short)")),
                 "🚀 Hacimli kırılım (LONG): " + (f"hacim önceki 20 mumun {cfg.futures_breakout_volume_ratio:g} "
                                                f"katından fazla ve kapanış son {cfg.futures_breakout_hours} saatin "
                                                f"zirvesinin üstünde" if cfg.futures_breakout else "kapalı"),
                 "💸 Fonlama sıkışması (LONG): " + (esc(
                     f"son ödenen fonlama (8 saatlik) ≤ %{cfg.futures_funding_rate_pct:g} ve "
                     + " veya ".join(f"son {h} saatte -%{p:g}" for h, p in futures_funding_rules(cfg)))
                     if cfg.futures_funding else "kapalı"),
                 "📐 Trend içi düzeltme (LONG): " + (esc(
                     f"son 1 saatlik kapanış EMA{cfg.futures_pullback_ema} üstünde ve 5 dk RSI(14) < "
                     f"{cfg.futures_pullback_rsi:g}") if cfg.futures_pullback else "kapalı"),
                 "Aynı anda birden çok sinyal: önce çöküş, sonra fonlama, pompa, düzeltme, kırılım, Bollinger (tek "
                 "pozisyon); çıkışlar 🎯 Çıkışlar sayfasındaki ayarlar. Bollinger / kırılım / düzeltme açıkken her "
                 "coinin mumları her 5 dakikada istenir.", LINE, f"<i>{MODULES_HINT}</i>"]
        if cfg.futures_sides != "both":
            lines.insert(-2, f"⚠️ FUTURES_SIDES={cfg.futures_sides}: "
                             + ("short modüller (pompa, Bollinger short) çalışmaz." if cfg.futures_sides == "long"
                                else "long modüller (çöküş, fonlama, düzeltme, kırılım, Bollinger long) "
                                     "çalışmaz."))
        return "\n".join(lines)

    def _text_fx(self) -> str:
        cfg = self.cfg
        lev = cfg.futures_leverage
        room = futures_liq_distance_pct(lev, cfg.futures_mmr_pct)
        be = (f"+%{cfg.breakeven_trigger_pct:g} → +%{cfg.breakeven_floor_pct:g}" if cfg.breakeven_enabled
              else "kapalı")
        be += (f" | 📈 İzleyen stop: +%{cfg.futures_trailing_act_pct:g}'de, %{cfg.futures_trailing_gap_pct:g} geriden"
               if cfg.futures_trailing else " | 📈 İzleyen stop: kapalı")
        tp_order = ("limit emir (Binance'te bekler, maker komisyon)" if cfg.futures_tp_order == "limit"
                    else "piyasa emri (hedefte bot kapatır, taker komisyon)")
        stop = ("pozisyon hemen kapatılır (güvenli)" if cfg.futures_exchange_stop
                else "⚠️ pozisyon açık kalır, bot izler (bot kapanırsa korumasız)")
        entry = ("piyasa emri (taker; kayma girişte de ödenir)" if cfg.futures_entry_order == "market" else
                 f"limit (maker): bid'in %{cfg.futures_limit_offset_pct:g} altına post-only emir, "
                 f"{cfg.futures_limit_ttl_min} dk dolmazsa iptal (fırsat kaçar)")
        fast = ("açık: tarama mum kapanışından 1 sn sonra, mumlar tek dalgada, teminat modu + kaldıraç önceden ayarlı "
                "(her taramadan önce Binance'ten doğrulanır)" if cfg.futures_fast_entry else
                "kapalı: tarama mum kapanışından 3 sn sonra, teminat modu + kaldıraç her girişte ayarlanır")
        timing = self._timing_line()
        if futures_scan_shifts(cfg):
            minute = "açık: 5 dk kapanışlarının yanında aradaki dakikalarda da (1 dk mumlarla, aynı kural)"
        elif cfg.futures_minute_scan:
            minute = "açık ama bu ayarlarla kullanılamıyor (sade çöküş taktiği + piyasa emriyle giriş gerekir)"
        else:
            minute = "kapalı: yalnızca 5 dk mum kapanışlarında"
        if futures_snapshot_on(cfg):
            snap = "açık: tarama mum kapanışından ~0.15 sn sonra anlık fiyatla; emir bakiye / emir defteri beklemeden"
        elif cfg.futures_snapshot_scan:
            snap = "açık ama bu ayarlarla kullanılamıyor (hızlı giriş + sade çöküş taktiği + piyasa emriyle giriş gerekir)"
        else:
            snap = "kapalı: mumların kesinleşmesi beklenir (kapanıştan 1-2.5 sn sonra)"
        return (f"🎯 <b>Vadeli Çıkışlar ve Emirler</b>\n{LINE}\n"
                f"🎯 Kâr Al: fiyat +%{cfg.take_profit_pct:g} → ROE ~+%{cfg.take_profit_pct * lev:.0f}\n"
                f"🛑 Zarar Kes: fiyat -%{cfg.stop_loss_pct:g} → ROE ~-%{cfg.stop_loss_pct * lev:.0f} | likidasyon "
                f"~%{room:.1f}'te ({lev}x)\n"
                f"🔒 Başabaş: {be}\n"
                f"⏱️ Zaman stopu: {cfg.max_hold_minutes} dk (dolunca piyasa emriyle kapanır)\n"
                f"📌 Kâr Al emri: {tp_order}\n"
                f"🚪 Giriş emri: {entry}\n"
                f"⚡ Hızlı giriş: {fast}\n"
                f"⏱️ Her dakika tarama: {minute}\n"
                f"🚀 Anlık tarama: {snap}\n"
                f"🛡️ Zarar kes emri Binance'e konamazsa: {stop}\n{timing}{LINE}\n"
                "<i>Kâr Al / Zarar Kes değişikliği SONRAKİ işlemde geçerli (açık pozisyonun Binance emirleri "
                "değişmez); zaman stopu ve başabaş açık pozisyona da uygulanır. Testte seçilen (çöküş dönüşü): Kâr Al "
                f"%6, Zarar Kes %4, başabaş 1.5 → 0.3, 30 dk.\n{esc(FAST_ENTRY_HINT)}\n{esc(MINUTE_SCAN_HINT)}\n{esc(SNAPSHOT_HINT)}</i>")

    def _timing_line(self) -> str:
        """Son taramanın ölçülen zamanlaması (FuturesBot.last_timing); ölçüm yoksa boş."""
        tm = getattr(self.bot, "last_timing", None)
        if not tm or len(tm.get("ms") or []) < 3:
            return ""
        st, kl, _sg = (v / 1000 for v in tm["ms"][:3])
        return f"⏱️ Son tarama: mum kapanışından {st:.1f} sn sonra başladı, mumlar {kl:.1f} sn'de alındı\n"

    def _text_fb(self) -> str:
        cfg = self.cfg
        lev = cfg.futures_leverage
        be = (f"açık — fiyat +%{cfg.breakeven_trigger_pct:g} olunca zarar kes girişin +%{cfg.breakeven_floor_pct:g} "
              f"üstüne çekilir (teminatın ~%{cfg.breakeven_floor_pct * lev:.0f}'i kilitlenir)" if cfg.breakeven_enabled
              else "kapalı")
        trail = (f"açık — fiyat +%{cfg.futures_trailing_act_pct:g} olunca devreye girer; zarar kes, görülen en iyi "
                 f"fiyatın %{cfg.futures_trailing_gap_pct:g} gerisinden izler (yalnızca lehte ilerler)"
                 if cfg.futures_trailing else "kapalı")
        warn = []
        if cfg.futures_trailing and cfg.futures_trailing_act_pct >= cfg.take_profit_pct:
            warn.append(f"⚠️ İzleyen stop başlangıcı Kâr Al'dan (+%{cfg.take_profit_pct:g}) büyük: önce Kâr Al çalışır, "
                        f"izleyen stop hiç devreye girmez. Kâr Al'ı yükseltin veya başlangıcı düşürün.")
        if cfg.breakeven_enabled and cfg.breakeven_trigger_pct >= cfg.take_profit_pct:
            warn.append(f"⚠️ Başabaş tetiği Kâr Al'dan (+%{cfg.take_profit_pct:g}) büyük: başabaş hiç devreye girmez.")
        return (f"🔒 <b>Başabaş ve İzleyen Stop</b> (fiyat %, girişten)\n{LINE}\n"
                f"🔒 Başabaş: {be}\n📈 İzleyen stop: {trail}\n"
                + ("\n".join(warn) + "\n" if warn else "") + f"{LINE}\n"
                f"<i>{TRAIL_HINT} İkisi birlikte açıksa hangisi daha yukarıdaysa zarar kes orada durur. Binance'teki "
                f"stop emri de taşınır (izleyen stopta en sık 10 sn'de bir); bot arada fiyatı kendisi izler.</i>")

    def _text_fc(self) -> str:
        cfg = self.cfg
        if cfg.futures_universe > 0:
            now = getattr(self.bot, "universe", []) or []
            shown = ", ".join(s[:-4] for s in now[:20]) + (" ..." if len(now) > 20 else "")
            volume = ("bir önceki UTC gününün USDT hacmine göre ilk {n} (her gün 00:00 UTC'de yenilenir"
                      if cfg.futures_universe_mode == "daily" else
                      f"son {cfg.futures_universe_avg_days} günün ortalama USDT hacmine göre ilk {{n}} (günde bir "
                      f"yenilenir").format(n=cfg.futures_universe)
            return (f"🪙 <b>Vadeli Coinler</b>: en hacimli <b>{cfg.futures_universe}</b> köklü coin\n{LINE}\n"
                    f"En az {cfg.futures_universe_min_age_days} gündür ({_age_label(cfg.futures_universe_min_age_days)}"
                    f") Binance USDⓈ-M'de olan kripto paritelerden, {volume}; stabil coin, endeks, hisse / emtia "
                    f"vadelileri ve kaldırılması planlanan pariteler hariç)."
                    + (f"\nŞu an: {esc(shown)}" if now else "\nListe ilk taramada oluşur.")
                    + (f"\n📋 + listeniz: {esc(', '.join(futures_coin_list(cfg)))}" if cfg.futures_universe_with_list
                       else "")
                    + f"\n{LINE}\n<i>{self._universe_hint()} Elle seçilen liste için 📋 Liste.</i>")
        coins = futures_coin_list(cfg)
        return (f"🪙 <b>Vadeli Coinler</b> ({len(coins)} seçili)\n{LINE}\n✅ {esc(', '.join(coins))}\n{LINE}\n"
                "<i>Bot yalnızca seçili coinlerin USDT vadeli paritelerinde işlem açar. Varsayılan 10 coin "
                "backtestte kullanılan büyük coinlerdir; eklediğiniz diğer coinler test edilmedi. Binance'te "
                "vadelisi olmayan coin taranmaz. Listede olmayan bir coin için: /ayar FUTURES_COINS BTC,ETH,... "
                f"Ya da yukarıdan en hacimli 20-100 coin.</i>")

    def _universe_hint(self) -> str:
        return UNIVERSE_HINT

    def _text_ks(self) -> str:
        bot, cfg = self.bot, self.cfg
        st, q = bot.state, cfg.quote_asset
        unreal = bot.unrealized()
        total = st.realized_pnl + unreal
        if st.killed:
            status = (f"⛔ <b>TETİKLENDİ</b>: {esc(st.kill_reason)}\nLimit değiştirmek kilidi açmaz: servisi "
                      f"durdurup sunucuda <code>python main.py --mode {cfg.trading_mode} --reset-state</code> "
                      f"çalıştırın.")
        else:
            status = f"✅ Devrede | limite kalan: <b>{fmt_usdt(max(0.0, bot.loss_limit + total))} {q}</b>"
        legacy = (f"\nℹ️ Eski sabit ayar MAX_TOTAL_LOSS_USDT={cfg.legacy_loss_usdt:g} kullanılıyor; bir düğmeye "
                  f"basınca yüzdelik ayara (MAX_TOTAL_LOSS_PCT) geçilir." if cfg.legacy_loss_usdt is not None else "")
        return (f"☠️ <b>Kill-switch (toplam zarar sigortası)</b>\n{LINE}\n"
                f"Limit: <b>%{bot.loss_pct:.4g}</b> → −{fmt_usdt(bot.loss_limit)} {q} (bota verilen sermaye "
                f"{fmt_usdt(st.start_capital)} {q})\n"
                f"📊 Toplam K/Z: {fmt_signed(total)} {q} (gerçekleşen {fmt_signed(st.realized_pnl)} | açık "
                f"pozisyonlar {fmt_signed(unreal)})\n"
                f"Durum: {status}{legacy}\n{LINE}\n"
                "<i>Toplam zarar (gerçekleşen + açık pozisyonlar) limite ulaşınca bot tüm pozisyonları kapatır, yeni "
                "işlem açmaz ve kilitlenir (yeniden başlasa da açılmaz). Sıkılaştırmak hemen uygulanır; gevşetmek "
                "(limiti büyütmek) onay ister; şu anki zararın altında bir limit seçilemez. Backtest de aynı limiti "
                "kullanır.</i>")

    def _text_risk(self) -> str:
        bot, cfg = self.bot, self.cfg
        q = cfg.quote_asset
        day_pnl = bot.today_pnl()
        daily = (f"%{cfg.daily_loss_limit_pct:g} (≈{fmt_usdt(bot.daily_loss_usdt())} {q})"
                 if cfg.daily_loss_limit_pct > 0 else "kapalı")
        target = f"%{cfg.daily_profit_target_pct:g}" if cfg.daily_profit_target_pct > 0 else "kapalı"
        streak = (f"üst üste {cfg.loss_streak_limit} zarar kes → {cfg.loss_streak_pause_hours:g} saat alım yok "
                  f"(şu an seri: {bot.state.sl_streak})" if cfg.loss_streak_limit > 0 else "kapalı")
        karne = (f"aynı coinde üst üste {cfg.coin_sl_limit} zarar kes → 24 saat kara liste teklifi"
                 if cfg.coin_sl_limit > 0 else "kapalı")
        slip = (f"son 10 alımın ortalaması sınırı aşarsa uyarır (sınır %{cfg.slippage_alert_pct:g})"
                if cfg.slippage_alert_pct > 0 else "kapalı")
        cool = (f"zararla kapanan coine {_mins(cfg.loss_cooldown_minutes)} girilmez"
                if cfg.loss_cooldown_minutes > cfg.symbol_cooldown_minutes else "normal (her çıkıştaki bekleme)")
        frozen = bot.freeze_note()
        return (f"🧯 <b>Risk Sigortaları</b>\n{LINE}\n"
                f"{self.kill_line()}\n"
                f"📉 Günlük zarar limiti: {daily} | bugün: {fmt_signed(day_pnl)} {q}\n"
                f"🎯 Günlük kâr hedefi: {target}\n🔁 Zarar serisi sigortası: {streak}\n"
                f"⏳ Her çıkıştan sonra aynı coine bekleme: "
                f"{_mins(cfg.symbol_cooldown_minutes) if cfg.symbol_cooldown_minutes else 'yok (hemen tekrar girilebilir)'}\n"
                f"🚫 Zarardan sonra bekleme: {cool}\n"
                f"📒 Coin karnesi: {karne}\n" + ("" if is_futures(cfg.strategy) else f"💧 Kayma alarmı: {slip}\n") +
                f"{frozen or '▶️ Alımlar dondurulmamış'}\n{LINE}\n"
                "<i>Sigortalar yalnızca YENİ alımları durdurur; açık pozisyonların takibi sürer. Kill-switch ise "
                "her şeyi kapatıp botu kilitler. Diğerleri: /ayar ANAHTAR DEĞER</i>")

    def _text_ntf(self) -> str:
        cfg = self.cfg
        mode = cfg.notify_mode
        status = (f"her {_mins(cfg.status_report_minutes)}" if cfg.status_report_minutes > 0 else "kapalı")
        daily = cfg.daily_report_time or "kapalı"
        weekly = "pazar günleri" if cfg.weekly_report else "kapalı"
        quiet = (f"{cfg.quiet_hours} (mesajlar sessiz gelir; acil uyarılar sesli)" if cfg.quiet_hours else "kapalı")
        return (f"🔔 <b>Bildirim Ayarı</b>\n{LINE}\n"
                + "\n".join(f"{'✅' if m == mode else '▫️'} <b>{NOTIFY_LABELS[m]}</b>: {NOTIFY_HELP[m]}"
                            for m in NOTIFY_LABELS)
                + f"\n{LINE}\n📊 Durum raporu: {status}\n📅 Günlük özet: {esc(daily)}\n🗓️ Haftalık özet: {weekly}\n"
                  f"🌙 Sessiz saatler: {esc(quiet)}\n{LINE}\n"
                  "<i>Komut cevapları, uyarılar ve hatalar her modda gelir. Susturulan mesajlar loglara yazılır. "
                  "Başka saat için: /ayar DAILY_REPORT_TIME 22:30, /ayar QUIET_HOURS 01:00-09:00</i>")

    def _text_sx(self) -> str:
        cfg = self.cfg
        hold = fmt_duration(cfg.max_hold_minutes * 60, exact=True) if cfg.max_hold_minutes else "sınırsız"
        trail = ("rotasyonda kullanılmaz" if is_rotation(cfg.strategy) else
                 f"net +%{cfg.trailing_activation_pct:g} görünce başlar, net kâr +%{cfg.trailing_floor_pct:g} "
                 f"tabanına inerse satılır")
        be = (f"açık — net +%{cfg.breakeven_trigger_pct:g} görünce, net +%{cfg.breakeven_floor_pct:g}'e inerse "
              f"satılır" if cfg.breakeven_enabled else "kapalı")
        backup = (f"net −%{cfg.backup_stop_pct:g} (Binance'te bekler, bot ulaşılamazken de korur; yeni pozisyonlarda "
                  f"geçerli)" if cfg.backup_stop_enabled else "kapalı (BACKUP_STOP_ENABLED=false)")
        return (f"🔧 <b>Çıkış Ayrıntıları</b>\n{LINE}\n"
                f"📈 İzleyen stop: {trail}\n⏱️ Maks. süre: {hold} (dolunca satılır)\n🔒 Başabaş: {be}\n"
                f"🧱 Yedek stop: {backup}\n{LINE}\n"
                "<i>Kâr Al, Zarar Kes ve izleyen stop başlangıcı: ⚙️ Ayarlar. Yedek stop zarar kesin altında "
                "olmalı. Değişiklik hemen uygulanır ve .env'ye kaydedilir.</i>")
