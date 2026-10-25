"""Terminal UI. Implements the EventSink (print) and Approver (y/N prompt) interfaces."""

from __future__ import annotations

import argparse
import asyncio
import json

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from .agent import Agent
from .config import MAIL_PROVIDERS, ConfigError, load_settings
from .events import (
    ApprovalRequest,
    AssistantText,
    ErrorEvent,
    Event,
    ToolCallEvent,
    ToolResultEvent,
    TurnDone,
)
from . import claude_code
from .models import handle_model_command
from .router import route
from .schedule_tools import TASKS_PROMPT
from .tasks import load_task_settings, owner_chat, tasks_command
from .runtime import Runtime, open_runtime

console = Console()


def _short(value: object, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def print_event(event: Event) -> None:
    if isinstance(event, AssistantText):
        if event.reasoning:
            console.print(Panel(event.reasoning, title="reasoning", style="dim", expand=False))
        if event.text:
            console.print(Markdown(event.text))
    elif isinstance(event, ToolCallEvent):
        console.print(f"[dim]→ {event.name}({_short(event.arguments)})[/dim]")
    elif isinstance(event, ToolResultEvent):
        mark = "[red]✗[/red]" if event.is_error else "[green]✓[/green]"
        console.print(f"[dim]  {mark} {_short(event.result)}[/dim]")
    elif isinstance(event, ErrorEvent):
        console.print(f"[bold red]Error:[/bold red] {event.message}")
    elif isinstance(event, TurnDone):
        console.print(
            f"[dim]({event.steps} steps, {event.prompt_tokens}+{event.completion_tokens} tokens)[/dim]\n"
        )


async def ask_approval(request: ApprovalRequest) -> bool:
    args = json.dumps(request.arguments, ensure_ascii=False, indent=2)
    console.print(
        Panel(args, title=f"[bold yellow]Approval needed: {request.name}[/bold yellow]", expand=False)
    )
    # input() blocks, so run it in a thread to keep the event loop (and MCP) alive.
    return await asyncio.to_thread(Confirm.ask, "Approve this action?", default=False)


def show_tools(runtime: Runtime) -> None:
    table = Table(title="Tools")
    table.add_column("name")
    table.add_column("source")
    table.add_column("policy")
    for t in runtime.registry.all():
        policy = "blocked" if t.blocked else ("ask" if t.needs_approval else "auto")
        table.add_row(t.name, t.source, policy)
    console.print(table)


async def repl(provider: str | None, model: str | None, mail: str | None) -> None:
    try:
        settings = load_settings(provider, model, mail)
    except ConfigError as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        return

    with console.status("Starting tools and MCP servers..."):
        runtime_cm = open_runtime(settings)
        runtime = await runtime_cm.__aenter__()
    try:
        # The CLI user is the owner: /claude and scheduled tasks (reports go to the owner's Feishu DM;
        # the tasks themselves run in agent-demo-server).
        agent = Agent(
            runtime.llm,
            runtime.registry.with_tools(runtime.tasks().tools(target=owner_chat, created_by="cli",
                                                              model=lambda: agent.profile)),
            print_event, ask_approval, settings.max_steps,
            system_prompt=runtime.system_prompt() + claude_code.ASSISTANT_PROMPT
            + TASKS_PROMPT.format(tz=load_task_settings().timezone),
        )
        models = runtime.models()
        model_warning = models.start(agent)
        claude = claude_code.ClaudeCodeSession()
        console.print(
            f"[bold]agent-demo[/bold] · {agent.model_label} ({agent.profile}) · "
            f"{len(runtime.registry.all())} tools · mail: {settings.mail_label} · "
            "commands: /tools /new /strongmodel /weakmodel /model /tasks /claude /exit"
        )
        for warning in [*runtime.warnings, *([model_warning] if model_warning else [])]:
            console.print(f"[yellow]⚠ {warning}[/yellow]")

        while True:
            label = "[bold magenta]claude> [/bold magenta]" if claude.mode else "[bold cyan]you> [/bold cyan]"
            try:
                text = (await asyncio.to_thread(console.input, label)).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text in ("/exit", "/quit"):
                break
            target = route(text, claude.mode)
            if target.kind == "assistant":
                await agent.run_turn(text)
            elif target.kind == "command":
                run_command(target.name, text, agent, models, claude, runtime)
            else:
                await run_claude_message(target, agent, claude, runtime)
    finally:
        await runtime_cm.__aexit__(None, None, None)


def run_command(command: str, text: str, agent: Agent, models, claude, runtime: Runtime) -> None:
    if command == "/new":
        agent.reset()
        claude.reset()
        console.print("[dim]Conversation cleared.[/dim]")
        if reply := models.reset(agent):
            console.print(f"[dim]{reply.text}[/dim]")
    elif command == "/tools":
        show_tools(runtime)
    elif command in ("/help", "help"):
        console.print(f"Commands: /tools /new /strongmodel /weakmodel /model /tasks /exit. Claude Code: {claude_code.HELP}.")
    elif command == "/tasks":
        service = runtime.tasks()
        console.print(tasks_command(service.store, service.scheduler, text.strip()[len("/tasks"):].strip()))
    elif reply := handle_model_command(agent, models, text, busy=False):
        text = reply.text
        if command == "/model":
            text += f" · {claude.status(claude_code.load_claude_settings())}."
        elif reply.changed and claude.mode:
            text += " Claude mode is on, so your messages still go to Claude Code (/claude off to talk to the assistant)."
        console.print(f"[green]{text}[/green]" if reply.ok else f"[yellow]{text}[/yellow]")
    else:
        console.print(f"[yellow]{command} only works in group chats.[/yellow]")


async def run_claude_message(target, agent: Agent, claude, runtime: Runtime) -> None:
    """/claude …, or a message in Claude mode (design/claude_code_delegation.md)."""
    settings = claude_code.load_claude_settings()
    prompt = target.text
    if target.kind == "claude_command":
        reply, prompt = claude_code.command_reply(claude, target.name, target.text, settings)
        if reply:
            console.print(f"[magenta]{reply}[/magenta]")
        if prompt is None:
            return

    async def progress(line: str) -> None:
        console.print(f"[dim]  🤖 {line}[/dim]")

    folder = claude.current_folder(settings)
    try:
        mcp, previews = runtime.tasks().claude_mcp(target=owner_chat, created_by="cli")
        result = await claude_code.run_claude(claude, prompt, settings, ask_approval, progress,
                                              mcp_servers=mcp, previews=previews)
    except asyncio.CancelledError:  # Ctrl+C while it works
        result = claude_code.ClaudeResult(False, "Stopped.")
    console.print(Markdown(claude_code.format_answer(result)))
    agent.add_note(claude_code.memory_note(prompt, folder, result))


def main() -> None:
    parser = argparse.ArgumentParser(description="Learning agent demo (CLI)")
    parser.add_argument("--provider", help="deepseek, openrouter, qwen, openai, ollama, custom")
    parser.add_argument("--model", help="override the model name")
    parser.add_argument("--mail", help="mail provider for this run: " + ", ".join(MAIL_PROVIDERS))
    args = parser.parse_args()
    try:
        asyncio.run(repl(args.provider, args.model, args.mail))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
