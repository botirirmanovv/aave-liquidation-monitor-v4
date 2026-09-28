"""Gold Plan B — same impulse as Combine, not the stock book.

Old Jam sell on GC=F is rewritten: session-open one-sided impulse,
HOLD=6, 20%→$25k cap $2500. Combine BOOK (MSTR…TSLA) is not touched.
Live / 7496 / Aave / Morpho live stay off.

  python3 -m xau_jam.gold --from 01/01/26 --bank 350
  python3 -m xau_jam.gold --once --bank 350
"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xau_jam.burst_open import REPORTS
from xau_jam.combine import MAX_STAKE, START_BANK, START_RISK, TARGET_BANK, RUN_RISK, plan_stake
from xau_jam.data import fetch_yahoo
from xau_jam.paper import HOLD, day_groups, parse_day, signal_for_day
from xau_jam.pattern import Bar

SYMBOL = "GC=F"
TRIGGER = 0.006
LEV = 10
OZ_STEP = 0.01
TAX_OZ = 0.15  # USD/oz per side; old Jam spread was 0.3 round-trip
STATE = REPORTS / "gold_state_350.json"
WATCH_LOG = REPORTS / "gold_watch.log"
LIVE_MONEY = False


@dataclass(slots=True)
class Fill:
    time: str
    symbol: str
    side: str
    oz: float
    entry: float
    exit: float
    cash: float
    equity: float
    event: str
    stake: float = 0.0
    exit_time: str = ""


def ny_session_idxs(bars: list[Bar], idxs: list[int]) -> list[int]:
    """First bar hour>=13 UTC = COMEX/NY analog of stock session open."""
    for j, i in enumerate(idxs):
        if bars[i].time.hour >= 13:
            return idxs[j:]
    return []


def gold_cash(side: str, entry: float, exit_px: float, oz: float) -> float:
    if oz <= 0:
        return 0.0
    if side == "buy":
        pnl = ((exit_px - TAX_OZ) - (entry + TAX_OZ)) * oz
    else:
        pnl = ((entry - TAX_OZ) - (exit_px + TAX_OZ)) * oz
    return pnl


def oz_lot(stake: float, price: float, lev: int = LEV) -> float:
    notional = stake * lev
    raw = notional / max(price, 1e-9)
    oz = math.floor(raw / OZ_STEP) * OZ_STEP
    return round(oz, 2)


def collect_gold(
    h1: list[Bar],
    begin,
    end,
    trigger: float = TRIGGER,
) -> list[tuple[datetime, str, float, int]]:
    shots: list[tuple[datetime, str, float, int]] = []
    by = day_groups(h1)
    for day, idxs in by.items():
        if day < begin:
            continue
        if end is not None and day >= end:
            continue
        sl = ny_session_idxs(h1, idxs)
        if not sl:
            continue
        sig = signal_for_day(h1, sl, trigger=trigger)
        if sig is None:
            continue
        side, fi, fill = sig
        shots.append((h1[fi].time, side, fill, fi))
    shots.sort(key=lambda s: s[0])
    return shots


def replay_gold(
    h1: list[Bar],
    start: float,
    begin,
    end=None,
    risk: float = START_RISK,
    cap: float = MAX_STAKE,
    trigger: float = TRIGGER,
    hold: int = HOLD,
    lev: int = LEV,
) -> tuple[float, list[Fill], list[dict]]:
    shots = collect_gold(h1, begin, end, trigger)
    jobs = []
    for i, (t, side, fill, fi) in enumerate(shots):
        ex_i = min(len(h1) - 1, fi + hold)
        jobs.append(
            {
                "i": i,
                "open": t,
                "close": h1[ex_i].time,
                "side": side,
                "fill": fill,
                "exit": h1[ex_i].close,
            }
        )
    events: list[tuple[datetime, int, dict]] = []
    for job in jobs:
        events.append((job["close"], 0, job))
        events.append((job["open"], 1, job))
    events.sort(key=lambda e: (e[0], e[1], e[2]["i"]))
    eq = start
    reached = False
    opened: dict[int, tuple[float, float]] = {}
    path: list[Fill] = []
    withdrawals: list[dict] = []
    last_month: str | None = None

    def _withdraw(month: str) -> None:
        nonlocal eq
        if eq <= TARGET_BANK + 1e-9:
            return
        took = round(eq - TARGET_BANK, 2)
        eq = TARGET_BANK
        withdrawals.append({"month": month, "took": took, "equity": eq})

    for ts, kind, job in events:
        month = ts.strftime("%Y-%m")
        if last_month and month != last_month:
            _withdraw(last_month)
        last_month = month
        if kind == 1:
            if LIVE_MONEY:
                continue
            stake = plan_stake(
                eq,
                start=start,
                risk=risk,
                cap=cap,
                target=TARGET_BANK,
                run_risk=RUN_RISK,
                reached=reached,
            )
            oz = oz_lot(stake, job["fill"], lev)
            if oz < OZ_STEP:
                continue
            opened[job["i"]] = (oz, stake)
            continue
        got = opened.pop(job["i"], None)
        if got is None:
            continue
        oz, stake = got
        cash = gold_cash(job["side"], job["fill"], job["exit"], oz)
        event = "ok"
        if cash < -stake:
            cash = -stake
            event = "clip"
        eq = max(0.0, eq + cash)
        if eq + 1e-9 >= TARGET_BANK:
            reached = True
        path.append(
            Fill(
                job["open"].isoformat(),
                SYMBOL,
                job["side"],
                oz,
                round(job["fill"], 4),
                round(job["exit"], 4),
                round(cash, 2),
                round(eq, 2),
                event,
                round(stake, 2),
                job["close"].isoformat(),
            )
        )
        if eq <= 0:
            break
    if last_month:
        _withdraw(last_month)
    path.sort(key=lambda s: s.time)
    return round(eq, 2), path, withdrawals


def state_path(bank: float) -> Path:
    return REPORTS / f"gold_state_{bank:.0f}.json"


def load_state(bank: float, lev: int) -> dict:
    path = state_path(bank)
    if path.exists() and path.stat().st_size > 0:
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "venue": "gold-yahoo-paper",
        "symbol": SYMBOL,
        "start": bank,
        "equity": bank,
        "leverage": lev,
        "risk": START_RISK,
        "max_stake": MAX_STAKE,
        "target_bank": TARGET_BANK,
        "reached": False,
        "pos": None,
        "fills": [],
        "note": "",
    }


def save_state(bank: float, state: dict) -> Path:
    state["updated"] = datetime.now(timezone.utc).isoformat()
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = state_path(bank)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return path


def watch_once(bank: float, lev: int = LEV) -> dict:
    h1 = fetch_yahoo(SYMBOL, "5d", "60m")
    state = load_state(bank, lev)
    pos = state.get("pos")
    by = day_groups(h1)
    if pos:
        fi = None
        for i, b in enumerate(h1):
            if b.time.isoformat() == pos.get("entry_time"):
                fi = i
                break
        if fi is None:
            state["note"] = "вход не найден в барах, жду"
            save_state(bank, state)
            return state
        due = fi + HOLD
        if len(h1) - 1 < due:
            state["note"] = f"держит GC=F {pos['side']} {pos['oz']}oz, выход через {due - (len(h1) - 1)} бар"
            save_state(bank, state)
            return state
        exit_px = h1[due].close
        cash = gold_cash(pos["side"], pos["entry"], exit_px, float(pos["oz"]))
        stake = float(pos.get("stake") or 0)
        event = "ok"
        if stake and cash < -stake:
            cash = -stake
            event = "clip"
        state["equity"] = round(max(0.0, float(state["equity"]) + cash), 2)
        state.setdefault("fills", []).append(
            asdict(
                Fill(
                    pos["entry_time"],
                    SYMBOL,
                    pos["side"],
                    float(pos["oz"]),
                    pos["entry"],
                    round(exit_px, 4),
                    round(cash, 2),
                    state["equity"],
                    event,
                    stake,
                    h1[due].time.isoformat(),
                )
            )
        )
        state["pos"] = None
        state["note"] = f"закрыл {pos['side']} GC=F {cash:+.2f} eq=${state['equity']:.2f}"
        save_state(bank, state)
        return state
    today = h1[-1].time.date() if h1 else None
    if today is None:
        state["note"] = "нет баров"
        save_state(bank, state)
        return state
    idxs = by.get(today) or []
    sl = ny_session_idxs(h1, idxs)
    sig = signal_for_day(h1, sl, trigger=TRIGGER) if sl else None
    if sig is None:
        state["note"] = f"{today} GC=F нет импульса"
        save_state(bank, state)
        return state
    side, fi, fill = sig
    stake = plan_stake(
        float(state["equity"]),
        start=bank,
        risk=float(state.get("risk") or START_RISK),
        cap=float(state.get("max_stake") or MAX_STAKE),
        target=float(state.get("target_bank") or TARGET_BANK),
        run_risk=RUN_RISK,
        reached=bool(state.get("reached")),
    )
    oz = oz_lot(stake, fill, lev)
    if oz < OZ_STEP:
        state["note"] = "не хватает на 0.01oz"
        save_state(bank, state)
        return state
    state["pos"] = {
        "symbol": SYMBOL,
        "side": side,
        "oz": oz,
        "entry": round(fill, 4),
        "entry_time": h1[fi].time.isoformat(),
        "stake": round(stake, 2),
    }
    state["note"] = f"открыл {side} GC=F {oz}oz @ {fill:.2f}"
    save_state(bank, state)
    return state


def run_loop(bank: float, interval: int = 3600) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    while True:
        ts = datetime.now(timezone.utc).isoformat()
        try:
            state = watch_once(bank)
            row = f"{ts} {state.get('note', '')} eq=${state['equity']:.2f}\n"
        except Exception as exc:  # noqa: BLE001
            row = f"{ts} ошибка: {exc}\n"
        print(row, end="", flush=True)
        with WATCH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(row)
        time.sleep(max(30, interval))


def write_report(end: float, fills: list[Fill], withdrawals: list[dict], start: float, last: float) -> str:
    took = sum(w["took"] for w in withdrawals)
    lines = [
        "GOLD B — импульс как Combine, не Jam sell.",
        "A (семёрка) не трогали. Live/7496/Aave/Morpho live закрыты.",
        f"GC=F H1 Yahoo. OPEN = первый бар с hour>=13 UTC. порог {TRIGGER:.1%} HOLD={HOLD}.",
        f"банк ${start:.0f}, ставка 20% до ${TARGET_BANK:.0f}, потолок ${MAX_STAKE:.0f}, лот 0.01oz, спред ${TAX_OZ*2:.2f}/oz RT.",
        f"последняя цена ${last:.2f}. сделок {len(fills)}. банк ${end:.2f}. снял ${took:.2f}. всего ${end+took:.2f}.",
        "2026 на золоте $25k не взяли — это не семёрка. Правила не крутил.",
        "",
    ]
    for f in fills[:8]:
        lines.append(
            f"  {f.time} {f.side:4} {f.oz}oz {f.entry:.2f}→{f.exit:.2f} {f.cash:+.2f} eq=${f.equity:.2f}"
        )
    if len(fills) > 8:
        lines.append(f"  … ещё {len(fills) - 8}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=START_BANK)
    ap.add_argument("--from", dest="date_from", default="01/01/26")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    if args.once or args.loop:
        if args.once:
            state = watch_once(args.bank)
            print(state.get("note", ""), f"eq=${state['equity']:.2f}")
            return 0
        run_loop(args.bank, args.interval)
        return 0
    begin = parse_day(args.date_from)
    print(f"gold B from {begin} GC=F trigger {TRIGGER:.1%} hold={HOLD}", flush=True)
    h1 = fetch_yahoo(SYMBOL, "1y", "60m")
    end_eq, fills, withdrawals = replay_gold(h1, args.bank, begin)
    text = write_report(end_eq, fills, withdrawals, args.bank, h1[-1].close)
    (REPORTS / "GOLD_B.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
