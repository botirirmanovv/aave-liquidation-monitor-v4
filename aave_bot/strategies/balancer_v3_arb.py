"""Balancer Vault (0% flash) + Uniswap V3 fee-tier arbitrage.

Replaces the Aave 5 bps tax that made V2 round-trips dead on L2 majors.
Observation quotes QuoterV2 across fee pairs; execution needs a deployed
BalancerV3FlashArbBot and AUTO_EXECUTE + key.
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

log = logging.getLogger("aave_bot.strategies.balancer_v3_arb")

# Canonical CREATE2 deployments shared across Ethereum / Base / Arb / OP.
BALANCER_VAULT = "0xBA12222222228d8Ba445958a75a0704d566BF2C8"
# QuoterV2: same CREATE2 on Eth/Arb/OP; Base uses a different address.
UNISWAP_V3_QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
UNISWAP_V3_QUOTER_V2_BY_CHAIN = {
    "base": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
    "arbitrum": UNISWAP_V3_QUOTER_V2,
    "optimism": UNISWAP_V3_QUOTER_V2,
    "ethereum": UNISWAP_V3_QUOTER_V2,
}

# SwapRouter02 per chain (exactInputSingle without deadline).
SWAP_ROUTER_02 = {
    "base": "0x2626664c2603336E57B271c5C0d842F2875A7dA0",
    "arbitrum": "0x68b3465833fb72A710864c33b2b4F9C8c7c6C7E5",
    "optimism": "0x68b3465833fb72A710864c33b2b4F9C8c7c6C7E5",
    "default": "0x68b3465833fb72A710864c33b2b4F9C8c7c6C7E5",
}

V3_FEES = (100, 500, 3000, 10000)

BALANCER_ARB_ERROR_SIGNATURES = [
    "OnlyOwner()",
    "OnlyOperator()",
    "ZeroAddress()",
    "InvalidAmount()",
    "ContractPaused()",
    "Reentrancy()",
    "UnauthorizedVault()",
    "RouterNotAllowed()",
    "InsufficientProfit()",
    "ExceedsLimit()",
    "NoFlashLoanInProgress()",
    "SameFee()",
]


@dataclass(slots=True)
class V3ArbOpportunity:
    borrow_asset: str
    mid_asset: str
    amount: int
    fee_buy: int
    fee_sell: int
    amount_out_buy: int
    amount_out_sell: int
    gross_profit: int
    min_profit: int


class BalancerV3ArbStrategy:
    name = "balancer_v3_arb"

    def __init__(self, ctx: ChainContext) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.log = logging.getLogger(f"aave_bot.{ctx.config.name}.balancer_v3_arb")
        self._bot = None
        self._quoter = None
        self._error_table = build_error_table(BALANCER_ARB_ERROR_SIGNATURES)
        self._last_scan_at = 0.0
        self.tokens: list[str] = []
        self.amounts: dict[str, int] = {}
        self.opportunities_seen = 0
        self.scans = 0

    def on_start(self) -> None:
        cfg = self.config
        if not cfg.balancer_arb_enabled:
            self.log.info("balancer_v3_arb disabled")
            return

        self.tokens = self._resolve_tokens(cfg.balancer_arb_token_refs or cfg.flash_arb_token_refs)
        self.amounts = self._resolve_amounts(
            cfg.balancer_arb_amount_refs or cfg.flash_arb_amount_refs
        )
        if len(self.tokens) < 2:
            self.log.warning("balancer_v3_arb needs >=2 tokens — disabled")
            return

        try:
            quoter_addr = UNISWAP_V3_QUOTER_V2_BY_CHAIN.get(
                self.config.name, UNISWAP_V3_QUOTER_V2
            )
            self._quoter = self.ctx.w3.eth.contract(
                address=Web3.to_checksum_address(quoter_addr),
                abi=abis.UNISWAP_V3_QUOTER_V2_ABI,
            )
        except Exception as exc:
            self.log.error("QuoterV2 unavailable: %s — disabled", exc)
            return

        if cfg.balancer_arb_bot and cfg.has_balancer_arb_credentials:
            self._bot = self.ctx.w3.eth.contract(
                address=cfg.balancer_arb_bot, abi=abis.BALANCER_V3_ARB_BOT_ABI
            )
            self.log.info(
                "balancer_v3_arb armed: bot=%s tokens=%s auto_execute=%s",
                cfg.balancer_arb_bot,
                ",".join(self.ctx.symbol_of(t) for t in self.tokens),
                cfg.auto_execute,
            )
        else:
            self.log.info(
                "balancer_v3_arb observation-only (%s, Balancer fee=0 + UniV3 fees)",
                ",".join(self.ctx.symbol_of(t) for t in self.tokens),
            )

    @property
    def _active(self) -> bool:
        return (
            self.config.balancer_arb_enabled
            and self._quoter is not None
            and len(self.tokens) >= 2
        )

    def on_price_update(self, assets: set[str]) -> None:
        if self._active and (assets & set(self.tokens)):
            self._last_scan_at = 0.0

    def on_aave_event(self, event_name: str, user: str, reserve: str) -> None:
        return

    def on_tick(self) -> None:
        if not self._active:
            return
        interval = self.config.balancer_arb_scan_interval_seconds
        now = time.monotonic()
        if self._last_scan_at and (now - self._last_scan_at) < interval:
            return
        self.scan()

    def scan(self) -> list[V3ArbOpportunity]:
        self._last_scan_at = time.monotonic()
        self.scans += 1
        found: list[V3ArbOpportunity] = []

        for borrow, mid in permutations(self.tokens, 2):
            for amount in self._amounts_ladder(borrow):
                opp = self._best_fee_route(borrow, mid, amount)
                if opp is None:
                    continue
                found.append(opp)
                self._handle(opp)

        if found:
            self.log.info("scan #%d: %d profitable V3 route(s)", self.scans, len(found))
        else:
            self.log.debug("scan #%d: no balancer/v3 arb", self.scans)
        return found

    def _best_fee_route(
        self, borrow: str, mid: str, amount: int
    ) -> V3ArbOpportunity | None:
        best: V3ArbOpportunity | None = None
        for fee_buy in V3_FEES:
            out_buy = self._quote_v3(borrow, mid, amount, fee_buy)
            if not out_buy:
                continue
            for fee_sell in V3_FEES:
                if fee_sell == fee_buy:
                    continue
                out_sell = self._quote_v3(mid, borrow, out_buy, fee_sell)
                if not out_sell:
                    continue
                # Balancer fee = 0 → repay exactly `amount`.
                if out_sell <= amount:
                    continue
                gross = out_sell - amount
                min_profit = self._min_profit(borrow, amount)
                if gross < min_profit:
                    continue
                cand = V3ArbOpportunity(
                    borrow_asset=borrow,
                    mid_asset=mid,
                    amount=amount,
                    fee_buy=fee_buy,
                    fee_sell=fee_sell,
                    amount_out_buy=out_buy,
                    amount_out_sell=out_sell,
                    gross_profit=gross,
                    min_profit=min_profit,
                )
                if best is None or cand.gross_profit > best.gross_profit:
                    best = cand
        return best

    def _quote_v3(
        self, token_in: str, token_out: str, amount_in: int, fee: int
    ) -> int | None:
        assert self._quoter is not None
        try:
            out, *_rest = self._quoter.functions.quoteExactInputSingle(
                (token_in, token_out, amount_in, fee, 0)
            ).call()
            return int(out)
        except Exception:
            return None

    def _amounts_ladder(self, asset: str) -> list[int]:
        base = self._amount_for(asset)
        if base <= 0:
            return []
        out: list[int] = []
        seen: set[int] = set()
        for scale in (0.5, 1.0, 2.0):
            amount = max(1, int(base * scale))
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
        target = self.config.balancer_arb_default_notional_base
        return (target * (10 ** decimals)) // price

    def _min_profit(self, asset: str, amount: int) -> int:
        floor = self.config.balancer_arb_min_profit_token_units
        bps = self.config.balancer_arb_min_profit_bps
        return max(floor, (amount * bps) // 10_000)

    def _handle(self, opp: V3ArbOpportunity) -> None:
        self.opportunities_seen += 1
        detail = f"fees {opp.fee_buy}/{opp.fee_sell}"
        self.log.info(
            "V3 arb %s->%s->%s %s size=%d gross=%d",
            self.ctx.symbol_of(opp.borrow_asset),
            self.ctx.symbol_of(opp.mid_asset),
            self.ctx.symbol_of(opp.borrow_asset),
            detail,
            opp.amount,
            opp.gross_profit,
        )
        reporter = getattr(self.ctx, "report_arb_opportunity", None)
        if callable(reporter):
            reporter(
                "balancer_v3",
                opp.borrow_asset,
                opp.mid_asset,
                opp.amount,
                opp.gross_profit,
                detail,
            )
        if not self.config.auto_execute:
            self.log.info("[dry-run] would initiateBalancerV3Arb gross=%d", opp.gross_profit)
            return
        if self._bot is None or not self.config.has_balancer_arb_credentials:
            self.log.warning("profitable V3 arb but bot/key not configured")
            return
        self._execute(opp)

    def _execute(self, opp: V3ArbOpportunity) -> None:
        assert self._bot is not None and self.ctx.account is not None
        slip = self.config.slippage_tolerance
        params = (
            opp.mid_asset,
            opp.fee_buy,
            opp.fee_sell,
            int(Decimal(opp.amount_out_buy) * (Decimal(1) - slip)),
            int(Decimal(opp.amount_out_sell) * (Decimal(1) - slip)),
            opp.min_profit,
        )
        try:
            tx = self._bot.functions.initiateArb(
                opp.borrow_asset, opp.amount, params
            ).build_transaction({
                "from": self.ctx.account.address,
                "nonce": self.ctx.w3.eth.get_transaction_count(self.ctx.account.address),
                "chainId": self.ctx.w3.eth.chain_id,
            })
            tx.update(self.ctx.gas_fields())
        except Exception as exc:
            self.log.error("build balancer arb tx failed: %s", exc)
            return

        if self.config.simulate_before_send:
            result = simulate_transaction(
                self.ctx.w3, tx, self._error_table, float(self.config.gas_limit_buffer)
            )
            if not result.ok:
                self.log.info("balancer arb simulation rejected: %s", result.reason)
                return
            tx["gas"] = result.gas_limit

        try:
            signed = self.ctx.account.sign_transaction(tx)
            raw = getattr(signed, "rawTransaction", None) or signed.raw_transaction
            sender = self.ctx.private_w3
            if sender is None:
                self.log.error(
                    "refusing public balancer arb broadcast — PRIVATE_TX_RPC_URL required"
                )
                return
            tx_hash = sender.eth.send_raw_transaction(raw)
            self.log.info("balancer arb broadcast via private relay %s", tx_hash.hex())
        except Exception as exc:
            self.log.error("balancer arb broadcast failed: %s", exc)

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
                self.log.warning("BALANCER_ARB token %r not in reserves — skipped", ref)
                continue
            resolved.append(asset)
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
        return out
