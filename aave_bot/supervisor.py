"""One OS process per chain.

Running several chains inside a single asyncio loop looked fine until a public
RPC started answering with 429s: the shared thread-pool and event loop then
starved every other chain. Isolating each chain into its own process gives it
its own event loop, its own HTTP session, and a crash boundary — exactly what
Stage 2 asked for.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import signal
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("aave_bot.supervisor")

# Cap so a permanently broken chain does not thrash forever after a misconfig.
DEFAULT_MAX_RESTARTS = 20
DEFAULT_RESTART_BACKOFF_SECONDS = 5.0


@dataclass(slots=True)
class ChildSpec:
    chain: str
    once: bool
    log_file: str | None
    log_level: str | None


@dataclass(slots=True)
class ChildHandle:
    spec: ChildSpec
    process: mp.Process
    restarts: int = 0
    started_at: float = 0.0


def chain_log_path(base: str | None, chain: str) -> str:
    """Per-chain log file. Interleaved stdout from N processes is unreadable."""
    if base:
        path = Path(base)
        return str(path.with_name(f"{path.stem}_{chain}{path.suffix or '.log'}"))
    return f"monitor_{chain}.log"


def _worker(spec: ChildSpec) -> None:
    """Child entry point. Must be a top-level function for Windows spawn."""
    # Imported inside the worker so the parent stays light and so import-time
    # side effects (dotenv load, logging) happen in the child process.
    from .app import configure_logging, run_one_chain

    configure_logging(spec.log_level, spec.log_file)
    raise SystemExit(run_one_chain(spec.chain, once=spec.once))


def _terminate(handle: ChildHandle, grace_seconds: float = 5.0) -> None:
    process = handle.process
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=grace_seconds)
    if process.is_alive():
        process.kill()
        process.join(timeout=2.0)


def run_processes(
    chains: list[str],
    *,
    once: bool = False,
    log_file: str | None = None,
    log_level: str | None = None,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    restart_backoff_seconds: float = DEFAULT_RESTART_BACKOFF_SECONDS,
) -> int:
    """Spawn one process per chain and supervise them until all exit.

    Returns 0 when every child exited cleanly, otherwise the first non-zero
    exit code observed. In continuous mode a non-zero exit triggers a restart
    until max_restarts is hit for that chain.
    """
    if not chains:
        raise ValueError("run_processes requires at least one chain name")

    # Windows defaults to spawn; be explicit so Linux behaves the same in tests.
    ctx = mp.get_context("spawn")
    handles: list[ChildHandle] = []
    stopping = False

    def spawn(spec: ChildSpec, restarts: int = 0) -> ChildHandle:
        process = ctx.Process(
            target=_worker,
            args=(spec,),
            name=f"aave-{spec.chain}",
            daemon=False,
        )
        process.start()
        handle = ChildHandle(
            spec=spec, process=process, restarts=restarts, started_at=time.monotonic()
        )
        log.info(
            "started %s pid=%s log=%s (restarts=%d)",
            spec.chain, process.pid, spec.log_file or "-", restarts,
        )
        return handle

    def request_stop(signum=None, frame=None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        log.info("supervisor stopping (%s), signalling children",
                 signal.Signals(signum).name if isinstance(signum, int) else "request")
        for handle in handles:
            _terminate(handle)

    # SIGTERM is what systemd / docker send; SIGINT is Ctrl-C. Windows has both
    # for console processes; either must bring the whole tree down.
    previous: dict[int, object] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, request_stop)
        except (ValueError, OSError):
            # Not the main thread, or the signal is unavailable on this platform.
            pass

    try:
        for chain in chains:
            handles.append(spawn(ChildSpec(
                chain=chain,
                once=once,
                log_file=chain_log_path(log_file, chain),
                log_level=log_level,
            )))

        exit_code = 0
        while handles and not stopping:
            time.sleep(0.5)
            survivors: list[ChildHandle] = []
            for handle in handles:
                if handle.process.is_alive():
                    survivors.append(handle)
                    continue

                code = handle.process.exitcode
                if code is None:
                    code = 1
                if code != 0 and exit_code == 0:
                    exit_code = code

                lived = time.monotonic() - handle.started_at
                log.warning(
                    "%s pid=%s exited with %s after %.1fs",
                    handle.spec.chain, handle.process.pid, code, lived,
                )

                if once or stopping or code == 0:
                    continue
                if handle.restarts >= max_restarts:
                    log.error(
                        "%s exceeded max restarts (%d), not restarting",
                        handle.spec.chain, max_restarts,
                    )
                    continue

                time.sleep(restart_backoff_seconds)
                if stopping:
                    continue
                survivors.append(spawn(handle.spec, restarts=handle.restarts + 1))

            handles = survivors

            if once and not handles:
                break

        return exit_code
    finally:
        for handle in handles:
            _terminate(handle)
        for sig, handler in previous.items():
            with suppress(ValueError, OSError):
                signal.signal(sig, handler)  # type: ignore[arg-type]


def available() -> bool:
    """True when this interpreter can spawn subprocesses (not always true under IDE runners)."""
    return hasattr(os, "fork") or os.name == "nt"
