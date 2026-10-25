"""Turn the model's Markdown into something IM apps display well. Pure functions."""

from __future__ import annotations

import html
import json
import re
import unicodedata

_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def shorten(text: str, limit: int) -> str:
    """One line, at most `limit` characters."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_args(args: dict, limit: int = 80) -> str:
    """`{"query": "from:github.com", "limit": 3}` -> `query="from:github.com", limit=3`."""
    return shorten(", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items()), limit)


def _width(text: str) -> int:
    """Display width: Chinese characters take two columns in a monospace font."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip().replace("**", "") for cell in line.split("|")]


def _table_to_code(lines: list[str]) -> str:
    rows = [_cells(line) for line in lines if not _TABLE_SEPARATOR.match(line)]
    columns = max(len(r) for r in rows)
    rows = [r + [""] * (columns - len(r)) for r in rows]
    widths = [max(_width(r[i]) for r in rows) for i in range(columns)]

    def fmt(row: list[str]) -> str:
        return "  ".join(cell + " " * (widths[i] - _width(cell)) for i, cell in enumerate(row)).rstrip()

    out = [fmt(rows[0]), "  ".join("-" * w for w in widths)] + [fmt(r) for r in rows[1:]]
    return "```\n" + "\n".join(out) + "\n```"


def tables_to_code(text: str) -> str:
    """Markdown tables become aligned monospace blocks (IM apps rarely render tables)."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        is_table = (
            lines[i].lstrip().startswith("|")
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR.match(lines[i + 1])
        )
        if not is_table:
            out.append(lines[i])
            i += 1
            continue
        block = []
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            block.append(lines[i])
            i += 1
        out.append(_table_to_code(block))
    return "\n".join(out)


def split_text(text: str, limit: int) -> list[str]:
    """Split at paragraph breaks (then lines, then hard cuts) so each part fits `limit`."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts, current = [], ""
    for piece in _pieces(text, limit):
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
        else:
            parts.append(current)
            current = piece
    if current:
        parts.append(current)
    return parts


def _pieces(text: str, limit: int) -> list[str]:
    pieces = []
    for paragraph in text.split("\n\n"):
        if len(paragraph) <= limit:
            pieces.append(paragraph)
            continue
        for line in paragraph.split("\n"):
            pieces += _cut_line(line, limit)
    return [p for p in pieces if p.strip()]


_BREAKS = " ，。！？；、,.!?;"


def _cut_line(line: str, limit: int) -> list[str]:
    """Cut an over-long line at the last space or punctuation before `limit` (hard cut if none)."""
    parts = []
    while len(line) > limit:
        cut = max(line.rfind(ch, limit // 2, limit) for ch in _BREAKS)
        cut = cut + 1 if cut > 0 else limit
        parts.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    return parts + [line]


def to_chat(text: str, limit: int) -> list[str]:
    """The full pipeline for an assistant answer: tables -> code blocks, then split."""
    return split_text(tables_to_code(text), limit)


# ---- WhatsApp dialect ----------------------------------------------------------

_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_ITALIC_STAR = re.compile(r"(?<![*\w])\*(?![*\s])(.+?)(?<![*\s])\*(?![*\w])")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_STRIKE = re.compile(r"~~(.+?)~~")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _link(m: re.Match) -> str:
    text, url = m.group(1), m.group(2)
    return url if text == url else f"{text} ({url})"


def _whatsapp_prose(text: str) -> str:
    text = _ITALIC_STAR.sub(r"_\1_", text)  # before bold: WhatsApp's bold is a single *
    text = _BOLD.sub(lambda m: f"*{m.group(1) or m.group(2)}*", text)
    text = _HEADING.sub(lambda m: "*" + m.group(1).replace("*", "") + "*", text)  # no headings: bold line
    text = _STRIKE.sub(r"~\1~", text)
    return _LINK.sub(_link, text)


def to_whatsapp(text: str) -> str:
    """Markdown -> WhatsApp markup (*bold*, _italic_, ~strike~); code is left as it is."""
    out, last = [], 0
    for m in _CODE.finditer(text):
        out.append(_whatsapp_prose(text[last : m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_whatsapp_prose(text[last:]))
    return "".join(out)


# ---- Telegram dialect (HTML) ----------------------------------------------------


def _telegram_prose(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_STAR.sub(r"<i>\1</i>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    return _HEADING.sub(lambda m: "<b>" + re.sub(r"</?b>", "", m.group(1)) + "</b>", text)


def to_telegram_html(text: str) -> str:
    """Markdown -> Telegram HTML (parse_mode=HTML): <b>, <i>, <s>, <code>, <pre>, <a>; the rest escaped."""
    out, last = [], 0
    for m in _CODE.finditer(text):
        out.append(_telegram_prose(text[last : m.start()]))
        code = m.group(0)
        if code.startswith("```"):
            body = code[3:-3]
            first, newline, rest = body.partition("\n")
            if newline and re.fullmatch(r"[\w+-]*", first.strip()):  # ```python … ``` : drop the language
                body = rest
            out.append(f"<pre>{html.escape(body.strip(chr(10)), quote=False)}</pre>")
        else:
            out.append(f"<code>{html.escape(code[1:-1], quote=False)}</code>")
        last = m.end()
    out.append(_telegram_prose(text[last:]))
    return "".join(out)
