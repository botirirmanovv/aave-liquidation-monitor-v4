"""Top-3 gold recipes on stocks keep the same knobs."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.gold_impulse import Recipe
from xau_jam.pattern import Bar
from xau_jam.stock_top3 import STOCKS, TOP3, collect_shots


def bars() -> list[Bar]:
    start = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)
    out = []
    for i in range(16):
        t = start + timedelta(hours=i)
        if i == 0:
            out.append(Bar(t, 100, 110, 99.9, 101))
        else:
            out.append(Bar(t, 101, 102, 100, 101.5))
    return out


class StockTop3Tests(unittest.TestCase):
    def test_three_recipes(self) -> None:
        self.assertEqual(len(TOP3), 3)
        self.assertEqual(TOP3[0].hold, 12)
        self.assertEqual(TOP3[1].trigger, 0.003)
        self.assertEqual(TOP3[2].hold, 1)
        self.assertTrue(all(r.kind in {"usd", "pct"} for r in TOP3))
        self.assertNotIn("GC=F", STOCKS)

    def test_daily_uses_first_bar(self) -> None:
        rec = Recipe("daily-0.3%-h6", "paper", "daily", "pct", 0.003, 6, 9)
        shots = collect_shots({"NVDA": bars()}, rec, datetime(2026, 1, 1).date())
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0][2], "buy")

    def test_collect_respects_end(self) -> None:
        rec = Recipe("daily-0.3%-h6", "paper", "daily", "pct", 0.003, 6, 9)
        shots = collect_shots(
            {"NVDA": bars()}, rec, datetime(2026, 1, 1).date(), datetime(2026, 1, 5).date()
        )
        self.assertEqual(shots, [])

    def test_ny_dollar_trigger_collects(self) -> None:
        rec = Recipe("ny-$6-h12", "oneshot", "ny", "usd", 6.0, 12, 9)
        shots = collect_shots({"NVDA": bars()}, rec, datetime(2026, 1, 1).date())
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0][2], "buy")


if __name__ == "__main__":
    unittest.main()
