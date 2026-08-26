#!/usr/bin/env python3
"""One-shot: setApprovals for a collateral token on MorphoFlashLiquidator. Never prints keys."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "morpho_research"))

from dotenv import load_dotenv
from web3 import Web3

import aave_bot.config as bot_config  # noqa: E402
from morpho_executor import (  # noqa: E402
    AERODROME_ROUTER_BASE,
    UNI_V3_SWAP_ROUTER_02_ALT,
    UNI_V3_SWAP_ROUTER_02_BASE,
    _addr,
)

SET_APPROVALS_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "address[]", "name": "spenders", "type": "address[]"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "setApprovals",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

DEFAULT_SPENDERS = (
    UNI_V3_SWAP_ROUTER_02_ALT,
    UNI_V3_SWAP_ROUTER_02_BASE,
    AERODROME_ROUTER_BASE,
)


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="base")
    ap.add_argument("--token", required=True, help="collateral ERC20")
    ap.add_argument("--cap", type=int, default=30_000 * 10**6, help="finite approval cap (6 dec default)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    contract = bot_config._text("MORPHO_LIQ_CONTRACT", args.chain, inherit=True)
    if not contract:
        print("MISSING_MORPHO_LIQ_CONTRACT")
        return 2
    key = bot_config._text("MORPHO_PRIVATE_KEY", args.chain, inherit=True)
    if not key and not args.dry_run:
        print("MISSING_MORPHO_PRIVATE_KEY")
        return 2

    rpc = bot_config._text("BASE_HTTP_RPC_URL", args.chain, inherit=True) or bot_config._text(
        "BASE_RPC_URL", args.chain, inherit=True
    )
    if not rpc:
        rpc = "https://base.drpc.org"
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        print("RPC_FAIL")
        return 3

    token = _addr(args.token)
    spenders = [_addr(s) for s in DEFAULT_SPENDERS]
    bot = w3.eth.contract(address=_addr(contract), abi=SET_APPROVALS_ABI)
    data = bot.encode_abi("setApprovals", args=[token, spenders, int(args.cap)])
    print(f"contract={_addr(contract)} token={token} cap={args.cap} spenders={len(spenders)}")
    if args.dry_run:
        print("dry-run ok")
        return 0

    acct = w3.eth.account.from_key(key)
    tx = {
        "from": acct.address,
        "to": _addr(contract),
        "data": data,
        "value": 0,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": w3.eth.chain_id,
    }
    try:
        tx["gas"] = w3.eth.estimate_gas(tx)
    except Exception as exc:  # noqa: BLE001
        print(f"estimate_gas failed: {exc}")
        return 4
    tx["maxPriorityFeePerGas"] = w3.to_wei(0.006, "gwei")
    tx["maxFeePerGas"] = max(w3.eth.gas_price, tx["maxPriorityFeePerGas"] * 2)
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"tx={h.hex()}")
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    print(f"status={rcpt.status} gas={rcpt.gasUsed}")
    return 0 if rcpt.status == 1 else 5


if __name__ == "__main__":
    raise SystemExit(main())
