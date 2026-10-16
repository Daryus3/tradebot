"""
Haftalık momentum rotasyonu: canlı bot ve backtest'in ORTAK karar fonksiyonları (ağ çağrısı yok).

Fikir: Kriptoda en çok araştırılmış ve en tutarlı görünen etki "momentum"dur — son haftalarda en çok yükselen
coinler sonraki haftalarda da ortalamada daha iyi gitme eğilimindedir. Bot:
  1. Her ROTATION_DAYS günde bir (varsayılan 7) en hacimli ROTATION_UNIVERSE coini son ROTATION_LOOKBACK_DAYS
     günlük getirisine göre sıralar.
  2. Getirisi pozitif olan en güçlü ROTATION_TOP_N coini eşit tutarla tutar. Elindeki coin hâlâ ilk 2×N
     içindeyse satmaz (gereksiz al-sat ve komisyon azalır); sıralamadan düşeni satıp yerine yenisini alır.
  3. BTC günlük kapanışı ROTATION_BTC_MA_DAYS günlük ortalamasının altına inerse (piyasa düşüş trendinde)
     her şeyi satıp USDT'de bekler; bir sonraki yeniden dengelemede trend düzeldiyse tekrar alır.
Kâr al / izleyen stop kullanılmaz; zarar kes yalnızca felaket korumasıdır.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

KEEP_RANK_FACTOR = 2   # Tutulan coin ilk ROTATION_TOP_N × 2 içindeyse satılmaz


@dataclass
class RotationPlan:
    risk_on: Optional[bool]                  # BTC trendi olumlu mu? (None = değerlendirilemedi, alım yapılır)
    slots: int                               # Hedef coin sayısı
    ranking: List[Tuple[str, float]] = field(default_factory=list)  # (parite, momentum %), pozitifler, büyükten küçüğe
    keep: List[str] = field(default_factory=list)       # Tutulmaya devam edilecekler
    sell: List[str] = field(default_factory=list)       # Satılacaklar
    candidates: List[str] = field(default_factory=list)  # Alınabilecekler (sıralı); ilk (slots - keep) tanesi alınır

    @property
    def wanted(self) -> int:
        return max(0, self.slots - len(self.keep))

    def rank_of(self, symbol: str) -> Optional[int]:
        for i, (sym, _) in enumerate(self.ranking):
            if sym == symbol:
                return i + 1
        return None


def momentum_pct(closes: Sequence[float], lookback: int) -> Optional[float]:
    """Son kapanmış günlük kapanışın `lookback` gün önceki kapanışa göre değişimi (%). Veri yetersizse None."""
    if lookback <= 0 or len(closes) < lookback + 1:
        return None
    start, end = closes[-1 - lookback], closes[-1]
    if start <= 0 or end <= 0:
        return None
    return (end / start - 1) * 100


def btc_trend(closes: Sequence[float], ma_days: int) -> Tuple[Optional[bool], Optional[float]]:
    """(BTC son kapanışı son `ma_days` kapanışın ortalamasının üstünde mi, ortalama). Filtre kapalıysa (True, None),
    veri yetersizse (None, None)."""
    if ma_days <= 0:
        return True, None
    if len(closes) < ma_days or closes[-1] <= 0:
        return None, None
    avg = sum(closes[-ma_days:]) / ma_days
    return closes[-1] > avg, avg


def plan_rotation(held: Sequence[str], momentum: Dict[str, Optional[float]], slots: int,
                  risk_on: Optional[bool]) -> RotationPlan:
    """Elde tutulanlar + sıralama → ne tutulacak, ne satılacak, ne alınacak.

    - Momentumu pozitif olmayan veya sıralamada hiç olmayan (hacmi düşen, güvenlik filtresine takılan) coin satılır.
    - Tutulan coin ilk slots×2 içindeyse tutulur (en fazla `slots` tane, en iyi sıradakiler).
    - BTC trendi olumsuzsa (risk_on False) her şey satılır, alım yapılmaz.
    """
    ranking = sorted(((s, m) for s, m in momentum.items() if m is not None and m > 0), key=lambda x: (-x[1], x[0]))
    plan = RotationPlan(risk_on=risk_on, slots=max(0, slots), ranking=ranking)
    if risk_on is False or plan.slots == 0:
        plan.sell = list(held)
        return plan
    rank = {s: i for i, (s, _) in enumerate(ranking)}
    keep = sorted((s for s in held if s in rank and rank[s] < plan.slots * KEEP_RANK_FACTOR), key=lambda s: rank[s])
    plan.keep = keep[: plan.slots]
    plan.sell = [s for s in held if s not in plan.keep]
    plan.candidates = [s for s, _ in ranking if s not in held]
    return plan


def pump_pct(highs: Sequence[float], lows: Sequence[float], days: int) -> Optional[float]:
    """Canlı coin filtresinin pompa ölçüsü (coin_filters._pump_pct ile aynı): son `days` günün en yükseği /
    önceki `days` günün en düşüğü − 1 (%). Diziler canlıdaki gibi günlük mumlardır (en sondaki bugünün mumu)."""
    if days <= 0 or len(highs) < 2 * days or len(lows) < 2 * days:
        return None
    top = max(highs[-days:])
    bottom = min(lows[-2 * days:-days])
    if bottom <= 0:
        return None
    return (top / bottom - 1) * 100
