#!/usr/bin/env python3
"""Inside-block analysis: why competitor Morpho liqs land before us.

For cascade blocks: list tx order, find oracle updates, liq txs, tips, positions.
Also probe 0x8cc0 empty-calldata pattern via Blockscout.
Writes morpho_research/out/competitor_inside_block.{json,txt}
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "morpho_research" / "out"
OUT.mkdir(parents=True, exist_ok=True)
BS = "https://base.blockscout.com/api/v2"
UA = {"User-Agent": "morpho-inside-block/1"}

# Cascade blocks from prior analysis + big meat block
CASCADE_BLOCKS = [50293054, 50293068, 50293083, 50302440, 50302464]

# Known competitor contracts from analysis
WHALES = {
    "0x8cc0204e1e12aeb98d35b384fc0676e2df5a16e5",
    "0x48630e5780d5a45a555578cbbc921797ce4f6e7a",
    "0x358954d610222225bbc169ee0d65cf33ac9de34e",
    "0x2134695f185027845d257bcd41cc403560cf60f6",
    "0x30e91ff0bb5220f9df82a52841602de44f2eeab3",
    "0xfabb2d4ac6acf663f5648fa1866a91e0b94e3d49",
}

MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
# cbXRP Morpho oracle from morpho_markets.py
CBXRP_ORACLE = "0x031b2efc8d70042ac8d9f5c793c4149ec4b60fde".lower()

LIQ_TOPIC0 = "0x" + Web3.keccak(
    text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
).hex()
# Chainlink-style AnswerUpdated(int256,uint256,uint256)
ANSWER_UPDATED = "0x" + Web3.keccak(text="AnswerUpdated(int256,uint256,uint256)").hex()
# Generic "Update" / transmit often used by aggregators — also scan for oracle address touches


def http_json(url: str):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        print(f"fail {url[:70]} {exc}")
        return None


def connect_w3() -> Web3:
    for url in (
        "https://mainnet.base.org",
        "https://base-rpc.publicnode.com",
        "https://base.gateway.tenderly.co",
        "https://1rpc.io/base",
    ):
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 40}))
            if w3.is_connected():
                print(f"rpc={url}")
                return w3
        except Exception:
            continue
    raise SystemExit("no rpc")


def short(a: str | None) -> str:
    if not a:
        return "?"
    a = str(a)
    return f"{a[:8]}…{a[-4:]}" if len(a) > 14 else a


def analyze_block(w3: Web3, block_num: int) -> dict:
    print(f"block {block_num}…")
    blk = w3.eth.get_block(block_num, full_transactions=True)
    txs = list(blk.transactions)
    rows = []
    liq_idxs = []
    oracle_idxs = []

    for i, tx in enumerate(txs):
        to = (tx.get("to") or "").lower() if tx.get("to") else ""
        frm = (tx.get("from") or "").lower()
        hx = tx.hash.hex() if hasattr(tx.hash, "hex") else str(tx.hash)
        if not hx.startswith("0x"):
            hx = "0x" + hx
        tip = None
        try:
            if tx.get("maxPriorityFeePerGas") is not None:
                tip = int(tx["maxPriorityFeePerGas"]) / 1e9
            elif tx.get("gasPrice") is not None:
                tip = int(tx["gasPrice"]) / 1e9
        except Exception:
            tip = None
        inp = tx.get("input") or b""
        if isinstance(inp, bytes):
            in_len = len(inp)
            sel = "0x" + inp[:4].hex() if len(inp) >= 4 else ""
        else:
            s = str(inp)
            in_len = max(0, (len(s) - 2) // 2)
            sel = s[:10].lower() if len(s) >= 10 else ""

        is_whale_to = to in WHALES
        is_morpho = to == MORPHO
        is_oracle = to == CBXRP_ORACLE

        # receipt for liquidate event count (only for whale/morpho/oracle candidates)
        liq_ev = 0
        gas_used = None
        interesting = is_whale_to or is_morpho or is_oracle
        if interesting:
            try:
                rcpt = w3.eth.get_transaction_receipt(hx)
                gas_used = int(rcpt.gasUsed)
                for lg in rcpt.logs:
                    t0 = lg.topics[0].hex() if lg.topics else ""
                    if not t0.startswith("0x"):
                        t0 = "0x" + t0
                    if t0.lower() == LIQ_TOPIC0.lower():
                        liq_ev += 1
                    addr = lg.address.lower()
                    if addr == CBXRP_ORACLE:
                        is_oracle = True
            except Exception as exc:  # noqa: BLE001
                print(f"  rcpt fail {hx[:16]} {exc}")

        role = []
        if liq_ev:
            role.append(f"LIQ×{liq_ev}")
            liq_idxs.append(i)
        if is_oracle:
            role.append("ORACLE")
            oracle_idxs.append(i)
        if is_whale_to:
            role.append("WHALE_TO")
        if is_morpho and not liq_ev:
            role.append("MORPHO")

        if role:
            rows.append(
                {
                    "idx": i,
                    "tx": hx,
                    "from": frm,
                    "to": to,
                    "tip_gwei": round(tip, 6) if tip is not None else None,
                    "gas": int(tx.get("gas") or 0),
                    "gas_used": gas_used,
                    "input_bytes": in_len,
                    "selector": sel,
                    "liq_events": liq_ev,
                    "roles": role,
                }
            )
        time.sleep(0.05)

    # Also scan ALL receipts for Liquidate if we missed (Morpho to via internal)
    # Too heavy — instead use blockscout logs for Liquidate in block
    bs_logs = http_json(
        f"{BS}/logs?topic0={LIQ_TOPIC0}&from_block={block_num}&to_block={block_num}"
    )
    bs_liq = []
    items = []
    if isinstance(bs_logs, dict):
        items = bs_logs.get("items") or []
    elif isinstance(bs_logs, list):
        items = bs_logs
    for lg in items:
        th = lg.get("transaction_hash")
        # find idx
        idx = None
        for i, tx in enumerate(txs):
            hx = tx.hash.hex() if hasattr(tx.hash, "hex") else str(tx.hash)
            if not hx.startswith("0x"):
                hx = "0x" + hx
            if hx.lower() == str(th).lower():
                idx = i
                break
        bs_liq.append({"tx": th, "idx": idx, "address": (lg.get("address") or "").lower()})

    return {
        "block": block_num,
        "n_txs": len(txs),
        "timestamp": int(blk.timestamp),
        "interesting": rows,
        "liq_tx_indexes": liq_idxs,
        "oracle_tx_indexes": oracle_idxs,
        "bs_liquidate_logs": bs_liq,
        "first_liq_idx": min(liq_idxs) if liq_idxs else None,
        "first_oracle_idx": min(oracle_idxs) if oracle_idxs else None,
    }


def probe_8cc0(w3: Web3) -> dict:
    addr = Web3.to_checksum_address("0x8cc0204e1e12aeb98D35b384fC0676e2df5a16e5")
    code = w3.eth.get_code(addr)
    info = http_json(f"{BS}/addresses/{addr}") or {}
    # recent txs to this contract
    txs = http_json(f"{BS}/addresses/{addr}/transactions") or {}
    items = txs.get("items") if isinstance(txs, dict) else []
    samples = []
    for it in (items or [])[:8]:
        samples.append(
            {
                "hash": it.get("hash"),
                "method": it.get("method"),
                "from": ((it.get("from") or {}) if isinstance(it.get("from"), dict) else {}).get("hash"),
                "raw_input_len": len((it.get("raw_input") or "0x")) // 2 - 1,
                "status": it.get("status"),
                "result": it.get("result"),
            }
        )
    return {
        "address": addr,
        "code_bytes": len(code),
        "is_contract": info.get("is_contract"),
        "verified": info.get("is_verified"),
        "name": info.get("name"),
        "recent_calls": samples,
        "note": (
            "Empty/tiny calldata + high gas → likely fallback/receive with "
            "preconfigured borrowers OR EIP-7702/auth OR packed tip-only trigger"
        ),
    }


def main() -> int:
    w3 = connect_w3()
    blocks = []
    for b in CASCADE_BLOCKS:
        try:
            blocks.append(analyze_block(w3, b))
        except Exception as exc:  # noqa: BLE001
            print(f"block {b} ERR {exc}")
            blocks.append({"block": b, "error": str(exc)[:200]})
        time.sleep(0.3)

    whale8 = probe_8cc0(w3)

    # Summarize timing patterns
    patterns = []
    for b in blocks:
        if b.get("error"):
            continue
        fo = b.get("first_oracle_idx")
        fl = b.get("first_liq_idx")
        tips = [x.get("tip_gwei") for x in b.get("interesting") or [] if x.get("liq_events")]
        tips = [t for t in tips if t is not None]
        patterns.append(
            {
                "block": b["block"],
                "n_txs": b["n_txs"],
                "oracle_before_liq": (
                    fo is not None and fl is not None and fo < fl
                ),
                "oracle_idx": fo,
                "first_liq_idx": fl,
                "liq_span": (
                    [min(b["liq_tx_indexes"]), max(b["liq_tx_indexes"])]
                    if b.get("liq_tx_indexes")
                    else None
                ),
                "n_liq_txs": len(b.get("liq_tx_indexes") or []),
                "tip_gwei_min": min(tips) if tips else None,
                "tip_gwei_max": max(tips) if tips else None,
                "bs_liq_logs": len(b.get("bs_liquidate_logs") or []),
            }
        )

    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cbxrp_oracle": CBXRP_ORACLE,
        "patterns": patterns,
        "blocks": blocks,
        "whale_8cc0": whale8,
        "how_they_win": [
            "Same-block multi-bot: liquidations clustered after (or without visible) oracle tx in public mempool view",
            "Own unverified contract + operator EOAs (not direct Morpho from hot wallet)",
            "Batch: 2–4 Liquidate events per tx (one simulation, multiple victims)",
            "0x8cc0: near-empty calldata — state already on contract / trigger-only call",
            "High priority tips during cascade vs our default ~0.05 gwei class tips",
            "Our path sees Liquidate log AFTER inclusion → never_saw for HF poll loop",
        ],
        "how_to_beat": [
            "1. Oracle-fast path: on cbXRP oracle AnswerUpdated/storage change → local HF from cached shares → fire WITHOUT waiting Multicall round-trip",
            "2. Prebuild: keep encoded liquidateWithFlash ready for hot HF<1.05 victims; on tick only swap price/amounts",
            "3. Batch: one tx multiple borrowers when cascade (competitors do ×2–×4)",
            "4. Tip policy: storm mode raise priority when oracle Δ large on cbXRP (paid RPC helps inclusion)",
            "5. Don't compete on $16 dust; compete on cascade meat where batch+prebuild matters",
            "6. Optional: watch same whale contracts' mempool input (advanced) — only with private tx infra",
        ],
    }

    (OUT / "competitor_inside_block.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )

    lines = ["=== INSIDE BLOCK: как они нас обходят ===", ""]
    for p in patterns:
        lines.append(
            f"block {p['block']}: txs={p['n_txs']} oracle_idx={p['oracle_idx']} "
            f"first_liq={p['first_liq_idx']} oracle_before_liq={p['oracle_before_liq']} "
            f"liq_txs={p['n_liq_txs']} tip_gwei={p['tip_gwei_min']}..{p['tip_gwei_max']}"
        )
    lines.append("")
    lines.append("--- 0x8cc0 ---")
    lines.append(
        f"code={whale8['code_bytes']}b verified={whale8.get('verified')} "
        f"note={whale8.get('note')}"
    )
    for s in whale8.get("recent_calls") or []:
        lines.append(
            f"  {str(s.get('hash') or '')[:18]} method={s.get('method')} "
            f"in≈{s.get('raw_input_len')} from={short(s.get('from'))}"
        )
    lines.append("")
    lines.append("--- ПОЧЕМУ ОНИ ВНУТРИ БЛОКА ---")
    for h in report["how_they_win"]:
        lines.append(f"• {h}")
    lines.append("")
    lines.append("--- КАК НАМ ИХ ОПЕРЕДИТЬ ---")
    for h in report["how_to_beat"]:
        lines.append(h)
    lines.append("")
    # Per-block interesting txs compact
    for b in blocks:
        if b.get("error"):
            lines.append(f"block {b['block']} ERR {b['error']}")
            continue
        lines.append(f"\nblock {b['block']} interesting txs:")
        for r in b.get("interesting") or []:
            lines.append(
                f"  #{r['idx']:4d} {','.join(r['roles']):20s} tip={r.get('tip_gwei')} "
                f"in={r.get('input_bytes')}b gas={r.get('gas_used')} "
                f"to={short(r.get('to'))} tx={r['tx'][:16]}…"
            )

    text = "\n".join(lines) + "\n"
    (OUT / "competitor_inside_block.txt").write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
