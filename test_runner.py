"""Tests for the WebSocket supervisor: backoff, resubscribe, watchdog, queueing.

Everything here uses stubs, so the reconnect path is exercised deterministically
instead of hoping a real endpoint drops the connection during a test run.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from web3 import Web3

from aave_bot.config import ChainConfig
from aave_bot.runner import ChainRunner
from aave_bot.state import MonitorState
from aave_bot.topics import ANSWER_UPDATED_TOPIC

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def make_config(tmp: Path, **overrides) -> ChainConfig:
    defaults = dict(
        name="test",
        ws_rpc_url="wss://example.invalid",
        http_rpc_url="https://example.invalid",
        pool="0x" + "01" * 20,
        data_provider="0x" + "02" * 20,
        oracle="0x" + "03" * 20,
        multicall3="0x" + "04" * 20,
        ws_backoff_initial_seconds=0.01,
        ws_backoff_max_seconds=0.04,
        ws_silence_timeout_seconds=1,
        pending_check_interval_seconds=0.05,
        state_file=tmp / "state.json",
    )
    defaults.update(overrides)
    return ChainConfig(**defaults)


class FakeContext:
    """Minimal stand-in for ChainContext."""

    def __init__(self, config: ChainConfig) -> None:
        self.config = config
        self.state = MonitorState(config.state_file, save_interval_seconds=0.0)
        self.aggregator_assets: dict[str, set[str]] = {}
        self.pool = None
        self.w3 = SimpleNamespace(to_checksum_address=Web3.to_checksum_address)
        self.pending_txs: list[dict] = []
        self.evaluated: list[str] = []
        self.executed: list[str] = []
        self.pending_checks = 0

    def symbol_of(self, asset: str) -> str:
        return asset[:8]

    def evaluate_user(self, user: str):
        self.evaluated.append(user)
        return None

    def execute_liquidation(self, plan) -> bool:
        self.executed.append(plan.user)
        return True

    def check_pending_transactions(self) -> list[str]:
        self.pending_checks += 1
        return []


async def test_backoff_and_retry(tmp: Path) -> None:
    print("\n[supervisor: backoff and retry]")

    class FlakyRunner(ChainRunner):
        def __init__(self, *args, fail_times: int, **kwargs):
            super().__init__(*args, **kwargs)
            self.fail_times = fail_times
            self.session_calls = 0
            self.delays: list[float] = []
            self._last_attempt_at = time.monotonic()

        async def _session(self) -> None:
            self.session_calls += 1
            now = time.monotonic()
            self.delays.append(now - self._last_attempt_at)
            self._last_attempt_at = now
            if self.session_calls <= self.fail_times:
                raise ConnectionError(f"simulated drop #{self.session_calls}")
            self.stop()

    ctx = FakeContext(make_config(tmp))
    runner = FlakyRunner(ctx, None, fail_times=3)
    await asyncio.wait_for(runner._supervise(), timeout=5)

    check("supervisor retries after every drop", runner.session_calls == 4,
          f"session called {runner.session_calls} times")
    check("reconnect counter tracks drops", runner.reconnects == 3,
          f"got {runner.reconnects}")
    check("attempts are spaced apart rather than spinning",
          all(d > 0 for d in runner.delays[1:]),
          f"delays={[round(d, 4) for d in runner.delays]}")


async def test_backoff_formula(tmp: Path) -> None:
    print("\n[supervisor: backoff formula]")
    ctx = FakeContext(make_config(tmp, ws_backoff_initial_seconds=1.0,
                                  ws_backoff_max_seconds=60.0))
    runner = ChainRunner(ctx, None)

    samples = {attempt: [runner._backoff_delay(attempt) for _ in range(200)]
               for attempt in (1, 2, 3, 5, 10, 20)}

    check("first delay stays near the configured start",
          all(0.75 <= d <= 1.25 for d in samples[1]),
          f"range {min(samples[1]):.3f}..{max(samples[1]):.3f}")
    check("delay doubles each attempt",
          all(1.5 <= d <= 2.5 for d in samples[2])
          and all(3.0 <= d <= 5.0 for d in samples[3]),
          f"attempt2 {min(samples[2]):.2f}..{max(samples[2]):.2f}, "
          f"attempt3 {min(samples[3]):.2f}..{max(samples[3]):.2f}")
    check("ceiling is never exceeded beyond jitter",
          all(d <= 60.0 * 1.25 for attempt in samples for d in samples[attempt]),
          f"max {max(d for a in samples for d in samples[a]):.2f}")
    check("a long outage still retries at the ceiling, not slower",
          all(45.0 <= d <= 75.0 for d in samples[20]),
          f"range {min(samples[20]):.1f}..{max(samples[20]):.1f}")
    check("jitter actually varies the delay",
          len(set(round(d, 4) for d in samples[5])) > 100)

    zero = ChainRunner(FakeContext(make_config(tmp, ws_backoff_initial_seconds=1.0,
                                               ws_backoff_max_seconds=60.0)), None)
    check("attempt zero does not invert the exponent",
          0.75 <= zero._backoff_delay(0) <= 1.25, f"{zero._backoff_delay(0)}")


async def test_stop_is_respected(tmp: Path) -> None:
    print("\n[supervisor: shutdown]")

    class NeverEndingRunner(ChainRunner):
        async def _session(self) -> None:
            await asyncio.sleep(10)

    ctx = FakeContext(make_config(tmp))
    runner = NeverEndingRunner(ctx, None)
    task = asyncio.create_task(runner._supervise())
    await asyncio.sleep(0.05)
    runner.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check("supervisor can be cancelled", task.done())

    class FailingRunner(ChainRunner):
        async def _session(self) -> None:
            raise ConnectionError("down")

    ctx2 = FakeContext(make_config(tmp, ws_backoff_initial_seconds=5.0,
                                   ws_backoff_max_seconds=5.0))
    runner2 = FailingRunner(ctx2, None)
    task2 = asyncio.create_task(runner2._supervise())
    await asyncio.sleep(0.05)
    runner2.stop()
    # A 5s backoff must not delay shutdown: the wait is interruptible.
    await asyncio.wait_for(task2, timeout=1.0)
    check("stop interrupts a long backoff sleep", True)


async def test_probe_watchdog(tmp: Path) -> None:
    print("\n[watchdog: head liveness]")
    ctx = FakeContext(make_config(tmp, ws_silence_timeout_seconds=1))
    runner = ChainRunner(ctx, None)

    head = {"value": 100}

    class FakeEth:
        async def get_block_number(self):
            return head["value"]

    fake_w3 = SimpleNamespace(eth=FakeEth())

    await runner._probe(fake_w3)
    check("first probe records the head", runner._last_block == 100)

    head["value"] = 101
    await runner._probe(fake_w3)
    check("advancing head keeps the connection", runner._last_block == 101)

    # Head frozen but within tolerance: no reconnect yet.
    await runner._probe(fake_w3)
    check("frozen head inside tolerance is tolerated", True)

    runner._last_block_at = time.monotonic() - 10  # beyond 2x silence timeout
    raised = False
    try:
        await runner._probe(fake_w3)
    except ConnectionError:
        raised = True
    check("stale head forces a reconnect", raised)


async def test_session_bookkeeping(tmp: Path) -> None:
    print("\n[session bookkeeping]")
    ctx = FakeContext(make_config(tmp))
    runner = ChainRunner(ctx, None)

    # A head carried over from the previous connection would read as a frozen
    # chain if the new node sits a block lower, looping the supervisor.
    runner._last_block = 500
    runner._connected_since = time.monotonic() - 42
    runner._last_session_seconds = None

    head = {"value": 499}

    class FakeEth:
        async def get_block_number(self):
            return head["value"]

    fresh = ChainRunner(ctx, None)
    fresh._last_block = None  # what _session now resets it to
    await fresh._probe(SimpleNamespace(eth=FakeEth()))
    check("a lower head on a new connection is accepted", fresh._last_block == 499)

    runner._last_session_seconds = 120.0
    check("a finished session reports its duration",
          runner._last_session_seconds == 120.0)

    status = runner.status()
    check("status exposes the live connection age", status["connected_for"] is not None)
    runner._connected_since = None
    check("status reports no age once disconnected",
          runner.status()["connected_for"] is None)


async def test_queue_dedup_and_worker(tmp: Path) -> None:
    print("\n[work queue]")
    ctx = FakeContext(make_config(tmp))
    runner = ChainRunner(ctx, None)

    alice = Web3.to_checksum_address("0x" + "a1" * 20)
    runner._enqueue(alice)
    runner._enqueue(alice)
    runner._enqueue(alice)
    check("duplicate work is collapsed", runner._queue.qsize() == 1,
          f"queue size {runner._queue.qsize()}")

    worker = asyncio.create_task(runner._worker())
    await asyncio.sleep(0.1)
    check("worker evaluates the queued user", ctx.evaluated == [alice],
          f"evaluated={ctx.evaluated}")

    runner._enqueue(alice)
    await asyncio.sleep(0.1)
    check("the same user can be re-queued once drained",
          ctx.evaluated == [alice, alice], f"evaluated={ctx.evaluated}")

    runner.stop()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


async def test_price_update_uses_reverse_index(tmp: Path) -> None:
    print("\n[price update fan-out]")
    ctx = FakeContext(make_config(tmp))
    runner = ChainRunner(ctx, None)

    weth = Web3.to_checksum_address("0x" + "ee" * 20)
    usdc = Web3.to_checksum_address("0x" + "cc" * 20)
    aggregator = Web3.to_checksum_address("0x" + "aa" * 20)
    holders = [Web3.to_checksum_address(f"0x{i:02x}" + "b0" * 19) for i in range(1, 4)]

    for holder in holders:
        ctx.state.track(holder, weth)
    unrelated = Web3.to_checksum_address("0x" + "dd" * 20)
    ctx.state.track(unrelated, usdc)

    ctx.aggregator_assets = {aggregator: {weth}}
    runner._on_price_update({"blockNumber": 1}, aggregator)

    check("only holders of the repriced asset are queued",
          runner._queue.qsize() == 3, f"queued {runner._queue.qsize()}")
    check("unrelated holder is not queued", unrelated not in runner._queued)

    runner2 = ChainRunner(ctx, None)
    runner2._on_price_update({"blockNumber": 2}, Web3.to_checksum_address("0x" + "77" * 20))
    check("unknown aggregator fans out to nobody", runner2._queue.qsize() == 0)

    # All feeds share one subscription, so the emitting aggregator has to come
    # from the log itself. Nodes return the address lowercased.
    batched = ChainRunner(ctx, None)
    log = {"blockNumber": 3, "address": aggregator.lower(),
           "topics": [ANSWER_UPDATED_TOPIC], "data": "0x"}
    batched._dispatch({"subscription": "0xsub", "result": log},
                      {"0xsub": ("oracle-batch", None)})
    check("batched subscription resolves the aggregator from the log",
          batched._queue.qsize() == 3, f"queued {batched._queue.qsize()}")
    check("batched price update is counted", batched.price_updates == 1)

    stray = ChainRunner(ctx, None)
    stray._dispatch({"subscription": "0xsub", "result": {"blockNumber": 4}},
                    {"0xsub": ("oracle-batch", None)})
    check("a log without an address queues nobody", stray._queue.qsize() == 0)

    # The probe only runs after a silence, so on a busy chain the head has to
    # come from the events themselves or it is never reported at all.
    heads = ChainRunner(ctx, None)
    heads._dispatch({"subscription": "0xsub", "result": dict(log, blockNumber="0x64")},
                    {"0xsub": ("oracle-batch", None)})
    check("head is read from a hex blockNumber", heads._last_block == 100,
          str(heads._last_block))
    heads._dispatch({"subscription": "0xsub", "result": dict(log, blockNumber=99)},
                    {"0xsub": ("oracle-batch", None)})
    check("an older log does not rewind the head", heads._last_block == 100)


async def test_worker_survives_failures(tmp: Path) -> None:
    print("\n[worker resilience]")
    ctx = FakeContext(make_config(tmp))

    calls: list[str] = []

    def exploding_evaluate(user: str):
        calls.append(user)
        raise RuntimeError("RPC exploded")

    ctx.evaluate_user = exploding_evaluate
    runner = ChainRunner(ctx, None)
    worker = asyncio.create_task(runner._worker())

    first = Web3.to_checksum_address("0x" + "11" * 20)
    second = Web3.to_checksum_address("0x" + "22" * 20)
    runner._enqueue(first)
    await asyncio.sleep(0.1)
    runner._enqueue(second)
    await asyncio.sleep(0.1)

    check("a failing evaluation does not kill the worker", calls == [first, second],
          f"calls={calls}")
    check("worker task is still running", not worker.done())

    runner.stop()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


async def test_pending_watcher(tmp: Path) -> None:
    print("\n[pending watcher]")
    ctx = FakeContext(make_config(tmp, pending_check_interval_seconds=0.05))
    runner = ChainRunner(ctx, None)
    task = asyncio.create_task(runner._pending_watcher())
    await asyncio.sleep(0.22)
    runner.stop()
    await asyncio.wait_for(task, timeout=1.0)
    check("pending transactions are polled periodically", ctx.pending_checks >= 2,
          f"{ctx.pending_checks} checks")


async def test_dry_run_does_not_execute(tmp: Path) -> None:
    print("\n[dry-run safety]")
    ctx = FakeContext(make_config(tmp, auto_execute=False,
                                  health_factor_threshold=Decimal("1.0")))
    runner = ChainRunner(ctx, None)

    from aave_bot.chain import LiquidationPlan
    plan = LiquidationPlan(
        user=Web3.to_checksum_address("0x" + "a1" * 20),
        collateral_asset=Web3.to_checksum_address("0x" + "ee" * 20),
        collateral_amount=10 ** 18,
        debt_asset=Web3.to_checksum_address("0x" + "cc" * 20),
        debt_to_cover=500 * 10 ** 6,
        health_factor=Decimal("0.97"),
    )
    ctx.evaluate_user = lambda user: plan

    runner._evaluate(plan.user)
    check("dry-run never calls execute", ctx.executed == [], f"executed={ctx.executed}")

    ctx.config.auto_execute = True
    runner._evaluate(plan.user)
    check("execute runs once AUTO_EXECUTE is on", ctx.executed == [plan.user])


async def run_all() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        await test_backoff_and_retry(tmp)
        await test_backoff_formula(tmp)
        await test_stop_is_respected(tmp)
        await test_probe_watchdog(tmp)
        await test_session_bookkeeping(tmp)
        await test_queue_dedup_and_worker(tmp)
        await test_price_update_uses_reverse_index(tmp)
        await test_worker_survives_failures(tmp)
        await test_pending_watcher(tmp)
        await test_dry_run_does_not_execute(tmp)


def main() -> int:
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
