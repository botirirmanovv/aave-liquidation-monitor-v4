"""Hunt a Binance USDT-M spike that can print ~1000% of the bank in one trade.

Picks isolated leverage (default 50x — allowed on small $500 notional on most perps).
Honest one-side impulse + gap. Also records the day's wick (the raw spike).

Taker 0.05% ×2, slip 0.03% ×2, isolated liq. Not live.

  python3 -m xau_jam.binance_spike --bank 500
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import timedelta

from xau_jam.burst_open import REPORTS
from xau_jam.data import binance_ticker_24h, binance_usdt_perps, fetch_binance_klines
from xau_jam.paper import day_groups, signal_for_day
from xau_jam.pattern import Bar

TAKER = 0.0005
SLIP = 0.0003
MAINT = 0.004
CORE = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "1000PEPEUSDT",
    "1000BONKUSDT",
    "WIFUSDT",
    "WLDUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "SUIUSDT",
    "INJUSDT",
    "TIAUSDT",
    "ORDIUSDT",
    "NEARUSDT",
    "APTUSDT",
    "ENAUSDT",
    "OPUSDT",
    "ARBUSDT",
    "SEIUSDT",
    "JUPUSDT",
    "TONUSDT",
    "NOTUSDT",
    "BOMEUSDT",
    "PNUTUSDT",
    "WUSDT",
    "TRUMPUSDT",
    "FARTCOINUSDT",
    "HYPEUSDT",
    "TAOUSDT",
    "RENDERUSDT",
    "FETUSDT",
    "AAVEUSDT",
    "LTCUSDT",
    "ADAUSDT",
    "DOTUSDT",
    "FILUSDT",
    "ATOMUSDT",
    "GALAUSDT",
    "SANDUSDT",
    "MANAUSDT",
    "APEUSDT",
    "BLURUSDT",
    "STRKUSDT",
    "TIAUSDT",
    "SAGAUSDT",
    "ORDIUSDT",
    "1000FLOKIUSDT",
    "1000SHIBUSDT",
    "NEIROUSDT",
    "ACTUSDT",
    "GOATUSDT",
    "PENGUUSDT",
    "VIRTUALUSDT",
    "AI16ZUSDT",
)


@dataclass(slots=True)
class Shot:
    symbol: str
    style: str
    day: str
    side: str
    entry: float
    exit: float
    lev: int
    ret_pct: float
    bank_pct: float
    cash: float
    liq: bool


def pick_leverage(symbol: str) -> int:
    """Isolated X I pick for $500. Majors 50 (cap 125/100), alts 50 (typical max)."""
    if symbol in ("BTCUSDT", "ETHUSDT"):
        return 50
    return 50


def liq_price(side: str, entry: float, lev: int) -> float:
    dist = max(1.0 / lev - MAINT, 0.4 / lev)
    return entry * (1.0 - dist) if side == "buy" else entry * (1.0 + dist)


def hit_liq(side: str, entry: float, bars: list[Bar], lev: int) -> bool:
    px = liq_price(side, entry, lev)
    for b in bars:
        if side == "buy" and b.low <= px:
            return True
        if side == "sell" and b.high >= px:
            return True
    return False


def futures_cash(side: str, entry: float, exit_px: float, bank: float, lev: int) -> float:
    notional = bank * lev
    qty = notional / max(entry, 1e-12)
    fee = notional * TAKER + qty * exit_px * TAKER
    slip = notional * SLIP + qty * exit_px * SLIP
    if side == "buy":
        pnl = qty * (exit_px - entry)
    else:
        pnl = qty * (entry - exit_px)
    return pnl - fee - slip


def _score(
    symbol: str,
    style: str,
    side: str,
    entry: float,
    exit_px: float,
    day: str,
    bank: float,
    lev: int,
    path: list[Bar] | None = None,
) -> Shot | None:
    if entry <= 0 or exit_px <= 0:
        return None
    dead = bool(path) and hit_liq(side, entry, path, lev)
    cash = -bank if dead else futures_cash(side, entry, exit_px, bank, lev)
    ret = (exit_px - entry) / entry if side == "buy" else (entry - exit_px) / entry
    return Shot(
        symbol=symbol,
        style=style,
        day=day,
        side=side,
        entry=entry,
        exit=exit_px,
        lev=lev,
        ret_pct=round(100.0 * ret, 2),
        bank_pct=round(100.0 * cash / bank, 1),
        cash=round(cash, 2),
        liq=dead,
    )


def hunt_h1(sym: str, h1: list[Bar], bank: float, lev: int) -> list[Shot]:
    out: list[Shot] = []
    by = day_groups(h1)
    for trig in (0.006, 0.01, 0.02, 0.03):
        for day, idxs in by.items():
            sig = signal_for_day(h1, idxs, trigger=trig)
            if sig is None:
                continue
            side, fi, fill = sig
            last_same = idxs[-1]
            holds = {
                "hold1": min(len(h1) - 1, fi + 1),
                "hold6": min(len(h1) - 1, fi + 6),
                "hold24": min(len(h1) - 1, fi + 24),
                "eod": last_same,
            }
            later = [j for j in range(fi, len(h1)) if h1[j].time.date() <= day + timedelta(days=2)]
            if later:
                holds["hold2d"] = later[-1]
            for name, ex_i in holds.items():
                path = h1[fi : ex_i + 1]
                got = _score(
                    sym,
                    f"impulse {trig:.1%} {name}",
                    side,
                    fill,
                    h1[ex_i].close,
                    day.isoformat(),
                    bank,
                    lev,
                    path,
                )
                if got:
                    out.append(got)
    return out


def hunt_daily(sym: str, d1: list[Bar], bank: float, lev: int) -> list[Shot]:
    out: list[Shot] = []
    for i in range(1, len(d1)):
        prev, b = d1[i - 1], d1[i]
        if prev.close <= 0 or b.open <= 0:
            continue
        day = b.time.date().isoformat()
        gap = (b.open - prev.close) / prev.close
        if abs(gap) >= 0.03:
            side = "buy" if gap > 0 else "sell"
            nxt = d1[min(len(d1) - 1, i + 1)]
            for name, ex, path in (
                ("gap-eod", b.close, [b]),
                ("gap-2d", nxt.close, [b, nxt]),
            ):
                got = _score(sym, name, side, b.open, ex, day, bank, lev, path)
                if got:
                    out.append(got)
        # raw spike: open → extreme (the jump itself)
        for style, side, ex in (
            ("wick-high", "buy", b.high),
            ("wick-low", "sell", b.low),
            ("oc-buy", "buy", b.close),
            ("oc-sell", "sell", b.close),
        ):
            got = _score(sym, style, side, b.open, ex, day, bank, lev, [b])
            if got:
                out.append(got)
    return out


def pick_symbols(limit: int = 48) -> list[str]:
    perps = {s["symbol"] for s in binance_usdt_perps()}
    ticks = [t for t in binance_ticker_24h() if t.get("symbol") in perps]
    by_vol = sorted(ticks, key=lambda t: float(t.get("quoteVolume") or 0), reverse=True)
    by_pct = sorted(ticks, key=lambda t: abs(float(t.get("priceChangePercent") or 0)), reverse=True)
    out: list[str] = []
    for src in (CORE, [t["symbol"] for t in by_vol[:24]], [t["symbol"] for t in by_pct[:24]]):
        for s in src:
            if s in perps and s not in out:
                out.append(s)
            if len(out) >= limit:
                return out
    return out


def _uniq(shots: list[Shot]) -> list[Shot]:
    shots = sorted(shots, key=lambda s: s.bank_pct, reverse=True)
    uniq: list[Shot] = []
    seen: set[tuple[str, str, str]] = set()
    for s in shots:
        key = (s.symbol, s.day, s.style.split()[0])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=0, help="0 = pick per symbol (50x)")
    ap.add_argument("--limit", type=int, default=48)
    args = ap.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    print("binance spike hunt — pick symbols", flush=True)
    symbols = pick_symbols(args.limit)
    print(f"  {len(symbols)} USDT-M perps", flush=True)
    shots: list[Shot] = []
    for i, sym in enumerate(symbols, 1):
        lev = args.leverage or pick_leverage(sym)
        try:
            d1 = fetch_binance_klines(sym, "1d", 1000)
            time.sleep(0.05)
            h1 = fetch_binance_klines(sym, "1h", 1500)
            time.sleep(0.05)
        except Exception as exc:
            print(f"  skip {sym}: {exc}", flush=True)
            continue
        got = hunt_daily(sym, d1, args.bank, lev) + hunt_h1(sym, h1, args.bank, lev)
        shots.extend(got)
        best = max(got, key=lambda s: s.bank_pct) if got else None
        if best:
            print(
                f"  [{i}/{len(symbols)}] {sym} x{lev} best {best.bank_pct:+.0f}% {best.style} {best.day}",
                flush=True,
            )
        else:
            print(f"  [{i}/{len(symbols)}] {sym} none", flush=True)

    honest = [s for s in shots if s.style.startswith(("impulse", "gap-"))]
    wick = [s for s in shots if s.style.startswith("wick-")]
    fat_h = [s for s in honest if s.bank_pct >= 1000]
    fat_w = [s for s in wick if s.bank_pct >= 1000]
    uniq_h = _uniq(honest)
    uniq_w = _uniq(wick)
    lev_used = args.leverage or 50
    need = 1000.0 / lev_used
    lines = [
        f"Binance USDT-M, all-in ${args.bank:.0f}, плечо x{lev_used} (сам выбрал; isolated, мелкий номинал).",
        f"теер 0.05%×2 + слип 0.03%×2, ликвидация isolated (~{100.0 / lev_used - 100.0 * MAINT:.1f}% против).",
        f"честных кандидатов={len(honest)}  ≥1000%={len(fat_h)}   wick-скачков={len(wick)}  ≥1000%={len(fat_w)}",
        f"1000% банка на x{lev_used} = ход цены ~{need:.1f}%.",
        "",
        "честный импульс/гэп — топ-12 разных дней:",
    ]
    for s in uniq_h[:12]:
        mark = " LIQ" if s.liq else ""
        lines.append(
            f"  {s.bank_pct:+8.1f}% банка  цена {s.ret_pct:+6.1f}%  {s.day} {s.symbol:16} "
            f"{s.side:4} {s.entry:.6g}→{s.exit:.6g}  {s.style}{mark}"
        )
    lines.append("")
    lines.append("сырой скачок дня (open→high / open→low) — топ-12:")
    for s in uniq_w[:12]:
        mark = " LIQ" if s.liq else ""
        lines.append(
            f"  {s.bank_pct:+8.1f}% банка  цена {s.ret_pct:+6.1f}%  {s.day} {s.symbol:16} "
            f"{s.side:4} {s.entry:.6g}→{s.exit:.6g}  {s.style}{mark}"
        )
    lines.append("")
    if fat_h:
        lines.append("есть ≥1000% за одну честную сделку:")
        shown = _uniq(fat_h)
        for s in shown[:10]:
            lines.append(f"  {s.symbol} {s.day} {s.bank_pct:+.0f}%  цена {s.ret_pct:+.1f}%  {s.style}")
    elif fat_w:
        best = uniq_w[0]
        lines.append(
            f"честного ≥1000% нет. На самом скачке (wick) есть: {best.symbol} {best.day} "
            f"{best.bank_pct:+.0f}% банка при ходе {best.ret_pct:+.1f}%."
        )
    else:
        best = (uniq_h or uniq_w or [None])[0]
        if best:
            lines.append(
                f"1000% не нашёл. Максимум {best.bank_pct:+.0f}% ({best.symbol} {best.day}, "
                f"цена {best.ret_pct:+.1f}%). На этом ходе нужно ~"
                f"{int(round(1000 / max(best.ret_pct, 0.1)))}x."
            )
    text = "\n".join(lines) + "\n"
    (REPORTS / "binance_spike.txt").write_text(text, encoding="utf-8")
    (REPORTS / "binance_spike.json").write_text(
        json.dumps(
            {
                "leverage": lev_used,
                "n_honest_1000": len(fat_h),
                "n_wick_1000": len(fat_w),
                "honest": [asdict(s) for s in uniq_h[:30]],
                "wick": [asdict(s) for s in uniq_w[:30]],
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
