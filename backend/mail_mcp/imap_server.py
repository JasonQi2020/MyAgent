"""MCP server for an IMAP/SMTP mailbox. Run over stdio:  python -m mail_mcp.imap_server

Configured by environment variables (the agent fills them from agent.properties and .env):
  EMAIL_USER, EMAIL_PASSWORD (an authorization code, not the login password),
  IMAP_HOST, IMAP_PORT=993, IMAP_SECURITY=ssl|starttls,
  SMTP_HOST, SMTP_PORT=465, SMTP_SECURITY=ssl|starttls, EMAIL_FROM_NAME, SAVE_SENT=true

Read tools: list_folders, search_emails, read_email. Write tools: send_email, mark_read.
Which tools need the user's approval is decided by the agent (mcp_servers.json), not here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .mailbox import Mailbox, MailConfig, MailError

server = MCPServer("mail")

_mailbox: Mailbox | None = None


def mailbox() -> Mailbox:
    # Created on first use, so tools/list works even before the account is configured.
    global _mailbox
    if _mailbox is None:
        _mailbox = Mailbox(MailConfig.from_env())
    return _mailbox


def run(action: Callable[[Mailbox], str]) -> str:
    """Turn expected failures into ToolError, so the model sees the message (not just "Error executing tool")."""
    try:
        return action(mailbox())
    except MailError as exc:
        raise ToolError(str(exc)) from exc


FolderArg = Annotated[
    str, Field(description="Folder name from list_folders, or a role: INBOX, Sent, Drafts, Trash, Junk")
]


@server.tool()
def list_folders() -> str:
    """List the mailbox folders (readable names, with roles such as Sent or Trash)."""
    return run(lambda m: m.list_folders())


@server.tool()
def search_emails(
    folder: FolderArg = "INBOX",
    sender: Annotated[str | None, Field(description="Part of the sender's name or address")] = None,
    subject: Annotated[str | None, Field(description="Part of the subject (Chinese is fine)")] = None,
    since: Annotated[str | None, Field(description="Only messages on or after this date, YYYY-MM-DD")] = None,
    unread_only: bool = False,
    limit: Annotated[int, Field(description="Max results (default 10, max 50)")] = 10,
) -> str:
    """Search emails, newest first. Returns id, date, sender and subject; use read_email for the content."""
    return run(lambda m: m.search(folder, sender, subject, since, unread_only, limit))


@server.tool()
def read_email(
    id: Annotated[str, Field(description="Message id from search_emails")],
    folder: FolderArg = "INBOX",
) -> str:
    """Read one email: headers, text body and attachment names. Does not mark it as read."""
    return run(lambda m: m.read(id, folder))


@server.tool()
def send_email(
    to: Annotated[list[str], Field(description="Recipient email addresses")],
    subject: str,
    body: Annotated[str, Field(description="Plain-text body")],
    cc: Annotated[list[str] | None, Field(description="CC addresses")] = None,
) -> str:
    """Send a plain-text email from the user's own address (a copy is saved to Sent)."""
    return run(lambda m: m.send(to, subject, body, cc))


@server.tool()
def mark_read(id: Annotated[str, Field(description="Message id from search_emails")], folder: FolderArg = "INBOX") -> str:
    """Mark one email as read."""
    return run(lambda m: m.mark_read(id, folder))


if __name__ == "__main__":
    server.run("stdio")
