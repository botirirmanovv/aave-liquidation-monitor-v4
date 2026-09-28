"""Offline: bot finds one-side 1% impulse and paper-opens it."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.binance_auto import first_signal, replay, tick
from xau_jam.pattern import Bar


def _bars(start: datetime, days: int, hour) -> list[Bar]:
    out: list[Bar] = []
    for d in range(days):
        for h in range(24):
            t = start + timedelta(days=d, hours=h)
            o, hi, lo, c = hour(d, h)
            out.append(Bar(t, o, hi, lo, c, 1.0))
    return out


class BinanceAutoTests(unittest.TestCase):
    def test_skips_both_wick(self) -> None:
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)

        def hour(d: int, h: int):
            if d == 0 and h == 0:
                return 100.0, 102.0, 98.0, 100.0
            return 100.0, 100.2, 99.8, 100.0

        book = {"ENAUSDT": _bars(start, 3, hour)}
        self.assertIsNone(first_signal(book, start.date()))
        eq, fills = replay(book, 500.0)
        self.assertEqual(fills, [])
        self.assertEqual(eq, 500.0)

    def test_opens_buy_and_holds_two_days(self) -> None:
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)

        def hour(d: int, h: int):
            if d == 0 and h == 0:
                return 100.0, 101.2, 99.9, 101.0
            px = 101.0 + d * 10 + h * 0.2
            return px, px + 0.3, px - 0.2, px + 0.1

        book = {"ENAUSDT": _bars(start, 3, hour)}
        got = first_signal(book, start.date())
        self.assertIsNotNone(got)
        self.assertEqual(got[0], "ENAUSDT")
        self.assertEqual(got[2], "buy")
        eq, fills = replay(book, 500.0)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].event, "ok")
        self.assertEqual(fills[0].side, "buy")
        self.assertGreater(eq, 500.0)

    def test_liq_blows_account(self) -> None:
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)

        def hour(d: int, h: int):
            if d == 0 and h == 0:
                return 100.0, 101.2, 99.9, 101.0
            if d == 1 and h == 3:
                return 100.0, 100.1, 90.0, 91.0
            return 101.0, 101.2, 100.8, 101.0

        book = {"ENAUSDT": _bars(start, 3, hour)}
        eq, fills = replay(book, 500.0)
        self.assertEqual(fills[0].event, "liq")
        self.assertEqual(eq, 0.0)

    def test_risk10_liq_keeps_reserve(self) -> None:
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)

        def hour(d: int, h: int):
            if d == 0 and h == 0:
                return 100.0, 101.2, 99.9, 101.0
            if d == 1 and h == 3:
                return 100.0, 100.1, 90.0, 91.0
            if d == 3 and h == 0:
                return 100.0, 101.2, 99.9, 101.0
            if d >= 3:
                px = 110.0 + (d - 3) * 2 + h * 0.1
                return px, px + 0.2, px - 0.05, px + 0.1
            return 101.0, 101.2, 100.8, 101.0

        book = {"ENAUSDT": _bars(start, 6, hour)}
        eq, fills = replay(book, 100.0, simple=False, risk=0.1)
        self.assertGreaterEqual(len(fills), 1)
        self.assertEqual(fills[0].event, "liq")
        self.assertAlmostEqual(fills[0].equity, 90.0)
        self.assertGreater(eq, 80.0)
        self.assertNotEqual(eq, 0.0)

    def test_tick_opens_from_state(self) -> None:
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)

        def hour(d: int, h: int):
            if d == 2 and h == 0:
                return 100.0, 101.3, 99.95, 101.1
            return 100.0, 100.2, 99.9, 100.0

        book = {"ENAUSDT": _bars(start, 3, hour)}
        state = {"equity": 500.0, "start": 500.0, "leverage": 50, "pos": None, "fills": []}
        state = tick(book, state)
        self.assertIsNotNone(state["pos"])
        self.assertEqual(state["pos"]["symbol"], "ENAUSDT")
        self.assertIn("открыл", state["note"])
