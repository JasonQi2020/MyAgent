"""Telegram channel: Bot API over HTTPS (httpx), long polling. See design/telegram_integration.md.

    Telegram ◀── getUpdates (long polling, outbound only) ── TelegramChannel ── parse_update ──▶ ChatHub
    ChatHub ──▶ send_text / send_approval / close_approval ──▶ sendMessage / editMessageText

No public URL is needed. The bot token is part of every API URL, so URLs are never logged
(our errors leave them out, and a filter blanks the token in httpx's own request log).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import ConfigError
from ..events import ApprovalRequest
from . import render
from .base import ButtonPress, ChannelLimits, GroupEvent, HistoryLine, IncomingMessage

log = logging.getLogger("agent_demo.im.telegram")

DEFAULT_API = "https://api.telegram.org"
POLL_SECONDS = 50  # long-poll timeout given to Telegram
GROUP_BUFFER = 200  # messages remembered per group (Telegram has no history API for bots)
MAX_ARGS_CHARS = 3500
CALLBACK_LIMIT = 64  # bytes, Telegram's limit for callback_data
UPDATES = ["message", "callback_query", "my_chat_member"]
OUTCOMES = {"approved": "✅ Approved", "denied": "❌ Denied",
            "expired": "⌛ Expired (no answer within 5 minutes, so it was not run)"}


class TelegramError(Exception):
    def __init__(self, code: Any, description: str):
        super().__init__(f"Telegram API error {code}: {description}")
        self.code, self.description = code, description


class _RedactToken(logging.Filter):
    """Blank the bot token wherever httpx logs a request URL."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self.token:
            record.msg = str(record.msg).replace(self.token, "<token>")
            if record.args:
                record.args = tuple(str(a).replace(self.token, "<token>") if isinstance(a, (str, httpx.URL)) else a
                                    for a in (record.args if isinstance(record.args, tuple) else (record.args,)))
        return True


def _ids(text: str) -> set[str]:
    return {part.strip() for part in text.split(",") if part.strip()}


def _name(user: dict) -> str:
    full = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return full or (f"@{user['username']}" if user.get("username") else f"user-{str(user.get('id', ''))[-4:]}")


# ---- parsing (pure) ---------------------------------------------------------------------


def _strip_mention(text: str, username: str) -> str:
    if username:
        lowered, mention, out, i = text.lower(), "@" + username.lower(), [], 0
        while (j := lowered.find(mention, i)) != -1:
            out.append(text[i:j])
            i = j + len(mention)
        text = "".join(out) + text[i:]
    return " ".join(text.split(" ")).strip()


def parse_update(update: dict, bot_id: int, bot_username: str) -> IncomingMessage | ButtonPress | GroupEvent | None:
    """One getUpdates item -> what the hub understands, or None for things we ignore."""
    if (query := update.get("callback_query")) is not None:
        data, chat = str(query.get("data") or ""), ((query.get("message") or {}).get("chat") or {})
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "a" or parts[2] not in ("0", "1") or "id" not in chat:
            return None
        return ButtonPress("telegram", str(chat["id"]), str((query.get("from") or {}).get("id", "")), parts[1],
                           parts[2] == "1")

    if (member := update.get("my_chat_member")) is not None:
        chat = member.get("chat") or {}
        if chat.get("type") not in ("group", "supergroup"):
            return None
        new = (member.get("new_chat_member") or {}).get("status")
        old = (member.get("old_chat_member") or {}).get("status")
        operator = str((member.get("from") or {}).get("id", ""))
        if new in ("member", "administrator") and old in ("left", "kicked", None):
            return GroupEvent("telegram", str(chat["id"]), operator, "bot_added", chat.get("title") or "")
        if new in ("left", "kicked"):
            return GroupEvent("telegram", str(chat["id"]), operator, "bot_removed", chat.get("title") or "")
        return None

    message = update.get("message")
    if not message:
        return None  # edited messages, channel posts, …
    sender, chat = message.get("from") or {}, message.get("chat") or {}
    if not sender or sender.get("is_bot") or chat.get("type") not in ("private", "group", "supergroup"):
        return None
    is_group = chat["type"] != "private"
    text = message.get("text")
    base = dict(channel="telegram", chat_id=str(chat["id"]), user_id=str(sender["id"]),
                message_id=str(message.get("message_id", "")), is_group=is_group)
    if text is None:
        return IncomingMessage(text="", unsupported=True, mentioned_bot=not is_group, **base)

    mentioned = not is_group
    lowered = text.lower()
    if is_group and bot_username:
        mentioned = ("@" + bot_username.lower()) in lowered  # "@bot …" and "/cmd@bot"
    for entity in message.get("entities") or []:
        if entity.get("type") == "text_mention" and (entity.get("user") or {}).get("id") == bot_id:
            mentioned = True
    if ((message.get("reply_to_message") or {}).get("from") or {}).get("id") == bot_id:
        mentioned = True
    return IncomingMessage(text=_strip_mention(text, bot_username), mentioned_bot=mentioned, **base)


# ---- messages we send (pure) --------------------------------------------------------------


def approval_text(request: ApprovalRequest, header: str = "<b>Approval needed</b>") -> str:
    args = json.dumps(request.arguments, ensure_ascii=False, indent=2)
    if len(args) > MAX_ARGS_CHARS:
        args = args[:MAX_ARGS_CHARS] + f"\n… ({len(args) - MAX_ARGS_CHARS} more characters not shown)"
    return (f"{header}\nThe agent wants to run <code>{render.html.escape(request.name)}</code> with:\n"
            f"<pre>{render.html.escape(args, quote=False)}</pre>")


def approval_keyboard(call_id: str) -> dict:
    buttons = [("Approve", f"a:{call_id}:1"), ("Deny", f"a:{call_id}:0")]
    for _, data in buttons:
        if len(data.encode()) > CALLBACK_LIMIT:
            raise ValueError(f"tool call id too long for a Telegram button ({len(call_id)} characters)")
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}


# ---- the channel ----------------------------------------------------------------------------


class TelegramChannel:
    name = "telegram"
    limits = ChannelLimits(max_text_chars=3800, supports_buttons=True)  # 4096 minus room for HTML tags

    def __init__(self, token: str, allowed_users: set[str], allowed_chats: set[str] | None = None,
                 api_base: str = DEFAULT_API, client: httpx.AsyncClient | None = None):
        self.allowed_users, self.allowed_chats = allowed_users, allowed_chats or set()
        self._token, self._api = token, f"{api_base.rstrip('/')}/bot{token}"
        self._own_client = client is None
        # httpx honours HTTPS_PROXY, for networks where Telegram is not reachable directly.
        self.client = client or httpx.AsyncClient(timeout=POLL_SECONDS + 15)
        self.bot_id, self.bot_username, self.bot_name = 0, "", "the bot"
        self._buffers: dict[str, deque[HistoryLine]] = defaultdict(lambda: deque(maxlen=GROUP_BUFFER))
        self._approvals: dict[str, ApprovalRequest] = {}  # message id -> request, until closed
        self._tasks: set[asyncio.Task] = set()
        self._poller: asyncio.Task | None = None
        self._hub = None
        self._filter = _RedactToken(token)
        logging.getLogger("httpx").addFilter(self._filter)

    @classmethod
    def from_env(cls) -> "TelegramChannel":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("Telegram needs TELEGRAM_BOT_TOKEN in .env (from @BotFather)")
        return cls(token, allowed_users=_ids(os.getenv("TELEGRAM_ALLOWED_USERS", "")),
                   allowed_chats=_ids(os.getenv("TELEGRAM_ALLOWED_CHATS", "")),
                   api_base=os.getenv("TELEGRAM_API_BASE", DEFAULT_API))

    # ---- the API ---------------------------------------------------------------------

    async def call(self, method: str, http_timeout: float | None = None, **params: Any) -> Any:
        # (Not "timeout": that is a getUpdates parameter.)
        try:
            response = await self.client.post(f"{self._api}/{method}", json=params,
                                              timeout=http_timeout or self.client.timeout)
        except httpx.HTTPError as exc:  # its message contains the URL, i.e. the token: leave it out
            raise TelegramError("network", type(exc).__name__) from None
        try:
            data = response.json()
        except ValueError:
            raise TelegramError(response.status_code, "not a JSON answer") from None
        if not data.get("ok"):
            raise TelegramError(data.get("error_code", response.status_code), data.get("description", ""))
        return data.get("result")

    # ---- lifecycle ----------------------------------------------------------------------

    async def start(self, hub) -> None:
        self._hub = hub
        me = await self.call("getMe")
        self.bot_id, self.bot_username, self.bot_name = me["id"], me.get("username", ""), me.get("first_name", "bot")
        await self.call("deleteWebhook")  # polling does not work while a webhook is set
        if not me.get("can_read_all_group_messages"):
            log.info("Telegram: privacy mode is on, so group context only holds messages addressed to the bot "
                     "(BotFather /setprivacy → Disable to change that)")
        self._poller = asyncio.create_task(self._poll())
        log.info("Telegram: polling as @%s", self.bot_username)

    async def stop(self) -> None:
        if self._poller:
            self._poller.cancel()
        for task in self._tasks:
            task.cancel()
        if self._own_client:
            await self.client.aclose()
        logging.getLogger("httpx").removeFilter(self._filter)

    def routes(self):
        return None  # long polling: no webhook

    async def _poll(self) -> None:
        offset, delay = 0, 1.0
        while True:
            try:
                updates = await self._get_updates(offset)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except TelegramError as exc:
                if exc.code == 409:
                    log.warning("Telegram: another program is polling this bot (409); run only one server")
                else:
                    log.warning("Telegram: %s; retrying in %.0f s", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            for update in updates or []:
                offset = max(offset, update.get("update_id", 0) + 1)
                self._spawn(self.handle(update))
            # Telegram normally holds an empty poll open for POLL_SECONDS. If an answer ever comes
            # back at once (a proxy, a fake API), never spin: give other tasks a turn, and pause briefly.
            await asyncio.sleep(0 if updates else 0.5)

    async def _get_updates(self, offset: int) -> list[dict]:
        # Telegram holds the request open up to POLL_SECONDS; the HTTP timeout must be longer.
        return await self.call("getUpdates", http_timeout=POLL_SECONDS + 15,
                               offset=offset, timeout=POLL_SECONDS, allowed_updates=UPDATES)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle(self, update: dict) -> None:
        """One update: remember group messages (for context), then hand it to the hub."""
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") in ("group", "supergroup") and message.get("text") is not None:
            sender = message.get("from") or {}
            self._buffers[str(chat["id"])].append(HistoryLine(
                str(message.get("message_id", "")),
                datetime.fromtimestamp(message.get("date", 0), tz=timezone.utc),
                _name(sender), _strip_mention(message["text"], self.bot_username), bool(sender.get("is_bot")),
            ))
        item = parse_update(update, self.bot_id, self.bot_username)
        try:
            if isinstance(item, IncomingMessage):
                await self._hub.on_message(item)
            elif isinstance(item, ButtonPress):
                notice = await self._hub.on_button(item)
                # Strangers get an empty answer (it only stops the client's spinner).
                text = notice if item.user_id in self.allowed_users else ""
                await self.call("answerCallbackQuery", callback_query_id=update["callback_query"]["id"],
                                text=text[:200])
            elif isinstance(item, GroupEvent):
                await self._hub.on_group_event(item)
        except Exception:  # noqa: BLE001 - one bad update must not stop the others
            log.exception("Telegram: handling an update failed")

    # ---- sending ------------------------------------------------------------------------------

    def _remember_own(self, chat_id: str, message_id: str, text: str) -> None:
        if chat_id in self._buffers or chat_id.startswith("-"):  # groups have negative ids
            self._buffers[chat_id].append(HistoryLine(message_id, datetime.now(timezone.utc), self.bot_name, text, True))

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> None:
        params: dict[str, Any] = {"chat_id": int(chat_id), "link_preview_options": {"is_disabled": True}}
        if reply_to:
            params["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        try:
            sent = await self.call("sendMessage", text=render.to_telegram_html(text), parse_mode="HTML", **params)
        except TelegramError as exc:
            if "parse entities" not in exc.description:
                raise
            sent = await self.call("sendMessage", text=text, **params)  # Telegram rejected the HTML: plain text
        self._remember_own(chat_id, str(sent.get("message_id", "")), text)

    async def send_approval(self, chat_id: str, request: ApprovalRequest, reply_to: str | None) -> str:
        params: dict[str, Any] = {"chat_id": int(chat_id), "text": approval_text(request), "parse_mode": "HTML",
                                  "reply_markup": approval_keyboard(request.id)}
        if reply_to:
            params["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        sent = await self.call("sendMessage", **params)
        handle = str(sent["message_id"])
        self._approvals[handle] = request
        return handle

    async def close_approval(self, chat_id: str, handle: str, outcome: str) -> None:
        request = self._approvals.pop(handle, None)
        if request is None:
            return
        # Editing without reply_markup removes the buttons, so an old card cannot be pressed again.
        await self.call("editMessageText", chat_id=int(chat_id), message_id=int(handle), parse_mode="HTML",
                        text=approval_text(request, f"<b>{OUTCOMES.get(outcome, outcome)}</b>"))

    # ---- group context (design §1.3) ------------------------------------------------------------

    async def history(self, chat_id: str, limit: int, since: datetime | None = None) -> list[HistoryLine]:
        lines = [line for line in self._buffers.get(str(chat_id), ()) if since is None or line.time >= since]
        return lines[-limit:]
