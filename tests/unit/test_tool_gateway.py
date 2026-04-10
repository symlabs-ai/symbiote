"""Tests for ToolGateway — T-16."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate, ToolResult
from symbiote.environment.tools import ToolGateway, _build_body, _build_url


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "tool_gw_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="ToolBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def gate(env_manager: EnvironmentManager, adapter: SQLiteAdapter) -> PolicyGate:
    return PolicyGate(env_manager=env_manager, storage=adapter)


@pytest.fixture()
def gw(gate: PolicyGate) -> ToolGateway:
    return ToolGateway(policy_gate=gate)


# ── register / list / has ─────────────────────────────────────────────────


class TestRegistry:
    def test_register_tool_appears_in_list(self, gw: ToolGateway) -> None:
        gw.register_tool("echo", lambda p: p)
        assert "echo" in gw.list_tools()

    def test_has_tool_true_after_register(self, gw: ToolGateway) -> None:
        gw.register_tool("echo", lambda p: p)
        assert gw.has_tool("echo") is True

    def test_has_tool_false_for_unknown(self, gw: ToolGateway) -> None:
        assert gw.has_tool("nonexistent") is False

    def test_list_tools_includes_builtins(self, gw: ToolGateway) -> None:
        tools = gw.list_tools()
        assert "fs_read" in tools
        assert "fs_write" in tools
        assert "fs_list" in tools


# ── execute ───────────────────────────────────────────────────────────────


class TestExecute:
    def test_registered_and_authorized_returns_success(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        gw.register_tool("echo", lambda p: p.get("msg", ""))
        env_manager.configure(symbiote_id=symbiote_id, tools=["echo"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="echo",
            params={"msg": "hello"},
        )
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.output == "hello"

    def test_registered_but_unauthorized_returns_blocked(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        gw.register_tool("echo", lambda p: p.get("msg", ""))
        env_manager.configure(symbiote_id=symbiote_id, tools=["git"])  # echo NOT listed

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="echo",
            params={"msg": "hello"},
        )
        assert result.success is False
        assert result.error is not None
        assert "blocked" in result.error.lower() or "not allowed" in result.error.lower()

    def test_unregistered_tool_returns_error(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["nope"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="nope",
            params={},
        )
        assert result.success is False
        assert result.error == "Tool not registered"

    def test_execute_with_workspace(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        gw.register_tool("ping", lambda p: "pong")
        env_manager.configure(
            symbiote_id=symbiote_id, workspace_id="ws-1", tools=["ping"]
        )

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="ping",
            params={},
            workspace_id="ws-1",
        )
        assert result.success is True
        assert result.output == "pong"


# ── built-in: fs_read ────────────────────────────────────────────────────


class TestFsRead:
    def test_reads_file_content(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "hello.txt"
        target.write_text("world", encoding="utf-8")

        env_manager.configure(symbiote_id=symbiote_id, tools=["fs_read"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="fs_read",
            params={"path": str(target)},
        )
        assert result.success is True
        assert result.output == "world"

    def test_missing_file_returns_error(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
        tmp_path: Path,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["fs_read"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="fs_read",
            params={"path": str(tmp_path / "no_such_file.txt")},
        )
        assert result.success is False
        assert result.error is not None


# ── built-in: fs_write ───────────────────────────────────────────────────


class TestFsWrite:
    def test_writes_file_to_disk(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "out.txt"
        env_manager.configure(symbiote_id=symbiote_id, tools=["fs_write"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="fs_write",
            params={"path": str(target), "content": "data"},
        )
        assert result.success is True
        assert result.output == "ok"
        assert target.read_text(encoding="utf-8") == "data"


# ── built-in: fs_list ────────────────────────────────────────────────────


class TestFsList:
    def test_lists_directory_contents(
        self,
        gw: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
        tmp_path: Path,
    ) -> None:
        sub = tmp_path / "listing"
        sub.mkdir()
        (sub / "a.txt").write_text("a")
        (sub / "b.txt").write_text("b")
        env_manager.configure(symbiote_id=symbiote_id, tools=["fs_list"])

        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id="sess-1",
            tool_id="fs_list",
            params={"path": str(sub)},
        )
        assert result.success is True
        assert sorted(result.output) == ["a.txt", "b.txt"]


# ── get_available_tags ────────────────────────────────────────────────────


class TestGetAvailableTags:
    def test_returns_distinct_sorted_tags(self, gw: ToolGateway) -> None:
        d1 = ToolDescriptor(tool_id="t1", name="T1", description="T1", tags=["Compose", "Items"])
        d2 = ToolDescriptor(tool_id="t2", name="T2", description="T2", tags=["Admin", "Items"])
        gw.register_descriptor(d1, lambda p: None)
        gw.register_descriptor(d2, lambda p: None)
        tags = gw.get_available_tags()
        # Must contain the registered tags (bash builtin also adds "shell", "system")
        for expected in ["Admin", "Compose", "Items", "shell", "system"]:
            assert expected in tags
        assert tags == sorted(tags)

    def test_no_extra_tags_beyond_builtins(self, gw: ToolGateway) -> None:
        d = ToolDescriptor(tool_id="t1", name="T1", description="T1")
        gw.register_descriptor(d, lambda p: None)
        # Only bash builtin tags present
        assert gw.get_available_tags() == ["shell", "system"]


# ── register_index_tool / get_tool_schema ────────────────────────────────


class TestIndexTool:
    def test_register_index_tool_creates_meta_tool(self, gw: ToolGateway) -> None:
        gw.register_index_tool()
        assert gw.has_tool("get_tool_schema")
        desc = gw.get_descriptor("get_tool_schema")
        assert desc is not None
        assert desc.handler_type == "builtin"

    def test_register_index_tool_idempotent(self, gw: ToolGateway) -> None:
        gw.register_index_tool()
        gw.register_index_tool()  # should not raise
        assert gw.has_tool("get_tool_schema")

    def test_get_tool_schema_returns_descriptor(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        # Register a tool with params
        d = ToolDescriptor(
            tool_id="yn_publish",
            name="Publish",
            description="Publish item",
            parameters={"type": "object", "properties": {"id": {"type": "string"}}},
            handler_type="http",
        )
        gw.register_descriptor(d, lambda p: None)
        gw.register_index_tool()

        env_manager.configure(symbiote_id=symbiote_id, tools=["get_tool_schema"])
        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id=None,
            tool_id="get_tool_schema",
            params={"tool_id": "yn_publish"},
        )
        assert result.success is True
        assert result.output["tool_id"] == "yn_publish"
        assert "properties" in result.output["parameters"]

    def test_get_tool_schema_unknown_tool(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str
    ) -> None:
        gw.register_index_tool()
        env_manager.configure(symbiote_id=symbiote_id, tools=["get_tool_schema"])
        result = gw.execute(
            symbiote_id=symbiote_id,
            session_id=None,
            tool_id="get_tool_schema",
            params={"tool_id": "nonexistent"},
        )
        assert result.success is True
        assert "error" in result.output


# ── get_descriptors(tags=...) ─────────────────────────────────────────────


class TestGetDescriptorsByTags:
    def test_no_tags_returns_all(self, gw: ToolGateway) -> None:
        d1 = ToolDescriptor(tool_id="t1", name="T1", description="T1", tags=["Items"])
        d2 = ToolDescriptor(tool_id="t2", name="T2", description="T2", tags=["Admin"])
        gw.register_descriptor(d1, lambda p: None)
        gw.register_descriptor(d2, lambda p: None)

        all_descs = gw.get_descriptors()
        ids = {d.tool_id for d in all_descs}
        assert "t1" in ids
        assert "t2" in ids

    def test_filter_by_single_tag(self, gw: ToolGateway) -> None:
        d1 = ToolDescriptor(tool_id="t1", name="T1", description="T1", tags=["Items"])
        d2 = ToolDescriptor(tool_id="t2", name="T2", description="T2", tags=["Admin"])
        d3 = ToolDescriptor(tool_id="t3", name="T3", description="T3", tags=["Items", "Compose"])
        gw.register_descriptor(d1, lambda p: None)
        gw.register_descriptor(d2, lambda p: None)
        gw.register_descriptor(d3, lambda p: None)

        filtered = gw.get_descriptors(tags=["Items"])
        ids = {d.tool_id for d in filtered}
        assert ids == {"t1", "t3"}

    def test_filter_by_multiple_tags(self, gw: ToolGateway) -> None:
        d1 = ToolDescriptor(tool_id="t1", name="T1", description="T1", tags=["Items"])
        d2 = ToolDescriptor(tool_id="t2", name="T2", description="T2", tags=["Admin"])
        d3 = ToolDescriptor(tool_id="t3", name="T3", description="T3", tags=["Compose"])
        gw.register_descriptor(d1, lambda p: None)
        gw.register_descriptor(d2, lambda p: None)
        gw.register_descriptor(d3, lambda p: None)

        filtered = gw.get_descriptors(tags=["Items", "Admin"])
        ids = {d.tool_id for d in filtered}
        assert ids == {"t1", "t2"}

    def test_filter_no_match_returns_empty(self, gw: ToolGateway) -> None:
        d1 = ToolDescriptor(tool_id="t1", name="T1", description="T1", tags=["Items"])
        gw.register_descriptor(d1, lambda p: None)

        filtered = gw.get_descriptors(tags=["NonExistent"])
        assert filtered == []

    def test_untagged_tools_excluded_when_filtering(self, gw: ToolGateway) -> None:
        d1 = ToolDescriptor(tool_id="t1", name="T1", description="T1", tags=[])
        d2 = ToolDescriptor(tool_id="t2", name="T2", description="T2", tags=["Items"])
        gw.register_descriptor(d1, lambda p: None)
        gw.register_descriptor(d2, lambda p: None)

        filtered = gw.get_descriptors(tags=["Items"])
        ids = {d.tool_id for d in filtered}
        assert ids == {"t2"}


# ── _build_url: optional_params ──────────────────────────────────────────


class TestBuildUrl:
    def test_all_params_present(self) -> None:
        url = _build_url(
            "http://api/items/?status={status}&limit={limit}",
            {"status": "draft", "limit": "10"},
            optional_params=["status", "limit"],
        )
        assert "status=draft" in url
        assert "limit=10" in url

    def test_optional_param_absent_stripped_ampersand(self) -> None:
        url = _build_url(
            "http://api/items/?status={status}&limit={limit}",
            {"status": "draft"},
            optional_params=["status", "limit"],
        )
        assert "limit" not in url
        assert "status=draft" in url
        assert url.endswith("?status=draft")

    def test_optional_param_absent_first_becomes_clean_querystring(self) -> None:
        url = _build_url(
            "http://api/items/?status={status}&limit={limit}",
            {"limit": "5"},
            optional_params=["status", "limit"],
        )
        assert "status" not in url
        assert "limit=5" in url
        # Must not start with &
        assert "?&" not in url

    def test_all_optional_params_absent_no_trailing_question(self) -> None:
        url = _build_url(
            "http://api/items/?status={status}&limit={limit}",
            {},
            optional_params=["status", "limit"],
        )
        assert "?" not in url
        assert url == "http://api/items/"

    def test_no_optional_params_normal_format(self) -> None:
        url = _build_url(
            "http://api/items/{id}/publish",
            {"id": "42"},
            optional_params=[],
        )
        assert url == "http://api/items/42/publish"

    def test_journal_id_optional_absent(self) -> None:
        """Mirrors yn_list_items pattern from YouNews."""
        url = _build_url(
            "http://api/items/?journal_id={journal_id}&status={status}",
            {"status": "published"},
            optional_params=["journal_id", "status"],
        )
        assert "journal_id" not in url
        assert "status=published" in url


# ── _build_body: array_params ────────────────────────────────────────────


class TestBuildBody:
    def test_string_param_rendered_normally(self) -> None:
        body = _build_body(
            {"action": "{action}"},
            {"action": "publish"},
            array_params=[],
        )
        assert body["action"] == "publish"

    def test_array_param_preserved_as_list(self) -> None:
        """item_ids must arrive as a JSON array, not as a string."""
        body = _build_body(
            {"item_ids": "{item_ids}", "action": "{action}"},
            {"item_ids": ["id1", "id2", "id3"], "action": "delete"},
            array_params=["item_ids"],
        )
        assert body["item_ids"] == ["id1", "id2", "id3"]
        assert isinstance(body["item_ids"], list)
        assert body["action"] == "delete"

    def test_array_param_empty_list(self) -> None:
        body = _build_body(
            {"item_ids": "{item_ids}"},
            {"item_ids": []},
            array_params=["item_ids"],
        )
        assert body["item_ids"] == []

    def test_array_param_missing_falls_back_to_empty_list(self) -> None:
        body = _build_body(
            {"item_ids": "{item_ids}"},
            {},
            array_params=["item_ids"],
        )
        assert body["item_ids"] == []

    def test_non_string_template_value_passed_through(self) -> None:
        body = _build_body(
            {"flag": True, "count": 42},
            {},
            array_params=[],
        )
        assert body["flag"] is True
        assert body["count"] == 42
