"""WhatsApp channel (Cloud API): a webhook for incoming messages, the Graph API for replies.

    WhatsApp ──HTTPS POST /webhooks/whatsapp (through a tunnel, signed)──▶ parse_webhook ──▶ ChatHub
    ChatHub ──send_text / send_approval / close_approval──▶ POST /{phone_number_id}/messages

Direct messages only (design/whatsapp_integration.md): a chat is one phone number, so
chat_id == user_id. WhatsApp messages cannot be edited, so the outcome of an approval
is a follow-up message quoting the buttons.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from ..config import ConfigError
from ..events import ApprovalRequest
from . import render
from .base import ButtonPress, ChannelLimits, IncomingMessage

log = logging.getLogger("agent_demo.im.whatsapp")

DEFAULT_API_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v25.0"
WEBHOOK_PATH = "/webhooks/whatsapp"
BODY_LIMIT = 1024  # interactive message body
BUTTON_ID_LIMIT = 256
MAX_ARGS_CHARS = 3500  # the arguments message stays under the 4096-character text limit
WINDOW_CLOSED = 131047  # more than 24 hours since the user's last message
OUTCOMES = {
    "approved": "✅ Approved",
    "denied": "❌ Denied",
    "expired": "⌛ Expired (no answer within 5 minutes, so it was not run)",
}


def _numbers(text: str) -> set[str]:
    """`+86 138-0000-0000, 4915112345678` -> {"8613800000000", "4915112345678"} (WhatsApp's wa_id form)."""
    return {re.sub(r"\D", "", part) for part in text.split(",") if re.sub(r"\D", "", part)}


# ---- webhook (pure functions) ------------------------------------------------


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 is "sha256=" + HMAC-SHA256(app secret, raw body)."""
    if not header:
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def button_id(call_id: str, approved: bool) -> str:
    value = f"ap:{call_id}:{'y' if approved else 'n'}"
    if len(value) > BUTTON_ID_LIMIT:
        raise ValueError(f"tool call id too long for a WhatsApp button ({len(call_id)} characters)")
    return value


def parse_button_id(value: str) -> tuple[str, bool] | None:
    if not value.startswith("ap:"):
        return None
    call_id, _, answer = value[3:].rpartition(":")
    if not call_id or answer not in ("y", "n"):
        return None
    return call_id, answer == "y"


def _parse_one(message: dict[str, Any]) -> IncomingMessage | ButtonPress | None:
    user, message_id, kind = message.get("from"), message.get("id"), message.get("type")
    if not (user and message_id):
        return None
    base = dict(channel="whatsapp", chat_id=user, user_id=user, message_id=message_id)
    if kind == "text":
        return IncomingMessage(text=(message.get("text") or {}).get("body", ""), **base)
    if kind == "interactive":
        reply = (message.get("interactive") or {}).get("button_reply") or {}
        parsed = parse_button_id(str(reply.get("id", "")))
        if parsed is None:
            return None
        return ButtonPress("whatsapp", user, user, *parsed)
    return IncomingMessage(text="", unsupported=True, **base)  # image, voice note, sticker, location…


def parse_webhook(payload: Any, phone_number_id: str) -> list[IncomingMessage | ButtonPress]:
    """A webhook POST body -> messages and button presses for our number. Status updates are ignored."""
    items = []
    try:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                if (value.get("metadata") or {}).get("phone_number_id") != phone_number_id:
                    continue
                for message in value.get("messages") or []:  # "statuses" (sent/delivered/read) skipped
                    item = _parse_one(message)
                    if item is not None:
                        items.append(item)
    except (AttributeError, TypeError):
        log.warning("WhatsApp: ignored a malformed webhook payload")
        return []
    return items


# ---- outgoing messages ---------------------------------------------------------


def text_payload(to: str, text: str, reply_to: str | None = None) -> dict:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": text, "preview_url": False},
    }
    if reply_to:
        payload["context"] = {"message_id": reply_to}
    return payload


def buttons_payload(to: str, body: str, call_id: str) -> dict:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": button_id(call_id, True), "title": "Approve"}},
                    {"type": "reply", "reply": {"id": button_id(call_id, False), "title": "Deny"}},
                ]
            },
        },
    }


def approval_messages(request: ApprovalRequest) -> tuple[str | None, str]:
    """(arguments message or None, body of the buttons message).

    Button bodies are limited to 1,024 characters, so long arguments (an email body)
    go in a text message of their own, just before the buttons.
    """
    args = json.dumps(request.arguments, ensure_ascii=False, indent=2)
    body = f"*Approval needed:* {request.name}\n```{args}```"
    if len(body) <= BODY_LIMIT:
        return None, body
    if len(args) > MAX_ARGS_CHARS:
        extra = len(args) - MAX_ARGS_CHARS
        args = args[:MAX_ARGS_CHARS] + f"\n… ({extra} more characters not shown)"
    return f"*Approval needed:* {request.name}\n```{args}```", f"Approve the action above? ({request.name})"[:BODY_LIMIT]


class WhatsAppChannel:
    name = "whatsapp"
    limits = ChannelLimits(max_text_chars=4000, supports_buttons=True)  # 4096 minus room for to_whatsapp()

    def __init__(
        self,
        token: str,
        phone_number_id: str,
        app_secret: str,
        verify_token: str,
        allowed_users: set[str],
        api_base: str = DEFAULT_API_BASE,
        api_version: str = DEFAULT_API_VERSION,
        client: httpx.AsyncClient | None = None,
    ):
        self.phone_number_id, self.app_secret, self.verify_token = phone_number_id, app_secret, verify_token
        self.allowed_users = allowed_users
        self.allowed_chats: set[str] = set()  # direct messages only
        self.api = f"{api_base.rstrip('/')}/{api_version}"
        self._own_client = client is None
        # httpx honours HTTPS_PROXY, for networks where Meta is not reachable directly.
        self.client = client or httpx.AsyncClient(timeout=20)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._open: dict[str, str] = {}  # approval message id -> tool call id, until answered
        self._tasks: set[asyncio.Task] = set()
        self._hub = None

    @classmethod
    def from_env(cls) -> "WhatsAppChannel":
        names = ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"]
        missing = [n for n in names if not os.getenv(n)]
        if missing:
            raise ConfigError(f"WhatsApp needs {', '.join(missing)} in .env")
        token, phone_number_id, app_secret, verify_token = (os.environ[n] for n in names)
        return cls(
            token, phone_number_id, app_secret, verify_token,
            allowed_users=_numbers(os.getenv("WHATSAPP_ALLOWED_NUMBERS", "")),
            api_base=os.getenv("WHATSAPP_API_BASE", DEFAULT_API_BASE),
            api_version=os.getenv("WHATSAPP_API_VERSION", DEFAULT_API_VERSION),
        )

    # ---- lifecycle ----------------------------------------------------------

    async def start(self, hub) -> None:
        self._hub = hub
        # Checks the token and the number id before the webhook starts accepting messages.
        response = await self.client.get(
            f"{self.api}/{self.phone_number_id}", params={"fields": "display_phone_number,verified_name"},
            headers=self._headers,
        )
        data = self._json(response)
        if response.is_error:
            error = data.get("error") or {}
            raise RuntimeError(f"cannot read the WhatsApp number: {error.get('code')} {error.get('message')} "
                               "(check WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID)")
        log.info("WhatsApp: number %s (%s); webhook at %s",
                 data.get("display_phone_number"), data.get("verified_name"), WEBHOOK_PATH)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._own_client:
            await self.client.aclose()

    # ---- webhook --------------------------------------------------------------

    def routes(self) -> APIRouter:
        router = APIRouter()
        router.add_api_route(WEBHOOK_PATH, self._verify, methods=["GET"])
        router.add_api_route(WEBHOOK_PATH, self._receive, methods=["POST"])
        return router

    async def _verify(self, request: Request) -> Response:
        """Meta's one-time check when the webhook URL is saved in the console."""
        params = request.query_params
        if params.get("hub.mode") == "subscribe" and hmac.compare_digest(
            params.get("hub.verify_token", ""), self.verify_token
        ):
            return PlainTextResponse(params.get("hub.challenge", ""))
        log.warning("WhatsApp: webhook verification with a wrong verify token")
        return Response(status_code=403)

    async def _receive(self, request: Request) -> Response:
        body = await request.body()
        if not verify_signature(self.app_secret, body, request.headers.get("x-hub-signature-256")):
            log.warning("WhatsApp: rejected a webhook call with a bad signature")
            return Response(status_code=401)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return Response(status_code=400)
        for item in parse_webhook(payload, self.phone_number_id):
            if isinstance(item, ButtonPress):
                self._spawn(self._on_button(item))
            else:
                self._spawn(self._hub.on_message(item))
        return Response(status_code=200)  # at once: Meta retries slow or failed deliveries

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_button(self, press: ButtonPress) -> None:
        was_open = press.call_id in self._open.values()
        notice = await self._hub.on_button(press)
        # A successful press is confirmed by close_approval. Strangers get no answer at all.
        if not was_open and press.user_id in self.allowed_users:
            try:
                await self.send_text(press.chat_id, notice)
            except Exception:  # noqa: BLE001
                log.exception("WhatsApp: sending the button notice failed")

    # ---- sending ------------------------------------------------------------------

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def _post(self, payload: dict) -> str:
        response = await self.client.post(f"{self.api}/{self.phone_number_id}/messages",
                                          json=payload, headers=self._headers)
        data = self._json(response)
        if response.is_error:
            error = data.get("error") or {}
            if error.get("code") == WINDOW_CLOSED:
                log.warning("WhatsApp: %s last wrote more than 24 hours ago; free-form replies are not allowed",
                            payload.get("to"))
            raise RuntimeError(f"WhatsApp send failed: {error.get('code', response.status_code)} "
                               f"{error.get('message', '')}".rstrip())
        return ((data.get("messages") or [{}])[0]).get("id", "")

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> None:
        await self._post(text_payload(chat_id, render.to_whatsapp(text), reply_to))

    async def send_approval(self, chat_id: str, request: ApprovalRequest, reply_to: str | None) -> str:
        args_text, body = approval_messages(request)
        if args_text:
            await self._post(text_payload(chat_id, args_text))
        handle = await self._post(buttons_payload(chat_id, body, request.id))
        self._open[handle] = request.id
        return handle

    async def close_approval(self, chat_id: str, handle: str, outcome: str) -> None:
        if self._open.pop(handle, None) is None:
            return
        await self._post(text_payload(chat_id, OUTCOMES.get(outcome, outcome), reply_to=handle))
