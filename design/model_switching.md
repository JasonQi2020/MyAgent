# Design: Switching the Model at Runtime (`/strongmodel`, `/weakmodel`)

> Status: **implemented** (2026-10-24). Notes on what changed during implementation are in §8.
> Companion to [`agent-demo-design.md`](agent-demo-design.md) (§5 agent loop, §10 configuration) and [`im_integration.md`](im_integration.md).

## Context

The agent uses one model for everything: DashScope `qwen-plus`, chosen at start-up from `.env`. Some questions deserve a stronger (and more expensive) model. The user wants to switch **per conversation, while chatting, without restarting the server**.

| Decision | Choice |
|---|---|
| Profiles | **weak** = Qwen on DashScope (`qwen-plus`, today's model, the default); **strong** = ChatGPT via the OpenAI API |
| Commands | `/strongmodel`, `/weakmodel`, and `/model` (show the current one) |
| Scope | **Per conversation**: one Feishu DM, one user in a group, one web tab, the CLI session. Other conversations are not affected |
| Back to weak | After `/new`, when an IM session is dropped (idle), on a new web tab or CLI start |
| No restart, ever | Profiles and keys are **re-read every time a model command runs**, so a key added to `.env` or a name changed in `agent.properties` takes effect at the next `/strongmodel` |
| Strong model id | Configurable (`model.strong.name`); default **`gpt-5.5`** (see §8) |

---

## 1. Configuration

`agent.properties` (no secrets):

```properties
# ---- Models -------------------------------------------------------------------
# Profile used when a conversation starts: weak | strong
model.default=weak
# weak: Qwen on DashScope (key: DASHSCOPE_API_KEY in .env)
model.weak.provider=qwen
model.weak.name=qwen-plus
# strong: ChatGPT (key: OPENAI_API_KEY in .env)
model.strong.provider=openai
model.strong.name=gpt-5.5
```

- `provider` is one of the existing presets in `config.PROVIDERS` (deepseek, openrouter, qwen, openai, ollama, custom), so each profile gets its base URL and key variable from there. `model.<profile>.base_url` can override, as `LLM_BASE_URL` does today.
- Keys stay in `.env`: `DASHSCOPE_API_KEY` (already set) and **`OPENAI_API_KEY` (new)**.
- **Compatibility:** if `agent.properties` has no `model.*` keys, the weak profile is exactly today's `LLM_PROVIDER` / `LLM_MODEL` from `.env`, so nothing changes for existing setups. `--provider` / `--model` on the CLI still set the starting model.

## 2. How a switch works

```
/strongmodel
  1. re-read .env (dotenv_values, without touching os.environ) and agent.properties
  2. resolve the profile: provider preset + name + key          → clear error if anything is missing
  3. get an LLMClient from a small cache (key: base_url, model, key fingerprint)
  4. agent.llm = that client     (the conversation history is kept)
  5. reply: "Switched to the strong model: openai / <name>."
```

| Rule | Why |
|---|---|
| Re-read on **every** model command, never cached between commands | The "no restart" requirement: edit `.env` or `agent.properties`, then type the command |
| Clients cached by (base_url, model, key fingerprint) | Re-reading is cheap; creating a new HTTP client for every turn is not. A changed key gives a new fingerprint, so a new client |
| The history is kept | Both providers use the same OpenAI-style message format; reasoning text is never stored (§5), so switching in the middle of a conversation is safe |
| Not while a turn is running | Like `/new`: "I can't switch while I'm working. Try again when I'm done." (avoids changing model between two steps of one turn) |
| The key is never shown | Errors name the variable, never its value: *"The strong model needs OPENAI_API_KEY in .env."* |

**New module `backend/agent_demo/models.py`** (shared by every UI):

```python
@dataclass
class ModelProfile:
    name: str            # "weak" | "strong"
    provider: str        # preset name
    model: str
    base_url: str
    api_key: str

def load_profiles() -> dict[str, ModelProfile]          # re-reads .env + agent.properties; ConfigError per profile
class ModelSwitcher:
    def client(self, profile: str) -> LLMClient          # cached
    def default_profile(self) -> str
def handle_model_command(agent, switcher, text, busy) -> str | None
    # "/strongmodel" | "/weakmodel" | "/model" -> reply text; None if `text` is not a model command
```

`Agent` gets one attribute, `profile` (the current profile name), so `/model` can answer and the UIs can show it.

## 3. Where the commands work

| UI | Change |
|---|---|
| CLI (`cli.py`) | Handled next to `/tools` `/new`; the reply is printed; the banner shows the starting model |
| Web (`server.py` + web UI) | A `user_message` that is a model command is handled by the server instead of the agent; the server answers with a new event **`model_changed`** `{profile, provider, model}` (or an `error`), and the header updates. `protocol.ts` mirrors it |
| Feishu and other IM channels (`channels/hub.py`) | Handled with the other commands (`/help` lists them). In a group it applies to the asker's own session only |

`TurnDone` / the server log also record which model answered, so costs can be traced.

## 4. Security, privacy and cost

| Topic | Note |
|---|---|
| Data leaves to a second provider | With the strong model, the conversation — including email content and (in groups) other members' recent messages — goes to **OpenAI** instead of Alibaba DashScope. The switch reply says so once per conversation |
| Who can switch | Anyone who can talk to the agent in that conversation (IM: allowlisted users only, as for every command) |
| Cost | The strong model costs more per token; token usage is already shown after each turn (CLI, web) and logged (IM) together with the model name |
| Network | `api.openai.com` must be reachable from the server. Checked during implementation from this machine; from a mainland-China VM it usually is not, and the switch then fails with a clear error (the conversation stays on the weak model) |
| Tool schemas | MCP tool schemas are passed unchanged; OpenAI is stricter about JSON Schema than DashScope, so implementation checks every current tool against the strong model once |

## 5. Tests

- `load_profiles`: defaults from today's `.env` when no `model.*` properties; profiles from properties; missing key → error naming the variable (never the value); unknown provider.
- **Dynamic reload:** write a temporary `.env` / properties, switch, change the files (new model name, add a key), switch again → the new values are used without creating a new switcher or restarting anything.
- Client cache: same profile twice → same client; changed key → new client.
- `handle_model_command`: the three commands, unknown text → None, busy → refused, history kept after a switch, `/new` resets to the default profile.
- Hub: per-session (two DMs, two users in one group); `/help` lists the commands; idle drop resets.
- Web: `model_changed` event round trip; the command never reaches the LLM.
- Integration (`-m integration`, opt-in): one tiny call to the strong model with all current tool schemas.
- Manual: add `OPENAI_API_KEY` to `.env` **while the server is running**, then in Feishu `/strongmodel` → answer from ChatGPT; `/model`; `/weakmodel`; a second chat stays on Qwen.

## 6. Files

| File | Change |
|---|---|
| `backend/agent_demo/models.py` | **New**: profiles, switcher, command handler |
| `backend/agent_demo/agent.py` | `profile` attribute |
| `backend/agent_demo/runtime.py` | holds the `ModelSwitcher`; the starting client comes from it |
| `backend/agent_demo/cli.py`, `server.py`, `channels/hub.py` | the commands |
| `backend/agent_demo/events.py`, `web/src/protocol.ts`, `web/src/useAgent.ts`, `web/src/App.tsx` | `model_changed` event, header |
| `agent.properties`, `.env.example`, `README.md` | `model.*` keys, `OPENAI_API_KEY` |
| `backend/tests/test_models.py` | **New** |

## 7. Not in scope

- Reloading every other setting without a restart (allowlists, `IM_CHANNELS`, the mail provider). Those are still read at start-up; making them dynamic is a separate change.
- Choosing the model automatically per question.

## 8. Implementation notes (2026-10-24)

| Topic | What was built / found |
|---|---|
| Strong default | The user's key lists many models. `gpt-5.5` works with tools on `/v1/chat/completions` (2.4 s, correct tool). `gpt-6.1-sol` (newest) refuses function tools on Chat Completions ("use /v1/responses"), so it cannot be used without changing `llm.py` to the Responses API |
| Tool schemas | All 8 current tools (3 database + 5 Sina MCP tools) are accepted by `gpt-5.5` unchanged; covered by `tests/test_models_integration.py` (opt-in) |
| Reachability | `api.openai.com` is reachable from this machine |
| Keys | `config.resolve_llm()` (shared with `load_settings`). The strong profile uses **only** its provider's own key: found during testing that it would otherwise fall back to `LLM_API_KEY` and send the Qwen key to OpenAI |
| Fresh values | `models._fresh_env()` = `os.environ` with the current `.env` on top, read with `dotenv_values` on every model command |
| Web | Commands are answered with `model_changed` (or `error`) and never reach the LLM; the UI does not wait for `turn_done` after a model command (`MODEL_COMMAND` in `protocol.ts`), otherwise it stayed on "thinking…" |
| Logs | IM turn log lines end with the model that answered |
| Tests | `tests/test_models.py` (11: profiles, reload without restart, cache, busy, reset/default, per-chat in the hub, web round trip) + 1 integration test. Full suite: 162 passed |
| Live | Running server, WebSocket: `/model` → weak (qwen / qwen-plus); `/strongmodel` → openai / gpt-5.5, which answered a database question with a tool call; `/weakmodel` → back |
