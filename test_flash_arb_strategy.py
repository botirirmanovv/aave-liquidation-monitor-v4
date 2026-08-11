"""Unit tests for flash-arb quoting math (no RPC, no wallet)."""
from __future__ import annotations

import sys
from types import SimpleNamespace

from aave_bot.strategies.flash_arb import AAVE_FLASH_PREMIUM_BPS, ArbOpportunity, FlashArbStrategy

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


class FakeQuote:
    def __init__(self, rate_bps: int):
        self.rate_bps = rate_bps

    @property
    def functions(self):
        return self

    def getAmountsOut(self, amount_in, path):
        class Call:
            def __init__(self, amounts):
                self._amounts = amounts

            def call(self):
                return self._amounts

        out = amount_in
        amounts = [amount_in]
        for _ in path[1:]:
            out = out * self.rate_bps // 10_000
            amounts.append(out)
        return Call(amounts)


def make_strategy(min_bps: int = 10) -> FlashArbStrategy:
    borrow = "0x" + "11" * 20
    mid = "0x" + "22" * 20
    buy = "0x" + "aa" * 20
    sell = "0x" + "bb" * 20

    ctx = SimpleNamespace(
        config=SimpleNamespace(
            name="test",
            flash_arb_enabled=True,
            flash_arb_bot="",
            flash_arb_token_refs=["BORROW", "MID"],
            flash_arb_amount_refs={},
            flash_arb_min_profit_bps=min_bps,
            flash_arb_min_profit_token_units=0,
            flash_arb_scan_interval_seconds=30,
            flash_arb_default_notional_base=0,
            routers=[buy, sell],
            auto_execute=False,
            has_flash_arb_credentials=False,
            slippage_tolerance=__import__("decimal").Decimal("0.02"),
            swap_deadline_seconds=120,
            simulate_before_send=True,
            gas_limit_buffer=__import__("decimal").Decimal("1.25"),
        ),
        w3=None,
        routers=[
            {"address": buy, "quote": FakeQuote(11000)},
            {"address": sell, "quote": FakeQuote(10000)},
        ],
        account=None,
    )
    ctx.symbol_of = lambda a: {borrow: "BORROW", mid: "MID"}.get(a, a[:8])
    ctx.decimals = lambda a: 18
    ctx.asset_price = lambda a: None
    ctx.all_reserves = lambda: [borrow, mid]

    strategy = FlashArbStrategy(ctx)
    strategy.tokens = [borrow, mid]
    strategy.amounts = {borrow: 1000 * 10**18}
    return strategy, borrow, mid, buy, sell


def test_premium_constant() -> None:
    print("\n[flash_arb: constants]")
    check("Aave flashLoanSimple premium is 5 bps", AAVE_FLASH_PREMIUM_BPS == 5)


def test_profitable_quote() -> None:
    print("\n[flash_arb: quote detection]")
    strategy, borrow, mid, buy, sell = make_strategy(min_bps=10)
    buy_info = strategy.ctx.routers[0]
    sell_info = strategy.ctx.routers[1]

    opp = strategy._quote(borrow, mid, 1000 * 10**18, buy_info, sell_info)
    check("mispriced venues produce an opportunity", opp is not None)
    assert opp is not None
    check("gross profit is after the Aave premium",
          opp.gross_profit == 995 * 10**17, str(opp.gross_profit))
    check("amount owed includes premium",
          opp.amount_owed == 1000 * 10**18 + 5 * 10**17, str(opp.amount_owed))
    check("routers are recorded",
          opp.router_buy == buy and opp.router_sell == sell)


def test_flat_market_is_ignored() -> None:
    print("\n[flash_arb: no free lunch]")
    strategy, borrow, mid, buy, sell = make_strategy()
    flat = {"address": buy, "quote": FakeQuote(10000)}
    other = {"address": sell, "quote": FakeQuote(10000)}
    opp = strategy._quote(borrow, mid, 1000 * 10**18, flat, other)
    check("1:1 round trip is not an opportunity", opp is None)


def test_scan_finds_directed_route() -> None:
    print("\n[flash_arb: scan]")
    strategy, *_ = make_strategy(min_bps=1)
    found = strategy.scan()
    check("scan returns at least one route", len(found) >= 1, str(len(found)))
    check("opportunities_seen increments", strategy.opportunities_seen >= 1)


def test_size_ladder() -> None:
    print("\n[flash_arb: size ladder]")
    strategy, borrow, *_ = make_strategy()
    ladder = strategy._amounts_ladder(borrow)
    check("ladder has multiple sizes", len(ladder) >= 3, str(ladder))
    check("configured size is in the ladder", 1000 * 10**18 in ladder)
    check("ladder is unique", len(ladder) == len(set(ladder)))


def test_arb_opportunity_fields() -> None:
    print("\n[flash_arb: dataclass]")
    opp = ArbOpportunity(
        borrow_asset="0x1", mid_asset="0x2", amount=100, router_buy="0xa",
        router_sell="0xb", path_buy=["0x1", "0x2"], path_sell=["0x2", "0x1"],
        amount_out_buy=110, amount_out_sell=110, premium=1,
        gross_profit=9, min_profit=1,
    )
    check("amount_owed property", opp.amount_owed == 101)


def main() -> int:
    test_premium_constant()
    test_profitable_quote()
    test_flat_market_is_ignored()
    test_scan_finds_directed_route()
    test_size_ladder()
    test_arb_opportunity_fields()

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
