"""Offline tests for burst open clips. No network."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.burst_open import BurstParams, run_burst
from xau_jam.pattern import Bar


def bar(h: int, m: int, o: float, hi: float, lo: float, c: float) -> Bar:
    return Bar(
        time=datetime(2026, 9, 1, h, m, tzinfo=timezone.utc),
        open=o,
        high=hi,
        low=lo,
        close=c,
    )


class BurstOpenTests(unittest.TestCase):
    def test_straddle_buys_up_spike_and_flattens(self) -> None:
        bars = [
            bar(13, 0, 2000, 2000.2, 1999.8, 2000),
            bar(13, 5, 2000, 2012, 1999.5, 2010),  # rips up, fills layers
            bar(13, 10, 2010, 2014, 2009, 2013),
            bar(13, 15, 2013, 2014, 2012, 2013.5),
            bar(13, 20, 2013.5, 2014, 2012, 2013),
        ]
        p = BurstParams(
            session="ny",
            model="straddle",
            trigger=6,
            layers=3,
            step=2,
            hold_bars=3,
            spread=0.0,
            fail_through_open=False,
        )
        r, trades = run_burst(bars, p)
        self.assertGreaterEqual(r.clips, 2)
        self.assertTrue(all(t.side == "buy" for t in trades))
        self.assertTrue(all(t.reason == "time" for t in trades))

    def test_spray_does_not_fire_on_tiny_wiggle(self) -> None:
        bars = [
            bar(13, 0, 2000, 2000.3, 1999.8, 2000.1),
            bar(13, 5, 2000.1, 2000.4, 1999.9, 2000.2),
            bar(13, 10, 2000.2, 2000.3, 2000.0, 2000.1),
        ]
        p = BurstParams(session="ny", model="spray", trigger=6, layers=5, hold_bars=2, spread=0.0)
        r, trades = run_burst(bars, p)
        self.assertEqual(r.clips, 0)
        self.assertEqual(trades, [])

    def test_same_bar_hold_exits_on_fill_close(self) -> None:
        bars = [
            bar(13, 0, 2000, 2000.2, 1999.8, 2000),
            bar(13, 1, 2000, 2014, 1999.9, 2011),  # fill + flatten same minute
            bar(13, 2, 2011, 2020, 2010, 2018),  # later spike must not be used
        ]
        p = BurstParams(
            session="ny",
            model="straddle",
            trigger=6,
            layers=3,
            step=2,
            orb_bars=5,
            hold_bars=0,
            spread=0.0,
            fail_through_open=False,
        )
        r, trades = run_burst(bars, p)
        self.assertGreaterEqual(r.clips, 2)
        self.assertTrue(all(t.exit_time == t.entry_time for t in trades))
        self.assertTrue(all(t.exit == 2011 for t in trades))

    def test_one_minute_hold_exits_next_bar(self) -> None:
        bars = [
            bar(13, 0, 2000, 2000.2, 1999.8, 2000),
            bar(13, 1, 2000, 2012, 1999.9, 2010),
            bar(13, 2, 2010, 2013, 2008, 2009),
        ]
        p = BurstParams(
            session="ny",
            model="straddle",
            trigger=6,
            layers=2,
            step=2,
            orb_bars=5,
            hold_bars=1,
            spread=0.0,
            fail_through_open=False,
        )
        _, trades = run_burst(bars, p)
        self.assertGreater(len(trades), 0)
        self.assertTrue(all(t.exit == 2009 for t in trades))
        self.assertTrue(all("13:02:00" in t.exit_time for t in trades))

    def test_fail_through_open_dumps_the_stack(self) -> None:
        bars = [
            bar(13, 0, 2000, 2000.2, 1999.8, 2000),
            bar(13, 5, 2000, 2012, 1999.9, 2010),
            bar(13, 10, 2010, 2011, 1999.0, 2001),  # back through open
        ]
        p = BurstParams(
            session="ny",
            model="straddle",
            trigger=6,
            layers=2,
            step=2,
            hold_bars=6,
            spread=0.0,
            fail_through_open=True,
        )
        r, trades = run_burst(bars, p)
        self.assertGreater(r.clips, 0)
        self.assertTrue(all(t.reason == "fail_open" for t in trades))


if __name__ == "__main__":
    unittest.main()
