"""Reconnect test that exercises the real socket path.

test_runner.py stubs _session to prove the supervisor's arithmetic. This one
lets the genuine AsyncWeb3 + WebSocketProvider code run against an endpoint that
refuses connections, so the supervisor is proven to catch what the websocket
stack actually raises rather than what the stubs pretend it raises.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from test_runner import FakeContext, make_config

from aave_bot.app import configure_logging
from aave_bot.runner import ChainRunner

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


async def scenario(label: str, ws_url: str, run_for: float) -> ChainRunner:
    print(f"\n[{label}]")
    with tempfile.TemporaryDirectory() as raw:
        config = make_config(
            Path(raw),
            ws_rpc_url=ws_url,
            ws_backoff_initial_seconds=0.2,
            ws_backoff_max_seconds=0.6,
            ws_silence_timeout_seconds=2,
        )
        ctx = FakeContext(config)
        runner = ChainRunner(ctx, None)
        task = asyncio.create_task(runner._supervise())
        await asyncio.sleep(run_for)
        runner.stop()
        try:
            await asyncio.wait_for(task, timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        return runner


async def run_all() -> None:
    refused = await scenario(
        "connection refused (nothing listening)", "ws://127.0.0.1:9/", 2.5
    )
    check("supervisor keeps retrying a refused endpoint", refused.reconnects >= 2,
          f"{refused.reconnects} attempts")
    check("process stays alive through the failures", True)

    unresolvable = await scenario(
        "dns failure", "wss://this-host-does-not-exist.invalid/ws", 2.5
    )
    check("supervisor survives DNS failures", unresolvable.reconnects >= 1,
          f"{unresolvable.reconnects} attempts")

    wrong_protocol = await scenario(
        "http endpoint given as websocket", "ws://ethereum-sepolia-rpc.publicnode.com/", 4.0
    )
    check("a non-websocket endpoint does not crash the supervisor",
          wrong_protocol.reconnects >= 1, f"{wrong_protocol.reconnects} attempts")


def main() -> int:
    configure_logging("INFO")
    asyncio.run(run_all())
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
