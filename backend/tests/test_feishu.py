"""Feishu channel: parsing, cards and sending, without a network connection.

Sample events go through the SDK's own classes (unmarshal -> marshal), so the parser
is tested against exactly the dict shape it receives at runtime.
"""

import asyncio
import json
from types import SimpleNamespace

import lark_oapi as lark
import pytest
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from agent_demo.channels import build_channels
from agent_demo.channels.base import ButtonPress, GroupEvent
from agent_demo.channels.feishu import (
    FeishuChannel,
    approval_card,
    closed_card,
    parse_card_action,
    parse_group_event,
    parse_message,
    text_card,
)
from agent_demo.config import ConfigError
from agent_demo.events import ApprovalRequest

BOT = "ou_bot"


def sdk_round_trip(payload: dict, cls) -> dict:
    return json.loads(lark.JSON.marshal(lark.JSON.unmarshal(json.dumps(payload), cls)))


def message_event(text="hi", chat_type="p2p", message_type="text", mentions=None, sender_type="user"):
    content = json.dumps({"text": text}) if message_type == "text" else json.dumps({"image_key": "img_1"})
    payload = {
        "schema": "2.0",
        "header": {"event_id": "ev1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": sender_type},
            "message": {
                "message_id": "om_1", "chat_id": "oc_chat", "chat_type": chat_type,
                "message_type": message_type, "content": content, "mentions": mentions or [],
            },
        },
    }
    return sdk_round_trip(payload, P2ImMessageReceiveV1)


def mention(key, open_id, name):
    return {"key": key, "id": {"open_id": open_id}, "name": name}


# ---- parsing ----------------------------------------------------------------


def test_parse_dm_text():
    msg = parse_message(message_event("  Which tables?  "), BOT)
    assert (msg.channel, msg.chat_id, msg.user_id, msg.message_id) == ("feishu", "oc_chat", "ou_user", "om_1")
    assert msg.text == "Which tables?" and not msg.is_group and msg.mentioned_bot and not msg.unsupported


def test_parse_group_mentions():
    event = message_event(
        "@_user_1 summarise mail for @_user_2", chat_type="group",
        mentions=[mention("@_user_1", BOT, "AgentDemoBot"), mention("@_user_2", "ou_amy", "Amy")],
    )
    msg = parse_message(event, BOT)
    assert msg.is_group and msg.mentioned_bot
    assert msg.text == "summarise mail for @Amy"  # the bot's own mention is removed

    other = parse_message(message_event("@_user_1 hi", "group", mentions=[mention("@_user_1", "ou_amy", "Amy")]), BOT)
    assert other.is_group and not other.mentioned_bot


def test_parse_ignored_and_unsupported():
    assert parse_message(message_event(sender_type="app"), BOT) is None  # bots, including ourselves
    assert parse_message({"event": {}}, BOT) is None
    image = parse_message(message_event(message_type="image"), BOT)
    assert image.unsupported and image.text == ""


def test_parse_card_action():
    payload = {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": "ou_user"},
            "action": {"tag": "button", "value": {"call_id": "call_9", "approved": True}},
            "context": {"open_message_id": "om_card", "open_chat_id": "oc_chat"},
        },
    }
    press = parse_card_action(sdk_round_trip(payload, P2CardActionTrigger))
    assert press == ButtonPress("feishu", "oc_chat", "ou_user", "call_9", True)
    payload["event"]["action"]["value"] = {"something": "else"}
    assert parse_card_action(sdk_round_trip(payload, P2CardActionTrigger)) is None


def test_parse_group_events():
    from lark_oapi.api.im.v1 import P2ImChatMemberBotAddedV1, P2ImChatMemberBotDeletedV1

    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.chat.member.bot.added_v1"},
        "event": {"chat_id": "oc_team", "operator_id": {"open_id": "ou_user"}, "external": False, "name": "Team"},
    }
    added = parse_group_event(sdk_round_trip(payload, P2ImChatMemberBotAddedV1), "bot_added")
    assert added == GroupEvent("feishu", "oc_team", "ou_user", "bot_added", "Team", False)
    payload["event"]["external"] = True
    removed = parse_group_event(sdk_round_trip(payload, P2ImChatMemberBotDeletedV1), "bot_removed")
    assert removed.kind == "bot_removed" and removed.external is True
    assert parse_group_event({"event": {}}, "bot_added") is None


# ---- cards ------------------------------------------------------------------


REQUEST = ApprovalRequest("call_9", "sina__send_email", {"to": ["me@sina.com"], "body": "周报"})


def test_approval_card_shows_full_args_and_both_buttons():
    card = approval_card(REQUEST)
    elements = card["body"]["elements"]
    assert card["schema"] == "2.0" and card["config"]["update_multi"] is True
    assert "**sina__send_email**" in elements[0]["content"]
    assert elements[1]["content"] == (
        '```json\n{\n  "to": [\n    "me@sina.com"\n  ],\n  "body": "周报"\n}\n```'
    )
    values = [col["elements"][0]["behaviors"][0]["value"] for col in card["body"]["elements"][2]["columns"]]
    assert values == [{"call_id": "call_9", "approved": True}, {"call_id": "call_9", "approved": False}]


def test_closed_card_has_no_buttons():
    for outcome, title in [("approved", "✅"), ("denied", "❌"), ("expired", "⌛")]:
        card = closed_card(REQUEST, outcome)
        assert card["header"]["title"]["content"].startswith(title)
        assert "button" not in json.dumps(card)


def test_huge_arguments_are_cut_with_a_note():
    card = approval_card(ApprovalRequest("c", "send", {"body": "x" * 10_000}))
    args = card["body"]["elements"][1]["content"]
    assert len(args) < 4000 and "more characters not shown" in args


def test_text_card():
    assert text_card("**hi**")["body"]["elements"] == [{"tag": "markdown", "content": "**hi**"}]


# ---- configuration ----------------------------------------------------------


def test_from_env(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "s")
    monkeypatch.setenv("FEISHU_ALLOWED_USERS", " ou_a, ou_b ,")
    monkeypatch.setenv("FEISHU_ALLOWED_CHATS", "")
    channel = FeishuChannel.from_env()
    assert channel.allowed_users == {"ou_a", "ou_b"} and channel.allowed_chats == set()
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    with pytest.raises(ConfigError, match="FEISHU_APP_SECRET"):
        FeishuChannel.from_env()


def test_build_channels_unknown():
    with pytest.raises(ConfigError, match="Unknown IM channel 'carrier-pigeon'"):
        build_channels(["carrier-pigeon"])


# ---- sending, with a fake SDK client ----------------------------------------


class FakeMessageAPI:
    def __init__(self):
        self.calls = []

    def _ok(self, name, request):
        self.calls.append((name, request))
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id=f"om_{len(self.calls)}"))

    async def acreate(self, request):
        return self._ok("create", request)

    async def areply(self, request):
        return self._ok("reply", request)

    async def apatch(self, request):
        return self._ok("patch", request)


@pytest.fixture
def channel():
    api = FakeMessageAPI()

    async def arequest(request):
        return SimpleNamespace(raw=SimpleNamespace(content=b'{"code": 0, "bot": {"open_id": "ou_bot"}}'))

    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=api)), arequest=arequest)
    ch = FeishuChannel("cli_x", "s", {"ou_user"}, client=client)
    return ch, api


async def test_send_text_dm_and_group_reply(channel):
    ch, api = channel
    await ch.send_text("oc_chat", "hello")
    await ch.send_text("oc_group", "threaded", reply_to="om_question")
    (kind1, create), (kind2, reply) = api.calls
    assert kind1 == "create" and create.receive_id_type == "chat_id"
    assert create.request_body.receive_id == "oc_chat" and create.request_body.msg_type == "interactive"
    assert json.loads(create.request_body.content) == text_card("hello")
    assert kind2 == "reply" and reply.message_id == "om_question"


async def test_approval_send_then_close(channel):
    ch, api = channel
    handle = await ch.send_approval("oc_chat", REQUEST, None)
    assert handle == "om_1"
    await ch.close_approval("oc_chat", handle, "denied")
    kind, patch = api.calls[-1]
    assert kind == "patch" and patch.message_id == "om_1"
    assert json.loads(patch.request_body.content) == closed_card(REQUEST, "denied")
    await ch.close_approval("oc_chat", handle, "denied")  # already closed: nothing sent
    assert len(api.calls) == 2


async def test_fetch_bot_open_id(channel):
    ch, _ = channel
    assert await ch._fetch_bot_open_id() == "ou_bot"


async def test_card_click_is_answered_with_a_toast(channel):
    """The SDK calls the handler on its own thread; the hub runs on the main loop."""
    ch, _ = channel
    presses = []

    async def on_button(press):
        presses.append(press)
        return "Approved."

    ch._hub, ch._loop = SimpleNamespace(on_button=on_button), asyncio.get_running_loop()
    payload = {"event": {"operator": {"open_id": "ou_user"}, "context": {"open_chat_id": "oc_chat"},
                         "action": {"value": {"call_id": "call_9", "approved": True}}}}
    data = lark.JSON.unmarshal(json.dumps(payload), P2CardActionTrigger)
    response = await asyncio.to_thread(ch._on_card_action, data)
    assert json.loads(lark.JSON.marshal(response)) == {"toast": {"type": "info", "content": "Approved."}}
    assert presses == [ButtonPress("feishu", "oc_chat", "ou_user", "call_9", True)]


async def test_message_event_is_handed_to_the_main_loop(channel):
    ch, _ = channel
    received = asyncio.Event()
    messages = []

    async def on_message(msg):
        messages.append(msg)
        received.set()

    ch._hub, ch._loop, ch.bot_open_id = SimpleNamespace(on_message=on_message), asyncio.get_running_loop(), BOT
    data = lark.JSON.unmarshal(json.dumps(message_event("hi")), P2ImMessageReceiveV1)
    await asyncio.to_thread(ch._on_message_event, data)
    await asyncio.wait_for(received.wait(), 2)
    assert messages[0].text == "hi"
