"""Grid-search Jam variants on one H1 month. Pick max net PnL.

  python3 -m xau_jam.optimize
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict
from pathlib import Path

from xau_jam.backtest import Params, REPORTS, run_backtest, write_outputs
from xau_jam.data import fetch_yahoo_h1

MIN_TRADES_ROBUST = 8


def grid() -> list[Params]:
    sides = ("sell", "buy", "both")
    rrs = (0.8, 1.0, 1.5, 2.0, 2.5, 3.0)
    buffers = (0.3, 0.8, 1.5)
    holds = (8, 16, 24, 48)
    min_risks = (0.0, 5.0, 10.0)
    max_risks = (20.0, 40.0, 999.0)
    sessions = ("all", "london", "ny", "asia")
    trends = ("none", "sma20")
    bodies = (0.0, 0.35)
    exits = ("fixed", "be_after_1r")
    out: list[Params] = []
    for combo in itertools.product(
        sides, rrs, buffers, holds, min_risks, max_risks, sessions, trends, bodies, exits
    ):
        side, rr, buf, hold, mn, mx, sess, trend, body, ex = combo
        if mn >= mx:
            continue
        out.append(
            Params(
                side=side,
                rr=rr,
                sl_buffer=buf,
                max_hold=hold,
                min_risk=mn,
                max_risk=mx,
                session=sess,
                trend=trend,
                min_body=body,
                exit_mode=ex,
            )
        )
    return out


def row_of(p: Params, r) -> dict:
    return {
        "net_pnl": r.net_pnl,
        "trades": r.trades,
        "wins": r.wins,
        "win_rate": r.win_rate,
        "avg_r": r.avg_r,
        "max_dd": r.max_dd,
        "profit_factor": r.profit_factor if r.profit_factor != float("inf") else None,
        "signals": r.signals,
        **asdict(p),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--range", dest="range_spec", default="1mo")
    ap.add_argument("--min-trades", type=int, default=MIN_TRADES_ROBUST)
    ap.add_argument("--out-dir", default=str(REPORTS))
    args = ap.parse_args()

    bars = fetch_yahoo_h1(args.symbol, args.range_spec)
    configs = grid()
    scored: list[dict] = []
    print(f"bars={len(bars)} {bars[0].time} -> {bars[-1].time}  grid={len(configs)}", flush=True)

    for i, p in enumerate(configs, 1):
        p.symbol = args.symbol
        r, _ = run_backtest(bars, params=p)
        scored.append(row_of(p, r))
        if i % 2000 == 0:
            print(f"  ... {i}/{len(configs)}", flush=True)

    scored.sort(key=lambda x: (x["net_pnl"], x["avg_r"], -x["max_dd"]), reverse=True)
    robust = [x for x in scored if x["trades"] >= args.min_trades]
    robust.sort(key=lambda x: (x["net_pnl"], x["avg_r"], -x["max_dd"]), reverse=True)

    raw_best = scored[0] if scored else None
    best = robust[0] if robust else raw_best
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "optimize_all.json").write_text(
        json.dumps(
            {
                "bars": len(bars),
                "start": bars[0].time.isoformat(),
                "end": bars[-1].time.isoformat(),
                "grid": len(configs),
                "min_trades": args.min_trades,
                "best_any": raw_best,
                "best_min_trades": best,
                "top20_any": scored[:20],
                "top20_min_trades": robust[:20],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    lines = [
        "Jam grid-search — максимальный плюс на последнем месяце",
        f"окно: {bars[0].time.isoformat()} → {bars[-1].time.isoformat()}  баров={len(bars)}",
        f"вариантов: {len(configs)}",
        "",
        "топ-10 любых (можно 1-7 сделок, переобучение):",
    ]
    for i, x in enumerate(scored[:10], 1):
        lines.append(
            f"  {i:02d} pnl={x['net_pnl']:+8.2f}  n={x['trades']:2d} wr={x['win_rate']:5.1f}%  "
            f"avgR={x['avg_r']:+.2f} dd={x['max_dd']:.1f}  "
            f"{x['side']} RR={x['rr']} hold={x['max_hold']} "
            f"sess={x['session']} trend={x['trend']} body={x['min_body']} "
            f"risk={x['min_risk']}-{x['max_risk']} {x['exit_mode']} buf={x['sl_buffer']}"
        )
    lines.append("")
    lines.append(f"топ-10 при сделках >= {args.min_trades}:")
    for i, x in enumerate(robust[:10], 1):
        lines.append(
            f"  {i:02d} pnl={x['net_pnl']:+8.2f}  n={x['trades']:2d} wr={x['win_rate']:5.1f}%  "
            f"avgR={x['avg_r']:+.2f} dd={x['max_dd']:.1f}  "
            f"{x['side']} RR={x['rr']} hold={x['max_hold']} "
            f"sess={x['session']} trend={x['trend']} body={x['min_body']} "
            f"risk={x['min_risk']}-{x['max_risk']} {x['exit_mode']} buf={x['sl_buffer']}"
        )
    if best:
        winner = Params(
            side=best["side"],
            rr=best["rr"],
            sl_buffer=best["sl_buffer"],
            max_hold=best["max_hold"],
            min_risk=best["min_risk"],
            max_risk=best["max_risk"],
            session=best["session"],
            trend=best["trend"],
            min_body=best["min_body"],
            exit_mode=best["exit_mode"],
            symbol=args.symbol,
        )
        result, trades = run_backtest(bars, params=winner)
        report = write_outputs(bars, result, trades, out)
        lines.append("")
        lines.append("выбранный рабочий вариант (max PnL, min trades):")
        lines.append(json.dumps(asdict(winner), ensure_ascii=False))
        lines.append(f"net={result.net_pnl:+.2f} trades={result.trades} wr={result.win_rate}% dd={result.max_dd}")
        lines.append("")
        lines.append(
            "это подгонка под один месяц — вне выборки цифры будут хуже. "
            "Паттерн Jam не менялся, крутились сторона/RR/фильтры/выход."
        )
        text = "\n".join(lines) + "\n"
        (out / "optimize_report.txt").write_text(text, encoding="utf-8")
        print(text)
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
