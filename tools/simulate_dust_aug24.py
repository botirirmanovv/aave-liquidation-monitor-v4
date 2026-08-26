#!/usr/bin/env python3
"""Dust sim via Blockscout + tx receipts (no huge getLogs)."""
from __future__ import annotations

import json
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "morpho_research" / "out"
OUT.mkdir(parents=True, exist_ok=True)
LOCAL = timezone(timedelta(hours=5))
MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
BS = "https://base.blockscout.com/api/v2"


def http_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "morpho-dust-sim/1"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def gql_liqs(since: int, until: int) -> list[dict]:
    q = """
    query($chainId: Int!, $ts: Int!, $first: Int!) {
      marketTransactions(
        first: $first
        orderBy: Timestamp
        orderDirection: Asc
        where: { chainId_in: [$chainId] type_in: [Liquidation] timestamp_gte: $ts }
      ) {
        items {
          txHash timestamp
          user { address }
          market {
            loanAsset { symbol decimals priceUsd }
            collateralAsset { symbol }
          }
          data { ... on MarketTransactionLiquidationData { repaidAssets } }
        }
      }
    }
    """
    body = json.dumps(
        {"query": q, "variables": {"chainId": 8453, "ts": since, "first": 50}}
    ).encode()
    req = urllib.request.Request(
        "https://blue-api.morpho.org/graphql",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        items = json.loads(resp.read().decode())["data"]["marketTransactions"]["items"]
    out = []
    for it in items:
        ts = int(it["timestamp"])
        if ts >= until:
            continue
        m = it["market"]
        if m["loanAsset"]["symbol"] != "USDC" or m["collateralAsset"]["symbol"] != "cbXRP":
            continue
        repaid = int((it.get("data") or {}).get("repaidAssets") or 0)
        dec = int(m["loanAsset"]["decimals"])
        px = float(m["loanAsset"]["priceUsd"] or 1)
        out.append(
            {
                "ts": ts,
                "tx": it["txHash"],
                "user": Web3.to_checksum_address(it["user"]["address"]),
                "debt": (repaid / (10**dec)) * px,
            }
        )
    return out


def bs_logs(addr: str) -> list[dict]:
    url = f"{BS}/addresses/{addr}/logs?topic0=null"
    # Better: transactions
    url = f"{BS}/addresses/{addr}/transactions?filter=to%20%7C%20from"
    try:
        data = http_json(url)
    except Exception as exc:  # noqa: BLE001
        print("bs tx fail", addr[:10], type(exc).__name__)
        return []
    return data.get("items") or []


def bs_token_xfers(addr: str) -> list[dict]:
    url = f"{BS}/addresses/{addr}/token-transfers?type=ERC-20"
    try:
        data = http_json(url)
    except Exception as exc:  # noqa: BLE001
        print("bs xfer fail", addr[:10], type(exc).__name__)
        return []
    return data.get("items") or []


def connect() -> Web3:
    cfg = dotenv_values(ROOT / ".env")
    urls = [u.strip() for u in (cfg.get("BASE_HTTP_RPC_URLS") or "").split(",") if u.strip()]
    for u in urls:
        if "publicnode" in u or "nodies" in u:
            continue  # flaky getLogs; fine for get_tx
    for u in urls:
        try:
            w3 = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                print("RPC", u[:56])
                return w3
        except Exception:
            continue
    raise RuntimeError("no rpc")


def topic0(sig: str) -> str:
    h = Web3.keccak(text=sig).hex()
    return h if h.startswith("0x") else "0x" + h


LIQ_T0 = topic0(
    "Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
)
SUP_T0 = topic0("SupplyCollateral(bytes32,address,address,uint256)")
BOR_T0 = topic0("Borrow(bytes32,address,address,address,uint256,uint256)")


def decode_topics(lg) -> list[str]:
    out = []
    for t in lg["topics"]:
        hx = t.hex() if hasattr(t, "hex") else str(t)
        if not hx.startswith("0x"):
            hx = "0x" + hx
        out.append(hx.lower())
    return out


def analyze_liq_tx(w3: Web3, txh: str) -> dict:
    tx = w3.eth.get_transaction(txh)
    rc = w3.eth.get_transaction_receipt(txh)
    tip = (
        float(Web3.from_wei(int(tx["maxPriorityFeePerGas"]), "gwei"))
        if tx.get("maxPriorityFeePerGas") is not None
        else None
    )
    inp = tx["input"]
    sel = inp[:4].hex() if hasattr(inp, "hex") else str(inp)[:10]
    if not sel.startswith("0x"):
        sel = "0x" + sel
    liq = None
    borrower = None
    for lg in rc["logs"]:
        if lg["address"].lower() != MORPHO:
            continue
        tops = decode_topics(lg)
        if tops and tops[0] == LIQ_T0.lower():
            if len(tops) >= 3:
                liq = "0x" + tops[2][-40:]
            if len(tops) >= 4:
                borrower = "0x" + tops[3][-40:]
    return {
        "block": rc["blockNumber"],
        "from": tx["from"],
        "to": tx["to"],
        "tip": tip,
        "sel": sel,
        "in_len": len(inp),
        "liquidator": liq,
        "borrower": borrower,
        "gas": rc["gasUsed"],
    }


def find_create(w3: Web3, user: str, liq_block: int) -> dict | None:
    """Scan recent txs involving user via Blockscout, then inspect Morpho receipts."""
    txs = bs_logs(user)
    # also token transfers to spot feeder
    xfers = bs_token_xfers(user)
    feeder_candidates = []
    for x in xfers[:30]:
        fr = ((x.get("from") or {}).get("hash") or "").lower()
        to = ((x.get("to") or {}).get("hash") or "").lower()
        token = ((x.get("token") or {}).get("symbol") or "")
        blk = int((x.get("block_number") or 0) or 0)
        if to == user.lower() and blk and blk < liq_block and blk > liq_block - 20000:
            feeder_candidates.append(
                {
                    "from": fr,
                    "token": token,
                    "block": blk,
                    "tx": x.get("transaction_hash"),
                    "delta_b": liq_block - blk,
                }
            )

    # Among address txs before liq, find ones that hit Morpho with Supply/Borrow for user
    creates = []
    for t in txs[:40]:
        hx = t.get("hash")
        if not hx:
            continue
        try:
            blk = int(t.get("block_number") or 0)
        except Exception:
            continue
        if not blk or blk >= liq_block or blk < liq_block - 20000:
            continue
        # only morpho-related
        to = ((t.get("to") or {}).get("hash") or "").lower()
        if to != MORPHO and "morpho" not in ((t.get("to") or {}).get("name") or "").lower():
            # might be router
            pass
        try:
            rc = w3.eth.get_transaction_receipt(hx)
            tx = w3.eth.get_transaction(hx)
        except Exception:
            time.sleep(0.2)
            continue
        for lg in rc["logs"]:
            if lg["address"].lower() != MORPHO:
                continue
            tops = decode_topics(lg)
            if not tops:
                continue
            name = None
            on_behalf = None
            caller = None
            if tops[0] == SUP_T0.lower():
                name = "SupplyCollateral"
                if len(tops) >= 3:
                    caller = "0x" + tops[2][-40:]
                if len(tops) >= 4:
                    on_behalf = "0x" + tops[3][-40:]
            elif tops[0] == BOR_T0.lower():
                name = "Borrow"
                if len(tops) >= 3:
                    on_behalf = "0x" + tops[2][-40:]
            if name and on_behalf and on_behalf.lower() == user.lower():
                creates.append(
                    {
                        "name": name,
                        "block": blk,
                        "tx": hx,
                        "from": tx["from"],
                        "to": tx["to"],
                        "caller": caller,
                        "delta_b": liq_block - blk,
                        "delta_s": (liq_block - blk) * 2,
                    }
                )
        time.sleep(0.08)

    creates.sort(key=lambda c: c["block"])
    return {
        "creates": creates,
        "last": creates[-1] if creates else None,
        "first": creates[0] if creates else None,
        "feeders": feeder_candidates[:8],
    }


def main() -> int:
    y0 = datetime(2026, 8, 24, 0, 0, tzinfo=LOCAL)
    y1 = y0 + timedelta(days=1)
    since = int(y0.astimezone(timezone.utc).timestamp())
    until = int(y1.astimezone(timezone.utc).timestamp())
    rows = gql_liqs(since, until)
    print("liqs", len(rows))
    w3 = connect()

    lines = ["=== AUG24 DUST SIM (blockscout+rpc) ===", ""]
    summaries = []

    for row in rows:
        local = datetime.fromtimestamp(row["ts"], tz=LOCAL).strftime("%H:%M:%S")
        print(f"\n== {local} ${row['debt']:.1f} {row['user']} ==")
        meta = analyze_liq_tx(w3, row["tx"])
        create = find_create(w3, row["user"], meta["block"])
        last = create["last"]
        age_s = last["delta_s"] if last else None
        age_b = last["delta_b"] if last else None
        feeder = None
        if create["feeders"]:
            # prefer cbXRP inbound closest before liq
            xrp = [f for f in create["feeders"] if "xrp" in (f["token"] or "").lower() or "cbXRP" in (f["token"] or "")]
            pick = (xrp or create["feeders"])[0]
            feeder = pick["from"]
        s = {
            "local": local,
            "debt": row["debt"],
            "user": row["user"],
            "liq_tx": row["tx"],
            "liq_block": meta["block"],
            "winner": meta["liquidator"] or meta["to"],
            "winner_from": meta["from"],
            "winner_to": meta["to"],
            "tip": meta["tip"],
            "sel": meta["sel"],
            "in_len": meta["in_len"],
            "age_s": age_s,
            "age_b": age_b,
            "last_create": last,
            "n_creates": len(create["creates"]),
            "feeder_token_from": feeder,
            "feeders": create["feeders"],
        }
        summaries.append(s)
        msg = (
            f"{local} ${row['debt']:.1f} age_s={age_s} age_b={age_b} "
            f"last={last['name'] if last else None} create_from={last['from'] if last else None} "
            f"feeder={feeder} winner={s['winner']} tip={meta['tip']} sel={meta['sel']} in={meta['in_len']}"
        )
        print(msg.encode("ascii", "replace").decode("ascii"))
        lines.append(msg)
        time.sleep(0.2)

    ages = [s["age_s"] for s in summaries if s["age_s"] is not None]
    winners = Counter((s["winner"] or "?").lower() for s in summaries)
    feeders = Counter((s["feeder_token_from"] or "?").lower() for s in summaries)
    tips = [s["tip"] for s in summaries if s["tip"] is not None]

    strategy = []
    lines += ["", "=== AGG ===", f"ages_s={ages}", f"winners={dict(winners)}", f"feeders={dict(feeders)}", f"tips={tips}"]

    if ages:
        lines.append(
            f"age min/med/max = {min(ages)} / {sorted(ages)[len(ages)//2]} / {max(ages)} s"
        )
        if min(ages) >= 20:
            strategy.append(
                f"OK window: min {min(ages)}s between last Borrow/Supply and Liquidate — "
                "WS enqueue on Borrow/SupplyCollateral + immediate eval can beat dust bots"
            )
        elif max(ages) <= 10:
            strategy.append(
                "TIGHT: <=10s window — need same-path: Borrow log -> local HF -> prebuilt send without Multicall"
            )
        else:
            strategy.append(
                f"MIXED windows {min(ages)}-{max(ages)}s — still win if Borrow handler is PRIO_ORACLE and skip queue backlog"
            )

    top_f = feeders.most_common(1)[0][0] if feeders else None
    if top_f and top_f != "?":
        strategy.append(
            f"WATCHLIST feeder {top_f}: subscribe Transfer/cbXRP + Morpho Supply onBehalf of new wallets they fund"
        )
    strategy.append(
        "BUG/GAP vs our bot: seed GraphQL won't list brand-new dust; must rely on WS Borrow/Supply. "
        "If Morpho WS down (publicnode) during window -> never_saw. Harden WS failover."
    )
    strategy.append(
        "FAST PATH: on Borrow/SupplyCollateral for watch markets: enqueue priority=0, "
        "read_position once (or local if shares known), if HF<1 try_liq immediately with dust-sized prebuild template"
    )
    strategy.append(
        "Do NOT wait for hot-refresh 12s or book-scan. Tip war useless (winners ~0.002-0.15 gwei)."
    )
    strategy.append(
        "Optional: mempool watch of dust contracts 0x57fC / 0x1A98 calldata to snipe same victim earlier — harder on Base sequencer"
    )

    lines += ["", "=== WIN STRATEGY ==="] + [f"- {x}" for x in strategy]
    text = "\n".join(lines) + "\n"
    (OUT / "dust_aug24_sim.txt").write_text(text, encoding="utf-8")
    (OUT / "dust_aug24_sim.json").write_text(
        json.dumps({"summaries": summaries, "strategy": strategy}, indent=2, default=str),
        encoding="utf-8",
    )
    print("\nSTRATEGY:")
    for x in strategy:
        print("-", x.encode("ascii", "replace").decode("ascii"))
    print("wrote", OUT / "dust_aug24_sim.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
