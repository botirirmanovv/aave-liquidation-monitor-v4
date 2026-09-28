"""Apply a cash bank to Jam trades (USD per oz → account PnL)."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from xau_jam.backtest import Params, REPORTS, Trade, run_backtest
from xau_jam.data import fetch_yahoo_h1

OZ_PER_LOT = 100.0  # 1.00 lot XAU = 100 oz


@dataclass(slots=True)
class SizedTrade:
    entry_time: str
    side: str
    reason: str
    oz: float
    lots: float
    risk_usd: float
    pnl: float
    equity_after: float
    skipped: str


@dataclass(slots=True)
class BankResult:
    name: str
    start: float
    end: float
    net: float
    max_dd: float
    max_dd_pct: float
    trades_taken: int
    trades_skipped: int
    win: int
    loss: int
    be: int
    notes: str
    path: list[SizedTrade]


def simulate(
    trades: list[Trade],
    *,
    start: float,
    name: str,
    notes: str,
    oz_fn,
) -> BankResult:
    eq = start
    peak = start
    max_dd = 0.0
    path: list[SizedTrade] = []
    taken = skipped = win = loss = be = 0
    for t in trades:
        risk_pts = abs(t.entry - t.stop)
        oz, skip = oz_fn(eq, t, risk_pts)
        if skip or oz <= 0:
            skipped += 1
            path.append(
                SizedTrade(
                    t.entry_time, t.side, t.reason, 0.0, 0.0, 0.0, 0.0, eq, skip or "oz=0"
                )
            )
            continue
        pnl = t.pnl * oz
        eq += pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        taken += 1
        if pnl > 1e-9:
            win += 1
        elif pnl < -1e-9:
            loss += 1
        else:
            be += 1
        path.append(
            SizedTrade(
                t.entry_time,
                t.side,
                t.reason,
                round(oz, 4),
                round(oz / OZ_PER_LOT, 4),
                round(risk_pts * oz, 2),
                round(pnl, 2),
                round(eq, 2),
                "",
            )
        )
    return BankResult(
        name=name,
        start=start,
        end=round(eq, 2),
        net=round(eq - start, 2),
        max_dd=round(max_dd, 2),
        max_dd_pct=round(100.0 * max_dd / peak, 2) if peak else 0.0,
        trades_taken=taken,
        trades_skipped=skipped,
        win=win,
        loss=loss,
        be=be,
        notes=notes,
        path=path,
    )


def oz_pct(risk_pct: float, min_oz: float = 0.0, max_oz: float | None = None):
    def _fn(eq: float, t: Trade, risk_pts: float) -> tuple[float, str]:
        if eq <= 0:
            return 0.0, "bank=0"
        if risk_pts <= 0:
            return 0.0, "risk_pts=0"
        oz = (eq * risk_pct) / risk_pts
        if max_oz is not None:
            oz = min(oz, max_oz)
        if oz < min_oz:
            return 0.0, f"oz<{min_oz}"
        return oz, ""

    return _fn


def oz_fixed(oz: float, skip_if_risk_pct: float | None = None):
    def _fn(eq: float, t: Trade, risk_pts: float) -> tuple[float, str]:
        if eq <= 0:
            return 0.0, "bank=0"
        if skip_if_risk_pct is not None and risk_pts * oz > eq * skip_if_risk_pct:
            return 0.0, f"1oz risk>{skip_if_risk_pct:.0%} bank"
        return oz, ""

    return _fn


def format_bank(results: list[BankResult]) -> str:
    lines = [
        f"Банк ${results[0].start:.0f}",
        "1.00 лот = 100 унций; 0.01 лот = 1 унция",
        "",
    ]
    for r in results:
        pct = 100.0 * r.net / r.start if r.start else 0.0
        lines.append(f"── {r.name} ──")
        lines.append(r.notes)
        lines.append(
            f"старт ${r.start:.0f} → финиш ${r.end:.2f}   "
            f"net {r.net:+.2f} ({pct:+.1f}%)"
        )
        lines.append(
            f"просадка ${r.max_dd:.2f} ({r.max_dd_pct:.1f}% от пика)   "
            f"сделок {r.trades_taken}  пропуск {r.trades_skipped}  "
            f"+{r.win} / −{r.loss} / 0={r.be}"
        )
        for i, t in enumerate(r.path, 1):
            if t.skipped:
                lines.append(f"  {i:02d} {t.entry_time}  ПРОПУСК ({t.skipped})")
                continue
            lines.append(
                f"  {i:02d} {t.entry_time}  {t.side} {t.reason:8}  "
                f"{t.oz:.3f} oz ({t.lots:.4f} lot)  risk=${t.risk_usd:.2f}  "
                f"PnL={t.pnl:+.2f}  eq=${t.equity_after:.2f}"
            )
        lines.append("")
    lines.append(
        "маржу/плечо не считал: при 0.01 лота и золоте ~$4300 на $500 "
        "нужно примерно 1:100, иначе брокер не даст открыть."
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--range", dest="range_spec", default="1mo")
    ap.add_argument("--out-dir", default=str(REPORTS))
    args = ap.parse_args()

    bars = fetch_yahoo_h1(args.symbol, args.range_spec)
    _, trades = run_backtest(bars, params=Params(symbol=args.symbol))
    bank = args.bank
    results = [
        simulate(
            trades,
            start=bank,
            name="1% риска, дробный объём",
            notes="риск 1% от текущего банка / размер стопа в $; лот дробный",
            oz_fn=oz_pct(0.01),
        ),
        simulate(
            trades,
            start=bank,
            name="2% риска, дробный объём",
            notes="риск 2% от текущего банка / размер стопа",
            oz_fn=oz_pct(0.02),
        ),
        simulate(
            trades,
            start=bank,
            name="фикс 0.01 лота (1 унция)",
            notes="как часто открывают с малого счёта; стоп в $ = пункты стопа × 1 oz",
            oz_fn=oz_fixed(1.0),
        ),
    ]
    text = format_bank(results)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"bank_{int(bank)}.txt").write_text(text, encoding="utf-8")
    payload = [
        {**asdict(r), "path": [asdict(t) for t in r.path]} for r in results
    ]
    (out / f"bank_{int(bank)}.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
