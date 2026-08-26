#!/usr/bin/env python3
"""Morpho Blue liquidation analysis (read-only GraphQL).

Daily/monthly counts and USD by market pair; frequency stats for strategy.

Usage (repo root):
  .venv-run/Scripts/python.exe tools/morpho_liq_analysis.py
  .venv-run/Scripts/python.exe tools/morpho_liq_analysis.py --days 90 --markets ours
  .venv-run/Scripts/python.exe tools/morpho_liq_analysis.py --days 60 --markets all --group day
  .venv-run/Scripts/python.exe tools/morpho_liq_analysis.py --pair USDC/KTA --days 365

Writes: morpho_research/out/liq_analysis_{chain}_{days}d.json
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DIR = ROOT / "morpho_research"
for p in (_DIR, ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from morpho_markets import markets_for_chain  # noqa: E402

GRAPHQL_URLS = (
    "https://blue-api.morpho.org/graphql",
    "https://api.morpho.org/graphql",
)
CHAIN_IDS = {"base": 8453, "arbitrum": 42161}
OUT_DIR = _DIR / "out"
TZ = timezone(timedelta(hours=5))

OUR_PAIRS_DEFAULT = ("USDC/cbXRP", "USDC/WETH", "USDC/yoUSD")

LIQ_QUERY = """
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
      data {
        ... on MarketTransactionLiquidationData { repaidAssets }
      }
      market {
        marketId
        loanAsset { symbol decimals priceUsd }
        collateralAsset { symbol }
      }
    }
  }
}
"""


@dataclass
class LiqRow:
    ts: int
    when: str
    day: str
    month: str
    pair: str
    market_id: str
    usd: float
    tx: str


@dataclass
class PairStats:
    pair: str
    market_id: str
    first_day: str
    last_day: str
    span_days: int
    active_days: int
    pct_active: float
    total_count: int
    total_usd: float
    meat_count: int
    meat_usd: float
    recent_active_days: int
    recent_pct: float
    recent_count: int
    recent_usd: float


@dataclass
class AnalysisReport:
    chain: str
    since_iso: str
    until_iso: str
    dust_usd: float
    markets_filter: str
    group: str
    total_fetched: int
    total_matched: int
    global_by_period: dict[str, dict[str, float | int]]
    by_pair: list[PairStats] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    near_daily_meat: list[dict] = field(default_factory=list)


def _graphql(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
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
    raise RuntimeError(f"Morpho GraphQL failed: {last}")


def fetch_liquidations(chain_id: int, since_ts: int) -> list[dict]:
    out: list[dict] = []
    skip = 0
    while True:
        data = _graphql(
            LIQ_QUERY,
            {"chainId": chain_id, "ts": since_ts, "first": 100, "skip": skip},
        )
        batch = data["marketTransactions"]["items"]
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        skip += 100
    return out


def pair_label(item: dict) -> tuple[str, str, float]:
    m = item.get("market") or {}
    loan = m.get("loanAsset") or {}
    coll = (m.get("collateralAsset") or {}).get("symbol") or "?"
    lsym = loan.get("symbol") or "?"
    pair = f"{lsym}/{coll}"
    mid = (m.get("marketId") or "").lower()
    dec = int(loan.get("decimals") or 6)
    px = float(loan.get("priceUsd") or 1.0)
    raw = int((item.get("data") or {}).get("repaidAssets") or 0)
    return pair, mid, raw / (10**dec) * px


def parse_markets_filter(spec: str, chain: str) -> set[str] | None:
    if spec == "all":
        return None
    if spec == "ours":
        try:
            from dotenv import dotenv_values

            cfg = dotenv_values(ROOT / ".env")
            raw = (cfg.get("MORPHO_ALLOWED_MARKETS") or cfg.get(f"{chain.upper()}_MORPHO_ALLOWED_MARKETS") or "")
            if raw.strip():
                return {p.strip() for p in raw.split(",") if p.strip()}
        except Exception:  # noqa: BLE001
            pass
        return set(OUR_PAIRS_DEFAULT)
    return {p.strip() for p in spec.split(",") if p.strip()}


def build_report(
    chain: str,
    days: int,
    dust: float,
    markets_filter: set[str] | None,
    filter_label: str,
    group: str,
    include_events: bool,
    recent_window: int,
) -> AnalysisReport:
    chain_id = CHAIN_IDS[chain]
    until = datetime.now(TZ)
    since = until - timedelta(days=days)
    since_ts = int(since.timestamp())

    raw = fetch_liquidations(chain_id, since_ts)
    rows: list[LiqRow] = []
    for item in raw:
        pair, mid, usd = pair_label(item)
        if markets_filter is not None and pair not in markets_filter:
            continue
        ts = int(item["timestamp"])
        dt = datetime.fromtimestamp(ts, tz=TZ)
        rows.append(
            LiqRow(
                ts=ts,
                when=dt.isoformat(),
                day=dt.strftime("%Y-%m-%d"),
                month=dt.strftime("%Y-%m"),
                pair=pair,
                market_id=mid,
                usd=usd,
                tx=item.get("txHash") or "",
            )
        )
    rows.sort(key=lambda r: r.ts)

    period_key = lambda r: r.day if group == "day" else r.month  # noqa: E731
    global_by: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"count": 0, "sum_usd": 0.0, "meat_count": 0, "meat_usd": 0.0, "pairs": 0}
    )
    pair_periods: dict[str, dict[str, dict[str, float | int]]] = defaultdict(
        lambda: defaultdict(lambda: {"count": 0, "sum": 0.0, "meat_count": 0, "meat_sum": 0.0})
    )
    pair_mid: dict[str, str] = {}

    for r in rows:
        pk = period_key(r)
        g = global_by[pk]
        g["count"] = int(g["count"]) + 1
        g["sum_usd"] = float(g["sum_usd"]) + r.usd
        pp = pair_periods[r.pair][pk]
        pp["count"] += 1
        pp["sum"] += r.usd
        pair_mid[r.pair] = r.market_id
        if r.usd >= dust:
            g["meat_count"] = int(g["meat_count"]) + 1
            g["meat_usd"] = float(g["meat_usd"]) + r.usd
            pp["meat_count"] += 1
            pp["meat_sum"] += r.usd

    # pair count per period for global
    period_pairs: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        period_pairs[period_key(r)].add(r.pair)
    for pk, pairs in period_pairs.items():
        global_by[pk]["pairs"] = len(pairs)

    recent_cut = (until.date() - timedelta(days=recent_window))
    pair_stats: list[PairStats] = []
    for pair, periods in pair_periods.items():
        days_sorted = sorted(periods.keys())
        first = days_sorted[0]
        last = days_sorted[-1]
        if group == "day":
            span = (datetime.strptime(last, "%Y-%m-%d").date() - datetime.strptime(first, "%Y-%m-%d").date()).days + 1
            recent_keys = [
                d for d in days_sorted
                if datetime.strptime(d, "%Y-%m-%d").date() >= recent_cut
            ]
        else:
            span = max(len(days_sorted) * 30, 1)
            recent_keys = days_sorted[-min(3, len(days_sorted)) :]

        meat_days = [d for d, v in periods.items() if v["meat_count"] > 0]
        active = len(days_sorted)
        total_n = sum(v["count"] for v in periods.values())
        total_usd = sum(v["sum"] for v in periods.values())
        meat_n = sum(v["meat_count"] for v in periods.values())
        meat_usd = sum(v["meat_sum"] for v in periods.values())
        recent_active = len(recent_keys) if group == "day" else len(meat_days)
        recent_n = sum(periods[d]["count"] for d in recent_keys)
        recent_usd = sum(periods[d]["sum"] for d in recent_keys)

        pair_stats.append(
            PairStats(
                pair=pair,
                market_id=pair_mid.get(pair, ""),
                first_day=first,
                last_day=last,
                span_days=span,
                active_days=active,
                pct_active=round(100 * active / max(span, 1), 1),
                total_count=total_n,
                total_usd=round(total_usd, 2),
                meat_count=meat_n,
                meat_usd=round(meat_usd, 2),
                recent_active_days=recent_active,
                recent_pct=round(100 * recent_active / max(recent_window, 1), 1),
                recent_count=recent_n,
                recent_usd=round(recent_usd, 2),
            )
        )
    pair_stats.sort(key=lambda s: (-s.meat_usd, -s.total_usd, -s.recent_active_days))

    near_daily = [
        asdict(s)
        for s in pair_stats
        if s.recent_active_days >= recent_window * 0.33 and s.meat_usd >= 1000
    ]

    return AnalysisReport(
        chain=chain,
        since_iso=since.isoformat(),
        until_iso=until.isoformat(),
        dust_usd=dust,
        markets_filter=filter_label,
        group=group,
        total_fetched=len(raw),
        total_matched=len(rows),
        global_by_period=dict(sorted(global_by.items())),
        by_pair=pair_stats,
        events=[asdict(r) for r in rows] if include_events else [],
        near_daily_meat=near_daily,
    )


def print_summary(report: AnalysisReport) -> None:
    print(f"Morpho liq analysis | {report.chain} | {report.markets_filter}")
    print(f"  period: {report.since_iso[:10]} .. {report.until_iso[:10]}")
    print(f"  fetched={report.total_fetched} matched={report.total_matched} dust>=${report.dust_usd:.0f}")
    if report.events:
        last = report.events[-1]
        print(f"  last: {last['when'][:16]} {last['pair']} ${last['usd']:.2f}")
    if report.total_fetched >= 990:
        print("  NOTE: GraphQL returned ~1000 rows — older liq may be truncated; use shorter --days windows.")
    print(f"  near-daily meat pairs (>33% active {report.group}s, >=$1k): {len(report.near_daily_meat)}")
    print("\nTop pairs (meat USD):")
    for s in report.by_pair[:12]:
        print(
            f"  {s.pair:16} meat ${s.meat_usd:>12,.0f}  "
            f"({s.meat_count} liq, {s.recent_active_days} active recent)  "
            f"total ${s.total_usd:,.0f}"
        )
    if report.group == "day" and report.global_by_period:
        print("\nLast 14 days (all matched):")
        keys = sorted(report.global_by_period.keys())[-14:]
        for k in keys:
            g = report.global_by_period[k]
            print(
                f"  {k}  n={int(g['count']):3d}  "
                f"${float(g['sum_usd']):,.0f}  meat={int(g['meat_count'])}"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description="Morpho liquidation analysis (GraphQL)")
    ap.add_argument("--chain", default="base", choices=sorted(CHAIN_IDS))
    ap.add_argument("--days", type=int, default=90, help="lookback days")
    ap.add_argument(
        "--markets",
        default="ours",
        help="all | ours (.env MORPHO_ALLOWED_MARKETS) | USDC/cbXRP,USDC/KTA",
    )
    ap.add_argument("--pair", default="", help="shortcut: single pair, e.g. USDC/KTA")
    ap.add_argument("--dust-usd", type=float, default=10.0, help="meat threshold")
    ap.add_argument("--group", choices=("day", "month"), default="day")
    ap.add_argument("--recent-days", type=int, default=60, help="window for frequency stats")
    ap.add_argument("--events", action="store_true", help="include full event list in JSON")
    ap.add_argument("--out", default="", help="output JSON path (default morpho_research/out/...)")
    args = ap.parse_args()

    if args.pair:
        markets_filter = {args.pair.strip()}
        filter_label = args.pair.strip()
    else:
        markets_filter = parse_markets_filter(args.markets, args.chain)
        filter_label = args.markets

    report = build_report(
        args.chain,
        args.days,
        args.dust_usd,
        markets_filter,
        filter_label,
        args.group,
        args.events,
        args.recent_days,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else OUT_DIR / f"liq_analysis_{args.chain}_{args.days}d.json"
    payload = asdict(report)
    # PairStats dataclass -> dict via asdict on nested; rebuild manually for clean JSON
    payload["by_pair"] = [asdict(p) for p in report.by_pair]
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print_summary(report)
    print(f"\nJSON: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
