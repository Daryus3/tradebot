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
Değişiklik ControlPanel.request_change ile doğrulanır, hemen uygulanır ve .env'ye yazılır. Kill-switch'i gevşetmek
ve Binance'teki zarar kes güvencesini kapatmak onay ister; şu anki zararın altında bir kill-switch seçilemez.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from config import (DEFAULT_FUTURES_COINS, FUTURES_LIQ_SOURCES, FUTURES_SIDES, FUTURES_TACTIC_EXITS, FUTURES_TACTICS,
                    FUTURES_TP_ORDERS, futures_coin_list, futures_liq_distance_pct, is_futures, is_rotation)
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
LEVERAGE_CHOICES = (1, 2, 3, 5, 10, 15, 20)
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


def encode(value) -> str:
    """Değerin düğme verisindeki yazımı: ':' düğme verisini böldüğü için saatlerde atılır, boş = 'off'."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value.replace(":", "") or "off"
    return format_value(value)


PAGES: Dict[str, Page] = {
    # ------------------------------------------------------------ vadeli işlem
    "fut": Page("⚙️ Dinamik Ayarlar — Vadeli", rows=(
        Row("FUTURES_LEVERAGE", icon="⚡", values=LEVERAGE_CHOICES, fmt=lambda v: f"{v}x", cb="lev",
            step=1, step_cb="adj:LEV", name="⚡", unit="x"),
        Row("FUTURES_MARGIN_USAGE_PCT", icon="💵", values=(25, 50, 75, 95), fmt=_pct),
        (Link(lambda m: "🧭 Giriş sinyali", "pg:fe"), Link(lambda m: "🎯 Çıkış ve emirler", "pg:fx")),
        (Link(lambda m: f"🪙 Coinler ({len(futures_coin_list(m.cfg))})", "pg:fc"),
         Link(lambda m: "☠️ Kill-switch", "pg:ks")),
        (Link(lambda m: "🧯 Risk sigortaları", "risk"), Link(lambda m: "🔔 Bildirim ve raporlar", "ntf")),
    ), back=(("⬅️ Menü", "menu"),), futures=True),
    "fe": Page("🧭 Vadeli Giriş Sinyali", rows=(
        Row("FUTURES_TACTIC", values=FUTURES_TACTICS, fmt=TACTIC_LABELS.get, cb="ftac", per_row=2),
        Row("FUTURES_CRASH_DROP_PCT", icon="📉", values=(6, 8, 10, 12, 15), fmt=_pct, show=_crash, per_row=5),
        Row("FUTURES_CRASH_HOURS", icon="🕓", values=(1, 2, 4, 8, 12, 24), fmt=lambda v: f"{v} sa", show=_crash,
            per_row=3),
        Row("FUTURES_CRASH_LIQ", name="💥 Likidasyon dalgası şartı", show=_crash),
        Row("FUTURES_SIDES", values=FUTURES_SIDES, fmt=SIDE_LABELS.get, show=_liq4, per_row=3),
        Row("FUTURES_VOLUME_RATIO", icon="📊", values=(1.5, 2, 2.5, 3), fmt=_x, show=_liq4),
        Row("FUTURES_LIQ_RATIO", icon="💥", values=(2, 3, 4, 5), fmt=_x, show=_liq4),
        Row("FUTURES_LIQ_SOURCE", values=FUTURES_LIQ_SOURCES, fmt=SOURCE_LABELS.get, show=_liq4, per_row=3),
        Row("FUTURES_LIQ_MIN_USDT", icon="💵", values=(50_000, 100_000, 250_000, 500_000),
            fmt=lambda v: f"{v / 1000:g}K", show=lambda cfg: _liq4(cfg) and cfg.futures_liq_source != "proxy"),
        Row("FUTURES_BTC_TREND", name="₿ BTC 1 saatlik trend filtresi", show=_liq4),
    ), back=(("⬅️ Vadeli ayarlar", "cfg"), ("🏠 Menü", "menu")), futures=True),
    "fx": Page("🎯 Vadeli Çıkışlar ve Emirler", rows=(
        Row("TAKE_PROFIT_PCT", icon="🎯", values=_by_tactic((3, 4, 6, 8), (1.0, 1.2, 1.5, 2.0)), fmt=_pct),
        Row("STOP_LOSS_PCT", icon="🛑", values=_by_tactic((3, 4, 5), (0.8, 0.9, 1.0)), fmt=_pct),
        (Row("TAKE_PROFIT_PCT", step=_by_tactic(0.5, 0.1), name="🎯"),
         Row("STOP_LOSS_PCT", step=_by_tactic(0.5, 0.1), name="🛑")),
        Row("BREAKEVEN_TRIGGER_PCT", icon="🔒", values=_by_tactic((1, 1.5, 2, 3), (0.3, 0.5, 0.7, 1.0)),
            fmt=lambda v: f"+%{v:g}"),
        Row("BREAKEVEN_FLOOR_PCT", icon="🧷", values=(0.1, 0.2, 0.3, 0.5), fmt=lambda v: f"+%{v:g}"),
        Row("MAX_HOLD_MINUTES", icon="⏱️", values=(15, 30, 60, 120), fmt=_mins),
        (Row("BREAKEVEN_ENABLED", name="Başabaş"), Row("FUTURES_EXCHANGE_STOP", name="Stop güvencesi")),
        Row("FUTURES_TP_ORDER", values=FUTURES_TP_ORDERS, fmt=TP_ORDER_LABELS.get, per_row=2),
    ), back=(("⬅️ Vadeli ayarlar", "cfg"), ("🏠 Menü", "menu")), futures=True),
    "fc": Page("🪙 Vadeli Coinler", back=(("⬅️ Vadeli ayarlar", "cfg"), ("🏠 Menü", "menu")), futures=True),
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
        Row("SYMBOL_COOLDOWN_MINUTES", icon="⏳", values=(15, 30, 60, 120), fmt=_mins),
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
        keyboard = (self._coin_buttons() if pid == "fc" else []) + self.keyboard(pid)
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
                data = f"{item.cb}:{encode(value)}" if item.cb else f"kv:{pid}:{item.key}:{encode(value)}"
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

    def _coin_buttons(self) -> List[List[Button]]:
        chosen = futures_coin_list(self.cfg)
        names = list(FUTURES_COIN_CHOICES) + [c for c in chosen if c not in FUTURES_COIN_CHOICES]
        buttons = [(f"{_on(c in chosen)} {c}", f"fc:{c}") for c in names]
        rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
        return rows + [[("↺ Varsayılan 10 coin", "fc:default")]]

    # ------------------------------------------------------------ metinler
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
        tactic = (f"çöküş dönüşü ({cfg.futures_crash_hours} saatte ≥%{cfg.futures_crash_drop_pct:g} düşüş, long)"
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
                f"💵 Teminat: serbest bakiyenin %{cfg.futures_margin_usage_pct:g} kadarı{sides} | 🪙 "
                f"{len(futures_coin_list(cfg))} coin\n"
                f"🛡️ Stop güvencesi: {'açık' if cfg.futures_exchange_stop else '⚠️ kapalı'} | Kâr Al emri: "
                f"{'limit' if cfg.futures_tp_order == 'limit' else 'piyasa'}\n"
                f"{self.kill_line()}\n{LINE}\n"
                "<i>Kaldıraç değişikliği hemen kaydedilir ve SONRAKİ işlemde (girişten hemen önce Binance'te) "
                "uygulanır; açık pozisyonun kaldıracı değişmez. Zarar kes likidasyondan en az 0.5 puan önce "
                "olmalı. Ayrıntılı ayarlar aşağıdaki sayfalarda; diğerleri: /ayar ANAHTAR DEĞER</i>")

    def _text_fe(self) -> str:
        cfg = self.cfg
        if _crash(cfg):
            liq = "açık (çöküş mumunda likidasyon dalgası da şart)" if cfg.futures_crash_liq else "kapalı"
            body = (f"Taktik: <b>{TACTIC_LABELS['crash']}</b> — coin son {cfg.futures_crash_hours} saatte en az "
                    f"%{cfg.futures_crash_drop_pct:g} düştüyse 5 dk mum kapanışında <b>LONG</b> açılır; aynı anda "
                    f"birden çok coin çökerse en derin düşen alınır.\n"
                    f"📉 Çöküş eşiği: %{cfg.futures_crash_drop_pct:g} | 🕓 Ölçüm süresi: {cfg.futures_crash_hours} "
                    f"saat\n💥 Likidasyon dalgası şartı: {liq}")
            hint = ("Eşik büyüdükçe daha az ama daha derin çöküşlerde işlem açılır. Testte seçilen: %10 / 4 saat, "
                    "şart kapalı (açıkken kazanma oranı %76-79, işlem sayısı daha az). Değişikliği önce 🧪 Backtest "
                    "ile deneyin. Taktik değişince o taktiğin çıkış ayarları da yüklenir (onay ister).")
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

    def _text_fx(self) -> str:
        cfg = self.cfg
        lev = cfg.futures_leverage
        room = futures_liq_distance_pct(lev, cfg.futures_mmr_pct)
        be = (f"açık — fiyat +%{cfg.breakeven_trigger_pct:g} olunca zarar kes girişin +%{cfg.breakeven_floor_pct:g} "
              f"üstüne çekilir" if cfg.breakeven_enabled else "kapalı")
        tp_order = ("limit emir (Binance'te bekler, maker komisyon)" if cfg.futures_tp_order == "limit"
                    else "piyasa emri (hedefte bot kapatır, taker komisyon)")
        stop = ("pozisyon hemen kapatılır (güvenli)" if cfg.futures_exchange_stop
                else "⚠️ pozisyon açık kalır, bot izler (bot kapanırsa korumasız)")
        return (f"🎯 <b>Vadeli Çıkışlar ve Emirler</b>\n{LINE}\n"
                f"🎯 Kâr Al: fiyat +%{cfg.take_profit_pct:g} → ROE ~+%{cfg.take_profit_pct * lev:.0f}\n"
                f"🛑 Zarar Kes: fiyat -%{cfg.stop_loss_pct:g} → ROE ~-%{cfg.stop_loss_pct * lev:.0f} | likidasyon "
                f"~%{room:.1f}'te ({lev}x)\n"
                f"🔒 Başabaş: {be}\n"
                f"⏱️ Zaman stopu: {cfg.max_hold_minutes} dk (dolunca piyasa emriyle kapanır)\n"
                f"📌 Kâr Al emri: {tp_order}\n"
                f"🛡️ Zarar kes emri Binance'e konamazsa: {stop}\n{LINE}\n"
                "<i>Kâr Al / Zarar Kes değişikliği SONRAKİ işlemde geçerli (açık pozisyonun Binance emirleri "
                "değişmez); zaman stopu ve başabaş açık pozisyona da uygulanır. Testte seçilen (çöküş dönüşü): Kâr Al "
                "%6, Zarar Kes %4, başabaş 1.5 → 0.3, 30 dk.</i>")

    def _text_fc(self) -> str:
        coins = futures_coin_list(self.cfg)
        return (f"🪙 <b>Vadeli Coinler</b> ({len(coins)} seçili)\n{LINE}\n✅ {esc(', '.join(coins))}\n{LINE}\n"
                "<i>Bot yalnızca seçili coinlerin USDT vadeli paritelerinde işlem açar. Varsayılan 10 coin "
                "backtestte kullanılan büyük coinlerdir; eklediğiniz diğer coinler test edilmedi. Binance'te "
                "vadelisi olmayan coin taranmaz. Listede olmayan bir coin için: /ayar FUTURES_COINS BTC,ETH,...</i>")

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
                f"⏳ Her çıkıştan sonra aynı coine bekleme: {_mins(cfg.symbol_cooldown_minutes)}\n"
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
