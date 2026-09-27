"""Auto-pick the best 1:10 book across markets, then backtest it fully.

Scores the whole sequence (compound, one position), not one lucky day.

  python3 -m xau_jam.auto_best --bank 500 --leverage 10
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import timedelta

from xau_jam.any_shot import SYMBOLS
from xau_jam.backtest import Params, Trade, run_backtest
from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_yahoo
from xau_jam.pattern import Bar


@dataclass(slots=True)
class Book:
    key: str
    symbol: str
    style: str
    start: float
    end: float
    net: float
    max_dd: float
    trades: int
    wins: int
    losses: int
    blown: bool
    path: list[dict]


def _impulse_trades(bars: list[Bar], trigger_pct: float, hold: int) -> list[Trade]:
    by_day: dict = {}
    for i, b in enumerate(bars):
        by_day.setdefault(b.time.date(), []).append(i)
    out: list[Trade] = []
    busy = -1
    for idxs in by_day.values():
        oi = idxs[0]
        session_px = bars[oi].open
        trig = session_px * trigger_pct
        side = fill_i = fill_px = None
        last_hunt = idxs[min(8, len(idxs) - 1)]
        for i in range(oi, last_hunt + 1):
            if i <= busy:
                continue
            up = bars[i].high - session_px
            dn = session_px - bars[i].low
            if up < trig and dn < trig:
                continue
            if up >= dn:
                side, fill_px = "buy", session_px + trig
                if bars[i].high < fill_px:
                    continue
            else:
                side, fill_px = "sell", session_px - trig
                if bars[i].low > fill_px:
                    continue
            fill_i = i
            break
        if side is None or fill_i is None or fill_px is None:
            continue
        ex_i = min(len(bars) - 1, fill_i + hold)
        exit_px = bars[ex_i].close
        pnl = (exit_px - fill_px) if side == "buy" else (fill_px - exit_px)
        busy = ex_i
        out.append(
            Trade(
                side=side,
                signal_time=bars[fill_i].time.isoformat(),
                entry_time=bars[fill_i].time.isoformat(),
                exit_time=bars[ex_i].time.isoformat(),
                entry=round(fill_px, 6),
                stop=round(session_px, 6),
                target=round(exit_px, 6),
                exit=round(exit_px, 6),
                bars_held=ex_i - fill_i + 1,
                reason="time",
                pnl=round(pnl, 6),
                r_multiple=0.0,
                signal_high=bars[fill_i].high,
                prev_high=session_px,
                prev_low=1.0,
                signal_close=bars[fill_i].close,
            )
        )
    return out


def _gap_trades(bars: list[Bar], gap_pct: float) -> list[Trade]:
    out: list[Trade] = []
    for i in range(1, len(bars)):
        prev, b = bars[i - 1], bars[i]
        if prev.close <= 0:
            continue
        gap = (b.open - prev.close) / prev.close
        if gap >= gap_pct:
            side = "buy"
        elif gap <= -gap_pct:
            side = "sell"
        else:
            continue
        pnl = (b.close - b.open) if side == "buy" else (b.open - b.close)
        out.append(
            Trade(
                side=side,
                signal_time=b.time.isoformat(),
                entry_time=b.time.isoformat(),
                exit_time=b.time.isoformat(),
                entry=round(b.open, 6),
                stop=round(prev.close, 6),
                target=round(b.close, 6),
                exit=round(b.close, 6),
                bars_held=1,
                reason="eod",
                pnl=round(pnl, 6),
                r_multiple=0.0,
                signal_high=b.high,
                prev_high=prev.close,
                prev_low=1.0,
                signal_close=b.close,
            )
        )
    return out


def _jam_trades(symbol: str, bars: list[Bar], rr: float) -> list[Trade]:
    px = bars[-1].close
    p = Params(
        side="both",
        rr=rr,
        spread=max(px * 0.0002, 1e-6),
        sl_buffer=max(px * 0.0015, 1e-6),
        max_hold=24,
        max_risk=1e9,
        session="all",
        trend="none",
        exit_mode="fixed",
        symbol=symbol,
    )
    try:
        _, trades = run_backtest(bars, params=p)
    except Exception:
        return []
    return trades


def run_book(trades: list[Trade], start: float, leverage: int, key: str, symbol: str, style: str) -> Book:
    eq = start
    peak = start
    dd = 0.0
    blown = False
    wins = losses = 0
    path: list[dict] = []
    for t in trades:
        if blown or eq <= 0:
            path.append({"time": t.entry_time, "side": t.side, "pnl": 0.0, "eq": eq, "event": "dead"})
            continue
        ret = t.pnl / max(t.entry, 1e-9)
        cash = eq * leverage * ret
        eq += cash
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        event = "ok"
        if eq <= 0:
            eq = 0.0
            blown = True
            event = "blown"
        if cash > 0:
            wins += 1
        elif cash < 0:
            losses += 1
        path.append(
            {
                "time": t.entry_time,
                "exit": t.exit_time,
                "side": t.side,
                "entry": t.entry,
                "exit_px": t.exit,
                "ret_pct": round(100.0 * ret, 3),
                "cash": round(cash, 2),
                "eq": round(eq, 2),
                "event": event,
            }
        )
    taken = wins + losses
    return Book(
        key=key,
        symbol=symbol,
        style=style,
        start=start,
        end=round(eq, 2),
        net=round(eq - start, 2),
        max_dd=round(dd, 2),
        trades=taken,
        wins=wins,
        losses=losses,
        blown=blown,
        path=path,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--min-trades", type=int, default=4)
    ap.add_argument("--range", dest="range_spec", default="3mo")
    ap.add_argument("--months", type=int, default=0)
    args = ap.parse_args()

    books: list[Book] = []
    print(
        f"auto-best bank=${args.bank:.0f} 1:{args.leverage} range={args.range_spec} "
        f"months={args.months or 'all'}",
        flush=True,
    )
    for sym in SYMBOLS:
        try:
            h1 = fetch_yahoo(sym, args.range_spec, "60m")
            d1 = fetch_yahoo(sym, args.range_spec, "1d")
        except Exception as exc:
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        if args.months and h1:
            cut = h1[-1].time - timedelta(days=30 * args.months)
            h1 = [b for b in h1 if b.time >= cut]
            d1 = [b for b in d1 if b.time >= cut]
        print(f"  {sym} h1={len(h1)} d1={len(d1)}", flush=True)
        for trig in (0.003, 0.004, 0.006):
            for hold in (6, 12, 24):
                tr = _impulse_trades(h1, trig, hold)
                key = f"{sym} impulse {trig:.1%} hold={hold}"
                books.append(run_book(tr, args.bank, args.leverage, key, sym, key))
        for rr in (2.0, 3.0):
            tr = _jam_trades(sym, h1, rr)
            key = f"{sym} jam rr={rr:g}"
            books.append(run_book(tr, args.bank, args.leverage, key, sym, key))
        for gp in (0.02, 0.03):
            tr = _gap_trades(d1, gp)
            key = f"{sym} gap {gp:.0%}"
            books.append(run_book(tr, args.bank, args.leverage, key, sym, key))

    alive = [b for b in books if not b.blown and b.trades >= args.min_trades]
    alive.sort(key=lambda b: (b.end, -b.max_dd), reverse=True)
    if not alive:
        alive = sorted(books, key=lambda b: b.end, reverse=True)
    win = alive[0]

    pct = 100.0 * win.net / win.start if win.start else 0.0
    lines = [
        f"Автопоиск лучшей книги, ${args.bank:.0f}, плечо 1:{args.leverage}, compound all-in, range={args.range_spec}",
        f"вариантов={len(books)} живых с ≥{args.min_trades} сделками={sum(1 for b in books if not b.blown and b.trades >= args.min_trades)}",
        "",
        f"ПОБЕДИТЕЛЬ: {win.key}",
        f"${win.start:.0f} → ${win.end:.2f}  net {win.net:+.2f}  ({pct:+.1f}%)  DD ${win.max_dd:.2f}  "
        f"n={win.trades}  +{win.wins}/−{win.losses}",
        "",
        "топ-10 живых:",
    ]
    for i, b in enumerate(alive[:10], 1):
        lines.append(
            f"  {i:02d} {b.end:9.2f}$  net={b.net:+8.2f}  DD={b.max_dd:7.0f}  "
            f"n={b.trades:2d} wr={100.0 * b.wins / b.trades:4.0f}%  {b.key}"
        )
    lines.append("")
    lines.append("сделки победителя:")
    for i, p in enumerate(win.path, 1):
        lines.append(
            f"  {i:02d} {p['time']} {p['side']:4} {p['ret_pct']:+6.2f}%  "
            f"cash={p['cash']:+8.2f}  eq=${p['eq']:.2f}  {p['event']}"
        )
    text = "\n".join(lines) + "\n"
    out = REPORTS
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{args.months}m" if args.months else args.range_spec.replace("mo", "m")
    (out / "auto_best.txt").write_text(text, encoding="utf-8")
    (out / f"auto_best_{tag}.txt").write_text(text, encoding="utf-8")
    (out / "auto_best.json").write_text(
        json.dumps(
            {
                "winner": {k: getattr(win, k) for k in ("key", "symbol", "style", "start", "end", "net", "max_dd", "trades", "wins", "losses", "blown")},
                "path": win.path,
                "top": [
                    {"key": b.key, "end": b.end, "net": b.net, "dd": b.max_dd, "n": b.trades}
                    for b in alive[:15]
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    rows = ["time,exit,side,entry,exit_px,ret_pct,cash,eq,event"]
    for p in win.path:
        rows.append(
            f"{p['time']},{p.get('exit','')},{p['side']},{p.get('entry','')},"
            f"{p.get('exit_px','')},{p['ret_pct']},{p['cash']},{p['eq']},{p['event']}"
        )
    (out / "auto_best_trades.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
