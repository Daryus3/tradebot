"""Vadeli-spot farkına göre boy (FUTURES_BASIS_SIZE_PCT / FUTURES_BASIS_DEEP_PCT; rapor 17 araştırması, 22 Kasım):
girişte vadeli fiyat spotun belirgin altındaysa (zorla satış izi) pozisyon büyük açılır. Ortak ölçü (strategy), spot
arşivi, backtest, canlı bot (sahte borsa, gerçek istek yok), ayarlar, Telegram ve rapor satırları."""
import io
import zipfile
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pytest

import futures_backtest as fb
from config import ConfigError, _setting_notes, load_config
from futures_data import FuturesArchive
from runtime_settings import EDITABLE
from strategy import basis_pct, basis_size_factor, spot_pairs
from test_futures import BASE_MS, fcfg, make_fbot, params  # noqa: F401 - pytest fikstürü
from test_futures_cascade import KlineCcxt, bt_data, bt_sig, crashing, flat, live_cfg, press, tp_window

MIN = 60_000
DAY = fb.DAY_MS


# ====================================================================== ortak ölçü
def test_spot_pairs_basis_and_size_factor():
    assert spot_pairs("BTCUSDT") == [("BTCUSDT", 1.0)]
    assert spot_pairs("1000PEPEUSDT") == [("PEPEUSDT", 1000.0), ("1000PEPEUSDT", 1.0)]   # Spotta 1000'siz
    assert spot_pairs("1000000MOGUSDT")[0] == ("MOGUSDT", 1_000_000.0)
    assert spot_pairs("1INCHUSDT") == [("1INCHUSDT", 1.0)]                               # 1000 değil
    assert basis_pct(99.4, 100.0) == pytest.approx(-0.6)
    assert basis_pct(0.0099, 0.00001, 1000.0) == pytest.approx(-1.0)                     # 1000'li vadeli
    assert basis_pct(0.0, 100.0) != basis_pct(0.0, 100.0)                                # Fiyat yok: NaN
    assert basis_size_factor(130.0, 0.5, -0.6) == pytest.approx(1.3)
    assert basis_size_factor(130.0, 0.5, -0.5) == pytest.approx(1.3)                     # Eşikte büyük
    assert basis_size_factor(130.0, 0.5, -0.4) == 1.0 and basis_size_factor(130.0, 0.5, 0.2) == 1.0
    assert basis_size_factor(100.0, 0.5, -2.0) == 1.0                                    # Kapalı
    assert basis_size_factor(130.0, 0.5, None) == 1.0 and basis_size_factor(130.0, 0.5, float("nan")) == 1.0


# ====================================================================== spot arşivi
def _zip(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name.replace(".zip", ".csv"), text)
    return buf.getvalue()


class _Session:
    def __init__(self, files):
        self.files, self.urls = files, []

    def get(self, url, timeout=None):
        self.urls.append(url)
        name = url.rsplit("/", 1)[-1]

        class R:
            pass
        r = R()
        r.status_code = 200 if name in self.files else 404
        r.content = self.files.get(name, b"")
        return r


def test_archive_downloads_spot_minutes_and_converts_microseconds(tmp_path):
    d = datetime(2025, 3, 1, tzinfo=timezone.utc)
    t0 = int(d.timestamp() * 1000)
    rows = "\n".join(f"{(t0 + i * MIN) * 1000},{100 + i},{101 + i},{99 + i},{100.5 + i},1,0,1,1,1,1,0" for i in range(3))
    name = "PEPEUSDT-1m-2025-03-01.zip"
    session = _Session({name: _zip(name, rows)})
    arch = FuturesArchive(tmp_path, "https://example.test", session=session, now_fn=lambda: t0 / 1000 + 30 * 86400)
    got = arch.spot_day("PEPEUSDT", d)
    assert list(got["t"]) == [t0, t0 + MIN, t0 + 2 * MIN] and list(got["o"]) == [100.0, 101.0, 102.0]
    assert session.urls[-1] == "https://example.test/data/spot/daily/klines/PEPEUSDT/1m/" + name
    arch.spot_day("PEPEUSDT", d)
    assert len(session.urls) == 1                                                        # Önbellekten


def test_attach_basis_uses_the_minute_open_and_the_1000_mapping(fcfg):
    t0 = BASE_MS // DAY * DAY + 3 * 3600_000

    class Arch:
        def spot_day(self, symbol, day):
            if symbol == "PEPEUSDT":                                                    # 1000PEPE → PEPE × 1000
                return {"t": np.array([t0], dtype=np.int64), "o": np.array([0.0101])}
            if symbol == "SOLUSDT":
                return {"t": np.array([t0 - MIN, t0], dtype=np.int64), "o": np.array([99.0, 100.0])}
            return {"t": np.zeros(0, dtype=np.int64), "o": np.zeros(0)}
    data = bt_data()
    data.archive = Arch()
    sol = fb.FutSignal(t0, "SOLUSDT", "long", 1.0, 99.4)
    pepe = fb.FutSignal(t0, "1000PEPEUSDT", "long", 1.0, 10.0)
    ada = fb.FutSignal(t0, "ADAUSDT", "long", 1.0, 0.5)                                 # Spot verisi yok
    late = fb.FutSignal(t0 + MIN, "SOLUSDT", "long", 1.0, 99.0)                         # O dakika yok
    cfg = replace(fcfg, futures_tactic="crash")
    assert fb.attach_basis(cfg, data, [sol, pepe, ada, late]) == 2
    assert sol.metrics["basis"] == pytest.approx(-0.6) and pepe.metrics["basis"] == pytest.approx((10 / 10.1 - 1) * 100)
    assert "basis" not in ada.metrics and "basis" not in late.metrics
    assert fb.attach_basis(replace(cfg, futures_tactic="liq4"), data, [ada]) == 0        # Yalnız çöküş taktiği


# ====================================================================== backtest
def _sig(symbol, close_ms, breadth, basis=None):
    s = bt_sig(symbol, close_ms, breadth)
    s.metrics = {"tactic": 1.0}
    if basis is not None:
        s.metrics["basis"] = basis
    return s


def test_backtest_sizes_deep_discount_up_only_where_margin_is_free():
    data = bt_data()
    a = _sig("AAAUSDT", BASE_MS, 9, -0.8)                     # Vadeli spotun %0.8 altında: büyük
    b = _sig("BBBUSDT", BASE_MS + 120 * MIN, 9, -0.2)         # Fark sığ: olağan boy
    w = {(s.symbol, s.close_ms): tp_window(s.close_ms) for s in (a, b)}
    p = params(start_capital=100.0, be_enabled=False, hold_min=60, margin_usage_pct=95.0)
    slots = replace(p, max_positions=4, extra_breadth=8, extra_size_pct=16.7)
    share = fb.slot_share(slots, 0)
    off = fb.simulate(slots, data, [a, b], w)
    assert off.trades[0].margin == pytest.approx(100 * 0.95 * share)
    on = fb.simulate(replace(slots, basis_size_pct=130.0, basis_deep_pct=0.5), data, [a, b], w)
    assert on.trades[0].margin == pytest.approx(100 * 0.95 * share * 1.3) and on.trades[0].basis == pytest.approx(-0.8)
    eq = on.trades[1].equity_after - on.trades[1].pnl
    assert on.trades[1].margin == pytest.approx(eq * 0.95 * share)                      # Sığ fark: olağan boy
    big = fb.simulate(replace(slots, basis_size_pct=200.0, basis_deep_pct=0.5), data, [a], w)
    assert big.trades[0].margin == pytest.approx(100 * 0.95)                             # Boş teminatla sınırlı
    single = fb.simulate(replace(p, basis_size_pct=150.0), data, [a], w)
    assert single.trades[0].margin == pytest.approx(100 * 0.95)                          # Tek pozisyonda büyütülmez
    fixed = fb.simulate(replace(slots, basis_size_pct=130.0), data, [a], w, fixed_notional=25.0)
    assert fixed.trades[0].notional == pytest.approx(25.0 * 4 * share * 1.3)
    a.metrics.pop("basis")                                                               # Ölçü yoksa değişmez
    assert fb.simulate(replace(slots, basis_size_pct=130.0), data, [a], w).trades[0].margin == pytest.approx(
        100 * 0.95 * share)


def test_report_lines_for_the_basis_rule(fcfg):
    p = replace(fb.params_from_cfg(fcfg), basis_size_pct=130.0, basis_deep_pct=0.5)
    trades = [fb.FutTrade("A", "long", 0, 1, 1, 1, "TP", 2, 1, 2, 0, 0, pnl, 1, 0, 0, 0, False, False, MIN, basis=b)
              for b, pnl in ((-0.9, 0.02), (-0.3, 0.004), (-0.05, 0.001), (0.1, -0.002), (float("nan"), 0.5))]
    line = fb.basis_breakdown_line(trades, p)
    assert line == ("Girişteki vadeli / spot farkına göre (sabit tutar; −%0.5'ten derinde boy %130): −%0.5'ten derin: "
                    "1 işlem, ort. +1.000% | −%0.5 … −%0.1: 1 işlem, ort. +0.200% | −%0.1 … 0: 1 işlem, ort. +0.050% | "
                    "0 ve üstü: 1 işlem, ort. -0.100%")
    assert "hepsi aynı boy" in fb.basis_breakdown_line(trades, replace(p, basis_size_pct=100.0))
    assert fb.basis_breakdown_line(trades[-1:], p) is None                               # Ölçü yok: satır yok
    names = [n for n, _e in fb.basis_variant_rows(p, lambda **kw: kw, True)]
    assert names == ["Vadeli-spot farkına göre boy kapalı", "Vadeli-spot boyu %150 (şimdi %130)",
                     "Vadeli-spot eşiği %0.3 (şimdi %0.5)", "Vadeli-spot eşiği %0.7 (şimdi %0.5)"]
    assert fb.basis_variant_rows(replace(p, basis_size_pct=100.0), lambda **kw: kw, True) == [
        ("Vadeli spotun %0.5 altındayken boy %130", {"basis_size_pct": 130.0, "basis_deep_pct": 0.5})]
    assert fb.basis_variant_rows(p, lambda **kw: kw, False) == []                        # Spot verisi yok


# ====================================================================== canlı bot (sahte borsa, test modu)
def _wave_cfg(fcfg, **over):
    return live_cfg(fcfg, futures_leverage=2, futures_max_positions=4, futures_extra_size_pct=16.7,
                    futures_extra_breadth=8, **over)


CLOSES = {"SOLUSDT": crashing(), "ADAUSDT": flat(), "XRPUSDT": flat(), "DOGEUSDT": flat(), "BTCUSDT": flat()}


def _bot(cfg, path, spot):
    bot, fake, notes, clock = make_fbot(cfg, path, fake=KlineCcxt(CLOSES))
    calls = []

    def spot_prices():
        calls.append(1)
        if isinstance(spot, Exception):
            raise spot
        return spot
    bot.exchange.spot_prices = spot_prices                                               # Gerçek istek yok
    return bot, clock, calls


def test_paper_bot_sizes_up_when_futures_trade_below_spot(fcfg, tmp_path):
    price = float(CLOSES["SOLUSDT"][-1])
    cfg = _wave_cfg(fcfg, futures_basis_size_pct=130.0, futures_basis_deep_pct=0.5)
    share = 1 / (1 + 3 * 0.167)
    bot, clock, calls = _bot(cfg, tmp_path, {"SOLUSDT": price / 0.99, "ADAUSDT": 1.0})   # Vadeli %1 altında
    bot.scan_and_enter(clock.t)
    p = bot.state.positions["SOLUSDT"]
    assert calls and p.margin_usdt == pytest.approx(30 * 0.95 * share * 1.3, rel=0.02)
    assert "Vadeli spotun %1.00 altında (zorla satış izi): pozisyon %130" in p.signal_note
    # Fark sığsa olağan boy
    bot2, clock2, _c2 = _bot(cfg, tmp_path / "b", {"SOLUSDT": price / 0.998})
    bot2.scan_and_enter(clock2.t)
    p2 = bot2.state.positions["SOLUSDT"]
    assert p2.margin_usdt == pytest.approx(30 * 0.95 * share, rel=0.02) and "Vadeli spotun" not in p2.signal_note
    # Spot fiyatı alınamazsa olağan boy, bot çökmez
    from futures_exchange import FuturesError
    bot3, clock3, _c3 = _bot(cfg, tmp_path / "c", FuturesError("yok", "SPOT_TICKER"))
    bot3.scan_and_enter(clock3.t)
    assert bot3.state.positions["SOLUSDT"].margin_usdt == pytest.approx(30 * 0.95 * share, rel=0.02)
    # Kapalıyken (varsayılan) spot fiyatı hiç istenmez
    bot4, clock4, calls4 = _bot(replace(cfg, futures_basis_size_pct=100.0), tmp_path / "d", {"SOLUSDT": price / 0.99})
    bot4.scan_and_enter(clock4.t)
    assert not calls4 and bot4.state.positions["SOLUSDT"].margin_usdt == pytest.approx(30 * 0.95 * share, rel=0.02)


def test_attach_basis_reuses_fresh_snapshot_prices_and_maps_1000_pairs(fcfg, tmp_path):
    cfg = _wave_cfg(fcfg, futures_basis_size_pct=130.0)
    bot, clock, calls = _bot(cfg, tmp_path, {"PEPEUSDT": 0.0101})
    from strategy import FuturesSignal
    sig = FuturesSignal("1000PEPEUSDT", "long", 10.0, 1.0, {})
    bot._spot_now = (clock.t, {"PEPEUSDT": 0.0101})                                     # Anlık taramada alındı
    bot._attach_basis([sig])
    assert not calls and sig.metrics["basis"] == pytest.approx((10 / 10.1 - 1) * 100, abs=1e-4)
    sig2 = FuturesSignal("1000PEPEUSDT", "long", 10.0, 1.0, {})
    bot._spot_now = (clock.t - 60, {"PEPEUSDT": 0.02})                                  # Bayat: yeniden istenir
    bot._attach_basis([sig2])
    assert calls and sig2.metrics["basis"] == pytest.approx((10 / 10.1 - 1) * 100, abs=1e-4)


# ====================================================================== ayarlar ve Telegram
def test_basis_settings_defaults_validation_env_runtime_and_notes(fcfg, monkeypatch, tmp_path):
    assert (fcfg.futures_basis_size_pct, fcfg.futures_basis_deep_pct) == (100.0, 0.5)
    for bad in (dict(futures_basis_size_pct=90.0), dict(futures_basis_size_pct=250.0),
                dict(futures_basis_deep_pct=0.0), dict(futures_basis_deep_pct=5.0)):
        with pytest.raises(ConfigError):
            replace(fcfg, **bad).validate()
    monkeypatch.setenv("FUTURES_BASIS_SIZE_PCT", "130")
    monkeypatch.setenv("FUTURES_BASIS_DEEP_PCT", "0.4")
    loaded = load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
    assert (loaded.futures_basis_size_pct, loaded.futures_basis_deep_pct) == (130.0, 0.4)
    assert EDITABLE["FUTURES_BASIS_SIZE_PCT"].attr == "futures_basis_size_pct"
    assert EDITABLE["FUTURES_BASIS_DEEP_PCT"].kind == "float"
    assert not _setting_notes(replace(fcfg, strategy="futures", futures_basis_size_pct=130.0))  # Açılış notu yok (rapor 34: bilgilendirme notları kaldırıldı)
    assert not any("FUTURES_BASIS_SIZE_PCT" in n for n in _setting_notes(replace(fcfg, strategy="futures")))
    p = fb.params_from_cfg(replace(fcfg, futures_basis_size_pct=130.0, futures_basis_deep_pct=0.4))
    assert (p.basis_size_pct, p.basis_deep_pct) == (130.0, 0.4)


def test_wave_page_rows_buttons_and_text(fcfg, tmp_path):
    import settings_menu as sm
    rows = {r.key: r for r in sm.PAGES["fw"].rows if isinstance(r, sm.Row)}
    size, deep = rows["FUTURES_BASIS_SIZE_PCT"], rows["FUTURES_BASIS_DEEP_PCT"]
    on = replace(fcfg, strategy="futures", futures_tactic="crash", futures_basis_size_pct=130.0)
    off = replace(on, futures_basis_size_pct=100.0)
    assert size.show(off) and not deep.show(off) and deep.show(on)
    assert size.fmt(100) == "Vadeli-spot farkı: kapalı" and size.fmt(130) == "Vadeli-spot farkı: %130"
    assert deep.fmt(0.5) == "vadeli spotun %0.5 altı"
    bot, _fake, notes, _clock = make_fbot(replace(fcfg, futures_tactic="crash"), tmp_path)
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=gizli\n", encoding="utf-8")
    bot.settings_path = env
    press(bot, "pg:fw")
    text, keyboard, _mid = notes.menus[-1]
    data = [d for row in keyboard for _l, d in row]
    assert "⚖️ Vadeli-spot farkına göre boy kapalı." in text and "kv:fw:FUTURES_BASIS_SIZE_PCT:130" in data
    assert all(len(d.encode()) <= 64 for d in data)
    press(bot, "kv:fw:FUTURES_BASIS_SIZE_PCT:130")
    assert bot.cfg.futures_basis_size_pct == 130.0 and "FUTURES_BASIS_SIZE_PCT=130" in env.read_text()
    assert "spotun %0.5'ten fazla altındaysa (zorla satış izi) pozisyon <b>%130</b>" in notes.menus[-1][0]
