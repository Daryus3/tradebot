"""
Anlık fiyat akışı (Binance WebSocket, bookTicker).

Açık pozisyonların en iyi alış/satış (bid/ask) fiyatlarını REST'e gitmeden,
anlık olarak takip etmek için kullanılır. Arka planda iki daemon iş parçacığı çalışır:

* ``price-stream``      : bağlantıyı açar/tutar; koparsa artan beklemelerle (1, 2, 5, 10, 30 sn)
  yeniden bağlanır.
* ``price-stream-sync`` : istenen sembollerle abone olunanları eşitler (SUBSCRIBE/UNSUBSCRIBE),
  kontrol mesajlarını Binance sınırının (saniyede 5) altında tutar ve sessiz kalan bağlantıyı yeniler.

Veri yoksa veya bayatsa ``get_bid_ask`` None döndürür; çağıran taraf REST'e (book_ticker) düşmelidir.
Hiçbir WebSocket geri çağrısı (callback) exception fırlatmaz.
"""
from __future__ import annotations

import inspect
import json
import logging
import math
import threading
import time
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

try:  # websocket-client kurulu değilse bot REST ile çalışmaya devam eder
    import websocket as _websocket
except ImportError:  # pragma: no cover - kurulum durumuna bağlı
    _websocket = None

from logger import get_logger

DEFAULT_URL = "wss://stream.binance.com:9443/ws"
RECONNECT_DELAYS: Tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)
MAX_STREAMS = 1024          # Binance: bağlantı başına en fazla 1024 akış
STREAM_SUFFIX = "@bookTicker"

WsFactory = Callable[[str, Callable, Callable, Callable, Callable], Any]


def _default_factory(url: str, on_open, on_message, on_error, on_close):
    """Gerçek websocket-client bağlantısı (geri çağrılar ws nesnesini ilk argüman olarak alır)."""
    return _websocket.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                   on_error=on_error, on_close=on_close)


def _chunks(items: List[str], size: int) -> List[List[str]]:
    size = max(1, int(size))
    return [items[i:i + size] for i in range(0, len(items), size)]


class PriceStream:
    """Binance bookTicker akışından anlık bid/ask fiyatlarını tutar (thread-safe).

    ``ws_factory(url, on_open, on_message, on_error, on_close)`` ``run_forever()``, ``send(str)`` ve
    ``close()`` metotları olan bir nesne döndürmelidir. Geri çağrılar websocket-client düzeniyle
    çağrılır: ``on_open(ws)``, ``on_message(ws, mesaj)``, ``on_error(ws, hata)``,
    ``on_close(ws, kod, neden)``. None ise ``websocket.WebSocketApp`` kullanılır.
    """

    def __init__(self, url: str = DEFAULT_URL, stale_after: Optional[float] = 5.0,
                 ws_factory: Optional[WsFactory] = None, logger: Optional[logging.Logger] = None,
                 time_fn: Callable[[], float] = time.time):
        self.url = url
        self.stale_after = stale_after      # Bundan eski fiyat "bayat" sayılır (None/<=0 = kapalı)
        self.time_fn = time_fn
        self.log = logger or get_logger()
        self._factory = ws_factory

        # İnce ayarlar (testler küçültebilir)
        self.reconnect_delays: Tuple[float, ...] = RECONNECT_DELAYS
        self.control_interval = 0.25        # Kontrol mesajları arası en az süre (Binance: sn'de en fazla 5)
        self.max_params_per_message = 100   # Tek SUBSCRIBE/UNSUBSCRIBE mesajındaki en fazla akış
        self.healthy_after = 60.0           # Bu kadar ayakta kalan bağlantıdan sonra bekleme baştan başlar
        self.idle_reconnect_sec = 90.0      # Abone olunduğu halde bu kadar sn hiç mesaj gelmezse yeniden bağlan (0 = kapalı)
        self.ping_interval = 30.0           # Yalnızca gerçek WebSocketApp için (ölü bağlantıyı yakalar)
        self.ping_timeout = 10.0
        self.log_interval = 60.0            # Tekrarlayan uyarılar en fazla bu sıklıkta loglanır

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sync_thread: Optional[threading.Thread] = None

        self._ws: Any = None
        self._gen = 0                       # Her bağlantı denemesinde artar; eski bağlantının geri çağrıları yok sayılır
        self._connected = False
        self._opened_mono: Optional[float] = None
        self._last_healthy = False
        self._failures = 0
        self._connect_count = 0

        self._wanted: Set[str] = set()      # İstenen semboller (BÜYÜK harf)
        self._subscribed: Set[str] = set()  # Mevcut bağlantıda abone olunanlar
        self._quotes: Dict[str, Tuple[float, float, float]] = {}  # sembol -> (bid, ask, zaman)
        self._last_message_at: Optional[float] = None
        self._last_sub_at = 0.0
        self._msg_id = 0
        self._last_control_mono = 0.0
        self._last_log: Dict[str, float] = {}
        self._bad_messages = 0

    # ================================================================ durum
    @property
    def available(self) -> bool:
        """websocket-client kullanılabilir mi (veya test için bağlantı fabrikası verilmiş mi)."""
        if self._factory is not None:
            return True
        return _websocket is not None and hasattr(_websocket, "WebSocketApp")

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def last_message_at(self) -> Optional[float]:
        """Son mesajın alındığı zaman (time_fn cinsinden); hiç mesaj gelmediyse None."""
        with self._lock:
            return self._last_message_at

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive() and not self._stop.is_set()

    @property
    def symbols(self) -> FrozenSet[str]:
        with self._lock:
            return frozenset(self._wanted)

    @property
    def connect_count(self) -> int:
        """Başarılı açılış sayısı (ilk bağlantı dahil)."""
        with self._lock:
            return self._connect_count

    @property
    def bad_messages(self) -> int:
        return self._bad_messages

    # ================================================================ yaşam döngüsü
    def start(self) -> bool:
        """Arka plan iş parçacıklarını başlatır. websocket-client yoksa False döner (bot REST ile devam eder)."""
        if not self.available:
            self._log_throttled(logging.WARNING, "unavailable",
                                "⚠️ websocket-client kurulu değil; anlık fiyat akışı kapalı "
                                "(kurmak için: pip install websocket-client)")
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._wake.clear()
            self._failures = 0
            self._last_healthy = False
            self._thread = threading.Thread(target=self._run, name="price-stream", daemon=True)
            self._sync_thread = threading.Thread(target=self._sync_loop, name="price-stream-sync", daemon=True)
            self._thread.start()
            self._sync_thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Bağlantıyı kapatır ve iş parçacıklarının bitmesini bekler. Birden çok kez çağrılabilir."""
        with self._lock:
            self._stop.set()
            ws = self._ws
            self._connected = False
        self._wake.set()
        if ws is not None:
            self._close_ws(ws)
        current = threading.current_thread()
        for t in (self._thread, self._sync_thread):
            if t is not None and t.is_alive() and t is not current:
                t.join(timeout)

    # ================================================================ semboller ve fiyatlar
    def set_symbols(self, symbols: Optional[Iterable[str]]) -> None:
        """Takip edilecek sembolleri belirler ('BTCUSDT'). Fark kümesine göre abone olunur/çıkılır.

        Bağlı değilken çağrılırsa liste saklanır ve bağlanınca abone olunur. Boş küme verilirse
        tüm aboneliklerden çıkılır, bağlantı açık kalır.
        """
        if symbols is None:
            symbols = ()
        elif isinstance(symbols, str):
            symbols = (symbols,)
        wanted = {str(s).strip().upper() for s in symbols if s is not None and str(s).strip()}
        if len(wanted) > MAX_STREAMS:
            self.log.warning("⚠️ Fiyat akışı en fazla %d parite izleyebilir; %d parite istendi, fazlası alınmadı",
                             MAX_STREAMS, len(wanted))
            wanted = set(sorted(wanted)[:MAX_STREAMS])
        with self._lock:
            if wanted == self._wanted:
                return
            self._wanted = wanted
            for sym in [s for s in self._quotes if s not in wanted]:
                del self._quotes[sym]
        self._wake.set()

    def get_bid_ask(self, symbol: str, max_age: Optional[float] = None) -> Optional[Tuple[float, float]]:
        """En güncel (bid, ask); veri yoksa veya ``max_age`` (verilmezse ``stale_after``) sn'den eskiyse None."""
        limit = self.stale_after if max_age is None else max_age
        with self._lock:
            quote = self._quotes.get(str(symbol).strip().upper())
        if quote is None:
            return None
        bid, ask, ts = quote
        if limit is not None and limit > 0 and self.time_fn() - ts > limit:
            return None
        return bid, ask

    # ================================================================ bağlantı döngüsü
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect_once()
            except Exception as exc:  # noqa: BLE001 - akış iş parçacığı asla ölmesin
                self._log_throttled(logging.WARNING, "connect", "⚠️ Fiyat akışı bağlantı hatası: %s", exc)
            if self._stop.is_set():
                break
            delay = self._next_delay()
            self._log_throttled(logging.INFO, "reconnect",
                                "🔌 Fiyat akışı bağlantısı yok; %g sn sonra yeniden bağlanılacak", delay)
            if self._wait(delay):
                break
        with self._lock:
            self._connected = False
            self._ws = None

    def _connect_once(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._gen += 1
            gen = self._gen
            self._opened_mono = None
            self._last_healthy = False
        ws = self._make_ws(gen)
        with self._lock:
            stopping = self._stop.is_set() or gen != self._gen
            if not stopping:
                self._ws = ws
        if stopping:
            self._close_ws(ws)
            return
        try:
            ws.run_forever(**self._run_kwargs(ws))
        finally:
            with self._lock:
                if gen == self._gen:
                    opened = self._opened_mono
                    self._last_healthy = opened is not None and time.monotonic() - opened >= self.healthy_after
                    self._ws = None
                    self._connected = False
                    self._subscribed.clear()

    def _make_ws(self, gen: int):
        # Geri çağrılar bağlantı numarasını (gen) yakalar; eski bağlantıdan gelenler yok sayılır.
        def on_open(*args):
            self._safe(self._handle_open, gen)

        def on_message(*args):
            self._safe(self._handle_message, gen, args[-1] if args else None)

        def on_error(*args):
            self._safe(self._handle_error, gen, args[-1] if args else None)

        def on_close(*args):
            code, reason = (args[1], args[2]) if len(args) >= 3 else (None, None)
            self._safe(self._handle_close, gen, code, reason)

        factory = self._factory or _default_factory
        return factory(self.url, on_open, on_message, on_error, on_close)

    def _run_kwargs(self, ws) -> Dict[str, Any]:
        """Gerçek WebSocketApp için run_forever ayarları (kütüphanenin kendi yeniden bağlanması kapalı)."""
        if self._factory is not None:
            return {}
        kwargs: Dict[str, Any] = {"reconnect": 0}
        if self.ping_interval and self.ping_timeout and self.ping_interval > self.ping_timeout:
            kwargs.update(ping_interval=self.ping_interval, ping_timeout=self.ping_timeout)
        try:
            params = inspect.signature(ws.run_forever).parameters
        except (TypeError, ValueError):
            return {}
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return kwargs
        return {k: v for k, v in kwargs.items() if k in params}

    def _next_delay(self) -> float:
        """Artan bekleme: 1, 2, 5, 10, 30, 30… sn. Sağlıklı bağlantıdan sonra baştan başlar."""
        if self._last_healthy:
            self._failures = 0
        delays = self.reconnect_delays or RECONNECT_DELAYS
        delay = float(delays[min(self._failures, len(delays) - 1)])
        self._failures += 1
        return delay

    def _wait(self, seconds: float) -> bool:
        """Yeniden bağlanmadan önce bekler; stop() çağrılırsa hemen True döner."""
        return self._stop.wait(seconds)

    def _close_ws(self, ws) -> None:
        try:
            ws.close()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("Fiyat akışı kapatılırken hata: %s", exc)

    # ================================================================ geri çağrılar
    def _safe(self, fn: Callable, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - geri çağrılar asla exception fırlatmasın
            self._log_throttled(logging.WARNING, "callback", "⚠️ Fiyat akışı iç hatası (%s): %s",
                                getattr(fn, "__name__", "?"), exc)

    def _handle_open(self, gen: int) -> None:
        with self._lock:
            if gen != self._gen:
                return
            self._connected = True
            self._subscribed.clear()
            self._opened_mono = time.monotonic()
            self._connect_count += 1
            first = self._connect_count == 1
            count = len(self._wanted)
            stopping = self._stop.is_set()
            ws = self._ws
        if stopping:
            if ws is not None:
                self._close_ws(ws)
            return
        self.log.info("📡 Anlık fiyat akışı %s (%d parite)", "bağlandı" if first else "yeniden bağlandı", count)
        self._wake.set()

    def _handle_close(self, gen: int, code=None, reason=None) -> None:
        with self._lock:
            if gen != self._gen:
                return
            was_connected = self._connected
            self._connected = False
            self._subscribed.clear()
        if was_connected and not self._stop.is_set():
            self._log_throttled(logging.INFO, "closed", "🔌 Fiyat akışı bağlantısı kapandı (kod: %s %s)",
                                code, reason or "")

    def _handle_error(self, gen: int, error) -> None:
        with self._lock:
            if gen != self._gen:
                return
        if self._stop.is_set():
            self.log.debug("Fiyat akışı (kapanırken) hata: %s", error)
            return
        self._log_throttled(logging.WARNING, "error", "⚠️ Fiyat akışı hatası: %s", error)

    def _handle_message(self, gen: int, message) -> None:
        now = self.time_fn()
        with self._lock:
            if gen != self._gen:
                return
            self._last_message_at = now
        try:
            text = message.decode("utf-8") if isinstance(message, (bytes, bytearray)) else message
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            self._bad(message, f"JSON okunamadı: {exc}")
            return
        if not isinstance(data, dict):
            self._bad(message, "beklenmeyen biçim")
            return
        if "result" in data and "id" in data:   # Abonelik onayı: {"result": null, "id": 1}
            if data["result"] is not None:
                self.log.debug("Fiyat akışı yanıtı: %s", data)
            return
        if "error" in data or ("code" in data and "msg" in data):
            self._log_throttled(logging.WARNING, "server_error", "⚠️ Fiyat akışı sunucu hatası: %s", data)
            return
        if isinstance(data.get("data"), dict):  # Birleşik akış (/stream) biçimi de desteklensin
            data = data["data"]
        symbol, raw_bid, raw_ask = data.get("s"), data.get("b"), data.get("a")
        if not isinstance(symbol, str) or raw_bid is None or raw_ask is None:
            self._bad(message, "eksik alan")
            return
        try:
            bid, ask = float(raw_bid), float(raw_ask)
        except (TypeError, ValueError):
            self._bad(message, "fiyat sayı değil")
            return
        sym = symbol.upper()
        with self._lock:
            if gen != self._gen or sym not in self._wanted:
                return
            if bid > 0 and ask > 0 and math.isfinite(bid) and math.isfinite(ask):
                self._quotes[sym] = (bid, ask, now)
            else:  # Defter boş/geçersiz: eski fiyatı kullanma, çağıran REST'e düşsün
                self._quotes.pop(sym, None)

    def _bad(self, message, why: str) -> None:
        self._bad_messages += 1
        self._log_throttled(logging.WARNING, "bad_message", "⚠️ Fiyat akışında bozuk mesaj yok sayıldı (%s): %.200r",
                            why, message)

    # ================================================================ abonelik eşitleme
    def _sync_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._sync()
                self._check_idle()
            except Exception as exc:  # noqa: BLE001 - eşitleme iş parçacığı asla ölmesin
                self._log_throttled(logging.WARNING, "sync", "⚠️ Fiyat akışı abonelik hatası: %s", exc)

    def _sync(self) -> None:
        """İstenen sembollerle abone olunanları eşitler (önce çıkış, sonra abonelik)."""
        with self._lock:
            if not self._connected or self._ws is None:
                return
            ws, gen = self._ws, self._gen
            to_unsub = sorted(self._subscribed - self._wanted)
            to_sub = sorted(self._wanted - self._subscribed)
        batches = [("UNSUBSCRIBE", c) for c in _chunks(to_unsub, self.max_params_per_message)]
        batches += [("SUBSCRIBE", c) for c in _chunks(to_sub, self.max_params_per_message)]
        for method, chunk in batches:
            if self._throttle():
                return
            with self._lock:
                if gen != self._gen or not self._connected:
                    return
                self._msg_id += 1
                payload = json.dumps({"method": method, "params": [s.lower() + STREAM_SUFFIX for s in chunk],
                                      "id": self._msg_id})
            try:
                ws.send(payload)
            except Exception as exc:  # noqa: BLE001 - yeniden bağlanınca baştan abone olunur
                self._log_throttled(logging.WARNING, "send", "⚠️ Fiyat akışı abonelik mesajı gönderilemedi: %s", exc)
                return
            finally:
                self._last_control_mono = time.monotonic()
            with self._lock:
                if gen != self._gen:
                    return
                if method == "SUBSCRIBE":
                    self._subscribed.update(chunk)
                    self._last_sub_at = self.time_fn()
                else:
                    self._subscribed.difference_update(chunk)
            self.log.debug("Fiyat akışı %s: %s", method, ", ".join(chunk))

    def _throttle(self) -> bool:
        """Kontrol mesajları arasında en az ``control_interval`` bekler; stop() çağrılırsa True."""
        wait = self._last_control_mono + self.control_interval - time.monotonic()
        return wait > 0 and self._stop.wait(wait)

    def _check_idle(self) -> None:
        """Abone olunduğu halde uzun süre hiç mesaj gelmiyorsa bağlantıyı yeniler (ölü bağlantı)."""
        if not self.idle_reconnect_sec or self.idle_reconnect_sec <= 0:
            return
        with self._lock:
            if not self._connected or not self._subscribed or self._ws is None:
                return
            ref = max(self._last_message_at or 0.0, self._last_sub_at)
            idle = self.time_fn() - ref
            if idle < self.idle_reconnect_sec:
                return
            ws = self._ws
            self._connected = False
        self.log.warning("⚠️ Fiyat akışından %.0f sn'dir veri gelmiyor; yeniden bağlanılıyor", idle)
        self._close_ws(ws)

    # ================================================================ yardımcılar
    def _log_throttled(self, level: int, key: str, msg: str, *args) -> None:
        """Aynı tür uyarıyı en fazla ``log_interval`` sn'de bir loglar (gerisi DEBUG)."""
        now = time.monotonic()
        last = self._last_log.get(key)
        if last is None or now - last >= self.log_interval:
            self._last_log[key] = now
            self.log.log(level, msg, *args)
        else:
            self.log.debug(msg, *args)
