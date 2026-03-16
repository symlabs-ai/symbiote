"""Tests for SymbioteKernel — the central orchestrator."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from symbiote.config.models import KernelConfig
from symbiote.core.capabilities import CapabilityError, CapabilitySurface
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.kernel import SymbioteKernel
from symbiote.core.models import Session, Symbiote
from symbiote.core.ports import LLMPort

# ── FakeLLM ──────────────────────────────────────────────────────────────────


class FakeLLM:
    """Minimal LLMPort implementation for testing."""

    def __init__(self, response: str = "fake-response") -> None:
        self._response = response
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict], config: dict | None = None) -> str:
        self.calls.append(messages)
        return self._response


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(db_path=tmp_path / "test.db")


@pytest.fixture()
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture()
def kernel(config: KernelConfig) -> SymbioteKernel:
    """Kernel without LLM."""
    k = SymbioteKernel(config)
    yield k
    k.shutdown()


@pytest.fixture()
def kernel_with_llm(config: KernelConfig, fake_llm: FakeLLM) -> SymbioteKernel:
    """Kernel with FakeLLM."""
    k = SymbioteKernel(config, llm=fake_llm)
    yield k
    k.shutdown()


# ── Tests: Initialization ────────────────────────────────────────────────────


class TestKernelInit:
    def test_create_kernel_with_config(self, kernel: SymbioteKernel) -> None:
        """Kernel initializes all components without errors."""
        assert kernel is not None

    def test_capabilities_property(self, kernel: SymbioteKernel) -> None:
        """Kernel exposes CapabilitySurface via property."""
        assert isinstance(kernel.capabilities, CapabilitySurface)

    def test_db_file_created(self, config: KernelConfig) -> None:
        """Kernel creates the SQLite database file on init."""
        k = SymbioteKernel(config)
        assert config.db_path.exists()
        k.shutdown()


# ── Tests: create_symbiote ───────────────────────────────────────────────────


class TestCreateSymbiote:
    def test_create_symbiote_returns_symbiote(self, kernel: SymbioteKernel) -> None:
        sym = kernel.create_symbiote("test-bot", "assistant")
        assert isinstance(sym, Symbiote)
        assert sym.name == "test-bot"
        assert sym.role == "assistant"

    def test_create_symbiote_with_persona(self, kernel: SymbioteKernel) -> None:
        persona = {"tone": "friendly"}
        sym = kernel.create_symbiote("bot", "helper", persona=persona)
        assert sym.persona_json == persona


# ── Tests: get_symbiote ──────────────────────────────────────────────────────


class TestGetSymbiote:
    def test_get_existing(self, kernel: SymbioteKernel) -> None:
        sym = kernel.create_symbiote("x", "role")
        fetched = kernel.get_symbiote(sym.id)
        assert fetched is not None
        assert fetched.id == sym.id

    def test_get_nonexistent(self, kernel: SymbioteKernel) -> None:
        assert kernel.get_symbiote("no-such-id") is None


# ── Tests: start_session / get_session ───────────────────────────────────────


class TestSessions:
    def test_start_session_returns_session(self, kernel: SymbioteKernel) -> None:
        sym = kernel.create_symbiote("s", "r")
        session = kernel.start_session(sym.id, goal="test goal")
        assert isinstance(session, Session)
        assert session.symbiote_id == sym.id
        assert session.goal == "test goal"
        assert session.status == "active"

    def test_get_session(self, kernel: SymbioteKernel) -> None:
        sym = kernel.create_symbiote("s", "r")
        session = kernel.start_session(sym.id)
        fetched = kernel.get_session(session.id)
        assert fetched is not None
        assert fetched.id == session.id

    def test_get_session_nonexistent(self, kernel: SymbioteKernel) -> None:
        assert kernel.get_session("no-such-id") is None


# ── Tests: close_session ─────────────────────────────────────────────────────


class TestCloseSession:
    def test_close_session_returns_closed(self, kernel: SymbioteKernel) -> None:
        sym = kernel.create_symbiote("s", "r")
        session = kernel.start_session(sym.id)
        closed = kernel.close_session(session.id)
        assert closed.status == "closed"
        assert closed.summary is not None


# ── Tests: message ───────────────────────────────────────────────────────────


class TestMessage:
    def test_message_with_llm(
        self, kernel_with_llm: SymbioteKernel, fake_llm: FakeLLM
    ) -> None:
        sym = kernel_with_llm.create_symbiote("bot", "assistant")
        session = kernel_with_llm.start_session(sym.id)
        response = kernel_with_llm.message(session.id, "hello")
        assert response == "fake-response"
        assert len(fake_llm.calls) == 1

    def test_message_stores_both_messages(
        self, kernel_with_llm: SymbioteKernel
    ) -> None:
        sym = kernel_with_llm.create_symbiote("bot", "assistant")
        session = kernel_with_llm.start_session(sym.id)
        kernel_with_llm.message(session.id, "hello")
        # Verify messages were stored in DB
        messages = kernel_with_llm._sessions.get_messages(session.id)
        roles = {m.role for m in messages}
        assert "user" in roles
        assert "assistant" in roles

    def test_message_without_llm_raises(self, kernel: SymbioteKernel) -> None:
        sym = kernel.create_symbiote("bot", "assistant")
        session = kernel.start_session(sym.id)
        with pytest.raises(CapabilityError):
            kernel.message(session.id, "hello")


# ── Tests: shutdown ──────────────────────────────────────────────────────────


class TestMessageErrors:
    def test_message_invalid_session_raises(self, kernel_with_llm: SymbioteKernel) -> None:
        with pytest.raises(EntityNotFoundError, match="not found"):
            kernel_with_llm.message("nonexistent-session", "hello")

    def test_close_session_invalid_raises(self, kernel_with_llm: SymbioteKernel) -> None:
        with pytest.raises(EntityNotFoundError, match="not found"):
            kernel_with_llm.close_session("nonexistent-session")


class TestShutdown:
    def test_shutdown_closes_adapter(self, config: KernelConfig) -> None:
        k = SymbioteKernel(config)
        k.shutdown()
        # After shutdown, storage should be closed — attempting to use it should fail
        with pytest.raises(sqlite3.ProgrammingError):
            k._storage.execute("SELECT 1")
