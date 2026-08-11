"""Unit tests for Balancer+V3 arb quoting (no RPC)."""
from __future__ import annotations

import sys
from types import SimpleNamespace

from aave_bot.strategies.balancer_v3_arb import BalancerV3ArbStrategy, V3ArbOpportunity

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def make_strategy() -> BalancerV3ArbStrategy:
    borrow = "0x" + "11" * 20
    mid = "0x" + "22" * 20
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            name="test",
            balancer_arb_enabled=True,
            balancer_arb_bot="",
            balancer_arb_token_refs=["BORROW", "MID"],
            balancer_arb_amount_refs={},
            balancer_arb_min_profit_bps=5,
            balancer_arb_min_profit_token_units=0,
            balancer_arb_scan_interval_seconds=20,
            balancer_arb_default_notional_base=0,
            flash_arb_token_refs=[],
            flash_arb_amount_refs={},
            auto_execute=False,
            has_balancer_arb_credentials=False,
            slippage_tolerance=__import__("decimal").Decimal("0.02"),
        ),
        w3=None,
        account=None,
    )
    ctx.symbol_of = lambda a: {borrow: "BORROW", mid: "MID"}.get(a, a[:8])
    ctx.decimals = lambda a: 18
    ctx.asset_price = lambda a: None
    ctx.all_reserves = lambda: [borrow, mid]

    strategy = BalancerV3ArbStrategy(ctx)
    strategy.tokens = [borrow, mid]
    strategy.amounts = {borrow: 1000 * 10**18}
    strategy._quoter = object()  # mark active
    return strategy, borrow, mid


def test_zero_flash_fee_math() -> None:
    print("\n[balancer_v3: zero flash fee]")
    strategy, borrow, mid = make_strategy()

    # Fake quoter: buy mid at 1.01x, sell back at 1.01x → ~2% gross before nothing
    # Balancer repay = amount (fee 0).
    def quote(token_in, token_out, amount_in, fee):
        if token_in == borrow:
            return amount_in * 10100 // 10_000
        return amount_in * 10100 // 10_000

    strategy._quote_v3 = quote  # type: ignore[method-assign]
    opp = strategy._best_fee_route(borrow, mid, 1000 * 10**18)
    check("mispriced fee tiers produce an opportunity", opp is not None)
    assert opp is not None
    check("gross is after zero flash fee (repay==amount)",
          opp.gross_profit == opp.amount_out_sell - opp.amount, str(opp.gross_profit))
    check("fee buy != fee sell", opp.fee_buy != opp.fee_sell)


def test_flat_market() -> None:
    print("\n[balancer_v3: flat market]")
    strategy, borrow, mid = make_strategy()

    def quote(token_in, token_out, amount_in, fee):
        return amount_in  # 1:1

    strategy._quote_v3 = quote  # type: ignore[method-assign]
    opp = strategy._best_fee_route(borrow, mid, 1000 * 10**18)
    check("1:1 round trip is not an opportunity", opp is None)


def test_same_fee_skipped() -> None:
    print("\n[balancer_v3: same fee]")
    # Contract rejects SameFee; strategy never quotes fee_buy == fee_sell.
    strategy, borrow, mid = make_strategy()
    seen_same = False

    def quote(token_in, token_out, amount_in, fee):
        return amount_in * 11000 // 10_000

    original = strategy._best_fee_route

    def wrapping(borrow_a, mid_a, amount):
        # monkey: ensure internal loops skip equal fees by checking result fees
        opp = original(borrow_a, mid_a, amount)
        return opp

    strategy._quote_v3 = quote  # type: ignore[method-assign]
    opp = strategy._best_fee_route(borrow, mid, 1000 * 10**18)
    if opp:
        check("selected route uses two different fees", opp.fee_buy != opp.fee_sell)
    else:
        # With identical quotes on every fee, first unequal pair still wins.
        check("route found with identical fee quotes", False, "expected an opp")


def test_dataclass() -> None:
    print("\n[balancer_v3: dataclass]")
    opp = V3ArbOpportunity(
        borrow_asset="0x1", mid_asset="0x2", amount=100, fee_buy=500, fee_sell=3000,
        amount_out_buy=110, amount_out_sell=120, gross_profit=20, min_profit=1,
    )
    check("gross stored", opp.gross_profit == 20)


def main() -> int:
    test_zero_flash_fee_math()
    test_flat_market()
    test_same_fee_skipped()
    test_dataclass()
    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for name in failures:
            print(" -", name)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
