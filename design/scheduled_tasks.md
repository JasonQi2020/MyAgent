# Design: Scheduled Tasks ("query FTNT every day and report to me at 9 PM")

> Status: **implemented** (2026-10-24). Notes on what changed during implementation are in §10.
> Companion to [`agent-demo-design.md`](agent-demo-design.md), [`im_integration.md`](im_integration.md), [`model_switching.md`](model_switching.md) and [`claude_code_delegation.md`](claude_code_delegation.md).

## Context

The user wants to say, in normal language, *"add a task: query the FTNT stock every day and report to me at 9 PM"*, and have it happen every day without being asked again — whether the request goes to the **assistant** (Qwen/GPT) or to **Claude Code** (`/claude …`).

| Decision (user, 2026-10-24) | Choice |
|---|---|
| Real stock quotes | **New `stock_quote` tool**, free and keyless: Yahoo Finance's public chart endpoint (checked from this machine: FTNT 179.99 USD, previous close 169.13). Stooq's old CSV endpoint no longer exists. The demo database stays as it is (fake data, no FTNT) |
| Where the report goes | **The chat where the task was created.** Tasks created in the CLI or the web UI (which may be closed at 9 PM) report to the **owner's Feishu DM** |
| Time zone | **America/Los_Angeles** (this machine's zone) by default, `tasks.timezone` in `agent.properties`; a task may name its own |
| Who | **Owner only** (CLI, local web UI, the owner's Feishu DM) |
| Who runs a task | Each task has an **executor**: `assistant` (Qwen/GPT, its tools: database, mail, quotes) or `claude` (Claude Code in its sandbox). Default: whoever created it |

---

## 1. How it looks

```
owner (Feishu DM): "add a task: query FTNT every day and report to me at 9 PM"
assistant → schedule__add(title="FTNT daily", prompt="Get the latest FTNT quote and report price, change and day range.",
                          cron="0 21 * * *", time_zone="America/Los_Angeles", executor="assistant")
  → APPROVAL card: "Schedule a task: FTNT daily · every day at 21:00 (America/Los_Angeles) · run by the assistant ·
                    next runs: Sat Oct 24 21:00, Sun Oct 25 21:00, Mon Oct 26 21:00 · reports here"   [Approve] [Deny]
  → "Task #3 'FTNT daily' added. Next run: today 21:00 PDT."
... every day at 21:00 PDT, in that DM:
  ⏰ FTNT daily (task #3)
  FTNT 179.99 USD, +6.4 % (prev. close 169.13); day range 171.20–181.05 …
```

The same works with `/claude add a task: …`: Claude Code calls the same scheduler tools (§4.2), and the task is run by Claude Code unless it says otherwise.

## 2. Tasks

| Field | Meaning |
|---|---|
| `id` | Small number (#3), shown everywhere |
| `title` | Short name |
| `prompt` | What to do each time, written as an instruction (the run does not see the original conversation) |
| `cron` | 5-field cron expression in `time_zone` (`0 21 * * *` = every day 21:00). The model translates the user's words; the tool validates it and shows the next 3 run times so the owner can check them in the approval card |
| `time_zone` | IANA name, default `tasks.timezone` |
| `executor` | `assistant` or `claude` |
| `model` | For `assistant`: `weak` or `strong` at the time of creation (the run uses that profile) |
| `deliver_to` | `channel` + `chat_id`; CLI/web tasks → the owner's Feishu DM |
| `enabled`, `created_at`, `created_by`, `last_run_at`, `last_status`, `next_run_at` | Bookkeeping |

**Store:** SQLite `agent-demo/state/tasks.db`. `state/` is a new folder for the bot's own state, **outside `data/`** — `data/` is Claude Code's writable sandbox folder, so anything stored there could be changed by Claude Code. The groups file of IM §11 (today `data/im_groups.json`) moves to `state/im_groups.json` for the same reason. `state/` is git-ignored.

## 3. Running tasks

- **Scheduler loop** inside `agent-demo-server` (an asyncio task started with the runtime): wakes every 30 s, runs every due task in its own background task, then computes `next_run_at` from the cron expression (`croniter`, checked during implementation). A task never runs twice at the same time.
- **The server must be running.** If it was not (laptop asleep or off), on start-up a missed run is **caught up once** if it is less than `tasks.catch_up_minutes` (default 60) late; older missed runs are skipped and logged. The CLI does not run tasks.
- **Each run starts fresh** (no conversation history), with the task's prompt and a note: *"This is scheduled task #3 'FTNT daily'. The user is not watching: do the work and write a short report."*
  - `assistant`: a new `Agent` with the shared tools + `stock_quote`, using the task's model profile.
  - `claude`: `run_claude()` with a new `ClaudeCodeSession`, in its sandbox and approval level as usual.
- **Approvals during a run** (e.g. sending an email, a dangerous command): sent as a normal approval card to the task's delivery chat; no answer in 5 minutes = no. So an unattended run can never do something risky on its own.
- **Report:** `⏰ <title> (task #N)` + the answer (or `⚠ <error>`), sent to `deliver_to`. The run's result is also added as a short note to that chat's assistant session (like the Claude Code memory bridge), so *"why did FTNT drop?"* afterwards has context.
- **Limits:** at most `tasks.max_tasks` (default 20) enabled tasks; no schedule more often than every `tasks.min_interval_minutes` (default 15); each run has the executor's normal timeouts.

## 4. Creating and managing tasks

### 4.1 Assistant tools (owner sessions only)

| Tool | Approval | Does |
|---|---|---|
| `schedule__add(title, prompt, cron, time_zone?, executor?)` | **ask** | Validate, show next runs, create. Asking matters: an email or a group message could try to make the model create a task |
| `schedule__list()` | auto | Tasks with schedule, next run, last result |
| `schedule__remove(id)`, `schedule__pause(id)`, `schedule__resume(id)` | auto | Manage. (Removing one's own task is harmless and easy to redo) |
| `schedule__run_now(id)` | auto | Run once now (to test) |

### 4.2 Claude Code

The same tools are given to Claude Code as an **in-process MCP server** (`claude_agent_sdk.create_sdk_mcp_server`), named `scheduler`, so Claude Code sees `mcp__scheduler__add`, etc. Our permission hook allows exactly these MCP tools (all others stay refused) and asks for approval on `add`, like the assistant. They run in the server process, outside the sandbox, so Claude Code never touches `state/tasks.db` directly. Tasks it creates default to `executor="claude"`.

### 4.3 Commands (no model involved)

| Command | Effect |
|---|---|
| `/tasks` | List tasks |
| `/tasks run <id>`, `/tasks pause <id>`, `/tasks resume <id>`, `/tasks remove <id>` | Manage directly |

Added to the router (§1 of the Claude Code design) as commands, so they work in Claude mode too.

## 5. The `stock_quote` tool

- `stock_quote(symbols: list[str])` → one line per symbol: price, currency, change and % vs. previous close, day range, market time (in the exchange's time zone), market state (open/closed).
- Source: `https://query1.finance.yahoo.com/v8/finance/chart/<SYMBOL>?range=5d&interval=1d` (no key). Unofficial: if it changes or blocks requests, the tool says so clearly; the source is one function, easy to replace (e.g. Alpha Vantage with a key).
- Results wrapped as untrusted (data from the internet). Auto-approved (read-only). Available to every assistant session, not only scheduled runs.
- The system prompt line about the database gets a sibling: *"For real, current stock prices use stock_quote; the database holds demo data only."*

## 6. Configuration (`agent.properties`, re-read when used)

| Setting | Default |
|---|---|
| `tasks.enabled` | `true` |
| `tasks.timezone` | `America/Los_Angeles` |
| `tasks.max_tasks` | `20` |
| `tasks.min_interval_minutes` | `15` |
| `tasks.catch_up_minutes` | `60` |

The owner's Feishu DM (for CLI/web tasks) is learned automatically the first time the owner messages the bot directly (`claude.owner.feishu` identifies the owner) and stored in `state/`.

## 7. Code

| File | Change |
|---|---|
| `backend/agent_demo/tasks.py` | **New**: store, cron validation, scheduler loop, run + deliver, catch-up |
| `backend/agent_demo/quotes.py` | **New**: `stock_quote` |
| `backend/agent_demo/schedule_tools.py` | **New**: the `schedule__*` tools; the same handlers wrapped as the in-process MCP server for Claude Code |
| `backend/agent_demo/claude_code.py` | allow `mcp__scheduler__*` (ask on `add`); pass the MCP server |
| `backend/agent_demo/channels/hub.py` | owner sessions get the schedule tools; `/tasks` commands; `deliver(channel, chat_id, text)` for the scheduler; remember the owner's DM |
| `backend/agent_demo/router.py` | `/tasks …` |
| `backend/agent_demo/server.py`, `cli.py` | start/stop the scheduler (server only); CLI/web owner sessions get the tools |
| `backend/agent_demo/agent.py` | system-prompt line for `stock_quote` |
| `backend/pyproject.toml` | add `croniter` (or similar) |
| `agent.properties`, `.gitignore`, `README.md` | `tasks.*`; `state/`; docs |
| Move | `data/im_groups.json` → `state/im_groups.json` (migrated once on start-up) |

## 8. Tests

- Cron: valid/invalid expressions, next runs in a time zone (including the PDT→PST switch on Nov 1, 2026), minimum interval refused.
- Store: add/list/pause/resume/remove, persistence, `state/` location, limit of 20.
- Scheduler (fake clock): due task runs once; no overlap; catch-up within 60 minutes, skip when older; next run computed after each run; disabled tasks skipped.
- Runs: assistant run with a fake LLM and fake quote tool → report delivered to the right chat with the `⏰` header and a note added to that chat's session; Claude Code run with the fake SDK; an approval during a run goes to the delivery chat; timeout → no.
- Tools: `schedule__add` needs approval and shows next runs; owner-only (absent in groups and for other users); Claude Code's MCP tools allowed only for `scheduler`.
- `stock_quote`: parsing a saved Yahoo response; error and unknown-symbol messages. Integration (`-m integration`, opt-in): real FTNT quote.
- Manual (Feishu DM): "add a task: query FTNT every day and report to me at 9 PM" → approval card with next runs → `/tasks` → `/tasks run <id>` → report arrives; the same via `/claude …`.

## 9. Not in scope (v1)

- Tasks for other allowlisted users; tasks posting into group chats.
- One-off reminders ("tomorrow at 9") — possible later with a date instead of a cron expression.
- Running tasks while the server is not running (would need an always-on host, e.g. the planned Tencent VM).

## 10. Implementation notes (2026-10-24)

| Topic | What was built / found |
|---|---|
| Quotes: wrong daily change | With `range=5d`, Yahoo's `chartPreviousClose` is the close **before the 5-day window** and `previousClose` is empty, so the first version reported a 5-day change as the daily one (+6.42 % instead of +4.05 %). `fetch_quote()` now takes the previous trading day's close from the daily closes |
| Cron | `croniter` 6.2.4; 21:00 stays 21:00 local across the PDT→PST switch (checked) |
| Approval preview | New generic `Tool.preview(args)`: its result is added to the approval card (next runs, where reports go). If it contains `problem`, the agent answers with the problem instead of showing a card that cannot succeed; same in Claude Code's hook |
| "Report to me" | Qwen first wrote "send a summary email to …" into the task prompt. The tool description now says the answer is delivered to the chat automatically and not to add email unless asked |
| Unknown owner chat | Reports of CLI/web tasks need the owner's Feishu DM, learned when the owner messages the bot (`state/owner_chats.json`). Qwen kept retrying with email variants; the message now says only the user can fix it and not to retry |
| `state/` | `state/tasks.db`, `state/owner_chats.json`, `state/im_groups.json` (moved from `data/`, migrated once); git-ignored. Tests use a temporary `state/` (an autouse fixture) — an early run had written a fake test chat into the real `state/owner_chats.json`, which was removed |
| Claude Code: deferred tools | Claude Code loads MCP tool schemas on demand with its `ToolSearch` tool; our policy refused it, so Claude Code saw `mcp__scheduler__add` but could not call it (reported by the user). `ToolSearch` is now allowed (it only loads schemas, and only our scheduler's MCP tools exist there). Real check: `ToolSearch` → `mcp__scheduler__add` → approval with next runs → task created |
| CLI | Found a syntax error from the `/claude` change (no test imported `cli.py`); fixed, and `tests/test_cli.py` now imports and exercises it |
| Tests | `tests/test_tasks.py` (18: DST, schedule errors, store/commands, limits, owner chat, approval preview, scheduler with a fake clock — once, catch-up, failures —, runs through the hub with report + note, approval during a run, Claude Code executor, owner-only, Claude policy, MCP server, quote parsing, no card on problems, description), `tests/test_cli.py`, `tests/test_quotes_integration.py` (opt-in). Full suite: 205 passed |
| Live | Web UI with Qwen: "What is the FTNT price right now?" → `stock_quote` → $179.99 +4.05 %; "add a task: query FTNT every day and report to me at 9 PM" → `schedule__add(cron "0 21 * * *", America/Los_Angeles)`; a real run (Qwen + real quote, not delivered) produced a correct report with no approval requests |

## 11. Change: web UI tasks report to the web UI (2026-10-24)

**Reported by the user:** a task created in the web UI and one created in the Feishu DM both reported to Feishu. Expected: each report where its task was created. (The first version sent CLI/web reports to the owner's Feishu DM because a browser tab may be closed at report time.)

| Topic | Now |
|---|---|
| Target | Tasks created in the web UI have `channel=web, chat_id=owner` (`web_delivery.WEB_TARGET`) |
| Delivery | `WebDelivery` (server): the report (`task_report` event) goes to **every open web UI tab**, and each tab's assistant gets the follow-up note |
| No tab open | The report waits in `state/web_inbox.json` (last 50) and is shown when a tab connects, then removed |
| Approvals during a web task | Asked in the most recently opened tab; no tab open = no |
| Scheduler | Dispatches by target: web → `WebDelivery`, IM → the hub. It now also runs when no IM channel is configured |
| Existing tasks | Once at start-up, tasks with `created_by=web` are moved to the web target (task #2 here) |
| CLI | Unchanged: the CLI is not running at report time, so its tasks report to the owner's Feishu DM |
| Tests | `tests/test_web_delivery.py` (6): the reported scenario (one web + one Feishu task, each report in its own place), migration, inbox, all tabs, approvals, wording. Full suite: 211 passed |
| Live | After restart: "task #2 now reports to the web UI"; `/tasks run 2` in a web tab → the FTNT report appeared in that tab |
