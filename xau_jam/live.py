"""Live-shaped bot on a demo account. Same honest TSLA impulse.

  python3 -m xau_jam.live --demo --replay --bank 500 --weeks 3
  python3 -m xau_jam.live --demo --once --bank 500
  python3 -m xau_jam.live --demo --loop --bank 500

Not a real broker. Not Aave.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from xau_jam.broker import DEMO_PATH, DemoBroker
from xau_jam.burst_open import REPORTS
from xau_jam.combine import BOOK, collect_signals, fetch_books
from xau_jam.paper import HOLD, SYMBOL, day_groups, signal_for_day
from xau_jam.pattern import Bar


def session_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= minutes <= 20 * 60


def replay_through_broker(bars: list[Bar], broker: DemoBroker, days: int) -> DemoBroker:
    if not bars:
        return broker
    cutoff = bars[-1].time.date() - timedelta(days=days)
    by = day_groups(bars)
    for day in sorted(by):
        if day < cutoff:
            continue
        sig = signal_for_day(bars, by[day])
        if sig is None:
            continue
        side, fi, fill = sig
        shares = int(broker.equity * broker.leverage / fill)
        if shares < 1:
            continue
        broker.submit_market(SYMBOL, side, shares, fill, bars[fi].time.isoformat(), "open")
        ex_i = min(len(bars) - 1, fi + HOLD)
        broker.close(bars[ex_i].close, bars[ex_i].time.isoformat())
        if broker.equity <= 0:
            break
    return broker


def tick(bars: list[Bar], broker: DemoBroker, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if not session_open(now):
        return f"биржа закрыта {now.date()} eq=${broker.equity:.2f}"
    if not bars:
        return "нет баров"
    by = day_groups(bars)
    today = now.date()
    idxs = by.get(today) or []
    if broker.pos:
        fi = None
        for i, b in enumerate(bars):
            if b.time.isoformat() == broker.pos.entry_time:
                fi = i
                break
        if fi is None:
            return "вход не найден в барах, жду"
        due = fi + HOLD
        if len(bars) - 1 < due:
            return f"открыт {broker.pos.side} {broker.pos.qty}шт, выход через {due - (len(bars) - 1)} бар."
        order = broker.close(bars[due].close, bars[due].time.isoformat())
        return f"закрыл {order.side} {order.qty}шт {order.cash:+.2f} eq=${broker.equity:.2f} id={order.id}"
    if not idxs:
        return f"{today} нет баров сессии"
    sig = signal_for_day(bars, idxs)
    if sig is None:
        return f"{today} нет одностороннего импульса 0.3%"
    side, fi, fill = sig
    shares = int(broker.equity * broker.leverage / fill)
    if shares < 1:
        return "не хватает на 1 акцию"
    order = broker.submit_market(SYMBOL, side, shares, fill, bars[fi].time.isoformat(), "open")
    return f"открыл {order.side} {order.qty}шт @ {order.price:.2f} id={order.id}"


def tick_book(books: dict, broker: DemoBroker, now: datetime | None = None) -> str:
    """One shared demo bank across the impulse family."""
    now = now or datetime.now(timezone.utc)
    if not session_open(now):
        return f"биржа закрыта {now.date()} eq=${broker.equity:.2f}"
    if broker.pos:
        packed = books.get(broker.pos.symbol)
        if not packed:
            return f"вход {broker.pos.symbol} нет баров, жду"
        h1, _trig = packed
        fi = None
        for i, b in enumerate(h1):
            if b.time.isoformat() == broker.pos.entry_time:
                fi = i
                break
        if fi is None:
            return "вход не найден в барах, жду"
        due = fi + HOLD
        if len(h1) - 1 < due:
            return f"открыт {broker.pos.side} {broker.pos.symbol} {broker.pos.qty}шт, выход через {due - (len(h1) - 1)} бар."
        sym = broker.pos.symbol
        order = broker.close(h1[due].close, h1[due].time.isoformat())
        return f"закрыл {sym} {order.side} {order.qty}шт {order.cash:+.2f} eq=${broker.equity:.2f} id={order.id}"
    today = now.date()
    shots = collect_signals(books, today, today + timedelta(days=1))
    if not shots:
        return f"{today} нет импульса ни по одной бумаге"
    t, sym, side, fill, fi, h1 = shots[0]
    shares = int(broker.start * broker.leverage / fill)
    if shares < 1:
        return "не хватает на 1 акцию"
    order = broker.submit_market(sym, side, shares, fill, t.isoformat(), "open")
    return f"открыл {order.side} {sym} {order.qty}шт @ {order.price:.2f} id={order.id}"


def replay_book_through_broker(books: dict, broker: DemoBroker, begin) -> DemoBroker:
    shots = collect_signals(books, begin, None)
    free_at = None
    for t, sym, side, fill, fi, h1 in shots:
        if free_at is not None and t < free_at:
            continue
        shares = int(broker.start * broker.leverage / max(fill, 1e-9))
        if shares < 1:
            continue
        broker.submit_market(sym, side, shares, fill, t.isoformat(), "open")
        ex_i = min(len(h1) - 1, fi + HOLD)
        broker.close(h1[ex_i].close, h1[ex_i].time.isoformat())
        free_at = h1[ex_i].time
        if broker.equity <= 0:
            break
    return broker


def _report(broker: DemoBroker, title: str) -> str:
    pct = 100.0 * (broker.equity - broker.start) / broker.start if broker.start else 0.0
    closes = [o for o in broker.orders if o.reason == "close"]
    lines = [
        title,
        f"${broker.start:.0f} → ${broker.equity:.2f}  ({pct:+.1f}%)  ордеров={len(broker.orders)}  сделок={len(closes)}",
        "демо-счёт, не биржа. Один банк на MSTR/COIN/SMCI/AMD/UVXY/PLTR/TSLA, 6 H1, 1:10, простой %.",
        "",
    ]
    for o in broker.orders:
        extra = f"  {o.cash:+.2f}  eq=${o.equity:.2f}" if o.reason == "close" else ""
        lines.append(
            f"  {o.id:8} {o.time} {o.reason:5} {o.side:4} {o.qty}шт @ {o.price:.2f}{extra}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", default=True)
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--weeks", type=int, default=3)
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    broker = DemoBroker(args.bank, args.leverage, DEMO_PATH)

    if args.replay:
        broker.reset(args.bank)
        books = fetch_books("3mo")
        last = max((h1[-1].time.date() for h1, _t in books.values() if h1), default=datetime.now(timezone.utc).date())
        begin = last - timedelta(days=7 * args.weeks)
        replay_book_through_broker(books, broker, begin)
        text = _report(
            broker,
            f"Демо-счёт общий банк, старт ${args.bank:.0f}, {args.weeks} нед, {', '.join(s for s,_ in BOOK)}",
        )
        (REPORTS / "live_demo.txt").write_text(text, encoding="utf-8")
        print(text, end="")
        print(f"счёт: {DEMO_PATH}  Это демо, не брокер. Один банк, не 5 ботов.")
        return 0

    if args.once or not args.loop:
        books = fetch_books("5d")
        note = tick_book(books, broker)
        print(note)
        return 0

    import time

    while True:
        try:
            books = fetch_books("5d")
            note = tick_book(books, broker)
        except Exception as exc:  # noqa: BLE001
            note = f"ошибка: {exc}"
        print(note, flush=True)
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
