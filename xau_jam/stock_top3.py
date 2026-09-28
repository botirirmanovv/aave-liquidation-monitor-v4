"""Top-3 gold impulse recipes, same rules on stocks.

  python3 -m xau_jam.stock_top3 --from 01/01/26 --bank 350
"""
from __future__ import annotations

import argparse
import time

from xau_jam.burst_open import REPORTS
from xau_jam.combine import (
    MAX_STAKE,
    RUN_RISK,
    START_RISK,
    TARGET_BANK,
    run_replay,
)
from xau_jam.data import fetch_yahoo
from xau_jam.gold_impulse import Recipe, trigger_for
from xau_jam.open_scalp import SESSIONS, _session_opens
from xau_jam.paper import day_groups, parse_day, signal_for_day
from xau_jam.pattern import Bar

STOCKS = ("NVDA", "META", "AMZN", "NFLX", "AAPL", "BABA", "MSFT", "GOOGL")

# Three best GC=F 2026 rows, unchanged knobs, now equities.
TOP3: tuple[Recipe, ...] = (
    Recipe("ny-$6-h12", "oneshot", "ny", "usd", 6.0, 12, 9),
    Recipe("daily-0.3%-h6", "paper", "daily", "pct", 0.003, 6, 9),
    Recipe("ny-$6-h1", "burst", "ny", "usd", 6.0, 1, 8),
)

PLAIN = {
    "ny-$6-h12": "Нью-Йорк открытие. Цена ушла на $6 только в одну сторону. Держим 12 часов.",
    "daily-0.3%-h6": "Открытие дня. Цена ушла на 0.3% только в одну сторону. Держим 6 часов.",
    "ny-$6-h1": "Нью-Йорк открытие. Цена ушла на $6 только в одну сторону. Держим 1 час.",
}


def session_opens(h1: list[Bar], session: str) -> list[int]:
    """Stocks: daily = first bar of the UTC date (RTH open). Gold used midnight."""
    if session == "daily":
        return [idxs[0] for idxs in day_groups(h1).values() if idxs]
    return _session_opens(h1, SESSIONS[session])


def collect_shots(
    books: dict[str, list[Bar]],
    recipe: Recipe,
    begin,
    end=None,
) -> list[tuple]:
    shots = []
    for sym, h1 in books.items():
        for oi in session_opens(h1, recipe.session):
            day = h1[oi].time.date()
            if day < begin:
                continue
            if end is not None and day >= end:
                continue
            last = min(len(h1) - 1, oi + max(recipe.hunt, 1) - 1)
            idxs = list(range(oi, last + 1))
            sig = signal_for_day(h1, idxs, trigger=trigger_for(h1, oi, recipe))
            if sig is None:
                continue
            side, fi, fill = sig
            shots.append((h1[fi].time, sym, side, fill, fi, h1))
    rank = {s: i for i, s in enumerate(STOCKS)}
    shots.sort(key=lambda s: (s[0], rank.get(s[1], 99)))
    return shots


def fetch_books(range_spec: str = "1y") -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    for i, sym in enumerate(STOCKS):
        try:
            out[sym] = fetch_yahoo(sym, range_spec, "60m")
            print(f"  {sym} last={out[sym][-1].close:.2f}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {sym}: {exc}", flush=True)
        if i + 1 < len(STOCKS):
            time.sleep(0.05)
    return out


def pack_recipe(recipe: Recipe, plan, bank: float) -> list[str]:
    took = sum(float(w.get("took") or 0.0) for w in plan.withdrawals)
    by: dict[str, int] = {}
    for f in plan.fills:
        by[f.symbol] = by.get(f.symbol, 0) + 1
    won = sum(1 for f in plan.fills if f.cash > 0)
    total = plan.end + took
    hit = "да" if total + 1e-9 >= TARGET_BANK or took else "нет"
    lines = [
        f"Правило {recipe.name}",
        PLAIN[recipe.name],
        f"акции: {', '.join(STOCKS)}",
        f"$350 → ${plan.end:.2f}  снял ${took:.2f}  всего ${total:.2f}  $25k={hit}",
        f"сделок {len(plan.fills)}  плюс {won}  clip {sum(1 for f in plan.fills if f.event == 'clip')}",
        "по бумагам: " + (" ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])) or "нет"),
        "",
    ]
    for f in plan.fills[:5]:
        lines.append(
            f"  {f.time} {f.symbol:5} {f.side:4} {f.shares}шт {f.entry:.2f}→{f.exit:.2f} {f.cash:+.2f} eq=${f.equity:.2f}"
        )
    if len(plan.fills) > 5:
        lines.append(f"  … ещё {len(plan.fills) - 5}")
    lines.append("")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=350.0)
    ap.add_argument("--from", dest="date_from", default="01/01/26")
    ap.add_argument("--leverage", type=int, default=10)
    args = ap.parse_args()
    begin = parse_day(args.date_from)
    REPORTS.mkdir(parents=True, exist_ok=True)
    print(f"stock top3 from {begin} {', '.join(STOCKS)}", flush=True)
    books = fetch_books()
    if not books:
        print("нет баров")
        return 1
    chunks = [
        "3 ЛУЧШИХ С ЗОЛОТА → ТЕ ЖЕ ПРАВИЛА НА АКЦИЯХ.",
        "Вход тот же: одна сторона от открытия, оба фитиля = пропуск.",
        f"Банк ${args.bank:.0f}, ставка 20% до ${TARGET_BANK:.0f}, потолок ${MAX_STAKE:.0f}, 1:{args.leverage}, целые акции.",
        f"Бумаги: {', '.join(STOCKS)}. с {begin}.",
        "$6 на дешёвой акции — это большой %, на META — маленький. Не подгонял.",
        "",
    ]
    results = []
    for rec in TOP3:
        shots = collect_shots(books, rec, begin)
        plan = run_replay(
            shots,
            args.bank,
            args.leverage,
            hold=rec.hold,
            risk=START_RISK,
            simple=False,
            max_stake=MAX_STAKE,
            target_bank=TARGET_BANK,
            run_risk=RUN_RISK,
            withdraw=True,
        )
        took = sum(float(w.get("took") or 0.0) for w in plan.withdrawals)
        results.append((rec, plan, took))
    chunks.append("Итог:")
    for rec, plan, took in results:
        chunks.append(f"  {rec.name}: ${args.bank:.0f} → ${plan.end:.2f} (снял ${took:.2f})")
    chunks.append("")
    for rec, plan, _took in results:
        chunks.extend(pack_recipe(rec, plan, args.bank))
    text = "\n".join(chunks).rstrip() + "\n"
    (REPORTS / "STOCK_TOP3.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
