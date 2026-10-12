"""Canlı mod testleri için sahte Binance hesabı: bakiyeler, piyasa ve stop emirleri, işlem geçmişi.

İnternet gerektirmez. Gerçek API yanıt biçimlerini taklit eder.
"""
import itertools
import time

import requests

from binance_client import BinanceAPIError


class FakeBinance:
    FEE = 0.001

    def __init__(self, prices, usdt=11.0, order_types=("LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT")):
        self.prices = dict(prices)
        self.free = {"USDT": usdt}
        self.locked = {}
        self.orders = {}          # orderId -> emir sözlüğü
        self.trades = []          # myTrades benzeri kayıtlar
        self.order_types = list(order_types)
        self.api_key = "key"
        self.time_offset_ms = 0
        self._ids = itertools.count(1000)
        self.fail_next_order_network = False   # Bir sonraki emirde ağ hatası (emir yine de gerçekleşir)
        self.fail_get_order = False
        self.taker = 0.001

    # ------------------------------------------------------------ herkese açık
    def sync_time(self):
        return 0

    def exchange_info(self):
        return {"symbols": [{
            "symbol": s, "status": "TRADING", "baseAsset": s[:-4], "quoteAsset": "USDT",
            "isSpotTradingAllowed": True, "orderTypes": self.order_types, "quoteOrderQtyMarketAllowed": True,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.00010000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01000000", "minQty": "0.01000000", "maxQty": "9000000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00000000", "minQty": "0", "maxQty": "100000"},
                {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
            ]} for s in self.prices]}

    def book_ticker(self, symbol):
        p = self.prices[symbol]
        return {"bidPrice": str(p), "askPrice": str(p)}

    def price(self, symbol):
        if symbol not in self.prices:
            raise BinanceAPIError(400, -1121, "Invalid symbol.")
        return self.prices[symbol]

    def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
        p = self.prices.get(symbol, 1.0)
        return [[i * 60_000, str(p), str(p), str(p), str(p), "0", i * 60_000 + 59_999, "0", 0, "0", "0", "0"]
                for i in range(limit)]

    # ------------------------------------------------------------ hesap
    def account(self):
        assets = set(self.free) | set(self.locked)
        return {"canTrade": True, "balances": [
            {"asset": a, "free": f"{self.free.get(a, 0):.8f}", "locked": f"{self.locked.get(a, 0):.8f}"}
            for a in sorted(assets)]}

    def commission_rates(self, symbol):
        return {"standardCommission": {"maker": "0.001", "taker": str(self.taker)}}

    def _add(self, book, asset, amount):
        book[asset] = book.get(asset, 0.0) + amount

    def _record_trade(self, order, qty, price, is_buyer, commission, commission_asset):
        self.trades.append({"symbol": order["symbol"], "orderId": order["orderId"], "price": str(price),
                            "qty": f"{qty:.8f}", "quoteQty": f"{qty * price:.8f}", "commission": f"{commission:.8f}",
                            "commissionAsset": commission_asset, "isBuyer": is_buyer, "time": int(time.time() * 1000)})

    def order_market(self, symbol, side, quantity=None, quote_order_qty=None, client_order_id=None):
        price = self.prices[symbol]
        base = symbol[:-4]
        oid = next(self._ids)
        if side == "BUY":
            qty = int(float(quote_order_qty) / price / 0.01) * 0.01
            if qty * price > self.free.get("USDT", 0) + 1e-9:
                raise BinanceAPIError(400, -2010, "Account has insufficient balance for requested action.")
            fee = qty * self.FEE
            self._add(self.free, "USDT", -qty * price)
            self._add(self.free, base, qty - fee)
            comm, comm_asset = fee, base
        else:
            qty = float(quantity)
            if qty > self.free.get(base, 0) + 1e-9:
                raise BinanceAPIError(400, -2010, "Account has insufficient balance for requested action.")
            fee = qty * price * self.FEE
            self._add(self.free, base, -qty)
            self._add(self.free, "USDT", qty * price - fee)
            comm, comm_asset = fee, "USDT"
        order = {"symbol": symbol, "orderId": oid, "clientOrderId": client_order_id or f"x{oid}", "side": side,
                 "type": "MARKET", "status": "FILLED", "executedQty": f"{qty:.8f}",
                 "cummulativeQuoteQty": f"{qty * price:.8f}"}
        self.orders[oid] = order
        self._record_trade(order, qty, price, side == "BUY", comm, comm_asset)
        resp = dict(order, fills=[{"price": str(price), "qty": f"{qty:.8f}", "commission": f"{comm:.8f}",
                                   "commissionAsset": comm_asset}])
        if self.fail_next_order_network:
            self.fail_next_order_network = False
            raise requests.ConnectionError("bağlantı koptu")
        return resp

    def order_stop_loss(self, symbol, quantity, stop_price, limit_price=None, client_order_id=None):
        base = symbol[:-4]
        qty = float(quantity)
        if qty > self.free.get(base, 0) + 1e-9:
            raise BinanceAPIError(400, -2010, "Account has insufficient balance for requested action.")
        self._add(self.free, base, -qty)
        self._add(self.locked, base, qty)
        oid = next(self._ids)
        self.orders[oid] = {"symbol": symbol, "orderId": oid, "clientOrderId": client_order_id, "side": "SELL",
                            "type": "STOP_LOSS_LIMIT" if limit_price else "STOP_LOSS", "status": "NEW",
                            "origQty": quantity, "executedQty": "0", "cummulativeQuoteQty": "0",
                            "stopPrice": stop_price, "price": limit_price or "0"}
        return {"orderId": oid, "symbol": symbol, "status": "NEW"}

    def trigger_stops(self):
        """Fiyat stop seviyesinin altındaysa borsa stop emirlerini çalıştırır."""
        for order in self.orders.values():
            if order["status"] != "NEW" or not order["type"].startswith("STOP_LOSS"):
                continue
            symbol = order["symbol"]
            price = self.prices[symbol]
            if price > float(order["stopPrice"]):
                continue
            base, qty = symbol[:-4], float(order["origQty"])
            fee = qty * price * self.FEE
            self._add(self.locked, base, -qty)
            self._add(self.free, "USDT", qty * price - fee)
            order.update(status="FILLED", executedQty=f"{qty:.8f}", cummulativeQuoteQty=f"{qty * price:.8f}")
            self._record_trade(order, qty, price, False, fee, "USDT")

    def cancel_order(self, symbol, order_id):
        order = self.orders.get(order_id)
        if order is None or order["status"] != "NEW":
            raise BinanceAPIError(400, -2011, "Unknown order sent.")
        base = symbol[:-4]
        qty = float(order["origQty"]) - float(order["executedQty"])
        self._add(self.locked, base, -qty)
        self._add(self.free, base, qty)
        order["status"] = "CANCELED"
        return dict(order)

    def get_order(self, symbol, client_order_id=None, order_id=None):
        if self.fail_get_order:
            raise requests.ConnectionError("hala bağlantı yok")
        for order in self.orders.values():
            if (order_id is not None and order["orderId"] == order_id) or \
                    (client_order_id is not None and order["clientOrderId"] == client_order_id):
                return dict(order)
        raise BinanceAPIError(400, -2013, "Order does not exist.")

    def open_orders(self, symbol):
        return [dict(o) for o in self.orders.values() if o["symbol"] == symbol and o["status"] == "NEW"]

    def my_trades(self, symbol, start_time=None, order_id=None, limit=500):
        out = [t for t in self.trades if t["symbol"] == symbol]
        if order_id is not None:
            out = [t for t in out if t["orderId"] == order_id]
        if start_time is not None:
            out = [t for t in out if t["time"] >= start_time]
        return out

    # Elle (bot dışı) satış: kullanıcı Binance uygulamasından satmış gibi
    def manual_sell_all(self, symbol):
        base = symbol[:-4]
        for order in list(self.orders.values()):
            if order["symbol"] == symbol and order["status"] == "NEW":
                self.cancel_order(symbol, order["orderId"])
        qty = int(self.free.get(base, 0) / 0.01) * 0.01
        return self.order_market(symbol, "SELL", quantity=f"{qty:.2f}")
