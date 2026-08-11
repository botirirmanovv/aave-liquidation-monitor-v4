"""Unit tests for the pure-logic modules: topics, decimals, state.

No RPC, no wallet. Runs in well under a second.
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from hexbytes import HexBytes
from web3 import Web3

from aave_bot.decimals import KNOWN_TOKEN_DECIMALS, DecimalsResolver, DecimalsUnavailable
from aave_bot.state import MonitorState
from aave_bot.topics import AAVE_EVENT_TOPICS, ANSWER_UPDATED_TOPIC, topic_hex

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def expect_raises(name: str, exc_type: type[Exception], action) -> None:
    try:
        result = action()
    except exc_type:
        check(name, True)
        return
    except Exception as exc:
        check(name, False, f"raised {type(exc).__name__} instead of {exc_type.__name__}")
        return
    check(name, False, f"returned {result!r} instead of raising")


def test_topics() -> None:
    print("\n[topics]")
    digest = Web3.keccak(text="Supply(address,address,address,uint256,uint16)")

    check("HexBytes input gets 0x prefix", topic_hex(digest).startswith("0x"))
    check("raw bytes input normalises",
          topic_hex(bytes(digest)) == topic_hex(digest))
    check("already-prefixed string is stable",
          topic_hex("0xAB" + "cd" * 31) == "0xab" + "cd" * 31)
    check("unprefixed string gains prefix",
          topic_hex("ab" + "cd" * 31) == "0xab" + "cd" * 31)

    check("all Aave topics are 32-byte prefixed hex",
          all(t.startswith("0x") and len(t) == 66 for t in AAVE_EVENT_TOPICS))
    check("Supply resolves from a log-shaped topic",
          AAVE_EVENT_TOPICS.get(topic_hex(HexBytes(digest))) == "Supply")

    # Cross-check against the topic the live Sepolia node accepted for the
    # Chainlink aggregator subscription.
    check("AnswerUpdated topic matches the value a live node accepted",
          ANSWER_UPDATED_TOPIC
          == "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f",
          f"got {ANSWER_UPDATED_TOPIC}")


def test_decimals() -> None:
    print("\n[decimals]")
    usdc = "0x" + "11" * 20
    weird = "0x" + "22" * 20
    broken = "0x" + "33" * 20

    agreeing = DecimalsResolver(
        token_decimals=lambda a: 6,
        reserve_decimals=lambda a: 6,
        symbols={usdc: "USDC"},
    )
    check("agreeing sources resolve", agreeing.resolve(usdc) == 6)
    check("result is cached", agreeing.resolve(usdc) == 6)

    conflicting = DecimalsResolver(lambda a: 6, lambda a: 18)
    expect_raises("conflicting sources are refused", DecimalsUnavailable,
                  lambda: conflicting.resolve(weird))

    single = DecimalsResolver(lambda a: 8, lambda a: None)
    check("single usable source is accepted", single.resolve(weird) == 8)

    dead = DecimalsResolver(lambda a: (_ for _ in ()).throw(ValueError("no such method")))
    expect_raises("unreachable token is refused instead of assuming 18",
                  DecimalsUnavailable, lambda: dead.resolve(broken))
    check("try_resolve returns None instead of raising", dead.try_resolve(broken) is None)

    implausible = DecimalsResolver(lambda a: 0, lambda a: None)
    expect_raises("zero decimals is refused", DecimalsUnavailable,
                  lambda: implausible.resolve(weird))

    boolean = DecimalsResolver(lambda a: True, lambda a: None)
    expect_raises("bool is not accepted as decimals", DecimalsUnavailable,
                  lambda: boolean.resolve(weird))

    # Regression: a rate-limited endpoint (429 on a public RPC) made warm-up
    # blacklist four real Base reserves for the life of the process. A node that
    # says nothing must not be treated as a verdict about the asset.
    flaky_calls = {"n": 0}

    def flaky(_asset):
        flaky_calls["n"] += 1
        if flaky_calls["n"] == 1:
            raise ValueError("429 Too Many Requests")
        return 6

    flaky_resolver = DecimalsResolver(flaky, lambda a: None)
    check("transport failure yields no answer", flaky_resolver.try_resolve(usdc) is None)
    check("transport failure is retried, not cached as a refusal",
          flaky_resolver.try_resolve(usdc) == 6)

    permanently_bad = DecimalsResolver(lambda a: 0, lambda a: 99)
    check("implausible answers give no value", permanently_bad.try_resolve(weird) is None)
    probes = {"n": 0}

    def counting(_asset):
        probes["n"] += 1
        return 0

    final = DecimalsResolver(counting, lambda a: None)
    final.try_resolve(weird)
    final.try_resolve(weird)
    check("a genuine bad answer is cached and not re-probed", probes["n"] == 1)

    seeded = DecimalsResolver(lambda a: (_ for _ in ()).throw(ValueError("unused")))
    check("seed accepts a plausible batched value", seeded.seed(usdc, 6))
    check("seeded value is served from cache", seeded.resolve(usdc) == 6)
    check("seed rejects an implausible batched value", not seeded.seed(weird, 0))

    mismatch = DecimalsResolver(lambda a: 18, lambda a: 18, symbols={usdc: "USDC"})
    check("on-chain value wins over the table", mismatch.resolve(usdc) == 18)

    # The trap the task description invites: not every stablecoin has 6 decimals.
    check("USDC/USDT are 6 in the table",
          KNOWN_TOKEN_DECIMALS["USDC"] == 6 and KNOWN_TOKEN_DECIMALS["USDT"] == 6)
    check("DAI/GHO/USDS stay 18 in the table",
          KNOWN_TOKEN_DECIMALS["DAI"] == 18
          and KNOWN_TOKEN_DECIMALS["GHO"] == 18
          and KNOWN_TOKEN_DECIMALS["USDS"] == 18)
    check("WBTC is 8", KNOWN_TOKEN_DECIMALS["WBTC"] == 8)


def test_state() -> None:
    print("\n[state]")
    alice, bob = "0x" + "a1" * 20, "0x" + "b0" * 20
    weth, usdc = "0x" + "ee" * 20, "0x" + "cc" * 20

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        state = MonitorState(path, save_interval_seconds=0)

        check("first track reports a new user", state.track(alice, weth) is True)
        check("second track of same user is not new", state.track(alice, usdc) is False)
        state.track(bob, weth)

        check("reverse index groups by asset", state.users_for_asset(weth) == {alice, bob})
        check("reverse index isolates other assets", state.users_for_asset(usdc) == {alice})
        check("multi-asset query unions holders",
              state.users_for_assets([weth, usdc]) == {alice, bob})
        check("unknown asset yields nobody", state.users_for_asset("0x" + "99" * 20) == set())

        state.mark_svr(usdc)
        state.save(force=True)
        check("state file written", path.exists())

        reloaded = MonitorState(path)
        reloaded.load()
        check("users survive a reload", reloaded.tracked_users == {alice, bob})
        check("reverse index is rebuilt on load",
              reloaded.users_for_asset(weth) == {alice, bob})
        check("svr marks survive a reload", reloaded.svr_reserves == {usdc})

        reloaded.forget(alice)
        check("forget removes the user", alice not in reloaded.tracked_users)
        check("forget cleans the reverse index",
              reloaded.users_for_asset(weth) == {bob})
        check("asset with no holders is dropped",
              usdc not in reloaded.asset_users)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "throttled.json"
        throttled = MonitorState(path, save_interval_seconds=3600)
        throttled.track(alice, weth)
        throttled.save()
        check("throttled save skips the write", not path.exists())
        throttled.save(force=True)
        check("forced save writes anyway", path.exists())

    missing = MonitorState(Path(tempfile.gettempdir()) / "definitely-absent-state.json")
    missing.load()
    check("missing state file loads as empty", len(missing) == 0)


def test_chain_scoped_config() -> None:
    """A chain's deployment identity must never inherit global values.

    Regression: an empty BASE_ROUTER_ADDRESSES fell through to the global
    (Sepolia) ROUTER_ADDRESSES, so the Base monitor quoted swaps against a
    router with no code on Base. The same fallback reaches the liquidation bot
    address, the signing key and the state file, where it is worse: two chains
    would share one state file and execution would target a foreign contract.
    """
    print("\n[config: per-chain isolation]")
    import os
    from decimal import Decimal

    from aave_bot import config as cfg

    env = {
        "ROUTER_ADDRESSES": "0xC532a74256D3Db42D0Bf7a0400fEFDbad7694008",
        "LIQUIDATION_BOT_ADDRESS": "0x1111111111111111111111111111111111111111",
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "WS_RPC_URL": "wss://sepolia.example",
        "HTTP_RPC_URL": "https://sepolia.example",
        "STATE_FILE": "shared_state.json",
        "PRICE_FEED_AGGREGATORS": "WETH:0x2222222222222222222222222222222222222222",
        "BASE_ROUTER_ADDRESSES": "",
        "BASE_WS_RPC_URL": "wss://base.example",
        "BASE_HTTP_RPC_URL": "https://base.example",
        "BASE_POOL_ADDRESS": "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
        "BASE_DATA_PROVIDER_ADDRESS": "0x0F43731EB8d45A581f4a36DD74F5f358bc90C73A",
        "BASE_ORACLE_ADDRESS": "0x2Cc0Fc26eD4563A5ce5e8bdcfe1A2878676Ae156",
        "SLIPPAGE_TOLERANCE": "0.05",
    }
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        base = cfg.load_chain_config("base")
        check("routers do not leak across chains", base.routers == [], str(base.routers))
        check("liquidation bot does not leak", base.liquidation_bot == "", base.liquidation_bot)
        check("signing key does not leak", base.private_key == "")
        check("aggregator overrides do not leak", base.price_feed_aggregators == {})
        check("ws endpoint is chain-scoped", base.ws_rpc_url == "wss://base.example")
        check("http endpoint is chain-scoped", base.http_rpc_url == "https://base.example")
        check("state file is per chain",
              base.state_file.name == "monitor_state_base.json", base.state_file.name)
        check("multicall falls back to the canonical address",
              base.multicall3 == cfg.DEFAULT_MULTICALL3)
        check("tuning knobs still inherit globals",
              base.slippage_tolerance == Decimal("0.05"), str(base.slippage_tolerance))

        single = cfg.load_chain_config(None)
        check("single-chain mode still reads unprefixed values",
              single.routers == [Web3.to_checksum_address(env["ROUTER_ADDRESSES"])])
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_multicall_failure_is_transient() -> None:
    """A failed batch read must not disable batching for the whole process.

    Regression: one 429 from a public endpoint cleared multicall_available, and
    from then on every position check issued one call per reserve instead of a
    single batch — turning a momentary rate limit into a permanent ~20x load
    increase, which produces more rate limiting.
    """
    print("\n[chain: multicall resilience]")
    from aave_bot.chain import ChainContext

    ctx = ChainContext.__new__(ChainContext)
    ctx.log = logging.getLogger("test.multicall")
    ctx.multicall_available = True
    ctx._multicall_failures = 0

    class ExplodingCall:
        def call(self):
            raise ValueError("429 Too Many Requests")

    class ExplodingFunctions:
        def tryAggregate(self, *_args):
            return ExplodingCall()

    ctx.multicall = SimpleNamespace(functions=ExplodingFunctions())

    check("a failed batch returns no result",
          ctx._try_aggregate([{"target": "0x", "callData": "0x"}], "test") is None)
    check("batching stays enabled after a failure", ctx.multicall_available)
    check("the failure is counted", ctx._multicall_failures == 1)


def test_simulation_decoding() -> None:
    print("\n[simulate: revert decoding]")
    from eth_abi import encode as abi_encode

    from aave_bot.abis import LIQUIDATION_BOT_ERROR_SIGNATURES
    from aave_bot.simulate import (
        SimulationResult, build_error_table, decode_revert, effective_gas_price,
    )

    table = build_error_table(LIQUIDATION_BOT_ERROR_SIGNATURES)
    check("error table covers every declared error",
          len(table) == len(LIQUIDATION_BOT_ERROR_SIGNATURES),
          f"{len(table)} selectors for {len(LIQUIDATION_BOT_ERROR_SIGNATURES)} signatures")

    only_operator = Web3.keccak(text="OnlyOperator()")[:4]
    check("custom error selector is named",
          "OnlyOperator()" in decode_revert(only_operator, table))
    check("named lookup works from a hex string",
          "OnlyOperator()" in decode_revert("0x" + only_operator.hex(), table))

    string_revert = bytes.fromhex("08c379a0") + abi_encode(["string"], ["insufficient profit"])
    check("Error(string) is decoded",
          "insufficient profit" in decode_revert(string_revert, table))

    panic = bytes.fromhex("4e487b71") + abi_encode(["uint256"], [0x11])
    check("Panic is decoded to a description",
          "overflow" in decode_revert(panic, table))

    unknown = Web3.keccak(text="SomethingNobodyDeclared()")[:4]
    decoded = decode_revert(unknown, table)
    check("unknown selector is reported verbatim", "unknown selector" in decoded, decoded)
    check("empty revert data is handled", "without data" in decode_revert(b""))

    check("EIP-1559 gas price is picked up",
          effective_gas_price({"maxFeePerGas": 1234, "maxPriorityFeePerGas": 1}) == 1234)
    check("legacy gas price is picked up", effective_gas_price({"gasPrice": 99}) == 99)
    check("missing gas price yields None", effective_gas_price({}) is None)

    check("gas cost multiplies limit by price",
          SimulationResult(True, gas_limit=100, gas_price_wei=5).gas_cost_wei == 500)
    check("gas cost is None when unpriced",
          SimulationResult(True, gas_limit=100).gas_cost_wei is None)


def test_pair_ranking() -> None:
    print("\n[pairs]")
    from aave_bot.pairs import rank_collateral_debt_pairs

    weth = "0x" + "11" * 20
    usdc = "0x" + "22" * 20
    dai = "0x" + "33" * 20

    # row: asset, aToken, stableDebt, variableDebt, …, usableAsCollateral
    rows = [
        (weth, 5 * 10**18, 0, 0, True),
        (usdc, 0, 0, 2000 * 10**6, False),
        (dai, 1000 * 10**18, 0, 50 * 10**18, True),
    ]

    def value(asset: str, amount: int) -> int | None:
        # Fake USD: WETH=$2000, USDC=$1, DAI=$1 (scaled as base 8dp-ish ints)
        if asset == weth:
            return (amount * 2000) // 10**18 * 10**8
        if asset == usdc:
            return (amount * 10**8) // 10**6
        if asset == dai:
            return (amount * 10**8) // 10**18
        return None

    ranked = rank_collateral_debt_pairs(rows, value)
    check("multi-asset book yields multiple pairs", len(ranked) >= 2)
    top = ranked[0]
    check("highest debt USD pair ranks first (USDC debt)",
          top[2].lower() == usdc.lower(), f"got debt={top[2]}")
    # Same-asset DAI/DAI should beat WETH/DAI when debt USD equal-ish — here
    # USDC debt is largest so stays first; verify DAI same-asset is preferred
    # over WETH/DAI when comparing those two.
    dai_pairs = [p for p in ranked if p[2].lower() == dai.lower()]
    check("same-asset DAI/DAI outranks WETH/DAI",
          dai_pairs and dai_pairs[0][0].lower() == dai.lower(),
          f"order={[ (p[0][-4:], p[2][-4:]) for p in dai_pairs ]}")


def main() -> int:
    test_topics()
    test_decimals()
    test_state()
    test_pair_ranking()
    test_chain_scoped_config()
    test_multicall_failure_is_transient()
    test_simulation_decoding()

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
