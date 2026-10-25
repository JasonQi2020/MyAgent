"""The CLI: imports and its command handling (it had no test, so a syntax error once went unnoticed)."""

import importlib

import agent_demo.cli as cli
from agent_demo.claude_code import ClaudeCodeSession
from agent_demo.llm import LLMReply
from test_im_hub import make_hub


def test_cli_and_server_import():
    importlib.reload(cli)
    import agent_demo.server  # noqa: F401


def test_run_command_tasks_and_help(capsys):
    hub, _ = make_hub([LLMReply(content="x")])
    runtime = hub.runtime
    agent = cli.Agent(runtime.llm, runtime.registry, cli.print_event, cli.ask_approval)
    claude = ClaudeCodeSession()
    cli.run_command("/tasks", "/tasks", agent, runtime.models(), claude, runtime)
    cli.run_command("/help", "/help", agent, runtime.models(), claude, runtime)
    out = capsys.readouterr().out
    assert "No scheduled tasks" in out and "/tasks" in out
