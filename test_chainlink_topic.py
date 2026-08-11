"""Validates the Chainlink assumptions against a real production feed.

Aave on Sepolia points at mock aggregators that never emit an event, so the
price path cannot be proven there. This checks the three things the price
subscription depends on, read-only, against mainnet ETH/USD:

  1. AnswerUpdated has the topic we compute and subscribe with;
  2. a Chainlink proxy dereferences to the aggregator that actually emits it,
     which is the hop discovery performs;
  3. the event payload decodes with the types we assume.
"""
from __future__ import annotations

import sys

from eth_abi import decode as abi_decode
from web3 import Web3

from aave_bot import abis
from aave_bot.topics import ANSWER_UPDATED_TOPIC, topic_hex

# Free endpoints differ in what they allow: several serve eth_call happily but
# reject eth_getLogs outright, so the first one that answers both wins.
MAINNET_RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    "https://eth.drpc.org",
    "https://cloudflare-eth.com",
    "https://1rpc.io/eth",
]
ETH_USD_PROXY = Web3.to_checksum_address("0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419")
SCAN_BLOCKS = 5_000

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def connect_any() -> tuple[Web3, int]:
    """First endpoint that answers plain reads."""
    for url in MAINNET_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 25}))
            if w3.eth.chain_id == 1:
                print(f"reads via {url}")
                return w3, w3.eth.block_number
        except Exception as exc:
            print(f"  skipping {url} for reads: {type(exc).__name__}")
    raise RuntimeError("no mainnet endpoint answered eth_chainId")


def get_logs_anywhere(address: str, topic: str | None, latest: int) -> tuple[list, int]:
    """Free endpoints cap log queries differently.

    The widest span is attempted across every endpoint before narrowing: a
    narrow window that happens to contain no updates would be indistinguishable
    from a wrong topic, which is the very thing under test.
    """
    for span in (SCAN_BLOCKS, 2_000, 500, 150):
        for url in MAINNET_RPCS:
            params = {
                "address": address,
                "fromBlock": latest - span,
                "toBlock": latest,
            }
            if topic:
                params["topics"] = [topic]
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 25}))
                return list(w3.eth.get_logs(params)), span
            except Exception:
                continue
        print(f"  no endpoint accepted a {span}-block query")
    raise RuntimeError("no mainnet endpoint served the log query")


def main() -> int:
    w3, latest = connect_any()
    print(f"mainnet chain id {w3.eth.chain_id}, ETH/USD proxy {ETH_USD_PROXY}")

    print("\n[proxy dereference]")
    proxy = w3.eth.contract(address=ETH_USD_PROXY, abi=abis.CHAINLINK_PROXY_ABI)
    description = proxy.functions.description().call()
    aggregator = Web3.to_checksum_address(proxy.functions.aggregator().call())
    print(f"    description: {description}")
    print(f"    aggregator:  {aggregator}")

    check("proxy exposes a description", "ETH" in description.upper(), description)
    check("proxy dereferences to a different contract", aggregator != ETH_USD_PROXY)
    check("the aggregator has code", len(w3.eth.get_code(aggregator)) > 0)

    print("\n[AnswerUpdated emissions]")
    logs, span = get_logs_anywhere(aggregator, ANSWER_UPDATED_TOPIC, latest)
    print(f"    {len(logs)} update(s) in the last {span} blocks")
    check("our topic matches what a real aggregator emits", len(logs) > 0,
          "no AnswerUpdated found — the topic constant would be wrong")

    proxy_logs, _ = get_logs_anywhere(ETH_USD_PROXY, ANSWER_UPDATED_TOPIC, latest)
    check("the proxy itself emits nothing, so the dereference is required",
          len(proxy_logs) == 0,
          f"proxy emitted {len(proxy_logs)}, subscribing to it would also work")

    if not logs:
        return finish()

    print("\n[payload decoding]")
    entry = logs[-1]
    check("topic0 round-trips through our normaliser",
          topic_hex(entry["topics"][0]) == ANSWER_UPDATED_TOPIC)

    # AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)
    current = abi_decode(["int256"], bytes(entry["topics"][1]))[0]
    round_id = abi_decode(["uint256"], bytes(entry["topics"][2]))[0]
    updated_at = abi_decode(["uint256"], bytes(entry["data"]))[0]
    price = current / 10 ** proxy.functions.decimals().call()
    print(f"    round {round_id}, price {price:,.2f} USD, updatedAt {updated_at}")

    check("decoded price is plausible for ETH/USD", 100 < price < 100_000, f"{price}")
    check("round id is non-zero", round_id > 0)
    check("timestamp looks like a unix time", updated_at > 1_600_000_000)
    check("indexed args live in topics, not data",
          len(entry["topics"]) == 3 and len(bytes(entry["data"])) == 32,
          f"{len(entry['topics'])} topics, {len(bytes(entry['data']))} data bytes")

    return finish()


def finish() -> int:
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
