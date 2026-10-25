"""The real Telegram bot: getMe with TELEGRAM_BOT_TOKEN. Run with: uv run pytest -m integration"""

import os

import pytest
from dotenv import dotenv_values

from agent_demo.channels.telegram import TelegramChannel
from agent_demo.config import PROJECT_ROOT

pytestmark = pytest.mark.integration
TOKEN = ({**dotenv_values(PROJECT_ROOT / ".env"), **os.environ}.get("TELEGRAM_BOT_TOKEN") or "").strip()


@pytest.mark.skipif(not TOKEN, reason="TELEGRAM_BOT_TOKEN not set in .env")
async def test_get_me():
    channel = TelegramChannel(TOKEN, set())
    try:
        me = await channel.call("getMe")
    finally:
        await channel.client.aclose()
    assert me["is_bot"] and me["username"].lower().endswith("bot")
