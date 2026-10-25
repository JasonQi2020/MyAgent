"""Scheduled tasks: "query FTNT every day and report to me at 9 PM". See design/scheduled_tasks.md.

    store (state/tasks.db) ──▶ Scheduler loop (in agent-demo-server, every 30 s)
                                  └─ due task ─▶ run with its executor (assistant or Claude Code)
                                                   └─ report ─▶ the chat it was created in

Tasks live in state/, outside data/: data/ is Claude Code's writable sandbox folder.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .config import PROJECT_ROOT, _resolve_path, load_properties
from .models import fresh_env

log = logging.getLogger("agent_demo.tasks")

STATE_DIR = PROJECT_ROOT / "state"
EXECUTORS = ("assistant", "claude")
TICK_SECONDS = 30
REPORT_NOTE_CHARS = 1000

# Added to the prompt of every scheduled run.
RUN_NOTE = (
    "This is scheduled task #{id} '{title}' ({when}). The user is not watching: do the work and "
    "write a short report for them. If something needs their approval, ask; no answer means no.\n\n"
    "Task: {prompt}"
)


class TaskError(Exception):
    """A message for the user (and the model)."""


# ---- settings (fresh when used) ---------------------------------------------------------


@dataclass
class TaskSettings:
    enabled: bool = True
    timezone: str = "America/Los_Angeles"
    max_tasks: int = 20
    min_interval_minutes: float = 15
    catch_up_minutes: float = 60


def load_task_settings() -> TaskSettings:
    env = fresh_env()
    props = load_properties(_resolve_path(env.get("AGENT_PROPERTIES", "agent.properties")))
    return TaskSettings(
        enabled=props.get("tasks.enabled", "true").strip().lower() not in ("false", "0", "no", "off"),
        timezone=props.get("tasks.timezone", "").strip() or "America/Los_Angeles",
        max_tasks=int(props.get("tasks.max_tasks") or 20),
        min_interval_minutes=float(props.get("tasks.min_interval_minutes") or 15),
        catch_up_minutes=float(props.get("tasks.catch_up_minutes") or 60),
    )


# ---- schedules ------------------------------------------------------------------------------


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise TaskError(f"Unknown time zone {name!r}; use a name like America/Los_Angeles or Asia/Shanghai.")


def next_runs(cron: str, time_zone: str, count: int = 3, after: datetime | None = None) -> list[datetime]:
    if not croniter.is_valid(cron):
        raise TaskError(f"Invalid schedule {cron!r}: use a 5-field cron expression, e.g. '0 21 * * *' for 21:00 daily.")
    tz = zone(time_zone)
    start = (after or datetime.now(timezone.utc)).astimezone(tz)
    it = croniter(cron, start)
    return [it.get_next(datetime) for _ in range(count)]


def check_schedule(cron: str, time_zone: str, min_interval_minutes: float) -> list[datetime]:
    """Validate a schedule; return its next 3 runs. Refuses schedules that run too often."""
    runs = next_runs(cron, time_zone, count=6)
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(runs, runs[1:])]
    if min(gaps) < min_interval_minutes:
        raise TaskError(f"That schedule runs every {min(gaps):g} minutes; the minimum is "
                        f"{min_interval_minutes:g} (tasks.min_interval_minutes).")
    return runs[:3]


def describe_runs(runs: list[datetime]) -> str:
    return ", ".join(f"{r:%a %b %d %H:%M} {r.tzname()}" for r in runs)


# ---- the store ------------------------------------------------------------------------------


@dataclass
class Task:
    id: int
    title: str
    prompt: str
    cron: str
    time_zone: str
    executor: str
    model: str
    channel: str
    chat_id: str
    created_by: str
    created_at: str
    enabled: bool = True
    next_run_at: str | None = None  # ISO, UTC
    last_run_at: str | None = None
    last_status: str | None = None

    @property
    def target(self) -> tuple[str, str]:
        return self.channel, self.chat_id

    def summary(self) -> str:
        state = "on" if self.enabled else "paused"
        nxt = ""
        if self.enabled and self.next_run_at:
            when = datetime.fromisoformat(self.next_run_at).astimezone(zone(self.time_zone))
            nxt = f", next {when:%a %b %d %H:%M} {when.tzname()}"
        last = f", last: {self.last_status}" if self.last_status else ""
        return (f"#{self.id} {self.title} · `{self.cron}` ({self.time_zone}) · {self.executor}"
                f"{' (' + self.model + ')' if self.executor == 'assistant' else ''} · {state}{nxt}{last}")


_COLUMNS = [f.name for f in fields(Task)]


class TaskStore:
    """SQLite store. Small and synchronous: a handful of rows, used from the event loop."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, prompt TEXT,"
            " cron TEXT, time_zone TEXT, executor TEXT, model TEXT, channel TEXT, chat_id TEXT,"
            " created_by TEXT, created_at TEXT, enabled INTEGER, next_run_at TEXT, last_run_at TEXT,"
            " last_status TEXT)"
        )
        self.db.commit()

    def _task(self, row: tuple) -> Task:
        data = dict(zip(_COLUMNS, row))
        data["enabled"] = bool(data["enabled"])
        return Task(**data)

    def list(self) -> list[Task]:
        return [self._task(r) for r in self.db.execute(f"SELECT {', '.join(_COLUMNS)} FROM tasks ORDER BY id")]

    def get(self, task_id: int) -> Task:
        row = self.db.execute(f"SELECT {', '.join(_COLUMNS)} FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskError(f"There is no task #{task_id}. Use /tasks to list them.")
        return self._task(row)

    def add(self, **values: Any) -> Task:
        values.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        values["enabled"] = int(values.get("enabled", True))
        names = [n for n in _COLUMNS if n != "id" and n in values]
        cursor = self.db.execute(
            f"INSERT INTO tasks ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            [values[n] for n in names],
        )
        self.db.commit()
        return self.get(cursor.lastrowid)

    def update(self, task_id: int, **values: Any) -> Task:
        if "enabled" in values:
            values["enabled"] = int(values["enabled"])
        sets = ", ".join(f"{name} = ?" for name in values)
        self.db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", [*values.values(), task_id])
        self.db.commit()
        return self.get(task_id)

    def remove(self, task_id: int) -> Task:
        task = self.get(task_id)
        self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.db.commit()
        return task


# ---- where reports go -----------------------------------------------------------------------


def _owner_chats_file() -> Path:
    return STATE_DIR / "owner_chats.json"


def remember_owner_chat(channel: str, chat_id: str) -> None:
    """The owner's direct chat per channel: reports of CLI/web tasks go there."""
    path = _owner_chats_file()
    try:
        chats = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        chats = {}
    if chats.get(channel) == chat_id:
        return
    chats[channel] = chat_id
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(chats, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def owner_chat(channel: str = "feishu") -> tuple[str, str]:
    path = _owner_chats_file()
    try:
        chat_id = json.loads(path.read_text(encoding="utf-8")).get(channel) if path.exists() else None
    except (OSError, ValueError):
        chat_id = None
    if not chat_id:
        raise TaskError("I don't know your Feishu chat yet: send the bot one direct message in Feishu first, "
                        "so scheduled reports can be delivered there. Only the user can fix this: tell them, and "
                        "do not retry or work around it (no email variants) until they have done it.")
    return channel, chat_id


# ---- the scheduler --------------------------------------------------------------------------

Runner = Callable[[Task], Awaitable[str]]  # runs a task, returns a short status ("ok" / "error: …")


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


class Scheduler:
    """Runs due tasks. One per server; `clock` is replaceable for tests."""

    def __init__(self, store: TaskStore, runner: Runner, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.running: dict[int, asyncio.Task] = {}
        self._loop: asyncio.Task | None = None

    async def start(self) -> None:
        self.catch_up()
        self._loop = asyncio.create_task(self._run_forever())
        log.info("scheduler started (%d tasks)", len(self.store.list()))

    async def stop(self) -> None:
        if self._loop:
            self._loop.cancel()
        for task in list(self.running.values()):
            task.cancel()
        await asyncio.gather(*self.running.values(), return_exceptions=True)

    async def drain(self) -> None:
        await asyncio.gather(*self.running.values(), return_exceptions=True)

    def catch_up(self) -> None:
        """On start-up: a missed run less than tasks.catch_up_minutes late runs once; older ones are skipped."""
        settings, now = load_task_settings(), self.clock()
        for task in self.store.list():
            if not task.enabled:
                continue
            due = datetime.fromisoformat(task.next_run_at) if task.next_run_at else None
            if due is None:
                self.store.update(task.id, next_run_at=_iso(next_runs(task.cron, task.time_zone, 1, now)[0]))
            elif due < now and now - due > timedelta(minutes=settings.catch_up_minutes):
                log.warning("task #%d missed its run at %s (server not running); skipped", task.id, task.next_run_at)
                self.store.update(task.id, next_run_at=_iso(next_runs(task.cron, task.time_zone, 1, now)[0]),
                                  last_status=f"missed {due:%Y-%m-%d %H:%M} UTC (server not running)")
            # due < now but recent: left as it is, so the next tick runs it once

    async def _run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad row must not stop the scheduler
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    def tick(self) -> list[int]:
        """Start every due task; returns their ids."""
        if not load_task_settings().enabled:
            return []
        now, started = self.clock(), []
        for task in self.store.list():
            if not task.enabled or task.id in self.running or not task.next_run_at:
                continue
            if datetime.fromisoformat(task.next_run_at) <= now:
                # The next run is set before this one starts, so a slow run never fires twice.
                self.store.update(task.id, next_run_at=_iso(next_runs(task.cron, task.time_zone, 1, now)[0]))
                self._start(task)
                started.append(task.id)
        return started

    def run_now(self, task_id: int) -> Task:
        task = self.store.get(task_id)
        if task.id in self.running:
            raise TaskError(f"Task #{task.id} is already running.")
        self._start(task)
        return task

    def _start(self, task: Task) -> None:
        async def run() -> None:
            try:
                status = await self.runner(task)
            except asyncio.CancelledError:
                status = "stopped"
            except Exception as exc:  # noqa: BLE001
                log.exception("task #%d failed", task.id)
                status = f"error: {exc}"
            finally:
                self.running.pop(task.id, None)
            try:
                self.store.update(task.id, last_run_at=_iso(self.clock()), last_status=status)
            except TaskError:
                pass  # removed while running
            log.info("task #%d '%s' ran: %s", task.id, task.title, status)

        self.running[task.id] = asyncio.create_task(run())


# ---- running one task ------------------------------------------------------------------------


def run_prompt(task: Task, now: datetime) -> str:
    when = now.astimezone(zone(task.time_zone))
    return RUN_NOTE.format(id=task.id, title=task.title, when=f"{when:%Y-%m-%d %H:%M} {when.tzname()}",
                           prompt=task.prompt)


async def execute(task: Task, runtime, approve, clock: Callable[[], datetime] | None = None) -> tuple[bool, str]:
    """Run the task's prompt with its executor. Returns (ok, answer text)."""
    from . import claude_code
    from .agent import Agent
    from .events import AssistantText, ErrorEvent

    prompt = run_prompt(task, (clock or (lambda: datetime.now(timezone.utc)))())
    if task.executor == "claude":
        result = await claude_code.run_claude(claude_code.ClaudeCodeSession(), prompt,
                                              claude_code.load_claude_settings(), approve, _quiet)
        return result.ok, result.text

    llm = runtime.llm
    if task.model == "strong":
        try:
            llm = runtime.models().resolve("strong")[1]
        except Exception as exc:  # noqa: BLE001 - e.g. the key was removed: fall back, but say so
            log.warning("task #%d: strong model unavailable (%s); using the weak model", task.id, exc)
    texts, errors = [], []

    async def sink(event) -> None:
        if isinstance(event, AssistantText) and event.text.strip():
            texts.append(event.text)
        elif isinstance(event, ErrorEvent):
            errors.append(event.message)

    agent = Agent(llm, runtime.registry, sink, approve, runtime.settings.max_steps,
                  system_prompt=runtime.system_prompt())
    await agent.run_turn(prompt)
    if texts:
        return True, texts[-1]
    return False, errors[-1] if errors else "The run ended without an answer."


async def _quiet(line: str) -> None:
    pass


def report(task: Task, ok: bool, text: str) -> str:
    return f"⏰ {task.title} (task #{task.id})\n\n{text if ok else '⚠ ' + text}"


def report_note(task: Task, ok: bool, text: str) -> str:
    short = text if len(text) <= REPORT_NOTE_CHARS else text[:REPORT_NOTE_CHARS] + " …"
    return (f"[Note: scheduled task #{task.id} '{task.title}' just ran ({'ok' if ok else 'failed'}) and sent "
            f"this report to the user: {short}]")


# ---- commands (/tasks …) -----------------------------------------------------------------------


def tasks_command(store: TaskStore, scheduler: Scheduler | None, arg: str) -> str:
    words = arg.split()
    if not words or words[0].lower() == "list":
        tasks = store.list()
        if not tasks:
            return "No scheduled tasks. Ask me, e.g. \"query FTNT every day and report to me at 9 PM\"."
        return "Scheduled tasks:\n" + "\n".join(f"- {t.summary()}" for t in tasks)
    action = words[0].lower()
    if action not in ("run", "pause", "resume", "remove") or len(words) != 2 or not words[1].lstrip("#").isdigit():
        return "Use: /tasks, /tasks run <id>, /tasks pause <id>, /tasks resume <id>, /tasks remove <id>."
    task_id = int(words[1].lstrip("#"))
    try:
        return manage(store, scheduler, action, task_id)
    except TaskError as exc:
        return f"⚠ {exc}"


def manage(store: TaskStore, scheduler: Scheduler | None, action: str, task_id: int) -> str:
    task = store.get(task_id)
    log.info("task #%d '%s': %s", task.id, task.title, action)
    if action == "remove":
        if scheduler and task.id in scheduler.running:
            scheduler.running[task.id].cancel()
        store.remove(task.id)
        return f"Task #{task.id} '{task.title}' removed."
    if action == "pause":
        store.update(task.id, enabled=False)
        return f"Task #{task.id} '{task.title}' paused."
    if action == "resume":
        nxt = next_runs(task.cron, task.time_zone, 1)[0]
        store.update(task.id, enabled=True, next_run_at=_iso(nxt))
        return f"Task #{task.id} '{task.title}' resumed. Next run: {describe_runs([nxt])}."
    if scheduler is None:
        raise TaskError("Tasks run in agent-demo-server; start it to run a task now.")
    scheduler.run_now(task.id)
    return f"Running task #{task.id} '{task.title}' now; the report follows."
