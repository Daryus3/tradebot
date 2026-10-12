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

from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Optional

from exchange import SymbolRules, floor_decimals, to_dec


class ExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    MAX_HOLD = "MAX_HOLD"
    KILL_SWITCH = "KILL_SWITCH"
    EXTERNAL = "EXTERNAL"  # Coin bot dışından satılmış / bakiyede bulunamadı


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


def update_trailing(pos: Position, current_pnl_pct: float, cfg) -> bool:
    """Zirveyi günceller. İzleyen stop bu çağrıda ilk kez aktifleştiyse True döner."""
    if current_pnl_pct > pos.peak_pnl_pct:
        pos.peak_pnl_pct = current_pnl_pct
    if not pos.trailing_active and pos.peak_pnl_pct >= cfg.trailing_activation_pct:
        pos.trailing_active = True
        return True
    return False


def evaluate_exit(pos: Position, current_pnl_pct: float, now: float, cfg) -> Optional[ExitReason]:
    """Pozisyonun kapatılması gerekiyorsa nedenini, gerekmiyorsa None döndürür."""
    if current_pnl_pct >= cfg.take_profit_pct:
        return ExitReason.TAKE_PROFIT
    if current_pnl_pct <= -cfg.stop_loss_pct:
        return ExitReason.STOP_LOSS
    if pos.trailing_active and current_pnl_pct <= trailing_floor_pct(pos, cfg):
        return ExitReason.TRAILING_STOP
    if cfg.max_hold_minutes > 0 and now - pos.opened_at >= cfg.max_hold_minutes * 60:
        return ExitReason.MAX_HOLD
    return None


def kill_switch_hit(realized_pnl: float, unrealized_pnl: float, max_total_loss: float) -> bool:
    """Gerçekleşen + açık pozisyon zararı toplamı limite ulaştı mı?"""
    return realized_pnl + unrealized_pnl <= -abs(max_total_loss) + 1e-9


def min_position_usdt(min_notional: float, cfg) -> float:
    """Stop-loss seviyesinde bile satılabilmesi için bir pozisyonun olması gereken en küçük tutar.

    Örn. min. emir 5 USDT, güvenlik payı 1.05, stop-loss %10 → 5 × 1.05 / 0.90 ≈ 5.83 USDT
    """
    return min_notional * cfg.min_notional_safety / (1 - cfg.stop_loss_pct / 100)


def position_slots(capital: float, min_notional: float, cfg) -> int:
    """Bakiyenin güvenle kaç pozisyona bölünebileceği (en az 1, en fazla MAX_OPEN_POSITIONS).

    11 USDT → 1 | 12 USDT → 2 | 17.5+ USDT → 3  (min. emir 5 USDT, SL %10 ile)
    """
    usable = capital * cfg.capital_usage_pct / 100
    fit = int(usable / min_position_usdt(min_notional, cfg) + 1e-9)
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

    qty = rules.floor_market_qty(to_dec(invest) / to_dec(price))
    if qty <= 0 or qty < rules.market_min_qty:
        return EntryPlan(False, reason="Miktar LOT_SIZE minimumunun altında")

    dust_pct = rules.step_value(price) / invest * 100
    if dust_pct > cfg.max_dust_pct:
        return EntryPlan(False, reason=f"LOT adımı çok büyük: satışta ~%{dust_pct:.2f} toz kaybı olur "
                                       f"(sınır %{cfg.max_dust_pct})")

    return EntryPlan(True, quote_amount=invest, est_qty=float(qty))
