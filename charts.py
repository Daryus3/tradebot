"""
Backtest grafikleri (isteğe bağlı: matplotlib kuruluysa).

`save_pnl_chart`: her işlemin net K/Z'sini (%) kapanış zamanına göre nokta grafiği olarak, yanında aynı ölçekte
dağılım çubuklarıyla çizer. Kazançlar mavi, kayıplar kırmızı; Kâr Al / Zarar Kes seviyeleri ince çizgiyle işaretlenir.
matplotlib yoksa hiçbir şey yapılmaz (bot ve backtest grafiksiz çalışır): pip install matplotlib
"""
from __future__ import annotations

import math
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from logger import get_logger

log = get_logger()

# Renkler (açık zemin): kazanç / kayıp zıt kutuplar, yazılar metin tonlarında
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
WIN = "#2a78d6"
LOSS = "#e34948"


def matplotlib_available() -> bool:
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        return False
    return True


def save_pnl_chart(trades: Sequence, path, title: str, tp_pct: Optional[float] = None,
                   sl_pct: Optional[float] = None) -> Optional[Path]:
    """İşlem K/Z dağılımı grafiğini PNG olarak kaydeder; matplotlib yoksa veya işlem yoksa None."""
    if not trades or not matplotlib_available():
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    times = [datetime.fromtimestamp(t.close_time_ms / 1000, tz=timezone.utc) for t in trades]
    pnls = [t.pnl_pct for t in trades]
    wins = [(x, y) for x, y in zip(times, pnls) if y >= 0]
    losses = [(x, y) for x, y in zip(times, pnls) if y < 0]
    win_rate = len(wins) / len(pnls) * 100
    avg = sum(pnls) / len(pnls)

    fig, (ax, hist) = plt.subplots(1, 2, figsize=(11, 5.2), dpi=150, sharey=True,
                                   gridspec_kw={"width_ratios": [4, 1], "wspace": 0.04})
    fig.patch.set_facecolor(SURFACE)
    for a in (ax, hist):
        a.set_facecolor(SURFACE)
        a.grid(axis="y", color=GRID, linewidth=0.8)
        a.set_axisbelow(True)
        for side in ("top", "right"):
            a.spines[side].set_visible(False)
        a.spines["left"].set_color(BASELINE)
        a.spines["bottom"].set_color(BASELINE)
        a.tick_params(colors=MUTED, labelsize=8, length=0)
        a.axhline(0, color=BASELINE, linewidth=1, zorder=1)

    ring = {"edgecolors": SURFACE, "linewidths": 1.0, "s": 40, "zorder": 3}
    if wins:
        ax.scatter([x for x, _ in wins], [y for _, y in wins], color=WIN, label=f"Kazanç ({len(wins)})", **ring)
    if losses:
        ax.scatter([x for x, _ in losses], [y for _, y in losses], color=LOSS, label=f"Kayıp ({len(losses)})", **ring)
    for level, name in ((tp_pct, "Kâr Al"), (-sl_pct if sl_pct else None, "Zarar Kes")):
        if level is None:
            continue
        ax.axhline(level, color=MUTED, linewidth=0.8, zorder=2)
        ax.annotate(f"{name} {level:+g}%", xy=(1, level), xycoords=("axes fraction", "data"),
                    xytext=(-4, 3), textcoords="offset points", ha="right", va="bottom", fontsize=8, color=INK_2)
    ax.set_ylabel("İşlem başına net K/Z (%)", color=INK_2, fontsize=9)
    locator = mdates.AutoDateLocator(maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, fontsize=8, labelcolor=INK_2,
              handletextpad=0.3, borderaxespad=0.2)

    # Dağılım: aynı K/Z ölçeğinde yatay çubuklar (kaç işlem hangi aralıkta bitti)
    # 0 bir aralık sınırına denk gelir: bir çubuk kazanç ve kaybı karıştırmaz
    lo, hi = min(pnls), max(pnls)
    width = (hi - lo) / 24 if hi > lo else 1.0
    start = math.floor(lo / width) * width
    bins = max(1, math.ceil((hi - start) / width + 1e-9))
    counts = [0] * bins
    for y in pnls:
        counts[min(bins - 1, int((y - start) / width))] += 1
    centers = [start + (i + 0.5) * width for i in range(bins)]
    hist.barh(centers, counts, height=width, color=[WIN if c >= 0 else LOSS for c in centers],
              edgecolor=SURFACE, linewidth=1.0, zorder=3)
    hist.set_xlabel("İşlem sayısı", color=INK_2, fontsize=9)
    hist.tick_params(axis="y", left=False)

    fig.suptitle(title, x=0.06, ha="left", color=INK, fontsize=12, fontweight="bold")
    fig.text(0.06, 0.905, f"{len(pnls)} işlem | kazanma oranı %{win_rate:.0f} | işlem başı ortalama {avg:+.2f}% "
                          f"(komisyon dahil) | saatler UTC", ha="left", color=INK_2, fontsize=9)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.85, bottom=0.1)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with warnings.catch_warnings():   # Çok kısa dönemde tarih ekseni uyarısı (grafik yine çizilir)
            warnings.simplefilter("ignore", UserWarning)
            fig.savefig(path, facecolor=SURFACE)
    finally:
        plt.close(fig)
    return path
