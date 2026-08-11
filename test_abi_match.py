"""Verifies the Python ABI against the compiled Solidity contract.

The original monitor called liquidate(...), a function AaveV3LiquidationBot does
not have, so every AUTO_EXECUTE attempt would have been sent with a selector no
contract answers to. Nothing catches that at runtime except a wasted
transaction, so the two sides are compared here by selector.

Requires solcx (available in .venv312):
    .venv312\\Scripts\\python.exe test_abi_match.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from eth_utils import function_abi_to_4byte_selector

from aave_bot.abis import LIQUIDATION_BOT_ABI, LIQUIDATION_BOT_ERROR_SIGNATURES

CONTRACT = Path("contracts/AaveV3LiquidationBot.sol")
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def canonical(entry: dict) -> str:
    def kind(item: dict) -> str:
        if item["type"].startswith("tuple"):
            inner = ",".join(kind(component) for component in item["components"])
            return f"({inner}){item['type'][5:]}"
        return item["type"]

    args = ",".join(kind(item) for item in entry.get("inputs", []))
    return f"{entry['name']}({args})"


def selector(entry: dict) -> str:
    return "0x" + function_abi_to_4byte_selector(entry).hex()


def compile_contract() -> list[dict]:
    import solcx

    source = CONTRACT.read_text(encoding="utf-8")
    compiled = solcx.compile_source(
        source, output_values=["abi"], solc_version="0.8.20", optimize=True
    )
    for name, artifact in compiled.items():
        if name.endswith(":AaveV3LiquidationBot"):
            return artifact["abi"]
    raise RuntimeError("AaveV3LiquidationBot not found in the compiled output")


def main() -> int:
    print(f"compiling {CONTRACT}")
    on_chain_abi = compile_contract()

    print("\n[functions]")
    solidity_functions = {
        canonical(e): selector(e) for e in on_chain_abi if e["type"] == "function"
    }
    python_functions = {
        canonical(e): selector(e) for e in LIQUIDATION_BOT_ABI if e["type"] == "function"
    }

    missing = [sig for sig in python_functions if sig not in solidity_functions]
    check("every function the bot calls exists in the contract", not missing,
          f"absent from the contract: {missing}")

    wrong = [
        f"{sig}: python {sel} vs solidity {solidity_functions[sig]}"
        for sig, sel in python_functions.items()
        if sig in solidity_functions and sel != solidity_functions[sig]
    ]
    check("selectors match for every shared function", not wrong, "; ".join(wrong))

    entry_point = "initiateLiquidation(address,uint256,(address,address,address,address[],uint256,uint256,uint256))"
    check("the liquidation entry point is present with the expected shape",
          entry_point in python_functions and entry_point in solidity_functions,
          f"python has {[s for s in python_functions if s.startswith('initiateLiquidation')]}, "
          f"solidity has {[s for s in solidity_functions if s.startswith('initiateLiquidation')]}")

    check("the legacy liquidate(...) signature really is absent",
          not any(sig.startswith("liquidate(") for sig in solidity_functions),
          f"found {[s for s in solidity_functions if s.startswith('liquidate(')]}")

    for signature, sel in sorted(python_functions.items()):
        mark = "ok " if solidity_functions.get(signature) == sel else "BAD"
        print(f"    {mark} {sel}  {signature}")

    print("\n[custom errors]")
    solidity_errors = {canonical(e) for e in on_chain_abi if e["type"] == "error"}
    declared = set(LIQUIDATION_BOT_ERROR_SIGNATURES)

    unknown = declared - solidity_errors
    check("every error we decode is declared by the contract", not unknown,
          f"not in the contract: {sorted(unknown)}")

    undecoded = solidity_errors - declared
    check("every contract error can be decoded", not undecoded,
          f"missing from the table: {sorted(undecoded)}")
    print(f"    contract declares {len(solidity_errors)} errors, "
          f"table covers {len(declared & solidity_errors)}")

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for name in failures:
            print(" -", name)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
