# Morpho flash-liq — workday notes (2026-08-16)

Agent-on-duty while you were at work. **Live stayed off.** `AUTO_EXECUTE=false`, no `MORPHO_MODE=live`, no Base mainnet deploy, no `liquidateWithFlash` broadcast, `monitor_v4.py` untouched, `.env` secrets not copied here.

## What ran

| Check | Result |
| --- | --- |
| Executor encode tests | **24 PASS** (`morpho_research/test_morpho_executor.py`) |
| Foundry `MorphoFlashLiquidatorTest` | **15/15 PASS** (public Base fork RPC) |
| Telegram duty ping | sent to Irmanov_bot |
| `--once` HTTP scout (seed=40) | 425 tracked, **29 hot**, **0 candidates**, `would_send=0`, `sent=0` |
| WS observe 9 min (`--duration-seconds 540`) | WS up on `base-rpc.publicnode.com`, 456 tracked, 29 hot, 151 raw Morpho logs / 61 events, **0 foreign liqs**, **0 oracle moves**, **0 reconnects**, `rate_limit_hits=0` |
| Anvil Base fork | started locally |
| Fork **broadcast** deploy | **skipped** (no mainnet; unsigned sim only) |
| `forge script` **simulate** on anvil | **ok**, ~0.000033 ETH gas, dry-run CREATE `0x5b73c5498c1e3b4dba84de0f1833c4a029d90519` (Foundry default sender, **not on chain**) |
| Sepolia deploy | **skipped** (`SEPOLIA_PRIVATE_KEY` / testnet key unset) |
| Base mainnet deploy | **skipped** (`PRIVATE_KEY` empty; even if set, not tonight) |

`would_send=0` on the live scout is expected: nobody in the seeded book had HF < 1. Unit tests still prove the encode / `$300–$25k` / whale-skip / live-gate path (`selector 0x5126ca52`).

Logs: `morpho_research/out/observe_once_workday.log`, `morpho_research/out/observe_ws_workday.log`.

## Code / docs changed this stretch

- **Executor** (`morpho_research/morpho_executor.py`): unprefixed `MORPHO_LIQ_CONTRACT` / `MORPHO_OPERATOR_ADDRESS` now **inherit** when chain=`base` (`.env.example` matched the code). Live inflight counter no longer sticks at max. Uni V3 checksum literal fixed. (Parallel: HTTP **429 counter** on the public session.)
- **Scanner** (`morpho_research/morpho_scanner.py`): `--duration-seconds`, GraphQL retries, `--once` start/done Telegram + nearest-HF line, first executor TG heartbeat no longer depends on Windows uptime, `MORPHO_STATUS_TG_SECONDS` (default 3600).
- **Tests**: healthy/dust skip, inherit, Aave `AUTO_EXECUTE=true` cannot enable Morpho live.
- **`.env.example`**: observe commands, inherit note, status interval.
- **Deploy script**: checksum + `Deployed` event. Simulate-only documented below.
- **`paper_fork_smoke.py`**: paper eth_call helper **after** a local fork deploy (not used live).
- **`tools/send_morpho_duty_ping.py`**: one-shot TG.

Env actually in `.env`: all `*_AUTO_EXECUTE=false`, **no** `MORPHO_*` keys (defaults = observe). Telegram token/chat present. Base RPC = publicnode HTTP+WS.

## Waiting on you (not done)

1. **Paper 1–2 days** on the live hot-set / cascades (`MORPHO_MODE` still observe until you want encode+eth_call paper).
2. **Dedicated deployer key** + you explicitly want step 4 → then Base deploy. Do **not** reuse a random hot wallet. Unsigned command is below.
3. Fill `MORPHO_LIQ_CONTRACT` + `MORPHO_OPERATOR_ADDRESS` after deploy.
4. Paper `eth_call` against the **real** deployed contract (needs that address).
5. Compare `would_send` vs foreign Liquidate scoreboard, then a tiny live window **only after 1–2 days** and all live gates.
6. Sepolia: add a cheap testnet key if you want a dress rehearsal.

**Do not set** `MORPHO_MODE=live`, `MORPHO_AUTO_EXECUTE=true`, `MORPHO_LIVE_CONFIRM=YES_SEND_LIVE`, `MORPHO_LIVE_MAINNET=true`, or Aave `AUTO_EXECUTE=true`.

## How to start observe tonight

Repo root, same venv, public RPC is enough:

```
.venv-run\Scripts\python.exe morpho_research\morpho_scanner.py --chains base
```

Bounded run (stops itself):

```
.venv-run\Scripts\python.exe morpho_research\morpho_scanner.py --chains base --duration-seconds 3600 --seed-per-market 80
```

One-shot HTTP (no WS):

```
.venv-run\Scripts\python.exe morpho_research\morpho_scanner.py --chains base --once --seed-per-market 50
```

Duty ping:

```
.venv-run\Scripts\python.exe tools\send_morpho_duty_ping.py
```

Leave Morpho mode **unset** (observe) or `MORPHO_MODE=observe`. Optional later: `MORPHO_MODE=paper` for eth_call **only after** `MORPHO_LIQ_CONTRACT` is set. Paper without a contract is encode-only.

A separate observe-metrics helper exists: `morpho_research/morpho_observe_metrics.py` (USDC/cbXRP, counts 429s). Do not overlap two heavy WS scanners on the same public RPC if 429s start showing.

## Unsigned Base / anvil deploy (do not broadcast from a discovered hot key)

Simulate (done today on local anvil):

```
forge script scripts/DeployMorphoFlashLiquidator.s.sol:DeployMorphoFlashLiquidator --rpc-url http://127.0.0.1:8545 -vv
```

**When you** have a deployer key you intend for this (testnet first, or dedicated):

```
forge script scripts/DeployMorphoFlashLiquidator.s.sol:DeployMorphoFlashLiquidator --rpc-url <RPC> --broadcast
```

Then `setApprovals` for cb* collaterals still needed beyond USDC/WETH. Remix/ethers twin: `scripts/deploy_morpho_flash_liq.ts`.

Local paper after **you** deploy on anvil:

```
.venv-run\Scripts\python.exe morpho_research\paper_fork_smoke.py --rpc http://127.0.0.1:8545 --contract 0x...
```

## Live gates (still)

Live send needs **all** of: `MORPHO_MODE=live` + `MORPHO_AUTO_EXECUTE=true` + `MORPHO_LIVE_CONFIRM=YES_SEND_LIVE` + `MORPHO_LIQ_CONTRACT` + `MORPHO_PRIVATE_KEY`. Base (8453) also `MORPHO_LIVE_MAINNET=true`. Missing any → paper/observe.
