"""Offline tests for the daily-open spike scalp. No network."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.open_scalp import OpenParams, run_open_scalp
from xau_jam.pattern import Bar


def _t(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def bar(day: int, hour: int, minute: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=_t(day, hour, minute), open=o, high=h, low=l, close=c)


def _flat_session(day: int, hour: int, px: float, n: int = 8) -> list[Bar]:
    out = []
    t0 = datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc)
    for i in range(n):
        ts = t0 + timedelta(minutes=5 * i)
        out.append(Bar(time=ts, open=px, high=px + 0.2, low=px - 0.2, close=px))
    return out


class OpenScalpTests(unittest.TestCase):
    def test_momentum_buy_on_up_spike(self) -> None:
        bars = [
            bar(1, 21, 55, 2000, 2000.2, 1999.8, 2000),
            bar(1, 22, 0, 2000, 2000.3, 1999.9, 2000.1),
            bar(1, 22, 5, 2000.1, 2010.0, 2000.0, 2009.0),  # +10 spike
            bar(1, 22, 10, 2009.0, 2012.0, 2008.5, 2011.0),
            bar(1, 22, 15, 2011.0, 2013.0, 2010.0, 2012.0),
            bar(1, 22, 20, 2012.0, 2014.0, 2011.0, 2013.0),
            bar(1, 22, 25, 2013.0, 2014.0, 2012.0, 2013.0),
            bar(1, 22, 30, 2013.0, 2014.0, 2012.0, 2013.5),
        ]
        p = OpenParams(
            session="cme",
            style="momentum",
            min_spike=6.0,
            orb_bars=4,
            hold_bars=4,
            clips=1,
            spread=0.0,
            sl_buffer=1.0,
            rr=1.0,
        )
        r, trades = run_open_scalp(bars, p)
        self.assertEqual(r.trades, 1)
        self.assertEqual(trades[0].side, "buy")
        self.assertAlmostEqual(trades[0].entry, 2009.0)

    def test_fade_sells_the_up_spike(self) -> None:
        bars = [
            bar(1, 22, 0, 2000, 2000.2, 1999.8, 2000),
            bar(1, 22, 5, 2000, 2010.0, 1999.5, 2009.0),
            bar(1, 22, 10, 2009.0, 2009.5, 1995.0, 1996.0),  # fade target
            bar(1, 22, 15, 1996.0, 1997.0, 1994.0, 1995.0),
            bar(1, 22, 20, 1995.0, 1996.0, 1993.0, 1994.0),
        ]
        p = OpenParams(
            session="cme",
            style="fade",
            min_spike=6.0,
            orb_bars=3,
            hold_bars=3,
            clips=1,
            spread=0.0,
            sl_buffer=1.0,
            rr=1.0,
        )
        r, trades = run_open_scalp(bars, p)
        self.assertEqual(r.trades, 1)
        self.assertEqual(trades[0].side, "sell")

    def test_small_wiggle_is_not_a_spike(self) -> None:
        bars = _flat_session(1, 22, 2000.0, 8)
        p = OpenParams(session="cme", min_spike=6.0, orb_bars=4, hold_bars=3, clips=1, spread=0.0)
        r, trades = run_open_scalp(bars, p)
        self.assertEqual(r.trades, 0)
        self.assertEqual(trades, [])

    def test_entry_is_next_bar_not_signal_bar(self) -> None:
        bars = [
            bar(1, 22, 0, 2000, 2000.1, 1999.9, 2000),
            bar(1, 22, 5, 2000, 2010, 2000, 2008),
            bar(1, 22, 10, 2007.5, 2008, 2007, 2007.2),
            bar(1, 22, 15, 2007.2, 2007.4, 2007.0, 2007.1),
        ]
        p = OpenParams(
            session="cme",
            style="momentum",
            min_spike=6,
            orb_bars=3,
            hold_bars=2,
            clips=1,
            spread=0.0,
            sl_buffer=1,
            rr=10,
        )
        _, trades = run_open_scalp(bars, p)
        self.assertEqual(trades[0].entry, 2007.5)


if __name__ == "__main__":
    unittest.main()
