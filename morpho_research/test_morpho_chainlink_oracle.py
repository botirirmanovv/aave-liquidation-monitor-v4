"""Chainlink AnswerUpdated → Morpho oracle projection (no RPC)."""
from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
for p in (_DIR, _ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from morpho_hf import parse_answer_updated_current, project_morpho_price  # noqa: E402


def test_parse_answer_updated_current() -> None:
    # int256 indexed current in topics[1]
    current = 5_000_000_000  # 8 decimals example
    topic1 = current.to_bytes(32, byteorder="big", signed=True)
    entry = {
        "topics": [
            b"\x00" * 32,
            topic1,
            (1).to_bytes(32, "big"),
        ],
        "data": b"\x00" * 32,
    }
    assert parse_answer_updated_current(entry) == current


def test_project_morpho_single_base_feed_drop() -> None:
    """XRP/USD drop → Morpho cbXRP price scales linearly."""
    base = ["0x92a7c3a57e17aff701c159c5480073b095100b62"]
    old_answers = {base[0]: 10_000_000_000}
    new_answers = {base[0]: 9_000_000_000}  # -10%
    old_morpho = 1_000_000_000_000_000_000_000_000  # 1e24-ish
    projected = project_morpho_price(
        old_morpho, old_answers, new_answers, base, []
    )
    assert projected is not None
    assert projected == old_morpho * 9 // 10


def test_project_morpho_base_over_quote() -> None:
    base = ["0xbase"]
    quote = ["0xquote"]
    old_answers = {"0xbase": 2000, "0xquote": 1000}
    new_answers = {"0xbase": 1800, "0xquote": 1000}
    old_morpho = 1_000_000
    projected = project_morpho_price(
        old_morpho, old_answers, new_answers, base, quote
    )
    assert projected == 900_000
