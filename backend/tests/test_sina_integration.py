"""The real Sina mailbox, read-only: log in, list folders, search one header.

Needs SINA_AUTH_CODE in agent-demo/.env and mail.sina.* in agent.properties.
Run with:  uv run pytest -m integration   (skipped in a normal full run)
"""

import os

import pytest
from dotenv import dotenv_values

from agent_demo.config import PROJECT_ROOT, load_properties
from mail_mcp.mailbox import Mailbox, MailConfig

_env = {**dotenv_values(PROJECT_ROOT / ".env"), **os.environ}
_props = load_properties(PROJECT_ROOT / "agent.properties")
CODE = (_env.get("SINA_AUTH_CODE") or "").strip()
ADDRESS = _props.get("mail.sina.address", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (CODE and ADDRESS), reason="SINA_AUTH_CODE or mail.sina.address not set"),
]


@pytest.fixture
def mailbox():
    return Mailbox(
        MailConfig(
            user=ADDRESS,
            password=CODE,
            imap_host=_props.get("mail.sina.imap_host", "imap.sina.com"),
            imap_port=int(_props.get("mail.sina.imap_port", "993")),
            smtp_host=_props.get("mail.sina.smtp_host", "smtp.sina.com"),
            smtp_port=int(_props.get("mail.sina.smtp_port", "465")),
        )
    )


def test_login_and_folders(mailbox):
    folders = mailbox.list_folders().splitlines()
    assert "INBOX" in folders
    assert any(f.endswith("(Sent)") for f in folders)


def test_search_newest_header(mailbox):
    rows = [r for r in mailbox.search(limit=1).splitlines() if r.startswith("id: ")]
    assert len(rows) == 1 and "�" not in rows[0]  # decoded cleanly
