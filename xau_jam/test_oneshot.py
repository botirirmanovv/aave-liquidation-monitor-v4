"""Offline tests for 1:10 one-shot sizing."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.oneshot import _lots_at_lev


class OneshotTests(unittest.TestCase):
    def test_lev10_on_500_is_one_micro_lot(self) -> None:
        self.assertEqual(_lots_at_lev(500, 4300, 10), 0.01)

    def test_lev500_opens_more(self) -> None:
        self.assertGreaterEqual(_lots_at_lev(500, 4300, 500), 0.50)


if __name__ == "__main__":
    unittest.main()
