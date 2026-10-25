"""Who handles a message: a bot command, Claude Code, or the assistant.

The rules of design/claude_code_delegation.md §1, shared by the CLI, the web server and the
IM hub (approval answers are checked by each UI before calling route()):

    1. commands at the very start of the message (/help, /new, /model, /strongmodel, /tasks …, /claude …)
    2. /claude <prompt>            -> Claude Code, this message only
    3. Claude mode on (/claude on) -> Claude Code
    4. everything else             -> the assistant (Qwen or GPT)
"""

from __future__ import annotations

from dataclasses import dataclass

from .claude_code import parse_command

COMMANDS = {"/help", "help", "/tools", "/new", "/model", "/weakmodel", "/strongmodel", "/enable", "/disable"}
ARG_COMMANDS = {"/tasks"}  # commands that take arguments, e.g. "/tasks run 3"


@dataclass
class Route:
    kind: str  # "command" | "claude_command" | "claude" | "assistant"
    name: str = ""  # the command ("/model") or the /claude sub-command ("on", "new", …)
    text: str = ""  # the prompt, or the sub-command's argument


def route(text: str, claude_mode: bool) -> Route:
    stripped = text.strip()
    parsed = parse_command(stripped)
    if parsed is not None:
        sub, arg = parsed
        if sub or not arg:
            return Route("claude_command", sub, arg)
        return Route("claude", "one-off", arg)
    if stripped.lower() in COMMANDS:
        return Route("command", stripped.lower())
    head, _, rest = stripped.partition(" ")
    if head.lower() in ARG_COMMANDS:
        return Route("command", head.lower(), rest.strip())
    if claude_mode:
        return Route("claude", "sticky", stripped)
    return Route("assistant", text=stripped)
