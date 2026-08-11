"""
End-to-end test of AaveV3LiquidationBot on an in-process EVM (py-evm via
eth-tester). No network, wallet, or Remix involved.

Covers the full flash-loan liquidation path plus every guard clause, and
reproduces the nonReentrant deadlock from the original contract by re-adding
the modifier to otherwise identical source.
"""
import ast
import re
import sys
from pathlib import Path

import solcx
from eth_tester import EthereumTester, PyEVMBackend
from web3 import EthereumTesterProvider, Web3
from web3.logs import DISCARD

SOLC_VERSION = "0.8.20"
BOT_SRC = Path("contracts/AaveV3LiquidationBot.sol")
MOCKS_SRC = Path("contracts/test/Mocks.sol")

WAD = 10 ** 18
DEADLINE = 2 ** 40

failures: list[str] = []


def selector(signature: str) -> str:
    return Web3.keccak(text=signature)[:4].hex().lower().removeprefix("0x")


def compile_all(bot_source: str) -> dict:
    if SOLC_VERSION not in [str(v) for v in solcx.get_installed_solc_versions()]:
        solcx.install_solc(SOLC_VERSION)

    std_input = {
        "language": "Solidity",
        "sources": {
            "AaveV3LiquidationBot.sol": {"content": bot_source},
            "Mocks.sol": {"content": MOCKS_SRC.read_text(encoding="utf-8")},
        },
        "settings": {
            # paris avoids PUSH0, which py-evm's default rules reject.
            "evmVersion": "paris",
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
        },
    }
    out = solcx.compile_standard(std_input, solc_version=SOLC_VERSION, allow_empty=True)

    artifacts = {}
    for source_file, contracts in out["contracts"].items():
        for name, data in contracts.items():
            artifacts[name] = (data["abi"], data["evm"]["bytecode"]["object"])
    return artifacts


def make_w3() -> Web3:
    return Web3(EthereumTesterProvider(EthereumTester(backend=PyEVMBackend())))


def deploy(w3: Web3, artifacts: dict, name: str, deployer: str, *args):
    abi, bytecode = artifacts[name]
    factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx_hash = factory.constructor(*args).transact({"from": deployer})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    assert receipt.status == 1, f"{name} deployment failed"
    return w3.eth.contract(address=receipt.contractAddress, abi=abi)


class Env:
    """A freshly deployed world: tokens, mock pool, mock router, and the bot."""

    def __init__(self, artifacts: dict, same_asset: bool = False):
        self.w3 = make_w3()
        self.owner, self.operator, self.victim, self.stranger = self.w3.eth.accounts[:4]

        self.debt = deploy(self.w3, artifacts, "MockERC20", self.owner, "DEBT")
        self.collateral = (
            self.debt if same_asset
            else deploy(self.w3, artifacts, "MockERC20", self.owner, "COLL")
        )
        self.pool = deploy(self.w3, artifacts, "MockAavePool", self.owner)
        self.router = deploy(self.w3, artifacts, "MockRouter", self.owner)
        self.bot = deploy(
            self.w3, artifacts, "AaveV3LiquidationBot",
            self.owner, self.pool.address, self.operator,
        )

        funding = 1_000_000 * WAD
        self._send(self.debt.functions.mint(self.pool.address, funding))
        self._send(self.collateral.functions.mint(self.pool.address, funding))
        self._send(self.debt.functions.mint(self.router.address, funding))

        self._send(self.bot.functions.setRouterAllowed(self.router.address, True))
        self._send(self.bot.functions.setMaxDebtCover(self.debt.address, 10_000 * WAD))

    def _send(self, fn, sender: str | None = None):
        tx_hash = fn.transact({"from": sender or self.owner})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        assert receipt.status == 1, "transaction reverted unexpectedly"
        return receipt

    def params(self, **overrides) -> tuple:
        values = {
            "user": self.victim,
            "collateralAsset": self.collateral.address,
            "swapRouter": self.router.address,
            "swapPath": [self.collateral.address, self.debt.address],
            "amountOutMin": 1,
            "minProfit": WAD,
            "deadline": DEADLINE,
        }
        values.update(overrides)
        if values["collateralAsset"] == self.debt.address and "swapPath" not in overrides:
            values["swapPath"] = []
            values["swapRouter"] = "0x" + "00" * 20
        return (
            values["user"], values["collateralAsset"], values["swapRouter"],
            values["swapPath"], values["amountOutMin"], values["minProfit"],
            values["deadline"],
        )

    def liquidate(self, amount: int = 1000 * WAD, sender: str | None = None, **overrides):
        fn = self.bot.functions.initiateLiquidation(
            self.debt.address, amount, self.params(**overrides)
        )
        return self._send(fn, sender or self.operator)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def revert_selector(exc: Exception) -> str | None:
    """Pull the 4-byte custom-error selector out of a revert, whichever shape
    the provider reports it in (bytes attribute, hex string, or bytes repr)."""
    data = getattr(exc, "data", None)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)[:4].hex()
    if isinstance(data, str) and data.startswith("0x"):
        return data[2:10].lower()

    message = str(exc)
    literal = re.search(r"""(b'(?:[^'\\]|\\.)*'|b"(?:[^"\\]|\\.)*")""", message)
    if literal:
        try:
            return bytes(ast.literal_eval(literal.group(1)))[:4].hex()
        except (ValueError, SyntaxError):
            pass
    hexdata = re.search(r"0x([0-9a-fA-F]{8})", message)
    return hexdata.group(1).lower() if hexdata else None


def expect_revert(name: str, error_signature: str, action) -> None:
    want = selector(error_signature)
    try:
        action()
    except Exception as exc:
        got = revert_selector(exc)
        check(name, got == want,
              f"expected {error_signature} ({want}), got {got}: {str(exc)[:120]}")
        return
    check(name, False, "no revert at all")


def test_happy_path(artifacts: dict) -> None:
    print("\n[1] Full liquidation with collateral swap")
    env = Env(artifacts)
    receipt = env.liquidate()

    profit_events = env.bot.events.Profit().process_receipt(receipt, errors=DISCARD)
    executed = env.bot.events.LiquidationExecuted().process_receipt(receipt, errors=DISCARD)
    swapped = env.bot.events.CollateralSwapped().process_receipt(receipt, errors=DISCARD)

    bot_balance = env.debt.functions.balanceOf(env.bot.address).call()

    # 1000 borrowed, 0.05% premium, 5% bonus, 1:1 swap -> 49.5 profit.
    expected_profit = int(49.5 * WAD)

    check("liquidation transaction succeeded", receipt.status == 1)
    check("collateral seized with 5% bonus",
          executed[0]["args"]["collateralReceived"] == 1050 * WAD,
          f"got {executed[0]['args']['collateralReceived']}")
    check("collateral swapped back to debt asset", len(swapped) == 1)
    check("profit event matches arithmetic",
          profit_events[0]["args"]["amount"] == expected_profit,
          f"got {profit_events[0]['args']['amount']}")
    check("bot holds the profit", bot_balance == expected_profit, f"got {bot_balance}")

    env._send(env.bot.functions.withdrawToken(env.debt.address))
    check("owner withdrew profit",
          env.debt.functions.balanceOf(env.owner).call() == expected_profit)


def test_same_asset(artifacts: dict) -> None:
    print("\n[2] Collateral == debt asset (no swap, no router)")
    env = Env(artifacts, same_asset=True)
    receipt = env.liquidate()
    swapped = env.bot.events.CollateralSwapped().process_receipt(receipt, errors=DISCARD)
    balance = env.debt.functions.balanceOf(env.bot.address).call()
    check("succeeded without touching the router", receipt.status == 1 and len(swapped) == 0)
    check("profit retained", balance == int(49.5 * WAD), f"got {balance}")


def test_guards(artifacts: dict) -> None:
    print("\n[3] Guard clauses")
    env = Env(artifacts)

    expect_revert("rejects non-operator caller", "OnlyOperator()",
                  lambda: env.liquidate(sender=env.stranger))

    expect_revert("rejects debtToCover above cap", "ExceedsLimit()",
                  lambda: env.liquidate(amount=20_000 * WAD))

    expect_revert("rejects zero amount", "InvalidAmount()",
                  lambda: env.liquidate(amount=0))

    expect_revert("rejects swap path not ending in debt asset", "InvalidSwapPath()",
                  lambda: env.liquidate(
                      swapPath=[env.collateral.address, env.collateral.address]))

    expect_revert("rejects unlisted router", "RouterNotAllowed()",
                  lambda: env.liquidate(swapRouter=env.stranger))

    expect_revert("rejects profit below minProfit", "InsufficientProfit()",
                  lambda: env.liquidate(minProfit=100 * WAD))

    env._send(env.bot.functions.setPaused(True))
    expect_revert("respects pause switch", "ContractPaused()", lambda: env.liquidate())
    env._send(env.bot.functions.setPaused(False))

    env._send(env.pool.functions.setHealthFactor(WAD))
    expect_revert("skips healthy position", "PositionHealthy()", lambda: env.liquidate())
    env._send(env.pool.functions.setHealthFactor(int(0.9 * WAD)))

    expect_revert("blocks direct executeOperation call", "UnauthorizedPool()",
                  lambda: env.bot.functions.executeOperation(
                      env.debt.address, WAD, 0, env.bot.address, b"\x00"
                  ).transact({"from": env.stranger}))

    check("liquidation still works after guard probes", env.liquidate().status == 1)


def test_original_reentrancy_deadlock(artifacts_fixed: dict) -> None:
    print("\n[4] Original contract: nonReentrant on executeOperation")
    original = BOT_SRC.read_text(encoding="utf-8").replace(
        "    ) external returns (bool) {\n        if (msg.sender != address(POOL)) revert UnauthorizedPool();",
        "    ) external nonReentrant returns (bool) {\n        if (msg.sender != address(POOL)) revert UnauthorizedPool();",
    )
    if "external nonReentrant returns (bool)" not in original:
        check("could re-introduce the original modifier", False, "patch did not apply")
        return

    env = Env(compile_all(original))
    expect_revert("original code deadlocks on its own reentrancy guard",
                  "Reentrancy()", lambda: env.liquidate())

    print("  (same scenario passes on the fixed contract)")
    check("fixed contract executes the identical scenario",
          Env(artifacts_fixed).liquidate().status == 1)


def main() -> int:
    artifacts = compile_all(BOT_SRC.read_text(encoding="utf-8"))
    print(f"Compiled with solc {SOLC_VERSION} (evmVersion=paris)")

    test_happy_path(artifacts)
    test_same_asset(artifacts)
    test_guards(artifacts)
    test_original_reentrancy_deadlock(artifacts)

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
