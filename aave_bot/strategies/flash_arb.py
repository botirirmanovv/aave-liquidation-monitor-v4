"""Two-venue UniswapV2 flash arbitrage against an Aave V3 flash loan.

Discovers mispricings by quoting the same path on every ordered pair of
configured routers. When the round-trip clears the Aave premium + min profit,
builds an initiateArb transaction (simulated, then optionally broadcast).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from itertools import permutations
from typing import TYPE_CHECKING

from web3 import Web3

from .. import abis
from ..simulate import build_error_table, simulate_transaction

if TYPE_CHECKING:
    from ..chain import ChainContext

log = logging.getLogger("aave_bot.strategies.flash_arb")

# Aave V3 flashLoanSimple premium is 0.05% on every network we care about.
AAVE_FLASH_PREMIUM_BPS = 5

FLASH_ARB_ERROR_SIGNATURES = [
    "OnlyOwner()",
    "OnlyOperator()",
    "ZeroAddress()",
    "InvalidAmount()",
    "ContractPaused()",
    "Reentrancy()",
    "UnauthorizedPool()",
    "UnauthorizedInitiator()",
    "RouterNotAllowed()",
    "InsufficientProfit()",
    "ExceedsLimit()",
    "NoFlashLoanInProgress()",
    "InvalidPath()",
    "SameRouter()",
]


@dataclass(slots=True)
class ArbOpportunity:
    borrow_asset: str
    mid_asset: str
    amount: int
    router_buy: str
    router_sell: str
    path_buy: list[str]
    path_sell: list[str]
    amount_out_buy: int
    amount_out_sell: int
    premium: int
    gross_profit: int
    min_profit: int

    @property
    def amount_owed(self) -> int:
        return self.amount + self.premium


class FlashArbStrategy:
    name = "flash_arb"

    def __init__(self, ctx: ChainContext) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.log = logging.getLogger(f"aave_bot.{ctx.config.name}.flash_arb")
        self._bot = None
        self._error_table = build_error_table(FLASH_ARB_ERROR_SIGNATURES)
        self._last_scan_at = 0.0
        self.tokens: list[str] = []
        self.amounts: dict[str, int] = {}
        self.opportunities_seen = 0
        self.scans = 0

    def on_start(self) -> None:
        cfg = self.config
        if not cfg.flash_arb_enabled:
            self.log.info("flash_arb disabled")
            return
        if len(cfg.routers) < 2:
            self.log.warning(
                "flash_arb needs at least two ROUTER_ADDRESSES to compare venues — disabled"
            )
            return

        self.tokens = self._resolve_tokens(cfg.flash_arb_token_refs)
        self.amounts = self._resolve_amounts(cfg.flash_arb_amount_refs)
        if len(self.tokens) < 2:
            self.log.warning(
                "flash_arb needs at least two tokens in FLASH_ARB_TOKENS — disabled"
            )
            return

        if cfg.flash_arb_bot and cfg.has_flash_arb_credentials:
            self._bot = self.ctx.w3.eth.contract(
                address=cfg.flash_arb_bot, abi=abis.FLASH_ARB_BOT_ABI
            )
            self.log.info(
                "flash_arb armed: bot=%s tokens=%s auto_execute=%s",
                cfg.flash_arb_bot,
                ",".join(self.ctx.symbol_of(t) for t in self.tokens),
                cfg.auto_execute,
            )
        else:
            self.log.info(
                "flash_arb observation-only (%s across %d routers)",
                ",".join(self.ctx.symbol_of(t) for t in self.tokens),
                len(cfg.routers),
            )

    def _resolve_tokens(self, refs: list[str]) -> list[str]:
        by_symbol = {
            self.ctx.symbol_of(asset).upper(): asset for asset in self.ctx.all_reserves()
        }
        resolved: list[str] = []
        for ref in refs:
            if ref.startswith("0x") and len(ref) == 42:
                resolved.append(Web3.to_checksum_address(ref))
                continue
            asset = by_symbol.get(ref.upper())
            if asset is None:
                self.log.warning("FLASH_ARB_TOKENS entry %r not in Aave reserves — skipped", ref)
                continue
            resolved.append(asset)
        # Stable order, unique.
        seen: set[str] = set()
        out: list[str] = []
        for asset in resolved:
            if asset not in seen:
                seen.add(asset)
                out.append(asset)
        return out

    def _resolve_amounts(self, refs: dict[str, int]) -> dict[str, int]:
        by_symbol = {
            self.ctx.symbol_of(asset).upper(): asset for asset in self.ctx.all_reserves()
        }
        out: dict[str, int] = {}
        for key, amount in refs.items():
            if key.startswith("0x"):
                out[Web3.to_checksum_address(key)] = amount
            elif key in by_symbol:
                out[by_symbol[key]] = amount
            else:
                self.log.warning("FLASH_ARB_AMOUNTS key %r unknown — skipped", key)
        return out

    def on_price_update(self, assets: set[str]) -> None:
        if not self._active:
            return
        if assets & set(self.tokens):
            # Do not quote on the event-loop thread — just invalidate the
            # throttle so the next strategy tick re-scans under the RPC lock.
            self._last_scan_at = 0.0

    def on_aave_event(self, event_name: str, user: str, reserve: str) -> None:
        return

    def on_tick(self) -> None:
        if not self._active:
            return
        interval = self.config.flash_arb_scan_interval_seconds
        now = time.monotonic()
        if self._last_scan_at and (now - self._last_scan_at) < interval:
            return
        self.scan()

    @property
    def _active(self) -> bool:
        return (
            self.config.flash_arb_enabled
            and len(self.config.routers) >= 2
            and len(self.tokens) >= 2
        )

    def scan(self) -> list[ArbOpportunity]:
        self._last_scan_at = time.monotonic()
        self.scans += 1
        found: list[ArbOpportunity] = []

        for borrow, mid in permutations(self.tokens, 2):
            for amount in self._amounts_ladder(borrow):
                if amount <= 0:
                    continue
                for buy_info, sell_info in permutations(self.ctx.routers, 2):
                    opp = self._quote(borrow, mid, amount, buy_info, sell_info)
                    if opp is None:
                        continue
                    found.append(opp)
                    self._handle(opp)

        if found:
            self.log.info("scan #%d: %d profitable route(s)", self.scans, len(found))
        else:
            self.log.debug("scan #%d: no arb", self.scans)
        return found

    def _amounts_ladder(self, asset: str) -> list[int]:
        """Probe several notionals — thin pools often only arb at one size."""
        base = self._amount_for(asset)
        if base <= 0:
            return []
        # 0.25x .. 4x around the configured / $default size, unique & sorted.
        scales = (0.25, 0.5, 1.0, 2.0, 4.0)
        out: list[int] = []
        seen: set[int] = set()
        for s in scales:
            amount = max(1, int(base * s))
            if amount not in seen:
                seen.add(amount)
                out.append(amount)
        return out

    def _amount_for(self, asset: str) -> int:
        configured = self.amounts.get(asset)
        if configured is not None:
            return configured
        decimals = self.ctx.decimals(asset)
        price = self.ctx.asset_price(asset)
        if decimals is None or not price:
            return 0
        target_base = self.config.flash_arb_default_notional_base
        return (target_base * (10 ** decimals)) // price

    def _quote(
        self,
        borrow: str,
        mid: str,
        amount: int,
        buy_info: dict,
        sell_info: dict,
    ) -> ArbOpportunity | None:
        path_buy = [borrow, mid]
        path_sell = [mid, borrow]
        try:
            out_buy = int(buy_info["quote"].functions.getAmountsOut(amount, path_buy).call()[-1])
            out_sell = int(
                sell_info["quote"].functions.getAmountsOut(out_buy, path_sell).call()[-1]
            )
        except Exception as exc:
            self.log.debug(
                "quote failed %s->%s via %s/%s: %s",
                self.ctx.symbol_of(borrow), self.ctx.symbol_of(mid),
                buy_info["address"][:10], sell_info["address"][:10], exc,
            )
            return None

        premium = (amount * AAVE_FLASH_PREMIUM_BPS) // 10_000
        owed = amount + premium
        if out_sell <= owed:
            return None

        gross = out_sell - owed
        min_profit = self._min_profit(borrow, amount, gross)
        if gross < min_profit:
            return None

        return ArbOpportunity(
            borrow_asset=borrow,
            mid_asset=mid,
            amount=amount,
            router_buy=buy_info["address"],
            router_sell=sell_info["address"],
            path_buy=path_buy,
            path_sell=path_sell,
            amount_out_buy=out_buy,
            amount_out_sell=out_sell,
            premium=premium,
            gross_profit=gross,
            min_profit=min_profit,
        )

    def _min_profit(self, asset: str, amount: int, gross: int) -> int:
        """Floor profit: configured token units, or bps of notional, whichever higher."""
        floor = self.config.flash_arb_min_profit_token_units
        bps = self.config.flash_arb_min_profit_bps
        from_bps = (amount * bps) // 10_000 if bps else 0
        return max(floor, from_bps)

    def _handle(self, opp: ArbOpportunity) -> None:
        self.opportunities_seen += 1
        detail = f"buy={opp.router_buy[:10]} sell={opp.router_sell[:10]}"
        self.log.info(
            "ARB #%d %s -> %s -> %s amount=%s profit=%s (%s)",
            self.opportunities_seen,
            self.ctx.symbol_of(opp.borrow_asset),
            self.ctx.symbol_of(opp.mid_asset),
            self.ctx.symbol_of(opp.borrow_asset),
            opp.amount,
            opp.gross_profit,
            detail,
        )
        reporter = getattr(self.ctx, "report_arb_opportunity", None)
        if callable(reporter):
            reporter(
                "aave_v2",
                opp.borrow_asset,
                opp.mid_asset,
                opp.amount,
                opp.gross_profit,
                detail,
            )
        if not self.config.auto_execute:
            return
        if self._bot is None or not self.config.has_flash_arb_credentials:
            self.log.warning("profitable arb but flash_arb bot/key not configured")
            return
        self._execute(opp)

    def _execute(self, opp: ArbOpportunity) -> None:
        slip = self.config.slippage_tolerance
        amount_out_min_buy = int(Decimal(opp.amount_out_buy) * (Decimal(1) - slip))
        amount_out_min_sell = int(Decimal(opp.amount_out_sell) * (Decimal(1) - slip))
        deadline = int(time.time()) + self.config.swap_deadline_seconds

        params = (
            opp.router_buy,
            opp.router_sell,
            opp.path_buy,
            opp.path_sell,
            amount_out_min_buy,
            amount_out_min_sell,
            opp.min_profit,
            deadline,
        )
        account = self.ctx.account
        assert account is not None and self._bot is not None

        tx = self._bot.functions.initiateArb(
            opp.borrow_asset, opp.amount, params
        ).build_transaction({
            "from": account.address,
            "nonce": self.ctx._next_nonce(),
            **self.ctx.gas_fields(),
        })

        simulation = simulate_transaction(
            self.ctx.w3, tx, account.address, error_table=self._error_table
        )
        if not simulation.ok:
            self.log.warning("arb simulation failed: %s", simulation.revert_reason)
            return

        if self.config.simulate_before_send:
            tx["gas"] = int(
                Decimal(simulation.gas_limit or 500_000)
                * self.config.gas_limit_buffer
            )

        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        sender = self.ctx.private_w3
        if sender is None:
            self.log.error("refusing public arb broadcast — PRIVATE_TX_RPC_URL required")
            return
        tx_hash = sender.eth.send_raw_transaction(raw)
        self.log.info("arb broadcast via private relay %s", tx_hash.hex())
