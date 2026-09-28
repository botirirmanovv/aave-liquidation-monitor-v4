"""All gold recipes, rewritten as impulse. Catalog, not a locked book.

Maps jam / burst / open-scalp / oneshot / paper onto signal_for_day
(one-sided from session open, both wicks = skip). GC=F H1 Yahoo.

  python3 -m xau_jam.gold_impulse --from 01/01/26 --bank 350
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date

from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_yahoo
from xau_jam.open_scalp import SESSIONS, _session_opens
from xau_jam.paper import parse_day, signal_for_day
from xau_jam.pattern import Bar

SYMBOL = "GC=F"
GLD = "GLD"
TAX_OZ = 0.15
OZ_STEP = 0.01
LEV = 10
RISK = 0.20


@dataclass(slots=True)
class Recipe:
    name: str
    source: str
    session: str
    kind: str  # pct | usd
    trigger: float
    hold: int
    hunt: int


@dataclass(slots=True)
class Row:
    name: str
    source: str
    session: str
    trigger: str
    hold: int
    n: int
    wins: int
    wr: float
    usd_oz: float
    bank: float
    blown: bool


# Old gold modules → impulse knobs. Not optimized.
RECIPES: tuple[Recipe, ...] = (
    Recipe("paper-0.6-h6", "paper", "daily", "pct", 0.006, 6, 9),
    Recipe("paper-0.3-h6", "paper", "daily", "pct", 0.003, 6, 9),
    Recipe("paper-0.6-h12", "paper", "daily", "pct", 0.006, 12, 9),
    Recipe("jam-0.6-h24", "jam", "daily", "pct", 0.006, 24, 9),
    Recipe("jam-0.3-h24", "jam", "daily", "pct", 0.003, 24, 9),
    Recipe("burst-london-$2.5-h1", "burst", "london", "usd", 2.5, 1, 8),
    Recipe("burst-ny-$2.5-h1", "burst", "ny", "usd", 2.5, 1, 8),
    Recipe("burst-cme-$2.5-h1", "burst", "cme", "usd", 2.5, 1, 8),
    Recipe("burst-london-$6-h1", "burst", "london", "usd", 6.0, 1, 8),
    Recipe("burst-ny-$6-h1", "burst", "ny", "usd", 6.0, 1, 8),
    Recipe("scalp-ny-$4-h6", "open_scalp", "ny", "usd", 4.0, 6, 6),
    Recipe("scalp-london-$4-h6", "open_scalp", "london", "usd", 4.0, 6, 6),
    Recipe("scalp-cme-$4-h6", "open_scalp", "cme", "usd", 4.0, 6, 6),
    Recipe("oneshot-london-$6-h12", "oneshot", "london", "usd", 6.0, 12, 9),
    Recipe("oneshot-ny-$6-h12", "oneshot", "ny", "usd", 6.0, 12, 9),
    Recipe("oneshot-cme-$6-h12", "oneshot", "cme", "usd", 6.0, 12, 9),
    Recipe("ny-0.6-h6", "impulse", "ny", "pct", 0.006, 6, 9),
    Recipe("ny-0.3-h6", "impulse", "ny", "pct", 0.003, 6, 9),
    Recipe("london-0.6-h6", "impulse", "london", "pct", 0.006, 6, 9),
    Recipe("london-0.3-h6", "impulse", "london", "pct", 0.003, 6, 9),
    Recipe("cme-0.6-h6", "impulse", "cme", "pct", 0.006, 6, 9),
    Recipe("cme-0.3-h6", "impulse", "cme", "pct", 0.003, 6, 9),
)


def gold_cash(side: str, entry: float, exit_px: float, oz: float) -> float:
    if oz <= 0:
        return 0.0
    if side == "buy":
        return ((exit_px - TAX_OZ) - (entry + TAX_OZ)) * oz
    return ((entry - TAX_OZ) - (exit_px + TAX_OZ)) * oz


def oz_lot(stake: float, price: float, lev: int = LEV) -> float:
    raw = stake * lev / max(price, 1e-9)
    return round(math.floor(raw / OZ_STEP) * OZ_STEP, 2)


def trigger_for(bars: list[Bar], oi: int, recipe: Recipe) -> float:
    if recipe.kind == "pct":
        return recipe.trigger
    return recipe.trigger / max(bars[oi].open, 1e-9)


def collect(
    bars: list[Bar],
    recipe: Recipe,
    begin: date,
) -> list[tuple[int, str, float, int]]:
    tod = SESSIONS[recipe.session]
    opens = _session_opens(bars, tod)
    out: list[tuple[int, str, float, int]] = []
    for oi in opens:
        if bars[oi].time.date() < begin:
            continue
        last = min(len(bars) - 1, oi + max(recipe.hunt, 1) - 1)
        idxs = list(range(oi, last + 1))
        sig = signal_for_day(bars, idxs, trigger=trigger_for(bars, oi, recipe))
        if sig is None:
            continue
        side, fi, fill = sig
        out.append((fi, side, fill, oi))
    return out


def score(
    bars: list[Bar],
    recipe: Recipe,
    begin: date,
    bank: float,
) -> Row:
    shots = collect(bars, recipe, begin)
    usd = 0.0
    eq = bank
    wins = 0
    blown = False
    n = 0
    for fi, side, fill, _oi in shots:
        ex_i = min(len(bars) - 1, fi + recipe.hold)
        exit_px = bars[ex_i].close
        usd += gold_cash(side, fill, exit_px, 1.0)
        if blown:
            continue
        stake = eq * RISK
        oz = oz_lot(stake, fill)
        if oz < OZ_STEP:
            continue
        cash = gold_cash(side, fill, exit_px, oz)
        if cash < -stake:
            cash = -stake
        eq = max(0.0, eq + cash)
        n += 1
        if cash > 0:
            wins += 1
        if eq <= 0:
            blown = True
    taken = n
    wr = 100.0 * wins / taken if taken else 0.0
    trig = f"{recipe.trigger:.1%}" if recipe.kind == "pct" else f"${recipe.trigger:.1f}"
    return Row(
        recipe.name,
        recipe.source,
        recipe.session,
        trig,
        recipe.hold,
        taken,
        wins,
        round(wr, 1),
        round(usd, 2),
        round(eq, 2),
        blown,
    )


def gld_rows(h1: list[Bar], begin: date, bank: float) -> list[Row]:
    from xau_jam.paper import replay

    rows: list[Row] = []
    for trig, hold, name in (
        (0.006, 6, "gld-0.6-h6"),
        (0.003, 6, "gld-0.3-h6"),
        (0.006, 12, "gld-0.6-h12"),
    ):
        eq, path = replay(h1, bank, LEV, compound=True, begin=begin, trigger=trig, hold=hold)
        taken = [p for p in path if p.event == "ok"]
        wins = sum(1 for p in taken if p.cash > 0)
        wr = 100.0 * wins / len(taken) if taken else 0.0
        rows.append(
            Row(
                name,
                "paper/GLD",
                "daily",
                f"{trig:.1%}",
                hold,
                len(taken),
                wins,
                round(wr, 1),
                0.0,
                round(eq, 2),
                eq <= 0,
            )
        )
    return rows


def format_table(rows: list[Row], bank: float, last: float, begin: date) -> str:
    lines = [
        "ЗОЛОТО → IMPULSE. Каталог, не канон. Сам выбирай.",
        "Вход: signal_for_day (одна сторона от OPEN сессии, оба фитиля = skip).",
        "GC=F H1 Yahoo. 1 oz спред $0.30 RT. Банк: 20% × 1:10, лот 0.01oz, клип −stake.",
        f"с {begin}, последняя цена ${last:.2f}, старт ${bank:.0f}.",
        "usd_oz = сумма на 1 унцию (без плеча). bank = $350 20%. blown = банк умер.",
        "Это 2026 in-sample. hold=1 и WR 90%+ не канон — спред может съесть. Сам решай.",
        "",
        f"{'name':28} {'src':11} {'sess':7} {'trig':7} h  n  wr    usd/oz     bank",
    ]
    for r in sorted(rows, key=lambda x: (-x.bank, -x.usd_oz)):
        dead = "  DEAD" if r.blown else ""
        lines.append(
            f"{r.name:28} {r.source:11} {r.session:7} {r.trigger:7} {r.hold:2} "
            f"{r.n:3} {r.wr:5.1f}% {r.usd_oz:8.2f} {r.bank:8.2f}{dead}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=350.0)
    ap.add_argument("--from", dest="date_from", default="01/01/26")
    args = ap.parse_args()
    begin = parse_day(args.date_from)
    REPORTS.mkdir(parents=True, exist_ok=True)
    print(f"gold impulse catalog from {begin}", flush=True)
    h1 = fetch_yahoo(SYMBOL, "1y", "60m")
    rows = [score(h1, rec, begin, args.bank) for rec in RECIPES]
    try:
        gld = fetch_yahoo(GLD, "1y", "60m")
        rows.extend(gld_rows(gld, begin, args.bank))
    except Exception as exc:  # noqa: BLE001
        print(f"  skip GLD: {exc}", flush=True)
    text = format_table(rows, args.bank, h1[-1].close, begin)
    (REPORTS / "GOLD_IMPULSE.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
