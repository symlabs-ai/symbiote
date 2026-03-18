"""Tests for ToolGateway extensions — descriptors, HTTP tools, execute_tool_calls."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.environment.descriptors import (
    HttpToolConfig,
    ToolCall,
    ToolCallResult,
    ToolDescriptor,
)
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "gw_ext_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="ExtBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def gw(env_manager: EnvironmentManager, adapter: SQLiteAdapter) -> ToolGateway:
    gate = PolicyGate(env_manager=env_manager, storage=adapter)
    return ToolGateway(policy_gate=gate)


# ── Descriptors ───────────────────────────────────────────────────────────


class TestDescriptors:
    def test_builtins_have_descriptors(self, gw: ToolGateway) -> None:
        descs = gw.get_descriptors()
        ids = [d.tool_id for d in descs]
        assert "fs_read" in ids
        assert "fs_write" in ids
        assert "fs_list" in ids

    def test_builtin_descriptors_are_builtin_type(self, gw: ToolGateway) -> None:
        for d in gw.get_descriptors():
            assert d.handler_type == "builtin"

    def test_register_descriptor_custom(self, gw: ToolGateway) -> None:
        desc = ToolDescriptor(
            tool_id="echo", name="Echo", description="Echoes input"
        )
        gw.register_descriptor(desc, lambda p: p.get("msg", ""))
        assert gw.get_descriptor("echo") is not None
        assert gw.get_descriptor("echo").name == "Echo"
        assert gw.has_tool("echo")

    def test_get_descriptor_none_for_legacy_tool(self, gw: ToolGateway) -> None:
        gw.register_tool("legacy", lambda p: "ok")
        assert gw.get_descriptor("legacy") is None
        assert gw.has_tool("legacy")

    def test_get_descriptors_returns_all(self, gw: ToolGateway) -> None:
        desc = ToolDescriptor(tool_id="x", name="X", description="X tool")
        gw.register_descriptor(desc, lambda p: "x")
        descs = gw.get_descriptors()
        assert len(descs) >= 4  # 3 builtins + x


# ── HTTP tools ────────────────────────────────────────────────────────────


class TestHttpTools:
    def test_register_http_tool(self, gw: ToolGateway) -> None:
        desc = ToolDescriptor(
            tool_id="api_search",
            name="API Search",
            description="Search via API",
        )
        config = HttpToolConfig(
            method="GET",
            url_template="http://localhost:9999/search?q={q}",
        )
        gw.register_http_tool(desc, config)

        assert gw.has_tool("api_search")
        d = gw.get_descriptor("api_search")
        assert d is not None
        assert d.handler_type == "http"

        hc = gw.get_http_config("api_search")
        assert hc is not None
        assert hc.method == "GET"

    def test_http_handler_makes_request(self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str) -> None:
        desc = ToolDescriptor(
            tool_id="api_get",
            name="API Get",
            description="Get from API",
        )
        config = HttpToolConfig(
            method="GET",
            url_template="http://localhost:12345/items/{id}",
        )
        gw.register_http_tool(desc, config)
        env_manager.configure(symbiote_id=symbiote_id, tools=["api_get"])

        # Mock the HTTP call
        import io
        import json

        mock_response = io.BytesIO(json.dumps({"title": "News"}).encode())
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.read = mock_response.read
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = lambda s, *a: None

        with patch("symbiote.security.network.validate_url", return_value="http://localhost:12345/items/42"), \
             patch("urllib.request.OpenerDirector.open", return_value=mock_response):
            result = gw.execute(
                symbiote_id=symbiote_id,
                session_id=None,
                tool_id="api_get",
                params={"id": "42"},
            )
        assert result.success is True
        # Response is wrapped with untrusted content banner (B-15)
        assert result.output["data"] == {"title": "News"}
        assert "_warning" in result.output


# ── Unregister ────────────────────────────────────────────────────────────


class TestUnregister:
    def test_unregister_existing(self, gw: ToolGateway) -> None:
        desc = ToolDescriptor(tool_id="tmp", name="Tmp", description="Temp")
        gw.register_descriptor(desc, lambda p: "ok")
        assert gw.unregister_tool("tmp") is True
        assert gw.has_tool("tmp") is False
        assert gw.get_descriptor("tmp") is None

    def test_unregister_nonexistent(self, gw: ToolGateway) -> None:
        assert gw.unregister_tool("nonexistent") is False


# ── execute_tool_calls ────────────────────────────────────────────────────


class TestExecuteToolCalls:
    def test_executes_list_of_calls(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        gw.register_tool("add", lambda p: p.get("a", 0) + p.get("b", 0))
        gw.register_tool("mul", lambda p: p.get("a", 0) * p.get("b", 0))
        env_manager.configure(symbiote_id=symbiote_id, tools=["add", "mul"])

        calls = [
            ToolCall(tool_id="add", params={"a": 2, "b": 3}),
            ToolCall(tool_id="mul", params={"a": 4, "b": 5}),
        ]
        results = gw.execute_tool_calls(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            calls=calls,
        )
        assert len(results) == 2
        assert all(isinstance(r, ToolCallResult) for r in results)
        assert results[0].success is True
        assert results[0].output == 5
        assert results[1].success is True
        assert results[1].output == 20

    def test_error_hint_injected_on_failure(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        """B-8: Tool error hints — failed tool calls get a retry hint."""
        def _raise(p):
            raise ValueError("bad input")

        gw.register_tool("boom", _raise)
        env_manager.configure(symbiote_id=symbiote_id, tools=["boom"])

        calls = [ToolCall(tool_id="boom", params={})]
        results = gw.execute_tool_calls(
            symbiote_id=symbiote_id, session_id="sess-1", calls=calls
        )
        assert len(results) == 1
        assert results[0].success is False
        assert "bad input" in results[0].error
        assert "[Hint: Analyze the error" in results[0].error

    def test_success_has_no_hint(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        """B-8: Successful tool calls must NOT have any hint."""
        gw.register_tool("ok_tool", lambda p: "fine")
        env_manager.configure(symbiote_id=symbiote_id, tools=["ok_tool"])

        calls = [ToolCall(tool_id="ok_tool", params={})]
        results = gw.execute_tool_calls(
            symbiote_id=symbiote_id, session_id="sess-1", calls=calls
        )
        assert results[0].success is True
        assert results[0].error is None

    def test_unregistered_tool_gets_hint(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        """B-8: Unregistered tool errors also get hints."""
        env_manager.configure(symbiote_id=symbiote_id, tools=["nope"])
        calls = [ToolCall(tool_id="nope", params={})]
        results = gw.execute_tool_calls(
            symbiote_id=symbiote_id, session_id=None, calls=calls
        )
        assert results[0].success is False
        assert "[Hint:" in results[0].error

    def test_policy_blocked_gets_hint(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        """B-8: Policy-blocked tool calls also get hints."""
        gw.register_tool("secret", lambda p: "classified")
        env_manager.configure(symbiote_id=symbiote_id, tools=["other"])  # NOT "secret"

        calls = [ToolCall(tool_id="secret", params={})]
        results = gw.execute_tool_calls(
            symbiote_id=symbiote_id, session_id="sess-1", calls=calls
        )
        assert results[0].success is False
        assert "blocked" in results[0].error.lower()
        assert "[Hint:" in results[0].error

    def test_unregistered_tool_in_list(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["nope"])
        calls = [ToolCall(tool_id="nope", params={})]
        results = gw.execute_tool_calls(
            symbiote_id=symbiote_id,
            session_id=None,
            calls=calls,
        )
        assert len(results) == 1
        assert results[0].success is False


# ── header_factory (dynamic auth) ─────────────────────────────────────────


class TestHeaderFactory:
    """HTTP tools with dynamic per-request auth headers via header_factory."""

    def test_header_factory_called_per_request(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        call_log: list[str] = []
        tokens = iter(["token-A", "token-B"])

        def factory() -> dict[str, str]:
            tok = next(tokens)
            call_log.append(tok)
            return {"Authorization": f"Bearer {tok}"}

        desc = ToolDescriptor(tool_id="dyn_get", name="DynGet", description="Dynamic auth")
        config = HttpToolConfig(
            method="GET",
            url_template="http://localhost:12345/items/{id}",
            header_factory=factory,
        )
        gw.register_http_tool(desc, config)
        env_manager.configure(symbiote_id=symbiote_id, tools=["dyn_get"])

        import io
        import json as _json

        def make_mock():
            body = io.BytesIO(_json.dumps({"ok": True}).encode())
            body.__enter__ = lambda s: s
            body.__exit__ = lambda s, *a: None
            return body

        with patch("symbiote.security.network.validate_url", return_value=None), \
             patch("urllib.request.OpenerDirector.open", side_effect=lambda *a, **kw: make_mock()):
            gw.execute(symbiote_id=symbiote_id, session_id=None, tool_id="dyn_get", params={"id": "1"})
            gw.execute(symbiote_id=symbiote_id, session_id=None, tool_id="dyn_get", params={"id": "2"})

        assert call_log == ["token-A", "token-B"]

    def test_static_headers_still_work_without_factory(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        desc = ToolDescriptor(tool_id="static_get", name="StaticGet", description="Static auth")
        config = HttpToolConfig(
            method="GET",
            url_template="http://localhost:12345/items",
            headers={"X-Key": "abc"},
        )
        gw.register_http_tool(desc, config)
        env_manager.configure(symbiote_id=symbiote_id, tools=["static_get"])

        import io
        import json as _json

        body = io.BytesIO(_json.dumps({"items": []}).encode())
        body.__enter__ = lambda s: s
        body.__exit__ = lambda s, *a: None

        with patch("symbiote.security.network.validate_url", return_value=None), \
             patch("urllib.request.OpenerDirector.open", return_value=body):
            result = gw.execute(
                symbiote_id=symbiote_id, session_id=None, tool_id="static_get", params={}
            )
        assert result.success is True


# ── async tool handlers ────────────────────────────────────────────────────


class TestAsyncToolHandlers:
    """Tools with async coroutine handlers via execute_tool_calls_async."""

    @pytest.mark.asyncio
    async def test_async_handler_executes(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        async def async_echo(params: dict) -> str:
            return f"async:{params.get('msg', '')}"

        gw.register_tool("async_echo", async_echo)
        env_manager.configure(symbiote_id=symbiote_id, tools=["async_echo"])

        calls = [ToolCall(tool_id="async_echo", params={"msg": "hello"})]
        results = await gw.execute_tool_calls_async(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            calls=calls,
        )
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].output == "async:hello"

    @pytest.mark.asyncio
    async def test_sync_handler_works_via_async_path(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        gw.register_tool("sync_add", lambda p: p.get("a", 0) + p.get("b", 0))
        env_manager.configure(symbiote_id=symbiote_id, tools=["sync_add"])

        calls = [ToolCall(tool_id="sync_add", params={"a": 3, "b": 4})]
        results = await gw.execute_tool_calls_async(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            calls=calls,
        )
        assert results[0].success is True
        assert results[0].output == 7

    @pytest.mark.asyncio
    async def test_async_handler_error_is_captured(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        async def failing(params: dict) -> str:
            raise ValueError("async failure")

        gw.register_tool("async_fail", failing)
        env_manager.configure(symbiote_id=symbiote_id, tools=["async_fail"])

        calls = [ToolCall(tool_id="async_fail", params={})]
        results = await gw.execute_tool_calls_async(
            symbiote_id=symbiote_id,
            session_id=None,
            calls=calls,
        )
        assert results[0].success is False
        assert "async failure" in results[0].error
