"""Offline tests for $100 paper impulse."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.paper import costed_cash, signal_for_day, watch
from xau_jam.pattern import Bar


def bar(h: int, o: float, hi: float, lo: float, c: float) -> Bar:
    return Bar(
        time=datetime(2026, 9, 21, h, 30, tzinfo=timezone.utc),
        open=o,
        high=hi,
        low=lo,
        close=c,
    )


class PaperTests(unittest.TestCase):
    def test_skip_both_wicks(self) -> None:
        bars = [bar(13, 100, 101, 99, 100.2)]
        self.assertIsNone(signal_for_day(bars, [0]))

    def test_one_side_buy(self) -> None:
        bars = [bar(13, 100, 101, 99.8, 100.5)]
        sig = signal_for_day(bars, [0])
        self.assertIsNotNone(sig)
        self.assertEqual(sig[0], "buy")

    def test_min_commission_hurts_100_bucks(self) -> None:
        # 2 shares, $1 min * 2 = $2 commission
        cash = costed_cash("buy", 100.0, 101.0, 2)
        self.assertLess(cash, 2.0 * 1.0)

    def test_watch_opens_then_exits_after_hold(self) -> None:
        bars = [bar(13 + i, 100, 101, 99.9, 100.4) for i in range(8)]
        # fix hours
        out = []
        for i in range(8):
            out.append(
                Bar(
                    time=datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc)
                    + __import__("datetime").timedelta(hours=i),
                    open=100,
                    high=102,
                    low=99.9,
                    close=100.4 + i * 0.1,
                )
            )
        st = {"equity": 100.0, "pos": None, "fills": []}
        st = watch(out[:2], st, 10)
        self.assertIsNotNone(st["pos"])
        st = watch(out, st, 10)
        self.assertIsNone(st["pos"])
        self.assertGreater(len(st["fills"]), 0)


if __name__ == "__main__":
    unittest.main()
