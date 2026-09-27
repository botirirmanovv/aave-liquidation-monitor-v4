"""One shared-bank paper bot for the impulse family. Not five bots.

  python3 -m xau_jam.auto                # $500, один тик и дальше каждый час
  python3 -m xau_jam.auto --once         # только сейчас
  python3 -m xau_jam.auto --bank 500
"""
from __future__ import annotations

import argparse

from xau_jam.burst_open import REPORTS
from xau_jam.combine import BOOK, MAX_STAKE, START_BANK, START_RISK, run_loop, run_watch_once


def cron_line(bank: float) -> str:
    from pathlib import Path
    import sys

    repo = Path(__file__).resolve().parents[1]
    return (
        f"7 * * * * cd {repo} && {sys.executable} -m xau_jam.auto "
        f"--once --bank {bank:.0f} >> {REPORTS / 'combine_watch.log'} 2>&1"
    )


def install_cron(bank: float) -> str | None:
    import shutil
    import subprocess

    REPORTS.mkdir(parents=True, exist_ok=True)
    line = cron_line(bank)
    (REPORTS / "paper_cron.txt").write_text(line + "\n", encoding="utf-8")
    crontab = shutil.which("crontab")
    if not crontab:
        return None
    try:
        prev = subprocess.check_output([crontab, "-l"], text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        prev = ""
    mark = "xau_jam.auto --once"
    kept = [row for row in prev.splitlines() if mark not in row and "xau_jam.paper --watch" not in row]
    kept.append(line)
    subprocess.run([crontab, "-"], input="\n".join(kept) + "\n", check=True, text=True)
    return line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=START_BANK)
    ap.add_argument("--risk", type=float, default=START_RISK)
    ap.add_argument("--max-stake", type=float, default=MAX_STAKE)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--interval", type=int, default=3600)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    cron = install_cron(args.bank)
    names = ",".join(s for s, _ in BOOK)
    print(
        f"один банк ${args.bank:.0f} 1:{args.leverage}  ставка {100 * args.risk:.0f}%  "
        f"потолок ${args.max_stake:.0f}  все сделки  {names}"
    )
    print("cron:" if cron else "cron нет, loop:", cron_line(args.bank))
    if args.once:
        state = run_watch_once(args.bank, args.leverage, args.risk, args.max_stake)
        print(state.get("note", ""), f"eq=${state['equity']:.2f}")
        return 0
    run_loop(args.bank, args.leverage, args.interval, args.risk, args.max_stake)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
