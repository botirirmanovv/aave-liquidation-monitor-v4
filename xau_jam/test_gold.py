"""Plan B stocks stay off BOOK A. Gold GC=F is gone."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.combine import BOOK
from xau_jam.gold import SYMBOL, TRIGGER
from xau_jam.paper import HOLD
from xau_jam.spare import BOOK_B


class SpareBTests(unittest.TestCase):
    def test_stock_book_a_unchanged(self) -> None:
        self.assertEqual(
            [s for s, _ in BOOK],
            ["MSTR", "COIN", "SMCI", "AMD", "UVXY", "PLTR", "TSLA"],
        )

    def test_b_disjoint_from_a(self) -> None:
        a = {s for s, _ in BOOK}
        b = {s for s, _ in BOOK_B}
        self.assertFalse(a & b)
        self.assertNotIn("GC=F", b)
        self.assertEqual(SYMBOL, "NVDA")
        self.assertEqual(TRIGGER, 0.006)
        self.assertEqual(HOLD, 6)

    def test_triggers_not_retuned(self) -> None:
        self.assertTrue(all(t == 0.006 for _, t in BOOK_B))
        self.assertEqual(
            [s for s, _ in BOOK_B],
            ["NVDA", "META", "AMZN", "NFLX", "AAPL", "BABA"],
        )


if __name__ == "__main__":
    unittest.main()
