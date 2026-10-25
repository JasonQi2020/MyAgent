"""The scheduler tools, for the assistant (registry tools) and for Claude Code (in-process MCP server).

Both use the same handlers, so tasks look the same whoever created them. Only the owner's
conversations get these tools. See design/scheduled_tasks.md §4.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .tasks import (
    EXECUTORS,
    Scheduler,
    TaskError,
    TaskStore,
    _iso,
    check_schedule,
    describe_runs,
    load_task_settings,
    manage,
    tasks_command,
)
from .tools import Tool

log = logging.getLogger("agent_demo.tasks")

Target = Callable[[], tuple[str, str]]  # where reports go: (channel, chat_id); may raise TaskError

ADD_DESCRIPTION = (
    "Schedule a recurring task, e.g. 'query FTNT every day and report to me at 9 PM'. Translate the "
    "user's words into a 5-field cron expression (minute hour day month weekday) in the given time zone, "
    "e.g. '0 21 * * *' = 21:00 every day, '30 8 * * 1-5' = 08:30 Monday to Friday. Write `prompt` as a "
    "standalone instruction: the run does not see this conversation. The run's answer is delivered to this "
    "chat automatically ('report to me' means exactly that): do NOT add sending an email or a message to the "
    "prompt unless the user explicitly asks for email. The user approves before it is saved."
)

# Added to the assistant's system prompt in the owner's conversations.
TASKS_PROMPT = """
- You can schedule recurring tasks for the user (schedule__add, schedule__list, schedule__remove,
  schedule__pause, schedule__resume, schedule__run_now). Times are in {tz} unless the user names a zone.
"""


class TaskService:
    """The store plus (in agent-demo-server) the scheduler that runs it."""

    def __init__(self, store: TaskStore, scheduler: Scheduler | None = None):
        self.store = store
        self.scheduler = scheduler

    # ---- shared handlers ----

    def preview(self, args: dict[str, Any], target: Target) -> dict[str, Any]:
        """Shown in the approval card: the next runs and where reports go."""
        settings = load_task_settings()
        try:
            runs = check_schedule(str(args.get("cron", "")), args.get("time_zone") or settings.timezone,
                                  settings.min_interval_minutes)
            channel, chat_id = target()
            where = "the web UI" if channel == "web" else f"{channel} chat {chat_id}"
            return {"next_runs": describe_runs(runs), "reports_to": where}
        except TaskError as exc:
            return {"problem": str(exc)}

    def add(self, args: dict[str, Any], target: Target, created_by: str, executor: str, model: str) -> str:
        settings = load_task_settings()
        try:
            if not settings.enabled:
                raise TaskError("Scheduled tasks are disabled (tasks.enabled in agent.properties).")
            title, prompt = str(args.get("title", "")).strip(), str(args.get("prompt", "")).strip()
            if not title or not prompt:
                raise TaskError("A task needs a title and a prompt.")
            executor = (args.get("executor") or executor).strip().lower()
            if executor not in EXECUTORS:
                raise TaskError(f"executor must be one of: {', '.join(EXECUTORS)}.")
            enabled = [t for t in self.store.list() if t.enabled]
            if len(enabled) >= settings.max_tasks:
                raise TaskError(f"There are already {len(enabled)} tasks (tasks.max_tasks); remove one first.")
            time_zone = args.get("time_zone") or settings.timezone
            runs = check_schedule(str(args.get("cron", "")), time_zone, settings.min_interval_minutes)
            channel, chat_id = target()
        except TaskError as exc:
            return f"Not added: {exc}"
        task = self.store.add(title=title[:80], prompt=prompt, cron=str(args["cron"]).strip(), time_zone=time_zone,
                              executor=executor, model=model if executor == "assistant" else "-",
                              channel=channel, chat_id=chat_id, created_by=created_by,
                              next_run_at=_iso(runs[0]))
        where = "the web UI" if channel == "web" else "this chat"
        log.info("task #%d '%s' added by %s (%s, %s), reports to %s", task.id, task.title, created_by, task.cron,
                 task.executor, where)
        return f"Added task #{task.id} '{task.title}'. Next runs: {describe_runs(runs)}. Reports go to {where}."

    def manage(self, action: str, args: dict[str, Any]) -> str:
        try:
            return manage(self.store, self.scheduler, action, int(args.get("id")))
        except (TaskError, TypeError, ValueError) as exc:
            return f"⚠ {exc}"

    def list_text(self) -> str:
        return tasks_command(self.store, self.scheduler, "")

    # ---- the assistant's tools ----

    def tools(self, target: Target, created_by: str, model: Callable[[], str]) -> list[Tool]:
        id_schema = {"type": "object", "properties": {"id": {"type": "integer", "description": "Task number"}},
                     "required": ["id"]}

        async def add(args):
            return self.add(args, target, created_by, "assistant", model())

        async def list_tasks(args):
            return self.list_text()

        def managing(action):
            async def handler(args):
                return self.manage(action, args)
            return handler

        return [
            Tool("schedule__add", ADD_DESCRIPTION, ADD_SCHEMA, add, needs_approval=True, source="scheduler",
                 preview=lambda args: self.preview(args, target)),
            Tool("schedule__list", "List the scheduled tasks with their next run and last result.",
                 {"type": "object", "properties": {}}, list_tasks, source="scheduler"),
            *[Tool(f"schedule__{action}", f"{verb} a scheduled task by its number.", id_schema, managing(action),
                   source="scheduler")
              for action, verb in (("remove", "Remove"), ("pause", "Pause"), ("resume", "Resume"),
                                   ("run_now", "Run once now (to test)"))],
        ]

    # ---- Claude Code's tools (in-process MCP server) ----

    def claude_mcp(self, target: Target, created_by: str) -> tuple[dict, dict[str, Callable]]:
        """(mcp_servers for ClaudeAgentOptions, previews for approval cards keyed by tool name)."""
        import claude_agent_sdk as sdk

        def text(value: str) -> dict:
            return {"content": [{"type": "text", "text": value}]}

        @sdk.tool("add", ADD_DESCRIPTION, ADD_SCHEMA)
        async def add(args):
            return text(self.add(args, target, created_by, "claude", "-"))

        @sdk.tool("list", "List the scheduled tasks.", {"type": "object", "properties": {}})
        async def list_tasks(args):
            return text(self.list_text())

        id_schema = {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}
        managers = []
        for action in ("remove", "pause", "resume", "run_now"):
            async def handler(args, action=action):
                return text(self.manage(action, args))
            managers.append(sdk.tool(action, f"{action.replace('_', ' ').capitalize()} a scheduled task.",
                                     id_schema)(handler))

        server = sdk.create_sdk_mcp_server(name="scheduler", tools=[add, list_tasks, *managers])
        return {"scheduler": server}, {"mcp__scheduler__add": lambda args: self.preview(args, target)}


ADD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short name, e.g. 'FTNT daily'"},
        "prompt": {"type": "string", "description": "What to do each run, as a standalone instruction"},
        "cron": {"type": "string", "description": "5-field cron expression, e.g. '0 21 * * *'"},
        "time_zone": {"type": "string", "description": "IANA time zone; default from tasks.timezone"},
        "executor": {"type": "string", "enum": list(EXECUTORS),
                     "description": "Who runs it: assistant (Qwen/GPT, mail, quotes) or claude (Claude Code)"},
    },
    "required": ["title", "prompt", "cron"],
}
