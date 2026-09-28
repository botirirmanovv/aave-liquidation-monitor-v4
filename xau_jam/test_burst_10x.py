"""Offline tests for locked impulse book / stack margin."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.backtest import Trade
from xau_jam.burst_10x import group_stacks, live_params, run_book


def _t(day: str, hm: str, side: str, pnl: float, entry: float = 4300.0) -> Trade:
    ts = f"2026-09-{day}T{hm}:00+00:00"
    return Trade(
        side=side,
        signal_time=ts,
        entry_time=ts,
        exit_time=ts,
        entry=entry,
        stop=entry - 2.5,
        target=entry + 1,
        exit=entry + pnl,
        bars_held=1,
        reason="time",
        pnl=pnl,
        r_multiple=0.0,
        signal_high=entry,
        prev_high=entry,
        prev_low=1.0,
        signal_close=entry,
    )


class Burst10xTests(unittest.TestCase):
    def test_live_params_are_london_straddle(self) -> None:
        p = live_params("1m")
        self.assertEqual(p.session, "london")
        self.assertEqual(p.model, "straddle")
        self.assertEqual(p.trigger, 2.5)
        self.assertEqual(p.step, 0.5)
        self.assertEqual(p.layers, 8)
        self.assertEqual(p.hold_bars, 1)
        self.assertEqual(p.flatten, "close")

    def test_group_stacks_by_exit(self) -> None:
        trades = [
            _t("21", "07:00", "sell", 1.0),
            _t("21", "07:00", "sell", 0.5),
            _t("22", "07:00", "buy", -1.0),
        ]
        # same exit_time on first two (both 07:00 same day) — wait, I used same clock
        stacks = group_stacks(trades)
        self.assertEqual(len(stacks), 2)
        self.assertEqual(len(stacks[0]), 2)
        self.assertEqual(len(stacks[1]), 1)

    def test_stack_margin_blocks_fat_lot_on_500(self) -> None:
        trades = [_t("21", "07:00", "sell", 2.0) for _ in range(8)]
        book = run_book(trades, start=500, leverage=500, lots=0.50, name="fat")
        # 8 * 50 oz * 4300 / 500 = $3440 margin > 40% of $500
        self.assertTrue(book.path)
        self.assertIn(book.path[0].event, {"no_margin", "ok"})
        if book.path[0].event == "ok":
            self.assertLess(book.path[0].lots, 0.50)

    def test_ten_x_scales_pnl(self) -> None:
        trades = [_t("21", "07:00", "sell", 1.0) for _ in range(4)]
        small = run_book(trades, start=5000, leverage=500, lots=0.01, name="s")
        fat = run_book(trades, start=5000, leverage=500, lots=0.10, name="f")
        self.assertAlmostEqual(fat.net, small.net * 10, places=0)


if __name__ == "__main__":
    unittest.main()
