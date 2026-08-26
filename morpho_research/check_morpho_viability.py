#!/usr/bin/env python3
"""Morpho Blue viability scout (standalone, read-only).

Does NOT touch monitor_v4 / contracts / live pipeline. Uses Morpho GraphQL first,
falls back to Morpho Blue CreateMarket/Liquidate logs via project RPC (.env).

Usage (from repo root):
    .venv-run/Scripts/python.exe morpho_research/check_morpho_viability.py
    .venv-run/Scripts/python.exe morpho_research/check_morpho_viability.py --chains base,arbitrum
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allow `python morpho_research/check_morpho_viability.py` from repo root.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from web3 import Web3  # noqa: E402

from aave_bot import config as bot_config  # noqa: E402  (loads .env)

LOG = logging.getLogger("morpho_research")

MORPHO_GRAPHQL_URLS = (
    "https://blue-api.morpho.org/graphql",
    "https://api.morpho.org/graphql",
)

# Canonical Morpho Blue CREATE2 address (same on Arb / Base / OP).
MORPHO_BLUE = Web3.to_checksum_address("0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb")

CHAIN_IDS = {
    "arbitrum": 42161,
    "base": 8453,
    "optimism": 10,
}

# Approximate seconds/block for log-range estimation (fallback only).
SEC_PER_BLOCK = {
    "arbitrum": 0.25,
    "base": 2.0,
    "optimism": 2.0,
}

WAD = 10**18
LIF_CURSOR = 0.3
LIF_MAX = 1.15

CREATE_MARKET_TOPIC = Web3.keccak(
    text="CreateMarket((address,address,address,address,uint256))"
).hex()
# Some deployments index Id; topic0 is the event signature either way.
CREATE_MARKET_TOPIC_ALT = Web3.keccak(
    text="CreateMarket(bytes32,(address,address,address,address,uint256))"
).hex()
LIQUIDATE_TOPIC = Web3.keccak(
    text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
).hex()

MORPHO_BLUE_MIN_ABI = [
    {
        "inputs": [{"internalType": "bytes32", "name": "id", "type": "bytes32"}],
        "name": "idToMarketParams",
        "outputs": [
            {"internalType": "address", "name": "loanToken", "type": "address"},
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "address", "name": "oracle", "type": "address"},
            {"internalType": "address", "name": "irm", "type": "address"},
            {"internalType": "uint256", "name": "lltv", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "bytes32", "name": "id", "type": "bytes32"}],
        "name": "market",
        "outputs": [
            {"internalType": "uint128", "name": "totalSupplyAssets", "type": "uint128"},
            {"internalType": "uint128", "name": "totalSupplyShares", "type": "uint128"},
            {"internalType": "uint128", "name": "totalBorrowAssets", "type": "uint128"},
            {"internalType": "uint128", "name": "totalBorrowShares", "type": "uint128"},
            {"internalType": "uint128", "name": "lastUpdate", "type": "uint128"},
            {"internalType": "uint128", "name": "fee", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_MIN_ABI = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class LiquidationRow:
    market_id: str
    loan_symbol: str
    coll_symbol: str
    repaid_usd: float
    seized_usd: float
    est_incentive_usd: float
    tx_hash: str
    timestamp: int


@dataclass
class MarketRow:
    market_id: str
    loan_symbol: str
    coll_symbol: str
    lltv: float
    supply_assets: int
    borrow_assets: int
    supply_usd: float
    borrow_usd: float
    borrowers: int = 0
    near_liq: int = 0
    liq_30d: int = 0
    liq_repaid_usd_30d: float = 0.0
    liq_est_profit_usd_30d: float = 0.0
    source: str = "api"


@dataclass
class ChainReport:
    chain: str
    chain_id: int
    markets: list[MarketRow] = field(default_factory=list)
    liquidations: list[LiquidationRow] = field(default_factory=list)
    api_ok: bool = False
    notes: list[str] = field(default_factory=list)


def _graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    body = json.dumps(payload).encode()
    last_err: Exception | None = None
    for url in MORPHO_GRAPHQL_URLS:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            if data.get("errors"):
                raise RuntimeError(str(data["errors"][:2]))
            return data["data"]
        except Exception as exc:  # noqa: BLE001 — probe both endpoints
            last_err = exc
            LOG.debug("GraphQL %s failed: %s", url, exc)
    raise RuntimeError(f"Morpho GraphQL unavailable: {last_err}")


def liquidation_incentive_factor(lltv_wad: int | float) -> float:
    """Morpho Blue LIF ≈ min(1.15, 1 / (0.3*LLTV + 0.7))."""
    lltv = float(lltv_wad) / WAD if float(lltv_wad) > 2 else float(lltv_wad)
    if lltv <= 0:
        return 1.0
    raw = 1.0 / (LIF_CURSOR * lltv + (1.0 - LIF_CURSOR))
    return min(LIF_MAX, raw)


def health_factor(collateral_usd: float, borrow_usd: float, lltv: float) -> float | None:
    if borrow_usd <= 0 or collateral_usd <= 0 or lltv <= 0:
        return None
    return (collateral_usd * lltv) / borrow_usd


def _usd_from_amount(amount: int, decimals: int, price_usd: float) -> float:
    if decimals < 0 or price_usd <= 0:
        return 0.0
    return (amount / (10**decimals)) * price_usd


def fetch_markets_api(chain_id: int, page_size: int = 100) -> list[dict[str, Any]]:
    query = """
    query($chainId: Int!, $first: Int!, $skip: Int!) {
      markets(
        first: $first
        skip: $skip
        orderBy: SupplyAssetsUsd
        orderDirection: Desc
        where: { chainId_in: [$chainId], listed: true }
      ) {
        items {
          marketId
          lltv
          loanAsset { symbol decimals priceUsd }
          collateralAsset { symbol decimals priceUsd }
          state {
            supplyAssets
            borrowAssets
            supplyAssetsUsd
            borrowAssetsUsd
          }
        }
        pageInfo { count }
      }
    }
    """
    out: list[dict[str, Any]] = []
    skip = 0
    while True:
        data = _graphql(
            query, {"chainId": chain_id, "first": page_size, "skip": skip}
        )
        items = (data.get("markets") or {}).get("items") or []
        if not items:
            break
        out.extend(items)
        if len(items) < page_size:
            break
        skip += page_size
        if skip > 5000:
            break
    return out


def fetch_liquidations_api(
    chain_id: int, since_ts: int, page_size: int = 100
) -> list[dict[str, Any]]:
    query = """
    query($chainId: Int!, $ts: Int!, $first: Int!, $skip: Int!) {
      marketTransactions(
        first: $first
        skip: $skip
        orderBy: Timestamp
        orderDirection: Desc
        where: {
          chainId_in: [$chainId]
          type_in: [Liquidation]
          timestamp_gte: $ts
        }
      ) {
        items {
          txHash
          timestamp
          data {
            ... on MarketTransactionLiquidationData {
              seizedAssets
              repaidAssets
              badDebtAssets
            }
          }
          market {
            marketId
            lltv
            loanAsset { symbol decimals priceUsd }
            collateralAsset { symbol decimals priceUsd }
          }
        }
        pageInfo { count }
      }
    }
    """
    out: list[dict[str, Any]] = []
    skip = 0
    while True:
        data = _graphql(
            query,
            {
                "chainId": chain_id,
                "ts": since_ts,
                "first": page_size,
                "skip": skip,
            },
        )
        items = (data.get("marketTransactions") or {}).get("items") or []
        if not items:
            break
        out.extend(items)
        if len(items) < page_size:
            break
        skip += page_size
        if skip > 10000:
            break
    return out


def count_borrowers_and_near(
    market_id: str,
    chain_id: int,
    lltv: float,
    *,
    near_hf: float = 1.05,
    page_size: int = 200,
    max_pages: int = 25,
) -> tuple[int, int]:
    """Return (active_borrowers, positions_with_HF < near_hf)."""
    query = """
    query($market: String!, $chainId: Int!, $first: Int!, $skip: Int!) {
      marketPositions(
        first: $first
        skip: $skip
        orderBy: BorrowShares
        orderDirection: Desc
        where: {
          marketUniqueKey_in: [$market]
          chainId_in: [$chainId]
          borrowShares_gte: "1"
        }
      ) {
        items {
          state { borrowAssetsUsd collateralUsd }
        }
        pageInfo { count }
      }
    }
    """
    borrowers = 0
    near = 0
    skip = 0
    for _ in range(max_pages):
        data = _graphql(
            query,
            {
                "market": market_id,
                "chainId": chain_id,
                "first": page_size,
                "skip": skip,
            },
        )
        items = (data.get("marketPositions") or {}).get("items") or []
        if not items:
            break
        for item in items:
            st = item.get("state") or {}
            borrow_usd = float(st.get("borrowAssetsUsd") or 0)
            coll_usd = float(st.get("collateralUsd") or 0)
            if borrow_usd <= 0:
                continue
            borrowers += 1
            hf = health_factor(coll_usd, borrow_usd, lltv)
            if hf is not None and hf < near_hf:
                near += 1
        if len(items) < page_size:
            break
        skip += page_size
    return borrowers, near


def http_rpc(chain: str) -> str:
    urls = bot_config.http_rpc_urls(chain)
    if urls:
        return urls[0]
    return bot_config._text("HTTP_RPC_URL", chain, required=True, inherit=False)


def connect_w3(chain: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(http_rpc(chain), request_kwargs={"timeout": 45}))
    if not w3.is_connected():
        raise RuntimeError(f"{chain}: RPC not connected")
    return w3


def _chunked_logs(
    w3: Web3,
    *,
    address: str,
    topics: list[Any],
    from_block: int,
    to_block: int,
    chunk: int = 40_000,
) -> list[Any]:
    logs: list[Any] = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        try:
            batch = w3.eth.get_logs(
                {
                    "fromBlock": start,
                    "toBlock": end,
                    "address": address,
                    "topics": topics,
                }
            )
            logs.extend(batch)
        except Exception as exc:  # noqa: BLE001
            if chunk <= 2_000:
                LOG.warning("getLogs failed %s..%s: %s", start, end, exc)
                start = end + 1
                continue
            # shrink and retry this window
            mid = (start + end) // 2
            logs.extend(
                _chunked_logs(
                    w3,
                    address=address,
                    topics=topics,
                    from_block=start,
                    to_block=mid,
                    chunk=max(2_000, chunk // 2),
                )
            )
            logs.extend(
                _chunked_logs(
                    w3,
                    address=address,
                    topics=topics,
                    from_block=mid + 1,
                    to_block=end,
                    chunk=max(2_000, chunk // 2),
                )
            )
        start = end + 1
    return logs


def estimate_from_block(w3: Web3, chain: str, days: float) -> int:
    head = w3.eth.block_number
    span = int((days * 86400) / SEC_PER_BLOCK.get(chain, 2.0))
    return max(0, head - span)


def fallback_markets_from_logs(chain: str, days: float = 90.0) -> list[MarketRow]:
    w3 = connect_w3(chain)
    morpho = w3.eth.contract(address=MORPHO_BLUE, abi=MORPHO_BLUE_MIN_ABI)
    head = w3.eth.block_number
    start = estimate_from_block(w3, chain, days)
    LOG.info("[%s] fallback CreateMarket logs %s..%s", chain, start, head)

    logs = _chunked_logs(
        w3,
        address=MORPHO_BLUE,
        topics=[[CREATE_MARKET_TOPIC, CREATE_MARKET_TOPIC_ALT]],
        from_block=start,
        to_block=head,
    )
    market_ids: list[str] = []
    for lg in logs:
        if len(lg["topics"]) >= 2:
            topic = lg["topics"][1]
            market_ids.append(topic.hex() if hasattr(topic, "hex") else str(topic))
    market_ids = list(dict.fromkeys(market_ids))
    if not market_ids:
        return []

    rows: list[MarketRow] = []
    for mid in market_ids:
        mid_hex = mid if mid.startswith("0x") else "0x" + mid
        mid_bytes = bytes.fromhex(mid_hex[2:])
        try:
            params = morpho.functions.idToMarketParams(mid_bytes).call()
            mkt = morpho.functions.market(mid_bytes).call()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("skip market %s: %s", mid_hex[:18], exc)
            continue
        loan, coll, _oracle, _irm, lltv = params
        supply_assets, _ss, borrow_assets, _bs, _lu, _fee = mkt
        coll_int = int(coll, 16) if isinstance(coll, str) else int(coll)
        rows.append(
            MarketRow(
                market_id=mid_hex,
                loan_symbol=_safe_symbol(w3, loan),
                coll_symbol=_safe_symbol(w3, coll) if coll_int != 0 else "—",
                lltv=float(lltv) / WAD,
                supply_assets=int(supply_assets),
                borrow_assets=int(borrow_assets),
                supply_usd=0.0,
                borrow_usd=0.0,
                source="logs",
            )
        )
    return rows


def _safe_symbol(w3: Web3, token: str) -> str:
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_MIN_ABI)
        return str(c.functions.symbol().call())
    except Exception:
        return token[:8]


def fallback_liquidations_from_logs(
    chain: str, days: float = 30.0
) -> list[LiquidationRow]:
    w3 = connect_w3(chain)
    head = w3.eth.block_number
    start = estimate_from_block(w3, chain, days)
    LOG.info("[%s] fallback Liquidate logs %s..%s", chain, start, head)
    logs = _chunked_logs(
        w3,
        address=MORPHO_BLUE,
        topics=[LIQUIDATE_TOPIC],
        from_block=start,
        to_block=head,
        chunk=20_000,
    )
    rows: list[LiquidationRow] = []
    for lg in logs:
        mid = lg["topics"][1].hex() if len(lg["topics"]) > 1 else ""
        # Non-indexed amounts start after the three indexed topics in data.
        data = lg["data"]
        if hasattr(data, "hex"):
            data_hex = data.hex()
        else:
            data_hex = data if isinstance(data, str) else bytes(data).hex()
        data_hex = data_hex[2:] if data_hex.startswith("0x") else data_hex
        # repaidAssets, repaidShares, seizedAssets, badDebtAssets, badDebtShares
        if len(data_hex) < 64 * 5:
            continue
        repaid = int(data_hex[0:64], 16)
        seized = int(data_hex[128:192], 16)
        rows.append(
            LiquidationRow(
                market_id=mid if mid.startswith("0x") else "0x" + mid,
                loan_symbol="?",
                coll_symbol="?",
                repaid_usd=0.0,  # unknown without prices in pure-log mode
                seized_usd=0.0,
                est_incentive_usd=0.0,
                tx_hash=lg["transactionHash"].hex()
                if hasattr(lg["transactionHash"], "hex")
                else str(lg["transactionHash"]),
                timestamp=0,
            )
        )
        # keep raw sizes in repaid_usd field as sentinel? better store note via symbols
        rows[-1].repaid_usd = float(repaid)  # raw units placeholder when USD unknown
        rows[-1].seized_usd = float(seized)
    return rows


def parse_markets(items: list[dict[str, Any]]) -> list[MarketRow]:
    rows: list[MarketRow] = []
    for item in items:
        loan = item.get("loanAsset") or {}
        coll = item.get("collateralAsset") or {}
        state = item.get("state") or {}
        lltv_raw = item.get("lltv") or 0
        lltv = float(lltv_raw) / WAD if float(lltv_raw) > 2 else float(lltv_raw)
        if not coll.get("symbol") and lltv <= 0:
            # idle/idle-like market without collateral — skip for liq research
            continue
        rows.append(
            MarketRow(
                market_id=item["marketId"],
                loan_symbol=str(loan.get("symbol") or "?"),
                coll_symbol=str(coll.get("symbol") or "—"),
                lltv=lltv,
                supply_assets=int(state.get("supplyAssets") or 0),
                borrow_assets=int(state.get("borrowAssets") or 0),
                supply_usd=float(state.get("supplyAssetsUsd") or 0),
                borrow_usd=float(state.get("borrowAssetsUsd") or 0),
                source="api",
            )
        )
    return rows


def parse_liquidations(items: list[dict[str, Any]]) -> list[LiquidationRow]:
    rows: list[LiquidationRow] = []
    for item in items:
        mkt = item.get("market") or {}
        data = item.get("data") or {}
        loan = mkt.get("loanAsset") or {}
        coll = mkt.get("collateralAsset") or {}
        lltv_raw = mkt.get("lltv") or 0
        lltv = float(lltv_raw) / WAD if float(lltv_raw) > 2 else float(lltv_raw)
        repaid = int(data.get("repaidAssets") or 0)
        seized = int(data.get("seizedAssets") or 0)
        repaid_usd = _usd_from_amount(
            repaid, int(loan.get("decimals") or 0), float(loan.get("priceUsd") or 0)
        )
        seized_usd = _usd_from_amount(
            seized, int(coll.get("decimals") or 0), float(coll.get("priceUsd") or 0)
        )
        lif = liquidation_incentive_factor(lltv)
        inventory = seized_usd - repaid_usd if (seized_usd > 0 and repaid_usd > 0) else 0.0
        lif_edge = repaid_usd * max(0.0, lif - 1.0)
        # Stale spot prices can make seized-repaid negative; keep LIF floor.
        est = max(0.0, inventory, lif_edge)
        rows.append(
            LiquidationRow(
                market_id=str(mkt.get("marketId") or ""),
                loan_symbol=str(loan.get("symbol") or "?"),
                coll_symbol=str(coll.get("symbol") or "?"),
                repaid_usd=repaid_usd,
                seized_usd=seized_usd,
                est_incentive_usd=est,
                tx_hash=str(item.get("txHash") or ""),
                timestamp=int(item.get("timestamp") or 0),
            )
        )
    return rows


def market_from_liq_item(item: dict[str, Any]) -> MarketRow | None:
    mkt = item.get("market") or {}
    mid = mkt.get("marketId")
    if not mid:
        return None
    loan = mkt.get("loanAsset") or {}
    coll = mkt.get("collateralAsset") or {}
    lltv_raw = mkt.get("lltv") or 0
    lltv = float(lltv_raw) / WAD if float(lltv_raw) > 2 else float(lltv_raw)
    return MarketRow(
        market_id=str(mid),
        loan_symbol=str(loan.get("symbol") or "?"),
        coll_symbol=str(coll.get("symbol") or "—"),
        lltv=lltv,
        supply_assets=0,
        borrow_assets=0,
        supply_usd=0.0,
        borrow_usd=0.0,
        source="liq-api",
    )


def attach_liq_stats(report: ChainReport, raw_liq_items: list[dict[str, Any]] | None = None) -> None:
    by_mkt: dict[str, list[LiquidationRow]] = defaultdict(list)
    for row in report.liquidations:
        by_mkt[row.market_id].append(row)

    known = {m.market_id: m for m in report.markets}
    # Ensure markets that liquidated in 30d appear even if not "listed".
    if raw_liq_items:
        for item in raw_liq_items:
            stub = market_from_liq_item(item)
            if stub and stub.market_id not in known:
                known[stub.market_id] = stub
                report.markets.append(stub)

    for m in report.markets:
        xs = by_mkt.get(m.market_id, [])
        m.liq_30d = len(xs)
        m.liq_repaid_usd_30d = sum(x.repaid_usd for x in xs)
        m.liq_est_profit_usd_30d = sum(x.est_incentive_usd for x in xs)


def enrich_borrowers_near(
    report: ChainReport,
    *,
    min_borrow_usd: float,
    top_n: int,
    near_hf: float,
) -> None:
    candidates = [
        m
        for m in report.markets
        if m.borrow_usd >= min_borrow_usd and m.coll_symbol not in ("—", "")
    ]
    candidates.sort(key=lambda m: m.borrow_usd, reverse=True)
    selected = candidates[:top_n]
    LOG.info(
        "[%s] scanning borrowers/HF on top %d markets (borrow>=$%.0f)",
        report.chain,
        len(selected),
        min_borrow_usd,
    )
    for m in selected:
        try:
            borrowers, near = count_borrowers_and_near(
                m.market_id, report.chain_id, m.lltv, near_hf=near_hf
            )
            m.borrowers = borrowers
            m.near_liq = near
            LOG.info(
                "  %s/%s borrowers=%d near_HF<%.2f=%d (borrow TVL $%.0f)",
                m.loan_symbol,
                m.coll_symbol,
                borrowers,
                near_hf,
                near,
                m.borrow_usd,
            )
        except Exception as exc:  # noqa: BLE001
            report.notes.append(f"borrower scan failed for {m.market_id[:10]}...: {exc}")
            LOG.warning("borrower scan failed %s: %s", m.market_id[:18], exc)


def print_report(report: ChainReport, near_hf: float) -> None:
    print("\n" + "=" * 78)
    print(f" MORPHO BLUE - {report.chain.upper()} (chainId={report.chain_id})")
    print("=" * 78)
    print(f"API: {'OK' if report.api_ok else 'FAILED/FALLBACK'}")
    print(f"Markets: {len(report.markets)}")
    print(f"Liquidations (30d): {len(report.liquidations)}")
    if report.liquidations:
        repaid = sum(x.repaid_usd for x in report.liquidations)
        profit = sum(x.est_incentive_usd for x in report.liquidations)
        sizes = sorted((x.repaid_usd for x in report.liquidations), reverse=True)
        print(
            f"  repaid notional ~${repaid:,.0f} | "
            f"est liquidator inventory edge ~${profit:,.0f} "
            f"(seized-repaid or LIF heuristic; before gas/swap)"
        )
        print(
            f"  size p50=${_pct(sizes, 0.5):,.0f} "
            f"p90=${_pct(sizes, 0.9):,.0f} "
            f"max=${sizes[0]:,.0f}"
        )

    top_tvl = sorted(report.markets, key=lambda m: m.supply_usd, reverse=True)[:10]
    print("\nTop-10 markets by supply TVL (USD):")
    print(
        f"{'#':>2} {'loan/coll':<18} {'LLTV':>6} {'supply$':>12} "
        f"{'borrow$':>12} {'brwrs':>6} {'near':>5} {'liq30d':>6}"
    )
    for i, m in enumerate(top_tvl, 1):
        pair = f"{m.loan_symbol}/{m.coll_symbol}"
        print(
            f"{i:>2} {pair:<18} {m.lltv*100:5.1f}% "
            f"{m.supply_usd:12,.0f} {m.borrow_usd:12,.0f} "
            f"{m.borrowers:6d} {m.near_liq:5d} {m.liq_30d:6d}"
        )

    top_liq = sorted(report.markets, key=lambda m: m.liq_30d, reverse=True)[:5]
    print("\nTop-5 markets by liquidation count (30d):")
    for i, m in enumerate(top_liq, 1):
        if m.liq_30d <= 0:
            continue
        print(
            f"  {i}. {m.loan_symbol}/{m.coll_symbol}  "
            f"n={m.liq_30d}  repaid~${m.liq_repaid_usd_30d:,.0f}  "
            f"estEdge~${m.liq_est_profit_usd_30d:,.0f}  "
            f"id={m.market_id[:18]}..."
        )
    if not any(m.liq_30d > 0 for m in top_liq):
        print("  (none in last 30d)")

    near_total = sum(m.near_liq for m in report.markets)
    borrowers_total = sum(m.borrowers for m in report.markets)
    print(
        f"\nNear-liquidation snapshot (HF < {near_hf:.2f}) on scanned markets: "
        f"{near_total} positions "
        f"(across {borrowers_total} active borrowers counted)"
    )
    for note in report.notes:
        print(f"NOTE: {note}")


def _pct(sorted_desc: list[float], p: float) -> float:
    if not sorted_desc:
        return 0.0
    # sorted descending; convert to ascending for percentile
    xs = list(reversed(sorted_desc))
    idx = min(len(xs) - 1, max(0, int(math.floor(p * (len(xs) - 1)))))
    return xs[idx]


def scout_chain(
    chain: str,
    *,
    near_hf: float,
    min_borrow_usd: float,
    top_markets_for_hf: int,
    skip_positions: bool,
) -> ChainReport:
    chain_id = CHAIN_IDS[chain]
    report = ChainReport(chain=chain, chain_id=chain_id)
    since_ts = int(time.time()) - 30 * 86400

    try:
        raw_markets = fetch_markets_api(chain_id)
        report.markets = parse_markets(raw_markets)
        report.api_ok = True
        LOG.info("[%s] API markets: %d", chain, len(report.markets))
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"markets API failed: {exc}")
        LOG.warning("[%s] markets API failed (%s) — trying CreateMarket logs", chain, exc)
        try:
            report.markets = fallback_markets_from_logs(chain, days=90)
            report.notes.append(
                f"fallback CreateMarket logs: {len(report.markets)} markets "
                "(USD TVL unavailable without oracle prices)"
            )
        except Exception as exc2:  # noqa: BLE001
            report.notes.append(f"CreateMarket fallback failed: {exc2}")

    raw_liq: list[dict[str, Any]] = []
    try:
        raw_liq = fetch_liquidations_api(chain_id, since_ts)
        report.liquidations = parse_liquidations(raw_liq)
        LOG.info("[%s] API liquidations 30d: %d", chain, len(report.liquidations))
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"liquidations API failed: {exc}")
        LOG.warning("[%s] liq API failed (%s) — trying Liquidate logs", chain, exc)
        try:
            report.liquidations = fallback_liquidations_from_logs(chain, days=30)
            report.notes.append(
                f"fallback Liquidate logs: {len(report.liquidations)} events "
                "(USD sizes are RAW token units if prices missing)"
            )
        except Exception as exc2:  # noqa: BLE001
            report.notes.append(f"Liquidate fallback failed: {exc2}")

    attach_liq_stats(report, raw_liq)

    if report.api_ok and not skip_positions:
        enrich_borrowers_near(
            report,
            min_borrow_usd=min_borrow_usd,
            top_n=top_markets_for_hf,
            near_hf=near_hf,
        )
    elif skip_positions:
        report.notes.append("position/HF scan skipped (--skip-positions)")

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Morpho Blue viability scout")
    parser.add_argument(
        "--chains",
        default="arbitrum,base,optimism",
        help="Comma-separated chains",
    )
    parser.add_argument("--near-hf", type=float, default=1.05)
    parser.add_argument(
        "--min-borrow-usd",
        type=float,
        default=50_000.0,
        help="Only scan borrowers/HF on markets with borrow TVL >= this",
    )
    parser.add_argument(
        "--top-markets-for-hf",
        type=int,
        default=15,
        help="Max markets per chain for borrower/near-HF scan",
    )
    parser.add_argument(
        "--skip-positions",
        action="store_true",
        help="Skip borrower/HF pagination (faster markets+liq only)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    chains = [c.strip().lower() for c in args.chains.split(",") if c.strip()]
    for chain in chains:
        if chain not in CHAIN_IDS:
            LOG.error("unknown chain %s (want %s)", chain, ",".join(CHAIN_IDS))
            return 2
        try:
            http_rpc(chain)
        except Exception as exc:  # noqa: BLE001
            LOG.error("[%s] missing HTTP RPC in .env: %s", chain, exc)
            return 2

    reports: list[ChainReport] = []
    for chain in chains:
        LOG.info("=== scouting %s ===", chain)
        reports.append(
            scout_chain(
                chain,
                near_hf=args.near_hf,
                min_borrow_usd=args.min_borrow_usd,
                top_markets_for_hf=args.top_markets_for_hf,
                skip_positions=args.skip_positions,
            )
        )

    for report in reports:
        print_report(report, args.near_hf)

    print("\n" + "=" * 78)
    print(" VIABILITY SNAPSHOT (all chains)")
    print("=" * 78)
    for r in reports:
        liq_n = len(r.liquidations)
        repaid = sum(x.repaid_usd for x in r.liquidations)
        edge = sum(x.est_incentive_usd for x in r.liquidations)
        near = sum(m.near_liq for m in r.markets)
        print(
            f"  {r.chain:<10} markets={len(r.markets):4d}  "
            f"liq30d={liq_n:4d}  repaid~${repaid:,.0f}  "
            f"estEdge~${edge:,.0f}  nearHF={near}"
        )
    print(
        "\nHeuristic: if liq30d is tiny AND nearHF~0 on big markets, "
        "Morpho monitor ROI is weak vs Aave on these L2s right now.\n"
        "estEdge = seizedUSD-repaidUSD (or LIF-1 * repaid); ignores gas/swap/competition.\n"
        "borrowers may be capped (pagination limit) on huge markets."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
