"""The IMAP/SMTP mail MCP server, tested against in-memory fakes (no network).

The sample messages copy what Chinese mailers really send: GB2312/GBK RFC 2047 headers,
raw (unencoded) GBK header bytes, HTML-only bodies and modified-UTF-7 folder names.
"""

import base64
import imaplib
import re
import smtplib
import sys
from email import message_from_bytes
from email.header import Header

import pytest

from agent_demo.mcp_client import MCPManager
from agent_demo.tools import ToolRegistry
from mail_mcp.mailbox import (
    MailConfig,
    MailError,
    Mailbox,
    decode_folder,
    decode_mime_header,
    encode_folder,
    html_to_text,
)

SENT = encode_folder("已发送")  # "&XfJT0ZAB-"


def gbk_plain(uid_subject: str, body: str, sender: str, date: str) -> bytes:
    subject = Header(uid_subject, "gb2312").encode()
    name = Header(sender, "gb2312").encode()
    payload = base64.b64encode(body.encode("gb2312")).decode()
    return (
        f"From: {name} <zhang@sina.com>\r\nTo: jason@sina.com\r\nSubject: {subject}\r\n"
        f"Date: {date}\r\nContent-Type: text/plain; charset=gb2312\r\n"
        f"Content-Transfer-Encoding: base64\r\n\r\n{payload}\r\n"
    ).encode()


HTML_ONLY = (
    b"From: GitHub <noreply@github.com>\r\nSubject: [repo] PR merged\r\n"
    b"Date: Thu, 23 Oct 2026 09:00:00 +0800\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    b"<html><head><style>p{color:red}</style></head><body><p>Hello&nbsp;<b>Jason</b></p>"
    b"<script>alert(1)</script><p>Line two</p></body></html>\r\n"
)

RAW_GBK_HEADER = (
    b"From: shop@example.cn\r\nSubject: " + "订单已发货".encode("gbk") + b"\r\n"
    b"Date: Fri, 24 Oct 2026 10:00:00 +0800\r\nMIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
    b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nYour order shipped.\r\n"
    b"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\""
    + Header("发票.pdf", "utf-8").encode().encode()
    + b'"\r\n\r\nPDFDATA\r\n--B--\r\n'
)


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for Mailbox, with imaplib's return shapes."""

    capabilities = ("IMAP4REV1", "ID")

    def __init__(self, password="code"):
        self.password = password
        self.folders = {
            "INBOX": {
                1: {"flags": {"\\Seen"}, "raw": gbk_plain("测试邮件", "你好，世界", "张三", "Wed, 22 Oct 2026 08:00:00 +0800")},
                2: {"flags": set(), "raw": HTML_ONLY},
                3: {"flags": set(), "raw": RAW_GBK_HEADER},
            },
            SENT: {},
        }
        self.selected = None
        self.readonly = None
        self.commands = []

    def login(self, user, password):
        self.commands.append("LOGIN")
        if password != self.password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b"LOGIN completed"]

    def xatom(self, name, *args):
        self.commands.append(name)
        return "OK", [None]

    def list(self):
        return "OK", [
            b'() "/" "INBOX"',
            b'(\\Sent) "/" "' + SENT.encode() + b'"',
            b'(\\Trash) "/" "&XfJSIJZk-"',
            b'() "/" "' + encode_folder("订阅邮件").encode() + b'"',
        ]

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected, self.readonly = name, readonly
        return "OK", [str(len(self.folders[name])).encode()]

    def uid(self, command, *args):
        box = self.folders[self.selected]
        if command == "SEARCH":
            uids = sorted(box)
            if "UNSEEN" in args:
                uids = [u for u in uids if "\\Seen" not in box[u]["flags"]]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            out = []
            for uid in map(int, args[0].split(",")):
                if uid not in box:
                    continue
                msg = box[uid]
                flags = " ".join(sorted(msg["flags"]))
                raw = msg["raw"]
                if "HEADER.FIELDS" in args[1]:
                    head = raw.split(b"\r\n\r\n", 1)[0]
                    raw = b"\r\n".join(
                        line for line in head.split(b"\r\n") if re.match(rb"(?i)(from|subject|date):", line)
                    ) + b"\r\n\r\n"
                out += [(f"{uid} (UID {uid} FLAGS ({flags}) BODY[] {{{len(raw)}}}".encode(), raw), b")"]
            return "OK", out
        if command == "STORE":
            assert not self.readonly, "STORE on a read-only SELECT"
            uid = int(args[0])
            if uid not in box:
                return "OK", [None]
            box[uid]["flags"].add("\\Seen")
            return "OK", [f"{uid} (UID {uid} FLAGS (\\Seen))".encode()]
        raise AssertionError(command)

    def append(self, mailbox, flags, date, message):
        box = self.folders[mailbox.strip('"')]
        box[max(box, default=0) + 1] = {"flags": {"\\Seen"}, "raw": message}
        return "OK", [b"APPEND completed"]

    def logout(self):
        self.commands.append("LOGOUT")


class FakeSMTP:
    def __init__(self, password="code"):
        self.password = password
        self.sent = []

    def login(self, user, password):
        if password != self.password:
            raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

    def send_message(self, msg, from_addr, to_addrs):
        self.sent.append((msg, from_addr, to_addrs))

    def quit(self):
        pass


@pytest.fixture
def fakes():
    imap, smtp = FakeIMAP(), FakeSMTP()
    config = MailConfig(user="jason@sina.com", password="code", imap_host="imap.test", smtp_host="smtp.test")
    mailbox = Mailbox(config, imap_factory=lambda c: imap, smtp_factory=lambda c: smtp)
    return mailbox, imap, smtp


# ---- decoding ---------------------------------------------------------------


def test_folder_name_round_trip():
    assert decode_folder("&XfJT0ZAB-") == "已发送"
    for name in ["已发送", "INBOX", "A&B", "Mixed 中文 name"]:
        assert decode_folder(encode_folder(name)) == name
    assert encode_folder("A&B") == "A&-B"


def test_decode_mime_header_gb2312():
    assert decode_mime_header(Header("测试邮件", "gb2312").encode()) == "测试邮件"
    assert decode_mime_header(None) == ""


def test_html_to_text_drops_scripts_and_styles():
    text = html_to_text("<style>x{}</style><p>Hi&nbsp;<b>there</b></p><script>bad()</script><p>bye</p>")
    assert text == "Hi there\n\nbye"


# ---- reading ----------------------------------------------------------------


def test_list_folders_decoded_with_roles(fakes):
    mailbox, imap, _ = fakes
    assert mailbox.list_folders().splitlines() == ["INBOX", "已发送 (Sent)", "已删除 (Trash)", "订阅邮件"]
    assert imap.commands[:2] == ["LOGIN", "ID"]  # ID right after login (needed by 163)
    assert imap.commands[-1] == "LOGOUT"


def test_search_newest_first_with_decoded_headers(fakes):
    mailbox, *_ = fakes
    rows = mailbox.search().splitlines()
    assert rows[0].startswith("id: 3 | 2026-10-24 10:00 | from: shop@example.cn | subject: 订单已发货 | unread")
    assert rows[1].startswith("id: 2 |") and "[repo] PR merged" in rows[1]
    assert rows[2] == "id: 1 | 2026-10-22 08:00 | from: 张三 <zhang@sina.com> | subject: 测试邮件"
    assert rows[-1] == "(3 shown, folder INBOX, newest first)"


def test_search_filters(fakes):
    mailbox, *_ = fakes
    assert "id: 1 " in mailbox.search(subject="测试")  # Chinese: filtered locally
    assert "No messages found" in mailbox.search(subject="不存在")
    only = mailbox.search(sender="github").splitlines()
    assert len(only) == 2 and only[0].startswith("id: 2 ")
    unread = mailbox.search(unread_only=True)
    assert "id: 1 " not in unread and "id: 2 " in unread
    assert len(mailbox.search(limit=1).splitlines()) == 2  # one row + footer


def test_search_errors(fakes):
    mailbox, *_ = fakes
    with pytest.raises(MailError, match="YYYY-MM-DD"):
        mailbox.search(since="23/10/2026")
    with pytest.raises(MailError, match="not found"):
        mailbox.search(folder="Nope")


def test_folders_by_role_or_chinese_name(fakes):
    mailbox, imap, _ = fakes
    for name in ("Sent", "sent", "已发送", SENT):
        mailbox.search(folder=name)
        assert imap.selected == SENT


def test_read_gbk_plain_text(fakes):
    mailbox, imap, _ = fakes
    text = mailbox.read("1")
    assert "From: 张三 <zhang@sina.com>" in text
    assert "Subject: 测试邮件" in text
    assert text.endswith("你好，世界")
    assert imap.readonly is True


def test_read_html_and_attachments(fakes):
    mailbox, imap, _ = fakes
    html = mailbox.read("2")
    assert html.endswith("Hello Jason\n\nLine two") and "alert" not in html
    mixed = mailbox.read("3")
    assert "Subject: 订单已发货" in mixed  # raw GBK bytes in the header
    assert "Attachments: 发票.pdf" in mixed and "PDFDATA" not in mixed
    assert imap.folders["INBOX"][3]["flags"] == set()  # reading does not mark as read


def test_read_errors(fakes):
    mailbox, *_ = fakes
    with pytest.raises(MailError, match="Invalid message id"):
        mailbox.read("1; DELETE")
    with pytest.raises(MailError, match="not found"):
        mailbox.read("99")


def test_bad_login(fakes):
    mailbox, imap, _ = fakes
    imap.password = "other"
    with pytest.raises(MailError, match="IMAP login failed"):
        mailbox.list_folders()


# ---- writing ----------------------------------------------------------------


def test_mark_read(fakes):
    mailbox, imap, _ = fakes
    assert mailbox.mark_read("2") == "Marked message 2 in INBOX as read."
    assert "\\Seen" in imap.folders["INBOX"][2]["flags"] and imap.readonly is False
    with pytest.raises(MailError, match="not found"):
        mailbox.mark_read("99")


def test_send_and_save_to_sent(fakes):
    mailbox, imap, smtp = fakes
    result = mailbox.send(["Boss <boss@example.com>"], "周报", "本周完成：MCP 服务器", cc=["team@example.com"])
    assert result.startswith("Sent to boss@example.com, team@example.com")
    assert "saved to the Sent folder" in result

    msg, from_addr, to_addrs = smtp.sent[0]
    assert from_addr == "jason@sina.com" and to_addrs == ["boss@example.com", "team@example.com"]
    assert msg["Subject"] == "周报" and msg["Cc"] == "team@example.com"
    assert msg.get_content().strip() == "本周完成：MCP 服务器"

    saved = message_from_bytes(imap.folders[SENT][1]["raw"])
    assert decode_mime_header(saved["Subject"]) == "周报"


def test_send_errors(fakes):
    mailbox, _, smtp = fakes
    with pytest.raises(MailError, match="valid recipient"):
        mailbox.send(["not-an-address"], "s", "b")
    smtp.password = "other"
    with pytest.raises(MailError, match="SMTP login failed"):
        mailbox.send(["a@b.com"], "s", "b")
    mailbox.config.smtp_host = ""
    with pytest.raises(MailError, match="SMTP_HOST"):
        mailbox.send(["a@b.com"], "s", "b")


# ---- the MCP server process -------------------------------------------------


async def test_mcp_server_tools_over_stdio(tmp_path):
    """Starts `python -m mail_mcp.imap_server` like the agent does; no account configured."""
    servers = {"mail": {"command": sys.executable, "args": ["-m", "mail_mcp.imap_server"],
                        "env": {"EMAIL_USER": ""}, "auto_approve": ["list_folders", "search_emails", "read_email"]}}
    registry = ToolRegistry()
    async with MCPManager(servers, log_dir=tmp_path) as mcp:
        assert mcp.warnings == []
        await mcp.register_tools(registry)
        names = {t.name: t for t in registry.all()}
        assert set(names) == {f"mail__{n}" for n in
                              ("list_folders", "search_emails", "read_email", "send_email", "mark_read")}
        assert names["mail__send_email"].needs_approval and not names["mail__read_email"].needs_approval
        assert "Chinese is fine" in str(names["mail__search_emails"].parameters)

        result = await names["mail__list_folders"].handler({})
        assert "not configured" in result and "EMAIL_USER" in result
