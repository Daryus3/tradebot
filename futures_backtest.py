"""
Vadeli scalping backtesti (STRATEGY=futures): Binance USDⓈ-M geçmiş verisiyle 4'lü onay stratejisi.

    python main.py --backtest --strateji futures --gun 365          # Son 1 yıl, FUTURES_COINS
    python main.py --backtest --strateji futures --tum-donem        # Ocak 2020'den bugüne, dönem başı ilk 10 coin

Sinyaller canlı botla AYNI fonksiyonlarla bulunur (strategy.futures_features / futures_candidates /
futures_signal_at). Simülasyon:
  - Giriş: sinyal mumunun kapanışında (bir sonraki mumun açılışı) piyasa emri + kayma (FUTURES_SLIPPAGE_PCT).
  - Pozisyon: serbest bakiyenin FUTURES_MARGIN_USAGE_PCT'si teminat × kaldıraç (bileşik); aynı anda tek pozisyon
    (tüm bakiye kullanıldığı için). Notional Binance'in minimum emir tutarının altındaysa sinyal atlanır.
  - Çıkış (fiyat yüzdesi, girişten): Kâr Al (limit → maker komisyon), Zarar Kes (stop-market + kayma; mum stopun
    ötesinde açılırsa açılıştan), başabaş (fiyat +BREAKEVEN_TRIGGER_PCT'ye gelince stop girişin
    +BREAKEVEN_FLOOR_PCT üstüne), zaman stopu (MAX_HOLD_MINUTES sonra piyasa emriyle). Çıkışlar 1 dk mumlarla
    izlenir (yoksa 5 dk); mum içindeki sıra için standart varsayım: yükselen mumda önce dip sonra tepe, düşen
    mumda önce tepe sonra dip.
  - Komisyon (BNB_FEES=true → %10 indirim), gerçek fonlama oranları (pozisyon 00/08/16 UTC'yi geçerse),
    yalıtılmış (isolated) teminatta likidasyon: mum likidasyon fiyatının ötesinde açılırsa teminat kaybedilir.
Rapor: yıllara göre kazanma oranı, getiri, maks. düşüş, en uzun kayıp serisi; likidasyon / marjin riski; kaldıraç ve
TP / SL duyarlılığı. Grafik: sermaye eğrisi, düşüş, yıllık getiri ve kazanma oranı.
"""
from __future__ import annotations

import csv
import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from config import DATA_DIR, futures_coin_list, futures_liq_distance_pct
from futures_data import FuturesArchive, concat, resample, slice_range
from strategy import (FUT_BAR_MS, FUT_HOUR_MS, btc_trend_1h, futures_candidates, futures_features, futures_signal_at,
                      trend_at)

DAY_MS = 86_400_000
MIN_MS = 60_000
WARMUP_MS = 4 * DAY_MS                  # 5 dk göstergeler + BTC 1 saatlik EMA50 (≥ 50 saat) için ısınma
# Binance USDⓈ-M minimum emir tutarı (USDT, 2024): BTC 100, ETH 20, diğerleri 5
MIN_NOTIONAL = {"BTC": 100.0, "ETH": 20.0}
DEFAULT_MIN_NOTIONAL = 5.0
MAX_NOTIONAL = 1_000_000.0              # Bileşik büyüme sınırı (kaldıraç kademeleri / likidite); üstü gerçekçi değil
FUNDING_DEFAULT = 0.0001                # Fonlama verisi yoksa 8 saatte %0.01 (pozitif: long öder)
FUNDING_HOURS = (0, 8, 16)
BNB_FUTURES_DISCOUNT = 0.10             # Vadeli işlemde BNB ile komisyon indirimi
LEVERAGES = (1, 3, 5, 10, 15, 20)
TP_GRID = (1.0, 1.2, 1.5)
SL_GRID = (0.8, 0.9, 1.0)
CRASH_TP_GRID = (3.0, 4.0, 6.0, 8.0)
CRASH_SL_GRID = (3.0, 4.0, 5.0)
MAX_WINDOW_MIN = 240                    # Çıkış penceresi (zaman stopu duyarlılığı için en uzun süre)
LONG_WINDOW_MIN = 720                   # Bundan uzun tutma sürelerinde çıkışlar 5 dk mumlarla izlenir (bellek)
# Dönem başında piyasa değeri en büyük 10 coin (stabil coinler hariç; o tarihte USDⓈ-M vadeli işlemi olanlar).
# Dönem dönem değişen liste, bugünün büyük coinlerini geçmişe taşımanın yaratacağı "hayatta kalan" yanılgısını önler.
HISTORY_SEGMENTS = (
    ("2020-01-01", "BTC,ETH,XRP,BCH,LTC,EOS,BNB,XTZ,XLM,ADA"),
    ("2020-11-01", "BTC,ETH,XRP,LINK,BCH,LTC,DOT,BNB,ADA,XLM"),
    ("2023-11-01", "BTC,ETH,BNB,XRP,SOL,ADA,DOGE,TRX,LINK,DOT"),
)
REASON_TR = {"TP": "Kâr Al", "SL": "Zarar Kes", "BE": "Başabaş stopu", "TIME": "Zaman stopu", "LIQ": "LİKİDASYON",
             "END": "Veri bitti"}


def _ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _year(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def min_notional(symbol: str) -> float:
    return MIN_NOTIONAL.get(symbol[:-4] if symbol.endswith("USDT") else symbol, DEFAULT_MIN_NOTIONAL)


# ===================================================================== veri
@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    coins: Tuple[str, ...]


@dataclass
class FutData:
    start_ms: int
    end_ms: int
    segments: List[Segment]
    k5: Dict[str, Dict[str, np.ndarray]]
    btc_trend: tuple
    btc_close: Tuple[np.ndarray, np.ndarray]
    funding: Dict[str, Tuple[np.ndarray, np.ndarray]]
    archive: FuturesArchive
    exit_interval: str = "1m"
    notes: List[str] = field(default_factory=list)


def history_segments(end_ms: int, start_ms: Optional[int] = None) -> List[Segment]:
    """HISTORY_SEGMENTS → [Segment] (start_ms'den önce başlayan dönemler kırpılır)."""
    out = []
    for i, (start, coins) in enumerate(HISTORY_SEGMENTS):
        a = _ms(start)
        b = _ms(HISTORY_SEGMENTS[i + 1][0]) if i + 1 < len(HISTORY_SEGMENTS) else end_ms
        a, b = max(a, start_ms or a), min(b, end_ms)
        if a < b:
            out.append(Segment(a, b, tuple(c.strip().upper() for c in coins.split(","))))
    return out


def prepare(cfg, start_ms: int, end_ms: int, segments: Optional[List[Segment]] = None, cache_dir=None,
            exit_interval: str = "1m", archive: Optional[FuturesArchive] = None,
            progress: Optional[Callable[[str], None]] = None) -> FutData:
    """Dönemin 5 dk mumlarını (ısınmalı), BTC 1 saatlik trendini ve fonlama oranlarını indirir / önbellekten okur."""
    archive = archive or FuturesArchive(cache_dir or DATA_DIR / "futures_cache", cfg.futures_data_url)
    segments = segments or [Segment(start_ms, end_ms, futures_coin_list(cfg))]
    coins = sorted({c for s in segments for c in s.coins})
    say = progress or (lambda _m: None)
    k5: Dict[str, Dict[str, np.ndarray]] = {}
    funding: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    notes: List[str] = []
    for coin in coins:
        symbol = coin + "USDT"
        active = [s for s in segments if coin in s.coins]
        a, b = min(s.start_ms for s in active) - WARMUP_MS, max(s.end_ms for s in active)
        say(f"📥 {symbol}: 5 dk mumlar {_date(a + WARMUP_MS)} → {_date(b)}")
        k = archive.klines(symbol, a, b, "5m")
        if len(k["t"]) < 500:
            notes.append(f"{symbol}: bu dönemde vadeli işlem verisi yok, atlandı")
            continue
        k5[symbol] = k
        funding[symbol] = archive.funding(symbol, a, b)
    btc = k5.get("BTCUSDT")
    if btc is None or btc["t"][0] > start_ms:
        say("📥 BTCUSDT: trend için 5 dk mumlar")
        btc = archive.klines("BTCUSDT", start_ms - WARMUP_MS, end_ms, "5m")
    btc_1h = resample(btc, FUT_HOUR_MS)
    inside = slice_range(btc, start_ms, end_ms)
    return FutData(start_ms, end_ms, segments, k5, btc_trend_1h(btc_1h), (inside["t"], inside["c"]), funding, archive,
                   exit_interval, notes)


# ===================================================================== sinyaller
@dataclass
class FutSignal:
    close_ms: int           # Sinyal mumunun kapanışı = giriş anı
    symbol: str
    side: str
    score: float
    entry_open: float       # Bir sonraki mumun açılışı (kaymasız giriş fiyatı)
    metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class Funnel:
    """4'lü onay hunisi: aktif dönemdeki 5 dk mumlardan kaçı hangi şartı geçti."""
    candles: int = 0
    liq: int = 0
    liq_vol: int = 0
    liq_vol_trend: int = 0
    signals: int = 0


def _active_mask(t_close: np.ndarray, coin: str, segments: List[Segment]) -> np.ndarray:
    mask = np.zeros(len(t_close), dtype=bool)
    for s in segments:
        if coin in s.coins:
            mask |= (t_close > s.start_ms) & (t_close <= s.end_ms)
    return mask


def find_signals(cfg, data: FutData) -> Tuple[List[FutSignal], Funnel]:
    """Canlı botun her 5 dk kapanışında yaptığı değerlendirmenin tüm geçmişe uygulanışı."""
    signals: List[FutSignal] = []
    funnel = Funnel()
    for symbol, k in data.k5.items():
        coin = symbol[:-4]
        t = k["t"]
        close = t + FUT_BAR_MS
        f = futures_features(k, cfg)
        active = _active_mask(close, coin, data.segments)
        nxt_ok = np.zeros(len(t), dtype=bool)
        nxt_ok[:-1] = t[1:] == close[:-1]                 # Bir sonraki mum boşluksuz geliyor (giriş yapılabilir)
        active &= nxt_ok
        up = down = None
        crash = cfg.futures_tactic == "crash"
        if cfg.futures_btc_trend and not crash:
            up, down = trend_at(close, data.btc_trend)
        long_ok, short_ok = futures_candidates(f, cfg, up, down)
        both = (long_ok | short_ok) & active
        funnel.candles += int(active.sum())
        if crash:   # Huni: çöküş → (likidasyon dalgası) → sinyal
            drop = (f["crash"] <= -cfg.futures_crash_drop_pct) & active
            funnel.liq += int(drop.sum())
            funnel.liq_vol += int((drop & f["liq_long"]).sum())
            funnel.liq_vol_trend += int(both.sum())
        else:
            funnel.liq += int(((f["liq_long"] | f["liq_short"]) & active).sum())
            funnel.liq_vol += int(((f["liq_long"] | f["liq_short"]) & f["vol_ok"] & active).sum())
            funnel.liq_vol_trend += int(both.sum())
        for i in np.flatnonzero(both):
            found = None
            for side, ok in (("long", long_ok), ("short", short_ok)):   # Canlıdaki sıra: önce long
                if ok[i]:
                    found = futures_signal_at(f, i, side, cfg)
                    if found is not None:
                        break
            if found is None:
                continue
            found["btc_trend"] = (1.0 if up is not None and up[i] else -1.0 if down is not None and down[i] else 0.0)
            signals.append(FutSignal(int(close[i]), symbol, found["side"], found["score"], float(k["o"][i + 1]),
                                     {key: v for key, v in found.items() if key not in ("side", "price")}))
    funnel.signals = len(signals)
    signals.sort(key=lambda s: (s.close_ms, -s.score))
    return signals, funnel


# ===================================================================== çıkış pencereleri
def load_windows(data: FutData, signals: Sequence[FutSignal], minutes: int = MAX_WINDOW_MIN,
                 progress: Optional[Callable[[str], None]] = None,
                 cache: Optional[Dict[Tuple[str, int], tuple]] = None) -> Dict[Tuple[str, int], tuple]:
    """Her sinyal için girişten itibaren `minutes` dakikalık mumlar: (aralık ms, t, o, h, l, c). 1 dk mumlar ay ay
    okunur (bellek: bir coinin tüm 1 dk geçmişi GB'larca olurdu); 1 dk verisi yoksa 5 dk mumlar kullanılır."""
    out: Dict[Tuple[str, int], tuple] = {} if cache is None else cache
    span = minutes * MIN_MS
    by_symbol: Dict[str, List[FutSignal]] = {}
    for s in signals:
        if (s.symbol, s.close_ms) not in out:
            by_symbol.setdefault(s.symbol, []).append(s)
    for symbol, sigs in by_symbol.items():
        k5 = data.k5[symbol]
        if data.exit_interval == "1m" and minutes <= LONG_WINDOW_MIN:
            months: Dict[Tuple[int, int], List[FutSignal]] = {}
            for s in sigs:
                d = datetime.fromtimestamp(s.close_ms / 1000, tz=timezone.utc)
                months.setdefault((d.year, d.month), []).append(s)
            if progress:
                progress(f"📥 {symbol}: 1 dk mumlar ({len(months)} ay)")
            for (y, m), group in sorted(months.items()):
                parts = [data.archive.month(symbol, "1m", y, m)]
                last_end = max(s.close_ms for s in group) + span
                ny, nm = y + (m == 12), m % 12 + 1
                if last_end > datetime(ny, nm, 1, tzinfo=timezone.utc).timestamp() * 1000:
                    parts.append(data.archive.month(symbol, "1m", ny, nm))
                k1 = concat(parts)
                for s in group:
                    win = slice_range(k1, s.close_ms, s.close_ms + span)
                    if len(win["t"]) and win["t"][0] == s.close_ms:
                        out[(symbol, s.close_ms)] = (MIN_MS, win["t"], win["o"], win["h"], win["l"], win["c"])
        for s in sigs:
            if (symbol, s.close_ms) not in out:
                win = slice_range(k5, s.close_ms, s.close_ms + span)
                out[(symbol, s.close_ms)] = (FUT_BAR_MS, win["t"], win["o"], win["h"], win["l"], win["c"])
    return out


# ===================================================================== simülasyon
@dataclass(frozen=True)
class SimParams:
    leverage: int
    tp_pct: float
    sl_pct: float
    be_enabled: bool
    be_trigger_pct: float
    be_floor_pct: float
    hold_min: int
    margin_usage_pct: float
    taker_fee: float
    maker_fee: float
    slippage_pct: float
    tp_limit: bool
    mmr_pct: float
    start_capital: float
    kill_pct: float
    max_notional: float = MAX_NOTIONAL
    symbol_cooldown_min: int = 0        # Her işlemden sonra aynı coine bekleme (SYMBOL_COOLDOWN_MINUTES, canlıyla aynı)
    harsh_stops: bool = False           # Stres: stop emri, mumun stopun ötesindeki fitilinin yarısında dolar
    # Canlı botun risk sigortaları (trader._after_close ile aynı): art arda zarar kes molası, günlük zarar / kâr limiti
    loss_streak_limit: int = 0
    loss_streak_pause_h: float = 4.0
    daily_loss_pct: float = 0.0
    daily_profit_pct: float = 0.0
    loss_cooldown_min: int = 0          # Zararla kapanınca (daha uzunsa) bekleme (LOSS_COOLDOWN_MINUTES)


def params_from_cfg(cfg, **over) -> SimParams:
    discount = 1 - BNB_FUTURES_DISCOUNT if getattr(cfg, "bnb_fees", False) else 1.0
    p = SimParams(leverage=cfg.futures_leverage, tp_pct=cfg.take_profit_pct, sl_pct=cfg.stop_loss_pct,
                  be_enabled=cfg.breakeven_enabled, be_trigger_pct=cfg.breakeven_trigger_pct,
                  be_floor_pct=cfg.breakeven_floor_pct, hold_min=cfg.max_hold_minutes,
                  margin_usage_pct=cfg.futures_margin_usage_pct, taker_fee=cfg.futures_taker_fee * discount,
                  maker_fee=cfg.futures_maker_fee * discount, slippage_pct=cfg.futures_slippage_pct,
                  tp_limit=cfg.futures_tp_order == "limit", mmr_pct=cfg.futures_mmr_pct,
                  start_capital=cfg.start_capital, kill_pct=cfg.max_total_loss_pct,
                  symbol_cooldown_min=cfg.symbol_cooldown_minutes, loss_cooldown_min=cfg.loss_cooldown_minutes,
                  loss_streak_limit=cfg.loss_streak_limit, loss_streak_pause_h=cfg.loss_streak_pause_hours,
                  daily_loss_pct=cfg.daily_loss_limit_pct, daily_profit_pct=cfg.daily_profit_target_pct)
    return replace(p, **over) if over else p


@dataclass
class FutTrade:
    symbol: str
    side: str
    entry_ms: int
    exit_ms: int
    entry: float
    exit: float
    reason: str
    leverage: int
    margin: float
    notional: float
    fees: float
    funding: float
    pnl: float
    roe_pct: float
    equity_after: float
    mae_pct: float          # İşlem boyunca en kötü fiyat hareketi (% , ters yönde pozitif)
    window_mae_pct: float   # Zaman stopuna kadar (stop olmasaydı) en kötü hareket: marjin riski için
    gap: bool               # Stop / likidasyon fiyatının ötesinde açılan mumla kapandı
    liquidated: bool
    bar_ms: int
    score: float = 0.0


@dataclass
class FutResult:
    params: SimParams
    trades: List[FutTrade]
    curve: List[Tuple[int, float]]
    skipped: Counter
    start_ms: int
    end_ms: int
    ruined_ms: Optional[int] = None
    kill_ms: Optional[int] = None

    @property
    def final_equity(self) -> float:
        return self.curve[-1][1] if self.curve else self.params.start_capital

    @property
    def net_pct(self) -> float:
        return (self.final_equity / self.params.start_capital - 1) * 100


def _funding_cost(data: FutData, symbol: str, side: int, notional: float, entry_ms: int, exit_ms: int) -> float:
    t, r = data.funding.get(symbol, (np.zeros(0, dtype=np.int64), np.zeros(0)))
    if len(t):
        i, j = np.searchsorted(t, entry_ms, "right"), np.searchsorted(t, exit_ms, "right")
        total = float(r[i:j].sum())
        if j == len(t) and exit_ms > t[-1] + 8 * FUT_HOUR_MS:   # Verinin bittiği yer (bitmemiş ay): varsayılan oran
            total += FUNDING_DEFAULT * _funding_count(max(entry_ms, int(t[-1])), exit_ms)
    else:
        total = FUNDING_DEFAULT * _funding_count(entry_ms, exit_ms)
    return side * notional * total


def _funding_count(a_ms: int, b_ms: int) -> int:
    """(a, b] aralığındaki fonlama anı sayısı (00/08/16 UTC)."""
    step = 8 * FUT_HOUR_MS
    return max(0, b_ms // step - a_ms // step)


def run_trade(p: SimParams, s: FutSignal, window: tuple, margin: float, notional: float,
              data: Optional[FutData] = None) -> FutTrade:
    """Tek işlem: giriş, başabaş, Kâr Al / Zarar Kes / zaman stopu, likidasyon (yalıtılmış teminat)."""
    bar_ms, t, o, h, l, c = window
    side = 1 if s.side == "long" else -1
    slip = p.slippage_pct / 100
    entry = s.entry_open * (1 + side * slip)
    tp = entry * (1 + side * p.tp_pct / 100)
    sl = entry * (1 - side * p.sl_pct / 100)
    be_trig = entry * (1 + side * p.be_trigger_pct / 100)
    be_stop = entry * (1 + side * p.be_floor_pct / 100)
    liq = entry * (1 - side * (1 / p.leverage - p.mmr_pct / 100))
    deadline = s.close_ms + p.hold_min * MIN_MS
    stop, be_on = sl, False
    exit_price, reason, exit_ms, gap, liquidated = None, "END", None, False, False
    worst = 0.0

    def worse(price: float, level: float) -> bool:      # Fiyat seviyenin ters tarafında (veya üstünde)
        return price <= level if side > 0 else price >= level

    def reached(price: float, level: float) -> bool:    # Fiyat lehte seviyeye ulaştı
        return price >= level if side > 0 else price <= level

    adverse = l if side > 0 else h
    favorable = h if side > 0 else l
    n = len(t)
    for j in range(n):
        tj = int(t[j])
        if tj >= deadline:
            exit_price, reason, exit_ms = o[j] * (1 - side * slip), "TIME", tj
            break
        oj, aj, fj = o[j], adverse[j], favorable[j]
        if worse(oj, liq):                                  # Likidasyon fiyatının ötesinde açıldı: stop çalışamadı
            exit_price, reason, exit_ms, gap, liquidated = oj, "LIQ", tj, True, True
            worst = max(worst, side * (entry - oj) / entry * 100)
            break
        if worse(oj, stop):                                 # Stopun ötesinde açıldı (fiyat boşluğu)
            exit_price, reason, exit_ms, gap = oj * (1 - side * slip), "BE" if be_on else "SL", tj, True
            worst = max(worst, side * (entry - oj) / entry * 100)
            break
        worst = max(worst, side * (entry - aj) / entry * 100)
        # Mum içi sıra (standart OHLC varsayımı): yükselen mum açılış → dip → tepe → kapanış, düşen mum açılış →
        # tepe → dip → kapanış. Pozisyonun ters tarafı önce görüldüyse önce stop, sonra hedef / başabaş bakılır.
        cj = c[j]
        adverse_first = cj >= oj if side > 0 else cj <= oj
        if adverse_first:
            if worse(aj, stop):
                fill = (stop + aj) / 2 if p.harsh_stops else stop
                exit_price, reason, exit_ms = fill * (1 - side * slip), "BE" if be_on else "SL", tj
                break
            if reached(fj, tp):
                exit_price = tp if p.tp_limit else tp * (1 - side * slip)
                reason, exit_ms = "TP", tj
                break
            if p.be_enabled and not be_on and reached(fj, be_trig):
                be_on = True
                stop = be_stop if worse(stop, be_stop) else stop
                if worse(cj, stop):                         # Tepeden sonra kapanışa inerken başabaş stopu geçildi
                    exit_price, reason, exit_ms = stop * (1 - side * slip), "BE", tj
                    break
        else:
            if reached(fj, tp):
                exit_price = tp if p.tp_limit else tp * (1 - side * slip)
                reason, exit_ms = "TP", tj
                break
            if p.be_enabled and not be_on and reached(fj, be_trig):
                be_on = True
                stop = be_stop if worse(stop, be_stop) else stop
            if worse(aj, stop):
                fill = (stop + aj) / 2 if p.harsh_stops else stop
                exit_price, reason, exit_ms = fill * (1 - side * slip), "BE" if be_on else "SL", tj
                break
    if exit_price is None:                                  # Veri penceresi bitti (son mumun kapanışı)
        last = n - 1
        exit_price = (c[last] if n else entry) * (1 - side * slip)
        exit_ms = int(t[last]) + bar_ms if n else s.close_ms
        reason = "END" if not n or int(t[last]) + bar_ms < deadline else "TIME"
    upto = np.searchsorted(t, deadline, "left")
    window_mae = (float(np.max(side * (entry - adverse[:upto]) / entry * 100)) if upto else 0.0)
    qty = notional / entry
    fee_in = notional * p.taker_fee
    fee_out = qty * exit_price * (p.maker_fee if reason == "TP" and p.tp_limit else p.taker_fee)
    funding = _funding_cost(data, s.symbol, side, notional, s.close_ms, exit_ms) if data is not None else 0.0
    if liquidated:
        pnl = -margin - fee_in
        fees = fee_in
        funding = 0.0
    else:
        pnl = side * qty * (exit_price - entry) - fee_in - fee_out - funding
        fees = fee_in + fee_out
    return FutTrade(s.symbol, s.side, s.close_ms, exit_ms, entry, float(exit_price), reason, p.leverage, margin,
                    notional, fees, funding, pnl, pnl / margin * 100 if margin else 0.0, 0.0, worst,
                    max(window_mae, worst), gap, liquidated, bar_ms, s.score)


def simulate(p: SimParams, data: FutData, signals: Sequence[FutSignal], windows: Dict[Tuple[str, int], tuple],
             kill_switch: bool = False, start_ms: Optional[int] = None, end_ms: Optional[int] = None,
             fixed_notional: Optional[float] = None) -> FutResult:
    """Sinyalleri zaman sırasıyla işler: aynı anda tek pozisyon, tüm bakiye (bileşik). start_ms / end_ms verilirse
    yalnızca o aralıktaki sinyaller (yıl yıl yeniden başlama için). fixed_notional verilirse her işlem bu tutarla
    açılır (bakiye ne olursa olsun; erime / minimum emir yok): stratejinin işlem başı gücünü ölçmek için."""
    start_ms = data.start_ms if start_ms is None else start_ms
    end_ms = data.end_ms if end_ms is None else end_ms
    signals = [s for s in signals if start_ms <= s.close_ms < end_ms]
    equity = p.start_capital
    curve: List[Tuple[int, float]] = [(start_ms, equity)]
    trades: List[FutTrade] = []
    skipped: Counter = Counter()
    busy_until = -1
    last_entry = -1
    cooldown: Dict[str, int] = {}
    freeze_until = -1
    sl_streak = 0
    day_key, day_pnl = -1, 0.0
    ruined = kill_ms = None
    usage = p.margin_usage_pct / 100
    kill_level = p.start_capital * (1 - p.kill_pct / 100) if p.kill_pct else -1.0
    for s in signals:
        if s.close_ms < busy_until:
            skipped["açık pozisyon vardı"] += 1
            continue
        if s.close_ms == last_entry:
            skipped["aynı mumda daha güçlü sinyal alındı"] += 1
            continue
        if s.close_ms < cooldown.get(s.symbol, -1):
            skipped["coin beklemede (önceki işlemden sonra)"] += 1
            continue
        if s.close_ms < freeze_until:
            skipped["risk sigortası (zarar serisi / günlük limit)"] += 1
            continue
        if fixed_notional:
            notional = fixed_notional
            margin = notional / p.leverage
        else:
            margin = equity * usage
            notional = min(margin * p.leverage, p.max_notional)
            margin = notional / p.leverage
        if not fixed_notional and notional < min_notional(s.symbol):
            skipped["minimum emir tutarının altında"] += 1
            if equity * usage * p.leverage < DEFAULT_MIN_NOTIONAL:
                ruined = s.close_ms
                break
            continue
        window = windows.get((s.symbol, s.close_ms))
        if window is None or not len(window[1]):
            skipped["çıkış verisi yok"] += 1
            continue
        tr = run_trade(p, s, window, margin, notional, data)
        equity = equity + tr.pnl if fixed_notional else max(0.0, equity + tr.pnl)
        tr.equity_after = equity
        trades.append(tr)
        curve.append((tr.exit_ms, equity))
        # Mum içinde kapanan işlem o mum bitene kadar "açık" sayılır (canlıda çıkış dakikanın içinde olur); zaman
        # stopu mum açılışında kapanır, aynı 5 dk kapanışındaki yeni sinyal alınabilir (canlı: önce çıkış, sonra tarama)
        busy_until = tr.exit_ms if tr.reason == "TIME" else tr.exit_ms + tr.bar_ms
        last_entry = s.close_ms
        wait = (max(p.symbol_cooldown_min, p.loss_cooldown_min) if tr.pnl < 0 and p.loss_cooldown_min > 0
                else p.symbol_cooldown_min)                # canlıdaki risk.cooldown_minutes ile aynı
        if wait > 0:
            cooldown[s.symbol] = tr.exit_ms + tr.bar_ms + wait * MIN_MS
        # Canlıdaki risk sigortaları (trader._after_close): günlük zarar / kâr limiti ve art arda zarar kes molası
        day = tr.exit_ms // DAY_MS
        if day != day_key:
            day_key, day_pnl = day, 0.0
        day_pnl += tr.pnl
        midnight = (day + 1) * DAY_MS
        if p.daily_loss_pct > 0 and day_pnl <= -p.start_capital * p.daily_loss_pct / 100:
            freeze_until = max(freeze_until, midnight)
        if p.daily_profit_pct > 0 and day_pnl >= p.start_capital * p.daily_profit_pct / 100:
            freeze_until = max(freeze_until, midnight)
        sl_streak = sl_streak + 1 if tr.reason == "SL" else 0
        if p.loss_streak_limit > 0 and sl_streak >= p.loss_streak_limit:
            freeze_until = max(freeze_until, tr.exit_ms + int(p.loss_streak_pause_h * FUT_HOUR_MS))
            sl_streak = 0
        if fixed_notional:
            continue
        if kill_ms is None and equity <= kill_level:
            kill_ms = tr.exit_ms
            if kill_switch:
                break
        if equity * usage * p.leverage < DEFAULT_MIN_NOTIONAL:
            ruined = tr.exit_ms
            break
    return FutResult(p, trades, curve, skipped, start_ms, end_ms, ruined, kill_ms)


# ===================================================================== istatistik
def max_drawdown(curve: Sequence[Tuple[int, float]]) -> Tuple[float, Optional[int], Optional[int]]:
    """Gerçekleşen sermaye eğrisinde en büyük düşüş (%), zirve ve dip zamanı."""
    peak, peak_t, dd, at = -1.0, None, 0.0, (None, None)
    for t, e in curve:
        if e > peak:
            peak, peak_t = e, t
        elif peak > 0 and (peak - e) / peak * 100 > dd:
            dd, at = (peak - e) / peak * 100, (peak_t, t)
    return dd, at[0], at[1]


def streaks(trades: Sequence[FutTrade]) -> Tuple[int, int, float]:
    """(en uzun kayıp serisi, en uzun kazanç serisi, en uzun kayıp serisinde bakiye kaybı %)."""
    best_loss = best_win = cur_loss = cur_win = 0
    worst_drop, start_eq = 0.0, 0.0
    for tr in trades:
        if tr.pnl <= 0:
            if cur_loss == 0:
                start_eq = tr.equity_after - tr.pnl
            cur_loss += 1
            cur_win = 0
            drop = (start_eq - tr.equity_after) / start_eq * 100 if start_eq > 0 else 0.0
            if cur_loss > best_loss or (cur_loss == best_loss and drop > worst_drop):
                best_loss, worst_drop = cur_loss, drop
        else:
            cur_win += 1
            cur_loss = 0
            best_win = max(best_win, cur_win)
    return best_loss, best_win, worst_drop


def net_move_pct(tr: FutTrade) -> float:
    """İşlemin kaldıraçsız net sonucu: fiyat hareketi - komisyon - kayma - fonlama, pozisyon tutarına göre (%)."""
    return tr.pnl / tr.notional * 100 if tr.notional else 0.0


@dataclass
class Stats:
    label: str
    trades: int
    wins: int
    win_rate: float
    net_pct: float
    max_dd: float
    loss_streak: int
    avg_roe: float
    avg_move: float
    profit_factor: float
    liquidations: int
    gaps: int
    start_eq: float
    end_eq: float
    ruined: bool = False


def stats_of(res: FutResult, label: str = "") -> Stats:
    trades = res.trades
    wins = sum(1 for tr in trades if tr.pnl > 0)
    gain = sum(tr.pnl for tr in trades if tr.pnl > 0)
    loss = -sum(tr.pnl for tr in trades if tr.pnl <= 0)
    start = res.params.start_capital
    return Stats(label, len(trades), wins, wins / len(trades) * 100 if trades else 0.0, res.net_pct,
                 max_drawdown(res.curve)[0], streaks(trades)[0],
                 float(np.mean([tr.roe_pct for tr in trades])) if trades else 0.0,
                 float(np.mean([net_move_pct(tr) for tr in trades])) if trades else 0.0,
                 gain / loss if loss > 0 else float("inf") if gain > 0 else 0.0,
                 sum(tr.liquidated for tr in trades), sum(tr.gap for tr in trades), start, res.final_equity,
                 res.ruined_ms is not None)


def year_ranges(data: FutData) -> List[Tuple[int, int, int]]:
    out = []
    y = _year(data.start_ms)
    while True:
        a = max(_ms(f"{y}-01-01"), data.start_ms)
        b = min(_ms(f"{y + 1}-01-01"), data.end_ms)
        if a >= data.end_ms:
            break
        out.append((y, a, b))
        y += 1
    return out


@dataclass
class Evaluation:
    """Bir ayar setinin üç senaryosu:
    A) chained: tek hesap, başlangıç sermayesiyle baştan sona (bileşik, gerçekçi; eriyince durur)
    B) years: her yıl başlangıç sermayesiyle yeniden (yıllara göre getiri / düşüş)
    C) fixed: her sinyal sabit tutarla (başlangıç teminatı × kaldıraç), bakiye etkisi yok: stratejinin işlem başı
       gücü tüm yıllarda kesintisiz ölçülür (kazanma oranı, işlem başı sonuç, çıkış nedenleri)."""
    chained: FutResult
    years: List[Tuple[int, FutResult]]
    fixed: FutResult

    @property
    def trades(self) -> List[FutTrade]:
        return self.fixed.trades

    @property
    def profitable_years(self) -> int:
        return sum(1 for _y, r in self.years if r.trades and r.net_pct > 0)

    @property
    def avg_move(self) -> float:
        return float(np.mean([net_move_pct(tr) for tr in self.trades])) if self.trades else 0.0

    @property
    def win_rate(self) -> float:
        return sum(tr.pnl > 0 for tr in self.trades) / len(self.trades) * 100 if self.trades else 0.0

    def fixed_years(self) -> List[Tuple[int, List[FutTrade]]]:
        out: Dict[int, List[FutTrade]] = {}
        for tr in self.trades:
            out.setdefault(_year(tr.entry_ms), []).append(tr)
        return sorted(out.items())


def evaluate(p: SimParams, data: FutData, signals: Sequence[FutSignal], windows) -> Evaluation:
    chained = simulate(p, data, signals, windows)
    years = [(y, simulate(p, data, signals, windows, start_ms=a, end_ms=b)) for y, a, b in year_ranges(data)]
    fixed = simulate(p, data, signals, windows,
                     fixed_notional=p.start_capital * p.margin_usage_pct / 100 * p.leverage)
    return Evaluation(chained, years, fixed)


def btc_hold_pct(data: FutData, start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> Optional[float]:
    t, c = data.btc_close
    if start_ms is not None:
        i, j = np.searchsorted(t, start_ms), np.searchsorted(t, end_ms or data.end_ms) - 1
        if j <= i:
            return None
        return (float(c[j]) / float(c[i]) - 1) * 100
    return (float(c[-1]) / float(c[0]) - 1) * 100 if len(c) > 1 else None


# ===================================================================== duyarlılık
def leverage_table(cfg, data: FutData, signals, windows) -> List[Tuple[int, Optional[Evaluation], str]]:
    rows = []
    for lev in LEVERAGES:
        room = futures_liq_distance_pct(lev, cfg.futures_mmr_pct)
        if cfg.stop_loss_pct >= room - 0.5:
            rows.append((lev, None, f"zarar kes (%{cfg.stop_loss_pct:g}) likidasyona (%{room:.1f}) çok yakın"))
            continue
        rows.append((lev, evaluate(params_from_cfg(cfg, leverage=lev), data, signals, windows), ""))
    return rows


def exit_grid(cfg, data: FutData, signals, windows) -> List[Tuple[float, float, Evaluation]]:
    tps, sls = (CRASH_TP_GRID, CRASH_SL_GRID) if cfg.futures_tactic == "crash" else (TP_GRID, SL_GRID)
    return [(tp, sl, evaluate(params_from_cfg(cfg, tp_pct=tp, sl_pct=sl), data, signals, windows))
            for tp in tps for sl in sls]


def variant_rows(cfg, data: FutData, signals, windows) -> List[Tuple[str, Evaluation]]:
    """Tek bir kuralı değiştirmenin etkisi (diğer her şey aynı)."""
    def ev(sigs=signals, **over):
        return evaluate(params_from_cfg(cfg, **over), data, sigs, windows)
    if cfg.futures_tactic == "crash":
        def other(**changes):
            c2 = replace(cfg, **changes)
            sigs, _f = find_signals(c2, data)
            load_windows(data, sigs, max(MAX_WINDOW_MIN, cfg.max_hold_minutes + 10), cache=windows)
            return evaluate(params_from_cfg(c2), data, sigs, windows)
        return [("Başabaş kalkanı kapalı", ev(be_enabled=False)),
                ("Zaman stopu 60 dk", ev(hold_min=60)),
                ("Zaman stopu 120 dk", ev(hold_min=120)),
                ("Zarardan sonra bekleme yok", ev(loss_cooldown_min=0)),
                ("Kayma 5 kat (%0.10)", ev(slippage_pct=0.10)),
                ("Komisyon 2 kat", ev(taker_fee=params_from_cfg(cfg).taker_fee * 2,
                                      maker_fee=params_from_cfg(cfg).maker_fee * 2)),
                ("Kâr Al piyasa emriyle (taker)", ev(tp_limit=False)),
                ("Kötümser stop dolumu (fitilin yarısı)", ev(harsh_stops=True)),
                (f"Çöküş eşiği %{cfg.futures_crash_drop_pct - 2:g}", other(futures_crash_drop_pct=cfg.futures_crash_drop_pct - 2)),
                (f"Çöküş eşiği %{cfg.futures_crash_drop_pct + 2:g}", other(futures_crash_drop_pct=cfg.futures_crash_drop_pct + 2)),
                ("+likidasyon dalgası şartı" if not cfg.futures_crash_liq else "Likidasyon şartı kapalı",
                 other(futures_crash_liq=not cfg.futures_crash_liq))]
    out = [("Başabaş kalkanı kapalı", ev(be_enabled=False)),
           ("Zaman stopu 30 dk", ev(hold_min=30)),
           ("Zaman stopu 120 dk", ev(hold_min=120)),
           ("Kâr Al piyasa emriyle (taker)", ev(tp_limit=False)),
           ("Komisyonsuz + kaymasız (kıyas)", ev(taker_fee=0.0, maker_fee=0.0, slippage_pct=0.0)),
           ("Yalnızca long", ev([s for s in signals if s.side == "long"])),
           ("Yalnızca short", ev([s for s in signals if s.side == "short"]))]
    return out


# ===================================================================== rapor
def _pct(v: Optional[float], sign: bool = True) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if abs(v) >= 1e5:
        return f"{v:+.1e}%" if sign else f"{v:.1e}%"
    return f"{v:+.1f}%" if sign else f"{v:.1f}%"


def _pf(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def _num(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _eval_line(ev: Evaluation) -> str:
    ch = ev.chained
    ruined = f"eridi {_date(ch.ruined_ms)}" if ch.ruined_ms else _pct(ch.net_pct)
    return (f"{ruined:>16s} | {ev.profitable_years}/{len(ev.years)} | {len(ev.trades):5d} işlem, kazanma "
            f"%{ev.win_rate:4.1f}, işlem başı {ev.avg_move:+.3f}%")


def format_report(cfg, data: FutData, ev: Evaluation, funnel: Funnel, levs=None, grid=None, variants=None) -> str:
    res = ev.chained
    p = res.params
    st = stats_of(res)
    dd, dd_a, dd_b = max_drawdown(res.curve)
    loss_streak, win_streak, streak_drop = streaks(res.trades)
    room = futures_liq_distance_pct(p.leverage, p.mmr_pct)
    days = (data.end_ms - data.start_ms) / DAY_MS
    fixed = ev.trades
    size = p.start_capital * p.margin_usage_pct / 100 * p.leverage
    L = [f"📊 VADELİ SCALPING BACKTESTİ — {_date(data.start_ms)} → {_date(data.end_ms)} ({days:.0f} gün)", "━" * 30]
    L.append("Coinler: " + "; ".join(f"{_date(s.start_ms)}'den: {','.join(s.coins)}" for s in data.segments))
    L.append(f"Kaldıraç {p.leverage}x isolated | teminat: serbest bakiyenin %{p.margin_usage_pct:g}'i (bileşik) | "
             f"başlangıç {p.start_capital:g} USDT")
    L.append(f"Kâr Al +%{p.tp_pct:g} ({'limit, maker' if p.tp_limit else 'piyasa'}) | Zarar Kes -%{p.sl_pct:g} | "
             + (f"başabaş +%{p.be_trigger_pct:g} → stop +%{p.be_floor_pct:g}" if p.be_enabled else "başabaş kapalı")
             + f" | zaman stopu {p.hold_min} dk (fiyat %, girişten)")
    L.append(f"Komisyon: piyasa %{p.taker_fee * 100:.3f} / limit %{p.maker_fee * 100:.3f}, kayma %{p.slippage_pct:g}, "
             f"gerçek fonlama oranları; çıkışlar {'1 dk' if data.exit_interval == '1m' else '5 dk'} mumlarla")
    L.append(f"Likidasyon ({p.leverage}x): fiyat ~%{room:.1f} ters giderse; zarar kes bundan {room - p.sl_pct:.1f} "
             f"puan önce çalışır")
    crash = cfg.futures_tactic == "crash"
    if crash:
        L.append(f"Taktik: ÇÖKÜŞ DÖNÜŞÜ — son {cfg.futures_crash_hours} saatte en az %{cfg.futures_crash_drop_pct:g} düşen "
                 f"coinde long" + (" (+ likidasyon dalgası şartı)" if cfg.futures_crash_liq else "")
                 + f"; aynı coine işlemden sonra {p.symbol_cooldown_min} dk, zarardan sonra "
                 f"{max(p.symbol_cooldown_min, p.loss_cooldown_min)} dk girilmez")
    else:
        L.append("Taktik: 4'lü onay (likidasyon + hacim + RSI uyumsuzluğu + BTC 1 saatlik trendi)")
    L.append("")
    per_month = funnel.signals / max(days / 30.4, 1e-9)
    if crash:
        L.append("🔎 Sinyal hunisi (aktif dönemdeki 5 dk mumlar):")
        L.append(f"  {_num(funnel.candles, 0)} mum → {cfg.futures_crash_hours} saatte ≥%{cfg.futures_crash_drop_pct:g} "
                 f"düşüş {_num(funnel.liq, 0)} (likidasyon dalgalı: {_num(funnel.liq_vol, 0)}) → {_num(funnel.signals, 0)} "
                 f"sinyal mumu (tek pozisyon ve bekleme kurallarıyla işleme dönüşen {len(fixed)})")
    else:
        L.append("🔎 4'lü onay hunisi (aktif dönemdeki 5 dk mumlar):")
        L.append(f"  {_num(funnel.candles, 0)} mum → likidasyon dalgası {_num(funnel.liq, 0)} → + hacim "
                 f"≥{cfg.futures_volume_ratio:g}x {_num(funnel.liq_vol, 0)} → + BTC 1s trend yönünde "
                 f"{_num(funnel.liq_vol_trend, 0)} → + RSI uyumsuzluğu {_num(funnel.signals, 0)} sinyal "
                 f"(ayda ~{per_month:.0f}; tek pozisyon kuralıyla işleme dönüşen {len(fixed)})")
    L.append("")
    L.append(f"💰 A) {p.start_capital:g} USDT ile {_date(data.start_ms)}'de başlayıp para eklemeseydiniz (tek hesap)")
    L.append(f"  Son bakiye: {_num(res.final_equity)} USDT ({_pct(res.net_pct)}) | BTC al-tut {_pct(btc_hold_pct(data))}")
    L.append(f"  İşlem {st.trades} | kazanan {st.wins} | kazanma %{st.win_rate:.1f} | kâr faktörü {_pf(st.profit_factor)}"
             f" | işlem başı ROE {st.avg_roe:+.2f}%")
    L.append(f"  Maks. düşüş %{dd:.1f}" + (f" ({_date(dd_a)} → {_date(dd_b)})" if dd_a else "")
             + f" | en uzun kayıp serisi {loss_streak} işlem (bakiye -%{streak_drop:.1f}) | kazanç serisi {win_streak}")
    skipped = ", ".join(f"{k}: {v}" for k, v in res.skipped.most_common())
    if skipped:
        L.append(f"  Atlanan sinyal: {skipped}")
    if res.kill_ms:
        L.append(f"  ⚠️ Kill-switch (-%{p.kill_pct:g}) canlıda {_date(res.kill_ms)} tarihinde botu durdururdu")
    if res.ruined_ms:
        L.append(f"  🛑 HESAP ERİDİ: {_date(res.ruined_ms)} tarihinde bakiye en küçük emrin altına indi; sonrası bu "
                 f"senaryoda hiç işlem görmedi (B ve C'ye bakın)")
    L.append("")
    L.append(f"📅 B) Her yıl {p.start_capital:g} USDT ile yeniden başlasaydınız")
    L.append("  Yıl   İşlem  Kazanma  Getiri      Maks.düşüş  Kayıp serisi  Likid.  BTC al-tut")
    for y, r in ev.years:
        s2 = stats_of(r)
        mark = f"  🛑 eridi {_date(r.ruined_ms)[5:]}" if s2.ruined else ""
        L.append(f"  {y}  {s2.trades:5d}  %{s2.win_rate:5.1f}  {_pct(s2.net_pct):>10s}  %{s2.max_dd:5.1f}      "
                 f"{s2.loss_streak:3d}       {s2.liquidations:3d}   {_pct(btc_hold_pct(data, r.start_ms, r.end_ms)):>8s}"
                 f"{mark}")
    L.append(f"  Kârlı yıl: {ev.profitable_years}/{len(ev.years)}")
    L.append("")
    L.append(f"📈 C) Stratejinin kendisi: her sinyal sabit {size:g} USDT'lik pozisyonla (bakiye etkisi ve erime yok)")
    L.append("  Yıl   İşlem  Kazanma  İşlem başı net  Toplam (fiyat %)  Kâr faktörü  Kayıp serisi")
    for y, group in ev.fixed_years():
        w = sum(tr.pnl > 0 for tr in group)
        gain = sum(tr.pnl for tr in group if tr.pnl > 0)
        loss = -sum(tr.pnl for tr in group if tr.pnl <= 0)
        moves = [net_move_pct(tr) for tr in group]
        L.append(f"  {y}  {len(group):5d}  %{w / len(group) * 100:5.1f}  {np.mean(moves):+13.3f}%  {np.sum(moves):+14.1f}%"
                 f"   {_pf(gain / loss if loss > 0 else float('inf')):>9s}   {streaks(group)[0]:6d}")
    if fixed:
        moves = [net_move_pct(tr) for tr in fixed]
        gross = [m + tr.fees / tr.notional * 100 + tr.funding / tr.notional * 100 for m, tr in zip(moves, fixed)]
        L.append(f"  Toplam {len(fixed)} işlem | kazanma %{ev.win_rate:.1f} | işlem başı net {np.mean(moves):+.3f}% "
                 f"(komisyon ve fonlama hariç {np.mean(gross):+.3f}%) | "
                 f"{p.leverage}x'te teminatın {np.mean(moves) * p.leverage:+.2f}%'i")
        L.append("")
        L.append("📌 Çıkış nedenleri (C):")
        for reason in ("TP", "BE", "SL", "TIME", "LIQ", "END"):
            group = [tr for tr in fixed if tr.reason == reason]
            if group:
                L.append(f"  {REASON_TR[reason]:14s} {len(group):5d} (%{len(group) / len(fixed) * 100:4.1f}) | ort. ROE "
                         f"{np.mean([tr.roe_pct for tr in group]):+6.2f}% | ort. süre "
                         f"{np.mean([(tr.exit_ms - tr.entry_ms) / MIN_MS for tr in group]):3.0f} dk")
        for side, name in (("long", "LONG"), ("short", "SHORT")):
            group = [tr for tr in fixed if tr.side == side]
            if group:
                w = sum(tr.pnl > 0 for tr in group)
                L.append(f"  {name:5s}: {len(group)} işlem, kazanma %{w / len(group) * 100:.1f}, işlem başı "
                         f"{np.mean([net_move_pct(tr) for tr in group]):+.3f}%")
        L.append("")
        L.append("⚠️ MARJİN ÇAĞRISI / LİKİDASYON RİSKİ (C'deki tüm işlemler)")
        L.append(f"  Likidasyon: {sum(tr.liquidated for tr in fixed)} | stopun ötesinde açılan mumla kapanan: "
                 f"{sum(tr.gap for tr in fixed)}")
        worst = min(fixed, key=lambda tr: tr.roe_pct)
        L.append(f"  En kötü işlem: {worst.symbol} {worst.side.upper()} {_date(worst.entry_ms)}: ROE {worst.roe_pct:+.1f}%"
                 f" ({REASON_TR[worst.reason]}, fiyat %{worst.mae_pct:.2f} ters)")
        wicks = sorted((tr.mae_pct - p.sl_pct for tr in fixed if tr.reason in ("SL", "BE")), reverse=True)
        if wicks and wicks[0] > 0:
            big = sum(1 for w in wicks if w >= 2)
            L.append(f"  Zarar kes mumunda fiyatın stopun ötesine sarktığı en uzun fitil: %{wicks[0]:.1f} (≥%2 fitilli "
                     f"stop: {big}). Backtest stopu stop fiyatından doldurur; böyle anlarda gerçek dolum daha kötü "
                     f"olabilir (bkz. 'Kötümser stop dolumu' satırı).")
        wm = np.array([tr.window_mae_pct for tr in fixed])
        L.append(f"  Zarar kes OLMASAYDI: {p.hold_min} dk içinde fiyatın likidasyon mesafesini aştığı işlem:")
        L.append("    " + " | ".join(f"{lev}x (%{futures_liq_distance_pct(lev, p.mmr_pct):.1f}): "
                                   f"{int((wm >= futures_liq_distance_pct(lev, p.mmr_pct)).sum())}" for lev in LEVERAGES))
        streak = max(streaks(fixed)[0], 1)
        per_loss = (p.sl_pct + 2 * p.slippage_pct + 2 * p.taker_fee * 100) * p.leverage * p.margin_usage_pct / 100
        L.append(f"  En uzun kayıp serisi {streak} işlem: {p.leverage}x'te art arda {streak} zarar kes bakiyenin "
                 f"~%{(1 - (1 - per_loss / 100) ** streak) * 100:.0f}'ini götürür (işlem başı ~%{per_loss:.1f}).")
    head_row = "  Ayar                        A) tek hesap | B) kârlı yıl | C) sabit pozisyon"
    if levs:
        L.append("")
        L.append("🎚️ KALDIRAÇ DUYARLILIĞI (aynı sinyaller ve çıkışlar)")
        L.append(head_row)
        for lev, ev2, note in levs:
            L.append(f"  {f'{lev}x':26s} " + (_eval_line(ev2) if ev2 is not None else f"— ({note})"))
    if grid:
        L.append("")
        L.append(f"🎯 KÂR AL / ZARAR KES DUYARLILIĞI ({p.leverage}x)")
        L.append(head_row)
        for tp, sl, ev2 in grid:
            L.append(f"  {f'TP +%{tp:g} / SL -%{sl:g}':26s} " + _eval_line(ev2))
    if variants:
        L.append("")
        L.append("🧪 TEK KURAL DEĞİŞİNCE")
        L.append(head_row)
        for name, ev2 in variants:
            L.append(f"  {name:26s} " + _eval_line(ev2))
    L.append("")
    if crash:
        L.append("⚠️ Çöküşte alım riskleri: ani çöküşlerde (ör. 10 Ekim 2025) zarar kes emri stop fiyatının çok ötesinde "
                 "dolabilir ve borsa gecikebilir; listedeki bir coin tamamen çökerse (LUNA, FTT gibi) art arda kayıp "
                 "yaşanır (test listelerinde böyle bir coin yoktu). Geçmiş sonuç gelecek garantisi değildir.")
    else:
        L.append("ℹ️ Likidasyon dalgası mumlardan ölçülür (zorla satış/alış hacmi ≥ ortalamanın "
                 f"{cfg.futures_liq_ratio:g} katı + son 1 saatin dibi/zirvesi kırıldı + ≥1 ATR itiş); Binance geçmiş "
                 "USDⓈ-M likidasyon kaydı yayımlamıyor. Geçmiş sonuç gelecek garantisi değildir.")
    for note in data.notes:
        L.append(f"ℹ️ {note}")
    return "\n".join(L)


def save_trades_csv(trades: Sequence[FutTrade], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["giris_utc", "cikis_utc", "coin", "yon", "kaldirac", "giris", "cikis", "neden", "teminat_usdt",
                    "pozisyon_usdt", "komisyon_usdt", "fonlama_usdt", "kz_usdt", "roe_%", "net_fiyat_%",
                    "bakiye_sonra", "en_kotu_hareket_%", "likidasyon", "bosluk"])
        for tr in trades:
            w.writerow([datetime.fromtimestamp(tr.entry_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        datetime.fromtimestamp(tr.exit_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        tr.symbol, tr.side.upper(), tr.leverage, f"{tr.entry:.8g}", f"{tr.exit:.8g}",
                        REASON_TR[tr.reason], f"{tr.margin:.4f}", f"{tr.notional:.4f}", f"{tr.fees:.4f}",
                        f"{tr.funding:.4f}", f"{tr.pnl:.4f}", f"{tr.roe_pct:.2f}", f"{net_move_pct(tr):.3f}",
                        f"{tr.equity_after:.4f}", f"{tr.mae_pct:.3f}", "evet" if tr.liquidated else "",
                        "evet" if tr.gap else ""])
    return path


def save_chart(ev: Evaluation, path: Path, title: str) -> Optional[Path]:
    """4 panel: tek hesabın bakiyesi (log), yıllık getiri (her yıl yeniden), işlem başı net sonucun birikimi
    (stratejinin avantajı var mı?), yıllık kazanma oranı + işlem sayısı."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    if not ev.trades and not ev.chained.trades:
        return None
    res = ev.chained
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5))
    ax = axes[0][0]
    times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t, _e in res.curve]
    eq = [max(e, 1e-3) for _t, e in res.curve]
    ax.plot(times, eq, color="#2563eb", lw=1.2, drawstyle="steps-post")
    ax.axhline(res.params.start_capital, color="#9ca3af", lw=0.8, ls="--")
    if res.ruined_ms:
        ax.axvline(datetime.fromtimestamp(res.ruined_ms / 1000, tz=timezone.utc), color="#dc2626", lw=1, ls=":")
    ax.set_yscale("log")
    ax.set_title(f"A) Tek hesap: {res.params.start_capital:g} USDT'nin seyri (log)")
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=6))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m.%Y"))
    ax.grid(alpha=0.3)
    labels = [str(y) for y, _r in ev.years]
    nets = [r.net_pct for _y, r in ev.years]
    shown = [max(min(v, 500.0), -100.0) for v in nets]
    ax = axes[0][1]
    ax.bar(labels, shown, color=["#16a34a" if v >= 0 else "#dc2626" for v in nets])
    for i, v in enumerate(nets):
        ax.text(i, shown[i], f"{v:+.0f}%", ha="center", va="bottom" if shown[i] >= 0 else "top", fontsize=8)
    ax.axhline(0, color="#374151", lw=0.8)
    ax.set_title(f"B) Yıllık getiri: her yıl {res.params.start_capital:g} USDT ile yeniden (%)")
    ax.grid(alpha=0.3, axis="y")
    ax = axes[1][0]
    fixed = ev.trades
    if fixed:
        tt = [datetime.fromtimestamp(tr.exit_ms / 1000, tz=timezone.utc) for tr in fixed]
        ax.plot(tt, np.cumsum([net_move_pct(tr) for tr in fixed]), color="#7c3aed", lw=1.2, label="net")
        ax.plot(tt, np.cumsum([net_move_pct(tr) + (tr.fees + tr.funding) / tr.notional * 100 for tr in fixed]),
                color="#9ca3af", lw=1.0, ls="--", label="komisyon ve fonlama hariç")
        ax.legend(loc="best", fontsize=8)
    ax.axhline(0, color="#374151", lw=0.8)
    ax.set_title("C) Sabit pozisyon: işlem sonuçlarının toplamı (kaldıraçsız %)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.grid(alpha=0.3)
    ax = axes[1][1]
    fy = ev.fixed_years()
    ylabels = [str(y) for y, _g in fy]
    wins = [sum(tr.pnl > 0 for tr in g) / len(g) * 100 for _y, g in fy]
    ax.bar(ylabels, wins, color="#0891b2", alpha=0.8)
    for i, (_y, g) in enumerate(fy):
        ax.text(i, wins[i], f"%{wins[i]:.0f}\n{len(g)} işl.", ha="center", va="bottom", fontsize=8)
    ax.set_ylim(0, 100)
    ax.set_title("C) Kazanma oranı (%) ve işlem sayısı")
    ax.grid(alpha=0.3, axis="y")
    fig.suptitle(title, fontsize=12)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


# ===================================================================== komut satırı
def period(cfg, days: Optional[int], end_ms: Optional[int], full_history: bool) -> Tuple[int, int, List[Segment]]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    end = min(end_ms or now_ms, now_ms) // DAY_MS * DAY_MS     # Bitmemiş gün yok (arşivde günlük dosya)
    if full_history:
        segments = history_segments(end)
    else:
        start = max(end - int(days or 365) * DAY_MS, _ms(HISTORY_SEGMENTS[0][0]))
        segments = [Segment(start, end, futures_coin_list(cfg))]
    return segments[0].start_ms, end, segments


def run_cli(cfg, days: Optional[int], end_ms: Optional[int], out_dir: Path, chart: bool = True,
            full_history: bool = False, exit_interval: str = "1m", sensitivity: bool = True,
            progress: Callable[[str], None] = print) -> Tuple[str, Optional[Path], Evaluation]:
    """Backtesti çalıştırır; (rapor, grafik yolu, değerlendirme). İşlemler CSV olarak `out_dir`'e yazılır."""
    start, end, segments = period(cfg, days, end_ms, full_history)
    data = prepare(cfg, start, end, segments, exit_interval=exit_interval, progress=progress)
    progress("🔎 Sinyaller aranıyor ("
             + ("çöküş dönüşü" if cfg.futures_tactic == "crash" else "4'lü onay") + ")...")
    signals, funnel = find_signals(cfg, data)
    progress(f"   {len(signals)} sinyal; çıkışlar için mumlar okunuyor...")
    windows = load_windows(data, signals, max(MAX_WINDOW_MIN, cfg.max_hold_minutes + 10), progress)
    ev = evaluate(params_from_cfg(cfg), data, signals, windows)
    levs = grid = variants = None
    if sensitivity:
        progress("🎚️ Kaldıraç, Kâr Al / Zarar Kes ve kural duyarlılığı hesaplanıyor...")
        levs = leverage_table(cfg, data, signals, windows)
        grid = exit_grid(cfg, data, signals, windows)
        variants = variant_rows(cfg, data, signals, windows)
    report = format_report(cfg, data, ev, funnel, levs, grid, variants)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    try:
        csv_path = save_trades_csv(ev.trades, out_dir / f"vadeli_islemler_{stamp}.csv")
        report += f"\n📄 Tüm işlemler (C: sabit pozisyon): {csv_path}"
    except OSError as exc:
        report += f"\n⚠️ İşlem dosyası yazılamadı: {exc}"
    chart_path = None
    if chart:
        try:
            chart_path = save_chart(ev, out_dir / f"vadeli_grafik_{stamp}.png",
                                    f"Vadeli scalping {ev.chained.params.leverage}x — {_date(data.start_ms)} → "
                                    f"{_date(data.end_ms)} | {ev.profitable_years}/{len(ev.years)} yıl kârlı, "
                                    f"işlem başı {ev.avg_move:+.3f}%")
        except Exception as exc:  # noqa: BLE001 - grafik hatası raporu engellemesin
            report += f"\n⚠️ Grafik çizilemedi: {exc}"
    return report, chart_path, ev
