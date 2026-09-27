"""$500 bank + large leverage. Size by risk and free margin, 0.01 lot step.

  python3 -m xau_jam.leverage --bank 500 --leverage 500
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

from xau_jam.backtest import Params, REPORTS, Trade, run_backtest
from xau_jam.data import fetch_yahoo, fetch_yahoo_h1
from xau_jam.open_scalp import OpenParams, run_open_scalp

OZ_PER_LOT = 100.0
MIN_OZ = 1.0  # 0.01 lot


def _clips(t: Trade) -> int:
    # open-scalp stores clip count in prev_low; Jam stores a price there.
    if 0 < t.prev_low <= 10:
        return max(1, int(round(t.prev_low)))
    return 1


def _per_oz_pnl(t: Trade) -> float:
    return t.pnl / _clips(t)


@dataclass(slots=True)
class LevTrade:
    entry_time: str
    side: str
    lots: float
    margin: float
    pnl: float
    equity: float
    event: str


@dataclass(slots=True)
class LevResult:
    name: str
    start: float
    leverage: int
    risk_pct: float
    end: float
    net: float
    max_dd: float
    blown: bool
    trades: int
    notes: str
    path: list[LevTrade]


def run_lev(
    trades: list[Trade],
    *,
    start: float,
    leverage: int,
    risk_pct: float,
    margin_use: float = 0.5,
    stopout: float = 0.5,
    name: str = "",
) -> LevResult:
    """risk_pct of equity / stop distance, capped by margin_use * equity * leverage."""
    eq = start
    peak = start
    max_dd = 0.0
    blown = False
    path: list[LevTrade] = []
    taken = 0
    for t in trades:
        if blown or eq <= 0:
            path.append(LevTrade(t.entry_time, t.side, 0.0, 0.0, 0.0, eq, "dead"))
            continue
        risk_pts = abs(t.entry - t.stop)
        price = max(t.entry, 1.0)
        if risk_pts <= 0:
            path.append(LevTrade(t.entry_time, t.side, 0.0, 0.0, 0.0, eq, "skip"))
            continue
        risk_oz = (eq * risk_pct) / risk_pts
        margin_oz = (eq * margin_use * leverage) / price
        oz = min(risk_oz, margin_oz)
        oz = (oz // MIN_OZ) * MIN_OZ
        if oz < MIN_OZ:
            # 0.01 lot if margin allows, else skip
            need = MIN_OZ * price / leverage
            if need <= eq * margin_use:
                oz = MIN_OZ
            else:
                path.append(LevTrade(t.entry_time, t.side, 0.0, 0.0, 0.0, eq, "no_margin"))
                continue
        margin = oz * price / leverage
        if margin > eq:
            path.append(LevTrade(t.entry_time, t.side, 0.0, 0.0, 0.0, eq, "no_margin"))
            continue
        pnl = _per_oz_pnl(t) * oz
        eq += pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        taken += 1
        event = "ok"
        if eq <= 0:
            eq = 0.0
            blown = True
            event = "blown"
        elif margin > 0 and eq / margin < stopout:
            eq = 0.0
            blown = True
            event = "stopout"
        path.append(
            LevTrade(
                t.entry_time,
                t.side,
                round(oz / OZ_PER_LOT, 4),
                round(margin, 2),
                round(pnl, 2),
                round(eq, 2),
                event,
            )
        )
    return LevResult(
        name=name or f"1:{leverage} risk={risk_pct:.0%}",
        start=start,
        leverage=leverage,
        risk_pct=risk_pct,
        end=round(eq, 2),
        net=round(eq - start, 2),
        max_dd=round(max_dd, 2),
        blown=blown,
        trades=taken,
        notes=f"плечо 1:{leverage}, риск {risk_pct:.0%}, маржа до {margin_use:.0%} банка, стоп-аут {stopout:.0%}",
        path=path,
    )


def run_fixed_lot(
    trades: list[Trade],
    *,
    start: float,
    leverage: int,
    lots: float,
    stopout: float = 0.5,
) -> LevResult:
    oz = lots * OZ_PER_LOT
    eq = start
    peak = start
    max_dd = 0.0
    blown = False
    path: list[LevTrade] = []
    taken = 0
    for t in trades:
        if blown or eq <= 0:
            path.append(LevTrade(t.entry_time, t.side, 0.0, 0.0, 0.0, eq, "dead"))
            continue
        margin = oz * t.entry / leverage
        if margin > eq:
            path.append(LevTrade(t.entry_time, t.side, 0.0, 0.0, 0.0, eq, "no_margin"))
            continue
        pnl = _per_oz_pnl(t) * oz
        eq += pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        taken += 1
        event = "ok"
        if eq <= 0:
            eq = 0.0
            blown = True
            event = "blown"
        elif margin > 0 and eq / margin < stopout:
            eq = 0.0
            blown = True
            event = "stopout"
        path.append(
            LevTrade(t.entry_time, t.side, lots, round(margin, 2), round(pnl, 2), round(eq, 2), event)
        )
    return LevResult(
        name=f"1:{leverage} фикс {lots:.2f} lot",
        start=start,
        leverage=leverage,
        risk_pct=0.0,
        end=round(eq, 2),
        net=round(eq - start, 2),
        max_dd=round(max_dd, 2),
        blown=blown,
        trades=taken,
        notes=f"фикс {lots:.2f} лота, плечо 1:{leverage}, стоп-аут {stopout:.0%}",
        path=path,
    )


def _row(book: str, r: LevResult) -> dict:
    return {
        "book": book,
        "name": r.name,
        "leverage": r.leverage,
        "risk_pct": r.risk_pct,
        "end": r.end,
        "net": r.net,
        "max_dd": r.max_dd,
        "blown": r.blown,
        "trades": r.trades,
        "notes": r.notes,
        "pct": round(100.0 * r.net / r.start, 1) if r.start else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=500)
    args = ap.parse_args()

    jam_bars = fetch_yahoo_h1("GC=F", "1mo")
    _, jam_tr = run_backtest(jam_bars, params=Params())
    os_bars = fetch_yahoo("GC=F", "1mo", "5m")
    os_p = OpenParams(
        session="ny",
        style="momentum",
        min_spike=4.0,
        hold_bars=6,
        rr=1.5,
        clips=3,
        clip_step=2.0,
    )
    _, os_tr = run_open_scalp(os_bars, os_p)
    london_p = OpenParams(
        session="london",
        style="momentum",
        min_spike=4.0,
        hold_bars=12,
        rr=0.8,
        clips=1,
        clip_step=2.0,
    )
    _, lon_tr = run_open_scalp(os_bars, london_p)

    books = {
        "jam_sell": jam_tr,
        "open_ny_momentum": os_tr,
        "open_london_momentum": lon_tr,
    }

    rows: list[dict] = []
    details: dict[str, LevResult] = {}
    for book, tr in books.items():
        for lev in (100, 200, 500, 1000):
            for risk in (0.01, 0.02, 0.05, 0.10, 0.20):
                r = run_lev(tr, start=args.bank, leverage=lev, risk_pct=risk, name=f"{book} 1:{lev} r{risk:.0%}")
                rows.append(_row(book, r))
                details[r.name] = r
            for lots in (0.01, 0.02, 0.05, 0.10, 0.20):
                r = run_fixed_lot(tr, start=args.bank, leverage=lev, lots=lots)
                rec = _row(book, r)
                rec["lots"] = lots
                rows.append(rec)
                details[f"{book} {r.name}"] = r

    alive = [x for x in rows if not x["blown"] and x["net"] > 0]
    alive.sort(key=lambda x: x["net"], reverse=True)
    dead = [x for x in rows if x["blown"]]
    dead.sort(key=lambda x: x["net"])

    lines = [
        f"Большое плечо, банк ${args.bank:.0f}, золото ~$4300",
        "0.01 лот = 1 унция. Стоп-аут если equity/маржа < 50%.",
        f"книги: jam sell, open-scalp NY, open-scalp London. вариантов={len(rows)}",
        "",
        "топ плюсовых (счёт жив):",
    ]
    for i, x in enumerate(alive[:15], 1):
        extra = f"  lot={x['lots']:.2f}" if "lots" in x else f"  risk={x['risk_pct']:.0%}"
        lines.append(
            f"  {i:02d} {x['book']:22} 1:{x['leverage']:<4}{extra}  "
            f"net={x['net']:+8.2f} ({x['pct']:+.0f}%)  end=${x['end']:.0f}  "
            f"DD=${x['max_dd']:.0f}  n={x['trades']}"
        )
    lines.append("")
    lines.append(f"сгорели: {len(dead)} из {len(rows)}")
    if dead:
        lines.append("примеры слива:")
        for x in dead[:8]:
            extra = f" lot={x.get('lots', 0):.2f}" if "lots" in x else f" risk={x['risk_pct']:.0%}"
            lines.append(f"  {x['book']} 1:{x['leverage']}{extra}  DD=${x['max_dd']:.0f}")

    best = alive[0] if alive else None
    if best:
        key = None
        for k, r in details.items():
            if r.net == best["net"] and r.leverage == best["leverage"] and (not r.blown):
                if best["book"] in k or best["name"] in k:
                    key = k
                    winner = r
                    break
        if key:
            lines.append("")
            lines.append("лучший живой прогон:")
            lines.append(f"  {best['book']}  {winner.notes}")
            lines.append(f"  ${winner.start:.0f} → ${winner.end:.2f}  net {winner.net:+.2f}  DD ${winner.max_dd:.2f}")
            for i, t in enumerate(winner.path, 1):
                lines.append(
                    f"  {i:02d} {t.entry_time} {t.side:4} {t.lots:.2f}lot  "
                    f"маржа=${t.margin:.0f}  PnL={t.pnl:+.2f}  eq=${t.equity:.2f}  {t.event}"
                )
        lines.append("")
        lines.append(
            "плечо само по себе плюс не делает: оно только даёт открыть лот. "
            "Чем больше лот, тем быстрее слив на широком стопе. "
            "Это один месяц, не живой совет."
        )

    text = "\n".join(lines) + "\n"
    out = REPORTS
    out.mkdir(parents=True, exist_ok=True)
    (out / "leverage_500.txt").write_text(text, encoding="utf-8")
    (out / "leverage_500.json").write_text(
        json.dumps({"rows": rows, "best": best}, indent=2) + "\n", encoding="utf-8"
    )
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
