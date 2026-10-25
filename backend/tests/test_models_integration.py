"""The real strong model accepts every tool the agent has (OpenAI checks JSON Schema strictly).

Needs OPENAI_API_KEY in agent-demo/.env and a reachable api.openai.com.
Run with:  uv run pytest -m integration   (skipped in a normal full run)
"""

import pytest

from agent_demo.config import ConfigError, load_settings
from agent_demo.models import load_profiles
from agent_demo.runtime import open_runtime

pytestmark = pytest.mark.integration


async def test_strong_model_with_all_tools():
    profiles, _ = load_profiles()
    if isinstance(profiles["strong"], ConfigError):
        pytest.skip(f"strong model not configured: {profiles['strong']}")
    async with open_runtime(load_settings()) as runtime:
        profile, client = runtime.models().resolve("strong")
        reply = await client.chat(
            [{"role": "user", "content": "Which tables are in the database? Call the right tool."}],
            runtime.registry.openai_schemas(),
        )
    assert [call.name for call in reply.tool_calls] == ["db_list_tables"], profile.label
