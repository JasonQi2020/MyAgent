"""Telegram channel (design/telegram_integration.md) against a fake Bot API: no network."""

import asyncio
import json
import logging
import time

import httpx
import pytest

import agent_demo.claude_code as cc
from agent_demo.channels import build_channels
from agent_demo.channels.base import ButtonPress, GroupEvent, IncomingMessage
from agent_demo.channels.render import to_telegram_html
from agent_demo.channels.telegram import TelegramChannel, approval_keyboard, parse_update
from agent_demo.config import ConfigError
from agent_demo.events import ApprovalRequest
from agent_demo.llm import LLMReply, ToolCall
from test_im_hub import make_hub

BOT_ID, BOT = 777, "MyAgent2026DecBot"
TOKEN = "123456:SECRET-token-value"
ME, AMY, GROUP = 1001, 1002, -100500


def msg(text, chat=ME, chat_type="private", sender=ME, mid=1, **extra):
    # "date" = now: the hub only attaches group messages from the last 30 minutes as context.
    return {"update_id": mid, "message": {"message_id": mid, "date": int(time.time()), "text": text,
                                          "chat": {"id": chat, "type": chat_type},
                                          "from": {"id": sender, "is_bot": False, "first_name": "Amy" if sender == AMY else "Jason"},
                                          **extra}}


# ---- parsing --------------------------------------------------------------------------


def test_parse_dm_and_commands():
    item = parse_update(msg("hello"), BOT_ID, BOT)
    assert item == IncomingMessage("telegram", str(ME), str(ME), "1", "hello", is_group=False, mentioned_bot=True)
    assert parse_update(msg(f"/tasks@{BOT} run 3"), BOT_ID, BOT).text == "/tasks run 3"


def test_parse_group_mentions():
    def group(text, **extra):
        return parse_update(msg(text, chat=GROUP, chat_type="supergroup", sender=AMY, **extra), BOT_ID, BOT)

    mention = group(f"@{BOT.lower()} what do you think?")
    assert mention.is_group and mention.mentioned_bot and mention.text == "what do you think?"
    assert not group("just chatting").mentioned_bot
    assert group(f"/enable@{BOT}").text == "/enable" and group(f"/enable@{BOT}").mentioned_bot
    assert group("yes", reply_to_message={"from": {"id": BOT_ID}}).mentioned_bot  # reply to the bot
    assert group("hey", entities=[{"type": "text_mention", "user": {"id": BOT_ID}}]).mentioned_bot


def test_parse_ignored_and_unsupported():
    bot_msg = msg("hi")
    bot_msg["message"]["from"]["is_bot"] = True
    assert parse_update(bot_msg, BOT_ID, BOT) is None
    assert parse_update({"update_id": 1, "edited_message": {}}, BOT_ID, BOT) is None
    assert parse_update(msg("x", chat_type="channel"), BOT_ID, BOT) is None
    photo = msg(None)
    photo["message"].pop("text")
    assert parse_update(photo, BOT_ID, BOT).unsupported


def test_parse_buttons_and_group_events():
    press = {"update_id": 9, "callback_query": {"id": "cq1", "data": "a:call_9:1", "from": {"id": ME},
                                                "message": {"chat": {"id": ME}}}}
    assert parse_update(press, BOT_ID, BOT) == ButtonPress("telegram", str(ME), str(ME), "call_9", True)
    press["callback_query"]["data"] = "something else"
    assert parse_update(press, BOT_ID, BOT) is None

    def member(old, new):
        return {"update_id": 5, "my_chat_member": {"chat": {"id": GROUP, "type": "group", "title": "Team"},
                                                   "from": {"id": ME}, "old_chat_member": {"status": old},
                                                   "new_chat_member": {"status": new}}}

    assert parse_update(member("left", "member"), BOT_ID, BOT) == GroupEvent("telegram", str(GROUP), str(ME), "bot_added", "Team")
    assert parse_update(member("member", "kicked"), BOT_ID, BOT).kind == "bot_removed"
    assert parse_update(member("member", "administrator"), BOT_ID, BOT) is None  # promoted, not added


def test_callback_data_limit():
    assert approval_keyboard("call_123")["inline_keyboard"][0][1]["callback_data"] == "a:call_123:0"
    with pytest.raises(ValueError):
        approval_keyboard("x" * 70)


def test_to_telegram_html():
    assert to_telegram_html("**FTNT** <up> & *fast*") == "<b>FTNT</b> &lt;up&gt; &amp; <i>fast</i>"
    assert to_telegram_html("```python\nx<1\n```") == "<pre>x&lt;1</pre>"
    assert to_telegram_html("# 报告\n`a&b`") == "<b>报告</b>\n<code>a&amp;b</code>"


def test_needs_a_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        build_channels(["telegram"])


# ---- a fake Bot API ------------------------------------------------------------------------


class FakeTelegram:
    def __init__(self, updates=(), fail_html=False):
        self.calls, self.updates, self.fail_html, self.next_id = [], list(updates), fail_html, 500

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert f"/bot{TOKEN}/" in request.url.path
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getMe":
            return self.ok({"id": BOT_ID, "username": BOT, "first_name": "MyAgentBot",
                            "can_read_all_group_messages": True})
        if method == "getUpdates":
            batch = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            return self.ok(batch)
        if method == "sendMessage" and self.fail_html and body.get("parse_mode") == "HTML":
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                             "description": "Bad Request: can't parse entities"})
        if method == "sendMessage":
            self.next_id += 1
            return self.ok({"message_id": self.next_id})
        return self.ok(True)

    @staticmethod
    def ok(result):
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent(self, method="sendMessage"):
        return [body for name, body in self.calls if name == method]


def hub_for(ch, replies, **kw):
    """make_hub() does not start channels (that is where a channel learns its hub), so connect them here."""
    hub, _ = make_hub(replies, channel=ch, **kw)
    ch._hub = hub
    return hub


def channel(api, allowed=(str(ME),)):
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    ch = TelegramChannel(TOKEN, set(allowed), client=client)
    ch.bot_id, ch.bot_username, ch.bot_name = BOT_ID, BOT, "MyAgentBot"
    return ch


async def test_send_text_html_reply_and_plain_fallback():
    api = FakeTelegram()
    ch = channel(api)
    await ch.send_text(str(GROUP), "**Hi** <you>", reply_to="42")
    body = api.sent()[0]
    assert body["chat_id"] == GROUP and body["parse_mode"] == "HTML" and body["text"] == "<b>Hi</b> &lt;you&gt;"
    assert body["reply_parameters"] == {"message_id": 42, "allow_sending_without_reply": True}

    api2 = FakeTelegram(fail_html=True)
    ch2 = channel(api2)
    await ch2.send_text(str(ME), "**weird** markup")
    assert [b.get("parse_mode") for b in api2.sent()] == ["HTML", None]
    assert api2.sent()[1]["text"] == "**weird** markup"


async def test_approval_card_and_outcome():
    api = FakeTelegram()
    ch = channel(api)
    request = ApprovalRequest("call_9", "sina__send_email", {"to": ["me@sina.com"], "body": "<周报>"})
    handle = await ch.send_approval(str(ME), request, None)
    card = api.sent()[0]
    assert "<code>sina__send_email</code>" in card["text"] and "&lt;周报&gt;" in card["text"]
    assert [b["callback_data"] for b in card["reply_markup"]["inline_keyboard"][0]] == ["a:call_9:1", "a:call_9:0"]
    await ch.close_approval(str(ME), handle, "denied")
    edit = api.sent("editMessageText")[0]
    assert edit["message_id"] == int(handle) and edit["text"].startswith("<b>❌ Denied</b>") and "reply_markup" not in edit
    await ch.close_approval(str(ME), handle, "denied")  # already closed: nothing
    assert len(api.sent("editMessageText")) == 1


async def test_token_never_logged(caplog):
    api = FakeTelegram()
    ch = channel(api)
    with caplog.at_level(logging.INFO, logger="httpx"):
        await ch.call("getMe")
    assert caplog.records and TOKEN not in caplog.text and "<token>" in caplog.text

    def broken(request):
        raise httpx.ConnectError("boom", request=request)

    ch2 = TelegramChannel(TOKEN, set(), client=httpx.AsyncClient(transport=httpx.MockTransport(broken)))
    with pytest.raises(Exception) as exc:
        await ch2.call("getMe")
    assert TOKEN not in str(exc.value) and "ConnectError" in str(exc.value)


async def test_polling_hands_updates_over_once():
    seen = []

    class Hub:
        async def on_message(self, m):
            seen.append(m.text)

    api = FakeTelegram([msg("one", mid=1), msg("two", mid=2)])
    ch = channel(api)
    await ch.start(Hub())
    for _ in range(100):
        if len(seen) >= 2:
            break
        await asyncio.sleep(0.01)
    await ch.stop()
    assert sorted(seen) == ["one", "two"]
    offsets = [b["offset"] for name, b in api.calls if name == "getUpdates"]
    assert offsets[0] == 0 and max(offsets) == 3  # the next poll asks only for newer updates
    assert ("deleteWebhook", {}) in api.calls


# ---- through the hub -------------------------------------------------------------------------


async def test_dm_round_trip_with_button_approval():
    api = FakeTelegram()
    ch = channel(api)
    emails = []
    hub = hub_for(ch, [LLMReply(content=None, tool_calls=[ToolCall("c1", "mail__send_email", '{"to": "me"}')]),
                       LLMReply(content="Sent it.")], sent_emails=emails)
    await ch.handle(msg("email me"))
    for _ in range(100):
        if api.sent() and "reply_markup" in api.sent()[-1]:
            break
        await asyncio.sleep(0.01)
    await ch.handle({"update_id": 2, "callback_query": {"id": "cq1", "data": "a:c1:1", "from": {"id": ME},
                                                        "message": {"chat": {"id": ME}}}})
    await hub.drain()
    assert emails == [{"to": "me"}]
    assert api.sent("answerCallbackQuery")[0] == {"callback_query_id": "cq1", "text": "Approved."}
    assert api.sent()[-1]["text"] == "Sent it."


async def test_strangers_and_setup_mode():
    api = FakeTelegram()
    ch = channel(api, allowed=())
    hub = hub_for(ch, [])
    await ch.handle(msg("hi", sender=AMY, chat=AMY))
    await ch.handle({"update_id": 3, "callback_query": {"id": "cq2", "data": "a:x:1", "from": {"id": AMY},
                                                        "message": {"chat": {"id": AMY}}}})
    assert api.sent() == [] and api.sent("answerCallbackQuery")[0]["text"] == ""


async def test_group_context_comes_from_the_buffer():
    api = FakeTelegram()
    ch = channel(api, allowed=(str(ME), str(AMY)))
    ch.allowed_chats = {str(GROUP)}
    hub = hub_for(ch, [LLMReply(content="Buy less SHOP.")])
    await ch.handle(msg("thinking of buying SHOP", chat=GROUP, chat_type="supergroup", sender=AMY, mid=10))
    await ch.handle(msg(f"@{BOT} any advice?", chat=GROUP, chat_type="supergroup", sender=ME, mid=11))
    await hub.drain()
    seen = hub.runtime.llm.calls[0][0][-1]["content"]
    assert "Amy: thinking of buying SHOP" in seen and seen.endswith("The question:\nany advice?")
    assert api.sent()[-1]["reply_parameters"]["message_id"] == 11  # threaded under the question
    lines = await ch.history(str(GROUP), 10)
    assert lines[-1].from_bot and lines[-1].text == "Buy less SHOP."  # its own answers are remembered too


async def test_owner_gets_claude_and_tasks(monkeypatch, tmp_path):
    settings = cc.ClaudeSettings(enabled=True, folders=[tmp_path], max_turns=5, timeout_minutes=1, max_budget_usd=1,
                                 model="", owners={"telegram": str(ME)}, env={}, auth="subscription", sandbox=False)
    monkeypatch.setattr(cc, "load_claude_settings", lambda: settings)
    api = FakeTelegram()
    ch = channel(api)
    hub = hub_for(ch, [])
    await ch.handle(msg("/tasks"))
    assert api.sent()[-1]["text"].startswith("No scheduled tasks")
    tools = {t.name for t in hub.sessions[("telegram", str(ME))].agent.registry.all()}
    assert "schedule__add" in tools
