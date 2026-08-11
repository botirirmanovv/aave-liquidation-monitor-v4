"""30d historical Balancer(0%) + UniV3 fee-tier arb stats.

Samples QuoterV2 round-trips at evenly spaced blocks (flash fee = 0).

    python tools/report_balancer_month.py --chains base,arbitrum --days 30
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aave_bot import abis  # noqa: E402
from aave_bot.config import load_chain_config  # noqa: E402
from aave_bot.strategies.balancer_v3_arb import (  # noqa: E402
    UNISWAP_V3_QUOTER_V2,
    UNISWAP_V3_QUOTER_V2_BY_CHAIN,
    V3_FEES,
)

BLOCK_SECONDS = {"base": 2.0, "arbitrum": 0.25, "optimism": 2.0}
HTTP = {
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
}
PAIR = {
    "base": (
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
        "0x4200000000000000000000000000000000000006",  # WETH
    ),
    "arbitrum": (
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
    ),
}
# Probe notionals in token units
PROBE = {
    "base": [("USDC->WETH->USDC", 0, 500_000000), ("USDC->WETH->USDC", 0, 2_000_000000)],
    "arbitrum": [("USDC->WETH->USDC", 0, 500_000000), ("USDC->WETH->USDC", 0, 2_000_000000)],
}
# Highest-liquidity fee-tier mismatches (buy != sell).
FEE_PAIRS = [
    (100, 500),
    (500, 100),
    (500, 3000),
    (3000, 500),
    (100, 3000),
    (3000, 100),
]


def quote(quoter, token_in, token_out, amount, fee, block) -> int | None:
    try:
        out, *_ = quoter.functions.quoteExactInputSingle(
            (token_in, token_out, amount, fee, 0)
        ).call(block_identifier=block)
        return int(out)
    except Exception:
        return None


def scan_chain(chain: str, days: float, samples: int, min_bps: int) -> dict:
    http = HTTP.get(chain) or load_chain_config(chain).http_rpc_url
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

    print(f"\n=== {chain.upper()} Balancer+V3 ({days:g}d) ===")
    print(f"blocks {start}..{head}  samples={len(blocks)}  quoter={q_addr}")
    print(f"flash fee=0%  min_profit={min_bps}bps  pair=USDC/WETH")

    ok = fail = hits = 0
    best = None  # (net_bps, net_usd_approx, detail)
    hit_blocks: set[int] = set()

    for i, block in enumerate(blocks):
        print(f"\r  sample {i+1}/{len(blocks)} block={block}  hits={hits}   ", end="", flush=True)
        for label, _idx, amount in PROBE[chain]:
            token_a, token_b = usdc, weth
            for fee_buy, fee_sell in FEE_PAIRS:
                mid = quote(quoter, token_a, token_b, amount, fee_buy, block)
                if mid is None:
                    fail += 1
                    continue
                back = quote(quoter, token_b, token_a, mid, fee_sell, block)
                if back is None:
                    fail += 1
                    continue
                ok += 1
                # Balancer flash premium = 0
                net = back - amount
                bps = (net * 10_000) // amount if amount else 0
                # USDC 6dp → USD
                net_usd = net / 1e6
                detail = (
                    f"{label} fees {fee_buy}/{fee_sell} size=${amount/1e6:.0f} "
                    f"net=${net_usd:.4f} ({bps}bps) block={block}"
                )
                if best is None or net > best[0]:
                    best = (net, net_usd, detail)
                if bps >= min_bps and net > 0:
                    hits += 1
                    hit_blocks.add(block)
        time.sleep(0.08)
    print()

    print(f"quotes ok/fail: {ok}/{fail}")
    print(f"+EV hits (>= {min_bps}bps, fee=0): {hits}")
    print(f"sample blocks with +EV: {len(hit_blocks)}/{len(blocks)}")
    if best:
        print(f"best (any): {best[2]}")
    if hits == 0:
        print("no durable Balancer+V3 edge on USDC/WETH over this window")

    return {
        "chain": chain,
        "ok": ok,
        "fail": fail,
        "hits": hits,
        "hit_blocks": len(hit_blocks),
        "blocks": len(blocks),
        "best_usd": best[1] if best else None,
        "best_detail": best[2] if best else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default="base,arbitrum")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--samples", type=int, default=24)
    ap.add_argument("--min-bps", type=int, default=5)
    args = ap.parse_args()

    print("BALANCER + UNIV3 — MONTHLY HISTORICAL REPORT")
    print(f"flash lender fee: 0% (Balancer Vault)  |  days={args.days:g}")
    rows = []
    for chain in [c.strip() for c in args.chains.split(",") if c.strip()]:
        rows.append(scan_chain(chain, args.days, args.samples, args.min_bps))

    print("\n=== SUMMARY ===")
    total_hits = sum(r["hits"] for r in rows)
    print(f"total +EV route hits: {total_hits}")
    for r in rows:
        print(
            f"  {r['chain']}: hits={r['hits']}  "
            f"+EV blocks={r['hit_blocks']}/{r['blocks']}  "
            f"quotes={r['ok']}/{r['ok']+r['fail']}  "
            f"best_usd={r['best_usd']}"
        )
    if total_hits == 0:
        print(
            "Conclusion: over ~1 month of sampled UniV3 fee-tier round-trips on "
            "USDC/WETH, Balancer 0% flash still shows no durable +EV vs min profit."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
