"""Group chats enabled at runtime, saved to a JSON file so they survive restarts.

A group is enabled when an allowlisted user adds the bot to it, or sends `@bot /enable`
there; it is disabled by `/disable` or by removing the bot. Groups listed in .env
(e.g. FEISHU_ALLOWED_CHATS) are separate and are not stored here.

    {"feishu": {"oc_…": {"enabled_by": "ou_…", "via": "bot_added", "name": "Team", "at": "…"}}}
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger("agent_demo.im")


class GroupStore:
    def __init__(self, path: Path | None = None):
        self.path = path  # None: in memory only (tests)
        self._groups: dict[str, dict[str, dict]] = {}
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._groups = {c: dict(g) for c, g in data.items() if isinstance(g, dict)}
            except (OSError, ValueError, AttributeError) as exc:
                log.warning("Could not read %s (%s); starting with no runtime-enabled groups", path, exc)

    def is_enabled(self, channel: str, chat_id: str) -> bool:
        return chat_id in self._groups.get(channel, {})

    def enable(self, channel: str, chat_id: str, by: str, via: str, name: str = "") -> bool:
        """Returns False if the group was already enabled."""
        if self.is_enabled(channel, chat_id):
            return False
        self._groups.setdefault(channel, {})[chat_id] = {
            "enabled_by": by, "via": via, "name": name, "at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save()
        return True

    def disable(self, channel: str, chat_id: str) -> bool:
        """Returns False if the group was not enabled here."""
        if not self.is_enabled(channel, chat_id):
            return False
        del self._groups[channel][chat_id]
        self._save()
        return True

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write a temp file and rename it, so a crash never leaves a half-written file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._groups, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
