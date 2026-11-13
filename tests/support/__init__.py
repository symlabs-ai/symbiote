"""Shared test helpers: scripted LLM, kernel factory, tool registration, API client.

Use these in new tests instead of redefining a fake LLM per file. Everything
here stays offline: no network, no real LLM, databases under ``tmp_path``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import ToolDescriptor

LLMScript = Callable[[int, list[dict]], Any]


class ScriptedLLM:
    """LLM fake driven by a function ``fn(call_index, messages) -> response``.

    The response may be a ``str`` or an ``LLMResponse``. Every call is kept
    in ``calls`` (deep copies of the messages) so tests can assert on what
    the model actually saw.
    """

    def __init__(self, fn: LLMScript | str = "ok") -> None:
        self._fn: LLMScript = (lambda _i, _m: fn) if isinstance(fn, str) else fn
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> Any:
        self.calls.append(
            {"messages": copy.deepcopy(messages), "config": config, "tools": tools}
        )
        return self._fn(len(self.calls) - 1, messages)

    def prompt_text(self, index: int = -1) -> str:
        """All message contents of one call joined — handy for ``in`` checks."""
        return "\n".join(str(m.get("content")) for m in self.calls[index]["messages"])

    def all_prompts_text(self) -> str:
        return "\n".join(self.prompt_text(i) for i in range(len(self.calls)))


def tool_call_block(tool: str, **params: Any) -> str:
    """Text-mode tool call block, as the model would emit it."""
    return "```tool_call\n" + json.dumps({"tool": tool, "params": params}) + "\n```"


def make_kernel(
    tmp_path: Path, llm: Any = None, **config: Any
) -> SymbioteKernel:
    """Kernel with its database and skills root inside ``tmp_path``."""
    config.setdefault("db_path", tmp_path / "symbiote.db")
    config.setdefault("skills_root", tmp_path / "skills")
    return SymbioteKernel(KernelConfig(**config), llm=llm)


def add_tool(
    kernel: SymbioteKernel,
    symbiote_id: str,
    tool_id: str,
    handler: Callable[[dict], Any],
    *,
    parameters: dict | None = None,
    **configure_kwargs: Any,
) -> None:
    """Register a custom tool and authorize it (plus any ``configure`` kwargs)."""
    descriptor = ToolDescriptor(
        tool_id=tool_id,
        name=tool_id,
        description=f"test tool {tool_id}",
        parameters=parameters or {"type": "object", "properties": {}},
    )
    kernel.tool_gateway.register_descriptor(descriptor, handler)
    current = kernel.environment.list_tools(symbiote_id)
    tools = current if tool_id in current else [*current, tool_id]
    kernel.environment.configure(symbiote_id=symbiote_id, tools=tools, **configure_kwargs)


class ApiHarness:
    """What the ``api_client`` fixture yields."""

    def __init__(self, client: Any, kernel: SymbioteKernel, key_manager: Any) -> None:
        self.client = client
        self.kernel = kernel
        self.key_manager = key_manager
        _, self.admin_key = key_manager.create_key("admin-tenant", "admin", role="admin")

    def new_key(self, tenant_id: str, role: str = "user") -> str:
        _, raw = self.key_manager.create_key(tenant_id, f"{tenant_id}-{role}", role=role)
        return raw

    @staticmethod
    def auth(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}


@pytest.fixture()
def api_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., ApiHarness]]:
    """Factory fixture: ``api_client(llm=None) -> ApiHarness`` with real auth.

    Disables ``SYMBIOTE_DEV_MODE`` (set globally in ``tests/conftest.py``),
    points the API at a database in ``tmp_path`` and resets the module-level
    singletons of ``symbiote.api.http`` / ``symbiote.api.middleware``.
    """
    from fastapi.testclient import TestClient

    import symbiote.api.http as http_mod
    import symbiote.api.middleware as mw_mod
    from symbiote.api.auth import APIKeyManager

    monkeypatch.delenv("SYMBIOTE_DEV_MODE", raising=False)
    monkeypatch.delenv("SYMBIOTE_LOCAL_ADMIN", raising=False)
    monkeypatch.setenv("SYMBIOTE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("SYMBIOTE_SKILL_SCOPE", "per_symbiote")
    monkeypatch.setenv("SYMBIOTE_SKILLS_ROOT", str(tmp_path / "skills"))

    kernels: list[SymbioteKernel] = []

    def factory(llm: Any = None) -> ApiHarness:
        kernel = SymbioteKernel(http_mod._resolve_config(), llm=llm)
        key_manager = APIKeyManager(kernel._storage)
        key_manager.init_schema()
        monkeypatch.setattr(http_mod, "_kernel", kernel)
        monkeypatch.setattr(http_mod, "_adapter", kernel._storage)
        monkeypatch.setattr(http_mod, "_tool_gateway", None)
        monkeypatch.setattr(http_mod, "_key_manager", key_manager)
        monkeypatch.setattr(http_mod, "_local_admin_key", None)
        monkeypatch.setattr(mw_mod, "_key_manager", key_manager)
        kernels.append(kernel)
        return ApiHarness(TestClient(http_mod.app), kernel, key_manager)

    yield factory

    http_mod.app.dependency_overrides.clear()
    for kernel in kernels:
        kernel.shutdown()
