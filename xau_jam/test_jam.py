"""Offline tests for the Jam sell pattern and fill engine. No network."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.bank import oz_fixed, oz_pct, simulate
from xau_jam.leverage import run_fixed_lot, run_lev
from xau_jam.backtest import Trade, detect_signals, run_backtest
from xau_jam.pattern import Bar, is_jam_buy, is_jam_sell


def _t(i: int) -> datetime:
    return datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(hours=i)


def bar(i: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=_t(i), open=o, high=h, low=l, close=c)


class JamPatternTests(unittest.TestCase):
    def test_sell_matches_spec(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 101.5, 103, 97, 98)
        self.assertTrue(curr.close < curr.open)
        self.assertTrue(curr.high > prev.high)
        self.assertTrue(curr.close < prev.low)
        self.assertTrue(is_jam_sell(prev, curr))

    def test_rejects_bullish(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 98, 103, 97, 99)
        self.assertFalse(is_jam_sell(prev, curr))

    def test_rejects_if_high_does_not_break(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 101, 102, 97, 98)
        self.assertFalse(is_jam_sell(prev, curr))

    def test_rejects_equal_high(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 101, 102.0, 97, 98)
        self.assertFalse(is_jam_sell(prev, curr))

    def test_rejects_close_not_below_prev_low(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 101.5, 103, 99, 99.5)
        self.assertFalse(is_jam_sell(prev, curr))

    def test_rejects_close_equal_prev_low(self) -> None:
        prev = bar(0, 100, 102, 99, 101)
        curr = bar(1, 101.5, 103, 98, 99)
        self.assertFalse(is_jam_sell(prev, curr))

    def test_buy_mirror(self) -> None:
        prev = bar(0, 101, 102, 99, 100)
        curr = bar(1, 100.5, 104, 98, 103)
        self.assertTrue(is_jam_buy(prev, curr))
        self.assertFalse(is_jam_sell(prev, curr))


class BacktestEngineTests(unittest.TestCase):
    def test_entry_is_next_open_not_signal_close(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),  # jam sell
            bar(2, 97.5, 98.0, 80.0, 81.0),  # entry + hits 2R target (85.5)
        ]
        self.assertEqual(detect_signals(bars), [1])
        result, trades = run_backtest(bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=24)
        self.assertEqual(result.trades, 1)
        self.assertAlmostEqual(trades[0].entry, 97.5)
        self.assertEqual(trades[0].reason, "target")
        self.assertGreater(trades[0].pnl, 0)

    def test_stop_hits(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),
            bar(2, 97.5, 110.0, 97.0, 109.0),
        ]
        _, trades = run_backtest(bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=24)
        self.assertEqual(trades[0].reason, "stop")
        self.assertLess(trades[0].pnl, 0)

    def test_same_bar_sl_and_tp_uses_stop(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),
            bar(2, 97.5, 110.0, 80.0, 90.0),
        ]
        _, trades = run_backtest(bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=24)
        self.assertEqual(trades[0].reason, "sl_before_tp")
        self.assertLess(trades[0].pnl, 0)

    def test_skips_while_in_trade(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),
            bar(2, 97.5, 98.2, 97.0, 97.2),
            bar(3, 97.2, 99.0, 90.0, 91.0),  # would also be jam vs bar 2
            bar(4, 91.0, 91.5, 80.0, 81.0),
        ]
        result, trades = run_backtest(bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=24)
        self.assertGreaterEqual(result.signals, 1)
        self.assertEqual(len(trades), 1)

    def test_time_stop_exits_at_last_close(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),
            bar(2, 97.5, 98.0, 97.0, 97.2),
            bar(3, 97.2, 98.0, 97.0, 97.1),
        ]
        _, trades = run_backtest(bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=2)
        self.assertEqual(trades[0].reason, "time")
        self.assertEqual(trades[0].bars_held, 2)

    def test_buy_jam_hits_target(self) -> None:
        bars = [
            bar(0, 101, 102, 99, 100),
            bar(1, 100.5, 104, 98, 103),  # jam buy
            bar(2, 103.2, 120.0, 103.0, 119.0),
        ]
        result, trades = run_backtest(
            bars, rr=2.0, spread=0.0, sl_buffer=0.5, max_hold=24, side="buy"
        )
        self.assertEqual(result.trades, 1)
        self.assertEqual(trades[0].side, "buy")
        self.assertEqual(trades[0].reason, "target")
        self.assertGreater(trades[0].pnl, 0)

    def test_session_filter_drops_asia_signal(self) -> None:
        bars = [
            bar(0, 100, 102, 99, 101),
            bar(1, 101.5, 103, 97, 98),
            bar(2, 97.5, 98.0, 80.0, 81.0),
        ]
        # bar(1) is 01:00 UTC — asia, not london
        result, _ = run_backtest(
            bars, rr=2.0, spread=0.0, sl_buffer=0.5, side="sell", session="london"
        )
        self.assertEqual(result.trades, 0)


class BankSizingTests(unittest.TestCase):
    def test_one_percent_on_500(self) -> None:
        t = Trade(
            side="sell",
            signal_time="t",
            entry_time="t",
            exit_time="t",
            entry=100.0,
            stop=110.0,
            target=70.0,
            exit=110.0,
            bars_held=1,
            reason="stop",
            pnl=-10.0,
            r_multiple=-1.0,
            signal_high=110.0,
            prev_high=109.0,
            prev_low=99.0,
            signal_close=100.0,
        )
        r = simulate([t], start=500.0, name="t", notes="", oz_fn=oz_pct(0.01))
        self.assertAlmostEqual(r.path[0].oz, 0.5)
        self.assertAlmostEqual(r.path[0].pnl, -5.0)
        self.assertAlmostEqual(r.end, 495.0)

    def test_fixed_one_oz(self) -> None:
        t = Trade(
            side="sell",
            signal_time="t",
            entry_time="t",
            exit_time="t",
            entry=100.0,
            stop=110.0,
            target=70.0,
            exit=70.0,
            bars_held=1,
            reason="target",
            pnl=30.0,
            r_multiple=3.0,
            signal_high=110.0,
            prev_high=109.0,
            prev_low=99.0,
            signal_close=100.0,
        )
        r = simulate([t], start=500.0, name="t", notes="", oz_fn=oz_fixed(1.0))
        self.assertAlmostEqual(r.net, 30.0)
        self.assertAlmostEqual(r.end, 530.0)

    def test_leverage_min_lot_and_blow(self) -> None:
        t = Trade(
            side="sell",
            signal_time="t",
            entry_time="t",
            exit_time="t",
            entry=4300.0,
            stop=4400.0,
            target=4000.0,
            exit=4400.0,
            bars_held=1,
            reason="stop",
            pnl=-100.0,
            r_multiple=-1.0,
            signal_high=4400.0,
            prev_high=4390.0,
            prev_low=4290.0,
            signal_close=4300.0,
        )
        ok = run_lev([t], start=500.0, leverage=500, risk_pct=0.01)
        self.assertFalse(ok.blown)
        boom = run_fixed_lot([t], start=500.0, leverage=500, lots=0.10)
        self.assertTrue(boom.blown or boom.end <= 0)


if __name__ == "__main__":
    unittest.main()
