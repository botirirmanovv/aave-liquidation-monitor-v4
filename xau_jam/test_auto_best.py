"""Offline tests for auto-best compound book."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.auto_best import run_book
from xau_jam.backtest import Trade


def _t(pnl: float, entry: float = 100.0) -> Trade:
    return Trade(
        side="buy",
        signal_time="2026-09-01T00:00:00+00:00",
        entry_time="2026-09-01T00:00:00+00:00",
        exit_time="2026-09-01T01:00:00+00:00",
        entry=entry,
        stop=entry - 1,
        target=entry + pnl,
        exit=entry + pnl,
        bars_held=1,
        reason="time",
        pnl=pnl,
        r_multiple=0.0,
        signal_high=entry,
        prev_high=entry,
        prev_low=1.0,
        signal_close=entry,
    )


class AutoBestTests(unittest.TestCase):
    def test_ten_percent_all_in_doubles(self) -> None:
        book = run_book([_t(10.0)], 500, 10, "t", "X", "t")
        self.assertAlmostEqual(book.end, 1000.0)
        self.assertFalse(book.blown)

    def test_compound_second_trade(self) -> None:
        book = run_book([_t(10.0), _t(10.0)], 500, 10, "t", "X", "t")
        self.assertAlmostEqual(book.end, 2000.0)


if __name__ == "__main__":
    unittest.main()
