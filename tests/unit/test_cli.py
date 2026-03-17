"""Tests for CLI — all 6 value tracks + management commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from symbiote.cli.main import app

runner = CliRunner()


# ── helpers ────────────────────────────────────────────────────────────────


def invoke(*args: str, db_path: Path | None = None) -> object:
    """Invoke the CLI with optional --db-path prepended."""
    cmd: list[str] = []
    if db_path is not None:
        cmd += ["--db-path", str(db_path)]
    cmd += list(args)
    return runner.invoke(app, cmd)


def _extract_id(output: str) -> str:
    """Extract a UUID-like ID from CLI output."""
    for line in output.strip().splitlines():
        for token in line.split():
            if token.count("-") >= 4 and len(token) >= 32:
                return token
    raise ValueError(f"Could not extract ID from output:\n{output}")


def _setup_session(db: Path) -> tuple[str, str]:
    """Create a symbiote + session, return (symbiote_id, session_id)."""
    cr = invoke("create", "--name", "TestBot", "--role", "assistant", db_path=db)
    sym_id = _extract_id(cr.output)
    sr = invoke("session", "start", sym_id, "--goal", "testing", db_path=db)
    sess_id = _extract_id(sr.output)
    return sym_id, sess_id


# ── create ─────────────────────────────────────────────────────────────────


class TestCreate:
    def test_create_valid(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke("create", "--name", "Alice", "--role", "coder", db_path=db)
        assert result.exit_code == 0, result.output
        assert "-" in result.output

    def test_create_with_persona(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke("create", "--name", "Bob", "--role", "analyst",
                        "--persona-json", '{"tone": "formal"}', db_path=db)
        assert result.exit_code == 0, result.output


# ── list ───────────────────────────────────────────────────────────────────


class TestList:
    def test_list_empty(self, tmp_path: Path) -> None:
        result = invoke("list", db_path=tmp_path / "test.db")
        assert result.exit_code == 0

    def test_list_after_create(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        invoke("create", "--name", "Alice", "--role", "coder", db_path=db)
        result = invoke("list", db_path=db)
        assert result.exit_code == 0
        assert "Alice" in result.output


# ── session ────────────────────────────────────────────────────────────────


class TestSession:
    def test_session_start(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        assert len(sess_id) >= 32

    def test_session_close(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        result = invoke("session", "close", sess_id, db_path=db)
        assert result.exit_code == 0
        assert "closed" in result.output.lower() or "summary" in result.output.lower()


# ══════════════════════════════════════════════════════════════════════════
# VALUE TRACKS
# ══════════════════════════════════════════════════════════════════════════


class TestChat:
    def test_chat_with_mock_llm(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        result = invoke("--llm", "mock", "chat", sess_id, "Hello!", db_path=db)
        assert result.exit_code == 0, result.output
        assert "mock" in result.output.lower() or "assistant" in result.output.lower()

    def test_chat_invalid_session(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke("--llm", "mock", "chat", "bad-session-id", "Hi", db_path=db)
        assert result.exit_code == 1


class TestLearn:
    def test_learn_fact(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        result = invoke("learn", sess_id, "User prefers dark mode", db_path=db)
        assert result.exit_code == 0, result.output
        assert "learned" in result.output.lower() or "-" in result.output

    def test_learn_with_type_and_importance(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        result = invoke("learn", sess_id, "Always use pytest",
                        "--type", "procedural", "--importance", "0.9", db_path=db)
        assert result.exit_code == 0, result.output


class TestTeach:
    def test_teach_no_data(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        result = invoke("teach", sess_id, "quantum physics", db_path=db)
        assert result.exit_code == 0, result.output

    def test_teach_with_learned_data(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        # First learn something
        invoke("learn", sess_id, "Python uses indentation for blocks", db_path=db)
        # Then ask to teach about it
        result = invoke("teach", sess_id, "Python", db_path=db)
        assert result.exit_code == 0, result.output


class TestWork:
    def test_work_with_chat_runner(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        result = invoke("--llm", "mock", "work", sess_id, "chat: tell me something",
                        "--intent", "chat", db_path=db)
        assert result.exit_code == 0, result.output

    def test_work_no_runner(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        result = invoke("--llm", "mock", "work", sess_id, "unknown_intent: do something", db_path=db)
        assert result.exit_code == 1


class TestShow:
    def test_show_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        result = invoke("show", sess_id, "anything", db_path=db)
        assert result.exit_code == 0, result.output

    def test_show_with_data(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        invoke("learn", sess_id, "Important searchable fact", db_path=db)
        result = invoke("show", sess_id, "searchable", db_path=db)
        assert result.exit_code == 0, result.output


class TestReflect:
    def test_reflect_empty_session(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        result = invoke("reflect", sess_id, db_path=db)
        assert result.exit_code == 0, result.output

    def test_reflect_after_messages(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        # Add some messages via chat
        invoke("--llm", "mock", "chat", sess_id, "I prefer dark mode", db_path=db)
        invoke("--llm", "mock", "chat", sess_id, "Always use type hints", db_path=db)
        result = invoke("reflect", sess_id, db_path=db)
        assert result.exit_code == 0, result.output


# ── memory + export ────────────────────────────────────────────────────────


class TestMemorySearch:
    def test_no_results(self, tmp_path: Path) -> None:
        result = invoke("memory", "search", "nonexistent", db_path=tmp_path / "test.db")
        assert result.exit_code == 0


class TestExport:
    def test_export_session(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        _, sess_id = _setup_session(db)
        result = invoke("export", "session", sess_id, db_path=db)
        assert result.exit_code == 0, result.output


# ── error handling ─────────────────────────────────────────────────────────


class TestErrorHandling:
    def test_session_start_invalid_symbiote(self, tmp_path: Path) -> None:
        result = invoke("session", "start", "nonexistent", db_path=tmp_path / "test.db")
        assert result.exit_code == 1

    def test_session_close_invalid(self, tmp_path: Path) -> None:
        result = invoke("session", "close", "nonexistent", db_path=tmp_path / "test.db")
        assert result.exit_code == 1


# ── Interactive Chat (B-2) ────────────────────────────────────────────────


class TestInteractiveChat:
    def test_interactive_nonexistent_symbiote(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        # Create DB by invoking any command first
        invoke("list", db_path=db)
        result = invoke("--llm", "mock", "interactive", "nonexistent", db_path=db)
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_interactive_with_quit(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        cr = invoke("create", "--name", "ChatBot", "--role", "assistant", db_path=db)
        sym_id = _extract_id(cr.output)

        # Simulate /quit as input
        result = runner.invoke(
            app,
            ["--db-path", str(db), "--llm", "mock", "interactive", sym_id],
            input="/quit\n",
        )
        assert result.exit_code == 0
        assert "interactive" in result.output.lower() or "session" in result.output.lower()

    def test_interactive_with_message_then_quit(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        cr = invoke("create", "--name", "TestBot", "--role", "assistant", db_path=db)
        sym_id = _extract_id(cr.output)

        result = runner.invoke(
            app,
            ["--db-path", str(db), "--llm", "mock", "interactive", sym_id],
            input="Hello!\n/quit\n",
        )
        assert result.exit_code == 0
        # Should contain the mock LLM response
        assert "mock" in result.output.lower() or "session" in result.output.lower()

    def test_interactive_by_name(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        invoke("create", "--name", "NameBot", "--role", "assistant", db_path=db)

        result = runner.invoke(
            app,
            ["--db-path", str(db), "--llm", "mock", "interactive", "NameBot"],
            input="/quit\n",
        )
        assert result.exit_code == 0
