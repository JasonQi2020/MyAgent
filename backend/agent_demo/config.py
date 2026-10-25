"""Configuration: LLM provider presets, .env (secrets), agent.properties (settings) and the MCP server list.

All paths are resolved relative to the project root (the `agent-demo/` folder),
so the CLI and the web server behave the same no matter where you start them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# agent-demo/backend/agent_demo/config.py -> parents[2] == agent-demo/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Every provider below speaks the OpenAI-compatible chat-completions API,
# so one client (the `openai` SDK) can talk to all of them.
PROVIDERS: dict[str, dict[str, str | None]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": None,
        "key_env": "OPENROUTER_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "key_env": "DASHSCOPE_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": None,
        "key_env": "OPENAI_API_KEY",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": None,
        "key_env": None,
    },
    "custom": {"base_url": None, "model": None, "key_env": None},
}

DEFAULT_PROVIDER = "deepseek"

# Mailboxes that can be chosen with `mail.provider` in agent.properties.
# Each name must match a server with "role": "mail" in mcp_servers.json.
MAIL_PROVIDERS = {"sina": "Sina Mail", "gmail": "Gmail"}
DEFAULT_MAIL_PROVIDER = "sina"


@dataclass
class Settings:
    provider: str
    base_url: str
    api_key: str
    model: str
    db_path: Path
    max_steps: int
    mcp_config_path: Path
    mail_provider: str = DEFAULT_MAIL_PROVIDER
    mail_address: str = ""
    properties: dict[str, str] = field(default_factory=dict)  # everything in agent.properties

    @property
    def mail_label(self) -> str:
        return MAIL_PROVIDERS.get(self.mail_provider, self.mail_provider)


class ConfigError(Exception):
    """Raised when required settings are missing or invalid."""


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_properties(path: Path) -> dict[str, str]:
    """Read a Java-style properties file: `key=value` or `key: value` lines, `#` or `!` comments."""
    properties: dict[str, str] = {}
    if not path.exists():
        return properties
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line[0] in "#!":
            continue
        match = re.match(r"([^=:]+?)\s*[=:]\s*(.*)$", line)
        key, value = (match.group(1), match.group(2)) if match else (line, "")
        properties[key.strip()] = value.strip()
    return properties


def resolve_llm(
    provider: str,
    model: str | None,
    base_url: str | None,
    env: dict[str, str],
    generic_key: str = "first",
    setting: str = "LLM_PROVIDER",
) -> tuple[str, str, str, str]:
    """Provider preset + overrides + keys -> (provider, base_url, api_key, model), or ConfigError.

    `env` holds the keys (os.environ, or a freshly read .env for runtime model switching).
    generic_key: "first" (LLM_API_KEY wins, as in .env so far) or "never" (only the provider's
    own key, e.g. OPENAI_API_KEY: a second profile must not send the first provider's key).
    """
    provider = provider.lower()
    if provider not in PROVIDERS:
        raise ConfigError(f"Unknown {setting} '{provider}'. Choose one of: {', '.join(PROVIDERS)}")
    preset = PROVIDERS[provider]

    base_url = base_url or preset["base_url"]
    model = model or preset["model"]
    key_env = preset["key_env"]
    specific = env.get(key_env) if key_env else None
    generic = env.get("LLM_API_KEY")
    api_key = (generic or specific) if generic_key == "first" else specific

    if not base_url:
        raise ConfigError("LLM_BASE_URL is required for provider 'custom'.")
    if not model:
        raise ConfigError(f"Set LLM_MODEL in .env (provider '{provider}' has no default model).")
    if not api_key:
        if provider == "ollama":
            api_key = "ollama"  # Ollama ignores the key, but the SDK requires one.
        else:
            hint = key_env if generic_key == "never" and key_env else (
                f"{key_env} or LLM_API_KEY" if key_env else "LLM_API_KEY"
            )
            raise ConfigError(f"No API key found. Set {hint} in {PROJECT_ROOT / '.env'}")
    return provider, base_url, api_key, model


def load_settings(
    provider: str | None = None, model: str | None = None, mail: str | None = None
) -> Settings:
    """Build Settings from .env, agent.properties, the provider preset and optional CLI overrides."""
    load_dotenv(PROJECT_ROOT / ".env")
    properties = load_properties(_resolve_path(os.getenv("AGENT_PROPERTIES", "agent.properties")))

    provider, base_url, api_key, model = resolve_llm(
        provider or os.getenv("LLM_PROVIDER") or DEFAULT_PROVIDER,
        model or os.getenv("LLM_MODEL"),
        os.getenv("LLM_BASE_URL"),
        dict(os.environ),
    )

    # --mail (CLI) > AGENT_MAIL_PROVIDER (set by `agent-demo-server --mail`) > agent.properties > default
    mail_provider = (
        mail or os.getenv("AGENT_MAIL_PROVIDER") or properties.get("mail.provider") or DEFAULT_MAIL_PROVIDER
    ).strip().lower()
    if mail_provider not in MAIL_PROVIDERS:
        raise ConfigError(
            f"Unknown mail.provider '{mail_provider}'. Choose one of: {', '.join(MAIL_PROVIDERS)}"
        )

    return Settings(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        db_path=_resolve_path(os.getenv("DB_PATH", "data/demo.db")),
        max_steps=int(os.getenv("MAX_STEPS", "15")),
        mcp_config_path=_resolve_path(os.getenv("MCP_CONFIG", "mcp_servers.json")),
        mail_provider=mail_provider,
        mail_address=properties.get(f"mail.{mail_provider}.address", ""),
        properties=properties,
    )


def load_mcp_config(path: Path) -> dict:
    """Return the `servers` mapping from mcp_servers.json (empty if the file is missing)."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("servers", {})
