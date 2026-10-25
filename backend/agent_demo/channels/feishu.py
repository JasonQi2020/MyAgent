"""Feishu (Lark) channel: long connection for events, HTTPS API for replies.

    Feishu ──outbound WebSocket (lark-oapi ws.Client, own thread)──▶ parse_* ──▶ ChatHub (main loop)
    ChatHub ──send_text / send_approval / close_approval──▶ im.v1.message (async HTTPS)

No public URL is needed: the laptop opens the connection. Events arrive on the SDK's
thread and are handed to the server's event loop with run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from ..config import ConfigError
from ..events import ApprovalRequest
from .base import ButtonPress, ChannelLimits, GroupEvent, HistoryLine, IncomingMessage

log = logging.getLogger("agent_demo.im.feishu")

DEFAULT_DOMAIN = "https://open.feishu.cn"
MAX_ARGS_CHARS = 3500  # keeps the approval card well under Feishu's card size limit
NAMES_CACHE_SECONDS = 600  # group member names, for history lines
OUTCOMES = {
    "approved": ("green", "✅ Approved"),
    "denied": ("red", "❌ Denied"),
    "expired": ("grey", "⌛ Expired (no answer within 5 minutes, so it was not run)"),
}


def _ids(text: str) -> set[str]:
    return {part.strip() for part in text.split(",") if part.strip()}


# ---- parsing (pure functions; the input is the event as a plain dict) ---------


def parse_message(event: dict[str, Any], bot_open_id: str) -> IncomingMessage | None:
    """im.message.receive_v1 -> IncomingMessage, or None for events we ignore."""
    body = event.get("event") or {}
    message = body.get("message") or {}
    sender = body.get("sender") or {}
    if sender.get("sender_type") not in (None, "user"):
        return None  # other bots, including ourselves
    user_id = (sender.get("sender_id") or {}).get("open_id")
    chat_id, message_id = message.get("chat_id"), message.get("message_id")
    chat_type = message.get("chat_type")
    if not (user_id and chat_id and message_id) or chat_type not in ("p2p", "group"):
        return None

    is_group = chat_type == "group"
    mentions = message.get("mentions") or []
    mentioned_bot = not is_group or any(
        (m.get("id") or {}).get("open_id") == bot_open_id for m in mentions
    )
    base = dict(channel="feishu", chat_id=chat_id, user_id=user_id, message_id=message_id,
                is_group=is_group, mentioned_bot=mentioned_bot)

    if message.get("message_type") != "text":
        return IncomingMessage(text="", unsupported=True, **base)
    try:
        text = json.loads(message.get("content") or "{}").get("text", "")
    except (json.JSONDecodeError, AttributeError):
        return IncomingMessage(text="", unsupported=True, **base)

    # Mentions arrive as placeholders (@_user_1). Drop the bot's own, keep others readable.
    for m in mentions:
        is_bot = (m.get("id") or {}).get("open_id") == bot_open_id
        text = text.replace(m.get("key", ""), "" if is_bot else f"@{m.get('name', '')}")
    text = re.sub(r"[ \t]+", " ", text).strip()
    return IncomingMessage(text=text, **base)


def parse_group_event(event: dict[str, Any], kind: str) -> GroupEvent | None:
    """im.chat.member.bot.added_v1 / deleted_v1 -> GroupEvent (kind: bot_added | bot_removed)."""
    body = event.get("event") or {}
    chat_id = body.get("chat_id")
    if not chat_id:
        return None
    operator = (body.get("operator_id") or {}).get("open_id") or ""
    return GroupEvent("feishu", chat_id, operator, kind, body.get("name") or "", bool(body.get("external")))


def parse_card_action(event: dict[str, Any]) -> ButtonPress | None:
    """card.action.trigger -> ButtonPress, or None if it is not one of our approval buttons."""
    body = event.get("event") or {}
    value = (body.get("action") or {}).get("value") or {}
    user_id = (body.get("operator") or {}).get("open_id")
    chat_id = (body.get("context") or {}).get("open_chat_id")
    if not (user_id and chat_id and value.get("call_id")) or not isinstance(value.get("approved"), bool):
        return None
    return ButtonPress("feishu", chat_id, user_id, str(value["call_id"]), value["approved"])


_PLACEHOLDER_TYPES = {"image": "[image]", "file": "[file]", "sticker": "[sticker]", "audio": "[voice message]",
                      "media": "[video]", "share_chat": "[shared group]", "share_user": "[shared contact]"}


def _segments_text(lines: list) -> str:
    """Rich text and cards come as lines of segments: [[{"tag": "text", "text": "..."}, ...], ...]."""
    out = []
    for line in lines or []:
        if isinstance(line, list):
            out.append("".join(seg.get("text") or seg.get("user_name") or "" for seg in line if isinstance(seg, dict)))
    return "\n".join(part for part in out if part.strip())


def message_text(msg_type: str, content: Any) -> str:
    """The readable text of a history item's content (already JSON-decoded)."""
    if not isinstance(content, dict):
        return f"[{msg_type}]"
    if msg_type == "text":
        return content.get("text", "")
    if msg_type == "post":
        body = content if "content" in content else next((v for v in content.values() if isinstance(v, dict)), {})
        return "\n".join(p for p in [body.get("title") or "", _segments_text(body.get("content"))] if p)
    if msg_type == "interactive":  # cards, e.g. the bot's own answers
        return "\n".join(p for p in [content.get("title") or "", _segments_text(content.get("elements"))] if p)
    return _PLACEHOLDER_TYPES.get(msg_type, f"[{msg_type}]")


def history_line(item: dict[str, Any], names: dict[str, str], app_id: str, bot_name: str) -> HistoryLine | None:
    """One item of im.v1.message.list -> HistoryLine (None for system and deleted messages)."""
    msg_type = item.get("msg_type") or ""
    if item.get("deleted") or msg_type == "system":
        return None
    try:
        content = json.loads((item.get("body") or {}).get("content") or "{}")
    except json.JSONDecodeError:
        content = None
    text = message_text(msg_type, content)
    for m in item.get("mentions") or []:  # in history, `id` is a plain string
        text = text.replace(m.get("key", ""), f"@{m.get('name', '')}")

    sender = item.get("sender") or {}
    sender_id = sender.get("id") or ""
    from_bot = sender.get("sender_type") == "app"
    if from_bot:
        name = bot_name if sender_id == app_id else "another bot"
    else:
        name = names.get(sender_id) or (f"user-{sender_id[-4:]}" if sender_id else "someone")
    created = datetime.fromtimestamp(int(item.get("create_time") or 0) / 1000, tz=timezone.utc)
    return HistoryLine(item.get("message_id") or "", created, name, text.strip(), from_bot)


# ---- cards (JSON 2.0) --------------------------------------------------------


def text_card(markdown: str) -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {"elements": [{"tag": "markdown", "content": markdown}]},
    }


def _args_block(request_args: dict) -> str:
    args = json.dumps(request_args, ensure_ascii=False, indent=2)
    if len(args) > MAX_ARGS_CHARS:
        extra = len(args) - MAX_ARGS_CHARS
        args = args[:MAX_ARGS_CHARS] + f"\n… ({extra} more characters not shown)"
    return f"```json\n{args}\n```"


def _button(label: str, kind: str, call_id: str, approved: bool) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": kind,
        "behaviors": [{"type": "callback", "value": {"call_id": call_id, "approved": approved}}],
    }


def approval_card(request: ApprovalRequest) -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "Approval needed"}, "template": "orange"},
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"The agent wants to run **{request.name}** with:"},
                {"tag": "markdown", "content": _args_block(request.arguments)},
                {
                    "tag": "column_set",
                    "columns": [
                        {"tag": "column", "width": "auto",
                         "elements": [_button("Approve", "primary", request.id, True)]},
                        {"tag": "column", "width": "auto",
                         "elements": [_button("Deny", "danger", request.id, False)]},
                    ],
                },
                {"tag": "markdown", "content": "Or reply **yes** / **no**. No answer in 5 minutes means no."},
            ]
        },
    }


def closed_card(request: ApprovalRequest, outcome: str) -> dict:
    template, title = OUTCOMES.get(outcome, ("grey", outcome))
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"**{request.name}** with:"},
                {"tag": "markdown", "content": _args_block(request.arguments)},
            ]
        },
    }


# ---- the channel -------------------------------------------------------------


class FeishuChannel:
    name = "feishu"
    limits = ChannelLimits(max_text_chars=4000, supports_buttons=True)

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        allowed_users: set[str],
        allowed_chats: set[str] | None = None,
        domain: str = DEFAULT_DOMAIN,
        client: Any = None,
    ):
        self.app_id, self.app_secret, self.domain = app_id, app_secret, domain
        self.allowed_users = allowed_users
        self.allowed_chats = allowed_chats or set()
        self.client = client or (
            lark.Client.builder().app_id(app_id).app_secret(app_secret).domain(domain)
            .log_level(lark.LogLevel.WARNING).build()
        )
        self.bot_open_id = ""
        self.bot_name = "AgentDemoBot"  # replaced by the real name at start-up
        self._names: dict[str, tuple[float, dict[str, str]]] = {}  # chat_id -> (when, open_id -> name)
        self._requests: dict[str, ApprovalRequest] = {}  # approval message id -> request
        self._hub = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    @classmethod
    def from_env(cls) -> "FeishuChannel":
        app_id, app_secret = os.getenv("FEISHU_APP_ID", ""), os.getenv("FEISHU_APP_SECRET", "")
        if not (app_id and app_secret):
            raise ConfigError("Feishu needs FEISHU_APP_ID and FEISHU_APP_SECRET in .env")
        return cls(
            app_id,
            app_secret,
            allowed_users=_ids(os.getenv("FEISHU_ALLOWED_USERS", "")),
            allowed_chats=_ids(os.getenv("FEISHU_ALLOWED_CHATS", "")),
            domain=os.getenv("FEISHU_DOMAIN", DEFAULT_DOMAIN).rstrip("/"),
        )

    # ---- lifecycle ----------------------------------------------------------

    async def start(self, hub) -> None:
        self._hub, self._loop = hub, asyncio.get_running_loop()
        self.bot_open_id = await self._fetch_bot_open_id()
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_event)
            .register_p2_card_action_trigger(self._on_card_action)
            .register_p2_im_chat_member_bot_added_v1(lambda data: self._on_group_event(data, "bot_added"))
            .register_p2_im_chat_member_bot_deleted_v1(lambda data: self._on_group_event(data, "bot_removed"))
            .build()
        )
        ws = lark.ws.Client(
            self.app_id, self.app_secret, event_handler=handler, domain=self.domain,
            log_level=lark.LogLevel.INFO,
        )
        self._thread = threading.Thread(target=self._run_ws, args=(ws,), name="feishu-ws", daemon=True)
        self._thread.start()
        log.info("Feishu: long connection starting (bot open_id %s)", self.bot_open_id)

    def _run_ws(self, ws) -> None:
        # The SDK keeps a module-level event loop picked at import time (the server's own loop
        # here). Give this thread a fresh loop and point the SDK at it before start().
        import lark_oapi.ws.client as ws_client

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ws_client.loop = loop
        try:
            ws.start()  # blocks; reconnects automatically
        except Exception:  # noqa: BLE001
            log.exception("Feishu: long connection stopped")

    async def stop(self) -> None:
        # The SDK has no clean shutdown; the daemon thread ends with the process.
        self._stopped = True

    def routes(self):
        return None  # long connection: no webhook

    async def _fetch_bot_open_id(self) -> str:
        request = (
            lark.BaseRequest.builder().http_method(lark.HttpMethod.GET).uri("/open-apis/bot/v3/info")
            .token_types({lark.AccessTokenType.TENANT}).build()
        )
        response = await self.client.arequest(request)
        data = json.loads(response.raw.content or b"{}")
        if data.get("code") != 0:
            raise RuntimeError(f"cannot read the bot's identity: {data.get('code')} {data.get('msg')} "
                               "(is the Bot feature enabled and the app version published?)")
        self.bot_name = data["bot"].get("app_name") or self.bot_name
        return data["bot"]["open_id"]

    # ---- group history (design §12) --------------------------------------------

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        # Parameters go through .queries(): the SDK's async client mishandles a query string
        # written into the URI (Feishu then answers 99992402 "field validation failed").
        request = (
            lark.BaseRequest.builder().http_method(lark.HttpMethod.GET).uri(path)
            .queries([(k, str(v)) for k, v in params.items()])
            .token_types({lark.AccessTokenType.TENANT}).build()
        )
        response = await self.client.arequest(request)
        data = json.loads(response.raw.content or b"{}")
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu API error {data.get('code')}: {data.get('msg')}")
        return data.get("data") or {}

    async def _member_names(self, chat_id: str) -> dict[str, str]:
        """open_id -> name for the people in a group, cached for a few minutes."""
        cached = self._names.get(chat_id)
        if cached and time.monotonic() - cached[0] < NAMES_CACHE_SECONDS:
            return cached[1]
        names, token = {}, ""
        while True:
            params = {"member_id_type": "open_id", "page_size": 100}
            if token:
                params["page_token"] = token
            data = await self._get(f"/open-apis/im/v1/chats/{chat_id}/members", params)
            names.update({m["member_id"]: m.get("name") or "" for m in data.get("items") or []})
            token = data.get("page_token") or ""
            if not data.get("has_more") or not token:
                break
        self._names[chat_id] = (time.monotonic(), names)
        return names

    async def history(self, chat_id: str, limit: int, since: datetime | None = None) -> list[HistoryLine]:
        """Up to `limit` of the group's most recent messages, oldest first."""
        try:
            names = await self._member_names(chat_id)
        except RuntimeError:
            log.warning("Feishu: cannot read the member list of %s; showing short ids", chat_id, exc_info=True)
            names = {}
        lines: list[HistoryLine] = []
        token = ""
        while len(lines) < limit:
            params = {
                "container_id_type": "chat", "container_id": chat_id,
                "sort_type": "ByCreateTimeDesc", "page_size": min(50, limit),
            }
            if since is not None:
                params["start_time"] = int(since.timestamp())
            if token:
                params["page_token"] = token
            data = await self._get("/open-apis/im/v1/messages", params)
            for item in data.get("items") or []:
                line = history_line(item, names, self.app_id, self.bot_name)
                if line is not None:
                    lines.append(line)
            token = data.get("page_token") or ""
            if not data.get("has_more") or not token:
                break
        return list(reversed(lines[:limit]))

    # ---- events (called on the SDK thread) -----------------------------------

    def _on_message_event(self, data) -> None:
        if self._stopped:
            return
        msg = parse_message(json.loads(lark.JSON.marshal(data)), self.bot_open_id)
        if msg is not None:
            # Hand over and return at once, so Feishu gets its acknowledgement quickly.
            asyncio.run_coroutine_threadsafe(self._hub.on_message(msg), self._loop)

    def _on_group_event(self, data, kind: str) -> None:
        if self._stopped:
            return
        event = parse_group_event(json.loads(lark.JSON.marshal(data)), kind)
        if event is not None:
            asyncio.run_coroutine_threadsafe(self._hub.on_group_event(event), self._loop)

    def _on_card_action(self, data) -> P2CardActionTriggerResponse:
        press = None if self._stopped else parse_card_action(json.loads(lark.JSON.marshal(data)))
        if press is None:
            notice = "This button is no longer active."
        else:
            future = asyncio.run_coroutine_threadsafe(self._hub.on_button(press), self._loop)
            try:
                notice = future.result(timeout=2.5)  # Feishu expects an answer within ~3 s
            except Exception:  # noqa: BLE001
                notice = "Received."
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": notice}})

    # ---- sending (main loop) -------------------------------------------------

    async def _send_card(self, chat_id: str, card: dict, reply_to: str | None) -> str:
        content = json.dumps(card, ensure_ascii=False)
        api = self.client.im.v1.message
        if reply_to:
            request = (
                ReplyMessageRequest.builder().message_id(reply_to)
                .request_body(ReplyMessageRequestBody.builder().msg_type("interactive").content(content).build())
                .build()
            )
            response = await api.areply(request)
        else:
            request = (
                CreateMessageRequest.builder().receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive")
                    .content(content).build()
                )
                .build()
            )
            response = await api.acreate(request)
        if not response.success():
            raise RuntimeError(f"Feishu send failed: {response.code} {response.msg}")
        return response.data.message_id

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> None:
        await self._send_card(chat_id, text_card(text), reply_to)

    async def send_approval(self, chat_id: str, request: ApprovalRequest, reply_to: str | None) -> str:
        message_id = await self._send_card(chat_id, approval_card(request), reply_to)
        self._requests[message_id] = request
        return message_id

    async def close_approval(self, chat_id: str, handle: str, outcome: str) -> None:
        request = self._requests.pop(handle, None)
        if request is None:
            return
        body = PatchMessageRequestBody.builder().content(json.dumps(closed_card(request, outcome), ensure_ascii=False))
        response = await self.client.im.v1.message.apatch(
            PatchMessageRequest.builder().message_id(handle).request_body(body.build()).build()
        )
        if not response.success():
            raise RuntimeError(f"Feishu card update failed: {response.code} {response.msg}")
