import hashlib
import hmac
from urllib.parse import parse_qsl, urlsplit

import pytest

from binance_client import BinanceAPIError, BinanceClient


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.headers = {}
        self.text = str(self._data)

    def json(self):
        return self._data


def test_signed_request_signature(monkeypatch):
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    client = BinanceClient("key", secret)
    captured = {}

    def fake_request(method, url, timeout):
        captured["method"], captured["url"] = method, url
        return FakeResp(200, {"orderId": 1})

    monkeypatch.setattr(client.session, "request", fake_request)
    client.order_market("LTCUSDT", "BUY", quote_order_qty="11.00")

    query = urlsplit(captured["url"]).query
    unsigned, signature = query.rsplit("&signature=", 1)
    assert signature == hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    params = dict(parse_qsl(unsigned))
    assert params["quoteOrderQty"] == "11.00" and "quantity" not in params
    assert captured["method"] == "POST" and client.session.headers["X-MBX-APIKEY"] == "key"


def test_timestamp_error_resyncs_once(monkeypatch):
    client = BinanceClient("key", "secret")
    calls = []

    def fake_request(method, url, timeout):
        calls.append(url)
        if "/api/v3/time" in url:
            return FakeResp(200, {"serverTime": 1})
        if len([c for c in calls if "account" in c]) == 1:
            return FakeResp(400, {"code": -1021, "msg": "Timestamp outside recvWindow"})
        return FakeResp(200, {"balances": []})

    monkeypatch.setattr(client.session, "request", fake_request)
    assert client.account() == {"balances": []}
    assert len([c for c in calls if "account" in c]) == 2


def test_api_error_raised(monkeypatch):
    client = BinanceClient("key", "secret")
    monkeypatch.setattr(client.session, "request",
                        lambda m, u, timeout: FakeResp(400, {"code": -2010, "msg": "insufficient balance"}))
    with pytest.raises(BinanceAPIError) as err:
        client.account()
    assert err.value.code == -2010
