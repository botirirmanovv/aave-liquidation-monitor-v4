"""H1 Jam backtest (sell / buy / both) with filters and BE exit.

Entry: next bar open after a completed Jam (no same-bar fill).
Wick stop + optional buffer. Target = RR * risk.
Same-bar SL+TP: stop first (conservative).
One position at a time.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from xau_jam.data import fetch_yahoo_h1, write_csv
from xau_jam.pattern import Bar, is_jam_buy, is_jam_sell

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"

Side = Literal["sell", "buy", "both"]
Session = Literal["all", "asia", "london", "ny"]
Trend = Literal["none", "sma20", "sma50"]
ExitMode = Literal["fixed", "be_after_1r"]


@dataclass(slots=True)
class Trade:
    side: str
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
class Params:
    # Defaults = 1-month grid winner (2026-08-27..2026-09-25, 62208 variants).
    side: Side = "sell"
    rr: float = 3.0
    spread: float = 0.30
    sl_buffer: float = 1.50
    max_hold: int = 24
    min_risk: float = 0.0
    max_risk: float = 999.0
    session: Session = "all"
    trend: Trend = "sma20"
    min_body: float = 0.0
    exit_mode: ExitMode = "be_after_1r"
    symbol: str = "GC=F"


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
    params: dict = field(default_factory=dict)


def detect_signals(bars: list[Bar]) -> list[int]:
    """Sell-only indices — kept for existing unit tests."""
    return [i for i in range(1, len(bars)) if is_jam_sell(bars[i - 1], bars[i])]


def _sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if n <= 0:
        return out
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= n:
            run -= values[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def _in_session(bar: Bar, session: Session) -> bool:
    hour = bar.time.hour
    if session == "all":
        return True
    if session == "asia":
        return 0 <= hour < 8
    if session == "london":
        return 7 <= hour < 16
    if session == "ny":
        return 13 <= hour < 21
    return True


def _body_frac(bar: Bar) -> float:
    rng = bar.high - bar.low
    if rng <= 0:
        return 0.0
    return abs(bar.close - bar.open) / rng


def _signal_side(prev: Bar, curr: Bar, side: Side) -> str | None:
    sell = is_jam_sell(prev, curr)
    buy = is_jam_buy(prev, curr)
    if side == "sell":
        return "sell" if sell else None
    if side == "buy":
        return "buy" if buy else None
    if sell:
        return "sell"
    if buy:
        return "buy"
    return None


def _ok_trend(close: float, sma: float | None, trade_side: str, trend: Trend) -> bool:
    if trend == "none":
        return True
    if sma is None:
        return False
    if trade_side == "sell":
        return close < sma
    return close > sma


def _fill_short(
    bars: list[Bar],
    start: int,
    entry: float,
    stop: float,
    target: float,
    max_hold: int,
    *,
    be_after_1r: bool,
) -> tuple[int, float, str]:
    last = min(len(bars) - 1, start + max_hold - 1)
    live_stop = stop
    one_r = entry - (stop - entry)
    armed = False
    for j in range(start, last + 1):
        bar = bars[j]
        if (not armed) and bar.high >= stop:
            hit_tp = bar.low <= target
            if hit_tp:
                return j, stop, "sl_before_tp"
            return j, stop, "stop"
        if be_after_1r and not armed and bar.low <= one_r:
            live_stop = entry
            armed = True
        hit_sl = bar.high >= live_stop
        hit_tp = bar.low <= target
        if hit_sl and hit_tp:
            return j, live_stop, "sl_before_tp"
        if hit_sl:
            return j, live_stop, "be" if armed else "stop"
        if hit_tp:
            return j, target, "target"
    return last, bars[last].close, "time"


def _fill_long(
    bars: list[Bar],
    start: int,
    entry: float,
    stop: float,
    target: float,
    max_hold: int,
    *,
    be_after_1r: bool,
) -> tuple[int, float, str]:
    last = min(len(bars) - 1, start + max_hold - 1)
    live_stop = stop
    one_r = entry + (entry - stop)
    armed = False
    for j in range(start, last + 1):
        bar = bars[j]
        if (not armed) and bar.low <= stop:
            hit_tp = bar.high >= target
            if hit_tp:
                return j, stop, "sl_before_tp"
            return j, stop, "stop"
        if be_after_1r and not armed and bar.high >= one_r:
            live_stop = entry
            armed = True
        hit_sl = bar.low <= live_stop
        hit_tp = bar.high >= target
        if hit_sl and hit_tp:
            return j, live_stop, "sl_before_tp"
        if hit_sl:
            return j, live_stop, "be" if armed else "stop"
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
    side: Side = "sell",
    min_risk: float = 0.0,
    max_risk: float = 999.0,
    session: Session = "all",
    trend: Trend = "none",
    min_body: float = 0.0,
    exit_mode: ExitMode = "fixed",
    params: Params | None = None,
) -> tuple[BacktestResult, list[Trade]]:
    p = params or Params(
        side=side,
        rr=rr,
        spread=spread,
        sl_buffer=sl_buffer,
        max_hold=max_hold,
        min_risk=min_risk,
        max_risk=max_risk,
        session=session,
        trend=trend,
        min_body=min_body,
        exit_mode=exit_mode,
        symbol=symbol,
    )
    if p.rr <= 0:
        raise ValueError("rr must be > 0")
    sma_n = 20 if p.trend == "sma20" else 50 if p.trend == "sma50" else 0
    closes = [b.close for b in bars]
    sma = _sma(closes, sma_n) if sma_n else [None] * len(bars)
    be = p.exit_mode == "be_after_1r"

    trades: list[Trade] = []
    signal_n = 0
    busy_until = -1
    for i in range(1, len(bars)):
        prev, curr = bars[i - 1], bars[i]
        trade_side = _signal_side(prev, curr, p.side)
        if trade_side is None:
            continue
        signal_n += 1
        if not _in_session(curr, p.session):
            continue
        if _body_frac(curr) < p.min_body:
            continue
        if not _ok_trend(curr.close, sma[i], trade_side, p.trend):
            continue
        entry_i = i + 1
        if entry_i >= len(bars) or entry_i <= busy_until:
            continue
        raw_open = bars[entry_i].open
        if trade_side == "sell":
            entry = raw_open - p.spread / 2.0
            stop = curr.high + p.sl_buffer
            risk = stop - entry
            if risk <= 0 or risk < p.min_risk or risk > p.max_risk:
                continue
            target = entry - p.rr * risk
            exit_i, exit_px, reason = _fill_short(
                bars, entry_i, entry, stop, target, p.max_hold, be_after_1r=be
            )
            if reason == "target":
                fill = target
            elif reason in {"stop", "sl_before_tp", "be"}:
                fill = exit_px
            else:
                fill = exit_px + p.spread / 2.0
            pnl = entry - fill
        else:
            entry = raw_open + p.spread / 2.0
            stop = curr.low - p.sl_buffer
            risk = entry - stop
            if risk <= 0 or risk < p.min_risk or risk > p.max_risk:
                continue
            target = entry + p.rr * risk
            exit_i, exit_px, reason = _fill_long(
                bars, entry_i, entry, stop, target, p.max_hold, be_after_1r=be
            )
            if reason == "target":
                fill = target
            elif reason in {"stop", "sl_before_tp", "be"}:
                fill = exit_px
            else:
                fill = exit_px - p.spread / 2.0
            pnl = fill - entry
        trades.append(
            Trade(
                side=trade_side,
                signal_time=curr.time.isoformat(),
                entry_time=bars[entry_i].time.isoformat(),
                exit_time=bars[exit_i].time.isoformat(),
                entry=round(entry, 5),
                stop=round(stop, 5),
                target=round(target, 5),
                exit=round(fill, 5),
                bars_held=exit_i - entry_i + 1,
                reason=reason,
                pnl=round(pnl, 5),
                r_multiple=round(pnl / risk, 4),
                signal_high=round(curr.high, 5),
                prev_high=round(prev.high, 5),
                prev_low=round(prev.low, 5),
                signal_close=round(curr.close, 5),
            )
        )
        busy_until = exit_i

    return _summarize(bars, trades, signal_n, p), trades


def _summarize(
    bars: list[Bar], trades: list[Trade], signal_n: int, p: Params
) -> BacktestResult:
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
    return BacktestResult(
        symbol=p.symbol,
        timeframe="H1",
        pattern=f"jam_{p.side}",
        start=bars[0].time.isoformat() if bars else "",
        end=bars[-1].time.isoformat() if bars else "",
        bars=len(bars),
        signals=signal_n,
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
        rr=p.rr,
        spread=p.spread,
        sl_buffer=p.sl_buffer,
        max_hold=p.max_hold,
        notes=(
            f"Jam {p.side}; session={p.session} trend={p.trend} "
            f"min_body={p.min_body} risk=[{p.min_risk},{p.max_risk}] "
            f"exit={p.exit_mode}. GC=F H1 proxy. SL-first if SL+TP same bar."
        ),
        params=asdict(p),
    )


def write_trades_csv(trades: list[Trade], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "side",
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
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), gridspec_kw={"height_ratios": [2, 1]})
    times = [b.time for b in bars]
    closes = [b.close for b in bars]
    xs = [times[0]]
    ys = [closes[0]]
    for i in range(1, len(times)):
        gap_h = (times[i] - times[i - 1]).total_seconds() / 3600.0
        if gap_h > 6:
            xs.append(times[i])
            ys.append(float("nan"))
        xs.append(times[i])
        ys.append(closes[i])
    ax1.plot(xs, ys, color="#222", linewidth=0.8, label="Close")
    sells = [t for t in trades if t.side == "sell"]
    buys = [t for t in trades if t.side == "buy"]
    if sells:
        ax1.scatter(
            [datetime.fromisoformat(t.signal_time) for t in sells],
            [t.signal_close for t in sells],
            color="#c0392b",
            marker="v",
            s=36,
            zorder=3,
            label="Jam sell",
        )
    if buys:
        ax1.scatter(
            [datetime.fromisoformat(t.signal_time) for t in buys],
            [t.signal_close for t in buys],
            color="#1e8449",
            marker="^",
            s=36,
            zorder=3,
            label="Jam buy",
        )
    ax1.set_title("XAU/USD proxy GC=F H1 - Jam signals")
    ax1.set_ylabel("USD / oz")
    ax1.grid(True, alpha=0.25)
    ax1.legend(loc="upper left")

    eq = []
    run = 0.0
    xs_eq = []
    for t in trades:
        run += t.pnl
        xs_eq.append(datetime.fromisoformat(t.exit_time))
        eq.append(run)
    ax2.plot(xs_eq, eq, color="#1f4e79", linewidth=1.4)
    ax2.axhline(0.0, color="#888", linewidth=0.6)
    ax2.set_title("Equity USD per 1 oz")
    ax2.set_ylabel("PnL")
    ax2.grid(True, alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def format_report(result: BacktestResult, trades: list[Trade]) -> str:
    pf = "inf" if result.profit_factor == float("inf") else f"{result.profit_factor:.3f}"
    p = result.params or {}
    lines = [
        f"XAU/USD H1 — Jam {p.get('side', result.pattern)}",
        f"окно: {result.start} → {result.end}",
        f"символ: {result.symbol}  ТФ: {result.timeframe}  баров: {result.bars}",
        "",
        "Jam sell: медвежья, wick > prev.high, close < prev.low",
        "Jam buy:  бычья, wick < prev.low, close > prev.high",
        f"параметры: {json.dumps(p, ensure_ascii=False)}",
        "",
        f"сигналов: {result.signals}   сделок: {result.trades}   "
        f"win={result.wins} loss={result.losses} time={result.timeouts}",
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
            f"  {i:02d} {t.side:4} {t.entry_time} → {t.exit_time}  "
            f"in={t.entry:.2f} out={t.exit:.2f}  {t.reason:12}  "
            f"PnL={t.pnl:+.2f}  R={t.r_multiple:+.2f}  hold={t.bars_held}h"
        )
    lines.append("")
    lines.append(result.notes)
    lines.append(f"сгенерировано {datetime.now(timezone.utc).isoformat()}")
    return "\n".join(lines) + "\n"


def write_outputs(
    bars: list[Bar],
    result: BacktestResult,
    trades: list[Trade],
    out: Path,
) -> str:
    out.mkdir(parents=True, exist_ok=True)
    write_csv(bars, out / "xau_h1.csv")
    write_trades_csv(trades, out / "trades.csv")
    (out / "summary.json").write_text(
        json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8"
    )
    report = format_report(result, trades)
    (out / "backtest_report.txt").write_text(report, encoding="utf-8")
    write_equity_chart(bars, trades, out / "equity.png")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest XAU/USD H1 Jam")
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--range", dest="range_spec", default="1mo")
    ap.add_argument("--side", default="sell", choices=("sell", "buy", "both"))
    ap.add_argument("--rr", type=float, default=3.0)
    ap.add_argument("--spread", type=float, default=0.30)
    ap.add_argument("--sl-buffer", type=float, default=1.50)
    ap.add_argument("--max-hold", type=int, default=24)
    ap.add_argument("--min-risk", type=float, default=0.0)
    ap.add_argument("--max-risk", type=float, default=999.0)
    ap.add_argument("--session", default="all", choices=("all", "asia", "london", "ny"))
    ap.add_argument("--trend", default="sma20", choices=("none", "sma20", "sma50"))
    ap.add_argument("--min-body", type=float, default=0.0)
    ap.add_argument("--exit-mode", default="be_after_1r", choices=("fixed", "be_after_1r"))
    ap.add_argument("--out-dir", default=str(REPORTS))
    args = ap.parse_args()

    bars = fetch_yahoo_h1(args.symbol, args.range_spec)
    params = Params(
        side=args.side,
        rr=args.rr,
        spread=args.spread,
        sl_buffer=args.sl_buffer,
        max_hold=args.max_hold,
        min_risk=args.min_risk,
        max_risk=args.max_risk,
        session=args.session,
        trend=args.trend,
        min_body=args.min_body,
        exit_mode=args.exit_mode,
        symbol=args.symbol,
    )
    result, trades = run_backtest(bars, params=params)
    report = write_outputs(bars, result, trades, Path(args.out_dir))
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
