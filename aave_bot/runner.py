"""WebSocket event loop with reconnect, resubscribe and a liveness watchdog.

Three failure modes the original loop had no answer for:

* the socket dies (the observed WinError 121 after ~75 minutes on a free
  endpoint) and the process exits;
* the socket stays open but stops delivering, which looks exactly like a quiet
  network, so nothing notices;
* position evaluation runs blocking HTTP calls directly inside the event loop,
  so one slow batch stalls the reader and messages queue up unread.

Blocking work is therefore pushed to a worker thread, and users awaiting
evaluation are de-duplicated: a single price update can touch thousands of
positions and must not enqueue the same address repeatedly.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import suppress

from pathlib import Path

from web3 import AsyncWeb3, Web3
from web3.providers import WebSocketProvider

from .alerts import Notifier
from .chain import ChainContext
from .topics import AAVE_EVENT_TOPICS, ANSWER_UPDATED_TOPIC, topic_hex
from .would_have import WouldHaveStats, estimate_amounts_usd, estimate_plan_usd

WORK_QUEUE_LIMIT = 10_000
# TG dry-run P&L summary cadence (seconds).
WOULD_HAVE_SUMMARY_SECONDS = 6 * 3600


class ChainRunner:
    def __init__(self, ctx: ChainContext, notifier: Notifier | None = None) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.notifier = notifier
        self.log = logging.getLogger(f"aave_bot.{ctx.config.name}.runner")

        self._stop = asyncio.Event()
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=WORK_QUEUE_LIMIT)
        self._queued: set[str] = set()
        # Evaluation and the pending-transaction poll both hop into worker
        # threads that share one Web3 instance, and requests.Session underneath
        # it is not thread-safe. Serialising the hops keeps a single thread on
        # the HTTP client at any moment.
        self._rpc_lock = asyncio.Lock()

        self._connected_since: float | None = None
        self._last_session_seconds: float | None = None
        self._last_message_at: float = 0.0
        self._last_block: int | None = None
        self._last_block_at: float = 0.0
        self.reconnects = 0

        self.aave_events = 0
        self.price_updates = 0
        self.observed_liquidations = 0
        self.candidates_seen = 0
        self.strategies: list = []
        self._loop: asyncio.AbstractEventLoop | None = None
        stats_path = Path(f"would_have_{ctx.config.name}.json")
        self.would_have = WouldHaveStats.load(ctx.config.name, stats_path)
        self.ctx.report_arb_opportunity = self._on_arb_opportunity  # type: ignore[attr-defined]
        self._init_strategies()

    def _init_strategies(self) -> None:
        """Stage 3 plugins. Liquidation stays inlined; flash arb strategies optional."""
        if self.config.flash_arb_enabled:
            from .strategies.flash_arb import FlashArbStrategy
            self.strategies.append(FlashArbStrategy(self.ctx))
        if self.config.balancer_arb_enabled:
            from .strategies.balancer_v3_arb import BalancerV3ArbStrategy
            self.strategies.append(BalancerV3ArbStrategy(self.ctx))

    # ── lifecycle ────────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        for strategy in self.strategies:
            try:
                strategy.on_start()
            except Exception as exc:
                self.log.error("strategy %s failed to start: %s",
                               getattr(strategy, "name", strategy), exc, exc_info=True)

        tasks = [
            asyncio.create_task(self._worker(), name=f"{self.config.name}-worker"),
            asyncio.create_task(self._pending_watcher(), name=f"{self.config.name}-pending"),
            asyncio.create_task(self._heartbeat(), name=f"{self.config.name}-heartbeat"),
            asyncio.create_task(self._strategy_ticker(), name=f"{self.config.name}-strategies"),
            asyncio.create_task(self._book_scanner(), name=f"{self.config.name}-book-scan"),
        ]
        try:
            await self._supervise()
        finally:
            self._stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.ctx.state.save(force=True)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped at the configured ceiling.

        Jitter keeps several chains (or several restarted instances) from
        hammering the same endpoint in lockstep after a shared outage.
        """
        capped = min(
            self.config.ws_backoff_initial_seconds * (2 ** max(0, attempt - 1)),
            self.config.ws_backoff_max_seconds,
        )
        return capped * (0.75 + random.random() * 0.5)

    async def _supervise(self) -> None:
        """Reconnect forever with exponential backoff plus jitter."""
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._session()
                attempt = 0  # a clean return means the socket lived and was closed
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                self.reconnects += 1
                delay = self._backoff_delay(attempt)
                phase = (
                    f"после {self._last_session_seconds:.0f}с"
                    if self._last_session_seconds is not None else "при подключении"
                )
                self.log.warning(
                    "websocket lost %s (%s: %s) — reconnecting in %.1fs (attempt %d)",
                    phase, type(exc).__name__, exc, delay, attempt,
                )
                await self._alert(
                    f"WebSocket оборван {phase}: {type(exc).__name__}.\n"
                    f"Переподключение через {delay:.0f}с (попытка {attempt}).",
                    dedup_key="ws-down",
                )
                self.ctx.state.save(force=True)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    def _build_provider(self) -> WebSocketProvider:
        cfg = self.config
        return WebSocketProvider(
            cfg.ws_rpc_url,
            # web3 otherwise retries a failed connect five times on its own
            # schedule — 1.75s growing to ~9s, about 20 seconds of silence —
            # before surfacing anything. Reconnection has to be ours: only we
            # know how to re-subscribe afterwards, and only we log and alert.
            max_connection_retries=1,
            request_timeout=cfg.ws_request_timeout_seconds,
            websocket_kwargs={
                "open_timeout": cfg.ws_connect_timeout_seconds,
                "close_timeout": 5,
                # Protocol keepalive: a missing pong tears the socket down, which
                # exposes a half-open connection well before the silence
                # watchdog would notice.
                "ping_interval": cfg.ws_ping_interval_seconds,
                "ping_timeout": cfg.ws_ping_timeout_seconds,
                "max_size": 16 * 1024 * 1024,
            },
        )

    async def _session(self) -> None:
        """One connection: subscribe, then consume until the socket misbehaves."""
        url = self.config.ws_rpc_url
        self.log.info("connecting to %s", url)
        self._last_session_seconds = None

        w3 = AsyncWeb3(self._build_provider())
        # A blackholed endpoint completes the TCP handshake and then says
        # nothing, so the connect itself needs its own deadline.
        async with asyncio.timeout(self.config.ws_connect_timeout_seconds):
            await w3.provider.connect()

        try:
            subscriptions = await self._subscribe_all(w3)
            self._connected_since = time.monotonic()
            self._last_message_at = time.monotonic()
            self._last_block_at = time.monotonic()
            # A fresh connection may land on a different node behind the same
            # hostname, whose head can legitimately sit a block or two lower.
            # Carrying the previous height over would read as a frozen chain and
            # send the supervisor into a reconnect loop.
            self._last_block = None

            if self.reconnects:
                await self._alert(
                    f"WebSocket восстановлен, подписок: {len(subscriptions)}.",
                    dedup_key="ws-up",
                )

            iterator = w3.socket.process_subscriptions().__aiter__()
            while not self._stop.is_set():
                try:
                    async with asyncio.timeout(self.config.ws_silence_timeout_seconds):
                        message = await iterator.__anext__()
                except asyncio.TimeoutError:
                    # Silence is normal on a quiet chain, so prove the socket is
                    # still usable rather than reconnecting on a hunch.
                    await self._probe(w3)
                    continue
                except StopAsyncIteration:
                    # web3 ends the stream instead of raising when the listener
                    # task dies, so a dropped socket arrives here.
                    raise ConnectionError("subscription stream ended")

                self._last_message_at = time.monotonic()
                self._dispatch(message, subscriptions)
        finally:
            self._last_session_seconds = (
                time.monotonic() - self._connected_since
                if self._connected_since is not None else None
            )
            self._connected_since = None
            with suppress(Exception):
                await w3.provider.disconnect()

    async def _subscribe_all(self, w3: AsyncWeb3) -> dict[str, tuple[str, str | None]]:
        """(Re)create every subscription. Called fresh on each connection."""
        subscriptions: dict[str, tuple[str, str | None]] = {}

        pool_sub = await self._subscribe_pool(w3)
        subscriptions[pool_sub] = ("aave", None)

        aggregators = list(self.ctx.aggregator_assets)
        if aggregators:
            subscriptions.update(await self._subscribe_feeds(w3, aggregators))

        feed_count = sum(1 for kind, _ in subscriptions.values() if kind.startswith("oracle"))
        covered = (
            len(aggregators)
            if any(kind == "oracle-batch" for kind, _ in subscriptions.values())
            else feed_count
        )
        self.log.info("price feed subscriptions: %d covering %d/%d feeds",
                      feed_count, covered, len(aggregators))
        if feed_count == 0:
            self.log.warning(
                "no price feed subscriptions — the bot only reacts to Aave user "
                "events, not to price moves, which is the more common trigger"
            )
        return subscriptions

    async def _subscribe_feeds(
        self, w3: AsyncWeb3, aggregators: list[str]
    ) -> dict[str, tuple[str, str | None]]:
        """Watch every Chainlink aggregator through one subscription.

        eth_subscribe accepts an address array, so a single request covers all
        feeds and the emitting aggregator is read back from the log. One request
        per feed looked equivalent but is not: on mainnet the endpoint starts
        dropping or stalling them, and since subscribing happens before the
        message pump starts, a dozen 30-second timeouts left the monitor deaf
        for minutes and let the connection die on a keepalive timeout.
        """
        params = {"address": aggregators, "topics": [ANSWER_UPDATED_TOPIC]}
        try:
            async with asyncio.timeout(self.config.ws_subscribe_timeout_seconds):
                sub_id = await w3.eth.subscribe("logs", params)
            return {sub_id: ("oracle-batch", None)}
        except Exception as exc:
            self.log.warning(
                "batched feed subscription unavailable (%s: %s), falling back to "
                "one subscription per feed", type(exc).__name__, exc,
            )

        subscriptions: dict[str, tuple[str, str | None]] = {}
        for aggregator in aggregators:
            try:
                async with asyncio.timeout(self.config.ws_subscribe_timeout_seconds):
                    sub_id = await w3.eth.subscribe(
                        "logs", {"address": aggregator, "topics": [ANSWER_UPDATED_TOPIC]}
                    )
            except Exception as exc:
                self.log.warning("could not subscribe to feed %s: %s", aggregator, exc)
                continue
            subscriptions[sub_id] = ("oracle", aggregator)
        return subscriptions

    async def _subscribe_pool(self, w3: AsyncWeb3) -> str:
        """Subscribe to the four position-changing events, not every pool log.

        An unfiltered pool subscription also delivers ReserveDataUpdated and
        friends, which fire on every single interaction and are discarded on
        arrival — pure waste on a busy chain. A nested topic array is the OR
        filter for topic0, but not every provider honours it, and a rejected
        eth_subscribe can hang rather than error, so the attempt is bounded and
        falls back to the unfiltered form.
        """
        params = {
            "address": self.config.pool,
            "topics": [list(AAVE_EVENT_TOPICS.keys())],
        }
        try:
            async with asyncio.timeout(self.config.ws_subscribe_timeout_seconds):
                sub_id = await w3.eth.subscribe("logs", params)
            self.log.info("subscribed to Aave Pool logs, filtered to %d event types",
                          len(AAVE_EVENT_TOPICS))
            return sub_id
        except Exception as exc:
            self.log.warning(
                "topic-filtered pool subscription unavailable (%s), falling back to "
                "all pool logs", type(exc).__name__,
            )

        sub_id = await w3.eth.subscribe("logs", {"address": self.config.pool})
        self.log.info("subscribed to all Aave Pool logs")
        return sub_id

    async def _probe(self, w3: AsyncWeb3) -> None:
        """Liveness check. Raises to force a reconnect when the socket is stale."""
        block = await w3.eth.get_block_number()
        now = time.monotonic()

        if self._last_block is None or block > self._last_block:
            self._last_block = block
            self._last_block_at = now
            self.log.debug("probe ok, head at %d", block)
            return

        stalled_for = now - self._last_block_at
        if stalled_for > self.config.ws_silence_timeout_seconds * 2:
            raise ConnectionError(
                f"chain head stuck at {block} for {stalled_for:.0f}s"
            )
        self.log.debug("head unchanged at %d for %.0fs", block, stalled_for)

    # ── dispatch ─────────────────────────────────────────────────────────
    def _dispatch(self, message: dict, subscriptions: dict[str, tuple[str, str | None]]) -> None:
        sub_id = message.get("subscription")
        result = message.get("result")
        if not sub_id or result is None:
            return
        entry = subscriptions.get(sub_id)
        if entry is None:
            return

        self._note_block(result)
        kind, extra = entry
        try:
            if kind == "aave":
                self._on_aave_log(result)
            elif kind == "oracle":
                self._on_price_update(result, extra or "")
            elif kind == "oracle-batch":
                self._on_price_update(result, self._aggregator_of(result))
        except Exception as exc:
            self.log.error("failed to handle a %s event: %s", kind, exc, exc_info=True)

    def _on_aave_log(self, entry: dict) -> None:
        topics = entry.get("topics") or []
        if not topics:
            return
        event_name = AAVE_EVENT_TOPICS.get(topic_hex(topics[0]))
        if not event_name:
            return

        try:
            decoded = getattr(self.ctx.pool.events, event_name)().process_log(entry)
        except Exception as exc:
            self.log.debug("could not decode %s: %s", event_name, exc)
            return

        self.aave_events += 1
        args = decoded["args"]

        if event_name == "LiquidationCall":
            self._report_liquidation(args, entry)

        user = args.get("onBehalfOf") or args.get("user")
        reserve = args.get("reserve") or args.get("collateralAsset")
        if not user or not reserve:
            return

        user = self.ctx.w3.to_checksum_address(user)
        reserve = self.ctx.w3.to_checksum_address(reserve)
        if self.ctx.state.track(user, reserve):
            self.log.info("new tracked address %s (via %s)", user, event_name)
        self.ctx.state.save()
        self._enqueue(user)

    def _report_liquidation(self, args: dict, entry: dict) -> None:
        """Log a liquidation somebody else won.

        This is the scoreboard: it shows how much flow the bot is positioned to
        compete for. Amounts are formatted from the warmed decimals cache so the
        event loop never blocks on an RPC call here.
        """
        self.observed_liquidations += 1
        ctx = self.ctx
        collateral = args.get("collateralAsset")
        debt = args.get("debtAsset")
        tx_hex = topic_hex(entry.get("transactionHash", ""))
        repaid = ctx.format_amount(debt, args.get("debtToCover", 0)) if debt else "?"
        seized = (
            ctx.format_amount(collateral, args.get("liquidatedCollateralAmount", 0))
            if collateral else "?"
        )
        liquidator = args.get("liquidator")
        user = args.get("user")
        block = entry.get("blockNumber")
        debt_amt = int(args.get("debtToCover", 0) or 0)
        debt_usd, profit_usd = (0.0, 0.0)
        if debt and collateral and debt_amt:
            debt_usd, profit_usd = estimate_amounts_usd(ctx, debt, debt_amt, collateral)
        kind = self.would_have.note_foreign_liq(
            str(user) if user else "", debt_usd, profit_usd
        )
        status_ru = (
            "видели кандидата, но упустили (гонка)"
            if kind == "ypustili"
            else "не успели увидеть до чужого tx"
        )

        self.log.info(
            "LIQUIDATION #%d by %s | user %s | repaid %s | seized %s | "
            "~$%.0f debt ~$%.2f profit | %s | block %s | tx %s",
            self.observed_liquidations,
            liquidator,
            user,
            repaid,
            seized,
            debt_usd,
            profit_usd,
            kind,
            block,
            tx_hex,
        )
        self._schedule_alert(
            f"ЛИКВИДАЦИЯ #{self.observed_liquidations} (чужая)\n"
            f"ликвидатор: {liquidator}\n"
            f"пользователь: {user}\n"
            f"погашено: {repaid} | забрано: {seized}\n"
            f"долг ~${debt_usd:,.0f} | оценка прибыли ~${profit_usd:,.2f}\n"
            f"наш статус: {status_ru}\n"
            f"блок {block} | tx {tx_hex}",
            dedup_key=f"liq-{tx_hex}" if tx_hex else None,
            cooldown=60.0,
        )

    def _note_block(self, entry: dict) -> None:
        """Track the head from arriving logs.

        The probe only runs after a silent stretch, so on a busy chain it never
        fires and the reported head stayed empty for the whole session. Reading
        it from the events also gives the watchdog a fresher baseline.
        """
        raw = entry.get("blockNumber")
        if raw is None:
            return
        try:
            block = int(raw, 16) if isinstance(raw, str) else int(raw)
        except (TypeError, ValueError):
            return
        if self._last_block is None or block > self._last_block:
            self._last_block = block
            self._last_block_at = time.monotonic()

    def _aggregator_of(self, entry: dict) -> str:
        """Which watched aggregator emitted this log, from the batched subscription."""
        address = entry.get("address")
        if address is None:
            return ""
        try:
            return Web3.to_checksum_address(address)
        except Exception:
            return str(address)

    def _on_price_update(self, entry: dict, aggregator: str) -> None:
        self.price_updates += 1
        assets = self.ctx.aggregator_assets.get(aggregator, set())
        affected = self.ctx.state.users_for_assets(assets)
        symbols = ", ".join(sorted(self.ctx.symbol_of(a) for a in assets)) or aggregator

        self.log.info("price update %s (block %s): %d of %d tracked positions affected",
                      symbols, entry.get("blockNumber"), len(affected),
                      len(self.ctx.state))
        for user in affected:
            self._enqueue(user)
        for strategy in self.strategies:
            try:
                strategy.on_price_update(set(assets))
            except Exception as exc:
                self.log.error("strategy %s on_price_update failed: %s",
                               getattr(strategy, "name", strategy), exc, exc_info=True)

    async def _strategy_ticker(self) -> None:
        """Drive strategy.on_tick without waiting for a quiet chain."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                return
            except asyncio.TimeoutError:
                pass
            for strategy in self.strategies:
                try:
                    async with self._rpc_lock:
                        await asyncio.to_thread(strategy.on_tick)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.log.error("strategy %s on_tick failed: %s",
                                   getattr(strategy, "name", strategy), exc, exc_info=True)

    async def _book_scanner(self) -> None:
        """Periodically re-queue tracked users for HF evaluation.

        Oracle ticks only wake holders of the moved reserve. Interest accrual
        can push a position under water with no price event — this sweep closes
        that gap. Disabled when BOOK_SCAN_INTERVAL_SECONDS=0.

        Users are enqueued in waves so public RPCs are not 429'd by a full-book
        stampede right after connect.
        """
        interval = self.config.book_scan_interval_seconds
        if interval <= 0:
            return
        # Let WS subscriptions settle before the first HF wave.
        first_delay = min(120.0, max(60.0, float(interval) * 0.25))
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=first_delay)
            return
        except asyncio.TimeoutError:
            pass

        wave = 100
        wave_pause = 15.0
        while not self._stop.is_set():
            users = list(self.ctx.state.tracked_users)
            if users:
                self.log.info(
                    "book scan: enqueueing %d tracked users in waves of %d",
                    len(users), wave,
                )
                for i in range(0, len(users), wave):
                    if self._stop.is_set():
                        return
                    for user in users[i : i + wave]:
                        self._enqueue(user)
                    # Pace waves so the worker can drain without 429 storms.
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=wave_pause)
                        return
                    except asyncio.TimeoutError:
                        pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=float(interval))
                return
            except asyncio.TimeoutError:
                pass

    def _enqueue(self, user: str) -> None:
        if user in self._queued:
            return
        try:
            self._queue.put_nowait(user)
        except asyncio.QueueFull:
            self.log.warning("evaluation queue full, dropping %s", user)
            return
        self._queued.add(user)

    # ── background tasks ─────────────────────────────────────────────────
    async def _worker(self) -> None:
        """Evaluates positions off the event loop so the reader never blocks."""
        while not self._stop.is_set():
            try:
                user = await self._queue.get()
            except asyncio.CancelledError:
                raise
            self._queued.discard(user)
            try:
                async with self._rpc_lock:
                    await asyncio.to_thread(self._evaluate, user)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error("evaluation failed for %s: %s", user, exc, exc_info=True)
            finally:
                self._queue.task_done()

    def _evaluate(self, user: str) -> None:
        plan = self.ctx.evaluate_user(user)
        if plan is None:
            return
        self.candidates_seen += 1
        debt_fmt = self.ctx.format_amount(plan.debt_asset, plan.debt_to_cover)
        coll_sym = self.ctx.symbol_of(plan.collateral_asset)
        debt_sym = self.ctx.symbol_of(plan.debt_asset)
        debt_usd, profit_usd = estimate_plan_usd(self.ctx, plan)
        counted = self.would_have.remember_candidate(plan.user, debt_usd, profit_usd)
        if not self.config.auto_execute:
            self.log.info(
                "[dry-run] would liquidate %s (HF=%.4f, debtToCover=%d, "
                "~$%.0f debt ~$%.2f profit%s)",
                plan.user, plan.health_factor, plan.debt_to_cover,
                debt_usd, profit_usd,
                "" if counted else " dedup",
            )
            self._schedule_alert(
                f"КАНДИДАТ (dry-run, без отправки)\n"
                f"{plan.user}\n"
                f"HF={plan.health_factor:.4f}  {debt_sym}→{coll_sym}\n"
                f"долг к покрытию: {debt_fmt} (~${debt_usd:,.0f})\n"
                f"оценка прибыли: ~${profit_usd:,.2f} "
                f"(бонус−5bps, без gas/slip)\n"
                f"итого сессия: кандидатов {self.would_have.candidates}, "
                f"~${self.would_have.est_profit_usd:,.2f}",
                dedup_key=f"cand-{plan.user}",
                cooldown=120.0,
            )
            return
        self._schedule_alert(
            f"ИСПОЛНЯЕМ ликвидацию\n"
            f"{plan.user}\n"
            f"HF={plan.health_factor:.4f}  {debt_sym}→{coll_sym}\n"
            f"долг к покрытию: {debt_fmt} (~${debt_usd:,.0f})\n"
            f"оценка прибыли: ~${profit_usd:,.2f}",
            dedup_key=f"exec-{plan.user}",
            cooldown=60.0,
        )
        self.ctx.execute_liquidation(plan)

    async def _pending_watcher(self) -> None:
        """Settles broadcast transactions and reports connection health."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.pending_check_interval_seconds
                )
                return
            except asyncio.TimeoutError:
                pass

            try:
                async with self._rpc_lock:
                    outcomes = await asyncio.to_thread(self.ctx.check_pending_transactions)
                for message in outcomes:
                    await self._alert(message, dedup_key=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.debug("pending check failed: %s", exc)

            self.ctx.state.save()

    async def _heartbeat(self) -> None:
        """Periodic proof of life.

        A quiet chain produces no log output for hours, which is
        indistinguishable from a wedged process. Without this the only way to
        tell them apart is to attach a debugger.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.heartbeat_interval_seconds
                )
                return
            except asyncio.TimeoutError:
                pass

            status = self.status()
            wh = self.would_have
            self.log.info(
                "alive: head=%s up=%ss events=%d price_updates=%d liquidations_seen=%d "
                "candidates=%d would_profit=$%.2f arb_hits=%d arb_profit=$%.2f "
                "raced_miss=%d never_saw=%d "
                "arb=%d users=%d queued=%d pending_txs=%d reconnects=%d",
                status["head"], status["connected_for"], status["aave_events"],
                status["price_updates"], status["observed_liquidations"],
                status["candidates_seen"], wh.est_profit_usd,
                wh.arb_hits, wh.arb_est_profit_usd,
                wh.raced_and_missed, wh.never_saw,
                status["arb_opportunities"],
                status["tracked_users"], status["queued"],
                status["pending_txs"], status["reconnects"],
            )
            now = time.monotonic()
            if wh.last_summary_at == 0:
                wh.last_summary_at = now
            elif (now - wh.last_summary_at) >= WOULD_HAVE_SUMMARY_SECONDS:
                wh.last_summary_at = now
                await self._alert(wh.summary_ru(), dedup_key=None, cooldown=0.0)

    def _on_arb_opportunity(
        self,
        kind: str,
        borrow: str,
        mid: str,
        amount: int,
        gross_profit: int,
        detail: str,
    ) -> None:
        """Called from flash/balancer strategies on +EV quote (dry-run or live)."""
        base_amt = self.ctx.value_in_base(borrow, amount) or 0
        base_profit = self.ctx.value_in_base(borrow, gross_profit) or 0
        notional_usd = float(base_amt) / 1e8
        profit_usd = float(base_profit) / 1e8
        route_key = f"{kind}:{borrow.lower()}:{mid.lower()}:{amount}"
        counted = self.would_have.remember_arb(kind, route_key, notional_usd, profit_usd)
        borrow_s = self.ctx.symbol_of(borrow)
        mid_s = self.ctx.symbol_of(mid)
        self.log.info(
            "[dry-run arb] %s %s→%s→%s size=~$%.0f profit=~$%.4f%s %s",
            kind, borrow_s, mid_s, borrow_s, notional_usd, profit_usd,
            "" if counted else " dedup", detail,
        )
        if not counted:
            return
        self._schedule_alert(
            f"ARB +EV ({kind}, dry-run)\n"
            f"{borrow_s}→{mid_s}→{borrow_s}\n"
            f"{detail}\n"
            f"размер ~${notional_usd:,.0f} | прибыль ~${profit_usd:,.4f}\n"
            f"итого arb сессия: {self.would_have.arb_hits} хитов, "
            f"~${self.would_have.arb_est_profit_usd:,.2f}",
            dedup_key=f"arb-{route_key}",
            cooldown=300.0,
        )

    def _schedule_alert(
        self,
        text: str,
        *,
        dedup_key: str | None,
        cooldown: float = 300.0,
    ) -> None:
        """Fire-and-forget from sync / worker-thread contexts."""
        if self.notifier is None or not self.notifier.enabled:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def _send() -> None:
            await self._alert(text, dedup_key=dedup_key, cooldown=cooldown)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(_send())
        else:
            loop.call_soon_threadsafe(lambda: loop.create_task(_send()))

    async def _alert(
        self,
        text: str,
        dedup_key: str | None,
        cooldown: float = 300.0,
    ) -> None:
        if self.notifier is None:
            return
        await self.notifier.send(
            f"[{self.config.name}] {text}",
            dedup_key=dedup_key,
            cooldown=cooldown,
        )

    # ── introspection ────────────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "chain": self.config.name,
            "connected_for": (
                round(time.monotonic() - self._connected_since, 1)
                if self._connected_since else None
            ),
            "reconnects": self.reconnects,
            "seconds_since_message": round(time.monotonic() - self._last_message_at, 1)
            if self._last_message_at else None,
            "head": self._last_block,
            "aave_events": self.aave_events,
            "price_updates": self.price_updates,
            "observed_liquidations": self.observed_liquidations,
            "candidates_seen": self.candidates_seen,
            "arb_opportunities": sum(
                getattr(s, "opportunities_seen", 0) for s in self.strategies
            ),
            "tracked_users": len(self.ctx.state),
            "queued": self._queue.qsize(),
            "pending_txs": len(self.ctx.pending_txs),
        }
