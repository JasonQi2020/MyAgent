# Design: WeCom (企业微信 / "WeWork") channel

> Status: **implemented, offline-tested** (2026-10-25); manual end-to-end with a real smart robot still to do. Differences from the draft are in §11.
> Companion to [`im_integration.md`](im_integration.md) (channel interface, ChatHub, Feishu) and [`whatsapp_integration.md`](whatsapp_integration.md).

## Context

The third IM, after Feishu and WhatsApp. WeCom offers three kinds of bots, and only one fits:

| Option | Receives messages? | Public URL needed? | Verdict |
|---|---|---|---|
| Group robot (群机器人, webhook key) | **No**: send-only | — | Not usable |
| Self-built app (自建应用) with a callback URL | Yes (encrypted XML) | **Yes**, and the domain must be ICP-filed (备案) and verified as the company's own; API calls also need a fixed **trusted IP** (企业可信IP) | Not workable from a laptop |
| **Smart robot (智能机器人), API mode, long connection (长连接)** | **Yes**: DMs and group @mentions | **No**: outbound WebSocket to `wss://openws.work.weixin.qq.com` | **Chosen** |

The smart robot's long connection works like Feishu's: the laptop connects out, and nothing listens on the network. Unlike Feishu there is no large SDK to run in a thread: the protocol is a few JSON frames, so we write a small asyncio client ourselves (§3).

| Decision | Choice |
|---|---|
| Bot type | **Smart robot, API mode, long connection** (BotID + Secret) |
| Chats | **DMs + groups when @mentioned**, the same rules as Feishu (allowlist, groups opt-in) |
| Client | **Own asyncio WebSocket client** using `websockets` (already installed through `lark-oapi`), on the server's event loop, with no thread. No SDK (review decision) |
| Answers | **Streaming**: one message per turn that grows while the agent works (progress lines, then the answer), as a reply to the user's message (§4). Needs a small, optional addition to the hub |
| Process | Inside `agent-demo-server`: `IM_CHANNELS=feishu,wecom` |
| Unchanged | `agent.py`, `server.py`. `hub.py` and `base.py` get an **optional** streaming hook (§4.2); Feishu and WhatsApp keep working as today |

---

## 1. Protocol summary (from the WeCom developer docs, "智能机器人长连接")

Every frame is JSON text: `{"cmd": "...", "headers": {"req_id": "..."}, "body": {...}}`. Answers to our own requests come back with the same `req_id` plus `errcode`/`errmsg`.

| Direction | `cmd` | Use |
|---|---|---|
| → | `aibot_subscribe` | Body `{"bot_id", "secret"}`; `errcode 0` = connected. Rate-limited, so don't resubscribe in a loop |
| → | `ping` | Every **30 s**; the server drops silent connections |
| ← | `aibot_msg_callback` | A user message: `msgid`, `aibotid`, `chattype` (`single`/`group`), `chatid` (groups only), `from.userid`, `msgtype` (`text`, `mixed`, `voice`; DMs also `image`/`file`/`video`) |
| ← | `aibot_event_callback` | `event.eventtype`: `enter_chat`, **`template_card_event`** (button click: `event_key`, `task_id`), `feedback_event`, **`disconnected_event`** (another connection took over) |
| → | `aibot_send_msg` | Push `markdown` or `template_card` to `chatid` (userid for DMs, chatid for groups), `chat_type` 1 = single, 2 = group. Allowed once the user has written to the bot |
| → | `aibot_respond_update_msg` | Replace a clicked card. **Must be sent within 5 s** of the click event, with the click's `req_id` |
| → | `aibot_respond_msg` | Reply to a message by its `req_id`. Used for the **stream** answer (§4): `stream.id`, `stream.content`, `stream.finish` |

Limits that matter:
- **One connection per bot.** A new connection kicks the old one, which first receives `disconnected_event`. This is the same "one server at a time" rule as Feishu.
- **30 messages/minute and 1,000/hour per conversation.** Progress lines are already buffered into one message.
- Replies are allowed for **24 hours** after the user's message.
- **Voice messages arrive already transcribed** (`voice.content`), so the bot can take them as text, which Feishu and WhatsApp can't.

## 2. Mapping onto the channel interface

| `Channel` part | WeCom |
|---|---|
| `IncomingMessage.chat_id` | `chatid` for groups; `from.userid` for DMs, which is also the target of `aibot_send_msg` |
| `user_id` | `from.userid`. It's plaintext if the bot was created by a super admin, otherwise an encrypted per-company id; either way, setup mode logs it for the allowlist |
| `message_id` | `msgid` (hub dedupe) |
| `is_group` / `mentioned_bot` | `chattype == "group"`; always `True`, because WeCom only delivers group messages that @mention the bot |
| `text` | `text.content` with the leading `@BotName` removed; `voice.content` for voice; the text parts of `mixed`. Other types → `unsupported` |
| `send_text` | Used **outside** a streamed turn (command answers, `/help`, setup notices, overflow): `aibot_send_msg` with `msgtype: markdown`; `chat_type` from what the channel remembers about the chat (1 for a userid, 2 for a group chatid) |
| `send_approval` | Two pushes: (1) markdown with the tool and its **full arguments** (cut at 3,500 characters, like Feishu); (2) a `template_card` of type `button_interaction`: title "Approval needed", the tool name, buttons **Approve** (`key: "y"`, primary) and **Deny** (`key: "n"`), and a fresh `task_id` (`ap-<n>`). The channel maps `task_id` → call id; the handle is the `task_id` |
| Button press | `template_card_event` → `ButtonPress(chat_id, user_id, call_id from task_id, approved = event_key == "y")` → `hub.on_button()`. Then, **in the same handler and within 5 s**, `aibot_respond_update_msg` replaces the card with one without buttons, showing "✅ Approved / ❌ Denied", or the hub's notice when the press was rejected ("Only the person who asked…") |
| `close_approval` | If the card was already updated by the click, nothing. Otherwise (timeout, or a typed yes/no answer) the card **can't** be updated any more (no click within 5 s), so a follow-up markdown "⌛ Expired" / "✅ Approved" is sent |
| Groups (§11) | `/enable` and `/disable` work as they are. **No auto-enable**: WeCom has no "bot added to group" event. `WECOM_ALLOWED_CHATS` is the static list |
| Group history (§12) | Not available (no API to read a group's messages), so no `history()` method; the hub already skips it |
| `routes()` | `None`: long connection, no webhook |
| `limits` | `ChannelLimits(max_text_chars=4000, supports_buttons=True)`. WeCom's markdown limit is **4,096 bytes** (UTF-8; a Chinese character is 3 bytes), so `send_text` splits again by bytes when a part is too long |

**Markdown:** smart-robot markdown supports headings, bold, links, quotes and code. Tables are already turned into code blocks by `render.to_chat`. No new render dialect at first; check the rendering by hand and add a `wecom` dialect only if needed.

## 3. Connection client (inside `channels/wecom.py`)

```
start(hub) → task: _run()
_run():  loop forever (unless stopped or kicked):
           connect wss://openws.work.weixin.qq.com
           send aibot_subscribe; wait for errcode 0 (wrong id/secret → log error, stop: retrying won't help)
           run together: _heartbeat (ping every 30 s) and _reader
           on a dropped connection: reconnect with backoff 1 s → 2 s → … → 30 s
_reader: for each frame:
           a reply to one of our requests (req_id in self._waiting) → resolve that future (errcode ≠ 0 → error)
           aibot_msg_callback                → parse_message → hub.on_message (as a task)
           aibot_event_callback:
             template_card_event             → _on_click (as a task; must answer within 5 s)
             disconnected_event              → log "another agent-demo-server took over this bot"; stop, no reconnect
             enter_chat / feedback_event     → ignored
_request(cmd, body) → send a frame with a new req_id; wait up to 10 s for the reply; raise on errcode ≠ 0
```

- Everything runs on the server's own event loop, so no thread and no `run_coroutine_threadsafe` (simpler than Feishu).
- `start()` waits for the first successful subscribe (up to 10 s), so a wrong BotID or Secret shows up as a start-up warning in the web header, like the other channels.
- Not reconnecting after `disconnected_event` prevents two server instances from kicking each other off forever.

## 4. Streaming answers (review decision: now)

### 4.1 What the user sees

```
you   ▸ Email me a table of my watchlist
bot   ▸ 🔧 db_query · sql="SELECT …"                 ← one message, updated in place
        🔧 mail__send_email · to="me@…"
        ⏳ Waiting for your approval (card below)
bot   ▸ [card] Approval needed · mail__send_email  [Approve] [Deny]
          … click Approve → card turns into "✅ Approved"
bot   ▸ (the first message continues)
        Done: I sent the table to me@….              ← stream finished
```

The answer is a **reply to the user's message** (`aibot_respond_msg` with that message's `req_id`), so in groups it is clearly tied to the question, and the per-turn message count drops to about two.

Note: the agent's LLM calls are still non-streaming (main design §5), so the text grows **per step** (each tool call, then the answer), not per token. Streaming tokens from the LLM is a later exercise; the stream message is ready for it.

### 4.2 Hub addition (optional capability, like `history()` in §12)

`base.py` gets a small protocol, and a channel may offer it:

```python
class Stream(Protocol):
    async def update(self, text: str) -> None: ...   # the full text so far (not a delta)
    async def finish(self, text: str) -> None: ...   # final text; the message is then frozen

# Optional on a Channel:
#   async def open_stream(self, chat_id: str, message_id: str) -> Stream | None
#   (None = cannot stream for this message, e.g. unknown req_id; the hub falls back to send_text)
```

`ChatSession` in `hub.py`:
- `_run_turn`: if the channel has `open_stream`, open one for the triggering `message_id`, keep `self.stream` and a turn transcript.
- The sink appends to the transcript instead of buffering progress lines:
  - `ToolCallEvent` → progress line;
  - an error `ToolResultEvent` → `✗` line;
  - `AssistantText` → the answer (through `render.tables_to_code`);
  - `ErrorEvent` → `⚠` line.

  Each append calls `stream.update(transcript)`.
- `approve()`: appends "⏳ Waiting for your approval (see below)", then sends the card as today. The answer replaces that line with "✅ approved" / "❌ denied" / "⌛ expired".
- `TurnDone`, a failed turn or cancellation (`finally`) → `stream.finish(transcript)`.
- If the transcript would pass the stream limit (20,480 bytes), the stream is finished and the rest goes through `send_text` (split as usual).
- Without `open_stream` (Feishu, WhatsApp, `FakeChannel`), nothing changes: the existing code path is untouched and its tests still pass.

### 4.3 WeCom stream object (`channels/wecom.py`)

- **`req_id`:** the channel remembers `msgid → req_id` for recent message callbacks (last 1,000), so `open_stream(chat_id, message_id)` finds the `req_id` to reply with. An unknown `msgid` returns `None`.
- **Frames:** `aibot_respond_msg` with `{"msgtype": "stream", "stream": {"id": "<new id>", "finish": false, "content": text}}`. The first frame creates the message, and later frames with the same `id` replace its content.
- **Throttling:** updates are coalesced, at most one frame every **2 s** (`WECOM_STREAM_INTERVAL`). This stays within 30 messages/minute per conversation even if stream refreshes count; that's to be confirmed with the real bot. `finish()` always sends at once, with `finish: true`.
- **Lifetime:** a stream lives at most **10 minutes**. A turn that waits for a 5-minute approval can come close, so after 8 minutes the stream object finishes the current message and continues in a new one (`aibot_send_msg` markdown for the rest).

### 4.4 Why not push everything

`aibot_send_msg` (push) remains for messages that aren't part of a turn. Pushing the answers too would be simpler, but each progress flush and answer would be a separate message, and group answers would not be tied to the question.

## 5. Security

| Risk | Control |
|---|---|
| Strangers use the bot | `WECOM_ALLOWED_USERS` (userids); empty = setup mode (logs `user_id=…`, answers nobody) |
| Someone else approves | Hub: only the session owner, in the same chat |
| Email content in groups | Groups opt-in (`WECOM_ALLOWED_CHATS` or `/enable` by an allowlisted user); the warning text from §11 applies. WeCom groups can include **external contacts (WeChat users)**: the README warns not to enable such groups |
| Network | Outbound WebSocket only; nothing new listens; no tunnel |
| Secrets | `WECOM_BOT_SECRET` in `.env`, never logged (the subscribe frame is not logged) |

## 6. Configuration

| Variable | Default | Notes |
|---|---|---|
| `IM_CHANNELS` | — | add `wecom` |
| `WECOM_BOT_ID` | required | from the smart robot's API-mode page |
| `WECOM_BOT_SECRET` | required | same page (long-connection secret, not Token/EncodingAESKey) |
| `WECOM_ALLOWED_USERS` | empty = setup mode | comma-separated userids |
| `WECOM_ALLOWED_CHATS` | empty | group chatids where @mentions are answered |
| `WECOM_WS_URL` | `wss://openws.work.weixin.qq.com` | for private deployments |
| `WECOM_STREAM_INTERVAL` | `2` | seconds between stream updates (§4.3) |

## 7. One-time setup (goes in the README)

1. Web admin console: **安全与管理 → 管理工具 → 智能机器人 → 创建机器人 → 手动创建** → name → the **API模式创建** link at the very bottom of the form.
2. API 配置 → **使用长连接** and copy the **Bot ID** and **Secret** (the Secret is shown once). Set the visible range (可见范围) to include yourself, then 保存.
3. `.env`: `IM_CHANNELS=wecom` (or with feishu/whatsapp), `WECOM_BOT_ID`, `WECOM_BOT_SECRET`; start `uv run agent-demo-server` (the log shows "WeCom connected").
4. Message the bot in WeCom. The log shows `user_id=…`; put it in `WECOM_ALLOWED_USERS` and restart.
5. Groups: add the bot to a group, then send `@bot /enable` (or put the chatid from the log into `WECOM_ALLOWED_CHATS`).

## 8. Files

| File | Change |
|---|---|
| `backend/agent_demo/channels/wecom.py` | **New**. Pure helpers: `parse_message(body)`, `parse_click(body)`, `approval_args_markdown(request)`, `approval_card(task_id, request)`, `closed_card(task_id, request, outcome)`, `split_bytes(text, limit)`. Then `WeComChannel`: connection client (§3), `send_text`, `send_approval`, `close_approval`, click handling |
| `backend/agent_demo/channels/__init__.py` | `"wecom": "agent_demo.channels.wecom:WeComChannel"` |
| `backend/agent_demo/channels/base.py` | `Stream` protocol + the optional `open_stream` note (§4.2) |
| `backend/agent_demo/channels/hub.py` | Streamed turns when the channel offers `open_stream` (§4.2); the non-streaming path is unchanged |
| `backend/tests/test_im_hub.py` | A `StreamingFakeChannel`: transcript grows per step, approval line, finish on done/error/cancel, overflow to `send_text`, `None` → fallback |
| `backend/pyproject.toml` | `websockets` listed explicitly (today it only comes through `lark-oapi`, pinned `<16`) |
| `.env.example`, `README.md` | WeCom variables and setup section |
| `backend/tests/test_wecom.py` | **New** (§9) |

## 9. Verification

**Offline (`uv run pytest`):**
- **Parsing:** DM text, group text (@name removed, `chat_id = chatid`), voice → text, `mixed` → text parts, image → unsupported, missing fields → None.
- **Click parsing:** a known `task_id` maps to the call id, `y`/`n`; an unknown `task_id` → None.
- **Cards and helpers:** card JSON has both buttons and the `task_id`; the closed card has no buttons; `split_bytes` with Chinese text stays under 4,096 bytes.
- **Fake WeCom server:** a `websockets.serve` on localhost answers subscribe, records pings and sends callbacks. Checks:
  - DM round trip through the ChatHub and FakeLLM: **stream frames** carry the user message's `req_id` and one `stream.id`, the content grows, and the last frame has `finish: true`;
  - throttling: many quick updates → few frames, last one complete;
  - group @mention → streamed reply; `/help` → pushed with `chat_type` 2;
  - approval: card pushed → click event → `respond_update_msg` sent with the click's `req_id` → tool runs;
  - an expired approval → follow-up message;
  - a wrong secret → `start()` raises;
  - a dropped connection → reconnects and subscribes again;
  - `disconnected_event` → no reconnect.
- The existing 151 tests still pass.

**Manual end to end:**
1. DM from a non-allowlisted user → ignored and logged.
2. DB question → one message that shows the progress line, then the answer (streamed).
3. A voice message as a question.
4. "Email me my watchlist" → card: Deny ❌, then Approve ✅ (the card updates in place).
5. Let one approval expire.
6. A group: `@bot` without `/enable` → ignored; `/enable` → answers; another member clicks Approve → rejected.
7. Start a second server → the first logs "took over" and stops.
8. Feishu, WhatsApp and the web UI still work alongside it.

## 10. Review decisions (2026-10-25)

| # | Question | Decision |
|---|---|---|
| 1 | WeCom organisation with admin rights | **Yes**, available |
| 2 | Own client or official SDK | **Own client** (§3) |
| 3 | Streaming replies | **Now** (§4) |

Still to confirm with the real bot during implementation: whether stream refreshes count toward the 30/minute limit (§4.3), how the smart robot's markdown renders code blocks, and the exact card fields accepted by `aibot_respond_update_msg`.

## 11. Implementation notes (2026-10-25)

| Topic | What was built (differences from the draft above) |
|---|---|
| Protocol check | A subscribe with dummy credentials against the real `wss://openws.work.weixin.qq.com` got `{"headers":{"req_id":…},"errcode":853000,"errmsg":"invalid bot_id or secret…"}`. So answers have **no `cmd`**, echo our `req_id`, and the endpoint is reachable from this machine without a proxy. The client relies on exactly this shape |
| Hub streaming | As in §4.2. `ChatSession` gets `open_stream` / `close_stream` / `_emit`. Turn output goes to the stream when there is one; otherwise it takes the old path (buffered progress lines + `say`). If a stream update fails, the rest of the turn falls back to separate messages. Only agent turns stream; commands, Claude Code (`/claude`) and scheduled-task reports still use `send_text` |
| Approval line | The "⏳ Waiting for your approval" line in the stream is replaced in place by ✅ / ❌ / ⌛ |
| Card click | `hub.on_button()` runs first, then `aibot_respond_update_msg` with the click's `req_id`. Accepted → the card shows ✅/❌ for everyone, and `close_approval` then sends nothing. Rejected (another member) → only that member's card shows the notice (`userids: [clicker]`), and the owner's buttons stay. Strangers' clicks get no update |
| Click payload | The docs show `event_key` / `task_id` both nested under `template_card_event` and flat in `event`; the parser accepts both |
| Closed card | `text_notice` with the outcome as `sub_title_text` and no buttons. **To check with the real bot** that this card type is accepted as an update |
| `chat_type` | Remembered per chat from callbacks. An unknown chat (e.g. a scheduled-task report after a restart) is treated as a DM (`chat_type` 1) |
| Start-up | `start()` waits up to 10 s for the first subscribe. A wrong id or secret, or no connection, makes the channel fail to start (a warning in the web header). Later drops reconnect with 1 s → 30 s backoff; `disconnected_event` → no reconnect |
| **First real run** | (1) The server failed to start: `WeComChannel` lacked `routes()` (the offline tests never went through `server.py`); fixed, plus a test that every `Channel` member exists. (2) The connection dropped every 20-40 s with `incorrect masking` / `invalid opcode` / `keepalive ping timeout`: the `websockets` library's own protocol-level pings (default every 20 s) confuse WeCom's server. Now `ping_interval=None`; the JSON `ping` command every 30 s is the only keepalive, and the connection has been stable since |
| Dependency | `websockets>=13,<16` listed explicitly (15.0.1 installed; `lark-oapi` pins `<16`) |
| Claude Code owner | `claude.owner.wecom` in `agent.properties` works without code changes (owners are keyed by channel name) |
| Tests | `test_im_stream.py` (9: hub streaming with a fake streaming channel) and `test_wecom.py` (18: parsing, cards, byte splitting, stream throttling and rollover, and the real client against a fake WeCom WebSocket server: subscribe, streamed DM reply, card click, expiry follow-up, group + wrong clicker, strangers, wrong secret, unreachable, heartbeat + reconnect, taken over). Full suite: 253 passed |

Sources: [智能机器人长连接 (WeCom developer docs)](https://developer.work.weixin.qq.com/document/path/101463), [接收消息](https://developer.work.weixin.qq.com/document/path/100719), [模板卡片类型](https://developer.work.weixin.qq.com/document/path/101839), [@wecom/aibot-node-sdk README](https://cdn.jsdelivr.net/npm/@wecom/aibot-node-sdk@1.0.7/README.md), [wecom-aibot-python-sdk](https://pypistats.com/packages/wecom-aibot-python-sdk).
