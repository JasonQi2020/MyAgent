"""WebSocket protocol test: approval round trip through the web server, with a fake LLM."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_demo.server as server
from agent_demo.config import Settings
from agent_demo.llm import LLMReply, ToolCall
from agent_demo.runtime import Runtime
from agent_demo.tools import Tool, ToolRegistry
from test_agent import FakeLLM


def make_runtime(sent):
    async def send_email(args):
        sent.append(args)
        return "sent"

    registry = ToolRegistry()
    registry.register(
        Tool("gmail__send_email", "Send", {"type": "object"}, send_email, needs_approval=True, source="mcp:gmail")
    )
    llm = FakeLLM(
        [
            LLMReply(content=None, tool_calls=[ToolCall("c1", "gmail__send_email", '{"to": "me@x.com"}')]),
            LLMReply(content="Email sent."),
        ]
    )
    settings = Settings("fake", "http://x", "k", "fake-model", Path("x.db"), 5, Path("none.json"))
    return Runtime(settings, llm, registry, [])


def test_approval_round_trip(monkeypatch):
    sent = []

    @asynccontextmanager
    async def fake_open_runtime(settings):
        yield make_runtime(sent)

    monkeypatch.setattr(server, "load_settings", lambda: None)
    monkeypatch.setattr(server, "open_runtime", fake_open_runtime)

    with TestClient(server.app) as client, client.websocket_connect("/ws") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready" and ready["tools"][0]["needs_approval"] is True

        ws.send_json({"type": "user_message", "text": "email me"})
        assert ws.receive_json()["type"] == "tool_call"
        request = ws.receive_json()
        assert request == {
            "type": "approval_request",
            "id": "c1",
            "name": "gmail__send_email",
            "arguments": {"to": "me@x.com"},
        }

        ws.send_json({"type": "approval_response", "id": "c1", "approved": True})
        result = ws.receive_json()
        assert result["type"] == "tool_result" and result["is_error"] is False
        assert ws.receive_json() == {"type": "assistant_text", "text": "Email sent.", "reasoning": None}
        assert ws.receive_json()["type"] == "turn_done"

    assert sent == [{"to": "me@x.com"}]


def test_unknown_message_type(monkeypatch):
    @asynccontextmanager
    async def fake_open_runtime(settings):
        yield make_runtime([])

    monkeypatch.setattr(server, "load_settings", lambda: None)
    monkeypatch.setattr(server, "open_runtime", fake_open_runtime)

    with TestClient(server.app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # ready
        ws.send_json({"type": "bogus"})
        assert ws.receive_json() == {"type": "error", "message": "Unknown message type: 'bogus'"}


def test_web_ui_index_is_never_cached(tmp_path):
    # A cached index.html would point at deleted bundles after a rebuild (blank page).
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "assets" / "app-123.js").write_text("console.log(1)")
    app = FastAPI()
    app.mount("/", server.WebUIFiles(directory=tmp_path, html=True))

    client = TestClient(app)
    assert client.get("/").headers["cache-control"] == "no-cache"
    asset = client.get("/assets/app-123.js")
    assert asset.status_code == 200 and "cache-control" not in asset.headers
