#!/usr/bin/env python3
"""Offline checks for feeder-watch helpers."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from morpho_research.morpho_scanner import (  # noqa: E402
    DEFAULT_FEEDER_ADDRESSES,
    ERC20_TRANSFER_TOPIC,
    PRIO_FEEDER,
    PRIO_ORACLE,
    PRIO_SEED,
    MorphoChainScanner,
    _addr_topic,
)


def check(name: str, cond: bool) -> None:
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        raise SystemExit(1)


def main() -> int:
    check("transfer topic 0x-prefixed", ERC20_TRANSFER_TOPIC.startswith("0x"))
    check("default feeders >= 1", len(DEFAULT_FEEDER_ADDRESSES) >= 1)
    check("feeder prio == oracle", PRIO_FEEDER == PRIO_ORACLE)
    pad = _addr_topic(DEFAULT_FEEDER_ADDRESSES[0])
    check("addr topic 66 chars", len(pad) == 66)
    check("addr topic ends with addr", pad.endswith(DEFAULT_FEEDER_ADDRESSES[0][2:].lower()))

    # Priority mapping without constructing full scanner (needs RPC).
    class _P:
        def _prio_for_reason(self, reason: str) -> int:
            return MorphoChainScanner._prio_for_reason(self, reason)  # type: ignore[arg-type]

    p = _P()
    check("borrow -> oracle prio", p._prio_for_reason("Borrow") == PRIO_ORACLE)
    check("feeder -> oracle prio", p._prio_for_reason("feeder") == PRIO_ORACLE)
    check("seed stays low", p._prio_for_reason("seed") == PRIO_SEED)
    print("all feeder-watch unit checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
