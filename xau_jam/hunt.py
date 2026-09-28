"""Hunt other honest impulse books: simple %, skip both-wick, costs on.

  python3 -m xau_jam.hunt --bank 500 --ago 21
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

from xau_jam.any_shot import SYMBOLS
from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_yahoo
from xau_jam.paper import replay, shift_months

EXTRA = ("META", "AAPL", "AMZN", "SMCI", "BABA", "NFLX")


@dataclass(slots=True)
class Row:
    symbol: str
    style: str
    window: str
    end: float
    pct: float
    per_month: float
    n: int
    wins: int


def _book(bars, start: float, lev: int, begin, end, trigger: float, hold: int, months: float) -> Row | None:
    eq, path = replay(
        bars, start, lev, compound=False, begin=begin, end=end, trigger=trigger, hold=hold
    )
    taken = [p for p in path if p.event == "ok"]
    if len(taken) < 8:
        return None
    wins = sum(1 for p in taken if p.cash > 0)
    pct = 100.0 * (eq - start) / start
    return Row(
        symbol="",
        style="",
        window="",
        end=eq,
        pct=round(pct, 1),
        per_month=round(pct / months, 1),
        n=len(taken),
        wins=wins,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--ago", type=int, default=21)
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    symbols = list(dict.fromkeys([*SYMBOLS, *EXTRA]))
    rows: list[Row] = []
    print(f"hunt simple $ {args.bank:.0f} 1:{args.leverage} ago={args.ago}", flush=True)
    for sym in symbols:
        try:
            bars = fetch_yahoo(sym, "730d", "60m")
        except Exception as exc:
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        last = bars[-1].time.date()
        origin = shift_months(last, -args.ago)
        windows = [
            ("ago21-9m", origin, shift_months(origin, 9), 9.0),
            ("ago21-12m", origin, shift_months(origin, 12), 12.0),
            ("last-9m", shift_months(last, -9), last, 9.0),
        ]
        print(f"  {sym} bars={len(bars)} {bars[0].time.date()}→{last}", flush=True)
        for trig in (0.003, 0.004, 0.006):
            for hold in (6, 12):
                style = f"impulse {trig:.1%} hold={hold}"
                for name, begin, finish, months in windows:
                    got = _book(bars, args.bank, args.leverage, begin, finish, trig, hold, months)
                    if got is None:
                        continue
                    got.symbol = sym
                    got.style = style
                    got.window = name
                    rows.append(got)

    by_key: dict[tuple[str, str], dict[str, Row]] = {}
    for r in rows:
        by_key.setdefault((r.symbol, r.style), {})[r.window] = r

    ranked: list[tuple[float, str, str, dict[str, Row]]] = []
    for (sym, style), parts in by_key.items():
        if "ago21-9m" not in parts:
            continue
        ranked.append((parts["ago21-9m"].per_month, sym, style, parts))
    ranked.sort(key=lambda x: (x[0], min(p.per_month for p in x[3].values())), reverse=True)

    lines = [
        "Охота: честный импульс, простой %, спред/комиссия, 1:10, старт $500.",
        "Окна: 9м и 12м от −21 мес (дек 2024) + последние 9м. Both-wick пропуск.",
        f"живых книг={len(ranked)}",
        "",
        f"{'pm9':>7} {'pm12':>7} {'pmL9':>7}  {'end9':>8} n9  wr   symbol  style",
    ]
    top = []
    for pm9, sym, style, parts in ranked[:20]:
        a = parts["ago21-9m"]
        b = parts.get("ago21-12m")
        c = parts.get("last-9m")
        pm12 = f"{b.per_month:6.1f}" if b else "     -"
        pml = f"{c.per_month:6.1f}" if c else "     -"
        wr = 100.0 * a.wins / a.n
        lines.append(
            f"{a.per_month:7.1f} {pm12:>7} {pml:>7}  {a.end:8.0f} {a.n:2d} {wr:3.0f}%  {sym:10} {style}"
        )
        top.append(
            {
                "symbol": sym,
                "style": style,
                "ago21_9m": asdict(a),
                "ago21_12m": asdict(b) if b else None,
                "last_9m": asdict(c) if c else None,
            }
        )
    fat = [x for x in ranked if x[3]["ago21-9m"].per_month >= 80 and x[3].get("last-9m") and x[3]["last-9m"].per_month >= 80]
    lines.append("")
    lines.append(f"≥80%/мес и на старом 9м, и на последних 9м: {len(fat)}")
    for pm9, sym, style, parts in fat:
        lines.append(
            f"  {sym} {style}  ago21-9m {parts['ago21-9m'].per_month}%/мес  "
            f"last-9m {parts['last-9m'].per_month}%/мес"
        )
    text = "\n".join(lines) + "\n"
    (REPORTS / "hunt.txt").write_text(text, encoding="utf-8")
    (REPORTS / "hunt.json").write_text(json.dumps({"top": top, "fat": len(fat)}, indent=2) + "\n", encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
