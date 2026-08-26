#!/usr/bin/env python3
"""Paper eth_call against a local Base fork (anvil). Never broadcasts.

Requires a fork with MorphoFlashLiquidator already deployed (unsigned /
anvil-only key). Expectation: call reverts (HEALTHY / OnlyOperator /
no underwater target) — that still proves encode + RPC paper path.

One-shot: no --duration-seconds cap (exits after one paper eth_call).

    .venv-run\\Scripts\\python.exe morpho_research\\paper_fork_smoke.py --rpc http://127.0.0.1:8545 --contract 0x...
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from web3 import Web3

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
for p in (_DIR, _ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from morpho_executor import LiqIntent, MorphoExecutor  # noqa: E402
from morpho_markets import markets_for_chain  # noqa: E402
from morpho_hf import shares_to_assets_up  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc", default="http://127.0.0.1:8545")
    parser.add_argument("--contract", required=True)
    parser.add_argument(
        "--from-addr",
        default="",
        help="msg.sender for eth_call (operator). Empty = anvil account 0.",
    )
    args = parser.parse_args()

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        print("RPC unreachable:", args.rpc)
        return 2

    os.environ["MORPHO_MODE"] = "paper"
    os.environ["MORPHO_LIQ_CONTRACT"] = args.contract
    os.environ["MORPHO_AUTO_EXECUTE"] = "false"
    os.environ.pop("MORPHO_LIVE_CONFIRM", None)

    exe = MorphoExecutor("base", w3)
    live_ok, why = exe._live_ready()
    print(f"mode={exe.mode} live_ready={live_ok} ({why}) contract={exe.contract_addr}")
    if exe.mode == "live" or live_ok:
        print("refusing: live must stay gated")
        return 3

    markets = markets_for_chain("base")
    m = markets[0]
    from_addr = args.from_addr or w3.eth.accounts[0]
    exe.operator = from_addr

    intent = LiqIntent(
        chain="base",
        user=from_addr,
        market=m,
        health_factor=__import__("decimal").Decimal("0.90"),
        debt_usd=1200.0,
        profit_usd=45.0,
        borrow_shares=1_000_000 * 10**6,
        collateral=2 * 10**18,
        total_borrow_assets=10_000_000 * 10**6,
        total_borrow_shares=10_000_000 * 10**6,
        oracle_price=10**36 // 2,
        loan_decimals=6,
        reason="paper-fork-smoke",
    )
    borrowed = shares_to_assets_up(
        intent.borrow_shares, intent.total_borrow_assets, intent.total_borrow_shares
    )
    print(f"dummy borrowed_assets={borrowed} (synthetic; expect revert)")
    encoded = exe.encode(intent, recipient=exe.contract_addr)
    if encoded is None:
        print("encode returned None")
        return 1
    print(
        f"encoded selector=0x{encoded.calldata[:4].hex()} seized={encoded.seized_assets} kind={encoded.kind}"
    )
    exe._paper(intent, encoded)
    print("paper result:", exe.metrics.snapshot())
    print("ok: paper path ran (sim_ok or sim_fail are both useful; sent must stay 0)")
    if exe.metrics.sent != 0:
        print("BUG: sent incremented on paper")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
