# agent-demo

A small, readable AI agent for **learning agent development**. It has:

- a **hand-written agent loop** (no framework)
- **tool calling** on a SQLite database (read-only)
- an **MCP connection** to Gmail (search, read, draft, send…), where every risky action needs your approval
- one **Python core** with two UIs: a **CLI** and a **TypeScript web UI**

Design document: [`../design/agent-demo-design.md`](../design/agent-demo-design.md)

```
agent-demo/
├── .env.example        LLM settings (copy to .env)
├── mcp_servers.json    MCP servers + approval policy
├── backend/            Python core, CLI, web server, tests
└── web/                TypeScript (React + Vite) web UI
```

---

## 1. Setup

You need [uv](https://docs.astral.sh/uv/) (Python) and Node.js 20+. Both are already installed on this machine.

```bash
cd agent-demo
cp .env.example .env            # then put your DeepSeek key in DEEPSEEK_API_KEY
cd backend
uv sync                         # installs Python 3.12 + dependencies
uv run python scripts/seed_db.py   # creates data/demo.db (fake stock data)
uv run pytest                   # 226 offline tests, no API key needed (-m integration: real Feishu/Sina/OpenAI checks)
```

### Other models

DeepSeek is the default. To use another provider, change `.env`:

| `LLM_PROVIDER` | Key variable | Model |
|---|---|---|
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` (default) |
| `qwen` | `DASHSCOPE_API_KEY` | `qwen-plus` (default) |
| `openrouter` | `OPENROUTER_API_KEY` | set `LLM_MODEL` (any model id from openrouter.ai/models) |
| `openai` | `OPENAI_API_KEY` | set `LLM_MODEL` |
| `ollama` | none | set `LLM_MODEL`, e.g. `qwen3:8b` (local) |
| `custom` | `LLM_API_KEY` | set `LLM_BASE_URL` + `LLM_MODEL` |

You can also override for one run: `uv run agent-demo --provider qwen --model qwen-max`.
The model **must support tool calling**.

### Choosing the mailbox: Sina Mail (default) or Gmail

The mailbox is set in `agent.properties` (no secrets in that file):

```properties
mail.provider=sina            # sina | gmail
mail.sina.address=you@sina.com
```

Restart the agent after changing it, or override for one run: `uv run agent-demo --mail gmail` (also `agent-demo-server --mail gmail`).
The CLI banner and the web header show which mailbox is connected.

### Sina Mail (one-time, about 5 minutes)

1. At mail.sina.com.cn open **Settings → client POP/IMAP/SMTP** (客户端POP/IMAP/SMTP) and enable **IMAP/SMTP**.
2. Generate an **authorization code** (授权码) and put it in `agent-demo/.env` as `SINA_AUTH_CODE=` (not your login password).
3. Check the hosts on that page match `agent.properties` (`imap.sina.com:993`, `smtp.sina.com:465`).
4. Optional check: `cd backend && uv run pytest -m integration` logs in read-only.

The Sina tools come from our own IMAP/SMTP MCP server, `backend/mail_mcp/` (it also works for QQ, 163 and other IMAP/SMTP mailboxes: add a server entry with their hosts). Reading never marks mail as read; sending and marking as read ask for approval.

### Gmail (one-time, about 10 minutes)

> The Gmail MCP server (`@gongrzhe/server-gmail-autoauth-mcp`) is a community package that gets OAuth access to your mailbox. Consider testing with a **secondary Gmail account** first.

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and **enable the Gmail API**.
2. **OAuth consent screen**: choose External and add your Gmail address as a **test user**.
3. **Credentials → Create OAuth client ID → Desktop app**, then download the JSON file.
4. Save it as `~/.gmail-mcp/gcp-oauth.keys.json`.
5. Run `npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth`. A browser opens; sign in and allow access.

Without this step, the agent still runs with the database tools only. It shows a warning explaining why Gmail is unavailable.

---

### Feishu bot (optional, about 15 minutes)

Chat with the agent from Feishu. It runs inside `agent-demo-server` over a **persistent connection** (outbound from your machine), so no public URL is needed. Design: [`../design/im_integration.md`](../design/im_integration.md).

1. On https://open.feishu.cn/app create a self-built app, add the **Bot** feature, and put `FEISHU_APP_ID` / `FEISHU_APP_SECRET` in `.env`.
2. **Permissions & Scopes**: direct messages to the bot (`im:message.p2p_msg:readonly`), @mentions in groups (`im:message.group_at_msg:readonly`), send as the bot (`im:message:send_as_bot`).
3. Set `IM_CHANNELS=feishu` in `.env` and start `uv run agent-demo-server` (Feishu checks for a live connection in the next step).
4. **Events & Callbacks**: choose **persistent connection** for both events and callbacks; add the event `im.message.receive_v1` and the callback `card.action.trigger`.
5. **Version Management**: publish a version whose availability includes you.
6. Message the bot (search "AgentDemoBot" in Feishu). With an empty `FEISHU_ALLOWED_USERS` the bot is in **setup mode**: it answers nobody and logs `user_id=ou_…` for each sender. Put your id in `FEISHU_ALLOWED_USERS` and restart.

In chat: progress lines show each tool call, risky actions arrive as an **Approval needed** card (Approve / Deny, or reply yes / no; no answer in 5 minutes = no), and `/new`, `/tools`, `/help` work as in the CLI.
Groups are off until enabled. **Add the bot to a group** yourself (needs the events *Bot added to group* `im.chat.member.bot.added_v1` and *Bot removed from group* `im.chat.member.bot.deleted_v1` in Events & Callbacks), or send **@AgentDemoBot /enable** in a group it is already in; **@AgentDemoBot /disable** or removing the bot turns it off. Enabled groups are saved in `data/im_groups.json`; `FEISHU_ALLOWED_CHATS` in `.env` still works as a fixed list. Only allowlisted users can enable a group, and everyone in an enabled group can read the bot's answers, which may quote your email. In a group, the bot answers only @mentions, but **each @mention comes with the group's recent messages** (last 15 from the last 30 minutes; `IM_GROUP_CONTEXT_MESSAGES` / `IM_GROUP_CONTEXT_MINUTES`, `0` = off), so *@AgentDemoBot any advice?* works after a discussion. For more history the agent calls `chat__recent_messages` (e.g. *summarise the last 100 messages*). Other members' messages are sent to the LLM, wrapped as untrusted data.
Run **one** server at a time: Feishu delivers each event to only one connected instance.

### WhatsApp bot (optional, about 30 minutes)

Chat with the agent from WhatsApp, **direct messages only**. WhatsApp's Cloud API delivers messages to a **webhook**, so Meta must reach `https://…/webhooks/whatsapp` through a tunnel. The server still listens only on 127.0.0.1, and it refuses every forwarded request except `/webhooks/*`, so the web UI and `/ws` stay private. Design: [`../design/whatsapp_integration.md`](../design/whatsapp_integration.md).

1. On [developers.facebook.com](https://developers.facebook.com) create an app (type **Business**) and add **WhatsApp**. Under *API Setup* note the test number's **Phone number ID**, and add and verify your own phone number as a recipient (the free test number can message up to 5 verified numbers).
2. In Business settings create a **System user**, give it the app, and generate a token with `whatsapp_business_messaging` (and `whatsapp_business_management`). The temporary token on the API Setup page only lasts 24 hours.
3. In `.env` set `IM_CHANNELS=whatsapp` (or `feishu,whatsapp`), `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET` (*App settings → Basic*) and a random `WHATSAPP_VERIFY_TOKEN`. If Meta is not reachable directly, set `HTTPS_PROXY`.
4. Start `uv run agent-demo-server`, then a tunnel to port 8000. A quick try: `cloudflared tunnel --url http://127.0.0.1:8000` (prints an `https://….trycloudflare.com` URL that changes on every run). For a fixed URL use a named tunnel whose ingress forwards only the webhook:
   ```yaml
   ingress:
     - hostname: bot.example.com
       path: ^/webhooks/
       service: http://127.0.0.1:8000
     - service: http_status:404
   ```
5. *WhatsApp → Configuration → Webhook*: callback URL `https://<tunnel>/webhooks/whatsapp`, the verify token, **Verify and save**; then subscribe to the **messages** field.
6. Message the test number from your phone. With an empty `WHATSAPP_ALLOWED_NUMBERS` the bot is in **setup mode** and logs `user_id=<number>`; put it in `WHATSAPP_ALLOWED_NUMBERS` and restart.

Approvals come as **Approve / Deny** buttons (or answer *yes* / *no*); because WhatsApp messages cannot be edited, the result is a follow-up "✅ Approved" message. WhatsApp only allows free-form replies within 24 hours of your last message, which is always the case when you start the conversation.

### Switching the model (no restart)

In any chat (CLI, web UI, Feishu) type:

| Command | Effect |
|---|---|
| `/strongmodel` | This conversation uses ChatGPT (`model.strong.*` in `agent.properties`, default `openai / gpt-5.5`; key `OPENAI_API_KEY` in `.env`) |
| `/weakmodel` | Back to the model from `.env` (here Qwen `qwen-plus` on DashScope) |
| `/model` | Show the current model |

The switch is per conversation; `/new` goes back to `model.default`. Every command re-reads `.env` and `agent.properties`, so a new key or model name works at once. With the strong model, the conversation (including email content) is sent to OpenAI.

### Claude Code (`/claude`)

Work on files and code goes to your Claude Code through the Claude Agent SDK; everything else goes to the assistant (Qwen/GPT).

| Command | Effect |
|---|---|
| `/claude <request>` | This one message goes to Claude Code |
| `/claude on` / `/claude off` | All following messages go to Claude Code / back to the assistant |
| `/claude new`, `/claude folder <path>`, `/claude status`, `/claude stop` | Fresh session, working folder, status, stop a run |

Setup: run `claude setup-token` and put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN` (or use `ANTHROPIC_API_KEY`); set `claude.folders` and `claude.owner.feishu` in `agent.properties`. Only the owner can use it (CLI, local web UI, the owner's Feishu DM). Claude Code runs inside a **bwrap sandbox** (`claude.sandbox=on`): only `claude.folders` (here `agent-demo/data`) and read-only system folders exist for it — not your code, `.env` or home folders — and none of your `.env` keys are in its environment. Our policy also refuses file tools outside the folders. Shell commands and web access follow `claude.approval`: `auto` (default) runs them without asking except dangerous ones (`sudo`, `rm -rf`, `git push`, `curl | sh`, …), `ask` asks for everything but read-only commands, `full` never asks. After each answer the assistant gets a short note of what Claude Code did, so you can ask it follow-up questions. Design: [`../design/claude_code_delegation.md`](../design/claude_code_delegation.md).

### Scheduled tasks

Ask in your own words, e.g. *"add a task: query FTNT every day and report to me at 9 PM"* — to the assistant or with `/claude`. You approve the task (the card shows the next 3 run times), and every day at that time the report arrives as `⏰ FTNT daily (task #1)` where you asked: a Feishu task in that Feishu chat, a web UI task in the web UI (open tabs; if none is open, it waits and appears when you open the UI). Tasks created in the CLI report to your Feishu DM (message the bot there once so it knows the chat).

| Command | Effect |
|---|---|
| `/tasks` | List tasks with next run and last result |
| `/tasks run <id>` · `pause` · `resume` · `remove` | Manage a task |

Tasks run inside `agent-demo-server`, so it must be running at that time (a run missed by less than an hour is caught up once). Settings: `tasks.*` in `agent.properties` (time zone `America/Los_Angeles`, at most 20 tasks, at least 15 minutes apart). Real prices come from the new `stock_quote` tool (Yahoo Finance's public endpoint); the database itself is demo data. Owner only. Design: [`../design/scheduled_tasks.md`](../design/scheduled_tasks.md).

### Telegram bot (optional, about 5 minutes)

Like Feishu, it polls Telegram over an outbound connection: no public URL. Design: [`../design/telegram_integration.md`](../design/telegram_integration.md).

1. In Telegram open **@BotFather** (https://t.me/BotFather, the one with the blue checkmark) → `/newbot` → put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Optional, for group context: BotFather `/setprivacy` → your bot → **Disable** (then re-add the bot to groups).
3. Add `telegram` to `IM_CHANNELS`, start `agent-demo-server`, open your bot, press **Start**, send "hi". The bot is in setup mode and logs `user_id=…`.
4. Put that id in `TELEGRAM_ALLOWED_USERS` (and `claude.owner.telegram` in `agent.properties` for `/claude` and scheduled tasks), restart.

Groups work as in Feishu: add the bot (or `@bot /enable`), then @mention it.

## 2. Run

**CLI**

```bash
cd backend
uv run agent-demo
```

Commands: `/tools` (list tools and their approval policy), `/new` (clear the conversation), `/exit`.

**Web UI (development)**, in two terminals:

```bash
cd backend && uv run agent-demo-server       # API on http://127.0.0.1:8000
cd web && npm install && npm run dev         # open http://localhost:5173
```

**Web UI (single server)**: run `cd web && npm run build` once. After that, `uv run agent-demo-server` also serves the UI at http://127.0.0.1:8000.

The server listens on **localhost only**, because it has no login and the agent can send email.

### Things to try

- *Which tables are in the database?*
- *Which TSX stocks in my watchlist rose the most over the last 20 trading days?*
- *Summarise my trades by symbol: total bought, sold and average price.*
- *Find my last 3 emails from GitHub and summarise them.* (Gmail)
- *Email me (my-address@gmail.com) a table of my watchlist with the latest close prices.* An approval prompt appears; try both **Deny** and **Approve**.

---

## 3. How to read the code (learning path)

Read the files in this order. Each one is short.

| # | File | What to learn |
|---|---|---|
| 1 | `backend/agent_demo/llm.py` | Calling an OpenAI-compatible API; what a tool call looks like |
| 2 | `backend/agent_demo/tools.py` | A tool = name + description + JSON Schema + handler |
| 3 | `backend/agent_demo/db_tools.py` | Designing safe tools: read-only, validation, limits, helpful errors |
| 4 | **`backend/agent_demo/agent.py`** | **The agent loop**: the heart of every agent |
| 5 | `backend/tests/test_agent.py` | Testing an agent with a scripted fake LLM (no API cost) |
| 6 | `backend/agent_demo/mcp_client.py` | MCP: start a server, handshake, discover tools, call tools |
| 7 | `backend/tests/fake_mcp_server.py` | The *server* side of MCP, in about 15 lines |
| 8 | `backend/agent_demo/events.py` | Separating the core from the UI with events |
| 9 | `backend/agent_demo/cli.py` | One UI: print events, ask approval with y/N |
| 10 | `backend/agent_demo/server.py` + `web/src/useAgent.ts` | The other UI: the same events over a WebSocket, async approvals |

### Exercises (next steps)

1. **Streaming**: show the answer word by word (`stream=True` in `llm.py`, plus a new `assistant_delta` event).
2. **Parallel tools**: run read-only tool calls at the same time with `asyncio.gather`.
3. **Persistent history**: save conversations to SQLite and add a `/resume` command.
4. **Context limit**: when the history gets long, summarise old messages (compaction).
5. **Memory**: add a `MEMORY.md` file plus a `memory` tool, loaded into the system prompt.
6. **Your own MCP server**: turn the DB tools into an MCP server (see `fake_mcp_server.py`) and connect it through `mcp_servers.json`.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Config error: No API key found` | Put your key in `agent-demo/.env` |
| `LLM call failed: 401` | Wrong key, or wrong provider for this key |
| Gmail warning `OAuth keys file not found` | Do the Gmail setup above |
| Gmail warning about timeout on first run | `npx` was downloading the package; start again |
| Model answers without using tools | Use a stronger model that supports tool calling |
| MCP server details | See `agent-demo/logs/mcp-<server>.log` |
