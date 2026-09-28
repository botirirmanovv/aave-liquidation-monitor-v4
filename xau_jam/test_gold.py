"""Gold Plan B uses Combine impulse rules. Does not touch the stock BOOK."""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.combine import BOOK
from xau_jam.gold import SYMBOL, TRIGGER, gold_cash, ny_session_idxs, oz_lot, replay_gold
from xau_jam.paper import HOLD, signal_for_day
from xau_jam.pattern import Bar


def b(day: int, hour: int, o: float, hi: float, lo: float, c: float) -> Bar:
    return Bar(
        time=datetime(2026, 1, day, hour, 30, tzinfo=timezone.utc),
        open=o,
        high=hi,
        low=lo,
        close=c,
    )


class GoldBTests(unittest.TestCase):
    def test_stock_book_unchanged(self) -> None:
        self.assertEqual(
            [s for s, _ in BOOK],
            ["MSTR", "COIN", "SMCI", "AMD", "UVXY", "PLTR", "TSLA"],
        )

    def test_gold_is_gcf_not_stock(self) -> None:
        self.assertEqual(SYMBOL, "GC=F")
        self.assertEqual(TRIGGER, 0.006)
        self.assertEqual(HOLD, 6)

    def test_ny_session_skips_asia(self) -> None:
        bars = [b(2, 1, 4000, 4001, 3999, 4000), b(2, 13, 4000, 4030, 3999, 4010)]
        sl = ny_session_idxs(bars, [0, 1])
        self.assertEqual(sl, [1])

    def test_impulse_same_function_as_stocks(self) -> None:
        bars = [b(2, 13, 4000, 4030, 3995, 4010)]
        sig = signal_for_day(bars, [0], trigger=0.006)
        self.assertIsNotNone(sig)
        self.assertEqual(sig[0], "buy")

    def test_oz_lot_steps(self) -> None:
        self.assertEqual(oz_lot(70, 3500, 10), 0.20)
        self.assertEqual(oz_lot(70, 1_000_000, 10), 0.0)

    def test_gold_cash_buy(self) -> None:
        cash = gold_cash("buy", 4000, 4010, 0.20)
        self.assertAlmostEqual(cash, (4010 - 0.15 - 4000 - 0.15) * 0.20)

    def test_replay_opens_one_shot(self) -> None:
        start = datetime(2026, 1, 5, 13, 30, tzinfo=timezone.utc)
        bars = []
        for i in range(10):
            t = start + timedelta(hours=i)
            if i == 0:
                bars.append(Bar(t, 4000, 4030, 3998, 4010))
            else:
                bars.append(Bar(t, 4010, 4012, 4008, 4011))
        end, fills, _w = replay_gold(bars, 350, date(2026, 1, 1))
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].side, "buy")
        self.assertGreater(end, 0)


if __name__ == "__main__":
    unittest.main()
