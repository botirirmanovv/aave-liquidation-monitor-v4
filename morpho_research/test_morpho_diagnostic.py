#!/usr/bin/env python3
"""Offline checks for diagnostic duration + net_profit math. No RPC send."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT))

from morpho_executor import duration_is_forever  # noqa: E402
from morpho_hf import (  # noqa: E402
    MORPHO_LIQUIDATE_FLASH_FEE_USD,
    estimate_liquidation_profit_usd,
    estimate_net_profit_usd,
    estimate_priority_gas_usd,
)
from morpho_markets import markets_for_chain  # noqa: E402
from morpho_scanner import (  # noqa: E402
    classify_ws_event,
    parse_args,
    resolved_duration_seconds,
)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def main() -> int:
    print("morpho diagnostic unit tests")
    a0 = parse_args(["--chains", "base"])
    check("duration omitted is None", a0.duration_seconds is None)
    check("omitted resolves to forever 0", resolved_duration_seconds(a0) == 0)
    a1 = parse_args(["--duration-seconds", "0"])
    check("duration 0 is forever", duration_is_forever(a1.duration_seconds))
    a2 = parse_args(["--duration-seconds", "1800"])
    check("explicit 1800 stays bounded", resolved_duration_seconds(a2) == 1800)
    a3 = parse_args(["--diagnostic-mode", "--min-net-profit-usd", "0.05"])
    check("diagnostic flag parsed", a3.diagnostic_mode is True)
    check("min net 0.05 parsed", a3.min_net_profit_usd == 0.05)

    check("flash fee documented 0", MORPHO_LIQUIDATE_FLASH_FEE_USD == 0.0)
    m = markets_for_chain("base")[0]
    bonus = estimate_liquidation_profit_usd(50.0, m.lltv_wad)
    gas = estimate_priority_gas_usd(gas_limit=800_000, priority_gwei=0.05, eth_usd=3500.0)
    br = estimate_net_profit_usd(50.0, m.lltv_wad, gas_cost_usd=gas, slippage_bps=200)
    check("bonus > 0 on $50", bonus > 0)
    check("gas USD > 0 from limit*priority*eth", gas > 0)
    check("flash fee in breakdown is 0", br.flash_fee_usd == 0.0)
    check("net_profit_usd field present", br.net_profit_usd == br.after_swap_usd - 50.0 - 0.0 - gas)
    tiny = estimate_net_profit_usd(0.01, m.lltv_wad, gas_cost_usd=1.0, slippage_bps=200)
    check("tiny debt can fail min net", tiny.net_profit_usd < 0.05)

    check("known borrow is core", classify_ws_event("Borrow", True) == "core")
    check("unknown borrow is drop", classify_ws_event("Borrow", False) == "drop")
    check(
        "unknown Liquidate is discovery",
        classify_ws_event("Liquidate", False) == "discovery",
    )
    check("known Liquidate is core", classify_ws_event("Liquidate", True) == "core")

    import morpho_scanner as ms

    def _fake_graphql(_query: str, _variables: dict | None = None) -> dict:
        return {
            "markets": {
                "items": [
                    {
                        "lltv": "625000000000000000",
                        "loanAsset": {
                            "symbol": "USDC",
                            "decimals": 6,
                            "priceUsd": 1.0,
                        },
                        "collateralAsset": {"symbol": "TAIL"},
                        "state": {},
                    }
                ]
            }
        }

    orig = ms._graphql
    ms._graphql = _fake_graphql  # type: ignore[method-assign]
    try:
        meta = ms.lookup_discovery_market("0x" + "ab" * 32, 8453)
    finally:
        ms._graphql = orig
    check("discovery meta pair", bool(meta) and meta["pair"] == "USDC/TAIL")
    check("discovery meta decimals", bool(meta) and meta["decimals"] == 6)

    if failures:
        print(f"{len(failures)} FAIL")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
