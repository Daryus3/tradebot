"""
Borsa katmanı.

- `SymbolRules`: Binance'in parite kuralları (LOT_SIZE, NOTIONAL vb.).
- `LiveExchange`: Gerçek Binance hesabında emir verir.
- `PaperExchange`: Canlı fiyat verisiyle SANAL bakiye üzerinde işlem simüle eder
  (paper trading / test modu). Gerçek emir göndermez.

Her iki sınıf da aynı arayüzü sunduğu için bot mantığı iki modda da aynıdır.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Dict, FrozenSet, List, Optional, Tuple

import requests

from binance_client import BinanceAPIError, BinanceClient
from logger import get_logger

log = get_logger()


def to_dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def dec_to_str(value: Decimal) -> str:
    """Bilimsel gösterim olmadan, gereksiz sıfırları atarak yazar (Binance bunu ister)."""
    text = format(value.normalize(), "f")
    return text if text not in ("-0", "") else "0"


def floor_decimals(value: float, decimals: int = 2) -> float:
    q = Decimal(1).scaleb(-decimals)
    return float(to_dec(value).quantize(q, rounding=ROUND_DOWN))


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    order_types: FrozenSet[str] = frozenset()

    def floor_market_qty(self, qty) -> Decimal:
        q = floor_to_step(to_dec(qty), self.market_step_size)
        if self.market_max_qty > 0 and q > self.market_max_qty:
            q = floor_to_step(self.market_max_qty, self.market_step_size)
        return max(q, Decimal(0))

    def step_value(self, price: float) -> float:
        """Bir LOT adımının USDT değeri (satışta kalabilecek en büyük 'toz')."""
        return float(self.market_step_size) * price

    def floor_order_qty(self, qty) -> Decimal:
        """Bekleyen (stop) emirler için miktar: hem LOT_SIZE hem MARKET_LOT_SIZE adımına uyar."""
        q = floor_to_step(to_dec(qty), self.step_size) if self.step_size > 0 else to_dec(qty)
        return self.floor_market_qty(q)

    def floor_price(self, price) -> Decimal:
        return floor_to_step(to_dec(price), self.tick_size) if self.tick_size > 0 else to_dec(price)


def parse_symbol_rules(exchange_info: dict, quote_asset: str = "USDT") -> Dict[str, SymbolRules]:
    """exchangeInfo yanıtından, piyasa emriyle alınıp satılabilen pariteleri çıkarır."""
    rules: Dict[str, SymbolRules] = {}
    for s in exchange_info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") != quote_asset:
            continue
        if not s.get("isSpotTradingAllowed", True):
            continue
        if "MARKET" not in s.get("orderTypes", []):
            continue
        if not s.get("quoteOrderQtyMarketAllowed", False):
            continue

        f = {flt.get("filterType"): flt for flt in s.get("filters", [])}
        lot = f.get("LOT_SIZE", {})
        mlot = f.get("MARKET_LOT_SIZE", {})
        step = to_dec(lot.get("stepSize", "0"))
        min_qty = to_dec(lot.get("minQty", "0"))
        max_qty = to_dec(lot.get("maxQty", "0"))
        m_step = to_dec(mlot.get("stepSize", "0"))
        m_min = to_dec(mlot.get("minQty", "0"))
        m_max = to_dec(mlot.get("maxQty", "0"))
        if m_step <= 0:
            m_step = step
        if m_min <= 0:
            m_min = min_qty
        if m_max <= 0:
            m_max = max_qty
        if m_step <= 0:
            continue

        # Güvenli tarafta kalmak için minNotional piyasa emirlerine de uygulanmış kabul edilir.
        notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
        min_notional = to_dec(notional.get("minNotional", "0"))
        tick = to_dec(f.get("PRICE_FILTER", {}).get("tickSize", "0"))

        rules[s["symbol"]] = SymbolRules(
            symbol=s["symbol"],
            base_asset=s["baseAsset"],
            quote_asset=s["quoteAsset"],
            tick_size=tick,
            step_size=step,
            min_qty=min_qty,
            market_step_size=m_step,
            market_min_qty=m_min,
            market_max_qty=m_max,
            min_notional=min_notional,
            order_types=frozenset(s.get("orderTypes", [])),
        )
    return rules


def is_unknown_outcome(exc: BinanceAPIError) -> bool:
    """Binance belgelerine göre 5xx ve -1006/-1007 yanıtlarında emrin sonucu BİLİNMEZ
    (gerçekleşmiş olabilir). Bunlar 'red' sayılmamalı, emir sorgulanmalıdır."""
    return (exc.status or 0) >= 500 or exc.code in (-1006, -1007)


class OrderError(Exception):
    """Emir verilemedi/reddedildi. `code`: NO_BALANCE, MIN_NOTIONAL, LOT_SIZE, NOT_FILLED, Binance kodu..."""

    def __init__(self, message: str, code: str = "", client_order_id: str = ""):
        super().__init__(message)
        self.code = code
        self.client_order_id = client_order_id


@dataclass
class Fill:
    """Gerçekleşen bir emrin özeti (komisyonlar düşülmüş net değerlerle)."""
    symbol: str
    side: str          # BUY / SELL
    qty: float         # BUY: komisyon düşüldükten sonra eldeki net coin | SELL: satılan coin
    avg_price: float
    usdt: float        # BUY: toplam maliyet | SELL: komisyon düşülmüş net gelir
    fee_usdt: float    # Ödenen komisyonun USDT karşılığı
    order_id: str = ""


def combine_fills(fills: List[Fill]) -> Optional[Fill]:
    """Aynı pozisyonun parça parça satışlarını tek satış özetinde birleştirir."""
    fills = [f for f in fills if f and f.qty > 0]
    if not fills:
        return None
    qty = sum(f.qty for f in fills)
    gross = sum(f.qty * f.avg_price for f in fills)
    return Fill(fills[0].symbol, fills[0].side, qty, gross / qty if qty else 0.0,
                sum(f.usdt for f in fills), sum(f.fee_usdt for f in fills),
                ",".join(f.order_id for f in fills if f.order_id))


BNB = "BNB"


def _flag(value) -> bool:
    """Binance bayrağı: JSON true veya "true" metni (bool("false") True olacağı için ayrı kontrol)."""
    return value is True or str(value).strip().lower() == "true"


@dataclass(frozen=True)
class FeeInfo:
    """Hesabın gerçek komisyon durumu (BNB indirimi dahil). Yalnızca okunur bilgidir.

    Binance komisyonu BNB'den ancak hem hesapta hem paritede "BNB ile komisyon öde" açıksa VE yeterli
    serbest BNB varsa keser; yoksa sessizce alınan coinden/USDT'den tam oranla keser.
    """
    taker: float                # Standart taker oranı (0.001 = %0.1)
    discount: float = 0.0       # BNB ile ödemedeki İNDİRİM payı (0.25 = %25 indirim)
    bnb_enabled: bool = False   # Hesapta ve paritede BNB ile ödeme açık (discountAsset BNB)
    bnb_free: float = 0.0       # Serbest BNB bakiyesi
    bnb_usdt: float = 0.0       # BNB fiyatı (USDT); öğrenilemediyse veya BNB yoksa 0

    @property
    def discounted_rate(self) -> float:
        """BNB ile ödenince uygulanan oran (%0.1 → %0.075)."""
        return round(self.taker * (1 - self.discount), 10)

    @property
    def bnb_active(self) -> bool:
        """İndirim şu an gerçekten uygulanıyor mu (açık ve BNB var)?"""
        return self.bnb_enabled and self.bnb_free > 0

    @property
    def effective_rate(self) -> float:
        return self.discounted_rate if self.bnb_active else self.taker

    @property
    def bnb_value(self) -> float:
        return self.bnb_free * self.bnb_usdt

    def bnb_trades(self, position_usdt: float) -> Optional[float]:
        """BNB bakiyesi bu büyüklükte kaç al-sat (alış + satış) işleminin komisyonuna yeter?
        BNB yoksa 0; BNB fiyatı bilinmiyorsa None."""
        if self.bnb_free <= 0:
            return 0.0
        per_trade = 2 * position_usdt * self.discounted_rate
        if self.bnb_usdt <= 0 or per_trade <= 0:
            return None
        return self.bnb_value / per_trade

    def covers(self, position_usdt: float) -> bool:
        """İndirim bu büyüklükteki bir işlemde gerçekten uygulanır mı? Binance komisyonu BNB'den ancak
        BNB TEK BİR emrin komisyonunun tamamına yetiyorsa keser; yetmeyen kırıntı BNB'de tam oran alınır."""
        if not self.bnb_active:
            return False
        if self.bnb_usdt <= 0 or position_usdt <= 0:  # Fiyat/boyut bilinmiyorsa eldeki BNB'ye güven
            return True
        return self.bnb_value >= position_usdt * self.discounted_rate

    def rate_for(self, position_usdt: float) -> float:
        """Bu büyüklükteki bir işlemde gerçekten kesilecek oran."""
        return self.discounted_rate if self.covers(position_usdt) else self.taker

    def status_for(self, cfg_rate: float, position_usdt: float) -> str:
        """fee_rate_status'un işlem büyüklüğünü (BNB kırıntısını) hesaba katan hali."""
        real = self.rate_for(position_usdt)
        if abs(cfg_rate - real) <= 1e-6:
            return "ok"
        return "conservative" if cfg_rate > real else "optimistic"

    def fee_rate_status(self, cfg_rate: float) -> str:
        """FEE_RATE ayarı gerçek orana göre: "ok", "conservative" (ayar yüksek, hesap temkinli) veya
        "optimistic" (ayar düşük: net kâr al / zarar kes hesapları fazla iyimser)."""
        real = self.effective_rate
        if abs(cfg_rate - real) <= 1e-6:
            return "ok"
        return "conservative" if cfg_rate > real else "optimistic"


def parse_fee_info(commission: dict, bnb_free: float = 0.0, bnb_usdt: float = 0.0) -> FeeInfo:
    """`/api/v3/account/commission` yanıtını FeeInfo'ya çevirir. `discount` kısmı yoksa veya
    indirim varlığı BNB değilse indirim yok sayılır."""
    taker = float(commission["standardCommission"]["taker"])
    block = commission.get("discount")
    discount, enabled = 0.0, False
    if isinstance(block, dict) and str(block.get("discountAsset", "")).upper() == BNB:
        try:
            discount = min(max(float(block.get("discount") or 0), 0.0), 1.0)
        except (TypeError, ValueError):
            discount = 0.0
        enabled = _flag(block.get("enabledForAccount")) and _flag(block.get("enabledForSymbol"))
    return FeeInfo(taker, discount, enabled, max(0.0, bnb_free), max(0.0, bnb_usdt))


@dataclass
class BackupStop:
    """Binance'te bekleyen yedek zarar-kes emri."""
    order_id: int
    stop_price: float
    qty: float
    kind: str  # STOP_LOSS (piyasa) veya STOP_LOSS_LIMIT


@dataclass
class BackupStatus:
    status: str                  # OPEN, FILLED, CANCELED, GONE (zaten yok), ERROR (öğrenilemedi)
    fill: Optional[Fill] = None  # Emrin gerçekleşen kısmı (varsa)
    detail: str = ""


class BaseExchange:
    is_paper = False
    name = "base"

    def __init__(self, client: BinanceClient, quote_asset: str = "USDT", fee_rate: float = 0.001):
        self.client = client
        self.quote_asset = quote_asset
        self.fee_rate = fee_rate
        self.rules: Dict[str, SymbolRules] = {}
        self.rules_loaded_at = 0.0

    def refresh_rules(self) -> int:
        self.rules = parse_symbol_rules(self.client.exchange_info(), self.quote_asset)
        self.rules_loaded_at = time.time()
        return len(self.rules)

    def typical_min_notional(self) -> float:
        """Paritelerde en sık görülen minimum emir tutarı (Binance'te genelde 5 USDT)."""
        values = [float(r.min_notional) for r in self.rules.values() if r.min_notional > 0]
        return Counter(values).most_common(1)[0][0] if values else 5.0

    def rules_for(self, symbol: str) -> SymbolRules:
        rules = self.rules.get(symbol)
        if rules is None:
            raise OrderError(f"{symbol} için işlem kuralı bulunamadı (parite kapalı olabilir)", "NO_RULES")
        return rules

    def get_bid_ask(self, symbol: str) -> Tuple[float, float]:
        book = self.client.book_ticker(symbol)
        bid, ask = float(book.get("bidPrice") or 0), float(book.get("askPrice") or 0)
        if bid <= 0 or ask <= 0:
            last = self.client.price(symbol)
            bid = bid if bid > 0 else last
            ask = ask if ask > 0 else last
        return bid, ask

    def sellable_qty(self, symbol: str, qty: float) -> float:
        """LOT adımına göre gerçekten satılabilecek miktar (kalan kısım 'toz' olur)."""
        rules = self.rules.get(symbol)
        if rules is None:
            return qty
        q = rules.floor_market_qty(qty)
        return float(q) if q >= rules.market_min_qty else 0.0

    def backup_order_params(self, symbol: str, qty: float, stop_price: float,
                            limit_gap_pct: float) -> Tuple[Decimal, Decimal, Optional[Decimal], str]:
        """Yedek stop emrinin miktar/fiyatlarını Binance kurallarına göre hazırlar (miktar, stop, limit, tür)."""
        rules = self.rules_for(symbol)
        q = rules.floor_order_qty(qty)
        stop = rules.floor_price(stop_price)
        if q <= 0 or q < rules.market_min_qty or q < rules.min_qty or stop <= 0:
            raise OrderError("Yedek stop için miktar/fiyat geçersiz", "LOT_SIZE")
        limit = None
        kind = "STOP_LOSS"
        if "STOP_LOSS" not in rules.order_types:
            if "STOP_LOSS_LIMIT" not in rules.order_types:
                raise OrderError(f"{symbol} paritesi stop emirlerini desteklemiyor", "NO_STOP_TYPE")
            kind = "STOP_LOSS_LIMIT"
            limit = rules.floor_price(stop * (1 - to_dec(limit_gap_pct) / 100))
        lowest = limit if limit is not None else stop
        if q * lowest < rules.min_notional:
            raise OrderError(f"Yedek stop tutarı ({float(q * lowest):.2f}) minimum emir tutarının altında",
                             "MIN_NOTIONAL")
        return q, stop, limit, kind

    # Alt sınıfların doldurduğu metotlar
    def free_balance(self, asset: str) -> float:
        raise NotImplementedError

    def total_balance(self, asset: str) -> float:
        """Serbest + emirde kilitli bakiye."""
        return self.free_balance(asset)

    def place_backup_stop(self, symbol: str, qty: float, stop_price: float, limit_gap_pct: float) -> BackupStop:
        raise NotImplementedError

    def cancel_backup_stop(self, symbol: str, order_id: int) -> BackupStatus:
        raise NotImplementedError

    def backup_stop_status(self, symbol: str, order_id: int) -> BackupStatus:
        raise NotImplementedError

    def find_sell_fills(self, symbol: str, since_sec: float) -> Optional[Fill]:
        """Bot dışında yapılmış satışların gerçek sonucunu bulur (sadece canlı modda)."""
        return None

    def recover_order(self, symbol: str, client_order_id: str, side: str) -> Optional[Fill]:
        """Durumu belirsiz kalmış bir emrin sonucunu öğrenir. Emir yoksa None."""
        return None

    def taker_fee_rate(self, symbol: str) -> Optional[float]:
        """Hesaba uygulanan gerçek komisyon oranı (sadece canlı modda)."""
        return None

    def fee_info(self, symbol: str = "BTCUSDT") -> Optional[FeeInfo]:
        """Komisyon ve BNB indirimi durumu (sadece canlı modda)."""
        return None

    def market_buy(self, symbol: str, quote_amount: float, client_order_id: Optional[str] = None) -> Fill:
        """client_order_id: önceden kaydedilmiş emir kimliği (bot emir sırasında kapanırsa sonucu sorgulanabilsin)."""
        raise NotImplementedError

    def dust_assets(self) -> List[Tuple[str, float, float]]:
        """BNB'ye çevrilebilecek tozlar: [(coin, miktar, BNB karşılığı)] (yalnızca canlı modda)."""
        raise OrderError("Toz dönüştürme yalnızca canlı modda (gerçek Binance hesabında) yapılabilir", "PAPER")

    def convert_dust(self, assets: List[str]) -> Tuple[float, float]:
        """Tozları BNB'ye çevirir: (alınan BNB, kesilen ücret BNB)."""
        raise OrderError("Toz dönüştürme yalnızca canlı modda (gerçek Binance hesabında) yapılabilir", "PAPER")

    def market_sell(self, symbol: str, qty: float) -> Fill:
        raise NotImplementedError


class PaperExchange(BaseExchange):
    """Sanal bakiye ile canlı fiyatlardan simülasyon. Alış ask, satış bid fiyatından yapılır."""

    is_paper = True
    name = "paper"

    def __init__(self, client: BinanceClient, balances: Dict[str, float],
                 quote_asset: str = "USDT", fee_rate: float = 0.001):
        super().__init__(client, quote_asset, fee_rate)
        self.balances = balances  # BotState içindeki sözlük; değişiklikler state ile kaydedilir

    def free_balance(self, asset: str) -> float:
        return float(self.balances.get(asset, 0.0))

    def market_buy(self, symbol: str, quote_amount: float, client_order_id: Optional[str] = None) -> Fill:
        rules = self.rules_for(symbol)
        _, ask = self.get_bid_ask(symbol)
        free = self.free_balance(self.quote_asset)
        spend = min(quote_amount, free)
        qty = rules.floor_market_qty(to_dec(spend) / to_dec(ask))
        if qty <= 0 or qty < rules.market_min_qty:
            raise OrderError("Miktar LOT_SIZE minimumunun altında", "LOT_SIZE")
        qty_f = float(qty)
        notional = qty_f * ask
        if notional < float(rules.min_notional):
            raise OrderError(f"Emir tutarı ({notional:.2f}) minimumun ({rules.min_notional}) altında", "MIN_NOTIONAL")
        if notional > free + 1e-9:
            raise OrderError("Yetersiz sanal bakiye", "NO_BALANCE")

        fee_base = qty_f * self.fee_rate  # Binance komisyonu alınan coinden keser
        net_qty = qty_f - fee_base
        self.balances[self.quote_asset] = free - notional
        self.balances[rules.base_asset] = self.free_balance(rules.base_asset) + net_qty
        return Fill(symbol, "BUY", net_qty, ask, notional, fee_base * ask, f"PAPER-{int(time.time() * 1000)}")

    def market_sell(self, symbol: str, qty: float) -> Fill:
        bid, _ = self.get_bid_ask(symbol)
        return self._paper_sell(symbol, qty, bid, check_notional=True)

    def _paper_sell(self, symbol: str, qty: float, price: float, check_notional: bool) -> Fill:
        rules = self.rules_for(symbol)
        held = self.free_balance(rules.base_asset)
        q = rules.floor_market_qty(min(qty, held))
        if q <= 0 or q < rules.market_min_qty:
            raise OrderError("Satılacak bakiye yok", "NO_BALANCE")
        q_f = float(q)
        notional = q_f * price
        if check_notional and notional < float(rules.min_notional):
            raise OrderError(f"Satış tutarı ({notional:.2f}) minimumun ({rules.min_notional}) altında", "MIN_NOTIONAL")

        fee = notional * self.fee_rate  # Satış komisyonu USDT'den kesilir
        proceeds = notional - fee
        self.balances[rules.base_asset] = held - q_f
        self.balances[self.quote_asset] = self.free_balance(self.quote_asset) + proceeds
        return Fill(symbol, "SELL", q_f, price, proceeds, fee, f"PAPER-{int(time.time() * 1000)}")

    # --- Yedek stop simülasyonu (gerçek emir yok; tetiklenme fiyat verisinden hesaplanır)
    def place_backup_stop(self, symbol: str, qty: float, stop_price: float, limit_gap_pct: float) -> BackupStop:
        q, stop, _, kind = self.backup_order_params(symbol, qty, stop_price, limit_gap_pct)
        return BackupStop(int(time.time() * 1000), float(stop), float(q), kind)

    def cancel_backup_stop(self, symbol: str, order_id: int) -> BackupStatus:
        return BackupStatus("CANCELED")

    def backup_stop_status(self, symbol: str, order_id: int, qty: float = 0.0,
                           stop_price: float = 0.0) -> BackupStatus:
        """Fiyat stop seviyesine indiyse emir borsada tetiklenmiş gibi satış yapılır."""
        if qty <= 0 or stop_price <= 0:
            return BackupStatus("OPEN")
        bid, _ = self.get_bid_ask(symbol)
        if bid > stop_price:
            return BackupStatus("OPEN")
        try:
            return BackupStatus("FILLED", self._paper_sell(symbol, qty, bid, check_notional=False))
        except OrderError as exc:
            return BackupStatus("GONE", detail=str(exc))

    def check_backup_offline(self, symbol: str, qty: float, stop_price: float, since_sec: float) -> BackupStatus:
        """Bot kapalıyken fiyat yedek stop seviyesine inmiş mi? (5 dk mumlarla geriye dönük kontrol)"""
        try:
            rows = self.client.klines(symbol, "5m", 1000, start_time=int(since_sec * 1000))
        except Exception as exc:  # noqa: BLE001
            return BackupStatus("ERROR", detail=str(exc))
        for row in rows:
            open_, low = float(row[1]), float(row[3])
            if low <= stop_price:
                price = min(stop_price, open_)  # Açılışta zaten altındaysa açılıştan dolar
                try:
                    return BackupStatus("FILLED", self._paper_sell(symbol, qty, price, check_notional=False))
                except OrderError as exc:
                    return BackupStatus("GONE", detail=str(exc))
        return BackupStatus("OPEN")


class LiveExchange(BaseExchange):
    """Gerçek Binance hesabı. DİKKAT: Gerçek para ile emir verir."""

    name = "live"

    def __init__(self, client: BinanceClient, quote_asset: str = "USDT", fee_rate: float = 0.001):
        super().__init__(client, quote_asset, fee_rate)
        self.last_fee_info: Optional[FeeInfo] = None  # Son fee_info() sonucu (BNB komisyon tahmini için)

    def balances(self) -> Dict[str, float]:
        acc = self.client.account()
        return {b["asset"]: float(b["free"]) for b in acc.get("balances", [])}

    def free_balance(self, asset: str) -> float:
        return self.balances().get(asset, 0.0)

    def total_balance(self, asset: str) -> float:
        acc = self.client.account()
        for b in acc.get("balances", []):
            if b.get("asset") == asset:
                return float(b.get("free") or 0) + float(b.get("locked") or 0)
        return 0.0

    def dust_assets(self) -> List[Tuple[str, float, float]]:
        data = self.client.dust_assets() or {}
        return [(str(d.get("asset", "")), float(d.get("amountFree") or 0), float(d.get("toBNB") or 0))
                for d in data.get("details") or [] if d.get("asset")]

    def convert_dust(self, assets: List[str]) -> Tuple[float, float]:
        data = self.client.dust_to_bnb(assets) or {}
        return float(data.get("totalTransfered") or 0), float(data.get("totalServiceCharge") or 0)

    def check_trading_enabled(self) -> None:
        acc = self.client.account()
        if not acc.get("canTrade", False):
            raise OrderError("Bu API anahtarının işlem (Spot Trading) izni yok", "NO_PERMISSION")

    def market_buy(self, symbol: str, quote_amount: float, client_order_id: Optional[str] = None) -> Fill:
        rules = self.rules_for(symbol)
        amount = floor_decimals(quote_amount, 2)
        if amount < float(rules.min_notional):
            raise OrderError(f"Emir tutarı ({amount:.2f}) minimumun ({rules.min_notional}) altında", "MIN_NOTIONAL")
        resp = self._place(symbol, "BUY", quote_order_qty=f"{amount:.2f}", client_id=client_order_id)
        return self._parse_fill(resp, rules, "BUY")

    def market_sell(self, symbol: str, qty: float) -> Fill:
        rules = self.rules_for(symbol)
        free = self.free_balance(rules.base_asset)
        q = rules.floor_market_qty(min(qty, free))
        if q <= 0 or q < rules.market_min_qty:
            raise OrderError("Satılacak bakiye yok", "NO_BALANCE")
        bid, _ = self.get_bid_ask(symbol)
        if float(q) * bid < float(rules.min_notional):
            raise OrderError(f"Satış tutarı ({float(q) * bid:.2f}) minimumun ({rules.min_notional}) altında",
                             "MIN_NOTIONAL")
        resp = self._place(symbol, "SELL", quantity=dec_to_str(q))
        return self._parse_fill(resp, rules, "SELL")

    # --- Yedek zarar-kes emri (Binance'te bekler; bot kapalıyken de korur)
    def place_backup_stop(self, symbol: str, qty: float, stop_price: float, limit_gap_pct: float) -> BackupStop:
        q, stop, limit, kind = self.backup_order_params(symbol, qty, stop_price, limit_gap_pct)
        client_id = f"tbX{int(time.time() * 1000)}"
        try:
            resp = self.client.order_stop_loss(symbol, dec_to_str(q), dec_to_str(stop),
                                               dec_to_str(limit) if limit is not None else None, client_id)
        except (BinanceAPIError, requests.RequestException) as exc:
            if isinstance(exc, BinanceAPIError) and not is_unknown_outcome(exc):
                raise OrderError(f"Yedek stop emri reddedildi: {exc.message}", str(exc.code)) from exc
            time.sleep(2)
            try:
                resp = self.client.get_order(symbol, client_order_id=client_id)
            except Exception as exc2:  # noqa: BLE001
                raise OrderError(f"Yedek stop emrinin durumu belirlenemedi: {exc2}", "UNKNOWN", client_id) from exc
        return BackupStop(int(resp["orderId"]), float(stop), float(q), kind)

    def cancel_backup_stop(self, symbol: str, order_id: int) -> BackupStatus:
        try:
            resp = self.client.cancel_order(symbol, order_id)
        except BinanceAPIError as exc:
            # Emir zaten açık değil (tetiklenip dolmuş veya iptal edilmiş olabilir): durumuna bak
            st = self.backup_stop_status(symbol, order_id)
            return BackupStatus("ERROR", detail=exc.message) if st.status == "OPEN" else st
        except requests.RequestException as exc:
            return BackupStatus("ERROR", detail=str(exc))
        executed = float(resp.get("executedQty") or 0)
        return BackupStatus("CANCELED", self._fill_from_order(symbol, resp) if executed > 0 else None)

    def backup_stop_status(self, symbol: str, order_id: int, qty: float = 0.0,
                           stop_price: float = 0.0) -> BackupStatus:
        try:
            order = self.client.get_order(symbol, order_id=order_id)
        except BinanceAPIError as exc:
            if exc.code == -2013:  # Emir bulunamadı
                return BackupStatus("GONE", detail=exc.message)
            return BackupStatus("ERROR", detail=exc.message)
        except requests.RequestException as exc:
            return BackupStatus("ERROR", detail=str(exc))
        status = order.get("status", "")
        executed = float(order.get("executedQty") or 0)
        fill = self._fill_from_order(symbol, order) if executed > 0 else None
        if status == "FILLED":
            return BackupStatus("FILLED", fill)
        if status in ("NEW", "PARTIALLY_FILLED", "PENDING_NEW"):
            return BackupStatus("OPEN", fill)
        return BackupStatus("GONE", fill, detail=status)

    def cancel_bot_stop_orders(self, symbol: str) -> int:
        """Botun bu paritede açık bıraktığı (tbX ile başlayan) stop emirlerini iptal eder."""
        cancelled = 0
        for order in self.client.open_orders(symbol):
            if str(order.get("clientOrderId", "")).startswith("tbX"):
                try:
                    self.client.cancel_order(symbol, int(order["orderId"]))
                    cancelled += 1
                except BinanceAPIError as exc:
                    log.warning("%s stop emri iptal edilemedi: %s", symbol, exc.message)
        return cancelled

    def _fill_from_order(self, symbol: str, order: dict) -> Fill:
        """Emir özetini (komisyonları işlem geçmişinden alarak) Fill'e çevirir."""
        rules = self.rules_for(symbol)
        order = self._with_fills(symbol, order)
        return self._parse_fill(order, rules, order.get("side", "SELL"))

    def find_sell_fills(self, symbol: str, since_sec: float) -> Optional[Fill]:
        trades = self.client.my_trades(symbol, start_time=int(since_sec * 1000))
        sells = [t for t in trades if not t.get("isBuyer")]
        if not sells:
            return None
        rules = self.rules_for(symbol)
        qty = sum(float(t["qty"]) for t in sells)
        quote = sum(float(t["quoteQty"]) for t in sells)
        fee = 0.0
        prices: Dict[str, float] = {}
        for t in sells:
            c, asset = float(t.get("commission") or 0), t.get("commissionAsset", "")
            if asset == rules.quote_asset:
                fee += c
            elif asset == rules.base_asset:
                fee += c * float(t["price"])
            elif c > 0:
                fee += self._commission_usdt(asset, c, float(t.get("quoteQty") or 0), prices)
        return Fill(symbol, "SELL", qty, quote / qty if qty else 0.0, quote - fee, fee, "EXTERNAL")

    def recover_order(self, symbol: str, client_order_id: str, side: str) -> Optional[Fill]:
        try:
            order = self.client.get_order(symbol, client_order_id=client_order_id)
        except BinanceAPIError as exc:
            if exc.code == -2013:  # Emir Binance'e hiç ulaşmamış
                return None
            raise OrderError(f"Emir sorgulanamadı: {exc.message}", str(exc.code)) from exc
        if float(order.get("executedQty") or 0) <= 0:
            return None
        order.setdefault("side", side)
        return self._fill_from_order(symbol, order)

    def taker_fee_rate(self, symbol: str) -> Optional[float]:
        data = self.client.commission_rates(symbol)
        return float(data["standardCommission"]["taker"])

    def fee_info(self, symbol: str = "BTCUSDT") -> FeeInfo:
        """Komisyon oranı + BNB indirimi durumu + BNB bakiyesi. YALNIZCA OKUR: "BNB ile komisyon öde"
        ayarını değiştirmez ve BNB satın almaz (bu kararlar kullanıcınındır)."""
        data = self.client.commission_rates(symbol)
        bnb_free = self.balances().get(BNB, 0.0)
        bnb_usdt = self._asset_usdt_price(BNB) if bnb_free > 0 else 0.0
        info = parse_fee_info(data, bnb_free, bnb_usdt)
        self.last_fee_info = info
        return info

    def _commission_usdt(self, asset: str, commission: float, notional: float,
                         prices: Dict[str, float]) -> float:
        """Üçüncü bir varlıkla (örn. BNB) ödenen komisyonun USDT karşılığı.

        Fiyat öğrenilemezse 0 yazmak kârı olduğundan yüksek gösterir; bu durumda komisyon işlem
        tutarı × oran ile tahmin edilir (BNB için son okunan indirimli oran, yoksa FEE_RATE)."""
        if asset not in prices:
            prices[asset] = self._asset_usdt_price(asset)
        if prices[asset] > 0:
            return commission * prices[asset]
        info = self.last_fee_info
        rate = info.discounted_rate if asset == BNB and info is not None and info.bnb_enabled else self.fee_rate
        log.debug("%s fiyatı alınamadı; komisyon %.4f × %g ile tahmin edildi", asset, notional, rate)
        return notional * rate

    def _place(self, symbol: str, side: str, quantity: Optional[str] = None,
               quote_order_qty: Optional[str] = None, client_id: Optional[str] = None) -> dict:
        client_id = client_id or f"tb{side[0]}{int(time.time() * 1000)}"
        try:
            return self.client.order_market(symbol, side, quantity=quantity,
                                            quote_order_qty=quote_order_qty, client_order_id=client_id)
        except BinanceAPIError as exc:
            if not is_unknown_outcome(exc):
                raise OrderError(f"Binance emri reddetti: {exc.message}", str(exc.code)) from exc
            return self._resolve_unknown(symbol, client_id, exc)
        except requests.RequestException as exc:
            return self._resolve_unknown(symbol, client_id, exc)

    def _resolve_unknown(self, symbol: str, client_id: str, exc: Exception) -> dict:
        """Emir borsaya ulaşmış olabilir (ağ hatası / 5xx). Çift işlem olmaması için tekrar emir
        GÖNDERMEK yerine emrin durumu sorgulanır; öğrenilemezse UNKNOWN fırlatılır ve bot takip eder."""
        log.warning("Emrin sonucu belirsiz (%s). Emir durumu kontrol ediliyor...", exc)
        time.sleep(2)
        try:
            return self._with_fills(symbol, self.client.get_order(symbol, client_order_id=client_id))
        except Exception as exc2:  # noqa: BLE001
            raise OrderError(f"Emir durumu belirlenemedi, bot tekrar kontrol edecek: {exc2}",
                             "UNKNOWN", client_id) from exc

    def _with_fills(self, symbol: str, order: dict) -> dict:
        """Sorgulanan emir özetine komisyon bilgilerini işlem geçmişinden ekler."""
        order = dict(order)
        if not order.get("fills") and order.get("orderId") is not None and float(order.get("executedQty") or 0) > 0:
            try:
                trades = self.client.my_trades(symbol, order_id=int(order["orderId"]))
                order["fills"] = [{"commission": t.get("commission"), "commissionAsset": t.get("commissionAsset"),
                                   "price": t.get("price"), "qty": t.get("qty")} for t in trades]
            except Exception as exc:  # noqa: BLE001 - komisyon tahmin edilir
                log.debug("İşlem geçmişi okunamadı: %s", exc)
        return order

    def _asset_usdt_price(self, asset: str) -> float:
        if asset == self.quote_asset:
            return 1.0
        try:
            return self.client.price(f"{asset}{self.quote_asset}")
        except Exception:  # noqa: BLE001
            return 0.0

    def _parse_fill(self, resp: dict, rules: SymbolRules, side: str) -> Fill:
        executed = float(resp.get("executedQty") or 0)
        quote = float(resp.get("cummulativeQuoteQty") or 0)
        if executed <= 0:
            raise OrderError(f"Emir gerçekleşmedi (durum: {resp.get('status')})", "NOT_FILLED")
        avg = quote / executed
        order_id = str(resp.get("orderId", ""))

        fills = resp.get("fills") or []
        comm_base = comm_quote = comm_other = 0.0
        prices: Dict[str, float] = {}
        for f in fills:
            c = float(f.get("commission") or 0)
            asset = f.get("commissionAsset", "")
            if c <= 0:
                continue
            if asset == rules.base_asset:
                comm_base += c
            elif asset == rules.quote_asset:
                comm_quote += c
            else:  # Örn. BNB ile ödenmişse USDT karşılığına çevir
                notional = float(f.get("qty") or 0) * float(f.get("price") or 0) or quote / len(fills)
                comm_other += self._commission_usdt(asset, c, notional, prices)

        if not fills:  # Ayrıntı yoksa standart komisyon oranıyla tahmin et
            if side == "BUY":
                comm_base = executed * self.fee_rate
            else:
                comm_quote = quote * self.fee_rate

        if side == "BUY":
            net_qty = executed - comm_base
            cost = quote + comm_quote + comm_other
            fee = comm_base * avg + comm_quote + comm_other
            return Fill(rules.symbol, "BUY", net_qty, avg, cost, fee, order_id)

        fee = comm_base * avg + comm_quote + comm_other
        proceeds = quote - fee
        return Fill(rules.symbol, "SELL", executed, avg, proceeds, fee, order_id)
