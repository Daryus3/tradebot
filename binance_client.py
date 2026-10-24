"""
Binance Spot REST API için hafif bir istemci.

Ek bir Binance kütüphanesine ihtiyaç duymadan yalnızca `requests` ile çalışır.
Herkese açık uç noktalar (fiyat, mum verisi) API anahtarı gerektirmez;
bakiye ve emir işlemleri HMAC-SHA256 ile imzalanır.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from logger import get_logger

log = get_logger()
SLOW_REQUEST_SEC = 5.0   # Bundan uzun süren Binance isteği loga yazılır (yavaş sunucu / ağ teşhisi için)


class BinanceAPIError(Exception):
    """Binance'in döndürdüğü hata (HTTP durum kodu + Binance hata kodu)."""

    def __init__(self, status: int, code: Optional[int], message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"Binance hatası (HTTP {status}, kod {code}): {message}")


class RateLimitError(BinanceAPIError):
    """İstek limiti aşıldı (HTTP 429/418). `retry_after` saniye beklenmeli."""

    def __init__(self, status: int, retry_after: int, message: str):
        super().__init__(status, None, message)
        self.retry_after = retry_after


class BinanceClient:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://api.binance.com",
        timeout: float = 10.0,
        recv_window: int = 10_000,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.recv_window = recv_window
        self.time_offset_ms = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "tradebot/1.0"})
        if api_key:
            self.session.headers["X-MBX-APIKEY"] = api_key

    # ------------------------------------------------------------------ altyapı
    def sync_time(self) -> int:
        """Bilgisayar saati ile Binance sunucu saati arasındaki farkı hesaplar.

        Windows saatleri sık sık birkaç saniye kayar; bu fark düzeltilmezse
        imzalı istekler -1021 (Timestamp) hatası alır.
        """
        t0 = time.time() * 1000
        server = self.server_time()
        t1 = time.time() * 1000
        self.time_offset_ms = int(server - (t0 + t1) / 2)
        return self.time_offset_ms

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 signed: bool = False, _retry: bool = True) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            if not (self.api_key and self.api_secret):
                raise BinanceAPIError(0, None, "Binance API anahtarı/secret tanımlı değil (.env dosyasını kontrol edin)")
            params["timestamp"] = int(time.time() * 1000 + self.time_offset_ms)
            params["recvWindow"] = self.recv_window
            query = urlencode(params)
            signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query = f"{query}&signature={signature}"
        else:
            query = urlencode(params)

        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        started = time.monotonic()
        resp = self.session.request(method, url, timeout=self.timeout)
        elapsed = time.monotonic() - started
        if elapsed > SLOW_REQUEST_SEC:   # Yalnızca yol yazılır (sorgu imza / anahtar içerebilir)
            log.warning("Binance yavaş yanıt verdi: %s %.1f sn", path, elapsed)

        if resp.status_code in (418, 429):
            retry_after = int(resp.headers.get("Retry-After", "60") or 60)
            raise RateLimitError(resp.status_code, retry_after, "İstek limiti aşıldı, bekleniyor")

        try:
            data = resp.json()
        except ValueError:
            data = None

        if resp.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg") if isinstance(data, dict) else resp.text[:300]
            if code == -1021 and signed and _retry:
                # Saat farkı: yeniden senkronize et ve bir kez daha dene
                self.sync_time()
                return self._request(method, path, params={k: v for k, v in params.items()
                                                           if k not in ("timestamp", "recvWindow")},
                                     signed=True, _retry=False)
            raise BinanceAPIError(resp.status_code, code, msg or "Bilinmeyen hata")
        return data

    # ---------------------------------------------------------- herkese açık
    def ping(self) -> bool:
        self._request("GET", "/api/v3/ping")
        return True

    def server_time(self) -> int:
        return int(self._request("GET", "/api/v3/time")["serverTime"])

    def exchange_info(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/exchangeInfo")

    def tickers_24h(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/v3/ticker/24hr")

    def klines(self, symbol: str, interval: str = "5m", limit: int = 100,
               start_time: Optional[int] = None, end_time: Optional[int] = None) -> List[list]:
        return self._request("GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit,
                                                       "startTime": start_time, "endTime": end_time})

    def book_ticker(self, symbol: str) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})

    def price(self, symbol: str) -> float:
        return float(self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"])

    # ---------------------------------------------------------------- imzalı
    def account(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)

    def api_restrictions(self) -> Dict[str, Any]:
        return self._request("GET", "/sapi/v1/account/apiRestrictions", signed=True)

    def order_market(self, symbol: str, side: str, quantity: Optional[str] = None,
                     quote_order_qty: Optional[str] = None,
                     client_order_id: Optional[str] = None) -> Dict[str, Any]:
        """Piyasa emri. Alışta `quote_order_qty` (harcanacak USDT), satışta `quantity` kullanılır."""
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "quoteOrderQty": quote_order_qty,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        return self._request("POST", "/api/v3/order", params, signed=True)

    def get_order(self, symbol: str, client_order_id: Optional[str] = None,
                  order_id: Optional[int] = None) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/order",
                             {"symbol": symbol, "origClientOrderId": client_order_id, "orderId": order_id},
                             signed=True)

    def order_stop_loss(self, symbol: str, quantity: str, stop_price: str, limit_price: Optional[str] = None,
                        client_order_id: Optional[str] = None) -> Dict[str, Any]:
        """Borsada bekleyen satış stop emri. limit_price verilirse STOP_LOSS_LIMIT, yoksa STOP_LOSS (piyasa)."""
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP_LOSS_LIMIT" if limit_price else "STOP_LOSS",
            "quantity": quantity,
            "stopPrice": stop_price,
            "price": limit_price,
            "timeInForce": "GTC" if limit_price else None,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        return self._request("POST", "/api/v3/order", params, signed=True)

    def open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True)

    def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        return self._request("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def my_trades(self, symbol: str, start_time: Optional[int] = None, order_id: Optional[int] = None,
                  limit: int = 500) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/v3/myTrades", {"symbol": symbol, "startTime": start_time,
                                                         "orderId": order_id, "limit": limit}, signed=True)

    def commission_rates(self, symbol: str) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/account/commission", {"symbol": symbol}, signed=True)
