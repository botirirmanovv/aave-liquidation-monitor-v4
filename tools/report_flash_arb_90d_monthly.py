"""90d flash-arb stats at MAX ladder size, broken down by calendar month.

Probes Aave V2 round-trip (5bps premium) and Balancer+V3 (0 fee) at the
configured 4x max notional (default $20k USDC / 4 WETH).

    python tools/report_flash_arb_90d_monthly.py --chains base,arbitrum --days 90
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aave_bot import abis  # noqa: E402
from aave_bot.config import load_chain_config  # noqa: E402
from aave_bot.strategies.balancer_v3_arb import (  # noqa: E402
    UNISWAP_V3_QUOTER_V2,
    UNISWAP_V3_QUOTER_V2_BY_CHAIN,
)
from aave_bot.strategies.flash_arb import AAVE_FLASH_PREMIUM_BPS  # noqa: E402

BLOCK_SECONDS = {"base": 2.0, "arbitrum": 0.25, "optimism": 2.0}
HTTP = {
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
}
ROUTERS = {
    "base": [
        "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86",
        "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891",
    ],
    "arbitrum": [
        "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
        "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",
    ],
}
PAIR = {
    "base": (
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
        "0x4200000000000000000000000000000000000006",  # WETH
    ),
    "arbitrum": (
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    ),
}
# Max ladder = 4x of $5k / 1 WETH config
MAX_USDC = 20_000_000000  # $20k, 6 dp
MAX_WETH = 4 * 10**18     # 4 WETH
V3_FEE_PAIRS = [(500, 3000), (3000, 500), (100, 500), (500, 100)]
MIN_BPS = 5


def month_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def quote_v2(router, token_a, token_b, amount, block) -> int | None:
    try:
        mid = router.functions.getAmountsOut(amount, [token_a, token_b]).call(
            block_identifier=block
        )[-1]
        back = router.functions.getAmountsOut(mid, [token_b, token_a]).call(
            block_identifier=block
        )[-1]
        return int(back)
    except Exception:
        return None


def quote_v3(quoter, token_in, token_out, amount, fee, block) -> int | None:
    try:
        out, *_ = quoter.functions.quoteExactInputSingle(
            (token_in, token_out, amount, fee, 0)
        ).call(block_identifier=block)
        return int(out)
    except Exception:
        return None


def best_v2(w3, chain, usdc, weth, block) -> tuple[float, str] | None:
    """Return (net_usd, detail) for best USDC round-trip at MAX_USDC, or None."""
    routers = [
        w3.eth.contract(address=Web3.to_checksum_address(a), abi=abis.ROUTER_QUOTE_ABI)
        for a in ROUTERS[chain]
    ]
    best = None
    amount = MAX_USDC
    for i, buy in enumerate(routers):
        for j, sell in enumerate(routers):
            if i == j:
                continue
            back = quote_v2(buy, usdc, weth, amount, block)
            if back is None:
                continue
            premium = (amount * AAVE_FLASH_PREMIUM_BPS) // 10_000
            net = back - amount - premium
            net_usd = net / 1e6
            bps = (net * 10_000) // amount
            detail = f"V2 r{i}->{j} net=${net_usd:.4f} ({bps}bps)"
            if best is None or net > best[0]:
                best = (net, net_usd, bps, detail)
    if best is None:
        return None
    return best[1], best[3]


def best_v3(quoter, usdc, weth, block) -> tuple[float, str] | None:
    amount = MAX_USDC
    best = None
    for fee_buy, fee_sell in V3_FEE_PAIRS:
        mid = quote_v3(quoter, usdc, weth, amount, fee_buy, block)
        if mid is None:
            continue
        back = quote_v3(quoter, weth, usdc, mid, fee_sell, block)
        if back is None:
            continue
        net = back - amount  # Balancer fee 0
        net_usd = net / 1e6
        bps = (net * 10_000) // amount
        detail = f"V3 fees {fee_buy}/{fee_sell} net=${net_usd:.4f} ({bps}bps)"
        if best is None or net > best[0]:
            best = (net, net_usd, bps, detail)
    if best is None:
        return None
    return best[1], best[3]


def scan_chain(chain: str, days: float, samples: int, min_bps: int) -> None:
    http = HTTP[chain]
    w3 = Web3(Web3.HTTPProvider(http, request_kwargs={"timeout": 45}))
    head = int(w3.eth.block_number)
    span = int(days * 24 * 3600 / BLOCK_SECONDS[chain])
    start = max(0, head - span)
    step = max(1, span // samples)
    blocks = list(range(start, head, step))[:samples]

    usdc, weth = (Web3.to_checksum_address(a) for a in PAIR[chain])
    q_addr = UNISWAP_V3_QUOTER_V2_BY_CHAIN.get(chain, UNISWAP_V3_QUOTER_V2)
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(q_addr), abi=abis.UNISWAP_V3_QUOTER_V2_ABI
    )

    print(f"\n=== {chain.upper()}  {days:g}d @ MAX size ${MAX_USDC/1e6:.0f} USDC / {MAX_WETH/1e18:.0f} WETH ===")
    print(f"blocks {start}..{head}  samples={len(blocks)}  min_bps={min_bps}")
    print(f"Aave flash premium={AAVE_FLASH_PREMIUM_BPS}bps | Balancer fee=0")

    by_month_v2: dict[str, list] = defaultdict(list)
    by_month_v3: dict[str, list] = defaultdict(list)
    # each entry: (net_usd, hit_bool, detail, block)

    for i, block in enumerate(blocks):
        print(f"\r  sample {i+1}/{len(blocks)} block={block}   ", end="", flush=True)
        try:
            ts = int(w3.eth.get_block(block)["timestamp"])
        except Exception:
            continue
        mk = month_key(ts)

        v2 = best_v2(w3, chain, usdc, weth, block)
        if v2:
            net_usd, detail = v2
            hit = net_usd * 1e6 * 10_000 / MAX_USDC >= min_bps and net_usd > 0
            by_month_v2[mk].append((net_usd, hit, detail, block))

        v3 = best_v3(quoter, usdc, weth, block)
        if v3:
            net_usd, detail = v3
            hit = net_usd * 1e6 * 10_000 / MAX_USDC >= min_bps and net_usd > 0
            by_month_v3[mk].append((net_usd, hit, detail, block))

        time.sleep(0.05)
    print()

    def print_engine(name: str, by_month: dict) -> None:
        print(f"\n--- {name} by month ---")
        if not by_month:
            print("  no quotes")
            return
        total_hits = 0
        total_n = 0
        best_all = None
        for mk in sorted(by_month):
            rows = by_month[mk]
            hits = sum(1 for _, h, _, _ in rows if h)
            total_hits += hits
            total_n += len(rows)
            best = max(rows, key=lambda r: r[0])
            if best_all is None or best[0] > best_all[0]:
                best_all = best
            avg = sum(r[0] for r in rows) / len(rows)
            print(
                f"  {mk}: samples={len(rows)}  +EV={hits}  "
                f"best=${best[0]:.4f}  avg_net=${avg:.4f}"
            )
            print(f"         best: {best[2]} @ block {best[3]}")
        print(
            f"  TOTAL {days:g}d: samples={total_n}  +EV hits={total_hits}  "
            f"hit_rate={100*total_hits/max(1,total_n):.1f}%"
        )
        if best_all:
            print(f"  overall best: ${best_all[0]:.4f}  {best_all[2]}")

    print_engine("Aave flash + UniV2 (5bps)", by_month_v2)
    print_engine("Balancer + UniV3 (0 fee)", by_month_v3)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default="base,arbitrum")
    ap.add_argument("--days", type=float, default=90.0)
    ap.add_argument("--samples", type=int, default=36)
    ap.add_argument("--min-bps", type=int, default=MIN_BPS)
    args = ap.parse_args()

    print("FLASH-ARB 90d MONTHLY REPORT @ MAX LADDER SIZE")
    print(f"max notional: ${MAX_USDC/1e6:.0f} USDC  |  {MAX_WETH/1e18:.0f} WETH (probe uses USDC leg)")
    print(f"ladder in live bot: 0.25x..4x around $5k / 1 WETH")

    for chain in [c.strip() for c in args.chains.split(",") if c.strip()]:
        scan_chain(chain, args.days, args.samples, args.min_bps)

    print("\nDone. Live .env amounts raised to match ($5k/1 WETH base → $20k/4 WETH max).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
