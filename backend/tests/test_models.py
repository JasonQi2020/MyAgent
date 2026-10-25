"""Switching the model at runtime (design/model_switching.md), with temporary .env / properties files."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_demo.models as models
from agent_demo.config import Settings
from agent_demo.llm import LLMClient
from agent_demo.models import ModelSwitcher, handle_model_command, load_profiles
from test_im_hub import FakeChannel, dm, make_hub

KEYS = ["LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY", "AGENT_PROPERTIES"]


@pytest.fixture
def files(tmp_path, monkeypatch):
    """A temporary project: .env and agent.properties the switcher re-reads on every command."""
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(models, "PROJECT_ROOT", tmp_path)
    env, props = tmp_path / ".env", tmp_path / "agent.properties"
    monkeypatch.setenv("AGENT_PROPERTIES", str(props))
    env.write_text("LLM_PROVIDER=qwen\nDASHSCOPE_API_KEY=dash-key\n", encoding="utf-8")
    props.write_text("mail.provider=sina\n", encoding="utf-8")
    return SimpleNamespace(env=env, props=props)


def settings():
    return Settings("qwen", "https://dashscope.example/v1", "dash-key", "qwen-plus", Path("x.db"), 5, Path("m.json"))


def agent(llm=None):
    return SimpleNamespace(llm=llm or object(), profile="weak", model_label="qwen / qwen-plus")


# ---- profiles ---------------------------------------------------------------


def test_profiles_from_env_and_defaults(files):
    profiles, _ = load_profiles()
    weak, strong = profiles["weak"], profiles["strong"]
    assert (weak.provider, weak.model, weak.api_key) == ("qwen", "qwen-plus", "dash-key")
    assert isinstance(strong, Exception) and "OPENAI_API_KEY" in str(strong)  # no key yet


def test_profiles_from_properties(files):
    files.env.write_text("LLM_PROVIDER=qwen\nDASHSCOPE_API_KEY=d\nOPENAI_API_KEY=sk-test\nDEEPSEEK_API_KEY=ds\n")
    files.props.write_text("model.weak.provider=deepseek\nmodel.strong.name=gpt-4.1\n")
    profiles, _ = load_profiles()
    assert profiles["weak"].label == "deepseek / deepseek-chat"
    assert profiles["strong"].label == "openai / gpt-4.1" and profiles["strong"].api_key == "sk-test"


def test_strong_never_borrows_the_generic_key(files):
    files.env.write_text("LLM_PROVIDER=qwen\nLLM_API_KEY=generic-qwen-key\n")
    profiles, _ = load_profiles()
    assert profiles["weak"].api_key == "generic-qwen-key"
    # Without its own key the strong profile fails, instead of sending the Qwen key to OpenAI.
    assert isinstance(profiles["strong"], Exception) and "OPENAI_API_KEY" in str(profiles["strong"])
    assert "LLM_API_KEY" not in str(profiles["strong"])
    files.env.write_text("LLM_PROVIDER=qwen\nLLM_API_KEY=generic-qwen-key\nOPENAI_API_KEY=sk-own\n")
    assert load_profiles()[0]["strong"].api_key == "sk-own"  # its own key wins over LLM_API_KEY


def test_unknown_provider_names_the_setting(files):
    files.props.write_text("model.strong.provider=claude-direct\n")
    error = load_profiles()[0]["strong"]
    assert "model.strong.provider" in str(error)


# ---- switching without a restart ----------------------------------------------


def test_key_and_name_changes_apply_at_the_next_command(files):
    switcher, a = ModelSwitcher(settings()), agent()

    reply = handle_model_command(a, switcher, "/strongmodel", busy=False)
    assert not reply.ok and "OPENAI_API_KEY" in reply.text and a.profile == "weak"

    files.env.write_text(files.env.read_text() + "OPENAI_API_KEY=sk-secret-value\n")  # added while running
    reply = handle_model_command(a, switcher, "/StrongModel", busy=False)
    assert reply.ok and reply.changed and a.profile == "strong"
    assert reply.text == ("Switched to the strong model: openai / gpt-5.5. "
                          "From now on this conversation is sent to openai.")
    assert isinstance(a.llm, LLMClient) and a.llm.model == "gpt-5.5"
    assert "sk-secret-value" not in reply.text

    first = a.llm
    assert handle_model_command(a, switcher, "/strongmodel", busy=False).text.startswith("Already using")
    files.props.write_text("model.strong.name=gpt-4.1\n")  # edited while running
    handle_model_command(a, switcher, "/strongmodel", busy=False)
    assert a.llm.model == "gpt-4.1" and a.llm is not first and a.model_label == "openai / gpt-4.1"


def test_clients_are_cached_per_key(files):
    files.env.write_text("LLM_PROVIDER=qwen\nDASHSCOPE_API_KEY=d\nOPENAI_API_KEY=sk-1\n")
    switcher = ModelSwitcher(settings())
    _, c1 = switcher.resolve("strong")
    _, c2 = switcher.resolve("strong")
    files.env.write_text("LLM_PROVIDER=qwen\nDASHSCOPE_API_KEY=d\nOPENAI_API_KEY=sk-2\n")
    _, c3 = switcher.resolve("strong")
    assert c1 is c2 and c3 is not c1


def test_model_command_busy_and_other_text(files):
    files.env.write_text("LLM_PROVIDER=qwen\nDASHSCOPE_API_KEY=d\nOPENAI_API_KEY=sk\n")
    switcher, a = ModelSwitcher(settings()), agent()
    assert handle_model_command(a, switcher, "hello", busy=False) is None
    assert handle_model_command(a, switcher, "/model", busy=True).text.startswith("Current model: weak (qwen / qwen-plus)")
    refused = handle_model_command(a, switcher, "/strongmodel", busy=True)
    assert not refused.ok and "while I'm working" in refused.text and a.profile == "weak"


def test_reset_and_default_profile(files):
    files.env.write_text("LLM_PROVIDER=qwen\nDASHSCOPE_API_KEY=d\nOPENAI_API_KEY=sk\n")
    start = object()
    switcher = ModelSwitcher(settings(), start_llm=start)
    a = agent(start)
    handle_model_command(a, switcher, "/strongmodel", busy=False)
    reply = switcher.reset(a)  # /new
    assert a.llm is start and a.profile == "weak" and reply.text.startswith("Back to the weak model")
    assert switcher.reset(a) is None  # nothing to change

    files.props.write_text("model.default=strong\n")
    b = agent()
    assert switcher.start(b) is None and b.profile == "strong"
    files.env.write_text("LLM_PROVIDER=qwen\nDASHSCOPE_API_KEY=d\n")  # key removed
    c = agent()
    assert "OPENAI_API_KEY" in switcher.start(c) and c.profile == "weak"


# ---- per conversation in the IM hub -------------------------------------------


async def test_switch_applies_to_one_chat_only(files):
    files.env.write_text("LLM_PROVIDER=qwen\nDASHSCOPE_API_KEY=d\nOPENAI_API_KEY=sk\n")
    hub, ch = make_hub([], channel=FakeChannel(allowed_users=("ou_owner", "ou_amy")))
    await hub.on_message(dm("/strongmodel", "m1"))
    await hub.on_message(dm("/model", "m2", user="ou_amy"))
    mine, amy = hub.sessions[("fake", "dm-ou_owner")].agent, hub.sessions[("fake", "dm-ou_amy")].agent
    assert mine.profile == "strong" and amy.profile == "weak"
    assert ch.texts()[0].startswith("Switched to the strong model: openai / gpt-5.5")
    assert ch.texts()[1].startswith("Current model: weak")
    await hub.on_message(dm("/new", "m3"))
    assert mine.profile == "weak" and "Back to the weak model" in ch.texts()[-1]


async def test_hub_reports_a_missing_key(files):
    hub, ch = make_hub([])
    await hub.on_message(dm("/strongmodel", "m1"))
    assert ch.texts()[-1].startswith("⚠ Can't switch to the strong model") and "OPENAI_API_KEY" in ch.texts()[-1]


# ---- the web UI ----------------------------------------------------------------


def test_web_model_command_is_answered_by_the_server(files, monkeypatch):
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    import agent_demo.server as server
    from test_server import make_runtime

    files.env.write_text("LLM_PROVIDER=qwen\nDASHSCOPE_API_KEY=d\nOPENAI_API_KEY=sk\n")
    runtime = make_runtime([])

    @asynccontextmanager
    async def fake_open_runtime(_settings):
        yield runtime

    monkeypatch.delenv("IM_CHANNELS", raising=False)
    monkeypatch.setattr(server, "load_settings", lambda: None)
    monkeypatch.setattr(server, "open_runtime", fake_open_runtime)
    with TestClient(server.app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "user_message", "text": "/strongmodel"})
        event = ws.receive_json()
        assert event["type"] == "model_changed" and event["profile"] == "strong"
        assert event["model"] == "openai / gpt-5.5" and event["message"].startswith("Switched")
        assert runtime.llm.calls == []  # the command never reached the LLM
        ws.send_json({"type": "reset"})
        back = ws.receive_json()
        assert back["type"] == "model_changed" and back["profile"] == "weak"
    json.dumps(event)
