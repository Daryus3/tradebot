from indicators import ema, rsi
from strategy import ReboundStrategy


def kline(t, o, h, l, c, vol=1000.0, taker=600.0):
    return [t, str(o), str(h), str(l), str(c), "0", t + 299_999, str(vol), 10, "0", str(taker), "0"]


def build_dip_and_bounce():
    """Yatay seyir → dalgalı sert düşüş (aşırı satım) → hacimli toparlanma başlangıcı."""
    rows, t, price = [], 0, 100.0
    for _ in range(60):                      # yatay
        rows.append(kline(t, price, price * 1.003, price * 0.997, price)); t += 300_000
    for i in range(16):                      # dalgalı düşüş: ~%6.5
        o = price; price *= 0.988 if i % 2 == 0 else 1.004
        rows.append(kline(t, o, max(o, price) * 1.001, min(o, price) * 0.998, price, vol=1500)); t += 300_000
    for _ in range(5):                       # toparlanma, hacim artıyor
        o = price; price *= 1.006
        rows.append(kline(t, o, price * 1.001, o * 0.999, price, vol=3000, taker=2000)); t += 300_000
    rows.append(kline(t, price, price * 1.002, price * 0.999, price * 1.001, vol=500))  # açık mum
    return rows


def test_indicators_basic():
    assert ema([1, 2, 3, 4, 5], 3)[-1] == 4.0
    up = rsi([float(i) for i in range(30)], 14)
    assert up[-1] == 100.0
    mixed = rsi([10, 11, 10.5, 11.5, 11, 12, 11.2, 12.4, 12, 13, 12.5, 13.6, 13, 14, 13.4, 14.5], 14)
    assert 0 < mixed[-1] < 100


def test_detects_rebound(cfg):
    strategy = ReboundStrategy(client=None, cfg=cfg)
    signal, reason = strategy.analyze("ABCUSDT", build_dip_and_bounce())
    assert signal is not None, reason
    assert signal.metrics["dip_pct"] > 3
    assert 0.15 <= signal.metrics["retrace"] <= 0.6


def test_rejects_still_falling(cfg):
    strategy = ReboundStrategy(client=None, cfg=cfg)
    rows = build_dip_and_bounce()[:-5]
    last_close = float(rows[-1][4])
    rows.append(kline(rows[-1][0] + 300_000, last_close, last_close, last_close * 0.97, last_close * 0.975))
    signal, reason = strategy.analyze("ABCUSDT", rows)
    assert signal is None
    assert reason == "dip sürüyor"


def test_rejects_flat_market(cfg):
    strategy = ReboundStrategy(client=None, cfg=cfg)
    rows = [kline(i * 300_000, 100, 100.2, 99.8, 100) for i in range(100)]
    signal, reason = strategy.analyze("ABCUSDT", rows)
    assert signal is None and reason == "düşüş az"
