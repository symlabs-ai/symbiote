"""End-to-end flows of the local CLI commands against a temporary database
(mock LLM, no network). Covers the happy paths that had no test (report item
C-3: cli/main.py was at 48% coverage)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from symbiote.cli.main import app

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Cli:
    def __init__(self, db: Path) -> None:
        self.db = db
        self.runner = CliRunner()

    def __call__(self, *args: str, ok: bool = True):
        result = self.runner.invoke(
            app, ["--db-path", str(self.db), "--llm", "mock", *args], catch_exceptions=False
        )
        if ok:
            assert result.exit_code == 0, result.output
        return result

    def uuid(self, *args: str) -> str:
        return _UUID.search(self(*args).output).group(0)


@pytest.fixture()
def cli(tmp_path: Path) -> Cli:
    return Cli(tmp_path / "cli.db")


@pytest.fixture()
def bot(cli: Cli) -> tuple[str, str]:
    sym = cli.uuid("create", "--name", "bot", "--role", "assistant",
                   "--persona-json", '{"tone": "dry"}')
    session = cli.uuid("session", "start", sym, "--goal", "test", "--external-key", "user:1")
    return sym, session


def test_symbiote_and_session_lifecycle(cli: Cli, bot) -> None:
    sym, session = bot
    assert "bot" in cli("list").output
    found = cli("session", "find", "user:1").output
    assert session in found and "user:1" in found
    cli("chat", session, "hello")
    cli("learn", session, "prefers tea", "--type", "preference", "--importance", "0.8")
    assert "prefers tea" in cli("memory", "search", "tea", "--symbiote", sym).output
    cli("reflect", session)
    assert "hello" in cli("export", "session", session).output
    assert "closed" in cli("session", "close", session).output


def test_session_find_unknown_key_says_so(cli: Cli) -> None:
    assert "No session found" in cli("session", "find", "nobody").output


def test_tool_commands(cli: Cli, bot) -> None:
    sym, _session = bot
    cli("tools", "add", sym, "--id", "echo_tool", "--name", "Echo", "--desc", "echo",
        "--url", "http://127.0.0.1:9/echo/{id}", "--params-json",
        '{"type": "object", "properties": {"id": {"type": "string"}}}')
    assert "echo_tool" in cli("tools", "list", sym).output

    # Not authorized for fs_write → blocked, reported as a failure.
    blocked = cli("tools", "exec", sym, "fs_write", "--params", '{"path": "x", "content": "y"}', ok=False)
    assert blocked.exit_code != 0
    assert "fs_write" in cli("tools", "audit", sym).output

    bad_json = cli("tools", "exec", sym, "echo_tool", "--params", "{", ok=False)
    assert bad_json.exit_code != 0 and "Traceback" not in bad_json.output

    cli("tools", "remove", sym, "echo_tool")
    assert "echo_tool" not in cli("tools", "list", sym).output


def test_audit_commands(cli: Cli, bot) -> None:
    sym, session = bot
    cli("audit", "reflection", "--symbiote", sym, "--diff")
    cli("audit", "skill-review", "--session", session, "--ops")
    prune = cli("audit", "prune", "--days", "30", "--dry-run").output
    assert "dry-run" in prune


def test_skills_commands(cli: Cli, tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = root / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy\ndescription: ship it\n---\nsteps\n")

    listed = cli("skills", "list", "--root", str(root), "--all").output
    assert "deploy" in listed
    cli("skills", "promote", "deploy", "--root", str(root))
    cli("skills", "pin", "deploy", "--root", str(root))
    cli("skills", "pin", "deploy", "--unpin", "--root", str(root))
    missing = cli("skills", "promote", "nope", "--root", str(root), ok=False)
    assert missing.exit_code != 0
