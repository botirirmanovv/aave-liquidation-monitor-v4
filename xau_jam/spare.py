"""Plan B stocks — same impulse as Combine, not BOOK A, not gold.

Gold GC=F dropped. Spare names from hunt EXTRA + NVDA (none of the seven).
HOLD=6, 0.6%, 20%→$25k. Live / 7496 / Aave / Morpho live stay off.

  python3 -m xau_jam.spare --from 01/01/26 --bank 350
  python3 -m xau_jam.gold --from 01/01/26 --bank 350
"""
from __future__ import annotations

import argparse
import time
from xau_jam.burst_open import REPORTS
from xau_jam.combine import (
    BOOK,
    LIVE_MONEY,
    MAX_STAKE,
    RUN_RISK,
    START_BANK,
    START_RISK,
    TARGET_BANK,
    collect_signals,
    min_start_bank,
    run_replay,
)
from xau_jam.data import fetch_yahoo
from xau_jam.paper import HOLD, parse_day
from xau_jam.pattern import Bar

# Not MSTR/COIN/SMCI/AMD/UVXY/PLTR/TSLA. Triggers not retuned.
BOOK_B = (
    ("NVDA", 0.006),
    ("META", 0.006),
    ("AMZN", 0.006),
    ("NFLX", 0.006),
    ("AAPL", 0.006),
    ("BABA", 0.006),
)

assert not {s for s, _ in BOOK_B} & {s for s, _ in BOOK}
assert not LIVE_MONEY


def fetch_books(range_spec: str = "1y") -> dict[str, tuple[list[Bar], float]]:
    books: dict[str, tuple[list[Bar], float]] = {}
    for i, (sym, trig) in enumerate(BOOK_B):
        try:
            books[sym] = (fetch_yahoo(sym, range_spec, "60m"), trig)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        if i + 1 < len(BOOK_B):
            time.sleep(0.05)
    return books


def write_report(
    bank: float,
    end: float,
    fills,
    withdrawals,
    last_px: dict[str, float],
    begin,
) -> str:
    took = sum(w["took"] for w in withdrawals)
    names = ", ".join(s for s, _ in BOOK_B)
    a_names = ", ".join(s for s, _ in BOOK)
    by: dict[str, int] = {}
    for f in fills:
        by[f.symbol] = by.get(f.symbol, 0) + 1
    won = sum(1 for f in fills if f.cash > 0)
    lines = [
        "B — не золото. Другие акции, тот же импульс, что у A.",
        f"A не трогали: {a_names}.",
        f"B: {names}. порог 0.6% HOLD={HOLD}. банк ${bank:.0f}, 20%→${TARGET_BANK:.0f}.",
        "GC=F / Jam / 0.01oz выкинуты. Live/7496/Aave/Morpho live закрыты.",
        f"с {begin} сделок {len(fills)} плюс={won} clip={sum(1 for f in fills if f.event == 'clip')}.",
        f"банк ${end:.2f}. снял ${took:.2f}. всего ${end + took:.2f}. $25k={'да' if end + 1e-9 >= TARGET_BANK or took else 'нет'}.",
        "по бумагам: " + " ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])),
        "последние цены: " + ", ".join(f"{s} ${p:.2f}" for s, p in last_px.items()),
        "HOOD/ARM/TQQQ/SOXL в книгу не клал — это подгонка под плюс.",
        "",
    ]
    for f in fills[:8]:
        lines.append(
            f"  {f.time} {f.symbol:5} {f.side:4} {f.shares}шт {f.entry:.2f}→{f.exit:.2f} {f.cash:+.2f} eq=${f.equity:.2f}"
        )
    if len(fills) > 8:
        lines.append(f"  … ещё {len(fills) - 8}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=START_BANK)
    ap.add_argument("--from", dest="date_from", default="01/01/26")
    ap.add_argument("--leverage", type=int, default=10)
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    begin = parse_day(args.date_from)
    print(f"spare B from {begin} {', '.join(s for s, _ in BOOK_B)}", flush=True)
    books = fetch_books("1y")
    if not books:
        print("нет баров")
        return 1
    need, last_px = min_start_bank(books, START_RISK, args.leverage)
    print(f"  1шт самой дорогой нужен банк ≥ ${need:.0f}", flush=True)
    shots = collect_signals(books, begin, None, book=BOOK_B)
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
    text = write_report(args.bank, plan.end, plan.fills, plan.withdrawals, last_px, begin)
    (REPORTS / "SPARE_B.txt").write_text(text, encoding="utf-8")
    (REPORTS / "GOLD_B.txt").write_text(
        "GOLD B снят. Золото заменили другими акциями.\n\n" + text, encoding="utf-8"
    )
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
