"""Redaction of secrets in what the kernel stores (P3 of the SymGateway request).

A value the redactor recognizes, through a parameter annotated in the tool
descriptor or through the host's function, appears in no table and in no
skill file. The model still gets the real value in the turn that uses it.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from symbiote.core.kernel import SymbioteKernel
from symbiote.core.redaction import RedactingStorage, Redactor
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor
from symbiote.skills.scope import reset_active_symbiote, set_active_symbiote
from tests.support import ScriptedLLM, make_kernel, tool_call_block

KEY = "sk-live-9f8e7d6c5b4a"
SKILL = "---\nname: {name}\ndescription: how to register a provider\n---\n\n# {name}\n\n{body}\n"


def _tables(kernel: SymbioteKernel) -> list[str]:
    rows = kernel._storage.fetch_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return [r["name"] for r in rows]


def _everything_stored(kernel: SymbioteKernel, root: Path) -> str:
    """Every row of every table plus every file under the skills root."""
    parts = [
        json.dumps(row, default=str, ensure_ascii=False)
        for table in _tables(kernel)
        for row in kernel._storage.fetch_all(f"SELECT * FROM {table}")  # noqa: S608
    ]
    skills = root / "skills"
    if skills.exists():
        parts += [p.read_text() for p in skills.rglob("*") if p.is_file()]
    return "\n".join(parts)


def _assert_json_columns_valid(kernel: SymbioteKernel) -> None:
    for table in _tables(kernel):
        for row in kernel._storage.fetch_all(f"SELECT * FROM {table}"):  # noqa: S608
            for column, value in row.items():
                if column.endswith("_json") and value:
                    json.loads(value)


def _provider_tool(kernel: SymbioteKernel, symbiote_id: str, received: list[dict]) -> None:
    kernel.tool_gateway.register_descriptor(
        ToolDescriptor(
            tool_id="create_provider",
            name="create_provider",
            description="Register an LLM provider",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}, "api_key": {"type": "string"}},
            },
            secret_params=["api_key"],
        ),
        lambda params: received.append(params) or f"provider created with {params['api_key']}",
    )
    kernel.environment.add_tools(symbiote_id, ["create_provider"])


class TestAnnotatedParameters:
    def _run(self, tmp_path: Path, *, use_async: bool = False):  # noqa: ANN202
        def script(i: int, _m: list[dict]) -> str:
            if i == 0:
                return tool_call_block("create_provider", name="acme", api_key=KEY)
            return f"Done, I always remember that the key {KEY} belongs to acme."

        llm = ScriptedLLM(script)
        kernel = make_kernel(tmp_path, llm, skill_scope="per_symbiote")
        kernel.set_evolver_llm(llm)
        sym = kernel.create_symbiote(name="admin", role="assistant")
        received: list[dict] = []
        _provider_tool(kernel, sym.id, received)
        kernel.environment.configure(symbiote_id=sym.id, reflection_mode="hybrid")
        session = kernel.start_session(sym.id)
        text = f"I always want provider acme registered with the key {KEY}, remember that"
        if use_async:
            reply = asyncio.run(kernel.message_async(session.id, text))
        else:
            reply = kernel.message(session.id, text)
        return kernel, llm, sym, session, received, reply

    @pytest.mark.parametrize("use_async", [False, True])
    def test_value_is_in_no_table(self, tmp_path: Path, use_async: bool) -> None:
        kernel, llm, sym, session, received, reply = self._run(tmp_path, use_async=use_async)
        kernel.close_session(session.id)  # reflection, scoring, failure memory
        kernel.dream(sym.id)

        # The turn itself worked with the real value...
        assert received == [{"name": "acme", "api_key": KEY}]
        assert KEY in llm.prompt_text(1)  # the tool result went back to the model
        assert KEY in reply["text"]  # and the caller got the model's answer as is
        # ...and none of it was stored.
        stored = _everything_stored(kernel, tmp_path)
        assert KEY not in stored
        assert "[REDACTED]" in stored
        _assert_json_columns_valid(kernel)
        kernel.shutdown()

    def test_records_of_the_call_are_masked(self, tmp_path: Path) -> None:
        kernel, _llm, sym, session, _received, _reply = self._run(tmp_path)

        audit = kernel.inspector.audit_log(sym.id)
        assert audit[0]["params"] == {"name": "acme", "api_key": "[REDACTED]"}
        trace = kernel.inspector.list_traces(session_id=session.id)[0]
        assert trace["steps"][0]["params"] == {"name": "acme", "api_key": "[REDACTED]"}
        messages = [m["content"] for m in kernel.inspector.list_messages(session.id)]
        assert len(messages) == 2
        assert all("[REDACTED]" in m and KEY not in m for m in messages)
        kernel.shutdown()

    def test_the_message_stored_before_the_call_is_scrubbed(self, tmp_path: Path) -> None:
        """The user's message is saved before the tool call that shows the
        value is a secret; an earlier turn may hold it too."""
        def script(i: int, _m: list[dict]) -> str:
            if i == 1:
                return tool_call_block("create_provider", name="acme", api_key=KEY)
            return "ok"

        kernel = make_kernel(tmp_path, ScriptedLLM(script))
        sym = kernel.create_symbiote(name="admin", role="assistant")
        _provider_tool(kernel, sym.id, [])
        session = kernel.start_session(sym.id)
        other = kernel.start_session(sym.id)

        kernel.message(session.id, f"my key is {KEY}")  # turn 1: nothing known yet
        assert KEY in _everything_stored(kernel, tmp_path)
        kernel.message(session.id, "now register acme with it")  # turn 2: the tool call

        assert KEY not in _everything_stored(kernel, tmp_path)
        # Learned for the whole kernel: another session does not store it either.
        kernel.message(other.id, f"is {KEY} a valid key?")
        assert KEY not in _everything_stored(kernel, tmp_path)
        kernel.shutdown()

    def test_nested_and_list_paths(self) -> None:
        redactor = Redactor()
        params = {
            "credentials": {"token": "tok-abcdef", "user": "ana"},
            "headers": [{"value": "Bearer zzz111"}, {"value": "Bearer yyy222"}],
            "pin": 42,
        }
        paths = ["credentials.token", "headers.value", "pin", "missing.path"]

        assert sorted(redactor.learn(params, paths)) == [
            "Bearer yyy222", "Bearer zzz111", "tok-abcdef",
        ]
        assert redactor.mask_params(params, paths) == {
            "credentials": {"token": "[REDACTED]", "user": "ana"},
            "headers": [{"value": "[REDACTED]"}, {"value": "[REDACTED]"}],
            "pin": "[REDACTED]",  # too short to learn, still masked in the record
        }
        assert params["pin"] == 42  # the caller's dict is not touched
        assert redactor.redact_text("pin 42, token tok-abcdef") == "pin 42, token [REDACTED]"

    def test_skill_files(self, tmp_path: Path) -> None:
        kernel, _llm, sym, _session, _received, _reply = self._run(tmp_path)
        kernel.environment.add_tools(sym.id, ["skill_manage"])
        token = set_active_symbiote(sym.id)
        try:
            result = kernel.tool_gateway.execute(
                sym.id, None, "skill_manage",
                {"action": "create", "name": "register-provider",
                 "content": SKILL.format(name="register-provider", body=f"Use the key {KEY}.")},
            )
        finally:
            reset_active_symbiote(token)

        assert '"success": true' in result.output
        written = (tmp_path / "skills" / sym.id / "skills" / "register-provider" / "SKILL.md").read_text()
        assert "Use the key [REDACTED]." in written
        assert KEY not in _everything_stored(kernel, tmp_path)
        kernel.shutdown()


class TestHostFunction:
    PATTERN = re.compile(r"sk-[a-z0-9-]{8,}")

    def _kernel(self, tmp_path: Path, llm: Any = "noted"):  # noqa: ANN202
        kernel = make_kernel(tmp_path, ScriptedLLM(llm), skill_scope="per_symbiote")
        kernel.set_redactor(lambda text: self.PATTERN.sub("[REDACTED]", text))
        sym = kernel.create_symbiote(name="admin", role="assistant")
        return kernel, sym

    def test_free_text_is_redacted_everywhere(self, tmp_path: Path) -> None:
        def script(i: int, _m: list[dict]) -> str:
            if i == 0:
                return tool_call_block("echo", text=f'he said "{KEY}"\nand left')
            return f"noted: {KEY}"

        kernel, sym = self._kernel(tmp_path, script)
        kernel.tool_gateway.register_tool("echo", lambda params: params["text"])
        kernel.environment.add_tools(sym.id, ["echo"])
        session = kernel.start_session(sym.id)
        kernel.message(session.id, f"I always use {KEY}, remember that")
        kernel.capabilities.learn(sym.id, session.id, f"the production key is {KEY}")
        kernel.close_session(session.id)

        stored = _everything_stored(kernel, tmp_path)
        assert KEY not in stored
        _assert_json_columns_valid(kernel)
        # An unannotated parameter is redacted inside the JSON it is stored in.
        assert kernel.inspector.audit_log(sym.id)[0]["params"] == {
            "text": 'he said "[REDACTED]"\nand left'
        }
        assert kernel.memory.search("production key", symbiote_id=sym.id)[0].content == (
            "the production key is [REDACTED]"
        )
        kernel.shutdown()

    def test_a_failing_function_withholds_the_text(self, tmp_path: Path) -> None:
        kernel, sym = self._kernel(tmp_path)

        def broken(text: str) -> str:
            raise RuntimeError("regex engine exploded")

        kernel.set_redactor(broken)
        session = kernel.start_session(sym.id)
        kernel.message(session.id, f"the key is {KEY}")

        stored = _everything_stored(kernel, tmp_path)
        assert KEY not in stored
        assert "the host redactor failed" in stored
        kernel.shutdown()

    def test_configuration_and_identifiers_are_left_alone(self, tmp_path: Path) -> None:
        kernel, sym = self._kernel(tmp_path)
        kernel.register_http_tool(
            sym.id,
            ToolDescriptor(tool_id="ping", name="ping", description="d"),
            HttpToolConfig(
                method="GET", url_template="https://example.com/x",
                headers={"Authorization": f"Bearer {KEY}"},
            ),
        )
        kernel.knowledge.register_source(sym.id, "runbook", content=f"rotate {KEY} monthly")
        session = kernel.get_or_create_session(sym.id, external_key=f"tenant:{KEY}")
        # A secret that happens to be part of an identifier must not break it.
        kernel.redactor.add_secret(session.id[:13])
        kernel.message(session.id, f"hello, my id starts with {session.id[:13]}")

        again = kernel.get_or_create_session(sym.id, external_key=f"tenant:{KEY}")
        assert again.id == session.id
        messages = kernel.inspector.list_messages(session.id)
        assert len(messages) == 2  # still linked to the session
        assert "hello, my id starts with [REDACTED]" in [m["content"] for m in messages]
        config = kernel._storage.fetch_one("SELECT config_json FROM http_tools")["config_json"]
        assert KEY in config
        assert KEY in kernel.knowledge.query(sym.id, "rotate")[0].content
        kernel.shutdown()

    def test_removed_with_none(self, tmp_path: Path) -> None:
        kernel, sym = self._kernel(tmp_path)
        kernel.set_redactor(None)
        session = kernel.start_session(sym.id)
        kernel.message(session.id, f"the key is {KEY}")

        assert KEY in _everything_stored(kernel, tmp_path)
        kernel.shutdown()


class TestStorageWrapper:
    def test_no_redactor_means_no_change(self, tmp_path: Path) -> None:
        kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
        sym = kernel.create_symbiote(name="a", role="assistant")
        session = kernel.start_session(sym.id)
        kernel.message(session.id, f"the key is {KEY}")

        assert isinstance(kernel._storage, RedactingStorage)
        assert not kernel.redactor.active
        contents = [m["content"] for m in kernel.inspector.list_messages(session.id)]
        assert f"the key is {KEY}" in contents
        kernel.shutdown()

    def test_adapter_internals_stay_reachable(self, tmp_path: Path) -> None:
        kernel = make_kernel(tmp_path)
        assert kernel._storage._db_path == tmp_path / "symbiote.db"
        kernel._storage._marker = "x"  # assignments reach the adapter too
        assert kernel._storage.inner._marker == "x"
        kernel.shutdown()

    def test_registered_literal(self, tmp_path: Path) -> None:
        kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
        sym = kernel.create_symbiote(name="a", role="assistant")
        assert kernel.redactor.add_secret(KEY) is True
        assert kernel.redactor.add_secret(KEY) is False  # already known
        assert kernel.redactor.add_secret("abc") is False  # too short to learn
        session = kernel.start_session(sym.id)
        kernel.message(session.id, f"the key is {KEY}")

        assert KEY not in _everything_stored(kernel, tmp_path)
        kernel.shutdown()
