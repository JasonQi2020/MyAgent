# Design: IM Integration (Feishu first, pluggable for WhatsApp and others)

> Status: **Feishu implemented and tested end to end** (2026-10-24). Notes on what changed during implementation are in §10.
> §11 (enabling groups without editing `.env`) is **implemented** (2026-10-24); see §11.8.
> §12 (reading the group discussion) is **implemented** (2026-10-24); see §12.8. §12.9 (attaching recent context automatically) is **implemented**.
> Companion to [`agent-demo-design.md`](agent-demo-design.md); this document replaces its earlier §16 draft.

## Context

Goal: talk to the agent from a **Feishu** bot now and a **WhatsApp** bot later, without redesigning anything when the second IM arrives. Today the agent has two UIs (CLI, web) on one core, connected through two interfaces in `backend/agent_demo/events.py`: `EventSink` ("something happened") and `Approver` ("may I run this?"). An IM channel is a third UI built on the same two interfaces — the agent core (`agent.py`) does not change.

The Feishu app already exists and its credentials are verified (`FEISHU_APP_ID` / `FEISHU_APP_SECRET` in `.env`; `tests/test_feishu_credential.py` gets a tenant token from `open.feishu.cn`). An earlier draft lives in `design/agent-demo-design.md` §16; this document supersedes it with these decisions:

| Decision | Choice |
|---|---|
| Process | **Inside `agent-demo-server`**: one process shares tools, the Gmail MCP connection and the LLM with the web UI. Enabled by `IM_CHANNELS=feishu` |
| Chats | **Direct messages + group chats when the bot is @mentioned**, allowlisted users only |
| Tool calls in chat | **Compact progress line** per tool call, then the answer |


---

## 1. Architecture

```
 Feishu ◀─long connection (outbound WebSocket, lark-oapi)─▶ FeishuChannel ─┐
 WhatsApp (later) ◀─webhook on the same FastAPI app────────▶ WhatsAppChannel┤  Channel interface
                                                                            ▼
                                                                ┌──────── ChatHub ────────┐
                                                                │ allowlist · dedupe      │  channel-agnostic
                                                                │ sessions · commands     │
                                                                │ approvals · rendering   │
                                                                └────────────┬────────────┘
                                                                             ▼ one Agent per session
                                              Agent core (agent.py) + shared Runtime (tools, MCP, LLM)
```

Two layers, so a new IM only adds one file:

| Layer | Knows about | Does |
|---|---|---|
| **Channel** (one per IM: `feishu.py`, later `whatsapp.py`) | The platform's SDK/API, message formats, cards/buttons | Receive → normalise to `IncomingMessage` / `ButtonPress`; send text, progress lines, approval prompts; update an approval prompt after it is answered |
| **ChatHub** (`hub.py`, shared) | Nothing platform-specific | Security checks, sessions, the `Agent` per session, `/new` `/tools` commands, approval futures, timeouts |

### 1.1 Normalised types (`channels/base.py`)

```python
@dataclass
class IncomingMessage:
    channel: str            # "feishu"
    chat_id: str            # Feishu chat_id (DM or group)
    user_id: str            # Feishu open_id of the sender
    message_id: str         # for dedupe and threaded replies
    text: str               # @mention placeholders already stripped
    is_group: bool
    mentioned_bot: bool     # always True for DMs

@dataclass
class ButtonPress:
    channel: str; chat_id: str; user_id: str
    call_id: str            # the tool call id from ApprovalRequest
    approved: bool

@dataclass
class ChannelLimits:
    max_text_chars: int     # Feishu card markdown ~ 4000 (safe value); WhatsApp 4096
    supports_buttons: bool  # Feishu True; WhatsApp True (max 3)

class Channel(Protocol):
    name: str
    limits: ChannelLimits
    async def start(self, hub: "ChatHub") -> None: ...      # connect; call hub.on_message / hub.on_button
    async def stop(self) -> None: ...
    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> None: ...
    async def send_approval(self, chat_id: str, req: ApprovalRequest, reply_to: str | None) -> str: ...  # returns a handle
    async def close_approval(self, chat_id: str, handle: str, outcome: str) -> None: ...  # "approved" / "denied" / "expired"
    def routes(self) -> APIRouter | None: ...               # webhook routes (None for Feishu; WhatsApp later)
```

`routes()` is the hook that lets WhatsApp's webhook mount on the existing FastAPI app later without touching `server.py` again.

### 1.2 Channel registry (`channels/__init__.py`)

```python
CHANNELS = {"feishu": FeishuChannel.from_env}      # later: "whatsapp": WhatsAppChannel.from_env
def build_channels(names: list[str]) -> list[Channel]   # from IM_CHANNELS; clear ConfigError for unknown names / missing vars
```

---

## 2. ChatHub (`channels/hub.py`)

### 2.1 Incoming message flow

```
on_message(msg)
  1. dedupe       message_id seen in the last 1,000? → drop (Feishu redelivers if an ack is late)
  2. allowlist    msg.user_id not in the channel's allowed users → drop, log "rejected open_id=…" (so the owner can find their id)
  3. groups       is_group and (not mentioned_bot or chat not allowed) → drop
  4. session      key = (channel, chat_id) for DMs, (channel, chat_id, user_id) for groups
                  → per-user context in groups; people never share history or approvals
  5. commands     /new → reset; /tools → list tools and policy (same as CLI)
  6. busy?        a turn is running → reply "Still working on your last message…"
  7. run          asyncio.create_task(session.agent.run_turn(msg.text))   ← return at once; the channel acks the platform
```

### 2.2 Session

```python
@dataclass
class ChatSession:
    key: tuple; channel: Channel; chat_id: str; owner_id: str
    agent: Agent                         # Agent(runtime.llm, runtime.registry, sink, approver, max_steps)
    turn: asyncio.Task | None
    pending: dict[str, PendingApproval]  # call_id → (future, handle)
    reply_to: str | None                 # message_id of the triggering message (threaded replies in groups)
    last_active: float
```

- Idle sessions are dropped after `IM_SESSION_IDLE_MINUTES` (default 120); a background sweep runs every 5 minutes.
- On shutdown: resolve pending approvals as denied, cancel running turns (same as `server.py` WebSocket cleanup).

### 2.3 ChatSink — what the user sees

| Event (`events.py`) | Sent to the chat |
|---|---|
| `ToolCallEvent` | One compact line, e.g. `🔧 gmail__search_emails · query="from:github.com"` (arguments shortened to ~80 chars) |
| `ToolResultEvent` | Nothing on success; on error append `✗` line with a short reason |
| `AssistantText` | The text, via `render.py`; `reasoning` is **not** sent |
| `ApprovalRequest` | Handled by the Approver (below), not the sink |
| `ErrorEvent` | `⚠ <message>` |
| `TurnDone` | Nothing (token usage goes to the server log) |

To avoid one IM message per tool call, progress lines are **buffered and flushed** with the next assistant text / approval / end of turn (one message holding all lines since the last flush).

### 2.4 ChatApprover — approvals in chat

1. `approve(request)` creates a Future, then `channel.send_approval(chat_id, request, reply_to)`. Feishu: an interactive card with the tool name, the **full arguments** in a code block, and **Approve / Deny** buttons whose values carry `call_id`.
2. `on_button(press)` resolves the Future **only if** the press comes from the session's chat **and** `owner_id`, and the call is still pending. Others get a toast "Only <requester> can answer this."
3. Text fallback: replying `yes` / `no` (also `y`/`n`, `approve`/`deny`) answers the oldest pending approval in that session.
4. No answer in **5 minutes** (reuse `APPROVAL_TIMEOUT_SECONDS` from `server.py`, moved to `events.py`/shared) → denied.
5. Afterwards `channel.close_approval(handle, outcome)` replaces the buttons with "✅ Approved by … / ❌ Denied / ⌛ Expired", so old buttons can't be pressed again.

### 2.5 Rendering (`channels/render.py`)

- Markdown tables → monospace code blocks (Feishu card markdown and WhatsApp both handle code blocks better than tables).
- Split long text at paragraph boundaries to `limits.max_text_chars`.
- Pure functions; each channel picks a dialect (`feishu` keeps `**bold**`/lists; `whatsapp` later converts to `*bold*`).

---

## 3. Feishu channel (`channels/feishu.py`)

| Topic | Design |
|---|---|
| SDK | `lark-oapi` (official). New dependency in `backend/pyproject.toml` |
| Receiving | **Long-connection mode** (`lark.ws.Client`): outbound WebSocket from the laptop, **no public URL**; the server stays on 127.0.0.1 |
| Events | `im.message.receive_v1` (messages) and `card.action.trigger` (button presses), both over the same connection, registered with `lark.EventDispatcherHandler.builder("", "")…build()` |
| Threading | The SDK's `ws.Client.start()` is blocking and runs its own asyncio loop. Run it in a **daemon thread with its own event loop**; each handler converts the SDK object to a plain dict (`lark.JSON.marshal`) and hands it to the main loop with `asyncio.run_coroutine_threadsafe(hub.on_message(...), main_loop)`. Card callbacks must answer quickly: the handler returns a toast immediately and resolves the approval asynchronously |
| Parsing (pure function, unit-tested) | `parse_message(event_dict, bot_open_id) -> IncomingMessage \| None`: accept `message_type == "text"` (content is a JSON string `{"text": "..."}`); `chat_type` `p2p` or `group`; for groups `mentioned_bot` = bot's `open_id` in `message.mentions`; strip `@_user_N` placeholders from the text. Other types (images, files, post) → reply "I can only read text messages for now." |
| Bot identity | At start, fetch the bot's own `open_id` (`GET /open-apis/bot/v3/info`) to detect @mentions |
| Sending | `im.v1.message.reply` (threaded under the user's message, used in groups) or `im.v1.message.create` with `receive_id_type=chat_id`; replies sent as an **interactive card with a markdown element** so formatting survives. Use the SDK's async methods (`areply`/`acreate`) or `asyncio.to_thread` |
| Approval card | Card JSON (schema 2.0) with header "Approval needed", a markdown code block for the arguments, two buttons with `value: {"call_id": …, "approved": true/false}`; `close_approval` patches the card (`im.v1.message.patch`) to show the outcome without buttons |
| Domain | `FEISHU_DOMAIN` = `https://open.feishu.cn` (default; verified working) or `https://open.larksuite.com` |

### 3.1 Feishu console setup (one time; goes in README)

1. App → **Add features → Bot**.
2. **Permissions**: read DMs to the bot, read group messages that @mention the bot, send messages as the bot (`im:message`, `im:message.p2p_msg:readonly`, `im:message.group_at_msg:readonly`, `im:message:send_as_bot`). Exact scope names are confirmed in the console during implementation.
3. **Events & Callbacks** → subscription mode **long connection**; add event `im.message.receive_v1` and callback `card.action.trigger`. (`agent-demo-server` must be running while saving the long-connection setting.)
4. **Publish a version**; add the bot to any group where it should answer.
5. Start the server, message the bot, copy your `open_id` from the log ("rejected open_id=ou_…") into `FEISHU_ALLOWED_USERS`, restart.

---

## 4. Security

| Risk | Control |
|---|---|
| A stranger (or any group member) uses the bot to read/send your email | **Allowlist required**: `FEISHU_ALLOWED_USERS` (open_ids). Empty allowlist → the Feishu channel refuses to start with a clear error. Non-allowlisted messages and button presses are ignored and logged |
| Someone else approves your action in a group | Approval is accepted only from the session owner (`owner_id`) in the same chat |
| Email content leaks into a group chat | Groups are opt-in per chat: `FEISHU_ALLOWED_CHATS` (group chat_ids); empty = groups off, DMs only. README warns that the bot's replies (which may quote email) are visible to everyone in that group |
| Prompt injection from emails | Unchanged: `<untrusted>` wrapping + approvals (§7 of the main design) |
| Network exposure | Feishu long connection is outbound only; nothing new listens on the network. `/ws` stays localhost-only |
| Secrets | `.env` (git-ignored); never logged |

---

## 5. Configuration (`.env`, mirrored in `.env.example`)

| Variable | Default | Notes |
|---|---|---|
| `IM_CHANNELS` | empty | `feishu` now; `feishu,whatsapp` later. Empty = no IM, server behaves exactly as today |
| `IM_SESSION_IDLE_MINUTES` | `120` | |
| `FEISHU_APP_ID`, `FEISHU_APP_SECRET` | — | already set |
| `FEISHU_DOMAIN` | `https://open.feishu.cn` | |
| `FEISHU_ALLOWED_USERS` | — (required) | comma-separated open_ids |
| `FEISHU_ALLOWED_CHATS` | empty | group chat_ids where the bot answers @mentions |

Channel config is loaded in the channel's `from_env()`; `config.Settings` stays unchanged (its positional constructor is used in `tests/test_server.py`).

---

## 6. Files

| File | Change |
|---|---|
| `backend/agent_demo/channels/__init__.py` | **New**: `CHANNELS` registry, `build_channels()` |
| `backend/agent_demo/channels/base.py` | **New**: `IncomingMessage`, `ButtonPress`, `ChannelLimits`, `Channel` protocol |
| `backend/agent_demo/channels/hub.py` | **New**: `ChatHub`, `ChatSession`, `ChatSink`, `ChatApprover` |
| `backend/agent_demo/channels/render.py` | **New**: table → code block, split, short-args helper (can reuse `cli._short`, moved to `tools.py` or `render.py`) |
| `backend/agent_demo/channels/feishu.py` | **New**: `FeishuChannel`, `parse_message()`, card builders |
| `backend/agent_demo/server.py` | `lifespan`: after `open_runtime`, `build_channels(IM_CHANNELS)`, start a `ChatHub(runtime, channels)`, mount each `channel.routes()`; stop on shutdown. `ready_event` warnings include channel start-up failures |
| `backend/agent_demo/events.py` | (small) share `APPROVAL_TIMEOUT_SECONDS` |
| `backend/pyproject.toml` | add `lark-oapi` |
| `.env.example`, `README.md` | new variables; Feishu setup section; test count |

Reused as is: `Agent` (`agent.py`), `Runtime`/`open_runtime` (`runtime.py`), `ToolRegistry` (`tools.py`), `FakeLLM` (`tests/test_agent.py`).

---

## 7. Implementation order

1. `base.py`, `render.py`, `hub.py` + tests with a **FakeChannel** (in-memory: records sent texts/approvals, lets the test push messages and button presses) and `FakeLLM`. No Feishu needed.
2. `feishu.py`: `parse_message()` and card builders first (pure, tested with sample event dicts), then the SDK wiring (thread + long connection + send).
3. `server.py` lifespan wiring + `.env.example` + README.
4. Manual end-to-end with the real bot.

## 8. Verification

**Automated (offline, `uv run pytest`):**
- `tests/test_im_hub.py`: one session per DM; per-user sessions in groups; dedupe; non-allowlisted user ignored; group without @mention ignored; group not in `FEISHU_ALLOWED_CHATS` ignored; `/new` `/tools`; busy reply; progress lines buffered and flushed; approval by button (approve, deny), wrong user ignored, `yes`/`no` fallback, timeout → denied, `close_approval` called with the outcome; shutdown resolves pending approvals.
- `tests/test_im_render.py`: table → code block; splitting at the limit; short args.
- `tests/test_feishu.py`: `parse_message` for p2p text, group with/without bot mention (placeholder stripped), non-text message, malformed content; approval card JSON contains full args and both button values; `build_channels` errors (unknown channel, missing allowlist).
- Existing 38 tests still pass; server with `IM_CHANNELS` empty behaves as before (`test_server.py`).

**Integration (`-m integration`, opt-in):** existing tenant-token test; optionally fetch bot info (`/bot/v3/info`) to confirm the Bot feature is enabled.

**Manual end-to-end (real Feishu bot):**
1. `IM_CHANNELS=feishu`, start `uv run agent-demo-server`; log shows "Feishu connected".
2. DM the bot from a non-allowlisted state → ignored, open_id logged; add it, restart.
3. DM "Which tables are in the database?" → progress line `🔧 db_list_tables`, then the answer.
4. DM "Find my latest email and summarise it" → Gmail tools + answer.
5. DM "Email me a table of my watchlist" → approval card; press **Deny** (agent stops, card shows ❌), repeat and **Approve** (email arrives, card shows ✅). Let one expire after 5 minutes.
6. Group: add the bot, add the chat_id to `FEISHU_ALLOWED_CHATS`; message without @ → ignored; @bot question → threaded reply; another member presses Approve → rejected toast.
7. Web UI still works at the same time (shared runtime).

## 9. Adding WhatsApp later (what the design guarantees)

New file `channels/whatsapp.py` implementing `Channel` (Cloud API: webhook via `routes()` with signature check, reply buttons for approvals, 4096-char limit, `whatsapp` render dialect) + one line in `CHANNELS` + `WHATSAPP_*` variables. No changes to `hub.py`, `agent.py` or `server.py`.

> **Detailed design:** [`whatsapp_integration.md`](whatsapp_integration.md) (implemented 2026-10-24): DMs only; adds one small guard middleware to `server.py` so the tunnel cannot reach `/ws`.

---

## 10. Implementation notes (2026-10-24)

| Topic | What was built (differences from the draft above) |
|---|---|
| Empty allowlist | **Setup mode** instead of refusing to start (§4 contradicted §3.1 step 5): the bot connects, answers nobody, logs `user_id=…` for each sender and shows a warning in the log and the web header |
| SDK event loop | `lark_oapi.ws.client` grabs a module-level event loop at import time (the server's own loop). `FeishuChannel._run_ws` creates a fresh loop in the `feishu-ws` thread and sets `ws_client.loop` before `start()` |
| Dependencies | `lark-oapi` 1.7.5 pins `websockets<16`, so `websockets` went from 17.3 to 15.0.1; uvicorn and all tests are fine with it |
| Bot identity | `GET /open-apis/bot/v3/info` at start-up (needed to tell @bot from other @mentions) |
| Lazy imports | `channels/__init__.py` maps names to `"module:Class"`; the Feishu SDK is only imported when `IM_CHANNELS` names it |
| Progress lines | `🔧 tool · args` (no ` · ` when there are no arguments); failed tools add `✗ reason`; buffered and sent as one message before the next answer or approval |
| Approval card | Arguments shown in full up to 3,500 characters, then cut with a note (card size limit); after the answer the card is patched to ✅ / ❌ / ⌛ without buttons |
| Text answers | `yes/y/approve/ok/是/同意` and `no/n/deny/否/拒绝` answer the oldest open approval |
| Finding the user | The first user could not find the bot in Messenger; we looked up their `open_id` by phone (`contact/v3/users/batch_get_id`, scope `contact:user.id:readonly`) and had the bot message them first, which makes the chat appear. That scope is only needed for this, and can be removed |
| Tests | `test_im_hub.py` (17, fake channel + FakeLLM), `test_im_render.py` (6), `test_feishu.py` (15: SDK round-trip parsing, cards, sending with a fake client, cross-thread handlers). Full suite: 100 passed |
| End to end | Real bot in a DM: database question, Sina mail questions, approval card; four turns, no errors in the server log |

---

## 11. Enabling groups without editing `.env` (implemented 2026-10-24)

### 11.1 Problem

Today a group only works after its `chat_id` is added to `FEISHU_ALLOWED_CHATS` in `.env` and the server is restarted. That keeps groups opt-in (answers in a group are visible to every member, and they can quote the owner's email), but it is clumsy for more than one group. Decision: **A + B**, keeping groups opt-in while removing the manual step.

| Way | Trigger | Who can do it |
|---|---|---|
| **A. Auto-enable** | Feishu event **bot added to group** (`im.chat.member.bot.added_v1`) | Only if the person who added the bot (`operator_id`) is in the allowlist |
| **B. Commands** | `@AgentDemoBot /enable` and `@AgentDemoBot /disable` in a group | Only allowlisted users |
| Removal | Event **bot removed from group** (`im.chat.member.bot.deleted_v1`) | Anyone who removes it; the group is disabled |
| Static list (unchanged) | `FEISHU_ALLOWED_CHATS` in `.env` | Edited by hand; still works, cannot be disabled by command |

### 11.2 Behaviour

| Situation | Result |
|---|---|
| An allowlisted user adds the bot to a group | Group enabled at once, saved, and the bot posts: *"Hi, I'm enabled here because <name> added me. @mention me to ask something. Everyone in this group can read my answers — keep email questions for a direct chat with me."* |
| Someone **not** on the allowlist adds the bot | Nothing is enabled and the bot stays silent; the server log names the group and who added it. An allowlisted member can still send `@AgentDemoBot /enable` |
| The group is **external** (event field `external: true`) | Never auto-enabled (other organisations' members would read the answers); `/enable` replies that external groups are not supported |
| `@AgentDemoBot /enable` by an allowlisted user | Group enabled and saved; the reply includes the visibility warning. Already enabled → "Already enabled." |
| `@AgentDemoBot /disable` by an allowlisted user | Group disabled; open sessions in that group are dropped and pending approvals denied. If the group comes from `FEISHU_ALLOWED_CHATS`: "This group is enabled in .env (FEISHU_ALLOWED_CHATS); remove it there." |
| `/enable` or `/disable` from a non-allowlisted user | Ignored (and logged), like every other message from them |
| The bot is removed from a group | Group disabled, sessions dropped, nothing posted (the bot can no longer post there) |
| `/enable`, `/disable` in a direct chat | "This command only works in group chats." |

`/help` lists the two group commands.

### 11.3 Design

**New normalised event** in `channels/base.py` (channel-agnostic, so WhatsApp groups can use it later):

```python
@dataclass
class GroupEvent:
    channel: str
    chat_id: str
    operator_id: str      # who added/removed the bot ("" if unknown)
    kind: str             # "bot_added" | "bot_removed"
    chat_name: str = ""
    external: bool = False
```

**Group store** `channels/groups.py`, owned by the hub (not by a channel):

```python
class GroupStore:
    # Groups enabled at runtime, per channel, saved in data/im_groups.json.
    def __init__(self, path: Path): ...
    def is_enabled(self, channel: str, chat_id: str) -> bool: ...
    def enable(self, channel: str, chat_id: str, by: str, via: str, name: str = "") -> bool: ...  # False if already
    def disable(self, channel: str, chat_id: str) -> bool: ...
```

- File: `data/im_groups.json` (`data/` is already git-ignored), e.g.
  `{"feishu": {"oc_…": {"enabled_by": "ou_…", "via": "bot_added", "name": "Team", "at": "2026-10-24T15:10:00"}}}`
- Written atomically (temp file + rename), loaded at start-up, so enabled groups survive restarts.
- The hub's group rule becomes: **enabled = in `channel.allowed_chats` (static) or in the store**.

**ChatHub changes** (`hub.py`):

- `on_group_event(event)`: applies §11.2 (allowlist check on `operator_id`, `external` check, enable/disable, welcome message, dropping sessions).
- `_allowed()`: for a group @mention from an allowlisted user in a **not enabled** group, the message is still dropped **unless** it is `/enable` (the only command that works there).
- `/enable` and `/disable` handled with the other commands.
- `drop_chat(channel, chat_id)`: denies pending approvals, cancels running turns and removes every session of that chat.

**Feishu changes** (`feishu.py`):

- Register `register_p2_im_chat_member_bot_added_v1` and `register_p2_im_chat_member_bot_deleted_v1` (both exist in lark-oapi 1.7.5; the event carries `chat_id`, `operator_id`, `external`, `name`).
- `parse_group_event(event_dict, kind) -> GroupEvent | None` (pure, unit-tested).
- Handlers hand the event to the hub with `run_coroutine_threadsafe`, like messages.

**Configuration:** no new required variables. `IM_GROUPS_FILE` (default `data/im_groups.json`) only if a different location is needed.

### 11.4 Feishu console (one time)

1. **Events & Callbacks → Event configuration → Add events**: *Bot added to group* (`im.chat.member.bot.added_v1`) and *Bot removed from group* (`im.chat.member.bot.deleted_v1`). The console shows which permission each needs (probably read access to group info); add it.
2. Publish a new version.

Without this step, B (`/enable`) still works; only A is missing.

### 11.5 Security check

| Risk | Control |
|---|---|
| Someone adds the bot to their group to read the owner's email | Their group is never enabled (operator not allowlisted), and they cannot use the bot anyway (sender not allowlisted) |
| An allowlisted colleague enables a group by accident | Welcome / `/enable` reply states that answers are visible to the group; `/disable` or removing the bot turns it off |
| Data from other organisations | External groups are never enabled |
| The store file is edited by hand | Only read at start-up; it can only enable groups, and senders still need the allowlist |

### 11.6 Tests

- `GroupStore`: enable/disable/is_enabled, persistence across instances, atomic write, missing or corrupt file → empty store with a warning.
- Hub: bot added by allowlisted user → enabled + welcome; by others → not enabled, silent; external → not enabled; bot removed → disabled + sessions dropped; `/enable` in a not-yet-enabled group works; `/disable` drops sessions and denies pending approvals; `/disable` on a static group explains `.env`; commands in DMs; `/enable` from non-allowlisted user ignored.
- Feishu: `parse_group_event` for added/removed events through the SDK classes (unmarshal → marshal), missing fields → None.
- Manual: remove and re-add the bot in the test group (auto-enable + welcome); `/disable` then `/enable`; restart the server and confirm the group stays enabled.

### 11.7 Files

| File | Change |
|---|---|
| `backend/agent_demo/channels/base.py` | `GroupEvent` |
| `backend/agent_demo/channels/groups.py` | **New**: `GroupStore` |
| `backend/agent_demo/channels/hub.py` | `on_group_event`, `/enable` `/disable`, store-aware group rule, `drop_chat` |
| `backend/agent_demo/channels/feishu.py` | two event handlers, `parse_group_event` |
| `backend/tests/test_im_groups.py` | **New**: store + hub group tests; Feishu parsing added to `test_feishu.py` |
| `README.md` | groups section: add the bot, or `/enable`; the two console events |

### 11.8 Implementation notes

| Topic | What was built (differences from §11.2–11.3) |
|---|---|
| Welcome text | Does not name the person who added the bot (the event only carries their open_id): *"Hi, I'm enabled in this group. @mention me to ask something. Everyone in this group can read my answers, so keep email questions for a direct chat with me. Send @me /disable to turn me off here."* |
| External groups | Checked only on *bot added* (the event has `external`); message events do not say whether a group is external. In practice the app's version settings already say *"Bot can't be added to external groups"*, so `/enable` cannot be reached from one |
| `/disable` in a group that was never enabled | Replies "I'm not enabled in this group." |
| Order of checks in `on_message` | allowlisted sender → in a group: @mentioned → `/enable` `/disable` (work even in a group that is not enabled) → group enabled? → session, commands, turn |
| Storage | `GroupStore` in `channels/groups.py`; `server.py` uses `data/im_groups.json` (or `IM_GROUPS_FILE`) |
| Tests | `tests/test_im_groups.py` (10) and `test_parse_group_events` in `tests/test_feishu.py`. Full suite: 110 passed |

---

## 12. Reading the group discussion (implemented 2026-10-24)

### 12.1 Problem and decision

The bot only sees messages that @mention it (§2.1, step 3), so it cannot answer *"@AgentDemoBot summarise the discussion above"* or *"what did Amy ask about RY?"*. Decision (option 1): the bot still **answers only @mentions**, but when asked it can **read the recent messages of that group**.

| Choice | Decision | Why |
|---|---|---|
| Where the messages come from | **Feishu's message history API**, fetched at the moment they are needed | Also sees messages from before a server restart; we never store other members' messages ourselves |
| When they are read | Through a **tool** the agent calls only when the question is about the discussion | Other members' messages reach the LLM only when needed, not with every @mention |
| Where it works | Group chats only (in a DM the agent already has the conversation) | |

### 12.2 The tool

`chat__recent_messages` is added **per session**, only for group sessions on channels that support history:

| Field | Value |
|---|---|
| Description (for the model) | *"Read the most recent messages in this group chat (other members' messages too). Use it when the user asks about the discussion, e.g. to summarise it or find what someone said."* |
| Arguments | `limit` (default 30, max 100), optional `since_minutes` |
| Policy | **auto** (no approval): it only reads the group the user is asking in, which they can already read |
| Result | One line per message, oldest first: `[14:02] Amy: has anyone looked at RY today?` (each message cut at 500 characters; images and files shown as `[image]`, `[file]`). The question that triggered the turn is left out. |
| Wrapping | `<untrusted source="feishu-group">` like email: other members could write instructions aimed at the bot (prompt injection), so the model must treat them as data |

Implementation: `ChatSession` builds its `Agent` with a **session registry** = the shared tools + this one tool bound to the session's chat (`ToolRegistry.with_tools([...])` returns a copy; the shared registry is not changed).

### 12.3 Channel interface

An optional method on `Channel` (channels without history simply do not offer the tool; WhatsApp has no history API and could later use an in-memory buffer instead):

```python
@dataclass
class HistoryLine:
    time: datetime
    sender: str        # display name, or a short id if the name is unknown
    text: str
    from_bot: bool

async def history(self, chat_id: str, limit: int, since: datetime | None) -> list[HistoryLine]: ...
```

### 12.4 Feishu implementation

| Step | API | Notes |
|---|---|---|
| Messages | `im.v1.message.list` with `container_id_type=chat`, `container_id=<chat_id>`, `sort_type=ByCreateTimeDesc`, paging until `limit` | Text: the `text` field; rich text (`post`): its text parts; others: `[image]`, `[file]`, `[sticker]` … Bot messages (our cards) shown as the bot's name with the card's markdown text |
| Sender names | `im.v1.chat.members.get` for the group, cached for 10 minutes | Messages only carry the sender's open_id; members not found are shown as `user-1a2b` |
| Mentions inside messages | `@_user_N` placeholders replaced by the mentioned name (the message's `mentions` list) | Same as `parse_message` |

**Permissions:** none to add. Checked on 2026-10-24 with the app's current tenant-token scopes: `GET /im/v1/messages` (group history) and `GET /im/v1/chats/{chat_id}/members` (names) both return `code 0` for the enabled test group. The member list contains people only, not bots.

### 12.5 Privacy and security

| Risk | Control |
|---|---|
| Other members' messages are sent to the LLM (DashScope) | Only when the agent calls the tool, i.e. when an allowlisted user asks about the discussion in an enabled group; the welcome message (§11) adds: *"If you ask me about the discussion here, I read the recent messages of this group."* |
| A member writes instructions for the bot into the group | Results are `<untrusted>`; the system prompt rule applies; risky actions still need the asker's approval |
| Reading groups the asker is not in | Impossible: the tool is bound to the chat of the session, which is the asker's own group |
| Very long histories | `limit` max 100, each message max 500 characters, and the agent's usual result truncation |

### 12.6 Tests

- `ToolRegistry.with_tools`: copy contains both; the shared registry is unchanged.
- Hub: group sessions get `chat__recent_messages`, DM sessions do not; channels without `history` do not; the tool result is wrapped as untrusted, oldest first, the triggering message excluded, `limit`/`since_minutes` passed through and capped.
- Feishu: building `HistoryLine`s from sample `message.list` pages through the SDK classes (text, post, image, bot card, mentions), paging, member-name cache and fallback names.
- Manual: in an enabled group, have a short conversation without the bot, then *"@AgentDemoBot summarise the discussion"* → `🔧 chat__recent_messages · limit=30` and a summary naming the right people.

### 12.7 Files

| File | Change |
|---|---|
| `backend/agent_demo/tools.py` | `ToolRegistry.with_tools()` |
| `backend/agent_demo/channels/base.py` | `HistoryLine`; optional `history()` on `Channel` |
| `backend/agent_demo/channels/hub.py` | per-session registry with `chat__recent_messages` for group sessions; welcome text |
| `backend/agent_demo/channels/feishu.py` | `history()`, message-to-line conversion, member-name cache |
| `backend/tests/test_im_history.py` | **New** |
| `README.md` | group section: what the bot can read, the new permission |

### 12.8 Implementation notes

| Topic | What was built (differences from §12.2–12.4) |
|---|---|
| SDK bug | The async `client.arequest` mishandles a query string written into the URI (Feishu answers `99992402 field validation failed`); the sync client does not. `FeishuChannel._get()` passes parameters with the request builder's `.queries()` |
| History item shapes | In `message.list`, mention `id` is a plain string (in live events it is an object); bot cards come back as `{"title", "elements": [[segments]]}`; `system` messages (e.g. someone added the bot) and deleted messages are skipped |
| Names | Bot messages: our own bot's name from `/bot/v3/info` (`app_name`), other bots "another bot"; people from the member list (cached 10 minutes), else `user-<last 4 chars of id>` |
| `/tools` in a group | Lists the session's tools, including `chat__recent_messages` |
| Splitting long answers | Found while testing: one over-long line was cut mid-word. `render.split_text` now cuts at the last space or punctuation (also Chinese `，。！？；、`), hard cut only if there is none |
| Live check | Real test group: 12 messages, bot messages named, 9 of 9 people resolved to real names, oldest first, `limit` and `since` respected |
| Tests | `tests/test_im_history.py` (10) + split tests in `test_im_render.py`. Full suite: 119 passed |

### 12.9 Change: recent context is attached automatically (implemented 2026-10-24)

**Why.** Live test: a user chatted in the group, then asked *"@AgentDemoBot can you give me some advice?"* and the bot answered that it had no context. The turn took 1 step: the model never called `chat__recent_messages`. A stronger system prompt for group sessions (now in `hub.py`, `GROUP_CHAT_PROMPT`) did not help: with qwen-plus the tool was called in **0 of 9** trials of vague questions; the model asks the user for context instead. Leaving the decision to the model (§12.1, "when they are read") is not reliable.

**Decision (option 1).** On every @mention in an enabled group, the hub **reads the recent messages itself** and gives them to the model together with the question. The tool stays for longer or older history.

| Topic | Design |
|---|---|
| What is attached | Up to `IM_GROUP_CONTEXT_MESSAGES` (default **15**) messages from the last `IM_GROUP_CONTEXT_MINUTES` (default **30**), oldest first, the question itself excluded. Same line format and 500-character cut as the tool |
| How | Before `run_turn`, the hub calls `channel.history(...)` and sends the model one user message: the context block, wrapped as `<untrusted source="feishu-group">`, followed by `The question:` and the user's text. Only the user's own text is kept in the conversation history for later turns, so old context does not pile up |
| Turned off | `IM_GROUP_CONTEXT_MESSAGES=0` → behaviour as before (tool only) |
| Failure | If reading the history fails, the question is answered without context and the error is logged; the turn never fails because of it |
| Empty | No recent messages → nothing attached |
| DMs | Unchanged: no context, no tool |
| Prompt | `GROUP_CHAT_PROMPT` is updated: recent messages are already included above the question; call `chat__recent_messages` only for more or older messages |

**Privacy (replaces the first row of §12.5).** Other members' recent messages are sent to the LLM **on every @mention** in an enabled group, not only when the model asks. This only happens in groups an allowlisted user enabled; the welcome text says so: *"When you @mention me, I read the recent messages of this group to understand the question."* Set `IM_GROUP_CONTEXT_MESSAGES=0` to stop it.

**Cost.** About 15 short lines extra per group question (a few hundred tokens), plus one history API call.

**Agent change.** `Agent.run_turn(user_message, context=None)`: when `context` is given, the model sees `context + question` for this turn, while the history stores only the question. (Small, generic: also usable later for other channels.)

**Tests.** Context attached and wrapped; question excluded; window and count passed to `history`; `0` disables; history failure → answer without context and a log line; the stored history holds only the question; DMs unaffected. Live: chat in the group, then *"@AgentDemoBot can you give me some advice?"* → an answer that uses the discussion, without a `chat__recent_messages` line.

**Implementation notes (§12.9).**

| Topic | Result |
|---|---|
| Context alone was not enough | With the context attached, qwen-plus saw the discussion but still asked "what kind of advice?". The group prompt now also says: *assume a short or vague question is about the recent discussion; answer it directly and ask for clarification only if the discussion does not make the meaning clear* |
| Real-model check | Made-up three-message discussion, Qwen (qwen-plus): "can you give me some advice?" and "what do you think?" answered from the discussion in **3/3** runs each (0/9 before); "Which tables are in the database?" still called the database tool in 3/3 |
| Code | `Agent.run_turn(question, context=None)`; `ChatSession.read_recent()` shared by the tool and `recent_context()`; `ChatHub(context_messages=15, context_minutes=30)` from `IM_GROUP_CONTEXT_MESSAGES` / `IM_GROUP_CONTEXT_MINUTES` |
| Tests | 5 new in `tests/test_im_history.py`, 1 in `tests/test_agent.py`. Full suite: 125 passed |
