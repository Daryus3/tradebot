"""Canlı vadeli bot: en hacimli N sıralamasının dayanıklılığı (geçici 24 saatlik hacim sıralaması, kayıtlı sıralama,
uyarı) ve canlı tarama kaydının backtest raporundaki "canlı bot karşılaştırması" bölümü."""
import json
from dataclasses import replace

import requests

import futures_backtest as fb
from config import scan_log_path
from futures_exchange import FuturesError
from strategy import FUT_BAR_MS
from test_futures import BASE_MS, fcfg, make_fbot  # noqa: F401 - pytest fikstürü
from test_futures_universe import META, TICKERS, UniCcxt, ucfg  # noqa: F401 - pytest fikstürü


def journal(bot):
    return [json.loads(line) for line in bot.scan_log_path.read_text(encoding="utf-8").splitlines()]


# ====================================================================== sıralama
def test_ranking_failure_uses_24h_volume_and_warns_then_recovers(ucfg, tmp_path, monkeypatch):
    import futures_exchange
    calls = []

    def broken(self, *a):
        calls.append(1)
        raise FuturesError("bağlantı reddedildi")
    monkeypatch.setattr(futures_exchange.FuturesExchange, "seasoned_ranking", broken)
    bot, fake, notes, clock = make_fbot(ucfg, tmp_path, fake=UniCcxt(TICKERS, META))
    # Geçici evren: köklü paritelerin 24 saatlik hacmi (MID yaş filtresine takılır, stabil / hisse / endeks yok)
    assert bot.universe == ["BTCUSDT", "ETHUSDT", "SOLUSDT"] and bot._ranking_source == "24h"
    assert "geçici: 24 saatlik hacme göre" in bot.universe_label()
    assert any("sıralaması alınamadı" in m and "bağlantı reddedildi" in m for m in notes.messages)
    bot._refresh_universe(clock.t + 60)                                   # 10 dk dolmadan yeniden denenmez
    assert len(calls) == 1
    monkeypatch.setattr(futures_exchange.FuturesExchange, "seasoned_ranking",
                        lambda self, *a: [("SOLUSDT", 3.0), ("BTCUSDT", 2.0), ("ETHUSDT", 1.0)])
    bot._refresh_universe(clock.t + 700)
    assert bot.universe == ["SOLUSDT", "BTCUSDT", "ETHUSDT"] and bot._ranking_source == "avg"
    assert any(m.startswith("✅ En hacimli coin sıralaması alındı") for m in notes.messages)
    assert bot.state.futures_ranking["symbols"][:3] == ["SOLUSDT", "BTCUSDT", "ETHUSDT"]


def test_empty_ranking_without_fallback_keeps_list_and_shows_in_scan_report(fcfg, tmp_path, monkeypatch):
    import futures_exchange
    monkeypatch.setattr(futures_exchange.FuturesExchange, "seasoned_ranking", lambda self, *a: [])
    monkeypatch.setattr(futures_exchange.FuturesExchange, "eligible_symbols", lambda self, *a: [])
    cfg = replace(fcfg, futures_universe=20, futures_universe_with_list=True)
    bot, fake, notes, clock = make_fbot(cfg, tmp_path, fake=UniCcxt(TICKERS, META))
    assert bot.universe == [] and bot.universe_label().startswith("yalnız liste")
    assert sum("sıralaması alınamadı" in m for m in notes.messages) == 1
    bot.scan_and_enter(clock.t)
    report = bot.scan_report()
    assert "🪙 Evren: yalnız liste" in report and "sıralama boş geldi" in report


def test_saved_ranking_is_reused_after_restart(ucfg, tmp_path):
    fake = UniCcxt(TICKERS, META)
    bot, fake, _notes, clock = make_fbot(ucfg, tmp_path, fake=fake)
    saved = bot.state.futures_ranking
    assert saved["symbols"][:3] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"] and saved["key"] == [1095, 180]
    (tmp_path / "2").mkdir()
    bot2, _f, _n, _c = make_fbot(ucfg, tmp_path / "2", fake=fake, clock=clock)
    fake.calls.clear()
    bot2._ranking, bot2._ranking_key = [], ()                            # Yeni süreç: bellekte sıralama yok,
    bot2.state.futures_ranking = dict(saved)                             # durum dosyasında var (yeniden başlatma)
    bot2._refresh_universe(clock.t + 60)
    assert bot2.universe == ["BTCUSDT", "ETHUSDT", "SOLUSDT"] and bot2._ranking_source == "avg"
    assert not [c for c in fake.calls if c[0] == "klines" and c[2] == "1d"]
    bot2._ranking = []
    bot2._refresh_universe(clock.t + 25 * 3600)                          # Kayıt bayat: yeniden sıralanır
    assert [c for c in fake.calls if c[0] == "klines" and c[2] == "1d"]


def test_seasoned_ranking_skips_few_network_errors_aborts_on_many(ucfg, tmp_path):
    fake = UniCcxt(TICKERS, META)
    bot, fake, _notes, _clock = make_fbot(ucfg, tmp_path, fake=fake)
    ex = bot.exchange
    orig = ex.average_volume

    def flaky(symbol, days):
        if symbol == "ETHUSDT":
            raise requests.ConnectionError("zaman aşımı")
        return orig(symbol, days)
    ex.average_volume = flaky
    assert [s for s, _v in ex.seasoned_ranking(1095, 180)] == ["BTCUSDT", "SOLUSDT", "DOGEUSDT"]
    ex.average_volume = lambda symbol, days: (_ for _ in ()).throw(requests.ConnectionError("ağ yok"))
    assert ex.seasoned_ranking(1095, 180) == []      # 4 parite (eşik 5): hepsi atlanır, sıralama boş
    ex.eligible_symbols = lambda min_age=0.0: [f"C{i}USDT" for i in range(40)]
    try:
        ex.seasoned_ranking(1095, 180)                # 40 paritenin 10'undan fazlası alınamadı: vazgeçilir
        raised = False
    except FuturesError as exc:
        raised = "paritenin hacim geçmişi alınamadı" in str(exc)
    assert raised


# ====================================================================== tarama kaydı
def test_scan_journal_records_scans_and_skip_reasons(ucfg, tmp_path):
    bot, fake, _notes, clock = make_fbot(ucfg, tmp_path, fake=UniCcxt(TICKERS, META))
    assert bot.scan_log_path == scan_log_path(tmp_path, "paper")
    bot.next_scan_at = 0
    bot.tick()                                                            # Tarama yapılır
    bot.state.paused = True
    clock.t += 300
    bot.next_scan_at = 0
    bot.tick()                                                            # Durdurulmuş: neden kaydedilir
    rows = journal(bot)
    assert rows[0]["t"] % FUT_BAR_MS == 0 and rows[0]["u"] == ["BTC", "ETH", "SOL"] and "a" in rows[0]
    assert rows[1]["skip"].startswith("yeni işlemler durdurulmuş")
    bot.state.paused = False
    clock.t += 300
    bot.next_scan_at = 0
    bot.tick()
    assert "u" not in journal(bot)[-1]                                    # Evren yalnızca değişince yazılır


def test_scan_journal_outcomes_and_prune(ucfg, tmp_path):
    bot, fake, _notes, clock = make_fbot(ucfg, tmp_path, fake=UniCcxt(TICKERS, META))
    bot._journal_scan(BASE_MS, ["BTCUSDT", "ETHUSDT"], ["ETHUSDT"], 9, {"ETHUSDT": "açıldı"}, 1, ["BTCUSDT"])
    row = journal(bot)[-1]
    assert row == {"t": BASE_MS, "u": ["BTC", "ETH"], "a": ["ETH"], "b": 9, "s": {"ETH": "açıldı"}, "x": ["BTC"],
                   "e": 1}
    old = BASE_MS - 40 * 86_400_000
    bot.scan_log_path.write_text(json.dumps({"t": old, "u": ["X"], "a": []}) + "\n"
                                 + json.dumps({"t": BASE_MS, "a": ["X"]}) + "\n", encoding="utf-8")
    bot._prune_scan_log()
    rows = journal(bot)
    assert rows == [{"t": BASE_MS, "a": ["X"], "u": ["X"]}]              # Eski satır silindi, evren taşındı


# ====================================================================== backtest karşılaştırması
def _trade(symbol, ms):
    return fb.FutTrade(symbol, "long", ms, ms + 600_000, 1.0, 1.01, "TP", 3, 1.0, 3.0, 0.0, 0.0, 0.03, 3.0, 0.0, 0.0,
                       0.0, False, False, 60_000)


def test_live_comparison_explains_each_backtest_trade(fcfg, tmp_path):
    t0 = BASE_MS
    path = tmp_path / "futures_scans_paper.jsonl"
    rows = [{"t": t0, "u": ["BTC", "ETH", "SOL"], "a": ["ETH", "SOL"], "s": {"ETH": "açıldı", "SOL": "boş yer yok"}},
            {"t": t0 + FUT_BAR_MS, "skip": "yeni işlemler durdurulmuş (/durdur)"},
            {"t": t0 + 2 * FUT_BAR_MS, "a": ["BTC"], "x": ["SOL"]},
            {"t": t0 + 4 * FUT_BAR_MS, "a": []}]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    data = fb.FutData(t0, t0 + 6 * FUT_BAR_MS, [fb.Segment(t0 - 1, t0 + 6 * FUT_BAR_MS, ("BTC", "ETH", "SOL", "ZEC"))],
                      {}, ((), (), ()), (None, None), {}, None, "5m")
    trades = [_trade("ETHUSDT", t0), _trade("SOLUSDT", t0), _trade("BTCUSDT", t0 + FUT_BAR_MS),
              _trade("SOLUSDT", t0 + 2 * FUT_BAR_MS), _trade("BTCUSDT", t0 + 2 * FUT_BAR_MS),
              _trade("ZECUSDT", t0 + 4 * FUT_BAR_MS), _trade("ETHUSDT", t0 + 3 * FUT_BAR_MS),
              _trade("ETHUSDT", t0 + 9 * FUT_BAR_MS)]
    text = "\n".join(fb.live_comparison(fcfg, data, trades, path=path))
    assert "CANLI BOT KARŞILAŞTIRMASI" in text and "Kayıtlı 5 dk mum: 4 / 5" in text
    assert "backtestte olup canlıda olmayan: ZEC" in text
    assert "ETH: ✅ canlı da açtı" in text and "SOL: sinyal vardı ama boş yer yok" in text
    assert "BTC: taranmadı: yeni işlemler durdurulmuş" in text and "SOL: coin beklemedeydi" in text
    assert "BTC: coin analiz edildi ama canlı mumlarda sinyal çıkmadı" in text
    assert "ZEC: coin canlı evrende yoktu (canlı 3 coin tarıyordu)" in text
    assert "ETH: kayıt yok" in text                                      # O mumda kayıt yok (bot kapalı)
    assert "Backtest işlemleri bu aralıkta: 7 | canlı da açtı: 1" in text   # Kayıt sonrası işlem sayılmaz
    assert fb.live_comparison(fcfg, data, trades, path=tmp_path / "yok.jsonl") == []


def test_run_cli_report_includes_live_comparison(fcfg, tmp_path, monkeypatch):
    """run_cli raporu: kayıt varsa karşılaştırma bölümü A) bölümünden önce gelir (veri ve simülasyon sahte)."""
    t0 = BASE_MS
    data = fb.FutData(t0, t0 + FUT_BAR_MS, [fb.Segment(t0 - 1, t0 + FUT_BAR_MS, ("BTC",))], {}, ((), (), ()),
                      (None, None), {}, None, "5m")
    seen = {}
    monkeypatch.setattr(fb, "prepare", lambda *a, **k: data)
    monkeypatch.setattr(fb, "find_signals", lambda cfg, d: ([], fb.Funnel()))
    monkeypatch.setattr(fb, "load_windows", lambda *a, **k: {})
    monkeypatch.setattr(fb, "evaluate", lambda *a, **k: "ev")
    monkeypatch.setattr(fb, "live_comparison", lambda cfg, d, trades: ["🔍 CANLI BOT KARŞILAŞTIRMASI (deneme)"])

    class Ev:
        trades = []

        class chained:
            trades = []
    monkeypatch.setattr(fb, "evaluate", lambda *a, **k: Ev)

    def fake_report(cfg, d, ev, funnel, levs, grid, variants, live=None):
        seen["live"] = live
        return "RAPOR"
    monkeypatch.setattr(fb, "format_report", fake_report)
    monkeypatch.setattr(fb, "save_trades_csv", lambda trades, path: path)
    report, _chart, _ev = fb.run_cli(fcfg, 7, None, tmp_path, chart=False, sensitivity=False, progress=lambda m: None)
    assert seen["live"] == ["🔍 CANLI BOT KARŞILAŞTIRMASI (deneme)"] and report.startswith("RAPOR")


def test_scan_error_is_journaled_and_reraised(ucfg, tmp_path):
    bot, fake, _notes, clock = make_fbot(ucfg, tmp_path, fake=UniCcxt(TICKERS, META))

    def boom(now):
        raise requests.ConnectionError("Binance'e ulaşılamadı")
    bot.scan_and_enter = boom
    bot.next_scan_at = 0
    try:
        bot.tick()
        raised = False
    except requests.ConnectionError:
        raised = True                                                     # Ana döngü her zamanki gibi ele alır
    assert raised and journal(bot)[-1]["skip"].startswith("tarama hatası (ConnectionError: Binance'e ulaşılamadı")
