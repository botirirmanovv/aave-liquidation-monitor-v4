"""One-shot live flash-arb scan — no WebSocket, just quotes.

    python tools/scan_flash_arb.py --chain base
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aave_bot.app import build_context, configure_logging
from aave_bot.config import load_chain_config
from aave_bot.strategies.flash_arb import AAVE_FLASH_PREMIUM_BPS, FlashArbStrategy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chain", required=True)
    args = parser.parse_args()

    configure_logging("INFO")
    config = load_chain_config(args.chain)
    config.flash_arb_enabled = True

    ctx = build_context(config)
    strategy = FlashArbStrategy(ctx)
    strategy.on_start()

    if not strategy._active:
        print("flash_arb inactive — need >=2 routers and >=2 tokens")
        print(f"  routers connected: {len(ctx.routers)}")
        print(f"  tokens resolved:   {strategy.tokens}")
        return 1

    print(f"\nscanning {config.name}: tokens="
          f"{[ctx.symbol_of(t) for t in strategy.tokens]} "
          f"routers={len(ctx.routers)} premium={AAVE_FLASH_PREMIUM_BPS}bps")

    print("\nraw round-trips (before min-profit filter):")
    from itertools import permutations
    import time as _time
    for borrow, mid in permutations(strategy.tokens, 2):
        amount = strategy._amount_for(borrow)
        if amount <= 0:
            continue
        for buy, sell in permutations(ctx.routers, 2):
            _time.sleep(0.35)
            try:
                out_buy = int(buy["quote"].functions.getAmountsOut(
                    amount, [borrow, mid]).call()[-1])
                _time.sleep(0.35)
                out_sell = int(sell["quote"].functions.getAmountsOut(
                    out_buy, [mid, borrow]).call()[-1])
            except Exception as exc:
                print(f"  {ctx.symbol_of(borrow)}->{ctx.symbol_of(mid)} "
                      f"via {buy['address'][:10]}/{sell['address'][:10]} FAIL {exc}")
                continue
            premium = (amount * AAVE_FLASH_PREMIUM_BPS) // 10_000
            profit = out_sell - amount - premium
            flag = "PROFIT" if profit > 0 else "loss"
            print(
                f"  {ctx.symbol_of(borrow):6} -> {ctx.symbol_of(mid):6} -> {ctx.symbol_of(borrow):6} "
                f"in={amount} mid={out_buy} back={out_sell} "
                f"premium={premium} net={profit} [{flag}] "
                f"buy={buy['address'][:10]} sell={sell['address'][:10]}"
            )

    print("\nstrategy.scan() (applies min-profit filter):")
    found = strategy.scan()
    if not found:
        print("  no opportunity cleared min-profit — market is tight (expected most of the time)")
    else:
        for opp in found:
            print(
                f"  ARB {ctx.symbol_of(opp.borrow_asset)}/{ctx.symbol_of(opp.mid_asset)} "
                f"amount={opp.amount} profit={opp.gross_profit} "
                f"buy={opp.router_buy} sell={opp.router_sell}"
            )
    print(f"\nopportunities_seen={strategy.opportunities_seen} scans={strategy.scans}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
