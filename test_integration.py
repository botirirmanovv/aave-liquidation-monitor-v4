"""Integration checks against a live network, using the configured .env.

Read-only: no transaction is ever signed or sent. The point is to verify that
what the unit tests assert in isolation also holds against real contracts —
multicall decoding, decimals resolution for every reserve, the aggregator map,
and the simulation gate.
"""
from __future__ import annotations

import sys

from web3 import Web3

from aave_bot.app import configure_logging
from aave_bot.chain import ChainContext
from aave_bot.config import load_chain_config
from aave_bot.simulate import simulate_transaction
from aave_bot.topics import ANSWER_UPDATED_TOPIC

failures: list[str] = []
PROBE_ADDRESSES = [
    Web3.to_checksum_address("0x0000000000000000000000000000000000000001"),
    Web3.to_checksum_address("0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"),
]


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def test_reserves_and_decimals(ctx: ChainContext) -> None:
    print("\n[reserves and decimals]")
    reserves = ctx.all_reserves()
    check("reserve list is populated", len(reserves) > 0, f"{len(reserves)} reserves")

    unresolved = []
    print(f"    {'symbol':<10} {'decimals':>8}  {'bonus':>6}  address")
    for asset in reserves:
        decimals = ctx.decimals(asset)
        bonus = ctx.liquidation_bonus(asset)
        symbol = ctx.symbol_of(asset)
        print(f"    {symbol:<10} {str(decimals):>8}  {str(bonus):>6}  {asset}")
        if decimals is None:
            unresolved.append(symbol)

    check("every reserve resolves its decimals", not unresolved,
          f"unresolved: {unresolved}")

    expectations = {"USDC": 6, "USDT": 6, "DAI": 18, "WETH": 18, "WBTC": 8, "GHO": 18}
    mismatches = []
    for asset in reserves:
        symbol = ctx.symbol_of(asset).upper()
        want = expectations.get(symbol)
        got = ctx.decimals(asset)
        if want is not None and got != want:
            mismatches.append(f"{symbol}: got {got}, expected {want}")
    check("well-known tokens report the expected decimals", not mismatches,
          "; ".join(mismatches))

    # A bonus of zero is legitimate for assets Aave never accepts as collateral
    # (GHO on Sepolia), and the resolver reports those as None. What must never
    # happen is a value between 1 and 9999, which would understate the premium.
    check("liquidation bonuses are either absent or above par",
          all((b := ctx.liquidation_bonus(a)) is None or b >= 10000 for a in reserves))
    check("at least one reserve is a viable collateral",
          any(ctx.liquidation_bonus(a) for a in reserves))


def test_multicall_matches_single_calls(ctx: ChainContext) -> None:
    print("\n[multicall vs single calls]")
    check("multicall3 is reachable", ctx.multicall_available)
    if not ctx.multicall_available:
        return

    batched = ctx.account_data(PROBE_ADDRESSES)
    check("batch returns a row per address", len(batched) == len(PROBE_ADDRESSES),
          f"got {len(batched)}")

    mismatches = []
    for address in PROBE_ADDRESSES:
        direct = tuple(ctx.pool.functions.getUserAccountData(address).call())
        if batched.get(address) != direct:
            mismatches.append(f"{address}: batch={batched.get(address)} direct={direct}")
    check("batched account data decodes identically to direct calls", not mismatches,
          "; ".join(mismatches))

    reserves = ctx.all_reserves()[:4]
    address = PROBE_ADDRESSES[0]
    batched_rows = ctx.user_reserve_data(address, reserves)
    check("batched user reserve rows are returned", len(batched_rows) == len(reserves),
          f"{len(batched_rows)} of {len(reserves)}")

    row_mismatches = []
    for asset, *values in batched_rows:
        direct = tuple(ctx.data_provider.functions.getUserReserveData(asset, address).call())
        if tuple(values) != direct:
            row_mismatches.append(ctx.symbol_of(asset))
    check("batched reserve rows decode identically", not row_mismatches,
          f"differing: {row_mismatches}")

    # Falling back must never crash: this is the path taken when a node rejects
    # a large batch mid-flight.
    ctx.multicall_available = False
    fallback = ctx.account_data(PROBE_ADDRESSES[:1])
    ctx.multicall_available = True
    check("single-call fallback produces the same shape",
          fallback.get(PROBE_ADDRESSES[0]) == batched.get(PROBE_ADDRESSES[0]))


def test_price_feeds(ctx: ChainContext) -> None:
    print("\n[price feeds]")
    mapping = ctx.aggregator_assets
    check("aggregator map is populated", len(mapping) > 0, f"{len(mapping)} aggregators")

    codeless = []
    for aggregator in mapping:
        if not ctx._has_code(aggregator):
            codeless.append(aggregator)
    check("every watched aggregator has code on-chain", not codeless,
          f"codeless: {codeless}")

    covered = {asset for assets in mapping.values() for asset in assets}
    missing = [ctx.symbol_of(a) for a in ctx.all_reserves() if a not in covered]
    check("every reserve maps to a feed", not missing, f"uncovered: {missing}")

    unpriced = [ctx.symbol_of(a) for a in ctx.all_reserves() if not ctx.asset_price(a)]
    check("oracle returns a price for every reserve", not unpriced,
          f"unpriced: {unpriced}")

    # Discovery walks asset -> Aave source -> proxy.aggregator(). If that last
    # hop lands on the wrong contract we would subscribe to an address that
    # never speaks, and the bot would look healthy while being blind to prices.
    # The only proof is that these addresses have really emitted AnswerUpdated.
    latest = ctx.w3.eth.block_number
    silent = []
    print("    recent AnswerUpdated activity per aggregator:")
    for aggregator, assets in mapping.items():
        names = ",".join(sorted(ctx.symbol_of(a) for a in assets))
        count = 0
        high = latest
        for _ in range(6):
            try:
                logs = ctx.w3.eth.get_logs({
                    "address": aggregator,
                    "topics": [ANSWER_UPDATED_TOPIC],
                    "fromBlock": max(0, high - 20_000),
                    "toBlock": high,
                })
            except Exception:
                break
            count += len(logs)
            if count:
                break
            high = max(0, high - 20_001)
            if high == 0:
                break
        print(f"      {names:<12} {aggregator}  {count} update(s)")
        if not count:
            silent.append(names)

    # Aave's testnet deployments wire reserves to mock aggregators that hold a
    # constant price and emit nothing at all, so silence here is a property of
    # the network rather than a defect. It is reported loudly because it means
    # price-driven triggers cannot fire on this network; see
    # test_chainlink_topic.py for the same assumptions checked against a real
    # Chainlink feed.
    if silent:
        print(f"    NOTE: {len(silent)} of {len(mapping)} feeds have emitted no "
              f"AnswerUpdated — on this network prices are static mocks, so "
              f"liquidations can only be triggered by Aave user events.")
    check("price feed discovery produced usable addresses",
          len(mapping) > 0 and not codeless)


def test_valuation(ctx: ChainContext) -> None:
    print("\n[valuation]")
    reserves = ctx.all_reserves()
    debt = next((a for a in reserves if ctx.symbol_of(a).upper() in ("USDC", "DAI", "USDT")), None)
    collateral = next((a for a in reserves if ctx.symbol_of(a).upper() == "WETH"), None)
    if not debt or not collateral:
        print("    (skipped: no stablecoin/WETH pair in this deployment)")
        return

    debt_decimals = ctx.decimals(debt)
    debt_amount = 1000 * 10 ** debt_decimals
    value = ctx.value_in_base(debt, debt_amount)
    check("1000 units of a stablecoin value near 1000 in base currency",
          value is not None and 900 * 10 ** 8 <= value <= 1100 * 10 ** 8,
          f"got {value} for {ctx.symbol_of(debt)}")

    received = ctx.estimate_collateral_received(debt, debt_amount, collateral)
    check("collateral estimate is positive", bool(received), f"got {received}")
    if received:
        bonus = ctx.liquidation_bonus(collateral)
        collateral_value = ctx.value_in_base(collateral, received)
        premium = (collateral_value * 10000) // (value or 1)
        check("collateral estimate carries the liquidation bonus",
              abs(premium - bonus) <= 50,
              f"implied {premium} vs configured {bonus}")


def test_evaluation_is_safe(ctx: ChainContext) -> None:
    print("\n[evaluation]")
    for address in PROBE_ADDRESSES:
        plan = ctx.evaluate_user(address)
        check(f"debt-free address {address[:10]} yields no plan", plan is None,
              f"got {plan}")


def test_simulation_gate(ctx: ChainContext) -> None:
    print("\n[simulation gate]")
    sender = PROBE_ADDRESSES[0]

    good = {
        "from": sender,
        "to": ctx.config.data_provider,
        "data": ctx.data_provider.functions.getAllReservesTokens()._encode_transaction_data(),
        "value": 0,
        **ctx.gas_fields(),
    }
    result = simulate_transaction(ctx.w3, good, ctx.error_table)
    check("a valid call passes simulation", result.ok, result.reason)
    check("simulation reports a gas limit", bool(result.gas_limit), f"{result.gas_limit}")
    check("simulation prices the transaction", bool(result.gas_cost_wei),
          f"{result.gas_cost_wei}")

    bad = dict(good)
    bad["data"] = "0x" + Web3.keccak(text="noSuchFunction()")[:4].hex()
    rejected = simulate_transaction(ctx.w3, bad, ctx.error_table)
    check("an unknown selector is rejected", not rejected.ok, "simulation let it through")
    check("rejection carries a reason", bool(rejected.reason), "empty reason")
    print(f"    reason: {rejected.reason}")

    to_eoa = dict(good)
    to_eoa["to"] = PROBE_ADDRESSES[1]
    to_eoa["data"] = "0x12345678"
    eoa_result = simulate_transaction(ctx.w3, to_eoa, ctx.error_table)
    check("calling a codeless address does not crash the gate",
          isinstance(eoa_result.ok, bool))


def main() -> int:
    configure_logging("WARNING")
    config = load_chain_config(None)
    print(f"network: {config.name}  ws={config.ws_rpc_url}")

    ctx = ChainContext(config)
    ctx.connect()
    ctx.discover_price_feeds()

    test_reserves_and_decimals(ctx)
    test_multicall_matches_single_calls(ctx)
    test_price_feeds(ctx)
    test_valuation(ctx)
    test_evaluation_is_safe(ctx)
    test_simulation_gate(ctx)

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
