"""Offline tests for the Jam sell pattern and fill engine. No network."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.backtest import detect_signals, run_backtest
from xau_jam.pattern import Bar, is_jam_buy, is_jam_sell


def _t(i: int) -> datetime:
    return datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(hours=i)


def bar(i: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=_t(i), open=o, high=h, low=l, close=c)


class JamPatternTests(unittest.TestCase):
    def test_sell_matches_spec(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 101.5, 103, 97, 98)
        self.assertTrue(curr.close < curr.open)
        self.assertTrue(curr.high > prev.high)
        self.assertTrue(curr.close < prev.low)
        self.assertTrue(is_jam_sell(prev, curr))

    def test_rejects_bullish(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 98, 103, 97, 99)
        self.assertFalse(is_jam_sell(prev, curr))

    def test_rejects_if_high_does_not_break(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 101, 102, 97, 98)
        self.assertFalse(is_jam_sell(prev, curr))

    def test_rejects_equal_high(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 101, 102.0, 97, 98)
        self.assertFalse(is_jam_sell(prev, curr))

    def test_rejects_close_not_below_prev_low(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 101.5, 103, 99, 99.5)
        self.assertFalse(is_jam_sell(prev, curr))

    def test_rejects_close_equal_prev_low(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 101.5, 103, 98, 99)
        self.assertFalse(is_jam_sell(prev, curr))

    def test_buy_mirror(self) -> None:
        prev = bar(0, 101, 102, 99, 100)
        curr = bar(1, 100.5, 104, 98, 103)
        self.assertTrue(is_jam_buy(prev, curr))
        self.assertFalse(is_jam_sell(prev, curr))


class BacktestEngineTests(unittest.TestCase):
    def test_entry_is_next_open_not_signal_close(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),  # jam sell
            bar(2, 97.5, 98.0, 80.0, 81.0),  # entry + hits 2R target (85.5)
        ]
        self.assertEqual(detect_signals(bars), [1])
        result, trades = run_backtest(bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=24)
        self.assertEqual(result.trades, 1)
        self.assertAlmostEqual(trades[0].entry, 97.5)
        self.assertEqual(trades[0].reason, "target")
        self.assertGreater(trades[0].pnl, 0)

    def test_stop_hits(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),
            bar(2, 97.5, 110.0, 97.0, 109.0),
        ]
        _, trades = run_backtest(bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=24)
        self.assertEqual(trades[0].reason, "stop")
        self.assertLess(trades[0].pnl, 0)

    def test_same_bar_sl_and_tp_uses_stop(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),
            bar(2, 97.5, 110.0, 80.0, 90.0),
        ]
        _, trades = run_backtest(bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=24)
        self.assertEqual(trades[0].reason, "sl_before_tp")
        self.assertLess(trades[0].pnl, 0)

    def test_skips_while_in_trade(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),
            bar(2, 97.5, 98.2, 97.0, 97.2),
            bar(3, 97.2, 99.0, 90.0, 91.0),  # would also be jam vs bar 2
            bar(4, 91.0, 91.5, 80.0, 81.0),
        ]
        result, trades = run_backtest(bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=24)
        self.assertGreaterEqual(result.signals, 1)
        self.assertEqual(len(trades), 1)

    def test_time_stop_exits_at_last_close(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),
            bar(2, 97.5, 98.0, 97.0, 97.2),
            bar(3, 97.2, 98.0, 97.0, 97.1),
        ]
        _, trades = run_backtest(bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=2)
        self.assertEqual(trades[0].reason, "time")
        self.assertEqual(trades[0].bars_held, 2)


if __name__ == "__main__":
    unittest.main()
