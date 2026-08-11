"""Process entry point: wire up chains and run their event loops."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import suppress

from .alerts import Notifier
from .chain import ChainContext
from .config import (
    ChainConfig,
    ConfigError,
    configured_chains,
    load_chain_config,
    load_telegram_config,
    log_file,
    log_level,
)
from .runner import ChainRunner

log = logging.getLogger("aave_bot.app")


def configure_logging(level: str | None = None, file_path: str | None = None) -> None:
    # Windows consoles default to a legacy code page (cp1251 here). Aave token
    # symbols like USD₮ then crash the StreamHandler mid-heartbeat. Prefer UTF-8
    # and fall back to replacing unencodable characters rather than dying.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")

    stream_handler = logging.StreamHandler(sys.stdout)
    if hasattr(stream_handler.stream, "reconfigure"):
        with suppress(Exception):
            stream_handler.stream.reconfigure(encoding="utf-8", errors="replace")

    handlers: list[logging.Handler] = [stream_handler]

    # Shell redirection is not a reliable log sink: PowerShell reflows a native
    # process's output to the console width, which silently truncates every line
    # past ~120 characters — exactly the heartbeat counters worth reading later.
    path = file_path or log_file()
    if path:
        handlers.append(logging.FileHandler(path, encoding="utf-8"))

    logging.basicConfig(
        level=level or log_level(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # web3 logs every subscription payload at INFO, which drowns our own output.
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if path:
        logging.getLogger("aave_bot.app").info("logging to %s", path)


def build_context(config: ChainConfig) -> ChainContext:
    ctx = ChainContext(config)
    ctx.connect()
    ctx.warm_caches()
    ctx.discover_price_feeds()
    return ctx


def describe(ctx: ChainContext) -> None:
    cfg = ctx.config
    feeds = len(ctx.aggregator_assets)
    log.info(
        "[%s] ready: %d reserves, %d price feeds, %d tracked users, "
        "auto_execute=%s, simulate=%s",
        cfg.name, len(ctx.all_reserves()), feeds, len(ctx.state),
        cfg.auto_execute, cfg.simulate_before_send,
    )


def report_feed_activity(ctx: ChainContext, blocks: int = 5_000) -> None:
    """Startup diagnostic: which watched feeds have actually been publishing."""
    activity = ctx.price_feed_activity(blocks)
    if not activity:
        return

    live = sum(1 for count in activity.values() if count > 0)
    unknown = sum(1 for count in activity.values() if count < 0)
    for aggregator, count in sorted(activity.items(), key=lambda kv: -kv[1]):
        assets = ", ".join(sorted(ctx.symbol_of(a) for a in ctx.aggregator_assets[aggregator]))
        state = "unknown" if count < 0 else f"{count} update(s)"
        log.info("  feed %-14s %s  %s", assets, aggregator, state)

    if live == 0 and unknown < len(activity):
        log.warning(
            "[%s] none of the %d watched feeds published in the last %d blocks. "
            "Aave testnet deployments use mock aggregators with a fixed price, so "
            "price-driven liquidations cannot trigger here — only Aave user events will.",
            ctx.config.name, len(activity), blocks,
        )
    else:
        log.info("[%s] %d of %d feeds are publishing", ctx.config.name, live, len(activity))


async def run_chain_async(chain: str | None, once: bool = False) -> int:
    """Run a single chain inside the current event loop.

    Each multi-chain child process calls this with exactly one chain name, so a
    wedged RPC on Base cannot starve Arbitrum's loop.
    """
    telegram = load_telegram_config()
    label = chain or "default"
    notifier = Notifier(telegram, prefix=f"[{label}] ")
    if telegram.enabled:
        log.info("Telegram alerts enabled")

    runner: ChainRunner | None = None
    try:
        try:
            config = load_chain_config(chain)
        except ConfigError as exc:
            log.error("configuration error: %s", exc)
            return 2

        ctx = build_context(config)
        describe(ctx)
        runner = ChainRunner(ctx, notifier)

        if once:
            report_feed_activity(ctx)
            log.info("startup check complete (--once), exiting without connecting")
            return 0

        await notifier.send(
            f"Монитор запущен: {config.name} (только наблюдение)",
            dedup_key=f"startup-{config.name}",
        )
        await runner.run()
        return 0
    except asyncio.CancelledError:
        return 0
    finally:
        if runner is not None:
            runner.stop()
        await notifier.close()


def run_one_chain(chain: str | None, once: bool = False) -> int:
    """Synchronous wrapper used by both the CLI and the process supervisor."""
    try:
        return asyncio.run(run_chain_async(chain, once=once))
    except KeyboardInterrupt:
        log.info("stopped by user")
        return 0
    except ConnectionError as exc:
        log.error("%s", exc)
        return 1
    except Exception as exc:
        log.critical("fatal: %s", exc, exc_info=True)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aave V3 liquidation monitor")
    parser.add_argument(
        "--chain", action="append", dest="chains", default=None,
        help="chain name whose env vars carry that prefix; repeatable",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="connect, resolve reserves and price feeds, print a summary, then exit",
    )
    parser.add_argument("--log-level", default=None)
    parser.add_argument(
        "--log-file", default=None,
        help="write full (untruncated) logs to this file; overrides LOG_FILE. "
             "In multi-chain mode each process gets a per-chain suffix.",
    )
    parser.add_argument(
        "--in-process", action="store_true",
        help="run every chain inside one process (debug only). Default for "
             "two or more chains is one OS process each.",
    )
    args = parser.parse_args(argv)

    chains = args.chains if args.chains is not None else configured_chains()

    # Multi-chain: one process per chain. Logging is configured inside each
    # child so file handlers do not race; the parent only needs a console logger.
    if len(chains) > 1 and not args.in_process:
        configure_logging(args.log_level, None)
        from .supervisor import run_processes

        log.info("Stage 2 multi-chain mode: %s (one process each)", ", ".join(chains))
        return run_processes(
            chains,
            once=args.once,
            log_file=args.log_file,
            log_level=args.log_level,
        )

    # Single chain (or explicit --in-process): stay in this process.
    configure_logging(args.log_level, args.log_file)
    if len(chains) > 1 and args.in_process:
        log.warning(
            "--in-process with %d chains: sharing one event loop. Prefer the "
            "default multi-process mode for production.",
            len(chains),
        )
        # Sequential --once is useful for a quick config check without spawn.
        if args.once:
            code = 0
            for chain in chains:
                code = run_one_chain(chain, once=True) or code
            return code
        log.error("--in-process continuous multi-chain is not supported; omit the flag")
        return 2

    return run_one_chain(chains[0] if chains else None, once=args.once)


if __name__ == "__main__":
    sys.exit(main())
