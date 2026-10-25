"""A real quote from the internet (Yahoo Finance). Run with: uv run pytest -m integration"""

import pytest

from agent_demo.quotes import stock_quote

pytestmark = pytest.mark.integration


async def test_real_ftnt_quote():
    text = await stock_quote(["FTNT"])
    assert "FTNT:" in text and "USD" in text and "vs. previous close" in text, text
