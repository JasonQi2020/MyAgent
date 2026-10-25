# Design: Deploying agent-demo on the shared Tencent Cloud VM (with WeCom)

> Status: **implemented and deployed** (2026-10-26 VM time); decisions in §13, implementation notes in §14. Based on a read-only survey of the VM (§1). No code or VM changes yet.
> Companion to the other designs in `design/` (IM: `im_integration.md`, `telegram_integration.md`, **`wecom_integration.md`**; tasks: `scheduled_tasks.md`; Claude Code: `claude_code_delegation.md`).

## Context

agent-demo should run 24/7 on the Tencent Cloud VM `49.233.173.2`. It will be used from the phone (**Feishu** and **WeCom / 企业微信**; Telegram stays on the laptop, §7.1) and from a CLI. Claude mode (`/claude`) and the strong model (OpenAI) may not work there; that is accepted.

The VM **already runs the Hermes agent**, and **more agents will follow**. So every agent gets its own user, files, secrets, ports, bot accounts and resource limits, and no agent can affect another.

The WeCom channel is **already built** (`channels/wecom.py`, `design/wecom_integration.md`). It uses the **smart robot (智能机器人) in API mode over a long connection**: an outbound WebSocket to `wss://openws.work.weixin.qq.com`. So, like Feishu, WeCom needs **no public URL, domain, ICP filing, HTTPS certificate, Caddy route or trusted IP**. The deployment only has to run it.

---

## 1. The VM (surveyed 2026-10-25, read-only, as `ubuntu` with the laptop's ed25519 key)

| Item | Found |
|---|---|
| Instance | Tencent **Lighthouse**, region **ap-beijing** (mainland China), `49.233.173.2`; host key `SHA256:A787W+V/c0npukL4AAoB+DgTr6qVpbqv1O47grOnShY` |
| OS | **Ubuntu 24.04.4 LTS**, kernel 6.8, x86_64, systemd; time zone Asia/Shanghai, NTP synced |
| Resources | **4 CPUs, 7.5 GB RAM** (5.8 GB free, 1.9 GB swap), **45 GB free** disk |
| Users | `ubuntu` (1000, **passwordless sudo**), `lighthouse` (1001, Tencent's) |
| **Hermes** | Runs as **`ubuntu`** from `/home/ubuntu/.hermes/`. Two services: `hermes-dashboard.service` (a system unit with `User=ubuntu`, on `127.0.0.1:9119`) and **`hermes-gateway.service` (a systemd *user* service of `ubuntu`)**. It also has a WeChat-Pay MCP plugin (Node) and its own **Feishu** app. This design does not touch Hermes |
| Other services | Caddy on **public `:80`** (default page), Caddy admin on `127.0.0.1:2019`; Tencent agents (tat, barad, stargate). This design does not touch Caddy |
| Free | Port **8101**; no Docker |
| Tools | Python 3.12.3, Node 22, npm 10, git, rsync. **Missing:** `uv`, `bwrap`, `sqlite3` CLI (not needed: Python's sqlite3 module is enough) |

### 1.1 Outbound reachability from the VM

| Endpoint | Result | Consequence |
|---|---|---|
| dashscope.aliyuncs.com (Qwen) | ✅ 0.05 s | the assistant works |
| open.feishu.cn | ✅ 0.07 s | **Feishu works** |
| qyapi.weixin.qq.com (WeCom API) | ✅ (a dummy request gets the expected `invalid corpid` answer) | WeCom's servers are reachable |
| **`wss://openws.work.weixin.qq.com`** (WeCom smart robot) | ✅ from the laptop (dummy subscribe → `853000 invalid bot_id or secret`). **Not yet checked from the VM**: rollout step 1 checks it the same way | **WeCom works** (expected: it is a mainland Tencent endpoint) |
| imap.sina.com:993 | ✅ | Sina Mail works |
| **api.telegram.org** | ❌ timeout (blocked) | **no Telegram on the VM** (§7.1) |
| query1.finance.yahoo.com | ⚠️ HTTP 429 | **`stock_quote` needs a China-reachable fallback** (§7.2) |
| api.openai.com | ❌ timeout | strong model off (accepted) |
| api.anthropic.com | ⚠️ 403 (region) | Claude mode off (accepted) |
| pypi.org | ⚠️ ~10 s, times out | install packages from **mirrors.cloud.tencent.com/pypi** (✅ 0.8 s) |
| github.com | ✅ 1.3 s | install `uv` from its official GitHub release |

## 2. Isolation model: one Linux user and one systemd service per agent

| Layer | agent-demo | Each further agent |
|---|---|---|
| **Linux user** | system user **`agent-demo`**: no password, **no sudo**, no login shell | its own user |
| **Folder** | `/srv/agents/agent-demo/`, owner `agent-demo`, mode `750` | `/srv/agents/<name>/` |
| **Secrets** | `/srv/agents/agent-demo/.env`, mode `600` | its own `.env`; no shared keys |
| **Service** | system unit `agent-demo.service` | `<name>.service` |
| **Port** | `127.0.0.1:8101` (web UI and WebSocket; nothing public) | next free port from `PORTS.md` |
| **Bot accounts** | its **own** Feishu app and WeCom smart robot (today's, moved from the laptop, §5) | its own |
| **Python** | its own venv and uv cache inside its folder | its own |
| **Resources** | `MemoryMax=1G`, `CPUQuota=100%` (1 of 4 CPUs), `TasksMax=256` | per agent |
| **Logs** | `journalctl -u agent-demo`; its `logs/` rotated | its own |

Both IM channels on the VM (Feishu and WeCom) **only connect outwards**. agent-demo therefore opens **no public port and no Caddy route**. The Lighthouse firewall stays as it is.

**Direction of isolation (important):** agent-demo cannot see Hermes, because `ProtectHome=true` hides `/home/ubuntu/.hermes` and agent-demo has no sudo. The reverse is not true. **Hermes runs as `ubuntu`, which has passwordless sudo**, so anything running inside Hermes could still read agent-demo's files as root.

agent-demo is protected from Hermes only once Hermes moves to its own unprivileged user. That is recommended for the "more agents" future (§10), but it is **not part of this deployment**: Hermes stays exactly as it is. Every *new* agent follows §2 from the start.

### 2.1 systemd unit (`deploy/agent-demo.service`)

```ini
[Unit]
Description=agent-demo (Qwen assistant: Feishu, WeCom, web UI on localhost)
After=network-online.target
Wants=network-online.target

[Service]
User=agent-demo
Group=agent-demo
WorkingDirectory=/srv/agents/agent-demo/backend
Environment=AGENT_PORT=8101
Environment=UV_CACHE_DIR=/srv/agents/agent-demo/.cache/uv
Environment=UV_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple/
ExecStart=/usr/local/bin/uv run --frozen --no-sync agent-demo-server
Restart=on-failure
RestartSec=5

# Isolation
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true                       # /home/* (ubuntu, Hermes, lighthouse) invisible
ReadWritePaths=/srv/agents/agent-demo  # the only writable place
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
UMask=0077
# Resources
MemoryMax=1G
CPUQuota=100%
TasksMax=256

[Install]
WantedBy=multi-user.target
```

`Restart=on-failure` also covers a WeCom start failure. If the robot's id or secret is wrong, `WeComChannel.start()` fails within 10 s. The server still starts and shows a warning, so the other channels keep working. The journal shows the error.

## 3. Layout on the VM

```
/srv/agents/                       root:root 755
├── PORTS.md                       port/user/bot table (§4), maintained by the admin
└── agent-demo/                    agent-demo:agent-demo 750  == agent-demo's PROJECT_ROOT
    ├── backend/  web/dist/  design/  mcp_servers.json   ← code: replaced on every deploy
    ├── .env                       600 secrets (prod bots, DashScope, Sina, WeCom) — never overwritten by deploys
    ├── agent.properties           VM settings (§6)                                — never overwritten
    ├── state/  data/              tasks, groups, owner chats, demo.db             — kept, backed up
    ├── logs/                      MCP logs (logrotate: daily, keep 7)
    ├── backups/                   daily archives (§9 step 10)
    └── .cache/uv/
```

The code resolves all paths from `PROJECT_ROOT` (`config.py`), so this layout needs no path changes.

## 4. Port and name registry (`/srv/agents/PORTS.md`)

| Agent | User | Service(s) | Port (127.0.0.1) | Bots |
|---|---|---|---|---|
| hermes | ubuntu | hermes-dashboard.service, hermes-gateway.service (user) | 9119 | its own (including its own Feishu app) |
| agent-demo | agent-demo | agent-demo.service | **8101** | Feishu app "AgentDemoBot", WeCom smart robot (both moved from the laptop, §5) |
| (next) | agent-x | agent-x.service | 8102 | its own |

Agent web servers bind **127.0.0.1 only**. They are reached through an SSH tunnel (§8), never from the internet. If a future agent needs a public webhook (for example WhatsApp), it gets one Caddy path prefix that forwards only its webhook path. Such a route is recorded in this table. agent-demo needs none.

## 5. Bot accounts: production (VM) and development (laptop)

Two programs must never consume the same bot:

| Channel | What goes wrong with two consumers |
|---|---|
| Feishu | events are split between the connections |
| Telegram | `409 Conflict` |
| **WeCom smart robot** | **one long connection per robot**: a second subscribe takes over, and the first gets `disconnected_event` and, by design, does not reconnect. The laptop and the VM would silently steal the robot from each other |

**Decision: move today's accounts to the VM** (review decision 3):

| Account | After the move |
|---|---|
| Feishu app "AgentDemoBot" | **VM only.** Same app, so the same `open_id`s: allowlists, owners and enabled groups carry over unchanged |
| WeCom smart robot | **VM only.** Same Bot ID, Secret and userids |
| Telegram bot (@MyAgent2026DecBot) | **Laptop only** (blocked on the VM, §7.1) |

**Rule from the cut-over on:** the laptop never runs `feishu` or `wecom`. Its `.env` gets `IM_CHANNELS=telegram` (or empty). Otherwise the laptop and the VM split Feishu events and steal the WeCom robot from each other.

If the laptop ever needs Feishu or WeCom again for development, it gets **separate development accounts** (a second Feishu app, a second smart robot), never the VM's. That can be added later without touching the VM.

Hermes keeps its own Feishu app; agent-demo never uses it.

## 6. Configuration on the VM (`agent.properties` + `.env`)

| Setting | Laptop today | VM |
|---|---|---|
| `IM_CHANNELS` | `feishu,telegram,wecom` → after the cut-over **`telegram`** | **`feishu,wecom`** |
| `FEISHU_*`, `WECOM_*` (ids, secrets, allowlists) | removed after the cut-over | **copied** from the laptop's `.env` (§9 step 4; the values are never shown in chat or logs) |
| `TELEGRAM_*` | kept | not set |
| `state/` (enabled groups, owner chats, tasks) | kept for Telegram | Feishu/WeCom groups and owner chats copied (§9 step 4) |
| `WECOM_STREAM_INTERVAL` | `2` | `2` (see §7.3 on the rate limit) |
| `WECOM_WS_URL` | default | default |
| `claude.enabled` | `true` | **`false`** (Anthropic refuses the region) |
| `claude.owner.wecom` / `.feishu` | today's ids | the **same ids** (same accounts): the owner who receives scheduled-task reports on each channel (`/claude` itself stays off) |
| `model.strong.*` | openai / gpt-5.5 | either left set, so `/strongmodel` reports the error (OpenAI is blocked), or removed |
| `mail.provider` | `sina` | `sina` |
| `tasks.timezone` | `America/Los_Angeles` | **`America/Vancouver`** (review decision 4: Vancouver time; the same clock and DST rules as Los Angeles). The scheduler uses this zone, not the VM's Asia/Shanghai clock, so "9 PM" means 9 PM in Vancouver |
| `AGENT_PORT` (new) | 8000 | 8101 |

### 6.1 Secrets and user ids: how they reach the VM

Yes, they are **copied from the laptop**, but only once, only the keys the VM needs, and only over SSH:

| What | Kind | On the VM |
|---|---|---|
| `LLM_API_KEY` (DashScope), `SINA_AUTH_CODE`, `FEISHU_APP_SECRET`, `WECOM_BOT_SECRET` | **secrets** | copied |
| `FEISHU_APP_ID`, `WECOM_BOT_ID` | identifiers (not secret alone) | copied |
| `FEISHU_ALLOWED_USERS`/`_CHATS`, `WECOM_ALLOWED_USERS`/`_CHATS` | user and chat ids (not secret, but personal) | copied unchanged: the same accounts give the same ids (§5) |
| `TELEGRAM_*`, OpenAI key, `CLAUDE_CODE_OAUTH_TOKEN`, Gmail | not needed there | **never copied** |

How the copy works (`deploy/make_vm_env.py` plus one step in `deploy.sh --env`):
1. **Build the file on the laptop.** The script reads the laptop's `.env` and writes the filtered file to the scratch folder with mode `600`. It prints **key names only**, so no value ever appears in the terminal, the chat or a log.
2. **Copy it over SSH.** `scp` sends it to the VM's `/home/ubuntu/` staging folder; the transfer is encrypted.
3. **Install it with the right owner.** `sudo install -m 600 -o agent-demo -g agent-demo … /srv/agents/agent-demo/.env`.
4. **Delete the temporary copies** in the staging folder and on the laptop (`shred -u`).
5. **Restart** the service.

**Rules:**
- **Never in the code deploy.** The normal `deploy.sh` excludes `.env` (§9 step 3), so code updates can never overwrite or leak it.
- **Never in chat.** The user does not paste secrets into chat; I never `cat` the file.
- **Change one value later** with `sudo -u agent-demo nano /srv/agents/agent-demo/.env` on the VM (or rerun `deploy.sh --env`), then restart.
- **No rotation before the move:** the current `SINA_AUTH_CODE` is kept, by the user's decision (2026-10-25), although it was shown once in an earlier session. It can be regenerated in Sina Mail's settings at any time; then only the VM's `.env` needs the new value.
- **Backups** (§9 step 10) include `.env`. They stay in `backups/` (mode `600`, owner `agent-demo`); a copy to the laptop goes over SSH only.
- **Known limit:** root, and therefore `ubuntu`/Hermes via sudo, can read the file (§2). Only moving Hermes to its own user removes that.

## 7. The mainland gaps, and WeCom notes

### 7.1 Telegram (blocked) — decision: no Telegram on the VM

The VM runs Feishu and WeCom only; no proxy is set up. Telegram stays on the laptop with today's bot and works whenever the laptop server runs.

Telegram tasks are affected too. A scheduled task created in Telegram lives in the **laptop's** `state/tasks.db` and runs only while the laptop runs. Tasks that must fire reliably are created on the VM, via Feishu or WeCom.

If this is needed later, there are two ways: an outbound proxy set in agent-demo's unit only, or a Hong Kong instance. With a proxy, `NO_PROXY` must list `feishu.cn` and `weixin.qq.com`, because `websockets` 15 honours the proxy variables.

### 7.2 Stock quotes (Yahoo 429) — decision: China-reachable fallback source

`quotes.fetch_quote()` tries **Yahoo first, then a fallback source** that is reachable from mainland China. The candidate is **Sina Finance's US-stock quote API** (`hq.sinajs.cn`, `gb_<symbol>`, which needs a `Referer: https://finance.sina.com.cn` header). Tencent's `qt.gtimg.cn` (`usFTNT`) is the alternative.

At implementation:
1. **Check reachability from the VM first** (read-only `curl` of both sources).
2. **Map the fallback's fields** to the existing `Quote`: price, previous close, change, percentage, currency and time. The previous-close field must be checked carefully; the Yahoo previous-close bug is the lesson here.
3. **Mark the source in the answer**, e.g. "(source: Sina Finance)".
4. **Fall back on any Yahoo failure:** 429, a timeout, or bad data.
5. **Test offline** with recorded sample answers from both sources.

The fallback also helps the laptop when Yahoo rate-limits.

### 7.3 WeCom on the VM: what to watch

| Topic | Note |
|---|---|
| Keepalive | The client sends its own JSON `ping` every 30 s; the `websockets` protocol pings are off (`ping_interval=None`), because they broke WeCom connections (see `wecom_integration.md` §11). No change for the VM |
| Reconnect | Drops reconnect with a 1 s → 30 s backoff. A **`disconnected_event` (taken over) does not reconnect**: if this appears in the VM journal, the laptop (or another program) is running `wecom` with the same robot (§5) |
| Rate limit | Stream refreshes may count toward the 30 messages/minute limit (still to confirm with the real robot, `wecom_integration.md` §10). If a busy VM hits it, raise `WECOM_STREAM_INTERVAL` |
| Restart | Restarting the service drops open streams and pending approvals (as for Feishu). After a restart, a scheduled-task report goes to the WeCom owner as a DM |
| Groups | WeCom groups can include **external contacts (WeChat users)**. Do not `/enable` such groups: mail content would reach outsiders |

## 8. Using it: phone and CLI

- **Phone:** DMs with the bots in **WeCom** and **Feishu**, served by the VM around the clock: assistant, Sina mail, approvals (card buttons), scheduled tasks, and group @mentions in enabled groups. **Telegram** is answered only while the laptop server runs.
- **CLI on the VM (no new code):** `ssh ubuntu@49.233.173.2`, then `sudo -u agent-demo /srv/agents/agent-demo/deploy/agent-demo-cli`. The wrapper runs `cd backend && uv run --frozen --no-sync agent-demo`. It starts its own runtime **without IM channels**, so it never clashes with the service (in particular it never takes over the WeCom robot).
- **CLI from the laptop (optional, small new code):** `agent-demo-remote`, talking to the server's existing WebSocket through `ssh -L 8101:127.0.0.1:8101 ubuntu@49.233.173.2`.
- **Web UI from the laptop:** the same SSH tunnel, then `http://127.0.0.1:8101`.

## 9. Rollout steps

0. ✅ **Survey** (done, §1).
1. **Base (admin, once):**
   - install `uv` to `/usr/local/bin` from its GitHub release (checksum checked);
   - create `/srv/agents/` and `PORTS.md`;
   - set the journald limit `SystemMaxUse=500M`;
   - **check `wss://openws.work.weixin.qq.com` from the VM** with a dummy subscribe (expect `853000`).
2. **User and folder:** `sudo useradd --system --home-dir /srv/agents/agent-demo --shell /usr/sbin/nologin agent-demo`; folder mode `750`.
3. **First deploy from the laptop** (`deploy/deploy.sh`):
   1. `npm run build`.
   2. `rsync` the code to a staging folder as `ubuntu`.
   3. `sudo rsync` into `/srv/agents/agent-demo`, then `chown agent-demo`. Excluded: `.env`, `agent.properties`, `state/`, `data/`, `logs/`, `backups/`, `.venv`, `.cache`, `.claude-sandbox`, `node_modules`.
   4. `sudo -u agent-demo uv sync --frozen` against the Tencent mirror. Verify that the lock file installs from the mirror; if not, fall back to resolving with `--index-url`.
4. **Config, copied from the laptop** (by a script, so no secret is printed):
   - **`.env`:** a VM `.env` (600) built from the laptop's, keeping only the keys the VM needs (DashScope, Sina, `FEISHU_*`, `WECOM_*`) and setting `IM_CHANNELS=feishu,wecom`. The `TELEGRAM_*`, OpenAI, Anthropic/Claude and Gmail keys are left out.
   - **`agent.properties`:** per §6 (`claude.enabled=false`, `tasks.timezone=America/Vancouver`).
   - **`state/`:** Feishu/WeCom enabled groups, owner chats, and the scheduled tasks that report to Feishu/WeCom. Web-UI and Telegram tasks stay on the laptop.
   - **`data/demo.db`:** `seed_db.py`.
5. **Service:** install the unit, `systemctl enable --now agent-demo`, and follow `journalctl -u agent-demo -f`. In the same step, **cut over**:
   1. Stop the laptop server.
   2. Set the laptop's `IM_CHANNELS=telegram`.
   3. Start the VM service.
   4. Restart the laptop server if wanted (Telegram only).
6. **Feishu:** check the journal for the Feishu connection. No console change is needed: the long connection is a property of the app, not of the machine. Then DM the bot from the phone.
7. **WeCom:** check the journal for "WeCom connected" and no take-over. Then DM the robot; a group @mention works in an already enabled group.
8. **Quotes (§7.2):** ask "FTNT price" through Feishu; the answer comes from Yahoo or the fallback, with the source named.
9. **Updates:** run `deploy/deploy.sh` again (rsync, `uv sync`, restart). It touches only `/srv/agents/agent-demo`.
10. **Backups:** a systemd timer makes a daily `tar` of `state/ data/ .env agent.properties` in `backups/` (keep 7); optionally copied to the laptop.
11. **Removal / moving back:** `systemctl disable --now agent-demo`, `userdel agent-demo`, delete `/srv/agents/agent-demo`. Put `feishu,wecom` back in the laptop's `IM_CHANNELS`. Nothing else changes.

## 10. Future agents and Hermes

- Each new agent gets the same setup: its own user, a folder under `/srv/agents/`, a unit with the §2.1 hardening, a port in `PORTS.md`, and its own bots and `.env`. For WeCom, that means **its own smart robot** (§5).
- **Recommendation for later (not now):** move Hermes to its own unprivileged user under `/srv/agents/hermes`, so no agent runs with sudo. Until then, Hermes can technically read other agents' files via sudo.
- If an agent ever needs Docker, the same rules map onto containers (`--user`, `-p 127.0.0.1:…`, `--memory`, `--cpus`, its own env file).

## 11. Code and files to add

The WeCom channel already exists and needs **no code changes** for the VM. Only deployment pieces are new:

| File | Change |
|---|---|
| `backend/agent_demo/server.py` | take the port from `AGENT_PORT` (default 8000) |
| `deploy/agent-demo.service` | **new** (§2.1) |
| `deploy/deploy.sh` | **new**: build, rsync with excludes, remote `uv sync`, restart |
| `deploy/agent-demo-cli` | **new** (§8) |
| `deploy/backup.sh`, `deploy/agent-demo-backup.{service,timer}` | **new** (§9 step 10) |
| `deploy/check_wecom.py` | **new**, small: the dummy-subscribe reachability check for §9 step 1 (no credentials) |
| `deploy/make_vm_env.py` | **new**: builds the VM `.env` and the `state/` subset from the laptop's (§9 step 4), printing key names only, never values |
| `deploy/README.md`, `README.md` | how to deploy, update, back up and remove; the "laptop never runs feishu/wecom" rule (§5) |
| `backend/agent_demo/quotes.py` | the China-reachable fallback source (§7.2) |
| `backend/tests/test_quotes.py` | fallback tests with recorded sample answers |
| (optional) `agent_demo/remote_cli.py` | the laptop CLI over the SSH tunnel |

## 12. Verification

- **Laptop:**
  - tests for `AGENT_PORT` and the quote fallback, plus the full suite (253 + new tests);
  - `deploy.sh --dry-run` lists only code files (never `.env` or `state/`);
  - `make_vm_env.py` output has no `TELEGRAM_*`, OpenAI or Claude keys.
- **VM:**
  - `systemctl status agent-demo`;
  - `ss -ltnp` shows agent-demo only on `127.0.0.1:8101`, and `ss -tnp` shows its outbound connections to Feishu and `openws.work.weixin.qq.com`;
  - as `agent-demo`, `ls /home/ubuntu` is refused;
  - `systemd-analyze security agent-demo`;
  - `hermes-dashboard` and Hermes' user service `hermes-gateway` are unchanged before and after;
  - Caddy's configuration is unchanged.
- **Phone, WeCom:**
  - a DM question gets a streamed answer;
  - a Sina mail summary works;
  - an approval card works with Approve and with Deny;
  - a scheduled task is delivered to WeCom at the right Vancouver time;
  - an @mention in an enabled group is answered.
- **Phone, Feishu:** the same checks, plus a stock quote answered while Yahoo refuses.
- **Stability:** the WeCom connection survives 24 h in the journal (no take-over; reconnects only after network drops).
- **Laptop:** the web UI and the CLI work through the SSH tunnel. The laptop server runs with Telegram only, at the same time as the VM. Telegram answers, and the VM journal shows no Feishu or WeCom disturbance.
- **Reboot the VM:** agent-demo, Caddy and both Hermes services come back, and a scheduled task fires.

## 13. Review decisions (2026-10-25)

| # | Question | Decision |
|---|---|---|
| 1 | Telegram on the VM | **No.** Telegram stays on the laptop (§7.1) |
| 2 | Stock quotes | **A China-reachable fallback source** after Yahoo (§7.2) |
| 3 | Accounts | **Move the current Feishu app and WeCom robot to the VM.** The laptop then runs Telegram only (§5) |
| 4 | Time zone for tasks | **Vancouver:** `tasks.timezone=America/Vancouver` (§6) |

## 14. Implementation notes (2026-10-25 laptop / 2026-10-26 VM time)

| Topic | What was built (differences from the design above) |
|---|---|
| Quote fallback | **Tencent Finance** (`qt.gtimg.cn`), not Sina. Both answered from the VM; Tencent's line has a clear previous close, the currency and the exchange time. Covers US (incl. `BRK.B`/`BRK-B`), `.HK`, `.SS`/`.SZ`; others (e.g. `RY.TO`) say "not covered by the fallback source". Fallback on any Yahoo failure except "unknown symbol". The answer ends with "source: Tencent Finance (Yahoo unavailable)"; the untrusted tag is `tencent-finance`. Tests: `tests/test_quotes.py` with answers recorded from the VM |
| `uv` | The GitHub release download timed out from the VM, so `bootstrap` copies the laptop's `uv` 0.13.0 binary (sha256 `3e801df8…592df0`) to `/usr/local/bin` |
| Dependencies | `uv sync --frozen --no-dev` works from the VM (the lock's `files.pythonhosted.org` URLs answer quickly); `UV_PYTHON_DOWNLOADS=never`, so the system Python 3.12 is used |
| Scripts | One `deploy/deploy.sh` with sub-commands (`bootstrap`, `code [--dry-run]`, `config`, `check`, `status`) instead of separate scripts; `deploy/make_vm_env.py`, `deploy/check_wecom.py`, `deploy/backup.sh`, the units and `deploy/README.md`. `tests/test_deploy.py` checks what the VM config keeps and drops. Full suite: 266 passed |
| Folder mode | `/srv/agents/agent-demo` ended up `700` (not `750`): stricter, nothing needs group access |
| Unit | Added `Environment=HOME=/srv/agents/agent-demo` (home is hidden by `ProtectHome`). `systemd-analyze security`: 6.8 MEDIUM; more options (`SystemCallFilter`, `RestrictAddressFamilies`, `CapabilityBoundingSet=`) can lower it later |
| Cut-over | Laptop server stopped; laptop `.env` now `IM_CHANNELS=telegram`; VM `.env` built from the laptop's (14 keys; OpenAI, Claude, Telegram keys left out); `owner_chats.json` (feishu, wecom) copied; no tasks existed |
| Verified on the VM | Feishu and WeCom connected; only `127.0.0.1:8101` listening; `agent-demo` cannot list `/home/ubuntu`; Hermes dashboard and gateway active before and after; CLI on the VM: an FTNT quote (Tencent fallback) and Sina folder listing work; backup timer scheduled (04:30 VM time) |
| Still to check by the user | From the phone: Feishu and WeCom DMs, an approval card, a scheduled task at a Vancouver time, a group @mention; a VM reboot |
| Log note | The Lark SDK logs its connection URL including a short-lived `access_key` (as on the laptop). The journal is readable only by root and the `adm` group; lowering the SDK's log level would hide it |
