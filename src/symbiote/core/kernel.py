"""SymbioteKernel — the central orchestrator that composes all components."""

from __future__ import annotations

from collections.abc import Callable

from symbiote.adapters.export.markdown import ExportService
from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.capabilities import CapabilitySurface
from symbiote.core.context import ContextAssembler
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.hooks import CompositeHook
from symbiote.core.identity import IdentityManager
from symbiote.core.models import Session, Symbiote
from symbiote.core.ports import LLMPort
from symbiote.core.reflection import ReflectionEngine
from symbiote.core.session import SessionManager
from symbiote.core.session_lock import SessionLock
from symbiote.discovery.loader import DiscoveredToolLoader
from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.process.engine import ProcessEngine
from symbiote.runners.base import RunnerRegistry
from symbiote.runners.chat import ChatRunner
from symbiote.runners.process import ProcessRunner
from symbiote.runners.subagent import SubagentManager
from symbiote.workspace.manager import WorkspaceManager


class SymbioteKernel:
    """Central orchestrator — composes all kernel components and exposes a thin public API."""

    def __init__(self, config: KernelConfig, llm: LLMPort | None = None) -> None:
        self._config = config
        self._llm = llm

        # Storage
        self._storage = SQLiteAdapter(config.db_path, check_same_thread=False)
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

        # Context assembler (with tool gateway + environment for auto tag filtering)
        self._context_assembler = ContextAssembler(
            identity=self._identity,
            memory=self._memory,
            knowledge=self._knowledge,
            context_budget=config.context_budget,
            tool_gateway=self._tool_gateway,
            environment=self._environment,
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
        self._message_repo = MessageRepository(self._storage)
        self._reflection = ReflectionEngine(self._memory, self._message_repo)

        # Subagent spawning
        self._subagent = SubagentManager(self)
        self._subagent.register()

        # Per-session concurrency control
        self._session_lock = SessionLock()

        # Export
        self._export = ExportService(self._storage)

        # Lifecycle hooks
        self._hooks = CompositeHook()

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
    def hooks(self) -> CompositeHook:
        return self._hooks

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

    def find_symbiote_by_name(self, name: str) -> Symbiote | None:
        """Find an active Symbiota by name."""
        row = self._storage.fetch_one(
            "SELECT id FROM symbiotes WHERE name = ? AND status = 'active'",
            (name,),
        )
        if row is None:
            return None
        return self._identity.get(row["id"])

    def start_session(
        self,
        symbiote_id: str,
        goal: str | None = None,
        external_key: str | None = None,
    ) -> Session:
        return self._sessions.start(
            symbiote_id=symbiote_id, goal=goal, external_key=external_key
        )

    def get_or_create_session(
        self,
        symbiote_id: str,
        external_key: str,
        goal: str | None = None,
    ) -> Session:
        """Find or create a session by external key (e.g. user_id:url_key)."""
        return self._sessions.get_or_create_by_external_key(
            symbiote_id=symbiote_id,
            external_key=external_key,
            goal=goal,
        )

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.resume(session_id)

    def message(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
    ) -> str:
        """Add user message, run chat capability, add assistant message, return response.

        Uses per-session locking: concurrent requests on the same session are
        serialized, while different sessions process in parallel.
        """
        with self._session_lock.acquire(session_id):
            return self._message_inner(session_id, content, extra_context)

    def _message_inner(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
    ) -> str:
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise EntityNotFoundError("Session", session_id)
        symbiote_id = row["symbiote_id"]

        self._sessions.add_message(session_id, "user", content)

        response = self._capabilities.chat(
            symbiote_id, session_id, content, extra_context=extra_context
        )

        if isinstance(response, dict):
            text = response.get("text", str(response))
        else:
            text = response

        self._sessions.add_message(session_id, "assistant", text)

        return response

    async def message_async(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        """Async variant of message() — supports async tool handlers and on_token streaming.

        Uses per-session async locking: concurrent requests on the same session
        are serialized, while different sessions process in parallel.
        """
        async with self._session_lock.acquire_async(session_id):
            return await self._message_async_inner(
                session_id, content, extra_context, on_token
            )

    async def _message_async_inner(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise EntityNotFoundError("Session", session_id)
        symbiote_id = row["symbiote_id"]

        self._sessions.add_message(session_id, "user", content)

        response = await self._capabilities.chat_async(
            symbiote_id,
            session_id,
            content,
            extra_context=extra_context,
            on_token=on_token,
        )

        if isinstance(response, dict):
            text = response.get("text", str(response))
        else:
            text = response

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

    def load_discovered_tools(self, symbiote_id: str, base_url: str = "") -> list[str]:
        """Load approved discovered tools into the ToolGateway and authorize them.

        Reads all tools with ``status=approved`` from ``discovered_tools`` for
        *symbiote_id*, registers each HTTP tool in the ToolGateway, and calls
        ``EnvironmentManager.configure()`` so the PolicyGate allows them.

        Returns the list of tool_ids that were registered.  Call this once
        during app startup after the kernel is initialized::

            tool_ids = kernel.load_discovered_tools(clark_id, base_url="http://127.0.0.1:8000")
        """
        loader = DiscoveredToolLoader(
            repository=DiscoveredToolRepository(self._storage),
            gateway=self._tool_gateway,
        )
        tool_ids = loader.load(symbiote_id, base_url=base_url)
        if tool_ids:
            self._environment.configure(symbiote_id=symbiote_id, tools=tool_ids)
        return tool_ids

    def load_mcp_tools(self, registry: object, symbiote_id: str) -> list[str]:
        """Register all tools from a forge_llm ToolRegistry into the ToolGateway.

        The *registry* must be a live ``forge_llm.application.tools.ToolRegistry``
        produced by ``McpToolset.from_stdio()`` or ``McpToolset.from_http()``.
        The caller is responsible for keeping the McpToolset context manager alive
        for as long as the registered tools are in use.

        Also calls ``EnvironmentManager.configure()`` so PolicyGate authorizes the
        tools for *symbiote_id*::

            async with McpToolset.from_http("http://localhost:8000/mcp") as registry:
                tool_ids = kernel.load_mcp_tools(registry, symbiote_id="clark")

        Returns the list of tool_ids that were registered.
        """
        from symbiote.mcp.provider import McpToolProvider

        provider = McpToolProvider()
        tool_ids = provider.load(registry, self._tool_gateway)
        if tool_ids:
            self._environment.configure(symbiote_id=symbiote_id, tools=tool_ids)
        return tool_ids

    def configure_tool_visibility(
        self,
        symbiote_id: str,
        tags: list[str],
        loading: str = "full",
        loop: bool = True,
    ) -> None:
        """Set which tool tags are visible in the LLM prompt for a symbiote.

        All tools remain registered in the ToolGateway and executable;
        tags only control which tools appear in the assembled context.

        Args:
            tags: OpenAPI tags that determine which tools are visible.
            loading: How tools appear in the prompt:
                ``"full"`` — complete schemas (default),
                ``"index"`` — compact catalog + ``get_tool_schema`` meta-tool,
                ``"semantic"`` — cheap LLM pre-filters tags per message.
            loop: When True (default), the ChatRunner feeds tool results
                back to the LLM and re-invokes it until no more tool calls
                are made or the iteration limit is reached.
        """
        self._environment.configure(
            symbiote_id=symbiote_id,
            tool_tags=tags,
            tool_loading=loading,
            tool_loop=loop,
        )

    def set_semantic_llm(self, llm: LLMPort) -> None:
        """Inject a cheap LLM for semantic tool tag resolution.

        The host provides this — the kernel never instantiates an LLM on its own.
        Only used when ``tool_loading="semantic"`` is configured for a symbiote.
        """
        self._context_assembler._semantic_llm = llm

    def shutdown(self) -> None:
        """Close the storage adapter."""
        self._storage.close()
