"""Morpho Blue health-factor math + Multicall3 position/market reads.

Independent from Aave HF logic. Formula (Morpho Blue):

    maxBorrow = collateral * oraclePrice / 1e36 * lltv / 1e18
    HF       = maxBorrow / borrowedAssets   (inf if borrowed == 0)

oraclePrice = IOracle.price() = collateral quoted in loan token, 1e36 scale.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from collections.abc import Mapping
from typing import Any

from eth_abi import decode as abi_decode
from web3 import Web3

from aave_bot.abis import MULTICALL3_ABI
from aave_bot.config import DEFAULT_MULTICALL3 as AAVE_DEFAULT_MULTICALL3

from morpho_markets import (
    DEFAULT_MULTICALL3,
    LIQUIDATION_CURSOR,
    MAX_LIQUIDATION_INCENTIVE_FACTOR,
    MORPHO_BLUE,
    ORACLE_PRICE_SCALE,
    WAD,
    MorphoMarketConfig,
)

# Prefer shared project constant when present.
if AAVE_DEFAULT_MULTICALL3:
    DEFAULT_MULTICALL3 = AAVE_DEFAULT_MULTICALL3  # noqa: F811

LOG = logging.getLogger("morpho_hf")

STABLE_LOAN_SYMBOLS = {
    "USDC",
    "USDT",
    "USDT0",
    "USDBC",
    "DAI",
    "USDE",
}


def loan_token_price_usd(loan_symbol: str, *, eth_usd: float) -> float:
    """USD per 1 loan token. USDC-style = 1. WETH uses Chainlink ETH."""
    sym = (loan_symbol or "").upper().replace(".", "")
    if sym in STABLE_LOAN_SYMBOLS or sym.endswith("USD"):
        return 1.0
    if sym in {"WETH", "ETH"}:
        return float(eth_usd) if eth_usd > 0 else 3500.0
    return 1.0

MORPHO_ABI = [
    {
        "inputs": [{"internalType": "bytes32", "name": "id", "type": "bytes32"}],
        "name": "market",
        "outputs": [
            {"internalType": "uint128", "name": "totalSupplyAssets", "type": "uint128"},
            {"internalType": "uint128", "name": "totalSupplyShares", "type": "uint128"},
            {"internalType": "uint128", "name": "totalBorrowAssets", "type": "uint128"},
            {"internalType": "uint128", "name": "totalBorrowShares", "type": "uint128"},
            {"internalType": "uint128", "name": "lastUpdate", "type": "uint128"},
            {"internalType": "uint128", "name": "fee", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "id", "type": "bytes32"},
            {"internalType": "address", "name": "user", "type": "address"},
        ],
        "name": "position",
        "outputs": [
            {"internalType": "uint256", "name": "supplyShares", "type": "uint256"},
            {"internalType": "uint128", "name": "borrowShares", "type": "uint128"},
            {"internalType": "uint128", "name": "collateral", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "bytes32", "name": "id", "type": "bytes32"}],
        "name": "idToMarketParams",
        "outputs": [
            {"internalType": "address", "name": "loanToken", "type": "address"},
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "address", "name": "oracle", "type": "address"},
            {"internalType": "address", "name": "irm", "type": "address"},
            {"internalType": "uint256", "name": "lltv", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

ORACLE_ABI = [
    {
        "inputs": [],
        "name": "price",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# Morpho ChainlinkOracle / ChainlinkOracleV2 feed getters.
CHAINLINK_ORACLE_FEEDS_ABI = [
    {
        "inputs": [],
        "name": name,
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
    for name in ("BASE_FEED_1", "BASE_FEED_2", "QUOTE_FEED_1", "QUOTE_FEED_2")
]

CHAINLINK_PROXY_AGG_ABI = [
    {
        "inputs": [],
        "name": "aggregator",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

CHAINLINK_AGGREGATOR_V3_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

# Fallback when live discovery 408s (Base Morpho oracles → Chainlink aggregators).
KNOWN_ORACLE_AGGREGATORS: dict[str, list[str]] = {
    # USDC/cbXRP Morpho oracle → XRP/USD aggregator
    "0x031b2efc8d70042ac8d9f5c793c4149ec4b60fde": [
        "0x92A7c3a57E17AfF701c159c5480073B095100b62",
    ],
    # USDC/WETH Morpho oracle → ETH/USD + USDC/USD aggregators
    "0xfea2d58cefcb9fcb597723c6bae66ffe4193afe4": [
        "0x1e0B2C3896338FBb201C4f0A27C6904801Dca06B",
        "0xE2F30AF46DddDdb7795995d615C828F722E2e6F0",
    ],
}


def _feed_addr_to_agg(w3: Web3, feed_addr: str) -> str:
    """Resolve a Chainlink feed/proxy address to the emitting aggregator (lower)."""
    if not feed_addr or int(feed_addr, 16) == 0:
        return ""
    feed = Web3.to_checksum_address(feed_addr)
    try:
        proxy = w3.eth.contract(address=feed, abi=CHAINLINK_PROXY_AGG_ABI)
        agg = Web3.to_checksum_address(proxy.functions.aggregator().call())
        if int(agg, 16):
            return agg.lower()
    except Exception:  # noqa: BLE001
        pass
    return feed.lower()


def parse_answer_updated_current(entry: Mapping[str, Any]) -> int | None:
    """Decode AnswerUpdated(int256 indexed current, ...) from a log entry."""
    topics = entry.get("topics") or []
    if len(topics) < 2:
        return None
    try:
        current = abi_decode(["int256"], bytes(topics[1]))[0]
        ans = int(current)
        return ans if ans > 0 else None
    except Exception:  # noqa: BLE001
        return None


def resolve_morpho_oracle_feed_roles(
    w3: Web3, market: MorphoMarketConfig
) -> tuple[list[str], list[str]]:
    """Base vs quote Chainlink aggregators for Morpho price projection."""
    oracle = Web3.to_checksum_address(market.oracle)
    base: list[str] = []
    quote: list[str] = []
    try:
        c = w3.eth.contract(address=oracle, abi=CHAINLINK_ORACLE_FEEDS_ABI)
        for name in ("BASE_FEED_1", "BASE_FEED_2"):
            try:
                addr = getattr(c.functions, name)().call()
                agg = _feed_addr_to_agg(w3, addr)
                if agg:
                    base.append(agg)
            except Exception:  # noqa: BLE001
                continue
        for name in ("QUOTE_FEED_1", "QUOTE_FEED_2"):
            try:
                addr = getattr(c.functions, name)().call()
                agg = _feed_addr_to_agg(w3, addr)
                if agg:
                    quote.append(agg)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    if not base and not quote:
        for raw in KNOWN_ORACLE_AGGREGATORS.get(oracle.lower(), []):
            base.append(Web3.to_checksum_address(raw).lower())
    return base, quote


def project_morpho_price(
    old_morpho: int,
    old_answers: dict[str, int],
    new_answers: dict[str, int],
    base_aggs: list[str],
    quote_aggs: list[str],
) -> int | None:
    """Project Morpho IOracle.price() after Chainlink feed moves.

    Morpho oracle price is proportional to product(base feeds) / product(quote feeds).
    """
    if old_morpho <= 0:
        return None

    def leg(answers: dict[str, int], aggs: list[str]) -> int | None:
        if not aggs:
            return 1
        prod = 1
        for a in aggs:
            v = answers.get(a.lower())
            if v is None or v <= 0:
                return None
            prod *= v
        return prod

    old_b = leg(old_answers, base_aggs)
    old_q = leg(old_answers, quote_aggs)
    new_b = leg(new_answers, base_aggs)
    new_q = leg(new_answers, quote_aggs)
    if None in (old_b, old_q, new_b, new_q) or old_b == 0 or old_q == 0 or new_q == 0:
        return None
    num = old_morpho * new_b * old_q
    den = old_b * new_q
    if den == 0:
        return None
    return num // den


def read_chainlink_answer(w3: Web3, aggregator: str) -> int | None:
    """latestRoundData().answer for a Chainlink aggregator."""
    try:
        c = w3.eth.contract(
            address=Web3.to_checksum_address(aggregator),
            abi=CHAINLINK_AGGREGATOR_V3_ABI,
        )
        ans = int(c.functions.latestRoundData().call()[1])
        return ans if ans > 0 else None
    except Exception:  # noqa: BLE001
        return None


def resolve_morpho_oracle_aggregators(
    w3: Web3, market: MorphoMarketConfig
) -> list[str]:
    """Chainlink aggregators that drive a Morpho IOracle (AnswerUpdated source)."""
    oracle = Web3.to_checksum_address(market.oracle)
    feeds: list[str] = []
    try:
        c = w3.eth.contract(address=oracle, abi=CHAINLINK_ORACLE_FEEDS_ABI)
        for name in ("BASE_FEED_1", "BASE_FEED_2", "QUOTE_FEED_1", "QUOTE_FEED_2"):
            try:
                addr = getattr(c.functions, name)().call()
                if int(addr, 16):
                    feeds.append(Web3.to_checksum_address(addr))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        feeds = []

    aggs: list[str] = []
    for feed in feeds:
        try:
            proxy = w3.eth.contract(address=feed, abi=CHAINLINK_PROXY_AGG_ABI)
            agg = Web3.to_checksum_address(proxy.functions.aggregator().call())
            if int(agg, 16):
                aggs.append(agg)
                continue
        except Exception:  # noqa: BLE001
            pass
        aggs.append(feed)

    if not aggs:
        for raw in KNOWN_ORACLE_AGGREGATORS.get(oracle.lower(), []):
            aggs.append(Web3.to_checksum_address(raw))

    # Stable unique order.
    return list(dict.fromkeys(aggs))

ERC20_DECIMALS_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    }
]


@dataclass(slots=True)
class MorphoPositionState:
    user: str
    market_id: str
    supply_shares: int
    borrow_shares: int
    collateral: int
    total_borrow_assets: int
    total_borrow_shares: int
    borrowed_assets: int
    oracle_price: int
    lltv_wad: int
    health_factor: Decimal
    debt_usd: float
    collateral_usd_loan_terms: float


@dataclass(slots=True)
class MorphoCandidate:
    """Candidate shape aligned with future shared pipeline (+ Aave dry-run)."""

    chain: str
    protocol: str
    user: str
    market_id: str
    health_factor: Decimal
    debt_to_cover: int
    collateral_asset: str
    borrow_asset: str
    collateral_amount: int
    estimated_profit_usd: float
    debt_usd: float
    loan_symbol: str
    collateral_symbol: str
    lltv: float
    net_profit_usd: float = 0.0
    gas_cost_usd: float = 0.0
    slippage_usd: float = 0.0
    flash_fee_usd: float = 0.0


def shares_to_assets_up(shares: int, total_assets: int, total_shares: int) -> int:
    """Morpho SharesMathLib.toAssetsUp (borrower owes slightly more)."""
    if shares == 0:
        return 0
    if total_shares == 0:
        return shares
    return (shares * total_assets + (total_shares - 1)) // total_shares


def shares_to_assets_down(shares: int, total_assets: int, total_shares: int) -> int:
    """Morpho SharesMathLib.toAssetsDown (no virtual shares; matches HF path)."""
    if shares == 0 or total_shares == 0:
        return 0
    return (shares * total_assets) // total_shares


def hf_from_cached_shares(
    *,
    collateral: int,
    borrow_shares: int,
    total_borrow_assets: int,
    total_borrow_shares: int,
    oracle_price: int,
    lltv_wad: int,
) -> tuple[Decimal, int]:
    """HF + borrowed assets from last shares + a new oracle tick (no RPC)."""
    borrowed = shares_to_assets_up(
        int(borrow_shares), int(total_borrow_assets), int(total_borrow_shares)
    )
    hf = compute_health_factor(
        int(collateral), borrowed, int(oracle_price), int(lltv_wad)
    )
    return hf, borrowed


def seized_assets_for_repay(
    repaid_assets: int,
    oracle_price: int,
    lltv_wad: int,
    *,
    collateral_cap: int,
    haircut_bps: int = 10,
) -> int:
    """Morpho repaidShares-path seizedAssets (down), with a tiny rounding haircut.

    seized = repaidAssets.wMulDown(LIF).mulDivDown(ORACLE_PRICE_SCALE, price)
    Passing this as `seizedAssets` keeps swap amountIn identical to what we receive.
    """
    if repaid_assets <= 0 or oracle_price <= 0:
        return 0
    lif = liquidation_incentive_factor(lltv_wad)
    seized = (repaid_assets * lif // WAD) * ORACLE_PRICE_SCALE // oracle_price
    if haircut_bps > 0:
        seized = seized * (10_000 - haircut_bps) // 10_000
    if collateral_cap > 0:
        seized = min(seized, collateral_cap)
    return seized


def max_borrow_assets(collateral: int, oracle_price: int, lltv_wad: int) -> int:
    if collateral == 0 or oracle_price == 0 or lltv_wad == 0:
        return 0
    return (collateral * oracle_price // ORACLE_PRICE_SCALE) * lltv_wad // WAD


def compute_health_factor(
    collateral: int,
    borrowed_assets: int,
    oracle_price: int,
    lltv_wad: int,
) -> Decimal:
    """HF = (collateral * oraclePrice * lltv) / borrowed  (with Morpho scales)."""
    if borrowed_assets <= 0:
        return Decimal("Infinity")
    mb = max_borrow_assets(collateral, oracle_price, lltv_wad)
    return Decimal(mb) / Decimal(borrowed_assets)


def liquidation_incentive_factor(lltv_wad: int) -> int:
    """WAD-scaled LIF = min(1.15e18, 1e18 / (1e18 - 0.3e18 * (1e18 - lltv)))."""
    one_minus = WAD - (LIQUIDATION_CURSOR * (WAD - lltv_wad) // WAD)
    if one_minus <= 0:
        return MAX_LIQUIDATION_INCENTIVE_FACTOR
    lif = (WAD * WAD) // one_minus
    return min(MAX_LIQUIDATION_INCENTIVE_FACTOR, lif)


def estimate_liquidation_profit_usd(
    debt_usd: float,
    lltv_wad: int,
) -> float:
    """Optimistic inventory edge ≈ debt_usd * (LIF/WAD - 1), before gas/swap."""
    if debt_usd <= 0:
        return 0.0
    lif = liquidation_incentive_factor(lltv_wad)
    return max(0.0, debt_usd * (lif / WAD - 1.0))


# Morpho Blue `liquidate` callback: collateral is seized in-tx; the liquidator
# repays debt without Morpho `flashLoan`. Fee is 0 (unlike Aave 5 bps).
# We still subtract 0 so the diagnostic net formula is explicit.
MORPHO_LIQUIDATE_FLASH_FEE_USD = 0.0


@dataclass(frozen=True, slots=True)
class NetProfitBreakdown:
    """net = after_swap(bonus) − gas − flash_fee(0) − already in after_swap slip."""

    bonus_usd: float
    seized_usd: float
    after_swap_usd: float
    slippage_usd: float
    gas_cost_usd: float
    flash_fee_usd: float
    net_profit_usd: float


def estimate_priority_gas_usd(
    *,
    gas_limit: int,
    priority_gwei: float,
    eth_usd: float,
) -> float:
    """USD ≈ gasLimit * priorityFee (gwei). Base fee not included (env-fixed)."""
    if gas_limit <= 0 or priority_gwei <= 0 or eth_usd <= 0:
        return 0.0
    gas_eth = float(gas_limit) * float(priority_gwei) * 1e-9
    return gas_eth * float(eth_usd)


def estimate_net_profit_usd(
    debt_usd: float,
    lltv_wad: int,
    *,
    gas_cost_usd: float,
    slippage_bps: int,
    flash_fee_usd: float = MORPHO_LIQUIDATE_FLASH_FEE_USD,
) -> NetProfitBreakdown:
    """Bonus after swap haircut, minus gas and Morpho liquidate flash fee (0).

    seized_usd ≈ debt * LIF. Slippage buffer is applied to seized notional
    (swap estimate). Flash fee is documented as 0 and still subtracted.
    """
    bonus = estimate_liquidation_profit_usd(debt_usd, lltv_wad)
    seized = max(0.0, debt_usd) + bonus
    slip = max(0.0, seized * max(0, int(slippage_bps)) / 10_000.0)
    after = max(0.0, seized - slip)
    fee = float(flash_fee_usd)
    gas = max(0.0, float(gas_cost_usd))
    net = after - max(0.0, debt_usd) - fee - gas
    return NetProfitBreakdown(
        bonus_usd=bonus,
        seized_usd=seized,
        after_swap_usd=after,
        slippage_usd=slip,
        gas_cost_usd=gas,
        flash_fee_usd=fee,
        net_profit_usd=net,
    )


def _market_id_bytes(market_id: str) -> bytes:
    hex_id = market_id[2:] if market_id.startswith("0x") else market_id
    return bytes.fromhex(hex_id)


class MorphoReader:
    """HTTP reader: market + position + oracle via Multicall3 when possible."""

    def __init__(
        self,
        w3: Web3,
        *,
        morpho: str = MORPHO_BLUE,
        multicall3: str = DEFAULT_MULTICALL3,
    ) -> None:
        self.w3 = w3
        self.morpho_addr = Web3.to_checksum_address(morpho)
        self.multicall_addr = Web3.to_checksum_address(multicall3)
        self.morpho = w3.eth.contract(address=self.morpho_addr, abi=MORPHO_ABI)
        self.multicall = w3.eth.contract(address=self.multicall_addr, abi=MULTICALL3_ABI)
        self._decimals_cache: dict[str, int] = {}
        # market_id -> (mono_ts, total_borrow_assets, total_borrow_shares, oracle_price)
        self._snap_cache: dict[str, tuple[float, int, int, int]] = {}
        self._snap_ttl = 2.0
        # market_id -> mono_ts until which we skip retries after hard failures
        self._snap_fail_until: dict[str, float] = {}
        self._snap_fail_cooldown = 30.0

    def token_decimals(self, token: str) -> int:
        token = Web3.to_checksum_address(token)
        if token in self._decimals_cache:
            return self._decimals_cache[token]
        c = self.w3.eth.contract(address=token, abi=ERC20_DECIMALS_ABI)
        dec = int(c.functions.decimals().call())
        self._decimals_cache[token] = dec
        return dec

    def _try_aggregate(self, calls: list[dict[str, Any]]) -> list[tuple[bool, bytes]]:
        try:
            return self.multicall.functions.tryAggregate(False, calls).call()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("multicall failed (%s); falling back to singles", exc)
            out: list[tuple[bool, bytes]] = []
            for call in calls:
                try:
                    raw = self.w3.eth.call({"to": call["target"], "data": call["callData"]})
                    out.append((True, raw))
                except Exception:
                    out.append((False, b""))
            return out

    def read_oracle_price(
        self, market: MorphoMarketConfig, *, pending: bool = False
    ) -> int | None:
        """Read Morpho IOracle.price(). pending=True sees mempool/state tip when RPC allows."""
        try:
            oracle = self.w3.eth.contract(
                address=Web3.to_checksum_address(market.oracle), abi=ORACLE_ABI
            )
            if pending:
                return int(oracle.functions.price().call(block_identifier="pending"))
            return int(oracle.functions.price().call())
        except Exception as exc:  # noqa: BLE001
            if pending:
                # Some free RPCs reject pending — fall back to latest once.
                try:
                    oracle = self.w3.eth.contract(
                        address=Web3.to_checksum_address(market.oracle), abi=ORACLE_ABI
                    )
                    return int(oracle.functions.price().call())
                except Exception as exc2:  # noqa: BLE001
                    LOG.warning(
                        "oracle price failed %s: %s", market.market_id[:12], exc2
                    )
                    return None
            LOG.warning("oracle price failed %s: %s", market.market_id[:12], exc)
            return None

    def _market_snapshot(
        self, market: MorphoMarketConfig
    ) -> tuple[int, int, int] | None:
        """Return (total_borrow_assets, total_borrow_shares, oracle_price)."""
        key = market.market_id.lower()
        now = time.monotonic()
        fail_until = self._snap_fail_until.get(key)
        if fail_until is not None and now < fail_until:
            return None
        cached = self._snap_cache.get(key)
        if cached is not None and (now - cached[0]) < self._snap_ttl:
            return cached[1], cached[2], cached[3]

        mid = _market_id_bytes(market.market_id)
        try:
            (
                _tsa,
                _tss,
                total_borrow_assets,
                total_borrow_shares,
                _lu,
                _fee,
            ) = self.morpho.functions.market(mid).call()
            oracle_price = self.read_oracle_price(market)
            if oracle_price is None:
                self._snap_fail_until[key] = now + self._snap_fail_cooldown
                return None
        except Exception as exc:  # noqa: BLE001
            self._snap_fail_until[key] = now + self._snap_fail_cooldown
            LOG.warning("market snapshot failed %s: %s", market.market_id[:12], exc)
            return None

        self._snap_fail_until.pop(key, None)
        tba, tbs = int(total_borrow_assets), int(total_borrow_shares)
        self._snap_cache[key] = (now, tba, tbs, oracle_price)
        return tba, tbs, oracle_price

    def _state_from_parts(
        self,
        market: MorphoMarketConfig,
        user: str,
        supply_shares: int,
        borrow_shares: int,
        collateral: int,
        total_borrow_assets: int,
        total_borrow_shares: int,
        oracle_price: int,
        *,
        loan_price_usd: float | None = None,
    ) -> MorphoPositionState:
        borrowed = shares_to_assets_up(
            int(borrow_shares), int(total_borrow_assets), int(total_borrow_shares)
        )
        hf = compute_health_factor(
            int(collateral), borrowed, int(oracle_price), market.lltv_wad
        )
        loan_dec = self.token_decimals(market.loan_token)
        px = 1.0 if loan_price_usd is None else float(loan_price_usd)
        debt_usd = (borrowed / (10**loan_dec)) * px
        coll_loan = (
            int(collateral) * int(oracle_price) // ORACLE_PRICE_SCALE
        ) / (10**loan_dec)
        return MorphoPositionState(
            user=user,
            market_id=market.market_id,
            supply_shares=int(supply_shares),
            borrow_shares=int(borrow_shares),
            collateral=int(collateral),
            total_borrow_assets=int(total_borrow_assets),
            total_borrow_shares=int(total_borrow_shares),
            borrowed_assets=borrowed,
            oracle_price=int(oracle_price),
            lltv_wad=market.lltv_wad,
            health_factor=hf,
            debt_usd=debt_usd,
            collateral_usd_loan_terms=coll_loan * px,
        )

    def read_position(
        self,
        market: MorphoMarketConfig,
        user: str,
        *,
        loan_price_usd: float | None = None,
    ) -> MorphoPositionState | None:
        user = Web3.to_checksum_address(user)
        mid = _market_id_bytes(market.market_id)
        snap = self._market_snapshot(market)
        if snap is None:
            return None
        total_borrow_assets, total_borrow_shares, oracle_price = snap

        try:
            supply_shares, borrow_shares, collateral = self.morpho.functions.position(
                mid, user
            ).call()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("read_position failed %s %s: %s", market.market_id[:12], user, exc)
            return None

        return self._state_from_parts(
            market,
            user,
            int(supply_shares),
            int(borrow_shares),
            int(collateral),
            total_borrow_assets,
            total_borrow_shares,
            oracle_price,
            loan_price_usd=loan_price_usd,
        )

    def read_positions_batch(
        self,
        market: MorphoMarketConfig,
        users: list[str],
        *,
        loan_price_usd: float | None = None,
        chunk: int = 40,
    ) -> list[MorphoPositionState]:
        """Batch position() via Multicall3; market+oracle fetched once per TTL."""
        if not users:
            return []
        snap = self._market_snapshot(market)
        if snap is None:
            return []
        total_borrow_assets, total_borrow_shares, oracle_price = snap
        mid = _market_id_bytes(market.market_id)
        out: list[MorphoPositionState] = []

        for i in range(0, len(users), chunk):
            batch = [Web3.to_checksum_address(u) for u in users[i : i + chunk]]
            calls = [
                {
                    "target": self.morpho_addr,
                    "callData": self.morpho.encode_abi("position", args=[mid, u]),
                }
                for u in batch
            ]
            try:
                results = self._try_aggregate(calls)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("batch position failed %s: %s", market.market_id[:12], exc)
                continue
            for user, (ok, raw) in zip(batch, results, strict=False):
                if not ok or not raw:
                    continue
                try:
                    supply_shares, borrow_shares, collateral = (
                        self.w3.codec.decode(
                            ["uint256", "uint128", "uint128"], raw
                        )
                    )
                except Exception:
                    continue
                out.append(
                    self._state_from_parts(
                        market,
                        user,
                        int(supply_shares),
                        int(borrow_shares),
                        int(collateral),
                        total_borrow_assets,
                        total_borrow_shares,
                        oracle_price,
                        loan_price_usd=loan_price_usd,
                    )
                )
        return out

    def build_candidate(
        self,
        chain: str,
        market: MorphoMarketConfig,
        state: MorphoPositionState,
        *,
        hf_threshold: Decimal = Decimal("1.0"),
        min_debt_usd: float = 100.0,
        max_debt_usd: float | None = None,
        gas_cost_usd: float = 0.0,
        slippage_bps: int = 0,
    ) -> MorphoCandidate | None:
        if state.borrowed_assets <= 0:
            return None
        if state.debt_usd < min_debt_usd:
            return None
        if max_debt_usd is not None and state.debt_usd > max_debt_usd:
            return None
        if state.health_factor >= hf_threshold:
            return None

        profit = estimate_liquidation_profit_usd(state.debt_usd, market.lltv_wad)
        net = estimate_net_profit_usd(
            state.debt_usd,
            market.lltv_wad,
            gas_cost_usd=gas_cost_usd,
            slippage_bps=slippage_bps,
        )
        return MorphoCandidate(
            chain=chain,
            protocol="morpho",
            user=state.user,
            market_id=market.market_id,
            health_factor=state.health_factor,
            debt_to_cover=state.borrowed_assets,
            collateral_asset=Web3.to_checksum_address(market.collateral_token),
            borrow_asset=Web3.to_checksum_address(market.loan_token),
            collateral_amount=state.collateral,
            estimated_profit_usd=profit,
            debt_usd=state.debt_usd,
            loan_symbol=market.loan_symbol,
            collateral_symbol=market.collateral_symbol,
            lltv=market.lltv_wad / WAD,
            net_profit_usd=net.net_profit_usd,
            gas_cost_usd=net.gas_cost_usd,
            slippage_usd=net.slippage_usd,
            flash_fee_usd=net.flash_fee_usd,
        )
