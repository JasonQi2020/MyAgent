"""Shared start-up for the CLI and the web server: tools + MCP connections + LLM."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from .agent import build_system_prompt
from .config import PROJECT_ROOT, Settings, load_mcp_config
from .db_tools import make_db_tools
from .events import Ready, ToolInfo
from .llm import LLMClient
from .mcp_client import MCPManager
from .models import ModelSwitcher
from .quotes import make_quote_tool
from .tools import ToolRegistry


@dataclass
class Runtime:
    settings: Settings
    llm: LLMClient
    registry: ToolRegistry
    warnings: list[str]
    switcher: ModelSwitcher | None = None  # /strongmodel, /weakmodel; created on first use
    task_service: Any = None  # scheduled tasks (state/tasks.db); created on first use

    def models(self) -> ModelSwitcher:
        if self.switcher is None:
            self.switcher = ModelSwitcher(self.settings, self.llm)
        return self.switcher

    def tasks(self):
        if self.task_service is None:
            from .schedule_tools import TaskService
            from .tasks import STATE_DIR, TaskStore

            self.task_service = TaskService(TaskStore(STATE_DIR / "tasks.db"))
        return self.task_service

    def ready_event(self) -> Ready:
        tools = [
            ToolInfo(t.name, t.source, t.needs_approval, t.blocked) for t in self.registry.all()
        ]
        s = self.settings
        mail = {"provider": s.mail_provider, "label": s.mail_label, "address": s.mail_address}
        return Ready(s.provider, s.model, tools, list(self.warnings), mail)

    def system_prompt(self) -> str:
        s = self.settings
        return build_system_prompt(s.mail_label, s.mail_provider, s.mail_address)


def select_servers(
    servers: dict[str, dict[str, Any]], mail_provider: str
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Keep every server except the mail servers ("role": "mail") that were not selected."""
    mail_servers = [name for name, cfg in servers.items() if cfg.get("role") == "mail"]
    warnings = []
    if mail_servers and mail_provider not in mail_servers:
        warnings.append(
            f"mail.provider is '{mail_provider}', but mcp_servers.json has no '{mail_provider}' "
            f"mail server (found: {', '.join(mail_servers)}). Email is unavailable."
        )
    kept = {name: cfg for name, cfg in servers.items() if cfg.get("role") != "mail" or name == mail_provider}
    return kept, warnings


@asynccontextmanager
async def open_runtime(settings: Settings) -> AsyncIterator[Runtime]:
    registry = ToolRegistry()
    for tool in make_db_tools(settings.db_path):
        registry.register(tool)
    registry.register(make_quote_tool())  # real stock prices (the database is demo data)

    warnings: list[str] = []
    if not settings.db_path.exists():
        warnings.append(f"Database {settings.db_path} not found. Run: uv run python scripts/seed_db.py")

    servers, mail_warnings = select_servers(load_mcp_config(settings.mcp_config_path), settings.mail_provider)
    warnings.extend(mail_warnings)
    # Values for ${NAME} in mcp_servers.json: agent.properties keys, two built-ins, then the environment.
    variables = {**settings.properties, "PYTHON": sys.executable, "PROJECT_ROOT": str(PROJECT_ROOT)}
    async with MCPManager(servers, log_dir=PROJECT_ROOT / "logs", variables=variables) as mcp:
        await mcp.register_tools(registry)
        warnings.extend(mcp.warnings)
        llm = LLMClient(settings.base_url, settings.api_key, settings.model)
        yield Runtime(settings, llm, registry, warnings)
