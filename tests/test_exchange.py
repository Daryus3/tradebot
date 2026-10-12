import pytest

from exchange import PaperExchange, dec_to_str, floor_to_step, parse_symbol_rules, OrderError
from decimal import Decimal


def test_parse_rules_fallbacks(fake_client):
    rules = parse_symbol_rules(fake_client({"ABCUSDT": 1.0}).exchange_info())
    r = rules["ABCUSDT"]
    assert r.market_step_size == Decimal("0.01")  # MARKET_LOT_SIZE 0 ise LOT_SIZE kullanılır
    assert r.min_notional == Decimal("5")
    assert r.market_max_qty == Decimal("100000")


def test_decimal_helpers():
    assert floor_to_step(Decimal("12.3456"), Decimal("0.01")) == Decimal("12.34")
    assert dec_to_str(Decimal("100.000")) == "100"
    assert dec_to_str(Decimal("0.00010000")) == "0.0001"


def test_paper_round_trip_fees(fake_client):
    client = fake_client({"ABCUSDT": 0.5})
    balances = {"USDT": 11.0}
    ex = PaperExchange(client, balances)
    ex.refresh_rules()

    buy = ex.market_buy("ABCUSDT", 11.0)
    assert buy.usdt == pytest.approx(11.0)
    assert buy.qty == pytest.approx(22.0 * 0.999)        # Komisyon coinden kesildi
    assert balances["USDT"] == pytest.approx(0.0)

    client.prices["ABCUSDT"] = 0.6
    sell = ex.market_sell("ABCUSDT", buy.qty)
    assert sell.qty == pytest.approx(21.97)              # LOT adımına yuvarlandı (toz kaldı)
    assert sell.usdt == pytest.approx(21.97 * 0.6 * 0.999)
    assert balances["USDT"] == pytest.approx(sell.usdt)
    assert balances["ABC"] == pytest.approx(22 * 0.999 - 21.97)


def test_paper_min_notional(fake_client):
    client = fake_client({"ABCUSDT": 1.0})
    ex = PaperExchange(client, {"USDT": 4.0})
    ex.refresh_rules()
    with pytest.raises(OrderError) as err:
        ex.market_buy("ABCUSDT", 4.0)
    assert err.value.code == "MIN_NOTIONAL"


class FakeLiveClient:
    """Binance'in FULL emir yanıtlarını taklit eder."""

    def __init__(self, base_client, fail_network=False):
        self.base = base_client
        self.orders = []
        self.fail_network = fail_network
        self.free = {"USDT": 11.0, "ABC": 0.0}

    def __getattr__(self, name):
        return getattr(self.base, name)

    def account(self):
        return {"canTrade": True, "balances": [{"asset": a, "free": str(v), "locked": "0"} for a, v in self.free.items()]}

    def order_market(self, symbol, side, quantity=None, quote_order_qty=None, client_order_id=None):
        self.orders.append((side, quantity, quote_order_qty, client_order_id))
        price = self.base.prices[symbol]
        if side == "BUY":
            qty = round(int(float(quote_order_qty) / price / 0.01) * 0.01, 8)
            resp = {"orderId": 1, "status": "FILLED", "executedQty": f"{qty:.8f}",
                    "cummulativeQuoteQty": f"{qty * price:.8f}",
                    "fills": [{"price": str(price), "qty": f"{qty:.8f}", "commission": f"{qty * 0.001:.8f}",
                               "commissionAsset": "ABC"}]}
        else:
            q = float(quantity)
            resp = {"orderId": 2, "status": "FILLED", "executedQty": quantity,
                    "cummulativeQuoteQty": f"{q * price:.8f}",
                    "fills": [{"price": str(price), "qty": quantity, "commission": f"{q * price * 0.001:.8f}",
                               "commissionAsset": "USDT"}]}
        if self.fail_network:
            self.last = resp
            import requests
            raise requests.ConnectionError("bağlantı koptu")
        return resp

    def get_order(self, symbol, client_order_id):
        resp = dict(self.last)
        resp.pop("fills")
        return resp


def test_live_fill_parsing(fake_client):
    from exchange import LiveExchange
    client = FakeLiveClient(fake_client({"ABCUSDT": 0.5}))
    ex = LiveExchange(client)
    ex.refresh_rules()

    buy = ex.market_buy("ABCUSDT", 10.999)
    assert client.orders[-1][2] == "10.99"               # 2 ondalığa aşağı yuvarlandı
    assert buy.qty == pytest.approx(21.98 * 0.999)
    assert buy.usdt == pytest.approx(10.99)
    assert buy.fee_usdt == pytest.approx(21.98 * 0.001 * 0.5)

    client.free["ABC"] = buy.qty
    sell = ex.market_sell("ABCUSDT", buy.qty)
    assert client.orders[-1][1] == "21.95"               # Toz atıldı, adım katı
    assert sell.usdt == pytest.approx(21.95 * 0.5 * 0.999)


def test_live_network_error_does_not_double_order(fake_client, monkeypatch):
    from exchange import LiveExchange
    monkeypatch.setattr("exchange.time.sleep", lambda s: None)
    client = FakeLiveClient(fake_client({"ABCUSDT": 0.5}), fail_network=True)
    ex = LiveExchange(client)
    ex.refresh_rules()
    buy = ex.market_buy("ABCUSDT", 11.0)
    assert len(client.orders) == 1                       # Tekrar emir gönderilmedi
    assert buy.qty == pytest.approx(22.0 * 0.999)        # Komisyon standart oranla tahmin edildi


def test_live_sell_without_balance(fake_client):
    from exchange import LiveExchange
    client = FakeLiveClient(fake_client({"ABCUSDT": 0.5}))
    ex = LiveExchange(client)
    ex.refresh_rules()
    with pytest.raises(OrderError) as err:
        ex.market_sell("ABCUSDT", 20.0)
    assert err.value.code == "NO_BALANCE"
