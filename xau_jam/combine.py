"""One paper book for the whole impulse family. One bank, one position.

Same costs as paper.py. Simple % (lot always from start). Not live.

  python3 -m xau_jam.combine --from 01/01/26 --bank 500
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime

from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_yahoo
from xau_jam.paper import HOLD, costed_cash, day_groups, parse_day, replay, signal_for_day
from xau_jam.pattern import Bar

BOOK = (
    ("MSTR", 0.006),
    ("COIN", 0.006),
    ("SMCI", 0.006),
    ("AMD", 0.006),
    ("UVXY", 0.006),
    ("PLTR", 0.006),
    ("TSLA", 0.003),
)


@dataclass(slots=True)
class Shot:
    time: str
    symbol: str
    side: str
    shares: int
    entry: float
    exit: float
    cash: float
    equity: float
    event: str


def collect_signals(
    books: dict[str, tuple[list[Bar], float]],
    begin: date,
    end: date | None,
) -> list[tuple[datetime, str, str, float, int, list[Bar]]]:
    shots: list[tuple[datetime, str, str, float, int, list[Bar]]] = []
    for sym, (h1, trig) in books.items():
        by = day_groups(h1)
        for day, idxs in by.items():
            if day < begin:
                continue
            if end is not None and day >= end:
                continue
            sig = signal_for_day(h1, idxs, trigger=trig)
            if sig is None:
                continue
            side, fi, fill = sig
            shots.append((h1[fi].time, sym, side, fill, fi, h1))
    rank = {sym: i for i, (sym, _) in enumerate(BOOK)}
    shots.sort(key=lambda s: (s[0], rank.get(s[1], 99)))
    return shots


def replay_one(
    shots: list[tuple[datetime, str, str, float, int, list[Bar]]],
    start: float,
    lev: int,
    hold: int = HOLD,
) -> tuple[float, list[Shot]]:
    eq = start
    path: list[Shot] = []
    free_at: datetime | None = None
    for t, sym, side, fill, fi, h1 in shots:
        if eq <= 0:
            break
        if free_at is not None and t < free_at:
            continue
        shares = int(start * lev / max(fill, 1e-9))
        if shares < 1:
            continue
        ex_i = min(len(h1) - 1, fi + hold)
        exit_px = h1[ex_i].close
        cash = costed_cash(side, fill, exit_px, shares)
        eq += cash
        event = "ok"
        if eq <= 0:
            eq = 0.0
            event = "blown"
        path.append(
            Shot(
                t.isoformat(),
                sym,
                side,
                shares,
                round(fill, 4),
                round(exit_px, 4),
                round(cash, 2),
                round(eq, 2),
                event,
            )
        )
        free_at = h1[ex_i].time
        if event == "blown":
            break
    return round(eq, 2), path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--from", dest="date_from", default="01/01/26")
    args = ap.parse_args()
    begin = parse_day(args.date_from)
    REPORTS.mkdir(parents=True, exist_ok=True)
    books: dict[str, tuple[list[Bar], float]] = {}
    singles: list[tuple[str, float, float, int]] = []
    print(f"combine simple ${args.bank:.0f} 1:{args.leverage} from {begin}", flush=True)
    last = begin
    for sym, trig in BOOK:
        try:
            h1 = fetch_yahoo(sym, "1y", "60m")
        except Exception as exc:
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        books[sym] = (h1, trig)
        last = max(last, h1[-1].time.date())
        eq, path = replay(
            h1,
            args.bank,
            args.leverage,
            compound=False,
            begin=begin,
            trigger=trig,
            hold=HOLD,
        )
        taken = [p for p in path if p.event == "ok"]
        pct = 100.0 * (eq - args.bank) / args.bank
        singles.append((sym, eq, pct, len(taken)))
        print(f"  solo {sym} {trig:.1%} ${eq:.0f} ({pct:+.0f}%) n={len(taken)}", flush=True)

    shots = collect_signals(books, begin, None)
    end, fills = replay_one(shots, args.bank, args.leverage)
    pct = 100.0 * (end - args.bank) / args.bank
    by_sym: dict[str, int] = {}
    for f in fills:
        by_sym[f.symbol] = by_sym.get(f.symbol, 0) + 1
    months = max((last - begin).days / 30.0, 1.0)
    lines = [
        f"Один бот, один банк ${args.bank:.0f}, 1:{args.leverage}, простой %, с {begin} → {last}.",
        "Семья импульса: MSTR/COIN/SMCI/AMD/UVXY/PLTR 0.6%, TSLA 0.3%, hold 6 H1.",
        "Одна позиция. Если в один час несколько — берём по списку (жирные первые).",
        "Пока держим 6 часов, остальные сигналы пропускаем. Те же спред/комиссия.",
        f"вместе ${args.bank:.0f} → ${end:.2f}  ({pct:+.1f}%, {pct / months:+.1f}%/мес)  сделок={len(fills)}",
        "по бумагам: " + " ".join(f"{k}={v}" for k, v in sorted(by_sym.items(), key=lambda kv: -kv[1])),
        "",
        "те же бумаги по одной (для сравнения, каждая со своим $500):",
    ]
    for sym, eq, sp, n in sorted(singles, key=lambda r: -r[2]):
        lines.append(f"  {sym:6} ${eq:8.0f}  {sp:+7.1f}%  n={n}")
    best = max(singles, key=lambda r: r[2]) if singles else None
    if best:
        lines.append("")
        lines.append(
            f"лучший соло: {best[0]} ${best[1]:.0f}. вместе "
            f"{'слабее' if end < best[1] else 'сильнее или как'} соло-лидера "
            f"(один счёт не может взять два импульса в один день)."
        )
    lines.append("")
    for i, f in enumerate(fills, 1):
        lines.append(
            f"  {i:02d} {f.time} {f.symbol:5} {f.side:4} {f.shares}шт  "
            f"{f.entry:.2f}→{f.exit:.2f}  {f.cash:+.2f}  eq=${f.equity:.2f}"
        )
    text = "\n".join(lines) + "\n"
    (REPORTS / "combine.txt").write_text(text, encoding="utf-8")
    (REPORTS / "combine.json").write_text(
        json.dumps(
            {
                "end": end,
                "pct": round(pct, 1),
                "n": len(fills),
                "by_sym": by_sym,
                "singles": [{"symbol": s, "end": e, "pct": p, "n": n} for s, e, p, n in singles],
                "fills": [asdict(f) for f in fills],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
