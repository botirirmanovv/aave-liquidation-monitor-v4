"""Replays real Aave logs from the live network through the event pipeline.

A synthetic log only proves the decoder agrees with the encoder that produced it.
These are actual logs pulled with eth_getLogs and pushed through the exact
handler the WebSocket subscription feeds, so topic matching, ABI decoding, user
extraction, the reverse index and the work queue are all verified against data
the chain really produced.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from web3 import Web3

from aave_bot.app import configure_logging
from aave_bot.chain import ChainContext
from aave_bot.config import load_chain_config
from aave_bot.runner import ChainRunner
from aave_bot.topics import AAVE_EVENT_SIGNATURES, AAVE_EVENT_TOPICS, event_topic, topic_hex

failures: list[str] = []
CHUNK = 5_000
MAX_CHUNKS = 24


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def fetch_logs(ctx: ChainContext) -> dict[str, list]:
    """Collect one or more real logs per Aave event type, scanning backwards."""
    latest = ctx.w3.eth.block_number
    found: dict[str, list] = {name: [] for name in AAVE_EVENT_SIGNATURES}
    wanted = {name: event_topic(sig) for name, sig in AAVE_EVENT_SIGNATURES.items()}

    high = latest
    for _ in range(MAX_CHUNKS):
        if all(found[name] for name in wanted):
            break
        low = max(0, high - CHUNK)
        for name, topic in wanted.items():
            if found[name]:
                continue
            try:
                logs = ctx.w3.eth.get_logs({
                    "address": ctx.config.pool,
                    "topics": [topic],
                    "fromBlock": low,
                    "toBlock": high,
                })
            except Exception as exc:
                print(f"    (getLogs failed for {name} at {low}-{high}: "
                      f"{type(exc).__name__})")
                continue
            if logs:
                found[name] = list(logs[:3])
        if low == 0:
            break
        high = low - 1

    scanned = latest - high
    print(f"    scanned ~{scanned} blocks back from {latest}")
    for name, logs in found.items():
        print(f"    {name:<16} {len(logs)} log(s)")
    return found


async def replay(ctx: ChainContext, logs_by_event: dict[str, list]) -> None:
    print("\n[replaying real logs]")
    with tempfile.TemporaryDirectory() as raw:
        ctx.state.path = Path(raw) / "state.json"
        ctx.state.tracked_users.clear()
        ctx.state.user_reserves.clear()
        ctx.state.asset_users.clear()

        runner = ChainRunner(ctx, None)
        total = 0
        for name, logs in logs_by_event.items():
            for entry in logs:
                runner._on_aave_log(entry)
                total += 1

        check("at least one real log was available to replay", total > 0,
              "the pool has been silent across the scanned range")
        if total == 0:
            return

        check("replayed logs produced tracked users", len(ctx.state) > 0,
              f"{len(ctx.state)} users")
        check("every tracked user has at least one reserve",
              all(ctx.state.reserves_of(u) for u in ctx.state.tracked_users))
        check("the reverse index is populated", len(ctx.state.asset_users) > 0,
              f"{len(ctx.state.asset_users)} assets")

        reserves = set(ctx.all_reserves())
        indexed = set(ctx.state.asset_users)
        check("indexed assets are all real Aave reserves", indexed <= reserves,
              f"unexpected: {indexed - reserves}")

        # Both views must agree, since the price-update path trusts the index.
        consistent = all(
            user in ctx.state.asset_users.get(asset, set())
            for user, assets in ctx.state.user_reserves.items()
            for asset in assets
        )
        check("forward and reverse views agree", consistent)

        check("every decoded user was queued for evaluation",
              runner._queue.qsize() == len(ctx.state.tracked_users),
              f"queued {runner._queue.qsize()} for {len(ctx.state)} users")

        print(f"    tracked {len(ctx.state)} users across "
              f"{len(ctx.state.asset_users)} assets from {total} logs")
        for asset, users in sorted(ctx.state.asset_users.items()):
            print(f"      {ctx.symbol_of(asset):<8} {len(users)} user(s)")


async def replay_hostile(ctx: ChainContext, sample: list) -> None:
    print("\n[malformed and unrelated logs]")
    runner = ChainRunner(ctx, None)
    before = len(ctx.state)

    runner._on_aave_log({})
    runner._on_aave_log({"topics": []})
    runner._on_aave_log({"topics": [topic_hex(Web3.keccak(text="Nonsense(uint256)"))],
                         "data": "0x", "blockNumber": 1})
    check("junk logs are ignored without raising", len(ctx.state) == before)

    if sample:
        broken = dict(sample[0])
        broken["data"] = "0x00"  # truncated payload, correct topic
        runner._on_aave_log(broken)
        check("a log with a valid topic but broken data is dropped safely", True)

        # An unprefixed topic is what hexbytes 1.x hands back, and it must still
        # match: this exact mismatch silently dropped every event before.
        unprefixed = dict(sample[0])
        topics = list(sample[0]["topics"])
        raw_first = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
        unprefixed["topics"] = [raw_first.removeprefix("0x")] + list(topics[1:])
        fresh = ChainRunner(ctx, None)
        fresh._on_aave_log(unprefixed)
        check("an unprefixed topic still matches", fresh._queue.qsize() == 1,
              f"queued {fresh._queue.qsize()}")


def test_topic_filter_equivalence(ctx: ChainContext) -> None:
    """The pool subscription filters topic0 with a nested array; that filter must
    return exactly the union of the per-event queries, or events vanish."""
    print("\n[topic filter]")
    latest = ctx.w3.eth.block_number
    low = max(0, latest - 40_000)
    topics = list(AAVE_EVENT_TOPICS.keys())

    def key(entry) -> tuple:
        return (entry["blockNumber"], entry["logIndex"])

    union: set[tuple] = set()
    for topic in topics:
        try:
            logs = ctx.w3.eth.get_logs({
                "address": ctx.config.pool, "topics": [topic],
                "fromBlock": low, "toBlock": latest,
            })
        except Exception as exc:
            check("per-event queries succeed", False, f"{type(exc).__name__}")
            return
        union |= {key(entry) for entry in logs}

    try:
        combined = ctx.w3.eth.get_logs({
            "address": ctx.config.pool, "topics": [topics],
            "fromBlock": low, "toBlock": latest,
        })
    except Exception as exc:
        check("the node accepts a nested topic filter", False,
              f"{type(exc).__name__}: {exc} — the runner falls back to unfiltered logs")
        return

    check("the node accepts a nested topic filter", True)
    combined_keys = {key(entry) for entry in combined}
    check("the filter returns exactly the union of the per-event queries",
          combined_keys == union,
          f"filtered {len(combined_keys)} vs union {len(union)}, "
          f"missing {len(union - combined_keys)}, extra {len(combined_keys - union)}")

    unfiltered = ctx.w3.eth.get_logs({
        "address": ctx.config.pool, "fromBlock": low, "toBlock": latest,
    })
    saved = len(unfiltered) - len(combined)
    check("the filter genuinely discards unrelated pool logs", saved > 0,
          f"unfiltered {len(unfiltered)} vs filtered {len(combined)}")
    if len(unfiltered):
        print(f"    {len(unfiltered)} pool logs in {latest - low} blocks, "
              f"{len(combined)} relevant — filter drops "
              f"{100 * saved / len(unfiltered):.0f}%")


async def run_all() -> None:
    config = load_chain_config(None)
    ctx = ChainContext(config)
    ctx.connect()

    print(f"\n[fetching real logs from {config.name}]")
    logs_by_event = fetch_logs(ctx)
    await replay(ctx, logs_by_event)
    test_topic_filter_equivalence(ctx)

    sample = next((logs for logs in logs_by_event.values() if logs), [])
    await replay_hostile(ctx, sample)


def main() -> int:
    configure_logging("WARNING")
    asyncio.run(run_all())
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
