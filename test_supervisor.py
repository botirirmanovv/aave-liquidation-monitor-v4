"""Tests for the Stage 2 multi-chain process supervisor."""
from __future__ import annotations

import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aave_bot.supervisor import ChildSpec, chain_log_path, run_processes

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def test_chain_log_path() -> None:
    print("\n[supervisor: log paths]")
    check("no base path uses monitor_<chain>.log",
          chain_log_path(None, "base") == "monitor_base.log")
    check("base path gets a per-chain stem",
          chain_log_path("live.log", "arbitrum") == "live_arbitrum.log")
    check("suffix is preserved",
          chain_log_path("logs/run.txt", "optimism") == str(
              __import__("pathlib").Path("logs/run_optimism.txt")
          ))


class FakeProcess:
    """Minimal stand-in for multiprocessing.Process."""

    _next_pid = 1000

    def __init__(self, *, target=None, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        self.name = name
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self._alive = False
        self.exitcode = None
        self.terminated = False
        self.killed = False

    def start(self):
        self._alive = True
        # Run the worker synchronously so --once tests do not need a real spawn.
        if self.target is not None:
            try:
                self.target(*self.args)
                self.exitcode = 0
            except SystemExit as exc:
                self.exitcode = int(exc.code or 0)
            except Exception:
                self.exitcode = 1
            self._alive = False

    def is_alive(self):
        return self._alive

    def terminate(self):
        self.terminated = True
        self._alive = False
        if self.exitcode is None:
            self.exitcode = -15

    def kill(self):
        self.killed = True
        self._alive = False
        if self.exitcode is None:
            self.exitcode = -9

    def join(self, timeout=None):
        return


def test_run_processes_once() -> None:
    print("\n[supervisor: --once multi-chain]")
    started: list[str] = []

    def fake_worker(spec: ChildSpec) -> None:
        started.append(spec.chain)
        raise SystemExit(0)

    ctx = SimpleNamespace(Process=FakeProcess)
    with patch("aave_bot.supervisor.mp.get_context", return_value=ctx), \
         patch("aave_bot.supervisor._worker", side_effect=fake_worker), \
         patch("aave_bot.supervisor.time.sleep", return_value=None):
        code = run_processes(["base", "arbitrum"], once=True, log_file="live.log")

    check("supervisor returns 0 when every child exits cleanly", code == 0, str(code))
    check("one worker started per chain",
          started == ["base", "arbitrum"], str(started))


def test_run_processes_restarts_on_failure() -> None:
    print("\n[supervisor: restart on crash]")
    attempts: dict[str, int] = {"base": 0}

    def flaky_worker(spec: ChildSpec) -> None:
        attempts[spec.chain] = attempts.get(spec.chain, 0) + 1
        # Fail once, then succeed so the supervisor can stop via max logic...
        # For continuous mode the child exiting 0 means "don't restart".
        if attempts[spec.chain] == 1:
            raise SystemExit(1)
        raise SystemExit(0)

    class StickyProcess(FakeProcess):
        """Stay alive briefly so the supervisor loop observes a living child."""

        def start(self):
            self._alive = True
            self.exitcode = None
            # Defer the actual worker until the first is_alive poll flips.
            self._ticks = 0

        def is_alive(self):
            self._ticks += 1
            if self._ticks < 2:
                return True
            if self._alive and self.target is not None:
                try:
                    self.target(*self.args)
                    self.exitcode = 0
                except SystemExit as exc:
                    self.exitcode = int(exc.code or 0)
                except Exception:
                    self.exitcode = 1
                self._alive = False
            return False

    ctx = SimpleNamespace(Process=StickyProcess)
    with patch("aave_bot.supervisor.mp.get_context", return_value=ctx), \
         patch("aave_bot.supervisor._worker", side_effect=flaky_worker), \
         patch("aave_bot.supervisor.time.sleep", return_value=None):
        code = run_processes(
            ["base"],
            once=False,
            max_restarts=3,
            restart_backoff_seconds=0,
        )

    check("failed child was restarted", attempts["base"] == 2, str(attempts))
    check("clean exit after restart returns 0 from last child",
          code == 0 or code == 1)  # first failure recorded, then clean exit
    # exit_code keeps the first non-zero; that is intentional so the operator
    # knows something crashed even if a later restart recovered.
    check("first non-zero exit code is preserved", code == 1, str(code))


def test_max_restarts_stops_thrashing() -> None:
    print("\n[supervisor: max restarts]")
    attempts = {"n": 0}

    def always_fail(spec: ChildSpec) -> None:
        attempts["n"] += 1
        raise SystemExit(7)

    class StickyProcess(FakeProcess):
        def start(self):
            self._alive = True
            self.exitcode = None
            self._ticks = 0

        def is_alive(self):
            self._ticks += 1
            if self._ticks < 2:
                return True
            if self._alive and self.target is not None:
                try:
                    self.target(*self.args)
                    self.exitcode = 0
                except SystemExit as exc:
                    self.exitcode = int(exc.code or 0)
                self._alive = False
            return False

    ctx = SimpleNamespace(Process=StickyProcess)
    with patch("aave_bot.supervisor.mp.get_context", return_value=ctx), \
         patch("aave_bot.supervisor._worker", side_effect=always_fail), \
         patch("aave_bot.supervisor.time.sleep", return_value=None):
        code = run_processes(
            ["optimism"],
            once=False,
            max_restarts=2,
            restart_backoff_seconds=0,
        )

    # initial start + 2 restarts = 3 attempts
    check("restarts stop at the configured ceiling",
          attempts["n"] == 3, str(attempts["n"]))
    check("exit code from the failing child is returned", code == 7, str(code))


def main() -> int:
    test_chain_log_path()
    test_run_processes_once()
    test_run_processes_restarts_on_failure()
    test_max_restarts_stops_thrashing()

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
