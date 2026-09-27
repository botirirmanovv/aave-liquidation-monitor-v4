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


def replay_one(
    shots: list[tuple[datetime, str, str, float, int, list[Bar]]],
    start: float,
    lev: int,
    hold: int = HOLD,
    risk: float = RISK,
    simple: bool = True,
) -> tuple[float, list[Shot]]:
    eq = start
    path: list[Shot] = []
    risk = min(max(risk, 0.0), 1.0)
    for t, sym, side, fill, fi, h1 in shots:
        if eq <= 0:
            break
        base = start if simple else eq
        stake = base * risk
        shares = int(stake * lev / max(fill, 1e-9))
        if shares < 1:
            continue
        ex_i = min(len(h1) - 1, fi + hold)
        exit_px = h1[ex_i].close
        cash = costed_cash(side, fill, exit_px, shares)
        event = "ok"
        if cash < -stake:
            cash = -stake
            event = "clip"
        eq = max(0.0, eq + cash)
        path.append(
            Shot(
                t.isoformat(),
                sym,
                side,
                shares,
                round(fill, 4),
                round(exit_px, 4),
                round(cash, 2),
                round(eq, 2),
                event,
                round(stake, 2),
            )
        )
        if eq <= 0:
            break
    return round(eq, 2), path


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
            notes.append(f"{sym} 10% не хватает на 1шт")
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
        notes.append(f"ставка 10% ×{opened}, общий банк eq=${float(state['equity']):.2f}")
    elif not notes:
        notes.append(f"{today} нет импульса ни по одной бумаге")
    state["note"] = "; ".join(notes)
    return state


def run_watch_once(bank: float, lev: int) -> dict:
    books = fetch_books("5d")
    state = load_state(bank, lev)
    state["leverage"] = lev
    state = watch_book(books, state, lev)
    save_state(bank, state)
    return state


def run_loop(bank: float, lev: int, interval: int) -> None:
    while True:
        ts = datetime.now(timezone.utc).isoformat()
        try:
            state = run_watch_once(bank, lev)
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
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--from", dest="date_from", default="01/01/26")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    if args.once or args.loop:
        print(f"один банк ${args.bank:.0f}, бумаги {', '.join(s for s, _ in BOOK)}", flush=True)
        if args.once or not args.loop:
            state = run_watch_once(args.bank, args.leverage)
            print(state.get("note", ""), f"eq=${state['equity']:.2f}")
            return 0
        run_loop(args.bank, args.leverage, args.interval)
        return 0
    begin = parse_day(args.date_from)
    books: dict[str, tuple[list[Bar], float]] = {}
    singles: list[tuple[str, float, float, int]] = []
    print(f"combine simple ${args.bank:.0f} 1:{args.leverage} from {begin}", flush=True)
    last = begin
    for sym, trig in BOOK:
        try:
            h1 = fetch_yahoo(sym, "1y", "60m")
        except Exception as exc:
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        books[sym] = (h1, trig)
        last = max(last, h1[-1].time.date())
        eq, path = replay(
            h1,
            args.bank,
            args.leverage,
            compound=False,
            begin=begin,
            trigger=trig,
            hold=HOLD,
        )
        taken = [p for p in path if p.event == "ok"]
        pct = 100.0 * (eq - args.bank) / args.bank
        singles.append((sym, eq, pct, len(taken)))
        print(f"  solo {sym} {trig:.1%} ${eq:.0f} ({pct:+.0f}%) n={len(taken)}", flush=True)

    shots = collect_signals(books, begin, None)
    end, fills = replay_one(shots, args.bank, args.leverage, risk=RISK, simple=True)
    pct = 100.0 * (end - args.bank) / args.bank
    by_sym: dict[str, int] = {}
    for f in fills:
        by_sym[f.symbol] = by_sym.get(f.symbol, 0) + 1
    months = max((last - begin).days / 30.0, 1.0)
    n_clip = sum(1 for f in fills if f.event == "clip")
    lines = [
        f"Один бот, один банк ${args.bank:.0f}, 1:{args.leverage}, простой %, ставка {100 * RISK:.0f}%, с {begin} → {last}.",
        "Семья импульса: MSTR/COIN/SMCI/AMD/UVXY/PLTR 0.6%, TSLA 0.3%, hold 6 H1.",
        "Берём все сделки, никого не ждём. Минус клипом не больше ставки. Те же спред/комиссия.",
        f"вместе ${args.bank:.0f} → ${end:.2f}  ({pct:+.1f}%, {pct / months:+.1f}%/мес)  сделок={len(fills)}  clip={n_clip}",
        "по бумагам: " + " ".join(f"{k}={v}" for k, v in sorted(by_sym.items(), key=lambda kv: -kv[1])),
        "",
        "соло 100% банка (для сравнения, не наш размер):",
    ]
    for sym, eq, sp, n in sorted(singles, key=lambda r: -r[2]):
        lines.append(f"  {sym:6} ${eq:8.0f}  {sp:+7.1f}%  n={n}")
    lines.append("")
    for i, f in enumerate(fills, 1):
        lines.append(
            f"  {i:02d} {f.time} {f.symbol:5} {f.side:4} {f.shares}шт  "
            f"{f.entry:.2f}→{f.exit:.2f}  {f.cash:+.2f}  eq=${f.equity:.2f}"
        )
    text = "\n".join(lines) + "\n"
    (REPORTS / "combine.txt").write_text(text, encoding="utf-8")
    (REPORTS / "combine.json").write_text(
        json.dumps(
            {
                "end": end,
                "pct": round(pct, 1),
                "risk": RISK,
                "n": len(fills),
                "by_sym": by_sym,
                "singles": [{"symbol": s, "end": e, "pct": p, "n": n} for s, e, p, n in singles],
                "fills": [asdict(f) for f in fills],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
