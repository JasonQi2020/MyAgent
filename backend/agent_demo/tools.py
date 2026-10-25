"""Tool definition and registry.

Local tools and MCP tools are stored the same way, so the agent loop never
needs to know where a tool comes from.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# A handler receives the parsed JSON arguments and returns text for the model.
ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]

MAX_RESULT_CHARS = 8000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments
    handler: ToolHandler
    needs_approval: bool = False
    blocked: bool = False
    source: str = "local"  # "local" or "mcp:<server>"
    # Extra facts for the approval request, computed from the arguments (e.g. a schedule's next runs).
    preview: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """Keep huge results (long emails, big queries) from flooding the context window."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n\n[... truncated {omitted} characters. Narrow your request to see more.]"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def with_tools(self, tools: list[Tool]) -> "ToolRegistry":
        """A copy with extra tools (e.g. one bound to a single chat); this registry is unchanged."""
        copy = ToolRegistry()
        copy._tools = dict(self._tools)
        for tool in tools:
            copy.register(tool)
        return copy

    def openai_schemas(self) -> list[dict[str, Any]]:
        """Convert every non-blocked tool to the format the chat-completions API expects."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
            if not t.blocked
        ]
