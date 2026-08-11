"""Strict token decimals resolution.

The original monitor fell back to 18 whenever decimals() failed, which silently
turns a 6-decimal USDC amount into a 10^12 times larger valuation and can drive
the bot into a guaranteed-loss liquidation. Nothing here ever guesses: an asset
whose decimals cannot be established is refused, and the caller skips it.

Two independent on-chain sources are consulted — the token's own decimals() and
Aave's getReserveConfigurationData — plus a table of well-known tokens used only
to catch disagreement. Note that "stablecoin" does not imply 6 decimals: USDC
and USDT use 6, while DAI, GHO, USDS and FRAX use 18, so the table stores
explicit values per symbol rather than a blanket rule.
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger("aave_bot.decimals")

KNOWN_TOKEN_DECIMALS: dict[str, int] = {
    # 6-decimal stablecoins
    "USDC": 6, "USDC.E": 6, "USDBC": 6, "USDT": 6, "USDT0": 6,
    "EURC": 6, "PYUSD": 6, "FDUSD": 6,
    # 18-decimal stablecoins — the reason a blanket "stables are 6" rule is wrong
    "DAI": 18, "GHO": 18, "USDS": 18, "FRAX": 18, "LUSD": 18,
    "TUSD": 18, "USDE": 18, "SUSD": 18, "CRVUSD": 18, "MAI": 18,
    # 8-decimal BTC representations
    "WBTC": 8, "CBBTC": 8,
    # 18-decimal majors
    "WETH": 18, "ETH": 18, "WSTETH": 18, "WEETH": 18, "RETH": 18,
    "CBETH": 18, "EZETH": 18, "RSETH": 18, "TBTC": 18,
    "LINK": 18, "AAVE": 18, "ARB": 18, "OP": 18, "CRV": 18,
    "SNX": 18, "BAL": 18, "UNI": 18, "LDO": 18, "RPL": 18,
}

MIN_PLAUSIBLE_DECIMALS = 2
MAX_PLAUSIBLE_DECIMALS = 24


class DecimalsUnavailable(RuntimeError):
    """Decimals could not be established for an asset, so it must be skipped."""


class _ProbeFailed:
    """A source did not answer, as opposed to answering with a bad value.

    The difference decides whether a refusal is permanent. A rate-limited or
    timed-out node says nothing about the token, so caching a refusal there
    would blacklist a perfectly good asset for the life of the process.
    """


PROBE_FAILED = _ProbeFailed()


class DecimalsResolver:
    def __init__(
        self,
        token_decimals: Callable[[str], int | None],
        reserve_decimals: Callable[[str], int | None] | None = None,
        symbols: dict[str, str] | None = None,
    ) -> None:
        self._token_decimals = token_decimals
        self._reserve_decimals = reserve_decimals
        self._symbols = {k.upper(): v.upper() for k, v in (symbols or {}).items()}
        self._cache: dict[str, int] = {}
        self._refused: set[str] = set()

    def known_for_symbol(self, symbol: str | None) -> int | None:
        return KNOWN_TOKEN_DECIMALS.get(symbol.upper()) if symbol else None

    def _symbol_of(self, asset: str) -> str | None:
        return self._symbols.get(asset.upper())

    def resolve(self, asset: str) -> int:
        """Decimals for an asset, or DecimalsUnavailable if not trustworthy."""
        cached = self._cache.get(asset)
        if cached is not None:
            return cached
        if asset in self._refused:
            raise DecimalsUnavailable(f"decimals for {asset} previously unresolved")

        from_token = self._safe(self._token_decimals, asset, "token decimals()")
        from_reserve = (
            self._safe(self._reserve_decimals, asset, "reserve configuration")
            if self._reserve_decimals
            else None
        )
        expected = self.known_for_symbol(self._symbol_of(asset))

        candidates = [value for value in (from_token, from_reserve) if self._plausible(value)]
        if not candidates:
            unreachable = PROBE_FAILED in (from_token, from_reserve)
            if not unreachable:
                # Both sources answered, and neither answer is usable. That is a
                # property of the asset, so the refusal is final.
                self._refused.add(asset)
            raise DecimalsUnavailable(
                f"no usable decimals for {asset} "
                f"(token={self._describe(from_token)}, reserve={self._describe(from_reserve)})"
                + ("; node did not answer, will retry" if unreachable else "")
            )

        if len(candidates) == 2 and candidates[0] != candidates[1]:
            self._refused.add(asset)
            raise DecimalsUnavailable(
                f"conflicting decimals for {asset}: "
                f"token={from_token}, reserve={from_reserve}"
            )

        resolved = candidates[0]
        if expected is not None and expected != resolved:
            # On-chain wins, but a mismatch means either our table is stale or
            # the address is not the token we think it is. Worth shouting about.
            log.warning(
                "decimals mismatch for %s (%s): on-chain %d, expected %d — using on-chain",
                asset, self._symbol_of(asset), resolved, expected,
            )

        self._cache[asset] = resolved
        return resolved

    def try_resolve(self, asset: str) -> int | None:
        try:
            return self.resolve(asset)
        except DecimalsUnavailable as exc:
            log.warning("skipping asset: %s", exc)
            return None

    def seed(self, asset: str, value: int) -> bool:
        """Populate the cache from a batched read, if the value is plausible."""
        if not self._plausible(value):
            return False
        self._cache[asset] = value
        self._refused.discard(asset)
        return True

    def cached(self, asset: str) -> int | None:
        return self._cache.get(asset)

    @staticmethod
    def _plausible(value) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and MIN_PLAUSIBLE_DECIMALS <= value <= MAX_PLAUSIBLE_DECIMALS
        )

    @staticmethod
    def _describe(value) -> str:
        return "unreachable" if value is PROBE_FAILED else repr(value)

    @staticmethod
    def _safe(fn: Callable[[str], int | None] | None, asset: str, label: str):
        if fn is None:
            return None
        try:
            return fn(asset)
        except Exception as exc:
            log.debug("%s failed for %s: %s", label, asset, exc)
            return PROBE_FAILED
