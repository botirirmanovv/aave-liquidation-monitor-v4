"""Per-chain runtime: RPC clients, contracts, caches, and liquidation decisions.

All mutable state lives on the instance rather than at module level, which is
what makes several chains able to share one process (stage 2) and what makes the
logic testable without opening a socket at import time.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from eth_abi import decode as abi_decode
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from . import abis
from .config import ChainConfig
from .decimals import DecimalsResolver
from .simulate import build_error_table, simulate_transaction
from .state import MonitorState

WAD = Decimal(10) ** 18
ACCOUNT_DATA_TYPES = ["uint256"] * 6
USER_RESERVE_TYPES = [
    "uint256", "uint256", "uint256", "uint256", "uint256",
    "uint256", "uint256", "uint40", "bool",
]
# decimals, ltv, liquidationThreshold, liquidationBonus, reserveFactor, then flags
RESERVE_CONFIG_TYPES = ["uint256"] * 5 + ["bool"] * 5


@dataclass(slots=True)
class LiquidationPlan:
    user: str
    collateral_asset: str
    collateral_amount: int
    debt_asset: str
    debt_to_cover: int
    health_factor: Decimal
    router: str = ""
    swap_path: list[str] | None = None
    amount_out_min: int = 0


class ChainContext:
    def __init__(self, config: ChainConfig) -> None:
        self.config = config
        self.log = logging.getLogger(f"aave_bot.{config.name}")

        self.w3: Web3 | None = None
        self.private_w3: Web3 | None = None
        self.account = None

        self.pool = None
        self.data_provider = None
        self.oracle = None
        self.multicall = None
        self.liquidation_bot = None
        self.routers: list[dict] = []
        # Set once at startup from whether Multicall3 has code on this chain; a
        # failing batch call does not clear it, see _try_aggregate.
        self.multicall_available = False
        self._multicall_failures = 0

        self.state = MonitorState(config.state_file)
        self.decimals_resolver: DecimalsResolver | None = None
        self.error_table = build_error_table(abis.LIQUIDATION_BOT_ERROR_SIGNATURES)

        self._reserve_symbols: dict[str, str] = {}
        self._reserves: list[str] = []
        self._bonus_cache: dict[str, int] = {}
        self._svr_checked: set[str] = set()
        self.aggregator_assets: dict[str, set[str]] = {}

        self._local_nonce: int | None = None
        self.pending_txs: list[dict] = []

    # ── setup ────────────────────────────────────────────────────────────
    def connect(self) -> None:
        cfg = self.config
        self.w3 = Web3(Web3.HTTPProvider(cfg.http_rpc_url, request_kwargs={"timeout": 20}))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if not self.w3.is_connected():
            raise ConnectionError(f"[{cfg.name}] HTTP RPC unreachable: {cfg.http_rpc_url}")
        self.log.info("HTTP RPC connected, chain id %s", self.w3.eth.chain_id)

        self.pool = self.w3.eth.contract(address=cfg.pool, abi=abis.POOL_ABI)
        self.data_provider = self.w3.eth.contract(
            address=cfg.data_provider, abi=abis.DATA_PROVIDER_ABI
        )
        self.oracle = self.w3.eth.contract(address=cfg.oracle, abi=abis.ORACLE_ABI)
        self.multicall = self.w3.eth.contract(address=cfg.multicall3, abi=abis.MULTICALL3_ABI)
        self.multicall_available = self._has_code(cfg.multicall3)
        self.log.info("Multicall3 available: %s", self.multicall_available)

        self._load_reserves()
        self.decimals_resolver = DecimalsResolver(
            token_decimals=self._token_decimals,
            reserve_decimals=self._reserve_config_decimals,
            symbols=self._reserve_symbols,
        )

        self._connect_routers()
        self._setup_execution()

        self.state.load()

    def _has_code(self, address: str) -> bool:
        try:
            code = self.w3.eth.get_code(address)
            return bool(code) and code not in (b"", b"0x")
        except Exception:
            return False

    def _load_reserves(self) -> None:
        try:
            tokens = self.data_provider.functions.getAllReservesTokens().call()
        except Exception as exc:
            self.log.error("could not read the reserve list: %s", exc)
            return
        self._reserves = [Web3.to_checksum_address(entry[1]) for entry in tokens]
        self._reserve_symbols = {
            Web3.to_checksum_address(entry[1]): entry[0] for entry in tokens
        }
        self.log.info("Aave reserves discovered: %d", len(self._reserves))

    def _connect_routers(self) -> None:
        for address in self.config.routers:
            try:
                quote = self.w3.eth.contract(address=address, abi=abis.ROUTER_QUOTE_ABI)
                entry: dict = {
                    "address": address,
                    "quote": quote,
                    "factory": None,
                    "weth": None,
                }
                try:
                    factory_address = quote.functions.factory().call()
                    weth = quote.functions.WETH().call()
                    entry["factory"] = self.w3.eth.contract(
                        address=Web3.to_checksum_address(factory_address),
                        abi=abis.FACTORY_ABI,
                    )
                    entry["weth"] = Web3.to_checksum_address(weth)
                except Exception as exc:
                    # Flash-arb only needs getAmountsOut; liquidation path-building
                    # wants factory/WETH. Keep the router either way.
                    self.log.debug(
                        "router %s missing factory/WETH (%s) — quotes still usable",
                        address, exc,
                    )
                self.routers.append(entry)
                self.log.info("router connected: %s", address)
            except Exception as exc:
                self.log.warning("router %s unusable (%s)", address, exc)

    def _setup_execution(self) -> None:
        cfg = self.config
        if not cfg.auto_execute:
            self.log.info("AUTO_EXECUTE off — observation only (dry-run)")
            return
        if not cfg.has_execution_credentials or not self.routers:
            raise ValueError(
                f"[{cfg.name}] AUTO_EXECUTE=true requires LIQUIDATION_BOT_ADDRESS, "
                "PRIVATE_KEY and at least one usable router"
            )
        if not cfg.private_tx_rpc_url:
            raise ValueError(
                f"[{cfg.name}] AUTO_EXECUTE=true requires PRIVATE_TX_RPC_URL "
                "(MEV-protected / private broadcast). Public mempool send is blocked."
            )
        self.liquidation_bot = self.w3.eth.contract(
            address=cfg.liquidation_bot, abi=abis.LIQUIDATION_BOT_ABI
        )
        self.account = self.w3.eth.account.from_key(cfg.private_key)
        self.log.info("AUTO_EXECUTE on, sender %s", self.account.address)

        self.private_w3 = Web3(Web3.HTTPProvider(cfg.private_tx_rpc_url))
        self.log.info("private relay required+enabled: %s", cfg.private_tx_rpc_url)

        self._verify_bot_permissions()

    def _verify_bot_permissions(self) -> None:
        """Catch misconfiguration now rather than on the first real opportunity."""
        try:
            operator = self.liquidation_bot.functions.operator().call()
            owner = self.liquidation_bot.functions.owner().call()
            paused = self.liquidation_bot.functions.paused().call()
        except Exception as exc:
            self.log.warning("could not read bot roles (%s) — check the address/ABI", exc)
            return

        sender = self.account.address
        if sender not in (operator, owner):
            self.log.error(
                "sender %s is neither operator (%s) nor owner (%s): "
                "initiateLiquidation will revert with OnlyOperator()",
                sender, operator, owner,
            )
        if paused:
            self.log.error("liquidation bot is paused — every call will revert")

        for router in self.routers:
            try:
                if not self.liquidation_bot.functions.allowedRouters(router["address"]).call():
                    self.log.warning(
                        "router %s is not allowlisted on the bot — swaps through it revert",
                        router["address"],
                    )
            except Exception:
                pass

        # initiateLiquidation reverts with ExceedsLimit() when the per-token cap
        # is unset, so an unconfigured cap makes that debt asset silently
        # untradeable. Better to know at startup than at the one moment a
        # position becomes liquidatable.
        uncapped = []
        for asset in self.all_reserves():
            try:
                if self.liquidation_bot.functions.maxDebtCoverPerToken(asset).call() == 0:
                    uncapped.append(self.symbol_of(asset))
            except Exception:
                break
        if uncapped:
            self.log.warning(
                "no maxDebtCoverPerToken set for %s — liquidations of those debts "
                "will revert with ExceedsLimit()", ", ".join(uncapped),
            )

    # ── token metadata ───────────────────────────────────────────────────
    def _token_decimals(self, asset: str) -> int | None:
        token = self.w3.eth.contract(address=asset, abi=abis.ERC20_ABI)
        return int(token.functions.decimals().call())

    def _reserve_config_decimals(self, asset: str) -> int | None:
        data = self.data_provider.functions.getReserveConfigurationData(asset).call()
        return int(data[0])

    def all_reserves(self) -> list[str]:
        if not self._reserves:
            self._load_reserves()
        return self._reserves

    def symbol_of(self, asset: str) -> str:
        return self._reserve_symbols.get(asset, asset[:10])

    def decimals(self, asset: str) -> int | None:
        return self.decimals_resolver.try_resolve(asset) if self.decimals_resolver else None

    def liquidation_bonus(self, asset: str) -> int | None:
        cached = self._bonus_cache.get(asset)
        if cached is not None:
            return cached
        try:
            data = self.data_provider.functions.getReserveConfigurationData(asset).call()
        except Exception as exc:
            self.log.warning("no liquidationBonus for %s (%s) — skipping", asset, exc)
            return None
        return self._cache_bonus(asset, int(data[3]))

    def is_svr_reserve(self, asset: str) -> bool:
        if asset in self.state.svr_reserves:
            return True
        if asset in self._svr_checked or not self.config.known_svr_proxies:
            return False
        self._svr_checked.add(asset)
        try:
            source = Web3.to_checksum_address(
                self.oracle.functions.getSourceOfAsset(asset).call()
            )
        except Exception as exc:
            self.log.debug("SVR check failed for %s: %s", asset, exc)
            return False
        if source in self.config.known_svr_proxies:
            self.state.mark_svr(asset)
            self.log.info("reserve %s is SVR-protected (feed %s)", asset, source)
            return True
        return False

    # ── price feed discovery ─────────────────────────────────────────────
    def discover_price_feeds(self) -> dict[str, set[str]]:
        """Resolve which Chainlink aggregator drives which Aave reserves.

        Aave's getSourceOfAsset returns the proxy; AnswerUpdated is emitted by
        the aggregator behind it, so the proxy is dereferenced one more step.
        Manual PRICE_FEED_AGGREGATORS entries are merged on top.
        """
        mapping: dict[str, set[str]] = {}

        if self.config.discover_price_feeds:
            for asset in self.all_reserves():
                aggregator = self._aggregator_for_asset(asset)
                if aggregator:
                    mapping.setdefault(aggregator, set()).add(asset)
            self.log.info(
                "price feeds discovered: %d aggregators for %d reserves",
                len(mapping), sum(len(v) for v in mapping.values()),
            )

        for symbol, aggregator in self.config.price_feed_aggregators.items():
            assets = {
                asset for asset, name in self._reserve_symbols.items()
                if name.upper() == symbol
            }
            mapping.setdefault(aggregator, set()).update(assets)
            if not assets:
                self.log.warning(
                    "manual feed %s:%s has no matching reserve — price updates from it "
                    "will not map to any position", symbol, aggregator,
                )

        self.aggregator_assets = mapping
        return mapping

    def _aggregator_for_asset(self, asset: str) -> str | None:
        try:
            source = self.oracle.functions.getSourceOfAsset(asset).call()
            source = Web3.to_checksum_address(source)
            if int(source, 16) == 0:
                return None
        except Exception as exc:
            self.log.debug("getSourceOfAsset failed for %s: %s", asset, exc)
            return None

        proxy = self.w3.eth.contract(address=source, abi=abis.CHAINLINK_PROXY_ABI)
        try:
            aggregator = Web3.to_checksum_address(proxy.functions.aggregator().call())
            return aggregator if int(aggregator, 16) else None
        except Exception:
            # Not every source is a Chainlink proxy: Aave also uses capped
            # adapters and rate providers that expose no aggregator(). Watching
            # the source itself is the best available approximation.
            self.log.debug("%s exposes no aggregator(), watching the source directly", source)
            return source

    def warm_caches(self) -> None:
        """Resolve decimals and liquidation bonuses for every reserve up front.

        Unusable assets then surface at startup rather than mid-event, and the
        event handlers can format amounts from cache without an RPC call inside
        the event loop. Batched through multicall: doing it one asset at a time
        is ~3 calls per reserve, which public endpoints answer with 429.
        """
        reserves = self.all_reserves()
        if not reserves:
            return

        configs = self._batch_reserve_configuration(reserves)
        for asset, config in configs.items():
            if config is None:
                continue
            self._cache_bonus(asset, int(config[3]))

        if self.decimals_resolver:
            for asset, config in configs.items():
                if config is not None:
                    self.decimals_resolver.seed(asset, int(config[0]))

        # Anything the batch could not settle falls back to individual probes,
        # which also cross-check the token's own decimals() against the reserve.
        unresolved = [a for a in reserves if self.decimals(a) is None]
        if unresolved:
            self.log.warning(
                "reserves with unresolved decimals, skipped until they resolve: %s",
                ", ".join(self.symbol_of(a) for a in unresolved),
            )
        self.log.info("warmed %d/%d reserves (decimals + liquidation bonus)",
                      len(reserves) - len(unresolved), len(reserves))

    def _batch_reserve_configuration(self, reserves: list[str]) -> dict[str, tuple | None]:
        """getReserveConfigurationData for every reserve, in one multicall."""
        out: dict[str, tuple | None] = {asset: None for asset in reserves}
        if not self.multicall_available:
            return out

        calls = [
            {
                "target": self.config.data_provider,
                "callData": self.data_provider.functions.getReserveConfigurationData(
                    asset
                )._encode_transaction_data(),
            }
            for asset in reserves
        ]
        raw = self._try_aggregate(calls, "reserve configuration")
        if raw is None:
            return out

        for asset, (success, data) in zip(reserves, raw):
            if not success or not data:
                continue
            try:
                out[asset] = abi_decode(RESERVE_CONFIG_TYPES, data)
            except Exception as exc:
                self.log.debug("reserve config decode failed for %s: %s", asset, exc)
        return out

    def _cache_bonus(self, asset: str, bonus: int) -> int | None:
        """Shared verdict on a liquidation bonus value, batched or single."""
        if bonus == 0:
            # Legitimate for assets Aave does not accept as collateral, e.g. GHO.
            # There is no liquidation premium to earn, so it is not a target.
            self.log.debug("%s has no liquidation bonus (not collateral)", asset)
            return None
        if bonus < 10000:
            self.log.warning("implausible liquidationBonus %d for %s — skipping", bonus, asset)
            return None
        self._bonus_cache[asset] = bonus
        return bonus

    def format_amount(self, asset: str, amount: int) -> str:
        """Human-readable amount using only cached decimals — never hits the RPC."""
        decimals = self._decimals_cache_peek(asset)
        symbol = self.symbol_of(asset)
        if decimals is None:
            return f"{amount} raw {symbol}"
        scaled = Decimal(amount) / (Decimal(10) ** decimals)
        return f"{scaled:,.6f}".rstrip("0").rstrip(".") + f" {symbol}"

    def _decimals_cache_peek(self, asset: str) -> int | None:
        return self.decimals_resolver.cached(asset) if self.decimals_resolver else None

    def price_feed_activity(self, blocks: int = 5_000) -> dict[str, int]:
        """Recent AnswerUpdated counts per watched aggregator.

        Testnet Aave deployments point at mock aggregators holding a constant
        price and emitting nothing, so a subscription there is established
        successfully and then stays silent forever. Counting real emissions is
        the only way to tell a working feed from a decorative one. -1 means the
        node refused the query rather than that the feed is quiet.
        """
        from .topics import ANSWER_UPDATED_TOPIC

        latest = self.w3.eth.block_number
        activity: dict[str, int] = {}
        for aggregator in self.aggregator_assets:
            try:
                logs = self.w3.eth.get_logs({
                    "address": aggregator,
                    "topics": [ANSWER_UPDATED_TOPIC],
                    "fromBlock": max(0, latest - blocks),
                    "toBlock": latest,
                })
                activity[aggregator] = len(logs)
            except Exception as exc:
                self.log.debug("feed activity query failed for %s: %s", aggregator, exc)
                activity[aggregator] = -1
        return activity

    # ── batch reads ──────────────────────────────────────────────────────
    def account_data(self, users: list[str]) -> dict[str, tuple]:
        results: dict[str, tuple] = {}
        if not users:
            return results

        if self.multicall_available:
            calls = [
                {
                    "target": self.config.pool,
                    "callData": self.pool.functions.getUserAccountData(
                        user
                    )._encode_transaction_data(),
                }
                for user in users
            ]
            raw = self._try_aggregate(calls, "account data")
            if raw is not None:
                for user, (success, data) in zip(users, raw):
                    if not success or not data:
                        continue
                    try:
                        results[user] = abi_decode(ACCOUNT_DATA_TYPES, data)
                    except Exception:
                        pass
                return results

        for user in users:
            try:
                results[user] = tuple(self.pool.functions.getUserAccountData(user).call())
            except Exception:
                pass
        return results

    def user_reserve_data(self, user: str, reserves: list[str]) -> list[tuple]:
        if not reserves:
            return []

        out: list[tuple] = []
        if self.multicall_available:
            calls = [
                {
                    "target": self.config.data_provider,
                    "callData": self.data_provider.functions.getUserReserveData(
                        asset, user
                    )._encode_transaction_data(),
                }
                for asset in reserves
            ]
            raw = self._try_aggregate(calls, "user reserves")
            if raw is not None:
                for asset, (success, data) in zip(reserves, raw):
                    if not success or not data:
                        continue
                    try:
                        out.append((asset, *abi_decode(USER_RESERVE_TYPES, data)))
                    except Exception:
                        pass
                return out

        for asset in reserves:
            try:
                out.append((
                    asset,
                    *self.data_provider.functions.getUserReserveData(asset, user).call(),
                ))
            except Exception:
                pass
        return out

    def _try_aggregate(self, calls: list[dict], label: str):
        """One batched read, or None if this attempt failed.

        A failure here is almost always transient — a rate-limited endpoint, a
        timeout — so it must not disable batching for the rest of the process.
        Doing that turns one 429 into permanently issuing N times more calls,
        which guarantees more 429s. Whether the contract exists at all is
        settled once at startup.
        """
        try:
            return self.multicall.functions.tryAggregate(False, calls).call()
        except Exception as exc:
            self._multicall_failures += 1
            self.log.warning(
                "multicall (%s) failed (%s), falling back to single calls for this read",
                label, exc,
            )
            return None

    # ── valuation ────────────────────────────────────────────────────────
    def asset_price(self, asset: str) -> int | None:
        try:
            return int(self.oracle.functions.getAssetPrice(asset).call())
        except Exception as exc:
            self.log.warning("no oracle price for %s: %s", asset, exc)
            return None

    def value_in_base(self, asset: str, amount: int) -> int | None:
        if amount == 0:
            return None
        decimals = self.decimals(asset)
        price = self.asset_price(asset)
        if decimals is None or price is None:
            return None
        return (amount * price) // (10 ** decimals)

    def estimate_collateral_received(
        self, debt_asset: str, debt_to_cover: int, collateral_asset: str
    ) -> int | None:
        debt_price = self.asset_price(debt_asset)
        collateral_price = self.asset_price(collateral_asset)
        debt_decimals = self.decimals(debt_asset)
        collateral_decimals = self.decimals(collateral_asset)
        bonus = self.liquidation_bonus(collateral_asset)

        if None in (debt_price, collateral_price, debt_decimals, collateral_decimals, bonus):
            return None
        if collateral_price == 0:
            return None

        debt_value = (debt_to_cover * debt_price) // (10 ** debt_decimals)
        collateral_value = (debt_value * bonus) // 10000
        return (collateral_value * (10 ** collateral_decimals)) // collateral_price

    # ── swap routing ─────────────────────────────────────────────────────
    def _path_for_router(self, router: dict, collateral: str, debt: str) -> list[str] | None:
        factory, weth = router.get("factory"), router.get("weth")
        if factory is None:
            return [collateral, debt]
        try:
            if int(factory.functions.getPair(collateral, debt).call(), 16) != 0:
                return [collateral, debt]
            if weth and weth not in (collateral, debt):
                first = factory.functions.getPair(collateral, weth).call()
                second = factory.functions.getPair(weth, debt).call()
                if int(first, 16) != 0 and int(second, 16) != 0:
                    return [collateral, weth, debt]
        except Exception as exc:
            self.log.warning("pair lookup failed on %s: %s", router["address"], exc)
        return None

    def _amount_out_min(self, router: dict, path: list[str], amount_in: int) -> int | None:
        if amount_in == 0:
            return None
        try:
            amounts = router["quote"].functions.getAmountsOut(amount_in, path).call()
        except Exception as exc:
            self.log.debug("no quote on %s: %s", router["address"], exc)
            return None
        expected = amounts[-1]
        return int(Decimal(expected) * (Decimal(1) - self.config.slippage_tolerance))

    def build_swap_params(
        self, collateral: str, debt: str, debt_to_cover: int
    ) -> tuple[str, list[str], int] | None:
        if not self.routers:
            return None
        collateral_amount = self.estimate_collateral_received(debt, debt_to_cover, collateral)
        if not collateral_amount:
            return None

        best: tuple[str, list[str], int] | None = None
        for router in self.routers:
            path = self._path_for_router(router, collateral, debt)
            if path is None:
                continue
            amount_out_min = self._amount_out_min(router, path, collateral_amount)
            if not amount_out_min:
                continue
            if best is None or amount_out_min > best[2]:
                best = (router["address"], path, amount_out_min)
        return best

    # ── position selection ───────────────────────────────────────────────
    def ranked_liquidation_pairs(
        self, user: str
    ) -> list[tuple[str, int, str, int, int]]:
        """Collateral/debt pairs ranked by USD debt (then collateral)."""
        from .pairs import rank_collateral_debt_pairs

        reserves = sorted(self.state.reserves_of(user)) or self.all_reserves()
        rows = self.user_reserve_data(user, reserves)
        return rank_collateral_debt_pairs(rows, self.value_in_base)

    def pick_collateral_and_debt(self, user: str) -> tuple[str | None, int, str | None, int]:
        ranked = self.ranked_liquidation_pairs(user)
        if not ranked:
            return None, 0, None, 0
        collateral, collateral_amount, debt, debt_amount, _score = ranked[0]
        return collateral, collateral_amount, debt, debt_amount

    def should_use_full_close_factor(
        self, health_factor: Decimal, debt_asset: str, debt_amount: int,
        collateral_asset: str, collateral_amount: int,
    ) -> bool:
        if health_factor < self.config.close_factor_hf_threshold:
            return True
        threshold = self.config.min_base_max_close_factor_threshold
        for asset, amount in ((debt_asset, debt_amount), (collateral_asset, collateral_amount)):
            value = self.value_in_base(asset, amount)
            if value is not None and value < threshold:
                return True
        return False

    def _plan_for_pair(
        self,
        user: str,
        health_factor: Decimal,
        collateral: str,
        collateral_amount: int,
        debt: str,
        debt_amount: int,
    ) -> LiquidationPlan | None:
        if self.config.skip_svr_reserves and (
            self.is_svr_reserve(debt) or self.is_svr_reserve(collateral)
        ):
            self.log.info("  %s: SVR-protected reserve %s/%s — skip pair",
                          user, self.symbol_of(collateral), self.symbol_of(debt))
            return None

        # Refuse rather than guess: a wrong decimals value silently corrupts
        # every size and profit calculation downstream.
        if self.decimals(debt) is None or self.decimals(collateral) is None:
            self.log.warning("  %s: decimals unresolved for %s/%s — skip pair",
                             user, self.symbol_of(collateral), self.symbol_of(debt))
            return None

        use_full = self.should_use_full_close_factor(
            health_factor, debt, debt_amount, collateral, collateral_amount
        )
        debt_to_cover = debt_amount if use_full else debt_amount // 2
        if debt_to_cover == 0:
            return None

        return LiquidationPlan(
            user=user,
            collateral_asset=collateral,
            collateral_amount=collateral_amount,
            debt_asset=debt,
            debt_to_cover=debt_to_cover,
            health_factor=health_factor,
        )

    def evaluate_user(self, user: str) -> LiquidationPlan | None:
        data = self.account_data([user]).get(user)
        if not data:
            return None
        total_debt_base, health_factor_raw = data[1], data[5]
        if total_debt_base == 0 or total_debt_base < self.config.min_debt_base_threshold:
            return None

        health_factor = Decimal(health_factor_raw) / WAD
        if health_factor >= self.config.health_factor_threshold:
            return None

        self.log.info("candidate %s | HF=%.4f", user, health_factor)
        ranked = self.ranked_liquidation_pairs(user)
        if not ranked:
            return None

        # Prefer a pair that already has a viable V2 swap (or same-asset).
        # Fall back to the top USD pair for observation if none quote.
        fallback: LiquidationPlan | None = None
        for collateral, collateral_amount, debt, debt_amount, _score in ranked:
            plan = self._plan_for_pair(
                user, health_factor, collateral, collateral_amount, debt, debt_amount
            )
            if plan is None:
                continue
            if fallback is None:
                fallback = plan
            same = collateral.lower() == debt.lower()
            if same or self.build_swap_params(collateral, debt, plan.debt_to_cover):
                self.log.info(
                    "  collateral=%s(%s) debt=%s(%s) debtToCover=%d closeFactor=%s",
                    self.symbol_of(collateral), collateral,
                    self.symbol_of(debt), debt, plan.debt_to_cover,
                    "full" if plan.debt_to_cover == debt_amount else "half",
                )
                return plan

        if fallback is not None:
            self.log.info(
                "  no quoted swap among %d pairs — using top USD %s/%s (observation)",
                len(ranked),
                self.symbol_of(fallback.collateral_asset),
                self.symbol_of(fallback.debt_asset),
            )
        return fallback

    # ── execution ────────────────────────────────────────────────────────
    def _next_nonce(self) -> int:
        confirmed = self.w3.eth.get_transaction_count(self.account.address)
        if self._local_nonce is None or confirmed > self._local_nonce:
            self._local_nonce = confirmed
        return self._local_nonce

    def _advance_nonce(self) -> None:
        if self._local_nonce is not None:
            self._local_nonce += 1

    def gas_fields(self) -> dict:
        try:
            latest = self.w3.eth.get_block("latest")
            base_fee = latest.get("baseFeePerGas")
            if base_fee is not None:
                try:
                    priority = self.w3.eth.max_priority_fee
                except Exception:
                    priority = Web3.to_wei(1, "gwei")
                return {
                    "maxFeePerGas": int(base_fee * 1.5) + int(priority),
                    "maxPriorityFeePerGas": int(priority),
                }
        except Exception:
            pass
        return {"gasPrice": int(self.w3.eth.gas_price * 1.2)}

    def _gas_price_within_ceiling(self, tx: dict) -> bool:
        ceiling = self.config.max_gas_price_gwei
        if ceiling <= 0:
            return True
        price = tx.get("maxFeePerGas") or tx.get("gasPrice") or 0
        limit = Web3.to_wei(ceiling, "gwei")
        if price > limit:
            self.log.info("gas price %d wei exceeds the %s gwei ceiling — skipping",
                          price, ceiling)
            return False
        return True

    def execute_liquidation(self, plan: LiquidationPlan) -> bool:
        if not self.config.auto_execute or self.liquidation_bot is None:
            return False

        # Re-read health factor: between detection and broadcast someone else may
        # have taken the position, or the user may have topped up collateral.
        try:
            fresh = self.pool.functions.getUserAccountData(plan.user).call()
            fresh_hf = Decimal(fresh[5]) / WAD
            if fresh_hf >= self.config.health_factor_threshold:
                self.log.info("[%s] no longer liquidatable at broadcast time (HF=%.4f)",
                              plan.user, fresh_hf)
                return False
        except Exception as exc:
            self.log.warning("[%s] fresh HF re-check failed (%s), continuing", plan.user, exc)

        if not self._prepare_swap(plan):
            return False

        tx = self._build_transaction(plan)
        if tx is None:
            return False
        if not self._gas_price_within_ceiling(tx):
            return False

        if self.config.simulate_before_send:
            result = simulate_transaction(
                self.w3, tx, self.error_table, float(self.config.gas_limit_buffer)
            )
            if not result.ok:
                self.log.info("[%s] simulation rejected the liquidation: %s",
                              plan.user, result.reason)
                return False
            tx["gas"] = result.gas_limit
            self.log.info("[%s] simulation passed, gas %d (~%s ETH)",
                          plan.user, result.gas_limit,
                          Web3.from_wei(result.gas_cost_wei or 0, "ether"))
        else:
            tx["gas"] = 3_000_000

        return self._broadcast(plan, tx)

    def _prepare_swap(self, plan: LiquidationPlan) -> bool:
        if plan.collateral_asset.lower() == plan.debt_asset.lower():
            plan.router = "0x" + "00" * 20
            plan.swap_path = []
            plan.amount_out_min = 0
            return True

        params = self.build_swap_params(
            plan.collateral_asset, plan.debt_asset, plan.debt_to_cover
        )
        if params is None:
            self.log.info("[%s] no viable swap %s -> %s, skipping", plan.user,
                          self.symbol_of(plan.collateral_asset),
                          self.symbol_of(plan.debt_asset))
            return False

        router, path, amount_out_min = params
        if path[0] != plan.collateral_asset or path[-1] != plan.debt_asset:
            self.log.error("[%s] swap path endpoints do not match, skipping", plan.user)
            return False

        plan.router, plan.swap_path, plan.amount_out_min = router, path, amount_out_min
        return True

    def _build_transaction(self, plan: LiquidationPlan) -> dict | None:
        try:
            deadline = int(time.time()) + self.config.swap_deadline_seconds
            params = (
                plan.user,
                plan.collateral_asset,
                plan.router,
                plan.swap_path or [],
                plan.amount_out_min,
                self.config.min_profit_token_units,
                deadline,
            )
            call = self.liquidation_bot.functions.initiateLiquidation(
                plan.debt_asset, plan.debt_to_cover, params
            )
            tx = {
                "from": self.account.address,
                "to": self.config.liquidation_bot,
                "data": call._encode_transaction_data(),
                "value": 0,
                "nonce": self._next_nonce(),
                "chainId": self.w3.eth.chain_id,
            }
            tx.update(self.gas_fields())
            return tx
        except Exception as exc:
            self.log.error("[%s] could not build the transaction: %s", plan.user, exc)
            return None

    def _broadcast(self, plan: LiquidationPlan, tx: dict) -> bool:
        if self.private_w3 is None:
            self.log.error(
                "[%s] refusing public broadcast — PRIVATE_TX_RPC_URL not configured",
                plan.user,
            )
            return False
        try:
            signed = self.account.sign_transaction(tx)
            tx_hash = self.private_w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as exc:
            self.log.error("[%s] broadcast failed: %s", plan.user, exc)
            self._local_nonce = None
            return False

        self.log.info("[%s] sent via private relay (nonce %d): %s",
                      plan.user, tx["nonce"], tx_hash.hex())
        self.pending_txs.append({
            "tx_hash": tx_hash,
            "user": plan.user,
            "nonce": tx["nonce"],
            "using_private": True,
            "sent_at": time.time(),
        })
        return True

    def check_pending_transactions(self) -> list[str]:
        """Returns human-readable outcomes for anything that settled."""
        if not self.pending_txs:
            return []

        outcomes: list[str] = []
        still_pending: list[dict] = []
        for entry in self.pending_txs:
            try:
                receipt = self.w3.eth.get_transaction_receipt(entry["tx_hash"])
            except Exception:
                elapsed = time.time() - entry["sent_at"]
                timeout = (
                    self.config.private_tx_timeout_seconds if entry["using_private"]
                    else self.config.public_tx_timeout_seconds
                )
                if elapsed > timeout:
                    message = (
                        f"[{entry['user']}] tx {entry['tx_hash'].hex()} "
                        f"не подтверждена за {timeout}с"
                    )
                    self.log.warning(message)
                    outcomes.append(message)
                else:
                    still_pending.append(entry)
                continue

            if receipt.status == 1:
                message = (
                    f"[{entry['user']}] ликвидация подтверждена, "
                    f"блок {receipt.blockNumber}, gas {receipt.gasUsed}"
                )
                self.log.info(message)
            else:
                message = f"[{entry['user']}] транзакция отклонена on-chain (revert)"
                self.log.warning(message)
            outcomes.append(message)
            if entry["using_private"]:
                self._advance_nonce()

        self.pending_txs = still_pending
        return outcomes
