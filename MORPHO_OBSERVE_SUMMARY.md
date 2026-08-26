# Morpho observe summary (Base USDC/cbXRP)

- reason: hours_elapsed (3.00 h, 10800 s)
- mode: `observe` (paper/live reserved in schema; not used)
- pair: USDC/cbXRP (`0xd4a9…4109`)
- tracked borrowers: 150
- detect loops: 7481
- rpc: base-rpc.publicnode.com (`rpc_ok=true`, consecutive_rpc_fail=0)
- no MorphoFlashLiquidator connection, no live txs

## Four numbers

1. **detect_count** (unhealthy / would_send / candidates): **0 / 0 / 0**
2. **avg latency_ms**: **358.8** (RPC roundtrip; loop_avg 448.84; no oracle-tick→eval samples this window)
3. **lost_to_foreign share**: **0.0000** (0 lost / 0 would_send; foreign_liq_total=0)
4. **rate_limit_hits** (every HTTP 429 from the public RPC): **0**

Reserved: simulated_ok=0, sent=0, landed=0, revert_reason=null.

Live file: `morpho_research/out/observe_metrics.json`.
Runner: `morpho_research/morpho_observe_metrics.py`.
This light USDC/cbXRP loop did not 429 publicnode; yesterday’s full scanner on the same host did.
