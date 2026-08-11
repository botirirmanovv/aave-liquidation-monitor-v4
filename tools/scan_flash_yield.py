"""Overnight flash-yield probe: multi-size V2 cross-router + optional UniV3 hops.

Searches for Aave-flash-repayable round trips that clear the 5 bps premium.
V2 alone rarely wins; this tool also quotes Uniswap V3 QuoterV2 fee tiers when
configured, which is the only realistic on-chain DEX edge left on L2.

    python tools/scan_flash_yield.py --chains base,arbitrum
    python tools/scan_flash_yield.py --chain base --sizes 200,500,1000,2000,5000

Writes yield_probe_<chain>.json and prints a ranked summary.
No transactions are sent.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from itertools import permutations
from pathlib import Path

from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aave_bot.config import load_chain_config  # noqa: E402
from aave_bot.strategies.flash_arb import AAVE_FLASH_PREMIUM_BPS  # noqa: E402

ROUTER_ABI = [
    {
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# Uniswap V3 QuoterV2 (same address on Base / Arbitrum / Optimism / Ethereum).
QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
QUOTER_V2_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]
V3_FEES = (100, 500, 3000, 10000)  # 0.01%, 0.05%, 0.3%, 1%

ERC20 = [
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}],
     "stateMutability": "view", "type": "function"},
]

ORACLE_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}], "name": "getAssetPrice",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

# Liquid majors available on Aave V3 Base / Arb (addresses resolved via symbols
# when present in FLASH_ARB_TOKENS / reserves; extras below are checksum literals).
EXTRA_MIDS = {
    "base": {
        # cbETH, DAI on Base when liquid
        "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22": "cbETH",
        "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb": "DAI",
    },
    "arbitrum": {
        "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9": "USDT",
        "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1": "DAI",
        "0x912CE59144191C1204E64559FE8253a0e49E6548": "ARB",
    },
}

HTTP = {
    "base": "https://base-rpc.publicnode.com",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
}


@dataclass
class Hit:
    chain: str
    kind: str  # v2_cross | v3_cross | v2v3_mixed
    borrow: str
    mid: str
    amount: int
    amount_usd: float
    net: int
    net_usd: float
    buy: str
    sell: str
    detail: str


def _meta(w3: Web3, addr: str) -> tuple[str, int]:
    c = w3.eth.contract(address=addr, abi=ERC20)
    try:
        sym = c.functions.symbol().call()
    except Exception:
        sym = addr[:10]
    try:
        dec = int(c.functions.decimals().call())
    except Exception:
        dec = 18
    return str(sym), dec


def _usd(oracle, asset: str, amount: int, decimals: int) -> float:
    try:
        price = int(oracle.functions.getAssetPrice(asset).call())  # 8 dp USD
    except Exception:
        return 0.0
    return (amount * price) / (10 ** decimals) / 1e8


def _v2_out(router, amount: int, path: list[str]) -> int | None:
    try:
        return int(router.functions.getAmountsOut(amount, path).call()[-1])
    except Exception:
        return None


def _v3_best_out(quoter, token_in: str, token_out: str, amount: int) -> tuple[int, int] | None:
    """Return (amountOut, fee) for the best fee tier, or None."""
    best: tuple[int, int] | None = None
    for fee in V3_FEES:
        try:
            # QuoterV2 uses a state-mutating call pattern; eth_call still works.
            out, *_rest = quoter.functions.quoteExactInputSingle(
                (token_in, token_out, amount, fee, 0)
            ).call()
            out = int(out)
            if best is None or out > best[0]:
                best = (out, fee)
        except Exception:
            continue
    return best


def probe_chain(chain: str, size_usd: list[float], min_profit_bps: int) -> list[Hit]:
    cfg = load_chain_config(chain)
    http = HTTP.get(chain, cfg.http_rpc_url)
    w3 = Web3(Web3.HTTPProvider(http, request_kwargs={"timeout": 45}))
    if not w3.is_connected():
        print(f"[{chain}] RPC down: {http}")
        return []

    oracle = w3.eth.contract(address=cfg.oracle, abi=ORACLE_ABI)
    routers = [
        w3.eth.contract(address=Web3.to_checksum_address(a), abi=ROUTER_ABI)
        for a in cfg.routers
    ]
    router_addrs = [r.address for r in routers]

    # Borrow / mid universe from config tokens + extras that exist on-chain.
    tokens: dict[str, tuple[str, int]] = {}
    refs = list(cfg.flash_arb_token_refs) or ["USDC", "WETH"]
    from aave_bot.chain import ChainContext

    ctx = ChainContext(cfg)
    ctx.connect()
    ctx.warm_caches()
    by_symbol = {
        ctx.symbol_of(asset).upper(): asset for asset in ctx.all_reserves()
    }
    for ref in refs:
        try:
            if ref.startswith("0x") and len(ref) == 42:
                addr = Web3.to_checksum_address(ref)
            else:
                addr = by_symbol.get(ref.upper())
                if not addr:
                    print(f"[{chain}] skip token {ref}: not in Aave reserves")
                    continue
            tokens[addr] = _meta(w3, addr)
        except Exception as exc:
            print(f"[{chain}] skip token {ref}: {exc}")

    for addr, _sym in EXTRA_MIDS.get(chain, {}).items():
        try:
            a = Web3.to_checksum_address(addr)
            if a not in tokens:
                tokens[a] = _meta(w3, a)
        except Exception:
            pass

    if len(tokens) < 2 or len(routers) < 2:
        print(f"[{chain}] need >=2 tokens and >=2 routers — got {len(tokens)}/{len(routers)}")
        return []

    quoter = None
    try:
        code = w3.eth.get_code(Web3.to_checksum_address(QUOTER_V2))
        if code and code != b"\x00" and len(code) > 2:
            quoter = w3.eth.contract(
                address=Web3.to_checksum_address(QUOTER_V2), abi=QUOTER_V2_ABI
            )
            print(f"[{chain}] UniV3 QuoterV2 available")
    except Exception:
        quoter = None

    hits: list[Hit] = []
    addrs = list(tokens.keys())
    print(f"[{chain}] tokens={[(tokens[a][0], a[:8]) for a in addrs]} routers={len(routers)}")

    for borrow in addrs:
        sym_b, dec_b = tokens[borrow]
        price = int(oracle.functions.getAssetPrice(borrow).call())
        if price <= 0:
            continue
        for usd in size_usd:
            # amount = usd * 1e8 / price * 10**decimals
            amount = int(usd * 1e8 * (10 ** dec_b) / price)
            if amount <= 0:
                continue
            for mid in addrs:
                if mid == borrow:
                    continue
                # --- V2 cross-router ---
                for i, j in permutations(range(len(routers)), 2):
                    out1 = _v2_out(routers[i], amount, [borrow, mid])
                    if not out1:
                        continue
                    out2 = _v2_out(routers[j], out1, [mid, borrow])
                    if not out2:
                        continue
                    premium = (amount * AAVE_FLASH_PREMIUM_BPS) // 10_000
                    net = out2 - amount - premium
                    min_p = (amount * min_profit_bps) // 10_000
                    if net > min_p:
                        hits.append(Hit(
                            chain=chain, kind="v2_cross", borrow=sym_b,
                            mid=tokens[mid][0], amount=amount,
                            amount_usd=_usd(oracle, borrow, amount, dec_b),
                            net=net, net_usd=_usd(oracle, borrow, net, dec_b),
                            buy=router_addrs[i], sell=router_addrs[j],
                            detail="getAmountsOut round-trip",
                        ))

                # --- V3 buy + V3 sell (different fees ok) ---
                if quoter is None:
                    continue
                buy = _v3_best_out(quoter, borrow, mid, amount)
                if not buy:
                    continue
                mid_out, fee_buy = buy
                sell = _v3_best_out(quoter, mid, borrow, mid_out)
                if not sell:
                    continue
                back, fee_sell = sell
                premium = (amount * AAVE_FLASH_PREMIUM_BPS) // 10_000
                net = back - amount - premium
                min_p = (amount * min_profit_bps) // 10_000
                if net > min_p:
                    hits.append(Hit(
                        chain=chain, kind="v3_cross", borrow=sym_b,
                        mid=tokens[mid][0], amount=amount,
                        amount_usd=_usd(oracle, borrow, amount, dec_b),
                        net=net, net_usd=_usd(oracle, borrow, net, dec_b),
                        buy=f"univ3:{fee_buy}", sell=f"univ3:{fee_sell}",
                        detail=f"QuoterV2 fees {fee_buy}/{fee_sell}",
                    ))

                # --- Mixed: V2 buy -> V3 sell and reverse ---
                for i, router in enumerate(routers):
                    out1 = _v2_out(router, amount, [borrow, mid])
                    if out1:
                        sell = _v3_best_out(quoter, mid, borrow, out1)
                        if sell:
                            back, fee_sell = sell
                            premium = (amount * AAVE_FLASH_PREMIUM_BPS) // 10_000
                            net = back - amount - premium
                            if net > (amount * min_profit_bps) // 10_000:
                                hits.append(Hit(
                                    chain=chain, kind="v2v3_mixed", borrow=sym_b,
                                    mid=tokens[mid][0], amount=amount,
                                    amount_usd=_usd(oracle, borrow, amount, dec_b),
                                    net=net, net_usd=_usd(oracle, borrow, net, dec_b),
                                    buy=router_addrs[i], sell=f"univ3:{fee_sell}",
                                    detail="V2 buy / V3 sell",
                                ))
                    buy = _v3_best_out(quoter, borrow, mid, amount)
                    if buy:
                        mid_out, fee_buy = buy
                        out2 = _v2_out(router, mid_out, [mid, borrow])
                        if out2:
                            premium = (amount * AAVE_FLASH_PREMIUM_BPS) // 10_000
                            net = out2 - amount - premium
                            if net > (amount * min_profit_bps) // 10_000:
                                hits.append(Hit(
                                    chain=chain, kind="v2v3_mixed", borrow=sym_b,
                                    mid=tokens[mid][0], amount=amount,
                                    amount_usd=_usd(oracle, borrow, amount, dec_b),
                                    net=net, net_usd=_usd(oracle, borrow, net, dec_b),
                                    buy=f"univ3:{fee_buy}", sell=router_addrs[i],
                                    detail="V3 buy / V2 sell",
                                ))

    hits.sort(key=lambda h: h.net_usd, reverse=True)
    out_path = Path(f"yield_probe_{chain}.json")
    out_path.write_text(json.dumps([asdict(h) for h in hits[:50]], indent=2), encoding="utf-8")
    print(f"[{chain}] hits={len(hits)}  top5:")
    for h in hits[:5]:
        print(
            f"  +${h.net_usd:,.2f}  {h.kind}  {h.borrow}->{h.mid}->{h.borrow}  "
            f"size=${h.amount_usd:,.0f}  {h.detail}"
        )
    if not hits:
        print(f"[{chain}] no +EV after premium@{AAVE_FLASH_PREMIUM_BPS}bps + min {min_profit_bps}bps")
    return hits


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--chains", default="base,arbitrum")
    p.add_argument("--sizes", default="200,500,1000,2000,5000",
                   help="comma USD notionals to probe")
    p.add_argument("--min-profit-bps", type=int, default=5)
    args = p.parse_args()
    sizes = [float(x) for x in args.sizes.split(",") if x.strip()]
    all_hits: list[Hit] = []
    for chain in [c.strip() for c in args.chains.split(",") if c.strip()]:
        t0 = time.time()
        all_hits.extend(probe_chain(chain, sizes, args.min_profit_bps))
        print(f"[{chain}] done in {time.time() - t0:.1f}s\n")

    print("=" * 60)
    print(f"TOTAL +EV hits: {len(all_hits)}")
    if all_hits:
        best = all_hits[0]
        print(
            f"BEST: +${best.net_usd:,.2f} on {best.chain} {best.kind} "
            f"{best.borrow}/{best.mid} size=${best.amount_usd:,.0f}"
        )
    else:
        print(
            "No flash-repayable DEX edge found. "
            "For ~$3k/mo prioritize flash-loan LIQUIDATIONS, not V2/V3 round-trips."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
