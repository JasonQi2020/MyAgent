"""The real Claude Code through the SDK, in a temporary folder.

Needs the `claude` CLI and CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in agent-demo/.env.
Run with:  uv run pytest -m integration   (skipped in a normal full run; uses a little of your plan)
"""

import pytest

from agent_demo.claude_code import ClaudeCodeSession, load_claude_settings, run_claude

pytestmark = pytest.mark.integration


async def test_real_claude_code_in_a_temporary_folder(tmp_path):
    settings = load_claude_settings()
    if settings.auth == "none":
        pytest.skip("no CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in .env")
    work, outside = tmp_path / "work", tmp_path / "outside.txt"
    work.mkdir()
    settings.folders, settings.max_turns = [work], 8

    async def deny_everything(request):
        return False

    lines = []

    async def progress(line):
        lines.append(line)

    session = ClaudeCodeSession()
    prompt = f"Create hello.txt containing the word hi. Then try to write bye into {outside}; if refused, say so."
    result = await run_claude(session, prompt, settings, deny_everything, progress)

    assert result.ok, result.text
    assert (work / "hello.txt").read_text().strip() == "hi"
    assert not outside.exists()  # the policy blocked the write outside the folder
    assert "hello.txt" in result.files_changed and session.session_id
    assert any(line.startswith("✗ Write") for line in lines)
