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



def test_audit_commands_list_filtered_rows(cli: Cli, bot) -> None:
    """The audit dumps read through kernel.inspector (B-78): same filters
    (--days, --symbiote, --session, --trigger), same table and panels."""
    from symbiote.adapters.storage.sqlite import SQLiteAdapter
    from symbiote.core.sql import days_ago

    sym, session = bot
    recent, old = days_ago(1), days_ago(30)
    adp = SQLiteAdapter(db_path=cli.db)
    for rid, sym_id, kw, at in (
        ("r1", sym, '["likes tea", "lives in Lisbon"]', recent),
        ("r2", sym, "{not json", recent),  # undecodable → counted as 0, no diff panel
        ("r3", sym, '["too old"]', old),
        ("r4", "other-sym", '["elsewhere"]', recent),
    ):
        adp.execute(
            "INSERT INTO reflection_audit (id, session_id, symbiote_id, mode, "
            "keyword_facts_json, llm_facts_json, created_at) VALUES (?, ?, ?, 'hybrid', ?, '[]', ?)",
            (rid, session, sym_id, kw, at),
        )
    for kid, trigger, at in (("k1", "final", recent), ("k2", "nudge", recent), ("k3", "final", old)):
        adp.execute(
            "INSERT INTO skill_review_audit (id, session_id, symbiote_id, trigger, applied, ok, "
            "ops_json, created_at) VALUES (?, ?, ?, ?, 1, 1, ?, ?)",
            (kid, session, sym, trigger, f'[{{"op": "{kid}-op"}}]', at),
        )
    adp.close()

    out = cli("audit", "reflection", "--symbiote", sym, "--diff").output
    assert "2 rows" in out
    assert "lives in Lisbon" in out and "too old" not in out and "elsewhere" not in out
    assert "4 rows" in cli("audit", "reflection", "--days", "60").output  # every symbiote
    assert "3 rows" in cli("audit", "reflection", "--days", "60", "--limit", "3").output

    out = cli("audit", "skill-review", "--session", session, "--trigger", "final", "--ops").output
    assert "1 rows" in out and "k1-op" in out and "k2-op" not in out

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
