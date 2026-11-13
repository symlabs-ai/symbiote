"""Regression: the meta-tools of the ``index`` and ``on_demand`` modes work
without manual authorization, and get_tool_schema only describes tools the
calling symbiote may use (report item T-3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.environment.descriptors import ToolDescriptor
from tests.support import ScriptedLLM, make_kernel


@pytest.fixture()
def kernel(tmp_path: Path):
    k = make_kernel(tmp_path, ScriptedLLM("ok"))
    gw = k.tool_gateway
    gw.register_descriptor(
        ToolDescriptor(tool_id="get_weather", name="W", description="weather", tags=["weather"],
                       parameters={"type": "object", "properties": {"city": {"type": "string"}}}),
        lambda p: "sunny",
    )
    gw.register_descriptor(
        ToolDescriptor(tool_id="admin_wipe", name="Wipe", description="wipe", tags=["admin"],
                       risk_level="high",
                       parameters={"type": "object", "properties": {"token": {"type": "string"}}}),
        lambda p: "wiped",
    )
    yield k
    k.shutdown()


def _schema(kernel, sid: str, tool_id: str):
    res = kernel.tool_gateway.execute(sid, None, "get_tool_schema", {"tool_id": tool_id})
    assert res.success, res.error
    return res.output


def test_index_mode_get_tool_schema_needs_no_manual_authorization(kernel) -> None:
    sid = kernel.create_symbiote(name="a", role="r").id
    kernel.environment.configure(symbiote_id=sid, tools=["get_weather"])
    kernel.configure_tool_visibility(sid, tags=["weather"], loading="index")
    kernel.tool_gateway.register_index_tool()

    out = _schema(kernel, sid, "get_weather")

    assert out["tool_id"] == "get_weather"
    assert "parameters" in out


def test_get_tool_schema_hides_unauthorized_and_untagged_tools(kernel) -> None:
    sid = kernel.create_symbiote(name="a", role="r").id
    kernel.environment.configure(symbiote_id=sid, tools=["get_weather", "admin_wipe"])
    kernel.configure_tool_visibility(sid, tags=["weather"], loading="index")
    other = kernel.create_symbiote(name="b", role="r").id
    kernel.environment.configure(symbiote_id=other, tools=[], tool_loading="index")
    kernel.tool_gateway.register_index_tool()

    outside_tags = _schema(kernel, sid, "admin_wipe")
    not_authorized = _schema(kernel, other, "get_weather")

    assert "parameters" not in outside_tags and "error" in outside_tags
    assert "parameters" not in not_authorized and "error" in not_authorized


def test_get_tool_schema_is_not_free_outside_index_mode(kernel) -> None:
    sid = kernel.create_symbiote(name="a", role="r").id
    kernel.environment.configure(symbiote_id=sid, tools=["get_weather"])  # loading=full
    kernel.tool_gateway.register_index_tool()

    res = kernel.tool_gateway.execute(sid, None, "get_tool_schema", {"tool_id": "get_weather"})

    assert res.success is False


def test_on_demand_search_tools_work_and_are_offered(kernel) -> None:
    sid = kernel.create_symbiote(name="a", role="r").id
    kernel.environment.configure(symbiote_id=sid, tools=["get_weather"], context_mode="on_demand",
                                 tool_tags=["weather"])
    session = kernel.start_session(sid)

    mem = kernel.tool_gateway.execute(sid, session.id, "search_memories", {"query": "x"})
    know = kernel.tool_gateway.execute(sid, session.id, "search_knowledge", {"query": "x"})
    ctx = kernel._context_assembler.build(session_id=session.id, symbiote_id=sid, user_input="x")
    offered = [t["tool_id"] for t in ctx.available_tools]

    assert mem.success, mem.error
    assert know.success, know.error
    assert "search_memories" in offered and "search_knowledge" in offered


def test_search_tools_stay_blocked_in_packed_mode(kernel) -> None:
    sid = kernel.create_symbiote(name="a", role="r").id
    kernel.environment.configure(symbiote_id=sid, tools=["get_weather"])  # context_mode=packed

    res = kernel.tool_gateway.execute(sid, None, "search_memories", {"query": "x"})

    assert res.success is False
