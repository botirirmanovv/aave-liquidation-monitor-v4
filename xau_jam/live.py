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
from xau_jam.data import fetch_yahoo
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


def _report(broker: DemoBroker, title: str) -> str:
    pct = 100.0 * (broker.equity - broker.start) / broker.start if broker.start else 0.0
    closes = [o for o in broker.orders if o.reason == "close"]
    lines = [
        title,
        f"${broker.start:.0f} → ${broker.equity:.2f}  ({pct:+.1f}%)  ордеров={len(broker.orders)}  сделок={len(closes)}",
        "демо-счёт, не биржа. TSLA 0.3% одна сторона, 6 H1, 1:10, те же издержки.",
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
        bars = fetch_yahoo(SYMBOL, "3mo", "60m")
        replay_through_broker(bars, broker, 7 * args.weeks)
        text = _report(
            broker,
            f"Демо-счёт TSLA, старт ${args.bank:.0f}, {args.weeks} нед",
        )
        (REPORTS / "live_demo.txt").write_text(text, encoding="utf-8")
        print(text, end="")
        print(f"счёт: {DEMO_PATH}  Это демо, не брокер.")
        return 0

    if args.once or not args.loop:
        bars = fetch_yahoo(SYMBOL, "5d", "60m")
        note = tick(bars, broker)
        print(note)
        return 0

    import time

    while True:
        try:
            bars = fetch_yahoo(SYMBOL, "5d", "60m")
            note = tick(bars, broker)
        except Exception as exc:  # noqa: BLE001
            note = f"ошибка: {exc}"
        print(note, flush=True)
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
