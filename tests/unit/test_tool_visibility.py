"""B-76: ``tool_visibility="authorized"`` lists only the tools the symbiote may
run, and keeps authorized untagged builtins when the host uses tool_tags."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import ToolDescriptor
from tests.support import ScriptedLLM, make_kernel


def _register(kernel: SymbioteKernel, tool_id: str, tags: list[str]) -> None:
    kernel.tool_gateway.register_descriptor(
        ToolDescriptor(
            tool_id=tool_id,
            name=tool_id,
            description=f"tool {tool_id}",
            parameters={"type": "object", "properties": {}},
            tags=tags,
        ),
        lambda params: "ok",
    )


@pytest.fixture
def kernel(tmp_path: Path):
    k = make_kernel(tmp_path, ScriptedLLM("x"))
    _register(k, "tasks_list", ["tasks"])
    _register(k, "tasks_delete", ["tasks"])
    _register(k, "mail_send", ["mail"])
    _register(k, "plain_helper", [])
    yield k
    k.shutdown()


def _listed(kernel: SymbioteKernel, symbiote_id: str) -> set[str]:
    ctx = kernel.context_assembler.build(
        session_id="s", symbiote_id=symbiote_id, user_input="hi"
    )
    return {t["tool_id"] for t in ctx.available_tools}


def test_tagged_default_lists_unauthorized_tools(kernel: SymbioteKernel) -> None:
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(
        symbiote_id=sym.id, tools=["tasks_list"], tool_tags=["tasks"]
    )

    listed = _listed(kernel, sym.id)

    assert {"tasks_list", "tasks_delete"} <= listed
    assert "mail_send" not in listed
    assert "plain_helper" not in listed  # untagged, hidden by tool_tags


def test_authorized_lists_only_what_policy_allows(kernel: SymbioteKernel) -> None:
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(
        symbiote_id=sym.id,
        tools=["tasks_list", "plain_helper", "mail_send"],
        tool_tags=["tasks"],
        tool_visibility="authorized",
    )

    listed = _listed(kernel, sym.id)

    assert "tasks_list" in listed
    assert "tasks_delete" not in listed  # tagged but not authorized
    assert "mail_send" not in listed  # authorized, but tool_tags leave it out
    assert "plain_helper" in listed  # authorized and untagged


def test_authorized_without_tags(kernel: SymbioteKernel) -> None:
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(
        symbiote_id=sym.id, tools=["mail_send"], tool_visibility="authorized"
    )

    assert _listed(kernel, sym.id) == {"mail_send"}


def test_index_mode_keeps_its_meta_tool(kernel: SymbioteKernel) -> None:
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(
        symbiote_id=sym.id,
        tools=["tasks_list"],
        tool_loading="index",
        tool_visibility="authorized",
    )

    assert _listed(kernel, sym.id) == {"get_tool_schema", "tasks_list"}


def test_get_tool_schema_follows_visibility(kernel: SymbioteKernel) -> None:
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(
        symbiote_id=sym.id,
        tools=["plain_helper"],
        tool_tags=["tasks"],
        tool_loading="index",
    )
    kernel.tool_gateway.register_index_tool()
    gateway = kernel.tool_gateway

    def schema(tool_id: str) -> dict:
        return gateway.execute(
            symbiote_id=sym.id,
            session_id="s",
            tool_id="get_tool_schema",
            params={"tool_id": tool_id},
        ).output

    assert "error" in schema("plain_helper")  # tagged mode: hidden by tool_tags
    kernel.environment.configure(symbiote_id=sym.id, tool_visibility="authorized")
    assert schema("plain_helper")["tool_id"] == "plain_helper"


def test_invalid_value_rejected(kernel: SymbioteKernel) -> None:
    sym = kernel.create_symbiote(name="a", role="assistant")
    with pytest.raises(ValidationError):
        kernel.environment.configure(symbiote_id=sym.id, tool_visibility="everything")


def test_value_persists(tmp_path: Path) -> None:
    k = make_kernel(tmp_path, ScriptedLLM("x"))
    sym = k.create_symbiote(name="a", role="assistant")
    k.environment.configure(symbiote_id=sym.id, tool_visibility="authorized")
    k.shutdown()

    k2 = make_kernel(tmp_path, ScriptedLLM("x"))
    assert k2.environment.get_tool_visibility(sym.id) == "authorized"
    k2.shutdown()


@pytest.mark.sqlite_only
def test_sqlite_migration_adds_column(tmp_path: Path) -> None:
    adapter = SQLiteAdapter(tmp_path / "db.sqlite")
    adapter.init_schema()
    cols = {r["name"] for r in adapter.fetch_all("PRAGMA table_info(environment_configs)")}
    assert "tool_visibility" in cols
    adapter.close()
