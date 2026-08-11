"""Liquidation debt-covered USD by calendar month (90d).

    python tools/report_liq_monthly.py --chains base,arbitrum --days 90
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from eth_abi import decode as abi_decode
from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aave_bot.config import load_chain_config  # noqa: E402
from aave_bot.topics import event_topic  # noqa: E402

LIQUIDATION_CALL = event_topic(
    "LiquidationCall(address,address,address,uint256,uint256,address,bool)"
)
BLOCK_SECONDS = {"base": 2.0, "arbitrum": 0.25}
HTTP = {
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
}
ORACLE_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}], "name": "getAssetPrice",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]
ERC20 = [
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}],
     "stateMutability": "view", "type": "function"},
]


def topic_address(topic) -> str:
    raw = topic.hex() if hasattr(topic, "hex") else str(topic)
    raw = raw[2:] if raw.startswith("0x") else raw
    return Web3.to_checksum_address("0x" + raw[-40:])


def fetch_logs(w3, pool, start, end, chunk):
    logs = []
    cur = start
    while cur <= end:
        to = min(cur + chunk - 1, end)
        for attempt in range(5):
            try:
                logs.extend(w3.eth.get_logs({
                    "address": pool,
                    "topics": [LIQUIDATION_CALL],
                    "fromBlock": cur,
                    "toBlock": to,
                }))
                break
            except Exception:
                time.sleep(0.3 * (attempt + 1))
                to = min(cur + max(500, (to - cur) // 2), end)
        cur = to + 1
        print(f"\r  logs {cur}/{end} n={len(logs)}   ", end="", flush=True)
    print()
    return logs


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default="base,arbitrum")
    ap.add_argument("--days", type=float, default=90)
    ap.add_argument("--chunk", type=int, default=12_000)
    args = ap.parse_args()

    print(f"LIQUIDATION TURNOVER (debt covered USD) — {args.days:g}d by month\n")
    grand = 0.0
    grand_n = 0

    for chain in [c.strip() for c in args.chains.split(",") if c.strip()]:
        cfg = load_chain_config(chain)
        http = HTTP.get(chain, cfg.http_rpc_url)
        w3 = Web3(Web3.HTTPProvider(http, request_kwargs={"timeout": 45}))
        head = int(w3.eth.block_number)
        span = int(args.days * 24 * 3600 / BLOCK_SECONDS[chain])
        start = max(0, head - span)
        print(f"=== {chain.upper()} blocks {start}..{head} ===")
        raw = fetch_logs(w3, cfg.pool, start, head, args.chunk)
        oracle = w3.eth.contract(address=cfg.oracle, abi=ORACLE_ABI)
        price: dict[str, int] = {}
        dec: dict[str, int] = {}

        def usd(asset: str, amount: int) -> float:
            if asset not in price:
                try:
                    price[asset] = int(oracle.functions.getAssetPrice(asset).call())
                except Exception:
                    price[asset] = -1
            if asset not in dec:
                try:
                    dec[asset] = int(
                        w3.eth.contract(address=asset, abi=ERC20).functions.decimals().call()
                    )
                except Exception:
                    dec[asset] = 18
            if price[asset] < 0 or amount == 0:
                return 0.0
            return amount * price[asset] / (10 ** dec[asset]) / 1e8

        # Cache block -> timestamp sparsely
        ts_cache: dict[int, int] = {}
        by_month: dict[str, list] = defaultdict(lambda: [0, 0.0])  # n, usd

        for i, entry in enumerate(raw):
            try:
                topics = entry["topics"]
                data = entry["data"]
                raw_data = bytes(data) if not isinstance(data, str) else bytes.fromhex(data[2:])
                debt_to_cover, _, _, _ = abi_decode(
                    ["uint256", "uint256", "address", "bool"], raw_data
                )
                debt = topic_address(topics[2])
                block = int(entry["blockNumber"])
                if block not in ts_cache:
                    ts_cache[block] = int(w3.eth.get_block(block)["timestamp"])
                mk = datetime.fromtimestamp(ts_cache[block], tz=timezone.utc).strftime("%Y-%m")
                u = usd(debt, int(debt_to_cover))
                by_month[mk][0] += 1
                by_month[mk][1] += u
            except Exception:
                continue
            if i % 200 == 0:
                print(f"\r  price {i}/{len(raw)}   ", end="", flush=True)
        print()

        chain_usd = 0.0
        chain_n = 0
        print(f"{'month':<10} {'deals':>8} {'debt_covered_USD':>18}")
        for mk in sorted(by_month):
            n, u = by_month[mk]
            chain_n += n
            chain_usd += u
            print(f"{mk:<10} {n:8d} ${u:17,.0f}")
        print(f"{'TOTAL':<10} {chain_n:8d} ${chain_usd:17,.0f}\n")
        grand += chain_usd
        grand_n += chain_n

    print(f"COMBINED {args.days:g}d: {grand_n} deals | ${grand:,.0f} debt covered (оборот)")
    print("(это оборот погашенного долга, не прибыль)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
