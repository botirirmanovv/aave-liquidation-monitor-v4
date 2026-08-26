"""Morpho Blue flash-loan DEX arb (observation / dry-run).

Funds conceptually via Morpho.flashLoan (0 fee). Quotes:
  - UniV2 cross-router roundtrips (BaseSwap/Sushi, …)
  - UniV3 fee-tier roundtrips (QuoterV2) — same style that hit +$3 once on Base

Execute path (later): contracts/MorphoFlashArbBot.sol — only when bot deployed
and AUTO_EXECUTE. Default is log + optional Telegram.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from itertools import permutations
from typing import Any

from web3 import Web3

from aave_bot import abis
from aave_bot import config as bot_config
from morpho_markets import MORPHO_BLUE

LOG = logging.getLogger("morpho_flash_arb")

# Morpho Blue flashLoan fee is always 0.
MORPHO_FLASH_PREMIUM_BPS = 0

DEFAULT_ROUTERS: dict[str, list[str]] = {
    "base": [
        "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86",  # BaseSwap
        "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891",  # Sushi
    ],
    "arbitrum": [
        "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",  # Sushi
        "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",  # UniV2
    ],
}

DEFAULT_TOKENS: dict[str, list[tuple[str, str, int]]] = {
    # (symbol, address, decimals)
    "base": [
        ("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
        ("WETH", "0x4200000000000000000000000000000000000006", 18),
    ],
    "arbitrum": [
        ("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
        ("WETH", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18),
    ],
}

DEFAULT_AMOUNTS: dict[str, dict[str, int]] = {
    "base": {
        "USDC": 5_000_000000,  # $5k
        "WETH": 10**18,  # 1 ETH
    },
    "arbitrum": {
        "USDC": 5_000_000000,
        "WETH": 10**18,
    },
}

UNISWAP_V3_QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
UNISWAP_V3_QUOTER_V2_BY_CHAIN = {
    "base": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
    "arbitrum": UNISWAP_V3_QUOTER_V2,
}

V3_FEES = (100, 500, 3000, 10000)

ROUTER_QUOTE_ABI = abis.ROUTER_QUOTE_ABI
QUOTER_V2_ABI = abis.UNISWAP_V3_QUOTER_V2_ABI


@dataclass(slots=True)
class MorphoArbOpportunity:
    kind: str  # "v2" | "v3"
    chain: str
    borrow_symbol: str
    mid_symbol: str
    borrow_asset: str
    mid_asset: str
    amount: int
    amount_out_sell: int
    gross_profit: int
    min_profit: int
    premium: int
    # v2
    router_buy: str = ""
    router_sell: str = ""
    # v3
    fee_buy: int = 0
    fee_sell: int = 0

    @property
    def profit_bps(self) -> float:
        if self.amount <= 0:
            return 0.0
        return self.gross_profit * 10_000 / self.amount

    @property
    def amount_owed(self) -> int:
        return self.amount + self.premium


class MorphoFlashArbScanner:
    """HTTP quoter; Morpho flash fee modeled as 0."""

    def __init__(self, chain: str, w3: Web3) -> None:
        self.chain = chain.lower()
        self.w3 = w3
        self.log = logging.getLogger(f"morpho_flash_arb.{self.chain}")
        self.enabled = bot_config._flag(
            "MORPHO_FLASH_ARB_ENABLED", self.chain, True
        )
        self.min_profit_bps = bot_config._integer(
            "MORPHO_FLASH_ARB_MIN_PROFIT_BPS", self.chain, 5
        )
        self.scan_interval = float(
            bot_config._decimal(
                "MORPHO_FLASH_ARB_SCAN_INTERVAL_SECONDS", self.chain, "20"
            )
        )
        self.scans = 0
        self.hits = 0
        self.est_profit_token_sum = 0  # raw token units (mixed — for log only)
        self.est_profit_usd_sum = 0.0
        self._last_scan_at = 0.0
        self._routers: list[dict[str, Any]] = []
        self._quoter = None
        self._tokens: list[tuple[str, str, int]] = []
        self._amounts: dict[str, int] = {}
        self._active = False

    def start(self) -> None:
        if not self.enabled:
            self.log.info("morpho flash_arb disabled")
            return
        tokens = list(DEFAULT_TOKENS.get(self.chain, []))
        if not tokens:
            self.log.warning("no default tokens for %s — disabled", self.chain)
            return
        self._tokens = [
            (sym, Web3.to_checksum_address(addr), dec) for sym, addr, dec in tokens
        ]
        self._amounts = dict(DEFAULT_AMOUNTS.get(self.chain, {}))

        routers = DEFAULT_ROUTERS.get(self.chain, [])
        for addr in routers:
            try:
                c = self.w3.eth.contract(
                    address=Web3.to_checksum_address(addr), abi=ROUTER_QUOTE_ABI
                )
                # Prove factory() exists (getAmountsOut needs a real pool later).
                c.functions.factory().call()
                self._routers.append({"address": Web3.to_checksum_address(addr), "quote": c})
            except Exception as exc:  # noqa: BLE001
                self.log.warning("router %s unusable: %s", addr[:10], exc)

        qaddr = UNISWAP_V3_QUOTER_V2_BY_CHAIN.get(self.chain)
        if qaddr:
            try:
                self._quoter = self.w3.eth.contract(
                    address=Web3.to_checksum_address(qaddr),
                    abi=QUOTER_V2_ABI,
                )
            except Exception as exc:  # noqa: BLE001
                self.log.warning("V3 quoter failed: %s", exc)

        if len(self._routers) < 2 and self._quoter is None:
            self.log.warning("no V2 routers and no V3 quoter — disabled")
            return

        self._active = True
        self.log.info(
            "morpho flash_arb observation-only (Morpho fee=0, morpho=%s) "
            "tokens=%s v2_routers=%d v3=%s min_profit=%dbps interval=%.0fs",
            MORPHO_BLUE[:10],
            ",".join(t[0] for t in self._tokens),
            len(self._routers),
            "yes" if self._quoter else "no",
            self.min_profit_bps,
            self.scan_interval,
        )

    @property
    def active(self) -> bool:
        return self._active

    def maybe_scan(self) -> list[MorphoArbOpportunity]:
        if not self._active:
            return []
        now = time.monotonic()
        if self._last_scan_at and (now - self._last_scan_at) < self.scan_interval:
            return []
        return self.scan()

    def scan(self) -> list[MorphoArbOpportunity]:
        self._last_scan_at = time.monotonic()
        self.scans += 1
        found: list[MorphoArbOpportunity] = []

        for (b_sym, b_addr, _bd), (m_sym, m_addr, _md) in permutations(self._tokens, 2):
            for amount in self._amounts_ladder(b_sym):
                if amount <= 0:
                    continue
                # UniV2 cross-router
                if len(self._routers) >= 2:
                    for buy, sell in permutations(self._routers, 2):
                        opp = self._quote_v2(
                            b_sym, m_sym, b_addr, m_addr, amount, buy, sell
                        )
                        if opp is not None:
                            found.append(opp)
                # UniV3 fee tiers (Morpho fee 0 — same as Balancer funding edge)
                if self._quoter is not None:
                    opp = self._quote_v3_best(b_sym, m_sym, b_addr, m_addr, amount)
                    if opp is not None:
                        found.append(opp)

        # Keep best per (kind, borrow, mid, amount) to avoid spam
        best: dict[tuple, MorphoArbOpportunity] = {}
        for opp in found:
            key = (opp.kind, opp.borrow_symbol, opp.mid_symbol, opp.amount)
            prev = best.get(key)
            if prev is None or opp.gross_profit > prev.gross_profit:
                best[key] = opp
        found = list(best.values())
        found.sort(key=lambda o: -o.gross_profit)

        for opp in found:
            self._handle(opp)

        if found:
            self.log.info(
                "scan #%d: %d Morpho-flash arb route(s) (best +%d bps)",
                self.scans,
                len(found),
                int(found[0].profit_bps),
            )
        return found

    def _amounts_ladder(self, symbol: str) -> list[int]:
        base = self._amounts.get(symbol, 0)
        if base <= 0:
            return []
        out: list[int] = []
        seen: set[int] = set()
        for scale in (0.25, 0.5, 1.0, 2.0):
            amount = max(1, int(base * scale))
            if amount not in seen:
                seen.add(amount)
                out.append(amount)
        return out

    def _min_profit(self, amount: int) -> int:
        return (amount * self.min_profit_bps) // 10_000 if self.min_profit_bps else 0

    def _quote_v2(
        self,
        b_sym: str,
        m_sym: str,
        borrow: str,
        mid: str,
        amount: int,
        buy: dict[str, Any],
        sell: dict[str, Any],
    ) -> MorphoArbOpportunity | None:
        path_buy = [borrow, mid]
        path_sell = [mid, borrow]
        try:
            out_buy = int(
                buy["quote"].functions.getAmountsOut(amount, path_buy).call()[-1]
            )
            out_sell = int(
                sell["quote"].functions.getAmountsOut(out_buy, path_sell).call()[-1]
            )
        except Exception:
            return None
        premium = (amount * MORPHO_FLASH_PREMIUM_BPS) // 10_000
        owed = amount + premium
        if out_sell <= owed:
            return None
        gross = out_sell - owed
        min_p = self._min_profit(amount)
        if gross < min_p:
            return None
        return MorphoArbOpportunity(
            kind="v2",
            chain=self.chain,
            borrow_symbol=b_sym,
            mid_symbol=m_sym,
            borrow_asset=borrow,
            mid_asset=mid,
            amount=amount,
            amount_out_sell=out_sell,
            gross_profit=gross,
            min_profit=min_p,
            premium=premium,
            router_buy=buy["address"],
            router_sell=sell["address"],
        )

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

    def _quote_v3_best(
        self,
        b_sym: str,
        m_sym: str,
        borrow: str,
        mid: str,
        amount: int,
    ) -> MorphoArbOpportunity | None:
        best: MorphoArbOpportunity | None = None
        for fee_buy in V3_FEES:
            out_buy = self._quote_v3(borrow, mid, amount, fee_buy)
            if not out_buy:
                continue
            for fee_sell in V3_FEES:
                if fee_sell == fee_buy:
                    continue
                out_sell = self._quote_v3(mid, borrow, out_buy, fee_sell)
                if not out_sell or out_sell <= amount:
                    continue
                gross = out_sell - amount
                min_p = self._min_profit(amount)
                if gross < min_p:
                    continue
                cand = MorphoArbOpportunity(
                    kind="v3",
                    chain=self.chain,
                    borrow_symbol=b_sym,
                    mid_symbol=m_sym,
                    borrow_asset=borrow,
                    mid_asset=mid,
                    amount=amount,
                    amount_out_sell=out_sell,
                    gross_profit=gross,
                    min_profit=min_p,
                    premium=0,
                    fee_buy=fee_buy,
                    fee_sell=fee_sell,
                )
                if best is None or cand.gross_profit > best.gross_profit:
                    best = cand
        return best

    def _handle(self, opp: MorphoArbOpportunity) -> None:
        self.hits += 1
        self.est_profit_token_sum += opp.gross_profit
        # Rough USD: USDC 1:1; WETH skip precise oracle — log token units.
        if opp.borrow_symbol == "USDC":
            self.est_profit_usd_sum += opp.gross_profit / 1e6
        detail = (
            f"fees {opp.fee_buy}/{opp.fee_sell}"
            if opp.kind == "v3"
            else f"buy={opp.router_buy[:10]} sell={opp.router_sell[:10]}"
        )
        self.log.info(
            "MORPHO_ARB #%d %s %s->%s->%s amount=%d profit=%d (%.1f bps) morpho_fee=0 [%s]",
            self.hits,
            opp.kind,
            opp.borrow_symbol,
            opp.mid_symbol,
            opp.borrow_symbol,
            opp.amount,
            opp.gross_profit,
            opp.profit_bps,
            detail,
        )
