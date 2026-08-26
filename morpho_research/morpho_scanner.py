#!/usr/bin/env python3
"""Morpho Blue liquidation scout — parallel candidate source (dry-run).

Does NOT modify monitor_v4.py or Aave modules. Reuses RPC/.env via aave_bot.config
and Multicall3 ABI pattern locally. Later this can feed the same candidate pipeline.

Usage (repo root):
    .venv-run/Scripts/python.exe morpho_research/morpho_scanner.py
    .venv-run/Scripts/python.exe morpho_research/morpho_scanner.py --chains base
    .venv-run/Scripts/python.exe morpho_research/morpho_scanner.py --once
    # Overnight diagnostic (no self-timeout; Ctrl+C to stop):
    .venv-run/Scripts/python.exe morpho_research/morpho_scanner.py --chains base --diagnostic-mode --min-net-profit-usd 0.05
    # Bounded test: --duration-seconds 1800 still works when you pass it explicitly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

from web3 import AsyncWeb3, Web3
from web3.providers import WebSocketProvider

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
for p in (_DIR, _ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from aave_bot import config as bot_config  # noqa: E402
from aave_bot.alerts import Notifier  # noqa: E402
from aave_bot.config import load_morpho_telegram_config  # noqa: E402

from aave_bot.topics import ANSWER_UPDATED_TOPIC, topic_hex  # noqa: E402

from morpho_hf import (  # noqa: E402
    MorphoCandidate,
    MorphoReader,
    estimate_liquidation_profit_usd,
    estimate_net_profit_usd,
    hf_from_cached_shares,
    loan_token_price_usd,
    parse_answer_updated_current,
    project_morpho_price,
    read_chainlink_answer,
    resolve_morpho_oracle_aggregators,
    resolve_morpho_oracle_feed_roles,
)
from morpho_markets import (  # noqa: E402
    MORPHO_BLUE,
    MorphoMarketConfig,
    markets_for_chain,
    parse_allowed_markets,
)
from morpho_scoreboard import MorphoScoreboard  # noqa: E402
from morpho_flash_arb import MorphoFlashArbScanner  # noqa: E402
from morpho_executor import (  # noqa: E402
    DIAGNOSTIC_NOT_COMBAT,
    LiqIntent,
    MorphoExecutor,
    connect_rotating_http,
    diagnostic_enabled,
    duration_is_forever,
    min_net_profit_usd,
)


def _connect_http(urls: list[str], timeout: int = 30) -> tuple[str, Web3]:
    url, w3, provider = connect_rotating_http(urls, timeout=timeout)
    # Keep provider on w3 for metrics / debugging.
    setattr(w3, "_morpho_rpc_rotator", provider)
    return url, w3

LOG = logging.getLogger("morpho_scanner")

# Lower number = higher priority. Oracle / feeder jumps the seed backlog.
PRIO_ORACLE = 0
PRIO_FEEDER = 0
PRIO_EVENT = 1
PRIO_HOT = 2
PRIO_SEED = 3
PRIO_BOOK = 5

# Default dust feeders (Aug24 recon): B90 cbXRP drip + 0x6c56 USDC funder.
DEFAULT_FEEDER_ADDRESSES = (
    "0xb90fe999be6869af0afc557dccfbe169ea3403d6",
    "0x6c561b446416e1a00e8e93e221854d6ea4171372",
)

# Positions with HF below this (and debt >= min) stay on the hot path.
HOT_HF_THRESHOLD = Decimal("1.05")
# On oracle move, also re-check tracked positions under this HF.
ORACLE_NEAR_HF = Decimal("1.15")


def classify_ws_event(event_name: str, market_known: bool) -> str:
    """How a Morpho log should be handled.

    core — watchlist market (seed/oracle/live as already configured)
    discovery — Liquidate on an unknown market: Telegram only, no seed
    drop — other events on unknown markets (do not enqueue)
    """
    if market_known:
        return "core"
    if event_name == "Liquidate":
        return "discovery"
    return "drop"


_DISCOVERY_MARKET_Q = """
query($id: String!, $chainId: Int!) {
  markets(
    first: 1
    where: { uniqueKey_in: [$id], chainId_in: [$chainId] }
  ) {
    items {
      lltv
      loanAsset { symbol decimals priceUsd }
      collateralAsset { symbol }
      state { priceUsd }
    }
  }
}
"""


def lookup_discovery_market(market_id: str, chain_id: int) -> dict[str, Any] | None:
    """GraphQL only — never Morpho HTTP/WS. None if unknown."""
    hid = market_id.lower()
    if not hid.startswith("0x"):
        hid = "0x" + hid
    items: list[Any] = []
    for where_field in ("uniqueKey_in", "marketId_in"):
        q = _DISCOVERY_MARKET_Q.replace("uniqueKey_in", where_field)
        try:
            data = _graphql(q, {"id": hid, "chainId": chain_id})
        except Exception:  # noqa: BLE001
            continue
        items = (data.get("markets") or {}).get("items") or []
        if items:
            break
    if not items:
        return None
    row = items[0]
    loan = row.get("loanAsset") or {}
    coll = row.get("collateralAsset") or {}
    state = row.get("state") or {}
    lltv_raw = row.get("lltv") or 0
    try:
        lltv = int(float(lltv_raw))
    except (TypeError, ValueError):
        lltv = 0
    if lltv > 0 and lltv < 10**9:
        lltv = int(float(lltv_raw) * (10**18))
    try:
        price = float(loan.get("priceUsd") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if not price:
        try:
            price = float(state.get("priceUsd") or 0)
        except (TypeError, ValueError):
            price = 0.0
    return {
        "pair": f"{loan.get('symbol') or '?'}/{coll.get('symbol') or '?'}",
        "decimals": int(loan.get("decimals") or 6),
        "price_usd": price or 1.0,
        "lltv_wad": lltv if lltv > 10**16 else 770_000_000_000_000_000,
    }


def _topic0_hex(text: str) -> str:
    raw = Web3.keccak(text=text).hex()
    return raw if raw.startswith("0x") else "0x" + raw


ERC20_TRANSFER_TOPIC = _topic0_hex("Transfer(address,address,uint256)")

# Event signatures (Morpho Blue EventsLib) — always 0x-prefixed for eth_subscribe
EVENT_TOPICS: dict[str, str] = {
    "Borrow": _topic0_hex("Borrow(bytes32,address,address,address,uint256,uint256)"),
    "Repay": _topic0_hex("Repay(bytes32,address,address,uint256,uint256)"),
    "Supply": _topic0_hex("Supply(bytes32,address,address,uint256,uint256)"),
    "SupplyCollateral": _topic0_hex(
        "SupplyCollateral(bytes32,address,address,uint256)"
    ),
    "Withdraw": _topic0_hex(
        "Withdraw(bytes32,address,address,address,uint256,uint256)"
    ),
    "WithdrawCollateral": _topic0_hex(
        "WithdrawCollateral(bytes32,address,address,address,uint256)"
    ),
    "Liquidate": _topic0_hex(
        "Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
    ),
}
TOPIC_TO_EVENT = {v.lower().removeprefix("0x"): k for k, v in EVENT_TOPICS.items()}

GRAPHQL_URLS = (
    "https://blue-api.morpho.org/graphql",
    "https://api.morpho.org/graphql",
)


@dataclass
class PositionKey:
    market_id: str
    user: str

    def __hash__(self) -> int:
        return hash((self.market_id.lower(), self.user.lower()))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PositionKey):
            return False
        return (
            self.market_id.lower() == other.market_id.lower()
            and self.user.lower() == other.user.lower()
        )


@dataclass
class TrackedPosition:
    market_id: str
    user: str
    last_event: str = ""
    last_block: int = 0
    last_hf: Decimal | None = None
    last_debt_usd: float = 0.0
    updated_at: float = field(default_factory=time.time)
    enqueued_at: float = 0.0
    # Cached shares for local HF on oracle tick (no Multicall wait).
    supply_shares: int = 0
    borrow_shares: int = 0
    collateral: int = 0
    total_borrow_assets: int = 0
    total_borrow_shares: int = 0
    oracle_price: int = 0
    # Feeder pin: keep in hot briefly even if first eval sees borrow=0.
    feeder_pin_until: float = 0.0
    feeder_empty_seen: bool = False
    feeder_evals: int = 0
    feeder_recovered_logged: bool = False


@dataclass
class QueueItem:
    priority: int
    seq: int
    key: PositionKey
    enqueued_mono: float = field(default_factory=time.monotonic)

    def __lt__(self, other: QueueItem) -> bool:
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.seq < other.seq


def _topic_addr(topic: Any) -> str:
    raw = topic.hex() if hasattr(topic, "hex") else str(topic)
    raw = raw[2:] if raw.startswith("0x") else raw
    return Web3.to_checksum_address("0x" + raw[-40:])


def _addr_topic(addr: str) -> str:
    """32-byte topic for indexed address (lowercase 0x-prefixed)."""
    a = addr.lower().replace("0x", "")
    return "0x" + a.rjust(64, "0")


def _topic_id(topic: Any) -> str:
    raw = topic.hex() if hasattr(topic, "hex") else str(topic)
    return raw if raw.startswith("0x") else "0x" + raw


def _log_data_uints(entry: dict[str, Any]) -> list[int]:
    data = entry.get("data") or b""
    if hasattr(data, "hex"):
        data_hex = data.hex()
    else:
        data_hex = data if isinstance(data, str) else bytes(data).hex()
    data_hex = data_hex[2:] if data_hex.startswith("0x") else data_hex
    out: list[int] = []
    for i in range(0, len(data_hex), 64):
        chunk = data_hex[i : i + 64]
        if len(chunk) < 64:
            break
        out.append(int(chunk, 16))
    return out


def _tx_hex(entry: dict[str, Any]) -> str:
    raw = entry.get("transactionHash") or entry.get("transaction_hash") or ""
    if hasattr(raw, "hex"):
        return raw.hex()
    text = str(raw)
    return text if text.startswith("0x") else ("0x" + text if text else "")


def _graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    body = json.dumps(payload).encode()
    last: Exception | None = None
    import urllib.request

    for url in GRAPHQL_URLS:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode())
                if data.get("errors"):
                    raise RuntimeError(str(data["errors"][:1]))
                return data["data"]
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"graphql failed: {last}")


def seed_borrowers_from_api(
    chain_id: int,
    markets: list[MorphoMarketConfig],
    *,
    per_market: int = 200,
) -> list[tuple[str, str]]:
    """Return (market_id, user) pairs with borrowShares > 0."""
    query = """
    query($market: String!, $chainId: Int!, $first: Int!) {
      marketPositions(
        first: $first
        orderBy: BorrowShares
        orderDirection: Desc
        where: {
          marketUniqueKey_in: [$market]
          chainId_in: [$chainId]
          borrowShares_gte: "1"
        }
      ) {
        items { user { address } }
      }
    }
    """
    out: list[tuple[str, str]] = []
    for m in markets:
        try:
            data = _graphql(
                query,
                {"market": m.market_id, "chainId": chain_id, "first": per_market},
            )
            items = (data.get("marketPositions") or {}).get("items") or []
            for it in items:
                addr = (it.get("user") or {}).get("address")
                if addr:
                    out.append((m.market_id, Web3.to_checksum_address(addr)))
            LOG.info(
                "[%s] seeded %d borrowers for %s/%s",
                m.chain,
                len(items),
                m.loan_symbol,
                m.collateral_symbol,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("seed failed for %s: %s", m.market_id[:18], exc)
    return out


class MorphoChainScanner:
    """One chain: WS event feed + evaluation worker + periodic book rescan."""

    CHAIN_IDS = {"base": 8453, "arbitrum": 42161}

    def __init__(
        self,
        chain: str,
        *,
        notifier: Notifier | None = None,
        min_debt_usd: float = 100.0,
        hf_threshold: Decimal = Decimal("1.0"),
        book_scan_seconds: int = 120,
        seed_per_market: int = 150,
    ) -> None:
        self.chain = chain.lower()
        self.chain_id = self.CHAIN_IDS[self.chain]
        self.notifier = notifier
        self.min_debt_usd = min_debt_usd
        self.hf_threshold = hf_threshold
        self.book_scan_seconds = book_scan_seconds
        self.seed_per_market = seed_per_market

        self.diagnostic_mode = diagnostic_enabled(self.chain)
        self.max_debt_usd = float(
            bot_config._decimal("MORPHO_MAX_DEBT_USD", self.chain, "25000")
        )
        self.min_net_profit_usd = min_net_profit_usd(
            self.chain, diagnostic=self.diagnostic_mode
        )
        if self.diagnostic_mode:
            # Diagnostic path: filter by net_profit, not $300 debt.
            self.min_debt_usd = 0.0

        allowed_spec = bot_config._text(
            "MORPHO_ALLOWED_MARKETS", self.chain, inherit=True
        )
        self.markets = parse_allowed_markets(self.chain, allowed_spec or None)
        if not self.markets:
            # Empty filter matched nothing — fall back so the process still boots.
            LOG.warning(
                "MORPHO_ALLOWED_MARKETS=%r matched 0 markets on %s; using all enabled",
                allowed_spec,
                self.chain,
            )
            self.markets = markets_for_chain(self.chain)
        self.market_ids = {m.market_id.lower() for m in self.markets}
        self.markets_by_id = {m.market_id.lower(): m for m in self.markets}

        self.ws_url = bot_config._text("WS_RPC_URL", self.chain, required=True, inherit=False)
        http_urls = bot_config.http_rpc_urls(self.chain)
        if not http_urls:
            raise bot_config.ConfigError(
                f"{self.chain.upper()}_HTTP_RPC_URL is required but not set"
            )
        self.http_urls = http_urls
        self.http_url, self.w3 = _connect_http(http_urls)
        self.log = logging.getLogger(f"morpho_scanner.{self.chain}")
        self.log.info(
            "morpho HTTP %s rotating=%d peers=%s ws=%s",
            bot_config.rpc_host(self.http_url),
            len(http_urls),
            [bot_config.rpc_host(u) for u in http_urls],
            bot_config.rpc_host(self.ws_url),
        )
        self.reader = MorphoReader(self.w3)
        self.reader_oracle = MorphoReader(self.w3)
        self.flash_arb = MorphoFlashArbScanner(self.chain, self.w3)

        self.registry: dict[PositionKey, TrackedPosition] = {}
        self.hot_set: set[PositionKey] = set()
        self._queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(
            maxsize=20_000
        )
        self._queued: dict[PositionKey, int] = {}
        self._seq = 0
        self._stop = asyncio.Event()
        # Fast path: worker + hot_refresher (short batches). Bulk near-HF uses oracle lock.
        self._eval_rpc_sem = asyncio.Semaphore(2)
        self._oracle_rpc_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

        self.reconnects = 0
        self.events_seen = 0
        self.raw_logs_seen = 0
        self.candidates_seen = 0
        self.foreign_liq_seen = 0
        self.foreign_liq_debt_usd = 0.0
        self.foreign_liq_profit_usd = 0.0
        self.discovery_liq_seen = 0
        self.discovery_tg = bot_config._flag("MORPHO_DISCOVERY_TG", self.chain, True)
        self.discovery_min_debt_usd = float(
            bot_config._decimal(
                "MORPHO_DISCOVERY_MIN_DEBT_USD",
                self.chain,
                "0" if self.diagnostic_mode else "300",
            )
        )
        self._discovery_meta_cache: dict[str, dict[str, Any]] = {}
        self.oracle_price_changes = 0
        self.eval_count = 0
        self.eval_latency_ms_sum = 0.0
        self.queue_wait_ms_sum = 0.0
        self.batch_evals = 0
        self._oracle_prices: dict[str, int] = {}
        self._cl_answer: dict[str, int] = {}
        self._market_feed_roles: dict[str, tuple[list[str], list[str]]] = {}
        self._loan_decimals: dict[str, int] = {}
        self._connected_since: float | None = None
        self._last_block: int | None = None
        self._last_exec_metrics_tg: float | None = None
        # 0 = no periodic TG status; alerts only on events (liq, candidate, live send).
        self.exec_metrics_tg_seconds = max(
            0,
            bot_config._integer("MORPHO_STATUS_TG_SECONDS", self.chain, 0),
        )

        data_dir = _ROOT / "morpho_research" / "out"
        self.scoreboard = MorphoScoreboard.load(
            self.chain,
            data_dir / f"would_have_morpho_{self.chain}.json",
            data_dir / f"morpho_events_{self.chain}.jsonl",
        )
        try:
            self.executor: MorphoExecutor | None = MorphoExecutor(
                self.chain, self.w3, alert=self._schedule_alert
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("morpho executor init failed: %s", exc)
            self.executor = None
        if self.executor is not None and self.diagnostic_mode:
            self.min_net_profit_usd = self.executor.min_net_profit_usd
            self.max_debt_usd = self.executor.max_debt_usd
        if self.diagnostic_mode:
            self.log.warning(
                "%s filter=net_profit_usd>=$%.4f max_debt=$%.0f min_debt=$0 "
                "(Aave is_actionable_liq $300 NOT used)",
                DIAGNOSTIC_NOT_COMBAT,
                self.min_net_profit_usd,
                self.max_debt_usd,
            )

        # Tunables mirrored from aave runner defaults
        self.ws_connect_timeout = float(
            bot_config._decimal("WS_CONNECT_TIMEOUT_SECONDS", self.chain, "20")
        )
        self.ws_silence_timeout = bot_config._integer(
            "WS_SILENCE_TIMEOUT_SECONDS", self.chain, 120
        )
        self.ws_ping_interval = float(
            bot_config._decimal("WS_PING_INTERVAL_SECONDS", self.chain, "20")
        )
        self.ws_ping_timeout = float(
            bot_config._decimal("WS_PING_TIMEOUT_SECONDS", self.chain, "20")
        )
        self.ws_request_timeout = float(
            bot_config._decimal("WS_REQUEST_TIMEOUT_SECONDS", self.chain, "30")
        )
        self.ws_backoff_initial = float(
            bot_config._decimal("WS_BACKOFF_INITIAL_SECONDS", self.chain, "1.0")
        )
        self.ws_backoff_max = float(
            bot_config._decimal("WS_BACKOFF_MAX_SECONDS", self.chain, "60")
        )
        self.heartbeat_seconds = bot_config._integer(
            "HEARTBEAT_INTERVAL_SECONDS", self.chain, 60
        )
        self.oracle_poll_seconds = float(
            bot_config._decimal("MORPHO_ORACLE_POLL_SECONDS", self.chain, "1.0")
        )
        self.oracle_poll_fast_seconds = float(
            bot_config._decimal("MORPHO_ORACLE_POLL_FAST_SECONDS", self.chain, "0.35")
        )
        self.oracle_feed_ws = bot_config._flag(
            "MORPHO_ORACLE_FEED_WS", self.chain, True
        )
        self.oracle_pending_price = bot_config._flag(
            "MORPHO_ORACLE_PENDING_PRICE", self.chain, True
        )
        self.oracle_storm_wide_bps = max(
            0,
            bot_config._integer("MORPHO_ORACLE_STORM_WIDE_BPS", self.chain, 50),
        )
        # aggregator(lower) → markets driven by that Chainlink feed
        self._agg_to_markets: dict[str, list[MorphoMarketConfig]] = {}
        self.oracle_feed_events = 0
        self.feed_cl_moves = 0
        self.feed_cl_try_liq = 0
        self.feed_cl_tick_ms_sum = 0.0
        self.feed_cl_tick_ms_max = 0.0
        self.feed_cl_last_tick_ms = 0.0
        self._feed_cl_ws_t0: float | None = None
        self.hot_refresh_seconds = float(
            bot_config._decimal("MORPHO_HOT_REFRESH_SECONDS", self.chain, "12.0")
        )
        self.near_refresh_seconds = float(
            bot_config._decimal("MORPHO_NEAR_REFRESH_SECONDS", self.chain, "120.0")
        )
        self.near_refresh_batch = max(
            1, bot_config._integer("MORPHO_NEAR_REFRESH_BATCH", self.chain, 40)
        )
        self._near_refresh_cursor: dict[str, int] = {}
        self.worker_count = max(
            1, bot_config._integer("MORPHO_WORKER_COUNT", self.chain, 2)
        )
        self.batch_drain = max(
            1, bot_config._integer("MORPHO_BATCH_DRAIN", self.chain, 24)
        )
        self.hot_hf_threshold = Decimal(
            str(
                bot_config._decimal(
                    "MORPHO_HOT_HF_THRESHOLD", self.chain, str(HOT_HF_THRESHOLD)
                )
            )
        )
        self.oracle_near_hf = Decimal(
            str(
                bot_config._decimal(
                    "MORPHO_ORACLE_NEAR_HF", self.chain, str(ORACLE_NEAR_HF)
                )
            )
        )
        self.feeder_watch = bot_config._flag("MORPHO_FEEDER_WATCH", self.chain, True)
        raw_feeders = bot_config._text("MORPHO_FEEDER_ADDRESSES", self.chain, inherit=False)
        if raw_feeders.strip():
            feeder_list = [p.strip() for p in raw_feeders.split(",") if p.strip()]
        else:
            feeder_list = list(DEFAULT_FEEDER_ADDRESSES)
        self.feeder_addrs: set[str] = {
            Web3.to_checksum_address(a).lower() for a in feeder_list
        }
        # token(lower) → markets that use it as collateral or loan
        self._token_to_markets: dict[str, list[MorphoMarketConfig]] = {}
        self.feeder_events = 0
        self.feeder_recovered = 0
        self.feeder_borrow_fast = 0
        # Keep feeder victims hot long enough to cover fund→oracle→liq (was 20s cap).
        self.feeder_pin_seconds = float(
            bot_config._decimal("MORPHO_FEEDER_PIN_SECONDS", self.chain, "35")
        )
        self.feeder_pin_seconds = min(60.0, max(8.0, self.feeder_pin_seconds))
        if self.feeder_watch:
            for m in self.markets:
                # Collateral only — loan-token Transfer (USDC/WETH) is too noisy;
                # Morpho Borrow still gets PRIO_ORACLE for the USDC-fund path.
                key = m.collateral_token.lower()
                self._token_to_markets.setdefault(key, [])
                if m not in self._token_to_markets[key]:
                    self._token_to_markets[key].append(m)

    def stop(self) -> None:
        self._stop.set()

    def _prio_for_reason(self, reason: str) -> int:
        r = (reason or "").lower()
        if r in {"oracle", "feeder", "oracle-feeder", "feeder-retry"}:
            return PRIO_ORACLE
        if r in {"borrow", "supplycollateral", "supply", "repay", "withdraw", "withdrawcollateral"}:
            # Position opens must beat seed backlog (dust fund→liq ~30s).
            return PRIO_ORACLE
        if r in {"seed"}:
            return PRIO_SEED
        if r in {"book-scan", "book"}:
            return PRIO_BOOK
        if r in {"hot", "hot-scan", "hot-refresh"}:
            return PRIO_HOT
        return PRIO_EVENT

    def _enqueue(
        self,
        market_id: str,
        user: str,
        *,
        reason: str = "",
        priority: int | None = None,
    ) -> None:
        key = PositionKey(market_id=market_id, user=Web3.to_checksum_address(user))
        if key not in self.registry:
            self.registry[key] = TrackedPosition(market_id=market_id, user=key.user)
        if reason:
            self.registry[key].last_event = reason
        prio = self._prio_for_reason(reason) if priority is None else priority
        existing = self._queued.get(key)
        if existing is not None and existing <= prio:
            return
        try:
            self._seq += 1
            item = QueueItem(priority=prio, seq=self._seq, key=key)
            self._queue.put_nowait(item)
            self._queued[key] = prio
            self.registry[key].enqueued_at = item.enqueued_mono
        except asyncio.QueueFull:
            self.log.warning("eval queue full, drop %s %s", market_id[:10], user)

    def _update_hot(self, key: PositionKey, state: Any) -> None:
        tracked = self.registry.get(key)
        pinned = (
            tracked is not None
            and tracked.feeder_pin_until > 0
            and time.monotonic() < tracked.feeder_pin_until
        )
        if (
            state.borrowed_assets > 0
            and state.debt_usd >= self.min_debt_usd
            and state.health_factor < self.hot_hf_threshold
        ):
            added = key not in self.hot_set
            self.hot_set.add(key)
            if added:
                self._prebuild_user(key)
        elif pinned:
            # Feeder just funded — first eval often sees borrow=0; keep hot briefly.
            self.hot_set.add(key)
        else:
            self.hot_set.discard(key)
            if tracked is not None and tracked.feeder_pin_until > 0:
                tracked.feeder_pin_until = 0.0
        # Prebuild near-liq too so oracle tick can fire without route RPC.
        if (
            state.borrowed_assets > 0
            and state.debt_usd >= self.min_debt_usd
            and state.health_factor < self.oracle_near_hf
            and self.executor is not None
        ):
            self._prebuild_user(key)
            # Point 4: arm + off-chain preencode before dump (not only feeder).
            market = self.markets_by_id.get(key.market_id.lower())
            if market is not None:
                self.executor.arm_feeder(market, key.user)
                intent = self._intent_from_tracked(
                    key,
                    hf=state.health_factor,
                    debt_usd=state.debt_usd,
                    reason="near-preencode",
                )
                if intent is not None:
                    try:
                        self.executor.warm_preencode(intent)
                    except Exception as exc:  # noqa: BLE001
                        self.log.debug("near preencode skip %s: %s", key.user[:10], exc)

    def _pin_feeder_victim(self, key: PositionKey) -> None:
        """After feeder Transfer: pin hot + immediate eval + backup re-eval timers."""
        if key not in self.registry:
            self.registry[key] = TrackedPosition(
                market_id=key.market_id, user=key.user
            )
        tracked = self.registry[key]
        tracked.feeder_pin_until = time.monotonic() + self.feeder_pin_seconds
        tracked.feeder_empty_seen = False
        tracked.feeder_evals = 0
        tracked.feeder_recovered_logged = False
        tracked.last_event = "feeder"
        self.hot_set.add(key)
        self._prebuild_user(key)
        # A: arm swap route before Borrow so encode is warm.
        market = self.markets_by_id.get(key.market_id.lower())
        if market is not None and self.executor is not None:
            self.executor.arm_feeder(market, key.user)
        loop = self._loop
        if loop is None:
            return
        # Immediate eval: already-liquidatable victims (no Borrow needed).
        self._schedule_feeder_borrow_fast(key)
        for delay in (1.0, 2.0, 5.0, 10.0, 20.0):
            if delay >= self.feeder_pin_seconds:
                break

            def _retry(d: float = delay, k: PositionKey = key) -> None:
                tr = self.registry.get(k)
                if tr is None or time.monotonic() >= tr.feeder_pin_until:
                    return
                self._enqueue(
                    k.market_id,
                    k.user,
                    reason="feeder-retry",
                    priority=PRIO_FEEDER,
                )

            loop.call_later(delay, _retry)

    def _prebuild_user(self, key: PositionKey) -> None:
        if self.executor is None:
            return
        market = self.markets_by_id.get(key.market_id.lower())
        if market is not None:
            self.executor.prebuild(market, key.user)

    def _cache_position(self, key: PositionKey, state: Any) -> None:
        tracked = self.registry.get(key)
        if tracked is None:
            return
        tracked.last_hf = state.health_factor
        tracked.last_debt_usd = state.debt_usd
        tracked.updated_at = time.time()
        tracked.supply_shares = int(state.supply_shares)
        tracked.borrow_shares = int(state.borrow_shares)
        tracked.collateral = int(state.collateral)
        tracked.total_borrow_assets = int(state.total_borrow_assets)
        tracked.total_borrow_shares = int(state.total_borrow_shares)
        tracked.oracle_price = int(state.oracle_price)
        # Partial liq left debt — lift dead-TTL so we can shoot remainder.
        if self.executor is not None and int(state.borrow_shares) > 0:
            self.executor.clear_position_dead(key.user, key.market_id)

    def _invalidate_cache_after_liquidate(self, market_id: str, borrower: str) -> None:
        """Zero share cache immediately so oracle soft-batch cannot re-CAND."""
        key = PositionKey(market_id=market_id, user=borrower)
        if key not in self.registry:
            self.registry[key] = TrackedPosition(
                market_id=market_id, user=borrower
            )
        tracked = self.registry[key]
        tracked.borrow_shares = 0
        tracked.collateral = 0
        tracked.supply_shares = 0
        tracked.last_hf = None
        tracked.last_debt_usd = 0.0
        tracked.feeder_pin_until = 0.0
        tracked.updated_at = time.time()
        tracked.last_event = "Liquidate"
        self.hot_set.discard(key)
        self.log.info(
            "cache invalidated after Liquidate user=%s market=%s…",
            borrower[:12],
            market_id[:12],
        )
        if self.executor is not None:
            self.executor.clear_feeder_arm(borrower, market_id)

    def _intent_from_tracked(
        self,
        key: PositionKey,
        *,
        hf: Decimal,
        debt_usd: float,
        reason: str,
    ) -> LiqIntent | None:
        market = self.markets_by_id.get(key.market_id.lower())
        tracked = self.registry.get(key)
        if market is None or tracked is None:
            return None
        profit = estimate_liquidation_profit_usd(debt_usd, market.lltv_wad)
        net = 0.0
        if self.executor is not None:
            tmp = LiqIntent(
                chain=self.chain,
                user=key.user,
                market=market,
                health_factor=hf,
                debt_usd=debt_usd,
                profit_usd=profit,
                borrow_shares=tracked.borrow_shares,
                collateral=tracked.collateral,
                total_borrow_assets=tracked.total_borrow_assets,
                total_borrow_shares=tracked.total_borrow_shares,
                oracle_price=tracked.oracle_price,
                loan_decimals=self._loan_decimals.get(key.market_id.lower(), 6),
                reason=reason,
            )
            net = self.executor.net_profit_for(tmp)
        else:
            net = estimate_net_profit_usd(
                debt_usd,
                market.lltv_wad,
                gas_cost_usd=0.0,
                slippage_bps=0,
            ).net_profit_usd
        return LiqIntent(
            chain=self.chain,
            user=key.user,
            market=market,
            health_factor=hf,
            debt_usd=debt_usd,
            profit_usd=profit,
            borrow_shares=tracked.borrow_shares,
            collateral=tracked.collateral,
            total_borrow_assets=tracked.total_borrow_assets,
            total_borrow_shares=tracked.total_borrow_shares,
            oracle_price=tracked.oracle_price,
            loan_decimals=self._loan_decimals.get(key.market_id.lower(), 6),
            reason=reason,
            net_profit_usd=net,
        )

    def _kick_liq(self, intent: LiqIntent | None) -> None:
        if intent is None or self.executor is None:
            return
        self.executor.schedule(intent)

    def _oracle_tick_local(
        self,
        market: MorphoMarketConfig,
        price: int,
        *,
        reason: str = "oracle-local",
        storm: bool = False,
    ) -> None:
        """HF from cached shares + this oracle tick. Soft-batch try_liq without Multicall."""
        mid = market.market_id.lower()
        if storm:
            users = self._users_storm_for_market(mid)
        else:
            users = self._users_for_oracle_refresh(mid)
        path_t0 = self._feed_cl_ws_t0 if "feed-cl" in reason else 0.0
        loan_dec = self._loan_decimals.get(market.market_id.lower(), 6)
        intents: list[LiqIntent] = []
        for user in users:
            key = PositionKey(market_id=market.market_id, user=user)
            tracked = self.registry.get(key)
            if tracked is None or tracked.borrow_shares <= 0 or tracked.collateral <= 0:
                continue
            tracked.oracle_price = int(price)
            hf, borrowed = hf_from_cached_shares(
                collateral=tracked.collateral,
                borrow_shares=tracked.borrow_shares,
                total_borrow_assets=tracked.total_borrow_assets,
                total_borrow_shares=tracked.total_borrow_shares,
                oracle_price=int(price),
                lltv_wad=market.lltv_wad,
            )
            tracked.last_hf = hf
            tracked.last_debt_usd = (borrowed / (10**loan_dec)) * self._loan_usd(market)
            if hf < self.hot_hf_threshold and tracked.last_debt_usd >= self.min_debt_usd:
                self.hot_set.add(key)
                self._prebuild_user(key)
            if hf >= self.hf_threshold:
                continue
            intent = self._intent_from_tracked(
                key, hf=hf, debt_usd=tracked.last_debt_usd, reason=reason
            )
            if intent is not None:
                # Feeder-pinned/armed: tag fast path even without Morpho Borrow
                # (oracle dump after collateral fund — common dust race).
                if self._feeder_hot(key):
                    intent.reason = f"feeder-fast:{reason}"
                    self.feeder_borrow_fast += 1
                if path_t0 > 0:
                    intent.path_t0 = path_t0
                intents.append(intent)
        if not intents:
            if path_t0 > 0:
                ms = (time.monotonic() - path_t0) * 1000.0
                self.feed_cl_last_tick_ms = ms
                self.feed_cl_tick_ms_sum += ms
                self.feed_cl_tick_ms_max = max(self.feed_cl_tick_ms_max, ms)
                self._feed_cl_ws_t0 = None
            return
        # Bigger debt first — cascade meat before dust.
        intents.sort(key=lambda i: i.debt_usd, reverse=True)
        if path_t0 > 0:
            ms = (time.monotonic() - path_t0) * 1000.0
            self.feed_cl_last_tick_ms = ms
            self.feed_cl_tick_ms_sum += ms
            self.feed_cl_tick_ms_max = max(self.feed_cl_tick_ms_max, ms)
            self.feed_cl_try_liq += len(intents)
            self._feed_cl_ws_t0 = None
            self.log.info(
                "feed-cl tick %.1fms ws->try_liq n=%d %s/%s",
                ms,
                len(intents),
                market.loan_symbol,
                market.collateral_symbol,
            )
        if self.executor is not None:
            self.executor.schedule_many(
                intents, reason=f"{reason}:{market.collateral_symbol}"
            )
        self.log.info(
            "oracle local HF %s/%s: %d try_liq soft-batch (no Multicall) reason=%s",
            market.loan_symbol,
            market.collateral_symbol,
            len(intents),
            reason,
        )

    def _handle_oracle_move(
        self,
        market: MorphoMarketConfig,
        price: int,
        *,
        source: str,
    ) -> None:
        """Shared path: poller + Chainlink AnswerUpdated WS."""
        mid = market.market_id.lower()
        prev = self._oracle_prices.get(mid)
        self._oracle_prices[mid] = price
        if prev is not None and prev == price:
            return
        if source == "feed-cl":
            self.feed_cl_moves += 1
        self.oracle_price_changes += 1
        bps = 0
        if prev is not None and prev > 0:
            bps = abs(price - prev) * 10_000 // prev
        if self.executor is not None:
            self.executor.maybe_storm_from_oracle_bps(bps)
        hot_n = len(self._users_hot_for_market(mid))
        near_n = len(self._users_near_for_market(mid))
        self.log.info(
            "oracle move %s/%s Δ=%d bps (~%d) src=%s → local tick hot=%d near=%d storm=%s",
            market.loan_symbol,
            market.collateral_symbol,
            bps,
            price,
            source,
            hot_n,
            near_n,
            bps >= self.oracle_storm_wide_bps,
        )
        storm = bps >= self.oracle_storm_wide_bps
        self._oracle_tick_local(
            market, price, reason=f"oracle-{source}", storm=storm
        )
        users = self._users_hot_for_market(mid)
        if not users:
            return
        # Multicall refresh is secondary — kick already fired from cached shares.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _refresh() -> None:
            try:
                async with self._eval_rpc_sem:
                    cands = await asyncio.to_thread(
                        self.refresh_market_batch, market, users
                    )
                for cand in cands:
                    self._log_candidate(cand)
            except Exception as exc:  # noqa: BLE001
                self.log.error(
                    "oracle refresh failed %s: %s", market.market_id[:12], exc
                )
                for u in users:
                    self._enqueue(
                        market.market_id, u, reason="oracle", priority=PRIO_ORACLE
                    )

        loop.create_task(_refresh(), name=f"morpho-oracle-refresh-{mid[:10]}")

    def _build_feed_map(self) -> None:
        """Resolve Chainlink aggregators for watched Morpho oracles."""
        from morpho_hf import KNOWN_ORACLE_AGGREGATORS

        mapping: dict[str, list[MorphoMarketConfig]] = {}
        for market in self.markets:
            aggs: list[str] = []
            try:
                aggs = resolve_morpho_oracle_aggregators(self.w3, market)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "oracle feed resolve failed %s/%s: %s",
                    market.loan_symbol,
                    market.collateral_symbol,
                    exc,
                )
            if not aggs:
                aggs = [
                    Web3.to_checksum_address(a)
                    for a in KNOWN_ORACLE_AGGREGATORS.get(market.oracle.lower(), [])
                ]
            if not aggs:
                self.log.info(
                    "oracle feed: no aggregator for %s/%s (poll-only)",
                    market.loan_symbol,
                    market.collateral_symbol,
                )
                continue
            for agg in aggs:
                mapping.setdefault(agg.lower(), []).append(market)
            self.log.info(
                "oracle feed %s/%s → %s",
                market.loan_symbol,
                market.collateral_symbol,
                ",".join(a[:10] for a in aggs),
            )
        self._agg_to_markets = mapping
        roles: dict[str, tuple[list[str], list[str]]] = {}
        for market in self.markets:
            mid = market.market_id.lower()
            try:
                roles[mid] = resolve_morpho_oracle_feed_roles(self.w3, market)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "oracle feed roles failed %s/%s: %s",
                    market.loan_symbol,
                    market.collateral_symbol,
                    exc,
                )
                roles[mid] = ([], [])
            for agg in roles[mid][0] + roles[mid][1]:
                if agg not in self._cl_answer:
                    ans = read_chainlink_answer(self.w3, agg)
                    if ans is not None:
                        self._cl_answer[agg] = ans
        self._market_feed_roles = roles
        self.log.info(
            "oracle feed map: %d aggregators → %d markets (ws=%s pending=%s cl=%d)",
            len(mapping),
            len(self.markets),
            self.oracle_feed_ws,
            self.oracle_pending_price,
            len(self._cl_answer),
        )

    def seed(self) -> None:
        pairs: list[tuple[str, str]] = []
        for m in self.markets:
            n = self.seed_per_market * 2 if m.priority <= 1 else self.seed_per_market
            pairs.extend(
                seed_borrowers_from_api(self.chain_id, [m], per_market=n)
            )
        # Priority markets first in equal-prio seed queue (lower seq earlier).
        pairs.sort(
            key=lambda p: self.markets_by_id[p[0].lower()].priority
            if p[0].lower() in self.markets_by_id
            else 999
        )
        for mid, user in pairs:
            self._enqueue(mid, user, reason="seed")
        for m in self.markets:
            try:
                self._loan_decimals[m.market_id.lower()] = self.reader.token_decimals(
                    m.loan_token
                )
            except Exception:  # noqa: BLE001
                self._loan_decimals[m.market_id.lower()] = 6
        self.log.info(
            "registry=%d markets=%d min_debt=$%.0f min_net=$%.4f max_debt=%s "
            "diagnostic=%s hot_hf<%.2f oracle_poll=%.1fs",
            len(self.registry),
            len(self.markets),
            self.min_debt_usd,
            self.min_net_profit_usd,
            "off" if self.max_debt_usd <= 0 else f"${self.max_debt_usd:,.0f}",
            self.diagnostic_mode,
            self.hot_hf_threshold,
            self.oracle_poll_seconds,
        )

    def _loan_usd(self, market: MorphoMarketConfig) -> float:
        eth = self.executor.eth_usd_price() if self.executor is not None else 3500.0
        return loan_token_price_usd(market.loan_symbol, eth_usd=eth)

    def _max_debt_cap(self) -> float | None:
        return None if self.max_debt_usd <= 0 else self.max_debt_usd

    def evaluate_key(self, key: PositionKey) -> MorphoCandidate | None:
        market = self.markets_by_id.get(key.market_id.lower())
        if market is None:
            return None
        state = self.reader.read_position(
            market, key.user, loan_price_usd=self._loan_usd(market)
        )
        if state is None:
            return None
        tracked = self.registry.get(key)
        if tracked is not None and tracked.feeder_pin_until > time.monotonic():
            tracked.feeder_evals += 1
            if state.borrowed_assets == 0:
                tracked.feeder_empty_seen = True
        self._cache_position(key, state)
        self._update_hot(key, state)
        # Drop emptied borrows from hot registry pressure (keep lightly).
        if state.borrowed_assets == 0:
            return None
        return self.reader.build_candidate(
            self.chain,
            market,
            state,
            hf_threshold=self.hf_threshold,
            min_debt_usd=self.min_debt_usd,
            max_debt_usd=self._max_debt_cap(),
            gas_cost_usd=self._gas_cost_usd(),
            slippage_bps=self._slippage_bps(),
        )

    def _gas_cost_usd(self) -> float:
        if self.executor is not None:
            return self.executor.gas_cost_usd()
        return 0.0

    def _slippage_bps(self) -> int:
        if self.executor is not None:
            return int(self.executor.slippage_bps)
        return 200

    def _apply_state(
        self, key: PositionKey, state: Any
    ) -> MorphoCandidate | None:
        tracked = self.registry.get(key)
        if tracked is not None and tracked.feeder_pin_until > time.monotonic():
            tracked.feeder_evals += 1
            if state.borrowed_assets == 0:
                tracked.feeder_empty_seen = True
        self._cache_position(key, state)
        self._update_hot(key, state)
        if state.borrowed_assets == 0:
            return None
        market = self.markets_by_id.get(key.market_id.lower())
        if market is None:
            return None
        return self.reader.build_candidate(
            self.chain,
            market,
            state,
            hf_threshold=self.hf_threshold,
            min_debt_usd=self.min_debt_usd,
            max_debt_usd=self._max_debt_cap(),
            gas_cost_usd=self._gas_cost_usd(),
            slippage_bps=self._slippage_bps(),
        )

    def refresh_market_batch(
        self,
        market: MorphoMarketConfig,
        users: list[str],
        *,
        reader: MorphoReader | None = None,
    ) -> list[MorphoCandidate]:
        """Multicall position reads for a market (oracle / hot path)."""
        if not users:
            return []
        r = reader if reader is not None else self.reader
        states = r.read_positions_batch(
            market, users, loan_price_usd=self._loan_usd(market)
        )
        found: list[MorphoCandidate] = []
        for state in states:
            key = PositionKey(market_id=market.market_id, user=state.user)
            if key not in self.registry:
                self.registry[key] = TrackedPosition(
                    market_id=market.market_id, user=state.user
                )
            cand = self._apply_state(key, state)
            if cand is not None:
                found.append(cand)
        return found

    def _log_candidate(self, cand: MorphoCandidate) -> None:
        cap = self._max_debt_cap()
        if cap is not None and cand.debt_usd > cap:
            self.log.debug(
                "skip whale candidate %s debt=$%.0f > max $%.0f",
                cand.user,
                cand.debt_usd,
                cap,
            )
            return
        net = cand.net_profit_usd
        if self.diagnostic_mode and net < self.min_net_profit_usd:
            self.log.debug(
                "skip diagnostic net_profit_usd=$%.4f < $%.4f %s",
                net,
                self.min_net_profit_usd,
                cand.user,
            )
            return
        self.candidates_seen += 1
        key = PositionKey(market_id=cand.market_id, user=cand.user)
        tracked = self.registry.get(key)
        if (
            tracked is not None
            and not tracked.feeder_recovered_logged
            and tracked.feeder_pin_until > 0
            and (
                tracked.feeder_empty_seen
                or tracked.feeder_evals >= 2
                or (tracked.last_event or "").lower()
                in {"borrow", "supplycollateral", "feeder-retry"}
            )
        ):
            self.feeder_recovered += 1
            tracked.feeder_recovered_logged = True
            self.log.info(
                "feeder recovered via retry n=%d evals=%d empty_seen=%s last=%s user=%s",
                self.feeder_recovered,
                tracked.feeder_evals,
                tracked.feeder_empty_seen,
                tracked.last_event,
                cand.user,
            )
        self.log.info(
            "[dry-run] would liquidate morpho %s (HF=%.4f, debtToCover=%d, "
            "~$%.0f debt ~$%.2f profit net_profit_usd=$%.4f) %s/%s market=%s...",
            cand.user,
            cand.health_factor,
            cand.debt_to_cover,
            cand.debt_usd,
            cand.estimated_profit_usd,
            net,
            cand.loan_symbol,
            cand.collateral_symbol,
            cand.market_id[:18],
        )
        self.log.info(
            "CANDIDATE morpho dry-run | chain=%s protocol=%s user=%s "
            "marketId=%s HF=%.4f debtToCover=%d collateral=%s borrow=%s "
            "estProfit=$%.2f debtUsd=$%.2f net_profit_usd=$%.4f gas=$%.4f "
            "slip=$%.4f flash_fee=$%.4f diagnostic=%s",
            cand.chain,
            cand.protocol,
            cand.user,
            cand.market_id,
            cand.health_factor,
            cand.debt_to_cover,
            cand.collateral_asset,
            cand.borrow_asset,
            cand.estimated_profit_usd,
            cand.debt_usd,
            net,
            cand.gas_cost_usd,
            cand.slippage_usd,
            cand.flash_fee_usd,
            self.diagnostic_mode,
        )
        self._kick_from_candidate(cand)
        extra = {
            "net_profit_usd": round(net, 6),
            "gas_cost_usd": round(cand.gas_cost_usd, 6),
            "slippage_usd": round(cand.slippage_usd, 6),
            "flash_fee_usd": round(cand.flash_fee_usd, 6),
            "diagnostic": self.diagnostic_mode,
        }
        if net < self.min_net_profit_usd:
            return
        self.scoreboard.remember_candidate(
            user=cand.user,
            market_id=cand.market_id,
            debt_usd=cand.debt_usd,
            profit_usd=cand.estimated_profit_usd,
            health_factor=float(cand.health_factor),
            pair=f"{cand.loan_symbol}/{cand.collateral_symbol}",
            extra=extra,
        )
        diag = "диагностика, не боевой режим\n" if self.diagnostic_mode else ""
        self._schedule_alert(
            f"Кандидат Morpho (без отправки)\n"
            f"{diag}"
            f"{cand.user}\n"
            f"{cand.loan_symbol}/{cand.collateral_symbol}\n"
            f"HF={cand.health_factor:.4f}  LLTV={cand.lltv*100:.1f}%\n"
            f"долг ~${cand.debt_usd:,.0f} | оценка прибыли ~${cand.estimated_profit_usd:,.2f}\n"
            f"чистыми ${net:,.4f}\n"
            f"рынок={cand.market_id[:22]}...\n"
            f"итого за сессию: кандидатов {self.candidates_seen}",
            dedup_key=f"morpho-cand-{self.chain}-{cand.market_id[:18]}-{cand.user}",
            cooldown=120.0,
        )

    def _schedule_alert(
        self,
        text: str,
        *,
        dedup_key: str | None,
        cooldown: float = 300.0,
    ) -> None:
        if self.notifier is None or not self.notifier.enabled:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def _send() -> None:
            assert self.notifier is not None
            await self.notifier.send(
                f"[morpho/{self.chain}] {text}",
                dedup_key=dedup_key,
                cooldown=cooldown,
            )

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(_send())
        else:
            loop.call_soon_threadsafe(lambda: loop.create_task(_send()))

    def _feeder_hot(self, key: PositionKey) -> bool:
        """Pinned Transfer window and/or executor arm still active."""
        tracked = self.registry.get(key)
        pinned = (
            tracked is not None
            and tracked.feeder_pin_until > 0
            and time.monotonic() < tracked.feeder_pin_until
        )
        armed = (
            self.executor is not None
            and self.executor.is_feeder_armed(key.user, key.market_id)
        )
        return bool(pinned or armed)

    def _kick_from_candidate(self, cand: MorphoCandidate) -> None:
        key = PositionKey(market_id=cand.market_id, user=cand.user)
        tracked = self.registry.get(key)
        reason = tracked.last_event if tracked is not None else "candidate"
        if self._feeder_hot(key) and not str(reason).lower().startswith("feeder"):
            reason = f"feeder-fast:{reason or 'candidate'}"
            self.feeder_borrow_fast += 1
        self._kick_liq(
            self._intent_from_tracked(
                key,
                hf=cand.health_factor,
                debt_usd=cand.debt_usd,
                reason=reason or "candidate",
            )
        )

    async def _alert(
        self,
        text: str,
        *,
        dedup_key: str | None = None,
        cooldown: float = 300.0,
    ) -> None:
        if self.notifier is None or not self.notifier.enabled:
            return
        await self.notifier.send(
            f"[morpho/{self.chain}] {text}",
            dedup_key=dedup_key,
            cooldown=cooldown,
        )

    async def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                raise
            # Drain same-priority peers for multicall batching by market.
            batch = [item]
            while len(batch) < self.batch_drain:
                try:
                    nxt = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                batch.append(nxt)

            # Drop stale duplicates inside the batch.
            fresh: list[QueueItem] = []
            for it in batch:
                cur = self._queued.get(it.key)
                if cur is not None and cur < it.priority:
                    self._queue.task_done()
                    continue
                self._queued.pop(it.key, None)
                fresh.append(it)
            if not fresh:
                continue

            wait_ms = (time.monotonic() - fresh[0].enqueued_mono) * 1000.0
            self.queue_wait_ms_sum += wait_ms

            by_market: dict[str, list[QueueItem]] = {}
            for it in fresh:
                by_market.setdefault(it.key.market_id.lower(), []).append(it)

            try:
                for mid, items in by_market.items():
                    market = self.markets_by_id.get(mid)
                    if market is None:
                        continue
                    users = [it.key.user for it in items]
                    t0 = 0.0
                    try:
                        async with self._eval_rpc_sem:
                            t0 = time.monotonic()
                            if len(users) >= 2:
                                self.batch_evals += 1
                                cands = await asyncio.to_thread(
                                    self.refresh_market_batch, market, users
                                )
                                for cand in cands:
                                    self._log_candidate(cand)
                            else:
                                cand = await asyncio.to_thread(
                                    self.evaluate_key, items[0].key
                                )
                                if cand is not None:
                                    self._log_candidate(cand)
                    except Exception as exc:  # noqa: BLE001
                        self.log.error(
                            "eval batch failed %s n=%d: %s", mid[:10], len(users), exc
                        )
                    finally:
                        if t0 > 0:
                            dt = (time.monotonic() - t0) * 1000.0
                            self.eval_count += len(users)
                            self.eval_latency_ms_sum += dt
            except asyncio.CancelledError:
                raise
            finally:
                for _ in fresh:
                    self._queue.task_done()

    async def _hot_refresher(self) -> None:
        """Periodic Multicall sweep of hot-set even without oracle moves."""
        if self.hot_refresh_seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=20.0)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            by_market: dict[str, list[str]] = {}
            for key in list(self.hot_set):
                by_market.setdefault(key.market_id.lower(), []).append(key.user)
            for mid, users in by_market.items():
                if self._stop.is_set():
                    return
                market = self.markets_by_id.get(mid)
                if market is None or not users:
                    continue
                try:
                    async with self._eval_rpc_sem:
                        cands = await asyncio.to_thread(
                            self.refresh_market_batch, market, users
                        )
                    for cand in cands:
                        self._log_candidate(cand)
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("hot refresh %s: %s", mid[:12], exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.hot_refresh_seconds
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _near_hf_refresher(self) -> None:
        """Low-priority sweep of near-HF registry (HF<oracle_near_hf), chunked — no eval lock."""
        if self.near_refresh_seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=90.0)
            return
        except asyncio.TimeoutError:
            pass
        self.log.info(
            "near-HF refresher: every %.0fs batch=%d (hot excluded — oracle move path)",
            self.near_refresh_seconds,
            self.near_refresh_batch,
        )
        while not self._stop.is_set():
            for market in self.markets:
                if self._stop.is_set():
                    return
                mid = market.market_id.lower()
                near = self._users_near_for_market(mid)
                if not near:
                    continue
                cur = self._near_refresh_cursor.get(mid, 0) % len(near)
                end = cur + self.near_refresh_batch
                chunk = near[cur:end]
                if len(chunk) < self.near_refresh_batch:
                    chunk = chunk + near[: self.near_refresh_batch - len(chunk)]
                self._near_refresh_cursor[mid] = (cur + self.near_refresh_batch) % len(
                    near
                )
                try:
                    async with self._oracle_rpc_lock:
                        cands = await asyncio.to_thread(
                            self.refresh_market_batch,
                            market,
                            chunk,
                            reader=self.reader_oracle,
                        )
                    for cand in cands:
                        self._log_candidate(cand)
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("near hf refresh %s: %s", mid[:12], exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.near_refresh_seconds
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _book_scanner(self) -> None:
        if self.book_scan_seconds <= 0:
            return
        # First pass after WS settles / seed drains a bit.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=45.0)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            keys = sorted(
                self.hot_set,
                key=lambda k: self.markets_by_id.get(
                    k.market_id.lower(),
                    MorphoMarketConfig(
                        chain=self.chain,
                        market_id=k.market_id,
                        loan_symbol="?",
                        collateral_symbol="?",
                        loan_token="0x" + "00" * 20,
                        collateral_token="0x" + "00" * 20,
                        oracle="0x" + "00" * 20,
                        irm="0x" + "00" * 20,
                        lltv_wad=0,
                        priority=999,
                    ),
                ).priority,
            )
            self.log.info(
                "book scan: enqueueing %d hot morpho positions (tracked=%d)",
                len(keys),
                len(self.registry),
            )
            for key in keys:
                if self._stop.is_set():
                    return
                self._enqueue(
                    key.market_id, key.user, reason="book-scan", priority=PRIO_BOOK
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=float(self.book_scan_seconds)
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _oracle_poller(self) -> None:
        """Poll priority-market oracles; on move, kick local HF then Multicall hot."""
        if self.oracle_poll_seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=8.0)
            return
        except asyncio.TimeoutError:
            pass

        # Priority markets get the fast path (cbXRP/USDe/cbADA…).
        watch_fast = [m for m in self.markets if m.priority <= 1]
        watch_norm = [m for m in self.markets if 1 < m.priority <= 10]
        if not watch_fast and not watch_norm:
            watch_norm = list(self.markets)
        self.log.info(
            "oracle poller: fast=%d@%.2fs norm=%d@%.2fs pending=%s",
            len(watch_fast),
            self.oracle_poll_fast_seconds,
            len(watch_norm),
            self.oracle_poll_seconds,
            self.oracle_pending_price,
        )

        tick = 0
        while not self._stop.is_set():
            tick += 1
            to_poll = list(watch_fast)
            # Norm markets every other fast tick (~2s if fast=1s).
            if tick % max(
                1,
                int(
                    self.oracle_poll_seconds
                    / max(self.oracle_poll_fast_seconds, 0.2)
                ),
            ) == 0:
                to_poll.extend(watch_norm)
            for market in to_poll:
                if self._stop.is_set():
                    return
                try:
                    price = await asyncio.to_thread(
                        partial(
                            self.reader.read_oracle_price,
                            market,
                            pending=self.oracle_pending_price,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.warning(
                        "oracle poll failed %s: %s", market.market_id[:12], exc
                    )
                    continue
                if price is None:
                    continue
                self._handle_oracle_move(market, price, source="poll")

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.oracle_poll_fast_seconds
                )
                return
            except asyncio.TimeoutError:
                pass

    def _users_hot_for_market(self, market_id_lower: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for key in self.hot_set:
            if key.market_id.lower() != market_id_lower:
                continue
            u = key.user.lower()
            if u in seen:
                continue
            seen.add(u)
            out.append(key.user)
        return out

    def _users_near_for_market(self, market_id_lower: str) -> list[str]:
        """Registry near-HF positions excluding hot_set (background refresh path)."""
        hot_addrs = {
            key.user.lower()
            for key in self.hot_set
            if key.market_id.lower() == market_id_lower
        }
        seen: set[str] = set()
        out: list[str] = []
        for key, tracked in self.registry.items():
            if key.market_id.lower() != market_id_lower:
                continue
            u = key.user.lower()
            if u in hot_addrs or u in seen:
                continue
            if tracked.last_hf is None:
                continue
            if tracked.last_hf >= self.oracle_near_hf:
                continue
            if tracked.last_debt_usd < self.min_debt_usd:
                continue
            seen.add(u)
            out.append(key.user)
        return out

    def _users_storm_for_market(self, market_id_lower: str) -> list[str]:
        """All tracked borrowers on a market — used on large oracle moves (cascade)."""
        seen: set[str] = set()
        out: list[str] = []
        for key, tracked in self.registry.items():
            if key.market_id.lower() != market_id_lower:
                continue
            if tracked.borrow_shares <= 0 or tracked.collateral <= 0:
                continue
            u = key.user.lower()
            if u in seen:
                continue
            seen.add(u)
            out.append(key.user)
        return out

    def _users_for_oracle_refresh(self, market_id_lower: str) -> list[str]:
        """Hot first, then near-HF — local tick must cover both."""
        hot = self._users_hot_for_market(market_id_lower)
        near = self._users_near_for_market(market_id_lower)
        if not near:
            return hot
        seen = {u.lower() for u in hot}
        out = list(hot)
        for u in near:
            if u.lower() in seen:
                continue
            seen.add(u.lower())
            out.append(u)
        return out

    async def _heartbeat(self) -> None:
        while not self._stop.is_set():
            up = (
                time.monotonic() - self._connected_since
                if self._connected_since is not None
                else 0.0
            )
            avg_eval = (
                self.eval_latency_ms_sum / self.eval_count if self.eval_count else 0.0
            )
            avg_wait = (
                self.queue_wait_ms_sum / self.eval_count if self.eval_count else 0.0
            )
            feed_cl_avg_tick = (
                self.feed_cl_tick_ms_sum / self.feed_cl_moves
                if self.feed_cl_moves
                else 0.0
            )
            self.log.info(
                "alive: chain=%s up=%.0fs raw_logs=%d events=%d candidates=%d "
                "foreign_liq=%d discovery_liq=%d tracked=%d hot=%d queued=%d "
                "oracle_moves=%d feed_ev=%d feed_cl=%d try_liq=%d tick_ms=%.0f/%.0f "
                "feeder_ev=%d feeder_rec=%d "
                "feeder_fast=%d "
                "batch=%d eval_ms=%.0f wait_ms=%.0f scoreboard_cand=%d "
                "scoreboard_miss=%d/%d reconnects=%d diagnostic=%s",
                self.chain,
                up,
                self.raw_logs_seen,
                self.events_seen,
                self.candidates_seen,
                self.foreign_liq_seen,
                self.discovery_liq_seen,
                len(self.registry),
                len(self.hot_set),
                self._queue.qsize(),
                self.oracle_price_changes,
                self.oracle_feed_events,
                self.feed_cl_moves,
                self.feed_cl_try_liq,
                self.feed_cl_last_tick_ms,
                feed_cl_avg_tick,
                self.feeder_events,
                self.feeder_recovered,
                self.feeder_borrow_fast,
                self.batch_evals,
                avg_eval,
                avg_wait,
                self.scoreboard.candidates,
                self.scoreboard.raced_and_missed,
                self.scoreboard.never_saw,
                self.reconnects,
                self.diagnostic_mode,
            )
            if self.executor is not None:
                self.log.info(
                    "liq_exec: mode=%s %s",
                    self.executor.mode,
                    self.executor.metrics.snapshot(),
                )
                if self.exec_metrics_tg_seconds > 0:
                    now = time.monotonic()
                    due = (
                        self._last_exec_metrics_tg is None
                        or (now - self._last_exec_metrics_tg)
                        >= self.exec_metrics_tg_seconds
                    )
                    if due:
                        self._last_exec_metrics_tg = now
                        live_line = (
                            "режим live, отправка включена"
                            if self.executor.mode == "live"
                            else "режим observe/paper, без отправки"
                        )
                        self._schedule_alert(
                            f"Статус Morpho ({self.executor.mode})\n"
                            f"{self.executor.metrics.snapshot_ru()}\n"
                            f"{live_line}",
                            dedup_key=f"morpho-exec-metrics-{self.chain}",
                            cooldown=max(60.0, self.exec_metrics_tg_seconds * 0.9),
                        )
            if self.flash_arb.active:
                self.log.info(
                    "arb: chain=%s scans=%d hits=%d est_profit_usd~$%.2f",
                    self.chain,
                    self.flash_arb.scans,
                    self.flash_arb.hits,
                    self.flash_arb.est_profit_usd_sum,
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=float(self.heartbeat_seconds)
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _arb_ticker(self) -> None:
        """Periodic Morpho-flash DEX arb quotes (observation)."""
        if not self.flash_arb.active:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=15.0)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                async with self._oracle_rpc_lock:
                    opps = await asyncio.to_thread(self.flash_arb.maybe_scan)
                for opp in opps:
                    if opp.borrow_symbol != "USDC":
                        continue
                    profit_usd = opp.gross_profit / 1e6
                    if profit_usd < 0.5:
                        continue
                    self._schedule_alert(
                        f"Morpho flash-arb (без отправки)\n"
                        f"{opp.kind} {opp.borrow_symbol}->{opp.mid_symbol}->"
                        f"{opp.borrow_symbol}\n"
                        f"прибыль ~${profit_usd:,.2f} ({opp.profit_bps:.1f} bps)\n"
                        f"размер={opp.amount} комиссия Morpho=0\n"
                        f"итого хитов: {self.flash_arb.hits}",
                        dedup_key=(
                            f"morpho-arb-{self.chain}-{opp.kind}-"
                            f"{opp.borrow_symbol}-{opp.amount}"
                        ),
                        cooldown=300.0,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning("arb ticker: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(5.0, self.flash_arb.scan_interval),
                )
                return
            except asyncio.TimeoutError:
                pass

    def _build_provider(self) -> WebSocketProvider:
        return WebSocketProvider(
            self.ws_url,
            max_connection_retries=1,
            request_timeout=self.ws_request_timeout,
            websocket_kwargs={
                "open_timeout": self.ws_connect_timeout,
                "close_timeout": 5,
                "ping_interval": self.ws_ping_interval,
                "ping_timeout": self.ws_ping_timeout,
                "max_size": 16 * 1024 * 1024,
            },
        )

    async def _subscribe(self, w3: AsyncWeb3) -> str:
        # Address-only filter: public free WS often drops OR-topic subscriptions.
        # Topic matching is done client-side in _on_log.
        params = {"address": Web3.to_checksum_address(MORPHO_BLUE)}
        async with asyncio.timeout(15):
            return await w3.eth.subscribe("logs", params)

    async def _subscribe_oracle_feeds(self, w3: AsyncWeb3, aggregators: list[str]) -> str:
        """One batched AnswerUpdated subscription (Aave pattern)."""
        params = {
            "address": [Web3.to_checksum_address(a) for a in aggregators],
            "topics": [ANSWER_UPDATED_TOPIC],
        }
        async with asyncio.timeout(15):
            return await w3.eth.subscribe("logs", params)

    def _on_oracle_feed_log(self, entry: Mapping[str, Any]) -> None:
        addr = entry.get("address")
        if addr is None:
            return
        if hasattr(addr, "hex"):
            addr_s = topic_hex(addr)
        else:
            addr_s = topic_hex(str(addr))
        # address may be 32-byte topic-padded in some providers — take last 20 bytes
        if len(addr_s) > 42:
            addr_s = "0x" + addr_s[-40:]
        agg_lower = addr_s.lower()
        markets = self._agg_to_markets.get(agg_lower)
        if not markets:
            return
        self.oracle_feed_events += 1
        new_cl = parse_answer_updated_current(entry)
        old_answers = dict(self._cl_answer)
        if new_cl is not None:
            self._cl_answer[agg_lower] = int(new_cl)
        seen_mids: set[str] = set()
        for market in markets:
            mid = market.market_id.lower()
            if mid in seen_mids:
                continue
            seen_mids.add(mid)
            morpho_prev = self._oracle_prices.get(mid)
            base_aggs, quote_aggs = self._market_feed_roles.get(mid, ([], []))
            projected: int | None = None
            if morpho_prev and new_cl is not None:
                old_cl = old_answers.get(agg_lower)
                if old_cl and old_cl != new_cl:
                    projected = project_morpho_price(
                        morpho_prev,
                        old_answers,
                        self._cl_answer,
                        base_aggs,
                        quote_aggs,
                    )
            morpho_read: int | None = None
            try:
                morpho_read = self.reader.read_oracle_price(
                    market, pending=self.oracle_pending_price
                )
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "oracle feed price read failed %s/%s: %s",
                    market.loan_symbol,
                    market.collateral_symbol,
                    exc,
                )
            if projected is not None:
                self._feed_cl_ws_t0 = time.monotonic()
                self._handle_oracle_move(market, projected, source="feed-cl")
            elif morpho_read is not None:
                self._handle_oracle_move(market, morpho_read, source="feed-ws")

    async def _oracle_feed_session(self) -> None:
        if not self._agg_to_markets:
            return
        aggregators = [Web3.to_checksum_address(a) for a in self._agg_to_markets]
        self.log.info(
            "connecting oracle-feed WS %s aggs=%d",
            bot_config.rpc_host(self.ws_url),
            len(aggregators),
        )
        w3 = AsyncWeb3(self._build_provider())
        async with asyncio.timeout(self.ws_connect_timeout):
            await w3.provider.connect()
        try:
            try:
                sub_id = await self._subscribe_oracle_feeds(w3, aggregators)
            except Exception as exc:  # noqa: BLE001
                # Fallback: per-aggregator subs if batched filter rejected.
                self.log.warning(
                    "batched oracle-feed sub failed (%s) — trying per-feed", exc
                )
                sub_id = None
                for agg in aggregators:
                    try:
                        await self._subscribe_oracle_feeds(w3, [agg])
                    except Exception as exc2:  # noqa: BLE001
                        self.log.warning("oracle-feed sub %s: %s", agg[:10], exc2)
                if sub_id is None:
                    self.log.info("oracle-feed: per-feed subscriptions active")
            else:
                self.log.info("subscribed oracle feeds sub=%s aggs=%d", sub_id, len(aggregators))
            iterator = w3.socket.process_subscriptions().__aiter__()
            while not self._stop.is_set():
                try:
                    async with asyncio.timeout(self.ws_silence_timeout):
                        message = await iterator.__anext__()
                except asyncio.TimeoutError:
                    await self._probe(w3)
                    continue
                except StopAsyncIteration as exc:
                    raise ConnectionError("oracle-feed subscription ended") from exc

                result = message.get("result") if isinstance(message, dict) else None
                if isinstance(result, Mapping) and result.get("topics"):
                    self._on_oracle_feed_log(result)
                elif isinstance(message, Mapping) and message.get("topics"):
                    self._on_oracle_feed_log(message)
        finally:
            with suppress(Exception):
                await w3.provider.disconnect()

    async def _oracle_feed_watcher(self) -> None:
        """Dedicated WS for Chainlink AnswerUpdated → same-block kick path."""
        if not self.oracle_feed_ws:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            return
        except asyncio.TimeoutError:
            pass
        if not self._agg_to_markets:
            self.log.info("oracle-feed watcher: no aggregators — idle")
            return
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._oracle_feed_session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                delay = self._backoff(attempt)
                self.log.warning(
                    "oracle-feed WS down (%s) retry in %.1fs", exc, delay
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass

    async def _subscribe_feeder_transfers(
        self, w3: AsyncWeb3, tokens: list[str], feeders: list[str]
    ) -> str:
        """ERC20 Transfer where from ∈ feeders, on watchlist collateral/loan tokens."""
        params = {
            "address": [Web3.to_checksum_address(t) for t in tokens],
            "topics":                 [
                    ERC20_TRANSFER_TOPIC,
                    [_addr_topic(f) for f in feeders],
                ],
        }
        async with asyncio.timeout(15):
            return await w3.eth.subscribe("logs", params)

    def _on_feeder_transfer(self, entry: Mapping[str, Any]) -> None:
        """Dust feeder funded a wallet — track + eval at oracle priority."""
        topics = entry.get("topics") or []
        if len(topics) < 3:
            return
        addr = entry.get("address")
        if addr is None:
            return
        if hasattr(addr, "hex"):
            tok = topic_hex(addr)
        else:
            tok = topic_hex(str(addr))
        if len(tok) > 42:
            tok = "0x" + tok[-40:]
        markets = self._token_to_markets.get(tok.lower())
        if not markets:
            return
        frm = _topic_addr(topics[1]).lower()
        if frm not in self.feeder_addrs:
            return
        to = _topic_addr(topics[2])
        if int(to, 16) == 0:
            return
        self.feeder_events += 1
        self.log.info(
            "feeder transfer %s→%s token=%s markets=%d",
            frm[:10],
            to[:10],
            tok[:10],
            len(markets),
        )
        for market in markets:
            key = PositionKey(
                market_id=market.market_id, user=Web3.to_checksum_address(to)
            )
            self._pin_feeder_victim(key)
            self._enqueue(
                market.market_id,
                to,
                reason="feeder",
                priority=PRIO_FEEDER,
            )

    async def _feeder_watch_session(self) -> None:
        tokens = list(self._token_to_markets.keys())
        feeders = list(self.feeder_addrs)
        if not tokens or not feeders:
            return
        self.log.info(
            "connecting feeder-watch WS %s tokens=%d feeders=%d",
            bot_config.rpc_host(self.ws_url),
            len(tokens),
            len(feeders),
        )
        w3 = AsyncWeb3(self._build_provider())
        async with asyncio.timeout(self.ws_connect_timeout):
            await w3.provider.connect()
        try:
            try:
                sub_id = await self._subscribe_feeder_transfers(w3, tokens, feeders)
                self.log.info(
                    "subscribed feeder transfers sub=%s tokens=%d feeders=%d",
                    sub_id,
                    len(tokens),
                    len(feeders),
                )
            except Exception as exc:  # noqa: BLE001
                # Fallback: subscribe all Transfers on tokens, filter client-side.
                self.log.warning(
                    "feeder topic-filter sub failed (%s) — broad Transfer sub", exc
                )
                params = {
                    "address": [Web3.to_checksum_address(t) for t in tokens],
                    "topics": [ERC20_TRANSFER_TOPIC],
                }
                async with asyncio.timeout(15):
                    sub_id = await w3.eth.subscribe("logs", params)
                self.log.info("subscribed feeder broad Transfer sub=%s", sub_id)

            iterator = w3.socket.process_subscriptions().__aiter__()
            while not self._stop.is_set():
                try:
                    async with asyncio.timeout(self.ws_silence_timeout):
                        message = await iterator.__anext__()
                except asyncio.TimeoutError:
                    await self._probe(w3)
                    continue
                except StopAsyncIteration as exc:
                    raise ConnectionError("feeder-watch subscription ended") from exc

                result = message.get("result") if isinstance(message, dict) else None
                if isinstance(result, Mapping) and result.get("topics"):
                    self._on_feeder_transfer(result)
                elif isinstance(message, Mapping) and message.get("topics"):
                    self._on_feeder_transfer(message)
        finally:
            with suppress(Exception):
                await w3.provider.disconnect()

    async def _feeder_watcher(self) -> None:
        """Watch dust-feeder ERC20 Transfer → instant track/eval (Aug24 pattern)."""
        if not self.feeder_watch or not self.feeder_addrs or not self._token_to_markets:
            self.log.info(
                "feeder-watch off (enabled=%s feeders=%d tokens=%d)",
                self.feeder_watch,
                len(self.feeder_addrs),
                len(self._token_to_markets),
            )
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=6.0)
            return
        except asyncio.TimeoutError:
            pass
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._feeder_watch_session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                delay = self._backoff(attempt)
                self.log.warning("feeder-watch WS down (%s) retry in %.1fs", exc, delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass

    def _on_log(self, entry: dict[str, Any]) -> None:
        self.raw_logs_seen += 1
        topics = entry.get("topics") or []
        if len(topics) < 2:
            return
        topic0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
        event_name = TOPIC_TO_EVENT.get(topic0.lower().removeprefix("0x"))
        if not event_name:
            return
        market_id = _topic_id(topics[1]).lower()
        if not market_id.startswith("0x"):
            market_id = "0x" + market_id
        kind = classify_ws_event(event_name, market_id in self.market_ids)
        if kind == "drop":
            return

        block = entry.get("blockNumber")
        if isinstance(block, str):
            block = int(block, 16)
        if isinstance(block, int):
            self._last_block = block
        self.events_seen += 1

        if kind == "discovery":
            self._on_discovery_liquidate(entry, market_id, topics, block)
            return

        if event_name == "Liquidate":
            self._on_liquidate(entry, market_id, topics, block)
            return

        # Indexed layout (Morpho EventsLib):
        # Borrow/Withdraw:           [sig, id, onBehalf, receiver]
        # Repay/Supply/SupplyColl:   [sig, id, caller, onBehalf]
        # WithdrawCollateral:        [sig, id, caller, onBehalf, receiver]
        # Liquidate:                 [sig, id, caller, borrower]
        user = None
        if event_name in ("Borrow", "Withdraw"):
            if len(topics) >= 3:
                user = _topic_addr(topics[2])
        elif event_name == "WithdrawCollateral":
            if len(topics) >= 4:
                user = _topic_addr(topics[3])
        elif event_name in ("Repay", "Supply", "SupplyCollateral"):
            if len(topics) >= 4:
                user = _topic_addr(topics[3])
            elif len(topics) >= 3:
                user = _topic_addr(topics[2])

        if user is None:
            return

        self._enqueue(market_id, user, reason=event_name)
        key = PositionKey(market_id=market_id, user=user)
        tracked = self.registry.get(key)
        if tracked is not None and isinstance(block, int):
            tracked.last_block = block

        # B: feeder-pinned Borrow → immediate eval+kick (skip seed backlog).
        if event_name == "Borrow" and tracked is not None:
            pinned = (
                tracked.feeder_pin_until > 0
                and time.monotonic() < tracked.feeder_pin_until
            )
            armed = (
                self.executor is not None
                and self.executor.is_feeder_armed(user, market_id)
            )
            if pinned or armed:
                tracked.last_event = "borrow-feeder"
                self._schedule_feeder_borrow_fast(key)

    def _schedule_feeder_borrow_fast(self, key: PositionKey) -> None:
        loop = self._loop
        if loop is None:
            return

        def _spawn() -> None:
            loop.create_task(
                self._feeder_borrow_fast(key), name="feeder-borrow-fast"
            )

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _spawn()
        else:
            loop.call_soon_threadsafe(_spawn)

    async def _feeder_borrow_fast(self, key: PositionKey) -> None:
        """B: Morpho Borrow on feeder victim — read+kick without waiting on queue."""
        tracked = self.registry.get(key)
        if tracked is None:
            return
        if (
            tracked.feeder_pin_until > 0
            and time.monotonic() >= tracked.feeder_pin_until
            and not (
                self.executor is not None
                and self.executor.is_feeder_armed(key.user, key.market_id)
            )
        ):
            return
        if self.executor is not None and self.executor.is_position_dead(
            key.user, key.market_id
        ):
            return
        try:
            async with self._eval_rpc_sem:
                cand = await asyncio.to_thread(self.evaluate_key, key)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "feeder-borrow-fast fail %s: %s", key.user[:12], exc
            )
            return
        if cand is None:
            return
        tracked = self.registry.get(key)
        if tracked is not None:
            tracked.last_event = "borrow-feeder"
        self.feeder_borrow_fast += 1
        self.log.info(
            "feeder-borrow-fast #%d user=%s HF=%.4f debt=$%.2f net=$%.4f",
            self.feeder_borrow_fast,
            cand.user[:12],
            float(cand.health_factor),
            cand.debt_usd,
            cand.net_profit_usd,
        )
        self._log_candidate(cand)

    def _on_discovery_liquidate(
        self,
        entry: dict[str, Any],
        market_id: str,
        topics: list[Any],
        block: Any,
    ) -> None:
        """Unknown-market Liquidate: Telegram only. Never seed, enqueue, or live."""
        liquidator = _topic_addr(topics[2]) if len(topics) >= 3 else "?"
        borrower = _topic_addr(topics[3]) if len(topics) >= 4 else "?"
        amounts = _log_data_uints(entry)
        repaid = amounts[0] if amounts else 0
        tx_hex = _tx_hex(entry)
        self.discovery_liq_seen += 1
        self.log.info(
            "DISCOVERY LIQ #%d (no seed/live) market=%s | liq=%s user=%s "
            "repaid=%d | block %s | tx %s",
            self.discovery_liq_seen,
            market_id[:22],
            liquidator,
            borrower,
            repaid,
            block,
            tx_hex,
        )
        if not self.discovery_tg:
            return
        self._schedule_discovery_alert(
            market_id=market_id,
            repaid=repaid,
            liquidator=liquidator,
            borrower=borrower,
            tx_hex=tx_hex,
            block=block,
        )

    def _schedule_discovery_alert(
        self,
        *,
        market_id: str,
        repaid: int,
        liquidator: str,
        borrower: str,
        tx_hex: str,
        block: Any,
    ) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def _run() -> None:
            await self._discovery_alert_task(
                market_id=market_id,
                repaid=repaid,
                liquidator=liquidator,
                borrower=borrower,
                tx_hex=tx_hex,
                block=block,
            )

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(_run())
        else:
            loop.call_soon_threadsafe(lambda: loop.create_task(_run()))

    async def _discovery_alert_task(
        self,
        *,
        market_id: str,
        repaid: int,
        liquidator: str,
        borrower: str,
        tx_hex: str,
        block: Any,
    ) -> None:
        meta = self._discovery_meta_cache.get(market_id)
        if meta is None:
            try:
                looked = await asyncio.to_thread(
                    lookup_discovery_market, market_id, self.chain_id
                )
            except Exception as exc:  # noqa: BLE001
                self.log.warning("discovery graphql %s: %s", market_id[:18], exc)
                looked = None
            if looked:
                self._discovery_meta_cache[market_id] = looked
                meta = looked

        pair = "?"
        debt_usd = 0.0
        profit_usd = 0.0
        if meta is not None:
            pair = str(meta.get("pair") or "?")
            decimals = int(meta.get("decimals") or 6)
            price = float(meta.get("price_usd") or 1.0)
            debt_usd = (repaid / (10**decimals) * price) if repaid else 0.0
            profit_usd = estimate_liquidation_profit_usd(
                debt_usd, int(meta.get("lltv_wad") or 0)
            )
        elif 300 * 10**6 <= repaid < 10**12:
            debt_usd = repaid / 1e6
            pair = "? (graphql нет)"
        else:
            self.log.info(
                "discovery skip no-meta repaid=%d market=%s",
                repaid,
                market_id[:22],
            )
            return

        if debt_usd < self.discovery_min_debt_usd:
            self.log.info(
                "discovery skip dust $%.2f < min $%.0f %s",
                debt_usd,
                self.discovery_min_debt_usd,
                pair,
            )
            return

        self._schedule_alert(
            "Discovery / хвост (не бой)\n"
            f"{pair}\n"
            f"рынок вне whitelist — live нет, сида нет\n"
            f"ликвидатор: {liquidator}\n"
            f"пользователь: {borrower}\n"
            f"долг ~${debt_usd:,.0f} | оценка прибыли ~${profit_usd:,.2f}\n"
            f"блок {block} | tx {tx_hex}\n"
            f"итого discovery: {self.discovery_liq_seen}\n"
            "чтобы ловить: скажи «лови этот» + пара",
            dedup_key=f"morpho-discovery-{tx_hex}" if tx_hex else None,
            cooldown=120.0,
        )

    def _on_liquidate(
        self,
        entry: dict[str, Any],
        market_id: str,
        topics: list[Any],
        block: Any,
    ) -> None:
        liquidator = _topic_addr(topics[2]) if len(topics) >= 3 else "?"
        borrower = _topic_addr(topics[3]) if len(topics) >= 4 else "?"
        amounts = _log_data_uints(entry)
        repaid = amounts[0] if amounts else 0
        market = self.markets_by_id.get(market_id)
        decimals = self._loan_decimals.get(market_id, 6)
        debt_usd = repaid / (10 ** decimals) if repaid else 0.0
        if market is not None:
            debt_usd *= self._loan_usd(market)
        profit_usd = 0.0
        net_usd = 0.0
        pair = "?"
        if market is not None:
            profit_usd = estimate_liquidation_profit_usd(debt_usd, market.lltv_wad)
            net_usd = estimate_net_profit_usd(
                debt_usd,
                market.lltv_wad,
                gas_cost_usd=self._gas_cost_usd(),
                slippage_bps=self._slippage_bps(),
            ).net_profit_usd
            pair = f"{market.loan_symbol}/{market.collateral_symbol}"
        tx_hex = _tx_hex(entry)
        self.foreign_liq_seen += 1
        self.log.info(
            "MORPHO LIQUIDATION #%d %s | liq=%s user=%s repaid=%d ~$%.0f "
            "profit~$%.2f net_profit_usd=$%.4f | block %s | tx %s",
            self.foreign_liq_seen,
            pair,
            liquidator,
            borrower,
            repaid,
            debt_usd,
            profit_usd,
            net_usd,
            block,
            tx_hex,
        )
        if borrower != "?":
            self._invalidate_cache_after_liquidate(market_id, borrower)
            if self.executor is not None:
                self.executor.note_foreign_liq(borrower, market_id)
            # Refresh on-chain truth (partials); soft-batch blocked by zero cache + dead-TTL.
            self._enqueue(market_id, borrower, reason="Liquidate")
        # Diagnostic / take-all: still alert on dust foreign liqs (analysis material).
        if (not self.diagnostic_mode) and net_usd < self.min_net_profit_usd:
            self.log.info(
                "morpho dust liq skipped TG net=$%.4f < min=$%.4f debt ~$%.2f",
                net_usd,
                self.min_net_profit_usd,
                debt_usd,
            )
            return
        self.foreign_liq_debt_usd += debt_usd
        self.foreign_liq_profit_usd += profit_usd
        race = self.scoreboard.note_foreign_liq(
            user=borrower,
            market_id=market_id,
            debt_usd=debt_usd,
            profit_usd=profit_usd,
            liquidator=liquidator,
            pair=pair,
            tx=tx_hex,
            block=block if isinstance(block, int) else None,
            extra={"net_profit_usd": round(net_usd, 6), "diagnostic": self.diagnostic_mode},
        )
        diag = "диагностика, не боевой режим\n" if self.diagnostic_mode else ""
        self._schedule_alert(
            f"Ликвидация Morpho (чужая) [{race}]\n"
            f"{diag}"
            f"{pair}\n"
            f"ликвидатор: {liquidator}\n"
            f"пользователь: {borrower}\n"
            f"долг ~${debt_usd:,.0f} | оценка прибыли ~${profit_usd:,.2f}\n"
            f"чистыми ${net_usd:,.4f}\n"
            f"блок {block} | tx {tx_hex}\n"
            f"итого за сессию: {self.foreign_liq_seen} шт, "
            f"долг ${self.foreign_liq_debt_usd:,.0f}",
            dedup_key=f"morpho-liq-{tx_hex}" if tx_hex else None,
            cooldown=60.0,
        )

    async def _probe(self, w3: AsyncWeb3) -> None:
        head = await w3.eth.block_number
        if self._last_block is not None and head <= self._last_block:
            # Allow small lag; freeze for long = reconnect.
            raise ConnectionError(
                f"head frozen at {head} (last_log_block={self._last_block})"
            )
        self._last_block = head

    async def _session(self) -> None:
        self.log.info("connecting morpho WS %s", bot_config.rpc_host(self.ws_url))
        w3 = AsyncWeb3(self._build_provider())
        async with asyncio.timeout(self.ws_connect_timeout):
            await w3.provider.connect()
        try:
            sub_id = await self._subscribe(w3)
            self._connected_since = time.monotonic()
            self._last_block = None
            self.log.info(
                "subscribed morpho logs sub=%s markets=%d",
                sub_id,
                len(self.markets),
            )
            if self.reconnects:
                await self._alert(
                    f"WebSocket восстановлен, markets={len(self.markets)}",
                    dedup_key=f"morpho-ws-up-{self.chain}",
                    cooldown=60.0,
                )
            iterator = w3.socket.process_subscriptions().__aiter__()
            while not self._stop.is_set():
                try:
                    async with asyncio.timeout(self.ws_silence_timeout):
                        message = await iterator.__anext__()
                except asyncio.TimeoutError:
                    await self._probe(w3)
                    continue
                except StopAsyncIteration as exc:
                    raise ConnectionError("subscription stream ended") from exc

                result = message.get("result") if isinstance(message, dict) else None
                # web3 delivers AttributeDict (Mapping), not a plain dict.
                if isinstance(result, Mapping) and result.get("topics"):
                    self._on_log(result)
                elif isinstance(message, Mapping) and message.get("topics"):
                    self._on_log(message)
        finally:
            self._connected_since = None
            with suppress(Exception):
                await w3.provider.disconnect()

    def _backoff(self, attempt: int) -> float:
        capped = min(
            self.ws_backoff_initial * (2 ** max(0, attempt - 1)),
            self.ws_backoff_max,
        )
        return capped * (0.5 + random.random())

    async def run(self) -> None:
        if not self.markets:
            self.log.error("no morpho markets configured for %s", self.chain)
            return
        if not self.w3.is_connected():
            raise ConnectionError(f"{self.chain} HTTP RPC unreachable")

        self._loop = asyncio.get_running_loop()
        self.seed()
        self._build_feed_map()
        self.flash_arb.start()
        mode = self.executor.mode if self.executor else "выкл"
        live_line = (
            "отправка включена"
            if self.executor is not None and self.executor.mode == "live"
            else "без отправки (observe/paper)"
        )
        await self._alert(
            f"Morpho сканер запущен ({self.chain})\n"
            f"режим={mode} — {live_line}\n"
            f"рынки={len(self.markets)} в книге={len(self.registry)}\n"
            f"мин. чистыми=${self.min_net_profit_usd:.2f} "
            f"макс. долг={'нет' if self.max_debt_usd <= 0 else f'${self.max_debt_usd:,.0f}'}\n"
            f"flash-arb={'вкл' if self.flash_arb.active else 'выкл'}\n"
            f"discovery TG={'вкл' if self.discovery_tg else 'выкл'} "
            f"мин. хвост=${self.discovery_min_debt_usd:,.0f} "
            "(без live/сида)"
            + (
                f"\nдиагностика, не боевой режим"
                if self.diagnostic_mode
                else ""
            ),
            dedup_key=f"morpho-startup-{self.chain}",
            cooldown=10.0,
        )
        tasks = [
            asyncio.create_task(
                self._worker(), name=f"morpho-{self.chain}-worker-{i}"
            )
            for i in range(self.worker_count)
        ]
        tasks.extend(
            [
                asyncio.create_task(
                    self._book_scanner(), name=f"morpho-{self.chain}-book"
                ),
                asyncio.create_task(
                    self._oracle_poller(), name=f"morpho-{self.chain}-oracle"
                ),
                asyncio.create_task(
                    self._oracle_feed_watcher(),
                    name=f"morpho-{self.chain}-oracle-feed",
                ),
                asyncio.create_task(
                    self._feeder_watcher(),
                    name=f"morpho-{self.chain}-feeder",
                ),
                asyncio.create_task(
                    self._hot_refresher(), name=f"morpho-{self.chain}-hot"
                ),
                asyncio.create_task(
                    self._near_hf_refresher(), name=f"morpho-{self.chain}-near"
                ),
                asyncio.create_task(
                    self._heartbeat(), name=f"morpho-{self.chain}-hb"
                ),
            ]
        )
        if self.flash_arb.enabled:
            tasks.append(
                asyncio.create_task(
                    self._arb_ticker(), name=f"morpho-{self.chain}-arb"
                )
            )
        self.log.info(
            "eval-opt: flash_arb=%s near_refresh=%.0fs/%d eval_sem=2 oracle_lock=1 snap_ttl=%.0fs",
            "on" if self.flash_arb.enabled else "off",
            self.near_refresh_seconds,
            self.near_refresh_batch,
            self.reader._snap_ttl,
        )
        self.log.info(
            "tasks: workers=%d batch_drain=%d hot_refresh=%.0fs arb=%s",
            self.worker_count,
            self.batch_drain,
            self.hot_refresh_seconds,
            "on" if self.flash_arb.active else "off",
        )
        attempt = 0
        try:
            while not self._stop.is_set():
                try:
                    await self._session()
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    attempt += 1
                    self.reconnects += 1
                    delay = self._backoff(attempt)
                    self.log.warning(
                        "morpho WS lost (%s: %s) — reconnect in %.1fs (attempt %d)",
                        type(exc).__name__,
                        exc,
                        delay,
                        attempt,
                    )
                    await self._alert(
                        f"WebSocket потерян ({type(exc).__name__}: {exc}), "
                        f"переподключение через {delay:.0f} с",
                        dedup_key=f"morpho-ws-down-{self.chain}",
                        cooldown=120.0,
                    )
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)
                        return
                    except asyncio.TimeoutError:
                        pass
        finally:
            self._stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_once(self) -> int:
        """Seed + evaluate current registry once (no WS)."""
        if not self.w3.is_connected():
            raise ConnectionError(f"{self.chain} HTTP RPC unreachable")
        self._loop = asyncio.get_running_loop()
        self.seed()
        await self._alert(
            f"Morpho разовый скан — старт ({self.chain})\n"
            f"режим={self.executor.mode if self.executor else 'выкл'} "
            f"(без отправки)\n"
            f"рынки={len(self.markets)} в книге={len(self.registry)}",
            dedup_key=f"morpho-once-start-{self.chain}",
            cooldown=10.0,
        )
        found = 0
        # Priority order
        keys = sorted(
            self.registry.keys(),
            key=lambda k: self.markets_by_id[k.market_id.lower()].priority,
        )
        for key in keys:
            cand = self.evaluate_key(key)
            if cand is not None:
                self._log_candidate(cand)
                found += 1
        near = [
            t
            for t in self.registry.values()
            if t.last_hf is not None and t.last_debt_usd >= self.min_debt_usd
        ]
        nearest_line = "ближайший=нет"
        if near:
            worst = min(near, key=lambda t: t.last_hf or Decimal("Infinity"))
            nearest_line = (
                f"ближайший HF={worst.last_hf:.4f} долг ~${worst.last_debt_usd:,.0f} "
                f"user={worst.user}"
            )
            self.log.info("once %s", nearest_line)
        # Drain fire-and-forget try_liq + Telegram tasks
        await asyncio.sleep(2.0)
        metrics = self.executor.metrics.snapshot() if self.executor else "off"
        self.log.info(
            "[%s] once-scan done: tracked=%d candidates=%d hot=%d exec=%s",
            self.chain,
            len(self.registry),
            found,
            len(self.hot_set),
            metrics,
        )
        await self._alert(
            f"Morpho разовый скан — готово ({self.chain})\n"
            f"в книге={len(self.registry)} кандидатов={found} hot={len(self.hot_set)}\n"
            f"{nearest_line}\n"
            f"{self.executor.metrics.snapshot_ru() if self.executor else 'выкл'}\n"
            f"без отправки",
            dedup_key=f"morpho-once-done-{self.chain}",
            cooldown=10.0,
        )
        return found


async def _run_chains(
    chains: list[str],
    *,
    once: bool,
    duration_seconds: int = 0,
    **kwargs: Any,
) -> int:
    telegram = load_morpho_telegram_config()
    notifier = Notifier(telegram, prefix="")
    if telegram.enabled:
        LOG.info("Telegram alerts enabled for Morpho scanner (Morpho bot token)")
    else:
        LOG.info("Telegram not configured — Morpho alerts stay in logs only")

    scanners = [
        MorphoChainScanner(c, notifier=notifier, **kwargs) for c in chains
    ]
    try:
        if once:
            total = 0
            for s in scanners:
                total += await s.run_once()
            return total
        if duration_seconds > 0:
            LOG.info("observe duration=%ss then stop (explicit cap)", duration_seconds)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(s.run() for s in scanners)),
                    timeout=float(duration_seconds),
                )
            except asyncio.TimeoutError:
                LOG.info("duration elapsed — stopping Morpho scanners")
                for s in scanners:
                    s.stop()
            await asyncio.sleep(1.5)
            for s in scanners:
                metrics = s.executor.metrics.snapshot() if s.executor else "off"
                LOG.info(
                    "duration summary %s: tracked=%d hot=%d cand=%d foreign=%d %s",
                    s.chain,
                    len(s.registry),
                    len(s.hot_set),
                    s.candidates_seen,
                    s.foreign_liq_seen,
                    metrics,
                )
                await s._alert(
                    f"Morpho сканер остановлен ({s.chain}, {duration_seconds} с)\n"
                    f"в книге={len(s.registry)} hot={len(s.hot_set)} "
                    f"кандидатов={s.candidates_seen} чужих liq={s.foreign_liq_seen}\n"
                    f"{s.executor.metrics.snapshot_ru() if s.executor else 'выкл'}",
                    dedup_key=f"morpho-duration-{s.chain}",
                    cooldown=5.0,
                )
            return 0
        LOG.info(
            "observe duration=forever (CLI --duration-seconds omitted or 0); "
            "no self-timeout — stop with SIGINT / manual kill"
        )
        await asyncio.gather(*(s.run() for s in scanners))
        return 0
    finally:
        await notifier.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Morpho Blue dry-run liquidation scanner")
    parser.add_argument(
        "--chains",
        default="base,arbitrum",
        help="Comma-separated (optimism skipped by design)",
    )
    parser.add_argument("--min-debt-usd", type=float, default=0.0)
    parser.add_argument(
        "--min-net-profit-usd",
        type=float,
        default=None,
        help="Diagnostic filter floor (default 0.05 via MIN_NET_PROFIT_USD)",
    )
    parser.add_argument("--hf-threshold", type=float, default=1.0)
    parser.add_argument("--book-scan-seconds", type=int, default=120)
    parser.add_argument("--seed-per-market", type=int, default=150)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Seed+evaluate once over HTTP, then exit (no WS)",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=None,
        metavar="N",
        help="Stop WS after N seconds. Omit or 0 = forever (SIGINT).",
    )
    parser.add_argument(
        "--diagnostic-mode",
        action="store_true",
        help="TEMPORARY net-profit filter, no $300 min debt. NOT combat 19.08.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def apply_cli_env(args: argparse.Namespace) -> None:
    """CLI wins over .env for diagnostic knobs. Does not touch AUTO_EXECUTE."""
    if args.diagnostic_mode:
        os.environ["MORPHO_DIAGNOSTIC_MODE"] = "1"
    if args.min_net_profit_usd is not None:
        os.environ["MIN_NET_PROFIT_USD"] = str(args.min_net_profit_usd)


def resolved_duration_seconds(args: argparse.Namespace) -> int:
    if getattr(args, "once", False):
        return 0
    raw = args.duration_seconds
    if duration_is_forever(raw):
        return 0
    return int(raw)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    apply_cli_env(args)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    duration = resolved_duration_seconds(args)
    if args.duration_seconds is None:
        LOG.info("CLI flags: --duration-seconds omitted → forever")
    elif args.duration_seconds <= 0:
        LOG.info("CLI flags: --duration-seconds=%s → forever", args.duration_seconds)
    else:
        LOG.info("CLI flags: --duration-seconds=%s → bounded run", args.duration_seconds)
    if diagnostic_enabled(None) or args.diagnostic_mode:
        LOG.warning(
            "%s min_net_profit_usd=%s",
            DIAGNOSTIC_NOT_COMBAT,
            args.min_net_profit_usd
            if args.min_net_profit_usd is not None
            else min_net_profit_usd(None, diagnostic=True),
        )

    chains = [c.strip().lower() for c in args.chains.split(",") if c.strip()]
    for c in chains:
        if c == "optimism":
            LOG.error("optimism skipped (viability: near-dead Morpho liq flow)")
            return 2
        if c not in MorphoChainScanner.CHAIN_IDS:
            LOG.error("unsupported chain %s", c)
            return 2

    min_debt = 0.0 if (args.diagnostic_mode or diagnostic_enabled(None)) else args.min_debt_usd
    kwargs = dict(
        min_debt_usd=min_debt,
        hf_threshold=Decimal(str(args.hf_threshold)),
        book_scan_seconds=args.book_scan_seconds,
        seed_per_market=args.seed_per_market,
    )
    try:
        asyncio.run(
            _run_chains(
                chains,
                once=args.once,
                duration_seconds=duration,
                **kwargs,
            )
        )
    except KeyboardInterrupt:
        LOG.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
