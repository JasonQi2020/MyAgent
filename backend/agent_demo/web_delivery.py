"""Scheduled-task reports for the web UI (design/scheduled_tasks.md §11).

A task created in the web UI reports there: to every open tab, and the assistant in each tab
gets a note for follow-up questions. With no tab open, reports wait in state/web_inbox.json and
are shown when a tab connects. Approvals during a run go to the most recently opened tab; with
no tab open the answer is "no" (as for an unanswered approval card).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import tasks as tasks_module
from .events import ApprovalRequest, TaskReport
from .tasks import Task, execute, report, report_note

log = logging.getLogger("agent_demo.web")

WEB_TARGET = ("web", "owner")  # the local web UI has one user: the owner


@dataclass
class Tab:
    conn: Any  # server.Connection
    agent: Any  # that tab's assistant (for the follow-up note)


class WebDelivery:
    def __init__(self, runtime):
        self.runtime = runtime
        self.tabs: list[Tab] = []

    def _inbox(self):
        return tasks_module.STATE_DIR / "web_inbox.json"

    async def connect(self, conn, agent) -> None:
        """A tab opened: register it and show reports that arrived while no tab was open."""
        self.tabs.append(Tab(conn, agent))
        path = self._inbox()
        try:
            waiting = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (OSError, ValueError):
            waiting = []
        if not waiting:
            return
        for item in waiting:
            await conn.send(TaskReport(item["text"]))
            agent.add_note(item["note"])
        path.unlink(missing_ok=True)

    def disconnect(self, conn) -> None:
        self.tabs = [tab for tab in self.tabs if tab.conn is not conn]

    async def approve(self, request: ApprovalRequest) -> bool:
        if not self.tabs:
            log.info("no web UI tab open to approve %s; treated as no", request.name)
            return False
        return await self.tabs[-1].conn.approve(request)

    async def deliver(self, text: str, note: str) -> None:
        delivered = False
        for tab in list(self.tabs):
            try:
                await tab.conn.send(TaskReport(text))
                tab.agent.add_note(note)
                delivered = True
            except Exception:  # noqa: BLE001 - the tab may have closed meanwhile
                self.disconnect(tab.conn)
        if not delivered:
            path = self._inbox()
            try:
                waiting = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            except (OSError, ValueError):
                waiting = []
            waiting.append({"text": text, "note": note, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(waiting[-50:], ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)

    async def run_task(self, task: Task) -> str:
        ok, text = await execute(task, self.runtime, self.approve)
        await self.deliver(report(task, ok, text), report_note(task, ok, text))
        return "ok" if ok else f"error: {text[:200]}"
