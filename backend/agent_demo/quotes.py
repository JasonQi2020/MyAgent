"""stock_quote: real, current stock prices (the database only holds demo data).

Source: Yahoo Finance's public chart endpoint (no key). It is unofficial, so everything about
the source lives in fetch_quote(); replace that function to use another provider.
See design/scheduled_tasks.md §5.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .mcp_client import wrap_untrusted
from .tools import Tool

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
HEADERS = {"User-Agent": "Mozilla/5.0 (agent-demo)"}  # the endpoint refuses requests without one
_SYMBOL = re.compile(r"^[A-Za-z0-9.\-^=]{1,15}$")
MAX_SYMBOLS = 10


class QuoteError(Exception):
    pass


async def fetch_quote(client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
    """Raw quote fields for one symbol, or QuoteError with a readable reason."""
    try:
        response = await client.get(CHART_URL.format(symbol=symbol), headers=HEADERS, timeout=15)
    except httpx.HTTPError as exc:
        raise QuoteError(f"quote service not reachable ({type(exc).__name__})")
    if response.status_code == 404:
        raise QuoteError("unknown symbol")
    if response.status_code != 200:
        raise QuoteError(f"quote service answered HTTP {response.status_code}")
    chart = (response.json() or {}).get("chart") or {}
    results = chart.get("result") or []
    if not results:
        error = (chart.get("error") or {}).get("description") or "no data"
        raise QuoteError("unknown symbol" if "No data found" in error else error)
    meta = dict(results[0].get("meta") or {})
    if meta.get("regularMarketPrice") is None:
        raise QuoteError("no price available")
    # The previous trading day's close. Not chartPreviousClose: with range=5d that is the close
    # *before the 5-day window*, which would turn a daily change into a 5-day change.
    closes = ((results[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    stamps = results[0].get("timestamp") or []
    daily = [c for c in closes if c is not None]
    if len(daily) >= 2 and stamps and meta.get("regularMarketTime", 0) >= stamps[-1]:
        meta["previousDayClose"] = daily[-2]  # the last bar is the current/latest day
    return meta


def format_quote(symbol: str, meta: dict[str, Any]) -> str:
    price = meta["regularMarketPrice"]
    previous = meta.get("previousClose") or meta.get("previousDayClose")
    currency = meta.get("currency") or ""
    parts = [f"{meta.get('symbol', symbol)}: {price:.2f} {currency}".strip()]
    if previous:
        change = price - previous
        parts.append(f"{change:+.2f} ({change / previous * 100:+.2f} %) vs. previous close {previous:.2f}")
    low, high = meta.get("regularMarketDayLow"), meta.get("regularMarketDayHigh")
    if low is not None and high is not None:
        parts.append(f"day range {low:.2f}–{high:.2f}")
    if meta.get("regularMarketTime"):
        zone = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
        when = datetime.fromtimestamp(meta["regularMarketTime"], zone)
        parts.append(f"as of {when:%Y-%m-%d %H:%M} {when.tzname()}")
    if meta.get("exchangeName") or meta.get("fullExchangeName"):
        parts.append(f"on {meta.get('fullExchangeName') or meta.get('exchangeName')}")
    return " · ".join(parts)


async def stock_quote(symbols: list[str], client: httpx.AsyncClient | None = None) -> str:
    cleaned = [s.strip().upper() for s in symbols if s and s.strip()][:MAX_SYMBOLS]
    if not cleaned:
        return "Give at least one stock symbol, e.g. FTNT."
    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        async def one(symbol: str) -> str:
            if not _SYMBOL.match(symbol):
                return f"{symbol}: not a valid symbol"
            try:
                return format_quote(symbol, await fetch_quote(client, symbol))
            except QuoteError as exc:
                return f"{symbol}: {exc}"

        lines = await asyncio.gather(*(one(s) for s in cleaned))
    finally:
        if owns_client:
            await client.aclose()
    return wrap_untrusted("yahoo-finance", "\n".join(lines))


def make_quote_tool() -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        symbols = args.get("symbols") or []
        if isinstance(symbols, str):
            symbols = [s for s in re.split(r"[,\s]+", symbols) if s]
        return await stock_quote(list(symbols))

    return Tool(
        name="stock_quote",
        description=(
            "Real, current stock quotes from the internet (the database only holds demo data): price, "
            "change vs. previous close, day range, time. Symbols like FTNT, AAPL, RY.TO (Toronto), 0700.HK."
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}, "description": "Ticker symbols (max 10)"},
            },
            "required": ["symbols"],
        },
        handler=handler,
    )
