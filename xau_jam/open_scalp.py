"""Automated daily-open spike scalp (скальп на открытии дня).

Wait for the first jump after a session open, pile clips in that direction
(or fade it), then flatten quickly on time / stop / target.

  python3 -m xau_jam.open_scalp --bank 500
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Literal

from xau_jam.bank import format_bank, oz_fixed, oz_pct, simulate
from xau_jam.backtest import Trade
from xau_jam.data import fetch_yahoo, write_csv
from xau_jam.pattern import Bar

REPORTS = Path(__file__).resolve().parent / "reports"
Style = Literal["momentum", "fade"]


SESSIONS = {
    "daily": time(0, 0),  # midnight UTC daily candle
    "london": time(7, 0),
    "ny": time(13, 0),
    "cme": time(22, 0),  # COMEX/Globex reopen after the daily halt
}


@dataclass(slots=True)
class OpenParams:
    session: str = "cme"
    style: Style = "momentum"
    min_spike: float = 6.0
    orb_bars: int = 6  # 30m of 5m bars
    hold_bars: int = 6  # flatten after ~30m
    sl_buffer: float = 1.0
    rr: float = 1.0
    clips: int = 3
    clip_step: float = 3.0
    spread: float = 0.20
    symbol: str = "GC=F"
    interval: str = "5m"


@dataclass(slots=True)
class OpenResult:
    params: dict
    start: str
    end: str
    bars: int
    days: int
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


def _session_opens(bars: list[Bar], session_tod: time) -> list[int]:
    """Index of the first bar at/after session time on each UTC date."""
    opens: list[int] = []
    seen: set = set()
    for i, b in enumerate(bars):
        d = b.time.date()
        if d in seen:
            continue
        tod = time(b.time.hour, b.time.minute)
        if tod < session_tod:
            continue
        # skip if the first qualifying bar is too late (session already gone)
        latest = time((session_tod.hour + 2) % 24, session_tod.minute)
        wrapped = session_tod.hour + 2 >= 24
        if not wrapped and tod > latest:
            continue
        seen.add(d)
        opens.append(i)
    return opens


def _fill_window(
    bars: list[Bar],
    start: int,
    last: int,
    side: str,
    stop: float,
    target: float,
) -> tuple[int, float, str]:
    for j in range(start, last + 1):
        bar = bars[j]
        if side == "buy":
            hit_sl = bar.low <= stop
            hit_tp = bar.high >= target
        else:
            hit_sl = bar.high >= stop
            hit_tp = bar.low <= target
        if hit_sl and hit_tp:
            return j, stop, "sl_before_tp"
        if hit_sl:
            return j, stop, "stop"
        if hit_tp:
            return j, target, "target"
    return last, bars[last].close, "time"


def run_open_scalp(bars: list[Bar], p: OpenParams) -> tuple[OpenResult, list[Trade]]:
    if p.session not in SESSIONS:
        raise ValueError(f"unknown session {p.session}")
    if p.min_spike <= 0 or p.hold_bars < 1 or p.orb_bars < 1:
        raise ValueError("bad window / spike")
    session_tod = SESSIONS[p.session]
    day_opens = _session_opens(bars, session_tod)
    trades: list[Trade] = []
    signals = 0
    busy_until = -1

    for oi in day_opens:
        if oi <= busy_until:
            continue
        session_px = bars[oi].open
        trigger_i: int | None = None
        side: str | None = None
        last_orb = min(len(bars) - 1, oi + p.orb_bars - 1)
        for i in range(oi, last_orb + 1):
            up = bars[i].high - session_px
            dn = session_px - bars[i].low
            if up < p.min_spike and dn < p.min_spike:
                continue
            raw = "buy" if up >= dn else "sell"
            side = raw if p.style == "momentum" else ("sell" if raw == "buy" else "buy")
            trigger_i = i
            break
        if trigger_i is None or side is None:
            continue
        signals += 1
        entry_i = trigger_i + 1
        if entry_i >= len(bars) or entry_i <= busy_until:
            continue

        entries: list[float] = []
        last_fill = bars[entry_i].open
        slip = p.spread / 2.0
        first = last_fill + slip if side == "buy" else last_fill - slip
        entries.append(first)
        last_ext = first

        stop = session_px - p.sl_buffer if side == "buy" else session_px + p.sl_buffer
        hold_last = min(len(bars) - 1, entry_i + p.hold_bars - 1)

        # extra clips while the spike keeps running, still inside the hold window
        for k in range(entry_i + 1, hold_last + 1):
            if len(entries) >= p.clips:
                break
            bar = bars[k]
            if side == "buy" and bar.high >= last_ext + p.clip_step:
                if k + 1 <= hold_last:
                    px = bars[k + 1].open + slip
                    entries.append(px)
                    last_ext = px
            elif side == "sell" and bar.low <= last_ext - p.clip_step:
                if k + 1 <= hold_last:
                    px = bars[k + 1].open - slip
                    entries.append(px)
                    last_ext = px

        avg = sum(entries) / len(entries)
        risk = abs(avg - stop)
        if risk <= 0:
            continue
        target = avg + p.rr * risk if side == "buy" else avg - p.rr * risk
        exit_i, exit_raw, reason = _fill_window(bars, entry_i, hold_last, side, stop, target)
        if reason == "target":
            fill = target
        elif reason in {"stop", "sl_before_tp"}:
            fill = stop
        else:
            fill = exit_raw + slip if side == "sell" else exit_raw - slip
        pnl = sum((fill - e) if side == "buy" else (e - fill) for e in entries)
        r_mult = pnl / (risk * len(entries))
        trades.append(
            Trade(
                side=side,
                signal_time=bars[trigger_i].time.isoformat(),
                entry_time=bars[entry_i].time.isoformat(),
                exit_time=bars[exit_i].time.isoformat(),
                entry=round(avg, 5),
                stop=round(stop, 5),
                target=round(target, 5),
                exit=round(fill, 5),
                bars_held=exit_i - entry_i + 1,
                reason=reason,
                pnl=round(pnl, 5),
                r_multiple=round(r_mult, 4),
                signal_high=round(bars[trigger_i].high, 5),
                prev_high=round(session_px, 5),
                prev_low=round(float(len(entries)), 5),
                signal_close=round(bars[trigger_i].close, 5),
            )
        )
        busy_until = exit_i

    return _summarize(bars, trades, signals, p, len(day_opens)), trades


def _summarize(
    bars: list[Bar],
    trades: list[Trade],
    signals: int,
    p: OpenParams,
    days: int,
) -> OpenResult:
    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl < 0)
    timeouts = sum(1 for t in trades if t.reason == "time")
    net = sum(t.pnl for t in trades)
    avg = net / len(trades) if trades else 0.0
    avg_r = sum(t.r_multiple for t in trades) / len(trades) if trades else 0.0
    eq = peak = max_dd = 0.0
    for t in trades:
        eq += t.pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    gw = sum(t.pnl for t in trades if t.pnl > 0)
    gl = -sum(t.pnl for t in trades if t.pnl < 0)
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    return OpenResult(
        params=asdict(p),
        start=bars[0].time.isoformat() if bars else "",
        end=bars[-1].time.isoformat() if bars else "",
        bars=len(bars),
        days=days,
        signals=signals,
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
    )


def grid() -> list[OpenParams]:
    out: list[OpenParams] = []
    for combo in itertools.product(
        ("daily", "london", "ny", "cme"),
        ("momentum", "fade"),
        (4.0, 8.0, 12.0),
        (3, 6, 12),
        (0.8, 1.5),
        (1, 3),
    ):
        sess, style, spike, hold, rr, clips = combo
        out.append(
            OpenParams(
                session=sess,
                style=style,
                min_spike=spike,
                hold_bars=hold,
                rr=rr,
                clips=clips,
                clip_step=max(2.0, spike / 2.0),
                orb_bars=6,
            )
        )
    return out


def format_open_report(r: OpenResult, trades: list[Trade], title: str) -> str:
    pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.3f}"
    lines = [
        title,
        f"окно: {r.start} → {r.end}  баров={r.bars}  сессий={r.days}",
        f"параметры: {json.dumps(r.params, ensure_ascii=False)}",
        "",
        "правило: ждём скачок от цены открытия сессии ≥ min_spike, "
        "набираем клипы по ходу (momentum) или против (fade), "
        "закрываем по времени / стопу за открытием / RR.",
        "",
        f"сигналов={r.signals} сделок={r.trades} win={r.wins} loss={r.losses} time={r.timeouts}",
        f"win rate={r.win_rate:.1f}%  net={r.net_pnl:+.2f} USD (сумма клипов)  "
        f"avgR={r.avg_r:+.3f}  DD={r.max_dd:.2f}  PF={pf}",
        "",
        "сделки:",
    ]
    if not trades:
        lines.append("  нет")
    for i, t in enumerate(trades, 1):
        lines.append(
            f"  {i:02d} {t.side:4} {t.entry_time} → {t.exit_time}  {t.reason:12}  "
            f"in={t.entry:.2f} out={t.exit:.2f}  PnL={t.pnl:+.2f}  "
            f"R={t.r_multiple:+.2f}  clips={int(t.prev_low)}  hold={t.bars_held}"
        )
    lines.append(f"сгенерировано {datetime.now(timezone.utc).isoformat()}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Automated XAU daily-open scalp backtest")
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--range", dest="range_spec", default="1mo")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--min-trades", type=int, default=8)
    ap.add_argument("--out-dir", default=str(REPORTS))
    args = ap.parse_args()

    bars = fetch_yahoo(args.symbol, args.range_spec, args.interval)
    configs = grid()
    print(f"open-scalp bars={len(bars)} {bars[0].time} -> {bars[-1].time} grid={len(configs)}", flush=True)
    scored: list[tuple[OpenResult, OpenParams]] = []
    for i, p in enumerate(configs, 1):
        p.symbol = args.symbol
        p.interval = args.interval
        r, _ = run_open_scalp(bars, p)
        scored.append((r, p))
        if i % 80 == 0:
            print(f"  ... {i}/{len(configs)}", flush=True)

    scored.sort(key=lambda x: (x[0].net_pnl, x[0].avg_r, -x[0].max_dd), reverse=True)
    robust = [x for x in scored if x[0].trades >= args.min_trades]
    robust.sort(key=lambda x: (x[0].net_pnl, x[0].avg_r, -x[0].max_dd), reverse=True)
    winner_r, winner_p = (robust[0] if robust else scored[0])

    result, trades = run_open_scalp(bars, winner_p)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(bars, out / "open_scalp_5m.csv")
    report = format_open_report(result, trades, "XAU open-scalp — авто, лучший вариант сетки")
    (out / "open_scalp_report.txt").write_text(report, encoding="utf-8")
    (out / "open_scalp_summary.json").write_text(
        json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8"
    )

    top_lines = [
        "скальп на открытии дня — перебор вариантов",
        f"баров {len(bars)}  сетка {len(configs)}  мин. сделок {args.min_trades}",
        "",
        "топ-10 (сделок >= min):",
    ]
    for i, (r, p) in enumerate((robust or scored)[:10], 1):
        top_lines.append(
            f"  {i:02d} pnl={r.net_pnl:+8.2f} n={r.trades:2d} wr={r.win_rate:5.1f}% "
            f"avgR={r.avg_r:+.2f} dd={r.max_dd:.1f}  "
            f"{p.session}/{p.style} spike={p.min_spike} hold={p.hold_bars} "
            f"RR={p.rr} clips={p.clips}"
        )
    top_lines.append("")
    top_lines.append("победитель: " + json.dumps(asdict(winner_p), ensure_ascii=False))
    top_text = "\n".join(top_lines) + "\n"
    (out / "open_scalp_optimize.txt").write_text(top_text, encoding="utf-8")

    b1 = simulate(
        trades,
        start=args.bank,
        name="open-scalp 1% риска",
        notes="1% банка / стоп от цены открытия сессии",
        oz_fn=oz_pct(0.01),
    )
    b2 = simulate(
        trades,
        start=args.bank,
        name="open-scalp фикс 0.01 лота",
        notes="1 унция на каждый клип-пакет",
        oz_fn=oz_fixed(1.0),
    )
    bank_txt = format_bank([b1, b2])
    (out / f"open_scalp_bank_{int(args.bank)}.txt").write_text(bank_txt, encoding="utf-8")

    print(top_text)
    print(report, end="")
    print(bank_txt, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
