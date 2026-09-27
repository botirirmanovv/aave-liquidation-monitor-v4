"""Paper-trade the locked impulse: TSLA, skip both-wick, costs on.

Honest rules: skip if first bar hits both sides, spread/slip/commission on.
Whole shares. One position. Not a live broker.

  python3 -m xau_jam.paper --replay --bank 500 --months 3 6 9
  python3 -m xau_jam.paper --watch --bank 500
  python3 -m xau_jam.paper --loop --bank 500
  python3 -m xau_jam.auto --bank 500
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_yahoo
from xau_jam.pattern import Bar

STATE_PATH = REPORTS / "paper_state.json"
WATCH_LOG = REPORTS / "paper_watch.log"
CRON_MARK = "xau_jam.paper --watch"
SYMBOL = "TSLA"
TRIGGER = 0.003
HOLD = 6
HALF_SPREAD = 0.02
SLIP = 0.03
COMM_SHARE = 0.005
COMM_MIN = 1.0


@dataclass(slots=True)
class Fill:
    time: str
    side: str
    shares: int
    entry: float
    exit: float
    cash: float
    equity: float
    event: str


def day_groups(bars: list[Bar]) -> dict:
    by: dict = defaultdict(list)
    for i, b in enumerate(bars):
        by[b.time.date()].append(i)
    return by


def state_path(bank: float) -> Path:
    return REPORTS / f"paper_state_{bank:.0f}.json"


def signal_for_day(
    bars: list[Bar], idxs: list[int], trigger: float = TRIGGER
) -> tuple[str, int, float] | None:
    """One-sided impulse only. None if quiet or both wicks."""
    oi = idxs[0]
    session_px = bars[oi].open
    trig = session_px * trigger
    last_hunt = idxs[min(8, len(idxs) - 1)]
    for i in range(oi, last_hunt + 1):
        up = bars[i].high - session_px
        dn = session_px - bars[i].low
        if up < trig and dn < trig:
            continue
        if up >= trig and dn >= trig:
            return None
        if up >= dn:
            fill = session_px + trig
            if bars[i].high < fill:
                continue
            return "buy", i, fill
        fill = session_px - trig
        if bars[i].low > fill:
            continue
        return "sell", i, fill
    return None


def costed_cash(side: str, entry: float, exit_px: float, shares: int) -> float:
    tax = HALF_SPREAD + SLIP
    if side == "buy":
        e, x = entry + tax, exit_px - tax
        pnl = (x - e) * shares
    else:
        e, x = entry - tax, exit_px + tax
        pnl = (e - x) * shares
    comm = 2.0 * max(COMM_MIN, COMM_SHARE * shares)
    return pnl - comm


def replay(bars: list[Bar], start: float, leverage: int, weeks: int = 0, days: int | None = None) -> tuple[float, list[Fill]]:
    if not bars:
        return start, []
    span = days if days is not None else 7 * weeks
    cutoff = bars[-1].time.date() - timedelta(days=span)
    by = day_groups(bars)
    eq = start
    path: list[Fill] = []
    for day in sorted(by):
        if day < cutoff:
            continue
        sig = signal_for_day(bars, by[day])
        if sig is None:
            continue
        side, fi, fill = sig
        if eq <= 0:
            break
        notional = eq * leverage
        shares = int(notional / fill)
        if shares < 1:
            path.append(Fill(bars[fi].time.isoformat(), side, 0, fill, fill, 0.0, eq, "no_share"))
            continue
        ex_i = min(len(bars) - 1, fi + HOLD)
        exit_px = bars[ex_i].close
        cash = costed_cash(side, fill, exit_px, shares)
        eq += cash
        event = "ok"
        if eq <= 0:
            eq = 0.0
            event = "blown"
        path.append(
            Fill(
                bars[fi].time.isoformat(),
                side,
                shares,
                round(fill, 4),
                round(exit_px, 4),
                round(cash, 2),
                round(eq, 2),
                event,
            )
        )
        if event == "blown":
            break
    return round(eq, 2), path


def _index_at(bars: list[Bar], iso: str) -> int | None:
    for i, b in enumerate(bars):
        if b.time.isoformat() == iso:
            return i
    return None


def watch(bars: list[Bar], state: dict, leverage: int) -> dict:
    """One pass: open or flatten paper from the latest session."""
    if not bars:
        state["note"] = "нет баров"
        return state
    by = day_groups(bars)
    today = bars[-1].time.date()
    idxs = by.get(today) or []
    pos = state.get("pos")
    if pos:
        fi = _index_at(bars, pos["entry_time"])
        if fi is None:
            state["note"] = "вход не найден в новых барах, жду"
            return state
        due = fi + HOLD
        if len(bars) - 1 < due:
            state["note"] = f"открыт {pos['side']} {pos['shares']}шт, выход через {due - (len(bars) - 1)} бар."
            return state
        exit_px = bars[due].close
        cash = costed_cash(pos["side"], pos["entry"], exit_px, pos["shares"])
        state["equity"] = round(state["equity"] + cash, 2)
        state.setdefault("fills", []).append(
            asdict(
                Fill(
                    pos["entry_time"],
                    pos["side"],
                    pos["shares"],
                    pos["entry"],
                    round(exit_px, 4),
                    round(cash, 2),
                    state["equity"],
                    "ok",
                )
            )
        )
        state["pos"] = None
        state["note"] = f"закрыл {pos['side']} {cash:+.2f} eq=${state['equity']:.2f}"
        return state
    if not idxs:
        state["note"] = "сегодня нет баров"
        return state
    sig = signal_for_day(bars, idxs)
    if sig is None:
        state["note"] = f"{today} нет одностороннего импульса 0.3%"
        return state
    side, fi, fill = sig
    shares = int(state["equity"] * leverage / fill)
    if shares < 1:
        state["note"] = "не хватает на 1 акцию"
        return state
    state["pos"] = {
        "side": side,
        "shares": shares,
        "entry": round(fill, 4),
        "entry_time": bars[fi].time.isoformat(),
    }
    state["note"] = f"открыл {side} {shares}шт @ {fill:.2f}"
    return state


def fresh_state(bank: float, leverage: int) -> dict:
    return {
        "equity": bank,
        "start": bank,
        "leverage": leverage,
        "pos": None,
        "fills": [],
        "note": "",
    }


def load_state(bank: float, leverage: int) -> dict:
    path = state_path(bank)
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        state.setdefault("leverage", leverage)
        return state
    if bank == 100 and STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state.setdefault("leverage", leverage)
        return state
    return fresh_state(bank, leverage)


def save_state(bank: float, state: dict) -> Path:
    state["updated"] = datetime.now(timezone.utc).isoformat()
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = state_path(bank)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return path


def run_watch_once(bank: float, leverage: int) -> dict:
    bars = fetch_yahoo(SYMBOL, "5d", "60m")
    state = load_state(bank, leverage)
    state = watch(bars, state, state.get("leverage", leverage))
    save_state(bank, state)
    return state


def cron_line(bank: float) -> str:
    repo = Path(__file__).resolve().parents[1]
    return (
        f"7 * * * * cd {repo} && {sys.executable} -m xau_jam.paper "
        f"--watch --bank {bank:.0f} >> {WATCH_LOG} 2>&1"
    )


def install_cron(bank: float) -> str | None:
    """Write the hourly line. Returns it if crontab accepted, else None."""
    REPORTS.mkdir(parents=True, exist_ok=True)
    line = cron_line(bank)
    (REPORTS / "paper_cron.txt").write_text(line + "\n", encoding="utf-8")
    crontab = shutil.which("crontab")
    if not crontab:
        return None
    try:
        prev = subprocess.check_output([crontab, "-l"], text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        prev = ""
    kept = [row for row in prev.splitlines() if CRON_MARK not in row]
    kept.append(line)
    subprocess.run([crontab, "-"], input="\n".join(kept) + "\n", check=True, text=True)
    return line


def append_watch_log(line: str) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    with WATCH_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line)


def run_loop(bank: float, leverage: int, interval: int) -> None:
    while True:
        ts = datetime.now(timezone.utc).isoformat()
        try:
            state = run_watch_once(bank, leverage)
            row = f"{ts} {state.get('note', '')} eq=${state['equity']:.2f}\n"
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            row = f"{ts} ошибка: {exc}\n"
        print(row, end="", flush=True)
        append_watch_log(row)
        time.sleep(max(30, interval))


def _print_path(start: float, end: float, path: list[Fill], title: str) -> str:
    pct = 100.0 * (end - start) / start if start else 0.0
    lines = [
        title,
        f"${start:.0f} → ${end:.2f}  ({pct:+.1f}%)  сделок={len(path)}",
        "правила: TSLA, импульс 0.3% в одну сторону, 6 H1, 1:10, "
        "спред $0.02+слип $0.03, комиссия $0.005/шт min $1, целые акции.",
        "",
    ]
    for i, f in enumerate(path, 1):
        lines.append(
            f"  {i:02d} {f.time} {f.side:4} {f.shares}шт  "
            f"{f.entry:.2f}→{f.exit:.2f}  {f.cash:+.2f}  eq=${f.equity:.2f}  {f.event}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=100.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--weeks", type=int, default=3)
    ap.add_argument("--months", type=int, nargs="*", default=None)
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)

    if args.watch or args.loop or args.auto:
        cron = install_cron(args.bank)
        if args.loop or args.auto:
            print(
                "автомат честный: TSLA 0.3% одна сторона, 6 H1, 1:10, "
                f"банк ${args.bank:.0f}, тик раз в {args.interval}с"
            )
            print("cron:" if cron else "cron нет, кручу loop:", cron_line(args.bank))
            run_loop(args.bank, args.leverage, args.interval)
            return 0
        state = run_watch_once(args.bank, args.leverage)
        print(state.get("note", ""), f"eq=${state['equity']:.2f}")
        return 0

    months = args.months
    bars = fetch_yahoo(SYMBOL, "1y" if months else "3mo", "60m")
    lines = [f"Бумажный тест TSLA, старт ${args.bank:.0f}, плечо 1:{args.leverage}"]
    results = {}
    if months:
        spans = [(f"{m} мес", 30 * m) for m in months]
    else:
        spans = [(f"{w} недели", 7 * w) for w in ((2, 3) if args.replay or True else (args.weeks,))]
    for label, days in spans:
        end, path = replay(bars, args.bank, args.leverage, days=days)
        first = path[0].time[:10] if path else "?"
        last = path[-1].time[:10] if path else "?"
        block = _print_path(args.bank, end, path, f"── {label} ({first} → {last}) ──")
        lines.append(block)
        results[label] = {
            "end": end,
            "pct": round(100.0 * (end - args.bank) / args.bank, 1),
            "n": len(path),
            "path": [asdict(p) for p in path],
        }
    text = "\n".join(lines) + "\n"
    text += (
        f"Автомат: python3 -m xau_jam.auto --bank {args.bank:.0f}\n"
        f"  или python3 -m xau_jam.paper --loop --bank {args.bank:.0f}\n"
        f"Состояние: {state_path(args.bank)}  Это бумага, не брокер.\n"
    )
    tag = f"{args.bank:.0f}"
    if months:
        tag = f"{tag}_" + "_".join(f"{m}m" for m in months)
    (REPORTS / f"paper_{tag}.txt").write_text(text, encoding="utf-8")
    (REPORTS / f"paper_{tag}.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
