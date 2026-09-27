"""Offline: 10% each, take every name, do not wait."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau_jam.auto import ibkr_paper_ready, looks_like_live
from xau_jam.combine import (
    Shot,
    collect_signals,
    first_cap_hit,
    max_drawdown,
    min_start_bank,
    monthly_rows,
    plan_stake,
    replay_one,
    run_replay,
    state_path,
    watch_book,
)
from xau_jam.pattern import Bar


def _day(sym_hour: int, o: float, hi: float, lo: float, c: float, day: int = 0) -> list[Bar]:
    start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc) + timedelta(days=day)
    out = []
    for i in range(8):
        if i == 0:
            out.append(Bar(start, o, hi, lo, c, 1.0))
        else:
            px = c
            out.append(Bar(start + timedelta(hours=i), px, px + 0.2, px - 0.1, px, 1.0))
    return out


class CombineTests(unittest.TestCase):
    def test_ibkr_stays_off_without_keys(self) -> None:
        self.assertFalse(ibkr_paper_ready())
        self.assertFalse(looks_like_live())

    def test_ibkr_du_account_is_paper_shaped(self) -> None:
        with patch.dict("os.environ", {"IBKR_PAPER_ACCOUNT": "DUR999999"}, clear=False):
            self.assertFalse(looks_like_live())
        with patch.dict("os.environ", {"IBKR_PAPER_ACCOUNT": "U216113"}, clear=False):
            self.assertTrue(looks_like_live())

    def test_venues_keep_separate_state(self) -> None:
        self.assertIn("combine_state_yahoo_350.json", str(state_path(350, "yahoo")))
        self.assertIn("combine_state_ibkr_350.json", str(state_path(350, "ibkr")))
        self.assertNotEqual(state_path(350, "yahoo"), state_path(350, "ibkr"))

    def test_locked_plan_stake(self) -> None:
        self.assertEqual(plan_stake(350.0, start=350.0), 70.0)
        self.assertEqual(plan_stake(20_000.0, start=350.0), 2500.0)
        self.assertEqual(plan_stake(25_000.0, start=350.0), 2500.0)
        self.assertEqual(plan_stake(80_000.0, start=350.0, reached=True), 2500.0)

    def test_same_open_takes_both(self) -> None:
        mstr = _day(14, 100.0, 101.0, 99.8, 100.5)
        tsla = _day(14, 200.0, 201.0, 199.8, 200.4)
        books = {"MSTR": (mstr, 0.006), "TSLA": (tsla, 0.003)}
        shots = collect_signals(books, datetime(2026, 1, 1).date(), None)
        eq, fills = replay_one(shots, 500.0, 10)
        self.assertEqual([f.symbol for f in fills], ["MSTR", "TSLA"])
        self.assertEqual(fills[0].stake, 50.0)
        self.assertLess(fills[0].shares, 20)
        self.assertGreater(eq, 0)

    def test_next_day_takes_other_too(self) -> None:
        mstr = _day(14, 100.0, 101.0, 99.8, 100.5, day=0)
        tsla = _day(14, 200.0, 201.2, 199.9, 200.8, day=1)
        books = {"MSTR": (mstr, 0.006), "TSLA": (tsla, 0.003)}
        shots = collect_signals(books, datetime(2026, 1, 1).date(), None)
        _, fills = replay_one(shots, 500.0, 10)
        self.assertEqual([f.symbol for f in fills], ["MSTR", "TSLA"])

    def test_clip_caps_loss_at_stake(self) -> None:
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        bars = [Bar(start, 10.0, 10.2, 9.95, 10.1, 1.0)]
        for i in range(1, 8):
            bars.append(Bar(start + timedelta(hours=i), 1.0, 1.1, 0.9, 1.0, 1.0))
        shots = collect_signals({"MSTR": (bars, 0.006)}, datetime(2026, 1, 1).date(), None)
        eq, fills = replay_one(shots, 500.0, 10)
        self.assertEqual(fills[0].event, "clip")
        self.assertGreaterEqual(eq, 450.0)
        self.assertEqual(fills[0].cash, -50.0)

    def test_compound_grows_lot_after_win(self) -> None:
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)

        def boom(day: int, end: float) -> list[Bar]:
            t0 = start + timedelta(days=day)
            out = [Bar(t0, 100.0, 120.0, 99.8, 119.0, 1.0)]
            for i in range(1, 8):
                out.append(Bar(t0 + timedelta(hours=i), end, end + 0.2, end - 0.1, end, 1.0))
            return out

        h1 = boom(0, 119.0) + boom(3, 119.0)
        shots = collect_signals({"MSTR": (h1, 0.006)}, datetime(2026, 1, 1).date(), None)
        _s_eq, simple = replay_one(shots, 500.0, 10, simple=True)
        _c_eq, compound = replay_one(shots, 500.0, 10, simple=False)
        self.assertEqual(simple[0].shares, simple[1].shares)
        self.assertGreater(compound[1].shares, compound[0].shares)

    def test_max_stake_caps_compound_lot(self) -> None:
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)

        def boom(day: int, end: float) -> list[Bar]:
            t0 = start + timedelta(days=day)
            out = [Bar(t0, 100.0, 120.0, 99.8, 119.0, 1.0)]
            for i in range(1, 8):
                out.append(Bar(t0 + timedelta(hours=i), end, end + 0.2, end - 0.1, end, 1.0))
            return out

        h1 = boom(0, 119.0) + boom(3, 119.0)
        shots = collect_signals({"MSTR": (h1, 0.006)}, datetime(2026, 1, 1).date(), None)
        _, capped = replay_one(shots, 500.0, 10, simple=False, max_stake=50.0)
        self.assertEqual(capped[0].stake, 50.0)
        self.assertEqual(capped[1].stake, 50.0)

    def test_default_cap_kicks_in_at_2500(self) -> None:
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        t0 = start
        out = [Bar(t0, 100.0, 120.0, 99.8, 119.0, 1.0)]
        for i in range(1, 8):
            out.append(Bar(t0 + timedelta(hours=i), 119.0, 119.2, 118.9, 119.0, 1.0))
        shots = collect_signals({"MSTR": (out, 0.006)}, datetime(2026, 1, 1).date(), None)
        _, fills = replay_one(shots, 20_000.0, 10, risk=0.20, simple=False)
        self.assertEqual(fills[0].stake, 2500.0)
        self.assertLess(fills[0].shares, int(20_000.0 * 0.20 * 10 / 100.0))
        self.assertIsNotNone(first_cap_hit(fills, 2500.0))

    def test_target_bank_then_monthly_withdraw(self) -> None:
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)

        def boom(day: int) -> list[Bar]:
            t0 = start + timedelta(days=day)
            out = [Bar(t0, 100.0, 120.0, 99.8, 119.0, 1.0)]
            for i in range(1, 8):
                out.append(Bar(t0 + timedelta(hours=i), 119.0, 119.2, 118.9, 119.0, 1.0))
            return out

        h1 = boom(0) + boom(32)
        shots = collect_signals({"MSTR": (h1, 0.006)}, datetime(2026, 1, 1).date(), None)
        got = run_replay(
            shots,
            23_000.0,
            10,
            risk=0.20,
            simple=False,
            max_stake=2500.0,
            target_bank=25_000.0,
            run_risk=0.10,
            withdraw=True,
        )
        self.assertIsNotNone(got.target_hit)
        self.assertGreaterEqual(got.target_hit.equity, 25_000.0)
        self.assertEqual(got.end, 25_000.0)
        self.assertGreater(got.withdrawals[0]["took"], 0.0)
        rows = monthly_rows(got.fills, 23_000.0, got.withdrawals, 25_000.0)
        self.assertEqual(rows[-1]["end"], 25_000.0)
        self.assertGreater(rows[-1]["took"], 0.0)

    def test_350_at_20pct_covers_amd_price(self) -> None:
        h1 = _day(14, 630.0, 640.0, 629.0, 630.63)
        need, last = min_start_bank({"AMD": (h1, 0.006)}, risk=0.20, lev=10)
        self.assertLessEqual(need, 350.0)
        self.assertGreater(last["AMD"], 630.0)

    def test_max_drawdown_from_peak(self) -> None:
        fills = [
            Shot("2026-01-10T14:30:00+00:00", "MSTR", "buy", 1, 1, 2, 50.0, 400.0, "ok", 70.0),
            Shot("2026-01-11T14:30:00+00:00", "COIN", "sell", 1, 1, 2, -120.0, 280.0, "ok", 70.0),
            Shot("2026-01-12T14:30:00+00:00", "AMD", "buy", 1, 1, 2, 20.0, 300.0, "ok", 70.0),
        ]
        dd = max_drawdown(fills, 350.0)
        self.assertEqual(dd["dd"], 30.0)
        self.assertEqual(dd["peak"], 400.0)
        self.assertEqual(dd["trough"], 280.0)

    def test_monthly_splits_jan_feb(self) -> None:
        fills = [
            Shot("2026-01-10T14:30:00+00:00", "MSTR", "buy", 1, 1, 2, 10.0, 640.0, "ok", 63.0),
            Shot("2026-02-03T14:30:00+00:00", "COIN", "sell", 1, 1, 2, -5.0, 635.0, "ok", 63.0),
        ]
        rows = monthly_rows(fills, 630.0)
        self.assertEqual([r["month"] for r in rows], ["2026-01", "2026-02"])
        self.assertEqual(rows[0]["n"], 1)
        self.assertEqual(rows[0]["pnl"], 10.0)
        self.assertEqual(rows[1]["pnl"], -5.0)

    def test_watch_book_opens_every_name(self) -> None:
        mstr = _day(14, 100.0, 101.0, 99.8, 100.5)
        tsla = _day(14, 200.0, 201.0, 199.8, 200.4)
        books = {"MSTR": (mstr, 0.006), "TSLA": (tsla, 0.003)}
        state = {
            "start": 500.0,
            "equity": 500.0,
            "simple": True,
            "risk": 0.1,
            "pos": None,
            "positions": [],
            "fills": [],
        }
        state = watch_book(books, state, 10)
        self.assertEqual({p["symbol"] for p in state["positions"]}, {"MSTR", "TSLA"})
        self.assertFalse(state.get("simple"))
        state = watch_book(books, state, 10)
        self.assertEqual(state["positions"], [])
        self.assertEqual({f["symbol"] for f in state["fills"]}, {"MSTR", "TSLA"})
