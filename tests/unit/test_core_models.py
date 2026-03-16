"""Tests for symbiote.core.models — Pydantic v2 domain models."""

from datetime import UTC, datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from symbiote.core.models import (
    Artifact,
    Decision,
    EnvironmentConfig,
    MemoryEntry,
    Message,
    ProcessInstance,
    Session,
    Symbiote,
    Workspace,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _is_valid_uuid(value: str) -> bool:
    try:
        UUID(value, version=4)
        return True
    except ValueError:
        return False


# ── Symbiote ─────────────────────────────────────────────────────────────────


class TestSymbiote:
    def test_defaults(self):
        s = Symbiote(name="Alpha", role="coder", persona_json={"tone": "formal"})
        assert _is_valid_uuid(s.id)
        assert s.name == "Alpha"
        assert s.role == "coder"
        assert s.owner_id is None
        assert s.persona_json == {"tone": "formal"}
        assert s.behavioral_constraints == []
        assert s.interaction_style is None
        assert s.status == "active"
        assert isinstance(s.created_at, datetime)
        assert isinstance(s.updated_at, datetime)

    def test_explicit_values(self):
        s = Symbiote(
            id="abc-123",
            name="Beta",
            role="reviewer",
            owner_id="user-1",
            persona_json={},
            behavioral_constraints=["no profanity"],
            interaction_style="casual",
            status="inactive",
        )
        assert s.id == "abc-123"
        assert s.owner_id == "user-1"
        assert s.behavioral_constraints == ["no profanity"]
        assert s.interaction_style == "casual"
        assert s.status == "inactive"


# ── Session ──────────────────────────────────────────────────────────────────


class TestSession:
    def test_defaults(self):
        s = Session(symbiote_id="sym-1")
        assert _is_valid_uuid(s.id)
        assert s.symbiote_id == "sym-1"
        assert s.goal is None
        assert s.workspace_id is None
        assert s.status == "active"
        assert isinstance(s.started_at, datetime)
        assert s.ended_at is None
        assert s.summary is None

    def test_explicit_values(self):
        now = datetime.now(tz=UTC)
        s = Session(
            symbiote_id="sym-1",
            goal="implement feature",
            workspace_id="ws-1",
            status="completed",
            ended_at=now,
            summary="Done.",
        )
        assert s.goal == "implement feature"
        assert s.status == "completed"
        assert s.ended_at == now
        assert s.summary == "Done."


# ── Message ──────────────────────────────────────────────────────────────────


class TestMessage:
    def test_defaults(self):
        m = Message(session_id="ses-1", role="user", content="Hello")
        assert _is_valid_uuid(m.id)
        assert m.role == "user"
        assert isinstance(m.created_at, datetime)

    def test_valid_roles(self):
        for role in ("user", "assistant", "system"):
            m = Message(session_id="ses-1", role=role, content="x")
            assert m.role == role

    def test_invalid_role_rejected(self):
        with pytest.raises(ValidationError):
            Message(session_id="ses-1", role="admin", content="x")


# ── MemoryEntry ──────────────────────────────────────────────────────────────


class TestMemoryEntry:
    def test_defaults(self):
        me = MemoryEntry(
            symbiote_id="sym-1",
            type="working",
            scope="global",
            content="remember this",
            source="user",
        )
        assert _is_valid_uuid(me.id)
        assert me.session_id is None
        assert me.tags == []
        assert me.importance == 0.5
        assert me.confidence == 1.0
        assert me.is_active is True
        assert isinstance(me.created_at, datetime)
        assert isinstance(me.last_used_at, datetime)

    def test_valid_types(self):
        valid_types = [
            "working",
            "session_summary",
            "relational",
            "preference",
            "constraint",
            "factual",
            "procedural",
            "decision",
            "reflection",
            "semantic_note",
        ]
        for t in valid_types:
            me = MemoryEntry(
                symbiote_id="sym-1",
                type=t,
                scope="global",
                content="x",
                source="user",
            )
            assert me.type == t

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            MemoryEntry(
                symbiote_id="sym-1",
                type="invalid_type",
                scope="global",
                content="x",
                source="user",
            )

    def test_valid_scopes(self):
        for scope in ("global", "user", "project", "workspace", "session"):
            me = MemoryEntry(
                symbiote_id="sym-1",
                type="working",
                scope=scope,
                content="x",
                source="user",
            )
            assert me.scope == scope

    def test_invalid_scope_rejected(self):
        with pytest.raises(ValidationError):
            MemoryEntry(
                symbiote_id="sym-1",
                type="working",
                scope="unknown",
                content="x",
                source="user",
            )

    def test_valid_sources(self):
        for src in ("user", "system", "reflection", "inference"):
            me = MemoryEntry(
                symbiote_id="sym-1",
                type="working",
                scope="global",
                content="x",
                source=src,
            )
            assert me.source == src

    def test_invalid_source_rejected(self):
        with pytest.raises(ValidationError):
            MemoryEntry(
                symbiote_id="sym-1",
                type="working",
                scope="global",
                content="x",
                source="magic",
            )

    def test_importance_bounds(self):
        # Valid boundaries
        MemoryEntry(
            symbiote_id="sym-1", type="working", scope="global",
            content="x", source="user", importance=0.0,
        )
        MemoryEntry(
            symbiote_id="sym-1", type="working", scope="global",
            content="x", source="user", importance=1.0,
        )

    def test_importance_out_of_range(self):
        with pytest.raises(ValidationError):
            MemoryEntry(
                symbiote_id="sym-1", type="working", scope="global",
                content="x", source="user", importance=1.1,
            )
        with pytest.raises(ValidationError):
            MemoryEntry(
                symbiote_id="sym-1", type="working", scope="global",
                content="x", source="user", importance=-0.1,
            )

    def test_confidence_out_of_range(self):
        with pytest.raises(ValidationError):
            MemoryEntry(
                symbiote_id="sym-1", type="working", scope="global",
                content="x", source="user", confidence=1.5,
            )
        with pytest.raises(ValidationError):
            MemoryEntry(
                symbiote_id="sym-1", type="working", scope="global",
                content="x", source="user", confidence=-0.1,
            )


# ── Workspace ────────────────────────────────────────────────────────────────


class TestWorkspace:
    def test_defaults(self):
        w = Workspace(symbiote_id="sym-1", name="main", root_path="/tmp/ws")
        assert _is_valid_uuid(w.id)
        assert w.type == "general"
        assert isinstance(w.created_at, datetime)

    def test_valid_types(self):
        for t in ("code", "docs", "data", "general"):
            w = Workspace(symbiote_id="sym-1", name="ws", root_path="/x", type=t)
            assert w.type == t

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            Workspace(symbiote_id="sym-1", name="ws", root_path="/x", type="unknown")


# ── Artifact ─────────────────────────────────────────────────────────────────


class TestArtifact:
    def test_defaults(self):
        a = Artifact(
            session_id="ses-1",
            workspace_id="ws-1",
            path="/tmp/report.txt",
            type="file",
        )
        assert _is_valid_uuid(a.id)
        assert a.description is None
        assert isinstance(a.created_at, datetime)

    def test_valid_types(self):
        for t in ("file", "directory", "report", "export"):
            a = Artifact(
                session_id="ses-1", workspace_id="ws-1", path="/x", type=t,
            )
            assert a.type == t

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            Artifact(
                session_id="ses-1", workspace_id="ws-1", path="/x", type="blob",
            )


# ── EnvironmentConfig ────────────────────────────────────────────────────────


class TestEnvironmentConfig:
    def test_defaults(self):
        ec = EnvironmentConfig(symbiote_id="sym-1")
        assert _is_valid_uuid(ec.id)
        assert ec.workspace_id is None
        assert ec.tools == []
        assert ec.services == []
        assert ec.humans == []
        assert ec.policies == {}
        assert ec.resources == {}

    def test_explicit_values(self):
        ec = EnvironmentConfig(
            symbiote_id="sym-1",
            workspace_id="ws-1",
            tools=["git", "pytest"],
            services=["postgres"],
            humans=["alice"],
            policies={"max_tokens": 4096},
            resources={"cpu": 2},
        )
        assert ec.tools == ["git", "pytest"]
        assert ec.policies == {"max_tokens": 4096}


# ── Decision ─────────────────────────────────────────────────────────────────


class TestDecision:
    def test_defaults(self):
        d = Decision(session_id="ses-1", title="Use Pydantic v2")
        assert _is_valid_uuid(d.id)
        assert d.description is None
        assert d.tags == []
        assert isinstance(d.created_at, datetime)

    def test_explicit_values(self):
        d = Decision(
            session_id="ses-1",
            title="Use Pydantic v2",
            description="Better perf",
            tags=["arch", "deps"],
        )
        assert d.description == "Better perf"
        assert d.tags == ["arch", "deps"]


# ── ProcessInstance ──────────────────────────────────────────────────────────


class TestProcessInstance:
    def test_defaults(self):
        p = ProcessInstance(
            session_id="ses-1", process_name="deploy", state="running",
        )
        assert _is_valid_uuid(p.id)
        assert p.current_step is None
        assert p.logs == []
        assert isinstance(p.created_at, datetime)
        assert isinstance(p.updated_at, datetime)

    def test_valid_states(self):
        for state in ("running", "paused", "completed", "failed"):
            p = ProcessInstance(
                session_id="ses-1", process_name="x", state=state,
            )
            assert p.state == state

    def test_invalid_state_rejected(self):
        with pytest.raises(ValidationError):
            ProcessInstance(
                session_id="ses-1", process_name="x", state="unknown",
            )
