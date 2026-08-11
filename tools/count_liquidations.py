"""How many Aave V3 liquidations actually happen on a chain, per day.

Answers the question a fresh dry-run cannot: waiting an hour and seeing nothing
says little, because liquidations are bursty. This walks LiquidationCall logs
over a historical window and reports the rate, so the monitor's observed count
can be judged against what there was to observe.

    python tools/count_liquidations.py --chain base --hours 24
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from eth_abi import decode as abi_decode
from web3 import Web3

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from aave_bot.config import load_chain_config  # noqa: E402
from aave_bot.topics import event_topic  # noqa: E402

LIQUIDATION_CALL = event_topic(
    "LiquidationCall(address,address,address,uint256,uint256,address,bool)"
)

# Rough block times, only used to translate hours into a block span.
BLOCK_SECONDS = {"base": 2.0, "arbitrum": 0.25, "optimism": 2.0, "ethereum": 12.0}


def fetch_in_chunks(w3: Web3, address: str, from_block: int, to_block: int,
                    chunk: int) -> list[dict]:
    logs: list[dict] = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        try:
            logs.extend(w3.eth.get_logs({
                "address": address,
                "topics": [LIQUIDATION_CALL],
                "fromBlock": start,
                "toBlock": end,
            }))
        except Exception as exc:
            print(f"  ! blocks {start}-{end} failed: {str(exc)[:90]}")
        start = end + 1
        print(f"\r  scanned to {end} ({len(logs)} liquidations)", end="", flush=True)
    print()
    return logs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chain", required=True)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--chunk", type=int, default=5_000)
    args = parser.parse_args()

    config = load_chain_config(args.chain)
    w3 = Web3(Web3.HTTPProvider(config.http_rpc_url, request_kwargs={"timeout": 30}))
    head = w3.eth.block_number

    seconds_per_block = BLOCK_SECONDS.get(args.chain, 2.0)
    span = int(args.hours * 3600 / seconds_per_block)
    start = max(0, head - span)

    print(f"{args.chain}: scanning blocks {start}..{head} "
          f"(~{args.hours}h at {seconds_per_block}s/block)")
    logs = fetch_in_chunks(w3, config.pool, start, head, args.chunk)

    print(f"\n{len(logs)} liquidations in ~{args.hours:g}h "
          f"=> ~{len(logs) / args.hours:.1f} per hour, "
          f"~{len(logs) * 24 / args.hours:.0f} per day")

    if logs:
        liquidators: Counter[str] = Counter()
        for log in logs:
            data = log["data"]
            raw = bytes(data) if not isinstance(data, str) else bytes.fromhex(data[2:])
            try:
                # debtToCover, liquidatedCollateralAmount, liquidator, receiveAToken
                _, _, liquidator, _ = abi_decode(
                    ["uint256", "uint256", "address", "bool"], raw
                )
                liquidators[Web3.to_checksum_address(liquidator)] += 1
            except Exception:
                liquidators["<undecodable>"] += 1
        print(f"distinct blocks touched: {len({log['blockNumber'] for log in logs})}")
        print("busiest liquidators (competition you would be bidding against):")
        for address, count in liquidators.most_common(5):
            print(f"  {address}  {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
