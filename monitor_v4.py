"""Aave V3 Liquidation Monitor v4 — Event-Driven Edition.

Entry point kept for backwards compatibility; the implementation lives in the
aave_bot package:

    aave_bot/config.py      settings, per-chain env prefixes
    aave_bot/topics.py      event topic normalisation
    aave_bot/decimals.py    strict decimals resolution
    aave_bot/state.py       persistence + asset -> users reverse index
    aave_bot/chain.py       RPC clients, contracts, liquidation decisions
    aave_bot/simulate.py    pre-broadcast transaction simulation
    aave_bot/runner.py      WebSocket loop with reconnect and watchdog
    aave_bot/supervisor.py  one OS process per chain (Stage 2)
    aave_bot/strategies/    pluggable strategies (Stage 3: flash_arb)
    aave_bot/alerts.py      Telegram notifications

Usage:
    python monitor_v4.py                          CHAINS=... or single unprefixed chain
    python monitor_v4.py --once                   startup self-check, no event loop
    python monitor_v4.py --chain base             one chain (BASE_* env vars)
    python monitor_v4.py --chain base --chain arbitrum --chain optimism

Flash arb (observation):
    FLASH_ARB_ENABLED=true
    two ROUTER_ADDRESSES (UniswapV2-compatible venues)
    FLASH_ARB_TOKENS=USDC,WETH
    deploy contracts/AaveV3FlashArbBot.sol before AUTO_EXECUTE
"""
import multiprocessing
import sys

from aave_bot.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
