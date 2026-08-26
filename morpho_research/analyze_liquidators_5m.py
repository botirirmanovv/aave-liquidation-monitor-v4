#!/usr/bin/env python3
"""5-month Morpho Base liquidator competition — who took the meat.

Uses Morpho GraphQL liquidator field. Fetches in time windows to avoid
deep `skip` pagination holes in the API.
"""
from __future__ import annotations

import json
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
DAYS = 150  # ~5 months
WINDOW_SEC = 14 * 86400
WAD = 10**18
LLTV_CBXRP = 0.625
MEAT_USD = 1000.0


def lif(lltv: float = LLTV_CBXRP) -> float:
    return min(1.15, 1.0 / (0.3 * lltv + 0.7))


def gql(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://blue-api.morpho.org/graphql",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    if data.get("errors"):
        raise RuntimeError(str(data["errors"][:2]))
    return data["data"]


def fetch_window(ts_gte: int, ts_lt: int) -> list[dict]:
    """Fetch [ts_gte, ts_lt) via Asc + client cut (API has no reliable timestamp_lt)."""
    q = """
    query($gte: Int!, $skip: Int!) {
      marketTransactions(
        first: 100
        skip: $skip
        orderBy: Timestamp
        orderDirection: Asc
        where: {
          chainId_in: [8453]
          type_in: [Liquidation]
          timestamp_gte: $gte
        }
      ) {
        items {
          txHash
          timestamp
          logIndex
          data {
            ... on MarketTransactionLiquidationData {
              liquidator
              repaidAssets
              seizedAssets
            }
          }
          market {
            marketId
            lltv
            loanAsset { symbol decimals priceUsd }
            collateralAsset { symbol }
          }
        }
      }
    }
    """
    out: list[dict] = []
    skip = 0
    while skip < 5000:
        data = gql(q, {"gte": ts_gte, "skip": skip})
        items = (data.get("marketTransactions") or {}).get("items") or []
        if not items:
            break
        stop = False
        for it in items:
            ts = int(it["timestamp"])
            if ts >= ts_lt:
                stop = True
                break
            out.append(it)
        if stop or len(items) < 100:
            break
        skip += 100
        time.sleep(0.05)
    return out


def fetch_liquidations(since: int, until: int | None = None) -> list[dict]:
    until = until or int(time.time()) + 60
    # Dedup key: tx + logIndex + repaid (logIndex may be absent on some)
    seen: set[str] = set()
    out: list[dict] = []
    t0 = since
    while t0 < until:
        t1 = min(t0 + WINDOW_SEC, until)
        chunk = fetch_window(t0, t1)
        for it in chunk:
            data = it.get("data") or {}
            key = (
                f"{it.get('txHash')}|{it.get('logIndex')}|"
                f"{data.get('repaidAssets')}|{it.get('timestamp')}"
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        print(
            f"\r  window {datetime.fromtimestamp(t0, tz=timezone.utc).date()}->"
            f"{datetime.fromtimestamp(t1, tz=timezone.utc).date()} "
            f"+{len(chunk)} total={len(out)}...",
            end="",
            flush=True,
        )
        t0 = t1
        time.sleep(0.08)
    print()
    out.sort(key=lambda x: (int(x["timestamp"]), int(x.get("logIndex") or 0)))
    return out


def short(addr: str) -> str:
    a = addr or "?"
    if len(a) < 12:
        return a
    return f"{a[:6]}…{a[-4:]}"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    since = now - DAYS * 86400
    print(f"Fetching Morpho Base liquidations since {since} (~{DAYS}d)…")
    items = fetch_liquidations(since)
    print(f"API liquidations: {len(items)}")

    rows: list[dict] = []
    for it in items:
        m = it["market"]
        loan = m["loanAsset"]
        data = it.get("data") or {}
        repaid = int(data.get("repaidAssets") or 0)
        dec = int(loan.get("decimals") or 6)
        px = float(loan.get("priceUsd") or 1.0)
        debt = (repaid / (10**dec)) * px
        lltv_raw = float(m.get("lltv") or 0)
        lltv = lltv_raw / WAD if lltv_raw > 2 else lltv_raw
        edge = debt * max(0.0, lif(lltv) - 1.0)
        liq = (data.get("liquidator") or "").strip()
        if liq and not liq.startswith("0x"):
            liq = "0x" + liq
        # checksum-preserving lower for aggregation
        liq_key = liq.lower() if liq else ""
        pair = f"{loan['symbol']}/{m['collateralAsset']['symbol']}"
        rows.append(
            {
                "tx": it["txHash"],
                "ts": int(it["timestamp"]),
                "pair": pair,
                "debt": debt,
                "edge": edge,
                "lltv": lltv,
                "liquidator": liq_key,
                "liquidator_raw": liq,
            }
        )

    by_liq_debt: dict[str, float] = defaultdict(float)
    by_liq_edge: dict[str, float] = defaultdict(float)
    by_liq_n: dict[str, int] = defaultdict(int)
    by_liq_meat: dict[str, float] = defaultdict(float)
    by_liq_meat_edge: dict[str, float] = defaultdict(float)
    by_liq_pair: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_liq_sizes: dict[str, list[float]] = defaultdict(list)
    display: dict[str, str] = {}
    unmatched_debt = 0.0
    matched = 0

    for r in rows:
        liq = r["liquidator"]
        if not liq:
            unmatched_debt += r["debt"]
            continue
        matched += 1
        display[liq] = r["liquidator_raw"] or liq
        by_liq_debt[liq] += r["debt"]
        by_liq_edge[liq] += r["edge"]
        by_liq_n[liq] += 1
        by_liq_pair[liq][r["pair"]] += r["debt"]
        by_liq_sizes[liq].append(r["debt"])
        if r["debt"] >= MEAT_USD:
            by_liq_meat[liq] += r["debt"]
            by_liq_meat_edge[liq] += r["edge"]

    total_debt = sum(r["debt"] for r in rows)
    total_edge = sum(r["edge"] for r in rows)
    meat_rows = [r for r in rows if r["debt"] >= MEAT_USD]
    meat_debt = sum(r["debt"] for r in meat_rows)
    meat_edge = sum(r["edge"] for r in meat_rows)

    ranked_meat = sorted(by_liq_meat.items(), key=lambda x: -x[1])
    ranked = sorted(by_liq_debt.items(), key=lambda x: -x[1])
    top2 = ranked_meat[:2] if ranked_meat else ranked[:2]
    top2_set = {a for a, _ in top2}
    top2_debt = sum(by_liq_debt[a] for a in top2_set)
    top2_edge = sum(by_liq_edge[a] for a in top2_set)
    top2_meat = sum(by_liq_meat[a] for a in top2_set)
    top2_meat_edge = sum(by_liq_meat_edge[a] for a in top2_set)

    if rows:
        tmin = datetime.fromtimestamp(min(r["ts"] for r in rows), tz=timezone.utc)
        tmax = datetime.fromtimestamp(max(r["ts"] for r in rows), tz=timezone.utc)
        span = f"{tmin.date()} -> {tmax.date()}"
    else:
        span = "empty"

    lines: list[str] = []
    lines.append(f"=== Morpho Base liquidators ~{DAYS}d (~5 months) ===")
    lines.append(f"generated: {datetime.now(tz=timezone.utc).isoformat()}")
    lines.append(f"span: {span}")
    lines.append(
        f"liquidations={len(rows)} with_liquidator={matched} "
        f"missing_liq_debt=${unmatched_debt:,.0f}"
    )
    lines.append(f"total debt=${total_debt:,.0f} est_edge=${total_edge:,.0f}")
    lines.append(
        f"MEAT (debt>=${MEAT_USD:,.0f}): n={len(meat_rows)} "
        f"debt=${meat_debt:,.0f} est_edge=${meat_edge:,.0f} "
        f"({100 * meat_debt / max(total_debt, 1):.1f}% of all debt)"
    )
    lines.append("")
    lines.append("--- TOP by MEAT debt ---")
    for i, (addr, meat) in enumerate(ranked_meat[:10], 1):
        shown = display.get(addr, addr)
        debt = by_liq_debt[addr]
        edge = by_liq_edge[addr]
        pairs = sorted(by_liq_pair[addr].items(), key=lambda x: -x[1])[:3]
        pair_s = ", ".join(f"{p}=${d:,.0f}" for p, d in pairs)
        sizes = sorted(by_liq_sizes[addr], reverse=True)
        top_hits = ", ".join(f"${s:,.0f}" for s in sizes[:5])
        lines.append(
            f"#{i} {shown}\n"
            f"   n={by_liq_n[addr]} debt=${debt:,.0f} ({100 * debt / max(total_debt, 1):.1f}%) "
            f"est_edge=${edge:,.0f}\n"
            f"   meat>=${MEAT_USD:,.0f}: ${meat:,.0f} "
            f"({100 * meat / max(meat_debt, 1):.1f}% of meat) "
            f"meat_edge=${by_liq_meat_edge[addr]:,.0f}\n"
            f"   top pairs: {pair_s}\n"
            f"   largest hits: {top_hits}"
        )

    lines.append("")
    lines.append("--- TOP-2 meat hunters ---")
    for addr, meat in top2:
        shown = display.get(addr, addr)
        lines.append(
            f"  {shown}\n"
            f"    n={by_liq_n[addr]} debt=${by_liq_debt[addr]:,.0f} "
            f"est_edge=${by_liq_edge[addr]:,.0f} meat=${meat:,.0f} "
            f"meat_edge=${by_liq_meat_edge[addr]:,.0f}"
        )
    lines.append(
        f"TOP2 combined: debt=${top2_debt:,.0f} "
        f"({100 * top2_debt / max(total_debt, 1):.1f}%) "
        f"est_edge=${top2_edge:,.0f} | "
        f"meat=${top2_meat:,.0f} ({100 * top2_meat / max(meat_debt, 1):.1f}% of meat) "
        f"meat_edge=${top2_meat_edge:,.0f}"
    )
    rest_meat = max(0.0, meat_debt - top2_meat)
    rest_meat_edge = max(0.0, meat_edge - top2_meat_edge)
    lines.append(
        f"EVERYONE ELSE meat: ${rest_meat:,.0f} est_edge=${rest_meat_edge:,.0f}"
    )

    lines.append("")
    lines.append("--- Size buckets (who wins) ---")
    buckets = [
        (0, 100, "<$100"),
        (100, 500, "$100-500"),
        (500, 1000, "$500-1k"),
        (1000, 5000, "$1k-5k"),
        (5000, 25000, "$5k-25k"),
        (25000, 1e18, ">$25k"),
    ]
    for lo, hi, label in buckets:
        subset = [r for r in rows if lo <= r["debt"] < hi and r["liquidator"]]
        if not subset:
            continue
        d = sum(r["debt"] for r in subset)
        e = sum(r["edge"] for r in subset)
        t2d = sum(r["debt"] for r in subset if r["liquidator"] in top2_set)
        lines.append(
            f"  {label}: n={len(subset)} debt=${d:,.0f} edge=${e:,.0f} "
            f"TOP2={100 * t2d / max(d, 1e-9):.0f}%"
        )

    lines.append("")
    lines.append("--- TOP2 share by month ---")
    by_month: dict[str, dict] = defaultdict(
        lambda: {
            "debt": 0.0,
            "top2": 0.0,
            "n": 0,
            "meat": 0.0,
            "top2_meat": 0.0,
            "edge": 0.0,
            "top2_edge": 0.0,
        }
    )
    for r in rows:
        mk = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m")
        by_month[mk]["debt"] += r["debt"]
        by_month[mk]["n"] += 1
        by_month[mk]["edge"] += r["edge"]
        if r["debt"] >= MEAT_USD:
            by_month[mk]["meat"] += r["debt"]
        if r["liquidator"] in top2_set:
            by_month[mk]["top2"] += r["debt"]
            by_month[mk]["top2_edge"] += r["edge"]
            if r["debt"] >= MEAT_USD:
                by_month[mk]["top2_meat"] += r["debt"]

    for mk in sorted(by_month):
        m = by_month[mk]
        share = 100 * m["top2"] / max(m["debt"], 1e-9)
        mshare = 100 * m["top2_meat"] / max(m["meat"], 1e-9)
        lines.append(
            f"  {mk}: n={m['n']} debt=${m['debt']:,.0f} edge=${m['edge']:,.0f} "
            f"TOP2={share:.0f}% meat_TOP2={mshare:.0f}% "
            f"(meat=${m['meat']:,.0f})"
        )

    lines.append("")
    lines.append("--- Each TOP2 bot by month (meat debt) ---")
    for addr, _ in top2:
        parts = []
        for mk in sorted(by_month):
            md = sum(
                r["debt"]
                for r in rows
                if r["liquidator"] == addr
                and r["debt"] >= MEAT_USD
                and datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m")
                == mk
            )
            if md > 0:
                parts.append(f"{mk}=${md:,.0f}")
        lines.append(
            f"  {short(display.get(addr, addr))}: "
            + (", ".join(parts) if parts else "none")
        )

    # Recent specialists (last 30d meat) for context vs historical whales
    cut = now - 30 * 86400
    recent = [r for r in rows if r["ts"] >= cut and r["debt"] >= MEAT_USD]
    recent_by: dict[str, float] = defaultdict(float)
    for r in recent:
        if r["liquidator"]:
            recent_by[r["liquidator"]] += r["debt"]
    lines.append("")
    lines.append("--- Last 30d meat leaders (may differ from 5m) ---")
    for i, (addr, meat) in enumerate(sorted(recent_by.items(), key=lambda x: -x[1])[:5], 1):
        lines.append(
            f"  #{i} {display.get(addr, addr)} meat=${meat:,.0f} "
            f"n={sum(1 for r in recent if r['liquidator']==addr)}"
        )

    text = "\n".join(lines)
    print()
    print(text)
    path = OUT / f"liquidators_base_{DAYS}d.txt"
    path.write_text(text, encoding="utf-8")
    payload = {
        "days": DAYS,
        "span": span,
        "total_debt": total_debt,
        "total_edge": total_edge,
        "meat_debt": meat_debt,
        "meat_edge": meat_edge,
        "ranked_meat": [
            {
                "address": display.get(a, a),
                "debt": by_liq_debt[a],
                "edge": by_liq_edge[a],
                "n": by_liq_n[a],
                "meat": by_liq_meat[a],
                "meat_edge": by_liq_meat_edge[a],
                "pairs": dict(by_liq_pair[a]),
            }
            for a, _ in ranked_meat[:20]
        ],
        "top2": [
            {
                "address": display.get(a, a),
                "debt": by_liq_debt[a],
                "edge": by_liq_edge[a],
                "meat": m,
                "meat_edge": by_liq_meat_edge[a],
                "n": by_liq_n[a],
            }
            for a, m in top2
        ],
    }
    (OUT / f"liquidators_base_{DAYS}d.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
