"""PriceStream testleri: sahte WebSocket ile (internet gerektirmez)."""
import json
import logging
import threading
import time

import pytest

import price_stream
from price_stream import PriceStream

LOG = logging.getLogger("test.price_stream")

BNB_MSG = {"u": 400900217, "s": "BNBUSDT", "b": "25.35190000", "B": "31.21", "a": "25.36520000", "A": "40.66"}


def quote(symbol, bid, ask, u=1):
    return {"u": u, "s": symbol, "b": str(bid), "B": "1", "a": str(ask), "A": "1"}


def wait_until(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return bool(cond())


class Clock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


class FakeWS:
    """websocket.WebSocketApp benzeri: run_forever close() çağrılana kadar bloklar."""

    def __init__(self, hub, behavior, url, on_open, on_message, on_error, on_close):
        self.hub = hub
        self.behavior = behavior
        self.url = url
        self.on_open, self.on_message, self.on_error, self.on_close = on_open, on_message, on_error, on_close
        self.sent = []
        self.send_times = []
        self.closed = threading.Event()
        self.opened = threading.Event()
        self.finished = threading.Event()
        self._lock = threading.Lock()

    def run_forever(self):
        try:
            if self.behavior == "fail":
                self.on_error(self, ConnectionError("bağlantı kurulamadı"))
                self.on_close(self, None, None)
                return
            self.on_open(self)
            self.opened.set()
            if self.behavior == "open_drop":
                self.on_close(self, 1006, "koptu")
                return
            self.closed.wait(10)
            self.on_close(self, 1000, "kapandı")
        finally:
            self.finished.set()

    def send(self, text):
        with self._lock:
            self.sent.append(json.loads(text))
            self.send_times.append(time.monotonic())

    def close(self):
        self.closed.set()

    def push(self, msg):
        self.on_message(self, msg if isinstance(msg, (str, bytes)) else json.dumps(msg))

    def messages(self):
        with self._lock:
            return list(self.sent)


class FakeFactory:
    def __init__(self, script=None):
        self.sockets = []
        self.script = list(script or [])   # Sıradaki bağlantıların davranışı; bitince "ok"
        self.urls = []

    def __call__(self, url, on_open, on_message, on_error, on_close):
        behavior = self.script.pop(0) if self.script else "ok"
        ws = FakeWS(self, behavior, url, on_open, on_message, on_error, on_close)
        self.sockets.append(ws)
        self.urls.append(url)
        return ws

    @property
    def last(self):
        return self.sockets[-1]


@pytest.fixture
def make_stream():
    created = []

    def make(script=None, **kw):
        factory = FakeFactory(script)
        clock = Clock()
        ps = PriceStream(ws_factory=factory, time_fn=clock, logger=LOG, **kw)
        ps.control_interval = 0.0
        ps.reconnect_delays = (0.01,)
        created.append(ps)
        return ps, factory, clock

    yield make
    for ps in created:
        ps.stop(timeout=2)


def connect(ps, factory, n=1):
    assert ps.start()
    assert wait_until(lambda: len(factory.sockets) >= n and factory.sockets[n - 1].opened.is_set() and ps.connected)
    return factory.sockets[n - 1]


def sent_once(ws, count=1):
    assert wait_until(lambda: len(ws.messages()) >= count), ws.messages()
    return ws.messages()


# ---------------------------------------------------------------- kullanılabilirlik
def test_unavailable_without_library(monkeypatch):
    monkeypatch.setattr(price_stream, "_websocket", None)
    ps = PriceStream(logger=LOG)
    assert not ps.available
    assert ps.start() is False
    assert not ps.running and not ps.connected
    assert ps.get_bid_ask("BTCUSDT") is None
    ps.set_symbols(["BTCUSDT"])            # Hata vermez
    ps.stop()
    # Sahte bağlantı fabrikası verilirse kütüphane olmadan da çalışabilir
    assert PriceStream(ws_factory=FakeFactory(), logger=LOG).available


def test_default_factory_uses_websocketapp(monkeypatch):
    calls = {}

    class FakeApp:
        def __init__(self, url, on_open=None, on_message=None, on_error=None, on_close=None):
            calls["url"] = url
            self.on_open, self.on_close = on_open, on_close
            self.stop = threading.Event()

        def run_forever(self, ping_interval=0, ping_timeout=None, reconnect=None, sslopt=None):
            calls["run"] = {"ping_interval": ping_interval, "ping_timeout": ping_timeout, "reconnect": reconnect}
            self.on_open(self)
            self.stop.wait(5)
            self.on_close(self, 1000, "bye")

        def send(self, text):
            calls.setdefault("sent", []).append(json.loads(text))

        def close(self):
            self.stop.set()

    class FakeModule:
        WebSocketApp = FakeApp

    monkeypatch.setattr(price_stream, "_websocket", FakeModule)
    ps = PriceStream(url="wss://example.test/ws", logger=LOG)
    ps.control_interval = 0.0
    try:
        assert ps.available
        ps.set_symbols(["BTCUSDT"])
        assert ps.start()
        assert wait_until(lambda: calls.get("sent"))
        assert calls["url"] == "wss://example.test/ws"
        # Kütüphanenin kendi yeniden bağlanması kapalı, ping/pong ile ölü bağlantı yakalanır
        assert calls["run"] == {"ping_interval": 30.0, "ping_timeout": 10.0, "reconnect": 0}
        assert calls["sent"][0] == {"method": "SUBSCRIBE", "params": ["btcusdt@bookTicker"], "id": 1}
    finally:
        ps.stop(timeout=2)
    assert not ps.running


# ---------------------------------------------------------------- mesaj işleme
def test_subscribes_on_connect_and_parses_book_ticker(make_stream):
    ps, factory, clock = make_stream()
    ps.set_symbols(["BNBUSDT", "btcusdt", " ethusdt "])   # Bağlanmadan önce saklanır
    ws = connect(ps, factory)
    msgs = sent_once(ws)
    assert msgs[0] == {"method": "SUBSCRIBE",
                       "params": ["bnbusdt@bookTicker", "btcusdt@bookTicker", "ethusdt@bookTicker"], "id": 1}
    assert ps.symbols == frozenset({"BNBUSDT", "BTCUSDT", "ETHUSDT"})

    assert ps.last_message_at is None
    ws.push({"result": None, "id": 1})                  # Abonelik onayı yok sayılır
    assert ps.get_bid_ask("BNBUSDT") is None
    ws.push(BNB_MSG)
    assert ps.get_bid_ask("BNBUSDT") == (25.3519, 25.3652)
    assert ps.get_bid_ask("bnbusdt") == (25.3519, 25.3652)
    assert ps.last_message_at == clock()
    assert ps.get_bid_ask("BTCUSDT") is None            # Henüz veri yok

    # Birleşik akış (/stream) biçimi de okunur
    ws.push({"stream": "btcusdt@bookTicker", "data": quote("BTCUSDT", 65000.1, 65000.2)})
    assert ps.get_bid_ask("BTCUSDT") == (65000.1, 65000.2)

    # İzlenmeyen sembol saklanmaz
    ws.push(quote("XRPUSDT", 0.5, 0.51))
    assert ps.get_bid_ask("XRPUSDT") is None
    assert ps.bad_messages == 0


def test_set_symbols_sends_diff(make_stream):
    ps, factory, clock = make_stream()
    ps.set_symbols({"BTCUSDT", "ETHUSDT"})
    ws = connect(ps, factory)
    sent_once(ws, 1)
    ws.push(quote("BTCUSDT", 100, 101))
    ws.push(quote("ETHUSDT", 10, 11))

    ps.set_symbols(["ETHUSDT", "BNBUSDT"])
    msgs = sent_once(ws, 3)
    assert msgs[1] == {"method": "UNSUBSCRIBE", "params": ["btcusdt@bookTicker"], "id": 2}
    assert msgs[2] == {"method": "SUBSCRIBE", "params": ["bnbusdt@bookTicker"], "id": 3}
    assert ps.get_bid_ask("BTCUSDT") is None            # Çıkılan sembolün fiyatı silinir
    assert ps.get_bid_ask("ETHUSDT") == (10.0, 11.0)
    ws.push(quote("BTCUSDT", 100, 101))                 # Geç gelen mesaj saklanmaz
    assert ps.get_bid_ask("BTCUSDT") is None

    ps.set_symbols(["BNBUSDT", "ETHUSDT"])              # Aynı küme: mesaj yok
    ps.set_symbols("BNBUSDT")                           # Tek string de kabul edilir
    msgs = sent_once(ws, 4)
    assert msgs[3] == {"method": "UNSUBSCRIBE", "params": ["ethusdt@bookTicker"], "id": 4}

    ps.set_symbols([])                                  # Boş küme: tüm aboneliklerden çık, bağlantı açık kalsın
    msgs = sent_once(ws, 5)
    assert msgs[4] == {"method": "UNSUBSCRIBE", "params": ["bnbusdt@bookTicker"], "id": 5}
    time.sleep(0.05)
    assert len(ws.messages()) == 5
    assert ps.connected and not ws.closed.is_set()
    assert len(factory.sockets) == 1


def test_control_messages_are_rate_limited_and_chunked(make_stream):
    ps, factory, clock = make_stream()
    ps.control_interval = 0.25
    ps.max_params_per_message = 2
    symbols = ["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"]
    ps.set_symbols(symbols)
    ws = connect(ps, factory)
    msgs = sent_once(ws, 3)
    assert [m["method"] for m in msgs] == ["SUBSCRIBE"] * 3
    assert [len(m["params"]) for m in msgs] == [2, 2, 1]
    assert sorted(p for m in msgs for p in m["params"]) == sorted(s.lower() + "@bookTicker" for s in symbols)
    gaps = [b - a for a, b in zip(ws.send_times, ws.send_times[1:])]
    assert all(g >= 0.2 for g in gaps), gaps            # Saniyede 5'ten az kontrol mesajı


# ---------------------------------------------------------------- bayatlık
def test_stale_quotes_are_not_returned(make_stream):
    ps, factory, clock = make_stream(stale_after=5.0)
    ps.idle_reconnect_sec = 0
    ps.set_symbols(["BNBUSDT"])
    ws = connect(ps, factory)
    ws.push(BNB_MSG)
    assert ps.get_bid_ask("BNBUSDT") == (25.3519, 25.3652)
    clock.advance(4.9)
    assert ps.get_bid_ask("BNBUSDT") is not None
    assert ps.get_bid_ask("BNBUSDT", max_age=1.0) is None   # Çağıranın daha sıkı sınırı
    clock.advance(0.2)
    assert ps.get_bid_ask("BNBUSDT") is None                 # stale_after aşıldı
    assert ps.get_bid_ask("BNBUSDT", max_age=10.0) == (25.3519, 25.3652)
    ws.push(quote("BNBUSDT", 26, 26.1))                      # Yeni veri gelince tekrar taze
    assert ps.get_bid_ask("BNBUSDT") == (26.0, 26.1)


def test_stale_check_can_be_disabled(make_stream):
    ps, factory, clock = make_stream(stale_after=None)
    ps.idle_reconnect_sec = 0
    ps.set_symbols(["BNBUSDT"])
    ws = connect(ps, factory)
    ws.push(BNB_MSG)
    clock.advance(3600)
    assert ps.get_bid_ask("BNBUSDT") == (25.3519, 25.3652)


# ---------------------------------------------------------------- yeniden bağlanma
def test_reconnect_resubscribes_and_ignores_old_socket(make_stream):
    ps, factory, clock = make_stream()
    ps.set_symbols(["BTCUSDT", "ETHUSDT"])
    ws1 = connect(ps, factory)
    sent_once(ws1, 1)
    ws1.push(quote("BTCUSDT", 100, 101))

    ws1.close()                                          # Sunucu bağlantıyı kapattı (ör. 24 saat sınırı)
    ws2 = connect(ps, factory, n=2)
    msgs = sent_once(ws2, 1)
    assert msgs[0]["method"] == "SUBSCRIBE"
    assert msgs[0]["params"] == ["btcusdt@bookTicker", "ethusdt@bookTicker"]
    assert msgs[0]["id"] > 1                             # Mesaj numaraları artmaya devam eder
    assert ps.connect_count == 2

    ws1.push(quote("BTCUSDT", 1, 2))                     # Eski bağlantıdan gelen mesaj yok sayılır
    assert ps.get_bid_ask("BTCUSDT") == (100.0, 101.0)
    ws2.push(quote("BTCUSDT", 102, 103))
    assert ps.get_bid_ask("BTCUSDT") == (102.0, 103.0)


def test_symbols_changed_while_disconnected_are_used_on_reconnect(make_stream):
    ps, factory, clock = make_stream(script=["fail", "fail"])
    ps.reconnect_delays = (0.05,)
    ps.set_symbols(["BTCUSDT"])
    assert ps.start()
    assert wait_until(lambda: len(factory.sockets) >= 1)
    ps.set_symbols(["ETHUSDT"])
    ws = connect(ps, factory, n=3)
    msgs = sent_once(ws, 1)
    assert msgs == [{"method": "SUBSCRIBE", "params": ["ethusdt@bookTicker"], "id": 1}]


@pytest.mark.parametrize("script, healthy_after, expected", [
    (["fail"] * 10, 60.0, [1.0, 2.0, 5.0, 10.0, 30.0, 30.0, 30.0]),
    (["fail", "fail", "open_drop", "fail"], 0.0, [1.0, 2.0, 1.0, 2.0]),     # Sağlıklı bağlantı beklemeyi sıfırlar
    (["open_drop"] * 4, 60.0, [1.0, 2.0, 5.0, 10.0]),                       # Açılıp hemen kopan bağlantı sıfırlamaz
])
def test_reconnect_backoff(make_stream, script, healthy_after, expected):
    ps, factory, clock = make_stream(script=script)
    ps.reconnect_delays = price_stream.RECONNECT_DELAYS
    ps.healthy_after = healthy_after
    delays = []

    def fake_wait(sec):
        delays.append(sec)
        return len(delays) >= len(expected)               # True = dur

    ps._wait = fake_wait
    assert ps.start()
    assert wait_until(lambda: not ps._thread.is_alive())
    assert delays == expected
    assert len(factory.sockets) == len(expected)


def test_idle_connection_is_recycled(make_stream):
    ps, factory, clock = make_stream()
    ps.idle_reconnect_sec = 30
    ps.set_symbols(["BTCUSDT"])
    ws1 = connect(ps, factory)
    sent_once(ws1, 1)
    assert wait_until(lambda: ps._subscribed == {"BTCUSDT"})
    ws1.push(quote("BTCUSDT", 1, 2))
    clock.advance(10)
    ps._check_idle()
    assert not ws1.closed.is_set()
    clock.advance(25)                                    # 35 sn'dir mesaj yok
    ps._check_idle()
    assert ws1.closed.is_set()
    ws2 = connect(ps, factory, n=2)
    assert sent_once(ws2, 1)[0]["params"] == ["btcusdt@bookTicker"]


# ---------------------------------------------------------------- dayanıklılık
def test_bad_messages_are_swallowed(make_stream, caplog):
    ps, factory, clock = make_stream()
    ps.set_symbols(["BTCUSDT"])
    ws = connect(ps, factory)
    ws.push(quote("BTCUSDT", 100, 101))
    with caplog.at_level(logging.DEBUG, logger=LOG.name):
        for bad in ["bozuk {json", b"\xff\xfe\x00", "[1, 2, 3]", "null", '"metin"',
                    {"s": "BTCUSDT"}, {"s": "BTCUSDT", "b": "abc", "a": "1"},
                    {"s": 123, "b": "1", "a": "2"}, {"s": "BTCUSDT", "b": None, "a": "2"}]:
            ws.push(bad)
        ws.on_message(ws, None)
        ws.on_message()                                  # Beklenmeyen argümanlar da exception fırlatmaz
        ws.on_error()
        ws.push({"code": 2, "msg": "Invalid request", "id": 9})
        ws.push({"error": {"code": 2, "msg": "Invalid request"}, "id": 10})
    assert ps.bad_messages >= 10
    assert any("bozuk mesaj" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert ps.get_bid_ask("BTCUSDT") == (100.0, 101.0)  # Eski geçerli veri bozulmadı

    ws.push(quote("BTCUSDT", 0, 101))                    # Boş defter: eski fiyat kullanılmaz
    assert ps.get_bid_ask("BTCUSDT") is None
    ws.push(quote("BTCUSDT", 102, 103))                  # Akış çalışmaya devam eder
    assert ps.get_bid_ask("BTCUSDT") == (102.0, 103.0)
    assert ps._thread.is_alive() and ps._sync_thread.is_alive()
    ws.on_close()                                        # Argümansız kapanış da exception fırlatmaz
    assert not ps.connected


def test_send_failure_does_not_kill_stream(make_stream):
    ps, factory, clock = make_stream()
    ws = connect(ps, factory)
    calls = []

    def broken_send(text):
        calls.append(text)
        if len(calls) == 1:
            raise OSError("soket kapalı")
        FakeWS.send(ws, text)

    ws.send = broken_send
    ps.set_symbols(["BTCUSDT"])
    # İlk gönderim başarısız; eşitleme döngüsü bir sonraki turda tekrar dener
    assert sent_once(ws, 1)[0]["params"] == ["btcusdt@bookTicker"]
    assert ps._sync_thread.is_alive()


# ---------------------------------------------------------------- durdurma
def test_stop_closes_cleanly(make_stream):
    ps, factory, clock = make_stream()
    ps.stop()                                            # Başlamadan durdurmak sorun değil
    ps.set_symbols(["BTCUSDT"])
    ws = connect(ps, factory)
    assert ps.running
    assert ps.start() is True                            # İkinci start yeni iş parçacığı açmaz
    t0 = time.time()
    ps.stop(timeout=2)
    assert time.time() - t0 < 1.5
    assert ws.closed.is_set() and ws.finished.is_set()
    assert not ps.connected and not ps.running
    assert not ps._thread.is_alive() and not ps._sync_thread.is_alive()
    assert len(factory.sockets) == 1                     # Durunca yeniden bağlanmaz
    ps.stop()                                            # İkinci kez çağrılabilir
    ps.set_symbols(["ETHUSDT"])                          # Durduktan sonra da exception yok


def test_stop_during_reconnect_wait(make_stream):
    ps, factory, clock = make_stream(script=["fail"] * 5)
    ps.reconnect_delays = (30.0,)
    assert ps.start()
    assert wait_until(lambda: len(factory.sockets) >= 1 and factory.sockets[0].finished.is_set())
    t0 = time.time()
    ps.stop(timeout=2)
    assert time.time() - t0 < 1.5                        # 30 sn beklemeden hemen çıkar
    assert not ps._thread.is_alive()


def test_restart_after_stop(make_stream):
    ps, factory, clock = make_stream()
    ps.set_symbols(["BTCUSDT"])
    connect(ps, factory)
    ps.stop(timeout=2)
    ws2 = connect(ps, factory, n=2)
    assert sent_once(ws2, 1)[0]["params"] == ["btcusdt@bookTicker"]
