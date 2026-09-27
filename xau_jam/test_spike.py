"""Offline test: 100% price move at 1:10 is ~1000% of bank."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.spike import _score


class SpikeTests(unittest.TestCase):
    def test_double_price_near_thousand_pct(self) -> None:
        s = _score("X", "t", "buy", 10.0, 20.0, "2026-01-02", 500.0, 10)
        self.assertIsNotNone(s)
        self.assertGreater(s.bank_pct, 900)
        self.assertLess(s.bank_pct, 1100)
