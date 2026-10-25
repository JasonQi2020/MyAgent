# Design: `/claude` — Sending Work to Claude Code (Claude Agent SDK)

> Status: **implemented** (2026-10-24, revision 3: one-off and sticky modes, memory bridge to the assistant, combined `/model`). Notes on what changed during implementation are in §10.
> Companion to [`agent-demo-design.md`](agent-demo-design.md) (§7 approvals), [`im_integration.md`](im_integration.md) and [`model_switching.md`](model_switching.md).

## Context

The **assistant** (the normal agent: Qwen or GPT, see `model_switching.md`) is good at chat, the stock database and mail, but it cannot do real work on files. **Claude Code** can: write and fix code, run tests, refactor, produce documents. The user chooses explicitly who handles a message:

```
/claude <prompt>   →  one-off: this message goes to Claude Code
/claude on         →  sticky: every following message goes to Claude Code, until /claude off
<anything else>    →  the assistant (Qwen or GPT), exactly as today
```

### Two different kinds of command

| | `/weakmodel`, `/strongmodel` | `/claude …` |
|---|---|---|
| What it changes | **Which model powers the assistant** | **Which agent handles the message** |
| Agent | The same assistant, same tools (database, mail, group history), same history | A different agent (Claude Code), its own tools (files, shell) and its own session |
| Model | Qwen (weak) or GPT (strong) | Claude Code's own model (`claude.model`); **not affected** by `/strongmodel` / `/weakmodel` |
| Lasts | Sticky until switched, `/new` or idle reset | One-off (`/claude <prompt>`) or sticky (`/claude on` … `/claude off`) |

They are **independent**: switching the assistant's model while in Claude mode is allowed and only affects messages that go to the assistant. `/help`, `/model` and the switch replies say so.

| Decision | Choice |
|---|---|
| How | The Python **Claude Agent SDK** (`claude-agent-sdk`) runs a Claude Code session |
| Routing | By command only. The assistant never calls Claude Code by itself; messages for Claude Code never reach Qwen/GPT |
| Memory between the two | After every Claude Code answer, a **short note** goes into the assistant's history (§3), so follow-ups like "email me what you changed" work |
| What work | Anything Claude Code can do inside the allowed folders |
| Where | Only folders listed in `agent.properties` (`claude.folders`); default **`~/myHermes`** |
| Who | **The owner only** (CLI, local web UI, the owner's Feishu DM). In groups and for other users `/claude` answers "not available here" |
| Claude Code session | One per conversation; consecutive Claude Code messages continue it (it remembers what it did); `/claude new` starts a fresh one |
| Sign-in | `CLAUDE_CODE_OAUTH_TOKEN` (the owner's subscription, from `claude setup-token`) if set, otherwise `ANTHROPIC_API_KEY`. Anthropic's SDK docs restrict offering claude.ai login in products for others; owner-only use keeps the bot on the personal side of that line |
| No restart | Every Claude Code message re-reads `.env` and `agent.properties` |

Checked on this machine (2026-10-24): `claude` CLI 2.1.310 at `~/.local/bin/claude`; `claude-agent-sdk` 0.2.174 installs next to the project's `mcp` 2.3.0 (it needs `mcp>=1.23,<3`) on Python 3.12.

---

## 1. Commands

| Command | Effect |
|---|---|
| `/claude <prompt>` | **One-off**: send this prompt to Claude Code (continuing this conversation's Claude Code session if there is one). The next message goes to the assistant again (unless Claude mode is on) |
| `/claude on` | **Sticky**: Claude mode on. Every following message goes to Claude Code, without typing `/claude` |
| `/claude off` | Claude mode off: messages go to the assistant again |
| `/claude new [<prompt>]` | Start a fresh Claude Code session (then send the prompt, if given) |
| `/claude folder [<path>]` | Show or set this conversation's working folder (must be inside an allowed folder) |
| `/claude status` | Mode (on/off), folder, session (new / continuing), running or idle, sign-in method (never the token) |
| `/claude stop` | Stop the running Claude Code task |
| `/model` | **Both parts**: *"Assistant: weak (qwen / qwen-plus) · Claude Code: mode off, session open in ~/myHermes/agent-demo, idle"* |
| `/new` | Clears the assistant's conversation (and resets its model, as today), clears the Claude Code session, **turns Claude mode off** |

### Routing rules (in this order)

1. **Approval answers** (buttons, or `yes`/`no` while an approval is open) go to whoever asked, in any mode.
2. **Commands** are recognised only at the very start of a message and are always handled by the bot itself, in any mode: `/help`, `/tools`, `/new`, `/model`, `/weakmodel`, `/strongmodel`, `/claude …`, and in groups `/enable`, `/disable`.
3. `/claude <prompt>` → Claude Code (one-off).
4. Claude mode on → the message goes to Claude Code.
5. Otherwise → the assistant.

Consequences: `/claude /strongmodel` sends the literal text "/strongmodel" to Claude Code (the command is only recognised at the start). `/strongmodel` in Claude mode changes the assistant's model and says *"The assistant now uses openai / gpt-5.5. Claude mode is on, so your messages still go to Claude Code (/claude off to talk to the assistant)."*

**Busy rules:** while Claude Code is running in a conversation, a new message for Claude Code gets *"Claude Code is still working (/claude stop to stop it)"*; commands still work; a message for the assistant (Claude mode off) is answered normally. Only one Claude Code run and one assistant turn at a time per conversation.

**Visibility of the mode:** every Claude Code reply starts with `🤖 Claude Code`; the web header shows *Claude mode*; the CLI prompt becomes `claude> `. Claude mode turns off after `/new`, after the IM session's idle timeout (default 2 hours), and on a new web tab or CLI start.

## 2. Flow

```
user (Feishu DM): "/claude on"                                   → 🤖 Claude mode on (folder ~/myHermes)
user: "add a --json flag to seed_db.py and a test for it"        → Claude Code (sticky mode)
  → Claude Agent SDK session (cwd = folder, resume = session id), streaming:
        progress:  🤖 Read scripts/seed_db.py · Edit scripts/seed_db.py · Write tests/test_seed_db.py
        risky step → APPROVAL card: "Claude Code wants to run: uv run pytest tests/test_seed_db.py"
  → 🤖 Claude Code: <final answer>   Files changed: … · 12 turns · 3 min 10 s
user: "now update the README too"                               → Claude Code (same session)
user: "/claude off"                                              → messages go to the assistant again
user: "email me a summary of what was changed"                   → assistant (Qwen/GPT) — knows via the memory note (§3)
```

There is **no approval for the prompt itself**: the owner typed it. Approvals are for Claude Code's risky actions (§4).

## 3. Memory bridge to the assistant

After each Claude Code answer, one short note is appended to the **assistant's** history (as a user-role message, so every provider accepts it):

```
[Note: the user asked Claude Code (working in ~/myHermes/agent-demo): "add a --json flag to seed_db.py and a test for it".
Claude Code finished in 12 turns. Files changed: scripts/seed_db.py, tests/test_seed_db.py.
Commands run: uv run pytest tests/test_seed_db.py (approved).
Its answer (shortened): Added --json … all 3 tests pass.]
```

- Only the request, the file list, the commands and Claude Code's final answer **cut to 1,500 characters** — not the transcript or file contents.
- Wrapped as `<untrusted source="claude-code">` (it may quote file contents), so the assistant uses it as information, not instructions.
- The assistant's system prompt gets one line: *"Notes marked [Note: …] report work done by Claude Code at the user's request; use them to answer follow-up questions about that work. You cannot run Claude Code yourself; tell the user to use /claude."*
- Nothing goes the other way: Claude Code does not see the assistant's conversation (the user's prompt is all it gets, plus its own session memory).

## 4. Permissions inside Claude Code (`can_use_tool`)

| Claude Code tool | Inside an allowed folder | Outside |
|---|---|---|
| `Read`, `Glob`, `Grep`, `LS` | allow | **deny** |
| `Edit`, `Write`, `MultiEdit`, `NotebookEdit` | allow (shown as progress) | **deny** |
| `Bash` | **ask the user** (the approval shows the exact command and folder) | — |
| `WebFetch`, `WebSearch` | **ask the user** | — |
| anything else (MCP tools, `Task` sub-agents, …) | **deny** in v1 | — |

- Paths are resolved first (no `..` or symlink tricks out of a folder).
- Denials go back to Claude Code with a reason (`PermissionResultDeny(message=…)`), so it can adapt.
- The approval uses the channel's existing UI (Feishu card, web dialog, CLI y/N), same 5-minute timeout; no answer = deny. An identical command is asked once per run.
- `setting_sources=["project"]`: Claude Code reads the folder's `CLAUDE.md`, but **not** the user's global settings, hooks or MCP servers.
- Sign-in variables are passed in `env`; with `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` is removed from that env (it would take precedence).

## 5. Settings (`agent.properties`, re-read on every Claude Code message)

| Setting | Default | Meaning |
|---|---|---|
| `claude.enabled` | `true` | Off = `/claude` answers "disabled" (and Claude mode turns off) |
| `claude.folders` | `~/myHermes` | Comma-separated allowed folders; the first is the default working folder |
| `claude.max_turns` | `40` | SDK `max_turns` per message |
| `claude.timeout_minutes` | `20` | Per message; then the run is stopped |
| `claude.max_budget_usd` | `2` | SDK `max_budget_usd` per message (only meaningful with an API key) |
| `claude.model` | empty | Claude Code's model; empty = its default. Independent of `/strongmodel` |
| `claude.owner.feishu` | empty | The owner's Feishu open_id; `/claude` works only in that DM |

## 6. Progress and answers

- One short line per Claude Code action: `Read seed_db.py`, `Edit tests/test_seed_db.py`, `$ uv run pytest …`.
- **Feishu:** buffered and sent at most every 20 s as one message. **Web:** a "Claude Code" card with the lines. **CLI:** dim lines.
- The final answer is Claude Code's last message, prefixed `🤖 Claude Code`, rendered like normal answers, plus a footer (files changed, turns, time; cost with an API key).
- Errors are shown plainly: folder not allowed, not signed in, usage limit reached (SDK `RateLimitEvent` / result errors), timeout, stopped.

## 7. Code

| File | Change |
|---|---|
| `backend/agent_demo/claude_code.py` | **New**: settings (fresh), `ClaudeCodeSession` per conversation (mode, folder, session id, running task), `run()` with the SDK, `can_use_tool` policy, progress/result formatting, the memory note, `/claude …` parsing |
| `backend/agent_demo/router.py` | **New**: the routing rules of §1, shared by the three UIs (returns: command reply / Claude Code / assistant) |
| `backend/agent_demo/models.py` | `/model` shows both parts; switch replies mention Claude mode when it is on |
| `backend/agent_demo/agent.py` | `add_note()` for the memory bridge; the extra system-prompt line |
| `backend/agent_demo/events.py`, `web/src/protocol.ts`, web components | events `claude_progress`, `claude_done`, `mode_changed` (header) |
| `backend/agent_demo/cli.py`, `server.py`, `channels/hub.py` | use the router; owner check; approvals through the conversation's approver; `/new` resets everything |
| `backend/pyproject.toml` | add `claude-agent-sdk` |
| `agent.properties`, `.env.example`, `README.md` | `claude.*` settings, `CLAUDE_CODE_OAUTH_TOKEN` |

`run()` takes the SDK `query` function as a parameter, so tests replace it with a scripted fake (no Claude Code, no network).

## 8. Tests

- **Routing** (the five rules): approval answers in both modes; commands in Claude mode (`/strongmodel`, `/model`, `/new`, `/claude off`) are handled, never sent to Claude Code; `/claude <prompt>` is one-off; `/claude on` is sticky until `/claude off`, `/new` or idle; `/claude /strongmodel` sends the literal text; nothing for Claude Code reaches the LLM and vice versa; `/claude` in groups / from other users → "not available here".
- **Independence:** `/strongmodel` in Claude mode changes only the assistant's model and says Claude mode is still on; `/model` shows both parts.
- **Memory bridge:** after a fake run, the assistant's history has the note (wrapped, cut to 1,500 characters); a follow-up assistant turn sees it; Claude Code never receives the assistant's history.
- **Session:** the next Claude Code message resumes the stored session id; `/claude new` and `/new` reset it.
- **Busy:** Claude Code running → another Claude Code message gets the busy reply, an assistant message (mode off) is answered, commands work.
- **Config:** folders and `~`, owner id, re-read without restart; missing sign-in → error naming the variable; OAuth token removes `ANTHROPIC_API_KEY` from the run's env.
- **`can_use_tool`:** the table in §4, path tricks denied, identical command asked once, denial reason returned.
- **Fake SDK run:** progress lines (Feishu batching), final answer + footer, timeout, usage-limit error, `/claude stop`.
- Integration (`-m integration`, opt-in): a real tiny run in a temporary folder ("create hello.txt containing hi").
- Manual (Feishu DM): `/claude on`, a small coding task with a test run (approval card), a follow-up, `/claude off`, then "email me what was changed" to the assistant.

## 9. Not in scope (v1)

- The assistant calling Claude Code by itself (possible later as a tool on top of `claude_code.py`).
- Claude Code using the assistant's mail/database tools, its own MCP servers or sub-agents.
- Running a Claude Code task in the background while the same conversation keeps using Claude Code.
- Undo of Claude Code's changes (use git in the folder; `enable_file_checkpointing` could be a later step).

## 10. Implementation notes (2026-10-24)

| Topic | What was built / found |
|---|---|
| **`can_use_tool` is not enough** | Probe with the real SDK: Claude Code ran `cat hello.txt` without calling `can_use_tool` (it approves commands it considers safe by itself). The policy therefore lives in a **`PreToolUse` hook**, which sees every tool call (also checked: a `Write` outside the folder was blocked and the file never created). `can_use_tool` stays as a backup with the same policy |
| Read-only commands | Because the hook sees every command, "ask for every Bash command" would mean approval cards for `ls`. Auto-allowed without asking: `ls pwd cat head tail wc which tree du file stat` and `git status/diff/log/show/branch`, only with **no shell syntax** (`; & \| < > $ \` ` …) and no paths outside the allowed folders. Everything else is asked (e.g. `find … \| wc -l` in the live test) |
| Streaming input | The SDK needs the prompt as an async stream when permission callbacks are used (`_prompt_stream`) |
| Folders and `~` | The server runs as the user `claude`, the project lives in `/home/myall/myHermes`, so `~/myHermes` pointed at a missing folder. `agent.properties` now sets `claude.folders=/home/myall/myHermes`; the built-in default is the folder that contains agent-demo instead of `~/myHermes` |
| Sessions per folder | `/claude folder` starts a new Claude Code session (sessions belong to one folder) |
| Router | `router.py` is used by the CLI, the web server and the hub. In the web UI, typed `/new`, `/tools`, `/help` are now handled by the server (before, they went to the model as text); new `notice` event for command replies |
| Notes | `Agent.add_note()` holds the memory note until the assistant's next turn, so it never lands between a tool call and its result |
| Owner | `claude.owner.feishu` = the owner's open_id; CLI and the local web UI are always the owner |
| Test isolation | A hub test with `/strongmodel` read the real `.env` and called OpenAI once; the Claude Code test fixture now points the model loader at an empty temporary project |
| Tests | `tests/test_claude_code.py` (16: policy, symlink/`..` tricks, fake SDK runs through the real hook, approvals asked once, timeout, max turns, routing, commands, owner-only, sticky/one-off, busy/stop, memory bridge) + `tests/test_claude_code_integration.py` (real Claude Code in a temporary folder, opt-in). Full suite: 178 passed |
| Live (web socket, real Claude Code, subscription) | `/claude folder agent-demo`; one-off question → approval for `find … \| wc -l` → "14" (correct, 2 turns, 4 s); Qwen then answered "Claude Code reported 14" (memory bridge); `/claude on` + plain message → "7" (correct, same session); `/model` shows both parts; `/claude off` |

### 10.1 Change: approval levels, `auto` by default (2026-10-24)

The owner found too many approval prompts in the CLI and Feishu. New setting **`claude.approval`** (re-read on every message):

| Level | Shell commands (`Bash`) and web access |
|---|---|
| `ask` | The behaviour of §4: only read-only commands run without asking |
| **`auto`** (default) | Everything runs without asking, **except dangerous commands**, which are still asked: `sudo`/`su`, `rm -r`/`-f`, `git push`, `git reset --hard`, `git clean -f`, `git branch -D`, `git checkout -- …`/`git restore`, `curl`/`wget … \| sh`, `mkfs`/`fdisk`/`shutdown`/`reboot`, `dd … of=`, `chmod`/`chown -R`, `kill -9`, fork bombs, writes to `/dev/sd*`, global `pip`/`npm` installs. Matched anywhere in the command (also after `;`, `&&`, `\|`) |
| `full` | Never asks |

Unchanged at every level: Claude Code's file tools stay inside `claude.folders`, and other tools (MCP, sub-agents) are refused. **Trade-off:** in `auto` and `full`, shell commands are not limited to the folders; the owner keeps work in git. An unknown value falls back to `ask`. `/claude status` shows the level. Tests: `test_approval_levels`, `test_approval_setting_from_properties` (180 passed). Live: the `find … \| wc -l` that needed approval before now runs at once.

## 11. OS sandbox: Claude Code sees only `agent-demo/data` (implemented 2026-10-24; see §11.6)

### 11.1 Requirement and decision

Claude Code may access **only `/home/myall/myHermes/agent-demo/data`**; every other directory is refused — also from the shell, also from scripts it runs. Our `PreToolUse` hook can confine Claude Code's file tools, but not what a shell command or a `python -c …` does. Decision: run the **whole Claude Code process inside a bubblewrap (`bwrap`) sandbox**, so other directories simply do not exist for it. The shell keeps working (python3, sqlite3 and other system tools).

### 11.2 What Claude Code can see

| Path inside the sandbox | Access | Why |
|---|---|---|
| `/home/myall/myHermes/agent-demo/data` (each folder in `claude.folders`) | **read-write**, same path as outside | Its working folder; paths in answers match the real ones |
| `/usr`, `/etc`, `/bin`, `/lib*` (links into `/usr`) | read-only | System programs and libraries (python3, sqlite3, certificates) |
| `/run/systemd/resolve` | read-only | DNS (on this machine `/etc/resolv.conf` points there) |
| `/opt/claude` | read-only | The Claude Code program itself (resolved from `claude` on PATH, so upgrades work) |
| `/sandbox-home` = `agent-demo/.claude-sandbox/home` | read-write | Claude Code's own home: its sessions (so `/claude` follow-ups resume) and settings. Not the real `~/.claude` |
| `/tmp`, `/proc`, `/dev` | private, minimal | Fresh for every run |
| Everything else (`/home/claude`, the rest of `/home/myall`, `agent-demo/.env`, the code, mail tokens, `~/.ssh`) | **does not exist** | |
| Network | shared | Needed for the Claude API. (Not restricted in v1; see §11.5) |

Plus `--unshare-all --share-net --die-with-parent --new-session`: own process, user, IPC and hostname namespaces; the sandbox ends with the server.

**Verified with a prototype** (2026-10-24, real Claude Code 2.1.310, our hook set to allow everything so only the sandbox protected): `cat agent-demo/.env` → "does not exist"; `ls /home/claude` → "does not exist"; `/home/myall/myHermes` shows only the path to `data`; `python3` + `sqlite3` on `demo.db` worked; the API was reachable after adding the DNS folder.

### 11.3 How it is wired

- `agent.properties`:
  - `claude.folders=/home/myall/myHermes/agent-demo/data`
  - **`claude.sandbox=on`** (new; default `on`). If `bwrap` is missing or fails, `/claude` **refuses to run** (fail closed) instead of running unsandboxed. `off` = previous behaviour (for machines without bwrap).
- `claude_code.py` builds the `bwrap` command line for every run (current binary path, current folders) and writes it as a small launcher script `agent-demo/.claude-sandbox/bin/claude` (mode 700); the SDK starts that via `ClaudeAgentOptions(cli_path=…)`. Re-built on every message, so no restart after changing folders.
- The sign-in token is passed as before (environment variable); it is the only secret inside the sandbox.
- Our `PreToolUse` hook stays as a second layer: file tools outside `claude.folders` are refused with a clear message before they even reach the sandbox; approval levels (`claude.approval`) work as before.
- `agent-demo/.claude-sandbox/` is added to `.gitignore`.
- `/claude status` shows `sandbox: on (bwrap)`; `/claude folder` only accepts folders inside `claude.folders`.

### 11.4 Tests

- Pure: the generated `bwrap` arguments (each folder bound read-write at the same path, binary read-only, no `/home` bind other than those, DNS folder only if it exists, `HOME` set to the sandbox home); `claude.sandbox=on` without `bwrap` → refuse with a clear message; `off` → no `cli_path`.
- Integration (`-m integration`, opt-in, real Claude Code in the sandbox): create a file in the allowed folder (works); read `agent-demo/.env` and list `/home/claude` from the shell (both "do not exist"); `python3`/`sqlite3` on a test database works.
- Manual (Feishu DM): `/claude how many companies are in demo.db?` → answered via sqlite3; `/claude show me agent-demo/.env` → refused / not found.

### 11.5 Limits (v1)

- **Network is open** inside the sandbox (the Claude API needs it). Claude Code could send the contents of `data/` to a website if asked; dangerous commands such as `curl … | sh` are still asked in `auto` mode. A later step could allow only the Anthropic API.
- `/etc` is readable (system configuration only; no user files, and `/etc/shadow` is not readable by the user anyway).
- Tools that live in a home folder (e.g. `uv`, user-installed Node) are not available inside; system `python3` and `sqlite3` are.

### 11.6 Implementation notes

| Topic | What was built / found |
|---|---|
| **Secrets in the environment** (not in the draft) | The server's environment holds every `.env` key (DashScope, OpenAI, Feishu, Sina), and the SDK passes the whole environment to Claude Code. Hiding the files alone would not have hidden the keys. The launcher starts bwrap with **`--clearenv`** and passes only `CLAUDE_*`, `ANTHROPIC_*`, `LC_*`, `PATH`, `LANG`, `LANGUAGE`, `TERM`, `TZ`, `NO_COLOR`, `FORCE_COLOR`. `--clearenv` comes first, because bwrap applies options in order and the arguments set `HOME` |
| Launcher | `agent-demo/.claude-sandbox/bin/claude`, a small Python script (mode 700) rewritten atomically on every run; it holds the bwrap arguments, **not** the token (the token arrives in its environment) |
| Paths | `/bin`, `/lib`, `/lib64`, `/sbin` re-created as symlinks into `/usr` when the host uses a merged `/usr`; the DNS folder is found from where `/etc/resolv.conf` points (here `/run/systemd/resolve`) |
| Fail closed | `claude.sandbox` is on unless it is exactly `off`/`false`/`0`/`no`; with it on and no `bwrap` (or no `claude` on PATH), `/claude` refuses before starting anything |
| Tests | `tests/test_claude_sandbox.py` (5: bwrap arguments, the launcher run with a fake bwrap — `--clearenv` first, token passed, `.env` keys dropped, token not on disk —, launcher used, fail closed, setting) and `tests/test_claude_sandbox_integration.py` (real Claude Code in the real sandbox, with our policy at `full` so only the sandbox protects: `.env` and `/home/claude` do not exist, sqlite3 works, no `.env` key in the environment). Full suite 185 passed; all 6 integration tests passed |
| Live (web socket) | `/claude status` → sandbox on, folder `agent-demo/data`; "how many companies in demo.db" → `sqlite3` inside → 12; "show me agent-demo/.env" → refused; follow-up "the number two questions ago" → 12 (sessions resume from the sandbox home) |
