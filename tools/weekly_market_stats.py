"""Weekly market report: Aave liquidations + cross-DEX flash-arb opportunities.

Liquidations come from LiquidationCall logs.
Arb opportunities are sampled by replaying getAmountsOut at historical blocks
(round-trip across two UniswapV2 routers, minus Aave 5bps flash premium).

    python tools/weekly_market_stats.py --chains base,arbitrum --days 7
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from eth_abi import decode as abi_decode
from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aave_bot.abis import ROUTER_QUOTE_ABI  # noqa: E402
from aave_bot.config import load_chain_config  # noqa: E402
from aave_bot.strategies.flash_arb import AAVE_FLASH_PREMIUM_BPS  # noqa: E402
from aave_bot.topics import event_topic  # noqa: E402

LIQUIDATION_CALL = event_topic(
    "LiquidationCall(address,address,address,uint256,uint256,address,bool)"
)
BLOCK_SECONDS = {"base": 2.0, "arbitrum": 0.25, "optimism": 2.0}

# Prefer getLogs-friendly HTTP when .env points at a limited public relay.
HTTP_OVERRIDE = {
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
}

ERC20_META = [
    {"inputs": [], "name": "symbol",
     "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals",
     "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
]
ORACLE_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}], "name": "getAssetPrice",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

# Known liquid V2 venues (verified earlier).
DEFAULT_ROUTERS = {
    "base": [
        "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86",  # BaseSwap
        "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891",  # Sushi
    ],
    "arbitrum": [
        "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",  # Sushi
        "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",  # UniswapV2
    ],
}
DEFAULT_PAIR = {
    "base": (
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
        "0x4200000000000000000000000000000000000006",  # WETH
    ),
    "arbitrum": (
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # native USDC
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
    ),
}
# Probe sizes: 200 USDC (6dp) and 0.1 WETH (18dp)
PROBE_USDC = 200_000000
PROBE_WETH = 10**17


@dataclass
class Liq:
    block: int
    tx: str
    collateral: str
    debt: str
    debt_to_cover: int
    collateral_seized: int
    liquidator: str


@dataclass
class ArbSample:
    block: int
    direction: str
    buy: str
    sell: str
    amount_in: int
    amount_back: int
    premium: int
    net: int


@dataclass
class Report:
    chain: str
    days: float
    liquidations: list[Liq] = field(default_factory=list)
    debt_usd: float = 0.0
    arb_samples: list[ArbSample] = field(default_factory=list)
    arb_errors: int = 0


def http_for(chain: str, configured: str) -> str:
    return HTTP_OVERRIDE.get(chain, configured)


def fetch_logs(w3: Web3, pool: str, start: int, end: int, chunk: int) -> list[dict]:
    logs: list[dict] = []
    cur = start
    while cur <= end:
        to = min(cur + chunk - 1, end)
        ok = False
        for attempt in range(5):
            try:
                logs.extend(w3.eth.get_logs({
                    "address": pool,
                    "topics": [LIQUIDATION_CALL],
                    "fromBlock": cur,
                    "toBlock": to,
                }))
                ok = True
                break
            except Exception as exc:
                time.sleep(0.4 * (attempt + 1))
                to = min(cur + max(250, (to - cur) // 2), end)
                if attempt == 4:
                    print(f"\n  ! logs {cur}-{to}: {str(exc)[:90]}")
        cur = to + 1
        print(f"\r  liquidations scan {cur}/{end}  n={len(logs)}   ", end="", flush=True)
    print()
    return logs


def topic_address(topic) -> str:
    raw = topic.hex() if hasattr(topic, "hex") else str(topic)
    raw = raw[2:] if raw.startswith("0x") else raw
    return Web3.to_checksum_address("0x" + raw[-40:])


def decode_liq(entry: dict) -> Liq | None:
    try:
        topics = entry["topics"]
        data = entry["data"]
        raw = bytes(data) if not isinstance(data, str) else bytes.fromhex(data[2:])
        debt_to_cover, seized, liquidator, _ = abi_decode(
            ["uint256", "uint256", "address", "bool"], raw
        )
        tx = entry["transactionHash"]
        tx_hex = tx.hex() if hasattr(tx, "hex") else str(tx)
        if not tx_hex.startswith("0x"):
            tx_hex = "0x" + tx_hex
        return Liq(
            block=int(entry["blockNumber"]),
            tx=tx_hex,
            collateral=topic_address(topics[1]),
            debt=topic_address(topics[2]),
            debt_to_cover=int(debt_to_cover),
            collateral_seized=int(seized),
            liquidator=Web3.to_checksum_address(liquidator),
        )
    except Exception:
        return None


class Meta:
    def __init__(self, w3: Web3, oracle: str):
        self.w3 = w3
        self.oracle = w3.eth.contract(address=oracle, abi=ORACLE_ABI)
        self._s: dict[str, str] = {}
        self._d: dict[str, int] = {}
        self._p: dict[str, int] = {}

    def symbol(self, a: str) -> str:
        if a not in self._s:
            try:
                self._s[a] = self.w3.eth.contract(address=a, abi=ERC20_META).functions.symbol().call()
            except Exception:
                self._s[a] = a[:10]
        return self._s[a]

    def decimals(self, a: str) -> int:
        if a not in self._d:
            try:
                self._d[a] = int(
                    self.w3.eth.contract(address=a, abi=ERC20_META).functions.decimals().call()
                )
            except Exception:
                self._d[a] = 18
        return self._d[a]

    def usd(self, asset: str, amount: int) -> float:
        if asset not in self._p:
            try:
                self._p[asset] = int(self.oracle.functions.getAssetPrice(asset).call())
            except Exception:
                self._p[asset] = -1
        if self._p[asset] < 0 or amount == 0:
            return 0.0
        return amount * self._p[asset] / (10 ** self.decimals(asset)) / 1e8


def quote_roundtrip(router_buy, router_sell, token_a, token_b, amount, block) -> int | None:
    """Return amount of token_a back after A->B on buy and B->A on sell, or None."""
    try:
        mid = router_buy.functions.getAmountsOut(amount, [token_a, token_b]).call(
            block_identifier=block
        )[-1]
        back = router_sell.functions.getAmountsOut(mid, [token_b, token_a]).call(
            block_identifier=block
        )[-1]
        return int(back)
    except Exception:
        return None


def sample_arb(w3: Web3, chain: str, start: int, head: int, samples: int) -> tuple[list[ArbSample], int]:
    routers = [Web3.to_checksum_address(a) for a in DEFAULT_ROUTERS[chain]]
    usdc, weth = (Web3.to_checksum_address(a) for a in DEFAULT_PAIR[chain])
    r0 = w3.eth.contract(address=routers[0], abi=ROUTER_QUOTE_ABI)
    r1 = w3.eth.contract(address=routers[1], abi=ROUTER_QUOTE_ABI)

    span = max(1, head - start)
    step = max(1, span // samples)
    blocks = list(range(start, head, step))[:samples]
    out: list[ArbSample] = []
    errors = 0

    directions = [
        ("USDC->WETH->USDC", usdc, weth, PROBE_USDC),
        ("WETH->USDC->WETH", weth, usdc, PROBE_WETH),
    ]
    routes = [(r0, r1, routers[0], routers[1]), (r1, r0, routers[1], routers[0])]

    for i, block in enumerate(blocks):
        print(f"\r  arb sample {i+1}/{len(blocks)} block={block}   ", end="", flush=True)
        for label, a, b, amount in directions:
            for buy, sell, buy_addr, sell_addr in routes:
                back = quote_roundtrip(buy, sell, a, b, amount, block)
                if back is None:
                    errors += 1
                    continue
                premium = (amount * AAVE_FLASH_PREMIUM_BPS) // 10_000
                out.append(ArbSample(
                    block=block,
                    direction=label,
                    buy=buy_addr,
                    sell=sell_addr,
                    amount_in=amount,
                    amount_back=back,
                    premium=premium,
                    net=back - amount - premium,
                ))
        time.sleep(0.15)
    print()
    return out, errors


def build_report(chain: str, days: float, chunk: int, arb_samples: int) -> Report:
    config = load_chain_config(chain)
    http = http_for(chain, config.http_rpc_url)
    w3 = Web3(Web3.HTTPProvider(http, request_kwargs={"timeout": 45}))
    head = w3.eth.block_number
    span = int(days * 24 * 3600 / BLOCK_SECONDS[chain])
    start = max(0, head - span)

    print(f"\n=== {chain.upper()} ===  blocks {start}..{head} (~{days:g}d)  rpc={http}")
    report = Report(chain=chain, days=days)

    print("liquidations:")
    raw = fetch_logs(w3, config.pool, start, head, chunk)
    meta = Meta(w3, config.oracle)
    for entry in raw:
        liq = decode_liq(entry)
        if liq is None:
            continue
        report.liquidations.append(liq)
        report.debt_usd += meta.usd(liq.debt, liq.debt_to_cover)
    report._meta = meta  # type: ignore[attr-defined]

    print("flash-arb historical quotes:")
    samples, errors = sample_arb(w3, chain, start, head, arb_samples)
    report.arb_samples = samples
    report.arb_errors = errors
    return report


def print_liquidation_section(r: Report) -> None:
    n = len(r.liquidations)
    print(f"\n--- {r.chain} LIQUIDATIONS ({r.days:g}d) ---")
    print(f"deals:                 {n}  (~{n / r.days:.2f}/day)")
    print(f"debt covered (USD):    ${r.debt_usd:,.0f}")
    if not n:
        print("competition:           none")
        print("our capture estimate:  ~0/week")
        return

    meta: Meta = r._meta  # type: ignore[attr-defined]
    actors = Counter(l.liquidator for l in r.liquidations)
    top_addr, top_n = actors.most_common(1)[0]
    print(f"unique liquidators:    {len(actors)}")
    print(f"top share:             {top_n / n:.0%} ({top_n}/{n}) {top_addr}")

    by_block_actors: dict[int, set[str]] = defaultdict(set)
    by_block_count: Counter[int] = Counter()
    for l in r.liquidations:
        by_block_actors[l.block].add(l.liquidator)
        by_block_count[l.block] += 1
    contested = sum(1 for s in by_block_actors.values() if len(s) > 1)
    print(f"multi-bot race blocks: {contested}")
    print("leaderboard:")
    for addr, count in actors.most_common(6):
        usd = sum(meta.usd(l.debt, l.debt_to_cover) for l in r.liquidations if l.liquidator == addr)
        print(f"  {count:4d}  {count/n:5.1%}  ${usd:10,.0f}  {addr}")

    pairs = Counter((meta.symbol(l.collateral), meta.symbol(l.debt)) for l in r.liquidations)
    print("top pairs:")
    for (c, d), count in pairs.most_common(5):
        print(f"  {count:4d}  {c}/{d}")

    share = top_n / n
    if share >= 0.85 and len(actors) <= 3:
        low, high = 0, max(1, int(n * 0.05))
        note = "dominated market; public-RPC bot ≈ scraps"
    elif share >= 0.5:
        low, high = max(1, int(n * 0.05)), max(1, int(n * 0.15))
        note = "one leader; with good RPC/bot maybe low-teens %"
    else:
        low, high = max(1, int(n * 0.10)), max(1, int(n * 0.25))
        note = "fragmented; room for a fast entrant"
    print(f"our realistic capture: ~{low}-{high} deals/week  ({note})")
    print(f"  notional touch:      ~${r.debt_usd * low / n:,.0f}-${r.debt_usd * high / n:,.0f}")
    print("  (profit is a small % of notional after bonus/gas/slip)")


def print_arb_section(r: Report) -> None:
    print(f"\n--- {r.chain} FLASH ARB ({r.days:g}d historical samples) ---")
    total = len(r.arb_samples)
    print(f"quotes attempted:      {total} successful, {r.arb_errors} failed")
    if not total:
        print("no usable historical quotes (RPC may not support eth_call at old blocks)")
        return

    profitable = [s for s in r.arb_samples if s.net > 0]
    # Unique sample blocks where ANY direction/route was green.
    green_blocks = {s.block for s in profitable}
    all_blocks = {s.block for s in r.arb_samples}
    hit_rate = len(green_blocks) / len(all_blocks) if all_blocks else 0

    print(f"sample blocks:         {len(all_blocks)}")
    print(f"blocks with +EV arb:   {len(green_blocks)}  ({hit_rate:.1%} of samples)")
    print(f"profitable route hits: {len(profitable)} / {total} quotes")

    if profitable:
        # Net is in token units of the borrowed asset.
        by_dir = defaultdict(list)
        for s in profitable:
            by_dir[s.direction].append(s)
        print("best profitable hits:")
        for s in sorted(profitable, key=lambda x: x.net, reverse=True)[:5]:
            print(
                f"  block {s.block}  {s.direction}  "
                f"in={s.amount_in} back={s.amount_back} net=+{s.net}  "
                f"buy={s.buy[:10]} sell={s.sell[:10]}"
            )
    else:
        # Show how negative the best (least bad) was — proves scanner works.
        best = max(r.arb_samples, key=lambda s: s.net)
        print(
            f"best (still loss):     {best.direction} net={best.net} "
            f"at block {best.block}  (fees+premium ate the spread)"
        )

    # Extrapolate: if hit_rate of random blocks had arb, weekly "windows".
    # With a 30s scanner we'd revisit often; countable "opportunities" ≈
    # green_block_fraction * (week seconds / reprice interval). Use 5 min as
    # a conservative reprice cadence for V2 pools.
    windows = int(hit_rate * (r.days * 24 * 60 / 5))
    print(f"extrapolated +EV windows/week (@5min cadence): ~{windows}")
    if hit_rate == 0:
        print("our realistic arb capture: ~0 executed arbs/week on these V2 venues")
        print("  (spreads rarely clear 5bps premium + 2x V2 fees; need V3/agg or CEX-DEX)")
    else:
        # Capturing 10-30% of windows if we are always watching with private tx.
        low, high = max(1, int(windows * 0.1)), max(1, int(windows * 0.3))
        print(f"our realistic arb capture: ~{low}-{high} fills/week if always-on + fast relay")
        print("  (size limited by pool depth; many windows are tiny $)")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--chains", default="base,arbitrum")
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--chunk", type=int, default=10_000)
    parser.add_argument("--arb-samples", type=int, default=24,
                        help="historical blocks to probe for arb (per chain)")
    args = parser.parse_args()

    chains = [c.strip().lower() for c in args.chains.split(",") if c.strip()]
    reports = [
        build_report(c, args.days, args.chunk, args.arb_samples) for c in chains
    ]

    print("\n" + "=" * 64)
    print(f"WEEKLY MARKET REPORT — liquidations + flash arb  ({args.days:g}d)")
    print("=" * 64)
    for r in reports:
        print_liquidation_section(r)
        print_arb_section(r)

    print("\n=== COMBINED ===")
    print(f"liquidations: {sum(len(r.liquidations) for r in reports)}  "
          f"(${sum(r.debt_usd for r in reports):,.0f} debt covered)")
    green = sum(len({s.block for s in r.arb_samples if s.net > 0}) for r in reports)
    blocks = sum(len({s.block for s in r.arb_samples}) for r in reports)
    print(f"arb green sample-blocks: {green}/{blocks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
