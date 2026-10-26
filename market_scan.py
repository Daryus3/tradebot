"""
"Piyasayı Tara" (/piyasa veya /menu → Piyasayı Tara): en hacimli coinlerde "dipte olup hacmi artmaya başlayanlar".

Bilgi amaçlıdır; alım yapmaz ve botun kendi taramasını etkilemez. 1 saatlik mumlarla, her coin için:
  - Dip konumu: fiyatın son 24 saatin en düşüğü (0) ile en yükseği (100) arasındaki yeri.
  - Hacim artışı: son 3 saatin ortalama hacmi / önceki 21 saatin ortalama hacmi.
  - RSI(14).
Dip konumu ≤ %25 VE hacim artışı ≥ 1.5 olanlar listelenir. Ayrı bir istemciyle (yalnızca herkese açık veri) ve
ayrı bir iş parçacığında çalışır: tarama sürerken bot pozisyonları izlemeye devam eder.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from binance_client import RateLimitError
from indicators import rsi
from logger import get_logger
from strategy import LEVERAGED_SUFFIXES
from telegram_bot import LINE, esc

log = get_logger()

SCAN_TOP = 40              # Hacme göre ilk kaç coin taranır
BOTTOM_PCT = 25.0          # 24 saatlik aralığın alt %25'i "dip" sayılır
VOLUME_RISE = 1.5          # Son 3 saatin hacmi önceki saatlerin en az 1.5 katı
RECENT_BARS = 3


@dataclass
class CoinView:
    symbol: str
    price: float
    position_pct: float     # 24 saatlik aralıktaki yer (0 = en düşük, 100 = en yüksek)
    volume_ratio: float
    rsi: Optional[float]
    change_24h: float


def analyze_coin(symbol: str, rows: List[list], change_24h: float) -> Optional[CoinView]:
    if len(rows) < 25:
        return None
    closed = rows[:-1]
    price = float(rows[-1][4])
    day = closed[-24:]
    high = max(max(float(r[2]) for r in day), price)
    low = min(min(float(r[3]) for r in day), price)
    if price <= 0 or high <= low:
        return None
    vols = [float(r[7]) for r in day]
    before = vols[:-RECENT_BARS]
    base = sum(before) / len(before) if before else 0.0
    recent = sum(vols[-RECENT_BARS:]) / RECENT_BARS
    closes = [float(r[4]) for r in closed] + [price]
    r = rsi(closes, 14)[-1]
    return CoinView(symbol, price, (price - low) / (high - low) * 100, recent / base if base > 0 else 0.0, r,
                    change_24h)


def universe(tickers: List[dict], rules: Dict[str, object], cfg, top: int = SCAN_TOP) -> List[dict]:
    out = []
    for t in tickers:
        r = rules.get(t.get("symbol", ""))
        if r is None or r.base_asset in cfg.excluded_assets or r.base_asset.endswith(LEVERAGED_SUFFIXES):
            continue
        try:
            if float(t["lastPrice"]) <= 0:
                continue
            out.append(t)
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda t: -float(t.get("quoteVolume") or 0))
    return out[:top]


def scan_market(client, rules: Dict[str, object], cfg, top: int = SCAN_TOP) -> str:
    """Taramayı yapar ve Telegram mesajını döndürür."""
    coins: List[CoinView] = []
    errors = 0
    for t in universe(client.tickers_24h(), rules, cfg, top):
        try:
            view = analyze_coin(t["symbol"], client.klines(t["symbol"], "1h", 30),
                                float(t.get("priceChangePercent") or 0))
        except RateLimitError:
            raise
        except Exception as exc:  # noqa: BLE001 - tek coin hatası taramayı durdurmasın
            log.debug("%s piyasa taraması: %s", t.get("symbol"), exc)
            errors += 1
            continue
        if view is not None:
            coins.append(view)
    if not coins:
        return "🔎 Piyasa taraması yapılamadı (veri alınamadı)."
    hits = sorted((c for c in coins if c.position_pct <= BOTTOM_PCT and c.volume_ratio >= VOLUME_RISE),
                  key=lambda c: -c.volume_ratio)
    lines = [f"🔎 <b>Piyasa Taraması</b> — hacme göre ilk {len(coins)} coin (1 saatlik mumlar)", LINE]
    if hits:
        lines.append(f"📍 <b>Dipte ve hacmi artan</b> (24s aralığın alt %{BOTTOM_PCT:g}'i, hacim ≥ "
                     f"x{VOLUME_RISE:g}):")
        lines += [_line(c) for c in hits[:10]]
    else:
        lines.append("Şu an dipte olup hacmi belirgin artan coin yok. Dibe en yakın 5 coin:")
        lines += [_line(c) for c in sorted(coins, key=lambda c: c.position_pct)[:5]]
    lines.append(LINE)
    lines.append("<i>Dip konumu: 0 = 24 saatin en düşüğü, 100 = en yükseği. Bilgi amaçlıdır, alım sinyali "
                 "değildir; bot yalnızca kendi stratejisinin sinyaliyle alım yapar.</i>")
    if errors:
        lines.append(f"<i>{errors} coinin verisi alınamadı.</i>")
    return "\n".join(lines)


def _line(c: CoinView) -> str:
    rsi_text = f" | RSI {c.rsi:.0f}" if c.rsi is not None else ""
    return (f"• <b>{esc(c.symbol)}</b> dip konumu %{c.position_pct:.0f} | hacim x{c.volume_ratio:.1f} | "
            f"24s %{c.change_24h:+.1f}{rsi_text}")
