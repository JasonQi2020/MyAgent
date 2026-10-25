import pytest

from agent_demo.tools import MAX_RESULT_CHARS, Tool, ToolRegistry, truncate


async def noop(args):
    return ""


def test_truncate_keeps_short_text():
    assert truncate("hello") == "hello"
    assert truncate("x" * MAX_RESULT_CHARS) == "x" * MAX_RESULT_CHARS


def test_truncate_cuts_long_text_with_note():
    result = truncate("x" * 120, limit=100)
    assert result.startswith("x" * 100)
    assert "truncated 20 characters" in result


def test_duplicate_name_is_rejected():
    registry = ToolRegistry()
    registry.register(Tool("a", "A", {"type": "object"}, noop))
    with pytest.raises(ValueError):
        registry.register(Tool("a", "again", {"type": "object"}, noop))


def test_openai_schemas_format_and_blocked_tools():
    registry = ToolRegistry()
    params = {"type": "object", "properties": {"q": {"type": "string"}}}
    registry.register(Tool("search", "Search things", params, noop))
    registry.register(Tool("nuke", "Never", {"type": "object"}, noop, blocked=True))

    assert registry.openai_schemas() == [
        {"type": "function", "function": {"name": "search", "description": "Search things", "parameters": params}}
    ]
    # Blocked tools stay in the registry (the UI lists them), but the model never sees them.
    assert [t.name for t in registry.all()] == ["search", "nuke"]
    assert registry.get("missing") is None
