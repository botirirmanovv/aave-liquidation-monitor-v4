"""One paper book for the whole impulse family. One bank, take every shot.

10% of start bank per trade, 90% sits. Do not skip a name because another is open.
Same costs as paper.py. Simple %. Not live.

  python3 -m xau_jam.combine --from 01/01/26 --bank 500
  python3 -m xau_jam.combine --once --bank 500
  python3 -m xau_jam.auto --once --bank 500
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


def replay_one(
    shots: list[tuple[datetime, str, str, float, int, list[Bar]]],
    start: float,
    lev: int,
    hold: int = HOLD,
    risk: float = RISK,
    simple: bool = True,
) -> tuple[float, list[Shot]]:
    risk = min(max(risk, 0.0), 1.0)
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
    for _when, kind, job in events:
        if eq <= 0 and kind == 1:
            continue
        if kind == 1:
            base = start if simple else eq
            stake = base * risk
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
        path.append(
            Shot(
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
        )
        if eq <= 0:
            break
    path.sort(key=lambda s: s.time)
    return round(eq, 2), path


def _shot_get(f, name: str):
    return getattr(f, name) if hasattr(f, name) else f[name]


def monthly_rows(fills: list, start: float) -> list[dict]:
    """Equity path by calendar month of the entry."""
    ordered = sorted(fills, key=lambda f: _shot_get(f, "time"))
    by: dict[str, list] = {}
    for f in ordered:
        by.setdefault(str(_shot_get(f, "time"))[:7], []).append(f)
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
        rows.append(
            {
                "month": month,
                "n": len(chunk),
                "wins": wins,
                "pnl": round(pnl, 2),
                "start": round(begin_eq, 2),
                "end": round(eq, 2),
                "pct_start": round(100.0 * pnl / start, 1) if start else 0.0,
                "pct_month": round(100.0 * pnl / begin_eq, 1) if begin_eq else 0.0,
            }
        )
    return rows


def format_monthly(rows: list[dict], title: str) -> list[str]:
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
        "simple": True,
        "risk": RISK,
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
        raw.setdefault("simple", True)
        raw.setdefault("risk", RISK)
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
    risk = float(state.get("risk") or RISK)
    base = start if state.get("simple", True) else float(state["equity"])
    stake = base * min(max(risk, 0.0), 1.0)
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


def run_watch_once(bank: float, lev: int, risk: float | None = None) -> dict:
    books = fetch_books("5d")
    state = load_state(bank, lev)
    state["leverage"] = lev
    if risk is not None:
        state["risk"] = risk
    state = watch_book(books, state, lev)
    save_state(bank, state)
    return state


def run_loop(bank: float, lev: int, interval: int, risk: float | None = None) -> None:
    while True:
        ts = datetime.now(timezone.utc).isoformat()
        try:
            state = run_watch_once(bank, lev, risk)
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
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--from", dest="date_from", default="01/01/26")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    risk = min(max(args.risk, 0.0), 1.0)
    if args.once or args.loop:
        bank = args.bank if args.bank > 0 else START_BANK
        print(f"один банк ${bank:.0f}, ставка {100 * risk:.0f}%, бумаги {', '.join(s for s, _ in BOOK)}", flush=True)
        if args.once or not args.loop:
            state = run_watch_once(bank, args.leverage, risk)
            print(state.get("note", ""), f"eq=${state['equity']:.2f}")
            return 0
        run_loop(bank, args.leverage, args.interval, risk)
        return 0
    begin = parse_day(args.date_from)
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

    need, last_px = min_start_bank(books, risk, args.leverage)
    min_bank = float(max(10, int(need + 9) // 10 * 10))
    bank = args.bank if args.bank > 0 else min_bank
    stake0 = bank * risk
    comm_rt = 2.0
    comm_pct = 100.0 * comm_rt / stake0 if stake0 else 0.0
    shots = collect_signals(books, begin, None)
    months = max((last - begin).days / 30.0, 1.0)
    simple_end, simple_fills = replay_one(shots, bank, args.leverage, risk=risk, simple=True)
    comp_end, comp_fills = replay_one(shots, bank, args.leverage, risk=risk, simple=False)

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
        f"Отчёт с {begin} → {last}, один банк ${bank:.0f}, ставка {100 * risk:.0f}%, все сделки, 1:{args.leverage}.",
        "",
    ]
    s_pct = 100.0 * (simple_end - bank) / bank
    c_pct = 100.0 * (comp_end - bank) / bank
    s_months = monthly_rows(simple_fills, bank)
    c_months = monthly_rows(comp_fills, bank)
    head += [
        f"простой %:  ${bank:.0f} → ${simple_end:.2f}  ({s_pct:+.1f}%)  n={len(simple_fills)}",
        f"сложный %:  ${bank:.0f} → ${comp_end:.2f}  ({c_pct:+.1f}%)  n={len(comp_fills)}",
        "",
    ]
    head += format_monthly(s_months, "── помесячно простой % ──")
    head.append("")
    head += format_monthly(c_months, "── помесячно сложный % ──")
    head.append("")
    month_text = "\n".join(head) + "\n"
    (REPORTS / "combine_monthly.txt").write_text(month_text, encoding="utf-8")
    lines = head + _pack(simple_end, simple_fills, "── простой % (лот всегда 10% от старта) ──")
    lines.append("")
    lines += _pack(comp_end, comp_fills, "── сложный % (лот 10% от текущего банка) ──")
    text = "\n".join(lines) + "\n"
    (REPORTS / "combine.txt").write_text(text, encoding="utf-8")
    (REPORTS / "combine_jan_simple_compound.txt").write_text(text, encoding="utf-8")
    (REPORTS / "combine.json").write_text(
        json.dumps(
            {
                "min_bank": min_bank,
                "need": need,
                "last": last_px,
                "bank": bank,
                "risk": risk,
                "from": begin.isoformat(),
                "simple": {
                    "end": simple_end,
                    "pct": round(s_pct, 1),
                    "n": len(simple_fills),
                    "months": s_months,
                    "fills": [asdict(f) for f in simple_fills],
                },
                "compound": {
                    "end": comp_end,
                    "pct": round(c_pct, 1),
                    "n": len(comp_fills),
                    "months": c_months,
                    "fills": [asdict(f) for f in comp_fills],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("\n".join(head), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
