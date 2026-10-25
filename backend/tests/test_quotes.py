"""stock_quote's fallback to Tencent Finance when Yahoo is unavailable (design/deployment.md §7.2).

The Tencent answers below were recorded from the Tencent Cloud VM on 2026-10-25.
"""

import httpx
import pytest

from agent_demo.quotes import fetch_quote_tencent, stock_quote, tencent_code
from agent_demo.server import server_port

FTNT = (
    'v_usFTNT="200~飞塔信息~FTNT.OQ~179.99~172.98~172.88~3225722~0~0~180.20~100~0~0~0~0~0~0~0~0~181.10~100'
    '~0~0~0~0~0~0~0~0~~2026-10-23 16:00:01~7.01~4.05~180.17~171.19~USD~3225722~575657804~0.44~63.60~~74.38'
    '~~5.19~1091.93844~1320.61120~Fortinet, Inc.~2.83~197.13~73.55~0~85.14~~1320.61120~126.66~6.42~GP";\n'
)
TENCENT_HK = 'v_hk00700="100~腾讯控股~00700~386.000~382.400~381.800' + "~0" * 24 + "~2026/10/23 16:08:14~3.600~0.94~388.800~378.000~386.000~0\";\n"
UNKNOWN = 'v_pv_none_match="1";\n'


def transport(yahoo_status=429, tencent=None):
    tencent = tencent or {}

    def handler(request):
        if "yahoo" in request.url.host:
            return httpx.Response(yahoo_status, text="Too Many Requests")
        code = request.url.path.removeprefix("/q=")  # Tencent puts the code in the path: /q=usFTNT
        return httpx.Response(200, content=tencent.get(code, UNKNOWN).encode("gbk"))

    return httpx.MockTransport(handler)


def test_tencent_codes():
    assert tencent_code("FTNT") == ("usFTNT", "USD", "America/New_York", "")
    assert tencent_code("BRK-B")[0] == tencent_code("BRK.B")[0] == "usBRK.B"
    assert tencent_code("0700.HK")[0] == "hk00700"
    assert tencent_code("600519.SS")[0] == "sh600519" and tencent_code("000001.SZ")[0] == "sz000001"
    assert tencent_code("RY.TO") is None and tencent_code("^GSPC") is None


async def test_fallback_when_yahoo_refuses():
    async with httpx.AsyncClient(transport=transport(tencent={"usFTNT": FTNT})) as client:
        text = await stock_quote(["ftnt"], client=client)
    assert text.startswith('<untrusted source="tencent-finance">')
    assert ("FTNT: 179.99 USD · +7.01 (+4.05 %) vs. previous close 172.98 · day range 171.19–180.17 · "
            "as of 2026-10-23 16:00 EDT · on NASDAQ · source: Tencent Finance (Yahoo unavailable)") in text


async def test_fallback_hk_time_zone_and_currency():
    async with httpx.AsyncClient(transport=transport(tencent={"hk00700": TENCENT_HK})) as client:
        meta = await fetch_quote_tencent(client, "0700.HK")
    assert (meta["regularMarketPrice"], meta["previousClose"], meta["currency"]) == (386.0, 382.4, "HKD")
    assert meta["fullExchangeName"] == "HKEX" and meta["regularMarketTime"] == 1792742894  # 16:08:14 HKT


async def test_unknown_and_uncovered_symbols():
    async with httpx.AsyncClient(transport=transport()) as client:
        text = await stock_quote(["NOPEX", "RY.TO"], client=client)
    assert "NOPEX: unknown symbol" in text
    assert "RY.TO: quote service answered HTTP 429; not covered by the fallback source" in text


async def test_yahoo_unknown_symbol_is_final():
    asked = []

    def handler(request):
        asked.append(request.url.host)
        return httpx.Response(404, json={"chart": {"result": None, "error": {"description": "No data found"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await stock_quote(["NOPE"], client=client)
    assert "NOPE: unknown symbol" in text and text.startswith('<untrusted source="yahoo-finance">')
    assert all("yahoo" in host for host in asked)  # no fallback request


@pytest.mark.parametrize("value, port", [(None, 8000), ("", 8000), ("8101", 8101)])
def test_server_port(monkeypatch, value, port):
    if value is None:
        monkeypatch.delenv("AGENT_PORT", raising=False)
    else:
        monkeypatch.setenv("AGENT_PORT", value)
    assert server_port() == port


def test_server_port_rejects_garbage(monkeypatch):
    monkeypatch.setenv("AGENT_PORT", "80a")
    with pytest.raises(SystemExit, match="AGENT_PORT"):
        server_port()
