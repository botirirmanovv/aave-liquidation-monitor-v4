"""Offline tests for $100 paper impulse."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.paper import costed_cash, cron_line, install_cron, replay, shift_months, signal_for_day, state_path, watch
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

    def test_state_path_is_per_bank(self) -> None:
        self.assertIn("paper_state_500.json", str(state_path(500)))
        self.assertIn("paper_state_100.json", str(state_path(100)))

    def test_shift_months_back_21(self) -> None:
        from datetime import date

        self.assertEqual(shift_months(date(2026, 9, 25), -21), date(2024, 12, 25))

    def test_replay_begin_end_skips_outside(self) -> None:
        bars = []
        for d in (1, 2, 20):
            day = datetime(2025, 1, d, 13, 30, tzinfo=timezone.utc)
            bars.append(Bar(time=day, open=100, high=102, low=99.8, close=101))
            for i in range(1, 7):
                bars.append(
                    Bar(
                        time=day + __import__("datetime").timedelta(hours=i),
                        open=101,
                        high=105,
                        low=100.5,
                        close=104,
                    )
                )
        _, path = replay(
            bars,
            500,
            10,
            compound=False,
            begin=datetime(2025, 1, 1).date(),
            end=datetime(2025, 1, 10).date(),
        )
        self.assertEqual(len(path), 2)
        self.assertTrue(all(p.time.startswith("2025-01-0") for p in path))

    def test_simple_keeps_share_size(self) -> None:
        bars = []
        t0 = datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc)
        for d in (1, 2):
            day = datetime(2026, 9, d, 13, 30, tzinfo=timezone.utc)
            bars.append(
                Bar(time=day, open=100, high=102, low=99.8, close=101)
            )
            for i in range(1, 7):
                bars.append(
                    Bar(
                        time=day + __import__("datetime").timedelta(hours=i),
                        open=101,
                        high=105,
                        low=100.5,
                        close=104,
                    )
                )
        _, simple = replay(bars, 500, 10, days=30, compound=False)
        _, rich = replay(bars, 500, 10, days=30, compound=True)
        self.assertEqual(len(simple), 2)
        self.assertEqual(simple[0].shares, simple[1].shares)
        self.assertGreater(rich[1].shares, rich[0].shares)

    def test_cron_line_and_file(self) -> None:
        line = cron_line(500)
        self.assertIn("--watch --bank 500", line)
        self.assertIsNone(install_cron(500))
        written = (Path(__file__).resolve().parent / "reports" / "paper_cron.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("--bank 500", written)


if __name__ == "__main__":
    unittest.main()
