#!/usr/bin/env python3
"""Deploy MorphoFlashLiquidator (hard-batch) to Base, update .env, restart VPS Morpho.

Never prints private keys. Never touches Aave.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
BROADCAST = (
    ROOT
    / "broadcast"
    / "DeployMorphoFlashLiquidator.s.sol"
    / "8453"
    / "run-latest.json"
)


def _env() -> dict[str, str]:
    raw = dotenv_values(ENV_PATH)
    out: dict[str, str] = {}
    for k, v in raw.items():
        if k and v is not None:
            out[str(k)] = str(v).strip().strip('"').strip("'")
    return out


def _pick_key(cfg: dict[str, str]) -> str:
    for name in ("MORPHO_PRIVATE_KEY", "BASE_PRIVATE_KEY", "DEPLOYER_KEY", "PRIVATE_KEY"):
        v = cfg.get(name, "")
        if v:
            if not v.startswith("0x"):
                v = "0x" + v
            return v
    raise SystemExit("NO_DEPLOY_KEY")


def _pick_rpc(cfg: dict[str, str]) -> str:
    # Prefer public endpoints for forge broadcast — free dRPC often 408s.
    publics = (
        "https://mainnet.base.org",
        "https://base.publicnode.com",
        "https://base.llamarpc.com",
        "https://1rpc.io/base",
    )
    for name in ("BASE_HTTP_RPC_URL", "BASE_RPC_URL", "BASE_HTTP_RPC_URLS"):
        v = cfg.get(name, "")
        if not v:
            continue
        first = v.split(",")[0].strip()
        # Skip known free-tier flaky hosts for long forge scripts.
        if "drpc" in first.lower() or "ankr.com/free" in first.lower():
            continue
        return first
    return publics[0]


def _update_env_contract(addr: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8")
    if re.search(r"^MORPHO_LIQ_CONTRACT=", text, flags=re.M):
        text = re.sub(
            r"^MORPHO_LIQ_CONTRACT=.*$",
            f"MORPHO_LIQ_CONTRACT={addr}",
            text,
            count=1,
            flags=re.M,
        )
    else:
        text = text.rstrip() + f"\nMORPHO_LIQ_CONTRACT={addr}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    print(f"env_updated MORPHO_LIQ_CONTRACT={addr}")


def _parse_deployed() -> str:
    if not BROADCAST.exists():
        raise SystemExit(f"NO_BROADCAST {BROADCAST}")
    data = json.loads(BROADCAST.read_text(encoding="utf-8"))
    for tx in data.get("transactions") or []:
        if (tx.get("contractName") or "") == "MorphoFlashLiquidator":
            addr = tx.get("contractAddress") or ""
            if addr:
                return addr
        # CREATE txs
        if tx.get("transactionType") == "CREATE" and tx.get("contractAddress"):
            return str(tx["contractAddress"])
    # receipts fallback
    for rec in data.get("receipts") or []:
        addr = rec.get("contractAddress")
        if addr and addr != "0x" + "00" * 20:
            return str(addr)
    raise SystemExit("DEPLOY_ADDR_NOT_FOUND")


def main() -> int:
    cfg = _env()
    key = _pick_key(cfg)
    rpc = _pick_rpc(cfg)
    operator = cfg.get("MORPHO_OPERATOR_ADDRESS") or ""
    if not operator:
        raise SystemExit("NO_MORPHO_OPERATOR_ADDRESS")

    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".foundry" / "bin") + os.pathsep + env.get("PATH", "")
    env["PRIVATE_KEY"] = key
    env["MORPHO_OPERATOR_ADDRESS"] = operator
    env["MORPHO_DEPLOY_UNPAUSE"] = "true"
    env["BASE_RPC_URL"] = rpc

    print("forge_build")
    r = subprocess.run(
        ["forge", "build"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print((r.stderr or r.stdout)[-2000:])
        return 1

    print(f"forge_script chain=8453 operator={operator[:10]}… rpc_host={rpc.split('/')[2] if '://' in rpc else rpc[:30]}")
    r = subprocess.run(
        [
            "forge",
            "script",
            "scripts/DeployMorphoFlashLiquidator.s.sol:DeployMorphoFlashLiquidator",
            "--rpc-url",
            rpc,
            "--broadcast",
            "--chain",
            "8453",
            "--private-key",
            key,
            "-vvvv",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    # scrub any accidental key echo
    out = out.replace(key, "<KEY>")
    print(out[-3500:])
    if r.returncode != 0:
        print("DEPLOY_FAIL")
        return 1

    addr = _parse_deployed()
    print(f"DEPLOYED={addr}")
    _update_env_contract(addr)

    print("vps_deploy")
    r2 = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "deploy_morpho_vps.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    print((r2.stdout or "")[-1500:])
    if r2.stderr:
        print((r2.stderr or "")[-500:])
    if r2.returncode != 0:
        print("VPS_FAIL")
        return 1
    print("HARD_BATCH_DEPLOY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
