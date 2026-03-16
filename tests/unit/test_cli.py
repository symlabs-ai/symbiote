"""Tests for CLI — T-21."""

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


# ── create ─────────────────────────────────────────────────────────────────


class TestCreate:
    def test_create_valid(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke("create", "--name", "Alice", "--role", "coder", db_path=db)
        assert result.exit_code == 0, result.output
        # Output should contain a UUID-like ID
        assert "-" in result.output  # UUID contains dashes

    def test_create_with_persona_json(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke(
            "create",
            "--name",
            "Bob",
            "--role",
            "analyst",
            "--persona-json",
            '{"tone": "formal"}',
            db_path=db,
        )
        assert result.exit_code == 0, result.output


# ── list ───────────────────────────────────────────────────────────────────


class TestList:
    def test_list_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke("list", db_path=db)
        assert result.exit_code == 0, result.output

    def test_list_after_create(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        invoke("create", "--name", "Alice", "--role", "coder", db_path=db)
        result = invoke("list", db_path=db)
        assert result.exit_code == 0, result.output
        assert "Alice" in result.output
        assert "coder" in result.output


# ── session start ──────────────────────────────────────────────────────────


class TestSessionStart:
    def test_session_start(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        # Create a symbiote first
        create_result = invoke(
            "create", "--name", "Alice", "--role", "coder", db_path=db
        )
        # Extract symbiote ID from output
        symbiote_id = _extract_id(create_result.output)

        result = invoke("session", "start", symbiote_id, db_path=db)
        assert result.exit_code == 0, result.output
        assert "-" in result.output  # session UUID

    def test_session_start_with_goal(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        create_result = invoke(
            "create", "--name", "Alice", "--role", "coder", db_path=db
        )
        symbiote_id = _extract_id(create_result.output)

        result = invoke(
            "session", "start", symbiote_id, "--goal", "Fix bug #42", db_path=db
        )
        assert result.exit_code == 0, result.output


# ── session close ──────────────────────────────────────────────────────────


class TestSessionClose:
    def test_session_close(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        # Create symbiote + session
        create_result = invoke(
            "create", "--name", "Alice", "--role", "coder", db_path=db
        )
        symbiote_id = _extract_id(create_result.output)

        session_result = invoke("session", "start", symbiote_id, db_path=db)
        session_id = _extract_id(session_result.output)

        result = invoke("session", "close", session_id, db_path=db)
        assert result.exit_code == 0, result.output
        # Should contain some summary info
        assert "closed" in result.output.lower() or "summary" in result.output.lower()


# ── message ────────────────────────────────────────────────────────────────


class TestMessage:
    def test_message_send(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        # Create symbiote + session
        create_result = invoke(
            "create", "--name", "Alice", "--role", "coder", db_path=db
        )
        symbiote_id = _extract_id(create_result.output)

        session_result = invoke("session", "start", symbiote_id, db_path=db)
        session_id = _extract_id(session_result.output)

        result = invoke("message", session_id, "Hello world", db_path=db)
        assert result.exit_code == 0, result.output
        assert "message" in result.output.lower() or "stored" in result.output.lower()


# ── memory search ──────────────────────────────────────────────────────────


class TestMemorySearch:
    def test_memory_search_no_results(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke("memory", "search", "nonexistent", db_path=db)
        assert result.exit_code == 0, result.output

    def test_memory_search_with_scope(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke(
            "memory", "search", "test", "--scope", "global", db_path=db
        )
        assert result.exit_code == 0, result.output

    def test_memory_search_with_limit(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke(
            "memory", "search", "test", "--limit", "5", db_path=db
        )
        assert result.exit_code == 0, result.output


# ── export session ─────────────────────────────────────────────────────────


class TestExportSession:
    def test_export_session(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        # Create symbiote + session + message
        create_result = invoke(
            "create", "--name", "Alice", "--role", "coder", db_path=db
        )
        symbiote_id = _extract_id(create_result.output)

        session_result = invoke("session", "start", symbiote_id, db_path=db)
        session_id = _extract_id(session_result.output)

        invoke("message", session_id, "Hello world", db_path=db)

        result = invoke("export", "session", session_id, db_path=db)
        assert result.exit_code == 0, result.output
        # Markdown export should contain session info
        assert "session" in result.output.lower() or session_id in result.output


# ── error handling ─────────────────────────────────────────────────────────


class TestErrorHandling:
    def test_session_start_invalid_symbiote(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke("session", "start", "nonexistent-id", db_path=db)
        # Should not crash — exit 1 with friendly message
        assert result.exit_code == 1

    def test_session_close_invalid_session(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke("session", "close", "nonexistent-id", db_path=db)
        assert result.exit_code == 1

    def test_message_invalid_session(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        result = invoke("message", "nonexistent-id", "hello", db_path=db)
        assert result.exit_code == 1


# ── helper ─────────────────────────────────────────────────────────────────


def _extract_id(output: str) -> str:
    """Extract a UUID-like ID from CLI output.

    Looks for a token that contains at least 4 dashes (UUID format).
    """
    for line in output.strip().splitlines():
        for token in line.split():
            # UUID v4: 8-4-4-4-12 = 4 dashes
            if token.count("-") >= 4 and len(token) >= 32:
                return token
    raise ValueError(f"Could not extract ID from output:\n{output}")
