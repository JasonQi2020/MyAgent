"""deploy/make_vm_env.py: what the VM gets from the laptop's config (design/deployment.md §6.1).

Uses temporary files only: never the real .env or state/.
"""

import importlib.util
import json
import sqlite3
from pathlib import Path

from agent_demo.config import load_properties

_spec = importlib.util.spec_from_file_location(
    "make_vm_env", Path(__file__).resolve().parents[2] / "deploy" / "make_vm_env.py")
make_vm_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_vm_env)

LAPTOP_ENV = """\
# comment
LLM_PROVIDER=qwen
DASHSCOPE_API_KEY=sk-dash
OPENAI_API_KEY=sk-openai
CLAUDE_CODE_OAUTH_TOKEN=tok
DB_PATH=data/demo.db
FEISHU_APP_SECRET="quoted value"
TELEGRAM_BOT_TOKEN=123:abc
TELEGRAM_ALLOWED_USERS=42
SINA_AUTH_CODE=sina
IM_CHANNELS=feishu,telegram,wecom
WECOM_BOT_SECRET=wsec
"""


def test_env_keeps_only_what_the_vm_needs(tmp_path):
    (tmp_path / ".env").write_text(LAPTOP_ENV)
    text, kept, dropped = make_vm_env.build_env(tmp_path / ".env")
    assert kept == ["LLM_PROVIDER", "DASHSCOPE_API_KEY", "DB_PATH", "FEISHU_APP_SECRET", "SINA_AUTH_CODE",
                    "WECOM_BOT_SECRET", "IM_CHANNELS"]
    assert dropped == ["OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"]
    for secret in ("sk-openai", "tok", "123:abc", "TELEGRAM"):
        assert secret not in text
    assert 'FEISHU_APP_SECRET="quoted value"\n' in text and text.endswith("IM_CHANNELS=feishu,wecom\n")


def test_properties_overrides(tmp_path):
    (tmp_path / "agent.properties").write_text("mail.provider=sina\nclaude.enabled=true\ntasks.timezone=America/Los_Angeles\n")
    (tmp_path / "out.properties").write_text(make_vm_env.build_properties(tmp_path / "agent.properties"))
    props = load_properties(tmp_path / "out.properties")
    assert props["claude.enabled"] == "false" and props["tasks.timezone"] == "America/Vancouver"
    assert props["claude.folders"] == "/srv/agents/agent-demo/data" and props["mail.provider"] == "sina"


def test_state_keeps_feishu_and_wecom_only(tmp_path):
    state, out = tmp_path / "state", tmp_path / "out"
    state.mkdir()
    (state / "owner_chats.json").write_text(json.dumps({"feishu": "oc_1", "telegram": "8", "wecom": "u"}))
    (state / "im_groups.json").write_text(json.dumps({"feishu": {"oc_g": {}}, "telegram": {"-100": {}}}))
    with sqlite3.connect(state / "tasks.db") as db:
        db.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, channel TEXT)")
        db.executemany("INSERT INTO tasks (channel) VALUES (?)", [("feishu",), ("web",), ("telegram",), ("wecom",)])
    copied = make_vm_env.build_state(state, out)
    assert json.loads((out / "owner_chats.json").read_text()) == {"feishu": "oc_1", "wecom": "u"}
    assert json.loads((out / "im_groups.json").read_text()) == {"feishu": {"oc_g": {}}}
    with sqlite3.connect(out / "tasks.db") as db:
        assert [r[0] for r in db.execute("SELECT channel FROM tasks ORDER BY id")] == ["feishu", "wecom"]
    assert copied[-1] == "tasks.db (2 Feishu/WeCom tasks)"
    assert all((out / f).stat().st_mode & 0o077 == 0 for f in ("owner_chats.json", "im_groups.json", "tasks.db"))
