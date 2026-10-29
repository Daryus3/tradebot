"""
Risk yönetimi.

Tüm kâr/zarar kararları KOMİSYON DÜŞÜLDÜKTEN SONRAKİ NET para üzerinden verilir:

    maliyet      = alışta harcanan USDT (alış komisyonu coinden kesilir → elde daha az coin kalır)
    net değer    = satılabilir_miktar × güncel_alış_fiyatı(bid) × (1 − komisyon)
    net K/Z (%)  = (net değer − maliyet) / maliyet × 100

Örnek (11 USDT, %0.1 komisyon):
    Kâr Al        → net değer ≥ 12.65 USDT (+%15)
    Zarar Kes     → net değer ≤  9.90 USDT (−%10)
    İzleyen Stop  → net değer bir kez ≥ 12.10 (+%10) olduktan sonra ≤ 11.55 (+%5) olursa sat
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Dict, List, Optional

from config import is_rotation, strategy_family
from exchange import SymbolRules, floor_decimals, to_dec


class ExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    MAX_HOLD = "MAX_HOLD"
    KILL_SWITCH = "KILL_SWITCH"
    EXTERNAL = "EXTERNAL"      # Coin bot dışından satılmış / bakiyede bulunamadı
    BREAKEVEN = "BREAKEVEN"    # Başabaş koruması
    MANUAL = "MANUAL"          # Telegram /sat komutuyla elle satış
    BTC_CRASH = "BTC_CRASH"    # BTC sert düştüğü için erken çıkış
    BACKUP_STOP = "BACKUP_STOP"  # Binance'teki yedek zarar-kes emri çalıştı
    ROTATION = "ROTATION"        # Momentum rotasyonu: coin sıralamadan düştü (yerine daha güçlüsü alınır)
    TREND_FILTER = "TREND_FILTER"  # Momentum rotasyonu: BTC düşüş trendine girdi, her şey USDT'ye çevrildi
    TP_EXTENDED = "TP_EXTENDED"    # Kâr Al'da momentum güçlüydü, hedef uzatıldı; zirveden geri dönünce satıldı
    ESCAPE = "ESCAPE"              # Hacimli satış baskısında zarar kes beklenmeden çıkıldı
    TIME_EXIT = "TIME_EXIT"        # Süre sorusu: yatay kalan pozisyon başabaşta satıldı
    PROFIT_LOCK = "PROFIT_LOCK"    # Piramit sonrası kilit: birleşik pozisyon zarara dönmeden satıldı
    STALE = "STALE"                # Takılan işlem: STALE_EXIT_MINUTES içinde toparlanma başlamadı


@dataclass
class Position:
    symbol: str
    base_asset: str
    qty: float            # Komisyon düşüldükten sonra eldeki coin miktarı
    entry_price: float    # Ortalama alış fiyatı
    cost_usdt: float      # Toplam maliyet (USDT)
    opened_at: float      # Unix zamanı
    buy_fee_usdt: float = 0.0
    peak_pnl_pct: float = 0.0
    trailing_active: bool = False
    signal_note: str = ""
    min_pnl_pct: float = 0.0          # İşlem boyunca görülen en kötü net K/Z (%)
    breakeven_active: bool = False
    # Binance'te bekleyen yedek zarar-kes emri
    backup_order_id: Optional[int] = None
    backup_stop_price: float = 0.0
    backup_qty: float = 0.0
    pending_sell_client_id: str = ""  # Sonucu belirsiz kalan satış emri (tekrar satmadan önce sorgulanır)
    # Ayrıntılı işlem kaydı için alış anı ölçümleri
    score: float = 0.0
    metrics: Dict[str, float] = field(default_factory=dict)
    spread_pct: float = 0.0
    btc_1h_pct: Optional[float] = None
    regime: str = ""
    # Akıllı pozisyon yönetimi
    tp_pct: Optional[float] = None    # Pozisyona özel Kâr Al (ATR_TARGETS); None = TAKE_PROFIT_PCT
    sl_pct: Optional[float] = None    # Pozisyona özel Zarar Kes (ATR_TARGETS); None = STOP_LOSS_PCT
    tp_extended: bool = False         # Kâr Al aşıldı, momentum güçlü: zirveden TP_EXTEND_GAP_PCT geri dönünce satılır
    reserve_usdt: float = 0.0         # Bu pozisyon için yedekte bekleyen USDT (CAPITAL_USAGE_PCT < 100)
    adds: int = 0                     # Yedek akçeyle kaç kez eklendi (DCA / piramit)
    lock_pct: Optional[float] = None  # Piramit sonrası: net K/Z bunun altına inerse sat (zarara dönmesin)
    exit_at_breakeven: bool = False   # Süre sorusu "başabaşta sat": net K/Z ≥ 0 olunca satılır
    asked: List[str] = field(default_factory=list)   # Sorulan sorular (her biri pozisyon başına bir kez)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class EntryPlan:
    ok: bool
    quote_amount: float = 0.0
    est_qty: float = 0.0
    reason: str = ""


def net_exit_value(sellable_qty: float, price: float, fee_rate: float) -> float:
    """Şu an satılsa eline geçecek net USDT."""
    return sellable_qty * price * (1 - fee_rate)


def pnl_pct(net_value: float, cost: float) -> float:
    return (net_value - cost) / cost * 100 if cost > 0 else 0.0


def price_for_pnl(target_pct: float, cost: float, sellable_qty: float, fee_rate: float) -> float:
    """Net K/Z'nin `target_pct` olacağı satış fiyatı (Telegram'da hedef fiyatları göstermek için)."""
    if sellable_qty <= 0:
        return 0.0
    return cost * (1 + target_pct / 100) / (sellable_qty * (1 - fee_rate))


def trailing_floor_pct(pos: Position, cfg) -> float:
    """İzleyen stop aktifken satışın tetikleneceği net kâr yüzdesi."""
    floor = cfg.trailing_floor_pct
    if cfg.trailing_dynamic:
        floor = max(floor, pos.peak_pnl_pct * 0.5)  # Zirve kârın yarısını koru
    return floor


def target_exits(cfg) -> bool:
    """Kâr al / izleyen stop / başabaş / maks. süre çıkışları kullanılıyor mu?

    Momentum rotasyonunda (STRATEGY=rotation) pozisyon sıralamadan düşünce veya BTC trend filtresiyle satılır;
    yalnızca zarar kes (felaket koruması) ve kill-switch geçerlidir.
    """
    return not is_rotation(getattr(cfg, "strategy", ""))


def update_trailing(pos: Position, current_pnl_pct: float, cfg) -> bool:
    """Zirveyi günceller. İzleyen stop bu çağrıda ilk kez aktifleştiyse True döner."""
    if current_pnl_pct > pos.peak_pnl_pct:
        pos.peak_pnl_pct = current_pnl_pct
    if current_pnl_pct < pos.min_pnl_pct:
        pos.min_pnl_pct = current_pnl_pct
    if not target_exits(cfg):
        return False
    if not pos.trailing_active and pos.peak_pnl_pct >= cfg.trailing_activation_pct:
        pos.trailing_active = True
        return True
    return False


def breakeven_effective(cfg) -> bool:
    """Başabaş koruması bu ayarlarla bir işe yarar mı?

    Tetik Kâr Al'a eşit/üstündeyse önce Kâr Al satar. Tetik izleyen stop başlangıcına eşit/üstündeyse
    ve başabaş tabanı izleyen stop tabanından yüksek değilse, izleyen stop (daha yüksek tabanla) her
    zaman önce satar. Bu durumlarda koruma etkisizdir.
    """
    if not getattr(cfg, "breakeven_enabled", False) or not target_exits(cfg):
        return False
    if cfg.breakeven_trigger_pct >= cfg.take_profit_pct:
        return False
    return not (cfg.breakeven_trigger_pct >= cfg.trailing_activation_pct
                and cfg.breakeven_floor_pct <= cfg.trailing_floor_pct)


def update_breakeven(pos: Position, cfg) -> bool:
    """Başabaş koruması (varsayılan açık): zirve net kâr BREAKEVEN_TRIGGER_PCT'yi gördüyse aktifleşir.

    Yeni aktifleştiyse True döner. update_trailing'den SONRA çağrılmalıdır (zirve güncel olsun).
    """
    if pos.breakeven_active or not breakeven_effective(cfg):
        return False
    if pos.peak_pnl_pct >= cfg.breakeven_trigger_pct:
        pos.breakeven_active = True
        return True
    return False


def position_tp(pos: Position, cfg) -> float:
    """Pozisyonun Kâr Al yüzdesi (ATR_TARGETS ile pozisyona özel olabilir)."""
    return pos.tp_pct if getattr(pos, "tp_pct", None) is not None else cfg.take_profit_pct


def position_sl(pos: Position, cfg) -> float:
    """Pozisyonun Zarar Kes yüzdesi (ATR_TARGETS ile pozisyona özel olabilir)."""
    return pos.sl_pct if getattr(pos, "sl_pct", None) is not None else cfg.stop_loss_pct


def extension_floor(pos: Position, cfg) -> float:
    """Hedefi uzatılmış pozisyonun satış eşiği: görülen zirve net kârın TP_EXTEND_GAP_PCT altı."""
    return pos.peak_pnl_pct - cfg.tp_extend_gap_pct


def evaluate_exit(pos: Position, current_pnl_pct: float, now: float, cfg) -> Optional[ExitReason]:
    """Pozisyonun kapatılması gerekiyorsa nedenini, gerekmiyorsa None döndürür.

    Kâr Al'da hedef uzatma (TP_EXTEND) kararı ÇAĞIRAN tarafta verilir (pos.tp_extended); burada yalnızca uygulanır.
    """
    sl = position_sl(pos, cfg)
    if not target_exits(cfg):   # Rotasyon: yalnızca felaket koruması
        return ExitReason.STOP_LOSS if current_pnl_pct <= -sl else None
    if pos.tp_extended:
        if current_pnl_pct <= extension_floor(pos, cfg):
            return ExitReason.TP_EXTENDED
    elif current_pnl_pct >= position_tp(pos, cfg):
        return ExitReason.TAKE_PROFIT
    if current_pnl_pct <= -sl:
        return ExitReason.STOP_LOSS
    if pos.lock_pct is not None and current_pnl_pct <= pos.lock_pct:
        return ExitReason.PROFIT_LOCK
    if pos.exit_at_breakeven and current_pnl_pct >= 0:
        return ExitReason.TIME_EXIT
    if pos.trailing_active and current_pnl_pct <= trailing_floor_pct(pos, cfg):
        return ExitReason.TRAILING_STOP
    if (getattr(cfg, "breakeven_enabled", False) and pos.breakeven_active
            and current_pnl_pct <= cfg.breakeven_floor_pct):
        return ExitReason.BREAKEVEN
    if stale_exit_due(pos, now, cfg):
        return ExitReason.STALE
    if cfg.max_hold_minutes > 0 and now - pos.opened_at >= cfg.max_hold_minutes * 60:
        return ExitReason.MAX_HOLD
    return None


def stale_exit_due(pos: Position, now: float, cfg) -> bool:
    """Takılan işlem (STALE_EXIT_MINUTES): bu süre dolduğu halde net kâr bir kez bile STALE_EXIT_PEAK_PCT'ye
    çıkmadıysa toparlanma gelmemiş sayılır ve satılır (maks. süreyi beklemeden). DCA ile eklenen pozisyon ortalama
    düşürüp dönüşü beklemek için eklendiğinden bu kurala tabi değildir."""
    minutes = getattr(cfg, "stale_exit_minutes", 0)
    return (minutes > 0 and not pos.adds and now - pos.opened_at >= minutes * 60
            and pos.peak_pnl_pct < cfg.stale_exit_peak_pct)


def cooldown_minutes(cfg, pnl: float) -> int:
    """Kapanan coine tekrar girmeden önce beklenecek süre: zararla kapandıysa LOSS_COOLDOWN_MINUTES (daha uzunsa)."""
    loss = getattr(cfg, "loss_cooldown_minutes", 0)
    return max(cfg.symbol_cooldown_minutes, loss) if pnl < 0 and loss > 0 else cfg.symbol_cooldown_minutes


def kill_switch_hit(realized_pnl: float, unrealized_pnl: float, loss_limit: float) -> bool:
    """Gerçekleşen + açık pozisyon zararı toplamı limite ulaştı mı?"""
    return realized_pnl + unrealized_pnl <= -abs(loss_limit) + 1e-9


def _backup_floor(cfg) -> float:
    """Binance yedek stop emrinin (STOP_LOSS / STOP_LOSS_LIMIT) tetiklenince değerinin pozisyona oranı.

    Yalnızca günlerce / haftalarca tutulan stratejilerde (swing, rotasyon) boyutlandırmaya katılır: bot kapalıyken
    korumanın asıl önemli olduğu yer orası. 5 dk stratejilerinin boyutlandırması değişmez (1.0 döner).
    """
    if not getattr(cfg, "backup_stop_enabled", False) or strategy_family(getattr(cfg, "strategy", "")) == "short":
        return 1.0
    return (1 - cfg.backup_stop_pct / 100) * (1 - getattr(cfg, "backup_stop_limit_gap_pct", 0.0) / 100)


def min_position_usdt(min_notional: float, cfg) -> float:
    """Stop-loss seviyesinde bile satılabilmesi için bir pozisyonun olması gereken en küçük tutar.

    Örn. min. emir 5 USDT, güvenlik payı 1.05, stop-loss %10 → 5 × 1.05 / 0.90 ≈ 5.83 USDT
    Swing / rotasyonda Binance yedek stop emri de (daha aşağıda, örn. -%25) minimum emir tutarının üstünde kalmalı;
    yoksa emir konulamaz ve pozisyon bot kapalıyken korumasız kalır.
    """
    by_stop = min_notional * cfg.min_notional_safety / (1 - cfg.stop_loss_pct / 100)
    floor = _backup_floor(cfg)
    # Yedek stopta da aynı güvenlik payı: alış komisyonu, LOT / fiyat adımı yuvarlamaları emri biraz küçültür
    return max(by_stop, min_notional * cfg.min_notional_safety / floor) if floor > 0 else by_stop


def position_slots(capital: float, min_notional: float, cfg) -> int:
    """Bakiyenin güvenle kaç pozisyona bölünebileceği (en az 1, en fazla MAX_OPEN_POSITIONS).

    11 USDT → 1 | 12 USDT → 2 | 17.5+ USDT → 3  (min. emir 5 USDT, SL %10 ile)
    """
    usable = capital * cfg.capital_usage_pct / 100
    # plan_entry tutarı kuruşa AŞAĞI yuvarladığı için en küçük pozisyon da kuruşa YUKARI yuvarlanır;
    # yoksa ör. 11.67 USDT'de 2 slot hesaplanıp her alım reddedilir.
    per = math.ceil(min_position_usdt(min_notional, cfg) * 100 - 1e-9) / 100
    fit = int(usable / per + 1e-9)
    return max(1, min(cfg.max_open_positions, fit))


def plan_entry(available_usdt: float, rules: SymbolRules, price: float, cfg) -> EntryPlan:
    """Küçük bakiyeyi Binance kurallarına göre optimize eden pozisyon boyutlandırma.

    Kontroller:
      1. Yatırılacak tutar minimum emir tutarının (minNotional, genelde 5 USDT) üstünde mi?
      2. Fiyat stop-loss seviyesine düşse bile pozisyon hâlâ satılabilir mi?
         (aksi halde coin 'satılamaz toz' olarak cüzdanda kalır)
      3. LOT adımı yüzünden satılamayacak 'toz' kaybı kabul edilebilir mi?
    """
    if price <= 0:
        return EntryPlan(False, reason="Geçersiz fiyat")
    invest = floor_decimals(available_usdt * cfg.capital_usage_pct / 100, 2)
    min_notional = float(rules.min_notional)

    if invest < min_notional * cfg.min_notional_safety:
        return EntryPlan(False, reason=f"Kullanılabilir bakiye ({invest:.2f}) minimum emir tutarına "
                                       f"({min_notional:.2f}) yetmiyor")

    value_at_stop = invest * (1 - cfg.stop_loss_pct / 100)
    if value_at_stop < min_notional * cfg.min_notional_safety:
        return EntryPlan(False, reason=f"Stop-loss seviyesinde pozisyon değeri ({value_at_stop:.2f}) "
                                       f"minimum satış tutarının ({min_notional:.2f}) altına düşer")
    value_at_backup = invest * _backup_floor(cfg)
    if value_at_backup < min_notional * cfg.min_notional_safety:
        return EntryPlan(False, reason=f"Binance yedek stop seviyesinde pozisyon değeri ({value_at_backup:.2f}) "
                                       f"minimum emir tutarının ({min_notional:.2f}) altına düşer")

    qty = rules.floor_market_qty(to_dec(invest) / to_dec(price))
    if qty <= 0 or qty < rules.market_min_qty:
        return EntryPlan(False, reason="Miktar LOT_SIZE minimumunun altında")

    dust_pct = rules.step_value(price) / invest * 100
    if dust_pct > cfg.max_dust_pct:
        return EntryPlan(False, reason=f"LOT adımı çok büyük: satışta ~%{dust_pct:.2f} toz kaybı olur "
                                       f"(sınır %{cfg.max_dust_pct})")

    return EntryPlan(True, quote_amount=invest, est_qty=float(qty))
