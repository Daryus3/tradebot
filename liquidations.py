"""
Binance vadeli işlemler (futures) likidasyon akışı: wss://fstream.binance.com/ws/!forceOrder@arr

Herkese açık bir akıştır (API anahtarı gerekmez). Long pozisyonların zorla kapatılması (SELL) fiyatı aşağı iter;
kısa sürede büyük long likidasyonları "panik satışı / volatilite" işaretidir. Bot bunu yalnızca
  - sinyal onayında (SIGNAL_APPROVAL=volatile) "oynak piyasa" ölçütü olarak ve
  - LIQ_ALERT_USDT > 0 ise bilgi bildirimi olarak
kullanır; tek başına alım yapmaz. Not: Binance bu akışta her parite için saniyede en fazla bir likidasyon gönderir;
toplamlar gerçek likidasyonların ALT sınırıdır. Vadeli işlem sembolü spot karşılığına çevrilir (1000PEPEUSDT → PEPEUSDT).
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

try:
    import websocket as _websocket
except ImportError:  # pragma: no cover - kurulum durumuna bağlı
    _websocket = None

from logger import get_logger

log = get_logger()

LIQ_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"
KEEP_SEC = 900                    # Bu kadar eski kayıtlar silinir
RECONNECT_DELAYS = (2.0, 5.0, 15.0, 30.0, 60.0)


def spot_symbol(futures_symbol: str) -> str:
    for prefix in ("1000000", "1000"):
        rest = futures_symbol[len(prefix):]
        if futures_symbol.startswith(prefix) and len(rest) > 4 and not rest[0].isdigit():
            return rest
    return futures_symbol


class LiquidationFeed:
    def __init__(self, url: str = LIQ_URL, ws_factory: Optional[Callable] = None, time_fn=time.time):
        self.url = url
        self.time_fn = time_fn
        self._factory = ws_factory
        self._events: Dict[str, Deque[Tuple[float, str, float]]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self.connected = False

    @property
    def available(self) -> bool:
        return self._factory is not None or _websocket is not None

    def start(self) -> None:
        if not self.available or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="liquidations", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ okuma
    def recent(self, symbol: str, seconds: float = 300) -> Tuple[float, float]:
        """Son `seconds` saniyedeki likidasyonlar (USDT): (long'lar = zorla satış, short'lar = zorla alış)."""
        since = self.time_fn() - seconds
        long_usdt = short_usdt = 0.0
        with self._lock:
            for t, side, usdt in self._events.get(symbol, ()):
                if t < since:
                    continue
                if side == "SELL":
                    long_usdt += usdt
                else:
                    short_usdt += usdt
        return long_usdt, short_usdt

    def spikes(self, min_long_usdt: float, seconds: float = 300) -> List[Tuple[str, float, float]]:
        """Son `seconds` saniyede long likidasyonu `min_long_usdt`'yi aşan pariteler (büyükten küçüğe)."""
        with self._lock:
            symbols = list(self._events)
        out = []
        for symbol in symbols:
            long_usdt, short_usdt = self.recent(symbol, seconds)
            if long_usdt >= min_long_usdt:
                out.append((symbol, long_usdt, short_usdt))
        return sorted(out, key=lambda x: -x[1])

    # ------------------------------------------------------------ akış
    def handle_message(self, message) -> None:
        try:
            data = json.loads(message)
            order = data.get("o") or {}
            symbol = spot_symbol(str(order["s"]))
            price = float(order.get("ap") or order.get("p") or 0)
            qty = float(order.get("z") or order.get("q") or 0)
            side = str(order.get("S", ""))
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        if price <= 0 or qty <= 0 or side not in ("BUY", "SELL"):
            return
        now = self.time_fn()
        with self._lock:
            events = self._events.setdefault(symbol, deque())
            events.append((now, side, price * qty))
            while events and events[0][0] < now - KEEP_SEC:
                events.popleft()

    def _make_ws(self):
        def on_message(*args):
            self.handle_message(args[-1])

        def on_open(*_args):
            self.connected = True
            log.info("Likidasyon akışına bağlanıldı")

        def on_close(*_args):
            self.connected = False

        def on_error(*args):
            log.debug("Likidasyon akışı hatası: %s", args[-1])

        if self._factory is not None:
            return self._factory(self.url, on_open, on_message, on_error, on_close)
        return _websocket.WebSocketApp(self.url, on_open=on_open, on_message=on_message, on_error=on_error,
                                       on_close=on_close)

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._ws = self._make_ws()
                self._ws.run_forever(ping_interval=60, ping_timeout=20)
            except Exception as exc:  # noqa: BLE001 - akış asla ölmesin
                log.debug("Likidasyon akışı koptu: %s", exc)
            self.connected = False
            if self._stop.is_set():
                break
            attempt = 0 if time.monotonic() - started > 120 else attempt + 1
            self._stop.wait(RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)])
