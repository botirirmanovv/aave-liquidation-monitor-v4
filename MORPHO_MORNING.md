# Morpho flash-liq — morning status (2026-08-16)

Overnight resume of the Morpho flash-liquidation plan. Another workspace copy of `aave-liquidation-monitor-v4` was not found; APIs used are this repo’s (`aave_bot/abis.py`, `aave_bot/simulate.py`, Uni V3 SwapRouter02 + Aerodrome addresses already in MorphoFlashArb / Balancer V3).

Hard limits held: `monitor_v4.py` / Aave bots untouched, `AUTO_EXECUTE` not set true, no Base mainnet deploy or live send, no paid RPC. Telegram (existing `Irmanov_bot` in `.env`) is used only if you run the scanner — observe logs.

## Checklist

- [x] 1. `MorphoFlashLiquidator.sol` finished + compiled (`forge-out/…/MorphoFlashLiquidator.json`)
- [x] 2. Foundry Base-fork tests pass — **13/13** (`MorphoFlashLiquidatorTest`, `forge` 1.7.1, RPC `base-rpc.publicnode.com`)
- [ ] 2b. Deploy Base mainnet — **skipped** (no `PRIVATE_KEY` / `MORPHO_PRIVATE_KEY`; overnight mainnet deploy forbidden even if a key existed)
- [ ] 2c. Sepolia deploy — **skipped** (no testnet key). Scripts left for you.
- [x] 3. `morpho_research/morpho_executor.py` — encode Uni V3 / Aerodrome calldata, nonce pipeline cap 3–5, stagger, gas bump env, filters $300–$25k, `MORPHO_ALLOWED_MARKETS` from `morpho_markets.py`, modes observe / paper / live (live gated). Offline encode tests: `morpho_research/test_morpho_executor.py` (17/17 PASS; live gated, whale skip, selector `0x5126ca52`)
- [x] 4. Minimal wiring in `morpho_scanner.py`: fire-and-forget `try_liq`, HF from cached shares + oracle tick (no Multicall wait), pre-build swap for hot set
- [x] 5. `.env.example` documents `MORPHO_*` (no secrets)
- [ ] 6. Paper 1–2 days on live hot-set / cascades
- [ ] 7. Compare with foreign_liq scoreboard, then consider a tiny live window

## How to run observe

From repo root (existing Telegram token is fine; observe only):

```
.venv-run\Scripts\python.exe morpho_research\morpho_scanner.py --chains base
```

Optional paper (eth_call only, still no broadcast). Needs `MORPHO_LIQ_CONTRACT` after you deploy; without it, paper is encode-only:

```
# in .env (do not set AUTO_EXECUTE / MORPHO_AUTO_EXECUTE true)
MORPHO_MODE=paper
```

Do **not** set `MORPHO_MODE=live`, `MORPHO_AUTO_EXECUTE=true`, `MORPHO_LIVE_CONFIRM=YES_SEND_LIVE`, or `MORPHO_LIVE_MAINNET=true` until you have paper days and a deployed contract.

## Paths

| Piece | Path |
| --- | --- |
| Contract | `contracts/MorphoFlashLiquidator.sol` |
| Tests | `test/MorphoFlashLiquidator.t.sol`, `test/mocks/MockMorphoBlue.sol` |
| Executor | `morpho_research/morpho_executor.py` |
| Scanner wiring | `morpho_research/morpho_scanner.py` |
| Markets | `morpho_research/morpho_markets.py` (`parse_allowed_markets`) |
| Encode self-test | `morpho_research/test_morpho_executor.py` |
| Remix/ethers deploy | `scripts/deploy_morpho_flash_liq.ts` |
| Foundry deploy | `scripts/DeployMorphoFlashLiquidator.s.sol` |
| Env docs | `.env.example` (Morpho block at bottom) |
| Plan (evening) | `tools/send_morpho_evening_plan.py` |

## Test output (Foundry)

```
Ran 13 tests for test/MorphoFlashLiquidator.t.sol:MorphoFlashLiquidatorTest
[PASS] test_badCallbackSender()
[PASS] test_badRouter()
[PASS] test_callbackFromMorphoWithoutOuter()
[PASS] test_constructorSetsMorphoAndOperator()
[PASS] test_forkMorphoBlueHasCode()
[PASS] test_insufficientProfitAbsFloor()
[PASS] test_insufficientProfitReverts()
[PASS] test_onlyOperator()
[PASS] test_ownerCanLiquidate()
[PASS] test_pauseRevertsOuter()
[PASS] test_profitOk()
[PASS] test_setApprovalsAndRescue()
[PASS] test_swapFailed()
Suite result: ok. 13 passed; 0 failed; 0 skipped
```

Re-run:

```
$env:Path = "$env:USERPROFILE\.foundry\bin;" + $env:Path
forge test --match-contract MorphoFlashLiquidatorTest -vv
.venv-run\Scripts\python.exe morpho_research\test_morpho_executor.py
```

## What is NOT done (human)

1. **Base mainnet deploy** of `MorphoFlashLiquidator` + `setRouterAllowed` (Uni V3 `0x2626664c2603336E57B271c5C0d842F2875A7dA0` / alt `…e481`, Aerodrome `0xcF77…E43`) + `setApprovals` for USDC and cb* collaterals + Morpho/routers.
2. Fill `MORPHO_LIQ_CONTRACT` and `MORPHO_OPERATOR_ADDRESS` (bot EOA). Constructor operator may be placeholder/`address(0)` → owner.
3. **Paper mode 1–2 days** on live Base hot-set; watch `would_send` / `paper eth_call` vs foreign Liquidate events in the scoreboard.
4. Live: only after paper, with `MORPHO_MODE=live`, `MORPHO_AUTO_EXECUTE=true`, `MORPHO_LIVE_CONFIRM=YES_SEND_LIVE`, `MORPHO_LIVE_MAINNET=true`, `MORPHO_PRIVATE_KEY` (not the Aave `PRIVATE_KEY` fallback). Default stays observe.
5. Sepolia: use `scripts/deploy_morpho_flash_liq.ts` or `forge script scripts/DeployMorphoFlashLiquidator.s.sol:DeployMorphoFlashLiquidator --rpc-url <sepolia> --broadcast` if you add a testnet key.

## Live gates (do not flip overnight)

Live send requires **all** of: `MORPHO_MODE=live`, `MORPHO_AUTO_EXECUTE=true`, `MORPHO_LIVE_CONFIRM=YES_SEND_LIVE`, `MORPHO_LIQ_CONTRACT` set, `MORPHO_PRIVATE_KEY` set. Base (8453) also needs `MORPHO_LIVE_MAINNET=true`. Missing any gate falls back to paper/observe.
