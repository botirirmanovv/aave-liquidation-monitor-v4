#!/usr/bin/env python3
"""Offline encode / filter checks for Morpho flash-liq executor. No RPC send."""
from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT))

from web3 import Web3

from morpho_executor import (
    UNI_V3_SWAP_ROUTER_02_BASE,
    EncodedLiq,
    LiqIntent,
    MorphoExecutor,
    encode_aerodrome_swap,
    encode_univ3_exact_input_hop,
    encode_univ3_exact_input_single,
)
from morpho_hf import hf_from_cached_shares, seized_assets_for_repay
from morpho_markets import markets_for_chain, parse_allowed_markets

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  ' + detail}")
    if not condition:
        failures.append(name)


def _intent(market, hf: str = "0.98") -> LiqIntent:
    return LiqIntent(
        chain="base",
        user="0x" + "b0" * 20,
        market=market,
        health_factor=Decimal(hf),
        debt_usd=1200.0,
        profit_usd=45.0,
        borrow_shares=1_000_000 * 10**6,
        collateral=2 * 10**18,
        total_borrow_assets=10_000_000 * 10**6,
        total_borrow_shares=10_000_000 * 10**6,
        oracle_price=10**36 // 2,  # 0.5 loan per coll (dummy)
        loan_decimals=6,
        reason="test",
    )


def main() -> int:
    print("morpho executor encode tests")
    markets = markets_for_chain("base")
    check("base markets loaded", len(markets) >= 3)
    allowed = parse_allowed_markets("base", "USDC/cbXRP,cbDOGE")
    check(
        "allowed filter by pair/symbol",
        {m.collateral_symbol for m in allowed} == {"cbXRP", "cbDOGE"},
        str([m.collateral_symbol for m in allowed]),
    )
    kta = parse_allowed_markets("base", "USDC/KTA")
    check(
        "kta market wired",
        len(kta) == 1
        and kta[0].collateral_symbol == "KTA"
        and kta[0].lltv_wad == 770_000_000_000_000_000,
        str([m.collateral_symbol for m in kta]),
    )
    tails = parse_allowed_markets("base", "WETH/cbBTC,WETH/USDC,USDC/stkWELL")
    check(
        "tail markets wired",
        {f"{m.loan_symbol}/{m.collateral_symbol}" for m in tails}
        == {"WETH/cbBTC", "WETH/USDC", "USDC/stkWELL"},
        str([f"{m.loan_symbol}/{m.collateral_symbol}" for m in tails]),
    )
    from morpho_hf import loan_token_price_usd

    check("usdc loan usd is 1", loan_token_price_usd("USDC", eth_usd=3500) == 1.0)
    check("weth loan usd uses eth", loan_token_price_usd("WETH", eth_usd=3500) == 3500.0)

    recip = "0x" + "11" * 20
    single = encode_univ3_exact_input_single(
        token_in=markets[0].collateral_token,
        token_out=markets[0].loan_token,
        fee=3000,
        recipient=recip,
        amount_in=10**18,
        amount_out_min=100 * 10**6,
    )
    check("univ3 single selector 0x04e45aaf", single[:4].hex() == "04e45aaf", single[:4].hex())
    hop = encode_univ3_exact_input_hop(
        token_in=markets[0].collateral_token,
        mid="0x4200000000000000000000000000000000000006",
        token_out=markets[0].loan_token,
        fee_in=3000,
        fee_out=500,
        recipient=recip,
        amount_in=10**18,
        amount_out_min=100 * 10**6,
    )
    check("univ3 hop calldata", len(hop) > 4)
    aero = encode_aerodrome_swap(
        token_in=markets[0].collateral_token,
        token_out=markets[0].loan_token,
        amount_in=10**18,
        amount_out_min=100 * 10**6,
        recipient=recip,
        deadline=1_700_000_000,
    )
    check("aerodrome calldata", len(aero) > 4)

    hf, borrowed = hf_from_cached_shares(
        collateral=10**18,
        borrow_shares=100,
        total_borrow_assets=200,
        total_borrow_shares=100,
        oracle_price=10**36,
        lltv_wad=625 * 10**15,
    )
    check("cached HF is finite", hf < Decimal("Infinity"))
    seized = seized_assets_for_repay(borrowed, 10**36, 625 * 10**15, collateral_cap=10**18)
    check("seized > 0", seized > 0)

    w3 = Web3()
    saved_mode = os.environ.get("MORPHO_MODE")
    saved_allowed = os.environ.get("MORPHO_ALLOWED_MARKETS")
    os.environ["MORPHO_MODE"] = "observe"
    os.environ.pop("MORPHO_ALLOWED_MARKETS", None)
    os.environ.pop("BASE_MORPHO_ALLOWED_MARKETS", None)
    os.environ.pop("BASE_MORPHO_MODE", None)
    try:
        exe = MorphoExecutor("base", w3)
    finally:
        if saved_mode is None:
            os.environ.pop("MORPHO_MODE", None)
        else:
            os.environ["MORPHO_MODE"] = saved_mode
        if saved_allowed is None:
            os.environ.pop("MORPHO_ALLOWED_MARKETS", None)
        else:
            os.environ["MORPHO_ALLOWED_MARKETS"] = saved_allowed
    check("default mode observe", exe.mode == "observe")
    check("auto_execute off", exe.auto_execute is False)
    live_ok, why = exe._live_ready()
    check("live gated", live_ok is False, why)

    intent = _intent(markets[0])
    encoded = exe.encode(intent, recipient=recip)
    check("encode returns EncodedLiq", isinstance(encoded, EncodedLiq))
    if encoded is not None:
        check("liquidateWithFlash selector 0x5126ca52", encoded.calldata[:4].hex() == "5126ca52", encoded.calldata[:4].hex())
        check("router is allowlisted UniV3", encoded.router == Web3.to_checksum_address(UNI_V3_SWAP_ROUTER_02_BASE))
        check("seizedAssets set, repaidShares 0", encoded.seized_assets > 0 and encoded.repaid_shares == 0)

    whale = _intent(markets[0])
    whale.debt_usd = 80_000
    encoded_whale = exe.try_liq(whale)
    check("whale >$25k skipped", encoded_whale is None)
    check("would_send incremented for in-window", exe.try_liq(intent) is not None)
    check("metrics would_send >= 1", exe.metrics.would_send >= 1)
    check("rate_limit_hits starts at 0", exe.metrics.rate_limit_hits == 0)
    check("mode stays observe", exe.mode == "observe")

    healthy = _intent(markets[0], hf="1.05")
    check("healthy HF skipped", exe.try_liq(healthy) is None)
    check("min debt floor off (only net $0.50)", exe.min_debt_usd == 0.0)

    # Dead-TTL: foreign liq must block LIVE/would_send even after send-cooldown.
    saved_dead = {k: os.environ.get(k) for k in (
        "MORPHO_MODE",
        "MORPHO_AUTO_EXECUTE",
        "MORPHO_LIVE_CONFIRM",
        "MORPHO_LIVE_MAINNET",
    )}
    try:
        os.environ["MORPHO_MODE"] = "observe"
        os.environ["MORPHO_AUTO_EXECUTE"] = "false"
        os.environ.pop("MORPHO_LIVE_CONFIRM", None)
        os.environ.pop("MORPHO_LIVE_MAINNET", None)
        exe_dead = MorphoExecutor("base", Web3())
        exe_dead.cooldown_seconds = 0.01
        exe_dead.dead_ttl_seconds = 90.0
        victim = _intent(exe_dead.allowed[0] if exe_dead.allowed else markets[0])
        exe_dead.note_foreign_liq(victim.user, victim.market.market_id)
        exe_dead._cooldown_until.clear()  # send-cooldown gone; dead-TTL must still block
        check("dead-TTL blocks after foreign", exe_dead.try_liq(victim) is None)
        exe_dead.clear_position_dead(victim.user, victim.market.market_id)
        check("clear dead allows try_liq again", exe_dead.try_liq(victim) is not None)
        m_arm = exe_dead.allowed[0] if exe_dead.allowed else markets[0]
        exe_dead.arm_feeder(m_arm, victim.user)
        check("feeder armed after arm_feeder", exe_dead.is_feeder_armed(victim.user, m_arm.market_id))
        exe_dead.mark_position_dead(victim.user, m_arm.market_id, reason="test")
        check("dead clears feeder arm", not exe_dead.is_feeder_armed(victim.user, m_arm.market_id))
    finally:
        for k, v in saved_dead.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    dust = _intent(markets[0])
    dust.debt_usd = 50.0
    # $300 debt floor removed; $50 may still skip on net/edge, not on debt.

    saved_diag = {k: os.environ.get(k) for k in (
        "MORPHO_DIAGNOSTIC_MODE",
        "MIN_NET_PROFIT_USD",
        "MORPHO_MIN_NET_PROFIT_USD",
        "MORPHO_GAS_USD",
        "MORPHO_ETH_USD",
        "BASE_MORPHO_DIAGNOSTIC_MODE",
        "MORPHO_MODE",
        "MORPHO_AUTO_EXECUTE",
    )}
    try:
        os.environ["MORPHO_DIAGNOSTIC_MODE"] = "1"
        os.environ["MIN_NET_PROFIT_USD"] = "0.05"
        os.environ["MORPHO_GAS_USD"] = "0.01"
        os.environ["MORPHO_MODE"] = "observe"
        os.environ["MORPHO_AUTO_EXECUTE"] = "false"
        os.environ.pop("BASE_MORPHO_DIAGNOSTIC_MODE", None)
        exe_d = MorphoExecutor("base", Web3())
        check("diagnostic mode on", exe_d.diagnostic_mode is True)
        check("diagnostic min debt floor off", exe_d.min_debt_usd == 0.0)
        check("diagnostic keeps max $25k", exe_d.max_debt_usd == 25000.0)
        m_ok = exe_d.allowed[0] if exe_d.allowed else markets[0]
        dust_d = _intent(m_ok)
        dust_d.debt_usd = 50.0
        check("diagnostic dust $50 passes min-debt (net filter)", exe_d.try_liq(dust_d) is not None)
        check("diagnostic would_send logged net", exe_d.metrics.would_send >= 1)
        check("diagnostic net_profit_usd on intent", dust_d.net_profit_usd >= 0.05)
        whale_d = _intent(m_ok)
        whale_d.debt_usd = 80_000
        check("diagnostic whale >$25k skipped", exe_d.try_liq(whale_d) is None)
        check("non-diagnostic min debt stays 0", exe.min_debt_usd == 0.0)
    finally:
        for k, v in saved_diag.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    saved = {k: os.environ.get(k) for k in (
        "MORPHO_LIQ_CONTRACT",
        "BASE_MORPHO_LIQ_CONTRACT",
        "AUTO_EXECUTE",
        "MORPHO_AUTO_EXECUTE",
        "MORPHO_MODE",
        "MORPHO_LIVE_CONFIRM",
        "MORPHO_LIVE_MAINNET",
        "MORPHO_PRIVATE_KEY",
    )}
    try:
        dummy = "0x" + "ab" * 20
        os.environ["MORPHO_LIQ_CONTRACT"] = dummy
        os.environ.pop("BASE_MORPHO_LIQ_CONTRACT", None)
        exe_inh = MorphoExecutor("base", Web3())
        check(
            "unprefixed MORPHO_LIQ_CONTRACT inherited on base",
            exe_inh.contract_addr.lower() == dummy.lower(),
            exe_inh.contract_addr,
        )

        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["MORPHO_AUTO_EXECUTE"] = "false"
        os.environ["MORPHO_MODE"] = "observe"
        os.environ.pop("MORPHO_LIVE_CONFIRM", None)
        os.environ.pop("MORPHO_LIVE_MAINNET", None)
        os.environ.pop("MORPHO_PRIVATE_KEY", None)
        exe_aave = MorphoExecutor("base", Web3())
        live_aave, why_aave = exe_aave._live_ready()
        check("Aave AUTO_EXECUTE does not enable Morpho live", live_aave is False, why_aave)
        check("Morpho auto_execute stays false", exe_aave.auto_execute is False)
        check("mode stays observe", exe_aave.mode == "observe")

        dummy_enc = exe.encode(intent, recipient=dummy)
        check(
            "prepare_signed without key returns None",
            exe_aave.prepare_signed_live_tx(dummy_enc) is None if dummy_enc else False,
        )
        check("prepare_signed does not increment sent", exe_aave.metrics.sent == 0)
        live_ok2, _ = exe_aave._live_ready()
        if live_ok2:
            # Must never happen in this test (AUTO_EXECUTE forced false).
            check("live still gated after prepare_signed", False, "live_ok unexpectedly true")
        else:
            check("live still gated after prepare_signed", True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    if failures:
        print(f"{len(failures)} FAIL")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
