"""Hourly honest TSLA paper bot. Not a broker.

Same book as the 6/9m paper: skip both-wick, spread/slip/commission, whole shares.

  python3 -m xau_jam.auto                # $500, один тик и дальше каждый час
  python3 -m xau_jam.auto --once         # только сейчас
  python3 -m xau_jam.auto --bank 100
"""
from __future__ import annotations

import argparse

from xau_jam.burst_open import REPORTS
from xau_jam.paper import cron_line, install_cron, run_loop, run_watch_once


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--interval", type=int, default=3600)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    cron = install_cron(args.bank)
    print(
        f"автомат честный TSLA 0.3% / 6 H1 / 1:{args.leverage} / ${args.bank:.0f}"
    )
    print("cron:" if cron else "cron нет, loop:", cron_line(args.bank))
    if args.once:
        state = run_watch_once(args.bank, args.leverage)
        print(state.get("note", ""), f"eq=${state['equity']:.2f}")
        return 0
    run_loop(args.bank, args.leverage, args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
