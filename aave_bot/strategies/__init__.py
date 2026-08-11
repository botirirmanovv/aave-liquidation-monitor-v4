"""Pluggable trading strategies.

Stage 3: Aave V2 flash arb + Balancer/UniV3 fee-tier arb plug into the same
event loop. Liquidation stays the default inlined path.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Strategy(Protocol):
    name: str

    def on_start(self) -> None:
        """Called once after the chain context is fully connected."""

    def on_price_update(self, assets: set[str]) -> None:
        """Price feed for one or more reserves moved."""

    def on_aave_event(self, event_name: str, user: str, reserve: str) -> None:
        """Aave pool position event (Supply/Borrow/…)."""

    def on_tick(self) -> None:
        """Periodic heartbeat — good for scanners that do not wait on events."""
