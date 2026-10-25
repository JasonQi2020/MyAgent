"""/claude: send work to Claude Code through the Claude Agent SDK.

See design/claude_code_delegation.md. One ClaudeCodeSession per conversation holds the mode
(one-off or sticky), the working folder and Claude Code's session id, so follow-ups continue
where it stopped. Every action Claude Code wants to take passes through a PreToolUse hook:

    allowed folder?   Read/Glob/Grep, Edit/Write  -> allow        outside -> deny
    Bash, WebFetch    claude.approval=auto (default): allow, except dangerous commands -> ask
                      ask: only read-only commands (ls, cat, git diff, ...) run; the rest -> ask
                      full: allow everything
    anything else     -> deny (MCP tools, sub-agents)

(A hook, not only can_use_tool: Claude Code approves "safe" commands by itself without
calling can_use_tool, so a policy there could be bypassed. The hook sees every call.)

Settings and sign-in are re-read from agent.properties / .env on every message: no restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, _resolve_path, load_properties
from .events import ApprovalRequest
from .mcp_client import wrap_untrusted
from .models import fresh_env

READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
ASK_TOOLS = {"WebFetch", "WebSearch"}
HARMLESS_TOOLS = {
    "TodoWrite",  # Claude Code's own task list
    "ToolSearch",  # loads deferred tool schemas (our scheduler's MCP tools); it cannot run anything itself
}
SAFE_COMMANDS = {"ls", "pwd", "cat", "head", "tail", "wc", "which", "tree", "du", "file", "stat"}
SAFE_GIT = {"status", "diff", "log", "show", "branch"}
_SHELL_SYNTAX = re.compile(r"[;&|<>`$\\\n]|\(\s*\)")
APPROVAL_LEVELS = ("ask", "auto", "full")
# Still asked in claude.approval=auto (anywhere in the command, also after ; && |).
_DANGEROUS = [re.compile(p) for p in (
    r"\b(sudo|su|doas)\b",
    r"\brm\s+(-[^\s]*[rRf]|--recursive|--force)",  # rm -r, rm -rf, rm -f
    r"\bgit\s+(push|reset\s+--hard|clean\s+-[^\s]*f|branch\s+-D|checkout\s+--\s|restore\s)",
    r"\b(curl|wget)\b[^\n]*\|\s*(ba|z|da)?sh\b",  # download | sh
    r"\b(mkfs|fdisk|parted|shutdown|reboot|halt|poweroff)\b",
    r"\bdd\s+[^\n]*\bof=",
    r"\bch(mod|own)\s+(-[^\s]*R|--recursive)",
    r"\bkill(all)?\s+-9\b",
    r":\s*\(\s*\)\s*\{",  # fork bomb
    r">\s*/dev/(sd|nvme|hd)",
    r"\b(pip|uv\s+pip|npm|pnpm|yarn)\s+(install|uninstall|remove)\b[^\n]*(-g\b|--global\b|--system\b)",
)]
SUBCOMMANDS = {"on", "off", "new", "folder", "status", "stop"}
NOTE_CHARS = 1500

# Added to the assistant's system prompt in conversations where /claude is available.
ASSISTANT_PROMPT = """
- Notes marked [Note: ...] report work done by Claude Code at the user's request (via /claude).
  Use them to answer follow-up questions about that work. You cannot run Claude Code yourself;
  if the user wants work done on files or code, tell them to use /claude <request>.
"""
HELP = (
    "/claude <request> (one message to Claude Code), /claude on | off (all messages to Claude Code), "
    "/claude new, /claude folder <path>, /claude status, /claude stop"
)


# ---- settings (fresh on every message) ----------------------------------------


@dataclass
class ClaudeSettings:
    enabled: bool
    folders: list[Path]
    max_turns: int
    timeout_minutes: float
    max_budget_usd: float
    model: str
    owners: dict[str, str]  # channel -> owner's user id, e.g. {"feishu": "ou_…"}
    env: dict[str, str] = field(repr=False)
    auth: str  # "subscription" | "api key" | "none"
    approval: str = "auto"  # claude.approval: ask | auto | full
    sandbox: bool = True  # claude.sandbox: run Claude Code inside bwrap (design §11)


def load_claude_settings() -> ClaudeSettings:
    env = fresh_env()
    props = load_properties(_resolve_path(env.get("AGENT_PROPERTIES", "agent.properties")))
    folders = [
        Path(os.path.expanduser(p.strip())).resolve()
        # Default: the folder that contains this project (not "~", which depends on who runs the server).
        for p in (props.get("claude.folders") or str(PROJECT_ROOT.parent)).split(",")
        if p.strip()
    ]
    token, key = env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip(), env.get("ANTHROPIC_API_KEY", "").strip()
    if token:  # the owner's subscription; an API key would take precedence, so it is blanked
        run_env, auth = {"CLAUDE_CODE_OAUTH_TOKEN": token, "ANTHROPIC_API_KEY": ""}, "subscription"
    elif key:
        run_env, auth = {"ANTHROPIC_API_KEY": key}, "api key"
    else:
        run_env, auth = {}, "none"
    level = (props.get("claude.approval") or "auto").strip().lower()
    approval = level if level in APPROVAL_LEVELS else "ask"  # an unknown value falls back to the safest
    return ClaudeSettings(
        enabled=props.get("claude.enabled", "true").strip().lower() not in ("false", "0", "no", "off"),
        folders=folders,
        max_turns=int(props.get("claude.max_turns", "40") or 40),
        timeout_minutes=float(props.get("claude.timeout_minutes", "20") or 20),
        max_budget_usd=float(props.get("claude.max_budget_usd", "2") or 2),
        model=props.get("claude.model", "").strip(),
        owners={k.removeprefix("claude.owner."): v.strip() for k, v in props.items()
                if k.startswith("claude.owner.") and v.strip()},
        env=run_env,
        auth=auth,
        approval=approval,
        # Anything but an explicit "off" keeps the sandbox on (fail closed).
        sandbox=props.get("claude.sandbox", "on").strip().lower() not in ("off", "false", "0", "no"),
    )


# ---- the permission policy (pure) ------------------------------------------------


def inside(path: Path, folders: list[Path]) -> bool:
    return any(path == f or f in path.parents for f in folders)


def _target(tool_input: dict, cwd: Path) -> Path | None:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if not raw:
        return None
    path = Path(os.path.expanduser(str(raw)))
    return (path if path.is_absolute() else cwd / path).resolve()  # resolves .. and symlinks


def safe_command(command: str, cwd: Path, folders: list[Path]) -> bool:
    """Read-only commands that need no approval: no shell syntax, no paths outside the folders."""
    if not command.strip() or _SHELL_SYNTAX.search(command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if words[0] == "git":
        if len(words) < 2 or words[1] not in SAFE_GIT:
            return False
    elif words[0] not in SAFE_COMMANDS:
        return False
    for word in words[1:]:
        if word.startswith("-"):
            continue
        path = Path(os.path.expanduser(word))
        if not inside((path if path.is_absolute() else cwd / path).resolve(), folders):
            return False
    return True


def dangerous_command(command: str) -> bool:
    """Commands that still need approval in claude.approval=auto: hard or impossible to undo."""
    return any(p.search(command) for p in _DANGEROUS)


def decide(tool: str, tool_input: dict, cwd: Path, folders: list[Path], approval: str = "ask") -> tuple[str, str]:
    """-> ("allow" | "deny" | "ask", reason). `approval`: ask | auto | full (claude.approval)."""
    if tool in READ_TOOLS or tool in WRITE_TOOLS:
        target = _target(tool_input, cwd)
        if target is not None and not inside(target, folders):
            return "deny", f"{target} is outside the allowed folders; only these may be used: " + ", ".join(
                str(f) for f in folders)
        return "allow", ""
    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        if safe_command(command, cwd, folders) or approval == "full":
            return "allow", ""
        if approval == "auto" and not dangerous_command(command):
            return "allow", ""
        return "ask", "dangerous shell command" if approval == "auto" else "shell command"
    if tool in ASK_TOOLS:
        return ("allow", "") if approval in ("auto", "full") else ("ask", "web access")
    if tool in HARMLESS_TOOLS:
        return "allow", ""
    if tool.startswith("mcp__scheduler__"):  # our own in-process scheduler tools (scheduled_tasks.md §4.2)
        return ("ask", "scheduling a task") if tool == "mcp__scheduler__add" else ("allow", "")
    return "deny", f"the tool {tool} is not available in this setup"


def progress_line(tool: str, tool_input: dict, cwd: Path) -> str:
    target = _target(tool_input, cwd)
    shown = str(target.relative_to(cwd)) if target and cwd in target.parents else (str(target) if target else "")
    if tool == "Bash":
        command = " ".join(str(tool_input.get("command", "")).split())
        return f"$ {command[:100]}{'…' if len(command) > 100 else ''}"
    if tool in ("Glob", "Grep"):
        return f"Search {tool_input.get('pattern', '')}" + (f" in {shown}" if shown else "")
    if tool in ("WebFetch", "WebSearch"):
        return f"{tool} {tool_input.get('url') or tool_input.get('query', '')}"
    return f"{tool} {shown}".strip()


# ---- one conversation's Claude Code state ------------------------------------------


@dataclass
class ClaudeResult:
    ok: bool
    text: str
    files_changed: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    turns: int = 0
    seconds: float = 0
    cost_usd: float | None = None


@dataclass
class ClaudeCodeSession:
    mode: bool = False  # sticky: every message goes to Claude Code
    folder: Path | None = None  # None = the first allowed folder
    session_id: str | None = None  # Claude Code's own session, resumed by the next message
    task: asyncio.Task | None = None

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def current_folder(self, settings: ClaudeSettings) -> Path | None:
        return self.folder or (settings.folders[0] if settings.folders else None)

    def stop(self) -> bool:
        if self.busy():
            self.task.cancel()
            return True
        return False

    def reset(self) -> None:
        """/new: stop any run, forget the session, Claude mode off."""
        self.stop()
        self.mode, self.folder, self.session_id = False, None, None

    def status(self, settings: ClaudeSettings) -> str:
        folder = self.current_folder(settings)
        session = "session open" if self.session_id else "no session yet"
        state = "working" if self.busy() else "idle"
        return f"Claude Code: mode {'on' if self.mode else 'off'}, {session} in {folder}, {state}"


Approve = Callable[[ApprovalRequest], Awaitable[bool]]
Progress = Callable[[str], Awaitable[None]]


async def _prompt_stream(text: str):
    # Streaming input: required by the SDK when permission callbacks are used.
    yield {"type": "user", "message": {"role": "user", "content": text}}


def _problem(settings: ClaudeSettings, folder: Path | None) -> str | None:
    if not settings.enabled:
        return "Claude Code is disabled (claude.enabled in agent.properties)."
    if settings.auth == "none":
        return "Claude Code is not signed in: set CLAUDE_CODE_OAUTH_TOKEN (claude setup-token) or ANTHROPIC_API_KEY in .env."
    if folder is None or not settings.folders:
        return "No folder is allowed: set claude.folders in agent.properties."
    if not folder.is_dir():
        return f"The working folder {folder} does not exist."
    if not inside(folder, settings.folders):
        return f"{folder} is not inside the allowed folders (claude.folders)."
    if settings.sandbox:  # fail closed: never run unsandboxed when the sandbox is on
        if not shutil.which("bwrap"):
            return "The sandbox is on (claude.sandbox) but bwrap is not installed: install bubblewrap, or set claude.sandbox=off."
        if claude_binary() is None:
            return "The claude program was not found on PATH."
    return None


# ---- the OS sandbox (design §11) -------------------------------------------------------

SANDBOX_DIR = PROJECT_ROOT / ".claude-sandbox"  # Claude Code's own home + the launcher; git-ignored
SANDBOX_HOME = "/sandbox-home"
SANDBOX_BINARY = "/opt/claude"
# Environment variables passed into the sandbox. Everything else is dropped: the server's
# environment holds every key from .env (DashScope, OpenAI, Feishu, mail...).
ENV_PREFIXES = ("CLAUDE_", "ANTHROPIC_", "LC_")
ENV_NAMES = ("PATH", "LANG", "LANGUAGE", "TERM", "TZ", "NO_COLOR", "FORCE_COLOR")


def claude_binary() -> Path | None:
    found = shutil.which("claude")
    return Path(found).resolve() if found else None  # the real file, so upgrades are picked up


def bwrap_args(folders: list[Path], binary: Path, home: Path) -> list[str]:
    """bwrap command line: only `folders` (read-write), system folders (read-only) and Claude Code's own home."""
    args = ["bwrap", "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc"]
    for name in ("bin", "lib", "lib64", "lib32", "sbin"):
        top = Path("/") / name
        if top.is_symlink():  # merged /usr: /bin -> usr/bin
            args += ["--symlink", os.readlink(top), str(top)]
        elif top.is_dir():
            args += ["--ro-bind", str(top), str(top)]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    resolver = Path("/etc/resolv.conf").resolve()  # e.g. /run/systemd/resolve/stub-resolv.conf
    if resolver.exists() and not str(resolver).startswith(("/etc/", "/usr/")):
        args += ["--ro-bind", str(resolver.parent), str(resolver.parent)]
    args += ["--bind", str(home), SANDBOX_HOME, "--setenv", "HOME", SANDBOX_HOME,
             "--ro-bind", str(binary), SANDBOX_BINARY]
    for folder in folders:
        args += ["--bind", str(folder), str(folder)]  # same path inside, so paths in answers are real
    args += ["--unshare-all", "--share-net", "--die-with-parent", "--new-session"]
    return args


LAUNCHER = '''#!{python}
# Generated by agent_demo.claude_code (design/claude_code_delegation.md §11). Do not edit.
# Starts Claude Code inside the bwrap sandbox with a clean environment.
import os, sys
args = {args!r}
env = [a for k, v in os.environ.items()
       if k.startswith({prefixes!r}) or k in {names!r}
       for a in ("--setenv", k, v)]
# --clearenv first: bwrap applies options in order, and the arguments set HOME.
os.execvp(args[0], [args[0], "--clearenv"] + env + args[1:] + [{binary!r}] + sys.argv[1:])
'''


def write_launcher(folders: list[Path], directory: Path = SANDBOX_DIR) -> Path:
    """The program the SDK starts instead of `claude`. Rebuilt on every run (current folders, binary)."""
    binary = claude_binary()
    home, bindir = directory / "home", directory / "bin"
    home.mkdir(parents=True, exist_ok=True)
    bindir.mkdir(parents=True, exist_ok=True)
    launcher = bindir / "claude"
    tmp = bindir / f".claude.{os.getpid()}.tmp"
    tmp.write_text(LAUNCHER.format(python=sys.executable, args=bwrap_args(folders, binary, home),
                                   prefixes=ENV_PREFIXES, names=ENV_NAMES, binary=SANDBOX_BINARY),
                   encoding="utf-8")
    tmp.chmod(0o700)
    os.replace(tmp, launcher)
    return launcher


async def run_claude(
    session: ClaudeCodeSession,
    prompt: str,
    settings: ClaudeSettings,
    approve: Approve,
    progress: Progress,
    query: Any = None,
    mcp_servers: dict | None = None,
    previews: dict[str, Callable[[dict], dict]] | None = None,
) -> ClaudeResult:
    """Run one message through Claude Code. Never raises for expected problems."""
    folder = session.current_folder(settings)
    if (problem := _problem(settings, folder)) is not None:
        return ClaudeResult(False, problem)

    import claude_agent_sdk as sdk  # imported here: only needed when /claude is used

    query = query or sdk.query
    files: list[str] = []
    commands: list[str] = []
    answers: dict[str, bool] = {}  # the same command is asked once per run

    async def judge(tool: str, tool_input: dict) -> tuple[bool, str]:
        verdict, reason = decide(tool, tool_input, folder, settings.folders, settings.approval)
        line = progress_line(tool, tool_input, folder)
        if verdict == "ask":
            key = tool + json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
            if key not in answers:
                arguments = ({"command": tool_input.get("command", ""), "folder": str(folder)}
                             if tool == "Bash" else {**tool_input, "folder": str(folder)})
                if previews and tool in previews:  # e.g. a schedule's next runs
                    arguments.update(previews[tool](tool_input))
                if "problem" in arguments:  # it cannot work: refuse with the reason instead of asking
                    await progress(f"✗ {line} — {arguments['problem']}")
                    return False, arguments["problem"]
                request = ApprovalRequest(f"cc-{uuid.uuid4().hex[:10]}", f"claude_code__{tool.lower()}", arguments)
                answers[key] = await approve(request)
            verdict, reason = ("allow", "approved") if answers[key] else ("deny", "the user denied this")
        if verdict == "allow":
            if tool in WRITE_TOOLS and (target := _target(tool_input, folder)) is not None:
                name = str(target.relative_to(folder)) if folder in target.parents else str(target)
                if name not in files:
                    files.append(name)
            if tool == "Bash":
                commands.append(str(tool_input.get("command", "")) + (" (approved)" if reason == "approved" else ""))
            await progress(line)
            return True, ""
        await progress(f"✗ {line} — refused: {reason}")
        return False, reason

    async def pre_tool_use(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        allowed, reason = await judge(input_data.get("tool_name", ""), input_data.get("tool_input") or {})
        output = {"hookEventName": "PreToolUse", "permissionDecision": "allow" if allowed else "deny"}
        if not allowed:
            output["permissionDecisionReason"] = f"Refused: {reason}."
        return {"hookSpecificOutput": output}

    async def can_use_tool(tool: str, tool_input: dict, context: Any):
        # Backup only: the hook above already decided every call.
        allowed, reason = await judge(tool, tool_input)
        return sdk.PermissionResultAllow() if allowed else sdk.PermissionResultDeny(message=f"Refused: {reason}.")

    options = sdk.ClaudeAgentOptions(
        # With the sandbox on, the SDK starts our launcher, which runs Claude Code inside bwrap.
        cli_path=str(write_launcher(settings.folders)) if settings.sandbox else None,
        cwd=str(folder),
        env=settings.env,
        mcp_servers=mcp_servers or {},
        resume=session.session_id,
        max_turns=settings.max_turns,
        max_budget_usd=settings.max_budget_usd if settings.auth == "api key" else None,
        model=settings.model or None,
        setting_sources=["project"],  # the folder's CLAUDE.md, not the user's global settings or MCP servers
        hooks={"PreToolUse": [sdk.HookMatcher(matcher=None, hooks=[pre_tool_use])]},
        can_use_tool=can_use_tool,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "You are working for the user through a chat app. Only the allowed folders are "
                      "accessible. Finish with a short summary of what you did.",
        },
    )

    started = time.monotonic()
    result: Any = None
    last_text = ""

    async def consume() -> None:
        nonlocal result, last_text
        async for message in query(prompt=_prompt_stream(prompt), options=options):
            kind = type(message).__name__
            if kind == "SystemMessage" and getattr(message, "subtype", "") == "init":
                session.session_id = (getattr(message, "data", None) or {}).get("session_id") or session.session_id
            elif kind == "AssistantMessage":
                texts = [b.text for b in message.content if type(b).__name__ == "TextBlock" and b.text.strip()]
                last_text = texts[-1] if texts else last_text
            elif kind == "ResultMessage":
                result = message

    try:
        await asyncio.wait_for(consume(), settings.timeout_minutes * 60)
    except asyncio.TimeoutError:
        return ClaudeResult(False, f"Stopped: no answer within {settings.timeout_minutes:g} minutes "
                                   "(claude.timeout_minutes).", files, commands,
                            seconds=time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001 - CLI missing, sign-in rejected, network...
        return ClaudeResult(False, f"Claude Code failed: {exc}", files, commands, seconds=time.monotonic() - started)

    seconds = time.monotonic() - started
    if result is None:
        return ClaudeResult(False, "Claude Code ended without an answer.", files, commands, seconds=seconds)
    session.session_id = getattr(result, "session_id", None) or session.session_id
    cost = getattr(result, "total_cost_usd", None) if settings.auth == "api key" else None
    turns = getattr(result, "num_turns", 0) or 0
    if getattr(result, "is_error", False) or str(getattr(result, "subtype", "")).startswith("error"):
        subtype = getattr(result, "subtype", "")
        detail = getattr(result, "result", None) or "; ".join(map(str, getattr(result, "errors", None) or [])) or subtype
        if subtype == "error_max_turns":
            detail = f"stopped after {turns} turns (claude.max_turns)"
        return ClaudeResult(False, f"Claude Code could not finish: {detail}", files, commands, turns, seconds, cost)
    return ClaudeResult(True, getattr(result, "result", None) or last_text or "(no answer)", files, commands,
                        turns, seconds, cost)


# ---- what the user and the assistant see --------------------------------------------


def _duration(seconds: float) -> str:
    return f"{int(seconds // 60)} min {int(seconds % 60)} s" if seconds >= 60 else f"{seconds:.0f} s"


def format_answer(result: ClaudeResult) -> str:
    if not result.ok:
        return f"🤖 Claude Code — ⚠ {result.text}"
    footer = [f"Files changed: {', '.join(result.files_changed)}" if result.files_changed else "No files changed",
              f"{result.turns} turns", _duration(result.seconds)]
    if result.cost_usd is not None:
        footer.append(f"${result.cost_usd:.2f}")
    return f"🤖 Claude Code\n\n{result.text}\n\n_{' · '.join(footer)}_"


def memory_note(prompt: str, folder: Path | None, result: ClaudeResult) -> str:
    """What the assistant learns about the run (design §3): never the transcript or file contents."""
    answer = result.text if len(result.text) <= NOTE_CHARS else result.text[:NOTE_CHARS] + " …"
    lines = [
        f'[Note: the user asked Claude Code (working in {folder}): "{prompt}".',
        (f"Claude Code finished in {result.turns} turns." if result.ok else "Claude Code did not finish."),
        f"Files changed: {', '.join(result.files_changed) or 'none'}.",
    ]
    if result.commands:
        lines.append(f"Commands run: {'; '.join(result.commands)}.")
    lines.append(f"Its answer (shortened): {answer}]")
    return wrap_untrusted("claude-code", "\n".join(lines))


# ---- /claude commands ------------------------------------------------------------------


def parse_command(text: str) -> tuple[str, str] | None:
    """'/claude on' -> ("on", ""), '/claude fix it' -> ("", "fix it"); None if not /claude."""
    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() != "/claude":
        return None
    rest = rest.strip()
    word, _, arg = rest.partition(" ")
    if word.lower() in SUBCOMMANDS:
        return word.lower(), arg.strip()
    return "", rest


def command_reply(session: ClaudeCodeSession, sub: str, arg: str, settings: ClaudeSettings) -> tuple[str, str | None]:
    """Handle a /claude sub-command. Returns (reply, prompt to run or None)."""
    folder = session.current_folder(settings)
    if sub == "on":
        if (problem := _problem(settings, folder)) is not None:
            return f"⚠ {problem}", None
        session.mode = True
        return (f"🤖 Claude mode on: your messages now go to Claude Code (folder {folder}). "
                "/claude off to talk to the assistant again."), None
    if sub == "off":
        session.mode = False
        return "Claude mode off: your messages go to the assistant again.", None
    if sub == "new":
        if session.busy():
            return "Claude Code is still working (/claude stop to stop it).", None
        session.session_id = None
        return ("New Claude Code session.", arg or None)
    if sub == "folder":
        if not arg:
            return f"Claude Code works in {folder}. Allowed: {', '.join(map(str, settings.folders))}.", None
        if session.busy():
            return "Claude Code is still working (/claude stop to stop it).", None
        path = Path(os.path.expanduser(arg))
        path = (path if path.is_absolute() else (folder or Path.cwd()) / path).resolve()
        if not inside(path, settings.folders):
            return f"⚠ {path} is not inside the allowed folders: {', '.join(map(str, settings.folders))}.", None
        if not path.is_dir():
            return f"⚠ {path} is not a folder.", None
        session.folder, session.session_id = path, None  # Claude Code sessions belong to one folder
        return f"Claude Code now works in {path} (new session).", None
    if sub == "status":
        sandbox = "on (bwrap: only the allowed folders are visible)" if settings.sandbox else "off"
        return (f"{session.status(settings)}. Sign-in: {settings.auth}. Approvals: {settings.approval}. "
                f"Sandbox: {sandbox}."), None
    if sub == "stop":
        return ("Stopping Claude Code…" if session.stop() else "Claude Code is not running."), None
    if not arg:
        return f"Use: {HELP}.", None
    return "", arg  # /claude <prompt>


def available(owners: dict[str, str], channel: str, user_id: str, is_group: bool) -> bool:
    """IM: only the owner's own direct chat (CLI and the local web UI are always the owner)."""
    return not is_group and bool(user_id) and owners.get(channel) == user_id
