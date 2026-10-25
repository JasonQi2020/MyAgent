"""What every IM channel (Feishu now, WhatsApp later) exchanges with the ChatHub.

A channel only translates: platform events -> IncomingMessage / ButtonPress, and
send_text / send_approval / close_approval -> platform API calls. Everything else
(security checks, sessions, the agent, approvals) lives in hub.py, shared by all channels.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from ..events import ApprovalRequest

if TYPE_CHECKING:
    from fastapi import APIRouter

    from .hub import ChatHub


@dataclass
class IncomingMessage:
    channel: str  # "feishu"
    chat_id: str  # the DM or group chat
    user_id: str  # the sender (Feishu open_id)
    message_id: str  # for dedupe and threaded replies
    text: str  # @mention placeholders already removed
    is_group: bool = False
    mentioned_bot: bool = True  # always True in DMs
    unsupported: bool = False  # e.g. an image: the hub answers "text only"


@dataclass
class ButtonPress:
    channel: str
    chat_id: str
    user_id: str
    call_id: str  # the tool call id from ApprovalRequest
    approved: bool


@dataclass
class GroupEvent:
    """The bot was added to or removed from a group chat."""

    channel: str
    chat_id: str
    operator_id: str  # who added/removed the bot ("" if unknown)
    kind: str  # "bot_added" | "bot_removed"
    chat_name: str = ""
    external: bool = False  # the group includes people from other organisations


@dataclass
class HistoryLine:
    """One message of a group's recent history (design §12)."""

    message_id: str
    time: datetime
    sender: str  # display name, or a short id if the name is unknown
    text: str
    from_bot: bool = False


@dataclass
class ChannelLimits:
    max_text_chars: int  # longer answers are split into several messages
    supports_buttons: bool


class Channel(Protocol):
    name: str
    limits: ChannelLimits
    allowed_users: set[str]  # empty = setup mode: nobody is answered, senders are logged
    allowed_chats: set[str]  # group chats where the bot answers @mentions; empty = DMs only

    async def start(self, hub: ChatHub) -> None: ...  # connect; then call hub.on_message / hub.on_button
    async def stop(self) -> None: ...
    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> None: ...
    async def send_approval(self, chat_id: str, request: ApprovalRequest, reply_to: str | None) -> str: ...
    async def close_approval(self, chat_id: str, handle: str, outcome: str) -> None: ...  # approved|denied|expired
    def routes(self) -> APIRouter | None: ...  # webhook routes (WhatsApp); None for long connections

    # Optional (design §12): channels that can read a group's history define
    #   async def history(self, chat_id: str, limit: int, since: datetime | None) -> list[HistoryLine]
    # returning up to `limit` of the most recent messages, oldest first.
