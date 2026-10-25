"""The real Claude Code inside the real bwrap sandbox.

Needs bwrap, the claude CLI and CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in agent-demo/.env.
Run with:  uv run pytest -m integration   (skipped in a normal full run; uses a little of your plan)
"""

import shutil
import sqlite3

import pytest

from agent_demo.claude_code import ClaudeCodeSession, load_claude_settings, run_claude
from agent_demo.config import PROJECT_ROOT

pytestmark = pytest.mark.integration


async def test_only_the_folder_is_visible(tmp_path):
    settings = load_claude_settings()
    if settings.auth == "none" or not shutil.which("bwrap"):
        pytest.skip("needs bwrap and a Claude sign-in in .env")
    work = tmp_path / "work"
    work.mkdir()
    db = sqlite3.connect(work / "t.db")
    db.execute("create table t (x)")
    db.executemany("insert into t values (?)", [(1,), (2,), (3,)])
    db.commit()
    db.close()
    settings.folders, settings.sandbox, settings.approval, settings.max_turns = [work], True, "full", 12

    async def allow(request):
        return True

    async def progress(line):
        pass

    env_file = PROJECT_ROOT / ".env"
    prompt = (
        "Use the shell for each step and answer with exactly four lines, no file contents: "
        f"1) does {env_file} exist? (yes/no) "
        "2) does /home/claude exist? (yes/no) "
        "3) the number of rows in table t of t.db, using python3 and sqlite3 "
        "4) the names of environment variables that contain the words KEY, SECRET or CODE, or 'none'."
    )
    result = await run_claude(ClaudeCodeSession(), prompt, settings, allow, progress)
    assert result.ok, result.text
    answer = result.text.lower()
    lines = [line for line in answer.splitlines() if line.strip()]
    assert "no" in lines[0] and "no" in lines[1], answer  # .env and the home folder are invisible
    assert "3" in lines[2], answer  # sqlite3 works inside
    for leaked in ("dashscope", "openai_api", "feishu", "sina_auth"):
        assert leaked not in answer, answer  # no .env key in the sandbox's environment
