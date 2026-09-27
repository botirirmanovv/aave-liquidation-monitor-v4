"""Find the one-shot: biggest single-day lift at 1:10.

No grid philosophy. Ride the first London/NY/CME impulse and keep it.
Also score the Jam H1 lottery trades. Rank by one-day cash at 1:10.

  python3 -m xau_jam.oneshot --bank 500 --leverage 10
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

from xau_jam.backtest import Params, run_backtest
from xau_jam.data import fetch_yahoo, fetch_yahoo_h1
from xau_jam.leverage import OZ_PER_LOT
from xau_jam.open_scalp import SESSIONS, _session_opens
from xau_jam.pattern import Bar

from xau_jam.burst_open import REPORTS


@dataclass(slots=True)
class Shot:
    name: str
    day: str
    side: str
    entry: float
    exit: float
    pnl_oz: float
    hold: str
    cash_10: float
    lots: float


def _lots_at_lev(bank: float, price: float, leverage: int) -> float:
    oz = bank * leverage / max(price, 1.0)
    lots = int(oz / OZ_PER_LOT / 0.01) * 0.01
    return max(0.01, lots) if oz >= 1.0 else 0.0


def _ride_opens(
    bars: list[Bar],
    session: str,
    trigger: float,
    hold_bars: int,
    label: str,
    bank: float,
    leverage: int,
) -> list[Shot]:
    opens = _session_opens(bars, SESSIONS[session])
    out: list[Shot] = []
    for oi in opens:
        session_px = bars[oi].open
        last_hunt = min(len(bars) - 1, oi + 8)
        side = None
        fill_i = None
        fill_px = None
        for i in range(oi, last_hunt + 1):
            up = bars[i].high - session_px
            dn = session_px - bars[i].low
            if up < trigger and dn < trigger:
                continue
            if up >= dn:
                side = "buy"
                fill_px = session_px + trigger
                if bars[i].high < fill_px:
                    continue
            else:
                side = "sell"
                fill_px = session_px - trigger
                if bars[i].low > fill_px:
                    continue
            fill_i = i
            break
        if side is None or fill_i is None or fill_px is None:
            continue
        ex_i = min(len(bars) - 1, fill_i + hold_bars)
        exit_px = bars[ex_i].close
        pnl = (exit_px - fill_px) if side == "buy" else (fill_px - exit_px)
        lots = _lots_at_lev(bank, fill_px, leverage)
        if lots <= 0:
            continue
        cash = pnl * lots * OZ_PER_LOT
        out.append(
            Shot(
                name=f"{session} {label}",
                day=bars[fill_i].time.date().isoformat(),
                side=side,
                entry=round(fill_px, 2),
                exit=round(exit_px, 2),
                pnl_oz=round(pnl, 2),
                hold=label,
                cash_10=round(cash, 2),
                lots=lots,
            )
        )
    return out


def _jam_shots(bars: list[Bar], bank: float, leverage: int) -> list[Shot]:
    _, trades = run_backtest(bars, params=Params())
    out: list[Shot] = []
    for t in trades:
        lots = _lots_at_lev(bank, t.entry, leverage)
        if lots <= 0:
            continue
        out.append(
            Shot(
                name="jam sell H1",
                day=t.entry_time[:10],
                side=t.side,
                entry=round(t.entry, 2),
                exit=round(t.exit, 2),
                pnl_oz=round(t.pnl, 2),
                hold=f"{t.bars_held}h {t.reason}",
                cash_10=round(t.pnl * lots * OZ_PER_LOT, 2),
                lots=lots,
            )
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=10)
    args = ap.parse_args()

    h1 = fetch_yahoo_h1("GC=F", "1mo")
    m5 = fetch_yahoo("GC=F", "1mo", "5m")
    shots: list[Shot] = []
    shots.extend(_jam_shots(h1, args.bank, args.leverage))
    for sess in ("london", "ny", "cme"):
        for hold, label in ((6, "30m"), (12, "1h"), (24, "2h"), (72, "6h")):
            shots.extend(
                _ride_opens(m5, sess, 2.5, hold, label, args.bank, args.leverage)
            )

    shots.sort(key=lambda s: s.cash_10, reverse=True)
    best = shots[0]
    # same-day best only (true one-shot)
    lines = [
        f"Один выстрел, банк ${args.bank:.0f}, плечо 1:{args.leverage}",
        f"лот на выстрел ≈ {best.lots:.2f} (потолок маржи 1:{args.leverage})",
        "",
        "ПОБЕДИТЕЛЬ ЗА РАЗ:",
        f"  {best.name}  {best.day}  {best.side}",
        f"  in={best.entry:.2f} out={best.exit:.2f}  {best.hold}",
        f"  {best.pnl_oz:+.2f} $/oz   кэш {best.cash_10:+.2f}   лот {best.lots:.2f}",
        "",
        "топ-12 дней:",
    ]
    for i, s in enumerate(shots[:12], 1):
        lines.append(
            f"  {i:02d} {s.day} {s.name:16} {s.side:4} {s.hold:10} "
            f"{s.pnl_oz:+7.2f}/oz  {s.cash_10:+8.2f}$  {s.lots:.2f}lot"
        )

    # if 1:10 is tiny, also print what the same winner does at 1:100 / 1:500
    extra = []
    for lev in (10, 100, 500):
        lots = _lots_at_lev(args.bank, best.entry, lev)
        cash = best.pnl_oz * lots * OZ_PER_LOT
        extra.append(f"  тот же выстрел 1:{lev}: {lots:.2f}lot  {cash:+.0f}$")
    lines.append("")
    lines.append("тот же лучший день на другом плече:")
    lines.extend(extra)

    text = "\n".join(lines) + "\n"
    out = REPORTS
    out.mkdir(parents=True, exist_ok=True)
    (out / "oneshot.txt").write_text(text, encoding="utf-8")
    (out / "oneshot.json").write_text(
        json.dumps({"best": asdict(best), "top": [asdict(s) for s in shots[:20]]}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
