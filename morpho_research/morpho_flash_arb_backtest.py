#!/usr/bin/env python3
"""Morpho flash-arb 30d historical sample backtest (fee = 0).

Samples past blocks; quotes UniV2 cross-router + UniV3 fee-tier USDC->WETH->USDC
as if funded by Morpho.flashLoan (0 premium).

Usage:
  .venv-run/Scripts/python.exe morpho_research/morpho_flash_arb_backtest.py --days 30 --samples 24
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from web3 import Web3

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
for p in (_DIR, _ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from aave_bot import abis  # noqa: E402
from aave_bot import config as bot_config  # noqa: E402
from aave_bot.strategies.balancer_v3_arb import (  # noqa: E402
    UNISWAP_V3_QUOTER_V2,
    UNISWAP_V3_QUOTER_V2_BY_CHAIN,
)
from morpho_flash_arb import (  # noqa: E402
    DEFAULT_AMOUNTS,
    DEFAULT_ROUTERS,
    DEFAULT_TOKENS,
    MORPHO_FLASH_PREMIUM_BPS,
)

OUT_DIR = _DIR / "out"
BLOCK_SECONDS = {"base": 2.0, "arbitrum": 0.25}
V3_FEE_PAIRS = ((500, 100), (100, 500), (500, 3000), (3000, 500))
MIN_BPS = 5


@dataclass
class Hit:
    chain: str
    block: int
    ts: int
    kind: str
    borrow: str
    amount: int
    profit: int
    profit_usd: float
    bps: float
    detail: str


def month_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def http_rpc(chain: str) -> str:
    try:
        return bot_config._text("HTTP_RPC_URL", chain, required=True, inherit=False)
    except Exception:
        return {
            "base": "https://mainnet.base.org",
            "arbitrum": "https://arb1.arbitrum.io/rpc",
        }[chain]


def quote_v2_leg(router, amount: int, path: list[str], block: int) -> int | None:
    try:
        return int(
            router.functions.getAmountsOut(amount, path).call(block_identifier=block)[-1]
        )
    except Exception:
        return None


def quote_v3(
    quoter, token_in: str, token_out: str, amount: int, fee: int, block: int
) -> int | None:
    try:
        out, *_ = quoter.functions.quoteExactInputSingle(
            (token_in, token_out, amount, fee, 0)
        ).call(block_identifier=block)
        return int(out)
    except Exception:
        return None


def sizes_usdc(chain: str) -> list[int]:
    base = DEFAULT_AMOUNTS.get(chain, {}).get("USDC", 5_000_000000)
    return [max(1, int(base * s)) for s in (0.5, 1.0, 2.0)]


def best_at_block(
    *,
    chain: str,
    routers: list,
    quoter,
    usdc: str,
    weth: str,
    block: int,
    ts: int,
    min_bps: int,
) -> list[Hit]:
    hits: list[Hit] = []
    for amount in sizes_usdc(chain):
        if len(routers) >= 2:
            best_v2: Hit | None = None
            for i, buy in enumerate(routers):
                for j, sell in enumerate(routers):
                    if i == j:
                        continue
                    mid_out = quote_v2_leg(buy, amount, [usdc, weth], block)
                    if not mid_out:
                        continue
                    back = quote_v2_leg(sell, mid_out, [weth, usdc], block)
                    if not back:
                        continue
                    premium = (amount * MORPHO_FLASH_PREMIUM_BPS) // 10_000
                    owed = amount + premium
                    if back <= owed:
                        continue
                    profit = back - owed
                    bps = profit * 10_000 / amount
                    if bps < min_bps:
                        continue
                    cand = Hit(
                        chain=chain,
                        block=block,
                        ts=ts,
                        kind="v2",
                        borrow="USDC",
                        amount=amount,
                        profit=profit,
                        profit_usd=profit / 1e6,
                        bps=bps,
                        detail=f"V2 r{i}->{j}",
                    )
                    if best_v2 is None or cand.profit > best_v2.profit:
                        best_v2 = cand
            if best_v2 is not None:
                hits.append(best_v2)

        if quoter is not None:
            best_v3: Hit | None = None
            for fee_buy, fee_sell in V3_FEE_PAIRS:
                mid_out = quote_v3(quoter, usdc, weth, amount, fee_buy, block)
                if not mid_out:
                    continue
                back = quote_v3(quoter, weth, usdc, mid_out, fee_sell, block)
                if not back or back <= amount:
                    continue
                profit = back - amount
                bps = profit * 10_000 / amount
                if bps < min_bps:
                    continue
                cand = Hit(
                    chain=chain,
                    block=block,
                    ts=ts,
                    kind="v3",
                    borrow="USDC",
                    amount=amount,
                    profit=profit,
                    profit_usd=profit / 1e6,
                    bps=bps,
                    detail=f"V3 {fee_buy}/{fee_sell}",
                )
                if best_v3 is None or cand.profit > best_v3.profit:
                    best_v3 = cand
            if best_v3 is not None:
                hits.append(best_v3)
    return hits


def run_chain(chain: str, days: float, samples: int, min_bps: int) -> dict:
    w3 = Web3(Web3.HTTPProvider(http_rpc(chain), request_kwargs={"timeout": 45}))
    if not w3.is_connected():
        raise RuntimeError(f"{chain} RPC down")
    head = int(w3.eth.block_number)
    span = int(days * 86400 / BLOCK_SECONDS[chain])
    start = max(0, head - span)
    step = max(1, span // samples)
    blocks = list(range(start, head, step))[:samples]

    usdc = Web3.to_checksum_address(DEFAULT_TOKENS[chain][0][1])
    weth = Web3.to_checksum_address(DEFAULT_TOKENS[chain][1][1])
    routers = [
        w3.eth.contract(address=Web3.to_checksum_address(a), abi=abis.ROUTER_QUOTE_ABI)
        for a in DEFAULT_ROUTERS[chain]
    ]
    q_addr = UNISWAP_V3_QUOTER_V2_BY_CHAIN.get(chain, UNISWAP_V3_QUOTER_V2)
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(q_addr), abi=abis.UNISWAP_V3_QUOTER_V2_ABI
    )

    print(
        f"\n=== MORPHO FLASH-ARB {chain.upper()} {days:g}d "
        f"(fee={MORPHO_FLASH_PREMIUM_BPS}bps, min={min_bps}bps) ==="
    )
    print(f"blocks {start}..{head} samples={len(blocks)}")

    all_hits: list[Hit] = []
    samples_with_hit = 0
    by_month: dict[str, list[Hit]] = defaultdict(list)
    sample_best_usd: list[float] = []

    for i, block in enumerate(blocks):
        print(f"\r  sample {i+1}/{len(blocks)} block={block}   ", end="", flush=True)
        try:
            ts = int(w3.eth.get_block(block)["timestamp"])
        except Exception:
            continue
        hits = best_at_block(
            chain=chain,
            routers=routers,
            quoter=quoter,
            usdc=usdc,
            weth=weth,
            block=block,
            ts=ts,
            min_bps=min_bps,
        )
        if hits:
            samples_with_hit += 1
            best = max(hits, key=lambda h: h.profit_usd)
            sample_best_usd.append(best.profit_usd)
            all_hits.extend(hits)
            by_month[month_key(ts)].extend(hits)
        else:
            sample_best_usd.append(0.0)
        time.sleep(0.02)
    print()

    n = len(sample_best_usd)
    hit_rate = samples_with_hit / max(1, n)
    avg_hit = (
        sum(x for x in sample_best_usd if x > 0) / samples_with_hit
        if samples_with_hit
        else 0.0
    )
    live_scans_per_day = 86400 / 20
    monthly_hits_est = hit_rate * live_scans_per_day * 30
    monthly_gross_est = monthly_hits_est * avg_hit
    monthly_capture_est = monthly_gross_est * 0.25
    best_hit = max(all_hits, key=lambda h: h.profit_usd) if all_hits else None

    print(f"\n--- {chain} summary ---")
    print(f"samples={n}  +EV samples={samples_with_hit}  hit_rate={100*hit_rate:.1f}%")
    print(f"USDC +EV routes={len(all_hits)}")
    if best_hit:
        when = datetime.fromtimestamp(best_hit.ts, tz=timezone.utc)
        print(
            f"best: ${best_hit.profit_usd:.4f} ({best_hit.bps:.1f} bps) "
            f"{best_hit.detail} size=${best_hit.amount/1e6:.0f} @ {when}"
        )
    print(f"avg +EV (when hit): ${avg_hit:.4f}")
    print(f"naive monthly gross (20s scans): ${monthly_gross_est:,.2f}")
    print(f"realistic capture @25%: ${monthly_capture_est:,.2f}/mo")
    for mk in sorted(by_month):
        rows = by_month[mk]
        best = max(rows, key=lambda h: h.profit_usd)
        print(
            f"  {mk}: routes={len(rows)} best=${best.profit_usd:.4f} "
            f"({best.bps:.1f}bps) {best.detail}"
        )

    return {
        "chain": chain,
        "days": days,
        "samples": n,
        "ev_samples": samples_with_hit,
        "hit_rate": hit_rate,
        "avg_hit_usd": avg_hit,
        "best_usd": best_hit.profit_usd if best_hit else 0.0,
        "best": asdict(best_hit) if best_hit else None,
        "monthly_gross_est": monthly_gross_est,
        "monthly_capture_est_25pct": monthly_capture_est,
        "hits": [asdict(h) for h in all_hits],
        "morpho_flash_fee_bps": MORPHO_FLASH_PREMIUM_BPS,
        "min_bps": min_bps,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default="base,arbitrum")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--samples", type=int, default=24)
    ap.add_argument("--min-bps", type=int, default=MIN_BPS)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("MORPHO FLASH-ARB BACKTEST (Morpho.flashLoan fee=0)")
    print(f"USDC sizes around ${DEFAULT_AMOUNTS['base']['USDC']/1e6:.0f}")
    print(f"min_bps={args.min_bps} samples/chain={args.samples}")

    reports = []
    for chain in [c.strip().lower() for c in args.chains.split(",") if c.strip()]:
        if chain not in DEFAULT_TOKENS:
            continue
        rep = run_chain(chain, args.days, args.samples, args.min_bps)
        reports.append(rep)
        path = OUT_DIR / f"flash_arb_backtest_{chain}_{int(args.days)}d.json"
        path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(f"wrote {path}")

    lines = [
        "=== Morpho flash-arb monthly outlook ===",
        f"generated: {datetime.now(tz=timezone.utc).isoformat()}",
        f"fee=0 (Morpho) min_bps={args.min_bps} days={args.days}",
        "",
    ]
    total_cap = 0.0
    for r in reports:
        lines.append(
            f"{r['chain']}: hit_rate={100*r['hit_rate']:.1f}% "
            f"best=${r['best_usd']:.4f} "
            f"est_gross~${r['monthly_gross_est']:,.0f}/mo "
            f"capture25%~${r['monthly_capture_est_25pct']:,.0f}/mo"
        )
        total_cap += r["monthly_capture_est_25pct"]
    lines.append("")
    lines.append(f"Combined capture@25% ~ ${total_cap:,.0f}/mo")
    lines.append(
        "Note: sparse historical samples; live edge is rare. "
        "Ground truth = best_usd + hit_rate, not naive gross."
    )
    summary = "\n".join(lines)
    summary_path = OUT_DIR / "flash_arb_backtest_summary.txt"
    summary_path.write_text(summary, encoding="utf-8")
    print()
    print(summary)
    print(f"\nsummary -> {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
