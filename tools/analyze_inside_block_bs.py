#!/usr/bin/env python3
"""Inside-block competitor analysis via Blockscout only (no full block RPC).

Uses known liq txs from competitor_bots_analysis.json + cascade block numbers.
Writes morpho_research/out/competitor_inside_block.{json,txt}
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "morpho_research" / "out"
BS = "https://base.blockscout.com/api/v2"
UA = {"User-Agent": "morpho-inside-block/2"}

CASCADE = [50293054, 50293068, 50293083, 50302440]
CBXRP_ORACLE = "0x031b2efc8d70042ac8d9f5c793c4149ec4b60fde"
MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"


def http_json(url: str, retries: int = 3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.8 * (i + 1))
    print(f"fail {url[:80]} {last}")
    return None


def load_liq_txs() -> list[dict]:
    p = OUT / "competitor_bots_analysis.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for liq in data.get("liquidators") or []:
        for t in liq.get("txs") or []:
            if t.get("tx"):
                out.append(
                    {
                        "liquidator": liq.get("liquidator"),
                        "tier": liq.get("tier"),
                        "tx": t["tx"],
                        "debt": t.get("sample_debt"),
                        "block": t.get("sample_block"),
                        "from": t.get("from"),
                        "to": t.get("to"),
                        "method": t.get("selector_label") or t.get("method"),
                        "gas_used": t.get("gas_used"),
                        "input_bytes": t.get("input_bytes"),
                        "liq_events": t.get("liq_events"),
                        "priority_fee_wei": t.get("priority_fee"),
                    }
                )
    # also pull cascades list for all liq addrs in those blocks via logs
    return out


def tx_position(txh: str) -> dict:
    data = http_json(f"{BS}/transactions/{txh}") or {}
    pos = data.get("position")
    # blockscout: position = index in block
    tip = data.get("priority_fee")
    tip_gwei = None
    try:
        if tip is not None:
            tip_gwei = int(tip) / 1e9
    except Exception:
        pass
    return {
        "tx": txh,
        "block": data.get("block"),
        "position": pos,
        "from": ((data.get("from") or {}) if isinstance(data.get("from"), dict) else {}).get("hash"),
        "to": ((data.get("to") or {}) if isinstance(data.get("to"), dict) else {}).get("hash"),
        "method": data.get("method"),
        "gas_used": data.get("gas_used"),
        "gas_limit": data.get("gas_limit"),
        "priority_fee_gwei": tip_gwei,
        "raw_input_bytes": max(0, len(data.get("raw_input") or "0x") // 2 - 1),
        "status": data.get("status"),
        "tx_types": data.get("transaction_types") or [],
        "confirmation_duration": data.get("confirmation_duration"),
    }


def block_txs_touching(block: int, addrs: set[str]) -> list[dict]:
    """Page block transactions; keep those to whale/oracle/morpho."""
    url = f"{BS}/blocks/{block}/transactions"
    data = http_json(url)
    items = []
    if isinstance(data, dict):
        items = data.get("items") or []
    elif isinstance(data, list):
        items = data
    # also next pages if any
    next_params = (data or {}).get("next_page_params") if isinstance(data, dict) else None
    pages = 0
    while next_params and pages < 8:
        pages += 1
        q = "&".join(f"{k}={v}" for k, v in next_params.items())
        data2 = http_json(f"{url}?{q}")
        if not data2:
            break
        more = data2.get("items") or []
        items.extend(more)
        next_params = data2.get("next_page_params")
        time.sleep(0.25)

    kept = []
    for it in items:
        to = ((it.get("to") or {}) if isinstance(it.get("to"), dict) else {}).get("hash") or ""
        to_l = to.lower()
        roles = []
        if to_l in addrs:
            roles.append("WHALE")
        if to_l == MORPHO:
            roles.append("MORPHO")
        if to_l == CBXRP_ORACLE:
            roles.append("ORACLE")
        if not roles:
            continue
        tip = it.get("priority_fee")
        tip_gwei = None
        try:
            if tip is not None:
                tip_gwei = int(tip) / 1e9
        except Exception:
            pass
        kept.append(
            {
                "position": it.get("position"),
                "hash": it.get("hash"),
                "to": to,
                "from": ((it.get("from") or {}) if isinstance(it.get("from"), dict) else {}).get("hash"),
                "method": it.get("method"),
                "gas_used": it.get("gas_used"),
                "priority_fee_gwei": tip_gwei,
                "roles": roles,
                "raw_input_bytes": max(0, len(it.get("raw_input") or "0x") // 2 - 1),
            }
        )
    kept.sort(key=lambda x: (x["position"] is None, x["position"] or 0))
    return kept


def main() -> int:
    prior = load_liq_txs()
    whales = {str(t["liquidator"]).lower() for t in prior if t.get("liquidator")}
    whales |= {
        "0x8cc0204e1e12aeb98d35b384fc0676e2df5a16e5",
        "0x48630e5780d5a45a555578cbbc921797ce4f6e7a",
        "0x358954d610222225bbc169ee0d65cf33ac9de34e",
        "0x2134695f185027845d257bcd41cc403560cf60f6",
    }

    print("enrich sample txs with block position…")
    enriched = []
    for t in prior:
        if not t.get("block") or t["block"] not in CASCADE and t.get("debt", 0) < 5000:
            # still enrich meat
            if (t.get("debt") or 0) < 1000:
                continue
        time.sleep(0.3)
        pos = tx_position(t["tx"])
        enriched.append({**t, **{f"bs_{k}": v for k, v in pos.items() if k != "tx"}})
        print(
            f"  blk={pos.get('block')} pos={pos.get('position')} "
            f"tip={pos.get('priority_fee_gwei')} debt={t.get('debt')} {t['tx'][:16]}"
        )

    print("scan cascade blocks for whale/oracle txs…")
    block_views = []
    for b in CASCADE:
        print(f"  block {b}")
        time.sleep(0.4)
        kept = block_txs_touching(b, whales)
        oracle_pos = [x["position"] for x in kept if "ORACLE" in x["roles"] and x.get("position") is not None]
        whale_pos = [x["position"] for x in kept if "WHALE" in x["roles"] and x.get("position") is not None]
        block_views.append(
            {
                "block": b,
                "n_interesting": len(kept),
                "oracle_positions": oracle_pos,
                "whale_positions": whale_pos,
                "oracle_before_first_whale": (
                    min(oracle_pos) < min(whale_pos)
                    if oracle_pos and whale_pos
                    else None
                ),
                "txs": kept,
            }
        )

    # 8cc0 special
    print("probe 8cc0…")
    a8 = "0x8cc0204e1e12aeb98D35b384fC0676e2df5a16e5"
    info = http_json(f"{BS}/addresses/{a8}") or {}
    recent = http_json(f"{BS}/addresses/{a8}/transactions") or {}
    recent_items = (recent.get("items") if isinstance(recent, dict) else []) or []

    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "enriched_meat_txs": enriched,
        "cascade_blocks": block_views,
        "whale_8cc0": {
            "is_contract": info.get("is_contract"),
            "verified": info.get("is_verified"),
            "recent": [
                {
                    "hash": it.get("hash"),
                    "method": it.get("method"),
                    "position": it.get("position"),
                    "block": it.get("block"),
                    "from": ((it.get("from") or {}) if isinstance(it.get("from"), dict) else {}).get("hash"),
                    "raw_input_bytes": max(0, len(it.get("raw_input") or "0x") // 2 - 1),
                    "priority_fee": it.get("priority_fee"),
                }
                for it in recent_items[:10]
            ],
        },
    }
    (OUT / "competitor_inside_block.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    lines = [
        "=== КАК ОНИ ВНУТРИ БЛОКА (причина + как обогнать) ===",
        "",
        "--- CASCADE BLOCKS: oracle vs whale position ---",
    ]
    for bv in block_views:
        lines.append(
            f"block {bv['block']}: oracle_pos={bv['oracle_positions']} "
            f"whale_pos={bv['whale_positions'][:8]}… "
            f"oracle_before_whale={bv['oracle_before_first_whale']} n={bv['n_interesting']}"
        )
        for x in bv["txs"][:12]:
            lines.append(
                f"  #{x.get('position')} {','.join(x['roles']):10} "
                f"tip={x.get('priority_fee_gwei')} in={x.get('raw_input_bytes')}b "
                f"method={x.get('method')} to={str(x.get('to') or '')[:12]}"
            )

    lines += ["", "--- MEAT TX POSITIONS ---"]
    for e in sorted(enriched, key=lambda z: (z.get("bs_block") or 0, z.get("bs_position") or 0)):
        tip = e.get("bs_priority_fee_gwei")
        lines.append(
            f"blk={e.get('bs_block')} #{e.get('bs_position')} "
            f"tip={tip}g debt~${e.get('debt')} liq={str(e.get('liquidator') or '')[:10]} "
            f"in={e.get('bs_raw_input_bytes')}b method={e.get('bs_method') or e.get('method')}"
        )

    lines += [
        "",
        "--- ПРИЧИНА (почему never_saw) ---",
        "1. Они НЕ ждут HF из нашего poll. Триггер = oracle/цена → сразу call своего контракта.",
        "2. Всё внутри СВОЕГО unverified contract: liquidate+swap+batch. EOA только жмёт кнопку.",
        "3. Batch 2–4 жертвы в 1 tx = один слот в блоке, несколько liq.",
        "4. 0x8cc0: calldata ~0 — состояние уже в контракте / trigger-only (мы encode каждый раз).",
        "5. Tip в каскаде выше «обычного» — платят за inclusion рядом с oracle update.",
        "6. Наш цикл: poll oracle 2–4с → multicall/local HF → encode → send. К этому моменту tx уже в блоке.",
        "7. Мы узнаём по event Liquidate = ПОСЛЕ факта. Это репортёр, не same-block race.",
        "",
        "--- КАК ОПЕРЕДИТЬ (практично) ---",
        "A. Oracle-event path (не poll): подписка на cbXRP oracle logs → мгновенный local HF (у нас УЖЕ есть _oracle_tick_local) → fire.",
        "   Сейчас узкое место часто POLL, не math. Нужен event-driven oracle, не sleep 2с.",
        "B. Prebuild: для hot HF<1.05 держать готовый calldata; на tick только подставить price/amounts.",
        "C. Batch: один tx — несколько borrowers (как 3589×3, 2134×4).",
        "D. Storm tips: при oracle Δ большой — поднять priority (без этого в каскаде не влезешь).",
        "E. Не гнаться за $16; целиться в cascade meat где batch решает.",
        "F. Paid RPC + private submit — потом; сначала A+B дают главный скачок.",
        "",
        "Итог: они выигрывают не «магией внутри EVM», а тем что РЕШЕНИЕ+CALLDATA готовы",
        "ДО/В момент oracle tx в том же блоке. Мы начинаем думать после.",
    ]
    text = "\n".join(lines) + "\n"
    (OUT / "competitor_inside_block.txt").write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
