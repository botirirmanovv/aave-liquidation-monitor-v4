"""Any market, 1:10 all-in, fattest single trade this month.

Cash = bank * leverage * (exit - entry) / entry.
Works on futures, crypto, stocks, FX. Not gold-only.

  python3 -m xau_jam.any_shot --bank 500 --leverage 10
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

from xau_jam.backtest import Params, run_backtest
from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_yahoo
from xau_jam.pattern import Bar

SYMBOLS = (
    "NG=F",
    "CL=F",
    "SI=F",
    "GC=F",
    "HG=F",
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "DOGE-USD",
    "NQ=F",
    "ES=F",
    "RTY=F",
    "TSLA",
    "NVDA",
    "MSTR",
    "COIN",
    "PLTR",
    "AMD",
    "UVXY",
    "GBPJPY=X",
    "EURUSD=X",
)


@dataclass(slots=True)
class Shot:
    symbol: str
    style: str
    day: str
    side: str
    entry: float
    exit: float
    pct: float
    cash: float
    hold: str


def cash_at(bank: float, leverage: int, entry: float, pnl: float) -> float:
    return bank * leverage * pnl / max(entry, 1e-9)


def _jam(symbol: str, bars: list[Bar], bank: float, lev: int) -> list[Shot]:
    px = bars[-1].close
    p = Params(
        side="both",
        rr=3.0,
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
    out: list[Shot] = []
    for t in trades:
        out.append(
            Shot(
                symbol=symbol,
                style="jam H1",
                day=t.entry_time[:10],
                side=t.side,
                entry=t.entry,
                exit=t.exit,
                pct=round(100.0 * t.pnl / t.entry, 3),
                cash=round(cash_at(bank, lev, t.entry, t.pnl), 2),
                hold=f"{t.bars_held}h {t.reason}",
            )
        )
    return out


def _ride(symbol: str, bars: list[Bar], bank: float, lev: int) -> list[Shot]:
    """First 0.4% run from each UTC day's first open, hold 6/12/24 H1 bars."""
    out: list[Shot] = []
    by_day: dict = {}
    for i, b in enumerate(bars):
        by_day.setdefault(b.time.date(), []).append(i)
    for day, idxs in by_day.items():
        oi = idxs[0]
        session_px = bars[oi].open
        trig = session_px * 0.004
        side = None
        fill_i = None
        fill_px = None
        last_hunt = idxs[min(8, len(idxs) - 1)]
        for i in range(oi, last_hunt + 1):
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
        for hold, label in ((6, "6h"), (12, "12h"), (24, "24h")):
            ex_i = min(len(bars) - 1, fill_i + hold)
            exit_px = bars[ex_i].close
            pnl = (exit_px - fill_px) if side == "buy" else (fill_px - exit_px)
            out.append(
                Shot(
                    symbol=symbol,
                    style="day impulse",
                    day=day.isoformat(),
                    side=side,
                    entry=round(fill_px, 6),
                    exit=round(exit_px, 6),
                    pct=round(100.0 * pnl / fill_px, 3),
                    cash=round(cash_at(bank, lev, fill_px, pnl), 2),
                    hold=label,
                )
            )
    return out


def _gap(symbol: str, bars: list[Bar], bank: float, lev: int) -> list[Shot]:
    """Daily gap ≥2%, ride open → close that day."""
    out: list[Shot] = []
    for i in range(1, len(bars)):
        prev, b = bars[i - 1], bars[i]
        if prev.close <= 0:
            continue
        gap = (b.open - prev.close) / prev.close
        if gap >= 0.02:
            side, entry, exit_px = "buy", b.open, b.close
        elif gap <= -0.02:
            side, entry, exit_px = "sell", b.open, b.close
        else:
            continue
        pnl = (exit_px - entry) if side == "buy" else (entry - exit_px)
        out.append(
            Shot(
                symbol=symbol,
                style="gap 2%",
                day=b.time.date().isoformat(),
                side=side,
                entry=round(entry, 6),
                exit=round(exit_px, 6),
                pct=round(100.0 * pnl / entry, 3),
                cash=round(cash_at(bank, lev, entry, pnl), 2),
                hold="EOD",
            )
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=10)
    args = ap.parse_args()

    shots: list[Shot] = []
    ok = []
    bad = []
    for sym in SYMBOLS:
        try:
            h1 = fetch_yahoo(sym, "1mo", "60m")
            shots.extend(_jam(sym, h1, args.bank, args.leverage))
            shots.extend(_ride(sym, h1, args.bank, args.leverage))
            d1 = fetch_yahoo(sym, "3mo", "1d")
            shots.extend(_gap(sym, d1, args.bank, args.leverage))
            ok.append(sym)
            print(f"  {sym} h1={len(h1)}", flush=True)
        except Exception as exc:
            bad.append(f"{sym}: {exc}")
            print(f"  skip {sym}: {exc}", flush=True)

    shots.sort(key=lambda s: s.cash, reverse=True)
    if not shots:
        raise SystemExit("no shots")
    best = shots[0]
    lines = [
        f"Любой рынок, банк ${args.bank:.0f}, плечо 1:{args.leverage}, all-in",
        f"символы ок={len(ok)} нет={len(bad)} сделок={len(shots)}",
        "",
        "ПОБЕДИТЕЛЬ ЗА РАЗ:",
        f"  {best.symbol}  {best.style}  {best.day}  {best.side}",
        f"  in={best.entry} out={best.exit}  {best.hold}",
        f"  {best.pct:+.2f}%   кэш {best.cash:+.2f}$",
        "",
        "топ-15:",
    ]
    for i, s in enumerate(shots[:15], 1):
        lines.append(
            f"  {i:02d} {s.day} {s.symbol:10} {s.style:12} {s.side:4} {s.hold:8} "
            f"{s.pct:+6.2f}%  {s.cash:+8.0f}$"
        )
    if bad:
        lines.append("")
        lines.append("не скачались: " + "; ".join(bad[:8]))
    text = "\n".join(lines) + "\n"
    out = REPORTS
    out.mkdir(parents=True, exist_ok=True)
    (out / "any_shot.txt").write_text(text, encoding="utf-8")
    (out / "any_shot.json").write_text(
        json.dumps({"best": asdict(best), "top": [asdict(s) for s in shots[:25]]}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
