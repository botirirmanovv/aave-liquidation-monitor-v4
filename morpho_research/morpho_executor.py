#!/usr/bin/env python3
"""Thin Morpho Blue flash-liquidation execute path.

Does not touch monitor_v4 / Aave. Defaults: observe (log only),
MORPHO_AUTO_EXECUTE=false. Live is gated and will not fire on Base
mainnet overnight.

Usage from morpho_scanner (fire-and-forget):
    executor.schedule(intent)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from web3 import Web3
from web3.providers import HTTPProvider

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
for p in (_DIR, _ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from aave_bot import config as bot_config
from aave_bot.simulate import decode_revert

from morpho_hf import (
    ORACLE_PRICE_SCALE,
    estimate_net_profit_usd,
    estimate_priority_gas_usd,
    hf_from_cached_shares,
    loan_token_price_usd,
    seized_assets_for_repay,
    shares_to_assets_up,
)
from morpho_markets import MorphoMarketConfig, parse_allowed_markets

LOG = logging.getLogger("morpho_executor")


def _is_rate_limited(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg:
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    for attr in ("__cause__", "__context__"):
        nested = getattr(exc, attr, None)
        if nested is not None and nested is not exc and _is_rate_limited(nested):
            return True
    return False


def _is_transient_rpc(exc: BaseException) -> bool:
    if _is_rate_limited(exc):
        return True
    msg = str(exc).lower()
    needles = (
        "502",
        "503",
        "504",
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
    )
    return any(n in msg for n in needles)


class RateLimitCounter:
    """Count every HTTP 429 from the public RPC (retries still count)."""

    __slots__ = ("_n", "_lock")

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def inc(self, n: int = 1) -> None:
        with self._lock:
            self._n += n

    @property
    def hits(self) -> int:
        with self._lock:
            return self._n


class RotatingHTTPProvider(HTTPProvider):
    """HTTP provider that switches BASE_HTTP_RPC_URLS peers on 429/transient errors.

    Same Web3 instance stays valid — MorphoReader/executor keep working after rotate.
    Cooldown avoids thrashing the same dead peer every call.
    """

    def __init__(
        self,
        urls: list[str],
        *,
        request_kwargs: dict[str, Any] | None = None,
        cooldown_seconds: float = 25.0,
        counter: RateLimitCounter | None = None,
    ) -> None:
        clean = [u.strip() for u in urls if u and u.strip()]
        if not clean:
            raise ValueError("RotatingHTTPProvider requires at least one URL")
        self._urls = clean
        self._idx = 0
        self._lock = threading.Lock()
        self._cooldown_until: dict[int, float] = {}
        self._cooldown_seconds = max(5.0, float(cooldown_seconds))
        self.rotations = 0
        self.rate_limit = counter if counter is not None else RateLimitCounter()
        super().__init__(
            clean[0],
            request_kwargs=request_kwargs or {"timeout": 30},
        )

    @property
    def current_url(self) -> str:
        return self._urls[self._idx]

    def _pick_next(self, reason: str) -> bool:
        with self._lock:
            n = len(self._urls)
            if n <= 1:
                return False
            now = time.monotonic()
            old_idx = self._idx
            old_host = bot_config.rpc_host(self._urls[old_idx])
            self._cooldown_until[old_idx] = now + self._cooldown_seconds
            chosen: int | None = None
            for step in range(1, n + 1):
                cand = (old_idx + step) % n
                if now < self._cooldown_until.get(cand, 0.0):
                    continue
                chosen = cand
                break
            if chosen is None:
                chosen = (old_idx + 1) % n
                reason = f"{reason},all_cooling"
            self._idx = chosen
            new_url = self._urls[chosen]
            self.endpoint_uri = new_url  # type: ignore[assignment]
            self.rotations += 1
            LOG.warning(
                "rpc rotate %s -> %s (%s) rotations=%d",
                old_host,
                bot_config.rpc_host(new_url),
                reason,
                self.rotations,
            )
            return True

    def make_request(self, method: str, params: Any) -> Any:
        last_exc: BaseException | None = None
        attempts = max(1, len(self._urls))
        for attempt in range(attempts):
            try:
                return super().make_request(method, params)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _is_rate_limited(exc):
                    self.rate_limit.inc()
                    if attempt + 1 < attempts and self._pick_next("429"):
                        continue
                elif _is_transient_rpc(exc) and attempt + 1 < attempts:
                    if self._pick_next("transient"):
                        continue
                raise
        assert last_exc is not None
        raise last_exc


def connect_rotating_http(
    urls: list[str],
    *,
    timeout: int = 30,
    cooldown_seconds: float = 25.0,
) -> tuple[str, Web3, RotatingHTTPProvider]:
    """Build Web3 on the first reachable URL; keep peers for mid-run rotate."""
    clean = [u.strip() for u in urls if u and u.strip()]
    if not clean:
        raise RuntimeError("HTTP RPC not connected: empty URL list")
    # Prefer starting on a peer that answers eth_blockNumber.
    order = list(clean)
    errors: list[str] = []
    start_idx = 0
    for i, url in enumerate(order):
        probe = Web3(HTTPProvider(url, request_kwargs={"timeout": timeout}))
        try:
            if probe.is_connected():
                start_idx = i
                break
            errors.append(f"{bot_config.rpc_host(url)}: not connected")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{bot_config.rpc_host(url)}: {exc}")
    else:
        detail = "; ".join(errors) if errors else "empty URL list"
        raise RuntimeError(f"HTTP RPC not connected: {detail}")

    rotated = order[start_idx:] + order[:start_idx]
    provider = RotatingHTTPProvider(
        rotated,
        request_kwargs={"timeout": timeout},
        cooldown_seconds=cooldown_seconds,
    )
    w3 = Web3(provider)
    return provider.current_url, w3, provider

_ABI_JSON = (
    _ROOT / "forge-out" / "MorphoFlashLiquidator.sol" / "MorphoFlashLiquidator.json"
)

# Base routers (same as MorphoFlashLiquidator / BalancerV3FlashArbBot comments).
UNI_V3_SWAP_ROUTER_02_BASE = "0x2626664c2603336E57b271c5c0d842f2875A7dA0"
UNI_V3_SWAP_ROUTER_02_ALT = "0x2626664c2603336E57B271c5C0b26F421741e481"
AERODROME_ROUTER_BASE = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
AERODROME_FACTORY_BASE = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
WETH_BASE = "0x4200000000000000000000000000000000000006"

CHAIN_IDS = {"base": 8453, "arbitrum": 42161}
V3_FEES = (100, 500, 3000, 10000)

MODES = ("observe", "paper", "live")
LIVE_CONFIRM_TOKEN = "YES_SEND_LIVE"
DIAGNOSTIC_NOT_COMBAT = (
    "TEMPORARY diagnostic pipeline — NOT for combat window 19.08. "
    "ВРЕМЕННЫЙ диагностический режим. НЕ для боевого окна 19.08."
)
# Chainlink ETH/USD (8 decimals) for gas USD. Fallback: MORPHO_ETH_USD / MORPHO_GAS_USD.
CHAINLINK_ETH_USD = {
    "base": "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70",
    "arbitrum": "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612",
}
_LATEST_ANSWER_ABI = [
    {
        "inputs": [],
        "name": "latestAnswer",
        "outputs": [{"internalType": "int256", "name": "", "type": "int256"}],
        "stateMutability": "view",
        "type": "function",
    }
]
DEFAULT_ETH_USD = 3500.0

ERROR_SIGNATURES = [
    "OnlyOwner()",
    "OnlyOperator()",
    "ZeroAddress()",
    "InvalidAmount()",
    "ContractPaused()",
    "Reentrancy()",
    "UnauthorizedMorpho()",
    "RouterNotAllowed()",
    "IntermediateTokenNotAllowed()",
    "InsufficientProfit()",
    "SwapFailed()",
    "NoLiquidationInProgress()",
    "TransferFailed()",
    "InvalidBps()",
    "ERC20CallFailed()",
    "ERC20OperationFailed()",
    "HEALTHY_POSITION()",
    "INCONSISTENT_INPUT()",
]

# liquidateWithFlash — kept inline if forge-out is missing.
_FALLBACK_LIQ_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "loanToken", "type": "address"},
                    {"name": "collateralToken", "type": "address"},
                    {"name": "oracle", "type": "address"},
                    {"name": "irm", "type": "address"},
                    {"name": "lltv", "type": "uint256"},
                ],
                "name": "p",
                "type": "tuple",
            },
            {"name": "borrower", "type": "address"},
            {"name": "seizedAssets", "type": "uint256"},
            {"name": "repaidShares", "type": "uint256"},
            {
                "components": [
                    {"name": "router", "type": "address"},
                    {"name": "swapCalldata", "type": "bytes"},
                ],
                "name": "swap",
                "type": "tuple",
            },
            {"name": "minProfit", "type": "uint256"},
        ],
        "name": "liquidateWithFlash",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"name": "loanToken", "type": "address"},
                    {"name": "collateralToken", "type": "address"},
                    {"name": "oracle", "type": "address"},
                    {"name": "irm", "type": "address"},
                    {"name": "lltv", "type": "uint256"},
                ],
                "name": "p",
                "type": "tuple",
            },
            {"name": "borrowers", "type": "address[]"},
            {"name": "seizedAssets", "type": "uint256[]"},
            {"name": "repaidShares", "type": "uint256[]"},
            {
                "components": [
                    {"name": "router", "type": "address"},
                    {"name": "swapCalldata", "type": "bytes"},
                ],
                "name": "swaps",
                "type": "tuple[]",
            },
            {"name": "minProfits", "type": "uint256[]"},
        ],
        "name": "liquidateWithFlashBatch",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"name": "loanToken", "type": "address"},
                    {"name": "collateralToken", "type": "address"},
                    {"name": "oracle", "type": "address"},
                    {"name": "irm", "type": "address"},
                    {"name": "lltv", "type": "uint256"},
                ],
                "name": "p",
                "type": "tuple",
            },
            {"name": "borrower", "type": "address"},
            {"name": "seizedAssets", "type": "uint256"},
            {"name": "repaidShares", "type": "uint256"},
            {
                "components": [
                    {"name": "router", "type": "address"},
                    {"name": "swapCalldata", "type": "bytes"},
                ],
                "name": "swap",
                "type": "tuple",
            },
            {"name": "minProfit", "type": "uint256"},
        ],
        "name": "loadShot",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "fire",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "shotLoaded",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

UNI_V3_EXACT_INPUT_SINGLE_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    }
]

UNI_V3_EXACT_INPUT_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "path", "type": "bytes"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInput",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    }
]

AERO_SWAP_ABI = [
    {
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {
                "components": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "stable", "type": "bool"},
                    {"name": "factory", "type": "address"},
                ],
                "name": "routes",
                "type": "tuple[]",
            },
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def _to_bytes(data: Any) -> bytes:
    if isinstance(data, bytes):
        return data
    text = str(data)
    if text.startswith("0x"):
        text = text[2:]
    return bytes.fromhex(text)


def _load_liquidator_abi() -> list[dict[str, Any]]:
    abi: list[dict[str, Any]] = []
    try:
        payload = json.loads(_ABI_JSON.read_text(encoding="utf-8"))
        raw = payload.get("abi")
        if raw:
            abi = list(raw)
    except (OSError, json.JSONDecodeError, TypeError):
        abi = []
    if not abi:
        return list(_FALLBACK_LIQ_ABI)
    names = {e.get("name") for e in abi if e.get("type") == "function"}
    for entry in _FALLBACK_LIQ_ABI:
        if entry.get("name") not in names:
            abi.append(entry)
    return abi


BATCH_SELECTOR = Web3.keccak(
    text="liquidateWithFlashBatch((address,address,address,address,uint256),address[],uint256[],uint256[],(address,bytes)[],uint256[])"
)[:4]
FIRE_SELECTOR = Web3.keccak(text="fire()")[:4]


def _addr(value: str) -> str:
    return Web3.to_checksum_address(value)


def _cfg_addr(name: str, chain: str) -> str:
    """Morpho-only addresses: unprefixed `.env` inherits; `BASE_*` overrides.

    Do not use `bot_config._address` here — that helper never inherits, so
    `MORPHO_LIQ_CONTRACT=` (as documented in `.env.example`) would be ignored
    when the scanner runs with `chain=base`.
    """
    raw = bot_config._text(name, chain, inherit=True)
    return _addr(raw) if raw else ""


def _encode_v3_path(token_in: str, fee: int, mid: str, fee2: int, token_out: str) -> bytes:
    return (
        bytes.fromhex(_addr(token_in)[2:])
        + int(fee).to_bytes(3, "big")
        + bytes.fromhex(_addr(mid)[2:])
        + int(fee2).to_bytes(3, "big")
        + bytes.fromhex(_addr(token_out)[2:])
    )


def encode_univ3_exact_input_single(
    *,
    token_in: str,
    token_out: str,
    fee: int,
    recipient: str,
    amount_in: int,
    amount_out_min: int,
) -> bytes:
    c = Web3().eth.contract(address=_addr(UNI_V3_SWAP_ROUTER_02_BASE), abi=UNI_V3_EXACT_INPUT_SINGLE_ABI)
    return _to_bytes(
        c.encode_abi(
            "exactInputSingle",
            args=[
                (
                    _addr(token_in),
                    _addr(token_out),
                    int(fee),
                    _addr(recipient),
                    int(amount_in),
                    int(amount_out_min),
                    0,
                )
            ],
        )
    )


def encode_univ3_exact_input_hop(
    *,
    token_in: str,
    mid: str,
    token_out: str,
    fee_in: int,
    fee_out: int,
    recipient: str,
    amount_in: int,
    amount_out_min: int,
) -> bytes:
    path = _encode_v3_path(token_in, fee_in, mid, fee_out, token_out)
    c = Web3().eth.contract(address=_addr(UNI_V3_SWAP_ROUTER_02_BASE), abi=UNI_V3_EXACT_INPUT_ABI)
    return _to_bytes(
        c.encode_abi(
            "exactInput",
            args=[(path, _addr(recipient), int(amount_in), int(amount_out_min))],
        )
    )


def encode_aerodrome_swap(
    *,
    token_in: str,
    token_out: str,
    amount_in: int,
    amount_out_min: int,
    recipient: str,
    deadline: int,
    factory: str = AERODROME_FACTORY_BASE,
    stable: bool = False,
    hop: str | None = None,
) -> bytes:
    routes: list[tuple[str, str, bool, str]]
    if hop:
        routes = [
            (_addr(token_in), _addr(hop), bool(stable), _addr(factory)),
            (_addr(hop), _addr(token_out), bool(stable), _addr(factory)),
        ]
    else:
        routes = [(_addr(token_in), _addr(token_out), bool(stable), _addr(factory))]
    c = Web3().eth.contract(address=_addr(AERODROME_ROUTER_BASE), abi=AERO_SWAP_ABI)
    return _to_bytes(
        c.encode_abi(
            "swapExactTokensForTokens",
            args=[int(amount_in), int(amount_out_min), routes, _addr(recipient), int(deadline)],
        )
    )


@dataclass(slots=True)
class LiqIntent:
    chain: str
    user: str
    market: MorphoMarketConfig
    health_factor: Decimal
    debt_usd: float
    profit_usd: float
    borrow_shares: int
    collateral: int
    total_borrow_assets: int
    total_borrow_shares: int
    oracle_price: int
    loan_decimals: int = 6
    reason: str = ""
    net_profit_usd: float = 0.0
    path_t0: float = 0.0  # monotonic: feed-cl WS → would_send latency


@dataclass(slots=True)
class PrebuiltSwap:
    router: str
    kind: str  # univ3_single | univ3_hop | aero | aero_hop
    fee: int = 3000
    fee_hop: int = 500
    hop: str = ""
    stable: bool = False


@dataclass(slots=True)
class EncodedLiq:
    market_params: tuple[str, str, str, str, int]
    borrower: str
    seized_assets: int
    repaid_shares: int
    router: str
    swap_calldata: bytes
    min_profit: int
    calldata: bytes
    amount_in: int
    amount_out_min: int
    kind: str


@dataclass(slots=True)
class PreencodedShot:
    """Off-chain preload: reuse encode when shares/oracle unchanged."""

    encoded: EncodedLiq
    borrow_shares: int
    collateral: int
    oracle_price: int
    stored_mono: float


def _wrap_session_count_429(session: Any, counter: RateLimitCounter) -> RateLimitCounter:
    if session is None:
        return counter
    existing = getattr(session, "_morpho_429_counter", None)
    if isinstance(existing, RateLimitCounter):
        return existing
    orig = session.request

    def request(method: str, url: str, **kwargs: Any) -> Any:
        resp = orig(method, url, **kwargs)
        if getattr(resp, "status_code", None) == 429:
            counter.inc()
        return resp

    session.request = request  # type: ignore[method-assign]
    session._morpho_429_wrapped = True
    session._morpho_429_counter = counter
    return counter


def attach_public_rpc_429_counter(w3: Web3, counter: RateLimitCounter | None = None) -> RateLimitCounter:
    """Hook the public HTTPProvider session so each 429 increments `counter`.

    RotatingHTTPProvider already counts + retries — reuse its counter (no double wrap).
    """
    provider = getattr(w3, "provider", None)
    if isinstance(provider, RotatingHTTPProvider):
        if counter is not None and counter is not provider.rate_limit:
            # Keep rotator as source of truth; ignore alternate counter.
            pass
        return provider.rate_limit
    if counter is None:
        counter = RateLimitCounter()
    if provider is None:
        return counter
    mgr = getattr(provider, "_request_session_manager", None)
    explicit = getattr(mgr, "_explicit_session", None) if mgr is not None else None
    if explicit is not None:
        return _wrap_session_count_429(explicit, counter)
    session = getattr(provider, "_session", None) or getattr(provider, "session", None)
    if session is not None:
        return _wrap_session_count_429(session, counter)
    if mgr is None:
        return counter
    orig = mgr.cache_and_return_session

    def wrapped(endpoint_uri, session=None, request_timeout=None):  # noqa: ANN001
        sess = orig(endpoint_uri, session=session, request_timeout=request_timeout)
        _wrap_session_count_429(sess, counter)
        return sess

    mgr.cache_and_return_session = wrapped  # type: ignore[method-assign]
    return counter


@dataclass
class ExecutorMetrics:
    would_send: int = 0
    simulated_ok: int = 0
    simulated_fail: int = 0
    sent: int = 0
    landed: int = 0
    lost_to_foreign: int = 0
    skipped: int = 0
    last_revert: str = ""
    rate_limit_hits: int = 0
    revert_reason: str = ""  # reserved alias of last_revert for observe schema
    feed_cl_to_send_ms_last: float = 0.0
    feed_cl_to_send_ms_max: float = 0.0
    feed_cl_to_send_n: int = 0

    def snapshot(self) -> str:
        base = (
            f"would_send={self.would_send} sim_ok={self.simulated_ok} "
            f"sim_fail={self.simulated_fail} sent={self.sent} landed={self.landed} "
            f"lost_foreign={self.lost_to_foreign} skipped={self.skipped} "
            f"rate_limit_hits={self.rate_limit_hits} "
            f"last_revert={self.last_revert or self.revert_reason or '-'}"
        )
        if self.feed_cl_to_send_n:
            base += (
                f" feed_cl_ms_last={self.feed_cl_to_send_ms_last:.0f}"
                f" feed_cl_ms_max={self.feed_cl_to_send_ms_max:.0f}"
                f" feed_cl_n={self.feed_cl_to_send_n}"
            )
        return base

    def snapshot_ru(self) -> str:
        return (
            f"готовы слать={self.would_send} симуляция ок={self.simulated_ok} "
            f"симуляция fail={self.simulated_fail} отправлено={self.sent} "
            f"зашли={self.landed} ушли чужим={self.lost_to_foreign} "
            f"пропуск={self.skipped} 429={self.rate_limit_hits} "
            f"последний revert={self.last_revert or self.revert_reason or 'нет'}"
        )


def diagnostic_enabled(chain: str | None = None) -> bool:
    return bot_config._flag("MORPHO_DIAGNOSTIC_MODE", chain, False)


def min_net_profit_usd(chain: str | None = None, *, diagnostic: bool = False) -> float:
    """MIN_NET_PROFIT_USD / MORPHO_MIN_NET_PROFIT_USD. Diagnostic default 0.05."""
    raw = bot_config._text("MIN_NET_PROFIT_USD", chain, "")
    if not raw:
        raw = bot_config._text(
            "MORPHO_MIN_NET_PROFIT_USD", chain, "0.05" if diagnostic else "0"
        )
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.05 if diagnostic else 0.0


def duration_is_forever(seconds: int | None) -> bool:
    return seconds is None or int(seconds) <= 0


AlertFn = Callable[..., Any]


class MorphoExecutor:
    """Encode + (observe|paper|gated-live) Morpho flash liquidations."""

    def __init__(self, chain: str, w3: Web3, *, alert: AlertFn | None = None) -> None:
        self.chain = chain.lower()
        self.w3 = w3
        self.alert = alert
        self.log = logging.getLogger(f"morpho_executor.{self.chain}")
        self.metrics = ExecutorMetrics()
        self.rate_limit = attach_public_rpc_429_counter(self.w3)
        self.enabled = bot_config._flag("MORPHO_EXECUTOR_ENABLED", self.chain, True)
        raw_mode = (bot_config._text("MORPHO_MODE", self.chain, "observe") or "observe").strip().lower()
        self.mode = raw_mode if raw_mode in MODES else "observe"
        self.auto_execute = bot_config._flag("MORPHO_AUTO_EXECUTE", self.chain, False)
        self.live_confirm = (bot_config._text("MORPHO_LIVE_CONFIRM", self.chain, "") or "").strip()
        self.live_mainnet = bot_config._flag("MORPHO_LIVE_MAINNET", self.chain, False)
        self.contract_addr = _cfg_addr("MORPHO_LIQ_CONTRACT", self.chain)
        self.diagnostic_mode = diagnostic_enabled(self.chain)
        self.min_debt_usd = float(bot_config._decimal("MORPHO_MIN_DEBT_USD", self.chain, "0"))
        self.max_debt_usd = float(bot_config._decimal("MORPHO_MAX_DEBT_USD", self.chain, "25000"))
        # Cap flash repay size (whale chop). 0 = full. Prefer MORPHO_REPAY_SLICES_USD.
        self.max_repay_usd = float(bot_config._decimal("MORPHO_MAX_REPAY_USD", self.chain, "0"))
        self.repay_slices_usd = self._parse_repay_slices()
        self.min_edge_usd = float(bot_config._decimal("MORPHO_MIN_EDGE_USD", self.chain, "20"))
        self.min_net_profit_usd = min_net_profit_usd(
            self.chain, diagnostic=self.diagnostic_mode
        )
        if self.diagnostic_mode:
            # Diagnostic: no $300 min-debt floor. max_debt from env (0 = uncapped).
            self.min_debt_usd = 0.0
        self.gas_limit = max(21000, bot_config._integer("MORPHO_GAS_LIMIT", self.chain, 800_000))
        self.priority_gwei = float(bot_config._decimal("MORPHO_PRIORITY_FEE_GWEI", self.chain, "0.05"))
        self.gas_usd_override = float(
            bot_config._decimal("MORPHO_GAS_USD", self.chain, "0")
        )
        self.eth_usd_override = float(
            bot_config._decimal("MORPHO_ETH_USD", self.chain, "0")
        )
        self._eth_usd_cache: tuple[float, float] | None = None
        self.priority_max_gwei = float(
            bot_config._decimal("MORPHO_PRIORITY_FEE_MAX_GWEI", self.chain, "3")
        )
        self.storm_priority_gwei = float(
            bot_config._decimal("MORPHO_STORM_PRIORITY_GWEI", self.chain, "2.0")
        )
        self.storm_seconds = float(
            bot_config._decimal("MORPHO_STORM_SECONDS", self.chain, "15")
        )
        self.storm_oracle_bps = max(
            1, bot_config._integer("MORPHO_STORM_ORACLE_BPS", self.chain, 25)
        )
        self._storm_until = 0.0
        self.gas_bump_pct = max(1, bot_config._integer("MORPHO_GAS_BUMP_PCT", self.chain, 25))
        self.gas_bump_max = max(0, bot_config._integer("MORPHO_GAS_BUMP_MAX", self.chain, 4))
        self.max_inflight = min(
            8, max(3, bot_config._integer("MORPHO_MAX_INFLIGHT", self.chain, 6))
        )
        self.stagger_ms = max(0, bot_config._integer("MORPHO_STAGGER_MS", self.chain, 40))
        self.soft_batch_max = min(
            8, max(1, bot_config._integer("MORPHO_SOFT_BATCH_MAX", self.chain, 6))
        )
        # Hard-batch inclusion: prefer 2–3 victims (faster than fat 6-pack).
        self.hard_batch_max = min(
            4, max(2, bot_config._integer("MORPHO_HARD_BATCH_MAX", self.chain, 3))
        )
        self.preencode_ttl_seconds = float(
            bot_config._decimal("MORPHO_PREENCODE_TTL_SECONDS", self.chain, "20")
        )
        self.preencode_ttl_seconds = min(60.0, max(5.0, self.preencode_ttl_seconds))
        self.slippage_bps = max(1, bot_config._integer("MORPHO_SLIPPAGE_BPS", self.chain, 200))
        self.swap_deadline_seconds = bot_config._integer(
            "MORPHO_SWAP_DEADLINE_SECONDS", self.chain, 120
        )
        self.cooldown_seconds = float(
            bot_config._decimal("MORPHO_LIQ_COOLDOWN_SECONDS", self.chain, "15")
        )
        # After foreign liq / known-dead: block LIVE longer than send cooldown.
        self.dead_ttl_seconds = float(
            bot_config._decimal("MORPHO_DEAD_TTL_SECONDS", self.chain, "90")
        )
        self.dead_ttl_seconds = min(300.0, max(15.0, self.dead_ttl_seconds))
        self.hf_threshold = Decimal(
            str(bot_config._decimal("MORPHO_HF_THRESHOLD", self.chain, "1.0"))
        )
        self.private_tx_url = bot_config._text(
            "PRIVATE_TX_RPC_URL", self.chain, inherit=False
        )
        self.operator = _cfg_addr("MORPHO_OPERATOR_ADDRESS", self.chain)
        spec = bot_config._text("MORPHO_ALLOWED_MARKETS", self.chain, inherit=True)
        self.allowed = parse_allowed_markets(self.chain, spec or None)
        self.allowed_ids = {m.market_id.lower() for m in self.allowed}
        self.routers = self._load_routers()
        self.intermediates = self._load_intermediates()
        self.weth = _addr(
            bot_config._text("MORPHO_WETH", self.chain, WETH_BASE if self.chain == "base" else WETH_BASE)
            or WETH_BASE
        )
        self.aero_factory = _addr(
            bot_config._text("MORPHO_AERO_FACTORY", self.chain, AERODROME_FACTORY_BASE)
            or AERODROME_FACTORY_BASE
        )
        self.v3_fee_default = bot_config._integer("MORPHO_V3_FEE", self.chain, 3000)
        if self.v3_fee_default not in V3_FEES:
            self.v3_fee_default = 3000

        self.liq_abi = _load_liquidator_abi()
        self.contract = None
        if self.contract_addr:
            self.contract = self.w3.eth.contract(
                address=self.contract_addr, abi=self.liq_abi
            )
        self.error_table = {
            "0x" + Web3.keccak(text=sig)[:4].hex().removeprefix("0x"): sig
            for sig in ERROR_SIGNATURES
        }

        self._prebuilt: dict[tuple[str, str], PrebuiltSwap] = {}
        self._cooldown_until: dict[tuple[str, str], float] = {}
        self._dead_until: dict[tuple[str, str], float] = {}
        # Feeder-pin arm: swap route ready before Borrow (A).
        self._feeder_armed_until: dict[tuple[str, str], float] = {}
        self._preencoded: dict[tuple[str, str], PreencodedShot] = {}
        self.feeder_arm_seconds = float(
            bot_config._decimal("MORPHO_FEEDER_ARM_SECONDS", self.chain, "25")
        )
        self.feeder_arm_seconds = min(60.0, max(8.0, self.feeder_arm_seconds))
        self._inflight = 0
        self._nonce: int | None = None
        self._nonce_lock = threading.Lock()
        self._account = None
        self._private_w3 = None
        self._live_w3: Web3 | None = None
        self._live_w3_url: str = ""
        self.live_rpc_timeout = float(
            bot_config._decimal("MORPHO_LIVE_RPC_TIMEOUT", self.chain, "4")
        )
        self.live_rpc_timeout = min(12.0, max(2.0, self.live_rpc_timeout))
        self._sem: asyncio.Semaphore | None = None
        self._kick_seq = 0
        self._onchain_batch = self._detect_onchain_batch()
        self._onchain_fire = self._detect_onchain_fire()

        live_ok, live_why = self._live_ready()
        self.log.info(
            "executor mode=%s auto=%s contract=%s markets=%d inflight_cap=%d "
            "storm_tip=%.4fgwei/%.0fs dead_ttl=%.0fs hard_batch_max=%d "
            "onchain_batch=%s onchain_fire=%s live_rpc_timeout=%.0fs "
            "live_ready=%s (%s)",
            self.mode,
            self.auto_execute,
            self.contract_addr or "(none)",
            len(self.allowed),
            self.max_inflight,
            self.storm_priority_gwei,
            self.storm_seconds,
            self.dead_ttl_seconds,
            self.hard_batch_max,
            self._onchain_batch,
            self._onchain_fire,
            self.live_rpc_timeout,
            live_ok,
            live_why,
        )
        if self.diagnostic_mode:
            self.log.warning(
                "%s min_net_profit=$%.4f max_debt=$%.0f repay_slices=%s min_debt_floor=OFF flash_fee=$0",
                DIAGNOSTIC_NOT_COMBAT,
                self.min_net_profit_usd,
                self.max_debt_usd,
                self.repay_slices_usd,
            )
        if self.mode == "live" and not live_ok:
            self.log.warning("live requested but gated (%s) — behaving as paper/observe", live_why)
            self.mode = "paper" if self.contract_addr else "observe"

    def _load_routers(self) -> list[str]:
        raw = bot_config._text("MORPHO_ROUTERS", self.chain, inherit=False)
        if raw:
            return [_addr(p.strip()) for p in raw.split(",") if p.strip()]
        if self.chain == "base":
            return [
                _addr(UNI_V3_SWAP_ROUTER_02_BASE),
                _addr(UNI_V3_SWAP_ROUTER_02_ALT),
                _addr(AERODROME_ROUTER_BASE),
            ]
        return []

    def _load_intermediates(self) -> set[str]:
        raw = bot_config._text("MORPHO_INTERMEDIATE_TOKENS", self.chain, inherit=False)
        if raw:
            return {_addr(p.strip()) for p in raw.split(",") if p.strip()}
        if self.chain == "base":
            return {_addr(WETH_BASE)}
        return set()

    def _parse_repay_slices(self) -> list[float]:
        """USD caps to try per liq. 0 = full position. Default: 25k then full."""
        raw = (bot_config._text("MORPHO_REPAY_SLICES_USD", self.chain, inherit=True) or "").strip()
        if raw:
            out: list[float] = []
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                out.append(max(0.0, float(part)))
            if out:
                return out
        if self.max_repay_usd > 0:
            return [self.max_repay_usd]
        return [0.0]

    def _detect_onchain_batch(self) -> bool:
        """True if deployed bytecode contains liquidateWithFlashBatch selector."""
        if not self.contract_addr:
            return False
        try:
            code = self.w3.eth.get_code(self.contract_addr)
            return BATCH_SELECTOR in bytes(code)
        except Exception:  # noqa: BLE001
            return False

    def _detect_onchain_fire(self) -> bool:
        """True if deployed bytecode contains fire() (preload path)."""
        if not self.contract_addr:
            return False
        try:
            code = self.w3.eth.get_code(self.contract_addr)
            return FIRE_SELECTOR in bytes(code)
        except Exception:  # noqa: BLE001
            return False

    def in_storm(self) -> bool:
        return time.monotonic() < self._storm_until

    def enter_storm(self, reason: str = "") -> None:
        """Raise tip for a short window — cascade / oracle spike."""
        until = time.monotonic() + self.storm_seconds
        if until > self._storm_until:
            self._storm_until = until
        self.log.info(
            "STORM ON tip=%.4fgwei for %.0fs (%s)",
            self.storm_priority_gwei,
            self.storm_seconds,
            reason or "cascade",
        )

    def maybe_storm_from_oracle_bps(self, bps: int) -> None:
        if bps >= self.storm_oracle_bps:
            self.enter_storm(f"oracle Δ={bps}bps")

    def _effective_priority_gwei(self) -> float:
        tip = self.priority_gwei
        if self.in_storm():
            tip = max(tip, self.storm_priority_gwei)
        return min(tip, self.priority_max_gwei)

    def _note_feed_cl_would_send(self, intent: LiqIntent) -> None:
        t0 = intent.path_t0
        if t0 <= 0:
            return
        ms = (time.monotonic() - t0) * 1000.0
        self.metrics.feed_cl_to_send_ms_last = ms
        self.metrics.feed_cl_to_send_ms_max = max(
            self.metrics.feed_cl_to_send_ms_max, ms
        )
        self.metrics.feed_cl_to_send_n += 1
        self.log.info(
            "feed-cl path %.1fms ws->would_send user=%s",
            ms,
            intent.user[:12],
        )

    def _sized_debt_for_filter(self, debt_usd: float) -> float:
        """Optimistic size used for net filter — largest bite we might take."""
        if any(s <= 0 for s in self.repay_slices_usd):
            return float(debt_usd)
        return min(float(debt_usd), max(self.repay_slices_usd))

    def _live_ready(self) -> tuple[bool, str]:
        if self.mode != "live":
            return False, f"mode={self.mode}"
        if not self.auto_execute:
            return False, "MORPHO_AUTO_EXECUTE=false"
        if self.live_confirm != LIVE_CONFIRM_TOKEN:
            return False, f"MORPHO_LIVE_CONFIRM!={LIVE_CONFIRM_TOKEN}"
        if not self.contract_addr:
            return False, "MORPHO_LIQ_CONTRACT empty"
        key = bot_config._text("MORPHO_PRIVATE_KEY", self.chain, inherit=True)
        if not key:
            return False, "MORPHO_PRIVATE_KEY empty"
        cid = CHAIN_IDS.get(self.chain, 0)
        if cid == 8453 and not self.live_mainnet:
            return False, "Base mainnet live blocked (MORPHO_LIVE_MAINNET)"
        return True, "ok"

    def _maybe_load_signer(self) -> None:
        live_ok, _ = self._live_ready()
        if not live_ok:
            return
        self._load_signer_if_key()

    def prebuild(self, market: MorphoMarketConfig, user: str) -> PrebuiltSwap | None:
        """Cache router/path for a hot position. No RPC."""
        if market.market_id.lower() not in self.allowed_ids:
            return None
        key = (market.market_id.lower(), _addr(user).lower())
        existing = self._prebuilt.get(key)
        if existing is not None:
            return existing
        router = self.routers[0] if self.routers else _addr(UNI_V3_SWAP_ROUTER_02_BASE)
        hop = ""
        kind = "univ3_single"
        if _addr(router) == _addr(AERODROME_ROUTER_BASE):
            kind = "aero"
        fee = int(market.v3_fee) if getattr(market, "v3_fee", 0) else self.v3_fee_default
        if fee not in V3_FEES:
            fee = self.v3_fee_default
        pb = PrebuiltSwap(router=router, kind=kind, fee=fee, hop=hop)
        self._prebuilt[key] = pb
        return pb

    def arm_feeder(self, market: MorphoMarketConfig, user: str) -> None:
        """A: on feeder Transfer — prebuild swap + mark armed until Borrow."""
        if market.market_id.lower() not in self.allowed_ids:
            return
        pb = self.prebuild(market, user)
        key = self._position_key(user, market.market_id)
        self._feeder_armed_until[key] = time.monotonic() + self.feeder_arm_seconds
        self.log.info(
            "feeder armed %.0fs user=%s %s/%s route=%s",
            self.feeder_arm_seconds,
            key[1][:12],
            market.loan_symbol,
            market.collateral_symbol,
            pb.kind if pb else "?",
        )

    def is_feeder_armed(self, user: str, market_id: str) -> bool:
        key = self._position_key(user, market_id)
        until = self._feeder_armed_until.get(key)
        return until is not None and time.monotonic() < until

    def clear_feeder_arm(self, user: str, market_id: str) -> None:
        key = self._position_key(user, market_id)
        self._feeder_armed_until.pop(key, None)

    def _intent_key(self, intent: LiqIntent) -> tuple[str, str]:
        return (intent.market.market_id.lower(), _addr(intent.user).lower())

    def eth_usd_price(self) -> float:
        """ETH/USD via Chainlink if RPC works, else MORPHO_ETH_USD, else 3500."""
        if self.eth_usd_override > 0:
            return self.eth_usd_override
        now = time.monotonic()
        cached = self._eth_usd_cache
        if cached is not None and (now - cached[0]) < 60.0:
            return cached[1]
        addr = CHAINLINK_ETH_USD.get(self.chain)
        px = 0.0
        if addr:
            try:
                feed = self.w3.eth.contract(
                    address=Web3.to_checksum_address(addr), abi=_LATEST_ANSWER_ABI
                )
                raw = int(feed.functions.latestAnswer().call())
                if raw > 0:
                    px = raw / 1e8
            except Exception:  # noqa: BLE001
                px = 0.0
        if px <= 0:
            px = DEFAULT_ETH_USD
        self._eth_usd_cache = (now, px)
        return px

    def gas_cost_usd(self) -> float:
        """Fixed gasLimit * priority gwei, in USD. Else MORPHO_GAS_USD."""
        if self.gas_usd_override > 0:
            return self.gas_usd_override
        return estimate_priority_gas_usd(
            gas_limit=self.gas_limit,
            priority_gwei=self.priority_gwei,
            eth_usd=self.eth_usd_price(),
        )

    def net_profit_for(self, intent: LiqIntent) -> float:
        sized = self._sized_debt_for_filter(float(intent.debt_usd))
        br = estimate_net_profit_usd(
            sized,
            intent.market.lltv_wad,
            gas_cost_usd=self.gas_cost_usd(),
            slippage_bps=self.slippage_bps,
        )
        intent.net_profit_usd = br.net_profit_usd
        return br.net_profit_usd

    def _passes_filters(self, intent: LiqIntent) -> str | None:
        if not self.enabled:
            return "executor disabled"
        if intent.market.market_id.lower() not in self.allowed_ids:
            return "market not in MORPHO_ALLOWED_MARKETS"
        if intent.health_factor >= self.hf_threshold:
            return f"HF {intent.health_factor:.4f} >= {self.hf_threshold}"
        if self.max_debt_usd > 0 and intent.debt_usd > self.max_debt_usd:
            return f"debt ${intent.debt_usd:.0f} > max"
        net = self.net_profit_for(intent)
        # Take-all / diagnostic: still compute net for logs, but do not gate on size/edge.
        if not self.diagnostic_mode:
            if intent.debt_usd < self.min_debt_usd:
                return f"debt ${intent.debt_usd:.0f} < min"
            if net < self.min_net_profit_usd:
                return (
                    f"net_profit_usd ${net:.4f} < min ${self.min_net_profit_usd:.4f}"
                )
            if intent.profit_usd < self.min_edge_usd:
                return f"edge ${intent.profit_usd:.2f} < min"
        elif net < self.min_net_profit_usd:
            # Diagnostic can set MORPHO_MIN_NET_PROFIT_USD negative to eat dust.
            return (
                f"net_profit_usd ${net:.4f} < min ${self.min_net_profit_usd:.4f}"
            )
        if intent.borrow_shares <= 0 or intent.collateral <= 0:
            return "empty shares"
        if intent.oracle_price <= 0:
            return "no oracle"
        return None

    def _on_cooldown(self, key: tuple[str, str]) -> bool:
        until = self._cooldown_until.get(key)
        return until is not None and time.monotonic() < until

    def _touch_cooldown(self, key: tuple[str, str]) -> None:
        self._cooldown_until[key] = time.monotonic() + self.cooldown_seconds

    def _position_key(self, user: str, market_id: str) -> tuple[str, str]:
        user_l = user.lower()
        if user.startswith("0x") and len(user) >= 42:
            user_l = _addr(user).lower()
        return (market_id.lower(), user_l)

    def _on_dead(self, key: tuple[str, str]) -> bool:
        until = self._dead_until.get(key)
        return until is not None and time.monotonic() < until

    def mark_position_dead(
        self, user: str, market_id: str, *, reason: str = "foreign"
    ) -> None:
        """Negative cache: do not LIVE this (market,user) for dead_ttl_seconds."""
        key = self._position_key(user, market_id)
        self._dead_until[key] = time.monotonic() + self.dead_ttl_seconds
        self._touch_cooldown(key)
        self._feeder_armed_until.pop(key, None)
        self._preencoded.pop(key, None)
        self.log.info(
            "position dead-ttl %.0fs reason=%s user=%s market=%s…",
            self.dead_ttl_seconds,
            reason,
            key[1][:12],
            key[0][:12],
        )

    def is_position_dead(self, user: str, market_id: str) -> bool:
        return self._on_dead(self._position_key(user, market_id))

    def clear_position_dead(self, user: str, market_id: str) -> None:
        """Clear dead-TTL when on-chain refresh still shows borrow (partial liq)."""
        key = self._position_key(user, market_id)
        if key in self._dead_until:
            del self._dead_until[key]
            self.log.debug("position dead-ttl cleared user=%s", key[1][:12])

    def refresh_hf(self, intent: LiqIntent) -> LiqIntent:
        hf, borrowed = hf_from_cached_shares(
            collateral=intent.collateral,
            borrow_shares=intent.borrow_shares,
            total_borrow_assets=intent.total_borrow_assets,
            total_borrow_shares=intent.total_borrow_shares,
            oracle_price=intent.oracle_price,
            lltv_wad=intent.market.lltv_wad,
        )
        loan_dec = intent.loan_decimals
        intent.health_factor = hf
        px = loan_token_price_usd(
            intent.market.loan_symbol, eth_usd=self.eth_usd_price()
        )
        intent.debt_usd = (borrowed / (10**loan_dec)) * px
        return intent

    def encode(
        self,
        intent: LiqIntent,
        *,
        recipient: str | None = None,
        repay_cap_usd: float | None = None,
    ) -> EncodedLiq | None:
        to = recipient or self.contract_addr
        if not to:
            to = "0x" + "11" * 20
        to = _addr(to)
        borrowed = shares_to_assets_up(
            intent.borrow_shares,
            intent.total_borrow_assets,
            intent.total_borrow_shares,
        )
        # repay_cap_usd: None → legacy max_repay; 0 → full; >0 → chop.
        if repay_cap_usd is None:
            cap = self.max_repay_usd
        else:
            cap = float(repay_cap_usd)
        repay_assets = borrowed
        if cap > 0:
            px = loan_token_price_usd(
                intent.market.loan_symbol, eth_usd=self.eth_usd_price()
            )
            if px > 0:
                max_raw = int(cap / px * (10**intent.loan_decimals))
                if max_raw > 0:
                    repay_assets = min(borrowed, max_raw)
        seized = seized_assets_for_repay(
            repay_assets,
            intent.oracle_price,
            intent.market.lltv_wad,
            collateral_cap=intent.collateral,
        )
        if seized <= 0:
            return None
        expected_loan = seized * intent.oracle_price // ORACLE_PRICE_SCALE
        min_profit = int(self.min_edge_usd * (10**intent.loan_decimals))
        floor = expected_loan * (10_000 - self.slippage_bps) // 10_000
        amount_out_min = max(floor, min_profit)
        if amount_out_min >= expected_loan:
            amount_out_min = expected_loan * (10_000 - self.slippage_bps) // 10_000

        pb = self.prebuild(intent.market, intent.user)
        if pb is None:
            return None
        deadline = int(time.time()) + self.swap_deadline_seconds
        coll = _addr(intent.market.collateral_token)
        loan = _addr(intent.market.loan_token)
        if pb.kind == "univ3_hop" and pb.hop:
            calldata = encode_univ3_exact_input_hop(
                token_in=coll,
                mid=pb.hop,
                token_out=loan,
                fee_in=pb.fee,
                fee_out=pb.fee_hop,
                recipient=to,
                amount_in=seized,
                amount_out_min=amount_out_min,
            )
        elif pb.kind in {"aero", "aero_hop"}:
            hop = pb.hop if pb.kind == "aero_hop" else None
            calldata = encode_aerodrome_swap(
                token_in=coll,
                token_out=loan,
                amount_in=seized,
                amount_out_min=amount_out_min,
                recipient=to,
                deadline=deadline,
                factory=self.aero_factory,
                hop=hop,
            )
        else:
            calldata = encode_univ3_exact_input_single(
                token_in=coll,
                token_out=loan,
                fee=pb.fee,
                recipient=to,
                amount_in=seized,
                amount_out_min=amount_out_min,
            )
        params = (
            _addr(intent.market.loan_token),
            _addr(intent.market.collateral_token),
            _addr(intent.market.oracle),
            _addr(intent.market.irm),
            int(intent.market.lltv_wad),
        )
        dummy = self.w3.eth.contract(
            address=self.contract_addr or "0x" + "22" * 20, abi=self.liq_abi
        )
        tx_data = dummy.encode_abi(
            "liquidateWithFlash",
            args=[
                params,
                _addr(intent.user),
                int(seized),
                0,
                (_addr(pb.router), calldata),
                int(min_profit),
            ],
        )
        raw = _to_bytes(tx_data)
        encoded = EncodedLiq(
            market_params=params,
            borrower=_addr(intent.user),
            seized_assets=int(seized),
            repaid_shares=0,
            router=_addr(pb.router),
            swap_calldata=calldata,
            min_profit=int(min_profit),
            calldata=raw,
            amount_in=int(seized),
            amount_out_min=int(amount_out_min),
            kind=pb.kind,
        )
        key = self._intent_key(intent)
        self._preencoded[key] = PreencodedShot(
            encoded=encoded,
            borrow_shares=int(intent.borrow_shares),
            collateral=int(intent.collateral),
            oracle_price=int(intent.oracle_price),
            stored_mono=time.monotonic(),
        )
        return encoded

    def encode_cached(self, intent: LiqIntent, *, recipient: str | None = None) -> EncodedLiq | None:
        """Reuse preencoded calldata when shares/oracle unchanged (off-chain preload)."""
        key = self._intent_key(intent)
        hit = self._preencoded.get(key)
        if (
            hit is not None
            and hit.borrow_shares == int(intent.borrow_shares)
            and hit.collateral == int(intent.collateral)
            and hit.oracle_price == int(intent.oracle_price)
            and (time.monotonic() - hit.stored_mono) <= self.preencode_ttl_seconds
        ):
            return hit.encoded
        return self.encode(intent, recipient=recipient)

    def warm_preencode(self, intent: LiqIntent) -> bool:
        """Point 1/4: encode ahead of LIVE without sending."""
        if self._on_dead(self._intent_key(intent)) or self._passes_filters(intent):
            return False
        enc = self.encode(intent, recipient=self.contract_addr or None)
        return enc is not None

    def schedule(self, intent: LiqIntent) -> None:
        """Fire-and-forget. Never awaited by the scanner hot path."""
        if not self.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.try_liq(intent)
            return
        self._kick_seq += 1
        reason = str(intent.reason or "")
        # Oracle / storm / soft-batch / feeder Borrow: no stagger — every ms counts.
        rl = reason.lower()
        if (
            rl.startswith("oracle")
            or rl.startswith("storm")
            or rl.startswith("soft-batch")
            or rl.startswith("borrow")
            or rl.startswith("feeder")
            or self.in_storm()
        ):
            delay = 0.0
        else:
            delay = (self._kick_seq % self.max_inflight) * (self.stagger_ms / 1000.0)
        loop.create_task(self._run_scheduled(intent, delay), name="morpho-try-liq")

    def schedule_many(self, intents: list[LiqIntent], *, reason: str = "") -> None:
        """Soft-batch parallel kicks; if on-chain batch is live, one hard-batch tx."""
        if not intents or not self.enabled:
            return
        batch = intents[: self.soft_batch_max]
        if len(batch) >= 2 or self.in_storm():
            self.enter_storm(reason or f"soft-batch×{len(batch)}")
        # Hard-batch: same market + deployed liquidateWithFlashBatch (cap 2–3).
        if self._onchain_batch and len(batch) >= 2:
            mid = batch[0].market.market_id.lower()
            same = [i for i in batch if i.market.market_id.lower() == mid][
                : self.hard_batch_max
            ]
            if len(same) >= 2:
                self.log.info(
                    "hard-batch schedule n=%d market=%s/%s tip=%.4fgwei",
                    len(same),
                    same[0].market.loan_symbol,
                    same[0].market.collateral_symbol,
                    self._effective_priority_gwei(),
                )
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    self.try_hard_batch(same)
                    return
                loop.create_task(
                    self._run_hard_batch(same), name="morpho-hard-batch"
                )
                # Remainder (other markets) — soft path.
                rest = [i for i in batch if i.market.market_id.lower() != mid]
                for intent in rest:
                    intent.reason = f"soft-batch:{intent.reason or reason or 'kick'}"
                    self.schedule(intent)
                return
        self.log.info(
            "soft-batch schedule n=%d storm=%s tip=%.4fgwei",
            len(batch),
            self.in_storm(),
            self._effective_priority_gwei(),
        )
        for intent in batch:
            if not str(intent.reason or "").startswith("oracle"):
                intent.reason = f"soft-batch:{intent.reason or reason or 'kick'}"
            self.schedule(intent)

    async def _run_hard_batch(self, intents: list[LiqIntent]) -> None:
        async with self._semaphore():
            try:
                await asyncio.to_thread(self.try_hard_batch, intents)
            except Exception as exc:  # noqa: BLE001
                self.log.error("hard-batch failed: %s", exc)
                for intent in intents:
                    self.schedule(intent)

    def try_hard_batch(self, intents: list[LiqIntent]) -> None:
        """One on-chain liquidateWithFlashBatch for same-market victims."""
        if not intents or not self._onchain_batch:
            for intent in intents:
                self.try_liq(intent)
            return
        encoded_parts: list[EncodedLiq] = []
        for intent in intents:
            key = self._intent_key(intent)
            if self._on_dead(key) or self._on_cooldown(key):
                continue
            why = self._passes_filters(intent)
            if why:
                self.metrics.skipped += 1
                continue
            enc = self.encode_cached(intent, recipient=self.contract_addr or None)
            if enc is None:
                continue
            encoded_parts.append(enc)
            self.metrics.would_send += 1
            self._note_feed_cl_would_send(intent)
        if len(encoded_parts) < 2:
            # Fall back to singles.
            for intent in intents:
                self.try_liq(intent)
            return
        market = intents[0].market
        params = encoded_parts[0].market_params
        borrowers = [e.borrower for e in encoded_parts]
        seized = [e.seized_assets for e in encoded_parts]
        repaid = [0 for _ in encoded_parts]
        swaps = [(e.router, e.swap_calldata) for e in encoded_parts]
        mins = [e.min_profit for e in encoded_parts]
        dummy = self.w3.eth.contract(
            address=self.contract_addr or "0x" + "22" * 20, abi=self.liq_abi
        )
        tx_data = dummy.encode_abi(
            "liquidateWithFlashBatch",
            args=[params, borrowers, seized, repaid, swaps, mins],
        )
        raw = _to_bytes(tx_data)
        # Reuse first EncodedLiq shell for live send (calldata replaced).
        shell = EncodedLiq(
            market_params=params,
            borrower=borrowers[0],
            seized_assets=sum(seized),
            repaid_shares=0,
            router=swaps[0][0],
            swap_calldata=swaps[0][1],
            min_profit=sum(mins),
            calldata=raw,
            amount_in=sum(e.amount_in for e in encoded_parts),
            amount_out_min=sum(e.amount_out_min for e in encoded_parts),
            kind="hard-batch",
        )
        self.log.info(
            "would send morpho HARD-BATCH mode=%s n=%d %s/%s tip=%.4fgwei",
            self.mode,
            len(encoded_parts),
            market.loan_symbol,
            market.collateral_symbol,
            self._effective_priority_gwei(),
        )
        self._notify(
            f"Morpho HARD-BATCH готов ({self.mode}) n={len(encoded_parts)}\n"
            f"{market.loan_symbol}/{market.collateral_symbol}\n"
            f"tip={self._effective_priority_gwei():.4f}gwei",
            dedup_key=f"morpho-hard-batch-{self.chain}-{market.market_id[:12]}",
            cooldown=30.0,
        )
        live_ok, live_why = self._live_ready()
        if self.mode == "observe":
            return
        if self.mode == "paper":
            # Paper: simulate first item only (batch eth_call needs full state).
            self._paper(intents[0], encoded_parts[0])
            return
        if not live_ok:
            self.log.warning("hard-batch live blocked (%s)", live_why)
            return
        # Higher gas for multi-item.
        old_limit = self.gas_limit
        self.gas_limit = min(8_000_000, max(old_limit, old_limit * len(encoded_parts)))
        try:
            self._live_send(intents[0], shell)
        finally:
            self.gas_limit = old_limit
        for enc, intent in zip(encoded_parts, intents):
            self._touch_cooldown(self._intent_key(intent))

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_inflight)
        return self._sem

    async def _run_scheduled(self, intent: LiqIntent, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        async with self._semaphore():
            try:
                await asyncio.to_thread(self.try_liq, intent)
            except Exception as exc:  # noqa: BLE001
                self.log.error("try_liq failed %s: %s", intent.user, exc)

    def try_liq(self, intent: LiqIntent) -> EncodedLiq | None:
        """Observe/paper/live. Live is gated; Base mainnet requires extra flags.

        Whale path: fire each MORPHO_REPAY_SLICES_USD size (e.g. 25k then full).
        """
        key = self._intent_key(intent)
        if self._on_dead(key):
            self.metrics.skipped += 1
            self.log.debug(
                "skip dead-ttl %s %s", intent.market.collateral_symbol, intent.user
            )
            return None
        if self._on_cooldown(key):
            self.metrics.skipped += 1
            return None
        why = self._passes_filters(intent)
        if why:
            self.metrics.skipped += 1
            self.log.debug("skip %s %s: %s", intent.market.collateral_symbol, intent.user, why)
            return None

        slices = list(self.repay_slices_usd) or [0.0]
        # Dust / small debt: skip pointless $25k-first slice (saves encode+send ms).
        positive = [c for c in slices if c > 0]
        if positive and intent.debt_usd > 0 and intent.debt_usd < min(positive):
            slices = [0.0]
        last: EncodedLiq | None = None
        live_ok, live_why = self._live_ready()
        sent_any = False

        for i, cap in enumerate(slices):
            if self._on_dead(key):
                self.metrics.skipped += 1
                self.log.info(
                    "abort slices after dead-ttl %s %s",
                    intent.market.collateral_symbol,
                    intent.user[:12],
                )
                break
            # Full repay (cap<=0): reuse off-chain preencode; capped slices re-encode.
            if cap <= 0:
                encoded = self.encode_cached(
                    intent, recipient=self.contract_addr or None
                )
            else:
                encoded = self.encode(
                    intent,
                    recipient=self.contract_addr or None,
                    repay_cap_usd=cap,
                )
            if encoded is None:
                self.metrics.skipped += 1
                continue
            last = encoded
            self.metrics.would_send += 1
            self._note_feed_cl_would_send(intent)
            cap_lbl = "full" if cap <= 0 else f"${cap:,.0f}"
            fast = str(intent.reason or "").lower().startswith(
                ("borrow", "feeder", "oracle")
            )
            self.log.info(
                "would send morpho liq mode=%s slice=%s %s/%s user=%s HF=%.4f debt=$%.0f "
                "edge=$%.2f net_profit_usd=$%.4f seized=%d router=%s kind=%s reason=%s",
                self.mode,
                cap_lbl,
                intent.market.loan_symbol,
                intent.market.collateral_symbol,
                intent.user,
                intent.health_factor,
                intent.debt_usd,
                intent.profit_usd,
                intent.net_profit_usd,
                encoded.seized_assets,
                encoded.router,
                encoded.kind,
                intent.reason,
            )
            # Point 3: skip TG on hot-path race (still log).
            if not fast:
                self._notify(
                    f"Morpho готов слать ({self.mode}"
                    f"{', диагностика' if self.diagnostic_mode else ''}) slice={cap_lbl}\n"
                    f"{intent.market.loan_symbol}/{intent.market.collateral_symbol}\n"
                    f"{intent.user}\n"
                    f"HF={intent.health_factor:.4f} долг ~${intent.debt_usd:,.0f} "
                    f"edge ~${intent.profit_usd:,.2f} чистыми ${intent.net_profit_usd:,.4f}\n"
                    f"seized={encoded.seized_assets} kind={encoded.kind}\n"
                    f"причина={intent.reason}",
                    dedup_key=(
                        f"morpho-would-{self.chain}-{intent.market.market_id[:12]}-"
                        f"{intent.user}-{cap_lbl}"
                    ),
                    cooldown=max(60.0, self.cooldown_seconds),
                )
            if self.mode == "observe":
                continue
            if self.mode == "paper":
                self._paper(intent, encoded)
                continue
            if not live_ok:
                self.log.warning("live blocked (%s) — paper/observe only", live_why)
                if self.contract_addr:
                    self._paper(intent, encoded)
                continue
            self._live_send(intent, encoded)
            sent_any = True
            # Stagger second size so nonce / mempool can accept both.
            if i + 1 < len(slices) and self.stagger_ms > 0:
                time.sleep(self.stagger_ms / 1000.0)

        if last is not None or sent_any:
            self._touch_cooldown(key)
        return last
    def _paper(self, intent: LiqIntent, encoded: EncodedLiq) -> None:
        if not self.contract_addr:
            self.metrics.last_revert = "no MORPHO_LIQ_CONTRACT (paper encode-only)"
            self.log.info("paper: contract unset, encode-only for %s", intent.user)
            return
        frm = self.operator or ("0x" + "00" * 20)
        call_tx = {
            "from": frm,
            "to": self.contract_addr,
            "data": "0x" + encoded.calldata.hex(),
            "value": 0,
            "gas": self.gas_limit,
        }
        try:
            self.w3.eth.call(call_tx, "latest")
        except Exception as exc:  # noqa: BLE001
            reason = decode_revert(getattr(exc, "data", None), self.error_table)
            if reason == "revert without data":
                reason = f"{type(exc).__name__}: {exc}"
            self.metrics.simulated_fail += 1
            self.metrics.last_revert = reason[:180]
            self.log.info("paper eth_call revert %s: %s", intent.user, reason)
            self._notify(
                f"Morpho paper FAIL\n{intent.user}\n{reason[:300]}",
                dedup_key=f"morpho-paper-fail-{self.chain}-{intent.user}",
                cooldown=120.0,
            )
            return
        self.metrics.simulated_ok += 1
        self.log.info("paper eth_call OK %s seized=%d", intent.user, encoded.seized_assets)
        self._notify(
            f"Morpho paper OK (eth_call)\n"
            f"{intent.market.loan_symbol}/{intent.market.collateral_symbol}\n"
            f"{intent.user} HF={intent.health_factor:.4f} долг ~${intent.debt_usd:,.0f}",
            dedup_key=f"morpho-paper-ok-{self.chain}-{intent.user}",
            cooldown=120.0,
        )

    def _gas_fields(self, bump: int = 0) -> dict[str, int]:
        tip = Web3.to_wei(self._effective_priority_gwei(), "gwei")
        tip_max = Web3.to_wei(self.priority_max_gwei, "gwei")
        for _ in range(bump):
            tip = min(tip_max, tip * (100 + self.gas_bump_pct) // 100)
        try:
            latest = self._live_rpc().eth.get_block("latest")
            base = int(latest.get("baseFeePerGas") or 0)
        except Exception:
            base = 0
        max_fee = (base * 2 + int(tip)) if base else int(tip)
        for _ in range(bump):
            max_fee = max_fee * (100 + self.gas_bump_pct) // 100
        return {"maxFeePerGas": int(max_fee), "maxPriorityFeePerGas": int(tip)}

    def _rpc_url_for_live(self) -> str:
        provider = self.w3.provider
        url = getattr(provider, "current_url", None) or getattr(
            provider, "endpoint_uri", None
        )
        return str(url or "")

    def _live_rpc(self) -> Web3:
        """Short-timeout Web3 for nonce/send — never wait 30s on public RPC."""
        from web3.providers import HTTPProvider

        url = self._rpc_url_for_live()
        if (
            self._live_w3 is not None
            and self._live_w3_url == url
            and url
        ):
            return self._live_w3
        if not url:
            return self.w3
        self._live_w3 = Web3(
            HTTPProvider(
                url, request_kwargs={"timeout": self.live_rpc_timeout}
            )
        )
        self._live_w3_url = url
        return self._live_w3

    def _next_nonce(self) -> int:
        assert self._account is not None
        rpc = self._live_rpc()
        confirmed = rpc.eth.get_transaction_count(self._account.address)
        if self._nonce is None or confirmed > self._nonce:
            self._nonce = confirmed
        return self._nonce

    def _advance_nonce(self) -> None:
        if self._nonce is not None:
            self._nonce += 1

    def _live_send(self, intent: LiqIntent, encoded: EncodedLiq) -> None:
        """Broadcast path — only reached after _live_ready."""
        key = self._intent_key(intent)
        if self._on_dead(key):
            self.metrics.skipped += 1
            self.log.info(
                "live abort dead-ttl %s %s",
                intent.market.collateral_symbol,
                intent.user[:12],
            )
            return
        live_ok, live_why = self._live_ready()
        if not live_ok:
            self.log.error("live send refused (%s) — will not broadcast", live_why)
            return
        self._maybe_load_signer()
        if self._account is None or not self.contract_addr:
            self.log.error("live send missing signer/contract")
            return
        if self._inflight >= self.max_inflight:
            self.log.warning("inflight cap %d — drop %s", self.max_inflight, intent.user)
            self.metrics.skipped += 1
            return
        tx_hash = None
        tip_gwei = self._effective_priority_gwei()
        nonce = None
        with self._nonce_lock:
            if self._on_dead(key):
                self.metrics.skipped += 1
                return
            tx = self._unsigned_live_tx(encoded, bump=0)
            if tx is None:
                self.log.error("live send missing unsigned tx")
                return
            nonce = tx.get("nonce")
            relay = self._private_w3 or self._live_rpc()
            try:
                signed = self._account.sign_transaction(tx)
                raw = signed.raw_transaction
                tx_hash = relay.eth.send_raw_transaction(raw)
            except Exception as exc:  # noqa: BLE001
                self.log.error("live send failed %s: %s", intent.user, exc)
                self.metrics.last_revert = str(exc)[:180]
                self._nonce = None
                self._live_w3 = None
                self._notify(
                    f"Morpho LIVE не ушло\n"
                    f"{intent.market.loan_symbol}/{intent.market.collateral_symbol}\n"
                    f"{intent.user}\n"
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                    dedup_key=f"morpho-live-fail-{self.chain}-{intent.user}",
                    cooldown=45.0,
                )
                return
            self._inflight += 1
            self._advance_nonce()
            self.metrics.sent += 1
            tip_gwei = self._effective_priority_gwei()
            self.log.info(
                "LIVE sent nonce=%s hash=%s user=%s tip=%.4fgwei storm=%s",
                nonce,
                tx_hash.hex(),
                intent.user,
                tip_gwei,
                self.in_storm(),
            )
            self._inflight = max(0, self._inflight - 1)
        # TG outside nonce lock — never block the next LIVE on HTTP.
        if tx_hash is not None:
            self._notify(
                f"Morpho LIVE отправлено\n"
                f"{intent.market.loan_symbol}/{intent.market.collateral_symbol}\n"
                f"{intent.user}\n"
                f"HF={intent.health_factor:.4f} долг ~${intent.debt_usd:,.0f} "
                f"чистыми ${intent.net_profit_usd:,.4f}\n"
                f"tip={tip_gwei:.4f}gwei storm={self.in_storm()}\n"
                f"tx {tx_hash.hex()}",
                dedup_key=f"morpho-live-{tx_hash.hex()}",
                cooldown=15.0,
            )

    def _unsigned_live_tx(self, encoded: EncodedLiq, *, bump: int = 0) -> dict[str, Any] | None:
        """Build a typed live tx. Does not sign or send."""
        if not self._load_signer_if_key() or not self.contract_addr:
            return None
        rpc = self._live_rpc()
        chain_id = CHAIN_IDS.get(self.chain) or int(rpc.eth.chain_id)
        tx: dict[str, Any] = {
            "from": self._account.address,
            "to": self.contract_addr,
            "data": "0x" + encoded.calldata.hex(),
            "value": 0,
            "gas": self.gas_limit,
            "nonce": self._next_nonce(),
            "chainId": chain_id,
        }
        tx.update(self._gas_fields(bump))
        return tx

    def _load_signer_if_key(self) -> bool:
        if self._account is not None:
            return True
        key = bot_config._text("MORPHO_PRIVATE_KEY", self.chain, inherit=True)
        if not key:
            return False
        self._account = self.w3.eth.account.from_key(key)
        if self.private_tx_url:
            from web3.providers import HTTPProvider

            self._private_w3 = Web3(
                HTTPProvider(
                    self.private_tx_url,
                    request_kwargs={"timeout": self.live_rpc_timeout},
                )
            )
        return True

    def prepare_signed_live_tx(self, encoded: EncodedLiq) -> bytes | None:
        """Encode+sign live liquidateWithFlash. NEVER broadcasts. AUTO_EXECUTE is ignored.

        Requires MORPHO_PRIVATE_KEY + MORPHO_LIQ_CONTRACT. Scanner/try_liq never call this.
        """
        if not self.contract_addr:
            self.log.info("prepare_signed: MORPHO_LIQ_CONTRACT empty — encode-only")
            return None
        if not self._load_signer_if_key():
            self.log.info("prepare_signed: no MORPHO_PRIVATE_KEY — unsigned encode ready")
            return None
        tx = self._unsigned_live_tx(encoded, bump=0)
        if tx is None:
            return None
        signed = self._account.sign_transaction(tx)
        raw = signed.raw_transaction
        self.log.info(
            "LIVE PATH signed nonce=%s bytes=%d NOT sent (AUTO_EXECUTE=%s paused expected)",
            tx.get("nonce"),
            len(raw),
            self.auto_execute,
        )
        return bytes(raw)

    def note_foreign_liq(self, user: str, market_id: str) -> None:
        self.metrics.lost_to_foreign += 1
        self.mark_position_dead(user, market_id, reason="foreign_liq")

    def _notify(self, text: str, *, dedup_key: str | None, cooldown: float) -> None:
        if self.alert is None:
            return

        def _run() -> None:
            try:
                self.alert(text, dedup_key=dedup_key, cooldown=cooldown)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("alert skipped: %s", exc)

        threading.Thread(target=_run, name="morpho-tg", daemon=True).start()
