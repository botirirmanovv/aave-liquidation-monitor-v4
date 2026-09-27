"""H1 Jam-sell backtest.

Entry: next bar's open after a completed Jam sell (no same-bar fill).
Stop: just above the signal wick high.
Target: entry - rr * (stop - entry).
Same-bar SL+TP: stop first (conservative).
One position at a time. Time stop after `max_hold` H1 bars.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from xau_jam.data import fetch_yahoo_h1, write_csv
from xau_jam.pattern import Bar, is_jam_sell

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"


@dataclass(slots=True)
class Trade:
    signal_time: str
    entry_time: str
    exit_time: str
    entry: float
    stop: float
    target: float
    exit: float
    bars_held: int
    reason: str
    pnl: float
    r_multiple: float
    signal_high: float
    prev_high: float
    prev_low: float
    signal_close: float


@dataclass(slots=True)
class BacktestResult:
    symbol: str
    timeframe: str
    pattern: str
    start: str
    end: str
    bars: int
    signals: int
    trades: int
    wins: int
    losses: int
    timeouts: int
    win_rate: float
    net_pnl: float
    avg_pnl: float
    avg_r: float
    max_dd: float
    profit_factor: float
    rr: float
    spread: float
    sl_buffer: float
    max_hold: int
    notes: str


def detect_signals(bars: list[Bar]) -> list[int]:
    """Return indices of completed Jam-sell bars (need a following bar to enter)."""
    hits: list[int] = []
    for i in range(1, len(bars)):
        if is_jam_sell(bars[i - 1], bars[i]):
            hits.append(i)
    return hits


def _fill_short(
    bars: list[Bar],
    start: int,
    entry: float,
    stop: float,
    target: float,
    max_hold: int,
) -> tuple[int, float, str]:
    last = min(len(bars) - 1, start + max_hold - 1)
    for j in range(start, last + 1):
        bar = bars[j]
        hit_sl = bar.high >= stop
        hit_tp = bar.low <= target
        if hit_sl and hit_tp:
            return j, stop, "sl_before_tp"
        if hit_sl:
            return j, stop, "stop"
        if hit_tp:
            return j, target, "target"
    return last, bars[last].close, "time"


def run_backtest(
    bars: list[Bar],
    *,
    rr: float = 2.0,
    spread: float = 0.30,
    sl_buffer: float = 0.50,
    max_hold: int = 24,
    symbol: str = "GC=F",
) -> tuple[BacktestResult, list[Trade]]:
    if rr <= 0:
        raise ValueError("rr must be > 0")
    signals = detect_signals(bars)
    trades: list[Trade] = []
    busy_until = -1
    for idx in signals:
        entry_i = idx + 1
        if entry_i >= len(bars):
            continue
        if entry_i <= busy_until:
            continue
        signal = bars[idx]
        prev = bars[idx - 1]
        raw_entry = bars[entry_i].open
        entry = raw_entry - spread / 2.0  # short: sell the bid
        stop = signal.high + sl_buffer
        risk = stop - entry
        if risk <= 0:
            continue
        target = entry - rr * risk
        exit_i, exit_px, reason = _fill_short(bars, entry_i, entry, stop, target, max_hold)
        if reason == "target":
            fill = target
        elif reason in {"stop", "sl_before_tp"}:
            fill = stop
        else:
            fill = exit_px + spread / 2.0  # buy back the offer
        pnl = entry - fill
        r_mult = pnl / risk
        trades.append(
            Trade(
                signal_time=signal.time.isoformat(),
                entry_time=bars[entry_i].time.isoformat(),
                exit_time=bars[exit_i].time.isoformat(),
                entry=round(entry, 5),
                stop=round(stop, 5),
                target=round(target, 5),
                exit=round(fill, 5),
                bars_held=exit_i - entry_i + 1,
                reason=reason,
                pnl=round(pnl, 5),
                r_multiple=round(r_mult, 4),
                signal_high=round(signal.high, 5),
                prev_high=round(prev.high, 5),
                prev_low=round(prev.low, 5),
                signal_close=round(signal.close, 5),
            )
        )
        busy_until = exit_i

    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl < 0)
    timeouts = sum(1 for t in trades if t.reason == "time")
    net = sum(t.pnl for t in trades)
    avg = net / len(trades) if trades else 0.0
    avg_r = sum(t.r_multiple for t in trades) / len(trades) if trades else 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    gross_win = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = -sum(t.pnl for t in trades if t.pnl < 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    start = bars[0].time.isoformat() if bars else ""
    end = bars[-1].time.isoformat() if bars else ""
    result = BacktestResult(
        symbol=symbol,
        timeframe="H1",
        pattern="jam_sell",
        start=start,
        end=end,
        bars=len(bars),
        signals=len(signals),
        trades=len(trades),
        wins=wins,
        losses=losses,
        timeouts=timeouts,
        win_rate=round(100.0 * wins / len(trades), 2) if trades else 0.0,
        net_pnl=round(net, 4),
        avg_pnl=round(avg, 4),
        avg_r=round(avg_r, 4),
        max_dd=round(max_dd, 4),
        profit_factor=round(pf, 3) if pf != float("inf") else float("inf"),
        rr=rr,
        spread=spread,
        sl_buffer=sl_buffer,
        max_hold=max_hold,
        notes=(
            "XAU/USD proxied by Yahoo COMEX gold futures GC=F. "
            "Sell-only Jam: bearish bar, wick > prev.high, close < prev.low. "
            "Fill next open; SL first if SL and TP print in the same bar."
        ),
    )
    return result, trades


def write_trades_csv(trades: list[Trade], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "signal_time",
        "entry_time",
        "exit_time",
        "entry",
        "stop",
        "target",
        "exit",
        "bars_held",
        "reason",
        "pnl",
        "r_multiple",
        "signal_high",
        "prev_high",
        "prev_low",
        "signal_close",
    ]
    lines = [",".join(cols)]
    for t in trades:
        d = asdict(t)
        lines.append(",".join(str(d[c]) for c in cols))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_equity_chart(bars: list[Bar], trades: list[Trade], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path.parent.mkdir(parents=True, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=False, gridspec_kw={"height_ratios": [2, 1]})
    ax1.plot([b.time for b in bars], [b.close for b in bars], color="#222", linewidth=0.8, label="Close")
    sold = [t for t in trades]
    if sold:
        ax1.scatter(
            [datetime.fromisoformat(t.signal_time) for t in sold],
            [t.signal_close for t in sold],
            color="#c0392b",
            marker="v",
            s=36,
            zorder=3,
            label="Jam sell",
        )
    ax1.set_title("XAU/USD proxy (GC=F) H1 — Jam sell signals")
    ax1.set_ylabel("USD / oz")
    ax1.grid(True, alpha=0.25)
    ax1.legend(loc="upper left")

    eq = []
    run = 0.0
    xs = []
    for t in trades:
        run += t.pnl
        xs.append(datetime.fromisoformat(t.exit_time))
        eq.append(run)
    ax2.plot(xs, eq, color="#1f4e79", linewidth=1.4)
    ax2.axhline(0.0, color="#888", linewidth=0.6)
    ax2.set_title("Equity (USD per 1 oz, costs = half-spread on entry)")
    ax2.set_ylabel("PnL")
    ax2.grid(True, alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def format_report(result: BacktestResult, trades: list[Trade]) -> str:
    pf = "inf" if result.profit_factor == float("inf") else f"{result.profit_factor:.3f}"
    lines = [
        "XAU/USD H1 — стратегия «Джем» (продажа)",
        f"окно: {result.start} → {result.end}",
        f"символ: {result.symbol}  ТФ: {result.timeframe}  баров: {result.bars}",
        "",
        "правило входа (sell):",
        "  медвежья свеча, фитиль выше high предыдущей, close ниже low предыдущей",
        "исполнение: open следующей свечи; SL = high сигнала + buffer; TP = RR * риск",
        f"параметры: RR={result.rr} spread={result.spread} sl_buffer={result.sl_buffer} max_hold={result.max_hold}",
        "",
        f"сигналов: {result.signals}   сделок: {result.trades}   win={result.wins} loss={result.losses} time={result.timeouts}",
        f"win rate: {result.win_rate:.2f}%",
        f"net PnL: {result.net_pnl:+.2f} USD/oz",
        f"avg PnL: {result.avg_pnl:+.2f}   avg R: {result.avg_r:+.3f}",
        f"max DD: {result.max_dd:.2f}   profit factor: {pf}",
        "",
        "сделки:",
    ]
    if not trades:
        lines.append("  нет")
    for i, t in enumerate(trades, 1):
        lines.append(
            f"  {i:02d} {t.entry_time} → {t.exit_time}  "
            f"in={t.entry:.2f} out={t.exit:.2f}  {t.reason:12}  "
            f"PnL={t.pnl:+.2f}  R={t.r_multiple:+.2f}  hold={t.bars_held}h"
        )
    lines.append("")
    lines.append(result.notes)
    lines.append(f"сгенерировано {datetime.now(timezone.utc).isoformat()}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest XAU/USD H1 Jam sell")
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--range", dest="range_spec", default="1mo")
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--spread", type=float, default=0.30)
    ap.add_argument("--sl-buffer", type=float, default=0.50)
    ap.add_argument("--max-hold", type=int, default=24)
    ap.add_argument("--out-dir", default=str(REPORTS))
    args = ap.parse_args()

    bars = fetch_yahoo_h1(args.symbol, args.range_spec)
    result, trades = run_backtest(
        bars,
        rr=args.rr,
        spread=args.spread,
        sl_buffer=args.sl_buffer,
        max_hold=args.max_hold,
        symbol=args.symbol,
    )
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(bars, out / "xau_h1.csv")
    write_trades_csv(trades, out / "trades.csv")
    (out / "summary.json").write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    report = format_report(result, trades)
    (out / "backtest_report.txt").write_text(report, encoding="utf-8")
    write_equity_chart(bars, trades, out / "equity.png")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
