"""WhatsApp channel: webhook parsing and checks, message building, sending through a fake
Graph API (httpx.MockTransport), a full DM round trip with the ChatHub, and the server's
tunnel guard. No network."""

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_demo.channels import build_channels
from agent_demo.channels.base import ButtonPress, IncomingMessage
from agent_demo.channels.render import to_whatsapp
from agent_demo.channels.whatsapp import (
    BODY_LIMIT,
    WhatsAppChannel,
    approval_messages,
    button_id,
    parse_button_id,
    parse_webhook,
    verify_signature,
)
from agent_demo.config import ConfigError
from agent_demo.events import ApprovalRequest
from agent_demo.llm import LLMReply
from agent_demo.server import TunnelGuard
from test_im_hub import call, make_hub

NUMBER_ID, OWNER, SECRET, VERIFY = "1234567890", "8613800000000", "app-secret", "verify-me"


def webhook(*messages, number_id=NUMBER_ID, statuses=None):
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": number_id}}
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "waba", "changes": [{"field": "messages", "value": value}]}]}


def text_msg(body, message_id="wamid.in1", sender=OWNER):
    return {"from": sender, "id": message_id, "timestamp": "1761300000", "type": "text", "text": {"body": body}}


def button_msg(reply_id, message_id="wamid.in2", sender=OWNER):
    return {"from": sender, "id": message_id, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": reply_id, "title": "Approve"}}}


def sign(body: bytes, secret=SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeGraph:
    """Records Graph API calls and answers like Meta does."""

    def __init__(self, fail_with=None):
        self.requests, self.fail_with, self.count = [], fail_with, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, str(request.url), request.headers.get("authorization"), body))
        if self.fail_with:
            return httpx.Response(400, json={"error": {"code": self.fail_with, "message": "nope"}})
        if request.method == "GET":
            return httpx.Response(200, json={"display_phone_number": "+1 555 0100", "verified_name": "Test"})
        self.count += 1
        return httpx.Response(200, json={"messages": [{"id": f"wamid.out{self.count}"}]})

    def sent(self):
        return [body for method, _, _, body in self.requests if method == "POST"]


def make_channel(graph=None, allowed=(OWNER,)):
    graph = graph or FakeGraph()
    client = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    channel = WhatsAppChannel("tok", NUMBER_ID, SECRET, VERIFY, set(allowed), client=client)
    return channel, graph


# ---- parsing ------------------------------------------------------------------


def test_parse_text_message():
    [msg] = parse_webhook(webhook(text_msg("hello")), NUMBER_ID)
    assert msg == IncomingMessage("whatsapp", OWNER, OWNER, "wamid.in1", "hello")
    assert not msg.is_group and msg.mentioned_bot


def test_parse_button_reply():
    [press] = parse_webhook(webhook(button_msg("ap:call_0:y")), NUMBER_ID)
    assert press == ButtonPress("whatsapp", OWNER, OWNER, "call_0", True)
    [press] = parse_webhook(webhook(button_msg("ap:a:b:n")), NUMBER_ID)
    assert press.call_id == "a:b" and press.approved is False
    assert parse_webhook(webhook(button_msg("other")), NUMBER_ID) == []


def test_statuses_other_numbers_and_malformed_payloads_are_ignored():
    assert parse_webhook(webhook(statuses=[{"id": "wamid.out1", "status": "read"}]), NUMBER_ID) == []
    assert parse_webhook(webhook(text_msg("hi"), number_id="999"), NUMBER_ID) == []
    assert parse_webhook({"entry": "nonsense"}, NUMBER_ID) == []
    assert parse_webhook([], NUMBER_ID) == []
    assert parse_webhook(webhook({"type": "text"}), NUMBER_ID) == []  # no sender or id


@pytest.mark.parametrize("kind", ["audio", "image", "sticker", "location"])
def test_other_message_types_are_unsupported(kind):
    [msg] = parse_webhook(webhook({"from": OWNER, "id": "wamid.x", "type": kind}), NUMBER_ID)
    assert msg.unsupported and msg.text == ""


def test_signature():
    body = b'{"a": 1}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(body, "wrong"))
    assert not verify_signature(SECRET, body, None)


def test_button_ids():
    assert parse_button_id(button_id("call_1", True)) == ("call_1", True)
    assert parse_button_id(button_id("call_1", False)) == ("call_1", False)
    with pytest.raises(ValueError):
        button_id("x" * 300, True)


# ---- rendering and message building ----------------------------------------------


def test_to_whatsapp():
    assert to_whatsapp("**bold**, *italic*, ~~gone~~") == "*bold*, _italic_, ~gone~"
    assert to_whatsapp("## Summary **now**") == "*Summary now*"
    assert to_whatsapp("[docs](https://x.io) and [https://x.io](https://x.io)") == "docs (https://x.io) and https://x.io"
    assert to_whatsapp("* item **b**\n2*3*4") == "* item *b*\n2*3*4"
    code = "```\n**keep** *this*\n```"
    assert to_whatsapp(f"**a** `**b**`\n{code}") == f"*a* `**b**`\n{code}"


def test_short_approval_is_one_message():
    args_text, body = approval_messages(ApprovalRequest("c1", "mail__send_email", {"to": "me@x.com"}))
    assert args_text is None
    assert body.startswith("*Approval needed:* mail__send_email") and '"to": "me@x.com"' in body


def test_long_approval_sends_arguments_first():
    request = ApprovalRequest("c1", "mail__send_email", {"to": "me@x.com", "body": "x" * 5000})
    args_text, body = approval_messages(request)
    assert len(body) <= BODY_LIMIT and "mail__send_email" in body
    assert '"to": "me@x.com"' in args_text and "more characters not shown" in args_text
    assert len(args_text) < 4096


# ---- configuration ---------------------------------------------------------------------


def test_from_env(monkeypatch):
    for name, value in [("WHATSAPP_TOKEN", "t"), ("WHATSAPP_PHONE_NUMBER_ID", NUMBER_ID),
                        ("WHATSAPP_APP_SECRET", "s"), ("WHATSAPP_VERIFY_TOKEN", "v"),
                        ("WHATSAPP_ALLOWED_NUMBERS", "+86 138-0000-0000, 4915112345678,")]:
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("WHATSAPP_API_VERSION", raising=False)
    [channel] = build_channels(["whatsapp"])
    assert channel.allowed_users == {OWNER, "4915112345678"} and channel.allowed_chats == set()
    assert channel.api == "https://graph.facebook.com/v25.0"
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "")
    with pytest.raises(ConfigError, match="WHATSAPP_APP_SECRET"):
        WhatsAppChannel.from_env()


# ---- sending ---------------------------------------------------------------------------


async def test_start_checks_the_number():
    channel, graph = make_channel()
    await channel.start(hub=None)
    method, url, auth, _ = graph.requests[0]
    assert method == "GET" and url.startswith(f"https://graph.facebook.com/v25.0/{NUMBER_ID}?")
    assert auth == "Bearer tok"

    channel, _ = make_channel(FakeGraph(fail_with=190))
    with pytest.raises(RuntimeError, match="190"):
        await channel.start(hub=None)


async def test_send_text_converts_markup():
    channel, graph = make_channel()
    await channel.send_text(OWNER, "**hi**")
    _, url, _, body = graph.requests[0]
    assert url == f"https://graph.facebook.com/v25.0/{NUMBER_ID}/messages"
    assert body["to"] == OWNER and body["text"]["body"] == "*hi*" and "context" not in body


async def test_send_errors_raise():
    channel, _ = make_channel(FakeGraph(fail_with=131047))
    with pytest.raises(RuntimeError, match="131047"):
        await channel.send_text(OWNER, "hi")


async def test_approval_and_close():
    channel, graph = make_channel()
    handle = await channel.send_approval(OWNER, ApprovalRequest("c1", "mail__send_email", {"to": "me"}), None)
    buttons = graph.sent()[0]["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["ap:c1:y", "ap:c1:n"]
    await channel.close_approval(OWNER, handle, "denied")
    closed = graph.sent()[1]
    assert closed["text"]["body"] == "❌ Denied" and closed["context"] == {"message_id": handle}
    await channel.close_approval(OWNER, handle, "denied")  # already closed: nothing sent
    assert len(graph.sent()) == 2


async def test_long_approval_sends_two_messages():
    channel, graph = make_channel()
    await channel.send_approval(OWNER, ApprovalRequest("c1", "mail__send_email", {"body": "x" * 2000}), None)
    first, second = graph.sent()
    assert first["type"] == "text" and second["type"] == "interactive"


# ---- webhook routes and a full round trip with the hub --------------------------------


def webhook_app(channel):
    app = FastAPI()
    app.include_router(channel.routes())
    return app


def test_webhook_verification():
    channel, _ = make_channel()
    client = TestClient(webhook_app(channel))
    ok = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY,
                                                   "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                                    "hub.challenge": "12345"})
    assert bad.status_code == 403


def test_webhook_rejects_bad_signatures():
    channel, _ = make_channel()
    client = TestClient(webhook_app(channel))
    body = json.dumps(webhook(text_msg("hi"))).encode()
    assert client.post("/webhooks/whatsapp", content=body).status_code == 401
    assert client.post("/webhooks/whatsapp", content=body,
                       headers={"X-Hub-Signature-256": sign(body, "wrong")}).status_code == 401
    assert client.post("/webhooks/whatsapp", content=b"{", headers={"X-Hub-Signature-256": sign(b"{")}).status_code == 400


async def post(client, payload):
    body = json.dumps(payload).encode()
    response = await client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert response.status_code == 200


async def settle(channel, hub):
    while channel._tasks:
        await asyncio.gather(*list(channel._tasks))
    await hub.drain()


async def wait_for(condition):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def test_dm_round_trip_with_approval_button():
    channel, graph = make_channel()
    sent_emails = []
    hub, _ = make_hub([call("mail__send_email", to="me@x.com"), LLMReply(content="**Sent.**")],
                      channel=channel, sent_emails=sent_emails)
    channel._hub = hub
    transport = httpx.ASGITransport(app=webhook_app(channel))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await post(client, webhook(text_msg("email me")))
        await wait_for(lambda: any(b["type"] == "interactive" for b in graph.sent()))
        assert graph.sent()[0]["text"]["body"].startswith("🔧 mail__send_email")

        await post(client, webhook(button_msg("ap:c1:y")))
        await settle(channel, hub)

    bodies = [b["text"]["body"] for b in graph.sent() if b["type"] == "text"]
    assert bodies[-2:] == ["✅ Approved", "*Sent.*"]
    assert sent_emails == [{"to": "me@x.com"}]

    # Pressing the old button again: the approval is gone, so the user is told.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await post(client, webhook(button_msg("ap:c1:y", message_id="wamid.in3")))
        await settle(channel, hub)
    assert graph.sent()[-1]["text"]["body"] == "This request has already been answered or has expired."


async def test_strangers_get_no_answer():
    channel, graph = make_channel()
    hub, _ = make_hub([], channel=channel)
    channel._hub = hub
    transport = httpx.ASGITransport(app=webhook_app(channel))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await post(client, webhook(text_msg("hi", sender="4900000")))
        await post(client, webhook(button_msg("ap:c1:y", sender="4900000")))
        await settle(channel, hub)
    assert graph.sent() == []


# ---- tunnel guard (server.py) ----------------------------------------------------------


def guarded_app():
    app = FastAPI()

    @app.get("/api/x")
    async def local_only():
        return {"ok": True}

    @app.post("/webhooks/whatsapp")
    async def hook():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"ok": True})
        await websocket.close()

    app.add_middleware(TunnelGuard)
    return app


@pytest.mark.parametrize("header", ["X-Forwarded-For", "CF-Connecting-IP", "Forwarded"])
def test_tunnel_guard(header):
    client = TestClient(guarded_app())
    forwarded = {header: "203.0.113.9"}
    assert client.get("/api/x").status_code == 200
    assert client.get("/api/x", headers=forwarded).status_code == 403
    assert client.post("/webhooks/whatsapp", headers=forwarded).status_code == 200
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"ok": True}
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws", headers=forwarded) as ws:
        ws.receive_json()


def test_webhook_routes_go_before_the_web_ui_mount(tmp_path):
    # The built web UI is mounted at "/" and would answer POST /webhooks/whatsapp with 405.
    from agent_demo.server import WebUIFiles, include_first

    (tmp_path / "index.html").write_text("<html></html>")
    app = FastAPI()
    app.mount("/", WebUIFiles(directory=tmp_path, html=True))
    channel, _ = make_channel()
    include_first(app, channel.routes())
    client = TestClient(app)
    assert client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY,
                                                     "hub.challenge": "7"}).text == "7"
    assert client.get("/").status_code == 200
