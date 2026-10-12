import time

from telegram_bot import Messages, TelegramNotifier, fmt_price


class FakeResp:
    status_code = 200
    ok = True
    text = "{}"

    def json(self):
        return {"ok": True}


def test_async_send_does_not_block(monkeypatch):
    sent = []

    def slow_post(url, json, timeout):
        time.sleep(0.3)
        sent.append(json)
        return FakeResp()

    n = TelegramNotifier("token", "123", prefix="🧪 ")
    monkeypatch.setattr(n.session, "post", slow_post)
    t0 = time.time()
    assert n.send("🟢 <b>ALIM</b>")
    assert time.time() - t0 < 0.1          # Ana döngü beklemez
    n.flush(timeout=5)
    assert sent and sent[0]["text"].startswith("🧪 🟢") and sent[0]["parse_mode"] == "HTML"


def test_error_throttling(monkeypatch):
    n = TelegramNotifier("token", "123", async_mode=False)
    monkeypatch.setattr(n.session, "post", lambda url, json, timeout: FakeResp())
    assert n.send_error("net", "hata 1")
    assert not n.send_error("net", "hata 2")   # 10 dk içinde aynı hata tekrar gönderilmez
    assert n.send_error("baska", "hata 3")


def test_disabled_notifier_never_fails():
    n = TelegramNotifier("", "")
    assert not n.enabled
    assert n.send("test") is False


def test_messages_escape_html():
    text = Messages.error("x < y & z > w")
    assert "&lt;" in text and "&amp;" in text and "⛔" not in text
    assert "⛔" in Messages.error("boom", fatal=True)


def test_fmt_price():
    assert fmt_price(65000.123) == "65000.12"
    assert fmt_price(2.5) == "2.5000"
    assert fmt_price(0.08912) == "0.089120"
    assert fmt_price(0.00001234) == "0.000012340"
