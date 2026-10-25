"""ChatHub with an in-memory channel and the scripted FakeLLM: no Feishu, no network."""

import asyncio
import json
from pathlib import Path

import pytest

from agent_demo.channels.base import ButtonPress, ChannelLimits, IncomingMessage
from agent_demo.channels.hub import ChatHub
from agent_demo.config import Settings
from agent_demo.llm import LLMReply, ToolCall
from agent_demo.runtime import Runtime
from agent_demo.tools import Tool, ToolRegistry
from test_agent import FakeLLM

OWNER, STRANGER, GROUP = "ou_owner", "ou_stranger", "oc_group"


class FakeChannel:
    name = "fake"
    limits = ChannelLimits(max_text_chars=200, supports_buttons=True)

    def __init__(self, allowed_users=(OWNER,), allowed_chats=(GROUP,)):
        self.allowed_users, self.allowed_chats = set(allowed_users), set(allowed_chats)
        self.sent, self.approvals, self.closed = [], [], []
        self.started = self.stopped = False

    async def start(self, hub):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send_text(self, chat_id, text, reply_to=None):
        self.sent.append((chat_id, text, reply_to))

    async def send_approval(self, chat_id, request, reply_to):
        self.approvals.append((chat_id, request))
        return f"card-{request.id}"

    async def close_approval(self, chat_id, handle, outcome):
        self.closed.append((handle, outcome))

    def routes(self):
        return None

    def texts(self):
        return [t for _, t, _ in self.sent]


def call(name, call_id="c1", **args):
    return LLMReply(content=None, tool_calls=[ToolCall(call_id, name, json.dumps(args))])


def make_hub(replies, channel=None, approval_timeout=5.0, sent_emails=None):
    sent_emails = [] if sent_emails is None else sent_emails

    async def lookup(args):
        return "4 tables"

    async def send(args):
        sent_emails.append(args)
        return "sent"

    async def broken(args):
        raise RuntimeError("database is locked")

    registry = ToolRegistry()
    registry.register(Tool("db_list_tables", "List", {"type": "object"}, lookup))
    registry.register(Tool("db_query", "Query", {"type": "object"}, broken))
    registry.register(Tool("mail__send_email", "Send", {"type": "object"}, send, needs_approval=True))
    settings = Settings("fake", "http://x", "k", "m", Path("x.db"), 5, Path("none.json"))
    runtime = Runtime(settings, FakeLLM(replies), registry, [])
    channel = channel or FakeChannel()
    return ChatHub(runtime, [channel], approval_timeout=approval_timeout), channel


def dm(text, message_id="m1", user=OWNER):
    return IncomingMessage("fake", f"dm-{user}", user, message_id, text)


def group(text, message_id="g1", user=OWNER, mentioned=True, chat=GROUP):
    return IncomingMessage("fake", chat, user, message_id, text, is_group=True, mentioned_bot=mentioned)


async def wait_for(condition, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


# ---- conversation -----------------------------------------------------------


async def test_dm_round_trip_with_progress_line():
    hub, ch = make_hub([call("db_list_tables"), LLMReply(content="You have **4** tables.")])
    await hub.on_message(dm("Which tables?"))
    await hub.drain()
    assert ch.sent == [
        ("dm-ou_owner", "🔧 db_list_tables", None),  # progress, flushed before the answer
        ("dm-ou_owner", "You have **4** tables.", None),
    ]


async def test_tool_errors_are_shown_compactly():
    hub, ch = make_hub([call("db_query", sql="SELECT 1"), LLMReply(content="Sorry.")])
    await hub.on_message(dm("query"))
    await hub.drain()
    assert ch.texts()[0] == '🔧 db_query · sql="SELECT 1"\n  ✗ Tool \'db_query\' failed: database is locked'


async def test_one_session_per_dm_and_history_kept():
    hub, ch = make_hub([LLMReply(content="a"), LLMReply(content="b"), LLMReply(content="c")])
    await hub.on_message(dm("one", "m1"))
    await hub.drain()
    await hub.on_message(dm("two", "m2"))
    await hub.drain()
    await hub.on_message(dm("hi", "m3", user="ou_other"))  # not allowed: no session
    assert list(hub.sessions) == [("fake", "dm-ou_owner")]
    history = hub.sessions[("fake", "dm-ou_owner")].agent.history
    assert [m["content"] for m in history if m["role"] == "user"] == ["one", "two"]


async def test_long_answers_are_split_and_tables_converted():
    table = "| a | b |\n|---|---|\n| 1 | 2 |"
    long = "\n\n".join(["x" * 150] * 3)
    hub, ch = make_hub([LLMReply(content=table), LLMReply(content=long)])
    await hub.on_message(dm("t", "m1"))
    await hub.drain()
    assert ch.texts()[0] == "```\na  b\n-  -\n1  2\n```"
    await hub.on_message(dm("l", "m2"))
    await hub.drain()
    assert len(ch.texts()) == 4 and all(len(t) <= 200 for t in ch.texts())


async def test_busy_reply_and_reasoning_not_sent():
    hub, ch = make_hub([LLMReply(content="done", reasoning="secret")])
    gate = asyncio.Event()
    original = hub.runtime.llm.chat

    async def slow_chat(messages, tools):
        await gate.wait()
        return await original(messages, tools)

    hub.runtime.llm.chat = slow_chat
    await hub.on_message(dm("first", "m1"))
    await hub.on_message(dm("second", "m2"))
    gate.set()
    await hub.drain()
    assert ch.texts()[0].startswith("Still working")
    assert ch.texts()[1] == "done" and "secret" not in " ".join(ch.texts())


# ---- security rules ---------------------------------------------------------


async def test_dedupe():
    hub, ch = make_hub([LLMReply(content="once")])
    await hub.on_message(dm("hi", "same"))
    await hub.drain()
    await hub.on_message(dm("hi", "same"))
    assert ch.texts() == ["once"]


async def test_allowlist_and_setup_mode(caplog):
    hub, ch = make_hub([], channel=FakeChannel(allowed_users=()))
    await hub.start()
    assert "setup mode" in hub.warnings[0]
    with caplog.at_level("WARNING", logger="agent_demo.im"):
        await hub.on_message(dm("hi", user="ou_new_user"))
    assert ch.sent == [] and hub.sessions == {}
    assert "user_id=ou_new_user" in caplog.text  # so the owner can copy their id
    await hub.stop()


async def test_group_rules(caplog):
    hub, ch = make_hub([LLMReply(content="group answer")])
    await hub.on_message(group("chatter", "g1", mentioned=False))
    with caplog.at_level("WARNING", logger="agent_demo.im"):
        await hub.on_message(group("hi", "g2", chat="oc_other"))
    assert ch.sent == [] and "chat_id=oc_other" in caplog.text
    await hub.on_message(group("hello bot", "g3"))
    await hub.drain()
    assert ch.sent == [(GROUP, "group answer", "g3")]  # threaded under the question
    assert list(hub.sessions) == [("fake", GROUP, OWNER)]  # per-user session in groups


async def test_unsupported_and_empty_messages():
    hub, ch = make_hub([])
    await hub.on_message(IncomingMessage("fake", "dm-ou_owner", OWNER, "i1", "", unsupported=True))
    await hub.on_message(dm("   ", "e1"))
    assert ch.texts()[0] == "I can only read text messages for now."
    assert ch.texts()[1].startswith("Commands:")  # the help text (split at 200 characters here)


# ---- commands ---------------------------------------------------------------


async def test_commands():
    hub, ch = make_hub([LLMReply(content="a")])
    await hub.on_message(dm("remember this", "m1"))
    await hub.drain()
    await hub.on_message(dm("/tools", "m2"))
    assert "- mail__send_email · ask" in ch.texts()[-1] and "- db_list_tables · auto" in ch.texts()[-1]
    await hub.on_message(dm("/new", "m3"))
    assert ch.texts()[-1] == "Conversation cleared."
    assert hub.sessions[("fake", "dm-ou_owner")].agent.history == []
    before = len(ch.texts())
    await hub.on_message(dm("/help", "m4"))
    help_text = " ".join(ch.texts()[before:])  # split at the fake channel's 200 characters
    assert help_text.startswith("Commands:") and "/strongmodel" in help_text


# ---- approvals --------------------------------------------------------------


async def run_until_approval(hub, ch, message):
    await hub.on_message(message)
    await wait_for(lambda: ch.approvals)
    return ch.approvals[-1][1]


@pytest.mark.parametrize("approved, outcome", [(True, "approved"), (False, "denied")])
async def test_button_approval(approved, outcome):
    emails = []
    hub, ch = make_hub([call("mail__send_email", to="me"), LLMReply(content="ok")], sent_emails=emails)
    request = await run_until_approval(hub, ch, dm("email me"))
    assert request.name == "mail__send_email" and request.arguments == {"to": "me"}
    notice = await hub.on_button(ButtonPress("fake", "dm-ou_owner", OWNER, request.id, approved))
    await hub.drain()
    assert notice == ("Approved." if approved else "Denied.")
    assert emails == ([{"to": "me"}] if approved else [])
    assert ch.closed == [(f"card-{request.id}", outcome)]
    assert await hub.on_button(ButtonPress("fake", "dm-ou_owner", OWNER, request.id, True)) == (
        "This request has already been answered or has expired."
    )


async def test_only_the_requester_can_approve():
    hub, ch = make_hub([call("mail__send_email", to="me"), LLMReply(content="ok")],
                       channel=FakeChannel(allowed_users=(OWNER, "ou_colleague")))
    request = await run_until_approval(hub, ch, group("email me"))
    notice = await hub.on_button(ButtonPress("fake", GROUP, "ou_colleague", request.id, True))
    assert notice == "Only the person who asked can answer this request."
    stranger = await hub.on_button(ButtonPress("fake", GROUP, STRANGER, request.id, True))
    assert stranger == "You are not allowed to use this bot."
    assert await hub.on_button(ButtonPress("fake", GROUP, OWNER, request.id, False)) == "Denied."
    await hub.drain()


async def test_yes_no_text_fallback():
    emails = []
    hub, ch = make_hub([call("mail__send_email", to="me"), LLMReply(content="ok")], sent_emails=emails)
    await run_until_approval(hub, ch, dm("email me", "m1"))
    await hub.on_message(dm("Yes!", "m2"))
    await hub.drain()
    assert emails == [{"to": "me"}] and ch.closed[0][1] == "approved"


async def test_approval_timeout_means_no():
    emails = []
    hub, ch = make_hub([call("mail__send_email", to="me"), LLMReply(content="ok")],
                       approval_timeout=0.05, sent_emails=emails)
    await hub.on_message(dm("email me"))
    await hub.drain()
    assert emails == [] and ch.closed[0][1] == "expired"


async def test_stop_denies_pending_and_stops_channels():
    emails = []
    hub, ch = make_hub([call("mail__send_email", to="me"), LLMReply(content="ok")], sent_emails=emails)
    await hub.start()
    await run_until_approval(hub, ch, dm("email me"))
    await hub.stop()
    assert emails == [] and ch.stopped


async def test_idle_sessions_are_dropped():
    hub, ch = make_hub([LLMReply(content="a")])
    await hub.on_message(dm("hi"))
    await hub.drain()
    hub.idle_seconds = 0
    hub.drop_idle()
    assert hub.sessions == {}
