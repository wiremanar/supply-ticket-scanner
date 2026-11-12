"""Persistent portfolio / audit state.

Tracks realized P&L, the day's trade count, the day's starting equity (for the
daily loss circuit breaker) and a full audit log of decisions and fills.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .brokers.base import Fill, Side


@dataclass
class DayState:
    day: str  # ISO date
    starting_equity: float
    trades_today: int = 0
    realized_pnl_today: float = 0.0
    halted: bool = False
    halt_reason: str = ""


@dataclass
class PortfolioState:
    realized_pnl: float = 0.0
    day: DayState | None = None
    audit_log: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "PortfolioState":
        if not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        day = raw.get("day")
        return cls(
            realized_pnl=raw.get("realized_pnl", 0.0),
            day=DayState(**day) if day else None,
            audit_log=raw.get("audit_log", []),
        )

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        data = {
            "realized_pnl": self.realized_pnl,
            "day": asdict(self.day) if self.day else None,
            "audit_log": self.audit_log[-1000:],  # keep the log bounded
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)

    def start_day_if_needed(self, equity: float, today: str | None = None) -> None:
        today = today or date.today().isoformat()
        if self.day is None or self.day.day != today:
            self.day = DayState(day=today, starting_equity=equity)

    def record_fill(self, fill: Fill, realized: float = 0.0) -> None:
        self.day.trades_today += 1  # type: ignore[union-attr]
        self.realized_pnl += realized
        self.day.realized_pnl_today += realized  # type: ignore[union-attr]
        self.log("fill", {
            "symbol": fill.symbol,
            "side": fill.side.value if isinstance(fill.side, Side) else fill.side,
            "quantity": fill.quantity,
            "price": fill.price,
            "notional": fill.notional,
            "realized": realized,
            "order_id": fill.broker_order_id,
            "timestamp": fill.timestamp,
        })

    def log(self, event: str, payload: dict[str, Any]) -> None:
        self.audit_log.append({"event": event, **payload})
