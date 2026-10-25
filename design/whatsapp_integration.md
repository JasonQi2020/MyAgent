# Design: WhatsApp channel (direct messages only)

> Status: **implemented, offline-tested** (2026-10-24); manual end-to-end with a real Meta app still to do. Differences from the draft are in §11.
> §12 (WhatsApp with `/claude` and scheduled tasks; the 24-hour rule) is a **draft for review** (2026-10-24).
> Companion to [`im_integration.md`](im_integration.md) (channel interface, ChatHub, Feishu). This document fills in its §9 "Adding WhatsApp later".

## Context

The IM layer was built so that a second IM only adds one channel module: `ChatHub` (allowlist, sessions, approvals, commands) is platform-agnostic, and `server.py` already mounts webhook routes from `channel.routes()`. WhatsApp is added through Meta's **WhatsApp Business Cloud API**, for **direct messages only**: Cloud API groups need an Official Business Account, are limited to 8 participants, are created by the business (the bot can't be added to an existing group) and allow no interactive buttons.

Main differences from Feishu: a **public webhook** (via a tunnel) instead of a long connection, **signed requests**, no official Python SDK (plain `httpx`), **messages can't be edited** (approval outcomes are follow-up messages), reply buttons with tight size limits, WhatsApp text markup, and the **24-hour customer-service window**.

---

## 1. Decisions
| Decision | Choice |
|---|---|
| API | **WhatsApp Business Cloud API** (official). No unofficial WhatsApp Web bridge |
| Chats | **Direct messages only**; the channel always reports `is_group=False`, so §11 group features don't apply |
| Process | Inside `agent-demo-server`, like Feishu: `IM_CHANNELS=feishu,whatsapp` |
| Receiving | **Webhook** mounted with the existing `channel.routes()` hook (`server.py:64-67`), reached through a tunnel |
| HTTP client | `httpx.AsyncClient` (no official Python SDK); `httpx` moves from dev to main dependencies |
| Unchanged | `hub.py`, `agent.py`, `runtime.py`, `base.py` types |

## 2. Webhook (`GET`/`POST /webhooks/whatsapp`)
- **GET verify:** if `hub.mode == "subscribe"` and `hub.verify_token == WHATSAPP_VERIFY_TOKEN`, return `hub.challenge` as plain text; otherwise 403.
- **POST:** read the raw body and check `X-Hub-Signature-256 == "sha256=" + HMAC_SHA256(WHATSAPP_APP_SECRET, body)` with `hmac.compare_digest`; on a mismatch return 401 and log it. Then parse, start `hub.on_message` / button handling as tasks, and **return 200 at once** (Meta retries non-200 responses, and the hub's dedupe absorbs retries).
- Parsing is a pure function, `parse_webhook(payload, phone_number_id) -> list[IncomingMessage | ButtonPress]`:
  - walks `entry[].changes[].value`;
  - skips values whose `metadata.phone_number_id` isn't ours;
  - **ignores `statuses`** (sent/delivered/read);
  - `type == "text"` → `IncomingMessage(chat_id=user_id=from, message_id=id, text=text.body, is_group=False)`;
  - `type == "interactive"` with `button_reply.id` `ap:<call_id>:y|n` → `ButtonPress`;
  - anything else (image, **audio/voice**, sticker, location…) → `unsupported=True`.

## 3. Sending (Graph API `POST {base}/{version}/{phone_number_id}/messages`, Bearer token)
| Need | WhatsApp |
|---|---|
| Text | `type: text`. Markdown is converted by a new `render.to_whatsapp()` inside `send_text`, which keeps the hub untouched: `**b**`→`*b*`, `*i*`/`_i_`→`_i_`, `~~s~~`→`~s~`, `# H`→`*H*`, `[t](u)`→`t (u)`, code blocks left as they are. `limits = ChannelLimits(max_text_chars=4000, supports_buttons=True)` (4096 minus a margin for the conversion) |
| Approval | Reply-button message with **Approve** / **Deny**, ids `ap:<call_id>:y` / `ap:<call_id>:n` (≤ 256 chars). The body (≤ 1024) shows the tool name and arguments. If the arguments don't fit, the full arguments are first sent as text (split normally), then the buttons with the body "Approve the action above? (tool)". The handle is the button message's `wamid` |
| Close approval | Messages can't be edited, so it sends "✅ Approved / ❌ Denied / ⌛ Expired" quoting the approval (`context.message_id = handle`). Old buttons stay tappable; the hub answers "already answered or expired" |
| Button notice | No toasts. The text returned by `hub.on_button()` is sent back as a message **only when the press was rejected** (the call_id was not an open approval of this channel); a successful press is confirmed by `close_approval` |
| Errors | A non-2xx response raises `RuntimeError(code, message)` (the hub logs it). Error 131047 (24-hour window closed) gets a clear log line |

## 4. Platform rules that matter
- **24-hour window:** free-form messages are only allowed within 24h of the user's last message. The bot only replies, so that's fine; no templates are needed.
- **Test number:** Meta's free test number can only message up to 5 recipient numbers verified in the console.
- **Allowlist:** `WHATSAPP_ALLOWED_NUMBERS`, phone numbers normalised to digits (no `+`, spaces or dashes), matching `wa_id`. Empty means **setup mode** (existing hub behaviour: logs `user_id=…`, answers nobody).

## 5. Security
| Risk | Control |
|---|---|
| Forged webhook calls | HMAC signature check on every POST; verify token on GET |
| The tunnel exposes `/ws` (no login, can send email) | (a) The cloudflared ingress forwards only `^/webhooks/whatsapp$` and returns 404 for everything else. (b) **Defence in depth in `server.py`:** a small ASGI middleware refuses any HTTP/WebSocket request that came through a proxy (`Cf-Connecting-Ip` or `X-Forwarded-For` present) unless its path belongs to a channel route. This is a deliberate small change to `server.py`, beyond what §9 of `im_integration.md` promised |
| Strangers messaging the number | Allowlist (as Feishu); approvals only from the session owner |
| Secrets | Token, app secret and verify token live in `.env` only and are never logged |

## 6. Configuration (`.env` / `.env.example`)
| Variable | Default | Notes |
|---|---|---|
| `IM_CHANNELS` | — | add `whatsapp` |
| `WHATSAPP_TOKEN` | required | System User permanent token (the console's temporary token expires after 24h) |
| `WHATSAPP_PHONE_NUMBER_ID` | required | from WhatsApp → API Setup |
| `WHATSAPP_APP_SECRET` | required | App settings → Basic; used for the signature check |
| `WHATSAPP_VERIFY_TOKEN` | required | any random string, also entered in the console |
| `WHATSAPP_ALLOWED_NUMBERS` | empty = setup mode | comma-separated |
| `WHATSAPP_API_VERSION` | current Graph version, checked during implementation | |
| `WHATSAPP_API_BASE` | `https://graph.facebook.com` | |
| `HTTPS_PROXY` | — | honoured by `httpx` if Meta isn't directly reachable |

## 7. One-time setup (goes in the README)
1. developers.facebook.com → create an app (Business) → add **WhatsApp** → note the test number's `phone_number_id`, and add and verify your own number as a recipient.
2. Business settings → System user → generate a token with `whatsapp_business_messaging` (and `whatsapp_business_management`).
3. Start the server and the tunnel: `cloudflared tunnel --config …` with path-restricted ingress (or a quick tunnel for a first try).
4. WhatsApp → Configuration → Webhook: URL `https://<tunnel>/webhooks/whatsapp`, verify token, **Verify and save**; subscribe to the `messages` field.
5. Message the test number from your phone. The log shows `user_id=…`; put it in `WHATSAPP_ALLOWED_NUMBERS` and restart.

## 8. Files (implementation stage)
| File | Change |
|---|---|
| `backend/agent_demo/channels/whatsapp.py` | **New**: `WhatsAppChannel` (`from_env`, `start` checks the token with `GET /{phone_number_id}`, `routes()`, `send_text`, `send_approval`, `close_approval`), `parse_webhook`, `verify_signature`, `approval_messages` builders. Modelled on `feishu.py`: pure helpers on top, class below |
| `backend/agent_demo/channels/__init__.py` | `"whatsapp": "agent_demo.channels.whatsapp:WhatsAppChannel"` |
| `backend/agent_demo/channels/render.py` | `to_whatsapp(text)` (pure) |
| `backend/agent_demo/server.py` | proxied-request guard middleware (§5b) |
| `backend/pyproject.toml` | `httpx` to `dependencies` |
| `.env.example`, `README.md` | WhatsApp variables, setup section, test count |
| `backend/tests/test_whatsapp.py` | **New** (see Verification) |

Reused as they are: `ChatHub` (`hub.py`: allowlist, setup mode, sessions, yes/no fallback, timeouts), `render.to_chat`/`short_args`, `ConfigError`, `_ids`-style parsing from `feishu.py`.

## 9. Verification
**Offline (`uv run pytest`):**
- `parse_webhook`: text, button reply, status-only payload, voice/image → unsupported, other `phone_number_id` ignored, malformed payload.
- Signature: valid, wrong secret, missing header.
- GET verify: right token, wrong token.
- `to_whatsapp` conversions with code blocks left alone.
- Approval builders: short arguments → one message; long arguments → text + buttons; ids under 256 chars.
- Sending with `httpx.MockTransport`: correct URL, headers and body; an API error raises.
- `close_approval` quotes the handle.
- A rejected button press sends a notice.
- Route through FastAPI `TestClient` + a hub with `FakeLLM`: DM → answer; approval → button press → approved.
- Server guard: a forwarded request to `/ws` is refused, to `/webhooks/whatsapp` it's allowed, and a local request to `/ws` is allowed.
- `build_channels` error for missing variables.
- The existing 100+ tests still pass.

**Manual end to end:** follow setup §7. Then:
1. A DM from a non-allowlisted number is ignored and logged.
2. DB question → progress line + answer.
3. "Email me my watchlist" → buttons: Deny → ❌, then Approve → ✅ and the email arrives.
4. Let one approval expire → ⌛.
5. A voice note → "text only".
6. Feishu and the web UI still work at the same time.
7. `curl` through the tunnel to `/ws` → refused.

## 10. Open questions for review
1. Tunnel: cloudflared (default), ngrok, or your own public server?
2. Is `graph.facebook.com` reachable directly, or do we need `HTTPS_PROXY`?
3. Is the test number enough, or will you register a real business number?

---

## 11. Implementation notes (2026-10-24)

| Topic | What was built (differences from the draft above) |
|---|---|
| Route order | **Bug found during implementation:** `server.py` mounts the built web UI at `/` at import time, and routes added later in the lifespan came *after* it, so `POST /webhooks/whatsapp` got 405. New `server.include_first()` puts channel routes ahead of the mount |
| Tunnel guard | `server.TunnelGuard` (pure ASGI middleware): a request carrying `Cf-Connecting-Ip`, `X-Forwarded-For` or `Forwarded` may only reach `/webhooks/*`. HTTP gets 403; WebSocket gets close code 1008. The Vite dev proxy sends none of these headers (`xfwd` is off), so `npm run dev` still works |
| Graph API version | Default `v25.0` (Feb 2026; v26.0 is the newest). Set with `WHATSAPP_API_VERSION` |
| Network | `graph.facebook.com` is reachable directly from this machine; no proxy needed (`HTTPS_PROXY` still honoured by httpx) |
| Approval arguments | Shown in full in the buttons message when that fits in 1,024 characters; otherwise a separate text message with the arguments (cut at 3,500 characters, like Feishu), then the buttons |
| Button notices | A press on an approval this channel still has open gets no extra message (the follow-up "✅ Approved" confirms it). A press on an old one gets the hub's "already answered or expired". Strangers get no answer at all, for messages and button presses alike |
| Markup | `render.to_whatsapp()`: italics are converted before bold (WhatsApp bold is a single `*`), and headings last (a bold line); fenced and inline code are left alone |
| Start-up check | `start()` reads `GET /{phone_number_id}` (number and verified name go to the log). A bad token or id makes the channel fail to start; it becomes a warning, and the rest of the server runs as usual |
| Not done | No dedupe for button presses (Meta only redelivers when we don't answer 200 quickly, and we answer at once); no read receipts or typing indicator |
| Tests | `tests/test_whatsapp.py` (26): parsing, signature, verification route, markup, approval messages, sending via `httpx.MockTransport`, a full DM + button approval round trip with the ChatHub and FakeLLM, strangers ignored, tunnel guard, route order. Full suite: 151 passed |

---

## 12. WhatsApp with `/claude` and scheduled tasks (draft for review, 2026-10-24)

Since this channel was built, the assistant gained `/claude` (Claude Code, owner only) and scheduled tasks (owner only, reports sent by the bot at a set time). Two gaps follow. Decisions (user, 2026-10-24): hold reports when WhatsApp refuses them; the owner's WhatsApp number is an owner like the Feishu one; tunnel = cloudflared quick tunnel (installed: `~/.local/bin/cloudflared` 2026.10.3, official GitHub release).

### 12.1 The 24-hour rule and bot-initiated messages

§4 assumed "the bot only replies". A scheduled report is **sent by the bot on its own**: if the owner has not written to the bot in the last 24 hours, Meta refuses it (error `131047`) and today the report is logged and lost.

| Topic | Design |
|---|---|
| Signal | New exception `base.WindowClosed` ("the platform refuses messages the bot starts until the user writes"). `WhatsAppChannel._post` raises it for `131047` instead of a plain `RuntimeError`. Channel-agnostic, so another IM with a similar rule can use it |
| Holding | `ChatHub.run_task`: when delivering the report raises `WindowClosed`, the report and its follow-up note are stored in `state/held_reports.json` under (channel, chat_id); the task's `last_status` becomes `held: WhatsApp 24-hour window closed` |
| Delivering | The next message from that chat first gets the held reports (oldest first, each with its `⏰` header and a line *"held since 21:00 because WhatsApp only allows replies within 24 hours of your last message"*), then the message is handled as usual. A button press also counts as a message |
| Approvals during such a run | Sending the approval message also fails with `WindowClosed` → the approval is treated as **no** (already the rule when an approval cannot be asked); the run continues without that action |
| Warning at creation | `ChannelLimits` gets `reply_window_hours` (`24` for WhatsApp, `None` for Feishu). `schedule__add`'s preview for such a chat adds: *"WhatsApp only delivers bot messages within 24 h of your last message; otherwise the report waits until you next write."* |
| Limits | At most 20 held reports per chat (the oldest are dropped, with a count) |

Only the owner can have tasks (tasks are owner-only), so this only concerns the owner's DM.

### 12.2 The owner in WhatsApp

- `agent.properties`: **`claude.owner.whatsapp=<wa_id>`** — the owner's WhatsApp id (digits, e.g. `15551234567`), learned from the log after the first message (setup mode logs `user_id=…`). The existing `claude_code.available(owners, channel, …)` already looks owners up per channel; no code change for `/claude`.
- Scheduled tasks: tasks created in the owner's WhatsApp DM report there (the hub's existing `chat_session`/`run_task`), subject to §12.1. The owner's WhatsApp DM is remembered like the Feishu one (`state/owner_chats.json`), but CLI tasks keep reporting to Feishu.
- `WHATSAPP_ALLOWED_NUMBERS` must contain the same number (owners are always allowlisted users).

### 12.3 Tunnel

- Quick tunnel: `cloudflared tunnel --url http://127.0.0.1:8000`. The printed `https://<random>.trycloudflare.com` URL changes on every start, so the webhook URL in Meta's console must be updated after each restart of the tunnel (a named tunnel with a free Cloudflare account gives a fixed URL later).
- Only `/webhooks/whatsapp` is reachable through it: `server.TunnelGuard` refuses anything else that arrives with proxy headers (§11).

### 12.4 Tests

- `WindowClosed` raised for `131047`; other errors unchanged.
- Hub with a fake channel that raises `WindowClosed`: the report is held (state file, task status), delivered first on the next message, the 20-report cap; approval during a closed window = no.
- Preview warning for channels with `reply_window_hours`.
- Owner: `/claude` and `/tasks` in a WhatsApp DM with `claude.owner.whatsapp` set; refused for other numbers.
- Manual: real Meta test number through the tunnel — message → answer; approval buttons; `/tasks run <id>` in WhatsApp; a held report (by testing with an expired window is impractical; covered by the offline tests).

### 12.5 Files

| File | Change |
|---|---|
| `channels/base.py` | `WindowClosed`; `ChannelLimits.reply_window_hours` |
| `channels/whatsapp.py` | raise `WindowClosed` for `131047`; `reply_window_hours=24` |
| `channels/hub.py` | hold/deliver reports; deliver held reports before handling a message |
| `schedule_tools.py` | the creation warning |
| `agent.properties`, `README.md` | `claude.owner.whatsapp`; WhatsApp setup + tunnel steps |
