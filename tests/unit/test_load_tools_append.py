"""Regression: loading discovered or MCP tools adds to a symbiote's
authorized tools instead of replacing them (report item T-5, D-9)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from symbiote.discovery.models import DiscoveredTool
from symbiote.discovery.repository import DiscoveredToolRepository
from tests.support import ScriptedLLM, make_kernel


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.definition = SimpleNamespace(
            name=name, description="d", parameters={"type": "object", "properties": {}}
        )


class _FakeRegistry:
    def __init__(self, *names: str) -> None:
        self._tools = {n: _FakeTool(n) for n in names}

    def list_tools(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> _FakeTool:
        return self._tools[name]


def test_load_discovered_tools_keeps_previous_authorizations(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="a", role="r").id
    kernel.environment.configure(symbiote_id=sid, tools=["fs_read", "skill_view"])
    repo = DiscoveredToolRepository(kernel._storage)
    repo.save(DiscoveredTool(id=str(uuid4()), symbiote_id=sid, tool_id="list_items",
                             name="l", method="GET", url_template="{base_url}/items"))
    repo.set_status(sid, "list_items", "approved")

    kernel.load_discovered_tools(sid, base_url="http://127.0.0.1:9")

    assert kernel.environment.list_tools(sid) == ["fs_read", "skill_view", "list_items"]
    kernel.shutdown()


def test_load_mcp_tools_keeps_previous_authorizations(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="a", role="r").id
    kernel.environment.configure(symbiote_id=sid, tools=["fs_read"])

    loaded = kernel.load_mcp_tools(_FakeRegistry("mcp_search"), sid)

    assert loaded == ["mcp_search"]
    assert kernel.environment.list_tools(sid) == ["fs_read", "mcp_search"]
    kernel.shutdown()
