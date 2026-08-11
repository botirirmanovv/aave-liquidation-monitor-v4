"""Weekly Aave V3 liquidation market report for one or more chains.

    python tools/weekly_liquidation_stats.py --chains base,arbitrum --days 7
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

from aave_bot.config import load_chain_config  # noqa: E402
from aave_bot.topics import event_topic  # noqa: E402

LIQUIDATION_CALL = event_topic(
    "LiquidationCall(address,address,address,uint256,uint256,address,bool)"
)

BLOCK_SECONDS = {"base": 2.0, "arbitrum": 0.25, "optimism": 2.0, "ethereum": 12.0}

# Enough ABI to resolve symbols + decimals for volume formatting.
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


@dataclass
class Liq:
    block: int
    tx: str
    collateral: str
    debt: str
    user: str
    debt_to_cover: int
    collateral_seized: int
    liquidator: str


@dataclass
class ChainReport:
    chain: str
    hours: float
    liquidations: list[Liq] = field(default_factory=list)
    debt_usd: float = 0.0
    collateral_usd: float = 0.0
    decode_failures: int = 0


def fetch_logs(w3: Web3, pool: str, start: int, end: int, chunk: int) -> list[dict]:
    logs: list[dict] = []
    cur = start
    while cur <= end:
        to = min(cur + chunk - 1, end)
        for attempt in range(4):
            try:
                batch = w3.eth.get_logs({
                    "address": pool,
                    "topics": [LIQUIDATION_CALL],
                    "fromBlock": cur,
                    "toBlock": to,
                })
                logs.extend(batch)
                break
            except Exception as exc:
                if attempt == 3:
                    print(f"\n  ! {cur}-{to} failed: {str(exc)[:100]}")
                else:
                    time.sleep(0.5 * (attempt + 1))
                    # Shrink chunk on rate limits.
                    to = min(cur + max(200, (to - cur) // 2), end)
        cur = to + 1
        print(f"\r  [{start}..{end}] at {to}  found={len(logs)}   ", end="", flush=True)
    print()
    return logs


def topic_address(topic) -> str:
    raw = topic.hex() if hasattr(topic, "hex") else str(topic)
    raw = raw[2:] if raw.startswith("0x") else raw
    return Web3.to_checksum_address("0x" + raw[-40:])


def decode_log(entry: dict) -> Liq | None:
    try:
        topics = entry["topics"]
        collateral = topic_address(topics[1])
        debt = topic_address(topics[2])
        user = topic_address(topics[3])
        data = entry["data"]
        raw = bytes(data) if not isinstance(data, str) else bytes.fromhex(data[2:])
        debt_to_cover, seized, liquidator, _ = abi_decode(
            ["uint256", "uint256", "address", "bool"], raw
        )
        tx = entry["transactionHash"]
        tx_hex = tx.hex() if hasattr(tx, "hex") else str(tx)
        return Liq(
            block=int(entry["blockNumber"]),
            tx=tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex,
            collateral=collateral,
            debt=debt,
            user=user,
            debt_to_cover=int(debt_to_cover),
            collateral_seized=int(seized),
            liquidator=Web3.to_checksum_address(liquidator),
        )
    except Exception:
        return None


class MetaCache:
    def __init__(self, w3: Web3, oracle_address: str):
        self.w3 = w3
        self.oracle = w3.eth.contract(address=oracle_address, abi=ORACLE_ABI)
        self._sym: dict[str, str] = {}
        self._dec: dict[str, int] = {}
        self._price: dict[str, int] = {}

    def symbol(self, asset: str) -> str:
        if asset not in self._sym:
            try:
                c = self.w3.eth.contract(address=asset, abi=ERC20_META)
                self._sym[asset] = c.functions.symbol().call()
            except Exception:
                self._sym[asset] = asset[:10]
        return self._sym[asset]

    def decimals(self, asset: str) -> int:
        if asset not in self._dec:
            try:
                c = self.w3.eth.contract(address=asset, abi=ERC20_META)
                self._dec[asset] = int(c.functions.decimals().call())
            except Exception:
                self._dec[asset] = 18
        return self._dec[asset]

    def price_usd_8(self, asset: str) -> int | None:
        """Aave oracle price, USD with 8 decimals."""
        if asset not in self._price:
            try:
                self._price[asset] = int(self.oracle.functions.getAssetPrice(asset).call())
            except Exception:
                self._price[asset] = -1
        return None if self._price[asset] < 0 else self._price[asset]

    def usd(self, asset: str, amount: int) -> float:
        price = self.price_usd_8(asset)
        if price is None or amount == 0:
            return 0.0
        return amount * price / (10 ** self.decimals(asset)) / 1e8


def analyze(chain: str, days: float, chunk: int) -> ChainReport:
    config = load_chain_config(chain)
    # Prefer HTTP endpoints that allow eth_getLogs.
    http = config.http_rpc_url
    if "drpc.org" in http and chain == "base":
        http = "https://mainnet.base.org"
    w3 = Web3(Web3.HTTPProvider(http, request_kwargs={"timeout": 45}))
    head = w3.eth.block_number
    hours = days * 24
    span = int(hours * 3600 / BLOCK_SECONDS.get(chain, 2.0))
    start = max(0, head - span)

    print(f"\n=== {chain.upper()} ===")
    print(f"blocks {start}..{head}  (~{days:g}d)  pool={config.pool}")
    raw_logs = fetch_logs(w3, config.pool, start, head, chunk)

    report = ChainReport(chain=chain, hours=hours)
    meta = MetaCache(w3, config.oracle)
    for entry in raw_logs:
        liq = decode_log(entry)
        if liq is None:
            report.decode_failures += 1
            continue
        report.liquidations.append(liq)
        report.debt_usd += meta.usd(liq.debt, liq.debt_to_cover)
        report.collateral_usd += meta.usd(liq.collateral, liq.collateral_seized)

    report._meta = meta  # type: ignore[attr-defined]
    return report


def print_report(report: ChainReport) -> None:
    n = len(report.liquidations)
    days = report.hours / 24
    print(f"\n--- {report.chain} summary ({days:g} days) ---")
    print(f"liquidations:          {n}")
    print(f"per day:               {n / days:.2f}")
    print(f"debt covered (USD):    ${report.debt_usd:,.0f}")
    print(f"collateral seized USD: ${report.collateral_usd:,.0f}")
    if n:
        print(f"avg debt / deal:       ${report.debt_usd / n:,.0f}")

    if not report.liquidations:
        print("competition:            n/a (no deals)")
        print("our realistic capture:  ~0 deals/week at current flow")
        return

    meta: MetaCache = report._meta  # type: ignore[attr-defined]
    liquidators = Counter(l.liquidator for l in report.liquidations)
    unique = len(liquidators)
    top_addr, top_n = liquidators.most_common(1)[0]
    top_share = top_n / n

    # Same-block multi-liquidator = real race; single liquidator dominating = latency war.
    by_block: dict[int, set[str]] = defaultdict(set)
    for l in report.liquidations:
        by_block[l.block].add(l.liquidator)
    contested = sum(1 for actors in by_block.values() if len(actors) > 1)
    multi_in_block = sum(1 for b, actors in by_block.items() if len(actors) >= 1 and
                         sum(1 for l in report.liquidations if l.block == b) > 1)

    print(f"unique liquidators:    {unique}")
    print(f"top liquidator share:  {top_share:.0%}  ({top_n}/{n})  {top_addr}")
    print(f"blocks with >1 liq:    {sum(1 for b, ls in Counter(l.block for l in report.liquidations).items() if ls > 1)}")
    print(f"blocks with >1 actor:  {contested}  (true multi-bot races)")

    print("leaderboard:")
    for addr, count in liquidators.most_common(8):
        share = count / n
        # Rough USD attributed to this liquidator
        usd = sum(
            meta.usd(l.debt, l.debt_to_cover)
            for l in report.liquidations if l.liquidator == addr
        )
        print(f"  {count:4d}  {share:5.1%}  ${usd:10,.0f}  {addr}")

    # Pair frequency
    pairs = Counter(
        (meta.symbol(l.collateral), meta.symbol(l.debt)) for l in report.liquidations
    )
    print("top collateral/debt pairs:")
    for (col, debt), count in pairs.most_common(5):
        print(f"  {count:4d}  {col}/{debt}")

    # Capacity estimate for US — honest, based on competition structure.
    print("\nour realistic weekly capture (order-of-magnitude):")
    if top_share >= 0.85 and unique <= 3:
        # Monopoly / oligopoly — without private relay + better latency we get scraps.
        low, high = 0, max(1, int(n * 0.05))
        note = "market dominated by 1-2 bots; public RPC dry-run bot catches near-zero"
    elif top_share >= 0.5:
        low, high = max(1, int(n * 0.05)), max(1, int(n * 0.15))
        note = "one leader + fringe; with deployed bot + decent RPC maybe low-teens %"
    else:
        low, high = max(1, int(n * 0.10)), max(1, int(n * 0.25))
        note = "fragmented competition; room for a new fast entrant"
    usd_low = report.debt_usd * (low / n) if n else 0
    usd_high = report.debt_usd * (high / n) if n else 0
    print(f"  deals:  ~{low}-{high} of {n}  ({note})")
    print(f"  debt notional we might touch: ~${usd_low:,.0f}-${usd_high:,.0f}")
    print("  (profit << notional: liquidation bonus ~1-5% minus gas, swap slip, competition)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chains", default="base,arbitrum")
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--chunk", type=int, default=8_000)
    args = parser.parse_args()

    chains = [c.strip().lower() for c in args.chains.split(",") if c.strip()]
    reports = [analyze(c, args.days, args.chunk) for c in chains]

    print("\n" + "=" * 64)
    print(f"WEEKLY AAVE V3 LIQUIDATION REPORT  ({args.days:g} days)")
    print("=" * 64)
    for r in reports:
        print_report(r)

    if len(reports) >= 2:
        total_n = sum(len(r.liquidations) for r in reports)
        total_usd = sum(r.debt_usd for r in reports)
        print("\n=== COMBINED ===")
        print(f"total liquidations: {total_n}")
        print(f"total debt covered: ${total_usd:,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
