"""Offline: 20% price at 50x is ~1000% of bank after Binance fees."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.binance_spike import _score, futures_cash, hit_liq, liq_price, pick_leverage
from xau_jam.pattern import Bar


def _bar(o: float, h: float, l: float, c: float) -> Bar:
    return Bar(datetime(2026, 1, 2, tzinfo=timezone.utc), o, h, l, c, 1.0)


class BinanceSpikeTests(unittest.TestCase):
    def test_pick_50x(self) -> None:
        self.assertEqual(pick_leverage("BTCUSDT"), 50)
        self.assertEqual(pick_leverage("1000PEPEUSDT"), 50)

    def test_twenty_pct_near_thousand(self) -> None:
        cash = futures_cash("buy", 10.0, 12.0, 500.0, 50)
        pct = 100.0 * cash / 500.0
        self.assertGreater(pct, 900)
        self.assertLess(pct, 1100)

    def test_score_matches_cash(self) -> None:
        s = _score("XUSDT", "t", "buy", 10.0, 12.0, "2026-01-02", 500.0, 50)
        self.assertIsNotNone(s)
        self.assertGreater(s.bank_pct, 900)
        self.assertFalse(s.liq)

    def test_liq_on_adverse_wick(self) -> None:
        entry = 100.0
        px = liq_price("buy", entry, 50)
        self.assertLess(px, entry)
        self.assertTrue(hit_liq("buy", entry, [_bar(100, 101, px - 0.1, 100.5)], 50))
        self.assertFalse(hit_liq("buy", entry, [_bar(100, 110, 99.5, 109)], 50))
        dead = _score("XUSDT", "t", "buy", 100.0, 130.0, "2026-01-02", 500.0, 50, [_bar(100, 130, px - 1, 130)])
        self.assertTrue(dead.liq)
        self.assertEqual(dead.bank_pct, -100.0)
