"""WeCom channel: parsing, cards, the stream object, and the real WebSocket client against a
fake WeCom server on localhost (subscribe, ping, callbacks, replies). No network."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

import agent_demo.channels.wecom as wecom
from agent_demo.channels import build_channels
from agent_demo.channels.wecom import (
    WeComChannel,
    WeComStream,
    approval_card,
    approval_markdown,
    closed_card,
    parse_click,
    parse_message,
    split_bytes,
)
from agent_demo.config import ConfigError
from agent_demo.events import ApprovalRequest
from agent_demo.llm import LLMReply
from test_im_hub import call, make_hub, wait_for

OWNER, OTHER, STRANGER, GROUP_ID = "zhangsan", "lisi", "stranger", "wrkSFfCgAAxxxx"


def text_body(content, msgid="msg1", user=OWNER, group=None, kind="text"):
    body = {"msgid": msgid, "aibotid": "bot", "chattype": "group" if group else "single",
            "from": {"userid": user}, "msgtype": kind}
    if kind in ("text", "voice"):
        body[kind] = {"content": content}
    if group:
        body["chatid"] = group
    return body


def click_body(task_id, key, user=OWNER, group=None, nested=True):
    detail = {"card_type": "button_interaction", "event_key": key, "task_id": task_id}
    event = {"eventtype": "template_card_event"}
    event.update({"template_card_event": detail} if nested else detail)
    body = {"msgid": "ev1", "aibotid": "bot", "chattype": "group" if group else "single",
            "from": {"userid": user}, "msgtype": "event", "event": event}
    if group:
        body["chatid"] = group
    return body


# ---- parsing ---------------------------------------------------------------------------


def test_parse_dm_text():
    msg = parse_message(text_body(" hello "))
    assert (msg.chat_id, msg.user_id, msg.message_id, msg.text) == (OWNER, OWNER, "msg1", "hello")
    assert not msg.is_group and not msg.unsupported


def test_parse_group_strips_the_mention():
    msg = parse_message(text_body("@AgentBot what is RY?", group=GROUP_ID))
    assert msg.chat_id == GROUP_ID and msg.user_id == OWNER and msg.is_group and msg.mentioned_bot
    assert msg.text == "what is RY?"


def test_parse_voice_mixed_and_other_types():
    assert parse_message(text_body("今天天气", kind="voice")).text == "今天天气"
    mixed = text_body("", kind="mixed")
    mixed["mixed"] = {"msg_item": [{"msgtype": "text", "text": {"content": "look"}},
                                   {"msgtype": "image", "image": {"url": "u"}}]}
    assert parse_message(mixed).text == "look"
    mixed["mixed"]["msg_item"] = [{"msgtype": "image", "image": {"url": "u"}}]
    assert parse_message(mixed).unsupported
    assert parse_message(text_body("", kind="image")).unsupported
    assert parse_message({"msgtype": "text", "text": {"content": "x"}}) is None  # no ids


def test_parse_click():
    cards = {"ap_1": "call_9"}
    task, press = parse_click(click_body("ap_1", "y"), cards)
    assert task == "ap_1" and (press.chat_id, press.user_id, press.call_id, press.approved) == (OWNER, OWNER, "call_9", True)
    _, press = parse_click(click_body("ap_1", "n", group=GROUP_ID, nested=False), cards)
    assert press.chat_id == GROUP_ID and press.approved is False
    assert parse_click(click_body("ap_unknown", "y"), cards) is None
    assert parse_click(click_body("ap_1", "maybe"), cards) is None


def test_cards_and_split():
    request = ApprovalRequest("c1", "mail__send_email", {"to": "me@x.com", "body": "x" * 5000})
    markdown = approval_markdown(request)
    assert "mail__send_email" in markdown and "more characters not shown" in markdown
    card = approval_card("ap_1", request)
    assert card["task_id"] == "ap_1" and [b["key"] for b in card["button_list"]] == ["y", "n"]
    assert "button_list" not in closed_card("ap_1", request, "✅ Approved")
    parts = split_bytes("中文段落。" * 2000, 4000)
    assert len(parts) > 1 and all(len(p.encode()) <= 4000 for p in parts)
    assert split_bytes("short", 4000) == ["short"] and split_bytes("  ", 4000) == []


def test_from_env(monkeypatch):
    monkeypatch.setenv("WECOM_BOT_ID", "bot")
    monkeypatch.setenv("WECOM_BOT_SECRET", "s")
    monkeypatch.setenv("WECOM_ALLOWED_USERS", "zhangsan, lisi,")
    monkeypatch.setenv("WECOM_ALLOWED_CHATS", "")
    [channel] = build_channels(["wecom"])
    assert channel.allowed_users == {"zhangsan", "lisi"} and channel.ws_url == wecom.DEFAULT_WS_URL
    monkeypatch.setenv("WECOM_BOT_SECRET", "")
    with pytest.raises(ConfigError, match="WECOM_BOT_SECRET"):
        WeComChannel.from_env()


# ---- the stream object -------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.frames, self.texts = [], []

    async def request(self, cmd, body, req_id=None):
        self.frames.append((cmd, req_id, json.loads(json.dumps(body))))
        return {"errcode": 0}

    async def send_text(self, chat_id, text, reply_to=None):
        self.texts.append(text)


async def test_stream_updates_are_coalesced():
    rec = Recorder()
    stream = WeComStream(rec, OWNER, "r1", interval=0.2)
    for i in range(1, 6):
        await stream.update("line\n" * i)
    await stream.finish("line\n" * 5 + "answer")
    contents = [(body["stream"]["content"], body["stream"]["finish"]) for _, _, body in rec.frames]
    assert len(contents) <= 3 and contents[0] == ("line\n", False)
    assert contents[-1] == ("line\n" * 5 + "answer", True)
    assert {req for _, req, _ in rec.frames} == {"r1"}
    assert len({body["stream"]["id"] for _, _, body in rec.frames}) == 1


async def test_stream_rolls_over_before_wecom_ends_it(monkeypatch):
    monkeypatch.setattr(wecom, "STREAM_ROLLOVER_SECONDS", 0)
    rec = Recorder()
    stream = WeComStream(rec, OWNER, "r1", interval=0)
    await stream.update("first part")
    await stream.update("first part\nmore")  # ignored: already frozen
    await stream.finish("first part\nmore\nanswer")
    assert [(b["stream"]["content"], b["stream"]["finish"]) for _, _, b in rec.frames] == [("first part", True)]
    assert rec.texts == ["more\nanswer"]


async def test_empty_stream_sends_nothing():
    rec = Recorder()
    await WeComStream(rec, OWNER, "r1", interval=0).finish("")
    assert rec.frames == []


# ---- the client against a fake WeCom server -------------------------------------------------


class FakeWeCom:
    def __init__(self, secret="s"):
        self.secret, self.frames, self.conns = secret, [], []

    async def handler(self, ws):
        self.conns.append(ws)
        async for raw in ws:
            frame = json.loads(raw)
            self.frames.append(frame)
            req_id = frame["headers"]["req_id"]
            if frame["cmd"] == "aibot_subscribe":
                ok = frame["body"] == {"bot_id": "bot", "secret": self.secret}
                await ws.send(json.dumps({"headers": {"req_id": req_id}, "errcode": 0 if ok else 40058,
                                          "errmsg": "ok" if ok else "invalid secret"}))
            else:
                await ws.send(json.dumps({"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))

    async def push(self, cmd, body, req_id):
        await self.conns[-1].send(json.dumps({"cmd": cmd, "headers": {"req_id": req_id}, "body": body}))

    def sent(self, cmd):
        return [f for f in self.frames if f["cmd"] == cmd]

    def finished(self):
        return [f for f in self.sent("aibot_respond_msg") if f["body"]["stream"]["finish"]]


@pytest.fixture
async def fake():
    server_side = FakeWeCom()
    async with serve(server_side.handler, "127.0.0.1", 0) as server:
        server_side.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        yield server_side


async def started(fake, replies, secret="s", approval_timeout=5.0, sent_emails=None, **kw):
    channel = WeComChannel("bot", secret, {OWNER, OTHER}, {GROUP_ID}, ws_url=fake.url, stream_interval=0, **kw)
    hub, _ = make_hub(replies, channel=channel, approval_timeout=approval_timeout, sent_emails=sent_emails)
    await channel.start(hub)
    return channel, hub


async def test_subscribe_and_dm_streamed_reply(fake):
    channel, hub = await started(fake, [call("db_list_tables"), LLMReply(content="You have **4** tables.")])
    assert fake.sent("aibot_subscribe")[0]["body"]["bot_id"] == "bot"
    await fake.push("aibot_msg_callback", text_body("Which tables?"), "r1")
    await wait_for(lambda: fake.finished())
    frames = fake.sent("aibot_respond_msg")
    assert {f["headers"]["req_id"] for f in frames} == {"r1"}
    assert len({f["body"]["stream"]["id"] for f in frames}) == 1
    assert frames[0]["body"]["stream"]["content"] == "🔧 db_list_tables"
    assert fake.finished()[0]["body"]["stream"]["content"] == "🔧 db_list_tables\n\nYou have **4** tables."
    assert fake.sent("aibot_send_msg") == []
    await channel.stop()


async def test_approval_card_click_updates_the_card(fake):
    sent_emails = []
    channel, hub = await started(fake, [call("mail__send_email", to="me@x.com"), LLMReply(content="Sent.")],
                                 sent_emails=sent_emails)
    await fake.push("aibot_msg_callback", text_body("email me"), "r1")
    await wait_for(lambda: any(f["body"]["msgtype"] == "template_card" for f in fake.sent("aibot_send_msg")))
    pushes = fake.sent("aibot_send_msg")
    assert pushes[0]["body"]["msgtype"] == "markdown" and "me@x.com" in pushes[0]["body"]["markdown"]["content"]
    card = pushes[1]["body"]
    assert card["chatid"] == OWNER and card["chat_type"] == 1

    await fake.push("aibot_event_callback", click_body(card["template_card"]["task_id"], "y"), "r2")
    await wait_for(lambda: fake.finished())
    [update] = fake.sent("aibot_respond_update_msg")
    assert update["headers"]["req_id"] == "r2" and update["body"]["template_card"]["sub_title_text"] == "✅ Approved"
    assert "userids" not in update["body"]
    final = fake.finished()[0]["body"]["stream"]["content"]
    assert "✅ Approved" in final and final.endswith("Sent.")
    assert sent_emails == [{"to": "me@x.com"}]
    assert len(fake.sent("aibot_send_msg")) == 2  # no follow-up: the card already shows the outcome
    await channel.stop()


async def test_expired_approval_gets_a_follow_up(fake):
    channel, hub = await started(fake, [call("mail__send_email", to="me"), LLMReply(content="Not sent.")],
                                 approval_timeout=0.1)
    await fake.push("aibot_msg_callback", text_body("email me"), "r1")
    await wait_for(lambda: fake.finished())
    follow_up = fake.sent("aibot_send_msg")[-1]["body"]
    assert follow_up["msgtype"] == "markdown" and follow_up["markdown"]["content"].startswith("⌛ Expired")
    await channel.stop()


async def test_group_reply_and_wrong_clicker(fake):
    channel, hub = await started(fake, [call("mail__send_email", to="me"), LLMReply(content="ok")])
    await fake.push("aibot_msg_callback", text_body("@AgentBot /help", msgid="g0", group=GROUP_ID), "r0")
    await wait_for(lambda: fake.sent("aibot_send_msg"))
    help_msg = fake.sent("aibot_send_msg")[0]["body"]
    assert help_msg["chatid"] == GROUP_ID and help_msg["chat_type"] == 2

    await fake.push("aibot_msg_callback", text_body("@AgentBot email me", msgid="g1", group=GROUP_ID), "r1")
    await wait_for(lambda: any(f["body"]["msgtype"] == "template_card" for f in fake.sent("aibot_send_msg")))
    task_id = fake.sent("aibot_send_msg")[-1]["body"]["template_card"]["task_id"]
    await fake.push("aibot_event_callback", click_body(task_id, "y", user=OTHER, group=GROUP_ID), "r2")
    await wait_for(lambda: fake.sent("aibot_respond_update_msg"))
    update = fake.sent("aibot_respond_update_msg")[0]["body"]
    assert update["userids"] == [OTHER] and "Only the person who asked" in update["template_card"]["sub_title_text"]
    assert task_id in channel._cards  # still open for the owner
    await hub.stop()


async def test_strangers_are_ignored(fake):
    channel, hub = await started(fake, [])
    await fake.push("aibot_msg_callback", text_body("hi", user=STRANGER), "r1")
    await asyncio.sleep(0.1)
    assert fake.sent("aibot_send_msg") == [] and fake.sent("aibot_respond_msg") == []
    await channel.stop()


async def test_wrong_secret_fails_start(fake):
    with pytest.raises(RuntimeError, match="subscribe refused: 40058"):
        await started(fake, [], secret="wrong")


async def test_unreachable_server_fails_start():
    channel = WeComChannel("bot", "s", {OWNER}, ws_url="ws://127.0.0.1:1")
    with pytest.raises(RuntimeError, match="cannot connect"):
        await channel.start(hub=None)


async def test_heartbeat_and_reconnect(fake):
    channel, hub = await started(fake, [], heartbeat=0.05)
    await wait_for(lambda: fake.sent("ping"))
    await fake.conns[-1].close()  # WeCom drops the connection
    await wait_for(lambda: len(fake.sent("aibot_subscribe")) == 2, timeout=5)
    await channel.stop()


async def test_taken_over_does_not_reconnect(fake):
    channel, hub = await started(fake, [])
    await fake.push("aibot_event_callback", {"event": {"eventtype": "disconnected_event"}}, "r9")
    await wait_for(lambda: channel._kicked)
    await fake.conns[-1].close()
    await wait_for(lambda: channel._runner.done())
    assert len(fake.sent("aibot_subscribe")) == 1


def test_has_every_channel_method():
    # server.py calls routes() on every channel; a missing method only showed at start-up.
    from agent_demo.channels.base import Channel

    channel = WeComChannel("bot", "s", {OWNER})
    members = [m for m in vars(Channel) if not m.startswith("_")] + ["allowed_users", "allowed_chats"]
    assert all(hasattr(channel, m) for m in members)
    assert channel.routes() is None
