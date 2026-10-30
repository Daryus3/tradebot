"""Ortalamaya dönüş (mean_revert) stratejisi ve strateji turnuvası (--turnuva)."""
from dataclasses import replace

import pytest

import backtest
from backtest import (TOURNAMENT_ENTRIES, TOURNAMENT_EXITS, TOURNAMENT_ROTATIONS, entry_combos,
                      format_tournament_report, run_tournament, simulate, strategy_combos, sweep_combos)
from config import MEAN_REVERSION_PROFILE, ConfigError, _setting_notes, load_config, strategy_family, with_strategy
from risk import ExitReason
from strategy import make_strategy

from test_backtest import no_pause  # noqa: F401 - istek beklemesi kapalı
from test_swing import DAYS, END_TIME_MS, H, N_BARS, START_MS, HourClient, k, uptrend


def dip_rows(drop=0.98, bars=4, n=320, t=0, p0=100.0, vol=300_000.0):
    """Güçlü yükseliş trendi → birkaç saatlik sert düşüş (20 saatlik ortalamanın 2σ altı) → açık mum."""
    rows, t, p = uptrend(n, drift=1.003, t=t, p=p0)
    for _ in range(bars):
        o = p
        p *= drop
        rows.append(k(t, o, o * 1.001, p * 0.998, p, vol=vol))
        t += H
    rows.append(k(t, p, p * 1.001, p * 0.999, p, vol=30_000))
    return rows


def mirror(rows):
    """Fiyatı ters çevirir (yükseliş → düşüş trendi), mum yapısını korur."""
    top = max(float(r[2]) for r in rows) * 2
    return [[r[0], str(top - float(r[1])), str(top - float(r[3])), str(top - float(r[2])), str(top - float(r[4])),
             *r[5:]] for r in rows]


def test_mean_revert_signal_and_rejections(cfg):
    mr = make_strategy(None, replace(cfg, strategy="mean_revert"))
    assert mr.interval == mr.bar_interval == "1h" and mr.candidate_limit == cfg.mr_universe and mr.uses_room
    sig, why = mr.analyze("ABCUSDT", dip_rows())
    assert why == "" and sig.metrics["kind"] == "mean_revert" and sig.metrics["z"] <= -2
    assert sig.metrics["room_pct"] > 0 and "Aşırı satım" in sig.note() and "σ" in sig.note()
    assert mr.analyze("ABCUSDT", dip_rows(drop=0.999))[1] == "aşırı satım yok"
    # Eşik daha derinse (ya da zayıf piyasada +0.5σ) aynı düşüş yetmez
    deeper = make_strategy(None, replace(cfg, strategy="mean_revert", mr_band_std=-sig.metrics["z"] + 0.1))
    assert deeper.analyze("ABCUSDT", dip_rows())[1] == "aşırı satım yok"
    # Fiyat sinyal mumunun kapanışından %0.5'ten fazla yükseldiyse kovalanmaz
    rows = dip_rows()
    p = float(rows[-2][4]) * 1.01
    rows[-1] = k(rows[-1][0], p, p, p, p, vol=30_000)
    assert mr.analyze("ABCUSDT", rows)[1] == "fiyat kaçtı"
    # Düşüş trendindeki coinde aynı ölçüde aşırı satım alınmaz (düşen bıçak)
    down = mirror(uptrend(320, drift=1.003)[0])
    t, p = down[-1][0] + H, float(down[-1][4])
    for _ in range(4):
        o = p
        p *= 0.98
        down.append(k(t, o, o * 1.001, p * 0.998, p))
        t += H
    down.append(k(t, p, p, p, p))
    assert mr.analyze("ABCUSDT", down)[1] in ("trend yok", "aşırı satım yok")
    assert mr.analyze("ABCUSDT", dip_rows()[:100])[1] == "veri az"


def test_mean_revert_universe_is_most_liquid(cfg, fake_client):
    from exchange import parse_symbol_rules
    client = fake_client({"AAAUSDT": 1.0, "BBBUSDT": 1.0, "CCCUSDT": 1.0})
    rules = parse_symbol_rules(client.exchange_info(), "USDT")
    tickers = [{"symbol": s, "lastPrice": "1.0", "priceChangePercent": ch, "quoteVolume": vol, "highPrice": "1.05",
                "lowPrice": "0.95"} for s, ch, vol in (("AAAUSDT", "-6", "4000000"), ("BBBUSDT", "1", "90000000"),
                                                       ("CCCUSDT", "-1", "20000000"))]
    mr = make_strategy(client, replace(cfg, strategy="mean_revert", mr_universe=2))
    assert [t["symbol"] for t in mr.prefilter(tickers, rules, set(), 0.0)] == ["BBBUSDT", "CCCUSDT"]
    assert mr.candidate_key(-6.0, 4e6) > mr.candidate_key(1.0, 9e7)     # Hacmi büyük önce


def test_mean_revert_config_profile_and_notes(cfg, tmp_path, monkeypatch):
    assert strategy_family("mean_revert") == "mr"
    mr, applied = with_strategy(replace(cfg, take_profit_pct=50, max_hold_minutes=500), "mean_revert")
    assert applied and mr.take_profit_pct == MEAN_REVERSION_PROFILE["TAKE_PROFIT_PCT"] == 1.5
    assert mr.stop_loss_pct == 5 and mr.max_hold_minutes == 2880 and mr.backup_stop_pct == 8
    assert with_strategy(mr, "mean_revert") == (mr, False)
    assert not _setting_notes(mr)
    assert not _setting_notes(replace(mr, take_profit_pct=12, max_hold_minutes=7200))   # Turnuvadaki "trend" çıkışı
    notes = _setting_notes(replace(mr, max_hold_minutes=0))
    assert notes and "süre sınırı yok" in notes[0]
    narrow, _ = with_strategy(replace(cfg, max_24h_gain_pct=12.0), "mean_revert")
    assert narrow.max_24h_gain_pct == 25.0                 # 5 dk için daraltılmış sınır turnuvadaki gibi genişler
    for key, value in (("MR_BAND_STD", "0"), ("MR_UNIVERSE", "0")):
        monkeypatch.setenv(key, value)
        with pytest.raises(ConfigError):
            load_config(env_file=str(tmp_path / "x.env"), mode_override="paper")
        monkeypatch.delenv(key)


def _mr_series(dip_at=500):
    """İki coin aynı dipte (biri daha hacimli), dipten sonra ortalamaya dönüş; BTC yatay."""
    def coin(vol):
        rows = dip_rows(n=dip_at - 4, t=START_MS, p0=1.0, vol=vol)[:-1]   # Sentetik açık mumu at
        t, p = rows[-1][0] + H, float(rows[-1][4])
        for _ in range(6):                    # Dönüş: +%2 / saat → Kâr Al (+%1.5)
            o = p
            p *= 1.02
            rows.append(k(t, o, p * 1.002, o * 0.999, p, vol=vol))
            t += H
        while len(rows) < N_BARS:
            rows.append(k(t, p, p * 1.003, p * 0.997, p, vol=vol))
            t += H
        return rows
    btc = [k(START_MS + i * H, 30_000, 30_030, 29_970, 30_000, vol=5e8) for i in range(N_BARS)]
    return {"ABCUSDT": coin(400_000.0), "XYZUSDT": coin(200_000.0), "BTCUSDT": btc}


def test_mean_revert_backtest_trades_only_the_liquid_universe(cfg, fake_client, tmp_path):
    mr, _ = with_strategy(replace(cfg, mr_universe=1, regime_filter=False), "mean_revert")
    hc = HourClient(fake_client, _mr_series())
    data = backtest.prepare_backtest(mr, hc.client, days=DAYS, cache_dir=tmp_path / "c", progress=None,
                                     symbols=["ABCUSDT", "XYZUSDT"], end_time_ms=END_TIME_MS)
    assert data.interval == "1h" and "1h" in hc.client.intervals and "5m" not in hc.client.intervals
    res = simulate(mr, data)
    assert [t.symbol for t in res.trades] == ["ABCUSDT"]               # Yalnızca en hacimli (MR_UNIVERSE=1)
    assert res.trades[0].reason in (ExitReason.TAKE_PROFIT.value, ExitReason.TRAILING_STOP.value)
    assert res.trades[0].pnl > 0
    both = simulate(replace(mr, mr_universe=2, max_open_positions=2, start_capital=32.0), data)   # 2 pozisyonluk sermaye
    assert sorted(t.symbol for t in both.trades) == ["ABCUSDT", "XYZUSDT"]


def test_mean_revert_sweeps_use_small_target_grids(cfg):
    mr, _ = with_strategy(cfg, "mean_revert")
    rows = sweep_combos(mr)
    assert {c["TAKE_PROFIT_PCT"] for c, _ in rows[1:]} == {1.0, 1.5, 2.5}
    assert {c["MAX_HOLD_MINUTES"] for c, _ in rows[1:]} == {720, 1440, 2880}
    assert all(c["TRAILING_ACTIVATION_PCT"] < c["TAKE_PROFIT_PCT"] for c, _ in rows[1:])
    entries = entry_combos(mr, [0.0])
    assert {c["MR_BAND_STD"] for c, _ in entries} == {1.5, 2.0, 2.5}
    # Karşılaştırma yalnızca aynı çıkış mantıklı stratejilerle (turnuva kendi çıkışlarıyla karşılaştırır)
    assert [st["STRATEGY"] for st, _ in strategy_combos(mr)] == ["mean_revert", "rsi_divergence"]
    swing, _ = with_strategy(cfg, "swing")
    assert "mean_revert" not in [st["STRATEGY"] for st, _ in strategy_combos(swing)]


def test_tournament_tries_every_combination_and_reports(cfg, fake_client, tmp_path):
    hc = HourClient(fake_client, _mr_series())
    result = run_tournament(replace(cfg, max_open_positions=2), hc.client, days=DAYS, max_symbols=3, progress=None,
                            end_time_ms=END_TIME_MS, cache_dir=tmp_path / "c")
    regime_rows = 2 if result.data.regime is not None else 1
    assert len(result.rows) == len(TOURNAMENT_ENTRIES) * len(TOURNAMENT_EXITS) * regime_rows + len(
        TOURNAMENT_ROTATIONS)
    by = {(t.entry, t.exit, t.skip_weak): t for t in result.rows}
    small = by[("mean_revert", "küçük hedef", False)]
    assert small.row.trades >= 1 and small.row.win_rate == 100.0 and small.avg_win_pct > 0
    assert small.row.settings["TAKE_PROFIT_PCT"] == 1.5 and small.row.settings["STRATEGY"] == "mean_revert"
    assert {t.exit for t in result.rows if t.entry == "rotation"} == set(TOURNAMENT_ROTATIONS)
    text = format_tournament_report(result, cfg)
    assert "STRATEJİ TURNUVASI" in text and "Kaz%" in text and "Ort.kayıp" in text
    assert f"--turnuva --gun {DAYS:g} --bitis" in text
    assert "kârlılığı bu belirler, kazanma oranı değil" in text
    # Hiç işlem açmayan satır: neden yazılır (ör. küçük sermayede BTC'nin LOT adımı / toz sınırı)
    empty = by[("mean_revert", "küçük hedef", False)]
    empty.row = backtest.SweepRow(empty.row.settings, False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None)
    empty.rejects = {backtest.DUST_REASON: 12}
    text = format_tournament_report(result, cfg)
    assert ("⚠️ mean_revert + küçük hedef: hiç işlem açılmadı, sinyal geldiği halde alım 12 kez reddedildi (en sık "
            'neden: "LOT adımı çok büyük")') in text
    # CSV de yazılabilir (tüm ayar anahtarları bilinen ayarlar)
    path = backtest.save_sweep_csv([t.row for t in result.rows], tmp_path / "t.csv")
    assert len(path.read_text(encoding="utf-8-sig").splitlines()) == len(result.rows) + 1


def test_cli_tournament_defaults_to_one_year(cfg, monkeypatch, capsys):
    import binance_client
    import main
    monkeypatch.setattr(binance_client, "BinanceClient", lambda *a, **k: object())
    seen = {}

    def fake(c, client, days, max_symbols, progress=None, end_time_ms=None):
        seen.update(days=days, symbols=max_symbols)
        raise RuntimeError("veri yok")
    monkeypatch.setattr(backtest, "run_tournament", fake)
    assert main.run_backtest_cli(cfg, main.parse_args(["--backtest", "--turnuva"])) == 1
    assert seen == {"days": 365, "symbols": 40} and "Turnuva çalıştırılamadı" in capsys.readouterr().out
    main.run_backtest_cli(cfg, main.parse_args(["--backtest", "--turnuva", "--gun", "90"]))
    assert seen["days"] == 90


def test_live_scan_uses_hourly_klines_of_liquid_coins(cfg, fake_client):
    rows = dip_rows()
    price = float(rows[-1][4])

    class Client(fake_client):
        def __init__(self):
            super().__init__({"ABCUSDT": price, "LOWUSDT": price, "BTCUSDT": 30_000.0})
            self.calls = []

        def tickers_24h(self):
            return [{"symbol": s, "lastPrice": str(price), "priceChangePercent": "-3", "quoteVolume": vol,
                     "highPrice": str(price * 1.08), "lowPrice": str(price * 0.99)}
                    for s, vol in (("ABCUSDT", "90000000"), ("LOWUSDT", "4000000"))]

        def klines(self, symbol, interval="5m", limit=100, start_time=None, end_time=None):
            self.calls.append((symbol, interval, limit))
            if symbol == "BTCUSDT":
                return [k(i * 300_000, 30_000, 30_000, 30_000, 30_000) for i in range(limit)]
            return rows[-limit:]

    client = Client()
    from exchange import parse_symbol_rules
    rules = parse_symbol_rules(client.exchange_info(), "USDT")
    live = make_strategy(client, replace(cfg, strategy="mean_revert", regime_filter=False, mr_universe=1))
    result = live.scan(rules, set(), 0.0)      # 0 = toz kontrolü yok (sentetik fiyat ~300, LOT adımı 0.01)
    assert ("ABCUSDT", "1h", 400) in client.calls and not any(c[0] == "LOWUSDT" for c in client.calls)
    assert [s.symbol for s in result.signals] == ["ABCUSDT"], result.rejects
