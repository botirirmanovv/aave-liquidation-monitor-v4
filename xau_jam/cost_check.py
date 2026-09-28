"""Re-score the TSLA 3mo book with spread, slip, commission.

  python3 -m xau_jam.cost_check
"""
from __future__ import annotations

from xau_jam.auto_best import _impulse_trades, run_book
from xau_jam.backtest import Trade
from xau_jam.burst_open import REPORTS
from xau_jam.data import fetch_yahoo
from xau_jam.pattern import Bar


def _impulse(bars: list[Bar], trigger_pct: float, hold: int, skip_both: bool) -> list[Trade]:
    return _impulse_trades(bars, trigger_pct, hold, skip_both=skip_both)


def run_costed(
    trades: list[Trade],
    *,
    start: float,
    leverage: int,
    half_spread: float,
    slip: float,
    comm_share: float,
    comm_min: float,
    extra_bps_rt: float,
) -> tuple[float, float, int, bool]:
    eq = start
    peak = start
    dd = 0.0
    n = 0
    blown = False
    for t in trades:
        if blown or eq <= 0:
            break
        notional = eq * leverage
        shares = notional / max(t.entry, 1e-9)
        tax = half_spread + slip
        if t.side == "buy":
            e = t.entry + tax
            x = t.exit - tax
            pnl_ps = x - e
        else:
            e = t.entry - tax
            x = t.exit + tax
            pnl_ps = e - x
        ret = pnl_ps / max(t.entry, 1e-9) - extra_bps_rt / 10000.0
        comm = 2.0 * max(comm_min, comm_share * shares)
        eq += notional * ret - comm
        if eq <= 0:
            eq = 0.0
            blown = True
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        n += 1
    return round(eq, 2), round(dd, 2), n, blown


def main() -> int:
    bars = fetch_yahoo("TSLA", "3mo", "60m")
    raw = _impulse(bars, 0.003, 6, skip_both=False)
    clean = _impulse(bars, 0.003, 6, skip_both=True)
    gross = run_book(raw, 500, 10, "gross", "TSLA", "g")

    rows = [
        (
            "как было (без комиссий, фитиль выбирает сторону)",
            *run_costed(
                raw, start=500, leverage=10, half_spread=0, slip=0, comm_share=0, comm_min=0, extra_bps_rt=0
            ),
        ),
        (
            "спред $0.02 + слип $0.03 + комиссия $0.005/шт min $1",
            *run_costed(
                raw, start=500, leverage=10, half_spread=0.02, slip=0.03, comm_share=0.005, comm_min=1, extra_bps_rt=0
            ),
        ),
        (
            "то же + 10 б.п. туда-обратно (проскальз. на размере)",
            *run_costed(
                raw, start=500, leverage=10, half_spread=0.02, slip=0.03, comm_share=0.005, comm_min=1, extra_bps_rt=10
            ),
        ),
        (
            "без дней где оба фитиля + спред/комиссия",
            *run_costed(
                clean, start=500, leverage=10, half_spread=0.02, slip=0.03, comm_share=0.005, comm_min=1, extra_bps_rt=0
            ),
        ),
        (
            "без обоих фитилей + спред/комиссия + 10 б.п.",
            *run_costed(
                clean, start=500, leverage=10, half_spread=0.02, slip=0.03, comm_share=0.005, comm_min=1, extra_bps_rt=10
            ),
        ),
    ]

    lines = [
        "Комиссии в $1.9M не были. Пересчёт TSLA 0.3% hold=6, 3 мес, $500, 1:10.",
        f"баров={len(bars)} {bars[0].time.date()} → {bars[-1].time.date()}  сырых сделок={len(raw)}  без обоих фитилей={len(clean)}",
        f"гросс сверка: ${gross.end:.2f}",
        "",
    ]
    for name, end, dd, n, blown in rows:
        pct = 100.0 * (end - 500) / 500
        dead = " СГОРЕЛ" if blown else ""
        lines.append(f"  {end:12.2f}$  ({pct:+.1f}%)  n={n} DD=${dd:.0f}{dead}  {name}")
    lines.append("")
    lines.append("Комиссия брокера сама по себе почти ничего не ест.")
    lines.append("Сомнительно из‑за выбора стороны по большему фитилю и нулевого спреда на выходе.")
    text = "\n".join(lines) + "\n"
    (REPORTS / "auto_best_costs.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
