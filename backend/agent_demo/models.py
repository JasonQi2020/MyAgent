"""Switching the model per conversation at runtime: /strongmodel, /weakmodel, /model.

See design/model_switching.md. Two profiles:
  weak   - the model from .env (LLM_PROVIDER / LLM_MODEL; DashScope qwen-plus here), unless
           agent.properties sets model.weak.*
  strong - model.strong.provider / model.strong.name from agent.properties (default openai / gpt-5.5)

Nothing is cached between commands: every model command re-reads .env and agent.properties,
so a key added to .env or a model name changed in agent.properties works without a restart.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from dotenv import dotenv_values

from .config import PROJECT_ROOT, ConfigError, Settings, _resolve_path, load_properties, resolve_llm
from .llm import LLMClient

PROFILES = ("weak", "strong")
STRONG_DEFAULT = ("openai", "gpt-5.5")
COMMANDS = {"/strongmodel": "strong", "/weakmodel": "weak"}
HELP = "/strongmodel (ChatGPT), /weakmodel (Qwen), /model (show the current model)"


@dataclass(frozen=True)
class ModelProfile:
    name: str  # "weak" | "strong"
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)

    @property
    def label(self) -> str:
        return f"{self.provider} / {self.model}"


@dataclass
class ModelReply:
    text: str
    ok: bool = True
    changed: bool = False


def fresh_env() -> dict[str, str]:
    """Environment with the current contents of .env on top (os.environ was loaded at start-up)."""
    from_file = {k: v for k, v in dotenv_values(PROJECT_ROOT / ".env").items() if v}
    return {**os.environ, **from_file}


def load_profiles() -> tuple[dict[str, ModelProfile | ConfigError], dict[str, str]]:
    """Read both profiles from the current files. A broken profile is a ConfigError, not a crash."""
    env = fresh_env()
    props = load_properties(_resolve_path(env.get("AGENT_PROPERTIES", "agent.properties")))
    profiles: dict[str, ModelProfile | ConfigError] = {}
    for name in PROFILES:
        if name == "weak":
            provider = props.get("model.weak.provider") or env.get("LLM_PROVIDER") or "deepseek"
            model = props.get("model.weak.name") or env.get("LLM_MODEL")
            base_url = props.get("model.weak.base_url") or env.get("LLM_BASE_URL")
            generic_key = "first"  # same rule as load_settings(), so weak == today's model
        else:
            provider = props.get("model.strong.provider") or STRONG_DEFAULT[0]
            model = props.get("model.strong.name") or (STRONG_DEFAULT[1] if provider == STRONG_DEFAULT[0] else None)
            base_url = props.get("model.strong.base_url")
            generic_key = "never"  # never send the weak provider's LLM_API_KEY to another provider
        try:
            p, url, key, m = resolve_llm(provider, model, base_url, env, generic_key, f"model.{name}.provider")
            profiles[name] = ModelProfile(name, p, m, url, key)
        except ConfigError as exc:
            profiles[name] = exc
    return profiles, props


class ModelSwitcher:
    """Creates (and caches) LLM clients for the profiles. One per server, shared by all chats."""

    def __init__(self, settings: Settings, start_llm=None):
        self.settings = settings
        self.start_llm = start_llm  # the client built at start-up from settings
        self._clients: dict[tuple, LLMClient] = {}

    def default_profile(self) -> str:
        _, props = load_profiles()
        name = (props.get("model.default") or "weak").strip().lower()
        return name if name in PROFILES else "weak"

    def resolve(self, name: str) -> tuple[ModelProfile, LLMClient]:
        profiles, _ = load_profiles()
        profile = profiles[name]
        if isinstance(profile, ConfigError):
            raise profile
        # A new key (or model, or URL) gives a new client; the same one is reused.
        fingerprint = hashlib.sha256(profile.api_key.encode()).hexdigest()[:16]
        key = (profile.base_url, profile.model, fingerprint)
        if key not in self._clients:
            self._clients[key] = LLMClient(profile.base_url, profile.api_key, profile.model)
        return profile, self._clients[key]

    def start(self, agent) -> str | None:
        """Put a new conversation on the default profile. Returns a warning if that failed."""
        agent.profile = "weak"
        agent.model_label = f"{self.settings.provider} / {self.settings.model}"
        default = self.default_profile()
        if default == "weak":
            return None
        reply = self.switch(agent, default, busy=False)
        return None if reply.ok else reply.text

    def switch(self, agent, name: str, busy: bool) -> ModelReply:
        if busy:
            return ModelReply("I can't switch the model while I'm working. Try again when I'm done.", ok=False)
        try:
            profile, client = self.resolve(name)
        except ConfigError as exc:
            return ModelReply(f"Can't switch to the {name} model: {exc}", ok=False)
        if agent.profile == name and agent.model_label == profile.label:
            return ModelReply(f"Already using the {name} model: {profile.label}.")
        before = agent.model_label.split(" / ")[0]
        agent.llm, agent.profile, agent.model_label = client, name, profile.label
        text = f"Switched to the {name} model: {profile.label}."
        if profile.provider != before:
            text += f" From now on this conversation is sent to {profile.provider}."
        return ModelReply(text, changed=True)

    def reset(self, agent) -> ModelReply | None:
        """After /new: back to the default profile (None if nothing changed)."""
        default = self.default_profile()
        if agent.profile == default:
            return None
        if default == "weak" and self.start_llm is not None:
            agent.llm, agent.profile = self.start_llm, "weak"
            agent.model_label = f"{self.settings.provider} / {self.settings.model}"
            return ModelReply(f"Back to the weak model: {agent.model_label}.", changed=True)
        return self.switch(agent, default, busy=False)


def handle_model_command(agent, switcher: ModelSwitcher, text: str, busy: bool) -> ModelReply | None:
    """The reply to /strongmodel, /weakmodel or /model; None if `text` is not a model command."""
    command = text.strip().lower()
    if command == "/model":
        return ModelReply(f"Current model: {agent.profile} ({agent.model_label}). Switch with {HELP}.")
    if command in COMMANDS:
        return switcher.switch(agent, COMMANDS[command], busy)
    return None
