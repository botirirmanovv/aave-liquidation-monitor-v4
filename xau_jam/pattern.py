"""Jam (Джем) sell pattern on a single H1 pair of bars.

Sell signal (exactly as specified):
  1. Current candle is bearish: close < open.
  2. Its wick breaks the previous candle high: high > prev.high.
  3. Its body closes below the previous candle low: close < prev.low.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be the bar maximum")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be the bar minimum")


def is_jam_sell(prev: Bar, curr: Bar) -> bool:
    """True when `curr` is a completed Jam sell candle vs `prev`."""
    if curr.close >= curr.open:
        return False
    if curr.high <= prev.high:
        return False
    if curr.close >= prev.low:
        return False
    return True


def is_jam_buy(prev: Bar, curr: Bar) -> bool:
    """Mirror of the sell Jam (not used for the requested sell-only book)."""
    if curr.close <= curr.open:
        return False
    if curr.low >= prev.low:
        return False
    if curr.close <= prev.high:
        return False
    return True
