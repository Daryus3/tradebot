"""
Performans istatistikleri ve dönem raporları.

İşlem geçmişi CSV'sini (`data/trades_<mod>.csv`, bkz. `state.TradeLog`) okur,
kazanma oranı / beklenti / kâr faktörü / drawdown gibi istatistikleri hesaplar
ve Telegram'a gönderilecek HTML mesajlarını üretir:

    /istatistik  → format_stats(...)
    /islemler    → format_recent_trades(...)
    günlük/haftalık rapor → format_period_report(...)

CSV'deki eksik/fazla sütunlara dayanıklıdır: eski dosyalarda olmayan isteğe bağlı
sütunlar (sinyal_puani, rsi, ...) None olur, bozuk satırlar atlanır.
"""
from __future__ import annotations

import csv
import math
import random
from datetime import datetime, timedelta
from itertools import accumulate
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from logger import get_logger
from state import TradeLog
from telegram_bot import LINE, esc, fmt_duration, fmt_price, fmt_signed, fmt_usdt

log = get_logger()

MAX_MESSAGE_LEN = 4000
MIN_RELIABLE_TRADES = 20      # Bundan az işlemde istatistikler yanıltıcı olabilir
MIN_MONTE_CARLO_TRADES = 10   # Monte Carlo için en az bu kadar geçmiş işlem gerekir

BASE_COLUMNS: Tuple[str, ...] = tuple(TradeLog.BASE_HEADER)
# İleride CSV'nin sonuna eklenecek isteğe bağlı sütunlar (eski dosyalarda yok → None)
OPTIONAL_COLUMNS: Tuple[str, ...] = (
    "sinyal_puani", "dusus_pct", "rsi", "hacim_orani", "spread_pct", "btc_1s_pct",
    "piyasa_rejimi", "max_kar_pct", "max_zarar_pct",
)
DATE_COLUMNS = ("acilis", "kapanis")
TEXT_COLUMNS = ("parite", "neden", "piyasa_rejimi")
NUMERIC_COLUMNS = tuple(c for c in BASE_COLUMNS + OPTIONAL_COLUMNS if c not in DATE_COLUMNS + TEXT_COLUMNS)
DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M")

REASON_LABELS: Dict[str, str] = {
    "TAKE_PROFIT": "Kâr Al",
    "STOP_LOSS": "Zarar Kes",
    "TRAILING_STOP": "İzleyen Stop",
    "BREAKEVEN": "Başabaş",
    "MAX_HOLD": "Süre Doldu",
    "KILL_SWITCH": "Kill-switch",
    "EXTERNAL": "Bot Dışı",
    "MANUAL": "Elle Satış",
    "BTC_CRASH": "BTC Çöküşü",
}
REASON_EMOJIS: Dict[str, str] = {
    "TAKE_PROFIT": "🎯", "STOP_LOSS": "🛑", "TRAILING_STOP": "🛡️", "BREAKEVEN": "⚖️",
    "MAX_HOLD": "⏱️", "KILL_SWITCH": "☠️", "EXTERNAL": "❔", "MANUAL": "✋", "BTC_CRASH": "📉",
}

PathLike = Union[str, Path]


# ---------------------------------------------------------------- yardımcılar
def reason_label(reason: Optional[str]) -> str:
    """Çıkış nedeninin Türkçe adı; bilinmeyen neden olduğu gibi döner."""
    if not reason:
        return "?"
    return REASON_LABELS.get(str(reason).strip().upper(), str(reason))


def _to_float(raw) -> Optional[float]:
    """'1.23' / '1,23' → float; boş/hatalı/NaN → None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip()
        if not text:
            return None
        if "," in text and "." not in text:  # Excel (TR) ondalık virgülü
            text = text.replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            return None
    return value if math.isfinite(value) else None


def _to_datetime(raw) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _naive_local(dt: datetime) -> datetime:
    """CSV zamanları yerel ve saat dilimsiz; karşılaştırma için aynı biçime getirir."""
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt


def _pnl(trade: dict) -> float:
    return _to_float(trade.get("kar_zarar_usdt")) or 0.0


def _pnl_pct(trade: dict) -> Optional[float]:
    pct = _to_float(trade.get("kar_zarar_yuzde"))
    if pct is None:
        cost = _to_float(trade.get("maliyet_usdt"))
        if cost:
            pct = _pnl(trade) / cost * 100
    return pct


def _hold_min(trade: dict) -> Optional[float]:
    minutes = _to_float(trade.get("sure_dk"))
    if minutes is None:
        opened, closed = trade.get("acilis"), trade.get("kapanis")
        if isinstance(opened, datetime) and isinstance(closed, datetime):
            minutes = (closed - opened).total_seconds() / 60
    return minutes


def _is_win(trade: dict) -> bool:
    """Bot sayaçlarıyla (BotState.wins) aynı kural: net K/Z ≥ 0 kazanç sayılır."""
    return _pnl(trade) >= 0


def _close_key(trade: dict) -> datetime:
    closed = trade.get("kapanis")
    return _naive_local(closed) if isinstance(closed, datetime) else datetime.min


def _sorted_by_close(trades: Iterable[dict]) -> List[dict]:
    return sorted(trades, key=_close_key)


def _money(x: float, quote_asset: str) -> str:
    return f"{fmt_signed(x)} {esc(quote_asset)}"


def _pct_text(pct: Optional[float]) -> str:
    return f" ({fmt_signed(pct)}%)" if pct is not None else ""


def _clip(lines: Sequence[str], limit: int = MAX_MESSAGE_LEN) -> str:
    """Satırları birleştirir; limit aşılırsa satır sınırından keser (HTML etiketleri satır içinde kapanır)."""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    tail = "…(kısaltıldı)"
    out: List[str] = []
    size = len(tail)
    for line in lines:
        if size + len(line) + 1 > limit:
            break
        out.append(line)
        size += len(line) + 1
    out.append(tail)
    return "\n".join(out)


# ---------------------------------------------------------------- CSV okuma
def _detect_delimiter(header_line: str) -> str:
    """Excel (TR) dosyayı ';' ile kaydetmiş olabilir."""
    return ";" if header_line.count(";") > header_line.count(",") else ","


def _parse_row(values: List[str], columns: List[str], needed: int) -> Optional[dict]:
    """Tek CSV satırını işlem dict'ine çevirir; bozuksa None."""
    if len(values) < needed:  # Yarım yazılmış satır (ör. elektrik kesintisi)
        return None
    raw = dict(zip(columns, (v.strip() for v in values)))
    closed = _to_datetime(raw.get("kapanis"))
    symbol = (raw.get("parite") or "").upper()
    pnl = _to_float(raw.get("kar_zarar_usdt"))
    if closed is None or not symbol or pnl is None:
        return None

    trade: dict = {}
    for col in BASE_COLUMNS:
        text = raw.get(col)
        if col in DATE_COLUMNS:
            trade[col] = _to_datetime(text)
        elif col in TEXT_COLUMNS:
            trade[col] = text or ""
        else:
            value = _to_float(text)
            if value is None and text:  # Sayı olması gereken yerde bozuk değer
                return None
            trade[col] = value
    trade["parite"] = symbol
    trade["neden"] = trade["neden"].upper()
    if trade["komisyon_usdt"] is None:
        trade["komisyon_usdt"] = 0.0
    if trade["sure_dk"] is None:
        trade["sure_dk"] = _hold_min(trade)

    for col in OPTIONAL_COLUMNS:
        text = raw.get(col)
        if col in TEXT_COLUMNS:
            trade[col] = text or None
        else:
            trade[col] = _to_float(text)  # Hatalı isteğe bağlı değer satırı bozmaz

    for col, text in raw.items():  # Bilinmeyen ek sütunlar metin olarak korunur
        if col and col not in trade:
            trade[col] = text
    return trade


def load_trades(path: PathLike) -> List[dict]:
    """İşlem CSV'sini okur (kapanış zamanına göre sıralı).

    Dosya yoksa []. acilis/kapanis → datetime, sayısal alanlar → float,
    eksik isteğe bağlı alanlar → None. Bozuk satırlar atlanır.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        log.warning("İşlem geçmişi okunamadı (%s): %s", path, exc)
        return []

    lines = [ln for ln in text.replace("\x00", "").splitlines() if ln.strip()]
    if not lines:
        return []
    delimiter = _detect_delimiter(lines[0])

    def split(line: str) -> Optional[List[str]]:
        try:
            return next(csv.reader([line], delimiter=delimiter))
        except (csv.Error, StopIteration):
            return None

    header = [h.strip().lower() for h in (split(lines[0]) or [])]
    if "kapanis" not in header or "kar_zarar_usdt" not in header:
        log.warning("İşlem geçmişi başlığı tanınmadı (%s), dosya atlandı", path)
        return []
    # Eski başlıklı dosyaya yeni sürümün yazdığı uzun satırlar: fazlalar isteğe bağlı sütunlara eşlenir
    columns = header + [c for c in OPTIONAL_COLUMNS if c not in header]
    needed = max(i for i, c in enumerate(header) if c in BASE_COLUMNS) + 1

    trades: List[dict] = []
    skipped = 0
    for line in lines[1:]:
        values = split(line)
        if values is None or [v.strip().lower() for v in values[:2]] == header[:2]:
            skipped += values is None  # Tekrarlanan başlık satırı sessizce atlanır
            continue
        trade = _parse_row(values, columns, needed)
        if trade is None:
            skipped += 1
            continue
        trades.append(trade)
    if skipped:
        log.warning("İşlem geçmişinde %d bozuk satır atlandı (%s)", skipped, path)
    return _sorted_by_close(trades)


def trades_between(trades: Iterable[dict], start: datetime, end: datetime) -> List[dict]:
    """Kapanış zamanı [start, end) aralığında olan işlemler."""
    start, end = _naive_local(start), _naive_local(end)
    return [t for t in _sorted_by_close(trades)
            if isinstance(t.get("kapanis"), datetime) and start <= _naive_local(t["kapanis"]) < end]


def day_range(now: datetime) -> Tuple[datetime, datetime]:
    """`now` gününün [00:00, ertesi gün 00:00) aralığı (günlük rapor için)."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def week_range(now: datetime) -> Tuple[datetime, datetime]:
    """`now`un içinde olduğu haftanın [Pazartesi 00:00, sonraki Pazartesi 00:00) aralığı."""
    start = day_range(now)[0] - timedelta(days=now.weekday())
    return start, start + timedelta(days=7)


# ---------------------------------------------------------------- istatistik
def _streaks(trades: Sequence[dict]) -> Tuple[int, int]:
    best_win = best_loss = cur_win = cur_loss = 0
    for t in trades:
        if _is_win(t):
            cur_win, cur_loss = cur_win + 1, 0
        else:
            cur_win, cur_loss = 0, cur_loss + 1
        best_win, best_loss = max(best_win, cur_win), max(best_loss, cur_loss)
    return best_win, best_loss


def _max_drawdown(pnls: Iterable[float]) -> float:
    """Kümülatif K/Z eğrisinin zirveden en büyük düşüşü (USDT, pozitif). Başlangıç 0 kabul edilir."""
    peak = cum = worst = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def compute_stats(trades: Iterable[dict]) -> dict:
    """İşlem listesinden performans istatistikleri.

    Kazanç: net K/Z ≥ 0 (botun sayaçlarıyla aynı). `avg_loss`, `gross_loss` ve
    `max_drawdown` pozitif büyüklüktür. Oranlar yüzde (0-100) cinsindendir.
    `profit_factor`: hiç zarar yoksa inf, hiç işlem/kâr yoksa None.
    """
    ordered = _sorted_by_close(trades)
    pnls = [_pnl(t) for t in ordered]
    count = len(ordered)
    win_pnls = [p for t, p in zip(ordered, pnls) if _is_win(t)]
    loss_pnls = [-p for t, p in zip(ordered, pnls) if not _is_win(t)]
    wins, losses = len(win_pnls), len(loss_pnls)
    total = sum(pnls)
    avg_win = sum(win_pnls) / wins if wins else 0.0
    avg_loss = sum(loss_pnls) / losses if losses else 0.0
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss > 0:
        profit_factor: Optional[float] = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else None
    breakeven = (avg_loss / (avg_win + avg_loss) * 100
                 if wins and losses and avg_win + avg_loss > 0 else None)
    holds = [h for h in (_hold_min(t) for t in ordered) if h is not None]
    pcts = [p for p in (_pnl_pct(t) for t in ordered) if p is not None]
    max_win_streak, max_loss_streak = _streaks(ordered)

    by_reason: Dict[str, dict] = {}
    by_symbol: Dict[str, dict] = {}
    for t, p in zip(ordered, pnls):
        reason = str(t.get("neden") or "?")
        r = by_reason.setdefault(reason, {"count": 0, "pnl": 0.0})
        r["count"] += 1
        r["pnl"] += p
        s = by_symbol.setdefault(str(t.get("parite") or "?"), {"count": 0, "pnl": 0.0, "wins": 0})
        s["count"] += 1
        s["pnl"] += p
        s["wins"] += int(_is_win(t))

    return {
        "count": count,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / count * 100 if count else 0.0,
        "total_pnl": total,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": total / count if count else 0.0,
        "best": max(ordered, key=_pnl) if ordered else None,
        "worst": min(ordered, key=_pnl) if ordered else None,
        "profit_factor": profit_factor,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "total_fees": sum(_to_float(t.get("komisyon_usdt")) or 0.0 for t in ordered),
        "avg_hold_min": sum(holds) / len(holds) if holds else 0.0,
        "avg_pnl_pct": sum(pcts) / len(pcts) if pcts else 0.0,
        "max_drawdown": _max_drawdown(pnls),
        "breakeven_win_rate": breakeven,
        "by_reason": by_reason,
        "by_symbol": by_symbol,
        "first_close": ordered[0].get("kapanis") if ordered else None,
        "last_close": ordered[-1].get("kapanis") if ordered else None,
    }


def kill_switch_probability(pnls: Iterable[float], realized_pnl: float, loss_limit: float,
                            horizon: int = 50, sims: int = 5000, seed: int = 42) -> Optional[float]:
    """Bootstrap Monte Carlo ile kill-switch olasılığı (0-1 arası).

    Geçmiş işlem K/Z'lerinden (yerine koyarak) rastgele seçilen `horizon` işlemlik
    `sims` gelecek senaryosunda, kümülatif K/Z'nin (gerçekleşen dahil) -loss_limit'a
    değdiği senaryoların oranı. 10'dan az işlem varsa None. `seed` ile tekrarlanabilir.
    """
    sample = [p for p in (_to_float(x) for x in pnls) if p is not None]
    if len(sample) < MIN_MONTE_CARLO_TRADES or horizon <= 0 or sims <= 0:
        return None
    limit = -abs(loss_limit) + 1e-9  # risk.kill_switch_hit ile aynı eşik
    start = float(realized_pnl)
    if start <= limit:
        return 1.0
    rng = random.Random(seed)
    room = limit - start  # Gelecek işlemlerin kümülatif K/Z'si buna inerse kill-switch
    hits = sum(1 for _ in range(sims) if min(accumulate(rng.choices(sample, k=horizon))) <= room)
    return hits / sims


def trade_pnls(trades: Iterable[dict]) -> List[float]:
    """kill_switch_probability için işlem K/Z listesi (kapanış sırasına göre)."""
    return [_pnl(t) for t in _sorted_by_close(trades)]


# ---------------------------------------------------------------- mesajlar
def _trade_brief(trade: dict, quote_asset: str) -> str:
    return (f"<b>{esc(trade.get('parite') or '?')}</b> {_money(_pnl(trade), quote_asset)}"
            f"{_pct_text(_pnl_pct(trade))}")


def _profit_factor_text(pf: Optional[float]) -> str:
    if pf is None:
        return "—"
    return "∞" if math.isinf(pf) else f"{pf:.2f}"


def _kill_prob_line(kill_prob: Optional[float], horizon: int) -> str:
    if kill_prob is None:
        return f"🎲 Kill-switch olasılığı: hesaplamak için en az {MIN_MONTE_CARLO_TRADES} işlem gerekli"
    pct = kill_prob * 100
    icon = "🟢" if pct < 5 else ("🟡" if pct < 20 else "🔴")
    return f"🎲 Sonraki {horizon} işlemde kill-switch olasılığı: {icon} <b>%{pct:.1f}</b> (Monte Carlo)"


def format_stats(stats: dict, quote_asset: str, realized_pnl: float, loss_limit: float,
                 tp_pct: float, sl_pct: float, kill_prob: Optional[float],
                 kill_horizon: int = 50) -> str:
    """/istatistik komutu için Telegram (HTML) mesajı. `kill_prob` 0-1 arası (veya None)."""
    q = esc(quote_asset)
    count = int(stats.get("count") or 0)
    remaining = max(0.0, abs(loss_limit) + realized_pnl)
    kill_lines = [
        f"☠️ Kill-switch: gerçekleşen K/Z {_money(realized_pnl, quote_asset)} | limit "
        f"-{fmt_usdt(abs(loss_limit))} {q} | kalan {fmt_usdt(remaining)} {q}",
        _kill_prob_line(kill_prob, kill_horizon),
    ]
    lines = ["📊 <b>Performans İstatistikleri</b>", LINE]
    if count == 0:
        lines += ["📭 Henüz kapanmış işlem yok; istatistik için işlem bekleniyor.", LINE] + kill_lines
        return _clip(lines)
    if count < MIN_RELIABLE_TRADES:
        lines.append(f"⚠️ Yalnızca <b>{count}</b> işlem var — sonuçlar yanıltıcı olabilir "
                     f"(en az {MIN_RELIABLE_TRADES} işlem önerilir).")

    win_rate = float(stats.get("win_rate") or 0.0)
    best, worst = stats.get("best"), stats.get("worst")
    lines += [
        f"🔁 İşlem: <b>{count}</b> (✅{stats.get('wins', 0)} / ❌{stats.get('losses', 0)})",
        f"🎯 Kazanma oranı: <b>%{win_rate:.1f}</b>",
        f"💰 Toplam K/Z: <b>{_money(stats.get('total_pnl', 0.0), quote_asset)}</b>",
        f"📈 Ort. kazanç: +{fmt_usdt(stats.get('avg_win', 0.0))} | "
        f"📉 Ort. kayıp: -{fmt_usdt(stats.get('avg_loss', 0.0))} {q}",
        f"🧮 İşlem başı beklenti: {_money(stats.get('expectancy', 0.0), quote_asset)}",
        f"⚖️ Kâr faktörü: {_profit_factor_text(stats.get('profit_factor'))}",
    ]
    if best is not None:
        lines.append(f"🏆 En iyi işlem: {_trade_brief(best, quote_asset)}")
    if worst is not None and worst is not best:
        lines.append(f"💀 En kötü işlem: {_trade_brief(worst, quote_asset)}")
    lines += [
        f"🔥 En uzun seri: ✅ {stats.get('max_win_streak', 0)} | ❌ {stats.get('max_loss_streak', 0)} art arda",
        f"💸 Toplam komisyon: {fmt_usdt(stats.get('total_fees', 0.0))} {q}",
        f"⏳ Ort. tutma süresi: {fmt_duration(float(stats.get('avg_hold_min') or 0.0) * 60)}",
        f"📉 Maks. düşüş (drawdown): -{fmt_usdt(stats.get('max_drawdown', 0.0))} {q}",
        LINE,
    ]

    # Başabaş analizi: +%TP / -%SL kuralında kâra geçmek için gereken kazanma oranı
    lines.append("🧭 <b>Başabaş analizi</b>")
    if tp_pct > 0 and sl_pct > 0:
        theory = sl_pct / (tp_pct + sl_pct) * 100
        verdict = ("✅ üzerinde" if win_rate > theory else
                   "⚖️ eşit" if math.isclose(win_rate, theory, abs_tol=0.05) else "⚠️ altında")
        lines.append(f"• +%{tp_pct:g} / -%{sl_pct:g} kuralında teorik başabaş kazanma oranı: <b>%{theory:.1f}</b>")
        lines.append(f"• Gerçekleşen kazanma oranı: <b>%{win_rate:.1f}</b> → başabaşın {verdict}")
    breakeven = stats.get("breakeven_win_rate")
    if breakeven is not None:
        status = "✅" if win_rate > breakeven else "⚠️"
        lines.append(f"• Gerçek ort. kazanç/kayba göre başabaş: %{breakeven:.1f} {status}")
    else:
        lines.append("• Gerçek başabaş oranı için hem kazanç hem kayıp işlemi gerekli")
    lines.append(LINE)

    # Coin bazında en iyi / en kötü 3
    by_symbol = stats.get("by_symbol") or {}
    ranked = sorted(by_symbol.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
    top = ranked[:3]
    top_syms = {s for s, _ in top}
    bottom = [kv for kv in reversed(ranked) if kv[0] not in top_syms][:3]

    def coin_line(sym: str, d: dict) -> str:
        return (f"• <b>{esc(sym)}</b>: {_money(d['pnl'], quote_asset)} "
                f"({d['count']} işlem, ✅{d.get('wins', 0)})")

    if top:
        lines.append("🏅 <b>En iyi coinler</b>")
        lines += [coin_line(s, d) for s, d in top]
    if bottom:
        lines.append("🥀 <b>En kötü coinler</b>")
        lines += [coin_line(s, d) for s, d in bottom]

    by_reason = stats.get("by_reason") or {}
    if by_reason:
        lines += [LINE, "🚪 <b>Çıkış nedenleri</b>"]
        for reason, d in sorted(by_reason.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
            icon = REASON_EMOJIS.get(reason, "🔹")
            lines.append(f"{icon} {esc(reason_label(reason))}: {d['count']} işlem, "
                         f"{_money(d['pnl'], quote_asset)}")
    lines += [LINE] + kill_lines
    return _clip(lines)


def stats_message(trades: Iterable[dict], cfg, realized_pnl: float, horizon: int = 50,
                  base_capital: Optional[float] = None, capital_added: float = 0.0) -> str:
    """Kısayol: istatistik + Monte Carlo + mesaj.

    Kill-switch limiti `base_capital` (botun sermayesi; verilmezse START_CAPITAL_USDT) × MAX_TOTAL_LOSS_PCT.
    """
    trades = list(trades)
    limit = cfg.loss_limit(cfg.start_capital if base_capital is None else base_capital, capital_added)
    prob = kill_switch_probability(trade_pnls(trades), realized_pnl, limit, horizon=horizon)
    return format_stats(compute_stats(trades), cfg.quote_asset, realized_pnl, limit,
                        cfg.take_profit_pct, cfg.stop_loss_pct, prob, kill_horizon=horizon)


def format_period_report(trades: Iterable[dict], start: datetime, end: datetime, title: str,
                         quote_asset: str, extra_lines: Sequence[str] = ()) -> str:
    """Günlük/haftalık rapor: kapanışı [start, end) aralığındaki işlemler.

    `title` düz metindir (kaçışlanır); `extra_lines` HTML olarak olduğu gibi sona eklenir.
    """
    q = esc(quote_asset)
    period = trades_between(trades, start, end)
    lines = [f"<b>{esc(title)}</b>",
             f"🗓️ {start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M}", LINE]
    if not period:
        lines.append("💤 Bu dönemde kapanan işlem yok.")
    else:
        st = compute_stats(period)
        pnl = st["total_pnl"]
        icon = "✅" if pnl >= 0 else "❌"
        lines += [
            f"🔁 İşlem: <b>{st['count']}</b> (✅{st['wins']} / ❌{st['losses']}) | "
            f"🎯 %{st['win_rate']:.1f}",
            f"{icon} Net K/Z: <b>{_money(pnl, quote_asset)}</b>",
            f"💸 Komisyon: {fmt_usdt(st['total_fees'])} {q}",
        ]
        if st["count"] == 1:
            lines.append(f"📌 İşlem: {_trade_brief(st['best'], quote_asset)}")
        else:
            lines.append(f"🏆 En iyi: {_trade_brief(st['best'], quote_asset)}")
            lines.append(f"💀 En kötü: {_trade_brief(st['worst'], quote_asset)}")
    extra = [str(x) for x in extra_lines if x is not None and str(x) != ""]
    if extra:
        lines += [LINE] + extra
    return _clip(lines)


def format_recent_trades(trades: Iterable[dict], n: int = 10, quote_asset: str = "USDT") -> str:
    """/islemler komutu: son `n` işlem, en yeni üstte, her işlem tek satır."""
    ordered = _sorted_by_close(trades)
    if not ordered:
        return "🧾 Henüz kapanmış işlem yok."
    n = max(1, int(n))
    recent = list(reversed(ordered[-n:]))
    lines = [f"🧾 <b>Son {len(recent)} İşlem</b> (en yeni üstte)", LINE]
    shown: List[dict] = []
    budget = MAX_MESSAGE_LEN - 200  # Alt bilgi satırlarına yer bırak
    for t in recent:
        reason = str(t.get("neden") or "")
        closed = t.get("kapanis")
        when = f" · {closed:%d.%m %H:%M}" if isinstance(closed, datetime) else ""
        hold = _hold_min(t)
        hold_text = f" · ⏳ {fmt_duration(hold * 60)}" if hold is not None else ""
        entry, exit_ = _to_float(t.get("alis_fiyati")), _to_float(t.get("satis_fiyati"))
        price = f" · {fmt_price(entry)}→{fmt_price(exit_)}" if entry and exit_ else ""
        line = (f"{'✅' if _is_win(t) else '❌'} {_trade_brief(t, quote_asset)} · "
                f"{REASON_EMOJIS.get(reason, '🔹')} {esc(reason_label(reason))}{when}{hold_text}{price}")
        if len("\n".join(lines)) + len(line) + 1 > budget:
            lines.append(f"…(+{len(recent) - len(shown)} işlem daha, mesaj sınırı)")
            break
        lines.append(line)
        shown.append(t)
    lines += [LINE, f"💰 Gösterilen {len(shown)} işlemin toplamı: "
                    f"<b>{_money(sum(_pnl(t) for t in shown), quote_asset)}</b>"]
    return "\n".join(lines)
