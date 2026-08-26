#!/usr/bin/env python3
"""Competitor bot deep dive via Blockscout (avoid flaky free dRPC).

Reads prior competitor_bots_analysis.json event list if present, else VPS logs.
Writes morpho_research/out/competitor_bots_analysis.{json,txt}
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import dotenv_values
from web3 import Web3
import paramiko

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "morpho_research" / "out"
OUT.mkdir(parents=True, exist_ok=True)
BS = "https://base.blockscout.com/api/v2"
MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
UA = {"User-Agent": "morpho-competitor-recon/1"}

LIQ_TOPIC0 = "0x" + Web3.keccak(
    text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
).hex()
MORPHO_LIQ_SEL = "0x" + Web3.keccak(
    text="liquidate((address,address,address,address,uint256),address,uint256,uint256,bytes)"
)[:4].hex()

PAT = re.compile(
    r"(?P<ts>\d{2}:\d{2}:\d{2}).*?MORPHO LIQUIDATION #(?P<n>\d+)\s+(?P<pair>\S+)\s+\|\s+"
    r"liq=(?P<liq>0x[0-9a-fA-F]+)\s+user=(?P<user>0x[0-9a-fA-F]+)\s+"
    r"repaid=\d+\s+~\$?(?P<debt>[\d.]+)\s+profit~\$?(?P<profit>[\d.]+)\s+"
    r"net_profit_usd=\$?(?P<net>[\d.]+)\s+\|\s+block\s+(?P<block>\d+)\s+\|\s+tx\s+(?P<tx>\S+)"
)


def http_json(url: str) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        print(f"http_fail {url[:60]} {exc}")
        return None


def fetch_rows() -> list[dict]:
    cfg = dotenv_values(ROOT / ".env")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        (cfg.get("VPS_HOST") or "").strip(),
        username=(cfg.get("VPS_USER") or "root").strip(),
        password=(cfg.get("VPS_PASSWORD") or "").strip().strip('"'),
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    _, out, _ = c.exec_command(
        "grep 'MORPHO LIQUIDATION #' /root/aave-liquidation-monitor-v4/morpho_scanner.err.log",
        timeout=60,
    )
    raw = out.read().decode("utf-8", "replace")
    c.close()
    rows = []
    for line in raw.splitlines():
        m = PAT.search(line)
        if not m:
            continue
        tx = m.group("tx")
        if not tx.startswith("0x"):
            tx = "0x" + tx
        rows.append(
            {
                "ts": m.group("ts"),
                "pair": m.group("pair"),
                "liq": Web3.to_checksum_address(m.group("liq")),
                "user": Web3.to_checksum_address(m.group("user")),
                "debt": float(m.group("debt")),
                "profit": float(m.group("profit")),
                "block": int(m.group("block")),
                "tx": tx,
            }
        )
    return rows


def sel_label(sel: str, method: str | None = None) -> str:
    if method:
        return method
    s = (sel or "").lower()
    if s == MORPHO_LIQ_SEL.lower():
        return "Morpho.liquidate"
    known = {
        "0xac9650d8": "multicall(bytes[])",
        "0x5ae401dc": "multicall(uint256,bytes[])",
    }
    return known.get(s, f"sel {s}" if s else "?")


def addr_info(addr: str) -> dict:
    data = http_json(f"{BS}/addresses/{addr}")
    if not data or not isinstance(data, dict):
        return {"address": addr, "kind": "?", "name": None}
    return {
        "address": Web3.to_checksum_address(addr),
        "kind": "CONTRACT" if data.get("is_contract") else "EOA",
        "name": data.get("name") or (data.get("token") or {}).get("name"),
        "is_verified": bool(data.get("is_verified")),
        "proxy_type": data.get("proxy_type"),
        "implementations": data.get("implementations"),
    }


def tx_info(txh: str, liquidator: str) -> dict:
    data = http_json(f"{BS}/transactions/{txh}")
    out: dict = {"tx": txh}
    if not data or not isinstance(data, dict):
        out["error"] = "blockscout_miss"
        return out
    raw_input = data.get("raw_input") or "0x"
    sel = raw_input[:10].lower() if len(raw_input) >= 10 else ""
    to = ((data.get("to") or {}) if isinstance(data.get("to"), dict) else {}).get("hash") or data.get("to")
    frm = ((data.get("from") or {}) if isinstance(data.get("from"), dict) else {}).get("hash") or data.get("from")
    method = data.get("method")
    out.update(
        {
            "from": frm,
            "to": to,
            "selector": sel,
            "selector_label": sel_label(sel, method),
            "method": method,
            "input_bytes": max(0, (len(raw_input) - 2) // 2),
            "gas_limit": int(data.get("gas_limit") or 0),
            "gas_used": int(data.get("gas_used") or 0),
            "status": data.get("status"),
            "priority_fee": data.get("priority_fee"),
            "tx_types": data.get("transaction_types") or [],
            "to_is_morpho": bool(to and str(to).lower() == MORPHO),
            "to_is_liq_addr": bool(to and str(to).lower() == liquidator.lower()),
            "from_is_liq_addr": bool(frm and str(frm).lower() == liquidator.lower()),
        }
    )
    logs = http_json(f"{BS}/transactions/{txh}/logs")
    n_liq = 0
    if isinstance(logs, dict):
        items = logs.get("items") or []
    elif isinstance(logs, list):
        items = logs
    else:
        items = []
    for lg in items:
        topics = lg.get("topics") or []
        if topics and str(topics[0]).lower() == LIQ_TOPIC0.lower():
            n_liq += 1
    out["liq_events"] = n_liq
    return out


def main() -> int:
    print("fetching logs…")
    rows = fetch_rows()
    print(f"events={len(rows)}")
    by_liq: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_liq[r["liq"].lower()].append(r)

    profiles = []
    for addr_l, rs in sorted(by_liq.items(), key=lambda x: -sum(z["debt"] for z in x[1])):
        print(f"probe {addr_l[:12]}… n={len(rs)}")
        time.sleep(0.25)
        info = addr_info(addr_l)
        rs_sorted = sorted(rs, key=lambda r: -r["debt"])
        samples = []
        seen = set()
        for r in rs_sorted:
            if r["tx"] in seen:
                continue
            seen.add(r["tx"])
            samples.append(r)
            if len(samples) >= 2:
                break
        for r in rs:
            if r["debt"] < 50 and r["tx"] not in seen:
                samples.append(r)
                break

        txs = []
        for r in samples:
            time.sleep(0.35)
            t = tx_info(r["tx"], addr_l)
            t["sample_debt"] = r["debt"]
            t["sample_pair"] = r["pair"]
            t["sample_block"] = r["block"]
            txs.append(t)

        debts = [r["debt"] for r in rs]
        tier = "WHALE" if sum(debts) >= 10_000 else ("MID" if sum(debts) >= 500 else "DUST")
        tags = []
        for t in txs:
            if t.get("error"):
                tags.append("tx_fail")
                continue
            if t.get("to_is_morpho"):
                tags.append("direct_Morpho")
            if t.get("to_is_liq_addr") and info.get("kind") == "CONTRACT":
                tags.append("call_own_contract")
            if t.get("from_is_liq_addr") and info.get("kind") == "EOA":
                tags.append("EOA_sender")
            if (t.get("liq_events") or 0) > 1:
                tags.append(f"batch_x{t['liq_events']}")
            if t.get("selector_label"):
                tags.append(str(t["selector_label"]))
            if t.get("input_bytes", 0) > 2000:
                tags.append("fat_calldata")

        profiles.append(
            {
                "liquidator": info.get("address") or addr_l,
                "tier": tier,
                "kind": info.get("kind"),
                "name": info.get("name"),
                "verified": info.get("is_verified"),
                "n": len(rs),
                "debt": round(sum(debts), 2),
                "profit": round(sum(r["profit"] for r in rs), 2),
                "pairs": dict(Counter(r["pair"] for r in rs)),
                "debt_min": min(debts),
                "debt_max": max(debts),
                "tags": sorted(set(tags)),
                "txs": txs,
            }
        )

    by_block: dict[int, list] = defaultdict(list)
    for r in rows:
        by_block[r["block"]].append(r)
    cascades = []
    for b, rs in sorted(by_block.items(), key=lambda x: -sum(z["debt"] for z in x[1])):
        if len(rs) < 2:
            continue
        cascades.append(
            {
                "block": b,
                "n": len(rs),
                "debt": round(sum(r["debt"] for r in rs), 2),
                "liquidators": dict(Counter(r["liq"] for r in rs)),
            }
        )

    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_events": len(rows),
        "liquidators": profiles,
        "cascades": cascades[:10],
        "morpho_liq_selector": MORPHO_LIQ_SEL,
    }
    (OUT / "competitor_bots_analysis.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    lines = ["=== MORPHO COMPETITOR BOTS (полный разбор) ===", f"events={len(rows)} bots={len(profiles)}", ""]
    for p in profiles:
        lines.append(
            f"[{p['tier']}] {p['liquidator']}  {p['kind']}"
            + (f" name={p['name']}" if p.get("name") else "")
            + (f" verified={p.get('verified')}" if p.get("kind") == "CONTRACT" else "")
        )
        lines.append(
            f"  n={p['n']} debt=${p['debt']:,.0f} profit~${p['profit']:,.0f} "
            f"range=${p['debt_min']:.0f}..${p['debt_max']:.0f} pairs={p['pairs']}"
        )
        lines.append(f"  tags={p['tags']}")
        for t in p["txs"]:
            if t.get("error"):
                lines.append(f"  tx FAIL {t['tx'][:18]}")
                continue
            lines.append(
                f"  tx {t['tx'][:18]}… debt~${t['sample_debt']:.0f} "
                f"from={str(t.get('from') or '')[:12]} to={str(t.get('to') or '')[:12]} "
                f"method={t.get('selector_label')} gas={t.get('gas_used')} "
                f"liq_ev={t.get('liq_events')} in={t.get('input_bytes')}b"
            )
        lines.append("")

    lines.append("--- CASCADES (same block) ---")
    for c in cascades[:6]:
        lines.append(f"block {c['block']}: n={c['n']} ${c['debt']:,.0f}")
        for a, n in sorted(c["liquidators"].items(), key=lambda x: -x[1]):
            lines.append(f"   {a} ×{n}")

    lines += [
        "",
        "--- ВЫВОД ---",
        "1) Киты и пылесосы — РАЗНЫЕ адреса. Киты не жрут $16.",
        "2) Мясо cbXRP: 2–6 ботов в ОДНОМ блоке = oracle/prebuild гонка.",
        "3) Пыль $8–$16: отдельные мелкие боты, стабильный ским.",
        "4) Смотри tags: call_own_contract / direct_Morpho / batch_xN / fat_calldata.",
        "5) Наш never_saw остаётся: видим Liquidate после факта.",
    ]
    text = "\n".join(lines) + "\n"
    (OUT / "competitor_bots_analysis.txt").write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
