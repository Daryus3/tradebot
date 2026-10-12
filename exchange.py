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
from typing import Dict, Optional, Tuple

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

    def floor_market_qty(self, qty) -> Decimal:
        q = floor_to_step(to_dec(qty), self.market_step_size)
        if self.market_max_qty > 0 and q > self.market_max_qty:
            q = floor_to_step(self.market_max_qty, self.market_step_size)
        return max(q, Decimal(0))

    def step_value(self, price: float) -> float:
        """Bir LOT adımının USDT değeri (satışta kalabilecek en büyük 'toz')."""
        return float(self.market_step_size) * price


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
        )
    return rules


class OrderError(Exception):
    """Emir verilemedi/reddedildi. `code`: NO_BALANCE, MIN_NOTIONAL, LOT_SIZE, NOT_FILLED, Binance kodu..."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


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

    # Alt sınıfların doldurduğu metotlar
    def free_balance(self, asset: str) -> float:
        raise NotImplementedError

    def market_buy(self, symbol: str, quote_amount: float) -> Fill:
        raise NotImplementedError

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

    def market_buy(self, symbol: str, quote_amount: float) -> Fill:
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
        rules = self.rules_for(symbol)
        bid, _ = self.get_bid_ask(symbol)
        held = self.free_balance(rules.base_asset)
        q = rules.floor_market_qty(min(qty, held))
        if q <= 0 or q < rules.market_min_qty:
            raise OrderError("Satılacak bakiye yok", "NO_BALANCE")
        q_f = float(q)
        notional = q_f * bid
        if notional < float(rules.min_notional):
            raise OrderError(f"Satış tutarı ({notional:.2f}) minimumun ({rules.min_notional}) altında", "MIN_NOTIONAL")

        fee = notional * self.fee_rate  # Satış komisyonu USDT'den kesilir
        proceeds = notional - fee
        self.balances[rules.base_asset] = held - q_f
        self.balances[self.quote_asset] = self.free_balance(self.quote_asset) + proceeds
        return Fill(symbol, "SELL", q_f, bid, proceeds, fee, f"PAPER-{int(time.time() * 1000)}")


class LiveExchange(BaseExchange):
    """Gerçek Binance hesabı. DİKKAT: Gerçek para ile emir verir."""

    name = "live"

    def balances(self) -> Dict[str, float]:
        acc = self.client.account()
        return {b["asset"]: float(b["free"]) for b in acc.get("balances", [])}

    def free_balance(self, asset: str) -> float:
        return self.balances().get(asset, 0.0)

    def check_trading_enabled(self) -> None:
        acc = self.client.account()
        if not acc.get("canTrade", False):
            raise OrderError("Bu API anahtarının işlem (Spot Trading) izni yok", "NO_PERMISSION")

    def market_buy(self, symbol: str, quote_amount: float) -> Fill:
        rules = self.rules_for(symbol)
        amount = floor_decimals(quote_amount, 2)
        if amount < float(rules.min_notional):
            raise OrderError(f"Emir tutarı ({amount:.2f}) minimumun ({rules.min_notional}) altında", "MIN_NOTIONAL")
        resp = self._place(symbol, "BUY", quote_order_qty=f"{amount:.2f}")
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

    def _place(self, symbol: str, side: str, quantity: Optional[str] = None,
               quote_order_qty: Optional[str] = None) -> dict:
        client_id = f"tb{side[0]}{int(time.time() * 1000)}"
        try:
            return self.client.order_market(symbol, side, quantity=quantity,
                                            quote_order_qty=quote_order_qty, client_order_id=client_id)
        except BinanceAPIError as exc:
            raise OrderError(f"Binance emri reddetti: {exc.message}", str(exc.code)) from exc
        except requests.RequestException as exc:
            # Ağ hatası: emir borsaya ulaşmış olabilir. Çift işlem olmaması için
            # tekrar emir GÖNDERMEK yerine emrin durumunu sorguluyoruz.
            log.warning("Emir sırasında ağ hatası (%s). Emir durumu kontrol ediliyor...", exc)
            time.sleep(2)
            try:
                return self.client.get_order(symbol, client_id)
            except Exception as exc2:  # noqa: BLE001
                raise OrderError(f"Emir durumu belirlenemedi, Binance'i kontrol edin: {exc2}", "UNKNOWN") from exc

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
                comm_other += c * self._asset_usdt_price(asset)

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
