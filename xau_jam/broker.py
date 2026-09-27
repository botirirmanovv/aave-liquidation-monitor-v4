"""Demo broker: same order path the live bot will use later.

Fills at the given price, one position, whole shares. Not a real venue.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from xau_jam.burst_open import REPORTS
from xau_jam.paper import costed_cash

DEMO_PATH = REPORTS / "demo_account.json"


@dataclass(slots=True)
class Order:
    id: str
    symbol: str
    side: str
    qty: int
    price: float
    status: str
    time: str
    reason: str
    cash: float
    equity: float


@dataclass(slots=True)
class Position:
    symbol: str
    side: str
    qty: int
    entry: float
    entry_time: str


class DemoBroker:
    """In-house demo account. Swap this class for a real broker later."""

    def __init__(self, start: float, leverage: int, path: Path | None = None) -> None:
        self.path = path or DEMO_PATH
        self.leverage = leverage
        self.start = start
        self.equity = start
        self.pos: Position | None = None
        self.orders: list[Order] = []
        self._n = 0
        if self.path.exists() and self.path.stat().st_size > 0:
            self._load()

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        self.start = float(raw.get("start", self.start))
        self.equity = float(raw.get("equity", self.equity))
        self.leverage = int(raw.get("leverage", self.leverage))
        self._n = int(raw.get("n", 0))
        pos = raw.get("pos")
        self.pos = Position(**pos) if pos else None
        self.orders = [Order(**o) for o in raw.get("orders", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "venue": "demo",
                    "start": self.start,
                    "equity": round(self.equity, 2),
                    "leverage": self.leverage,
                    "n": self._n,
                    "pos": asdict(self.pos) if self.pos else None,
                    "orders": [asdict(o) for o in self.orders],
                    "updated": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def reset(self, start: float | None = None) -> None:
        self.start = start if start is not None else self.start
        self.equity = self.start
        self.pos = None
        self.orders = []
        self._n = 0
        self.save()

    def submit_market(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        time: str,
        reason: str = "open",
    ) -> Order:
        if qty < 1:
            raise ValueError("qty < 1")
        if reason == "open" and self.pos is not None:
            raise ValueError("already in a position")
        if reason == "close" and self.pos is None:
            raise ValueError("flat, nothing to close")
        cash = 0.0
        if reason == "open":
            self.pos = Position(symbol, side, qty, round(price, 4), time)
        else:
            assert self.pos is not None
            cash = costed_cash(self.pos.side, self.pos.entry, price, self.pos.qty)
            self.equity = round(self.equity + cash, 2)
            if self.equity < 0:
                self.equity = 0.0
            self.pos = None
        self._n += 1
        order = Order(
            id=f"demo-{self._n}",
            symbol=symbol,
            side=side,
            qty=qty,
            price=round(price, 4),
            status="filled",
            time=time,
            reason=reason,
            cash=round(cash, 2),
            equity=round(self.equity, 2),
        )
        self.orders.append(order)
        self.save()
        return order

    def close(self, price: float, time: str) -> Order:
        if self.pos is None:
            raise ValueError("flat")
        side = "sell" if self.pos.side == "buy" else "buy"
        return self.submit_market(self.pos.symbol, side, self.pos.qty, price, time, "close")
