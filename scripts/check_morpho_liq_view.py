#!/usr/bin/env python3
"""Live RPC view-reads for MorphoFlashLiquidator (no send).

Default: Base Sepolia (84532). Verifies Morpho Blue has code, then — if an
address is provided — paused/owner/operator/MORPHO and bytecode size.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

MORPHO_BLUE = Web3.to_checksum_address("0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb")
VIEW_ABI = [
    {"inputs": [], "name": "paused", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "owner", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "operator", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [],
        "name": "MORPHO",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "router", "type": "address"}],
        "name": "allowedRouters",
        "outputs": [{"type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]
UNI_V3 = "0x2626664c2603336E57B271c5C0b26F421741e481"
UNI_V3_REPO = "0x2626664c2603336E57b271c5c0d842f2875A7dA0"
AERO = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"


def _rpc(network: str) -> str:
    if network == "mainnet":
        return (
            os.getenv("BASE_RPC_URL")
            or os.getenv("BASE_HTTP_RPC_URL")
            or "https://mainnet.base.org"
        )
    return os.getenv("BASE_SEPOLIA_RPC_URL") or "https://base-sepolia-rpc.publicnode.com"


def _upsert_env(key: str, value: str) -> None:
    path = ROOT / ".env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    line = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    path.write_text(text, encoding="utf-8")


def _from_broadcast(path: Path) -> str | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for tx in data.get("transactions") or []:
        if tx.get("contractName") == "MorphoFlashLiquidator" and tx.get("contractAddress"):
            return tx["contractAddress"]
    receipts = data.get("receipts") or []
    if receipts and receipts[0].get("contractAddress"):
        return receipts[0]["contractAddress"]
    return None


def check_contract(w3: Web3, addr: str) -> dict:
    checksum = Web3.to_checksum_address(addr)
    code = w3.eth.get_code(checksum)
    if len(code) == 0:
        print(json.dumps({
            "address": checksum,
            "chain_id": w3.eth.chain_id,
            "code_size": 0,
            "deployed": False,
        }, indent=2))
        raise SystemExit("code size is 0 — not a contract")
    c = w3.eth.contract(address=checksum, abi=VIEW_ABI)
    paused = bool(c.functions.paused().call())
    owner = c.functions.owner().call()
    operator = c.functions.operator().call()
    morpho = c.functions.MORPHO().call()
    routers = {}
    for name, r in (("uni_v3", UNI_V3), ("uni_v3_repo", UNI_V3_REPO), ("aero", AERO)):
        try:
            routers[name] = bool(c.functions.allowedRouters(Web3.to_checksum_address(r)).call())
        except Exception as exc:  # noqa: BLE001
            routers[name] = f"err:{exc}"
    out = {
        "address": checksum,
        "chain_id": w3.eth.chain_id,
        "code_size": len(code),
        "paused": paused,
        "owner": owner,
        "operator": operator,
        "MORPHO": morpho,
        "routers": routers,
    }
    print(json.dumps(out, indent=2))
    if len(code) == 0:
        raise SystemExit("code size is 0 — not a contract")
    if not paused:
        raise SystemExit("BLOCKER: paused != true (must stay paused)")
    if Web3.to_checksum_address(morpho) != MORPHO_BLUE:
        print("WARN: MORPHO != canonical CREATE2 Morpho Blue", file=sys.stderr)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--network", choices=("sepolia", "mainnet"), default="sepolia")
    p.add_argument("--address", default="")
    p.add_argument("--record-broadcast", default="")
    args = p.parse_args()

    w3 = Web3(Web3.HTTPProvider(_rpc(args.network), request_kwargs={"timeout": 30}))
    expected = 84532 if args.network == "sepolia" else 8453
    cid = w3.eth.chain_id
    print(f"rpc chain_id={cid} expected={expected}")
    if cid != expected:
        print("BLOCKER: wrong chain", file=sys.stderr)
        return 2
    morpho_code = w3.eth.get_code(MORPHO_BLUE)
    print(f"Morpho Blue {MORPHO_BLUE} code_size={len(morpho_code)}")
    if len(morpho_code) == 0:
        print("BLOCKER: Morpho Blue has no code on this chain", file=sys.stderr)
        return 3

    addr = args.address.strip()
    if args.record_broadcast:
        found = _from_broadcast(Path(args.record_broadcast))
        if not found:
            print("no MorphoFlashLiquidator in broadcast json", file=sys.stderr)
            return 4
        addr = found
        env_key = "MORPHO_LIQ_CONTRACT_SEPOLIA" if args.network == "sepolia" else "MORPHO_LIQ_CONTRACT"
        _upsert_env(env_key, Web3.to_checksum_address(addr))
        print(f"wrote {env_key} to .env")

    if not addr:
        addr = os.getenv(
            "MORPHO_LIQ_CONTRACT_SEPOLIA" if args.network == "sepolia" else "MORPHO_LIQ_CONTRACT",
            "",
        )
    if not addr:
        print("no contract address (deploy first). Morpho Blue on this chain: OK")
        return 0
    check_contract(w3, addr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
