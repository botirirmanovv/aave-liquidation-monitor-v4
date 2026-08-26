# Morpho deploy status (2026-08-16)

Agent-on-duty report for Botir. **Live stayed off.** `AUTO_EXECUTE=false`, `MORPHO_AUTO_EXECUTE=false`, `MORPHO_LIVE_MAINNET=false`. No `unpause()`, no liquidate txs, `monitor_v4.py` not edited, private keys not printed.

Morpho Blue **is** on Base Sepolia at the same CREATE2 as mainnet: `0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb` (live `eth_getCode` size **15623**, `chainId=84532`).

---

## Этап 1 — Base Sepolia

| Item | Result |
| --- | --- |
| Canonical Morpho | Present on 84532 at `0xBBBB…FFCb` (not a mainnet mix-up) |
| On-chain liquidator address | **not deployed** |
| `paused` | Constructor now **starts `paused=true`** (zero window). Not verified live: no bytecode. |
| Forge tests | **16/16 PASS** incl. `test_constructorStartsPaused` (Base fork + mocks) |
| Live RPC views | Morpho Blue OK. Liquidator address empty → no `paused/owner/operator` calls. Dry-run CREATE `0x5b73…0519` has **code_size=0** on Sepolia (Foundry default sender, **not on chain**) |
| Unsigned `forge script` | OK on 84532. CREATE → immediate `pause()` → UniV3 + Aerodrome + executor UniV3 whitelist. **No `unpause`. No `setApprovals` on Sepolia.** ~0.000029 ETH est. |

**BLOCKER:** `.env` `PRIVATE_KEY` / `MORPHO_PRIVATE_KEY` / `BASE_SEPOLIA_PRIVATE_KEY` / `SEPOLIA_PRIVATE_KEY` / `DEPLOYER_KEY` are empty. No Foundry keystore. **No address faked.**

When a testnet key is in `.env`, one command (deploy + pause + write `.env`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy_morpho_one_command.ps1 -Network sepolia
```

Then:

```text
python scripts\check_morpho_liq_view.py --network sepolia
```

Expected live checks after a real deploy: `paused==true`, `code_size>0`, `MORPHO==0xBBBB…FFCb`. Slot already in `.env`: `MORPHO_LIQ_CONTRACT_SEPOLIA=` (empty).

---

## Этап 2 — Base mainnet

**Skipped (broadcast).** Этап 1 did not produce a real paused Sepolia address, and there is no mainnet deploy key / `BASESCAN_API_KEY`.

Code/scripts are still patched so a later mainnet run is constructor-paused with **finite** approvals:

| Setting | Value |
| --- | --- |
| Starts paused | Yes (`paused = true` in constructor + script `pause()`) |
| USDC approve cap | `25_000e6` to Morpho only (6 decimals) |
| cbXRP approve cap | `30_000e6` to routers (cbXRP is **6 decimals**, not 18; extra headroom for ~15% bonus on $25k) |
| Infinite `uint256.max` | Rejected in `setApprovals`; hot path `_ensureApprove(needed)` only |
| Routers | Uni V3 `0x2626…e481`, Aerodrome `0xcF77…E43`, plus executor default Uni `0x2626…7dA0` |

Verify (no API key today — leave the command):

```text
forge verify-contract <MAINNET_ADDR> contracts/MorphoFlashLiquidator.sol:MorphoFlashLiquidator --chain 8453 --watch --constructor-args $(cast abi-encode "constructor(address,address)" 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb <OPERATOR>)
```

`MORPHO_LIQ_CONTRACT=` left empty. Do **not** point Base paper at a Sepolia address.

---

## Этап 3 — Paper path (started, not a 1–2 day wait)

`.env`: `MORPHO_MODE=paper`, `MORPHO_AUTO_EXECUTE=false`, `MORPHO_ALLOWED_MARKETS=USDC/cbXRP`, executor enabled. Contract unset → paper is **encode-only** (`eth_call` skipped). `sent` cannot increment.

**HTTP `--once` (seed=50, ~4 min):**

- executor `mode=paper` `auto=False` `contract=(none)` `live_ready=False`
- tracked **515**, candidates **0**, hot **29**
- `would_send=0` `sim_ok=0` `sim_fail=0` `sent=0` `rate_limit_hits=0`
- nearest HF **1.0054** (not `<1`; $20M whale would be skipped anyway)
- 0 detects is normal

Log: `morpho_research/out/paper_etap3_once.log`

**WS observe 1800s** (`morpho_research/out/paper_etap3_ws.log`):

- `mode=paper` `auto=False` `contract=(none)` `sent=0` the entire window
- duration summary: tracked **518**, hot **29**, candidates **0**, foreign_liq **0**
- `would_send=0` `sim_ok=0` `sim_fail=0` `sent=0` `rate_limit_hits=0`
- WS up on publicnode: ~925 raw Morpho logs / 445 events / 3 oracle moves / 0 reconnects
- 0 HF<1 detects is normal

---

## Этап 4 — Live path prepared, not armed

| Gate | State |
| --- | --- |
| Encode `liquidateWithFlash` | Ready (selector `0x5126ca52`) |
| Sign without send | `prepare_signed_live_tx()` — never called from scanner |
| Broadcast | `_live_send` extra-gated; refuses if `mode!=live` / `MORPHO_AUTO_EXECUTE=false` |
| `AUTO_EXECUTE` (Aave) | **false** |
| `MORPHO_AUTO_EXECUTE` | **false** |
| `MORPHO_LIVE_CONFIRM` | empty |
| `MORPHO_LIVE_MAINNET` | **false** |
| Contract paused | Yes, by construction (once deployed) |
| Whitelist | Uni V3 + Aerodrome (+ executor Uni) in the deploy script |
| Operator | unset until deploy |

Live send still needs **all** of: `MORPHO_MODE=live` + `MORPHO_AUTO_EXECUTE=true` + `MORPHO_LIVE_CONFIRM=YES_SEND_LIVE` + `MORPHO_LIQ_CONTRACT` + `MORPHO_PRIVATE_KEY` + (Base) `MORPHO_LIVE_MAINNET=true`. None of that is set.

---

## Code changed

- `contracts/MorphoFlashLiquidator.sol` — ctor paused; finite `setApprovals(..., amount)`; no max-approve on hot path
- `test/MorphoFlashLiquidator.t.sol` — ctor-paused test; mock instance unpause **in tests only**
- `scripts/DeployMorphoFlashLiquidator.s.sol`, `scripts/deploy_morpho_flash_liq.ts`, `scripts/deploy_morpho_one_command.ps1`, `scripts/check_morpho_liq_view.py`
- `morpho_research/morpho_executor.py` — `prepare_signed_live_tx` (no broadcast)
- `.env` — Morpho paper flags + empty address slots + `BASE_SEPOLIA_RPC_URL`

Executor offline tests: all passed (incl. live still gated, `sent==0` after `prepare_signed`).
