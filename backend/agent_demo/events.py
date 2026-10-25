"""Events sent from the agent core to a UI, plus the two UI-facing interfaces.

The web UI receives these as JSON (see web/src/protocol.ts, which mirrors this file).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar


@dataclass
class Event:
    type: ClassVar[str] = "event"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **asdict(self)}


@dataclass
class ToolInfo:
    name: str
    source: str
    needs_approval: bool
    blocked: bool


@dataclass
class Ready(Event):
    type: ClassVar[str] = "ready"
    provider: str
    model: str
    tools: list[ToolInfo]
    warnings: list[str] = field(default_factory=list)
    mail: dict[str, str] = field(default_factory=dict)  # provider, label, address


@dataclass
class AssistantText(Event):
    type: ClassVar[str] = "assistant_text"
    text: str
    reasoning: str | None = None


@dataclass
class ToolCallEvent(Event):
    type: ClassVar[str] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any]
    source: str


@dataclass
class ApprovalRequest(Event):
    type: ClassVar[str] = "approval_request"
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResultEvent(Event):
    type: ClassVar[str] = "tool_result"
    id: str
    name: str
    result: str
    is_error: bool


@dataclass
class ErrorEvent(Event):
    type: ClassVar[str] = "error"
    message: str


@dataclass
class TurnDone(Event):
    type: ClassVar[str] = "turn_done"
    steps: int
    prompt_tokens: int
    completion_tokens: int


@dataclass
class ModelChanged(Event):
    """Reply to /strongmodel, /weakmodel, /model (web UI); see models.py."""

    type: ClassVar[str] = "model_changed"
    profile: str  # "weak" | "strong"
    model: str  # "openai / gpt-5.5"
    message: str


@dataclass
class Notice(Event):
    """A short reply to a command (web UI), e.g. /tools or /claude status."""

    type: ClassVar[str] = "notice"
    text: str


@dataclass
class TaskReport(Event):
    """A scheduled task's report for the web UI (tasks created there)."""

    type: ClassVar[str] = "task_report"
    text: str


@dataclass
class ClaudeProgress(Event):
    """One action of a running /claude task (web UI); see claude_code.py."""

    type: ClassVar[str] = "claude_progress"
    text: str


@dataclass
class ClaudeDone(Event):
    """Claude Code's final answer (web UI)."""

    type: ClassVar[str] = "claude_done"
    text: str
    ok: bool


@dataclass
class ModeChanged(Event):
    """Claude mode switched on/off (web UI header)."""

    type: ClassVar[str] = "mode_changed"
    claude_mode: bool
    message: str


# An approval nobody answers within this time counts as "no" (web UI and IM channels).
APPROVAL_TIMEOUT_SECONDS = 300

# The two interfaces every UI implements:
EventSink = Callable[[Event], Awaitable[None]]  # "something happened"
Approver = Callable[[ApprovalRequest], Awaitable[bool]]  # "may I run this?"
