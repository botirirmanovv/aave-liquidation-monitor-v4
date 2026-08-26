#!/usr/bin/env python3
"""Morpho Blue liquidation backtest / counterfactual (read-only).

Pulls historical Liquidation txs from Morpho GraphQL, scores incentive edge,
finds cascades, and estimates capture under three observation stacks:

  baseline   — event-only eval, ~900 seed backlog (pre Stage-1)
  stage1     — hot-set + 2s oracle poll + priority queue (current code)
  stage1_max — stage1 + batch multicall + 1s p1 poll + race-ready (this pass)

Capture fractions are explicit assumptions (not guarantees). Output:
  morpho_research/out/backtest_{chain}_{days}d.json
  morpho_research/out/backtest_summary.txt

Usage (repo root):
  .venv-run/Scripts/python.exe morpho_research/morpho_backtest.py
  .venv-run/Scripts/python.exe morpho_research/morpho_backtest.py --days 30 --chains base
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
for p in (_DIR, _ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from morpho_markets import MORPHO_MARKETS, markets_for_chain  # noqa: E402

GRAPHQL_URLS = (
    "https://blue-api.morpho.org/graphql",
    "https://api.morpho.org/graphql",
)
WAD = 10**18
LIF_CURSOR = 0.3
LIF_MAX = 1.15
CHAIN_IDS = {"base": 8453, "arbitrum": 42161}
OUT_DIR = _DIR / "out"

# Explicit capture assumptions (share of est LIF edge we could win if we saw it).
CAPTURE = {
    "baseline": {
        # Almost never saw before foreign liq with 600+ queue.
        "see_rate": 0.02,
        "win_uncontested": 0.25,
        "win_contested": 0.02,
    },
    "stage1": {
        "see_rate": 0.75,  # hot + oracle poll catches price-driven HF crosses
        "win_uncontested": 0.35,
        "win_contested": 0.08,
    },
    "stage1_max": {
        "see_rate": 0.90,
        "win_uncontested": 0.45,
        "win_contested": 0.12,
    },
}


@dataclass
class LiqEvent:
    chain: str
    ts: int
    tx: str
    user: str
    market_id: str
    pair: str
    lltv: float
    repaid_usd: float
    seized_usd: float
    est_profit_usd: float
    in_watchlist: bool


@dataclass
class Cascade:
    chain: str
    start_ts: int
    end_ts: int
    n: int
    debt_usd: float
    profit_usd: float
    markets: list[str]
    users: list[str]
    txs: list[str]
    contested: bool


@dataclass
class StrategyResult:
    name: str
    see_rate: float
    win_uncontested: float
    win_contested: float
    expected_see_debt_usd: float = 0.0
    expected_see_profit_usd: float = 0.0
    expected_capture_usd: float = 0.0
    monthly_capture_usd: float = 0.0
    notes: str = ""


@dataclass
class ChainBacktest:
    chain: str
    days: float
    total_liq: int = 0
    actionable_liq: int = 0
    watch_liq: int = 0
    debt_usd: float = 0.0
    profit_usd: float = 0.0
    watch_debt_usd: float = 0.0
    watch_profit_usd: float = 0.0
    by_pair: dict[str, dict[str, float]] = field(default_factory=dict)
    cascades: list[Cascade] = field(default_factory=list)
    top_liquidators_proxy: dict[str, int] = field(default_factory=dict)
    strategies: list[StrategyResult] = field(default_factory=list)


def _graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    body = json.dumps(payload).encode()
    last: Exception | None = None
    for url in GRAPHQL_URLS:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode())
            if data.get("errors"):
                raise RuntimeError(str(data["errors"][:2]))
            return data["data"]
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"Morpho GraphQL unavailable: {last}")


def lif(lltv: float) -> float:
    if lltv <= 0:
        return 1.0
    return min(LIF_MAX, 1.0 / (LIF_CURSOR * lltv + (1.0 - LIF_CURSOR)))


def fetch_liquidations(chain_id: int, since_ts: int) -> list[dict[str, Any]]:
    query = """
    query($chainId: Int!, $ts: Int!, $first: Int!, $skip: Int!) {
      marketTransactions(
        first: $first
        skip: $skip
        orderBy: Timestamp
        orderDirection: Desc
        where: {
          chainId_in: [$chainId]
          type_in: [Liquidation]
          timestamp_gte: $ts
        }
      ) {
        items {
          txHash
          timestamp
          user { address }
          data {
            ... on MarketTransactionLiquidationData {
              seizedAssets
              repaidAssets
              badDebtAssets
            }
          }
          market {
            marketId
            lltv
            loanAsset { symbol decimals priceUsd }
            collateralAsset { symbol decimals priceUsd }
          }
        }
      }
    }
    """
    out: list[dict[str, Any]] = []
    skip = 0
    page = 100
    while True:
        data = _graphql(
            query,
            {"chainId": chain_id, "ts": since_ts, "first": page, "skip": skip},
        )
        items = (data.get("marketTransactions") or {}).get("items") or []
        if not items:
            break
        out.extend(items)
        if len(items) < page:
            break
        skip += page
        if skip > 15_000:
            break
        time.sleep(0.15)
    return out


def parse_events(chain: str, raw: list[dict[str, Any]], watch: set[str]) -> list[LiqEvent]:
    events: list[LiqEvent] = []
    for item in raw:
        market = item.get("market") or {}
        mid = str(market.get("marketId") or "").lower()
        loan = market.get("loanAsset") or {}
        coll = market.get("collateralAsset") or {}
        data = item.get("data") or {}
        try:
            lltv_raw = float(market.get("lltv") or 0)
            lltv = lltv_raw / WAD if lltv_raw > 2 else lltv_raw
            loan_dec = int(loan.get("decimals") or 6)
            coll_dec = int(coll.get("decimals") or 18)
            loan_px = float(loan.get("priceUsd") or 1.0)
            coll_px = float(coll.get("priceUsd") or 0.0)
            repaid = int(data.get("repaidAssets") or 0)
            seized = int(data.get("seizedAssets") or 0)
        except (TypeError, ValueError):
            continue
        repaid_usd = (repaid / (10**loan_dec)) * loan_px
        seized_usd = (seized / (10**coll_dec)) * coll_px if coll_px else 0.0
        incentive = lif(lltv)
        # Prefer LIF theoretical edge; clamp with seized-repaid when both present.
        theo = repaid_usd * max(0.0, incentive - 1.0)
        observed = max(0.0, seized_usd - repaid_usd) if seized_usd > 0 else theo
        est = min(theo, observed) if seized_usd > repaid_usd > 0 else theo
        user = ((item.get("user") or {}).get("address")) or ""
        pair = f"{loan.get('symbol')}/{coll.get('symbol')}"
        events.append(
            LiqEvent(
                chain=chain,
                ts=int(item.get("timestamp") or 0),
                tx=str(item.get("txHash") or ""),
                user=user,
                market_id=mid,
                pair=pair,
                lltv=lltv,
                repaid_usd=repaid_usd,
                seized_usd=seized_usd,
                est_profit_usd=est,
                in_watchlist=mid in watch,
            )
        )
    events.sort(key=lambda e: e.ts)
    return events


def build_cascades(events: list[LiqEvent], gap_sec: int = 30) -> list[Cascade]:
    """Group actionable watchlist liqs into cascades by time proximity."""
    rows = [e for e in events if e.in_watchlist and e.repaid_usd >= 100]
    if not rows:
        return []
    cascades: list[Cascade] = []
    cur: list[LiqEvent] = [rows[0]]
    for e in rows[1:]:
        if e.ts - cur[-1].ts <= gap_sec:
            cur.append(e)
        else:
            cascades.append(_cascade_from(cur))
            cur = [e]
    cascades.append(_cascade_from(cur))
    return cascades


def _cascade_from(rows: list[LiqEvent]) -> Cascade:
    users = list(dict.fromkeys(e.user for e in rows if e.user))
    txs = list(dict.fromkeys(e.tx for e in rows if e.tx))
    markets = list(dict.fromkeys(e.pair for e in rows))
    # Contested if multiple txs or multiple borrowers in a tight window.
    contested = len(txs) >= 2 or len(users) >= 3 or len(rows) >= 4
    return Cascade(
        chain=rows[0].chain,
        start_ts=rows[0].ts,
        end_ts=rows[-1].ts,
        n=len(rows),
        debt_usd=sum(e.repaid_usd for e in rows),
        profit_usd=sum(e.est_profit_usd for e in rows),
        markets=markets,
        users=users,
        txs=txs,
        contested=contested,
    )


def score_strategies(
    events: list[LiqEvent], cascades: list[Cascade], days: float
) -> list[StrategyResult]:
    """Expected capture = see_rate * win_rate * est_profit, by cascade contest."""
    # Attribute each actionable watch liq to cascade contest flag.
    cascade_by_tx: dict[str, Cascade] = {}
    for c in cascades:
        for tx in c.txs:
            cascade_by_tx[tx] = c

    actionable = [
        e for e in events if e.in_watchlist and e.repaid_usd >= 100 and e.est_profit_usd > 0
    ]
    results: list[StrategyResult] = []
    month_scale = 30.0 / max(days, 0.1)

    for name, params in CAPTURE.items():
        see = params["see_rate"]
        wu = params["win_uncontested"]
        wc = params["win_contested"]
        see_debt = 0.0
        see_profit = 0.0
        capture = 0.0
        for e in actionable:
            c = cascade_by_tx.get(e.tx)
            contested = c.contested if c is not None else (e.est_profit_usd >= 200)
            win = wc if contested else wu
            see_debt += see * e.repaid_usd
            see_profit += see * e.est_profit_usd
            capture += see * win * e.est_profit_usd
        results.append(
            StrategyResult(
                name=name,
                see_rate=see,
                win_uncontested=wu,
                win_contested=wc,
                expected_see_debt_usd=see_debt,
                expected_see_profit_usd=see_profit,
                expected_capture_usd=capture,
                monthly_capture_usd=capture * month_scale,
                notes=(
                    f"see={see:.0%} win_u={wu:.0%} win_c={wc:.0%} "
                    f"over {len(actionable)} actionable watch liqs"
                ),
            )
        )
    return results


def run_chain(chain: str, days: float, min_debt: float) -> ChainBacktest:
    watch = {m.market_id.lower() for m in markets_for_chain(chain)}
    since = int(time.time()) - int(days * 86400)
    print(f"[{chain}] fetching liquidations since {since} ({days}d)…")
    raw = fetch_liquidations(CHAIN_IDS[chain], since)
    events = parse_events(chain, raw, watch)
    bt = ChainBacktest(chain=chain, days=days)
    bt.total_liq = len(events)

    by_pair: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n": 0, "debt": 0.0, "profit": 0.0, "watch_n": 0}
    )
    user_counts: Counter[str] = Counter()

    for e in events:
        bt.debt_usd += e.repaid_usd
        bt.profit_usd += e.est_profit_usd
        bucket = by_pair[e.pair]
        bucket["n"] += 1
        bucket["debt"] += e.repaid_usd
        bucket["profit"] += e.est_profit_usd
        if e.user:
            user_counts[e.user.lower()] += 1
        if e.repaid_usd >= min_debt and e.est_profit_usd > 0:
            bt.actionable_liq += 1
        if e.in_watchlist:
            bt.watch_liq += 1
            bt.watch_debt_usd += e.repaid_usd
            bt.watch_profit_usd += e.est_profit_usd
            bucket["watch_n"] += 1

    bt.by_pair = {
        k: {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()}
        for k, v in sorted(by_pair.items(), key=lambda kv: -kv[1]["profit"])
    }
    bt.top_liquidators_proxy = dict(user_counts.most_common(10))
    bt.cascades = build_cascades(events)
    bt.strategies = score_strategies(events, bt.cascades, days)
    return bt


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def render_summary(reports: list[ChainBacktest]) -> str:
    lines: list[str] = []
    lines.append("=== Morpho liquidation backtest ===")
    lines.append(f"generated: {datetime.now(tz=timezone.utc).isoformat()}")
    lines.append("")
    for bt in reports:
        lines.append(f"## {bt.chain} ({bt.days:.0f}d)")
        lines.append(
            f"total liq={bt.total_liq} actionable(>=$100)={bt.actionable_liq} "
            f"watchlist={bt.watch_liq}"
        )
        lines.append(
            f"all debt=${bt.debt_usd:,.0f} est_edge=${bt.profit_usd:,.2f} | "
            f"watch debt=${bt.watch_debt_usd:,.0f} watch_edge=${bt.watch_profit_usd:,.2f}"
        )
        lines.append("top pairs:")
        for i, (pair, s) in enumerate(list(bt.by_pair.items())[:8]):
            lines.append(
                f"  {i+1}. {pair}: n={int(s['n'])} debt=${s['debt']:,.0f} "
                f"edge=${s['profit']:,.2f} watch_n={int(s['watch_n'])}"
            )
        big = [c for c in bt.cascades if c.n >= 3 or c.profit_usd >= 200]
        lines.append(
            f"cascades(gap<=30s): {len(bt.cascades)} total, "
            f"{len(big)} notable (n>=3 or edge>=$200)"
        )
        for c in sorted(big, key=lambda x: -x.profit_usd)[:5]:
            lines.append(
                f"  • {_fmt_ts(c.start_ts)} n={c.n} debt=${c.debt_usd:,.0f} "
                f"edge=${c.profit_usd:,.2f} contested={c.contested} "
                f"markets={','.join(c.markets)}"
            )
        lines.append("strategy expected capture (assumptions in CAPTURE):")
        for s in bt.strategies:
            lines.append(
                f"  [{s.name}] see_edge=${s.expected_see_profit_usd:,.2f} "
                f"capture~${s.expected_capture_usd:,.2f} "
                f"(~${s.monthly_capture_usd:,.0f}/mo) | {s.notes}"
            )
        lines.append("")

    # Combined monthly outlook (base+arb stage1_max)
    monthly = 0.0
    for bt in reports:
        for s in bt.strategies:
            if s.name == "stage1_max":
                monthly += s.monthly_capture_usd
    lines.append(
        f"Combined stage1_max expected ~ ${monthly:,.0f}/mo "
        f"(observation edge share - not execution guarantee)"
    )
    lines.append(
        "Goal $3000/mo needs higher win_rate (private RPC / contract / less competition) "
        f"or more markets - gap ~ ${max(0.0, 3000.0 - monthly):,.0f}/mo"
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Morpho liq backtest")
    ap.add_argument("--chains", default="base,arbitrum")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--min-debt", type=float, default=100.0)
    args = ap.parse_args()
    chains = [c.strip().lower() for c in args.chains.split(",") if c.strip()]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    reports: list[ChainBacktest] = []
    for chain in chains:
        if chain not in CHAIN_IDS:
            print(f"skip unknown chain {chain}")
            continue
        bt = run_chain(chain, args.days, args.min_debt)
        reports.append(bt)
        out = OUT_DIR / f"backtest_{chain}_{int(args.days)}d.json"
        payload = {
            "chain": bt.chain,
            "days": bt.days,
            "total_liq": bt.total_liq,
            "actionable_liq": bt.actionable_liq,
            "watch_liq": bt.watch_liq,
            "debt_usd": round(bt.debt_usd, 2),
            "profit_usd": round(bt.profit_usd, 2),
            "watch_debt_usd": round(bt.watch_debt_usd, 2),
            "watch_profit_usd": round(bt.watch_profit_usd, 2),
            "by_pair": bt.by_pair,
            "top_users_in_liq_feed": bt.top_liquidators_proxy,
            "cascades": [asdict(c) for c in bt.cascades],
            "strategies": [asdict(s) for s in bt.strategies],
            "capture_assumptions": CAPTURE,
            "watch_markets": [m.market_id for m in markets_for_chain(chain)],
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"[{chain}] wrote {out}")

    summary = render_summary(reports)
    summary_path = OUT_DIR / "backtest_summary.txt"
    summary_path.write_text(summary, encoding="utf-8")
    print()
    print(summary)
    print(f"\nsummary -> {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
