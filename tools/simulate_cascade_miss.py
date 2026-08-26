#!/usr/bin/env python3
"""Simulate: were cascade victims in seed top-N? On-chain HF before liq block."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MORPHO_DIR = ROOT / "morpho_research"
for p in (ROOT, MORPHO_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from dotenv import dotenv_values
from eth_abi import decode as abi_decode
from web3 import Web3

from aave_bot.topics import ANSWER_UPDATED_TOPIC
from morpho_hf import (
    hf_from_cached_shares,
    project_morpho_price,
    read_chainlink_answer,
    resolve_morpho_oracle_feed_roles,
    shares_to_assets_up,
)
from morpho_markets import markets_for_chain

VICTIMS = [
    "0x540411fFF8A34809B8E2461cAEAD4c7808C237d7",
    "0xb08FAaB46172eBF09173187Abf2a45EfB71B18B5",
    "0x77d39CBbE98763aECeb2c460cD2E7B634573aab0",
    "0xEeAD5b07b0d83e11e83e1cEEB8a433e0C793d713",
    "0x527DFfe3754487a6947D15AEC3379406DA268371",
    "0x3Fd1D7eBa572dFEc00e0dBEB94EC0d5FE5E71a88",
    "0x5f2bfae3201FaB8f7Abf4cC58D648fA6301BF2F2",
    "0x9630233A31cfEE6fb907C4d8AEfF93C8FE2a98c6",
]
BLOCK = 50451390  # before cascade block 50451391
LIQ_BLOCK = 50451391
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
MARKET_ID = "0xd4a903dc6d949519060c7707f9604fdc9772c046e05c2e3a8fce0bd7196e4109"
XRP_AGG = "0x92A7c3a57E17AfF701c159c5480073B095100b62"
WHALE = VICTIMS[0]

POSITION_ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "id", "type": "bytes32"},
            {"internalType": "address", "name": "user", "type": "address"},
        ],
        "name": "position",
        "outputs": [
            {"internalType": "uint256", "name": "supplyShares", "type": "uint256"},
            {"internalType": "uint128", "name": "borrowShares", "type": "uint128"},
            {"internalType": "uint128", "name": "collateral", "type": "uint128"},
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

ORACLE_ABI = [
    {
        "inputs": [],
        "name": "price",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


BASE_RPCS = (
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.gateway.tenderly.co",
    "https://1rpc.io/base",
)


def rpc_url() -> str:
    cfg = dotenv_values(ROOT / ".env")
    for key in ("BASE_HTTP_RPC_URL", "BASE_RPC_URL", "HTTP_RPC_URL"):
        v = (cfg.get(key) or "").strip()
        if v.startswith("http"):
            return v
    for k, v in cfg.items():
        if v and "RPC" in k.upper() and "BASE" in k.upper() and str(v).startswith("http"):
            return str(v).strip()
    return BASE_RPCS[0]


def connect_base(w3_url: str | None = None) -> Web3:
    urls = [w3_url] if w3_url else [rpc_url(), *BASE_RPCS]
    seen: set[str] = set()
    last: Exception | None = None
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))
        try:
            if w3.is_connected() and w3.eth.get_block(LIQ_BLOCK):
                print(f"rpc={url}")
                return w3
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
    raise SystemExit(f"no Base RPC for block {LIQ_BLOCK}: {last}")


def seed_rank_check() -> None:
    from morpho_scanner import seed_borrowers_from_api

    for n in (150, 300, 500, 1000):
        seeded = seed_borrowers_from_api(8453, markets_for_chain("base"), per_market=n)
        cbxrp = [u for mid, u in seeded if mid.lower() == MARKET_ID.lower()]
        hit = [u for u in cbxrp if u.lower() in {v.lower() for v in VICTIMS}]
        print(f"seed_top_{n}: cbXRP borrowers={len(cbxrp)} victims_in_seed={len(hit)}")
        for u in hit:
            print(f"  IN_SEED {u}")


def onchain_hf_before() -> None:
    w3 = connect_base()
    morpho = w3.eth.contract(address=Web3.to_checksum_address(MORPHO), abi=POSITION_ABI)
    market = markets_for_chain("base")
    m = next(x for x in market if x.market_id.lower() == MARKET_ID.lower())
    mid_b = bytes.fromhex(MARKET_ID[2:])
    mkt = morpho.functions.market(mid_b).call(block_identifier=BLOCK)
    tba, tbs = int(mkt[2]), int(mkt[3])
    oracle = w3.eth.contract(
        address=Web3.to_checksum_address(m.oracle), abi=ORACLE_ABI
    )
    op = int(oracle.functions.price().call(block_identifier=BLOCK))
    print(f"block={BLOCK} oracle_price={op} total_borrow_assets={tba}")
    for u in VICTIMS:
        pos = morpho.functions.position(mid_b, Web3.to_checksum_address(u)).call(
            block_identifier=BLOCK
        )
        bs, coll = int(pos[1]), int(pos[2])
        if bs == 0:
            print(f"{u[:14]} NO BORROW at block")
            continue
        borrowed = shares_to_assets_up(bs, tba, tbs)
        hf, _ = hf_from_cached_shares(
            collateral=coll,
            borrow_shares=bs,
            total_borrow_assets=tba,
            total_borrow_shares=tbs,
            oracle_price=op,
            lltv_wad=m.lltv_wad,
        )
        debt_usd = borrowed / 1e6
        print(
            f"{u[:14]} borrow_shares={bs} coll={coll} debt_usd~{debt_usd:.0f} "
            f"HF={float(hf):.4f} in_near_1.05={float(hf)<1.05} liq={float(hf)<1.0}"
        )


def oracle_move_sim() -> None:
    """Morpho oracle price across cascade blocks."""
    w3 = connect_base()
    m = next(x for x in markets_for_chain("base") if x.market_id.lower() == MARKET_ID.lower())
    oracle = w3.eth.contract(
        address=Web3.to_checksum_address(m.oracle), abi=ORACLE_ABI
    )
    p_before = int(oracle.functions.price().call(block_identifier=BLOCK - 50))
    p_at = int(oracle.functions.price().call(block_identifier=BLOCK))
    p_liq = int(oracle.functions.price().call(block_identifier=LIQ_BLOCK))
    p_after = int(oracle.functions.price().call(block_identifier=LIQ_BLOCK + 1))
    bps = abs(p_liq - p_before) * 10000 // p_before if p_before else 0
    print(f"morpho oracle block-{50}: {p_before}")
    print(f"morpho oracle block {BLOCK}: {p_at}")
    print(f"morpho oracle block {LIQ_BLOCK} (liq): {p_liq} (d vs -50 = {bps} bps)")
    print(f"morpho oracle block+1: {p_after}")


def _cl_answer_at_block(w3: Web3, agg: str, block: int) -> int | None:
    """Chainlink answer via getRoundData at block (historical)."""
    c = w3.eth.contract(address=Web3.to_checksum_address(agg), abi=[
        {
            "inputs": [],
            "name": "latestRoundData",
            "outputs": [
                {"internalType": "uint80", "name": "roundId", "type": "uint80"},
                {"internalType": "int256", "name": "answer", "type": "int256"},
                {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
                {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
                {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
            ],
            "stateMutability": "view",
            "type": "function",
        }
    ])
    try:
        ans = int(c.functions.latestRoundData().call(block_identifier=block)[1])
        return ans if ans > 0 else None
    except Exception:
        return None


def _answer_updated_in_block(w3: Web3, block: int) -> list[dict]:
    logs = w3.eth.get_logs(
        {
            "address": Web3.to_checksum_address(XRP_AGG),
            "fromBlock": block,
            "toBlock": block,
            "topics": [ANSWER_UPDATED_TOPIC],
        }
    )
    out = []
    for entry in logs:
        cur = abi_decode(["int256"], bytes(entry["topics"][1]))[0]
        out.append({"block": block, "tx": entry["transactionHash"].hex(), "answer": int(cur)})
    return out


def feed_cl_sim() -> None:
    """Old path vs new feed-cl: would we see HF<1 before foreign liq?"""
    w3 = connect_base()
    m = next(x for x in markets_for_chain("base") if x.market_id.lower() == MARKET_ID.lower())
    morpho = w3.eth.contract(address=Web3.to_checksum_address(MORPHO), abi=POSITION_ABI)
    oracle = w3.eth.contract(address=Web3.to_checksum_address(m.oracle), abi=ORACLE_ABI)
    mid_b = bytes.fromhex(MARKET_ID[2:])
    base, quote = resolve_morpho_oracle_feed_roles(w3, m)

    morpho_prev = int(oracle.functions.price().call(block_identifier=BLOCK))
    morpho_at_liq = int(oracle.functions.price().call(block_identifier=LIQ_BLOCK))
    morpho_pending_same = morpho_prev == morpho_at_liq

    cl_before = _cl_answer_at_block(w3, XRP_AGG, BLOCK)
    cl_at_liq = _cl_answer_at_block(w3, XRP_AGG, LIQ_BLOCK)
    cl_live = read_chainlink_answer(w3, XRP_AGG)

    old_answers = {XRP_AGG.lower(): cl_before} if cl_before else {}
    new_answers = {XRP_AGG.lower(): cl_at_liq} if cl_at_liq else {}
    projected = None
    if cl_before and cl_at_liq and cl_before != cl_at_liq:
        projected = project_morpho_price(
            morpho_prev, old_answers, new_answers, base, quote
        )

    mkt = morpho.functions.market(mid_b).call(block_identifier=BLOCK)
    tba, tbs = int(mkt[2]), int(mkt[3])

    print(f"XRP/USD cl block {BLOCK}: {cl_before}")
    print(f"XRP/USD cl block {LIQ_BLOCK}: {cl_at_liq}")
    if cl_before and cl_at_liq:
        cl_bps = abs(cl_at_liq - cl_before) * 10000 // cl_before
        print(f"XRP/USD d = {cl_bps} bps")
    print(f"morpho_prev={morpho_prev} morpho_liq_block={morpho_at_liq} same={morpho_pending_same}")
    print(f"projected_morpho={projected}")
    if projected and morpho_prev:
        pbps = abs(projected - morpho_prev) * 10000 // morpho_prev
        print(f"projected d vs morpho_prev = {pbps} bps (storm_wide={pbps >= 50})")

    # AnswerUpdated logs in liq block
    try:
        evs = _answer_updated_in_block(w3, LIQ_BLOCK)
        print(f"AnswerUpdated in block {LIQ_BLOCK}: {len(evs)}")
        for ev in evs:
            print(f"  tx={ev['tx'][:18]}... answer={ev['answer']}")
    except Exception as exc:  # noqa: BLE001
        print(f"get_logs_err {type(exc).__name__}: {exc}")

    def hf_for(user: str, op: int, label: str) -> None:
        pos = morpho.functions.position(mid_b, Web3.to_checksum_address(user)).call(
            block_identifier=BLOCK
        )
        bs, coll = int(pos[1]), int(pos[2])
        if bs == 0:
            print(f"  {label} {user[:14]} NO BORROW")
            return
        hf, borrowed = hf_from_cached_shares(
            collateral=coll,
            borrow_shares=bs,
            total_borrow_assets=tba,
            total_borrow_shares=tbs,
            oracle_price=op,
            lltv_wad=m.lltv_wad,
        )
        debt = borrowed / 1e6
        liq = float(hf) < 1.0
        hot = float(hf) < 1.05
        print(
            f"  {label} {user[:14]} debt~${debt:.0f} HF={float(hf):.4f} "
            f"hot={hot} LIQ={liq}"
        )

    print("\n--- OLD PATH (Morpho RPC @ block, WS sees unchanged) ---")
    hf_for(WHALE, morpho_prev, "feed-ws-skip")
    print("  -> tick skipped when morpho_read == morpho_prev (our bug)")

    print("\n--- NEW PATH (feed-cl projection from Chainlink move) ---")
    if projected:
        hf_for(WHALE, projected, "feed-cl")
    else:
        print("  (no projection — missing cl answers)")

    print("\n--- REALITY @ liq block morpho price ---")
    hf_for(WHALE, morpho_at_liq, "on-chain")

    print("\n--- ALL VICTIMS @ projected vs morpho_prev ---")
    if projected:
        liq_n = 0
        for u in VICTIMS:
            pos = morpho.functions.position(mid_b, Web3.to_checksum_address(u)).call(
                block_identifier=BLOCK
            )
            bs, coll = int(pos[1]), int(pos[2])
            if bs == 0:
                continue
            hf, borrowed = hf_from_cached_shares(
                collateral=coll,
                borrow_shares=bs,
                total_borrow_assets=tba,
                total_borrow_shares=tbs,
                oracle_price=projected,
                lltv_wad=m.lltv_wad,
            )
            if float(hf) < 1.0:
                liq_n += 1
                print(f"  LIQABLE {u[:14]} HF={float(hf):.4f} debt~${borrowed/1e6:.0f}")
        print(f"  feed-cl would try_liq: {liq_n}/{len(VICTIMS)} victims")


if __name__ == "__main__":
    print("=== SEED RANK ===")
    try:
        seed_rank_check()
    except Exception as e:
        print("seed_err", e)
    print("\n=== ONCHAIN HF @ block-1 ===")
    try:
        onchain_hf_before()
    except Exception as e:
        print("onchain_err", type(e).__name__, e)
    print("\n=== ORACLE MOVE ===")
    try:
        oracle_move_sim()
    except Exception as e:
        print("oracle_err", type(e).__name__, e)
    print("\n=== FEED-CL SIM (old vs new path) ===")
    try:
        feed_cl_sim()
    except Exception as e:
        print("feed_cl_err", type(e).__name__, e)
