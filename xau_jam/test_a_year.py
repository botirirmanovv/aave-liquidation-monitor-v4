"""Strategy A 2026 runner uses the locked BOOK."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.a_year import fetch_a
from xau_jam.combine import BOOK


class AYearTests(unittest.TestCase):
    def test_book_is_strategy_a(self) -> None:
        self.assertEqual(
            [s for s, _ in BOOK],
            ["MSTR", "COIN", "SMCI", "AMD", "UVXY", "PLTR", "TSLA"],
        )
        self.assertEqual(BOOK[-1], ("TSLA", 0.003))
        self.assertTrue(all(t == 0.006 for s, t in BOOK[:-1]))

    def test_fetch_helper_exists(self) -> None:
        self.assertTrue(callable(fetch_a))


if __name__ == "__main__":
    unittest.main()
