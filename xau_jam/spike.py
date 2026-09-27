"""Hunt one impulse that can print ~1000% on the bank at 1:10.

Varies trigger, hold, EOD, multi-day. Honest one-side entry. Simple all-in one trade.

  python3 -m xau_jam.spike --bank 500 --leverage 10
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import timedelta

from xau_jam.any_shot import SYMBOLS
from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_yahoo
from xau_jam.hunt import EXTRA
from xau_jam.paper import costed_cash, day_groups, signal_for_day

RISKY = ("MARA", "RIOT", "SOXL", "TQQQ", "SQQQ", "VIXY", "GME", "SMCI", "COIN", "MSTR", "UVXY")


@dataclass(slots=True)
class Spike:
    symbol: str
    style: str
    day: str
    side: str
    entry: float
    exit: float
    ret_pct: float
    bank_pct: float
    cash: float
    shares: int


def _score(symbol: str, style: str, side: str, entry: float, exit_px: float, day: str, start: float, lev: int) -> Spike | None:
    shares = int(start * lev / max(entry, 1e-9))
    if shares < 1:
        return None
    cash = costed_cash(side, entry, exit_px, shares)
    ret = (exit_px - entry) / entry if side == "buy" else (entry - exit_px) / entry
    return Spike(
        symbol=symbol,
        style=style,
        day=day,
        side=side,
        entry=round(entry, 4),
        exit=round(exit_px, 4),
        ret_pct=round(100.0 * ret, 2),
        bank_pct=round(100.0 * cash / start, 1),
        cash=round(cash, 2),
        shares=shares,
    )


def hunt_symbol(sym: str, h1, d1, start: float, lev: int) -> list[Spike]:
    out: list[Spike] = []
    by = day_groups(h1)
    for trig in (0.003, 0.006, 0.01):
        for day, idxs in by.items():
            sig = signal_for_day(h1, idxs, trigger=trig)
            if sig is None:
                continue
            side, fi, fill = sig
            last_same = idxs[-1]
            holds = {
                "hold1": min(len(h1) - 1, fi + 1),
                "hold6": min(len(h1) - 1, fi + 6),
                "hold24": min(len(h1) - 1, fi + 24),
                "eod": last_same,
            }
            later = [j for j in range(fi, len(h1)) if h1[j].time.date() <= day + timedelta(days=5)]
            if later:
                holds["hold5d"] = later[-1]
            for name, ex_i in holds.items():
                got = _score(sym, f"impulse {trig:.1%} {name}", side, fill, h1[ex_i].close, day.isoformat(), start, lev)
                if got:
                    out.append(got)
    for i in range(1, len(d1)):
        prev, b = d1[i - 1], d1[i]
        if prev.close <= 0:
            continue
        gap = (b.open - prev.close) / prev.close
        if abs(gap) < 0.03:
            continue
        side = "buy" if gap > 0 else "sell"
        for name, ex in (("gap-eod", b.close), ("gap-2d", d1[min(len(d1) - 1, i + 1)].close)):
            got = _score(sym, name, side, b.open, ex, b.time.date().isoformat(), start, lev)
            if got:
                out.append(got)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=10)
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    symbols = list(dict.fromkeys([*RISKY, *SYMBOLS, *EXTRA]))
    shots: list[Spike] = []
    print(f"spike hunt ${args.bank:.0f} 1:{args.leverage}", flush=True)
    for sym in symbols:
        try:
            h1 = fetch_yahoo(sym, "730d", "60m")
            d1 = fetch_yahoo(sym, "2y", "1d")
        except Exception as exc:
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        got = hunt_symbol(sym, h1, d1, args.bank, args.leverage)
        shots.extend(got)
        best = max(got, key=lambda s: s.bank_pct) if got else None
        if best:
            print(f"  {sym} best {best.bank_pct:+.0f}% {best.style} {best.day}", flush=True)
        else:
            print(f"  {sym} none", flush=True)

    shots.sort(key=lambda s: s.bank_pct, reverse=True)
    uniq: list[Spike] = []
    seen: set[tuple[str, str]] = set()
    for s in shots:
        key = (s.symbol, s.day)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    fat = [s for s in shots if s.bank_pct >= 1000]
    near = [s for s in shots if s.bank_pct >= 400]
    lines = [
        f"Один скачок, all-in ${args.bank:.0f} ×{args.leverage}, честный вход (без both-wick), комиссии.",
        f"сделок-кандидатов={len(shots)}  уникальных дней={len(uniq)}  ≥1000% банка={len(fat)}  ≥400%={len(near)}",
        "",
        "топ-15 разных дней:",
    ]
    for s in uniq[:15]:
        lines.append(
            f"  {s.bank_pct:+8.1f}% банка  цена {s.ret_pct:+6.1f}%  {s.day} {s.symbol:8} {s.side:4} "
            f"{s.entry:.2f}→{s.exit:.2f}  {s.style}"
        )
    lines.append("")
    if fat:
        lines.append("есть ≥1000% за один скачок:")
        for s in fat[:10]:
            lines.append(f"  {s.symbol} {s.day} {s.bank_pct:+.0f}%  {s.style}")
    else:
        best = uniq[0] if uniq else None
        if best:
            need_move = 100.0 / args.leverage
            lines.append(
                f"1000% банка на 1:{args.leverage} = ход цены ~{need_move:.0f}%. "
                f"Максимум тут {best.bank_pct:+.0f}% ({best.symbol} {best.day}, цена {best.ret_pct:+.1f}%)."
            )
            lines.append(
                f"Чтобы было 1000% на этом ходе {best.ret_pct:.1f}%, нужно плечо ~"
                f"{int(round(1000 / max(best.ret_pct, 0.1)))}x."
            )
    text = "\n".join(lines) + "\n"
    (REPORTS / "spike.txt").write_text(text, encoding="utf-8")
    (REPORTS / "spike.json").write_text(
        json.dumps({"top": [asdict(s) for s in uniq[:30]], "n1000": len(fat)}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
