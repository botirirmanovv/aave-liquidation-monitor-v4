"""Offline tests for hunt scoring."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.hunt import _book
from xau_jam.pattern import Bar


def _bar(day: int, h: int, o: float, hi: float, lo: float, c: float) -> Bar:
    return Bar(
        time=datetime(2025, 1, day, 13, 30, tzinfo=timezone.utc) + timedelta(hours=h),
        open=o,
        high=hi,
        low=lo,
        close=c,
    )


class HuntTests(unittest.TestCase):
    def test_book_none_if_few_trades(self) -> None:
        bars = [_bar(2, 0, 100, 101, 99.8, 100.5)]
        bars += [_bar(2, i, 100.5, 100.8, 100.4, 100.6) for i in range(1, 8)]
        self.assertIsNone(
            _book(bars, 500, 10, datetime(2025, 1, 1).date(), datetime(2025, 2, 1).date(), 0.003, 6, 1)
        )
