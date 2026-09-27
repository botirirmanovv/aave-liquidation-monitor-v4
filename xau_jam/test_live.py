"""Offline tests for the demo-broker live path."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.broker import DemoBroker
from xau_jam.live import replay_through_broker, session_open, tick
from xau_jam.pattern import Bar


def _bar(h: int, o: float, hi: float, lo: float, c: float) -> Bar:
    return Bar(
        time=datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc) + timedelta(hours=h),
        open=o,
        high=hi,
        low=lo,
        close=c,
    )


class DemoLiveTests(unittest.TestCase):
    def test_session_closed_weekend(self) -> None:
        sunday = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
        self.assertFalse(session_open(sunday))

    def test_session_open_weekday(self) -> None:
        monday = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)
        self.assertTrue(session_open(monday))

    def _broker(self) -> DemoBroker:
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(lambda: path.exists() and path.unlink())
        br = DemoBroker(500, 10, path)
        br.reset(500)
        return br

    def test_broker_open_and_close(self) -> None:
        br = self._broker()
        br.submit_market("TSLA", "buy", 10, 100.0, "t0", "open")
        self.assertIsNotNone(br.pos)
        br.close(101.0, "t1")
        self.assertIsNone(br.pos)
        self.assertGreater(br.equity, 500)
        self.assertEqual(len(br.orders), 2)
        self.assertEqual(br.orders[0].status, "filled")

    def test_replay_skips_both_wicks(self) -> None:
        bars = [_bar(0, 100, 101, 99, 100)]
        br = self._broker()
        replay_through_broker(bars, br, days=7)
        self.assertEqual(br.orders, [])
        self.assertEqual(br.equity, 500)

    def test_replay_places_demo_orders(self) -> None:
        bars = [_bar(0, 100, 101, 99.8, 100.5)]
        bars += [_bar(i, 100.5, 100.8, 100.4, 100.7) for i in range(1, 8)]
        br = self._broker()
        replay_through_broker(bars, br, days=7)
        self.assertEqual(len(br.orders), 2)
        self.assertEqual(br.orders[0].reason, "open")
        self.assertEqual(br.orders[0].side, "buy")
        self.assertEqual(br.orders[1].reason, "close")
        self.assertIsNone(br.pos)

    def test_tick_refuses_when_closed(self) -> None:
        br = self._broker()
        sunday = datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc)
        note = tick([], br, now=sunday)
        self.assertIn("закрыта", note)
        self.assertEqual(br.orders, [])
