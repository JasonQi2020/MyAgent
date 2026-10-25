# Design: Choosing the Mail Provider (Sina Mail or Gmail)

> Status: **implemented** (2026-10-24). Notes on what changed during implementation are in §10.
> Companion to [`agent-demo-design.md`](agent-demo-design.md) (§6.3 MCP tools, §7 approvals).

## Context

Today the agent is wired to **Gmail** only: `mcp_servers.json` starts the Gmail MCP server, and the system prompt in `agent.py` names the `gmail__` tools. The user wants to **choose** the mailbox, with **Sina Mail** as the default:

- Sina works from a mainland-China VM (Gmail does not), has no 7-day token expiry, and is the user's main address.
- Gmail stays available, already set up and tested (`~/.gmail-mcp/`).
- The choice lives in a **property file**, so switching needs no code change.

The agent core does not change: it still starts whatever MCP server is selected and discovers its tools at runtime (§6.3 of the main design).

| Decision | Choice |
|---|---|
| Providers | `sina` (default) and `gmail`; one active at a time |
| Where the choice lives | `agent.properties` in the project root (new, no secrets) |
| Secrets | Stay in `.env` (git-ignored): `SINA_AUTH_CODE` |
| Sina access | IMAP (read) + SMTP (send) with Sina's **authorization code (授权码)** |
| Sina MCP server | **Our own generic IMAP/SMTP MCP server in Python** (§4.2, decided 2026-10-24). It works for Sina and any other IMAP/SMTP provider (QQ, 163, Aliyun, …) through host settings |

---

## 1. The property file: `agent.properties`

Plain `key=value` lines, `#` comments, no sections (Java-style properties). Non-secret, so it can be committed and shared.

```properties
# ---- Mail -------------------------------------------------------------------
# Which mailbox the agent uses: sina | gmail
mail.provider=sina

# Sina Mail (IMAP/SMTP). The authorization code goes in .env as SINA_AUTH_CODE.
mail.sina.address=jasonqi8@sina.com
mail.sina.imap_host=imap.sina.com
mail.sina.imap_port=993
mail.sina.smtp_host=smtp.sina.com
mail.sina.smtp_port=465

# Gmail (OAuth files in ~/.gmail-mcp/). The address is optional and only used in the prompt.
mail.gmail.address=
```

| Rule | Behaviour |
|---|---|
| File missing | Defaults apply: `mail.provider=sina` and the Sina hosts above |
| `mail.provider` missing or empty | `sina` |
| Unknown provider (e.g. `mail.provider=qq`) | `ConfigError: Unknown mail.provider 'qq'. Choose one of: sina, gmail` at start-up |
| Location | `PROJECT_ROOT/agent.properties`; override with `AGENT_PROPERTIES=/path/file` in `.env` |
| One-run override | CLI `uv run agent-demo --mail gmail` (and `agent-demo-server --mail gmail`) wins over the file |
| Switching | Edit the file, restart the CLI or server. No hot reload (MCP servers start once) |

**Why a separate file instead of `.env`:** `.env` holds secrets and is git-ignored; the provider choice and host names are ordinary settings that are fine to commit. Each file has one job.

`.env` gains one variable:

```bash
# Sina Mail authorization code (Settings → client POP/IMAP/SMTP → authorization code). NOT your login password.
SINA_AUTH_CODE=
```

---

## 2. How the selection works

```
agent.properties ──▶ load_properties() ──▶ mail.provider = "sina"
                                                │
mcp_servers.json  (lists BOTH mail servers, tagged "role": "mail")
   "sina":  {..., "role": "mail"}   ◀── started
   "gmail": {..., "role": "mail"}   ◀── skipped (not selected)
   other servers (no role)          ◀── always started
                                                │
                                                ▼
                       MCPManager starts only the selected mail server
                       → tools sina__search_emails, sina__read_email, …
                       → system prompt names the provider, prefix and the user's own address
```

### 2.1 `mcp_servers.json`

Both providers stay configured; a new `role` field marks them as alternatives:

```json
{
  "servers": {
    "sina": {
      "role": "mail",
      "command": "uv",
      "args": ["run", "python", "-m", "mail_mcp.imap_server"],
      "env": {
        "IMAP_HOST": "${mail.sina.imap_host}", "IMAP_PORT": "${mail.sina.imap_port}",
        "SMTP_HOST": "${mail.sina.smtp_host}", "SMTP_PORT": "${mail.sina.smtp_port}",
        "EMAIL_USER": "${mail.sina.address}", "EMAIL_PASSWORD": "${SINA_AUTH_CODE}"
      },
      "auto_approve": ["list_folders", "search_emails", "read_email"],
      "untrusted": true
    },
    "gmail": {
      "role": "mail",
      "command": "npx",
      "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp@1.1.11"],
      "auto_approve": ["search_emails", "read_email", "list_email_labels", "list_filters", "get_filter"],
      "blocked": [],
      "untrusted": true
    }
  }
}
```

The server reads these env variables (`IMAP_HOST`, `EMAIL_PASSWORD`, …); any IMAP/SMTP provider only needs different values.

### 2.2 `${…}` placeholders (new, small)

`env` values may contain `${NAME}`. At start-up each placeholder is replaced from, in order: **`agent.properties`** (dotted keys such as `mail.sina.address`), then **environment / `.env`** (such as `SINA_AUTH_CODE`).

- The secret never appears in `mcp_servers.json` or `agent.properties`.
- An unresolved placeholder is a **start-up warning for that server** (like a failed Gmail start today): `MCP server 'sina' not started: SINA_AUTH_CODE is not set in .env`. The agent still runs with the database tools.
- This also fixes a current limitation in `mcp_client.py`: without an `env` block the MCP SDK passes only a minimal environment, so `.env` values never reached a server.

### 2.3 Which servers start (`runtime.py`)

```python
servers = load_mcp_config(settings.mcp_config_path)
servers = {name: cfg for name, cfg in servers.items()
           if cfg.get("role") != "mail" or name == settings.mail_provider}
```

- If the selected provider has no entry in `mcp_servers.json` → `ConfigError` naming the file.
- `role` is generic: a later `"role": "calendar"` could use the same rule with a `calendar.provider` property.

### 2.4 System prompt (`agent.py`)

The Gmail-specific line becomes a template filled from the selection:

```
- the user's email ({provider_label}, address {address}): tools whose names start with {name}__, if connected.
```

e.g. *"the user's email (Sina Mail, address jasonqi8@sina.com): tools whose names start with sina__"*. Knowing the user's own address lets requests like *"email me the watchlist"* work without typing the address (sending still needs approval). The rest of the prompt (untrusted content, approvals) is unchanged.

### 2.5 UI

- `ready` event: new field `mail: {"provider": "sina", "address": "jasonqi8@sina.com"}`; the web header and CLI banner show it (e.g. `mail: sina`). `protocol.ts` mirrors it.
- CLI `/tools` already shows each tool's `source` (`mcp:sina`) and policy.

---

## 3. Settings changes (`config.py`)

| Item | Design |
|---|---|
| `load_properties(path) -> dict[str, str]` | Small parser: `key=value` or `key: value`, `#`/`!` comments, whitespace trimmed. No external dependency |
| `MAIL_PROVIDERS` | `{"sina": "Sina Mail", "gmail": "Gmail"}` (labels for the prompt and UI) |
| `Settings` | New fields **with defaults at the end** (`mail_provider: str = "sina"`, `mail_address: str = ""`, `properties: dict = {}`), so the positional constructor in `tests/test_server.py` keeps working |
| `load_settings(provider, model, mail=None)` | Reads `AGENT_PROPERTIES` or `PROJECT_ROOT/agent.properties`; `mail` (from `--mail`) overrides; validates the provider |

---

## 4. The Sina MCP server

### 4.1 Requirements (decided: we write our own, §4.2)

The server must meet all of these:

| Check | Why |
|---|---|
| Connects only to the configured IMAP/SMTP hosts; reads the password from env, never logs it | The authorization code gives **full** mailbox access (no read-only scope like OAuth) |
| Has separate tools for reading (search, read, list folders) and for writing (send, move, delete, flag) | So `auto_approve` can list only the read tools |
| Decodes Chinese mail correctly (RFC 2047 headers, GBK/GB2312/GB18030 bodies) | Sina mail is often not UTF-8; garbled subjects make the agent useless |
| Search works without server-side Chinese text search (or falls back to local filtering) | Sina's IMAP `SEARCH` may not support Chinese text |
| Supports SSL on 993/465 | Sina's client settings |
| Actively maintained, open source, version pinned in `args` | Same rule as the Gmail server (`@1.1.11`) |

### 4.2 Our own server: `backend/mail_mcp/`

Write `backend/mail_mcp/imap_server.py` (about 150–200 lines, standard library `imaplib`/`smtplib`/`email` + the MCP SDK, same pattern as `tests/fake_mcp_server.py`), started with `"command": "uv", "args": ["run", "python", "-m", "mail_mcp.imap_server"]`.

**Tool contract:**

| Tool | Policy | Arguments → result |
|---|---|---|
| `list_folders` | auto | → folder names (INBOX, Sent, …) |
| `search_emails` | auto | `folder="INBOX"`, `from`, `subject`, `since` (YYYY-MM-DD), `unread_only`, `limit=10` → id, date, from, subject |
| `read_email` | auto | `id`, `folder` → headers + text body (HTML converted to text), attachment names only |
| `send_email` | **ask** | `to[]`, `subject`, `body`, optional `cc[]` → message id |
| `mark_read`, `move_email` | **ask** | `id`, `folder` / `target_folder` |

Delete is not offered in v1. Results are wrapped as `<untrusted source="sina">` like Gmail's.

### 4.3 Sina account setup (one time)

1. Sign in at mail.sina.com.cn → **Settings → client POP/IMAP/SMTP** (客户端POP/IMAP/SMTP).
2. Enable **IMAP** and **SMTP**.
3. Generate an **authorization code (授权码)** if offered and put it in `.env` as `SINA_AUTH_CODE`. Never use the login password.
4. Confirm the hosts shown on that page (`imap.sina.com` / `smtp.sina.com` for @sina.com; @sina.cn and VIP accounts use different hosts) and update `agent.properties` if they differ.

---

## 5. Security

| Risk | Control |
|---|---|
| Authorization code leaks | Only in `.env` (git-ignored, `chmod 600` on servers); passed to the MCP process through the `${SINA_AUTH_CODE}` placeholder; never in `agent.properties`, `mcp_servers.json`, logs or the `ready` event |
| No read-only credential for IMAP | The tool list is the boundary: read tools auto-approved, every write tool asks, no delete tool |
| A provider switch silently auto-approves risky tools | Each provider has its own `auto_approve` list; the safety test checks **every** `role: mail` server (§6) |
| Prompt injection in Sina emails | Same as Gmail: `<untrusted>` wrapping + approvals |
| Wrong mailbox used by accident | Provider and address shown in the CLI banner and web header |

---

## 6. Tests

| Test | Checks |
|---|---|
| `load_properties` | comments, blank lines, `=` and `:`, whitespace, values containing `=` |
| Provider selection | file missing → `sina`; `mail.provider=gmail` → gmail; `--mail` overrides the file; unknown provider → `ConfigError`; selected provider missing from `mcp_servers.json` → `ConfigError` |
| Server filtering | only the selected `role: mail` server is passed to `MCPManager`; servers without `role` always pass |
| `${…}` expansion | from properties, from env, properties before env; unresolved → that server is skipped with a warning naming the variable; the secret value never appears in warnings |
| System prompt | contains provider label, address and `sina__` prefix |
| `ready` event / `protocol.ts` | `mail` field present |
| `test_project_mcp_config_is_safe` (update) | for **every** `role: mail` server: `untrusted` is true and no send/delete/move tool is in `auto_approve` |
| Integration (`-m integration`, opt-in) | log in to `imap.sina.com` with `SINA_AUTH_CODE` and list INBOX (like the Feishu token test); skipped when the code is not set |

Manual: with `mail.provider=sina`, ask *"Find my latest 3 emails and summarise them"* (Chinese subjects must be readable) and *"Email me a table of my watchlist"* (approval, then the mail arrives at jasonqi8@sina.com). Switch to `gmail`, restart, repeat.

---

## 7. Files

| File | Change |
|---|---|
| `agent.properties` | **New**: `mail.provider=sina` and the Sina/Gmail keys above |
| `.env.example` | `SINA_AUTH_CODE=`, optional `AGENT_PROPERTIES=` |
| `mcp_servers.json` | Add `sina`; add `"role": "mail"` to both mail servers |
| `backend/agent_demo/config.py` | `load_properties`, `MAIL_PROVIDERS`, new `Settings` fields, `--mail` |
| `backend/agent_demo/mcp_client.py` | `${…}` expansion in `env`; `role` read by `ServerConfig.from_dict` |
| `backend/agent_demo/runtime.py` | Start only the selected mail server; pass mail info to `Ready` |
| `backend/agent_demo/agent.py` | Prompt template for the mail line |
| `backend/agent_demo/events.py`, `web/src/protocol.ts`, `web/src/App.tsx`, `cli.py` | `mail` in `ready`; show it |
| `backend/tests/…` | Tests from §6 |
| `README.md` | "Choosing the mail provider" + Sina setup |
| `backend/mail_mcp/` | **New**: the IMAP/SMTP MCP server (§4.2) |

## 8. Order of work

1. Sina account setup (§4.3) and a login check.
1. Write the IMAP/SMTP MCP server (§4.2) with tests against a fake IMAP/SMTP server.
2. Properties + provider selection + `${…}` expansion, with tests (works with Gmail before Sina is ready).
3. Add the `sina` server entry; Sina account setup; integration test.
4. Prompt, `ready` event and UI.
5. Manual test with both providers.

## 9. Relation to the IM design

Independent of [`im_integration.md`](im_integration.md): Feishu users get whichever mailbox is selected, because the IM hub reuses the same runtime and tool registry. For a mainland-China VM, `mail.provider=sina` is the setting that works.

---

## 10. Implementation notes (2026-10-24)

| Topic | What was built (differences from the draft above) |
|---|---|
| Sina capabilities | Sina's IMAP advertises only `IMAP4REV1 ID` (no `MOVE`, no `UIDPLUS`). **`move_email` is not offered**: without `MOVE` it would need `EXPUNGE`, which could also remove other messages flagged as deleted |
| Tools | `list_folders`, `search_emails`, `read_email` (auto); `send_email`, `mark_read` (ask). `send_email` saves a copy to the Sent folder (`SAVE_SENT=true`) |
| Server command | `"command": "${PYTHON}"` with `["-m", "mail_mcp.imap_server"]`. `${PYTHON}` and `${PROJECT_ROOT}` are built-in placeholders, so the server always runs in the agent's own virtualenv. `mail_mcp` is packaged in `pyproject.toml` |
| Missing mail server for the selected provider | A **start-up warning** (agent keeps running with the database tools), not a `ConfigError` |
| Errors shown to the model | The MCP SDK hides ordinary exceptions ("Error executing tool …"); `imap_server.py` turns `MailError` into `ToolError`, so the model sees the reason |
| Search | Non-ASCII filters (Chinese sender or subject) run locally over the newest 300 messages; the result footer says so. ASCII filters, `since` and `unread_only` run on the server |
| Encodings | GB2312/GBK decoded as GB18030; raw (non-RFC 2047) GBK header bytes are read from the raw message; folder names decoded from modified UTF-7 |
| Tests | `tests/test_mail_mcp.py` (fake IMAP/SMTP, 16 tests incl. a real stdio MCP round trip), new cases in `tests/test_config.py`, `tests/test_sina_integration.py` (opt-in, read-only) |
