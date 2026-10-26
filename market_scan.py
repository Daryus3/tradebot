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

import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from binance_client import RateLimitError
from indicators import rsi
from logger import get_logger
from position_rules import bullish_divergence
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


# ================================================================ pozitif RSI uyumsuzluğu bildirimi
DIV_COOLDOWN_SEC = 12 * 3600      # Aynı coin için en fazla 12 saatte bir bildirim
DIV_MAX_LINES = 8


def scan_divergences(client, rules: Dict[str, object], cfg, top: int = SCAN_TOP) -> List[Tuple[str, dict, float]]:
    """En hacimli coinlerde 1 saatlik pozitif RSI uyumsuzluğu: [(parite, ayrıntı, fiyat)]."""
    hits = []
    for t in universe(client.tickers_24h(), rules, cfg, top):
        try:
            rows = client.klines(t["symbol"], "1h", 80)
        except RateLimitError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("%s uyumsuzluk taraması: %s", t.get("symbol"), exc)
            continue
        found = bullish_divergence(rows[:-1], cfg.rsi_period) if len(rows) > 1 else None
        if found:
            hits.append((t["symbol"], found, float(rows[-1][4])))
    return hits


def format_divergences(hits: List[Tuple[str, dict, float]]) -> str:
    lines = ["📐 <b>Pozitif RSI uyumsuzluğu</b> (1 saatlik mumlar)",
             "Fiyat daha düşük dip yaptı ama RSI daha yüksek dip yaptı: satış gücü azalıyor olabilir."]
    for symbol, d, _price in hits[:DIV_MAX_LINES]:
        lines.append(f"• <b>{esc(symbol)}</b>: dip %{d['price_drop_pct']:+.1f}, RSI {d['rsi_prev']:.0f} → "
                     f"{d['rsi_now']:.0f} ({d['bars_apart']} saat arayla)")
    lines.append("<i>Bilgi amaçlıdır, alım sinyali değildir; dönüş teyidi (yeşil mum, hacim) beklenmeli.</i>")
    return "\n".join(lines)


class DivergenceWatcher:
    """RSI_DIV_ALERTS: RSI_DIV_SCAN_MINUTES aralıkla arka planda tarar, yeni uyumsuzlukları bildirir."""

    def __init__(self, client_factory: Callable, scan_fn: Callable = scan_divergences, time_fn=None):
        import time
        self.client_factory = client_factory
        self.scan_fn = scan_fn
        self.time_fn = time_fn or time.time
        self.next_at = 0.0
        self.notified: Dict[str, float] = {}
        self._thread: Optional[threading.Thread] = None

    def maybe_start(self, cfg, rules: Dict[str, object], notifier, now: float) -> bool:
        if not cfg.rsi_div_alerts or now < self.next_at or (self._thread and self._thread.is_alive()):
            return False
        self.next_at = now + cfg.rsi_div_scan_minutes * 60
        client = self.client_factory(cfg)
        self._thread = threading.Thread(target=self._run, args=(client, dict(rules), cfg, notifier),
                                        name="rsi-divergence", daemon=True)
        self._thread.start()
        return True

    def _run(self, client, rules, cfg, notifier) -> None:
        try:
            hits = self.scan_fn(client, rules, cfg)
        except Exception as exc:  # noqa: BLE001
            log.debug("RSI uyumsuzluk taraması başarısız: %s", exc)
            return
        now = self.time_fn()
        fresh = [h for h in hits if now - self.notified.get(h[0], 0.0) >= DIV_COOLDOWN_SEC]
        for symbol, _d, _p in fresh:
            self.notified[symbol] = now
        if fresh:
            notifier.notify(format_divergences(fresh), "info")
