"""Offline tests for auto-best compound book."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.auto_best import _impulse_trades, run_book
from xau_jam.backtest import Trade
from xau_jam.pattern import Bar


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


def _bar(h: int, o: float, hi: float, lo: float, c: float) -> Bar:
    return Bar(
        time=datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc) + timedelta(hours=h),
        open=o,
        high=hi,
        low=lo,
        close=c,
    )


class AutoBestTests(unittest.TestCase):
    def test_ten_percent_all_in_doubles(self) -> None:
        book = run_book([_t(10.0)], 500, 10, "t", "X", "t")
        self.assertAlmostEqual(book.end, 1000.0)
        self.assertFalse(book.blown)

    def test_compound_second_trade(self) -> None:
        book = run_book([_t(10.0), _t(10.0)], 500, 10, "t", "X", "t")
        self.assertAlmostEqual(book.end, 2000.0)

    def test_impulse_skips_both_wicks(self) -> None:
        bars = [_bar(0, 100, 101, 99, 100)]
        self.assertEqual(_impulse_trades(bars, 0.003, 6, skip_both=True), [])
        raw = _impulse_trades(bars, 0.003, 6, skip_both=False)
        self.assertEqual(len(raw), 1)

    def test_impulse_keeps_one_side(self) -> None:
        bars = [_bar(0, 100, 101, 99.8, 100.5)]
        bars += [_bar(i, 100.5, 100.8, 100.4, 100.6) for i in range(1, 8)]
        clean = _impulse_trades(bars, 0.003, 6, skip_both=True)
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0].side, "buy")


if __name__ == "__main__":
    unittest.main()
