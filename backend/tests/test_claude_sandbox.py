"""The bwrap sandbox for /claude (design/claude_code_delegation.md §11), without starting Claude Code."""

import json
import os
import subprocess
import sys
from pathlib import Path

import agent_demo.claude_code as cc
from agent_demo.claude_code import ClaudeCodeSession, bwrap_args, run_claude, write_launcher
from test_claude_code import FakeClaude, no_approval, settings


def pairs(args, flag):
    return [(args[i + 1], args[i + 2]) for i, a in enumerate(args) if a == flag]


def test_bwrap_args_expose_only_the_folders(tmp_path):
    data, other = tmp_path / "data", tmp_path / "more"
    args = bwrap_args([data, other], Path("/opt/x/claude-2.1"), tmp_path / "home")
    assert args[0] == "bwrap"
    assert (str(data), str(data)) in pairs(args, "--bind") and (str(other), str(other)) in pairs(args, "--bind")
    assert (str(tmp_path / "home"), "/sandbox-home") in pairs(args, "--bind")
    writable = {target for _, target in pairs(args, "--bind")}
    assert writable == {str(data), str(other), "/sandbox-home"}  # nothing else is writable
    assert ("/usr", "/usr") in pairs(args, "--ro-bind") and ("/opt/x/claude-2.1", "/opt/claude") in pairs(args, "--ro-bind")
    visible = {target for flag in ("--bind", "--ro-bind") for _, target in pairs(args, flag)}
    assert not any(v.startswith("/home") for v in visible - {str(data), str(other)})  # no home folders
    for flag in ("--unshare-all", "--share-net", "--die-with-parent", "--new-session"):
        assert flag in args
    assert ("HOME", "/sandbox-home") in pairs(args, "--setenv")


def test_launcher_clears_the_environment(tmp_path, monkeypatch):
    """Run the generated launcher with a fake bwrap that prints its arguments."""
    fake_bwrap = tmp_path / "fake-bwrap"
    fake_bwrap.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    fake_bwrap.chmod(0o755)
    monkeypatch.setattr(cc, "bwrap_args", lambda folders, binary, home: [str(fake_bwrap), "--setenv", "HOME", "/sandbox-home"])
    monkeypatch.setattr(cc, "claude_binary", lambda: Path("/opt/x/claude"))
    launcher = write_launcher([tmp_path / "data"], directory=tmp_path / "sbx")
    assert oct(launcher.stat().st_mode & 0o777) == "0o700"

    env = {"PATH": os.environ["PATH"], "CLAUDE_CODE_OAUTH_TOKEN": "tok", "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
           "DASHSCOPE_API_KEY": "secret-1", "OPENAI_API_KEY": "secret-2", "FEISHU_APP_SECRET": "secret-3",
           "SINA_AUTH_CODE": "secret-4", "LANG": "C.UTF-8"}
    out = subprocess.run([str(launcher), "--output-format", "stream-json"], env=env, capture_output=True,
                         text=True, check=True).stdout
    args = json.loads(out)
    assert args[0] == "--clearenv"  # before every --setenv, including HOME
    passed = dict(pairs(["x"] + args, "--setenv"))
    assert passed["CLAUDE_CODE_OAUTH_TOKEN"] == "tok" and passed["LANG"] == "C.UTF-8" and passed["HOME"] == "/sandbox-home"
    assert not any(v.startswith("secret") for v in passed.values())  # .env keys never enter the sandbox
    assert args[-3:] == ["/opt/claude", "--output-format", "stream-json"]
    assert "tok" not in launcher.read_text()  # the token is not written to disk


async def test_sandbox_on_uses_the_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "SANDBOX_DIR", tmp_path / "sbx")
    monkeypatch.setattr(cc, "write_launcher", lambda folders: tmp_path / "sbx" / "bin" / "claude")
    monkeypatch.setattr(cc, "claude_binary", lambda: Path("/opt/x/claude"))
    monkeypatch.setattr(cc.shutil, "which", lambda name: f"/usr/bin/{name}")
    fake = FakeClaude([])
    await run_claude(ClaudeCodeSession(), "hi", settings(tmp_path, sandbox=True), no_approval, None, query=fake)
    assert fake.options[0].cli_path == str(tmp_path / "sbx" / "bin" / "claude")

    off = FakeClaude([])
    await run_claude(ClaudeCodeSession(), "hi", settings(tmp_path, sandbox=False), no_approval, None, query=off)
    assert off.options[0].cli_path is None


async def test_fail_closed_without_bwrap(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: None if name == "bwrap" else f"/usr/bin/{name}")
    fake = FakeClaude([])
    result = await run_claude(ClaudeCodeSession(), "hi", settings(tmp_path, sandbox=True), no_approval, None, query=fake)
    assert not result.ok and "bwrap is not installed" in result.text and fake.options == []  # never started


def test_sandbox_setting(tmp_path, monkeypatch):
    import agent_demo.models as models

    monkeypatch.setattr(models, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=t\n")
    props = tmp_path / "agent.properties"
    monkeypatch.setenv("AGENT_PROPERTIES", str(props))
    for text, expected in [("", True), ("claude.sandbox=off\n", False), ("claude.sandbox=OFF\n", False),
                           ("claude.sandbox=maybe\n", True)]:  # anything but off keeps it on
        props.write_text(text)
        assert cc.load_claude_settings().sandbox is expected, text
