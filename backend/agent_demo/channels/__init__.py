"""IM channels: Feishu, WhatsApp and Telegram. See design/im_integration.md, whatsapp_ and telegram_integration.md.

Enable with e.g. IM_CHANNELS=feishu,telegram in .env. Adding a platform = one module with a Channel
class (see base.py) + one line in CHANNELS.
"""

from __future__ import annotations

import importlib
import os

from ..config import ConfigError
from .base import Channel

# name -> "module:Class"; imported only when enabled (the Feishu SDK is large).
CHANNELS = {
    "feishu": "agent_demo.channels.feishu:FeishuChannel",
    "whatsapp": "agent_demo.channels.whatsapp:WhatsAppChannel",
    "telegram": "agent_demo.channels.telegram:TelegramChannel",
}


def enabled_channels() -> list[str]:
    return [n.strip().lower() for n in os.getenv("IM_CHANNELS", "").split(",") if n.strip()]


def build_channels(names: list[str]) -> list[Channel]:
    channels = []
    for name in names:
        if name not in CHANNELS:
            raise ConfigError(f"Unknown IM channel '{name}' in IM_CHANNELS. Choose from: {', '.join(CHANNELS)}")
        module, cls = CHANNELS[name].split(":")
        channels.append(getattr(importlib.import_module(module), cls).from_env())
    return channels
