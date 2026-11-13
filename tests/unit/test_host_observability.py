"""Host-facing observability added for embedded hosts (the demo app):

- ``kernel.set_progress_callbacks`` reaches the internal chat and long-run runners;
- the approval gate covers long-run blocks and does not block the event loop
  in ``message_async``;
- ``BaseHook.on_context`` sees the context of each turn;
- ``kernel.inspector`` reads the stored state;
- ``KnowledgeService.query`` matches by terms, not only by substring.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from symbiote.core.hooks import BaseHook
from symbiote.environment.descriptors import ToolDescriptor
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


def _last_content(messages: list[dict]) -> str:
    return str(messages[-1].get("content"))


def _tool_then_text(tool_id: str, text: str = "feito"):
    """First reply calls ``tool_id``; once a tool result is in the prompt, answer."""

    def fn(_i: int, messages: list[dict]) -> str:
        if "[Tool" in _last_content(messages):
            return text
        return tool_call_block(tool_id)

    return fn


def _register_high_risk(kernel: Any, symbiote_id: str, tool_id: str, ran: list[str]) -> None:
    def handler(_params: dict) -> str:
        ran.append(tool_id)
        return "ok"

    kernel.tool_gateway.register_descriptor(
        ToolDescriptor(
            tool_id=tool_id, name=tool_id, description="dangerous", risk_level="high"
        ),
        handler,
    )
    kernel.environment.add_tools(symbiote_id, [tool_id])


# ── K1: progress callbacks ───────────────────────────────────────────────


def test_progress_callbacks_reach_chat_runner(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM(_tool_then_text("ping")))
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "ping", lambda _p: "pong")
    session = kernel.start_session(sym.id)

    events: list[str] = []
    streamed: list[tuple[str, int]] = []
    kernel.set_progress_callbacks(
        on_progress=lambda event, _i, _n: events.append(event),
        on_stream=lambda text, i: streamed.append((text, i)),
    )

    kernel.message(session.id, "use ping")

    assert events[:3] == ["iteration_start", "tool_start", "tool_end"]
    assert "iteration_end" in events
    assert streamed[-1] == ("feito", 2)

    # Each call replaces all callbacks; omitted ones are cleared.
    kernel.set_progress_callbacks()
    events.clear()
    kernel.message(session.id, "use ping again")
    assert events == []
    kernel.shutdown()


def test_progress_callbacks_reach_long_run(tmp_path: Path) -> None:
    plan = json.dumps(
        [
            {"name": "A", "description": "d", "success_criteria": "c"},
            {"name": "B", "description": "d", "success_criteria": "c"},
        ]
    )

    def fn(_i: int, messages: list[dict]) -> str:
        if str(messages[0].get("content")).startswith("PLANNER"):
            return plan
        return "bloco pronto"

    kernel = make_kernel(tmp_path, ScriptedLLM(fn))
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(
        symbiote_id=sym.id, tool_mode="long_run", planner_prompt="PLANNER: split"
    )
    session = kernel.start_session(sym.id)

    plans: list[Any] = []
    blocks: list[Any] = []
    events: list[str] = []
    kernel.set_progress_callbacks(
        on_progress=lambda event, _i, _n: events.append(event),
        on_plan=plans.append,
        on_block_complete=blocks.append,
    )

    kernel.message(session.id, "build it")

    assert [b["name"] for b in plans[0].blocks] == ["A", "B"]
    assert [b.block_name for b in blocks] == ["A", "B"]
    assert events[0] == "plan_complete"
    assert events.count("block_start") == 2
    kernel.shutdown()


def test_failing_on_plan_does_not_break_the_run(tmp_path: Path) -> None:
    def fn(_i: int, messages: list[dict]) -> str:
        if str(messages[0].get("content")).startswith("PLANNER"):
            return json.dumps([{"name": "A", "description": "d", "success_criteria": "c"}])
        return "ok"

    kernel = make_kernel(tmp_path, ScriptedLLM(fn))
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(
        symbiote_id=sym.id, tool_mode="long_run", planner_prompt="PLANNER: split"
    )
    session = kernel.start_session(sym.id)

    def boom(_plan: Any) -> None:
        raise RuntimeError("host bug")

    kernel.set_progress_callbacks(on_plan=boom)
    assert "1/1 blocks" in kernel.message(session.id, "go")
    kernel.shutdown()


def test_set_progress_callbacks_requires_llm(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, None)
    with pytest.raises(RuntimeError):
        kernel.set_progress_callbacks(on_progress=lambda *_: None)
    kernel.shutdown()


# ── K2: approval gate ────────────────────────────────────────────────────


def test_approval_gate_covers_long_run_blocks(tmp_path: Path) -> None:
    plan = json.dumps([{"name": "A", "description": "d", "success_criteria": "c"}])

    def fn(i: int, messages: list[dict]) -> str:
        if str(messages[0].get("content")).startswith("PLANNER"):
            return plan
        return _tool_then_text("wipe")(i, messages)

    kernel = make_kernel(tmp_path, ScriptedLLM(fn))
    sym = kernel.create_symbiote(name="a", role="assistant")
    ran: list[str] = []
    _register_high_risk(kernel, sym.id, "wipe", ran)
    kernel.environment.configure(
        symbiote_id=sym.id, tool_mode="long_run", planner_prompt="PLANNER: split"
    )
    asked: list[str] = []

    def gate(tool_id: str, _params: dict, _risk: str) -> bool:
        asked.append(tool_id)
        return False

    kernel.set_approval_callback(gate)
    session = kernel.start_session(sym.id)

    kernel.message(session.id, "clean everything")

    assert asked == ["wipe"]
    assert ran == []
    kernel.shutdown()


def test_async_approval_gate_does_not_block_event_loop(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM(_tool_then_text("wipe")))
    sym = kernel.create_symbiote(name="a", role="assistant")
    ran: list[str] = []
    _register_high_risk(kernel, sym.id, "wipe", ran)
    session = kernel.start_session(sym.id)

    gate_threads: list[threading.Thread] = []

    def slow_gate(_tool_id: str, _params: dict, _risk: str) -> bool:
        gate_threads.append(threading.current_thread())
        time.sleep(0.4)  # a human deciding
        return True

    kernel.set_approval_callback(slow_gate)

    async def scenario() -> int:
        ticks = 0
        done = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(0.02)

        tick_task = asyncio.create_task(ticker())
        await kernel.message_async(session.id, "wipe it")
        done.set()
        await tick_task
        return ticks

    ticks = asyncio.run(scenario())

    assert ran == ["wipe"]
    assert gate_threads and gate_threads[0] is not threading.main_thread()
    assert ticks >= 10  # the loop kept running while the gate waited
    kernel.shutdown()


# ── K3: on_context hook ──────────────────────────────────────────────────


class _ContextRecorder(BaseHook):
    def __init__(self) -> None:
        self.contexts: list[Any] = []

    def on_context(self, context: Any) -> None:
        self.contexts.append(context)


def test_on_context_sees_each_turn_sync_and_async(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("oi"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)
    kernel.capabilities.learn(sym.id, session.id, "User prefers the vim editor", "preference")
    recorder = _ContextRecorder()
    kernel.hooks.add(recorder)

    kernel.message(session.id, "Which editor do I prefer?", extra_context={"page": "x"})
    asyncio.run(kernel.message_async(session.id, "editor again?"))

    first = recorder.contexts[0]
    assert first.user_input == "Which editor do I prefer?"
    assert first.extra_context == {"page": "x"}
    assert any("vim" in m["content"] for m in first.relevant_memories)
    assert recorder.contexts[1].user_input == "editor again?"
    assert kernel.context_assembler.inspect(first).budget > 0
    kernel.shutdown()


def test_async_on_context_is_ignored_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class AsyncByMistake(BaseHook):
        async def on_context(self, context: Any) -> None:  # type: ignore[override]
            raise AssertionError("must not run")

    kernel = make_kernel(tmp_path, ScriptedLLM("oi"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)
    kernel.hooks.add(AsyncByMistake())

    with caplog.at_level("WARNING"):
        assert kernel.message(session.id, "hi") == "oi"
    assert "must be synchronous" in caplog.text
    kernel.shutdown()


# ── K4: inspector ────────────────────────────────────────────────────────


def test_inspector_reads_what_a_turn_and_close_produce(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM(_tool_then_text("ping", "pronto")))
    sym = kernel.create_symbiote(name="Sol", role="assistant")
    other = kernel.create_symbiote(name="Outro", role="assistant")
    add_tool(kernel, sym.id, "ping", lambda _p: "pong")
    session = kernel.start_session(sym.id, goal="g")
    kernel.capabilities.learn(sym.id, session.id, "fato", "factual")
    kernel.message(session.id, "Eu sempre prefiro respostas curtas")
    kernel.close_session(session.id)
    ins = kernel.inspector

    sessions = ins.list_sessions(sym.id)
    assert sessions[0]["id"] == session.id
    assert sessions[0]["symbiote_name"] == "Sol"
    assert sessions[0]["status"] == "closed"
    assert sessions[0]["final_score"] is not None
    assert ins.list_sessions(other.id) == []

    roles = [m["role"] for m in ins.list_messages(session.id)]
    assert roles == ["user", "assistant"]

    traces = ins.list_traces(session_id=session.id)
    assert traces[0]["steps"][0]["tool_id"] == "ping"
    assert ins.list_traces(symbiote_id=sym.id)[0]["id"] == traces[0]["id"]
    with pytest.raises(ValueError):
        ins.list_traces()

    audit = ins.audit_log(sym.id)
    assert audit[0]["tool_id"] == "ping"
    assert audit[0]["params"] == {}
    assert ins.audit_log(sym.id, session_id="nope") == []

    memories = ins.list_memories(sym.id)
    assert {m["content"] for m in memories} >= {"fato"}
    assert all(isinstance(m["tags"], list) for m in memories)
    assert ins.list_memories(other.id) == []
    assert ins.list_scores(sym.id)[0]["session_id"] == session.id
    assert ins.list_reflections(sym.id) == []  # keyword mode writes no audit row
    assert ins.list_skill_reviews(sym.id) == []

    kernel.dream(sym.id, dry_run=True)
    reports = ins.list_dream_reports(sym.id)
    assert reports[0]["dry_run"] == 1
    assert isinstance(reports[0]["phases"], list)
    kernel.shutdown()


def test_public_component_properties(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, None)
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.knowledge.register_source(sym.id, "doc", content="conteudo")
    assert kernel.knowledge.list_by_symbiote(sym.id)[0].name == "doc"
    assert kernel.memory.search("", symbiote_id=sym.id) == []
    assert kernel.workspaces.list_by_symbiote(sym.id) == []
    assert kernel.context_assembler is not None
    kernel.shutdown()


# ── K5: knowledge term matching ──────────────────────────────────────────


def test_knowledge_query_matches_terms_of_a_whole_message(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, None)
    sym = kernel.create_symbiote(name="a", role="assistant")
    other = kernel.create_symbiote(name="b", role="assistant")
    kernel.knowledge.register_source(
        sym.id, "Mudança", content="A mudança para Curitiba está marcada para março."
    )
    kernel.knowledge.register_source(sym.id, "Receitas", content="Bolo de cenoura.")
    kernel.knowledge.register_source(
        other.id, "Mudança", content="A mudança para Curitiba é do outro symbiote."
    )

    found = kernel.knowledge.query(sym.id, "Quando é a minha mudança pra Curitiba?")

    assert [e.name for e in found] == ["Mudança"]
    assert found[0].symbiote_id == sym.id


def test_knowledge_query_keeps_substring_matches_first(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, None)
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.knowledge.register_source(sym.id, "Deploy guide", content="how to deploy safely")
    kernel.knowledge.register_source(sym.id, "Rollback", content="deployment rollback steps")

    found = kernel.knowledge.query(sym.id, "deploy")
    assert {e.name for e in found} == {"Deploy guide", "Rollback"}

    found = kernel.knowledge.query(sym.id, "deploy safely", limit=1)
    assert [e.name for e in found] == ["Deploy guide"]
    assert kernel.knowledge.query(sym.id, "the of and") == []


# ── host helpers added with the demo ─────────────────────────────────────


def test_update_and_list_symbiotes(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, None)
    sym = kernel.create_symbiote(name="a", role="assistant", persona={"tone": "formal"})
    updated = kernel.update_symbiote(sym.id, persona={"tone": "casual"})
    assert updated.persona_json == {"tone": "casual"}
    assert [s.id for s in kernel.list_symbiotes()] == [sym.id]
    kernel.shutdown()


def test_discover_tools_from_a_repository(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/items')\ndef list_items():\n    return []\n"
    )
    kernel = make_kernel(tmp_path, None)
    sym = kernel.create_symbiote(name="a", role="assistant")

    result = kernel.discover_tools(sym.id, str(repo))

    assert [t.tool_id for t in result.discovered if t.handler_type == "http"] == ["get_items"]
    assert kernel.discovered_tools.list(sym.id, status="pending")[0].tool_id == "get_items"
    kernel.discovered_tools.set_status(sym.id, "get_items", "approved")
    assert kernel.load_discovered_tools(sym.id, base_url="https://api.example.com") == ["get_items"]
    kernel.shutdown()


def test_harness_helpers(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("x"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    assert "tool" in kernel.harness_default_text("tool_instructions").lower()
    with pytest.raises(ValueError):
        kernel.harness_default_text("nope")
    tuning = kernel.tune_parameters(sym.id)
    assert tuning.tier == 0 and tuning.adjustments == {}
    assert kernel.cross_learning_candidates(sym.id) == []
    # Without default_text the built-in one is used; too little data → refused.
    result = kernel.evolve_harness(sym.id, "tool_instructions")
    assert result.success is False and result.reason
    kernel.shutdown()


def test_empty_string_clears_long_run_prompts(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, None)
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(symbiote_id=sym.id, planner_prompt="P", evaluator_prompt="E")
    cfg = kernel.environment.configure(symbiote_id=sym.id, evaluator_prompt="")
    assert cfg.planner_prompt == "P"
    assert cfg.evaluator_prompt is None
    assert kernel.environment.get_config(sym.id).evaluator_prompt is None
    kernel.shutdown()
