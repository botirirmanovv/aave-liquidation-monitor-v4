"""Gold recipes mapped onto impulse. Catalog only."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.gold_impulse import RECIPES, Recipe, gold_cash, oz_lot, score
from xau_jam.open_scalp import SESSIONS
from xau_jam.pattern import Bar


def h1_day() -> list:
    start = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        t = start + timedelta(hours=i)
        if i == 0:
            bars.append(Bar(t, 4000, 4030, 3998, 4010))
        else:
            bars.append(Bar(t, 4010, 4015, 4005, 4012))
    return bars


class GoldImpulseTests(unittest.TestCase):
    def test_recipes_cover_old_modules(self) -> None:
        src = {r.source for r in RECIPES}
        self.assertTrue({"paper", "jam", "burst", "open_scalp", "oneshot", "impulse"} <= src)
        self.assertTrue(all(r.session in SESSIONS for r in RECIPES))

    def test_oz_and_cash(self) -> None:
        self.assertEqual(oz_lot(70, 3500, 10), 0.20)
        self.assertAlmostEqual(gold_cash("buy", 4000, 4010, 0.2), 1.94)

    def test_london_impulse_fires(self) -> None:
        rec = Recipe("t", "paper", "london", "pct", 0.006, 6, 9)
        row = score(h1_day(), rec, datetime(2026, 1, 1).date(), 350)
        self.assertGreaterEqual(row.n, 1)
        self.assertEqual(row.session, "london")


if __name__ == "__main__":
    unittest.main()
