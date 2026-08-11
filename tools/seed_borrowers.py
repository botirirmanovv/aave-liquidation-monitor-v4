"""Backfill Aave Borrow (+ optional Supply) users into monitor state.

Cold start without this leaves the book empty until live events arrive.

    python tools/seed_borrowers.py --chains base,arbitrum --days 14
    python tools/seed_borrowers.py --chain base --days 7 --chunk 8000

Uses public HTTP from .env (or --rpc / built-in override). No transactions.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aave_bot.config import load_chain_config  # noqa: E402
from aave_bot.state import MonitorState  # noqa: E402
from aave_bot.topics import event_topic  # noqa: E402

BORROW_TOPIC = event_topic(
    "Borrow(address,address,address,uint256,uint8,uint256,uint16)"
)
SUPPLY_TOPIC = event_topic(
    "Supply(address,address,address,uint256,uint16)"
)

BLOCK_SECONDS = {"base": 2.0, "arbitrum": 0.25, "optimism": 2.0, "default": 12.0}

HTTP_OVERRIDE = {
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "optimism": "https://mainnet.optimism.io",
}


def _topic_address(topic: str | bytes) -> str:
    raw = topic.hex() if hasattr(topic, "hex") else str(topic)
    raw = raw.lower().removeprefix("0x")
    return Web3.to_checksum_address("0x" + raw[-40:])


def seed_chain(
    chain: str,
    *,
    days: float,
    chunk: int,
    include_supply: bool,
    rpc: str | None,
    dry_run: bool,
) -> int:
    cfg = load_chain_config(chain)
    http = rpc or HTTP_OVERRIDE.get(chain) or cfg.http_rpc_url
    w3 = Web3(Web3.HTTPProvider(http, request_kwargs={"timeout": 60}))
    if not w3.is_connected():
        print(f"[{chain}] RPC not connected: {http}", file=sys.stderr)
        return 1

    head = w3.eth.block_number
    sec = BLOCK_SECONDS.get(chain, BLOCK_SECONDS["default"])
    span = max(1, int(days * 86400 / sec))
    start = max(0, head - span)

    topics: list[str] = [BORROW_TOPIC]
    if include_supply:
        topics.append(SUPPLY_TOPIC)

    state = MonitorState(cfg.state_file)
    state.load()
    before = len(state.tracked_users)

    print(
        f"=== {chain.upper()} ===  blocks {start}..{head} (~{days}d)  "
        f"rpc={http}  state={cfg.state_file}"
    )
    print(f"tracked before: {before}")

    added = 0
    cursor = start
    current_chunk = chunk
    while cursor <= head:
        to_block = min(cursor + current_chunk - 1, head)
        try:
            logs = w3.eth.get_logs({
                "fromBlock": cursor,
                "toBlock": to_block,
                "address": cfg.pool,
                "topics": [topics],
            })
        except Exception as exc:
            msg = str(exc).lower()
            if current_chunk > 500 and ("range" in msg or "limited" in msg or "10000" in msg):
                current_chunk = max(500, current_chunk // 2)
                print(f"  shrink chunk -> {current_chunk} ({exc})")
                continue
            print(f"  getLogs failed {cursor}-{to_block}: {exc}")
            time.sleep(1.0)
            cursor = to_block + 1
            continue

        for entry in logs:
            t = entry["topics"]
            if len(t) < 3:
                continue
            reserve = _topic_address(t[1])
            user = _topic_address(t[2])  # onBehalfOf
            if state.track(user, reserve):
                added += 1

        print(
            f"\r  scan {to_block}/{head}  users={len(state.tracked_users)}  "
            f"new={added}   ",
            end="",
            flush=True,
        )
        cursor = to_block + 1
        time.sleep(0.05)

    print()
    if dry_run:
        print(
            f"dry-run: would save {len(state.tracked_users)} users "
            f"(+{added} new) — skipped write"
        )
        return 0

    state.save(force=True)
    print(
        f"saved {len(state.tracked_users)} users (+{added} new since load) "
        f"-> {cfg.state_file}"
    )
    return 0


def _parse_chains(args: argparse.Namespace) -> list[str]:
    chains: list[str] = []
    if args.chains_csv:
        chains.extend(c.strip() for c in args.chains_csv.split(",") if c.strip())
    if args.chain:
        chains.extend(args.chain)
    # de-dupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for c in chains:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out or ["base", "arbitrum"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", action="append", default=None)
    parser.add_argument("--chains", dest="chains_csv", type=str, default=None)
    parser.add_argument("--days", type=float, default=14.0)
    parser.add_argument("--chunk", type=int, default=8_000)
    parser.add_argument(
        "--include-supply",
        action="store_true",
        help="also index Supply onBehalfOf (larger book)",
    )
    parser.add_argument("--rpc", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    code = 0
    for chain in _parse_chains(args):
        code = max(
            code,
            seed_chain(
                chain,
                days=args.days,
                chunk=args.chunk,
                include_supply=args.include_supply,
                rpc=args.rpc,
                dry_run=args.dry_run,
            ),
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
