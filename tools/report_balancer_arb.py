"""One-shot Balancer-fee=0 + UniV3 fee-tier stats report.

    python tools/report_balancer_arb.py --chains base,arbitrum --sizes 500,1000,2000,5000
"""
from __future__ import annotations

import argparse
import sys
import time
from itertools import permutations
from pathlib import Path

from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aave_bot.config import load_chain_config  # noqa: E402
from aave_bot.chain import ChainContext  # noqa: E402
from aave_bot.strategies.balancer_v3_arb import (  # noqa: E402
    UNISWAP_V3_QUOTER_V2,
    UNISWAP_V3_QUOTER_V2_BY_CHAIN,
    V3_FEES,
    BalancerV3ArbStrategy,
)

HTTP = {
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
}


def report_chain(chain: str, sizes_usd: list[float], min_bps: int) -> dict:
    cfg = load_chain_config(chain)
    # Prefer official HTTP for this one-shot (publicnode often flakes on eth_call).
    if chain in HTTP:
        cfg.http_rpc_url = HTTP[chain]

    ctx = ChainContext(cfg)
    ctx.connect()
    ctx.warm_caches()

    strat = BalancerV3ArbStrategy(ctx)
    # Force tokens from balancer refs or USDC/WETH
    refs = cfg.balancer_arb_token_refs or cfg.flash_arb_token_refs or ["USDC", "WETH"]
    strat.tokens = strat._resolve_tokens(refs)
    strat.amounts = {}
    strat.config.balancer_arb_min_profit_bps = min_bps  # type: ignore[attr-defined]

    from aave_bot import abis
    q_addr = UNISWAP_V3_QUOTER_V2_BY_CHAIN.get(chain, UNISWAP_V3_QUOTER_V2)
    strat._quoter = ctx.w3.eth.contract(
        address=Web3.to_checksum_address(q_addr),
        abi=abis.UNISWAP_V3_QUOTER_V2_ABI,
    )

    if len(strat.tokens) < 2:
        print(f"[{chain}] not enough tokens: {strat.tokens}")
        return {"chain": chain, "quotes": 0, "hits": 0}

    print(f"\n=== {chain.upper()} Balancer(0%) + UniV3 ===")
    print(f"tokens: {[ctx.symbol_of(t) for t in strat.tokens]}")
    print(f"fee tiers: {V3_FEES}  min_profit_bps={min_bps}")

    quotes_ok = 0
    quotes_fail = 0
    hits = []
    best_loss = None  # (net, detail)

    for borrow in strat.tokens:
        dec = ctx.decimals(borrow) or 18
        price = ctx.asset_price(borrow)
        if not price:
            continue
        for usd in sizes_usd:
            amount = int(usd * 1e8 * (10 ** dec) / price)
            if amount <= 0:
                continue
            for mid in strat.tokens:
                if mid == borrow:
                    continue
                for fee_buy in V3_FEES:
                    out_buy = strat._quote_v3(borrow, mid, amount, fee_buy)
                    if not out_buy:
                        quotes_fail += 1
                        continue
                    quotes_ok += 1
                    for fee_sell in V3_FEES:
                        if fee_sell == fee_buy:
                            continue
                        out_sell = strat._quote_v3(mid, borrow, out_buy, fee_sell)
                        if not out_sell:
                            quotes_fail += 1
                            continue
                        quotes_ok += 1
                        # Balancer repay = amount (fee 0)
                        net = out_sell - amount
                        net_usd = (net * price) / (10 ** dec) / 1e8
                        detail = (
                            f"{ctx.symbol_of(borrow)}->{ctx.symbol_of(mid)}->"
                            f"{ctx.symbol_of(borrow)} fees {fee_buy}/{fee_sell} "
                            f"size=${usd:g} net=${net_usd:,.4f}"
                        )
                        if best_loss is None or net > best_loss[0]:
                            best_loss = (net, detail, net_usd)
                        min_p = (amount * min_bps) // 10_000
                        if net > min_p:
                            hits.append((net_usd, detail))

    hits.sort(reverse=True)
    print(f"quotes ok/fail: {quotes_ok}/{quotes_fail}")
    print(f"+EV hits (after 0 flash fee, min {min_bps}bps): {len(hits)}")
    for net_usd, detail in hits[:10]:
        print(f"  HIT +${net_usd:,.4f}  {detail}")
    if best_loss:
        print(f"best (even if loss): {best_loss[2]:+.4f} USD  {best_loss[1]}")
    if not hits:
        print("no profitable Balancer+V3 round-trip at probed sizes")
    return {
        "chain": chain,
        "quotes_ok": quotes_ok,
        "quotes_fail": quotes_fail,
        "hits": len(hits),
        "best_usd": best_loss[2] if best_loss else None,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser()
    p.add_argument("--chains", default="base,arbitrum")
    p.add_argument("--sizes", default="500,1000,2000,5000")
    p.add_argument("--min-bps", type=int, default=5)
    args = p.parse_args()
    sizes = [float(x) for x in args.sizes.split(",") if x.strip()]

    print("BALANCER + UNIV3 FLASH-ARB REPORT")
    print(f"flash lender fee: 0% (Balancer Vault)  |  time={time.strftime('%Y-%m-%d %H:%M')}")
    rows = []
    for chain in [c.strip() for c in args.chains.split(",") if c.strip()]:
        try:
            rows.append(report_chain(chain, sizes, args.min_bps))
        except Exception as exc:
            print(f"[{chain}] FAILED: {exc}")
            rows.append({"chain": chain, "hits": 0, "error": str(exc)})

    print("\n=== SUMMARY ===")
    total_hits = sum(r.get("hits", 0) for r in rows)
    print(f"total +EV routes: {total_hits}")
    for r in rows:
        print(
            f"  {r.get('chain')}: hits={r.get('hits')}  "
            f"best_net_usd={r.get('best_usd')}  "
            f"quotes={r.get('quotes_ok')}/{r.get('quotes_fail')}"
        )
    if total_hits == 0:
        print(
            "Conclusion: with Balancer 0% fee, majors still show no durable "
            "UniV3 fee-tier edge at these sizes (spread < min profit / already arb'd)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
