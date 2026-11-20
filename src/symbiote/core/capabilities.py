"""CapabilitySurface — the six capabilities as explicit methods."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from symbiote.core.context import ContextAssembler
from symbiote.core.exceptions import SymbioteError
from symbiote.core.hooks import CompositeHook
from symbiote.core.identity import IdentityManager
from symbiote.core.models import MemoryEntry
from symbiote.core.origin import speaker
from symbiote.core.ports import KnowledgePort, MemoryPort
from symbiote.core.session import SessionManager
from symbiote.core.subjects import subject_kwargs
from symbiote.core.turn_record import current_turn_record
from symbiote.runners.base import RunnerRegistry, RunResult


class CapabilityError(SymbioteError):
    """Raised when a capability cannot be executed."""

    def __init__(self, capability: str, message: str) -> None:
        self.capability = capability
        super().__init__(f"[{capability}] {message}")


class CapabilitySurface:
    """Exposes six high-level capabilities backed by kernel components.

    Each method represents one capability: learn, teach, chat, work, show, reflect.
    """

    def __init__(
        self,
        identity: IdentityManager,
        sessions: SessionManager,
        memory: MemoryPort,
        knowledge: KnowledgePort,
        context_assembler: ContextAssembler,
        runner_registry: RunnerRegistry,
        export_fn: Callable[[str, str], str] | None = None,
        hooks: CompositeHook | None = None,
    ) -> None:
        self._identity = identity
        self._sessions = sessions
        self._memory = memory
        self._knowledge = knowledge
        self._context_assembler = context_assembler
        self._runner_registry = runner_registry
        self._export_fn = export_fn
        self._hooks = hooks
        self._last_loop_trace = None
        self._last_handoff_data: dict | None = None

    @property
    def last_loop_trace(self):
        """LoopTrace of the most recent chat call on this surface, or None.

        Deprecated: shared by every session, so concurrent turns overwrite
        each other. Use ``run_chat()`` / ``run_chat_async()``, which return
        the RunResult (with its trace) of that call only.
        """
        return self._last_loop_trace

    @property
    def last_handoff_data(self) -> dict | None:
        """Handoff artifact of the most recent long-run call, or None.

        Deprecated for the same reason as ``last_loop_trace``.
        """
        return self._last_handoff_data

    # ── learn ────────────────────────────────────────────────────────────

    def learn(
        self,
        symbiote_id: str,
        session_id: str,
        content: str,
        fact_type: str = "factual",
        importance: float = 0.7,
    ) -> MemoryEntry:
        """Persist a durable fact as long-term memory.

        In the session of an end user (a session with ``subject_key``) the
        fact belongs to that end user; otherwise it is shared by every
        session of the symbiote.

        Raises ``CapabilityError`` when long-term memory is off for the
        symbiote (``memory_enabled=False``).
        """
        is_enabled = getattr(self._memory, "is_enabled", None)
        if is_enabled is not None and not is_enabled(symbiote_id):
            raise CapabilityError(
                "learn", "long-term memory is disabled for this symbiote"
            )
        entry = MemoryEntry(
            symbiote_id=symbiote_id,
            session_id=session_id,
            type=fact_type,  # type: ignore[arg-type]
            scope="global",
            content=content,
            importance=importance,
            source="user",
        )
        self._memory.store(entry)
        return entry

    # ── teach ────────────────────────────────────────────────────────────

    def teach(
        self,
        symbiote_id: str,
        session_id: str,
        query: str,
    ) -> str:
        """Query knowledge + relevant memories, return a structured explanation."""
        knowledge_entries = self._knowledge.query(symbiote_id, query)
        memories = self._memory.get_relevant(
            query, session_id, symbiote_id=symbiote_id,
            **subject_kwargs(self._memory, session_id),
        )

        parts: list[str] = []

        if knowledge_entries:
            parts.append("## Knowledge")
            for k in knowledge_entries:
                parts.append(f"### {k.name}")
                if k.content:
                    parts.append(k.content)

        if memories:
            parts.append("## Relevant Memories")
            for m in memories:
                parts.append(f"- [{m.type}] {m.content}")

        if not parts:
            return f"No knowledge or memories found for: {query}"

        return "\n\n".join(parts)

    # ── chat ─────────────────────────────────────────────────────────────

    def chat(
        self,
        symbiote_id: str,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        llm_config: dict | None = None,
        *,
        origin: str | None = None,
    ) -> str:
        """Build context, select ChatRunner, run, return response text.

        ``llm_config`` is forwarded to the runner so per-call overrides
        (e.g. ``{"mode": "high"}`` for effort escalation) reach the LLM
        adapter via the ``config`` kwarg of ``stream()``. ``origin`` names
        who wrote ``content`` when the person did not (``kernel.message``).
        """
        result = self.run_chat(
            symbiote_id, session_id, content, extra_context, llm_config, origin=origin
        )
        if not result.success:
            raise CapabilityError("chat", result.error or "Chat runner failed")
        return result.output

    def run_chat(
        self,
        symbiote_id: str,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        llm_config: dict | None = None,
        *,
        origin: str | None = None,
    ) -> RunResult:
        """Like ``chat()`` but returns the RunResult (trace, handoff) of this
        call and does not raise when the run fails."""
        runner = self._runner_registry.select("chat")
        if runner is None:
            raise CapabilityError("chat", "No runner available for intent 'chat'")

        context = self._context_assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input=content,
            extra_context=extra_context,
            input_origin=origin,
        )
        record = current_turn_record()
        if record is not None:
            record.note_context(context)
        if self._hooks is not None:
            self._hooks.on_context(context)
        runner = self._runner_for_mode(context, runner)

        result = runner.run(context, llm_config=llm_config)
        self._last_loop_trace = getattr(result, "loop_trace", None)
        self._last_handoff_data = getattr(result, "handoff_data", None)
        return result

    async def chat_async(
        self,
        symbiote_id: str,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
        *,
        origin: str | None = None,
    ) -> Any:
        """Async variant of chat() — uses run_async() so tool handlers can be coroutines.

        Args:
            on_token: Optional callback invoked with each token as it is generated.
                Requires the LLM adapter to expose a ``stream()`` method; otherwise
                called once with the full response text.
            llm_config: Per-call LLM overrides forwarded to the runner.
            origin: Who wrote ``content`` when the person did not.
        """
        result = await self.run_chat_async(
            symbiote_id,
            session_id,
            content,
            extra_context,
            on_token,
            llm_config,
            origin=origin,
        )
        if not result.success:
            raise CapabilityError("chat", result.error or "Chat runner failed")
        return result.output

    async def run_chat_async(
        self,
        symbiote_id: str,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
        *,
        origin: str | None = None,
    ) -> RunResult:
        """Async ``run_chat()``: returns the RunResult and does not raise on a
        failed run."""
        runner = self._runner_registry.select("chat")
        if runner is None:
            raise CapabilityError("chat", "No runner available for intent 'chat'")

        context = self._context_assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input=content,
            extra_context=extra_context,
            input_origin=origin,
        )
        record = current_turn_record()
        if record is not None:
            record.note_context(context)
        if self._hooks is not None:
            self._hooks.on_context(context)

        runner = self._runner_for_mode(context, runner)
        if not hasattr(runner, "run_async"):
            raise CapabilityError("chat", "Runner does not support async execution")

        result = await runner.run_async(context, on_token=on_token, llm_config=llm_config)
        self._last_loop_trace = getattr(result, "loop_trace", None)
        self._last_handoff_data = getattr(result, "handoff_data", None)
        return result

    def _runner_for_mode(self, context: Any, default: Any) -> Any:
        """``tool_mode="long_run"`` goes to the LongRunRunner when registered."""
        if getattr(context, "tool_mode", None) == "long_run":
            long_run = self._runner_registry.get("long_run")
            if long_run is not None:
                return long_run
        return default

    # ── work ─────────────────────────────────────────────────────────────

    def work(
        self,
        symbiote_id: str,
        session_id: str,
        task: str,
        intent: str | None = None,
    ) -> dict:
        """Select appropriate runner by intent, run, return result dict.

        If *intent* is not provided, extracts it from the task string
        (first word before ':' or the full task).
        """
        if intent is None:
            intent = task.split(":")[0].strip() if ":" in task else task.strip()

        runner = self._runner_registry.select(intent)
        if runner is None:
            raise CapabilityError("work", f"No runner available for intent '{intent}'")

        context = self._context_assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input=task,
        )

        result = runner.run(context)
        return {
            "success": result.success,
            "output": result.output,
            "error": result.error,
            "runner_type": result.runner_type,
        }

    # ── show ─────────────────────────────────────────────────────────────

    def show(
        self,
        symbiote_id: str,
        session_id: str,
        query: str,
    ) -> str:
        """Format and return relevant data as readable Markdown."""
        memories = self._memory.get_relevant(
            query, session_id, symbiote_id=symbiote_id,
            **subject_kwargs(self._memory, session_id),
        )
        knowledge_entries = self._knowledge.query(symbiote_id, query)
        messages = self._sessions.get_messages(session_id, limit=20)

        parts: list[str] = []
        parts.append(f"# Show: {query}")

        if messages:
            parts.append("## Session Messages")
            for msg in reversed(messages):  # chronological order
                parts.append(f"- **{speaker(msg.role, msg.origin)}**: {msg.content}")

        if memories:
            parts.append("## Memories")
            for m in memories:
                parts.append(f"- [{m.type}, importance={m.importance}] {m.content}")

        if knowledge_entries:
            parts.append("## Knowledge")
            for k in knowledge_entries:
                parts.append(f"- **{k.name}**: {k.content or '(no content)'}")

        if not messages and not memories and not knowledge_entries:
            parts.append("_No data found._")

        md = "\n\n".join(parts)

        if self._export_fn is not None:
            self._export_fn("markdown", md)

        return md

    # ── reflect ──────────────────────────────────────────────────────────

    def reflect(
        self,
        symbiote_id: str,
        session_id: str,
    ) -> dict:
        """Return a summary dict from session messages.

        Will delegate to ReflectionEngine when available.
        """
        messages = self._sessions.get_messages(session_id, limit=50)

        roles: dict[str, int] = {}
        for msg in messages:
            roles[msg.role] = roles.get(msg.role, 0) + 1

        summary = ""
        if messages:
            # messages are DESC from get_messages; reverse to chronological, take last 5
            chronological = list(reversed(messages))
            summary = "\n".join(m.content for m in chronological[-5:])

        return {
            "session_id": session_id,
            "symbiote_id": symbiote_id,
            "message_count": len(messages),
            "role_counts": roles,
            "summary": summary,
        }
