"""Offline: 10% each, take every name, do not wait."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.combine import collect_signals, replay_one, watch_book
from xau_jam.pattern import Bar


def _day(sym_hour: int, o: float, hi: float, lo: float, c: float, day: int = 0) -> list[Bar]:
    start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc) + timedelta(days=day)
    out = []
    for i in range(8):
        if i == 0:
            out.append(Bar(start, o, hi, lo, c, 1.0))
        else:
            px = c
            out.append(Bar(start + timedelta(hours=i), px, px + 0.2, px - 0.1, px, 1.0))
    return out


class CombineTests(unittest.TestCase):
    def test_same_open_takes_both(self) -> None:
        mstr = _day(14, 100.0, 101.0, 99.8, 100.5)
        tsla = _day(14, 200.0, 201.0, 199.8, 200.4)
        books = {"MSTR": (mstr, 0.006), "TSLA": (tsla, 0.003)}
        shots = collect_signals(books, datetime(2026, 1, 1).date(), None)
        eq, fills = replay_one(shots, 500.0, 10)
        self.assertEqual([f.symbol for f in fills], ["MSTR", "TSLA"])
        self.assertEqual(fills[0].stake, 50.0)
        self.assertLess(fills[0].shares, 20)
        self.assertGreater(eq, 0)

    def test_next_day_takes_other_too(self) -> None:
        mstr = _day(14, 100.0, 101.0, 99.8, 100.5, day=0)
        tsla = _day(14, 200.0, 201.2, 199.9, 200.8, day=1)
        books = {"MSTR": (mstr, 0.006), "TSLA": (tsla, 0.003)}
        shots = collect_signals(books, datetime(2026, 1, 1).date(), None)
        _, fills = replay_one(shots, 500.0, 10)
        self.assertEqual([f.symbol for f in fills], ["MSTR", "TSLA"])

    def test_clip_caps_loss_at_stake(self) -> None:
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        bars = [Bar(start, 10.0, 10.2, 9.95, 10.1, 1.0)]
        for i in range(1, 8):
            bars.append(Bar(start + timedelta(hours=i), 1.0, 1.1, 0.9, 1.0, 1.0))
        shots = collect_signals({"MSTR": (bars, 0.006)}, datetime(2026, 1, 1).date(), None)
        eq, fills = replay_one(shots, 500.0, 10)
        self.assertEqual(fills[0].event, "clip")
        self.assertGreaterEqual(eq, 450.0)
        self.assertEqual(fills[0].cash, -50.0)

    def test_watch_book_opens_every_name(self) -> None:
        mstr = _day(14, 100.0, 101.0, 99.8, 100.5)
        tsla = _day(14, 200.0, 201.0, 199.8, 200.4)
        books = {"MSTR": (mstr, 0.006), "TSLA": (tsla, 0.003)}
        state = {
            "start": 500.0,
            "equity": 500.0,
            "simple": True,
            "risk": 0.1,
            "pos": None,
            "positions": [],
            "fills": [],
        }
        state = watch_book(books, state, 10)
        self.assertEqual({p["symbol"] for p in state["positions"]}, {"MSTR", "TSLA"})
        state = watch_book(books, state, 10)
        self.assertEqual(state["positions"], [])
        self.assertEqual({f["symbol"] for f in state["fills"]}, {"MSTR", "TSLA"})
