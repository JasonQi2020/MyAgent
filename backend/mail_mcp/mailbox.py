"""IMAP (read) + SMTP (send) logic, independent of MCP so it can be tested with fakes.

Written for Chinese providers such as Sina Mail:
  - folder names use IMAP "modified UTF-7" (RFC 3501), e.g. "&XfJT0ZAB-" = 已发送
  - headers and bodies are often GB2312/GBK, sometimes not even RFC 2047 encoded
  - server-side SEARCH may not support non-ASCII text, so text filters also run locally
  - some servers (e.g. 163) require the IMAP ID command after login
Every read uses BODY.PEEK, so reading never marks a message as read.
"""

from __future__ import annotations

import base64
import imaplib
import os
import re
import smtplib
import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from html.parser import HTMLParser

SCAN_LIMIT = 300  # newest messages scanned when a text filter must run locally
FETCH_BATCH = 50
MAX_BODY_CHARS = 20_000
MAX_LIMIT = 50
SPECIAL_USE = ("Sent", "Drafts", "Trash", "Junk", "Archive")


class MailError(Exception):
    """An error message meant for the model (and the user)."""


@dataclass
class MailConfig:
    user: str
    password: str
    imap_host: str
    imap_port: int = 993
    imap_security: str = "ssl"  # ssl | starttls
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"  # ssl | starttls
    from_name: str = ""
    save_sent: bool = True  # SMTP does not keep a copy; APPEND one to the Sent folder

    @classmethod
    def from_env(cls) -> "MailConfig":
        missing = [n for n in ("EMAIL_USER", "EMAIL_PASSWORD", "IMAP_HOST") if not os.getenv(n)]
        if missing:
            raise MailError(f"Mail server is not configured: set {', '.join(missing)}.")
        return cls(
            user=os.environ["EMAIL_USER"],
            password=os.environ["EMAIL_PASSWORD"],
            imap_host=os.environ["IMAP_HOST"],
            imap_port=int(os.getenv("IMAP_PORT", "993")),
            imap_security=os.getenv("IMAP_SECURITY", "ssl").lower(),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=int(os.getenv("SMTP_PORT", "465")),
            smtp_security=os.getenv("SMTP_SECURITY", "ssl").lower(),
            from_name=os.getenv("EMAIL_FROM_NAME", ""),
            save_sent=os.getenv("SAVE_SENT", "true").lower() in ("1", "true", "yes"),
        )


# ---- decoding helpers --------------------------------------------------------

_CHARSET_ALIASES = {"gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "cp936": "gb18030"}


def _codec(charset: str | None) -> str:
    """GB2312/GBK labels are decoded as GB18030, a superset, so rarer characters survive."""
    name = (charset or "utf-8").strip().strip('"').lower()
    return _CHARSET_ALIASES.get(name, name)


def decode_bytes(data: bytes, charset: str | None = None) -> str:
    for codec in (_codec(charset), "utf-8", "gb18030"):
        try:
            return data.decode(codec)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def decode_mime_header(value: str | None) -> str:
    """Decode RFC 2047 words (=?gb2312?B?...?=) into readable text."""
    if not value:
        return ""
    parts = []
    for chunk, charset in decode_header(value):
        parts.append(decode_bytes(chunk, charset) if isinstance(chunk, bytes) else chunk)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def decode_folder(name: str) -> str:
    """IMAP modified UTF-7 -> text: '&XfJT0ZAB-' -> '已发送', '&-' -> '&'."""

    def repl(match: re.Match) -> str:
        chunk = match.group(1)
        if not chunk:
            return "&"
        b64 = chunk.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        return base64.b64decode(b64).decode("utf-16-be")

    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, name)


def encode_folder(name: str) -> str:
    """Text -> IMAP modified UTF-7 (the reverse of decode_folder)."""
    out, buffer = [], []

    def flush() -> None:
        if buffer:
            raw = "".join(buffer).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            buffer.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buffer.append(ch)
    flush()
    return "".join(out)


class _TextExtractor(HTMLParser):
    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    return decode_bytes(payload, part.get_content_charset())


def extract_body(msg: Message) -> tuple[str, list[str]]:
    """Return (text body, attachment names). Prefers text/plain; falls back to HTML."""
    plain, html, attachments = [], [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = (part.get("Content-Disposition") or "").lower()
        if filename or disposition.startswith("attachment"):
            attachments.append(decode_mime_header(filename) or "(unnamed)")
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(_part_text(part))
        elif ctype == "text/html":
            html.append(_part_text(part))
    if plain:
        body = "\n\n".join(p.strip() for p in plain)
    elif html:
        body = html_to_text("\n".join(html))
    else:
        body = "(no text content)"
    return body, attachments


def _format_date(value: str | None) -> str:
    try:
        return parsedate_to_datetime(value).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, IndexError):
        return decode_mime_header(value)


def _imap_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d-%b-%Y")
    except ValueError:
        raise MailError(f"Invalid date {value!r}; use YYYY-MM-DD.")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_LINE = re.compile(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)')


@dataclass
class Folder:
    raw: str  # name as the server knows it (modified UTF-7)
    name: str  # decoded, readable name
    special: str  # "Sent", "Trash", ... or ""


# ---- the mailbox -------------------------------------------------------------


class Mailbox:
    def __init__(
        self,
        config: MailConfig,
        imap_factory: Callable[[MailConfig], imaplib.IMAP4] | None = None,
        smtp_factory: Callable[[MailConfig], smtplib.SMTP] | None = None,
    ):
        self.config = config
        self._imap_factory = imap_factory or _connect_imap
        self._smtp_factory = smtp_factory or _connect_smtp

    @contextmanager
    def _imap(self) -> Iterator[imaplib.IMAP4]:
        try:
            conn = self._imap_factory(self.config)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailError(f"Cannot connect to {self.config.imap_host}: {exc}")
        try:
            try:
                conn.login(self.config.user, self.config.password)
            except imaplib.IMAP4.error as exc:
                raise MailError(f"IMAP login failed for {self.config.user}: {exc}")
            if "ID" in conn.capabilities:  # some servers (163) refuse SELECT without it
                conn.xatom("ID", '("name" "agent-demo" "version" "0.1")')
            yield conn
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001 - best effort
                pass

    # -- folders --

    def _folders(self, conn: imaplib.IMAP4) -> list[Folder]:
        typ, data = conn.list()
        if typ != "OK":
            raise MailError("Could not list folders.")
        folders = []
        for line in data:
            if not line:
                continue
            text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
            match = _LIST_LINE.match(text)
            if not match:
                continue
            raw = match.group("name").strip()
            if raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1]
            flags = match.group("flags")
            special = next((s for s in SPECIAL_USE if f"\\{s}" in flags), "")
            folders.append(Folder(raw, decode_folder(raw), special))
        return folders

    def _resolve(self, conn: imaplib.IMAP4, folder: str) -> str:
        """Accept 'INBOX', a readable name ('已发送') or a role ('Sent'); return the server name."""
        wanted = (folder or "INBOX").strip()
        if wanted.upper() == "INBOX":
            return "INBOX"
        for f in self._folders(conn):
            if wanted in (f.name, f.raw) or wanted.lower() == f.special.lower():
                return f.raw
        raise MailError(f"Folder {wanted!r} not found. Use list_folders to see the folder names.")

    def _select(self, conn: imaplib.IMAP4, folder: str, readonly: bool = True) -> str:
        raw = self._resolve(conn, folder)
        typ, data = conn.select(_quote(raw), readonly=readonly)
        if typ != "OK":
            raise MailError(f"Cannot open folder {folder!r}: {data}")
        return raw

    def list_folders(self) -> str:
        with self._imap() as conn:
            folders = self._folders(conn)
        lines = []
        for f in folders:
            label = f.name + (f" ({f.special})" if f.special else "")
            lines.append(label)
        return "\n".join(lines) or "(no folders)"

    # -- search --

    def search(
        self,
        folder: str = "INBOX",
        sender: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        unread_only: bool = False,
        limit: int = 10,
    ) -> str:
        limit = max(1, min(int(limit), MAX_LIMIT))
        criteria: list[str] = []
        if since:
            criteria += ["SINCE", _imap_date(since)]
        if unread_only:
            criteria.append("UNSEEN")
        # Non-ASCII text cannot be sent in a plain SEARCH; those filters run locally only.
        if sender and sender.isascii():
            criteria += ["FROM", _quote(sender)]
        if subject and subject.isascii():
            criteria += ["SUBJECT", _quote(subject)]
        local_filter = bool(sender or subject)

        with self._imap() as conn:
            self._select(conn, folder)
            typ, data = conn.uid("SEARCH", *(criteria or ["ALL"]))
            if typ != "OK":
                raise MailError(f"Search failed: {data}")
            uids = (data[0] or b"").split()[::-1]  # newest first
            candidates = uids[:SCAN_LIMIT] if local_filter else uids[:limit]

            results, scanned = [], 0
            for start in range(0, len(candidates), FETCH_BATCH):
                batch = candidates[start : start + FETCH_BATCH]
                for summary in self._fetch_summaries(conn, batch):
                    scanned += 1
                    if sender and sender.lower() not in summary["from"].lower():
                        continue
                    if subject and subject.lower() not in summary["subject"].lower():
                        continue
                    results.append(summary)
                if len(results) >= limit:
                    break

        results = results[:limit]
        if not results:
            return f"No messages found in {folder} (scanned {scanned} of {len(uids)} matching)."
        lines = [
            f"id: {r['id']} | {r['date']} | from: {r['from']} | subject: {r['subject']}"
            + (" | unread" if r["unread"] else "")
            for r in results
        ]
        footer = f"({len(results)} shown, folder {folder}, newest first"
        if local_filter and len(uids) > SCAN_LIMIT:
            footer += f"; text filters searched the newest {SCAN_LIMIT} of {len(uids)}"
        return "\n".join(lines) + "\n" + footer + ")"

    def _fetch_summaries(self, conn: imaplib.IMAP4, uids: list[bytes]) -> list[dict]:
        if not uids:
            return []
        typ, data = conn.uid(
            "FETCH", b",".join(uids).decode(), "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
        )
        if typ != "OK":
            raise MailError(f"Fetch failed: {data}")
        summaries = {}
        for item in data:
            if not isinstance(item, tuple):
                continue
            meta = item[0].decode(errors="replace")
            uid = re.search(r"UID (\d+)", meta)
            if not uid:
                continue
            headers = message_from_bytes(item[1])
            summaries[uid.group(1)] = {
                "id": uid.group(1),
                "from": decode_mime_header(_raw_header(item[1], "From") or headers.get("From")),
                "subject": decode_mime_header(_raw_header(item[1], "Subject") or headers.get("Subject"))
                or "(no subject)",
                "date": _format_date(headers.get("Date")),
                "unread": "\\Seen" not in meta,
            }
        # Keep the requested (newest-first) order.
        return [summaries[u.decode()] for u in uids if u.decode() in summaries]

    # -- read --

    def read(self, message_id: str, folder: str = "INBOX") -> str:
        if not str(message_id).isdigit():
            raise MailError(f"Invalid message id {message_id!r}; use an id from search_emails.")
        with self._imap() as conn:
            self._select(conn, folder)
            typ, data = conn.uid("FETCH", str(message_id), "(BODY.PEEK[])")
        raw = next((item[1] for item in data or [] if isinstance(item, tuple)), None)
        if typ != "OK" or raw is None:
            raise MailError(f"Message {message_id} not found in {folder}.")
        msg = message_from_bytes(raw)
        body, attachments = extract_body(msg)
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + f"\n[... body truncated at {MAX_BODY_CHARS} characters]"
        lines = [
            f"From: {decode_mime_header(_raw_header(raw, 'From') or msg.get('From'))}",
            f"To: {decode_mime_header(_raw_header(raw, 'To') or msg.get('To'))}",
        ]
        if msg.get("Cc"):
            lines.append(f"Cc: {decode_mime_header(msg.get('Cc'))}")
        lines += [
            f"Date: {_format_date(msg.get('Date'))}",
            f"Subject: {decode_mime_header(_raw_header(raw, 'Subject') or msg.get('Subject'))}",
        ]
        if attachments:
            lines.append(f"Attachments: {', '.join(attachments)}")
        return "\n".join(lines) + "\n\n" + body

    # -- write (the agent asks the user before any of these) --

    def mark_read(self, message_id: str, folder: str = "INBOX") -> str:
        if not str(message_id).isdigit():
            raise MailError(f"Invalid message id {message_id!r}.")
        with self._imap() as conn:
            self._select(conn, folder, readonly=False)
            typ, data = conn.uid("STORE", str(message_id), "+FLAGS", "(\\Seen)")
        if typ != "OK" or not data or data[0] is None:
            raise MailError(f"Message {message_id} not found in {folder}.")
        return f"Marked message {message_id} in {folder} as read."

    def send(self, to: list[str], subject: str, body: str, cc: list[str] | None = None) -> str:
        if not self.config.smtp_host:
            raise MailError("Sending is not configured: set SMTP_HOST.")
        recipients = [addr for _, addr in getaddresses([*to, *(cc or [])]) if "@" in addr]
        if not to or not recipients:
            raise MailError("Give at least one valid recipient address.")

        msg = EmailMessage()
        sender = self.config.user
        msg["From"] = f"{self.config.from_name} <{sender}>" if self.config.from_name else sender
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
        msg.set_content(body)

        try:
            smtp = self._smtp_factory(self.config)
            try:
                smtp.login(self.config.user, self.config.password)
                smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
            finally:
                try:
                    smtp.quit()
                except Exception:  # noqa: BLE001
                    pass
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(f"SMTP login failed for {sender}: {exc.smtp_code} {exc.smtp_error!r}")
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"Sending failed: {exc}")

        result = f"Sent to {', '.join(recipients)} (Message-ID {msg['Message-ID']})."
        if self.config.save_sent:
            result += " " + self._save_to_sent(msg)
        return result

    def _save_to_sent(self, msg: EmailMessage) -> str:
        try:
            with self._imap() as conn:
                sent = self._resolve(conn, "Sent")
                conn.append(_quote(sent), "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            return "A copy was saved to the Sent folder."
        except MailError as exc:
            return f"(Could not save a copy to Sent: {exc})"


def _raw_header(raw: bytes, name: str) -> str | None:
    """Some Chinese mailers put raw GBK bytes in headers without RFC 2047 encoding.

    The email package cannot decode those, so read the header line from the raw bytes.
    """
    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    match = re.search(rb"^" + name.encode() + rb":[ \t]*(.*(?:\r?\n[ \t].*)*)", head, re.M | re.I)
    if not match:
        return None
    value = match.group(1)
    if value.isascii():
        return None  # plain or RFC 2047: the normal path handles it
    return decode_bytes(re.sub(rb"\r?\n[ \t]+", b" ", value).strip())


def _connect_imap(config: MailConfig) -> imaplib.IMAP4:
    if config.imap_security == "starttls":
        conn = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=30)
        conn.starttls(ssl.create_default_context())
        return conn
    return imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=30)


def _connect_smtp(config: MailConfig) -> smtplib.SMTP:
    if config.smtp_security == "starttls":
        smtp = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
        smtp.starttls(context=ssl.create_default_context())
        return smtp
    return smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30, context=ssl.create_default_context())
