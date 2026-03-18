"""CapabilitySurface — the six capabilities as explicit methods."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from symbiote.core.context import ContextAssembler
from symbiote.core.exceptions import SymbioteError
from symbiote.core.identity import IdentityManager
from symbiote.core.models import MemoryEntry
from symbiote.core.ports import KnowledgePort, MemoryPort
from symbiote.core.session import SessionManager
from symbiote.runners.base import RunnerRegistry


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
    ) -> None:
        self._identity = identity
        self._sessions = sessions
        self._memory = memory
        self._knowledge = knowledge
        self._context_assembler = context_assembler
        self._runner_registry = runner_registry
        self._export_fn = export_fn

    # ── learn ────────────────────────────────────────────────────────────

    def learn(
        self,
        symbiote_id: str,
        session_id: str,
        content: str,
        fact_type: str = "factual",
        importance: float = 0.7,
    ) -> MemoryEntry:
        """Persist a durable fact as long-term memory."""
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
        memories = self._memory.get_relevant(query, session_id)

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
    ) -> str:
        """Build context, select ChatRunner, run, return response text."""
        runner = self._runner_registry.select("chat")
        if runner is None:
            raise CapabilityError("chat", "No runner available for intent 'chat'")

        context = self._context_assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input=content,
            extra_context=extra_context,
        )

        result = runner.run(context)
        if not result.success:
            raise CapabilityError("chat", result.error or "Chat runner failed")

        return result.output

    async def chat_async(
        self,
        symbiote_id: str,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> Any:
        """Async variant of chat() — uses run_async() so tool handlers can be coroutines.

        Args:
            on_token: Optional callback invoked with each token as it is generated.
                Requires the LLM adapter to expose a ``stream()`` method; otherwise
                called once with the full response text.
        """
        runner = self._runner_registry.select("chat")
        if runner is None:
            raise CapabilityError("chat", "No runner available for intent 'chat'")

        context = self._context_assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input=content,
            extra_context=extra_context,
        )

        if not hasattr(runner, "run_async"):
            raise CapabilityError("chat", "Runner does not support async execution")

        result = await runner.run_async(context, on_token=on_token)
        if not result.success:
            raise CapabilityError("chat", result.error or "Chat runner failed")

        return result.output

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
        memories = self._memory.get_relevant(query, session_id)
        knowledge_entries = self._knowledge.query(symbiote_id, query)
        messages = self._sessions.get_messages(session_id, limit=20)

        parts: list[str] = []
        parts.append(f"# Show: {query}")

        if messages:
            parts.append("## Session Messages")
            for msg in reversed(messages):  # chronological order
                parts.append(f"- **{msg.role}**: {msg.content}")

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
