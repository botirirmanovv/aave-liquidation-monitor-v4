#!/usr/bin/env python3
"""Inspect known fund→liq windows from dust_aug24_sim.json."""
from __future__ import annotations

import json
import time
from pathlib import Path

from dotenv import dotenv_values
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "morpho_research" / "out"
MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"


def topic0(sig: str) -> str:
    h = Web3.keccak(text=sig).hex()
    return (h if h.startswith("0x") else "0x" + h).lower()


SUP = topic0("SupplyCollateral(bytes32,address,address,uint256)")
BOR = topic0("Borrow(bytes32,address,address,address,uint256,uint256)")
LIQ = topic0(
    "Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
)


def tops(lg):
    out = []
    for t in lg["topics"]:
        hx = t.hex() if hasattr(t, "hex") else str(t)
        if not hx.startswith("0x"):
            hx = "0x" + hx
        out.append(hx.lower())
    return out


def main() -> int:
    data = json.loads((OUT / "dust_aug24_sim.json").read_text(encoding="utf-8"))
    cfg = dotenv_values(ROOT / ".env")
    urls = [u.strip() for u in (cfg.get("BASE_HTTP_RPC_URLS") or "").split(",") if u.strip()]
    w3 = Web3(Web3.HTTPProvider(urls[0], request_kwargs={"timeout": 40}))
    assert w3.is_connected()

    lines = ["=== FUND TX -> LIQ WINDOW DETAIL ===", ""]
    for s in data["summaries"]:
        feeders = [f for f in s.get("feeders") or [] if f.get("token") in {"cbXRP", "USDC"}]
        print(f"\n{s['local']} debt=${s['debt']:.1f} liq_block={s['liq_block']}")
        lines.append(f"{s['local']} user={s['user']} liq={s['liq_block']} winner={s['winner']}")
        if not feeders:
            # try getLogs Borrow in 40 blocks before liq
            ut = "0x" + s["user"].lower().replace("0x", "").rjust(64, "0")
            start = s["liq_block"] - 40
            found = []
            for a in range(start, s["liq_block"], 10):
                b = min(s["liq_block"] - 1, a + 9)
                try:
                    logs = w3.eth.get_logs(
                        {
                            "fromBlock": a,
                            "toBlock": b,
                            "address": Web3.to_checksum_address(MORPHO),
                            "topics": [BOR, None, ut],
                        }
                    )
                    found.extend(logs)
                except Exception as exc:
                    print("  logerr", type(exc).__name__)
                time.sleep(0.05)
            if found:
                fb = found[-1]["blockNumber"]
                msg = f"  no token feeder in JSON; Borrow @ {fb} → liq Δ={(s['liq_block']-fb)*2}s"
                print(msg)
                lines.append(msg)
            else:
                lines.append("  no feeder + no Borrow in 40b")
            continue

        f = feeders[0]
        txh = f["tx"]
        rc = w3.eth.get_transaction_receipt(txh)
        tx = w3.eth.get_transaction(txh)
        sel = tx["input"][:4].hex() if hasattr(tx["input"], "hex") else str(tx["input"])[:10]
        morpho = []
        for lg in rc["logs"]:
            if lg["address"].lower() != MORPHO:
                continue
            tps = tops(lg)
            if tps[0] == SUP:
                morpho.append(
                    "SupplyCollateral caller="
                    + ("0x" + tps[2][-40:] if len(tps) > 2 else "?")
                    + " onBehalf="
                    + ("0x" + tps[3][-40:] if len(tps) > 3 else "?")
                )
            elif tps[0] == BOR:
                morpho.append(
                    "Borrow onBehalf=" + ("0x" + tps[2][-40:] if len(tps) > 2 else "?")
                )
        delta_s = f["delta_b"] * 2
        msg = (
            f"  fund {f['token']} from={f['from'][:12]}… block={f['block']} "
            f"Δliq={delta_s}s (~{f['delta_b']}b) fund_to={tx['to']} sel={sel} "
            f"morpho_in_fund_tx={morpho or 'NONE'}"
        )
        print(msg.encode("ascii", "replace").decode("ascii"))
        lines.append(msg)

        # If morpho not in fund tx, find Borrow between fund and liq
        if not morpho:
            ut = "0x" + s["user"].lower().replace("0x", "").rjust(64, "0")
            found = []
            for a in range(f["block"], s["liq_block"], 5):
                b = min(s["liq_block"] - 1, a + 4)
                try:
                    logs = w3.eth.get_logs(
                        {
                            "fromBlock": a,
                            "toBlock": b,
                            "address": Web3.to_checksum_address(MORPHO),
                            "topics": [BOR, None, ut],
                        }
                    )
                    found.extend([(lg["blockNumber"], lg["transactionHash"].hex()) for lg in logs])
                except Exception:
                    pass
                time.sleep(0.04)
            if found:
                fb, ftx = found[-1]
                lines.append(
                    f"  Borrow @ {fb} tx={ftx[:16]}… fund→borrow={(fb-f['block'])*2}s borrow→liq={(s['liq_block']-fb)*2}s"
                )
                print(
                    f"  Borrow->liq={(s['liq_block']-fb)*2}s fund->borrow={(fb-f['block'])*2}s"
                )
            else:
                # try SupplyCollateral onBehalf
                found2 = []
                for a in range(f["block"], s["liq_block"], 5):
                    b = min(s["liq_block"] - 1, a + 4)
                    try:
                        logs = w3.eth.get_logs(
                            {
                                "fromBlock": a,
                                "toBlock": b,
                                "address": Web3.to_checksum_address(MORPHO),
                                "topics": [SUP, None, None, ut],
                            }
                        )
                        found2.extend([lg["blockNumber"] for lg in logs])
                    except Exception:
                        pass
                    time.sleep(0.04)
                if found2:
                    fb = found2[-1]
                    lines.append(
                        f"  SupplyCollateral @ {fb} fund→sup={(fb-f['block'])*2}s sup→liq={(s['liq_block']-fb)*2}s"
                    )
                    print(f"  Supply->liq={(s['liq_block']-fb)*2}s")
                else:
                    lines.append("  no Borrow/Supply between fund and liq (odd)")
                    print("  no Borrow/Supply in window")
        else:
            lines.append(
                f"  SAME-TX open+fund: signal at fund block; race window = {delta_s}s until liq"
            )
        time.sleep(0.1)

    win = [
        "",
        "=== HOW TO WIN ===",
        "1. FEEDER WATCH (primary): monitor cbXRP Transfer from 0xb90fe999… (and similar).",
        "   On transfer to new addr → add victim to registry hot + prebuild + eval every block until HF<1 or timeout 120s.",
        "2. Observed window fund→liq ≈ 30–66s (15–33 blocks). Enough for free WS if healthy.",
        "3. If fund tx already contains Morpho Supply+Borrow → try_liq in next block (or same if builder).",
        "4. Morpho Borrow/Supply WS must be PRIO max; skip seed queue. publicnode down = miss.",
        "5. Tip irrelevant (0.002–0.15 gwei). Speed-to-see wins.",
        "6. Our gap yesterday: never tracked victims → lost_foreign=6. Fix = feeder sleeve, not storm tip.",
    ]
    lines.extend(win)
    path = OUT / "dust_aug24_winpath.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nwrote", path)
    for w in win:
        print(w.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
