"""Fetch H1 gold bars. Yahoo GC=F (COMEX gold) is the XAU/USD proxy."""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from xau_jam.pattern import Bar

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
BINANCE_FAPI = "https://www.binance.com/fapi/v1"
DEFAULT_SYMBOL = "GC=F"


def _http_json(url: str, timeout: int = 45):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 xau-jam-backtest"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _parse_binance_klines(raw) -> list[Bar]:
    bars: list[Bar] = []
    for row in raw:
        o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        hi = max(h, o, c, l)
        lo = min(l, o, c, h)
        bars.append(
            Bar(
                time=datetime.fromtimestamp(int(row[0]) / 1000.0, tz=timezone.utc),
                open=o,
                high=hi,
                low=lo,
                close=c,
                volume=float(row[5] or 0.0),
            )
        )
    return bars


def fetch_binance_klines(
    symbol: str,
    interval: str = "1d",
    limit: int = 1000,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[Bar]:
    """USDT-M perpetual klines via Binance fapi (www.binance.com; fapi.binance.com is 451 here)."""
    url = f"{BINANCE_FAPI}/klines?symbol={symbol}&interval={interval}&limit={int(limit)}"
    if start_ms is not None:
        url += f"&startTime={int(start_ms)}"
    if end_ms is not None:
        url += f"&endTime={int(end_ms)}"
    raw = _http_json(url)
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"Binance returned no klines for {symbol} {interval}")
    bars = _parse_binance_klines(raw)
    if len(bars) < 10:
        raise RuntimeError(f"too few Binance {interval} bars for {symbol}: {len(bars)}")
    return bars


def fetch_binance_span(symbol: str, interval: str, days: int) -> list[Bar]:
    """Paginated klines covering the last `days` (Binance limit 1500/request)."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - int(days * 24 * 3600 * 1000)
    out: list[Bar] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (
            f"{BINANCE_FAPI}/klines?symbol={symbol}&interval={interval}"
            f"&limit=1500&startTime={cursor}&endTime={end_ms}"
        )
        raw = _http_json(url)
        if not isinstance(raw, list) or not raw:
            break
        batch = _parse_binance_klines(raw)
        if not batch:
            break
        out.extend(batch)
        nxt = int(batch[-1].time.timestamp() * 1000) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < 1500:
            break
        time.sleep(0.05)
    seen: set[datetime] = set()
    uniq: list[Bar] = []
    for b in out:
        if b.time in seen:
            continue
        seen.add(b.time)
        uniq.append(b)
    if len(uniq) < 10:
        raise RuntimeError(f"too few Binance {interval} bars for {symbol}: {len(uniq)}")
    return uniq


def binance_usdt_perps() -> list[dict]:
    info = _http_json(f"{BINANCE_FAPI}/exchangeInfo")
    out = []
    for s in info.get("symbols") or []:
        if (
            s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
        ):
            out.append(s)
    return out


def binance_ticker_24h() -> list[dict]:
    raw = _http_json(f"{BINANCE_FAPI}/ticker/24hr")
    return raw if isinstance(raw, list) else []


def fetch_yahoo_period(
    symbol: str,
    start: datetime,
    end: datetime,
    interval: str = "60m",
) -> list[Bar]:
    """Chart by unix period. 60m/1h still usually capped at ~730d by Yahoo."""
    p1 = int(start.replace(tzinfo=timezone.utc).timestamp())
    p2 = int(end.replace(tzinfo=timezone.utc).timestamp())
    url = f"{YAHOO_CHART.format(symbol=symbol)}?interval={interval}&period1={p1}&period2={p2}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 xau-jam-backtest"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode())
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError(f"Yahoo returned no chart for {symbol}: {payload.get('chart', {}).get('error')}")
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or [0.0] * len(timestamps)
    bars: list[Bar] = []
    for ts, o, h, l, c, v in zip(timestamps, opens, highs, lows, closes, volumes, strict=False):
        if o is None or h is None or l is None or c is None:
            continue
        o, h, l, c = float(o), float(h), float(l), float(c)
        hi = max(h, o, c, l)
        lo = min(l, o, c, h)
        bars.append(
            Bar(
                time=datetime.fromtimestamp(int(ts), tz=timezone.utc),
                open=o,
                high=hi,
                low=lo,
                close=c,
                volume=float(v or 0.0),
            )
        )
    if len(bars) < 10:
        raise RuntimeError(f"too few {interval} bars for {symbol}: {len(bars)}")
    return bars


def fetch_yahoo(
    symbol: str = DEFAULT_SYMBOL,
    range_spec: str = "1mo",
    interval: str = "60m",
) -> list[Bar]:
    url = f"{YAHOO_CHART.format(symbol=symbol)}?interval={interval}&range={range_spec}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 xau-jam-backtest"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode())
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError(f"Yahoo returned no chart for {symbol}: {payload.get('chart', {}).get('error')}")
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or [0.0] * len(timestamps)
    bars: list[Bar] = []
    for ts, o, h, l, c, v in zip(timestamps, opens, highs, lows, closes, volumes, strict=False):
        if o is None or h is None or l is None or c is None:
            continue
        o, h, l, c = float(o), float(h), float(l), float(c)
        hi = max(h, o, c, l)
        lo = min(l, o, c, h)
        bars.append(
            Bar(
                time=datetime.fromtimestamp(int(ts), tz=timezone.utc),
                open=o,
                high=hi,
                low=lo,
                close=c,
                volume=float(v or 0.0),
            )
        )
    if len(bars) < 10:
        raise RuntimeError(f"too few {interval} bars for {symbol}: {len(bars)}")
    return bars


def fetch_yahoo_h1(symbol: str = DEFAULT_SYMBOL, range_spec: str = "1mo") -> list[Bar]:
    return fetch_yahoo(symbol, range_spec, interval="60m")


def write_csv(bars: list[Bar], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["time_utc,open,high,low,close,volume"]
    for b in bars:
        lines.append(
            f"{b.time.isoformat()},{b.open:.5f},{b.high:.5f},{b.low:.5f},{b.close:.5f},{b.volume:.4f}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
