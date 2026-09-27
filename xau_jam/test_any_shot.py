"""Offline cash math for any-market 1:10 all-in."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.any_shot import cash_at


class AnyShotTests(unittest.TestCase):
    def test_ten_percent_move_doubles_500_at_1_to_10(self) -> None:
        # $500 * 10 * 10% = $500
        self.assertAlmostEqual(cash_at(500, 10, 100.0, 10.0), 500.0)


if __name__ == "__main__":
    unittest.main()
