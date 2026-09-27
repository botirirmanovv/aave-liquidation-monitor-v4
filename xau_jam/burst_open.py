"""Burst open scalp — the 'spam opens then spam closes' at the session jump.

What it models (what you saw):
  At the cash/session open the first impulse prints.
  Trader dumps a stack of tiny market/stop clips in that direction
  (straddle: buy-stop AND sell-stop; first side that rips wins).
  A few minutes later they flatten the whole stack at once.

Not one position. Many clips in, many clips out.

  python3 -m xau_jam.burst_open --bank 500
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Literal

from xau_jam.bank import format_bank, oz_fixed, simulate
from xau_jam.backtest import Trade
from xau_jam.data import fetch_yahoo, write_csv
from xau_jam.leverage import run_fixed_lot
from xau_jam.open_scalp import SESSIONS, _session_opens
from xau_jam.pattern import Bar

REPORTS = Path(__file__).resolve().parent / "reports"
Model = Literal["straddle", "spray"]


@dataclass(slots=True)
class BurstParams:
    session: str = "ny"
    model: Model = "straddle"
    trigger: float = 6.0
    layers: int = 8
    step: float = 2.0
    hold_bars: int = 4
    spread: float = 0.20
    fail_through_open: bool = True
    symbol: str = "GC=F"
    interval: str = "5m"


@dataclass(slots=True)
class BurstResult:
    params: dict
    start: str
    end: str
    bars: int
    days: int
    sessions_traded: int
    clips: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    avg_clip: float
    max_dd: float
    profit_factor: float


def _flatten_px(bar: Bar, side: str, spread: float) -> float:
    slip = spread / 2.0
    return bar.close - slip if side == "buy" else bar.close + slip


def _straddle_day(
    bars: list[Bar],
    oi: int,
    p: BurstParams,
) -> list[Trade]:
    session_px = bars[oi].open
    last = min(len(bars) - 1, oi + p.hold_bars + p.layers + 4)
    buys = [session_px + p.trigger + i * p.step for i in range(p.layers)]
    sells = [session_px - p.trigger - i * p.step for i in range(p.layers)]
    side: str | None = None
    filled: list[tuple[int, float]] = []
    first_i: int | None = None

    for i in range(oi, last + 1):
        bar = bars[i]
        if side is None:
            hit_b = bar.high >= buys[0]
            hit_s = bar.low <= sells[0]
            if hit_b and hit_s:
                return []  # same-bar both sides — skip, no crystal ball
            if hit_b:
                side = "buy"
                first_i = i
            elif hit_s:
                side = "sell"
                first_i = i
            else:
                continue
        assert side is not None and first_i is not None
        levels = buys if side == "buy" else sells
        have = {round(px, 5) for _, px in filled}
        for lv in levels:
            fill_px = lv + p.spread / 2.0 if side == "buy" else lv - p.spread / 2.0
            if round(fill_px, 5) in have:
                continue
            if side == "buy" and bar.high >= lv:
                filled.append((i, fill_px))
                have.add(round(fill_px, 5))
            elif side == "sell" and bar.low <= lv:
                filled.append((i, fill_px))
                have.add(round(fill_px, 5))
        hold_last = min(len(bars) - 1, first_i + p.hold_bars)
        if p.fail_through_open:
            if side == "buy" and bar.low <= session_px:
                return _clips_to_trades(filled, side, i, session_px, "fail_open", session_px)
            if side == "sell" and bar.high >= session_px:
                return _clips_to_trades(filled, side, i, session_px, "fail_open", session_px)
        if i >= hold_last and filled:
            px = _flatten_px(bar, side, p.spread)
            return _clips_to_trades(filled, side, i, px, "time", session_px)
    if filled and side is not None:
        px = _flatten_px(bars[last], side, p.spread)
        return _clips_to_trades(filled, side, last, px, "time", session_px)
    return []


def _spray_day(bars: list[Bar], oi: int, p: BurstParams) -> list[Trade]:
    session_px = bars[oi].open
    last_orb = min(len(bars) - 1, oi + max(2, p.hold_bars))
    side: str | None = None
    trig_i: int | None = None
    for i in range(oi, last_orb + 1):
        up = bars[i].high - session_px
        dn = session_px - bars[i].low
        if up < p.trigger and dn < p.trigger:
            continue
        side = "buy" if up >= dn else "sell"
        trig_i = i
        break
    if side is None or trig_i is None:
        return []
    filled: list[tuple[int, float]] = []
    levels = []
    if side == "buy":
        levels = [session_px + p.trigger + i * p.step for i in range(p.layers)]
    else:
        levels = [session_px - p.trigger - i * p.step for i in range(p.layers)]
    last = min(len(bars) - 1, trig_i + p.hold_bars)
    for i in range(trig_i, last + 1):
        bar = bars[i]
        have = {round(px, 5) for _, px in filled}
        for lv in levels:
            fill_px = lv + p.spread / 2.0 if side == "buy" else lv - p.spread / 2.0
            if round(fill_px, 5) in have:
                continue
            if side == "buy" and bar.high >= lv:
                filled.append((i, fill_px))
                have.add(round(fill_px, 5))
            elif side == "sell" and bar.low <= lv:
                filled.append((i, fill_px))
                have.add(round(fill_px, 5))
        if p.fail_through_open:
            if side == "buy" and bar.low <= session_px:
                return _clips_to_trades(filled, side, i, session_px, "fail_open", session_px)
            if side == "sell" and bar.high >= session_px:
                return _clips_to_trades(filled, side, i, session_px, "fail_open", session_px)
        if i >= last and filled:
            px = _flatten_px(bar, side, p.spread)
            return _clips_to_trades(filled, side, i, px, "time", session_px)
    return []


def _clips_to_trades(
    filled: list[tuple[int, float]],
    side: str,
    exit_i: int,
    exit_px: float,
    reason: str,
    session_px: float,
) -> list[Trade]:
    out: list[Trade] = []
    # exit_i is a bar index; we need times from caller — stored after wrap
    return [
        (fi, fpx, side, exit_i, exit_px, reason, session_px)  # type: ignore[misc]
        for fi, fpx in filled
    ]


def _materialize(raw, bars: list[Bar]) -> list[Trade]:
    trades: list[Trade] = []
    for item in raw:
        fi, fpx, side, exit_i, exit_px, reason, session_px = item
        pnl = (exit_px - fpx) if side == "buy" else (fpx - exit_px)
        risk = abs(fpx - session_px) or 1.0
        trades.append(
            Trade(
                side=side,
                signal_time=bars[fi].time.isoformat(),
                entry_time=bars[fi].time.isoformat(),
                exit_time=bars[exit_i].time.isoformat(),
                entry=round(fpx, 5),
                stop=round(session_px, 5),
                target=round(exit_px, 5),
                exit=round(exit_px, 5),
                bars_held=max(1, exit_i - fi + 1),
                reason=reason,
                pnl=round(pnl, 5),
                r_multiple=round(pnl / risk, 4),
                signal_high=round(bars[fi].high, 5),
                prev_high=round(session_px, 5),
                prev_low=1.0,
                signal_close=round(bars[fi].close, 5),
            )
        )
    return trades


def run_burst(bars: list[Bar], p: BurstParams) -> tuple[BurstResult, list[Trade]]:
    if p.session not in SESSIONS:
        raise ValueError(p.session)
    opens = _session_opens(bars, SESSIONS[p.session])
    trades: list[Trade] = []
    days_hit = 0
    for oi in opens:
        raw = _straddle_day(bars, oi, p) if p.model == "straddle" else _spray_day(bars, oi, p)
        clips = _materialize(raw, bars)
        if clips:
            days_hit += 1
            trades.extend(clips)
    return _summarize(bars, trades, p, len(opens), days_hit), trades


def _summarize(
    bars: list[Bar], trades: list[Trade], p: BurstParams, days: int, hit: int
) -> BurstResult:
    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl < 0)
    net = sum(t.pnl for t in trades)
    eq = peak = dd = 0.0
    for t in trades:
        eq += t.pnl
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    gw = sum(t.pnl for t in trades if t.pnl > 0)
    gl = -sum(t.pnl for t in trades if t.pnl < 0)
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    return BurstResult(
        params=asdict(p),
        start=bars[0].time.isoformat() if bars else "",
        end=bars[-1].time.isoformat() if bars else "",
        bars=len(bars),
        days=days,
        sessions_traded=hit,
        clips=len(trades),
        wins=wins,
        losses=losses,
        win_rate=round(100.0 * wins / len(trades), 2) if trades else 0.0,
        net_pnl=round(net, 4),
        avg_clip=round(net / len(trades), 4) if trades else 0.0,
        max_dd=round(dd, 4),
        profit_factor=round(pf, 3) if pf != float("inf") else float("inf"),
    )


def grid() -> list[BurstParams]:
    out: list[BurstParams] = []
    for combo in itertools.product(
        ("ny", "london", "cme"),
        ("straddle", "spray"),
        (4.0, 8.0, 12.0),
        (5, 10),
        (2, 4, 6),
        (True, False),
    ):
        sess, model, trig, layers, hold, fail = combo
        out.append(
            BurstParams(
                session=sess,
                model=model,
                trigger=trig,
                layers=layers,
                step=2.0,
                hold_bars=hold,
                fail_through_open=fail,
            )
        )
    return out


def format_burst(r: BurstResult, trades: list[Trade]) -> str:
    pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.3f}"
    lines = [
        "Burst open — пачка входов на скачке и пачка закрытий",
        f"окно: {r.start} → {r.end}  баров={r.bars}",
        f"параметры: {json.dumps(r.params, ensure_ascii=False)}",
        "",
        "straddle: buy-stop и sell-stop от открытия, срабатывает сторона скачка, слои по step.",
        "spray: ждём первый вынос ≥ trigger, дальше клипы каждые step по ходу.",
        "выход: все клипы сразу по времени или если цена вернулась в открытие.",
        "",
        f"сессий={r.days} торговали={r.sessions_traded} клипов={r.clips} "
        f"win={r.wins} loss={r.losses} wr={r.win_rate:.1f}%",
        f"net={r.net_pnl:+.2f} USD (1 oz на клип)  avg_clip={r.avg_clip:+.2f}  "
        f"DD={r.max_dd:.2f}  PF={pf}",
        "",
        "клипы:",
    ]
    show = trades[:40]
    for i, t in enumerate(show, 1):
        lines.append(
            f"  {i:02d} {t.side:4} {t.entry_time} → {t.exit_time} {t.reason:10} "
            f"in={t.entry:.2f} out={t.exit:.2f}  {t.pnl:+.2f}"
        )
    if len(trades) > 40:
        lines.append(f"  ... ещё {len(trades) - 40}")
    lines.append(f"сгенерировано {datetime.now(timezone.utc).isoformat()}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--range", dest="range_spec", default="1mo")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--min-clips", type=int, default=20)
    args = ap.parse_args()

    bars = fetch_yahoo("GC=F", args.range_spec, args.interval)
    configs = grid()
    print(f"burst bars={len(bars)} {bars[0].time}->{bars[-1].time} grid={len(configs)}", flush=True)
    scored: list[tuple[BurstResult, BurstParams, list[Trade]]] = []
    for i, p in enumerate(configs, 1):
        p.interval = args.interval
        r, tr = run_burst(bars, p)
        scored.append((r, p, tr))
        if i % 40 == 0:
            print(f"  ... {i}/{len(configs)}", flush=True)

    scored.sort(key=lambda x: (x[0].net_pnl, -x[0].max_dd), reverse=True)
    robust = [x for x in scored if x[0].clips >= args.min_clips]
    robust.sort(key=lambda x: (x[0].net_pnl, -x[0].max_dd), reverse=True)
    wr, wp, wtr = (robust[0] if robust else scored[0])

    out = REPORTS
    out.mkdir(parents=True, exist_ok=True)
    write_csv(bars, out / "burst_5m.csv")
    report = format_burst(wr, wtr)
    (out / "burst_report.txt").write_text(report, encoding="utf-8")
    (out / "burst_summary.json").write_text(json.dumps(asdict(wr), indent=2) + "\n", encoding="utf-8")
    trade_rows = [
        "side,entry_time,exit_time,reason,entry,exit,pnl",
        *[
            f"{t.side},{t.entry_time},{t.exit_time},{t.reason},{t.entry},{t.exit},{t.pnl}"
            for t in wtr
        ],
    ]
    (out / "burst_trades.csv").write_text("\n".join(trade_rows) + "\n", encoding="utf-8")

    lines = [
        "burst open grid",
        f"баров={len(bars)} вариантов={len(configs)} мин.клипов={args.min_clips}",
        "",
        "топ-10:",
    ]
    for i, (r, p, _) in enumerate((robust or scored)[:10], 1):
        lines.append(
            f"  {i:02d} pnl={r.net_pnl:+8.2f} clips={r.clips:3d} days={r.sessions_traded:2d} "
            f"wr={r.win_rate:5.1f}% dd={r.max_dd:.1f}  "
            f"{p.session}/{p.model} trig={p.trigger} layers={p.layers} "
            f"hold={p.hold_bars} fail={p.fail_through_open}"
        )
    lines.append("")
    lines.append("лучший по сессии:")
    for sess in ("london", "ny", "cme"):
        pool = [x for x in (robust or scored) if x[1].session == sess]
        if not pool:
            lines.append(f"  {sess}: нет")
            continue
        r, p, _ = pool[0]
        lines.append(
            f"  {sess}: pnl={r.net_pnl:+.2f} clips={r.clips} wr={r.win_rate:.1f}% "
            f"{p.model} trig={p.trigger} layers={p.layers} hold={p.hold_bars} fail={p.fail_through_open}"
        )
    lines.append("")
    lines.append("победитель: " + json.dumps(asdict(wp), ensure_ascii=False))
    top = "\n".join(lines) + "\n"
    (out / "burst_optimize.txt").write_text(top, encoding="utf-8")

    b1 = simulate(
        wtr,
        start=args.bank,
        name="burst 1 oz / клип",
        notes="каждый клип = 1 унция (0.01 лота)",
        oz_fn=oz_fixed(1.0),
    )
    lev_lines = []
    for lots in (0.01, 0.02, 0.05):
        lev = run_fixed_lot(wtr, start=args.bank, leverage=500, lots=lots)
        lev_lines.append(
            f"плечо 1:500, клип {lots:.2f} лота: "
            f"${lev.start:.0f} → ${lev.end:.2f} net {lev.net:+.2f} "
            f"blown={lev.blown} DD=${lev.max_dd:.2f}"
        )
    bank = format_bank([b1])
    extra = "\n" + "\n".join(lev_lines) + "\n"
    (out / "burst_bank_500.txt").write_text(bank + extra, encoding="utf-8")
    print(top)
    print(report, end="")
    print(bank, end="")
    print(extra, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
