"""Compile AaveV3LiquidationBot.sol and report compiler diagnostics."""
import sys
from pathlib import Path

import solcx

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("contracts/AaveV3LiquidationBot.sol")

VERSION = "0.8.20"
if VERSION not in [str(v) for v in solcx.get_installed_solc_versions()]:
    print(f"Installing solc {VERSION} ...")
    solcx.install_solc(VERSION)

source = SRC.read_text(encoding="utf-8")

std_input = {
    "language": "Solidity",
    "sources": {"AaveV3LiquidationBot.sol": {"content": source}},
    "settings": {
        "optimizer": {"enabled": True, "runs": 200},
        "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
    },
}

try:
    out = solcx.compile_standard(std_input, solc_version=VERSION, allow_empty=True)
except solcx.exceptions.SolcError as e:
    print("COMPILE FAILED")
    print(e)
    sys.exit(1)

for err in out.get("errors", []):
    print(f"[{err['severity']}] {err.get('formattedMessage', err['message'])}")

contracts = out["contracts"]["AaveV3LiquidationBot.sol"]
print("\n=== COMPILED CONTRACTS ===")
for name, data in contracts.items():
    size = len(data["evm"]["bytecode"]["object"]) // 2
    print(f"{name}: bytecode {size} bytes")

bot = contracts.get("AaveV3LiquidationBot")
if bot:
    fns = sorted(
        item["name"] for item in bot["abi"]
        if item["type"] == "function"
    )
    print("\n=== EXTERNAL FUNCTIONS ===")
    for f in fns:
        print(" -", f)
    print("\nHas 'liquidate' (as monitor_v4.py expects):", "liquidate" in fns)

print("\nCOMPILE OK")
