"""Agent-loop tests with a scripted fake LLM: no API key or network needed."""

import json

from agent_demo.agent import Agent
from agent_demo.events import ApprovalRequest, ErrorEvent, ToolCallEvent, ToolResultEvent, TurnDone
from agent_demo.llm import LLMReply, ToolCall
from agent_demo.tools import Tool, ToolRegistry


class FakeLLM:
    """Returns pre-written replies in order and records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, tools):
        self.calls.append((messages, tools))
        return self.replies.pop(0)


def call(name, **args):
    return ToolCall(id=f"call_{name}", name=name, arguments=json.dumps(args))


def make_registry(sent):
    async def echo(args):
        return f"echo:{args['text']}"

    async def send(args):
        sent.append(args)
        return "sent"

    reg = ToolRegistry()
    reg.register(Tool("echo", "Echo text", {"type": "object"}, echo))
    reg.register(Tool("send", "Send", {"type": "object"}, send, needs_approval=True))
    reg.register(Tool("nuke", "Never", {"type": "object"}, send, blocked=True))
    return reg


async def run(replies, approve=True, max_steps=5):
    events, approvals, sent = [], [], []

    async def sink(e):
        events.append(e)

    async def approver(req: ApprovalRequest):
        approvals.append(req)
        return approve

    llm = FakeLLM(replies)
    agent = Agent(llm, make_registry(sent), sink, approver, max_steps=max_steps)
    await agent.run_turn("hi")
    return agent, llm, events, approvals, sent


async def test_plain_answer():
    agent, llm, events, *_ = await run([LLMReply(content="Hello!")])
    assert events[-1] == TurnDone(1, 0, 0)
    assert agent.history[-1] == {"role": "assistant", "content": "Hello!"}


async def test_tool_round_trip():
    agent, llm, events, *_ = await run(
        [LLMReply(content=None, tool_calls=[call("echo", text="x")]), LLMReply(content="done")]
    )
    tool_msg = agent.history[2]
    assert tool_msg == {"role": "tool", "tool_call_id": "call_echo", "content": "echo:x"}
    # The second LLM call must have seen the tool result.
    assert llm.calls[1][0][-1]["content"] == "echo:x"
    # Blocked tools are not offered to the model.
    assert {t["function"]["name"] for t in llm.calls[0][1]} == {"echo", "send"}


async def test_approval_granted_and_denied():
    replies = lambda: [LLMReply(content=None, tool_calls=[call("send", to="me")]), LLMReply(content="ok")]
    *_, approvals, sent = await run(replies(), approve=True)
    assert len(approvals) == 1 and sent == [{"to": "me"}]

    agent, _, events, approvals, sent = await run(replies(), approve=False)
    assert sent == []
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.is_error and "DENIED" in result.result


async def test_unknown_blocked_and_bad_json():
    bad = ToolCall(id="c1", name="echo", arguments="{not json")
    *_, events, _, _ = await run(
        [
            LLMReply(content=None, tool_calls=[call("missing"), call("nuke"), bad]),
            LLMReply(content="ok"),
        ]
    )
    errors = [e.result for e in events if isinstance(e, ToolResultEvent)]
    assert "Unknown tool" in errors[0]
    assert "blocked" in errors[1]
    assert "Invalid tool arguments" in errors[2]
    # Every result has a matching tool_call event (the web UI attaches results to cards by id).
    call_ids = [e.id for e in events if isinstance(e, ToolCallEvent)]
    assert call_ids == [e.id for e in events if isinstance(e, ToolResultEvent)]


async def test_step_limit():
    loop = [LLMReply(content=None, tool_calls=[call("echo", text="again")]) for _ in range(3)]
    *_, events, _, _ = await run(loop, max_steps=3)
    assert any(isinstance(e, ErrorEvent) and "limit" in e.message for e in events)


async def test_reasoning_not_sent_back():
    agent, *_ = await run([LLMReply(content="hi", reasoning="secret thoughts")])
    assert "secret" not in json.dumps(agent.history)


async def test_context_is_shown_for_this_turn_only():
    events = []

    async def sink(e):
        events.append(e)

    async def approver(req):
        return True

    llm = FakeLLM([LLMReply(content=None, tool_calls=[call("echo", text="x")]), LLMReply(content="done"),
                   LLMReply(content="second")])
    agent = Agent(llm, make_registry([]), sink, approver)
    await agent.run_turn("question", context="CONTEXT")
    # Both steps of the turn see the context with the question ...
    for messages, _ in llm.calls:
        assert messages[1]["content"] == "CONTEXT\n\nThe question:\nquestion"
    # ... but the history keeps the plain question, and the next turn does not see the old context.
    assert agent.history[0] == {"role": "user", "content": "question"}
    await agent.run_turn("next")
    assert "CONTEXT" not in json.dumps(llm.calls[-1][0])
