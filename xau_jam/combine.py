"""One paper book for the whole impulse family. One bank, take every shot.

Grow 20% until $25k, then 10% ($2500) and take monthly profit above $25k.
Same costs as paper.py. Not live.

  python3 -m xau_jam.combine --from 01/01/26 --bank 350
  python3 -m xau_jam.combine --once --bank 350
  python3 -m xau_jam.auto --once --bank 350
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_yahoo
from xau_jam.paper import HOLD, costed_cash, day_groups, parse_day, replay, signal_for_day
from xau_jam.pattern import Bar

WATCH_LOG = REPORTS / "combine_watch.log"
RISK = 0.10
START_BANK = 350.0
START_RISK = 0.20
MAX_STAKE = 2500.0
TARGET_BANK = 25_000.0
RUN_RISK = 0.10
PLAN = "20% до $25k, потолок $2500, потом 10% и снимаем месяц"

BOOK = (
    ("MSTR", 0.006),
    ("COIN", 0.006),
    ("SMCI", 0.006),
    ("AMD", 0.006),
    ("UVXY", 0.006),
    ("PLTR", 0.006),
    ("TSLA", 0.003),
)


@dataclass(slots=True)
class Shot:
    time: str
    symbol: str
    side: str
    shares: int
    entry: float
    exit: float
    cash: float
    equity: float
    event: str
    stake: float = 0.0


def collect_signals(
    books: dict[str, tuple[list[Bar], float]],
    begin: date,
    end: date | None,
) -> list[tuple[datetime, str, str, float, int, list[Bar]]]:
    shots: list[tuple[datetime, str, str, float, int, list[Bar]]] = []
    for sym, (h1, trig) in books.items():
        by = day_groups(h1)
        for day, idxs in by.items():
            if day < begin:
                continue
            if end is not None and day >= end:
                continue
            sig = signal_for_day(h1, idxs, trigger=trig)
            if sig is None:
                continue
            side, fi, fill = sig
            shots.append((h1[fi].time, sym, side, fill, fi, h1))
    rank = {sym: i for i, (sym, _) in enumerate(BOOK)}
    shots.sort(key=lambda s: (s[0], rank.get(s[1], 99)))
    return shots


def plan_stake(
    equity: float,
    *,
    start: float,
    risk: float = START_RISK,
    cap: float = MAX_STAKE,
    target: float = TARGET_BANK,
    run_risk: float = RUN_RISK,
    reached: bool = False,
    simple: bool = False,
) -> float:
    """Locked plan: 20% of bank until $25k, then 10% of $25k ($2500)."""
    if target > 0 and (reached or equity + 1e-9 >= target):
        stake = target * run_risk
    else:
        stake = (start if simple else equity) * min(max(risk, 0.0), 1.0)
    if cap:
        stake = min(stake, cap)
    return max(0.0, stake)


def min_start_bank(books: dict[str, tuple[list[Bar], float]], risk: float = RISK, lev: int = 10) -> tuple[float, dict[str, float]]:
    """Smallest bank that still buys 1 share on 10% × leverage for each last price."""
    last: dict[str, float] = {}
    need = 0.0
    for sym, (h1, _t) in books.items():
        if not h1:
            continue
        px = h1[-1].close
        last[sym] = px
        need = max(need, px / max(risk * lev, 1e-9))
    return need, last


@dataclass(slots=True)
class Replay:
    end: float
    fills: list[Shot]
    withdrawals: list[dict]
    target_hit: Shot | None = None


def run_replay(
    shots: list[tuple[datetime, str, str, float, int, list[Bar]]],
    start: float,
    lev: int,
    hold: int = HOLD,
    risk: float = RISK,
    simple: bool = True,
    max_stake: float | None = MAX_STAKE,
    target_bank: float | None = None,
    run_risk: float = RUN_RISK,
    withdraw: bool = False,
) -> Replay:
    risk = min(max(risk, 0.0), 1.0)
    run_risk = min(max(run_risk, 0.0), 1.0)
    jobs: list[dict] = []
    for i, (t, sym, side, fill, fi, h1) in enumerate(shots):
        ex_i = min(len(h1) - 1, fi + hold)
        jobs.append(
            {
                "i": i,
                "open": t,
                "close": h1[ex_i].time,
                "sym": sym,
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
    opened: dict[int, tuple[int, float]] = {}
    path: list[Shot] = []
    withdrawals: list[dict] = []
    target_hit: Shot | None = None
    reached = bool(target_bank and eq >= target_bank)
    last_month: str | None = None

    def _stake() -> float:
        return plan_stake(
            eq,
            start=start,
            risk=risk,
            cap=max_stake or 0.0,
            target=target_bank or 0.0,
            run_risk=run_risk,
            reached=reached,
            simple=simple,
        )

    def _withdraw(month: str) -> None:
        nonlocal eq
        if not (withdraw and target_bank and reached and eq > target_bank + 1e-9):
            return
        took = round(eq - target_bank, 2)
        eq = target_bank
        withdrawals.append({"month": month, "took": took, "bank": round(eq, 2)})

    for when, kind, job in events:
        month = when.strftime("%Y-%m")
        if last_month and month != last_month:
            _withdraw(last_month)
        last_month = month
        if eq <= 0 and kind == 1:
            continue
        if kind == 1:
            stake = _stake()
            shares = int(stake * lev / max(job["fill"], 1e-9))
            if shares < 1:
                continue
            opened[job["i"]] = (shares, stake)
            continue
        got = opened.pop(job["i"], None)
        if got is None:
            continue
        shares, stake = got
        cash = costed_cash(job["side"], job["fill"], job["exit"], shares)
        event = "ok"
        if cash < -stake:
            cash = -stake
            event = "clip"
        eq = max(0.0, eq + cash)
        fill = Shot(
            job["open"].isoformat(),
            job["sym"],
            job["side"],
            shares,
            round(job["fill"], 4),
            round(job["exit"], 4),
            round(cash, 2),
            round(eq, 2),
            event,
            round(stake, 2),
        )
        path.append(fill)
        if target_bank and target_hit is None and eq + 1e-9 >= target_bank:
            target_hit = fill
            reached = True
        if eq <= 0:
            break
    if last_month:
        _withdraw(last_month)
    path.sort(key=lambda s: s.time)
    return Replay(round(eq, 2), path, withdrawals, target_hit)


def replay_one(
    shots: list[tuple[datetime, str, str, float, int, list[Bar]]],
    start: float,
    lev: int,
    hold: int = HOLD,
    risk: float = RISK,
    simple: bool = True,
    max_stake: float | None = MAX_STAKE,
    target_bank: float | None = None,
    run_risk: float = RUN_RISK,
    withdraw: bool = False,
) -> tuple[float, list[Shot]]:
    got = run_replay(
        shots,
        start,
        lev,
        hold=hold,
        risk=risk,
        simple=simple,
        max_stake=max_stake,
        target_bank=target_bank,
        run_risk=run_risk,
        withdraw=withdraw,
    )
    return got.end, got.fills


def _shot_get(f, name: str):
    return getattr(f, name) if hasattr(f, name) else f[name]


def monthly_rows(
    fills: list,
    start: float,
    withdrawals: list[dict] | None = None,
    target: float | None = None,
) -> list[dict]:
    """Equity path by calendar month of the entry."""
    ordered = sorted(fills, key=lambda f: _shot_get(f, "time"))
    by: dict[str, list] = {}
    for f in ordered:
        by.setdefault(str(_shot_get(f, "time"))[:7], []).append(f)
    took_by = {str(w["month"]): float(w["took"]) for w in (withdrawals or [])}
    rows: list[dict] = []
    eq = start
    for month in sorted(by):
        chunk = by[month]
        begin_eq = eq
        pnl = 0.0
        wins = 0
        for f in chunk:
            cash = float(_shot_get(f, "cash"))
            pnl += cash
            if cash > 0:
                wins += 1
            eq = float(_shot_get(f, "equity"))
        took = took_by.get(month, 0.0)
        if took and target:
            eq = target
        elif took:
            eq = max(0.0, eq - took)
        rows.append(
            {
                "month": month,
                "n": len(chunk),
                "wins": wins,
                "pnl": round(pnl, 2),
                "start": round(begin_eq, 2),
                "end": round(eq, 2),
                "took": round(took, 2),
                "pct_start": round(100.0 * pnl / start, 1) if start else 0.0,
                "pct_month": round(100.0 * pnl / begin_eq, 1) if begin_eq else 0.0,
            }
        )
    return rows


def first_cap_hit(fills: list[Shot], cap: float) -> Shot | None:
    """First fill whose stake sits on the dollar ceiling."""
    for f in sorted(fills, key=lambda s: s.time):
        if float(f.stake) + 1e-9 >= cap:
            return f
    return None


def format_monthly(rows: list[dict], title: str) -> list[str]:
    show_took = any(float(r.get("took") or 0) for r in rows)
    if show_took:
        lines = [
            title,
            f"{'мес':8} {'сд':>4} {'+':>3} {'pnl':>10} {'забрал':>10} {'с':>10} {'по':>10} {'%мес':>8}",
        ]
        for r in rows:
            lines.append(
                f"{r['month']:8} {r['n']:4d} {r['wins']:3d} {r['pnl']:+10.2f} "
                f"{float(r.get('took') or 0):+10.2f} {r['start']:10.2f} {r['end']:10.2f} "
                f"{r['pct_month']:+7.1f}%"
            )
        return lines
    lines = [
        title,
        f"{'мес':8} {'сд':>4} {'+':>3} {'pnl':>10} {'с':>10} {'по':>10} {'%старт':>8} {'%мес':>8}",
    ]
    for r in rows:
        lines.append(
            f"{r['month']:8} {r['n']:4d} {r['wins']:3d} {r['pnl']:+10.2f} "
            f"{r['start']:10.2f} {r['end']:10.2f} {r['pct_start']:+7.1f}% {r['pct_month']:+7.1f}%"
        )
    return lines


def state_path(bank: float) -> Path:
    return REPORTS / f"combine_state_{bank:.0f}.json"


def fresh_state(bank: float, lev: int) -> dict:
    return {
        "venue": "combine-paper",
        "start": bank,
        "equity": bank,
        "leverage": lev,
        "simple": False,
        "risk": START_RISK,
        "max_stake": MAX_STAKE,
        "target_bank": TARGET_BANK,
        "reached": False,
        "pos": None,
        "positions": [],
        "fills": [],
        "note": "",
    }


def load_state(bank: float, lev: int) -> dict:
    path = state_path(bank)
    if path.exists() and path.stat().st_size > 0:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("leverage", lev)
        raw.setdefault("start", bank)
        raw.setdefault("simple", False)
        raw.setdefault("risk", START_RISK)
        raw.setdefault("max_stake", MAX_STAKE)
        raw.setdefault("target_bank", TARGET_BANK)
        raw.setdefault("reached", False)
        raw.setdefault("positions", [])
        return raw
    return fresh_state(bank, lev)


def save_state(bank: float, state: dict) -> Path:
    state["updated"] = datetime.now(timezone.utc).isoformat()
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = state_path(bank)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return path


def fetch_books(range_spec: str = "5d") -> dict[str, tuple[list[Bar], float]]:
    books: dict[str, tuple[list[Bar], float]] = {}
    for i, (sym, trig) in enumerate(BOOK):
        try:
            books[sym] = (fetch_yahoo(sym, range_spec, "60m"), trig)
        except Exception as exc:
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        if i + 1 < len(BOOK):
            time.sleep(0.05)
    return books


def _index_at(h1: list[Bar], iso: str) -> int | None:
    for i, b in enumerate(h1):
        if b.time.isoformat() == iso:
            return i
    return None


def watch_book(books: dict[str, tuple[list[Bar], float]], state: dict, lev: int) -> dict:
    """One shared bank. Take every name that fires. 10% each, do not wait."""
    start = float(state.get("start") or state.get("equity") or 0)
    risk = float(state.get("risk") or START_RISK)
    cap = float(state.get("max_stake") or MAX_STAKE)
    target = float(state.get("target_bank") or TARGET_BANK)
    if float(state.get("equity") or 0) + 1e-9 >= target:
        state["reached"] = True
        risk = RUN_RISK
        state["risk"] = risk
        start = target
        state["simple"] = True
        state["start"] = target
    elif not state.get("reached"):
        state["simple"] = False
    stake = plan_stake(
        float(state["equity"]),
        start=start,
        risk=risk,
        cap=cap,
        target=target,
        reached=bool(state.get("reached")),
        simple=bool(state.get("simple")),
    )
    positions = list(state.get("positions") or [])
    if state.get("pos") and not positions:
        positions = [state["pos"]]
    notes: list[str] = []
    kept: list[dict] = []
    for pos in positions:
        packed = books.get(pos["symbol"])
        if not packed:
            kept.append(pos)
            notes.append(f"{pos['symbol']} нет баров")
            continue
        h1, _trig = packed
        fi = _index_at(h1, pos["entry_time"])
        if fi is None:
            kept.append(pos)
            notes.append(f"{pos['symbol']} вход не в барах")
            continue
        due = fi + HOLD
        if len(h1) - 1 < due:
            kept.append(pos)
            notes.append(f"держит {pos['symbol']}")
            continue
        exit_px = h1[due].close
        cash = costed_cash(pos["side"], pos["entry"], exit_px, pos["shares"])
        if cash < -float(pos.get("stake") or stake):
            cash = -float(pos.get("stake") or stake)
        state["equity"] = round(float(state["equity"]) + cash, 2)
        state.setdefault("fills", []).append(
            asdict(
                Shot(
                    pos["entry_time"],
                    pos["symbol"],
                    pos["side"],
                    pos["shares"],
                    pos["entry"],
                    round(exit_px, 4),
                    round(cash, 2),
                    state["equity"],
                    "ok",
                    float(pos.get("stake") or stake),
                )
            )
        )
        notes.append(f"закрыл {pos['symbol']} {cash:+.2f}")
    if float(state.get("equity") or 0) + 1e-9 >= target:
        state["reached"] = True
        risk = RUN_RISK
        state["risk"] = risk
        start = target
        state["simple"] = True
        state["start"] = target
        notes.append(f"банк ≥ ${target:.0f}, ставка {100 * risk:.0f}%, прибыль сверх банка можно снимать")
    stake = plan_stake(
        float(state["equity"]),
        start=start,
        risk=risk,
        cap=cap,
        target=target,
        reached=bool(state.get("reached")),
        simple=bool(state.get("simple")),
    )
    today = max((h1[-1].time.date() for h1, _t in books.values() if h1), default=None)
    if today is None:
        state["positions"] = kept
        state["pos"] = kept[0] if kept else None
        state["note"] = "нет баров"
        return state
    shots = collect_signals(books, today, today + timedelta(days=1))
    open_syms = {p["symbol"] for p in kept}
    done = {(str(f.get("time", ""))[:10], f.get("symbol")) for f in state.get("fills", [])}
    opened = 0
    for t, sym, side, fill, fi, h1 in shots:
        if sym in open_syms:
            continue
        if (t.date().isoformat(), sym) in done:
            continue
        shares = int(stake * lev / max(fill, 1e-9))
        if shares < 1:
            notes.append(f"{sym} {100 * risk:.0f}% не хватает на 1шт")
            continue
        kept.append(
            {
                "symbol": sym,
                "side": side,
                "shares": shares,
                "entry": round(fill, 4),
                "entry_time": t.isoformat(),
                "stake": round(stake, 2),
            }
        )
        open_syms.add(sym)
        opened += 1
        notes.append(f"открыл {side} {sym} {shares}шт @ {fill:.2f}")
    state["positions"] = kept
    state["pos"] = kept[0] if kept else None
    if opened:
        notes.append(f"ставка {100 * risk:.0f}% ×{opened}, общий банк eq=${float(state['equity']):.2f}")
    elif not notes:
        notes.append(f"{today} нет импульса ни по одной бумаге")
    state["note"] = "; ".join(notes)
    return state


def run_watch_once(
    bank: float,
    lev: int,
    risk: float | None = None,
    max_stake: float | None = None,
) -> dict:
    books = fetch_books("5d")
    state = load_state(bank, lev)
    state["leverage"] = lev
    if risk is not None:
        state["risk"] = risk
    if max_stake is not None:
        state["max_stake"] = max_stake
    state = watch_book(books, state, lev)
    save_state(bank, state)
    return state


def run_loop(
    bank: float,
    lev: int,
    interval: int,
    risk: float | None = None,
    max_stake: float | None = None,
) -> None:
    while True:
        ts = datetime.now(timezone.utc).isoformat()
        try:
            state = run_watch_once(bank, lev, risk, max_stake)
            row = f"{ts} {state.get('note', '')} eq=${state['equity']:.2f}\n"
        except Exception as exc:  # noqa: BLE001
            row = f"{ts} ошибка: {exc}\n"
        print(row, end="", flush=True)
        REPORTS.mkdir(parents=True, exist_ok=True)
        with WATCH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(row)
        time.sleep(max(30, interval))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=0.0, help="0 = минимальный банк под выбранную ставку")
    ap.add_argument("--risk", type=float, default=START_RISK, help="доля банка на выстрел, 0.2 = 20%")
    ap.add_argument("--max-stake", type=float, default=MAX_STAKE, help="потолок ставки $, 0 = без потолка")
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--from", dest="date_from", default="01/01/26")
    ap.add_argument("--to", dest="date_to", default="")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    risk = min(max(args.risk, 0.0), 1.0)
    cap = max(args.max_stake, 0.0)
    if args.once or args.loop:
        bank = args.bank if args.bank > 0 else START_BANK
        print(
            f"один банк ${bank:.0f}, ставка {100 * risk:.0f}%, потолок ${cap:.0f}, "
            f"бумаги {', '.join(s for s, _ in BOOK)}",
            flush=True,
        )
        if args.once or not args.loop:
            state = run_watch_once(bank, args.leverage, risk, cap)
            print(state.get("note", ""), f"eq=${state['equity']:.2f}")
            return 0
        run_loop(bank, args.leverage, args.interval, risk, cap)
        return 0
    begin = parse_day(args.date_from)
    until = (parse_day(args.date_to) + timedelta(days=1)) if args.date_to else None
    books: dict[str, tuple[list[Bar], float]] = {}
    print(f"combine from {begin} risk {100 * risk:.0f}%", flush=True)
    last = begin
    for sym, trig in BOOK:
        try:
            h1 = fetch_yahoo(sym, "1y", "60m")
        except Exception as exc:
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        books[sym] = (h1, trig)
        last = max(last, h1[-1].time.date())
        print(f"  {sym} {h1[0].time.date()}→{h1[-1].time.date()} last={h1[-1].close:.2f}", flush=True)
    if args.date_to:
        last = min(last, parse_day(args.date_to))

    need, last_px = min_start_bank(books, risk, args.leverage)
    min_bank = float(max(10, int(need + 9) // 10 * 10))
    bank = args.bank if args.bank > 0 else min_bank
    stake0 = bank * risk
    comm_rt = 2.0
    comm_pct = 100.0 * comm_rt / stake0 if stake0 else 0.0
    shots = collect_signals(books, begin, until)
    months = max((last - begin).days / 30.0, 1.0)
    cap_arg = cap if cap > 0 else None
    simple_end, simple_fills = replay_one(shots, bank, args.leverage, risk=risk, simple=True, max_stake=cap_arg)
    plan = run_replay(
        shots,
        bank,
        args.leverage,
        risk=risk,
        simple=False,
        max_stake=cap_arg,
        target_bank=TARGET_BANK,
        run_risk=RUN_RISK,
        withdraw=True,
    )
    comp_end, comp_fills = plan.end, plan.fills
    hit = first_cap_hit(comp_fills, cap) if cap else None
    target_hit = plan.target_hit

    def _pack(end: float, fills: list[Shot], title: str) -> list[str]:
        pct = 100.0 * (end - bank) / bank if bank else 0.0
        by: dict[str, int] = {}
        for f in fills:
            by[f.symbol] = by.get(f.symbol, 0) + 1
        won = sum(1 for f in fills if f.cash > 0)
        lines = [
            title,
            f"${bank:.0f} → ${end:.2f}  ({pct:+.1f}%, {pct / months:+.1f}%/мес)  "
            f"сделок={len(fills)}  плюс={won}  clip={sum(1 for f in fills if f.event == 'clip')}",
            "по бумагам: " + " ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])),
            "",
        ]
        for i, f in enumerate(fills, 1):
            lines.append(
                f"  {i:03d} {f.time} {f.symbol:5} {f.side:4} {f.shares}шт  "
                f"{f.entry:.2f}→{f.exit:.2f}  stake=${f.stake:.2f}  {f.cash:+.2f}  eq=${f.equity:.2f}  {f.event}"
            )
        return lines

    fat = max(last_px.items(), key=lambda kv: kv[1]) if last_px else ("?", 0.0)
    head = [
        f"Минимальный банк сейчас: ${min_bank:.0f} ({100 * risk:.0f}%×{args.leverage} должно купить 1 акцию самой дорогой).",
        f"Дороже всех {fat[0]} ${fat[1]:.2f}. На 1шт нужно банк ≥ цена / ({100 * risk:.0f}%×{args.leverage}).",
        "По бумагам (последняя цена → банк на 1шт): "
        + ", ".join(f"{s} ${p:.0f}" for s, p in sorted(last_px.items(), key=lambda kv: -kv[1])),
        f"Комиссия min $1×2 = $2. При банке ${bank:.0f} ставка ${stake0:.0f}, комиссия {comm_pct:.1f}% ставки.",
        f"Потолок ставки ${cap:.0f} (номинал ~${cap * args.leverage:.0f} при 1:{args.leverage}).",
        f"Растём 20% до банка ${TARGET_BANK:.0f}, потом ставка {100 * RUN_RISK:.0f}% "
        f"(${TARGET_BANK * RUN_RISK:.0f}) и каждый месяц снимаем всё сверх ${TARGET_BANK:.0f}.",
        f"Отчёт с {begin} → {last}, старт ${bank:.0f}, 1:{args.leverage}.",
        "",
    ]
    s_pct = 100.0 * (simple_end - bank) / bank
    s_months = monthly_rows(simple_fills, bank)
    p_months = monthly_rows(comp_fills, bank, plan.withdrawals, TARGET_BANK)
    took_all = round(sum(float(w["took"]) for w in plan.withdrawals), 2)
    if hit is not None:
        cap_line = (
            f"потолок ставки ${cap:.0f} впервые: {hit.time[:10]}  {hit.symbol}  "
            f"stake=${hit.stake:.0f}  eq=${hit.equity:.2f}"
        )
    elif cap:
        cap_line = f"потолок ставки ${cap:.0f} на этом отрезке не достигли"
    else:
        cap_line = "потолка ставки нет"
    if target_hit is not None:
        tgt_line = (
            f"банк ${TARGET_BANK:.0f} впервые: {target_hit.time[:10]}  {target_hit.symbol}  "
            f"eq=${target_hit.equity:.2f}  дальше ставка {100 * RUN_RISK:.0f}%"
        )
    else:
        tgt_line = f"банк ${TARGET_BANK:.0f} на этом отрезке не достигли"
    head += [
        f"простой 20% без снятия:  ${bank:.0f} → ${simple_end:.2f}  ({s_pct:+.1f}%)  n={len(simple_fills)}",
        f"план до ${TARGET_BANK:.0f} + снятие: банк ${comp_end:.2f}  забрал ${took_all:.2f}  "
        f"всего ${comp_end + took_all:.2f}  n={len(comp_fills)}",
        cap_line,
        tgt_line,
        "",
    ]
    head += format_monthly(s_months, "── помесячно простой % (лот $70, не снимаем) ──")
    head.append("")
    head += format_monthly(p_months, "── помесячно план: 20% → $25k, потом 10% и забираем ──")
    head.append("")
    month_text = "\n".join(head) + "\n"
    tag = f"{begin.isoformat()}_{last.isoformat()}_b{bank:.0f}"
    (REPORTS / f"combine_{tag}.txt").write_text(month_text, encoding="utf-8")
    default_window = begin.isoformat() == "2026-01-01" and until is None
    if default_window:
        (REPORTS / "combine_monthly.txt").write_text(month_text, encoding="utf-8")
        if abs(bank - START_BANK) < 1e-9 and abs(risk - START_RISK) < 1e-9:
            (REPORTS / "combine_350_r20.txt").write_text(month_text, encoding="utf-8")
    lines = head + _pack(simple_end, simple_fills, "── простой % (лот всегда 10% от старта) ──")
    lines.append("")
    lines += _pack(comp_end, comp_fills, "── план: 20% до $25k, потом 10% и снятие ──")
    text = "\n".join(lines) + "\n"
    payload = {
        "min_bank": min_bank,
        "need": need,
        "last": last_px,
        "bank": bank,
        "risk": risk,
        "max_stake": cap,
        "target_bank": TARGET_BANK,
        "run_risk": RUN_RISK,
        "plan": PLAN,
        "cap_hit": None
        if hit is None
        else {
            "time": hit.time,
            "symbol": hit.symbol,
            "stake": hit.stake,
            "equity": hit.equity,
        },
        "target_hit": None
        if target_hit is None
        else {
            "time": target_hit.time,
            "symbol": target_hit.symbol,
            "stake": target_hit.stake,
            "equity": target_hit.equity,
        },
        "from": begin.isoformat(),
        "to": last.isoformat(),
        "simple": {
            "end": simple_end,
            "pct": round(s_pct, 1),
            "n": len(simple_fills),
            "months": s_months,
            "fills": [asdict(f) for f in simple_fills],
        },
        "pay": {
            "end": comp_end,
            "took": took_all,
            "total": round(comp_end + took_all, 2),
            "n": len(comp_fills),
            "months": p_months,
            "withdrawals": plan.withdrawals,
            "fills": [asdict(f) for f in comp_fills],
        },
    }
    (REPORTS / f"combine_{tag}.txt").write_text(text, encoding="utf-8")
    (REPORTS / f"combine_{tag}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if default_window:
        (REPORTS / "combine.txt").write_text(text, encoding="utf-8")
        (REPORTS / "combine_jan_simple_compound.txt").write_text(text, encoding="utf-8")
        (REPORTS / "combine.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if begin.isoformat() == "2025-10-01" and last.isoformat() == "2025-12-31":
        (REPORTS / "plan_oct_dec.txt").write_text(month_text, encoding="utf-8")
    print("\n".join(head), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
