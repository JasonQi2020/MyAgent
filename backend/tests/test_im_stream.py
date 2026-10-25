"""Streamed turns in the ChatHub (design/wecom_integration.md §4), with an in-memory channel
that offers open_stream. Channels without it are covered by test_im_hub.py."""

from agent_demo.channels.hub import WAITING_LINE
from agent_demo.llm import LLMReply
from test_im_hub import OWNER, FakeChannel, call, dm, make_hub, wait_for


class FakeStream:
    def __init__(self, max_bytes=20000, fail=False):
        self.max_bytes, self.fail = max_bytes, fail
        self.updates, self.finished = [], None

    async def update(self, text):
        if self.fail:
            raise RuntimeError("stream broken")
        self.updates.append(text)

    async def finish(self, text):
        self.finished = text


class StreamingChannel(FakeChannel):
    def __init__(self, stream=None, **kw):
        super().__init__(**kw)
        self.stream, self.opened = stream or FakeStream(), []

    async def open_stream(self, chat_id, message_id):
        self.opened.append((chat_id, message_id))
        return self.stream


async def test_turn_is_one_growing_message():
    ch = StreamingChannel()
    hub, _ = make_hub([call("db_list_tables"), LLMReply(content="You have **4** tables.")], channel=ch)
    await hub.on_message(dm("Which tables?", message_id="m7"))
    await hub.drain()
    assert ch.opened == [(f"dm-{OWNER}", "m7")]
    assert ch.stream.updates[0] == "🔧 db_list_tables"
    assert ch.stream.finished == "🔧 db_list_tables\n\nYou have **4** tables."
    assert ch.sent == []  # nothing sent as separate messages


async def test_tool_errors_and_tables_in_the_stream():
    ch = StreamingChannel()
    hub, _ = make_hub([call("db_query", sql="x"), LLMReply(content="| a | b |\n|---|---|\n| 1 | 2 |")], channel=ch)
    await hub.on_message(dm("query"))
    await hub.drain()
    assert "✗" in ch.stream.finished and "database is locked" in ch.stream.finished
    assert "```" in ch.stream.finished and "|---|" not in ch.stream.finished


async def test_approval_line_is_replaced_by_the_outcome():
    ch = StreamingChannel()
    sent_emails = []
    hub, _ = make_hub([call("mail__send_email", to="me@x.com"), LLMReply(content="Sent.")],
                      channel=ch, sent_emails=sent_emails)
    await hub.on_message(dm("email me"))
    await wait_for(lambda: ch.approvals)
    assert ch.stream.updates[-1].endswith(WAITING_LINE)
    await hub.on_message(dm("yes", message_id="m2"))
    await hub.drain()
    assert WAITING_LINE not in ch.stream.finished and "✅ Approved" in ch.stream.finished
    assert ch.stream.finished.endswith("Sent.") and sent_emails == [{"to": "me@x.com"}]


async def test_expired_approval_in_the_stream():
    ch = StreamingChannel()
    hub, _ = make_hub([call("mail__send_email", to="me"), LLMReply(content="Not sent.")],
                      channel=ch, approval_timeout=0.05)
    await hub.on_message(dm("email me"))
    await hub.drain()
    assert "⌛ Expired" in ch.stream.finished and ch.closed[0][1] == "expired"


async def test_overflow_finishes_the_stream_and_continues_with_messages():
    ch = StreamingChannel(stream=FakeStream(max_bytes=40))
    hub, _ = make_hub([call("db_list_tables"), LLMReply(content="x" * 100)], channel=ch)
    await hub.on_message(dm("long"))
    await hub.drain()
    assert ch.stream.finished == "🔧 db_list_tables"
    assert ch.texts() == ["x" * 100]


async def test_broken_stream_falls_back_to_messages():
    ch = StreamingChannel(stream=FakeStream(fail=True))
    hub, _ = make_hub([call("db_list_tables"), LLMReply(content="Four.")], channel=ch)
    await hub.on_message(dm("tables"))
    await hub.drain()
    assert ch.texts() == ["🔧 db_list_tables", "Four."]


async def test_no_stream_for_this_message_uses_messages():
    class NoStream(FakeChannel):
        async def open_stream(self, chat_id, message_id):
            return None

    ch = NoStream()
    hub, _ = make_hub([call("db_list_tables"), LLMReply(content="Four.")], channel=ch)
    await hub.on_message(dm("tables"))
    await hub.drain()
    assert ch.texts() == ["🔧 db_list_tables", "Four."]


async def test_commands_are_not_streamed():
    ch = StreamingChannel()
    hub, _ = make_hub([], channel=ch)
    await hub.on_message(dm("/help"))
    assert ch.opened == [] and ch.texts() and ch.texts()[0].startswith("Commands:")


async def test_failed_turn_is_finished_with_the_error():
    ch = StreamingChannel()
    hub, _ = make_hub([], channel=ch)  # FakeLLM has no replies: the LLM call fails
    await hub.on_message(dm("hi"))
    await hub.drain()
    assert ch.stream.finished.startswith("⚠ ")
