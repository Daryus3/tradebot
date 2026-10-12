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
from typing import Dict, Optional

from risk import Position


@dataclass
class BotState:
    mode: str
    start_capital: float
    realized_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_fees: float = 0.0
    position: Optional[Position] = None
    paper_balances: Dict[str, float] = field(default_factory=dict)
    cooldowns: Dict[str, float] = field(default_factory=dict)
    killed: bool = False
    kill_reason: str = ""
    killed_at: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def capital(self) -> float:
        """Botun yönettiği güncel sermaye (başlangıç + gerçekleşen K/Z)."""
        return self.start_capital + self.realized_pnl

    @classmethod
    def new(cls, mode: str, start_capital: float, quote_asset: str = "USDT") -> "BotState":
        state = cls(mode=mode, start_capital=round(start_capital, 8))
        if mode == "paper":
            state.paper_balances = {quote_asset: round(start_capital, 8)}
        return state

    def to_dict(self) -> dict:
        data = asdict(self)
        data["position"] = self.position.to_dict() if self.position else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "BotState":
        data = dict(data)
        pos = data.pop("position", None)
        known = set(cls.__dataclass_fields__)
        state = cls(**{k: v for k, v in data.items() if k in known})
        state.position = Position.from_dict(pos) if pos else None
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
    HEADER = ["acilis", "kapanis", "parite", "miktar", "alis_fiyati", "satis_fiyati", "maliyet_usdt",
              "net_gelir_usdt", "kar_zarar_usdt", "kar_zarar_yuzde", "komisyon_usdt", "neden", "sure_dk"]

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, pos: Position, exit_price: float, proceeds: float, fee_total: float,
               reason: str, closed_at: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        pnl = proceeds - pos.cost_usdt
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(self.HEADER)
            writer.writerow([
                datetime.fromtimestamp(pos.opened_at).strftime("%Y-%m-%d %H:%M:%S"),
                datetime.fromtimestamp(closed_at).strftime("%Y-%m-%d %H:%M:%S"),
                pos.symbol, f"{pos.qty:.8f}", f"{pos.entry_price:.8f}", f"{exit_price:.8f}",
                f"{pos.cost_usdt:.4f}", f"{proceeds:.4f}", f"{pnl:.4f}",
                f"{(pnl / pos.cost_usdt * 100) if pos.cost_usdt else 0:.2f}", f"{fee_total:.4f}",
                reason, f"{(closed_at - pos.opened_at) / 60:.1f}",
            ])
