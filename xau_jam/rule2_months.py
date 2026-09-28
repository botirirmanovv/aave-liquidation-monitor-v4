"""Rule 2 monthly: simple 20% of $350 vs compound 20% of equity.

  python3 -m xau_jam.rule2_months --bank 350
"""
from __future__ import annotations

import argparse
from datetime import timedelta

from xau_jam.burst_open import REPORTS
from xau_jam.combine import MAX_STAKE, START_RISK, monthly_rows, run_replay
from xau_jam.paper import parse_day
from xau_jam.stock_top3 import STOCKS, TOP3, collect_shots, fetch_books

RULE2 = TOP3[1]


def fill_months(rows: list[dict], start: float, first: str, last: str) -> list[dict]:
    by = {r["month"]: r for r in rows}
    y, m = int(first[:4]), int(first[5:7])
    ey, em = int(last[:4]), int(last[5:7])
    eq = start
    out: list[dict] = []
    while (y, m) <= (ey, em):
        key = f"{y:04d}-{m:02d}"
        if key in by:
            row = dict(by[key])
            eq = row["end"]
            out.append(row)
        else:
            out.append(
                {
                    "month": key,
                    "n": 0,
                    "wins": 0,
                    "pnl": 0.0,
                    "start": round(eq, 2),
                    "end": round(eq, 2),
                    "took": 0.0,
                    "pct_start": 0.0,
                    "pct_month": 0.0,
                }
            )
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def year_slice(rows: list[dict], year: int) -> list[dict]:
    return [r for r in rows if r["month"].startswith(str(year))]


def fmt_table(title: str, rows: list[dict], start: float, end: float, n: int) -> list[str]:
    pct = 100.0 * (end - start) / start if start else 0.0
    lines = [
        title,
        f"$350 → ${end:.2f}  ({pct:+.1f}%)  сделок {n}",
        f"{'мес':8} {'сд':>4} {'+':>4} {'pnl':>10} {'с':>10} {'по':>10} {'%мес':>8} {'%от350':>8}",
    ]
    for r in rows:
        lines.append(
            f"{r['month']:8} {r['n']:4} {r['wins']:4} {r['pnl']:+10.2f} "
            f"{r['start']:10.2f} {r['end']:10.2f} {r['pct_month']:+7.1f}% {r['pct_start']:+7.1f}%"
        )
    y24 = year_slice(rows, 2024)
    y25 = year_slice(rows, 2025)
    if y24:
        p24 = sum(r["pnl"] for r in y24)
        s24, e24 = y24[0]["start"], y24[-1]["end"]
        lines.append(
            f"2024 итог  сд {sum(r['n'] for r in y24):4}  "
            f"${s24:.2f} → ${e24:.2f}  pnl {p24:+.2f}  ({100 * (e24 - s24) / s24:+.1f}% за год)"
        )
    if y25:
        p25 = sum(r["pnl"] for r in y25)
        s25, e25 = y25[0]["start"], y25[-1]["end"]
        lines.append(
            f"2025 итог  сд {sum(r['n'] for r in y25):4}  "
            f"${s25:.2f} → ${e25:.2f}  pnl {p25:+.2f}  ({100 * (e25 - s25) / s25:+.1f}% за год)"
        )
    lines.append("")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=350.0)
    ap.add_argument("--from", dest="date_from", default="01/01/24")
    ap.add_argument("--to", dest="date_to", default="31/12/25")
    ap.add_argument("--leverage", type=int, default=10)
    args = ap.parse_args()
    begin = parse_day(args.date_from)
    end = parse_day(args.date_to)
    until = end + timedelta(days=1)
    REPORTS.mkdir(parents=True, exist_ok=True)
    print(f"rule2 months {begin}→{end} {', '.join(STOCKS)}", flush=True)
    books = fetch_books("730d")
    if not books:
        print("нет баров")
        return 1
    first_bar = min(h1[0].time.date() for h1 in books.values() if h1)
    last_bar = max(h1[-1].time.date() for h1 in books.values() if h1)
    shots = collect_shots(books, RULE2, begin, until)
    simple = run_replay(
        shots,
        args.bank,
        args.leverage,
        hold=RULE2.hold,
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
        hold=RULE2.hold,
        risk=START_RISK,
        simple=False,
        max_stake=MAX_STAKE,
        target_bank=None,
        withdraw=False,
    )
    s_rows = fill_months(monthly_rows(simple.fills, args.bank), args.bank, "2024-01", "2025-12")
    c_rows = fill_months(monthly_rows(compound.fills, args.bank), args.bank, "2024-01", "2025-12")
    lines = [
        "ПРАВИЛО 2 — 2024 и 2025, помесячно.",
        "Открытие дня, 0.3%, HOLD=6. NVDA META AMZN NFLX AAPL BABA MSFT GOOGL.",
        "Простой %: ставка всегда $70 (20% от $350), сложный не растёт.",
        "Сложный %: ставка 20% текущего банка, потолок $2500. Снятие сверх $25k выключено — чтобы видеть рост.",
        f"Yahoo H1 с {first_bar} по {last_bar}. Окно сделок {begin} … {end}.",
        f"Сделок простой {len(simple.fills)}, сложный {len(compound.fills)} — на мелком банке META/MSFT иногда не влезает 1 шт.",
        "",
    ]
    lines.extend(
        fmt_table(
            "── простой % (лот $70) ──",
            s_rows,
            args.bank,
            simple.end,
            len(simple.fills),
        )
    )
    lines.extend(
        fmt_table(
            "── сложный % (20% банка, потолок $2500, без снятия) ──",
            c_rows,
            args.bank,
            compound.end,
            len(compound.fills),
        )
    )
    text = "\n".join(lines)
    (REPORTS / "RULE2_2024_2025.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
