"""Strategy A (combine BOOK) monthly for a calendar window.

  python3 -m xau_jam.a_year --from 01/01/26 --to 28/09/26 --bank 350
"""
from __future__ import annotations

import argparse
import time
from datetime import timedelta

from xau_jam.burst_open import REPORTS
from xau_jam.combine import (
    BOOK,
    MAX_STAKE,
    RUN_RISK,
    START_RISK,
    TARGET_BANK,
    collect_signals,
    monthly_rows,
    run_replay,
)
from xau_jam.data import fetch_yahoo
from xau_jam.paper import parse_day
from xau_jam.rule2_months import fill_months, fmt_table


def fetch_a(range_spec: str = "1y"):
    books = {}
    for i, (sym, trig) in enumerate(BOOK):
        try:
            h1 = fetch_yahoo(sym, range_spec, "60m")
            books[sym] = (h1, trig)
            print(f"  {sym} {h1[0].time.date()}→{h1[-1].time.date()} last={h1[-1].close:.2f}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {sym}: {exc}", flush=True)
        if i + 1 < len(BOOK):
            time.sleep(0.05)
    return books


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=350.0)
    ap.add_argument("--from", dest="date_from", default="01/01/26")
    ap.add_argument("--to", dest="date_to", default="28/09/26")
    ap.add_argument("--leverage", type=int, default=10)
    args = ap.parse_args()
    begin = parse_day(args.date_from)
    end = parse_day(args.date_to)
    until = end + timedelta(days=1)
    REPORTS.mkdir(parents=True, exist_ok=True)
    names = ", ".join(s for s, _ in BOOK)
    print(f"A {begin}→{end} {names}", flush=True)
    books = fetch_a("1y")
    if not books:
        print("нет баров")
        return 1
    shots = collect_signals(books, begin, until)
    simple = run_replay(
        shots,
        args.bank,
        args.leverage,
        risk=START_RISK,
        simple=True,
        max_stake=MAX_STAKE,
        target_bank=None,
        withdraw=False,
    )
    compound = run_replay(
        shots,
        args.bank,
        args.leverage,
        risk=START_RISK,
        simple=False,
        max_stake=MAX_STAKE,
        target_bank=None,
        withdraw=False,
    )
    plan = run_replay(
        shots,
        args.bank,
        args.leverage,
        risk=START_RISK,
        simple=False,
        max_stake=MAX_STAKE,
        target_bank=TARGET_BANK,
        run_risk=RUN_RISK,
        withdraw=True,
    )
    first_m, last_m = begin.strftime("%Y-%m"), end.strftime("%Y-%m")
    s_rows = fill_months(monthly_rows(simple.fills, args.bank), args.bank, first_m, last_m)
    c_rows = fill_months(monthly_rows(compound.fills, args.bank), args.bank, first_m, last_m)
    p_rows = fill_months(
        monthly_rows(plan.fills, args.bank, plan.withdrawals, TARGET_BANK),
        args.bank,
        first_m,
        last_m,
    )
    took = sum(float(w.get("took") or 0.0) for w in plan.withdrawals)
    by: dict[str, int] = {}
    for f in simple.fills:
        by[f.symbol] = by.get(f.symbol, 0) + 1
    lines = [
        "СТРАТЕГИЯ A — этот год (2026), помесячно.",
        f"MSTR COIN SMCI AMD UVXY PLTR 0.6%, TSLA 0.3%. HOLD=6. Банк ${args.bank:.0f}.",
        f"Окно {begin} … {end}. сделок (простой) {len(simple.fills)}.",
        "по бумагам: " + " ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])),
        "",
    ]
    lines.extend(fmt_table("── простой % (лот $70, не растёт) ──", s_rows, args.bank, simple.end, len(simple.fills)))
    lines.extend(
        fmt_table(
            "── сложный % (20% банка, потолок $2500, без снятия) ──",
            c_rows,
            args.bank,
            compound.end,
            len(compound.fills),
        )
    )
    lines.extend(
        [
            "── план A: 20%→$25k, потом $2500 и снимаем месяц ──",
            f"банк ${plan.end:.2f}  снял ${took:.2f}  всего ${plan.end + took:.2f}  сделок {len(plan.fills)}",
            f"{'мес':8} {'сд':>4} {'+':>4} {'pnl':>10} {'снял':>10} {'с':>10} {'по':>10} {'%мес':>8}",
        ]
    )
    for r in p_rows:
        lines.append(
            f"{r['month']:8} {r['n']:4} {r['wins']:4} {r['pnl']:+10.2f} {r['took']:+10.2f} "
            f"{r['start']:10.2f} {r['end']:10.2f} {r['pct_month']:+7.1f}%"
        )
    lines.append("")
    text = "\n".join(lines)
    (REPORTS / "A_2026.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
