"""Web backend: FastAPI + one WebSocket endpoint. Implements EventSink and Approver over JSON.

Protocol (see design/agent-demo-design.md §8 and web/src/protocol.ts):
  client -> server: {"type":"user_message","text":...} | {"type":"approval_response","id":...,"approved":bool}
                    | {"type":"reset"}
  server -> client: ready, assistant_text, tool_call, approval_request, tool_result, error, turn_done,
                    model_changed, notice, mode_changed, claude_progress, claude_done (commands, /claude)
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .agent import Agent
from .config import MAIL_PROVIDERS, PROJECT_ROOT, ConfigError, load_settings
from . import claude_code
from .events import (
    APPROVAL_TIMEOUT_SECONDS,
    ApprovalRequest,
    ClaudeDone,
    ClaudeProgress,
    ErrorEvent,
    Event,
    ModeChanged,
    ModelChanged,
    Notice,
)
from .models import handle_model_command
from .router import route
from .schedule_tools import TASKS_PROMPT
from .tasks import load_task_settings, tasks_command
from .web_delivery import WEB_TARGET, WebDelivery
from .runtime import open_runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    # MCP servers and tools are started once and shared by all browser connections
    # and all IM chats (IM_CHANNELS in .env, see design/im_integration.md).
    settings = load_settings()
    async with open_runtime(settings) as runtime:
        hub = await start_im(app, runtime)
        app.state.web = WebDelivery(runtime)  # task reports for the web UI's tabs
        scheduler = await start_scheduler(runtime, hub, app.state.web)
        app.state.runtime = runtime
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            if hub is not None:
                await hub.stop()


async def start_scheduler(runtime, hub, web: WebDelivery):
    """Scheduled tasks run here (design/scheduled_tasks.md). Each report goes where its task was
    created: web UI tasks to the web UI, Feishu tasks to that Feishu chat."""
    from .tasks import Scheduler

    service = runtime.tasks()
    # Once: tasks created in the web UI used to report to the owner's Feishu DM.
    for task in service.store.list():
        if task.created_by == "web" and task.target != WEB_TARGET:
            service.store.update(task.id, channel=WEB_TARGET[0], chat_id=WEB_TARGET[1])
            log.info("task #%d now reports to the web UI (it was created there)", task.id)

    async def runner(task):
        if task.target == WEB_TARGET:
            return await web.run_task(task)
        if hub is not None:
            return await hub.run_task(task)
        return f"error: channel {task.channel} is not running (IM_CHANNELS)"

    service.scheduler = Scheduler(service.store, runner)
    await service.scheduler.start()
    return service.scheduler


async def start_im(app: FastAPI, runtime):
    """Start the IM channels named in IM_CHANNELS; problems become warnings, never a crash."""
    from .channels import build_channels, enabled_channels

    names = enabled_channels()
    if not names:
        return None
    from .channels.hub import ChatHub  # imported only when IM is enabled

    try:
        channels = build_channels(names)
    except ConfigError as exc:
        runtime.warnings.append(f"IM disabled: {exc}")
        return None
    from .channels.groups import GroupStore

    from .tasks import STATE_DIR

    # Groups enabled at runtime (bot added by an allowed user, or "@bot /enable"); survives restarts.
    # Kept in state/, not data/: data/ is Claude Code's writable sandbox folder.
    groups_file = PROJECT_ROOT / os.getenv("IM_GROUPS_FILE", "state/im_groups.json")
    old_file = PROJECT_ROOT / "data" / "im_groups.json"
    if old_file.exists() and not groups_file.exists() and groups_file.parent == STATE_DIR:
        groups_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.replace(groups_file)
    groups = GroupStore(groups_file)
    hub = ChatHub(
        runtime,
        channels,
        idle_minutes=float(os.getenv("IM_SESSION_IDLE_MINUTES", "120")),
        groups=groups,
        context_messages=int(os.getenv("IM_GROUP_CONTEXT_MESSAGES", "15")),
        context_minutes=float(os.getenv("IM_GROUP_CONTEXT_MINUTES", "30")),
    )
    await hub.start()
    runtime.warnings.extend(hub.warnings)
    for channel in hub.channels.values():
        router = channel.routes()
        if router is not None:  # webhook-based channels (WhatsApp)
            include_first(app, router)
    return hub


def include_first(app: FastAPI, router) -> None:
    """Add routes ahead of the web UI's catch-all "/" mount, which would otherwise answer them (405)."""
    routes = app.router.routes
    count = len(routes)
    app.include_router(router)
    added = routes[count:]
    del routes[count:]
    routes[0:0] = added


log = logging.getLogger("agent_demo.server")

# Headers that tunnels and reverse proxies add (cloudflared, ngrok, nginx). Local
# browsers and the Vite dev proxy send none of them.
PROXY_HEADERS = {b"cf-connecting-ip", b"x-forwarded-for", b"forwarded"}
PUBLIC_PREFIX = "/webhooks/"  # IM webhooks (WhatsApp) are the only paths a tunnel may reach


class TunnelGuard:
    """Requests that came through a tunnel or proxy may only reach the IM webhooks.

    The server listens on 127.0.0.1, but the WhatsApp tunnel forwards internet traffic
    to it. /ws and the web UI have no login and the agent can send email, so they stay
    local even if the tunnel is configured to forward everything.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] in ("http", "websocket")
            and not scope["path"].startswith(PUBLIC_PREFIX)
            and any(name in PROXY_HEADERS for name, _ in scope["headers"])
        ):
            log.warning("refused a forwarded request to %s (only %s* is public)", scope["path"], PUBLIC_PREFIX)
            if scope["type"] == "websocket":
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Forbidden"})
            return
        await self.app(scope, receive, send)


app = FastAPI(title="agent-demo", lifespan=lifespan)
app.add_middleware(TunnelGuard)


class Connection:
    """One browser tab = one conversation."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self.turn: asyncio.Task | None = None

    async def send(self, event: Event) -> None:
        await self.ws.send_json(event.to_dict())

    async def approve(self, request: ApprovalRequest) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[request.id] = future
        await self.send(request)
        try:
            return await asyncio.wait_for(future, APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return False  # no answer means "no"
        finally:
            self.pending.pop(request.id, None)

    def busy(self) -> bool:
        return self.turn is not None and not self.turn.done()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    runtime = ws.app.state.runtime
    conn = Connection(ws)
    # The web UI only listens on localhost, so its user is the owner: /claude and scheduled tasks
    # work here. Reports of tasks created here come back here (WebDelivery, with an inbox for closed tabs).
    agent = Agent(
        runtime.llm,
        runtime.registry.with_tools(runtime.tasks().tools(target=lambda: WEB_TARGET, created_by="web",
                                                          model=lambda: agent.profile)),
        conn.send, conn.approve, runtime.settings.max_steps,
        system_prompt=runtime.system_prompt() + claude_code.ASSISTANT_PROMPT
        + TASKS_PROMPT.format(tz=load_task_settings().timezone),
    )
    models = runtime.models()
    model_warning = models.start(agent)
    claude = claude_code.ClaudeCodeSession()
    await conn.send(runtime.ready_event())
    web = getattr(ws.app.state, "web", None)
    if web is not None:
        await web.connect(conn, agent)  # also shows task reports that arrived while no tab was open
    if agent.profile != "weak" or model_warning:
        await conn.send(ModelChanged(agent.profile, agent.model_label, model_warning or "Using the strong model."))

    async def model_reply(reply, extra: str = "") -> None:
        if reply.ok:
            await conn.send(ModelChanged(agent.profile, agent.model_label, reply.text + extra))
        else:
            await conn.send(ErrorEvent(reply.text))

    async def reset() -> None:
        if conn.busy():
            await conn.send(ErrorEvent("Cannot reset while the agent is working."))
            return
        was_on = claude.mode
        agent.reset()
        claude.reset()
        if reply := models.reset(agent):
            await model_reply(reply)
        if was_on:
            await conn.send(ModeChanged(False, "Claude mode off."))
        await conn.send(Notice("Conversation cleared."))

    async def command(name: str, text: str) -> None:
        if name == "/new":
            await reset()
        elif name == "/tools":
            policy = lambda t: "blocked" if t.blocked else ("ask" if t.needs_approval else "auto")  # noqa: E731
            await conn.send(Notice("Tools: " + ", ".join(f"{t.name} ({policy(t)})" for t in runtime.registry.all())))
        elif name in ("/help", "help"):
            await conn.send(Notice(f"Commands: /new, /tools, /strongmodel, /weakmodel, /model, /tasks. "
                                   f"Claude Code: {claude_code.HELP}."))
        elif name == "/tasks":
            service = runtime.tasks()
            await conn.send(Notice(tasks_command(service.store, service.scheduler, text.strip()[len("/tasks"):].strip())))
        elif (reply := handle_model_command(agent, models, text, conn.busy())) is not None:
            extra = ""
            if name == "/model":
                extra = f" · {claude.status(claude_code.load_claude_settings())}."
            elif reply.changed and claude.mode:
                extra = " Claude mode is on, so your messages still go to Claude Code (/claude off to talk to the assistant)."
            await model_reply(reply, extra)
        else:
            await conn.send(Notice(f"{name} only works in group chats."))

    async def run_claude(prompt: str, settings) -> None:
        async def progress(line: str) -> None:
            await conn.send(ClaudeProgress(line))

        folder = claude.current_folder(settings)
        try:
            mcp, previews = runtime.tasks().claude_mcp(target=lambda: WEB_TARGET, created_by="web")
            result = await claude_code.run_claude(claude, prompt, settings, conn.approve, progress,
                                                  mcp_servers=mcp, previews=previews)
        except asyncio.CancelledError:
            result = claude_code.ClaudeResult(False, "Stopped (/claude stop).")
        try:
            await conn.send(ClaudeDone(claude_code.format_answer(result), result.ok))
        except Exception:  # noqa: BLE001 - the tab may be gone
            pass
        agent.add_note(claude_code.memory_note(prompt, folder, result))  # memory bridge

    async def claude_message(target) -> None:
        settings = claude_code.load_claude_settings()
        prompt = target.text
        if target.kind == "claude_command":
            was_on = claude.mode
            reply, prompt = claude_code.command_reply(claude, target.name, target.text, settings)
            if claude.mode != was_on:
                await conn.send(ModeChanged(claude.mode, reply))
            elif reply:
                await conn.send(Notice(reply))
            if prompt is None:
                return
        if claude.busy():
            await conn.send(ErrorEvent("Claude Code is still working (/claude stop to stop it)."))
            return
        claude.task = asyncio.create_task(run_claude(prompt, settings))

    try:
        while True:
            message: dict[str, Any] = await ws.receive_json()
            kind = message.get("type")
            if kind == "user_message":
                text = str(message.get("text", ""))
                # Commands and Claude Code are handled here; only the rest reaches the assistant.
                target = route(text, claude.mode)
                if target.kind == "command":
                    await command(target.name, text)
                elif target.kind in ("claude_command", "claude"):
                    await claude_message(target)
                elif conn.busy():
                    await conn.send(ErrorEvent("Please wait: the agent is still working."))
                else:
                    # Run the turn in the background so we can keep receiving approval responses.
                    conn.turn = asyncio.create_task(agent.run_turn(text))
            elif kind == "approval_response":
                future = conn.pending.get(str(message.get("id")))
                if future and not future.done():
                    future.set_result(bool(message.get("approved")))
            elif kind == "reset":
                await reset()
            else:
                await conn.send(ErrorEvent(f"Unknown message type: {kind!r}"))
    except WebSocketDisconnect:
        pass
    finally:
        for future in conn.pending.values():
            if not future.done():
                future.set_result(False)
        if conn.busy():
            conn.turn.cancel()
        claude.stop()
        if web is not None:
            web.disconnect(conn)


class WebUIFiles(StaticFiles):
    """Static files where index.html is always revalidated.

    Each build gives the JS/CSS bundles new names and deletes the old ones. A cached
    index.html would point at a deleted bundle (404) and the page would stay blank.
    """

    async def get_response(self, path: str, scope) -> Any:
        response = await super().get_response(path, scope)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


# In production, serve the built web UI (npm run build -> web/dist) from the same server.
_dist = PROJECT_ROOT / "web" / "dist"
if _dist.exists():
    app.mount("/", WebUIFiles(directory=_dist, html=True), name="web")


def server_port() -> int:
    """AGENT_PORT, so several agents can share one machine (design/deployment.md §4). Default 8000."""
    value = os.environ.get("AGENT_PORT", "").strip() or "8000"
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise SystemExit(f"AGENT_PORT must be a port number (1-65535), not {value!r}")
    return int(value)


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Learning agent demo (web server)")
    parser.add_argument("--mail", help="mail provider for this run: " + ", ".join(MAIL_PROVIDERS))
    args = parser.parse_args()
    if args.mail:
        os.environ["AGENT_MAIL_PROVIDER"] = args.mail  # read by load_settings() in the lifespan

    # Our own log lines (IM channel status, ignored senders) next to uvicorn's.
    app_log = logging.getLogger("agent_demo")
    if not app_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        app_log.addHandler(handler)
        app_log.setLevel(logging.INFO)
        app_log.propagate = False

    # Localhost only: there is no login, and this agent can send email.
    uvicorn.run("agent_demo.server:app", host="127.0.0.1", port=server_port())
