"""Rank collateral/debt liquidation pairs by USD (Aave base) value.

Pure helpers — no RPC. ChainContext builds the input rows and prices.
"""
from __future__ import annotations

from collections.abc import Callable


ValueFn = Callable[[str, int], int | None]


def rank_collateral_debt_pairs(
    rows: list[tuple],
    value_in_base: ValueFn,
) -> list[tuple[str, int, str, int, int]]:
    """Return (collateral, col_amt, debt, debt_amt, score) best-first.

    `rows` match ChainContext.user_reserve_data: asset first, aToken/stable/
    variable next, usableAsCollateral last.
    Score prefers higher debt notional in base units; same-asset pairs win
    ties (no swap needed).
    """
    collaterals: list[tuple[str, int, int]] = []
    debts: list[tuple[str, int, int]] = []

    for row in rows:
        if len(row) < 5:
            continue
        asset = row[0]
        a_token = int(row[1])
        stable_debt = int(row[2])
        variable_debt = int(row[3])
        usable = bool(row[-1])

        if usable and a_token > 0:
            col_val = value_in_base(asset, a_token) or 0
            collaterals.append((asset, a_token, col_val))

        total_debt = stable_debt + variable_debt
        if total_debt > 0:
            debt_val = value_in_base(asset, total_debt) or 0
            debts.append((asset, total_debt, debt_val))

    # Sort key packed into score for a stable return type:
    #   debt_usd desc, same-asset preferred, then collateral_usd desc.
    ranked: list[tuple[str, int, str, int, int]] = []
    for c_asset, c_amt, c_val in collaterals:
        for d_asset, d_amt, d_val in debts:
            same = 1 if c_asset.lower() == d_asset.lower() else 0
            score = d_val * 10**18 + same * 10**15 + c_val
            ranked.append((c_asset, c_amt, d_asset, d_amt, score))

    ranked.sort(key=lambda item: item[4], reverse=True)
    return ranked
