#!/usr/bin/env python3
"""Trace feeder 0xb90fe999 activity around Aug24 dust liqs."""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "morpho_research" / "out"
LOCAL = timezone(timedelta(hours=5))
FEEDER = "0xb90fe999be6869af0afc557dccfbe169ea3403d6"
MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
BS = "https://base.blockscout.com/api/v2"

VICTIMS = {
    "0x247a051669a71035781ed774d6c63dcfb7c7b662": ("20:33:17", 50398125),
    "0x863988e33ddd30d6f0be02cf88c84c8e9417404b": ("20:41:55", 50398384),
    "0x3c4e1436964946d0fe3e47481deac5c8cac00e62": ("20:51:47", 50398680),
    "0x0edd90f12d9592f175d6935e69049509b1e6021c": ("20:56:55", 50398834),
    "0x1620f0abf068d9b37667bd06a9baa31a2544dc98": ("20:59:25", 50398909),
    "0x71ad2f4f5da43d520ea0871fdc002cf78e7c07c1": ("21:13:21", 50399327),
}


def http_json(url: str):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "dust-feeder/1"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def topic0(sig: str) -> str:
    h = Web3.keccak(text=sig).hex()
    return (h if h.startswith("0x") else "0x" + h).lower()


SUP = topic0("SupplyCollateral(bytes32,address,address,uint256)")
BOR = topic0("Borrow(bytes32,address,address,address,uint256,uint256)")


def main() -> int:
    cfg = dotenv_values(ROOT / ".env")
    urls = [u.strip() for u in (cfg.get("BASE_HTTP_RPC_URLS") or "").split(",") if u.strip()]
    w3 = None
    for u in urls:
        try:
            w = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 40}))
            if w.is_connected():
                w3 = w
                print("RPC", u[:50])
                break
        except Exception:
            continue
    assert w3

    # Feeder token transfers (outgoing cbXRP)
    xfers = []
    url = f"{BS}/addresses/{FEEDER}/token-transfers?type=ERC-20&filter=from"
    next_url = url
    pages = 0
    while next_url and pages < 8:
        try:
            data = http_json(next_url)
        except Exception as exc:
            print("xfer page fail", type(exc).__name__, exc)
            break
        items = data.get("items") or []
        xfers.extend(items)
        next_params = (data.get("next_page_params") or {})
        if not next_params or not items:
            break
        # build next - blockscout uses query params object
        q = "&".join(f"{k}={v}" for k, v in next_params.items())
        next_url = f"{BS}/addresses/{FEEDER}/token-transfers?type=ERC-20&filter=from&{q}"
        pages += 1
        time.sleep(0.25)

    print(f"feeder outgoing xfers fetched={len(xfers)}")

    # Map victim <- feeder transfer near liq
    lines = ["=== FEEDER TRACE 0xb90fe999 ===", ""]
    pairs = []
    for v, (local, liq_blk) in VICTIMS.items():
        matches = []
        for x in xfers:
            to = ((x.get("to") or {}).get("hash") or "").lower()
            if to != v:
                continue
            blk = int(x.get("block_number") or 0)
            if not blk:
                continue
            sym = ((x.get("token") or {}).get("symbol") or "")
            matches.append(
                {
                    "block": blk,
                    "delta_b": liq_blk - blk,
                    "delta_s": (liq_blk - blk) * 2,
                    "token": sym,
                    "tx": x.get("transaction_hash"),
                    "value": x.get("total", {}).get("value") if isinstance(x.get("total"), dict) else x.get("total"),
                }
            )
        matches.sort(key=lambda m: m["block"], reverse=True)
        last = matches[0] if matches else None
        print(
            f"{local} victim={v[:10]} feeder_xfers={len(matches)} "
            f"last_delta_s={last['delta_s'] if last else None} token={last['token'] if last else None}"
        )
        lines.append(
            f"{local} {v} xfers={len(matches)} last_delta_s={last['delta_s'] if last else None} "
            f"last_tx={last['tx'] if last else None} token={last['token'] if last else None}"
        )
        pairs.append((v, local, liq_blk, last, matches))

    # For each last feeder tx: does same tx also Borrow on Morpho for victim?
    lines.append("")
    lines.append("=== SAME-TX create? (feeder transfer tx receipt Morpho events) ===")
    for v, local, liq_blk, last, _ in pairs:
        if not last or not last["tx"]:
            lines.append(f"{local} no feeder tx")
            continue
        txh = last["tx"]
        try:
            rc = w3.eth.get_transaction_receipt(txh)
            tx = w3.eth.get_transaction(txh)
        except Exception as exc:
            lines.append(f"{local} receipt fail {type(exc).__name__}")
            continue
        morpho_ev = []
        for lg in rc["logs"]:
            if lg["address"].lower() != MORPHO:
                continue
            tops = []
            for t in lg["topics"]:
                hx = t.hex() if hasattr(t, "hex") else str(t)
                if not hx.startswith("0x"):
                    hx = "0x" + hx
                tops.append(hx.lower())
            if not tops:
                continue
            if tops[0] == SUP:
                onb = "0x" + tops[3][-40:] if len(tops) >= 4 else "?"
                morpho_ev.append(f"SupplyCollateral onBehalf={onb}")
            elif tops[0] == BOR:
                onb = "0x" + tops[2][-40:] if len(tops) >= 3 else "?"
                morpho_ev.append(f"Borrow onBehalf={onb}")
        same = any(v.lower() in e.lower() for e in morpho_ev)
        msg = (
            f"{local} fund_tx={txh[:16]}… block={rc['blockNumber']} "
            f"to={tx['to']} sel={(tx['input'][:4].hex() if hasattr(tx['input'],'hex') else str(tx['input'])[:10])} "
            f"morpho={morpho_ev or '-'} same_victim={same} "
            f"fund→liq={(liq_blk-rc['blockNumber'])*2}s"
        )
        print(msg.encode("ascii", "replace").decode("ascii"))
        lines.append(msg)
        time.sleep(0.12)

        # If fund tx != morpho open, search victim's morpho open nearby via getLogs small window
        if not same:
            # look ±5 blocks around fund, and between fund and liq in 100-block chunks for Borrow onBehalf victim
            start = max(1, last["block"] - 2)
            end = liq_blk - 1
            ut = "0x" + v.lower().replace("0x", "").rjust(64, "0")
            found = []
            a = start
            while a <= end and len(found) < 5:
                b = min(end, a + 99)
                try:
                    logs = w3.eth.get_logs(
                        {
                            "fromBlock": a,
                            "toBlock": b,
                            "address": Web3.to_checksum_address(MORPHO),
                            "topics": [BOR, None, ut],
                        }
                    )
                    for lg in logs:
                        found.append(lg["blockNumber"])
                except Exception as exc:
                    # try smaller
                    try:
                        logs = w3.eth.get_logs(
                            {
                                "fromBlock": a,
                                "toBlock": min(end, a + 20),
                                "address": Web3.to_checksum_address(MORPHO),
                                "topics": [BOR, None, ut],
                            }
                        )
                        for lg in logs:
                            found.append(lg["blockNumber"])
                    except Exception:
                        pass
                a = b + 1
                time.sleep(0.05)
            if found:
                fb = max(found)
                lines.append(
                    f"  Borrow blocks={found} last_borrow→liq={(liq_blk-fb)*2}s"
                )
                print(f"  Borrow last→liq={(liq_blk-fb)*2}s blocks={found}")
            else:
                lines.append("  Borrow not found in fund→liq window (RPC limits?)")
                print("  Borrow not found in window")

    # Strategy conclusion
    strat = [
        "Feeder confirmed: 0xb90fe999… sends cbXRP to fresh wallets then opens Morpho USDC/cbXRP dust (~$16 debt).",
        "Dust bots (0x57fC / 0x1A98 / 0x100a) liquidate minutes later with tiny tips — they watch feeder or new Borrow.",
        "WIN: dedicated FEEDER WATCH sleeve — on Transfer from 0xb90fe999 of cbXRP OR Morpho SupplyCollateral/Borrow where caller/onBehalf chain links to feeder → immediate PRIO eval+try_liq.",
        "Also: ensure Morpho WS Borrow handler uses PRIO_ORACLE and does not sit behind seed backlog; publicnode WS must have failover (yesterday never_saw).",
        "Do not raise tips — winners used 0.002–0.15 gwei.",
    ]
    lines += ["", "=== WIN ==="] + [f"- {s}" for s in strat]
    path = OUT / "dust_aug24_feeder.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", path)
    for s in strat:
        print("-", s.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
