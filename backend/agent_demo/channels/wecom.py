"""WeCom (企业微信) channel: a smart robot (智能机器人) over its long connection.

    WeCom ◀──outbound WebSocket (our own client, on the server's event loop)──▶ WeComChannel ──▶ ChatHub

No public URL is needed. The protocol is a few JSON frames (design/wecom_integration.md §1):
    {"cmd": "...", "headers": {"req_id": "..."}, "body": {...}}
We send aibot_subscribe, ping every 30 s, aibot_send_msg (push), aibot_respond_msg (the
streamed answer to a message) and aibot_respond_update_msg (a clicked card). WeCom sends
aibot_msg_callback and aibot_event_callback; answers to our frames echo our req_id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from ..config import ConfigError
from ..events import ApprovalRequest
from . import render
from .base import ButtonPress, ChannelLimits, IncomingMessage

log = logging.getLogger("agent_demo.im.wecom")

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_SECONDS = 30
REQUEST_TIMEOUT = 10
START_TIMEOUT = 10
MAX_BACKOFF = 30
MARKDOWN_BYTES = 4000  # WeCom markdown limit is 4,096 bytes
STREAM_BYTES = 20000  # stream content limit is 20,480 bytes
STREAM_ROLLOVER_SECONDS = 8 * 60  # a stream lives at most 10 minutes
MAX_ARGS_CHARS = 3500
REMEMBERED = 1000  # message id -> req_id entries kept for streamed replies
SINGLE, GROUP = 1, 2  # aibot_send_msg chat_type
OUTCOMES = {
    "approved": "✅ Approved",
    "denied": "❌ Denied",
    "expired": "⌛ Expired (no answer within 5 minutes, so it was not run)",
}


class AuthError(Exception):
    """aibot_subscribe was refused: retrying will not help."""


def _ids(text: str) -> set[str]:
    return {part.strip() for part in text.split(",") if part.strip()}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---- parsing (pure functions; the input is a callback's body) -------------------


def _chat(body: dict[str, Any]) -> tuple[str, str, bool] | None:
    """(chat_id, user_id, is_group): DMs are addressed by the user's id, groups by chatid."""
    user_id = (body.get("from") or {}).get("userid")
    is_group = body.get("chattype") == "group"
    chat_id = body.get("chatid") if is_group else user_id
    if not (user_id and chat_id):
        return None
    return chat_id, user_id, is_group


def parse_message(body: dict[str, Any]) -> IncomingMessage | None:
    """aibot_msg_callback body -> IncomingMessage, or None if it lacks the ids we need."""
    chat = _chat(body)
    message_id = body.get("msgid")
    if chat is None or not message_id:
        return None
    chat_id, user_id, is_group = chat
    base = dict(channel="wecom", chat_id=chat_id, user_id=user_id, message_id=message_id,
                is_group=is_group, mentioned_bot=True)  # WeCom only delivers group messages that @mention the bot

    kind = body.get("msgtype")
    if kind == "text":
        text = (body.get("text") or {}).get("content", "")
    elif kind == "voice":  # arrives already transcribed
        text = (body.get("voice") or {}).get("content", "")
    elif kind == "mixed":  # text and images: keep the text
        items = (body.get("mixed") or {}).get("msg_item") or []
        text = "\n".join((i.get("text") or {}).get("content", "") for i in items if i.get("msgtype") == "text")
    else:
        return IncomingMessage(text="", unsupported=True, **base)
    if is_group:
        text = re.sub(r"^\s*@\S+\s*", "", text)  # the leading "@BotName"
    text = text.strip()
    if kind == "mixed" and not text:
        return IncomingMessage(text="", unsupported=True, **base)
    return IncomingMessage(text=text, **base)


def parse_click(body: dict[str, Any], cards: dict[str, str]) -> tuple[str, ButtonPress] | None:
    """template_card_event body -> (task_id, ButtonPress); `cards` maps our task_ids to call ids."""
    event = body.get("event") or {}
    if event.get("eventtype") != "template_card_event":
        return None
    detail = event.get("template_card_event") or event  # the key and task id, nested or flat
    task_id, key = detail.get("task_id"), detail.get("event_key")
    chat = _chat(body)
    if chat is None or task_id not in cards or key not in ("y", "n"):
        return None
    chat_id, user_id, _ = chat
    return task_id, ButtonPress("wecom", chat_id, user_id, cards[task_id], key == "y")


# ---- messages and cards ---------------------------------------------------------


def approval_markdown(request: ApprovalRequest) -> str:
    args = json.dumps(request.arguments, ensure_ascii=False, indent=2)
    if len(args) > MAX_ARGS_CHARS:
        extra = len(args) - MAX_ARGS_CHARS
        args = args[:MAX_ARGS_CHARS] + f"\n… ({extra} more characters not shown)"
    return f"**Approval needed:** `{request.name}`\n```json\n{args}\n```"


def approval_card(task_id: str, request: ApprovalRequest) -> dict:
    return {
        "card_type": "button_interaction",
        "main_title": {"title": "Approval needed", "desc": request.name},
        "sub_title_text": "Approve or deny the action shown above.",
        "button_list": [
            {"text": "Approve", "style": 1, "key": "y"},
            {"text": "Deny", "style": 2, "key": "n"},
        ],
        "task_id": task_id,
    }


def closed_card(task_id: str, request: ApprovalRequest, label: str) -> dict:
    """The card after a click: same title, the outcome, no buttons."""
    return {
        "card_type": "text_notice",
        "main_title": {"title": "Approval needed", "desc": request.name},
        "sub_title_text": label,
        "task_id": task_id,
    }


def split_bytes(text: str, limit: int) -> list[str]:
    """Split so that every part fits `limit` UTF-8 bytes (a Chinese character is 3 bytes)."""
    if len(text.encode()) <= limit:
        return [text] if text.strip() else []
    return render.split_text(text, limit // 4)


# ---- the streamed answer to one message --------------------------------------------


class WeComStream:
    """One reply that grows while the agent works (aibot_respond_msg, msgtype stream).

    Updates are coalesced to one frame per `interval` seconds. After 8 minutes the
    stream is finished early (WeCom ends it at 10), and the rest is pushed at the end.
    """

    max_bytes = STREAM_BYTES

    def __init__(self, channel: WeComChannel, chat_id: str, req_id: str, interval: float):
        self.channel, self.chat_id, self.req_id, self.interval = channel, chat_id, req_id, interval
        self.id = new_id("stream")
        self.started = time.monotonic()
        self._text = ""
        self._last_sent = 0.0
        self._sent_any = False
        self._frozen: int | None = None  # length of the text already frozen by an early finish
        self._later: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _send(self, finish: bool) -> None:
        async with self._lock:
            body = {"msgtype": "stream", "stream": {"id": self.id, "finish": finish, "content": self._text}}
            self._last_sent = time.monotonic()
            self._sent_any = True
            await self.channel.request("aibot_respond_msg", body, req_id=self.req_id)

    async def _send_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._later = None
        await self._send(False)

    async def update(self, text: str) -> None:
        self._text = text
        if self._frozen is not None:
            return
        if time.monotonic() - self.started > STREAM_ROLLOVER_SECONDS:
            self._cancel_later()
            await self._send(True)
            self._frozen = len(text)
            return
        wait = self.interval - (time.monotonic() - self._last_sent)
        if wait <= 0:
            self._cancel_later()
            await self._send(False)
        elif self._later is None:
            self._later = asyncio.create_task(self._send_later(wait))

    def _cancel_later(self) -> None:
        if self._later is not None:
            self._later.cancel()
            self._later = None

    async def finish(self, text: str) -> None:
        self._cancel_later()
        self._text = text
        if self._frozen is None:
            if text.strip() or self._sent_any:
                await self._send(True)
            return
        rest = text[self._frozen:].strip()
        if rest:
            await self.channel.send_text(self.chat_id, rest)


# ---- the channel ---------------------------------------------------------------------


class WeComChannel:
    name = "wecom"
    limits = ChannelLimits(max_text_chars=4000, supports_buttons=True)  # send_text splits again by bytes

    def __init__(
        self,
        bot_id: str,
        secret: str,
        allowed_users: set[str],
        allowed_chats: set[str] | None = None,
        ws_url: str = DEFAULT_WS_URL,
        stream_interval: float = 2.0,
        heartbeat: float = HEARTBEAT_SECONDS,
    ):
        self.bot_id, self._secret, self.ws_url = bot_id, secret, ws_url
        self.allowed_users = allowed_users
        self.allowed_chats = allowed_chats or set()
        self.stream_interval, self.heartbeat = stream_interval, heartbeat
        self._hub = None
        self._ws = None
        self._runner: asyncio.Task | None = None
        self._connected: asyncio.Future | None = None  # the first subscribe's outcome, for start()
        self._waiting: dict[str, list[asyncio.Future]] = {}  # req_id -> answers we wait for
        self._req_ids: OrderedDict[str, str] = OrderedDict()  # message id -> its callback's req_id
        self._chat_types: dict[str, int] = {}  # chat_id -> SINGLE / GROUP
        self._cards: dict[str, tuple[str, ApprovalRequest]] = {}  # open cards: task_id -> (chat_id, request)
        self._tasks: set[asyncio.Task] = set()
        self._stopped = self._kicked = False

    @classmethod
    def from_env(cls) -> "WeComChannel":
        bot_id, secret = os.getenv("WECOM_BOT_ID", ""), os.getenv("WECOM_BOT_SECRET", "")
        if not (bot_id and secret):
            raise ConfigError("WeCom needs WECOM_BOT_ID and WECOM_BOT_SECRET in .env")
        return cls(
            bot_id, secret,
            allowed_users=_ids(os.getenv("WECOM_ALLOWED_USERS", "")),
            allowed_chats=_ids(os.getenv("WECOM_ALLOWED_CHATS", "")),
            ws_url=os.getenv("WECOM_WS_URL", DEFAULT_WS_URL),
            stream_interval=float(os.getenv("WECOM_STREAM_INTERVAL", "2")),
        )

    # ---- lifecycle ----------------------------------------------------------

    async def start(self, hub) -> None:
        self._hub = hub
        self._connected = asyncio.get_running_loop().create_future()
        self._runner = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(asyncio.shield(self._connected), START_TIMEOUT)
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stopped = True
        for task in list(self._tasks):
            task.cancel()
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)

    def routes(self):
        return None  # long connection: no webhook

    async def _run(self) -> None:
        """Connect, subscribe, read frames; reconnect with backoff until stopped or replaced."""
        backoff = 1
        while not self._stopped:
            try:
                # WeCom keeps the connection alive with its own JSON "ping" command; the library's
                # protocol-level pings broke the connection every 20-40 s in the first real test.
                async with ws_connect(self.ws_url, ping_interval=None) as ws:
                    await self._subscribe(ws)
                    backoff = 1
                    self._ws = ws
                    if not self._connected.done():
                        self._connected.set_result(True)
                    log.info("WeCom connected (bot %s)", self.bot_id)
                    heartbeat = asyncio.create_task(self._heartbeat())
                    try:
                        async for raw in ws:
                            self._dispatch(raw)
                    finally:
                        heartbeat.cancel()
                        self._ws = None
                        self._fail_waiting("WeCom connection closed")
            except AuthError as exc:
                log.error("WeCom: %s", exc)
                if not self._connected.done():
                    self._connected.set_exception(RuntimeError(str(exc)))
                return
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException, asyncio.TimeoutError, RuntimeError) as exc:
                if not self._connected.done():
                    self._connected.set_exception(RuntimeError(f"cannot connect to {self.ws_url}: {exc}"))
                    return
                log.warning("WeCom connection lost (%s); reconnecting in %d s", exc, backoff)
            if self._kicked or self._stopped:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _subscribe(self, ws) -> None:
        req_id = new_id("aibot_subscribe")
        # The secret is in this frame: never log it.
        await ws.send(json.dumps({"cmd": "aibot_subscribe", "headers": {"req_id": req_id},
                                  "body": {"bot_id": self.bot_id, "secret": self._secret}}))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), REQUEST_TIMEOUT))
            if (frame.get("headers") or {}).get("req_id") == req_id:
                break
        if frame.get("errcode", 0) != 0:
            raise AuthError(f"subscribe refused: {frame.get('errcode')} {frame.get('errmsg')} "
                            "(check WECOM_BOT_ID and WECOM_BOT_SECRET)")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat)
            try:
                await self.request("ping", None)
            except Exception as exc:  # noqa: BLE001 - no answer: drop the connection, _run reconnects
                log.warning("WeCom heartbeat failed (%r)", exc)
                if self._ws is not None:
                    await self._ws.close()
                return

    # ---- frames ---------------------------------------------------------------

    async def request(self, cmd: str, body: dict | None, req_id: str | None = None) -> dict:
        """Send a frame and wait for WeCom's answer (matched by req_id)."""
        ws = self._ws
        if ws is None:
            raise RuntimeError("WeCom is not connected")
        req_id = req_id or new_id(cmd)
        frame = {"cmd": cmd, "headers": {"req_id": req_id}}
        if body is not None:
            frame["body"] = body
        answer = asyncio.get_running_loop().create_future()
        self._waiting.setdefault(req_id, []).append(answer)
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
            result = await asyncio.wait_for(answer, REQUEST_TIMEOUT)
        finally:
            waiting = self._waiting.get(req_id, [])
            if answer in waiting:
                waiting.remove(answer)
            if not waiting:
                self._waiting.pop(req_id, None)
        if result.get("errcode", 0) != 0:
            raise RuntimeError(f"WeCom {cmd} failed: {result.get('errcode')} {result.get('errmsg')}")
        return result

    def _fail_waiting(self, reason: str) -> None:
        for answers in self._waiting.values():
            for answer in answers:
                if not answer.done():
                    answer.set_exception(RuntimeError(reason))
                    answer.exception()  # its waiter may already be cancelled (heartbeat): don't warn
        self._waiting.clear()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("WeCom: ignored a frame that is not JSON")
            return
        cmd, req_id = frame.get("cmd"), (frame.get("headers") or {}).get("req_id", "")
        body = frame.get("body") or {}
        if cmd == "aibot_msg_callback":
            msg = parse_message(body)
            if msg is None:
                return
            self._req_ids[msg.message_id] = req_id  # for the streamed reply
            if len(self._req_ids) > REMEMBERED:
                self._req_ids.popitem(last=False)
            self._chat_types[msg.chat_id] = GROUP if msg.is_group else SINGLE
            self._spawn(self._hub.on_message(msg))
        elif cmd == "aibot_event_callback":
            kind = (body.get("event") or {}).get("eventtype")
            if kind == "template_card_event":
                self._spawn(self._on_click(body, req_id))
            elif kind == "disconnected_event":
                # Another agent-demo-server connected with this bot. Don't fight over it.
                log.error("WeCom: another connection took over this bot (is a second agent-demo-server "
                          "running?). Not reconnecting.")
                self._kicked = True
        else:  # an answer to one of our frames
            answers = self._waiting.get(req_id)
            if answers:
                answer = answers.pop(0)
                if not answer.done():
                    answer.set_result(frame)

    async def _on_click(self, body: dict, req_id: str) -> None:
        parsed = parse_click(body, {task: request.id for task, (_, request) in self._cards.items()})
        if parsed is None:
            return
        task_id, press = parsed
        _, request = self._cards[task_id]
        notice = await self._hub.on_button(press)
        accepted = notice == ("Approved." if press.approved else "Denied.")
        if press.user_id not in self.allowed_users:
            return  # strangers get no answer
        update = {"response_type": "update_template_card"}
        if accepted:
            self._cards.pop(task_id, None)  # before any await: close_approval then has nothing to do
            update["template_card"] = closed_card(task_id, request, OUTCOMES["approved" if press.approved else "denied"])
        else:  # only the clicker's card shows the notice; the owner's buttons stay
            update["template_card"] = closed_card(task_id, request, notice)
            update["userids"] = [press.user_id]
        try:
            await self.request("aibot_respond_update_msg", update, req_id=req_id)  # within 5 s of the click
        except Exception:  # noqa: BLE001
            log.exception("WeCom: updating the approval card failed")

    # ---- sending ------------------------------------------------------------------

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> None:
        for part in split_bytes(text, MARKDOWN_BYTES):
            await self.request("aibot_send_msg", {
                "chatid": chat_id,
                "chat_type": self._chat_types.get(chat_id, SINGLE),  # unknown: a DM (scheduled task reports)
                "msgtype": "markdown",
                "markdown": {"content": part},
            })

    async def send_approval(self, chat_id: str, request: ApprovalRequest, reply_to: str | None) -> str:
        await self.send_text(chat_id, approval_markdown(request))
        task_id = new_id("ap")
        self._cards[task_id] = (chat_id, request)
        try:
            await self.request("aibot_send_msg", {
                "chatid": chat_id,
                "chat_type": self._chat_types.get(chat_id, SINGLE),
                "msgtype": "template_card",
                "template_card": approval_card(task_id, request),
            })
        except Exception:
            self._cards.pop(task_id, None)
            raise
        return task_id

    async def close_approval(self, chat_id: str, handle: str, outcome: str) -> None:
        card = self._cards.pop(handle, None)
        if card is None:
            return  # the click already updated the card
        # No click to answer (timeout, or a typed yes/no), so the card cannot be updated any more.
        await self.send_text(chat_id, f"{OUTCOMES.get(outcome, outcome)} · `{card[1].name}`")

    async def open_stream(self, chat_id: str, message_id: str) -> WeComStream | None:
        req_id = self._req_ids.get(message_id)
        if not req_id or self._ws is None:
            return None
        return WeComStream(self, chat_id, req_id, self.stream_interval)
