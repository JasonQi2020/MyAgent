"""Scheduled tasks (design/scheduled_tasks.md): schedules, store, scheduler, tools, runs, quotes."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import agent_demo.claude_code as cc
import agent_demo.tasks as tasks
from agent_demo.claude_code import decide
from agent_demo.llm import LLMReply, ToolCall
from agent_demo.quotes import stock_quote
from agent_demo.schedule_tools import TaskService
from agent_demo.tools import ToolRegistry
from agent_demo.tasks import Scheduler, TaskError, TaskStore, check_schedule, next_runs, owner_chat, tasks_command
from test_im_hub import FakeChannel, dm, make_hub

LA = "America/Los_Angeles"
TARGET = ("fake", "dm-ou_owner")


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "state" / "tasks.db")


def add(store, cron="0 21 * * *", executor="assistant", **kw):
    service = TaskService(store)
    text = service.add({"title": "FTNT daily", "prompt": "Report the FTNT quote.", "cron": cron, **kw},
                       lambda: TARGET, "ou_owner", executor, "weak")
    assert text.startswith("Added task #"), text
    return store.list()[-1]


# ---- schedules -------------------------------------------------------------------------


def test_next_runs_keep_local_time_across_dst():
    runs = next_runs("0 21 * * *", LA, 4, after=datetime(2026, 10, 30, 19, 0, tzinfo=timezone.utc))
    assert [r.strftime("%m-%d %H:%M %Z") for r in runs] == ["10-30 21:00 PDT", "10-31 21:00 PDT",
                                                           "11-01 21:00 PST", "11-02 21:00 PST"]


def test_check_schedule_errors():
    with pytest.raises(TaskError, match="Invalid schedule"):
        check_schedule("every day at nine", LA, 15)
    with pytest.raises(TaskError, match="Unknown time zone"):
        check_schedule("0 21 * * *", "Mars/Olympus", 15)
    with pytest.raises(TaskError, match="minimum is 15"):
        check_schedule("*/5 * * * *", LA, 15)
    assert len(check_schedule("30 8 * * 1-5", "Asia/Shanghai", 15)) == 3


# ---- store and service -------------------------------------------------------------------


def test_store_and_commands(store):
    task = add(store)
    assert task.id == 1 and task.target == TARGET and task.enabled and task.next_run_at
    assert TaskStore(store.path).get(1).title == "FTNT daily"  # persisted
    assert "#1 FTNT daily · `0 21 * * *` (America/Los_Angeles) · assistant (weak) · on, next" in \
        tasks_command(store, None, "")
    assert tasks_command(store, None, "pause 1") == "Task #1 'FTNT daily' paused."
    assert "paused" in tasks_command(store, None, "list")
    assert tasks_command(store, None, "resume #1").startswith("Task #1 'FTNT daily' resumed. Next run:")
    assert "start it to run a task now" in tasks_command(store, None, "run 1")
    assert tasks_command(store, None, "remove 1") == "Task #1 'FTNT daily' removed."
    assert tasks_command(store, None, "remove 9") == "⚠ There is no task #9. Use /tasks to list them."
    assert tasks_command(store, None, "explode 1").startswith("Use: /tasks")


def test_add_validation_and_limit(store, monkeypatch):
    service = TaskService(store)
    bad = service.add({"title": "x", "prompt": "y", "cron": "*/1 * * * *"}, lambda: TARGET, "o", "assistant", "weak")
    assert bad.startswith("Not added:") and "minimum" in bad
    assert "executor must be" in service.add({"title": "x", "prompt": "y", "cron": "0 9 * * *", "executor": "robot"},
                                            lambda: TARGET, "o", "assistant", "weak")
    monkeypatch.setattr(tasks, "load_task_settings", lambda: tasks.TaskSettings(max_tasks=1))
    import agent_demo.schedule_tools as st
    monkeypatch.setattr(st, "load_task_settings", lambda: tasks.TaskSettings(max_tasks=1))
    add(store)
    assert "already 1 tasks" in service.add({"title": "x", "prompt": "y", "cron": "0 9 * * *"},
                                            lambda: TARGET, "o", "assistant", "weak")


def test_owner_chat_must_be_known():
    with pytest.raises(TaskError, match="send the bot one direct message"):
        owner_chat("feishu")
    tasks.remember_owner_chat("feishu", "oc_me")
    assert owner_chat("feishu") == ("feishu", "oc_me")


async def test_add_tool_needs_approval_and_previews(store):
    tools = {t.name: t for t in TaskService(store).tools(lambda: TARGET, "ou_owner", lambda: "strong")}
    add_tool = tools["schedule__add"]
    assert add_tool.needs_approval and not tools["schedule__list"].needs_approval
    preview = add_tool.preview({"title": "x", "prompt": "y", "cron": "0 21 * * *"})
    assert "PDT" in preview["next_runs"] or "PST" in preview["next_runs"]
    assert preview["reports_to"] == "fake chat dm-ou_owner"
    assert "problem" in add_tool.preview({"cron": "nope"})
    await add_tool.handler({"title": "FTNT daily", "prompt": "p", "cron": "0 21 * * *"})
    assert store.get(1).model == "strong"  # the conversation's model profile at creation


# ---- the scheduler (fake clock) -----------------------------------------------------------


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


async def test_scheduler_runs_due_tasks_once(store):
    task = add(store)
    due = datetime.fromisoformat(task.next_run_at)
    ran = []

    async def runner(t):
        ran.append(t.id)
        await asyncio.sleep(0.05)
        return "ok"

    clock = Clock(due - timedelta(minutes=1))
    scheduler = Scheduler(store, runner, clock)
    assert scheduler.tick() == []  # not yet
    clock.now = due + timedelta(seconds=5)
    assert scheduler.tick() == [task.id]
    assert scheduler.tick() == []  # running / next run already moved to tomorrow
    await scheduler.drain()
    after = store.get(task.id)
    assert ran == [task.id] and after.last_status == "ok"
    assert datetime.fromisoformat(after.next_run_at) - due == timedelta(days=1)


async def test_catch_up_runs_recent_and_skips_old(store):
    recent, old = add(store), add(store, cron="0 9 * * *")
    clock = Clock(datetime.fromisoformat(recent.next_run_at) + timedelta(minutes=30))
    store.update(old.id, next_run_at=(clock.now - timedelta(hours=5)).isoformat())

    async def runner(t):
        return "ok"

    scheduler = Scheduler(store, runner, clock)
    scheduler.catch_up()
    assert "missed" in store.get(old.id).last_status
    assert datetime.fromisoformat(store.get(old.id).next_run_at) > clock.now  # skipped to the next run
    assert scheduler.tick() == [recent.id]  # caught up once
    await scheduler.drain()


async def test_failed_and_paused_tasks(store):
    task, paused = add(store), add(store, cron="0 8 * * *")
    store.update(paused.id, enabled=False)

    async def runner(t):
        raise RuntimeError("quote service down")

    clock = Clock(datetime.now(timezone.utc) + timedelta(days=2))
    scheduler = Scheduler(store, runner, clock)
    assert scheduler.tick() == [task.id]
    await scheduler.drain()
    assert store.get(task.id).last_status == "error: quote service down"


# ---- runs through the hub ------------------------------------------------------------------


@pytest.fixture
def owner_hub(tmp_path, monkeypatch):
    settings = cc.ClaudeSettings(enabled=True, folders=[tmp_path], max_turns=5, timeout_minutes=1, max_budget_usd=1,
                                 model="", owners={"fake": "ou_owner"}, env={}, auth="subscription", sandbox=False)
    monkeypatch.setattr(cc, "load_claude_settings", lambda: settings)
    return settings


async def test_assistant_run_delivers_report_and_note(owner_hub, store):
    hub, ch = make_hub([LLMReply(content="FTNT closed at 179.99 USD (+4.05 %).")])
    hub.runtime.task_service = TaskService(store)
    task = add(store)
    status = await hub.run_task(task)
    assert status == "ok"
    assert ch.sent[-1] == ("dm-ou_owner", "⏰ FTNT daily (task #1)\n\nFTNT closed at 179.99 USD (+4.05 %).", None)
    prompt = hub.runtime.llm.calls[0][0][-1]["content"]
    assert "This is scheduled task #1 'FTNT daily'" in prompt and "Task: Report the FTNT quote." in prompt
    session = hub.sessions[TARGET]
    assert any("scheduled task #1" in n for n in session.agent._notes)  # for follow-up questions


async def test_approval_during_a_run_goes_to_the_chat(owner_hub, store):
    hub, ch = make_hub([LLMReply(content=None, tool_calls=[ToolCall("c1", "mail__send_email", '{"to": "me"}')]),
                        LLMReply(content="Sent.")])
    hub.runtime.task_service = TaskService(store)
    run = asyncio.create_task(hub.run_task(add(store)))
    for _ in range(100):
        if ch.approvals:
            break
        await asyncio.sleep(0.01)
    assert ch.approvals[0][0] == "dm-ou_owner" and ch.approvals[0][1].name == "mail__send_email"
    await hub.on_message(dm("no", "m1"))  # the owner says no
    assert await run == "ok"
    assert "⏰ FTNT daily (task #1)" in ch.texts()[-1]


async def test_claude_executor(owner_hub, store, monkeypatch):
    import claude_agent_sdk

    from test_claude_code import FakeClaude

    fake = FakeClaude([], answer="FTNT 179.99")
    monkeypatch.setattr(claude_agent_sdk, "query", fake)
    hub, ch = make_hub([])
    task = add(store, executor="claude")
    assert await hub.run_task(task) == "ok"
    assert "This is scheduled task #1" in fake.prompts[0] and ch.texts()[-1].endswith("FTNT 179.99")
    assert hub.runtime.llm.calls == []  # the assistant was not involved


async def test_tasks_command_owner_only(owner_hub, store):
    hub, ch = make_hub([], channel=FakeChannel(allowed_users=("ou_owner", "ou_amy")))
    hub.runtime.task_service = TaskService(store)
    await hub.on_message(dm("/tasks", "m1"))
    await hub.on_message(dm("/tasks", "m2", user="ou_amy"))
    assert ch.texts()[0].startswith("No scheduled tasks") and "only available in the owner's" in ch.texts()[1]
    owner_tools = {t.name for t in hub.sessions[TARGET].agent.registry.all()}
    amy_tools = {t.name for t in hub.sessions[("fake", "dm-ou_amy")].agent.registry.all()}
    assert "schedule__add" in owner_tools and "schedule__add" not in amy_tools
    assert owner_chat("fake") == TARGET  # the owner's DM was remembered for CLI/web reports


def test_claude_policy_for_scheduler_tools(tmp_path):
    for level in ("ask", "auto", "full"):
        assert decide("mcp__scheduler__add", {}, tmp_path, [tmp_path], level)[0] == "ask"
        assert decide("mcp__scheduler__list", {}, tmp_path, [tmp_path], level)[0] == "allow"
        assert decide("mcp__github__create_issue", {}, tmp_path, [tmp_path], level)[0] == "deny"
        # Claude Code loads MCP tool schemas on demand with ToolSearch; without it, it cannot call them.
        assert decide("ToolSearch", {"query": "select:mcp__scheduler__add"}, tmp_path, [tmp_path], level)[0] == "allow"


def test_claude_mcp_server_is_built(store):
    servers, previews = TaskService(store).claude_mcp(lambda: TARGET, "ou_owner")
    assert set(servers) == {"scheduler"} and "mcp__scheduler__add" in previews
    assert "next_runs" in previews["mcp__scheduler__add"]({"cron": "0 21 * * *"})


# ---- stock_quote with a saved response ------------------------------------------------------


CHART = {"chart": {"result": [{
    "meta": {"symbol": "FTNT", "currency": "USD", "regularMarketPrice": 179.99, "chartPreviousClose": 169.13,
             "regularMarketTime": 1792785601, "exchangeTimezoneName": "America/New_York",
             "regularMarketDayLow": 171.19, "regularMarketDayHigh": 180.16, "fullExchangeName": "NasdaqGS"},
    "timestamp": [1792416600, 1792503000, 1792589400, 1792675800, 1792762200],
    "indicators": {"quote": [{"close": [171.5, 170.93, 174.05, 172.98, 179.99]}]},
}], "error": None}}


async def test_stock_quote_uses_the_previous_day_close():
    def handler(request):
        if "FTNT" in str(request.url):
            return httpx.Response(200, json=CHART)
        return httpx.Response(404, json={"chart": {"result": None, "error": {"description": "No data found"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await stock_quote(["ftnt", "NOPE", "bad sym"], client=client)
    assert "FTNT: 179.99 USD · +7.01 (+4.05 %) vs. previous close 172.98 · day range 171.19–180.16" in text
    assert "169.13" not in text  # not the close before the 5-day window
    assert "NOPE: unknown symbol" in text and "BAD SYM: not a valid symbol" in text
    assert text.startswith('<untrusted source="yahoo-finance">')
    json.dumps(text)


async def test_no_approval_card_when_the_preview_finds_a_problem(store):
    from agent_demo.agent import Agent
    from test_agent import FakeLLM

    asked = []

    async def approver(request):
        asked.append(request)
        return True

    async def sink(event):
        pass

    def unknown_chat():
        raise TaskError("I don't know your Feishu chat yet")

    registry = ToolRegistry()
    for tool in TaskService(store).tools(unknown_chat, "web", lambda: "weak"):
        registry.register(tool)
    llm = FakeLLM([LLMReply(content=None, tool_calls=[ToolCall("c1", "schedule__add",
                   '{"title": "x", "prompt": "y", "cron": "0 21 * * *"}')]), LLMReply(content="ok")])
    agent = Agent(llm, registry, sink, approver)
    await agent.run_turn("add a task")
    assert asked == [] and store.list() == []
    tool_result = next(m for m in agent.history if m["role"] == "tool")["content"]
    assert tool_result.startswith("Not done: I don't know your Feishu chat yet")


def test_add_description_says_reports_come_to_the_chat():
    from agent_demo.schedule_tools import ADD_DESCRIPTION

    assert "delivered to this chat automatically" in ADD_DESCRIPTION and "do NOT add sending an email" in ADD_DESCRIPTION
