"""Transaction simulation.

Nothing is broadcast until the exact calldata has been replayed with eth_call
against the latest block and priced with estimate_gas. A liquidation that lost
its race reverts here for free instead of burning gas on-chain, and the gas cost
this produces feeds the net-profit decision.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from eth_abi import decode as abi_decode
from web3 import Web3

log = logging.getLogger("aave_bot.simulate")

SOLIDITY_ERROR_STRING = "0x08c379a0"  # Error(string)
SOLIDITY_PANIC = "0x4e487b71"         # Panic(uint256)

PANIC_REASONS = {
    0x01: "assert failed",
    0x11: "arithmetic overflow/underflow",
    0x12: "division by zero",
    0x21: "invalid enum conversion",
    0x31: "pop on empty array",
    0x32: "array index out of bounds",
    0x41: "out of memory",
    0x51: "call to uninitialised function pointer",
}

# Deliberately excludes the gas price fields. When a price is supplied without an
# explicit gas limit, geth applies its 2^64-1 cap and then demands
# balance >= gasLimit * price, so a perfectly valid call is rejected with
# "insufficient funds" no matter how well funded the sender is. Price belongs in
# the profit calculation, not in the revert check.
CALL_FIELDS = ("from", "to", "data", "value", "gas")


def build_error_table(signatures: list[str]) -> dict[str, str]:
    """Maps 4-byte selectors to human-readable custom error signatures."""
    table: dict[str, str] = {}
    for signature in signatures:
        selector = Web3.keccak(text=signature)[:4].hex()
        table["0x" + selector.removeprefix("0x")] = signature
    return table


def _as_hex(data) -> str:
    if data is None:
        return ""
    if isinstance(data, (bytes, bytearray)):
        return "0x" + bytes(data).hex()
    text = str(data)
    return text if text.startswith("0x") else "0x" + text


def decode_revert(data, error_table: dict[str, str] | None = None) -> str:
    """Best-effort human-readable revert reason from raw revert data."""
    payload = _as_hex(data).lower()
    if len(payload) < 10:
        return "revert without data"

    selector = payload[:10]
    body = bytes.fromhex(payload[10:]) if len(payload) > 10 else b""

    if selector == SOLIDITY_ERROR_STRING:
        try:
            return f'revert "{abi_decode(["string"], body)[0]}"'
        except Exception:
            return "revert Error(string) (undecodable)"

    if selector == SOLIDITY_PANIC:
        try:
            code = abi_decode(["uint256"], body)[0]
            return f"panic 0x{code:02x}: {PANIC_REASONS.get(code, 'unknown')}"
        except Exception:
            return "revert Panic(uint256) (undecodable)"

    if error_table and selector in error_table:
        return f"revert {error_table[selector]}"

    return f"revert with unknown selector {selector}"


def _extract_revert_data(exc: Exception):
    data = getattr(exc, "data", None)
    if data not in (None, "", b""):
        return data
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, dict) and "data" in arg:
            return arg["data"]
        if isinstance(arg, (bytes, bytearray)):
            return arg
        if isinstance(arg, str) and arg.startswith("0x") and len(arg) >= 10:
            return arg
    return None


@dataclass(slots=True)
class SimulationResult:
    ok: bool
    reason: str = ""
    gas_limit: int | None = None
    gas_price_wei: int | None = None

    @property
    def gas_cost_wei(self) -> int | None:
        if self.gas_limit is None or self.gas_price_wei is None:
            return None
        return self.gas_limit * self.gas_price_wei


def effective_gas_price(tx: dict) -> int | None:
    for key in ("maxFeePerGas", "gasPrice"):
        value = tx.get(key)
        if value is not None:
            return int(value)
    return None


def simulate_transaction(
    w3: Web3,
    tx: dict,
    error_table: dict[str, str] | None = None,
    gas_limit_buffer: float = 1.25,
) -> SimulationResult:
    """Replay `tx` read-only, then price it. Never raises."""
    call_tx = {key: tx[key] for key in CALL_FIELDS if key in tx}

    try:
        w3.eth.call(call_tx, "latest")
    except Exception as exc:
        revert_data = _extract_revert_data(exc)
        if revert_data is not None:
            return SimulationResult(False, decode_revert(revert_data, error_table))
        return SimulationResult(False, f"eth_call failed: {type(exc).__name__}: {exc}")

    # A passing eth_call can still be unpriceable, e.g. when the node applies a
    # stricter gas cap than the call path does.
    try:
        estimated = w3.eth.estimate_gas(call_tx, "latest")
    except Exception as exc:
        revert_data = _extract_revert_data(exc)
        reason = (
            decode_revert(revert_data, error_table)
            if revert_data is not None
            else f"estimate_gas failed: {type(exc).__name__}: {exc}"
        )
        return SimulationResult(False, reason)

    gas_limit = int(int(estimated) * gas_limit_buffer)
    return SimulationResult(
        ok=True,
        gas_limit=gas_limit,
        gas_price_wei=effective_gas_price(tx),
    )
