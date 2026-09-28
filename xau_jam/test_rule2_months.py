"""Rule 2 monthly filler keeps empty months."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.rule2_months import RULE2, fill_months, year_slice
from xau_jam.stock_top3 import TOP3


class Rule2MonthsTests(unittest.TestCase):
    def test_is_rule_two(self) -> None:
        self.assertIs(RULE2, TOP3[1])
        self.assertEqual(RULE2.trigger, 0.003)
        self.assertEqual(RULE2.hold, 6)

    def test_fill_empty_months(self) -> None:
        rows = fill_months(
            [{"month": "2024-02", "n": 2, "wins": 1, "pnl": 10.0, "start": 350.0, "end": 360.0, "took": 0.0, "pct_start": 2.9, "pct_month": 2.9}],
            350.0,
            "2024-01",
            "2024-03",
        )
        self.assertEqual([r["month"] for r in rows], ["2024-01", "2024-02", "2024-03"])
        self.assertEqual(rows[0]["n"], 0)
        self.assertEqual(rows[0]["end"], 350.0)
        self.assertEqual(rows[1]["end"], 360.0)
        self.assertEqual(rows[2]["start"], 360.0)
        self.assertEqual(len(year_slice(rows, 2024)), 3)


if __name__ == "__main__":
    unittest.main()
