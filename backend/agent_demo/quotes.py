"""stock_quote: real, current stock prices (the database only holds demo data).

Sources: Yahoo Finance's public chart endpoint (no key), then Tencent Finance's quote endpoint
as a fallback when Yahoo is not available (it refuses mainland China with HTTP 429). Both are
unofficial, so everything about a source lives in its fetch function. Both return the same
Yahoo-style fields, so format_quote() serves either.
See design/scheduled_tasks.md §5 and design/deployment.md §7.2.
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
TENCENT_URL = "https://qt.gtimg.cn/q={code}"
_SYMBOL = re.compile(r"^[A-Za-z0-9.\-^=]{1,15}$")
MAX_SYMBOLS = 10


class QuoteError(Exception):
    pass


class UnknownSymbol(QuoteError):
    """The source knows the symbol does not exist: no point asking another source."""


SOURCE_TAGS = {"yahoo": "yahoo-finance", "tencent": "tencent-finance"}


async def fetch_quote(client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
    """Raw quote fields for one symbol, or QuoteError with a readable reason."""
    try:
        response = await client.get(CHART_URL.format(symbol=symbol), headers=HEADERS, timeout=15)
    except httpx.HTTPError as exc:
        raise QuoteError(f"quote service not reachable ({type(exc).__name__})")
    if response.status_code == 404:
        raise UnknownSymbol("unknown symbol")
    if response.status_code != 200:
        raise QuoteError(f"quote service answered HTTP {response.status_code}")
    chart = (response.json() or {}).get("chart") or {}
    results = chart.get("result") or []
    if not results:
        error = (chart.get("error") or {}).get("description") or "no data"
        if "No data found" in error:
            raise UnknownSymbol("unknown symbol")
        raise QuoteError(error)
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
    meta["source"] = "yahoo"
    return meta


# Tencent markets: Yahoo-style symbol -> (Tencent code, currency, time zone, exchange)
_TENCENT_EXCHANGES = {"OQ": "NASDAQ", "N": "NYSE", "AM": "NYSE American"}
_TENCENT_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d%H%M%S")


def tencent_code(symbol: str) -> tuple[str, str, str, str] | None:
    """FTNT -> usFTNT, BRK-B -> usBRK.B, 0700.HK -> hk00700, 600519.SS -> sh600519; None = not covered."""
    if m := re.fullmatch(r"(\d{1,5})\.HK", symbol):
        return f"hk{int(m[1]):05d}", "HKD", "Asia/Hong_Kong", "HKEX"
    if m := re.fullmatch(r"(\d{6})\.(SS|SZ)", symbol):
        market = "sh" if m[2] == "SS" else "sz"
        return f"{market}{m[1]}", "CNY", "Asia/Shanghai", "Shanghai" if market == "sh" else "Shenzhen"
    if re.fullmatch(r"[A-Z]{1,6}([.\-][A-Z])?", symbol):  # US, including class shares (BRK.B / BRK-B)
        return "us" + symbol.replace("-", "."), "USD", "America/New_York", ""
    return None  # e.g. RY.TO, ^GSPC: Tencent does not cover them


async def fetch_quote_tencent(client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
    """The same fields as fetch_quote(), from Tencent Finance (reachable from mainland China)."""
    market = tencent_code(symbol)
    if market is None:
        raise QuoteError("not covered by the fallback source")
    code, currency, zone, exchange = market
    try:
        response = await client.get(TENCENT_URL.format(code=code), headers=HEADERS, timeout=15)
    except httpx.HTTPError as exc:
        raise QuoteError(f"fallback source not reachable ({type(exc).__name__})")
    if response.status_code != 200:
        raise QuoteError(f"fallback source answered HTTP {response.status_code}")
    # One line: v_usFTNT="200~飞塔信息~FTNT.OQ~179.99~172.98~...";  (GBK; unknown codes: v_pv_none_match)
    text = response.content.decode("gbk", errors="replace").strip()
    prefix = f"v_{code}="
    if not text.startswith(prefix):
        raise UnknownSymbol("unknown symbol")
    fields = text[len(prefix):].strip('";\n').split("~")
    if len(fields) < 35:
        raise QuoteError("fallback source sent no data")
    try:
        price, previous = float(fields[3]), float(fields[4])
        high, low = float(fields[33]), float(fields[34])
    except ValueError:
        raise QuoteError("fallback source sent no price")
    if price <= 0:
        raise QuoteError("no price available")
    meta: dict[str, Any] = {
        "symbol": symbol, "currency": currency, "regularMarketPrice": price,
        "previousClose": previous or None, "regularMarketDayLow": low, "regularMarketDayHigh": high,
        "exchangeTimezoneName": zone, "source": "tencent",
        "fullExchangeName": exchange or _TENCENT_EXCHANGES.get(fields[2].rpartition(".")[2], ""),
    }
    for fmt in _TENCENT_TIME_FORMATS:  # in the exchange's own time zone
        try:
            when = datetime.strptime(fields[30], fmt).replace(tzinfo=ZoneInfo(zone))
        except ValueError:
            continue
        meta["regularMarketTime"] = int(when.timestamp())
        break
    return meta


async def fetch_any(client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
    """Yahoo first; Tencent when Yahoo is unavailable (but not when Yahoo says the symbol is unknown)."""
    try:
        return await fetch_quote(client, symbol)
    except UnknownSymbol:
        raise
    except QuoteError as first:
        try:
            return await fetch_quote_tencent(client, symbol)
        except UnknownSymbol:
            raise
        except QuoteError as second:
            raise QuoteError(f"{first}; {second}")


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
    if meta.get("source") == "tencent":
        parts.append("source: Tencent Finance (Yahoo unavailable)")
    return " · ".join(parts)


async def stock_quote(symbols: list[str], client: httpx.AsyncClient | None = None) -> str:
    cleaned = [s.strip().upper() for s in symbols if s and s.strip()][:MAX_SYMBOLS]
    if not cleaned:
        return "Give at least one stock symbol, e.g. FTNT."
    owns_client = client is None
    client = client or httpx.AsyncClient()
    sources: set[str] = set()
    try:
        async def one(symbol: str) -> str:
            if not _SYMBOL.match(symbol):
                return f"{symbol}: not a valid symbol"
            try:
                meta = await fetch_any(client, symbol)
            except QuoteError as exc:
                return f"{symbol}: {exc}"
            sources.add(meta["source"])
            return format_quote(symbol, meta)

        lines = await asyncio.gather(*(one(s) for s in cleaned))
    finally:
        if owns_client:
            await client.aclose()
    tag = "+".join(SOURCE_TAGS[s] for s in SOURCE_TAGS if s in sources) or SOURCE_TAGS["yahoo"]
    return wrap_untrusted(tag, "\n".join(lines))


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
