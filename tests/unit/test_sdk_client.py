"""Tests for SymbioteClient SDK — B-22."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKeyManager
from symbiote.api.http import app as fastapi_app
from symbiote.api.http import get_adapter, get_kernel
from symbiote.api.middleware import set_key_manager
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.sdk.client import SymbioteClient


@pytest.fixture()
def setup(tmp_path: Path):
    """Set up test environment with SDK client."""
    db = tmp_path / "sdk_test.db"
    adapter = SQLiteAdapter(db_path=db, check_same_thread=False)
    adapter.init_schema()

    key_mgr = APIKeyManager(adapter)
    key_mgr.init_schema()
    set_key_manager(key_mgr)
    _, user_key = key_mgr.create_key("sdk-tenant", "SDK Test", role="user")

    config = KernelConfig(db_path=db)
    llm = MockLLMAdapter(default_response="SDK mock response.")
    kernel = SymbioteKernel(config=config, llm=llm)
    import sqlite3
    kernel._storage._conn.close()
    kernel._storage._conn = sqlite3.connect(str(db), check_same_thread=False)
    kernel._storage._conn.row_factory = sqlite3.Row
    kernel._storage._conn.execute("PRAGMA journal_mode=WAL")
    kernel._storage._conn.execute("PRAGMA foreign_keys=ON")

    fastapi_app.dependency_overrides[get_adapter] = lambda: adapter
    fastapi_app.dependency_overrides[get_kernel] = lambda: kernel

    import symbiote.api.http as http_module
    http_module._key_manager = key_mgr

    test_client = TestClient(fastapi_app)

    # Create SDK client that uses TestClient transport
    sdk = SymbioteClient.__new__(SymbioteClient)
    sdk._base_url = "http://testserver"
    sdk._client = test_client
    sdk._client.headers["Authorization"] = f"Bearer {user_key}"

    yield {"sdk": sdk, "kernel": kernel}

    fastapi_app.dependency_overrides.clear()
    set_key_manager(None)
    kernel.shutdown()


class TestSDKFullFlow:
    def test_health(self, setup) -> None:
        result = setup["sdk"].health()
        assert result["status"] == "ok"
        assert result["service"] == "symbiote"
        assert "version" in result
        assert "commit" in result

    def test_create_and_get_symbiote(self, setup) -> None:
        sym = setup["sdk"].create_symbiote(name="SDKBot", role="assistant")
        assert sym["name"] == "SDKBot"
        assert "id" in sym

        fetched = setup["sdk"].get_symbiote(sym["id"])
        assert fetched["name"] == "SDKBot"

    def test_session_lifecycle(self, setup) -> None:
        sym = setup["sdk"].create_symbiote(name="SessionBot", role="assistant")
        session = setup["sdk"].create_session(symbiote_id=sym["id"], goal="testing")
        assert session["status"] == "active"
        assert session["goal"] == "testing"

        fetched = setup["sdk"].get_session(session["id"])
        assert fetched["id"] == session["id"]

    def test_chat(self, setup) -> None:
        sym = setup["sdk"].create_symbiote(name="ChatBot", role="assistant")
        session = setup["sdk"].create_session(symbiote_id=sym["id"])

        response = setup["sdk"].chat(
            session_id=session["id"],
            content="Hello from SDK!",
        )
        assert "response" in response
        assert "SDK mock" in str(response["response"])

    def test_chat_with_extra_context(self, setup) -> None:
        sym = setup["sdk"].create_symbiote(name="CtxBot", role="assistant")
        session = setup["sdk"].create_session(symbiote_id=sym["id"])

        response = setup["sdk"].chat(
            session_id=session["id"],
            content="What page am I on?",
            extra_context={"page_url": "/about", "page_title": "About Us"},
        )
        assert "response" in response

    def test_full_conversation_flow(self, setup) -> None:
        """Complete flow: create symbiote → session → chat → close."""
        sdk = setup["sdk"]

        # Create
        sym = sdk.create_symbiote(
            name="FullFlowBot",
            role="assistant",
            persona={"tone": "friendly"},
        )

        # Session
        session = sdk.create_session(
            symbiote_id=sym["id"],
            goal="Full flow test",
        )

        # Chat
        r1 = sdk.chat(session_id=session["id"], content="Hi!")
        assert r1["response"] is not None

        r2 = sdk.chat(session_id=session["id"], content="What can you do?")
        assert r2["response"] is not None

        # Close
        closed = sdk.close_session(session["id"])
        assert closed["status"] == "closed"
