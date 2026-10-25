# Design: Telegram channel

> Status: **implemented, offline-tested, running** (2026-10-24); manual end-to-end with the owner in progress. Notes in §9.
> Companion to [`im_integration.md`](im_integration.md) (channel interface, ChatHub, groups §11–12), [`whatsapp_integration.md`](whatsapp_integration.md) (paused), [`claude_code_delegation.md`](claude_code_delegation.md) and [`scheduled_tasks.md`](scheduled_tasks.md).

## Context

WhatsApp is paused (it needs a Meta app, a public tunnel, and has a 24-hour rule). Telegram is added instead, as a third channel on the same `ChatHub` — like Feishu and WhatsApp it is one module, `channels/telegram.py`, plus one line in `CHANNELS`.

| Decision (user, 2026-10-24) | Choice |
|---|---|
| API | Telegram **Bot API** over HTTPS with `httpx` (as WhatsApp; no SDK). `api.telegram.org` is reachable from this machine (checked) |
| Receiving | **Long polling** (`getUpdates`): outbound only, **no public URL, no tunnel** — like Feishu's long connection |
| Chats | **Direct messages + groups when the bot is @mentioned**, with the Feishu group features: allowlisted users only; groups enabled by an allowlisted user adding the bot or `/enable` (IM §11); recent group messages attached as context (IM §12.9) |
| Owner | The owner's Telegram account is an owner (`claude.owner.telegram`): `/claude` and scheduled tasks work in that DM; tasks created there report there |
| Process | Inside `agent-demo-server`: `IM_CHANNELS=feishu,telegram` |

Telegram has **no 24-hour rule**: once a user has started a chat with the bot, the bot may message them at any time, so scheduled reports are simply sent.

---

## 1. Receiving (`getUpdates` long polling)

- A background task calls `getUpdates(offset, timeout=50, allowed_updates=["message", "callback_query", "my_chat_member"])` in a loop; `offset` = last `update_id` + 1, so every update is handled once. Network errors back off (1 s → 30 s) and retry.
- At start: `getMe` (the bot's id and `@username`, for mentions) and `deleteWebhook` (polling does not work while a webhook is set).
- **One instance only:** Telegram answers `409 Conflict` when two programs poll the same bot; the channel logs it clearly and keeps retrying (same rule as Feishu: run one server).

### 1.1 Messages → `IncomingMessage`

| Telegram | Normalised |
|---|---|
| `chat.type` `private` | DM; `chat_id` = chat id; `mentioned_bot=True` |
| `chat.type` `group` / `supergroup` | group; `mentioned_bot` = a `mention` entity `@<bot username>`, a `text_mention` of the bot, a command addressed to the bot (`/tasks@AgentDemoBot`), or a reply to one of the bot's messages |
| `from.id` | `user_id` (digits as text); messages `from.is_bot` are ignored |
| `text` | the text, with `@<bot username>` removed and `/cmd@<bot username>` turned into `/cmd` |
| photos, files, stickers, voice … | `unsupported=True` (the hub answers "text only") |
| `channel` posts, edited messages | ignored |

### 1.2 Button presses → `ButtonPress`

`callback_query`: `from.id`, `message.chat.id`, and `data` = `a:<call_id>:1` / `a:<call_id>:0` (Telegram limits `callback_data` to 64 bytes; longer ids are refused when the card is built, as on WhatsApp). Every press is answered with `answerCallbackQuery` (the hub's notice as a small toast), otherwise the Telegram client shows a spinner.

### 1.3 Groups

| Telegram | Hub |
|---|---|
| `my_chat_member`: the bot became `member`/`administrator` | `GroupEvent(bot_added)`, `operator_id` = `from.id` (who added it) |
| the bot became `left`/`kicked` | `GroupEvent(bot_removed)` |
| Recent messages (§12.9 context, `chat__recent_messages`) | Telegram has **no history API for bots**. The channel keeps the last 200 messages it has received per group in memory and serves `history()` from them (lost on restart) |

**Privacy mode:** by default a Telegram bot in a group only receives messages that mention it or reply to it, so the context buffer would only hold those. For the group-context feature, turn privacy mode **off** in BotFather (`/setprivacy` → Disable); the hub still answers only @mentions, and only in enabled groups. With privacy mode on, everything works except that the context is limited to messages addressed to the bot.

## 2. Sending

| Need | Bot API |
|---|---|
| Text | `sendMessage(chat_id, text, parse_mode="HTML", reply_parameters={message_id} in groups)`; limit 4,096 characters → `max_text_chars=4000` |
| Formatting | New `render.to_telegram_html()`: escape `<>&`, then `**bold**` → `<b>`, `*italic*`/`_italic_` → `<i>`, `` `code` `` → `<code>`, fenced blocks (incl. tables, already turned into code blocks) → `<pre>`, `[text](url)` → `<a href>`, headings → bold line. If Telegram rejects the HTML (`can't parse entities`), the message is resent as plain text |
| Approval | `sendMessage` with the tool name and full arguments (`<pre>`, cut at 3,500 characters with a note) and an inline keyboard `[[Approve, Deny]]`; returns the message id as the handle |
| Approval outcome | `editMessageText` of that message: arguments kept, buttons removed, header `✅ Approved` / `❌ Denied` / `⌛ Expired` (Telegram can edit messages, unlike WhatsApp) |

## 3. Security

| Risk | Control |
|---|---|
| Anyone can find a Telegram bot and message it | **Allowlist** `TELEGRAM_ALLOWED_USERS` (user ids); empty = **setup mode** (answers nobody, logs `user_id=…`), as Feishu |
| Groups | Opt-in per group (IM §11): `TELEGRAM_ALLOWED_CHATS` and/or `/enable`; answers only to @mentions from allowlisted users |
| Bot token | `.env` only (`TELEGRAM_BOT_TOKEN`); it appears in every API URL, so URLs are **never logged** (errors are logged without the URL) |
| Network | Outbound only; nothing listens. `HTTPS_PROXY` honoured by httpx if needed |
| Group messages in the context buffer | In memory only, per enabled group, last 200; sent to the LLM only with an @mention (as Feishu) and wrapped as untrusted |

## 4. Configuration

`.env` (mirrored in `.env.example`):

| Variable | Notes |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather (`/newbot`) |
| `TELEGRAM_ALLOWED_USERS` | Comma-separated user ids; empty = setup mode |
| `TELEGRAM_ALLOWED_CHATS` | Group chat ids (negative numbers) always enabled; others via adding the bot / `/enable` |
| `IM_CHANNELS` | add `telegram` |

`agent.properties`: `claude.owner.telegram=<your user id>` (learned from the log after the first message).

## 5. One-time setup (goes in the README)

1. In Telegram, open **@BotFather** → `/newbot` → choose a name and a username ending in `bot` → copy the **token** into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Optional, for group context: `/setprivacy` → choose the bot → **Disable**.
3. Add `telegram` to `IM_CHANNELS`, start `agent-demo-server`, open the bot in Telegram, press **Start** and send "hi".
4. Copy `user_id=…` from the log into `TELEGRAM_ALLOWED_USERS` and `claude.owner.telegram`, restart.

## 6. Files

| File | Change |
|---|---|
| `backend/agent_demo/channels/telegram.py` | **New**: `TelegramChannel` (polling loop, parsing, sending, approvals, group buffer + `history()`), pure `parse_update()` |
| `backend/agent_demo/channels/__init__.py` | `"telegram": "agent_demo.channels.telegram:TelegramChannel"` |
| `backend/agent_demo/channels/render.py` | `to_telegram_html()` |
| `.env.example`, `agent.properties`, `README.md` | settings, setup |
| `backend/tests/test_telegram.py` | **New** |

No changes expected in `hub.py`, `agent.py` or `server.py`: the channel interface, group events, `history()` and the owner settings already cover Telegram.

## 7. Tests

- `parse_update`: DM text; group with `@bot`, `/cmd@bot`, reply to the bot, no mention; bot senders ignored; photo → unsupported; `my_chat_member` added/removed → `GroupEvent`; `callback_query` → `ButtonPress`; malformed updates ignored.
- `to_telegram_html`: escaping, bold/italic/code/pre/links/headings, tables, Chinese text; fallback to plain text when Telegram rejects the HTML.
- Sending with `httpx.MockTransport`: text (HTML, reply in groups, split at 4,000), approval keyboard (callback data ≤ 64 bytes), outcome edit, `answerCallbackQuery`; the token never appears in log records.
- Polling loop with a fake API: offsets advance, updates handled once, backoff on errors, `409` logged.
- Hub round trip with FakeLLM: DM question → answer; approval by button; group @mention with context from the buffer; setup mode; strangers ignored.
- Owner: `/claude` and `/tasks` in the owner's Telegram DM; task report delivered to Telegram.
- Integration (`-m integration`, opt-in): `getMe` with the real token.
- Manual: DM question, approval buttons, a group with the bot (`/enable`, @mention, "summarise the discussion"), `/tasks run <id>` from Telegram.

## 8. Not in scope (v1)

- Webhook mode (long polling is enough for one server).
- Voice/photo understanding, files.
- Persisting the group buffer across restarts.

## 9. Implementation notes (2026-10-24)

| Topic | What was built / found |
|---|---|
| Poll loop could spin | The first test run hung: a fake API that answers `getUpdates` at once made the loop never yield to the event loop. Real Telegram holds empty polls for 50 s, but a proxy could answer at once too, so the loop now always yields (`sleep(0)`, and 0.5 s after an empty answer) |
| Parameter clash | `getUpdates` has a `timeout` parameter; the channel's own HTTP timeout argument is named `http_timeout` |
| Token in logs | The token is part of every API URL. httpx logs request URLs at INFO, so a log filter blanks the token; our own errors never contain the URL (network errors report only the exception type). Checked: the token appears 0 times in the server log |
| Group buffer | Also holds the bot's own answers (so "what did you say?" works); messages older than the hub's context window (30 min) are not attached |
| Bot | @MyAgent2026DecBot ("MyAgentBot"); privacy mode was still on at setup, so group context only holds messages addressed to the bot until it is disabled in BotFather |
| Owner | `TELEGRAM_ALLOWED_USERS=8070391224`, `claude.owner.telegram=8070391224` (from the setup-mode log after the owner's first "hi") |
| Tests | `tests/test_telegram.py` (15: parsing, mentions, buttons, group events, callback limit, HTML, token redaction, polling offsets, HTML fallback, approval card + edit, hub round trip with button approval, strangers/setup mode, group context from the buffer, owner tools) + `tests/test_telegram_integration.py` (real `getMe`). Full suite: 226 passed |
