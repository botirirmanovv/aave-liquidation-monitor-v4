"""Event topic constants and normalisation.

Every topic comparison in the project must go through topic_hex(). HexBytes.hex()
stopped emitting the 0x prefix in hexbytes 1.x, while eth_subscribe silently
never answers when a filter topic arrives without it — a mismatch that hangs the
price-feed subscription and drops every Aave log.
"""
from __future__ import annotations

from web3 import Web3

AAVE_EVENT_SIGNATURES = {
    "Supply": "Supply(address,address,address,uint256,uint16)",
    "Borrow": "Borrow(address,address,address,uint256,uint8,uint256,uint16)",
    "Repay": "Repay(address,address,address,uint256,bool)",
    "Withdraw": "Withdraw(address,address,address,uint256)",
    "LiquidationCall": (
        "LiquidationCall(address,address,address,uint256,uint256,address,bool)"
    ),
}

ANSWER_UPDATED_SIGNATURE = "AnswerUpdated(int256,uint256,uint256)"


def topic_hex(value) -> str:
    """Normalise any topic representation to lowercase 0x-prefixed hex."""
    if hasattr(value, "hex"):
        value = value.hex()
    elif isinstance(value, (bytes, bytearray)):
        value = bytes(value).hex()
    text = str(value).lower()
    return text if text.startswith("0x") else "0x" + text


def event_topic(signature: str) -> str:
    return topic_hex(Web3.keccak(text=signature))


AAVE_EVENT_TOPICS: dict[str, str] = {
    event_topic(signature): name for name, signature in AAVE_EVENT_SIGNATURES.items()
}

ANSWER_UPDATED_TOPIC: str = event_topic(ANSWER_UPDATED_SIGNATURE)
