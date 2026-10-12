import csv
import time
from datetime import datetime, timedelta, timezone

import pytest

import stats
from risk import Position
from state import TradeLog
from stats import (OPTIONAL_COLUMNS, compute_stats, day_range, format_period_report, format_recent_trades,
                   format_stats, kill_switch_probability, load_trades, reason_label, stats_message,
                   trade_pnls, trades_between, week_range)

T0 = datetime(2026, 10, 5, 9, 0, 0)   # Pazartesi
FMT = "%Y-%m-%d %H:%M:%S"


def row(i, pnl, symbol="SOLUSDT", reason="TAKE_PROFIT", cost=11.0, hold=30.0, fee=0.022, closed=None,
        extra=None):
    """TradeLog.BASE_HEADER sırasıyla bir CSV satırı (+ isteğe bağlı ek sütun değerleri)."""
    closed = closed or T0 + timedelta(hours=i, minutes=hold)
    opened = closed - timedelta(minutes=hold)
    values = [opened.strftime(FMT), closed.strftime(FMT), symbol, "10.00000000", "1.10000000",
              "1.20000000", f"{cost:.4f}", f"{cost + pnl:.4f}", f"{pnl:.4f}",
              f"{pnl / cost * 100:.2f}", f"{fee:.4f}", reason, f"{hold:.1f}"]
    return values + list(extra or [])


def write_csv(path, header, rows, delimiter=","):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=delimiter)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def make_trade(i, pnl, symbol="SOLUSDT", reason="TAKE_PROFIT", cost=11.0, hold=30.0, fee=0.02, closed=None):
    """load_trades çıktısıyla aynı biçimde bellekte işlem dict'i."""
    closed = closed or T0 + timedelta(hours=i)
    return {"acilis": closed - timedelta(minutes=hold), "kapanis": closed, "parite": symbol,
            "miktar": 10.0, "alis_fiyati": 1.1, "satis_fiyati": 1.2, "maliyet_usdt": cost,
            "net_gelir_usdt": cost + pnl, "kar_zarar_usdt": pnl, "kar_zarar_yuzde": pnl / cost * 100,
            "komisyon_usdt": fee, "neden": reason, "sure_dk": hold}


# ---------------------------------------------------------------- load_trades
def test_load_missing_file_returns_empty(tmp_path):
    assert load_trades(tmp_path / "yok.csv") == []
    assert load_trades(str(tmp_path / "yok.csv")) == []


def test_load_full_format_with_optional_columns(tmp_path):
    extra = ["72.5", "-5.1", "48.2", "1.8", "0.05", "-0.3", "TREND", "8.1", "-2.0"]
    path = write_csv(tmp_path / "t.csv", TradeLog.BASE_HEADER + list(OPTIONAL_COLUMNS),
                     [row(2, -1.1, "DOGEUSDT", "STOP_LOSS", extra=extra), row(1, 1.65, extra=extra)])
    trades = load_trades(path)
    assert [t["parite"] for t in trades] == ["SOLUSDT", "DOGEUSDT"]   # kapanışa göre sıralı
    t = trades[0]
    assert isinstance(t["acilis"], datetime) and isinstance(t["kapanis"], datetime)
    assert t["kapanis"] - t["acilis"] == timedelta(minutes=30)
    assert t["kar_zarar_usdt"] == pytest.approx(1.65)
    assert t["maliyet_usdt"] == pytest.approx(11.0) and t["komisyon_usdt"] == pytest.approx(0.022)
    assert t["sure_dk"] == pytest.approx(30.0) and t["neden"] == "TAKE_PROFIT"
    assert t["sinyal_puani"] == pytest.approx(72.5) and t["rsi"] == pytest.approx(48.2)
    assert t["piyasa_rejimi"] == "TREND" and t["max_zarar_pct"] == pytest.approx(-2.0)


def test_load_old_format_missing_optional_columns(tmp_path):
    path = write_csv(tmp_path / "eski.csv", TradeLog.BASE_HEADER, [row(0, 1.0), row(1, -0.5)])
    trades = load_trades(path)
    assert len(trades) == 2
    for col in OPTIONAL_COLUMNS:
        assert trades[0][col] is None


def test_load_tolerates_missing_base_columns(tmp_path):
    """sure_dk / komisyon sütunu olmayan (çok eski/elle düzenlenmiş) dosya."""
    header = ["acilis", "kapanis", "parite", "kar_zarar_usdt", "neden"]
    path = write_csv(tmp_path / "kisa.csv", header, [
        ["2026-10-05 09:00:00", "2026-10-05 10:30:00", "btcusdt", "0.75", "take_profit"],
    ])
    [t] = load_trades(path)
    assert t["parite"] == "BTCUSDT" and t["neden"] == "TAKE_PROFIT"
    assert t["sure_dk"] == pytest.approx(90.0)          # zamanlardan hesaplanır
    assert t["komisyon_usdt"] == 0.0 and t["maliyet_usdt"] is None
    assert t["kar_zarar_yuzde"] is None and t["rsi"] is None


def test_load_skips_corrupt_rows_and_keeps_unknown_columns(tmp_path):
    path = tmp_path / "bozuk.csv"
    good = row(0, 1.0)
    lines = [
        ",".join(TradeLog.BASE_HEADER + ["not"]),
        ",".join(good + ["ilk"]),
        "",                                                     # boş satır
        ",".join(row(1, 2.0)[:5]),                              # yarım yazılmış satır
        ",".join(["dun", "bugun"] + row(2, 1.0)[2:]),           # bozuk tarih
        ",".join(row(3, 1.0)[:8] + ["abc"] + row(3, 1.0)[9:]),  # bozuk K/Z
        ",".join(row(4, 1.0)[:3] + ["x1"] + row(4, 1.0)[4:]),   # bozuk sayı (miktar)
        ",".join(TradeLog.BASE_HEADER),                              # tekrarlanan başlık
        ",".join(row(5, -0.4, "OPUSDT", "STOP_LOSS")),
        "\x00\x00\x00",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    trades = load_trades(path)
    assert [t["parite"] for t in trades] == ["SOLUSDT", "OPUSDT"]
    assert trades[0]["not"] == "ilk"
    assert trades[1]["kar_zarar_usdt"] == pytest.approx(-0.4)


def test_load_old_header_with_new_longer_rows(tmp_path):
    """Eski başlıklı dosyaya yeni sürüm uzun satır eklerse fazlalar isteğe bağlı sütunlara eşlenir."""
    extra = ["65", "-4.0", "52", "2.1", "0.04", "-0.2", "YATAY", "6.0", "-1.5"]
    path = write_csv(tmp_path / "karisik.csv", TradeLog.BASE_HEADER, [row(0, 1.0), row(1, 2.0, extra=extra)])
    old, new = load_trades(path)
    assert old["sinyal_puani"] is None and old["piyasa_rejimi"] is None
    assert new["sinyal_puani"] == pytest.approx(65) and new["piyasa_rejimi"] == "YATAY"
    assert new["max_kar_pct"] == pytest.approx(6.0)


def test_load_excel_semicolon_decimal_comma(tmp_path):
    path = tmp_path / "excel.csv"
    data = [x.replace(".", ",") if i >= 3 and i != 11 else x for i, x in enumerate(row(0, -1.25))]
    path.write_text("﻿" + ";".join(TradeLog.BASE_HEADER) + "\n" + ";".join(data) + "\n", encoding="utf-8")
    [t] = load_trades(path)
    assert t["kar_zarar_usdt"] == pytest.approx(-1.25) and t["maliyet_usdt"] == pytest.approx(11.0)


def test_load_reads_what_tradelog_writes(tmp_path):
    log = TradeLog(tmp_path / "trades_paper.csv")
    now = time.time()
    pos = Position("PEPEUSDT", "PEPE", 1000.0, 0.011, 11.0, now - 600, buy_fee_usdt=0.011)
    log.append(pos, 0.0125, 12.4875, 0.0235, "TAKE_PROFIT", now)
    [t] = load_trades(log.path)
    assert t["parite"] == "PEPEUSDT" and t["neden"] == "TAKE_PROFIT"
    assert t["kar_zarar_usdt"] == pytest.approx(1.4875)
    assert t["sure_dk"] == pytest.approx(10.0)


# ---------------------------------------------------------------- compute_stats
def test_compute_stats_known_values():
    pnls = [2.0, 3.0, -1.0, -1.0, -1.0, 4.0, -2.0]
    syms = ["A", "B", "A", "C", "C", "A", "B"]
    reasons = ["TAKE_PROFIT", "TAKE_PROFIT", "STOP_LOSS", "STOP_LOSS", "MAX_HOLD", "TRAILING_STOP", "STOP_LOSS"]
    trades = [make_trade(i, p, s, r, hold=10.0 * (i + 1)) for i, (p, s, r) in enumerate(zip(pnls, syms, reasons))]
    st = compute_stats(list(reversed(trades)))   # sıra karışık verilse de kapanışa göre hesaplanır
    assert st["count"] == 7 and st["wins"] == 3 and st["losses"] == 4
    assert st["win_rate"] == pytest.approx(300 / 7)
    assert st["total_pnl"] == pytest.approx(4.0)
    assert st["avg_win"] == pytest.approx(3.0) and st["avg_loss"] == pytest.approx(1.25)
    assert st["expectancy"] == pytest.approx(4 / 7)
    assert st["profit_factor"] == pytest.approx(9 / 5)
    assert st["best"]["kar_zarar_usdt"] == 4.0 and st["worst"]["kar_zarar_usdt"] == -2.0
    assert st["max_win_streak"] == 2 and st["max_loss_streak"] == 3
    assert st["total_fees"] == pytest.approx(0.14)
    assert st["avg_hold_min"] == pytest.approx(40.0)
    assert st["max_drawdown"] == pytest.approx(3.0)      # 5 → 2
    assert st["breakeven_win_rate"] == pytest.approx(1.25 / 4.25 * 100)
    assert st["by_reason"]["STOP_LOSS"] == {"count": 3, "pnl": pytest.approx(-4.0)}
    assert st["by_reason"]["MAX_HOLD"]["count"] == 1
    assert st["by_symbol"]["A"] == {"count": 3, "pnl": pytest.approx(5.0), "wins": 2}
    assert st["by_symbol"]["C"]["wins"] == 0


def test_compute_stats_drawdown_from_start_and_zero_pnl_is_win():
    st = compute_stats([make_trade(0, -1.0), make_trade(1, -2.0), make_trade(2, 3.0), make_trade(3, 0.0)])
    assert st["max_drawdown"] == pytest.approx(3.0)
    assert st["wins"] == 2 and st["losses"] == 2   # BotState gibi: K/Z ≥ 0 kazanç


def test_compute_stats_empty_and_only_wins():
    st = compute_stats([])
    assert st["count"] == 0 and st["best"] is None and st["worst"] is None
    assert st["profit_factor"] is None and st["breakeven_win_rate"] is None
    assert st["win_rate"] == 0.0 and st["by_symbol"] == {} and st["max_drawdown"] == 0.0

    st = compute_stats([make_trade(0, 1.0), make_trade(1, 2.0)])
    assert st["profit_factor"] == float("inf")
    assert st["breakeven_win_rate"] is None and st["avg_loss"] == 0.0


# ---------------------------------------------------------------- Monte Carlo
def test_kill_switch_probability_needs_ten_trades():
    assert kill_switch_probability([1.0] * 9, 0.0, 5.0) is None
    assert kill_switch_probability([1.0] * 10, 0.0, 5.0) == 0.0


def test_kill_switch_probability_is_deterministic():
    pnls = [1.6, -1.1, 1.6, -1.1, -1.1, 0.5, -0.8, 1.2, -1.1, 1.6, -1.1, 0.3]
    a = kill_switch_probability(pnls, 0.0, 5.0, horizon=50, sims=2000, seed=7)
    b = kill_switch_probability(pnls, 0.0, 5.0, horizon=50, sims=2000, seed=7)
    assert a == b and 0.0 < a < 1.0
    # Aynı tohumla daha büyük gerçekleşmiş zarar → olasılık azalmaz
    worse = kill_switch_probability(pnls, -3.0, 5.0, horizon=50, sims=2000, seed=7)
    assert worse >= a


def test_kill_switch_probability_edges():
    losses = [-1.0] * 10
    assert kill_switch_probability(losses, 0.0, 5.0, horizon=4) == 0.0
    assert kill_switch_probability(losses, 0.0, 5.0, horizon=5) == 1.0   # tam limitte tetiklenir
    assert kill_switch_probability([2.0] * 20, -6.0, 5.0) == 1.0         # zaten limitin altında
    assert kill_switch_probability(losses, 0.0, 5.0, horizon=0) is None


def test_kill_switch_probability_reasonable_estimate():
    # Yarı yarıya ±1: 5 adımda -5'e değme olasılığı tam olarak 1/32
    pnls = [1.0, -1.0] * 10
    p = kill_switch_probability(pnls, 0.0, 5.0, horizon=5, sims=20000, seed=1)
    assert p == pytest.approx(1 / 32, abs=0.006)


# ---------------------------------------------------------------- format_stats
def sample_trades(n, symbols=("OPUSDT", "SOLUSDT", "DOGEUSDT", "ARBUSDT", "PEPEUSDT", "WIFUSDT")):
    reasons = ["TAKE_PROFIT", "STOP_LOSS", "TRAILING_STOP", "BREAKEVEN", "MAX_HOLD", "KILL_SWITCH",
               "EXTERNAL", "MANUAL", "BTC_CRASH", "GARIP_NEDEN"]
    out = []
    for i in range(n):
        pnl = [1.6, -1.1, 0.5, 0.0, -0.3][i % 5] * (1 + (i % len(symbols)) / 10)
        out.append(make_trade(i, pnl, symbols[i % len(symbols)], reasons[i % len(reasons)]))
    return out


def test_format_stats_content():
    trades = sample_trades(30)
    st = compute_stats(trades)
    prob = kill_switch_probability(trade_pnls(trades), 1.0, 5.0)
    msg = format_stats(st, "USDT", 1.0, 5.0, 15.0, 10.0, prob)
    assert "yanıltıcı" not in msg
    assert "%40.0" in msg                                      # 10 / (15 + 10)
    assert f"%{st['win_rate']:.1f}" in msg
    for label in ["Kâr Al", "Zarar Kes", "İzleyen Stop", "Başabaş", "Süre Doldu", "Kill-switch",
                  "Bot Dışı", "Elle Satış", "BTC Çöküşü", "GARIP_NEDEN"]:
        assert label in msg
    assert "TAKE_PROFIT" not in msg
    assert "En iyi coinler" in msg and "En kötü coinler" in msg
    ranked = sorted(st["by_symbol"].items(), key=lambda kv: kv[1]["pnl"])
    assert ranked[-1][0] in msg and ranked[0][0] in msg
    assert f"%{prob * 100:.1f}" in msg and "Monte Carlo" in msg
    assert "kalan 6.00 USDT" in msg
    assert len(msg) < 4000


def test_format_stats_small_sample_warning_and_no_prob():
    trades = sample_trades(5)
    msg = format_stats(compute_stats(trades), "USDT", -1.0, 5.0, 15.0, 10.0, None)
    assert "yanıltıcı" in msg and "en az 10 işlem" in msg


def test_format_stats_empty():
    msg = format_stats(compute_stats([]), "USDT", 0.0, 5.0, 15.0, 10.0, None)
    assert "Henüz kapanmış işlem yok" in msg and "kalan 5.00" in msg


def test_format_stats_escapes_and_stays_short():
    syms = tuple(f"<X{i}>USDT" + "Z" * 40 for i in range(200))
    trades = sample_trades(400, syms) + [make_trade(500, -1.0, "BADUSDT", "<script>" * 30)]
    msg = format_stats(compute_stats(trades), "USDT", 0.0, 5.0, 15.0, 10.0, 0.5)
    assert "<X" not in msg and "&lt;X" in msg and "<script>" not in msg
    assert len(msg) <= 4000


def test_stats_message_shortcut(cfg):
    msg = stats_message(sample_trades(12), cfg, realized_pnl=0.5)
    assert "Performans" in msg and "Monte Carlo" in msg and cfg.quote_asset in msg


def test_reason_label():
    assert reason_label("TRAILING_STOP") == "İzleyen Stop"
    assert reason_label("btc_crash") == "BTC Çöküşü"
    assert reason_label("YENI") == "YENI" and reason_label("") == "?"


# ---------------------------------------------------------------- dönem raporu
def test_period_report_filters_half_open_range():
    start, end = datetime(2026, 10, 6), datetime(2026, 10, 7)
    trades = [
        make_trade(0, 5.0, "OLDUSDT", closed=start - timedelta(seconds=1)),
        make_trade(0, 1.2, "AUSDT", closed=start),
        make_trade(0, -0.7, "BUSDT", closed=start + timedelta(hours=5)),
        make_trade(0, 0.4, "CUSDT", closed=end - timedelta(seconds=1)),
        make_trade(0, 9.0, "NEXTUSDT", closed=end),
    ]
    assert [t["parite"] for t in trades_between(trades, start, end)] == ["AUSDT", "BUSDT", "CUSDT"]
    msg = format_period_report(trades, start, end, "📅 Günlük Rapor", "USDT",
                               extra_lines=["💰 Bakiye: <b>12.00 USDT</b>"])
    assert "OLDUSDT" not in msg and "NEXTUSDT" not in msg
    assert "<b>3</b>" in msg and "✅2 / ❌1" in msg
    assert "+0.90 USDT" in msg and "0.06 USDT" in msg       # net K/Z ve komisyon
    assert "En iyi: <b>AUSDT</b> +1.20" in msg and "En kötü: <b>BUSDT</b> -0.70" in msg
    assert msg.endswith("💰 Bakiye: <b>12.00 USDT</b>")
    assert msg.startswith("<b>📅 Günlük Rapor</b>")


def test_period_report_without_trades_and_aware_bounds():
    start = datetime(2026, 10, 6, tzinfo=timezone.utc)
    msg = format_period_report([make_trade(0, 1.0)], start, start + timedelta(days=1), "Haftalık <Özet>", "USDT")
    assert "işlem yok" in msg and "&lt;Özet&gt;" in msg


def test_period_report_single_trade():
    start, end = day_range(T0)
    msg = format_period_report([make_trade(1, 1.0, "AUSDT")], start, end, "Gün", "USDT")
    assert "📌 İşlem: <b>AUSDT</b>" in msg and "En kötü" not in msg


def test_day_and_week_range():
    now = datetime(2026, 10, 11, 23, 55)    # Pazar
    assert day_range(now) == (datetime(2026, 10, 11), datetime(2026, 10, 12))
    assert week_range(now) == (datetime(2026, 10, 5), datetime(2026, 10, 12))
    assert week_range(datetime(2026, 10, 5, 0, 0))[0] == datetime(2026, 10, 5)


# ---------------------------------------------------------------- son işlemler
def test_recent_trades_newest_first():
    trades = [make_trade(i, p, f"C{i}USDT", r) for i, (p, r) in
              enumerate([(1.0, "TAKE_PROFIT"), (-0.5, "STOP_LOSS"), (0.3, "TRAILING_STOP"), (-0.2, "MANUAL")])]
    msg = format_recent_trades(trades, n=3)
    lines = msg.splitlines()
    body = [ln for ln in lines if ln.startswith(("✅", "❌"))]
    assert [ln.split("<b>")[1].split("</b>")[0] for ln in body] == ["C3USDT", "C2USDT", "C1USDT"]
    assert body[0].startswith("❌") and "Elle Satış" in body[0]
    assert body[1].startswith("✅") and "İzleyen Stop" in body[1]
    assert "C0USDT" not in msg and "Son 3 İşlem" in msg
    assert "-0.40 USDT" in lines[-1]                       # gösterilenlerin toplamı


def test_recent_trades_empty_and_long():
    assert "Henüz" in format_recent_trades([])
    msg = format_recent_trades(sample_trades(300), n=300, quote_asset="USDT")
    assert len(msg) <= 4000 and "mesaj sınırı" in msg


def test_module_constants_match_tradelog():
    assert stats.BASE_COLUMNS == tuple(TradeLog.BASE_HEADER)
