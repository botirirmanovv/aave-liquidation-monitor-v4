"""Locked London impulse strategy + real stack margin / 10x size.

Rules are fixed (not a new grid). Clips that flatten together are one stack:
margin is n_clips * lots, not one clip at a time.

  python3 -m xau_jam.burst_10x --bank 500
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

from xau_jam.backtest import Trade
from xau_jam.burst_open import BurstParams, REPORTS, run_burst
from xau_jam.data import fetch_yahoo
from xau_jam.leverage import OZ_PER_LOT

# 0.01 lot book × 10. Same rules, ten times the clip.
BASE_LOTS = 0.01
TEN_X_LOTS = 0.10


def live_params(interval: str) -> BurstParams:
    """London straddle. Hunt ~8 minutes. Out in seconds/one bar."""
    if interval == "1m":
        orb, hold = 8, 1
    else:
        orb, hold = 2, 0  # 5m: hunt 10m, flatten same bar
    return BurstParams(
        session="london",
        model="straddle",
        trigger=2.5,
        layers=8,
        step=0.5,
        orb_bars=orb,
        hold_bars=hold,
        spread=0.20,
        fail_through_open=False,
        flatten="close",
        interval=interval,
    )


def group_stacks(trades: list[Trade]) -> list[list[Trade]]:
    stacks: list[list[Trade]] = []
    cur: list[Trade] = []
    for t in trades:
        if not cur or (t.exit_time == cur[0].exit_time and t.side == cur[0].side):
            cur.append(t)
        else:
            stacks.append(cur)
            cur = [t]
    if cur:
        stacks.append(cur)
    return stacks


def _floor_lot(lots: float) -> float:
    return max(0.0, int(round(lots / 0.01)) * 0.01)


@dataclass(slots=True)
class StackFill:
    time: str
    side: str
    clips: int
    lots: float
    margin: float
    pnl: float
    equity: float
    event: str


@dataclass(slots=True)
class BookResult:
    name: str
    start: float
    end: float
    net: float
    max_dd: float
    blown: bool
    hit_10x: bool
    stacks: int
    clips: int
    skipped: int
    leverage: int
    notes: str
    path: list[StackFill]


def run_book(
    trades: list[Trade],
    *,
    start: float,
    leverage: int,
    lots: float | None,
    margin_frac: float = 0.40,
    stopout: float = 0.50,
    daily_kill: float = 0.20,
    target_mult: float = 10.0,
    name: str = "",
) -> BookResult:
    """Size the whole stack at once. lots=None → clip size from free margin."""
    eq = start
    peak = start
    max_dd = 0.0
    blown = False
    hit_10x = False
    skipped = 0
    taken_clips = 0
    taken_stacks = 0
    path: list[StackFill] = []
    day_pnl = 0.0
    day = ""

    for stack in group_stacks(trades):
        d = stack[0].entry_time[:10]
        if d != day:
            day = d
            day_pnl = 0.0
        if blown or eq <= 0:
            path.append(StackFill(stack[0].entry_time, stack[0].side, 0, 0.0, 0.0, 0.0, eq, "dead"))
            continue
        if eq >= start * target_mult:
            hit_10x = True
            path.append(
                StackFill(stack[0].entry_time, stack[0].side, 0, 0.0, 0.0, 0.0, eq, "target")
            )
            continue
        if start > 0 and day_pnl <= -start * daily_kill:
            skipped += 1
            path.append(
                StackFill(stack[0].entry_time, stack[0].side, 0, 0.0, 0.0, 0.0, eq, "daily_kill")
            )
            continue

        n = len(stack)
        price = max(stack[0].entry, 1.0)
        cap = eq * margin_frac * leverage / (n * price * OZ_PER_LOT)
        use = lots if lots is not None else cap
        use = _floor_lot(min(use, cap))
        if use < 0.01:
            skipped += 1
            path.append(
                StackFill(stack[0].entry_time, stack[0].side, n, 0.0, 0.0, 0.0, eq, "no_margin")
            )
            continue

        oz = use * OZ_PER_LOT
        margin = n * oz * price / leverage
        pnl = sum(t.pnl * oz for t in stack)
        eq += pnl
        day_pnl += pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        taken_stacks += 1
        taken_clips += n
        event = "ok"
        if eq <= 0:
            eq = 0.0
            blown = True
            event = "blown"
        elif margin > 0 and eq / margin < stopout:
            eq = 0.0
            blown = True
            event = "stopout"
        if eq >= start * target_mult:
            hit_10x = True
            event = "hit_10x" if event == "ok" else event
        path.append(
            StackFill(
                stack[0].entry_time,
                stack[0].side,
                n,
                round(use, 2),
                round(margin, 2),
                round(pnl, 2),
                round(eq, 2),
                event,
            )
        )

    return BookResult(
        name=name,
        start=start,
        end=round(eq, 2),
        net=round(eq - start, 2),
        max_dd=round(max_dd, 2),
        blown=blown,
        hit_10x=hit_10x,
        stacks=taken_stacks,
        clips=taken_clips,
        skipped=skipped,
        leverage=leverage,
        notes=(
            f"{name}: 1:{leverage} lots={lots if lots is not None else 'compound'} "
            f"margin≤{margin_frac:.0%} stack, daily kill {daily_kill:.0%}"
        ),
        path=path,
    )


def _fmt_book(b: BookResult) -> list[str]:
    tag = "СГОРЕЛ" if b.blown else ("10x" if b.hit_10x else "жив")
    lines = [
        f"── {b.name} [{tag}] ──",
        f"{b.notes}",
        f"${b.start:.0f} → ${b.end:.2f}  net {b.net:+.2f}  DD ${b.max_dd:.2f}  "
        f"stacks={b.stacks} clips={b.clips} skip={b.skipped}",
    ]
    for i, s in enumerate(b.path, 1):
        lines.append(
            f"  {i:02d} {s.time} {s.side:4} n={s.clips} {s.lots:.2f}lot  "
            f"маржа=${s.margin:.0f}  PnL={s.pnl:+.2f}  eq=${s.equity:.2f}  {s.event}"
        )
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    args = ap.parse_args()

    jobs = (("1m", "7d"), ("5m", "1mo"))
    lines = [
        "Burst Impulse — зафиксированные правила, пачка как одна позиция",
        "Лондон 07:00 UTC. Стопы ±$2.5, клипы каждые $0.50 до 8.",
        "Выход: 1m = следующая минута; 5m = close того же 5m бара.",
        "10x размер = 0.10 лота на клип (0.01×10). compound = лот от 40% маржи.",
        "Цель 10x банка. Стоп-аут equity/маржа < 50%. Дневной стоп −20% от старта.",
        "",
    ]
    dumped: dict = {"rules": asdict(live_params("1m")), "books": []}

    for interval, rng in jobs:
        p = live_params(interval)
        bars = fetch_yahoo("GC=F", rng, interval)
        r, trades = run_burst(bars, p)
        lines.append(
            f"данные {interval} {rng}: {bars[0].time} → {bars[-1].time} "
            f"баров={len(bars)} клипов={r.clips} net/oz={r.net_pnl:+.2f}"
        )
        lines.append("")
        books = [
            run_book(
                trades,
                start=args.bank,
                leverage=500,
                lots=BASE_LOTS,
                name=f"{interval} 0.01 1:500",
            ),
            run_book(
                trades,
                start=args.bank,
                leverage=500,
                lots=TEN_X_LOTS,
                name=f"{interval} 10x=0.10 1:500",
            ),
            run_book(
                trades,
                start=args.bank,
                leverage=1000,
                lots=TEN_X_LOTS,
                name=f"{interval} 10x=0.10 1:1000",
            ),
            run_book(
                trades,
                start=args.bank,
                leverage=1000,
                lots=None,
                name=f"{interval} compound 40% 1:1000",
            ),
        ]
        for b in books:
            lines.extend(_fmt_book(b))
            lines.append("")
            dumped["books"].append(asdict(b) | {"interval": interval, "range": rng})

    lines.append(
        "10x за неделю с $500 на этом импульсе не получается честно: "
        "на унцию пачка даёт десятки долларов, не тысячи. "
        "0.10 лота — это 10x к 0.01, не 10x к банку. "
        "compound на 1:1000 жрёт маржу пачки из 8 клипов и сгорает на фейке."
    )
    text = "\n".join(lines) + "\n"
    out = REPORTS
    out.mkdir(parents=True, exist_ok=True)
    (out / "burst_10x.txt").write_text(text, encoding="utf-8")
    (out / "burst_10x.json").write_text(json.dumps(dumped, indent=2) + "\n", encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
