"""In-process EVM test for AaveV3FlashArbBot (two-router flash arbitrage)."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import solcx
from eth_tester import EthereumTester, PyEVMBackend
from web3 import EthereumTesterProvider, Web3
from web3.logs import DISCARD

SOLC_VERSION = "0.8.20"
BOT_SRC = Path("contracts/AaveV3FlashArbBot.sol")
MOCKS_SRC = Path("contracts/test/Mocks.sol")

WAD = 10 ** 18
DEADLINE = 2 ** 40
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def selector(signature: str) -> str:
    return Web3.keccak(text=signature)[:4].hex().lower().removeprefix("0x")


def revert_selector(exc: Exception) -> str | None:
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
    return None


def expect_revert(name: str, error_signature: str, action) -> None:
    want = selector(error_signature)
    try:
        action()
    except Exception as exc:
        got = revert_selector(exc)
        check(name, got == want,
              f"expected {error_signature} ({want}), got {got}: {str(exc)[:120]}")
        return
    check(name, False, "did not revert")


def compile_all() -> dict:
    if SOLC_VERSION not in [str(v) for v in solcx.get_installed_solc_versions()]:
        solcx.install_solc(SOLC_VERSION)
    std_input = {
        "language": "Solidity",
        "sources": {
            "AaveV3FlashArbBot.sol": {"content": BOT_SRC.read_text(encoding="utf-8")},
            "Mocks.sol": {"content": MOCKS_SRC.read_text(encoding="utf-8")},
        },
        "settings": {
            "evmVersion": "paris",
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
        },
    }
    out = solcx.compile_standard(std_input, solc_version=SOLC_VERSION, allow_empty=True)
    artifacts = {}
    for contracts in out["contracts"].values():
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
    def __init__(self, artifacts: dict):
        self.w3 = make_w3()
        self.owner, self.operator, self.stranger = self.w3.eth.accounts[:3]

        self.borrow = deploy(self.w3, artifacts, "MockERC20", self.owner, "BORROW")
        self.mid = deploy(self.w3, artifacts, "MockERC20", self.owner, "MID")
        self.pool = deploy(self.w3, artifacts, "MockAavePool", self.owner)
        self.router_buy = deploy(self.w3, artifacts, "MockRouter", self.owner)
        self.router_sell = deploy(self.w3, artifacts, "MockRouter", self.owner)
        self.bot = deploy(
            self.w3, artifacts, "AaveV3FlashArbBot", self.owner,
            self.pool.address, self.operator,
        )

        # Buy leg pays 10% more mid; sell leg is 1:1 — round trip clears the 0.05% premium.
        self.router_buy.functions.setRateBps(11000).transact({"from": self.owner})
        self.router_sell.functions.setRateBps(10000).transact({"from": self.owner})

        self.bot.functions.setRouterAllowed(self.router_buy.address, True).transact(
            {"from": self.owner}
        )
        self.bot.functions.setRouterAllowed(self.router_sell.address, True).transact(
            {"from": self.owner}
        )
        self.bot.functions.setMaxBorrow(self.borrow.address, 10_000 * WAD).transact(
            {"from": self.owner}
        )

        # Fund the pool (flash liquidity) and both routers (swap inventory).
        self.borrow.functions.mint(self.pool.address, 100_000 * WAD).transact({"from": self.owner})
        self.mid.functions.mint(self.router_buy.address, 100_000 * WAD).transact({"from": self.owner})
        self.borrow.functions.mint(self.router_sell.address, 100_000 * WAD).transact(
            {"from": self.owner}
        )

    def arb(self, amount: int = 1000 * WAD, **overrides):
        values = {
            "routerBuy": self.router_buy.address,
            "routerSell": self.router_sell.address,
            "pathBuy": [self.borrow.address, self.mid.address],
            "pathSell": [self.mid.address, self.borrow.address],
            "amountOutMinBuy": 0,
            "amountOutMinSell": 0,
            "minProfit": 0,
            "deadline": DEADLINE,
        }
        values.update(overrides)
        params = (
            values["routerBuy"], values["routerSell"],
            values["pathBuy"], values["pathSell"],
            values["amountOutMinBuy"], values["amountOutMinSell"],
            values["minProfit"], values["deadline"],
        )
        return self.bot.functions.initiateArb(
            self.borrow.address, amount, params
        ).transact({"from": self.operator})


def main() -> int:
    print("compiling AaveV3FlashArbBot + mocks…")
    artifacts = compile_all()
    check("bytecode produced", bool(artifacts["AaveV3FlashArbBot"][1]))

    print("\n[1] profitable two-router flash arb")
    env = Env(artifacts)
    before = env.borrow.functions.balanceOf(env.bot.address).call()
    tx = env.arb()
    receipt = env.w3.eth.wait_for_transaction_receipt(tx)
    check("tx succeeded", receipt.status == 1)
    after = env.borrow.functions.balanceOf(env.bot.address).call()
    # 1000 in, buy -> 1100 mid, sell -> 1100 back, premium 0.5, profit 99.5
    expected = 995 * WAD // 10
    check("profit left on the bot", after - before == expected,
          f"profit={after - before}")
    profits = env.bot.events.Profit().process_receipt(receipt, errors=DISCARD)
    check("Profit event emitted", len(profits) == 1)

    print("\n[2] guard clauses")
    env = Env(artifacts)
    expect_revert("rejects same router", "SameRouter()",
                  lambda: env.arb(routerBuy=env.router_buy.address,
                                  routerSell=env.router_buy.address))
    expect_revert("rejects unallowlisted router", "RouterNotAllowed()",
                  lambda: env.arb(routerSell=env.stranger))
    expect_revert("rejects broken path", "InvalidPath()",
                  lambda: env.arb(pathSell=[env.mid.address, env.mid.address]))
    expect_revert("rejects stranger caller", "OnlyOperator()",
                  lambda: env.bot.functions.initiateArb(
                      env.borrow.address, WAD,
                      (env.router_buy.address, env.router_sell.address,
                       [env.borrow.address, env.mid.address],
                       [env.mid.address, env.borrow.address],
                       0, 0, 0, DEADLINE),
                  ).transact({"from": env.stranger}))

    print("\n[3] unprofitable route reverts cleanly")
    env = Env(artifacts)
    env.router_buy.functions.setRateBps(10000).transact({"from": env.owner})
    expect_revert("1:1 round trip cannot cover premium", "InsufficientProfit()",
                  lambda: env.arb())

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
