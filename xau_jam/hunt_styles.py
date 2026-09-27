"""Other mechanics vs impulse, same honest simple book.

  python3 -m xau_jam.hunt_styles --from 01/01/26 --bank 500
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date, timedelta

from xau_jam.any_shot import SYMBOLS
from xau_jam.auto_best import _gap_trades, _jam_trades
from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_yahoo
from xau_jam.hunt import EXTRA, Row
from xau_jam.paper import costed_cash, parse_day, replay


def _from_trades(trades, start: float, lev: int, begin: date, end: date, months: float) -> Row | None:
    eq = start
    n = wins = 0
    for t in trades:
        day = date.fromisoformat(t.entry_time[:10])
        if day < begin or day >= end:
            continue
        shares = int(start * lev / max(t.entry, 1e-9))
        if shares < 1:
            continue
        cash = costed_cash(t.side, t.entry, t.exit, shares)
        eq += cash
        n += 1
        if cash > 0:
            wins += 1
    if n < 8:
        return None
    pct = 100.0 * (eq - start) / start
    return Row("", "", "", round(eq, 2), round(pct, 1), round(pct / months, 1), n, wins)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--from", dest="date_from", default="01/01/26")
    args = ap.parse_args()
    begin = parse_day(args.date_from)
    REPORTS.mkdir(parents=True, exist_ok=True)
    rows: list[Row] = []
    for sym in list(dict.fromkeys([*SYMBOLS, *EXTRA])):
        try:
            h1 = fetch_yahoo(sym, "1y", "60m")
            d1 = fetch_yahoo(sym, "1y", "1d")
        except Exception as exc:
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        last = h1[-1].time.date()
        finish = last + timedelta(days=1)
        months = max((last - begin).days / 30.0, 1.0)
        print(f"  {sym} {h1[0].time.date()}→{last}", flush=True)
        for trig, hold in ((0.003, 6), (0.006, 6)):
            got = replay(h1, args.bank, args.leverage, compound=False, begin=begin, end=finish, trigger=trig, hold=hold)
            eq, path = got
            taken = [p for p in path if p.event == "ok"]
            if len(taken) < 8:
                continue
            pct = 100.0 * (eq - args.bank) / args.bank
            rows.append(
                Row(sym, f"impulse {trig:.1%} hold={hold}", "from", eq, round(pct, 1), round(pct / months, 1), len(taken), sum(1 for p in taken if p.cash > 0))
            )
        for rr in (2.0, 3.0):
            got = _from_trades(_jam_trades(sym, h1, rr), args.bank, args.leverage, begin, finish, months)
            if got:
                got.symbol, got.style, got.window = sym, f"jam rr={rr:g}", "from"
                rows.append(got)
        for gp in (0.02, 0.03):
            got = _from_trades(_gap_trades(d1, gp), args.bank, args.leverage, begin, finish, months)
            if got:
                got.symbol, got.style, got.window = sym, f"gap {gp:.0%}", "from"
                rows.append(got)

    rows.sort(key=lambda r: r.pct, reverse=True)
    lines = [
        f"Другие механики, простой %, $500, 1:10, с {begin} → сейчас.",
        "impulse = наша. jam / gap = другие стратегии.",
        "",
        f"{'pct':>8} {'pm':>7} {'end':>9} n   wr  symbol     style",
    ]
    for r in rows[:25]:
        wr = 100.0 * r.wins / r.n
        lines.append(
            f"{r.pct:8.1f} {r.per_month:7.1f} {r.end:9.0f} {r.n:2d} {wr:3.0f}%  {r.symbol:10} {r.style}"
        )
    fat = [r for r in rows if r.pct >= 150 and not r.style.startswith("impulse")]
    lines.append("")
    lines.append(f"не-импульс с ≥+150% за окно: {len(fat)}")
    for r in fat[:15]:
        lines.append(f"  {r.symbol} {r.style}  {r.pct:+.1f}%  n={r.n}  ${r.end:.0f}")
    text = "\n".join(lines) + "\n"
    (REPORTS / "hunt_styles.txt").write_text(text, encoding="utf-8")
    (REPORTS / "hunt_styles.json").write_text(
        json.dumps([asdict(r) for r in rows[:40]], indent=2) + "\n", encoding="utf-8"
    )
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
