#!/usr/bin/env python3
"""Observe-only Morpho metrics for Base USDC/cbXRP.

Does not send txs, does not talk to MorphoFlashLiquidator, does not patch
morpho_scanner / monitor_v4. Counts every public-RPC HTTP 429 (including
retried responses). Mode is reserved observe/paper/live but this runner
stays observe.

    .venv-run/Scripts/python.exe morpho_research/morpho_observe_metrics.py
    .venv-run/Scripts/python.exe morpho_research/morpho_observe_metrics.py --hours 3
    # --hours 0 or omitted (default) = forever until SIGINT. 1800s summary is NOT a stop.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from web3 import Web3
from web3.providers import HTTPProvider

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
for p in (_DIR, _ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# This step: only USDC/cbXRP. Other pairs later via MORPHO_ALLOWED_MARKETS.
os.environ["MORPHO_MODE"] = "observe"
os.environ["MORPHO_AUTO_EXECUTE"] = "false"
os.environ["MORPHO_LIQ_CONTRACT"] = ""
os.environ["BASE_MORPHO_LIQ_CONTRACT"] = ""
os.environ.setdefault("MORPHO_ALLOWED_MARKETS", "USDC/cbXRP")

from aave_bot import config as bot_config  # noqa: E402
from morpho_executor import (  # noqa: E402
    DIAGNOSTIC_NOT_COMBAT,
    MODES,
    LiqIntent,
    MorphoExecutor,
    RateLimitCounter,
    attach_public_rpc_429_counter,
    diagnostic_enabled,
    duration_is_forever,
    min_net_profit_usd,
)
from morpho_hf import MorphoReader, estimate_liquidation_profit_usd, hf_from_cached_shares  # noqa: E402
from morpho_markets import MORPHO_BLUE, parse_allowed_markets  # noqa: E402
from morpho_scanner import (  # noqa: E402
    EVENT_TOPICS,
    _topic_addr,
    _tx_hex,
    seed_borrowers_from_api,
)

LOG = logging.getLogger("morpho_observe")
OUT_DIR = _ROOT / "morpho_research" / "out"
METRICS_PATH = OUT_DIR / "observe_metrics.json"
SUMMARY_PATH = _ROOT / "MORPHO_OBSERVE_SUMMARY.md"
PID_PATH = OUT_DIR / "observe_metrics.pid"
WOULD_HAVE_PATH = OUT_DIR / "would_have_morpho_base.json"
EVENTS_PATH = OUT_DIR / "morpho_events_base.jsonl"
SCANNER_ERR = _ROOT / "morpho_scanner.err.log"
CHAIN = "base"
CHAIN_ID = 8453
PAIR_DEFAULT = "USDC/cbXRP"


class Count429Adapter(HTTPAdapter):
    """Retry 429s but count every 429 response, including retries."""

    def __init__(self, counter: RateLimitCounter, retries: int = 4, **kwargs: Any) -> None:
        super().__init__(max_retries=0, **kwargs)
        self.counter = counter
        self.retries = max(0, retries)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # noqa: ANN001
        last = None
        for attempt in range(self.retries + 1):
            resp = super().send(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )
            if getattr(resp, "status_code", None) != 429:
                return resp
            self.counter.inc()
            last = resp
            if attempt >= self.retries:
                break
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 0.35 * (2**attempt)
            except (TypeError, ValueError):
                wait = 0.35 * (2**attempt)
            time.sleep(min(max(wait, 0.1), 8.0))
        return last


@dataclass
class CachedPos:
    user: str
    supply_shares: int
    borrow_shares: int
    collateral: int
    total_borrow_assets: int
    total_borrow_shares: int
    oracle_price: int
    debt_usd: float
    health_factor: Decimal


@dataclass
class ObserveState:
    mode: str = "observe"
    pair: str = PAIR_DEFAULT
    market_id: str = ""
    started_ts: float = 0.0
    loops: int = 0
    detect_count: int = 0
    unhealthy_hits: int = 0
    candidate_count: int = 0
    would_send: int = 0
    simulated_ok: int = 0
    sent: int = 0
    landed: int = 0
    lost_to_foreign: int = 0
    foreign_liq_total: int = 0
    rate_limit_hits: int = 0
    revert_reason: str | None = None
    tracked: int = 0
    rpc_ok: bool = True
    consecutive_rpc_fail: int = 0
    loop_ms: deque[float] = field(default_factory=lambda: deque(maxlen=400))
    rpc_ms: deque[float] = field(default_factory=lambda: deque(maxlen=400))
    oracle_eval_ms: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    would_send_keys: dict[str, float] = field(default_factory=dict)
    seen_liq_tx: set[str] = field(default_factory=set)
    events_offset: int = 0
    last_block: int = 0
    last_oracle: int = 0
    stop_reason: str = ""


def _avg(xs: deque[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def _share(lost: int, would: int) -> float:
    if would <= 0:
        return 0.0
    return lost / would


def build_session(counter: RateLimitCounter) -> requests.Session:
    session = requests.Session()
    adapter = Count429Adapter(counter, retries=4)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    # Prevent executor attach() from double-counting via session.request wrap.
    session._morpho_429_counter = counter  # type: ignore[attr-defined]
    session._morpho_429_wrapped = True  # type: ignore[attr-defined]
    return session


def connect_w3(counter: RateLimitCounter) -> tuple[Web3, str]:
    urls = bot_config.http_rpc_urls(CHAIN)
    if not urls:
        raise bot_config.ConfigError(f"{CHAIN.upper()}_HTTP_RPC_URL is required but not set")
    last_exc: Exception | None = None
    for url in urls:
        session = build_session(counter)
        provider = HTTPProvider(url, request_kwargs={"timeout": 20}, session=session)
        w3 = Web3(provider)
        try:
            if w3.is_connected():
                attach_public_rpc_429_counter(w3, counter)
                return w3, url
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise RuntimeError(f"HTTP RPC not connected: {last_exc}")


def _intent_from_cached(market, pos: CachedPos, reason: str) -> LiqIntent:
    return LiqIntent(
        chain=CHAIN,
        user=pos.user,
        market=market,
        health_factor=pos.health_factor,
        debt_usd=pos.debt_usd,
        profit_usd=estimate_liquidation_profit_usd(pos.debt_usd, market.lltv_wad),
        borrow_shares=pos.borrow_shares,
        collateral=pos.collateral,
        total_borrow_assets=pos.total_borrow_assets,
        total_borrow_shares=pos.total_borrow_shares,
        oracle_price=pos.oracle_price,
        loan_decimals=6,
        reason=reason,
    )


def note_would_send(st: ObserveState, exe: MorphoExecutor, market, pos: CachedPos, reason: str) -> None:
    key = f"{market.market_id.lower()}:{pos.user.lower()}"
    now = time.monotonic()
    prev = st.would_send_keys.get(key)
    if prev is not None and (now - prev) < 600:
        return
    intent = _intent_from_cached(market, pos, reason)
    why = exe._passes_filters(intent)
    if why:
        return
    st.would_send_keys[key] = now
    st.would_send += 1
    st.candidate_count += 1
    exe.metrics.would_send += 1
    LOG.warning(
        "would_send %s net_profit_usd=$%.4f debt=$%.2f HF=%s reason=%s",
        pos.user,
        intent.net_profit_usd,
        pos.debt_usd,
        pos.health_factor,
        reason,
    )


def note_foreign(st: ObserveState, exe: MorphoExecutor, tx: str, user: str, market_id: str) -> None:
    tx_k = (tx or "").lower()
    if tx_k and tx_k in st.seen_liq_tx:
        return
    if tx_k:
        st.seen_liq_tx.add(tx_k)
    st.foreign_liq_total += 1
    key = f"{market_id.lower()}:{user.lower()}"
    seen_at = st.would_send_keys.get(key)
    if seen_at is not None:
        st.lost_to_foreign += 1
        exe.note_foreign_liq(user, market_id)


def ingest_scanner_files(st: ObserveState, exe: MorphoExecutor, market_id: str) -> None:
    mid = market_id.lower()
    if EVENTS_PATH.exists():
        try:
            raw = EVENTS_PATH.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        lines = raw.splitlines()
        new_lines = lines[st.events_offset :]
        st.events_offset = len(lines)
        for line in new_lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row_mid = str(row.get("market_id") or "").lower()
            if row_mid and row_mid != mid:
                continue
            pair = str(row.get("pair") or "")
            if pair and pair.upper() != st.pair.upper():
                continue
            kind = row.get("kind")
            if kind == "candidate":
                user = str(row.get("user") or "")
                if user:
                    key = f"{mid}:{user.lower()}"
                    st.would_send_keys.setdefault(key, time.monotonic())
            elif kind == "foreign_liq":
                note_foreign(
                    st,
                    exe,
                    str(row.get("tx") or ""),
                    str(row.get("user") or ""),
                    row_mid or mid,
                )
    if WOULD_HAVE_PATH.exists():
        try:
            data = json.loads(WOULD_HAVE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        missed = int(data.get("raced_and_missed") or 0)
        if missed > st.lost_to_foreign:
            # Scoreboard is cumulative; only raise, don't double-add our getLogs.
            extra = missed - st.lost_to_foreign
            st.lost_to_foreign += extra
            exe.metrics.lost_to_foreign += extra


def poll_liquidates(
    w3: Web3,
    st: ObserveState,
    exe: MorphoExecutor,
    market_id: str,
    counter: RateLimitCounter,
) -> None:
    t0 = time.perf_counter()
    try:
        head = int(w3.eth.block_number)
    except Exception:
        st.consecutive_rpc_fail += 1
        st.rpc_ok = False
        return
    st.rpc_ms.append((time.perf_counter() - t0) * 1000.0)
    st.rpc_ok = True
    st.consecutive_rpc_fail = 0
    if st.last_block <= 0:
        st.last_block = max(0, head - 12)
    frm = st.last_block + 1
    if frm > head:
        return
    # Keep windows small — public RPC 429s are the thing we are measuring.
    if head - frm > 40:
        frm = head - 40
    mid = market_id if market_id.startswith("0x") else "0x" + market_id
    try:
        logs = w3.eth.get_logs(
            {
                "address": Web3.to_checksum_address(MORPHO_BLUE),
                "fromBlock": frm,
                "toBlock": head,
                "topics": [EVENT_TOPICS["Liquidate"], mid],
            }
        )
    except Exception:
        st.consecutive_rpc_fail += 1
        return
    st.rate_limit_hits = counter.hits
    st.last_block = head
    for entry in logs:
        topics = entry.get("topics") or []
        borrower = _topic_addr(topics[3]) if len(topics) >= 4 else ""
        txh = _tx_hex(entry)
        note_foreign(st, exe, txh, borrower, mid)


def refresh_book(
    reader: MorphoReader,
    market,
    users: list[str],
    st: ObserveState,
    cache: dict[str, CachedPos],
) -> None:
    t0 = time.perf_counter()
    rows = reader.read_positions_batch(market, users, loan_price_usd=1.0, chunk=40)
    st.rpc_ms.append((time.perf_counter() - t0) * 1000.0)
    if not rows and users:
        st.consecutive_rpc_fail += 1
        return
    st.consecutive_rpc_fail = 0
    st.rpc_ok = True
    for state in rows:
        cache[state.user.lower()] = CachedPos(
            user=state.user,
            supply_shares=state.supply_shares,
            borrow_shares=state.borrow_shares,
            collateral=state.collateral,
            total_borrow_assets=state.total_borrow_assets,
            total_borrow_shares=state.total_borrow_shares,
            oracle_price=state.oracle_price,
            debt_usd=state.debt_usd,
            health_factor=state.health_factor,
        )
        if state.oracle_price:
            st.last_oracle = state.oracle_price
    st.tracked = len(cache)


def eval_oracle_tick(
    reader: MorphoReader,
    market,
    exe: MorphoExecutor,
    cache: dict[str, CachedPos],
    st: ObserveState,
    hf_threshold: Decimal,
) -> None:
    t_rpc = time.perf_counter()
    price = reader.read_oracle_price(market)
    rpc_ms = (time.perf_counter() - t_rpc) * 1000.0
    st.rpc_ms.append(rpc_ms)
    if price is None:
        st.consecutive_rpc_fail += 1
        st.rpc_ok = False
        return
    st.rpc_ok = True
    st.consecutive_rpc_fail = 0
    moved = price != st.last_oracle and st.last_oracle != 0
    st.last_oracle = int(price)
    t_eval = time.perf_counter()
    detects = 0
    for pos in cache.values():
        if pos.borrow_shares <= 0 or pos.collateral <= 0:
            continue
        hf, borrowed = hf_from_cached_shares(
            collateral=pos.collateral,
            borrow_shares=pos.borrow_shares,
            total_borrow_assets=pos.total_borrow_assets,
            total_borrow_shares=pos.total_borrow_shares,
            oracle_price=int(price),
            lltv_wad=market.lltv_wad,
        )
        pos.oracle_price = int(price)
        pos.health_factor = hf
        pos.debt_usd = borrowed / 1_000_000
        if hf < hf_threshold:
            detects += 1
            st.unhealthy_hits += 1
            note_would_send(st, exe, market, pos, "oracle" if moved else "poll")
    if detects:
        st.detect_count += detects
    if moved:
        st.oracle_eval_ms.append((time.perf_counter() - t_eval) * 1000.0)


def payload(st: ObserveState, counter: RateLimitCounter, *, rpc_url: str, hours: float) -> dict[str, Any]:
    st.rate_limit_hits = counter.hits
    loop_avg = _avg(st.loop_ms)
    rpc_avg = _avg(st.rpc_ms)
    o2e = _avg(st.oracle_eval_ms)
    return {
        "mode": st.mode,
        "modes_reserved": list(MODES),
        "chain": CHAIN,
        "pair": st.pair,
        "market_id": st.market_id,
        "started_at": st.started_ts,
        "updated_at": time.time(),
        "uptime_s": round(time.time() - st.started_ts, 1) if st.started_ts else 0.0,
        "hours_target": hours,
        "rpc_host": bot_config.rpc_host(rpc_url),
        "would_send": st.would_send,
        "simulated_ok": 0,
        "sent": 0,
        "landed": 0,
        "lost_to_foreign": st.lost_to_foreign,
        "foreign_liq_total": st.foreign_liq_total,
        "lost_to_foreign_share": round(_share(st.lost_to_foreign, st.would_send), 4),
        "revert_reason": None,
        "rate_limit_hits": st.rate_limit_hits,
        "detect_count": st.detect_count,
        "unhealthy_hits": st.unhealthy_hits,
        "candidate_count": st.candidate_count,
        "tracked": st.tracked,
        "loops": st.loops,
        "rpc_ok": st.rpc_ok,
        "consecutive_rpc_fail": st.consecutive_rpc_fail,
        "stop_reason": st.stop_reason,
        "diagnostic_mode": diagnostic_enabled(CHAIN),
        "min_net_profit_usd": min_net_profit_usd(
            CHAIN, diagnostic=diagnostic_enabled(CHAIN)
        ),
        "latencies_ms": {
            "loop_avg": round(loop_avg, 2),
            "loop_last": round(st.loop_ms[-1], 2) if st.loop_ms else 0.0,
            "rpc_roundtrip_avg": round(rpc_avg, 2),
            "oracle_to_eval_avg": round(o2e, 2),
            "latency_used": round(o2e if o2e else (rpc_avg if rpc_avg else loop_avg), 2),
        },
    }


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_metrics(st: ObserveState, counter: RateLimitCounter, rpc_url: str, hours: float) -> dict[str, Any]:
    data = payload(st, counter, rpc_url=rpc_url, hours=hours)
    atomic_write(METRICS_PATH, json.dumps(data, indent=2))
    return data


def write_summary(data: dict[str, Any], *, reason: str) -> None:
    lat = (data.get("latencies_ms") or {}).get("latency_used") or 0.0
    detects = int(data.get("detect_count") or 0)
    would = int(data.get("would_send") or 0)
    lost = int(data.get("lost_to_foreign") or 0)
    share = float(data.get("lost_to_foreign_share") or 0)
    hits_429 = int(data.get("rate_limit_hits") or 0)
    lines = [
        "# Morpho observe summary (Base USDC/cbXRP)",
        "",
        f"- reason: {reason}",
        f"- mode: `{data.get('mode')}` (paper/live reserved, not used)",
        f"- diagnostic: {data.get('diagnostic_mode')} (TEMPORARY, NOT combat 19.08)",
        f"- min_net_profit_usd: {data.get('min_net_profit_usd')}",
        f"- uptime_s: {data.get('uptime_s')}",
        f"- tracked borrowers: {data.get('tracked')}",
        f"- loops: {data.get('loops')}",
        "",
        "## Four numbers",
        "",
        f"1. **detect_count** (unhealthy eval hits): **{detects}**",
        f"   - would_send: {would}",
        f"   - unique-ish candidates: {data.get('candidate_count')}",
        f"2. **avg latency_ms**: **{lat}**",
        f"   - loop_avg: {(data.get('latencies_ms') or {}).get('loop_avg')}",
        f"   - rpc_roundtrip_avg: {(data.get('latencies_ms') or {}).get('rpc_roundtrip_avg')}",
        f"   - oracle_to_eval_avg: {(data.get('latencies_ms') or {}).get('oracle_to_eval_avg')}",
        f"3. **lost_to_foreign share**: **{share:.4f}** ({lost}/{would or 0}; foreign_liq_total={data.get('foreign_liq_total')})",
        f"4. **rate_limit_hits** (HTTP 429, public RPC): **{hits_429}**",
        "",
        "Reserved zeros: simulated_ok=0 sent=0 landed=0 revert_reason=null.",
        "No MorphoFlashLiquidator calls. No live txs.",
        "",
    ]
    atomic_write(SUMMARY_PATH, "\n".join(lines) + "\n")


def maybe_telegram(data: dict[str, Any]) -> None:
    try:
        from aave_bot.alerts import Notifier
        from aave_bot.config import load_morpho_telegram_config
    except Exception:
        return
    cfg = load_morpho_telegram_config()
    if not cfg.enabled:
        return
    lat = (data.get("latencies_ms") or {}).get("latency_used")
    text = (
        f"[morpho] наблюдение {data.get('pair')} {data.get('uptime_s')} с\n"
        f"детектов={data.get('detect_count')} готовы слать={data.get('would_send')}\n"
        f"задержка_мс={lat} ушли чужим={data.get('lost_to_foreign_share')}\n"
        f"429={data.get('rate_limit_hits')}"
    )
    try:
        import asyncio

        notifier = Notifier(cfg, prefix="")

        async def _send() -> None:
            await notifier.send(text, dedup_key="morpho-observe-hours", cooldown=1.0)
            await notifier.close()

        asyncio.run(_send())
    except Exception as exc:  # noqa: BLE001
        LOG.debug("tg skip: %s", exc)


def run(
    hours: float,
    loop_s: float,
    seed_n: int,
    tg_at_end: bool,
    *,
    duration_seconds: int = 0,
) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    markets = parse_allowed_markets(CHAIN, PAIR_DEFAULT)
    if not markets:
        LOG.error("no USDC/cbXRP market config")
        return 2
    market = markets[0]
    counter = RateLimitCounter()
    w3, rpc_url = connect_w3(counter)
    exe = MorphoExecutor(CHAIN, w3, alert=None)
    exe.mode = "observe"
    exe.contract = None
    exe.contract_addr = ""
    exe.auto_execute = False
    reader = MorphoReader(w3)
    st = ObserveState(
        mode="observe",
        pair=f"{market.loan_symbol}/{market.collateral_symbol}",
        market_id=market.market_id,
        started_ts=time.time(),
    )
    cap_s = 0.0
    if not duration_is_forever(duration_seconds):
        cap_s = float(duration_seconds)
    elif hours > 0:
        cap_s = hours * 3600.0
    deadline = (st.started_ts + cap_s) if cap_s > 0 else None
    hours_target = (cap_s / 3600.0) if cap_s > 0 else 0.0
    if exe.diagnostic_mode:
        LOG.warning("%s min_net_profit=$%.4f", DIAGNOSTIC_NOT_COMBAT, exe.min_net_profit_usd)
    if deadline is None:
        LOG.warning(
            "observe duration=forever (no --hours/--duration-seconds or 0); "
            "no self-timeout — stop with SIGINT"
        )
    LOG.warning(
        "observe start pair=%s mode=%s rpc=%s hours=%.2f duration_s=%s diagnostic=%s "
        "min_net_profit=$%.4f",
        st.pair,
        st.mode,
        bot_config.rpc_host(rpc_url),
        hours_target,
        int(cap_s) if cap_s else 0,
        exe.diagnostic_mode,
        exe.min_net_profit_usd,
    )
    users: list[str] = []
    try:
        seeded = seed_borrowers_from_api(CHAIN_ID, [market], per_market=seed_n)
        users = [u for _mid, u in seeded]
    except Exception as exc:  # noqa: BLE001
        LOG.warning("graphql seed failed: %s", exc)
    cache: dict[str, CachedPos] = {}
    if users:
        refresh_book(reader, market, users, st, cache)
    next_refresh = time.monotonic() + 45.0
    next_logs = time.monotonic() + 6.0
    next_write = 0.0
    # Periodic summary interval only — not a run timeout.
    next_summary = time.monotonic() + 1800.0
    hf_threshold = exe.hf_threshold
    try:
        while True:
            if deadline is not None and time.time() >= deadline:
                st.stop_reason = st.stop_reason or "hours_elapsed"
                break
            t_loop = time.perf_counter()
            eval_oracle_tick(reader, market, exe, cache, st, hf_threshold)
            now_m = time.monotonic()
            if now_m >= next_logs:
                poll_liquidates(w3, st, exe, market.market_id, counter)
                ingest_scanner_files(st, exe, market.market_id)
                next_logs = now_m + 8.0
            if now_m >= next_refresh and users:
                refresh_book(reader, market, users, st, cache)
                next_refresh = now_m + 45.0
            st.loops += 1
            st.loop_ms.append((time.perf_counter() - t_loop) * 1000.0)
            st.rate_limit_hits = counter.hits
            exe.metrics.rate_limit_hits = counter.hits
            if now_m >= next_write:
                write_metrics(st, counter, rpc_url, hours_target)
                next_write = now_m + 15.0
            if now_m >= next_summary:
                data = write_metrics(st, counter, rpc_url, hours_target)
                write_summary(data, reason="periodic")
                next_summary = now_m + 1800.0
            if st.consecutive_rpc_fail >= 40:
                st.stop_reason = "rpc_dead"
                break
            sleep = loop_s
            if st.consecutive_rpc_fail:
                sleep = min(8.0, loop_s * (2 ** min(st.consecutive_rpc_fail, 4)))
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    st.stop_reason = st.stop_reason or "hours_elapsed"
                    break
                time.sleep(min(sleep, remaining))
            else:
                time.sleep(sleep)
    except KeyboardInterrupt:
        st.stop_reason = "interrupt"
    data = write_metrics(st, counter, rpc_url, hours_target)
    reason = st.stop_reason or ("hours_elapsed" if deadline is not None else "interrupt")
    write_summary(data, reason=reason)
    if tg_at_end:
        maybe_telegram(data)
    LOG.warning(
        "observe stop reason=%s detect=%s would_send=%s lost=%s 429=%s lat=%.1f",
        reason,
        data.get("detect_count"),
        data.get("would_send"),
        data.get("lost_to_foreign"),
        data.get("rate_limit_hits"),
        (data.get("latencies_ms") or {}).get("latency_used") or 0.0,
    )
    return 0 if reason != "rpc_dead" else 3


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Morpho observe metrics (USDC/cbXRP Base)")
    p.add_argument(
        "--hours",
        type=float,
        default=0.0,
        help="Stop after N hours. 0 (default) = forever until SIGINT.",
    )
    p.add_argument(
        "--duration-seconds",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N seconds. Omit or 0 = forever. Overrides --hours if >0.",
    )
    p.add_argument("--loop-s", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=150)
    p.add_argument("--tg-end", action="store_true", help="one short TG after hours if .env has tokens")
    p.add_argument(
        "--diagnostic-mode",
        action="store_true",
        help="TEMPORARY net-profit filter. NOT for combat window 19.08.",
    )
    p.add_argument(
        "--min-net-profit-usd",
        type=float,
        default=None,
        help="Diagnostic net profit floor (default 0.05)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.diagnostic_mode:
        os.environ["MORPHO_DIAGNOSTIC_MODE"] = "1"
    if args.min_net_profit_usd is not None:
        os.environ["MIN_NET_PROFIT_USD"] = str(args.min_net_profit_usd)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    hours = float(args.hours)
    if hours < 0:
        hours = 0.0
    dur = 0 if duration_is_forever(args.duration_seconds) else int(args.duration_seconds)
    if args.duration_seconds is None:
        LOG.warning("CLI --duration-seconds omitted (forever unless --hours>0)")
    elif args.duration_seconds <= 0:
        LOG.warning("CLI --duration-seconds=%s → forever unless --hours>0", args.duration_seconds)
    else:
        LOG.warning("CLI --duration-seconds=%s bounded", args.duration_seconds)
    return run(
        hours,
        max(0.2, float(args.loop_s)),
        max(10, int(args.seed)),
        bool(args.tg_end),
        duration_seconds=dur,
    )


if __name__ == "__main__":
    raise SystemExit(main())
