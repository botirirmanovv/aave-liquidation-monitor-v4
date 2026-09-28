"""Configuration loading.

Every setting can be given per chain by prefixing it with the chain name, e.g.
ARBITRUM_WS_RPC_URL. Unprefixed names act as the fallback, so a single-chain
.env keeps working unchanged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3

# .env is the source of truth for this process. Without override=True, a stale
# BASE_HTTP_RPC_URL (etc.) left in the parent shell silently wins over .env.
load_dotenv(override=True)

# Canonical CREATE2 deployment, identical on 250+ chains. Verified against
# etherscan; the value inherited from the original monitor was a corrupted
# variant with no code on any network, which silently disabled batching.
DEFAULT_MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"


class ConfigError(RuntimeError):
    pass


def _raw(name: str, chain: str | None, inherit: bool = True) -> str | None:
    """Chain-prefixed value, optionally falling back to the unprefixed one.

    Tuning knobs (thresholds, timeouts) may safely inherit a global default.
    Anything identifying a deployment — addresses, keys, endpoints, state files —
    must not: inheriting those silently points one chain at another chain's
    contracts. Callers pass inherit=False for those.
    """
    if chain:
        scoped = os.getenv(f"{chain.upper()}_{name}")
        if scoped is not None and scoped != "":
            return scoped
        if not inherit:
            return None
    value = os.getenv(name)
    return value if value not in (None, "") else None


def _text(
    name: str,
    chain: str | None,
    default: str | None = None,
    *,
    required: bool = False,
    inherit: bool = True,
) -> str:
    value = _raw(name, chain, inherit) or default
    if required and not value:
        scope = f"{chain.upper()}_" if chain else ""
        raise ConfigError(f"{scope}{name} is required but not set")
    return value or ""


def _flag(name: str, chain: str | None, default: bool) -> bool:
    value = _raw(name, chain)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _integer(name: str, chain: str | None, default: int) -> int:
    value = _raw(name, chain)
    return int(value) if value is not None else default


def _decimal(name: str, chain: str | None, default: str) -> Decimal:
    return Decimal(_raw(name, chain) or default)


def _address(name: str, chain: str | None, default: str | None = None, *, required: bool = False) -> str:
    """Addresses are always chain-scoped — see _raw for why they never inherit."""
    value = _text(name, chain, default, required=required, inherit=False)
    return Web3.to_checksum_address(value) if value else ""


def _address_list(name: str, chain: str | None) -> list[str]:
    raw = _text(name, chain, inherit=False)
    return [Web3.to_checksum_address(part.strip()) for part in raw.split(",") if part.strip()]


def _symbol_map(name: str, chain: str | None) -> dict[str, str]:
    """Parses "SYMBOL:0xaddr,SYMBOL:0xaddr" into {symbol: checksummed address}."""
    result: dict[str, str] = {}
    for entry in _text(name, chain, inherit=False).split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        symbol, address = entry.split(":", 1)
        result[symbol.strip().upper()] = Web3.to_checksum_address(address.strip())
    return result


@dataclass(slots=True)
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass(slots=True)
class ChainConfig:
    name: str
    ws_rpc_url: str
    http_rpc_url: str

    pool: str
    data_provider: str
    oracle: str
    multicall3: str

    liquidation_bot: str = ""
    private_key: str = ""
    auto_execute: bool = False
    private_tx_rpc_url: str = ""

    routers: list[str] = field(default_factory=list)

    health_factor_threshold: Decimal = Decimal("1.0")
    close_factor_hf_threshold: Decimal = Decimal("0.95")
    min_base_max_close_factor_threshold: int = 200_000_000_000
    slippage_tolerance: Decimal = Decimal("0.02")
    min_profit_token_units: int = 0
    min_debt_base_threshold: int = 0

    public_tx_timeout_seconds: int = 120
    private_tx_timeout_seconds: int = 30
    pending_check_interval_seconds: int = 3

    price_feed_aggregators: dict[str, str] = field(default_factory=dict)
    discover_price_feeds: bool = True
    known_svr_proxies: set[str] = field(default_factory=set)
    skip_svr_reserves: bool = True

    # WebSocket resilience
    ws_backoff_initial_seconds: float = 1.0
    ws_backoff_max_seconds: float = 60.0
    ws_silence_timeout_seconds: int = 120
    ws_health_interval_seconds: int = 30
    ws_connect_timeout_seconds: float = 20.0
    ws_ping_interval_seconds: float = 20.0
    ws_ping_timeout_seconds: float = 20.0
    ws_request_timeout_seconds: float = 30.0
    # Subscriptions are set up before the message pump runs, so a slow one delays
    # every event. Bounded tighter than a normal request on purpose.
    ws_subscribe_timeout_seconds: float = 15.0
    heartbeat_interval_seconds: int = 60
    # Full-book HF sweep (catches interest-rate drift without an oracle tick).
    # 0 disables. Default 5 minutes.
    book_scan_interval_seconds: int = 300

    # Execution safety
    simulate_before_send: bool = True
    max_gas_price_gwei: Decimal = Decimal("0")  # 0 disables the ceiling
    gas_limit_buffer: Decimal = Decimal("1.25")
    swap_deadline_seconds: int = 120

    # Stage 3 — flash arbitrage (two UniswapV2 routers + Aave flash loan)
    flash_arb_enabled: bool = False
    flash_arb_bot: str = ""
    flash_arb_token_refs: list[str] = field(default_factory=list)
    flash_arb_amount_refs: dict[str, int] = field(default_factory=dict)
    flash_arb_min_profit_bps: int = 10
    flash_arb_min_profit_token_units: int = 0
    flash_arb_scan_interval_seconds: int = 30
    # Default borrow size when no per-token amount is set: $1000 in Aave base (8 dp).
    flash_arb_default_notional_base: int = 1000_00000000

    # Stage 3b — Balancer 0% flash + Uniswap V3 fee-tier arb
    balancer_arb_enabled: bool = False
    balancer_arb_bot: str = ""
    balancer_arb_token_refs: list[str] = field(default_factory=list)
    balancer_arb_amount_refs: dict[str, int] = field(default_factory=dict)
    balancer_arb_min_profit_bps: int = 5
    balancer_arb_min_profit_token_units: int = 0
    balancer_arb_scan_interval_seconds: int = 20
    balancer_arb_default_notional_base: int = 1000_00000000

    state_file: Path = Path("monitor_state_v4.json")

    @property
    def has_execution_credentials(self) -> bool:
        return bool(self.liquidation_bot and self.private_key)

    @property
    def has_flash_arb_credentials(self) -> bool:
        return bool(self.flash_arb_bot and self.private_key)

    @property
    def has_balancer_arb_credentials(self) -> bool:
        return bool(self.balancer_arb_bot and self.private_key)


def _csv_list(name: str, chain: str | None) -> list[str]:
    raw = _text(name, chain, inherit=False)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _int_map(name: str, chain: str | None) -> dict[str, int]:
    """Parses "KEY:123,KEY:456" into {key: int}. Keys stay uppercased symbols or addresses."""
    result: dict[str, int] = {}
    for entry in _text(name, chain, inherit=False).split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        key, value = entry.split(":", 1)
        key = key.strip()
        if key.startswith("0x"):
            key = Web3.to_checksum_address(key)
        else:
            key = key.upper()
        result[key] = int(value.strip(), 0)
    return result


def load_chain_config(chain: str | None = None) -> ChainConfig:
    name = (chain or _text("CHAIN_NAME", None, "default")).lower()

    state_default = f"monitor_state_{name}.json" if chain else "monitor_state_v4.json"

    return ChainConfig(
        name=name,
        ws_rpc_url=_text("WS_RPC_URL", chain, required=True, inherit=False),
        http_rpc_url=_text("HTTP_RPC_URL", chain, required=True, inherit=False),
        pool=_address("POOL_ADDRESS", chain, required=True),
        data_provider=_address("DATA_PROVIDER_ADDRESS", chain, required=True),
        oracle=_address("ORACLE_ADDRESS", chain, required=True),
        multicall3=_address("MULTICALL3_ADDRESS", chain, DEFAULT_MULTICALL3),
        liquidation_bot=_address("LIQUIDATION_BOT_ADDRESS", chain),
        private_key=_text("PRIVATE_KEY", chain, inherit=False),
        auto_execute=_flag("AUTO_EXECUTE", chain, False),
        private_tx_rpc_url=_text("PRIVATE_TX_RPC_URL", chain, inherit=False),
        routers=_address_list("ROUTER_ADDRESSES", chain) or _address_list("ROUTER_ADDRESS", chain),
        health_factor_threshold=_decimal("HEALTH_FACTOR_THRESHOLD", chain, "1.0"),
        close_factor_hf_threshold=_decimal("CLOSE_FACTOR_HF_THRESHOLD", chain, "0.95"),
        min_base_max_close_factor_threshold=_integer(
            "MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD", chain, 200_000_000_000
        ),
        slippage_tolerance=_decimal("SLIPPAGE_TOLERANCE", chain, "0.02"),
        min_profit_token_units=_integer("MIN_PROFIT_TOKEN_UNITS", chain, 0),
        min_debt_base_threshold=_integer("MIN_DEBT_BASE_THRESHOLD", chain, 0),
        public_tx_timeout_seconds=_integer("PUBLIC_TX_TIMEOUT_SECONDS", chain, 120),
        private_tx_timeout_seconds=_integer("PRIVATE_TX_TIMEOUT_SECONDS", chain, 30),
        pending_check_interval_seconds=_integer("PENDING_CHECK_INTERVAL_SECONDS", chain, 3),
        price_feed_aggregators=_symbol_map("PRICE_FEED_AGGREGATORS", chain),
        discover_price_feeds=_flag("DISCOVER_PRICE_FEEDS", chain, True),
        known_svr_proxies=set(_address_list("KNOWN_SVR_PROXY_ADDRESSES", chain)),
        skip_svr_reserves=_flag("SKIP_SVR_RESERVES", chain, True),
        ws_backoff_initial_seconds=float(_decimal("WS_BACKOFF_INITIAL_SECONDS", chain, "1.0")),
        ws_backoff_max_seconds=float(_decimal("WS_BACKOFF_MAX_SECONDS", chain, "60")),
        ws_silence_timeout_seconds=_integer("WS_SILENCE_TIMEOUT_SECONDS", chain, 120),
        ws_health_interval_seconds=_integer("WS_HEALTH_INTERVAL_SECONDS", chain, 30),
        ws_connect_timeout_seconds=float(_decimal("WS_CONNECT_TIMEOUT_SECONDS", chain, "20")),
        ws_ping_interval_seconds=float(_decimal("WS_PING_INTERVAL_SECONDS", chain, "20")),
        ws_ping_timeout_seconds=float(_decimal("WS_PING_TIMEOUT_SECONDS", chain, "20")),
        ws_request_timeout_seconds=float(_decimal("WS_REQUEST_TIMEOUT_SECONDS", chain, "30")),
        ws_subscribe_timeout_seconds=float(_decimal("WS_SUBSCRIBE_TIMEOUT_SECONDS", chain, "15")),
        heartbeat_interval_seconds=_integer("HEARTBEAT_INTERVAL_SECONDS", chain, 60),
        book_scan_interval_seconds=_integer("BOOK_SCAN_INTERVAL_SECONDS", chain, 300),
        simulate_before_send=_flag("SIMULATE_BEFORE_SEND", chain, True),
        max_gas_price_gwei=_decimal("MAX_GAS_PRICE_GWEI", chain, "0"),
        gas_limit_buffer=_decimal("GAS_LIMIT_BUFFER", chain, "1.25"),
        swap_deadline_seconds=_integer("SWAP_DEADLINE_SECONDS", chain, 120),
        flash_arb_enabled=_flag("FLASH_ARB_ENABLED", chain, False),
        flash_arb_bot=_address("FLASH_ARB_BOT_ADDRESS", chain),
        flash_arb_token_refs=_csv_list("FLASH_ARB_TOKENS", chain),
        flash_arb_amount_refs=_int_map("FLASH_ARB_AMOUNTS", chain),
        flash_arb_min_profit_bps=_integer("FLASH_ARB_MIN_PROFIT_BPS", chain, 10),
        flash_arb_min_profit_token_units=_integer("FLASH_ARB_MIN_PROFIT_TOKEN_UNITS", chain, 0),
        flash_arb_scan_interval_seconds=_integer("FLASH_ARB_SCAN_INTERVAL_SECONDS", chain, 30),
        flash_arb_default_notional_base=_integer(
            "FLASH_ARB_DEFAULT_NOTIONAL_BASE", chain, 1000_00000000
        ),
        balancer_arb_enabled=_flag("BALANCER_ARB_ENABLED", chain, False),
        balancer_arb_bot=_address("BALANCER_ARB_BOT_ADDRESS", chain),
        balancer_arb_token_refs=_csv_list("BALANCER_ARB_TOKENS", chain),
        balancer_arb_amount_refs=_int_map("BALANCER_ARB_AMOUNTS", chain),
        balancer_arb_min_profit_bps=_integer("BALANCER_ARB_MIN_PROFIT_BPS", chain, 5),
        balancer_arb_min_profit_token_units=_integer(
            "BALANCER_ARB_MIN_PROFIT_TOKEN_UNITS", chain, 0
        ),
        balancer_arb_scan_interval_seconds=_integer(
            "BALANCER_ARB_SCAN_INTERVAL_SECONDS", chain, 20
        ),
        balancer_arb_default_notional_base=_integer(
            "BALANCER_ARB_DEFAULT_NOTIONAL_BASE", chain, 1000_00000000
        ),
        state_file=Path(_text("STATE_FILE", chain, state_default, inherit=False)),
    )


def load_telegram_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token=_text("TELEGRAM_BOT_TOKEN", None),
        chat_id=_text("TELEGRAM_CHAT_ID", None),
    )


def load_morpho_telegram_config() -> TelegramConfig:
    """Morpho alerts: dedicated bot/chat when set, else the shared Telegram pair.

    Token: MORPHO_TELEGRAM_BOT_TOKEN, then TELEGRAM_BOT_TOKEN.
    Chat: MORPHO_TELEGRAM_CHAT_ID, then TELEGRAM_CHAT_ID.
    Does not enable Morpho live or Aave.
    """
    return TelegramConfig(
        bot_token=_text("MORPHO_TELEGRAM_BOT_TOKEN", None) or _text("TELEGRAM_BOT_TOKEN", None),
        chat_id=_text("MORPHO_TELEGRAM_CHAT_ID", None) or _text("TELEGRAM_CHAT_ID", None),
    )


def configured_chains() -> list[str]:
    """Chain names from CHAINS=arbitrum,base. Empty means single-chain mode."""
    return [part.strip().lower() for part in _text("CHAINS", None).split(",") if part.strip()]


def log_level() -> str:
    return _text("LOG_LEVEL", None, "INFO").upper()


def log_file() -> str:
    """Optional log file, written by the app rather than by shell redirection."""
    return _text("LOG_FILE", None)
