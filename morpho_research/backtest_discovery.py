#!/usr/bin/env python3
"""Backtest: Morpho 'new market discovery' sleeve (off core watchlist).

Core watchlist = current morpho_markets minus KTA (KTA is the discovery example).
Discovery = any Base/Arb liquidation on a market id not in that core set.

Does not change MorphoFlashLiquidator / AUTO_EXECUTE.

  .venv-run/Scripts/python.exe morpho_research/backtest_discovery.py
  .venv-run/Scripts/python.exe morpho_research/backtest_discovery.py --send-tg
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
for p in (_DIR, _ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env", override=True)

from morpho_backtest import (  # noqa: E402
    OUT_DIR,
    _graphql,
    parse_events,
)
from morpho_markets import MORPHO_MARKETS  # noqa: E402

TZ5 = timezone(timedelta(hours=5))
MIN_DEBT = 300.0
# Skip GraphQL garbage (wrong decimals) — real Morpho liqs this size are core cbBTC anyway.
MAX_DEBT = 250_000.0

# Combat list BEFORE we added KTA as a discovery patch.
CORE_IDS = {
    m.market_id.lower()
    for m in MORPHO_MARKETS
    if m.enabled and m.collateral_symbol != "KTA"
}

_LIQ_Q = """
query($chainId: Int!, $ts: Int!, $first: Int!) {
  marketTransactions(
    first: $first
    orderBy: Timestamp
    orderDirection: Asc
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


def fetch_liquidations_windows(chain_id: int, since_ts: int, until_ts: int) -> list[dict]:
    """Page by short time windows (Morpho `skip` pagination has holes)."""
    import time as _time

    out: list[dict] = []
    seen: set[str] = set()
    step = 3 * 86400
    t0 = since_ts
    while t0 <= until_ts:
        t1 = min(t0 + step, until_ts)
        data = _graphql(_LIQ_Q, {"chainId": chain_id, "ts": t0, "first": 100})
        items = (data.get("marketTransactions") or {}).get("items") or []
        n_in = 0
        for it in items:
            t = int(it.get("timestamp") or 0)
            if t < t0 or t > t1:
                continue
            hx = str(it.get("txHash") or "")
            user = ((it.get("user") or {}).get("address") or "")
            key = f"{hx}:{t}:{user}"
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
            n_in += 1
        # Dense cascade week: shrink window.
        if n_in >= 95 and step > 3600:
            step = 3600
            continue
        t0 = t1 + 1
        if step < 3 * 86400 and n_in < 40:
            step = 3 * 86400
        _time.sleep(0.1)
        if len(out) > 20_000:
            break
    return out


def ym(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ5).strftime("%Y-%m")


def contested_txs(events: list[Any], gap: int = 30) -> set[str]:
    """Tx hashes that sit in a 30s cluster of 2+ discovery liqs."""
    rows = sorted(events, key=lambda e: e.ts)
    hot: set[str] = set()
    i = 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1].ts - rows[i].ts <= gap:
            j += 1
        cluster = rows[i : j + 1]
        txs = {e.tx for e in cluster if e.tx}
        if len(cluster) >= 2 or len(txs) >= 2:
            hot |= txs
        i = j + 1
    return hot


def capture(events: list[Any], see: float, win_u: float, win_c: float) -> float:
    hot = contested_txs(events)
    total = 0.0
    for e in events:
        win = win_c if e.tx in hot else win_u
        total += see * win * e.est_profit_usd
    return total


def month_iter(events: list[Any], months: int) -> list[str]:
    if not events:
        now = datetime.now(TZ5)
        keys = []
        for i in range(months - 1, -1, -1):
            d = (now.replace(day=1) - timedelta(days=32 * i)).replace(day=1)
            keys.append(d.strftime("%Y-%m"))
        return keys[-months:]
    last = datetime.fromtimestamp(max(e.ts for e in events), TZ5).replace(day=1)
    keys = []
    d = last
    for _ in range(months):
        keys.append(d.strftime("%Y-%m"))
        d = (d - timedelta(days=1)).replace(day=1)
    return list(reversed(keys))


def bucket(events: list[Any], keys: list[str]) -> dict[str, list[Any]]:
    want = set(keys)
    out: dict[str, list[Any]] = {k: [] for k in keys}
    for e in events:
        k = ym(e.ts)
        if k in want:
            out[k].append(e)
    return out


def summarize(rows: list[Any]) -> dict[str, Any]:
    n = len(rows)
    debt = sum(e.repaid_usd for e in rows)
    edge = sum(e.est_profit_usd for e in rows)
    by_pair: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n": 0, "debt": 0.0, "edge": 0.0}
    )
    for e in rows:
        b = by_pair[e.pair]
        b["n"] += 1
        b["debt"] += e.repaid_usd
        b["edge"] += e.est_profit_usd
    top = sorted(by_pair.items(), key=lambda kv: -kv[1]["edge"])[:8]
    return {
        "n": n,
        "debt": debt,
        "edge": edge,
        "honest": capture(rows, 0.90, 0.15, 0.05),
        "opt": capture(rows, 0.90, 0.35, 0.12),
        "top": [
            {
                "pair": p,
                "n": int(s["n"]),
                "debt": s["debt"],
                "edge": s["edge"],
            }
            for p, s in top
        ],
    }


def render(disc3: dict, disc6: dict, core_note: str) -> str:
    lines = [
        "DISCOVERY SLEEVE — бэктест (новые Morpho-рынки)",
        "не в боевом watchlist (KTA считаем discovery)",
        "Base+Arb  |  долг $300–$25k  |  edge = LIF до газа/слипа",
        "",
        "Это НЕ гарантия. 100% пирога мы не берём (гонка).",
        "Честно: see 90% x win 15%/5% (spokoyniy/kaskad).",
        "Optimist: see 90% x win 35%/12% kak stage1_max na tonkom rynke.",
        "",
        "=== 3 месяца ===",
    ]
    for k, s in disc3.items():
        lines.append(
            f"{k}  n={s['n']:3}  долг ${s['debt']:,.0f}  весь edge ${s['edge']:,.0f}"
        )
        lines.append(
            f"         если участвовали: честно ${s['honest']:,.0f}  |  оптим ${s['opt']:,.0f}"
        )
        if s["top"]:
            bits = ", ".join(
                f"{t['pair']} ${t['edge']:.0f}" for t in s["top"][:3]
            )
            lines.append(f"         топ: {bits}")
    h3 = sum(s["honest"] for s in disc3.values())
    o3 = sum(s["opt"] for s in disc3.values())
    e3 = sum(s["edge"] for s in disc3.values())
    lines.append(
        f"ИТОГО 3м  пирог ${e3:,.0f}  → честно ${h3:,.0f}  оптим ${o3:,.0f}"
    )
    n3 = max(len(disc3), 1)
    lines.append(
        f"в месяц: честно ~${h3/n3:,.0f}  оптим ~${o3/n3:,.0f}"
    )
    lines += ["", "=== 6 месяцев ==="]
    for k, s in disc6.items():
        lines.append(
            f"{k}  n={s['n']:3}  долг ${s['debt']:,.0f}  весь edge ${s['edge']:,.0f}"
        )
        lines.append(
            f"         честно ${s['honest']:,.0f}  |  оптим ${s['opt']:,.0f}"
        )
        if s["top"][:2]:
            bits = ", ".join(
                f"{t['pair']} ${t['edge']:.0f}" for t in s["top"][:3]
            )
            lines.append(f"         топ: {bits}")
    h6 = sum(s["honest"] for s in disc6.values())
    o6 = sum(s["opt"] for s in disc6.values())
    e6 = sum(s["edge"] for s in disc6.values())
    n6 = max(len(disc6), 1)
    lines.append(
        f"ИТОГО 6м  пирог ${e6:,.0f}  → честно ${h6:,.0f}  оптим ${o6:,.0f}"
    )
    lines.append(
        f"в месяц: честно ~${h6/n6:,.0f}  оптим ~${o6/n6:,.0f}"
    )
    lines += [
        "",
        "Концентрация: июль почти весь FXUSD/VVV (~$23k edge),",
        "июнь — PT-apxUSD (~$12k). Без этих двух хвостов типичный",
        "месяц discovery — сотни $, не тысячи. Это не станок.",
        "",
        core_note,
        "",
        "Куда в боте: morpho_scanner (не Aave). На скорость кита не влияет,",
        "если только TG на чужой Liquidate, без сида/оракула нового рынка.",
        "AUTO_EXECUTE не включал.",
    ]
    return "\n".join(lines)


async def send_tg(text: str) -> bool:
    from aave_bot.alerts import Notifier
    from aave_bot.config import load_telegram_config

    cfg = load_telegram_config()
    if not cfg.enabled:
        print("TG disabled")
        return False
    n = Notifier(cfg, prefix="")
    limit = 3500
    parts: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            parts.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        parts.append(buf)
    ok_all = True
    total = len(parts)
    for i, part in enumerate(parts, 1):
        hdr = f"DISCOVERY {i}/{total}\n" if total > 1 else ""
        ok = await n.send(hdr + part, dedup_key=None, cooldown=0)
        print(f"tg chunk {i}/{total}: {'ok' if ok else 'FAIL'}")
        ok_all = ok_all and ok
        await asyncio.sleep(0.35)
    await n.close()
    return ok_all


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-tg", action="store_true")
    args = ap.parse_args()

    days = 186.0
    since = int(time.time()) - int(days * 86400)
    all_ev = []
    for chain, cid in (("base", 8453), ("arbitrum", 42161)):
        print(f"fetch {chain}…")
        raw = fetch_liquidations_windows(cid, since, int(time.time()))
        ev = parse_events(chain, raw, CORE_IDS)
        all_ev.extend(ev)
        print(f"  {chain} n={len(ev)}")

    disc = [
        e
        for e in all_ev
        if (not e.in_watchlist)
        and e.repaid_usd >= MIN_DEBT
        and e.repaid_usd <= MAX_DEBT
        and e.est_profit_usd > 0
    ]
    core_act = [
        e
        for e in all_ev
        if e.in_watchlist
        and e.repaid_usd >= MIN_DEBT
        and e.repaid_usd <= MAX_DEBT
        and e.est_profit_usd > 0
    ]
    print(f"discovery actionable {len(disc)}  core actionable {len(core_act)}")

    keys6 = month_iter(all_ev, 6)
    keys3 = keys6[-3:]
    d6 = {k: summarize(v) for k, v in bucket(disc, keys6).items()}
    d3 = {k: summarize(v) for k, v in bucket(disc, keys3).items()}

    core6_edge = sum(e.est_profit_usd for e in core_act if ym(e.ts) in set(keys6))
    note = (
        f"Для сравнения: БОЕВОЙ watchlist (без KTA) за те же 6м "
        f"пирог edge ~${core6_edge:,.0f} (в основном cbXRP/cbBTC каскады — гонка)."
    )
    text = render(d3, d6, note)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_txt = OUT_DIR / "DISCOVERY_BACKTEST.txt"
    out_json = OUT_DIR / "DISCOVERY_BACKTEST.json"
    out_txt.write_text(text + "\n", encoding="utf-8")
    payload = {
        "min_debt": MIN_DEBT,
        "max_debt": MAX_DEBT,
        "core_ids": sorted(CORE_IDS),
        "months3": d3,
        "months6": d6,
        "core_6m_edge": core6_edge,
        "discovery_n": len(disc),
    }
    out_json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
    print(f"\nwrote {out_txt}")

    if args.send_tg:
        ok = asyncio.run(send_tg(text))
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
