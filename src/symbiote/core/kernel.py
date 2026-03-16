"""SymbioteKernel — the central orchestrator that composes all components."""

from __future__ import annotations

from symbiote.adapters.export.markdown import ExportService
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.capabilities import CapabilitySurface
from symbiote.core.context import ContextAssembler
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.identity import IdentityManager
from symbiote.core.models import Session, Symbiote
from symbiote.core.ports import LLMPort
from symbiote.core.reflection import ReflectionEngine
from symbiote.core.session import SessionManager
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.process.engine import ProcessEngine
from symbiote.runners.base import RunnerRegistry
from symbiote.runners.chat import ChatRunner
from symbiote.runners.process import ProcessRunner
from symbiote.workspace.manager import WorkspaceManager


class SymbioteKernel:
    """Central orchestrator — composes all kernel components and exposes a thin public API."""

    def __init__(self, config: KernelConfig, llm: LLMPort | None = None) -> None:
        self._config = config
        self._llm = llm

        # Storage
        self._storage = SQLiteAdapter(config.db_path)
        self._storage.init_schema()

        # Managers
        self._identity = IdentityManager(self._storage)
        self._sessions = SessionManager(self._storage)
        self._memory = MemoryStore(self._storage)
        self._knowledge = KnowledgeService(self._storage)
        self._workspace = WorkspaceManager(self._storage)
        self._environment = EnvironmentManager(self._storage)

        # Policies and tools
        self._policy_gate = PolicyGate(self._environment, self._storage)
        self._tool_gateway = ToolGateway(self._policy_gate)

        # Context assembler (with tool gateway)
        self._context_assembler = ContextAssembler(
            identity=self._identity,
            memory=self._memory,
            knowledge=self._knowledge,
            context_budget=config.context_budget,
            tool_gateway=self._tool_gateway,
        )

        # Runners
        self._runner_registry = RunnerRegistry()
        if llm is not None:
            self._runner_registry.register(
                ChatRunner(llm, tool_gateway=self._tool_gateway)
            )

        process_engine = ProcessEngine(self._storage)
        self._runner_registry.register(ProcessRunner(process_engine))

        # Reflection
        self._reflection = ReflectionEngine(self._memory, self._storage)

        # Export
        self._export = ExportService(self._storage)

        # Capability surface
        self._capabilities = CapabilitySurface(
            identity=self._identity,
            sessions=self._sessions,
            memory=self._memory,
            knowledge=self._knowledge,
            context_assembler=self._context_assembler,
            runner_registry=self._runner_registry,
            export_fn=None,
        )

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def capabilities(self) -> CapabilitySurface:
        return self._capabilities

    @property
    def tool_gateway(self) -> ToolGateway:
        return self._tool_gateway

    @property
    def environment(self) -> EnvironmentManager:
        return self._environment

    # ── Public API — thin delegation ──────────────────────────────────────

    def create_symbiote(
        self, name: str, role: str, persona: dict | None = None
    ) -> Symbiote:
        return self._identity.create(name=name, role=role, persona=persona)

    def get_symbiote(self, symbiote_id: str) -> Symbiote | None:
        return self._identity.get(symbiote_id)

    def start_session(self, symbiote_id: str, goal: str | None = None) -> Session:
        return self._sessions.start(symbiote_id=symbiote_id, goal=goal)

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.resume(session_id)

    def message(self, session_id: str, content: str) -> str:
        """Add user message, run chat capability, add assistant message, return response."""
        # Look up symbiote_id from the session
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise EntityNotFoundError("Session", session_id)
        symbiote_id = row["symbiote_id"]

        # Add user message
        self._sessions.add_message(session_id, "user", content)

        # Chat via capabilities
        response = self._capabilities.chat(symbiote_id, session_id, content)

        # Normalize response to string for message storage
        if isinstance(response, dict):
            text = response.get("text", str(response))
        else:
            text = response

        # Add assistant message
        self._sessions.add_message(session_id, "assistant", text)

        return response

    def close_session(self, session_id: str) -> Session:
        """Run reflection on the session, then close it."""
        # Get symbiote_id for reflection
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is not None:
            self._reflection.reflect_session(session_id, row["symbiote_id"])

        return self._sessions.close(session_id)

    def shutdown(self) -> None:
        """Close the storage adapter."""
        self._storage.close()
