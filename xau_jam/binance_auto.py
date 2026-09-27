"""Paper bot: find the Binance impulse and open/close it by itself.

Rule (locked to the 1000% hunt): UTC-day open, first 1% one-side, skip both-wick,
hold through day+2, isolated x50, all-in one position. Paper fills only.

  python3 -m xau_jam.binance_auto --replay --bank 500
  python3 -m xau_jam.binance_auto --once --bank 500
  python3 -m xau_jam.binance_auto --loop --bank 500

Not live Binance. Not Aave.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from xau_jam.binance_spike import futures_cash, hit_liq
from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_binance_klines
from xau_jam.paper import day_groups, signal_for_day
from xau_jam.pattern import Bar

STATE_PATH = REPORTS / "binance_auto_state.json"
TRIGGER = 0.01
HOLD_DAYS = 2
LEV = 50
WATCH = (
    "ENAUSDT",
    "STRKUSDT",
    "SAGAUSDT",
    "AVAXUSDT",
    "ARBUSDT",
    "INJUSDT",
    "WIFUSDT",
    "XRPUSDT",
    "BOMEUSDT",
    "AAVEUSDT",
    "1000PEPEUSDT",
    "TRUMPUSDT",
)


@dataclass(slots=True)
class Fill:
    time: str
    symbol: str
    side: str
    entry: float
    exit: float
    cash: float
    equity: float
    event: str


def state_path(bank: float) -> Path:
    return REPORTS / f"binance_auto_state_{bank:.0f}.json"


def hold_exit(h1: list[Bar], fi: int, day: date) -> int:
    later = [j for j in range(fi, len(h1)) if h1[j].time.date() <= day + timedelta(days=HOLD_DAYS)]
    return later[-1] if later else fi


def first_signal(book: dict[str, list[Bar]], day: date, trigger: float = TRIGGER):
    """First honest impulse that day, watchlist order."""
    for sym, h1 in book.items():
        idxs = day_groups(h1).get(day)
        if not idxs:
            continue
        sig = signal_for_day(h1, idxs, trigger=trigger)
        if sig is None:
            continue
        side, fi, fill = sig
        return sym, h1, side, fi, fill
    return None


def replay(
    book: dict[str, list[Bar]],
    start: float,
    lev: int = LEV,
    trigger: float = TRIGGER,
    simple: bool = True,
) -> tuple[float, list[Fill]]:
    days = sorted({b.time.date() for h1 in book.values() for b in h1})
    eq = start
    fills: list[Fill] = []
    busy_until: date | None = None
    for day in days:
        if eq <= 0:
            break
        if busy_until is not None and day <= busy_until:
            continue
        got = first_signal(book, day, trigger)
        if got is None:
            continue
        sym, h1, side, fi, fill = got
        stake = start if simple else eq
        ex_i = hold_exit(h1, fi, day)
        path = h1[fi : ex_i + 1]
        if hit_liq(side, fill, path, lev):
            cash = -stake
            event = "liq"
        else:
            cash = futures_cash(side, fill, h1[ex_i].close, stake, lev)
            event = "ok"
        eq = max(0.0, eq + cash)
        fills.append(
            Fill(
                h1[fi].time.isoformat(),
                sym,
                side,
                fill,
                h1[ex_i].close,
                round(cash, 2),
                round(eq, 2),
                event,
            )
        )
        busy_until = h1[ex_i].time.date()
        if event == "liq":
            break
    return round(eq, 2), fills


def fresh_state(bank: float, lev: int) -> dict:
    return {
        "venue": "binance-paper",
        "start": bank,
        "equity": bank,
        "leverage": lev,
        "pos": None,
        "fills": [],
        "note": "",
    }


def load_state(bank: float, lev: int) -> dict:
    path = state_path(bank)
    if path.exists() and path.stat().st_size > 0:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("leverage", lev)
        return raw
    return fresh_state(bank, lev)


def save_state(bank: float, state: dict) -> Path:
    state["updated"] = datetime.now(timezone.utc).isoformat()
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = state_path(bank)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return path


def _index_at(h1: list[Bar], iso: str) -> int | None:
    for i, b in enumerate(h1):
        if b.time.isoformat() == iso:
            return i
    return None


def tick(book: dict[str, list[Bar]], state: dict, trigger: float = TRIGGER) -> dict:
    """One pass: paper open or flatten from the latest 1h bars."""
    lev = int(state.get("leverage") or LEV)
    pos = state.get("pos")
    if pos:
        h1 = book.get(pos["symbol"]) or []
        fi = _index_at(h1, pos["entry_time"])
        if fi is None:
            state["note"] = f"вход {pos['symbol']} не в барах, жду"
            return state
        day = datetime.fromisoformat(pos["entry_time"]).date()
        due = hold_exit(h1, fi, day)
        path = h1[fi:]
        if hit_liq(pos["side"], pos["entry"], path, lev):
            cash = -float(pos["stake"])
            state["equity"] = max(0.0, round(state["equity"] + cash, 2))
            state["fills"].append(
                asdict(
                    Fill(pos["entry_time"], pos["symbol"], pos["side"], pos["entry"], path[-1].close, cash, state["equity"], "liq")
                )
            )
            state["pos"] = None
            state["note"] = f"ликвид {pos['symbol']} eq=${state['equity']:.2f}"
            return state
        if len(h1) - 1 < due:
            left = due - (len(h1) - 1)
            state["note"] = f"открыт {pos['side']} {pos['symbol']}, выход через {left} H1"
            return state
        exit_px = h1[due].close
        cash = futures_cash(pos["side"], pos["entry"], exit_px, float(pos["stake"]), lev)
        state["equity"] = max(0.0, round(state["equity"] + cash, 2))
        state["fills"].append(
            asdict(
                Fill(pos["entry_time"], pos["symbol"], pos["side"], pos["entry"], exit_px, round(cash, 2), state["equity"], "ok")
            )
        )
        state["pos"] = None
        state["note"] = f"закрыл {pos['symbol']} {cash:+.2f} eq=${state['equity']:.2f}"
        return state

    today = max((h1[-1].time.date() for h1 in book.values() if h1), default=None)
    if today is None:
        state["note"] = "нет баров"
        return state
    got = first_signal(book, today, trigger)
    if got is None:
        state["note"] = f"{today} нет импульса 1% в одну сторону"
        return state
    sym, h1, side, fi, fill = got
    stake = float(state["equity"])
    if stake <= 0:
        state["note"] = "банк 0"
        return state
    state["pos"] = {
        "symbol": sym,
        "side": side,
        "entry": fill,
        "entry_time": h1[fi].time.isoformat(),
        "stake": stake,
    }
    state["note"] = f"открыл {side} {sym} @ {fill:.6g} x{lev}"
    return state


def fetch_book(symbols: tuple[str, ...] | list[str], limit: int = 80) -> dict[str, list[Bar]]:
    book: dict[str, list[Bar]] = {}
    for i, sym in enumerate(symbols):
        try:
            book[sym] = fetch_binance_klines(sym, "1h", limit)
        except Exception as exc:
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        if i + 1 < len(symbols):
            time.sleep(0.05)
    return book


def _report(start: float, end: float, fills: list[Fill], title: str) -> str:
    pct = 100.0 * (end - start) / start if start else 0.0
    lines = [
        title,
        f"${start:.0f} → ${end:.2f}  ({pct:+.1f}%)  сделок={len(fills)}  liq={sum(1 for f in fills if f.event == 'liq')}",
        f"правила: Binance USDT-M бумага, импульс {TRIGGER:.0%} одна сторона, hold {HOLD_DAYS}д, x{LEV}, all-in, не live.",
        "",
    ]
    for i, f in enumerate(fills, 1):
        lines.append(
            f"  {i:02d} {f.time} {f.symbol:16} {f.side:4} {f.entry:.6g}→{f.exit:.6g}  "
            f"{f.cash:+.2f}  eq=${f.equity:.2f}  {f.event}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=LEV)
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    ap.add_argument("--live", action="store_true", help="отказано: живые ордера не шлём")
    ap.add_argument("--symbols", default=",".join(WATCH))
    args = ap.parse_args()
    if args.live:
        print("live Binance не включён. Сначала бумага: --replay / --once / --loop")
        return 2
    REPORTS.mkdir(parents=True, exist_ok=True)
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())

    if args.replay:
        print("binance paper replay", " ".join(symbols), flush=True)
        book = fetch_book(symbols, 1500)
        end, fills = replay(book, args.bank, args.leverage)
        text = _report(args.bank, end, fills, f"Бумага Binance x{args.leverage}, старт ${args.bank:.0f}")
        (REPORTS / "binance_auto.txt").write_text(text, encoding="utf-8")
        print(text, end="")
        return 0

    book = fetch_book(symbols, 80)
    state = load_state(args.bank, args.leverage)
    state["leverage"] = args.leverage
    if args.once or not args.loop:
        state = tick(book, state)
        save_state(args.bank, state)
        print(state.get("note", ""), f"eq=${state['equity']:.2f}")
        return 0

    while True:
        ts = datetime.now(timezone.utc).isoformat()
        try:
            book = fetch_book(symbols, 80)
            state = load_state(args.bank, args.leverage)
            state = tick(book, state)
            save_state(args.bank, state)
            note = f"{ts} {state.get('note', '')} eq=${state['equity']:.2f}"
        except Exception as exc:  # noqa: BLE001
            note = f"{ts} ошибка: {exc}"
        print(note, flush=True)
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
