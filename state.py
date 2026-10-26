"""
Kalıcı durum (state) ve işlem geçmişi.

Bot kapanıp açıldığında açık pozisyonu, toplam kâr/zararı ve kill-switch
durumunu `data/state_<mod>.json` dosyasından hatırlar. Böylece yeniden
başlatmak kill-switch sayacını sıfırlamaz. Her kapanan işlem
`data/trades_<mod>.csv` dosyasına yazılır (Excel ile açılabilir).
"""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from logger import get_logger
from risk import Position

log = get_logger()


@dataclass
class BotState:
    mode: str
    start_capital: float             # Bota verilen toplam sermaye (ilk sermaye + /sermayeekle ile eklenenler)
    realized_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_fees: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)  # parite → açık pozisyon
    paper_balances: Dict[str, float] = field(default_factory=dict)
    cooldowns: Dict[str, float] = field(default_factory=dict)
    killed: bool = False
    kill_reason: str = ""
    killed_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    # Sonucu belirsiz kalan emirler (internet kesintisi): clientOrderId → {symbol, side, created_at, ...}
    pending_orders: Dict[str, dict] = field(default_factory=dict)
    paused: bool = False             # Telegram /durdur: yeni alım yok, açık pozisyon takibi sürer
    last_alive: float = 0.0          # Botun en son çalıştığı an (kesinti tespiti için)
    last_daily_report: str = ""      # En son günlük raporun tarihi (YYYY-MM-DD)
    last_weekly_report: str = ""     # En son haftalık raporun haftası (YYYY-Www)
    capital_added: float = 0.0       # /sermayeekle ile sonradan eklenen toplam tutar (bilgi amaçlı)
    # Momentum rotasyonu (STRATEGY=rotation)
    last_rebalance_day: int = -1     # En son yeniden dengelemenin UTC gün numarası (-1 = henüz yapılmadı)
    last_trend_check_day: int = -1   # BTC trend filtresinin en son kontrol edildiği UTC günü
    rotation_sells: Dict[str, str] = field(default_factory=dict)  # Satılması gereken ama henüz satılamamış: parite → neden
    rotation_refill: bool = False    # Dengeleme günü boşalan / dolmayan yer var: aynı gün yalnızca alımla doldurulacak
    rotation_plan_day: int = -1      # Son planlı dengelemenin günü (yarıda kalan / doldurma denemeleri aynı günle sınırlı)
    rotation_plan_slots: int = 0     # O günün hedef coin sayısı (yeniden denemelerde aynı sayı kullanılır)
    # Risk sigortaları (yeni alımları geçici olarak dondurur)
    day_key: str = ""                # day_pnl'in ait olduğu gün (yerel saat, YYYY-MM-DD)
    day_pnl: float = 0.0             # Bugün kapanan işlemlerin net K/Z'si
    sl_streak: int = 0               # Üst üste zarar kesle kapanan işlem sayısı
    coin_sl_streak: Dict[str, int] = field(default_factory=dict)  # Coin karnesi: parite → üst üste zarar kes
    freeze_until: float = 0.0        # Bu ana kadar yeni alım yok (0 = dondurma yok)
    freeze_reason: str = ""
    slippage_hist: List[float] = field(default_factory=list)      # Son alımların kayması (%)
    public_ips: Dict[str, str] = field(default_factory=dict)      # Sunucunun bilinen dış IP'leri (IPv4 / IPv6)
    # Telegram'dan strateji değiştirilirken eski grubun çıkış ayarları (geri dönünce aynen yüklenir)
    family_settings: Dict[str, Dict[str, str]] = field(default_factory=dict)

    @property
    def capital(self) -> float:
        """Botun yönettiği güncel sermaye (başlangıç + gerçekleşen K/Z)."""
        return self.start_capital + self.realized_pnl

    @property
    def committed(self) -> float:
        """Açık pozisyonlara bağlanmış toplam maliyet."""
        return sum(p.cost_usdt for p in self.positions.values())

    @classmethod
    def new(cls, mode: str, start_capital: float, quote_asset: str = "USDT") -> "BotState":
        state = cls(mode=mode, start_capital=round(start_capital, 8))
        if mode == "paper":
            state.paper_balances = {quote_asset: round(start_capital, 8)}
        return state

    def to_dict(self) -> dict:
        data = asdict(self)
        data["positions"] = [p.to_dict() for p in self.positions.values()]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "BotState":
        data = dict(data)
        raw = data.pop("positions", None) or []
        legacy = data.pop("position", None)  # Eski sürüm: tek pozisyon
        if legacy:
            raw = list(raw) + [legacy]
        known = set(cls.__dataclass_fields__)
        state = cls(**{k: v for k, v in data.items() if k in known})
        state.positions = {p.symbol: p for p in (Position.from_dict(d) for d in raw)}
        return state


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Optional[BotState]:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as fh:
            return BotState.from_dict(json.load(fh))

    def save(self, state: BotState) -> None:
        """Atomik yazma: önce geçici dosyaya yazılır, sonra yer değiştirilir (elektrik kesintisine dayanıklı)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def reset(self) -> bool:
        if self.path.exists():
            backup = self.path.with_name(f"{self.path.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak.json")
            os.replace(self.path, backup)
            return True
        return False


class TradeLog:
    BASE_HEADER = ["acilis", "kapanis", "parite", "miktar", "alis_fiyati", "satis_fiyati", "maliyet_usdt",
                   "net_gelir_usdt", "kar_zarar_usdt", "kar_zarar_yuzde", "komisyon_usdt", "neden", "sure_dk"]
    EXTRA_HEADER = ["sinyal_puani", "dusus_pct", "rsi", "hacim_orani", "spread_pct", "btc_1s_pct",
                    "piyasa_rejimi", "max_kar_pct", "max_zarar_pct"]
    HEADER = BASE_HEADER + EXTRA_HEADER

    def __init__(self, path: Path):
        self.path = Path(path)
        self._pending: List[list] = []   # Yazılamayan satırlar (ör. dosya Excel'de açıkken)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _migrate_header(self) -> None:
        """Eski sürümün daha az sütunlu dosyasını yeni başlığa taşır (eski satırlar korunur)."""
        with self.path.open("r", newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        if not rows or rows[0] == self.HEADER:
            return
        has_header = bool(rows[0]) and rows[0][0] == self.HEADER[0]
        width = len(self.HEADER)
        body = rows[1:] if has_header else rows   # Başlıksız dosyada ilk satır da işlemdir
        body = [r + [""] * (width - len(r)) if len(r) < width else r for r in body if r]
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(self.HEADER)
            writer.writerows(body)
        os.replace(tmp, self.path)

    def _write_rows(self, rows: List[list]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        if not new_file:
            self._migrate_header()
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(self.HEADER)
            writer.writerows(rows)

    def flush_pending(self) -> bool:
        """Bekleyen satırları yazmayı dener. Dosya kilitliyse (Excel) False döner, satırlar saklanır."""
        if not self._pending:
            return True
        try:
            self._write_rows(self._pending)
        except OSError as exc:
            log.warning("İşlem kaydı yazılamadı (%s); dosya kapanınca tekrar denenecek. Bekleyen: %d",
                        exc, len(self._pending))
            return False
        self._pending.clear()
        return True

    def append(self, pos: Position, exit_price: float, proceeds: float, fee_total: float,
               reason: str, closed_at: float) -> bool:
        pnl = proceeds - pos.cost_usdt
        m = pos.metrics or {}

        def num(value, digits=2):
            return "" if value is None else f"{value:.{digits}f}"

        self._pending.append([
            datetime.fromtimestamp(pos.opened_at).strftime("%Y-%m-%d %H:%M:%S"),
            datetime.fromtimestamp(closed_at).strftime("%Y-%m-%d %H:%M:%S"),
            pos.symbol, f"{pos.qty:.8f}", f"{pos.entry_price:.8f}", f"{exit_price:.8f}",
            f"{pos.cost_usdt:.4f}", f"{proceeds:.4f}", f"{pnl:.4f}",
            f"{(pnl / pos.cost_usdt * 100) if pos.cost_usdt else 0:.2f}", f"{fee_total:.4f}",
            reason, f"{(closed_at - pos.opened_at) / 60:.1f}",
            num(pos.score), num(m.get("dip_pct")), num(m.get("rsi"), 1), num(m.get("vol_ratio")),
            num(pos.spread_pct, 3), num(pos.btc_1h_pct), pos.regime,
            num(pos.peak_pnl_pct), num(pos.min_pnl_pct),
        ])
        return self.flush_pending()


class EquityLog:
    """Bakiyenin zaman içindeki seyri (data/equity_<mod>.csv)."""
    HEADER = ["zaman", "gerceklesen_bakiye", "acik_kz", "toplam_bakiye", "acik_pozisyon"]

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, at: float, capital: float, unrealized: float, open_positions: int) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if new_file:
                    writer.writerow(self.HEADER)
                writer.writerow([datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S"), f"{capital:.4f}",
                                 f"{unrealized:.4f}", f"{capital + unrealized:.4f}", open_positions])
        except OSError as exc:  # Dosya Excel'de açıksa bu kayıt atlanır, bot çalışmaya devam eder
            log.warning("Bakiye kaydı yazılamadı: %s", exc)
