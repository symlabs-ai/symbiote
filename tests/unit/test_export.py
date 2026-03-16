"""Tests for ExportService — T-20."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.adapters.export.markdown import ExportService
from symbiote.adapters.storage.sqlite import SQLiteAdapter


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "export_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def service(adapter: SQLiteAdapter) -> ExportService:
    return ExportService(storage=adapter)


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    sid = "sym-export-001"
    adapter.execute(
        "INSERT INTO symbiotes (id, name, role, created_at, updated_at) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        (sid, "ExportBot", "assistant"),
    )
    return sid


@pytest.fixture()
def session_id(adapter: SQLiteAdapter, symbiote_id: str) -> str:
    sess_id = "sess-export-001"
    adapter.execute(
        "INSERT INTO sessions (id, symbiote_id, goal, status, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (sess_id, symbiote_id, "Test export", "active", "2026-03-15T10:00:00"),
    )
    return sess_id


# ── export_session ────────────────────────────────────────────────────────


class TestExportSession:
    def test_export_session_with_data(
        self, adapter: SQLiteAdapter, service: ExportService, session_id: str
    ) -> None:
        # Add messages
        adapter.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("msg-1", session_id, "user", "Hello there", "2026-03-15T10:01:00"),
        )
        adapter.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("msg-2", session_id, "assistant", "Hi! How can I help?", "2026-03-15T10:02:00"),
        )
        # Add decision
        adapter.execute(
            "INSERT INTO decisions (id, session_id, title, description, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dec-1", session_id, "Use Markdown", "Better readability", '["format"]', "2026-03-15T10:03:00"),
        )

        result = service.export_session(session_id)

        assert "## Session" in result
        assert session_id in result
        assert "Test export" in result
        assert "## Messages" in result
        assert "Hello there" in result
        assert "Hi! How can I help?" in result
        assert "## Decisions" in result
        assert "Use Markdown" in result
        assert "Better readability" in result

    def test_export_session_empty(
        self, service: ExportService, session_id: str
    ) -> None:
        result = service.export_session(session_id)

        assert "## Session" in result
        assert "No messages found." in result
        assert "No decisions found." in result

    def test_export_session_contains_timestamps(
        self, service: ExportService, session_id: str
    ) -> None:
        result = service.export_session(session_id)

        assert "2026-03-15" in result

    def test_export_session_includes_status(
        self, service: ExportService, session_id: str
    ) -> None:
        result = service.export_session(session_id)

        assert "active" in result


# ── export_memory ─────────────────────────────────────────────────────────


class TestExportMemory:
    def test_export_memory_grouped_by_type(
        self, adapter: SQLiteAdapter, service: ExportService, symbiote_id: str
    ) -> None:
        for i, (mtype, content, importance) in enumerate([
            ("fact", "Python is great", 0.9),
            ("fact", "SQLite is embedded", 0.8),
            ("preference", "User likes dark mode", 0.7),
        ]):
            adapter.execute(
                "INSERT INTO memory_entries "
                "(id, symbiote_id, type, scope, content, tags_json, importance, "
                "source, confidence, created_at, last_used_at, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"mem-{i}",
                    symbiote_id,
                    mtype,
                    "global",
                    content,
                    json.dumps(["test"]),
                    importance,
                    "test",
                    1.0,
                    "2026-03-15T10:00:00",
                    "2026-03-15T10:00:00",
                    1,
                ),
            )

        result = service.export_memory(symbiote_id)

        assert "## fact" in result
        assert "## preference" in result
        assert "Python is great" in result
        assert "SQLite is embedded" in result
        assert "User likes dark mode" in result

    def test_export_memory_includes_importance_and_tags(
        self, adapter: SQLiteAdapter, service: ExportService, symbiote_id: str
    ) -> None:
        adapter.execute(
            "INSERT INTO memory_entries "
            "(id, symbiote_id, type, scope, content, tags_json, importance, "
            "source, confidence, created_at, last_used_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "mem-tag",
                symbiote_id,
                "insight",
                "global",
                "Testing is important",
                json.dumps(["quality", "process"]),
                0.95,
                "test",
                1.0,
                "2026-03-15T10:00:00",
                "2026-03-15T10:00:00",
                1,
            ),
        )

        result = service.export_memory(symbiote_id)

        assert "0.95" in result
        assert "quality" in result
        assert "process" in result

    def test_export_memory_empty(
        self, service: ExportService, symbiote_id: str
    ) -> None:
        result = service.export_memory(symbiote_id)

        assert "No memories found." in result

    def test_export_memory_excludes_inactive(
        self, adapter: SQLiteAdapter, service: ExportService, symbiote_id: str
    ) -> None:
        adapter.execute(
            "INSERT INTO memory_entries "
            "(id, symbiote_id, type, scope, content, tags_json, importance, "
            "source, confidence, created_at, last_used_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "mem-inactive",
                symbiote_id,
                "fact",
                "global",
                "This should not appear",
                "[]",
                0.5,
                "test",
                1.0,
                "2026-03-15T10:00:00",
                "2026-03-15T10:00:00",
                0,
            ),
        )

        result = service.export_memory(symbiote_id)

        assert "This should not appear" not in result
        assert "No memories found." in result


# ── export_decisions ──────────────────────────────────────────────────────


class TestExportDecisions:
    def test_export_decisions_with_entries(
        self, adapter: SQLiteAdapter, service: ExportService, session_id: str
    ) -> None:
        adapter.execute(
            "INSERT INTO decisions (id, session_id, title, description, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dec-a", session_id, "Use TDD", "Red-green-refactor", '["process"]', "2026-03-15T10:01:00"),
        )
        adapter.execute(
            "INSERT INTO decisions (id, session_id, title, description, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dec-b", session_id, "SQLite adapter", "Lightweight DB", '["arch", "storage"]', "2026-03-15T10:02:00"),
        )

        result = service.export_decisions(session_id)

        assert "## Decisions" in result
        assert "Use TDD" in result
        assert "Red-green-refactor" in result
        assert "SQLite adapter" in result
        assert "Lightweight DB" in result
        assert "process" in result
        assert "arch" in result
        assert "2026-03-15" in result

    def test_export_decisions_empty(
        self, service: ExportService, session_id: str
    ) -> None:
        result = service.export_decisions(session_id)

        assert "No decisions found." in result

    def test_export_decisions_has_markdown_headers(
        self, adapter: SQLiteAdapter, service: ExportService, session_id: str
    ) -> None:
        adapter.execute(
            "INSERT INTO decisions (id, session_id, title, description, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dec-hdr", session_id, "Header test", "Desc", '[]', "2026-03-15T10:00:00"),
        )

        result = service.export_decisions(session_id)

        assert result.startswith("#")
        assert "##" in result
