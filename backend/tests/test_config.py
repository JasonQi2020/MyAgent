"""Settings come from env vars, agent.properties, provider presets and CLI overrides.

The real .env and agent.properties are never read here.
"""

import json

import pytest

import agent_demo.config as config
from agent_demo.agent import build_system_prompt
from agent_demo.config import PROJECT_ROOT, ConfigError, load_mcp_config, load_properties, load_settings
from agent_demo.mcp_client import MCPManager, MissingVariable, expand
from agent_demo.runtime import select_servers

ENV_VARS = [
    "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "DB_PATH", "MAX_STEPS", "MCP_CONFIG",
    "AGENT_PROPERTIES", "AGENT_MAIL_PROVIDER",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_PROPERTIES", str(tmp_path / "agent.properties"))  # missing by default
    return monkeypatch


@pytest.fixture
def properties_file(tmp_path, clean_env):
    clean_env.setenv("DEEPSEEK_API_KEY", "k")
    path = tmp_path / "agent.properties"

    def write(text: str):
        path.write_text(text, encoding="utf-8")
        return path

    return write


def test_default_provider_is_deepseek(clean_env):
    clean_env.setenv("DEEPSEEK_API_KEY", "k")
    s = load_settings()
    assert (s.provider, s.base_url, s.model, s.api_key) == ("deepseek", "https://api.deepseek.com", "deepseek-chat", "k")
    assert s.db_path == PROJECT_ROOT / "data" / "demo.db"
    assert s.max_steps == 15


def test_qwen_preset_and_overrides(clean_env):
    clean_env.setenv("LLM_PROVIDER", "QWEN")  # case-insensitive
    clean_env.setenv("DASHSCOPE_API_KEY", "k")
    assert load_settings().model == "qwen-plus"
    # CLI arguments win over the preset and .env.
    s = load_settings(model="qwen-max")
    assert (s.provider, s.model) == ("qwen", "qwen-max")
    clean_env.setenv("LLM_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    assert "intl" in load_settings().base_url


def test_llm_api_key_works_for_any_provider(clean_env):
    clean_env.setenv("LLM_API_KEY", "generic")
    clean_env.setenv("LLM_MODEL", "some/model")
    assert load_settings("openrouter").api_key == "generic"


def test_ollama_needs_no_key(clean_env):
    clean_env.setenv("LLM_MODEL", "qwen3:8b")
    assert load_settings("ollama").api_key == "ollama"


@pytest.mark.parametrize(
    "provider, env, message",
    [
        ("nope", {}, "Unknown LLM_PROVIDER"),
        ("deepseek", {}, "No API key found"),
        ("openai", {"OPENAI_API_KEY": "k"}, "Set LLM_MODEL"),
        ("custom", {"LLM_API_KEY": "k", "LLM_MODEL": "m"}, "LLM_BASE_URL is required"),
    ],
)
def test_config_errors(clean_env, provider, env, message):
    for name, value in env.items():
        clean_env.setenv(name, value)
    with pytest.raises(ConfigError, match=message):
        load_settings(provider)


def test_relative_and_absolute_paths(clean_env, tmp_path):
    clean_env.setenv("DEEPSEEK_API_KEY", "k")
    clean_env.setenv("DB_PATH", "other/x.db")
    clean_env.setenv("MCP_CONFIG", str(tmp_path / "mcp.json"))
    s = load_settings()
    assert s.db_path == PROJECT_ROOT / "other" / "x.db"
    assert s.mcp_config_path == tmp_path / "mcp.json"


def test_load_mcp_config(tmp_path):
    assert load_mcp_config(tmp_path / "missing.json") == {}
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {"mail": {"command": "x"}}}))
    assert load_mcp_config(path) == {"mail": {"command": "x"}}


def test_project_mcp_config_is_safe():
    """The real mcp_servers.json: only read-only Gmail tools may skip approval."""
    servers = load_mcp_config(PROJECT_ROOT / "mcp_servers.json")
    mail = {name: cfg for name, cfg in servers.items() if cfg.get("role") == "mail"}
    assert set(mail) == set(config.MAIL_PROVIDERS)  # every choice in agent.properties has a server
    risky = {"send_email", "draft_email", "delete_email", "batch_delete_emails", "modify_email",
             "mark_read", "move_email"}
    for name, cfg in mail.items():
        assert cfg["untrusted"] is True, name
        assert not risky & set(cfg["auto_approve"]), name
    # Secrets come from .env through placeholders, never written into the file.
    assert mail["sina"]["env"]["EMAIL_PASSWORD"] == "${SINA_AUTH_CODE}"


# ---- agent.properties and the mail provider ---------------------------------


def test_load_properties(tmp_path):
    path = tmp_path / "a.properties"
    path.write_text(
        "# comment\n! also a comment\n\nmail.provider = gmail\nkey: value\nurl=https://x/?a=b\nflag\n",
        encoding="utf-8",
    )
    assert load_properties(path) == {
        "mail.provider": "gmail", "key": "value", "url": "https://x/?a=b", "flag": "",
    }
    assert load_properties(tmp_path / "missing") == {}


def test_mail_provider_defaults_to_sina(properties_file, clean_env):
    s = load_settings()  # no properties file at all
    assert (s.mail_provider, s.mail_label, s.mail_address) == ("sina", "Sina Mail", "")


def test_mail_provider_from_file_and_overrides(properties_file, clean_env):
    properties_file("mail.provider=gmail\nmail.sina.address=me@sina.com\nmail.gmail.address=me@gmail.com\n")
    s = load_settings()
    assert (s.mail_provider, s.mail_address) == ("gmail", "me@gmail.com")
    assert s.properties["mail.sina.address"] == "me@sina.com"
    # `agent-demo-server --mail` sets AGENT_MAIL_PROVIDER; the CLI's --mail wins over everything.
    clean_env.setenv("AGENT_MAIL_PROVIDER", "sina")
    assert load_settings().mail_address == "me@sina.com"
    assert load_settings(mail="GMAIL").mail_provider == "gmail"


def test_unknown_mail_provider(properties_file):
    properties_file("mail.provider=qq\n")
    with pytest.raises(ConfigError, match="Unknown mail.provider 'qq'"):
        load_settings()


def test_system_prompt_names_the_mailbox():
    prompt = build_system_prompt("Sina Mail", "sina", "jasonqi8@sina.com")
    assert "the user's email (Sina Mail, address jasonqi8@sina.com): tools whose names start with sina__" in prompt
    assert "gmail" not in prompt.lower()
    assert "if a mail server is connected" in build_system_prompt()


def test_select_servers():
    servers = {"sina": {"role": "mail"}, "gmail": {"role": "mail"}, "db": {"command": "x"}}
    kept, warnings = select_servers(servers, "gmail")
    assert set(kept) == {"gmail", "db"} and warnings == []
    kept, warnings = select_servers({"gmail": {"role": "mail"}, "db": {}}, "sina")
    assert set(kept) == {"db"} and "no 'sina' mail server" in warnings[0]


# ---- ${NAME} placeholders in mcp_servers.json --------------------------------


def test_expand_placeholders(clean_env):
    clean_env.setenv("SECRET_CODE", "s3cret")
    clean_env.setenv("mail.host", "from-env")
    variables = {"mail.host": "imap.sina.com"}
    assert expand("${mail.host}:${SECRET_CODE}", variables) == "imap.sina.com:s3cret"  # properties first
    assert expand("no placeholders", variables) == "no placeholders"
    clean_env.setenv("EMPTY", "")
    with pytest.raises(MissingVariable, match="EMPTY"):
        expand("${EMPTY}", variables)


async def test_missing_secret_skips_server_without_leaking(tmp_path, clean_env):
    clean_env.delenv("SINA_AUTH_CODE", raising=False)
    servers = {"sina": {"command": "${PYTHON}", "env": {"EMAIL_USER": "${mail.sina.address}",
                                                        "EMAIL_PASSWORD": "${SINA_AUTH_CODE}"}}}
    variables = {"PYTHON": "python", "mail.sina.address": "me@sina.com"}
    async with MCPManager(servers, log_dir=tmp_path, variables=variables) as mcp:
        assert mcp.sessions == {}
    assert mcp.warnings == [
        "MCP server 'sina' not started: ${SINA_AUTH_CODE} is not set. Add SINA_AUTH_CODE to .env (secrets) "
        "or agent.properties."
    ]
