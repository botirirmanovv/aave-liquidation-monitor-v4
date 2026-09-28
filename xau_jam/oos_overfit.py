"""Out-of-sample / overfitting check. Same BOOK, same plan, no retune.

Yahoo 60m on 730d is the oldest same-algorithm window we can get.
"""
from __future__ import annotations

import json
import time
from datetime import date

from xau_jam.burst_open import REPORTS
from xau_jam.combine import (
    BOOK,
    MAX_STAKE,
    RUN_RISK,
    START_BANK,
    START_RISK,
    TARGET_BANK,
    collect_signals,
    format_monthly,
    max_drawdown,
    monthly_rows,
    run_replay,
)
from xau_jam.data import fetch_yahoo


def _plan(shots, start: float = START_BANK):
    return run_replay(
        shots,
        start,
        10,
        risk=START_RISK,
        simple=False,
        max_stake=MAX_STAKE,
        target_bank=TARGET_BANK,
        run_risk=RUN_RISK,
        withdraw=True,
    )


def _bank(shots, start: float = START_BANK):
    """Same lots, no monthly withdraw — true bank curve for drawdown."""
    return run_replay(
        shots,
        start,
        10,
        risk=START_RISK,
        simple=False,
        max_stake=MAX_STAKE,
        target_bank=None,
        withdraw=False,
    )


def main() -> int:
    books: dict = {}
    print("fetch 730d 60m", flush=True)
    for i, (sym, trig) in enumerate(BOOK):
        h1 = fetch_yahoo(sym, "730d", "60m")
        books[sym] = (h1, trig)
        print(f"  {sym} {h1[0].time.date()}→{h1[-1].time.date()} n={len(h1)}", flush=True)
        if i + 1 < len(BOOK):
            time.sleep(0.15)
    first = min(h[0].time.date() for h, _ in books.values())
    last = max(h[-1].time.date() for h, _ in books.values())
    lines = [
        "Проверка на переподгонку. Тот же BOOK, 20%→$25k, потолок $2500, 1:10.",
        f"H1 Yahoo 730d: {first} → {last}. 2023 полный год Yahoo 60m не отдаёт (старт ~{first}).",
        "Список и пороги не трогал.",
        "",
    ]
    years = [
        ("2023 (только то, что есть до 31.12)", date(2023, 1, 1), date(2024, 1, 1)),
        ("2024", date(2024, 1, 1), date(2025, 1, 1)),
        ("2025 out-of-sample", date(2025, 1, 1), date(2026, 1, 1)),
        ("2026 in-sample (под него собирали список)", date(2026, 1, 1), date(2027, 1, 1)),
    ]
    year_dd = []
    for title, begin, end in years:
        shots = collect_signals(books, begin, end)
        plan = _plan(shots)
        bank = _bank(shots)
        rows = monthly_rows(plan.fills, START_BANK, plan.withdrawals, TARGET_BANK)
        took = round(sum(float(w["took"]) for w in plan.withdrawals), 2)
        dd = max_drawdown(bank.fills, START_BANK)
        year_dd.append((title, dd, len(bank.fills), bank.end, plan.end, took))
        hit = plan.target_hit
        lines += [
            f"══ {title}  {begin} → {end} (конец не входит)  сделок={len(plan.fills)} ══",
            f"план: банк ${plan.end:.2f}  забрал ${took:.2f}  всего ${plan.end + took:.2f}",
            f"банк без снятия: ${bank.end:.2f}  просадка {dd['dd']}%  пик ${dd['peak']:.0f} {dd['peak_at'][:10]}  дно ${dd['trough']:.0f} {dd['trough_at'][:10]}",
            (
                f"банк $25k: {hit.time[:10]} {hit.symbol} eq=${hit.equity:.2f}"
                if hit
                else "банк $25k не достигли"
            ),
            "",
        ]
        lines += format_monthly(rows, f"── помесячно {title} ──")
        lines.append("")

    all_shots = collect_signals(books, first, None)
    all_bank = _bank(all_shots)
    all_dd = max_drawdown(all_bank.fills, START_BANK)
    lines += [
        f"══ весь H1 {first}→{last}, один банк ${START_BANK:.0f}, без снятия ══",
        f"конец ${all_bank.end:.2f}  сделок={len(all_bank.fills)}",
        f"худшая просадка {all_dd['dd']}%  пик ${all_dd['peak']:.0f} {all_dd['peak_at'][:10]}  дно ${all_dd['trough']:.0f} {all_dd['trough_at'][:10]}",
        "",
        "просадка по годам (каждый год старт $350, без снятия):",
    ]
    for title, dd, n, end, _p, _t in year_dd:
        lines.append(f"  {title}: {dd['dd']}%  n={n}  конец ${end:.0f}  пик ${dd['peak']:.0f} → дно ${dd['trough']:.0f}")
    lines += [
        "",
        "── как собирали семь имён ──",
        "Охота: 27 тикеров (SYMBOLS 21 + EXTRA 6: META/AAPL/AMZN/SMCI/BABA/NFLX).",
        "Стили: impulse 0.3/0.4/0.6% × hold 6/12. В hunt.txt «живых книг»=118 (комбо тикер×стиль с ≥8 сделками).",
        "Окна охоты: ago21-9м (дек 2024–сен 2025), ago21-12м, last-9м (янв–сен 2026).",
        "Семёрка = топ impulse 0.6% hold=6 в hunt_styles С 01.01.26: MSTR COIN SMCI AMD UVXY PLTR TSLA.",
        "Список подбирали по тому же 2026 (и по 2025 в ago21). Это переподгонка вселенной. Правила/пороги после этого не крутил.",
        "2025 в таблице — не чистый OOS: он внутри окна охоты. Чистый OOS: 2023-10–2024-11.",
        "",
        "── жирный первым ──",
        "В live и в бэктесте берём ВСЕ сигналы дня, не победителя дня.",
        "«Жирный» был критерий охоты (кто жирнее в таблице), не правило входа.",
        "Одновременные входы сортируются фиксированным порядком BOOK (MSTR…TSLA) — это не PnL дня.",
        "Сигнал в момент входа: первый односторонний импульс 0.6% (TSLA 0.3%) от open сессии в первых 8 часах.",
        "Он известен, когда закрылся/пропечатался тот H1 бар. Итог дня не нужен.",
        "Both-wick на этом же баре = пропуск дня, тоже видно сразу.",
        "",
        "── вывод ──",
        "Кривая 2026 красивая потому что список сняли с 2026. Это факт.",
        "На 2024 (до охоты) тот же алгоритм тоже дошёл до $25k (12.04.24) и дальше снимал. 2023-Q4 (неполный) +28%/+263%/+270% к $6k, $25k не успели.",
        "Значит правило импульса на этой волатильной семёрке жило и до подгонки. Это не доказательство, что так будет в 2027, и не отменяет отбор имён по прошлому жиру.",
        "Просадка банка без снятия: хуже всего 18.2% в 2024 (пик $12.7k → $10.4k, 8 марта). На всей ленте 2023-10→2026-09 худшая % просадка 14.5% (21–22 ноя 2023).",
        "Live не открывал. Деньги не вносим.",
    ]

    text = "\n".join(lines) + "\n"
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "oos_overfit.txt").write_text(text, encoding="utf-8")
    (REPORTS / "oos_overfit.json").write_text(
        json.dumps(
            {
                "first": first.isoformat(),
                "last": last.isoformat(),
                "all_dd": all_dd,
                "all_end": all_bank.end,
                "years": [
                    {
                        "title": t,
                        "dd": d,
                        "n": n,
                        "bank_end": e,
                        "plan_end": p,
                        "took": tk,
                    }
                    for t, d, n, e, p, tk in year_dd
                ],
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
