"""SymbioteKernel — the central orchestrator that composes all components."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from symbiote.adapters.export.markdown import ExportService
from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.agent_protocols import (
    AgentProtocol,
    AgentProtocolRegistry,
    create_default_protocol_registry,
)
from symbiote.config.models import KernelConfig
from symbiote.core.background_review import BackgroundReviewEngine
from symbiote.core.capabilities import CapabilityError, CapabilitySurface
from symbiote.core.context import ContextAssembler
from symbiote.core.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    SessionBusyError,
    SessionLockLostError,
    TurnCancelledError,
    ValidationError,
)
from symbiote.core.hooks import CompositeHook
from symbiote.core.identity import IdentityManager
from symbiote.core.inspector import KernelInspector
from symbiote.core.models import Session, Symbiote
from symbiote.core.ports import (
    LLMPort,
    SessionLockPort,
    SessionRecallPort,
    SkillsStoragePort,
    StoragePort,
)
from symbiote.core.redaction import RedactingStorage, Redactor
from symbiote.core.reflection import ReflectionEngine
from symbiote.core.scoring import compute_auto_score, compute_final_score
from symbiote.core.session import SessionManager
from symbiote.core.session_lock import (
    InProcessSessionLock,
    SessionLease,
    renew_interval,
)
from symbiote.core.subjects import (
    learns_from_subjects,
    subject_kwargs,
    subject_of_session,
    validate_subject_key,
)
from symbiote.core.turn_control import (
    CANCELLED,
    TurnControl,
    TurnStop,
    bind_turn_control,
    reset_turn_control,
)
from symbiote.core.turn_record import TurnRecord, close_turn_record, open_turn_record
from symbiote.core.turn_result import TurnOutcome, TurnReply, turn_outcome
from symbiote.discovery.loader import DiscoveredToolLoader
from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.dream.engine import DreamEngine
from symbiote.dream.models import DreamReport
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor
from symbiote.environment.exec_context import (
    bind_host_context,
    current_tool_context,
    reset_host_context,
)
from symbiote.environment.http_tool_store import HttpToolRepository
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway, ToolGuard
from symbiote.harness.versions import HarnessVersionRepository
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.process.engine import ProcessEngine
from symbiote.runners.base import (
    BlockResult,
    LongRunPlan,
    LoopTrace,
    RunnerRegistry,
    RunResult,
)
from symbiote.runners.chat import ChatRunner
from symbiote.runners.long_run import LongRunRunner
from symbiote.runners.process import ProcessRunner
from symbiote.runners.subagent import SubagentManager
from symbiote.skills import tool as skill_tool_module
from symbiote.skills import usage as skill_usage
from symbiote.skills.loader import SkillsLoader
from symbiote.skills.scope import (
    SkillScopeManager,
)
from symbiote.skills.scope import (
    reset_active_symbiote as _reset_active_symbiote,
)
from symbiote.skills.scope import (
    set_active_symbiote as _set_active_symbiote,
)
from symbiote.skills.shared import GLOBAL_SCOPE as GLOBAL_SKILL_SCOPE
from symbiote.skills.shared import SkillsSync
from symbiote.skills.store import SkillsStore
from symbiote.workspace.manager import WorkspaceManager


def _response_text(response) -> str:  # noqa: ANN001
    return response.get("text", str(response)) if isinstance(response, dict) else response


def _warn_if_results_exceed_loop_budget(config: KernelConfig) -> None:
    """A single tool result larger than about half the loop budget is cut by
    autocompact (and its tail lost). Tell the host which knob to turn."""
    # Autocompact triggers at 80% of loop_context_budget tokens (~4 chars each).
    loop_chars = int(config.loop_context_budget * 0.8) * 4
    if config.tool_result_max_chars > loop_chars // 2:
        logging.getLogger(__name__).warning(
            "tool_result_max_chars=%d is large for loop_context_budget=%d tokens; "
            "results that size will be truncated to fit the loop. Raise "
            "loop_context_budget together with tool_result_max_chars.",
            config.tool_result_max_chars,
            config.loop_context_budget,
        )


class SymbioteKernel:
    """Central orchestrator — composes all kernel components and exposes a thin public API."""

    def __init__(
        self,
        config: KernelConfig,
        llm: LLMPort | None = None,
        *,
        storage: StoragePort | None = None,
        session_lock: SessionLockPort | None = None,
        skills_storage: SkillsStoragePort | None = None,
        agent_protocol: str | AgentProtocol | None = None,
        protocol_registry: AgentProtocolRegistry | None = None,
    ) -> None:
        """Compose the kernel.

        ``storage`` injects the storage adapter; by default the kernel opens
        the SQLite file at ``config.db_path``. An injected adapter belongs to
        the host: the kernel calls its ``init_schema()`` once and never closes
        it. ``SQLiteAdapter`` and ``PostgresAdapter`` are maintained with the
        kernel; any other must satisfy ``StoragePort``.

        ``session_lock`` injects the lock that lets one turn at a time run on
        a session (``SessionLockPort``). The default serializes the turns of
        this process only; with several replicas pass
        ``StorageSessionLock(storage)`` or the host's own lock.

        ``skills_storage`` makes the writable skill library shared
        (``SkillsStoragePort``): every change is pushed to it and each turn
        starts by bringing the local skill directory up to date, so replicas
        without a common disk see each other's skills. Without it the
        library is the local directory. ``StorageSkillsLibrary(storage)``
        keeps it in the kernel's database.
        """
        active_protocol_registry = (
            protocol_registry or create_default_protocol_registry()
        )
        selected_protocol = (
            config.agent_protocol
            if agent_protocol is None
            else agent_protocol
        )
        # Validate the exact configured ID before storage or request-serving
        # components are initialized. A non-None constructor override wins.
        resolved_agent_protocol = active_protocol_registry.resolve(
            selected_protocol
        )

        self._config = config
        self._llm = llm

        # Storage: the host's adapter, or the default SQLite file.
        self._owns_storage = storage is None
        inner_storage: StoragePort = (
            storage
            if storage is not None
            else SQLiteAdapter(config.db_path, check_same_thread=False)
        )
        inner_storage.init_schema()
        # Every component writes through this wrapper, which removes the
        # secrets the redactor knows from the conversation tables.
        self._redactor = Redactor()
        self._storage: RedactingStorage = RedactingStorage(inner_storage, self._redactor)

        # Managers
        self._identity = IdentityManager(self._storage)
        self._sessions = SessionManager(self._storage)
        self._memory = MemoryStore(self._storage)
        self._knowledge = KnowledgeService(self._storage)
        self._workspace = WorkspaceManager(self._storage)
        self._environment = EnvironmentManager(self._storage)
        # Per-symbiote switch for long-term memory (``memory_enabled``),
        # enforced where every memory write and read goes through.
        self._memory.set_enabled_check(self._environment.get_memory_enabled)

        # Policies and tools
        self._policy_gate = PolicyGate(self._environment, self._storage)
        self._tool_gateway = ToolGateway(
            self._policy_gate, fs_allowed_root=config.fs_allowed_root
        )

        # Register memory/knowledge on-demand tools (always available,
        # authorized per-symbiote only when context_mode == "on_demand")
        self._tool_gateway.register_memory_tools(self._memory, self._knowledge)

        # HTTP tools registered through the API / CLI (persisted).
        self._http_tools = HttpToolRepository(self._storage)
        for _owner, descriptor, http_config in self._http_tools.list_all():
            if not self._tool_gateway.has_tool(descriptor.tool_id):
                self._tool_gateway.register_http_tool(descriptor, http_config)

        # Harness versioning
        self._harness_versions = HarnessVersionRepository(self._storage)

        # Context assembler (with tool gateway + environment for auto tag filtering)
        self._context_assembler = ContextAssembler(
            identity=self._identity,
            memory=self._memory,
            knowledge=self._knowledge,
            context_budget=config.context_budget,
            tool_gateway=self._tool_gateway,
            environment=self._environment,
            harness_versions=self._harness_versions,
            standing_memory_limit=config.standing_memory_limit,
        )

        # Runners
        self._runner_registry = RunnerRegistry()
        _warn_if_results_exceed_loop_budget(config)
        if llm is not None:
            self._runner_registry.register(
                ChatRunner(
                    llm,
                    tool_gateway=self._tool_gateway,
                    tool_result_max_chars=config.tool_result_max_chars,
                    context_budget=config.loop_context_budget,
                    # Resolver form: ChatRunner is per-kernel, but
                    # skill_review_enabled is per-symbiote. The resolver
                    # returns the engine (or None) for the symbiote driving
                    # the current turn.
                    background_review=self._background_review_for,
                    agent_protocol=resolved_agent_protocol,
                    protocol_registry=active_protocol_registry,
                    runtime_context=config.runtime_context,
                )
            )

        if llm is not None:
            # Selected by CapabilitySurface when tool_mode == "long_run".
            self._runner_registry.register(
                LongRunRunner(
                    llm,
                    tool_gateway=self._tool_gateway,
                    context_budget=config.loop_context_budget,
                    tool_result_max_chars=config.tool_result_max_chars,
                    runtime_context=config.runtime_context,
                    agent_protocol=resolved_agent_protocol,
                    protocol_registry=active_protocol_registry,
                    background_review=self._background_review_for,
                )
            )

        process_engine = ProcessEngine(self._storage)
        self._runner_registry.register(ProcessRunner(process_engine))

        # Reflection — engine is rebuilt per close_session so it can pick up
        # the symbiote's reflection_mode + the right LLM (evolver vs main).
        # The default engine (keyword, no LLM) is the safe baseline used when
        # callers invoke reflect directly or no per-symbiote config exists.
        self._message_repo = MessageRepository(self._storage)
        # ``storage`` lets the engine store facts and watermark in one transaction.
        self._reflection = ReflectionEngine(
            self._memory, self._message_repo, storage=self._storage
        )

        # Subagent spawning
        self._subagent = SubagentManager(self)
        self._subagent.register()

        # Per-session concurrency control
        self._session_lock: SessionLockPort = (
            session_lock if session_lock is not None else InProcessSessionLock()
        )
        # The turn running on each session of this process (cancel_turn).
        self._turns: dict[str, TurnControl] = {}
        self._turns_lock = threading.Lock()

        # Export
        self._export = ExportService(self._storage)

        # Lifecycle hooks: tool events fire in the ToolGateway, turn events
        # in message() / message_async().
        self._hooks = CompositeHook()
        self._tool_gateway.hooks = self._hooks
        self._tool_gateway.secrets_hook = self._learn_secret_params
        self._policy_gate.params_filter = self._mask_secret_params

        # Optional session recall (host provides implementation)
        self._session_recall: SessionRecallPort | None = None

        # Long-run handoff per session — set by message(), persisted by
        # close_session(). Loop traces are not kept here: message() persists
        # each one to execution_traces and close_session() reads them back.
        self._handoffs: dict[str, dict] = {}
        self._shutdown_callbacks: list[Callable[[], None]] = []  # see on_shutdown
        self._handoffs_lock = threading.Lock()

        # Optional evolver LLM (host provides, can be different from main LLM)
        self._evolver_llm: LLMPort | None = None

        # Back-reference so EnvironmentManager.configure() can validate
        # reflection_mode='llm' against the kernel's _evolver_llm presence.
        self._environment._kernel = self

        # Dream engine (lazy — created on first use)
        # Background dream threads, one per symbiote at most.
        self._dream_threads: dict[str, threading.Thread] = {}
        self._dream_lock = threading.Lock()

        # Skill self-improvement wiring (Sprint 4)
        # Lazily resolved when first needed so a kernel with no skills/ root
        # on disk still works (you_news pattern: host owns the layout).
        self._skills_store: SkillsStore | None = None
        self._skills_loader: SkillsLoader | None = None
        # Per-symbiote scope (Option A). Non-None only when
        # config.skill_scope == "per_symbiote"; then _skills_store /
        # _skills_loader stay None and all access goes through this manager.
        self._skill_scope_mgr: SkillScopeManager | None = None
        # In per_symbiote mode the review engine is cached per-symbiote (each
        # symbiote sees only its own store/loader). In global mode the single
        # _background_review is reused.
        # Bounded: least recently used engines are dropped once they have no
        # thread running (``KernelConfig.symbiote_cache_size``).
        self._background_review_by_symbiote: OrderedDict[
            str, BackgroundReviewEngine
        ] = OrderedDict()
        self._background_review: BackgroundReviewEngine | None = None
        # Guards the lazy construction of _background_review against the
        # race where ChatRunner (mid-session nudge) and close_session
        # both hit `_background_review_for` simultaneously for the same
        # symbiote — without the lock, both threads see None and build
        # two engines, leaking one. Acquired only on the cold path.
        self._background_review_lock = threading.Lock()
        # Shared skill library (None: the local directory is the library).
        self._skills_storage = skills_storage
        self._skills_sync: SkillsSync | None = None
        if skills_storage is not None:
            self._skills_sync = SkillsSync(
                skills_storage,
                redact=self._redactor.redact_text,
                writable=config.skill_writes,
            )
            skill_usage.add_write_listener(self._skills_sync.on_sidecar_written)
        self._setup_skills_wiring()
        if self._skills_sync is not None:
            try:
                self.sweep_skill_cache()
            except Exception:
                logging.getLogger(__name__).warning(
                    "could not sweep the local skill cache", exc_info=True
                )

        # Capability surface
        self._capabilities = CapabilitySurface(
            identity=self._identity,
            sessions=self._sessions,
            memory=self._memory,
            knowledge=self._knowledge,
            context_assembler=self._context_assembler,
            runner_registry=self._runner_registry,
            export_fn=None,
            hooks=self._hooks,
        )

        # Read-only views over the stored state (sessions, traces, scores...)
        self._inspector = KernelInspector(self._storage)

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
    def inspector(self) -> KernelInspector:
        """Read-only views over sessions, messages, memories, scores, traces,
        reflections, dream reports, skill reviews and the tool audit log."""
        return self._inspector

    @property
    def memory(self) -> MemoryStore:
        """Long-term memory store (``learn`` writes here; recall reads it)."""
        return self._memory

    @property
    def knowledge(self) -> KnowledgeService:
        """Knowledge entries injected into the context (or searched on demand)."""
        return self._knowledge

    @property
    def workspaces(self) -> WorkspaceManager:
        return self._workspace

    @property
    def context_assembler(self) -> ContextAssembler:
        """Builds each turn's context; ``inspect(context)`` reports its budget."""
        return self._context_assembler

    @property
    def session_recall(self) -> SessionRecallPort | None:
        return self._session_recall

    def set_session_recall(self, recall: SessionRecallPort) -> None:
        """Inject a host-provided session recall implementation."""
        self._session_recall = recall

    @property
    def harness_versions(self) -> HarnessVersionRepository:
        return self._harness_versions

    def set_evolver_llm(self, llm: LLMPort) -> None:
        """Inject an LLM for harness evolution (can differ from main LLM).

        If not set, the evolver uses the kernel's main LLM as fallback.
        Using a different model avoids blind spots (the proposer has different
        strengths/weaknesses than the model being optimized for).
        """
        self._evolver_llm = llm

    def set_approval_callback(
        self,
        callback: Callable[..., bool] | None,
        *,
        pass_context: bool = False,
        risk_threshold: str = "high",
    ) -> None:
        """Install a human-in-the-loop approval gate on the chat runner.

        The kernel builds its ChatRunner internally, so this is the supported
        way for an embedded host to wire an approval callback that ``message()``
        / ``message_async()`` actually use. Without it, the gate is inert and
        high-risk tools execute unchecked.

        The callback receives ``(tool_id, params, risk_level)`` and returns
        ``True`` to allow or ``False`` to deny. By default it is invoked for
        high-risk tools; ``risk_threshold`` may be ``"medium"`` or ``"low"``
        to ask for those risks too. Tools whose descriptor sets
        ``requires_approval`` are always asked; those
        last are refused when no callback is installed.
        With ``pass_context=True`` it receives a fourth argument, the
        ``ToolExecContext`` of the call (symbiote, session and the turn's
        ``host_context``); either way ``current_tool_context()`` returns it
        while the callback runs. The callback is consulted only after policy
        and the tool guard (``set_tool_guard``) have accepted the call.
        Pass ``None`` to remove a previously installed gate. The gate also
        applies to the blocks of ``tool_mode="long_run"``. In
        ``message_async`` the callback runs in a worker thread, so it may
        block while waiting for a human answer.

        Raises:
            RuntimeError: if no chat runner is registered (kernel built
                without an LLM).
        """
        runner = self._runner_registry.select("chat")
        if not isinstance(runner, ChatRunner):
            raise RuntimeError(
                "No chat runner available to attach an approval callback "
                "(kernel was constructed without an LLM)."
            )
        gate = callback
        if callback is not None and pass_context:

            def gate(tool_id: str, params: dict, risk_level: str) -> bool:
                return callback(tool_id, params, risk_level, current_tool_context())

        runner.set_approval_callback(gate, risk_threshold=risk_threshold)
        long_run = self._runner_registry.get("long_run")
        if isinstance(long_run, LongRunRunner):
            long_run.set_approval_callback(gate, risk_threshold=risk_threshold)

    @property
    def redactor(self) -> Redactor:
        """Removes secrets from what the kernel stores (see ``set_redactor``).

        ``kernel.redactor.add_secret(value)`` registers a literal directly.
        """
        return self._redactor

    def set_redactor(
        self,
        fn: Callable[[str], str] | None,
        *,
        replacement: str | None = None,
    ) -> None:
        """Install the host's redaction function (``None`` removes it).

        ``fn(text) -> text`` is given every piece of conversation-derived text
        before it is stored (messages, session summaries, memories, audit log,
        execution traces, reflection / skill-review / dream records, skill
        files) and returns it without the secrets it recognizes. Inside JSON
        columns it is called once per string value, so the stored JSON stays
        valid. If it raises, the text is replaced by a marker instead of
        being stored unchecked.

        Independently of ``fn``, the values of the parameters a tool declares
        in ``ToolDescriptor.secret_params`` are masked in the records of the
        call and removed from everything stored afterwards, and from what the
        same session had already stored.

        Redaction applies to storage only: the model sees the real value in
        the turn that uses it and the redacted history in later turns.
        Identifiers, timestamps and host configuration (HTTP tool configs,
        personas, knowledge, environment configs) are never rewritten.
        """
        self._redactor.set_function(fn)
        if replacement is not None:
            self._redactor.replacement = replacement

    def _learn_secret_params(
        self, session_id: str | None, secret_paths: list[str], params: dict
    ) -> None:
        new = self._redactor.learn(params, secret_paths)
        if new and session_id is not None:
            self._storage.scrub_session(session_id, new, self._redactor.replacement)

    def _mask_secret_params(self, tool_id: str, params: dict) -> dict:
        descriptor = self._tool_gateway.get_descriptor(tool_id)
        if descriptor is None or not descriptor.secret_params:
            return params
        return self._redactor.mask_params(params, descriptor.secret_params)

    def set_tool_guard(self, guard: ToolGuard | None) -> None:
        """Install (or remove, with ``None``) the host's veto over tool calls.

        ``guard(tool_id, params, context)`` returns ``ToolDecision.allow()``
        or ``ToolDecision.deny(reason)`` for every tool call that policy
        allows, whatever its risk level, before the approval callback and
        before the handler. See ``ToolGateway.set_tool_guard``.
        """
        self._tool_gateway.set_tool_guard(guard)

    def set_progress_callbacks(
        self,
        *,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_stream: Callable[[str, int], None] | None = None,
        on_plan: Callable[[LongRunPlan], None] | None = None,
        on_block_complete: Callable[[BlockResult], None] | None = None,
    ) -> None:
        """Install mid-turn callbacks on the internal runners.

        Chat turns: ``on_progress(event, iteration, max_iterations)`` with
        ``iteration_start`` / ``tool_start`` / ``tool_end`` /
        ``iteration_end``, and ``on_stream(text, iteration)`` with the parsed
        text of every model reply. Long-run turns: ``on_progress(event,
        index, total)`` with ``plan_complete`` / ``block_start`` /
        ``block_end``, ``on_plan(plan)`` and ``on_block_complete(result)``.

        Each call replaces all four; omitted ones are cleared. The callbacks
        are shared by every session of this kernel (they receive no session
        id), and in ``message_async`` they may run on a worker thread.

        Raises:
            RuntimeError: if the kernel was constructed without an LLM.
        """
        runner = self._runner_registry.select("chat")
        if not isinstance(runner, ChatRunner):
            raise RuntimeError(
                "No chat runner available to set progress callbacks "
                "(kernel was constructed without an LLM)."
            )
        runner.set_progress_callbacks(on_progress=on_progress, on_stream=on_stream)
        long_run = self._runner_registry.get("long_run")
        if isinstance(long_run, LongRunRunner):
            long_run.set_progress_callbacks(
                on_progress=on_progress,
                on_block_complete=on_block_complete,
                on_plan=on_plan,
            )

    def set_native_tools(self, enabled: bool) -> None:
        """Enable/disable native function-calling on the internal chat runner.

        The kernel builds its ChatRunner with ``native_tools=False`` (text-based
        tool-call parsing) for broad LLM-adapter compatibility. Hosts whose LLM
        adapter supports native function calling should call this with ``True``
        to use the provider's structured tool_calls instead of text parsing —
        the supported way to flip it without poking the runner's private attr.

        Raises:
            RuntimeError: if no chat runner is registered (kernel built
                without an LLM).
        """
        runner = self._runner_registry.select("chat")
        if not isinstance(runner, ChatRunner):
            raise RuntimeError(
                "No chat runner available to set native_tools "
                "(kernel was constructed without an LLM)."
            )
        runner.set_native_tools(enabled)
        long_run = self._runner_registry.get("long_run")
        if isinstance(long_run, LongRunRunner):
            long_run.set_native_tools(enabled)

    def set_token_streaming(self, mode: str) -> None:
        """How the text of a turn reaches ``on_token``: ``"final"`` or ``"live"``.

        ``"final"`` (default) releases only the validated answer, once its
        model call has ended. ``"live"`` streams the text as the model writes
        it, in every round of the tool loop, with tool-call markup held back;
        the narration before a tool call is shown, rounds are separated by a
        blank line, and the turn's return value stays the authoritative
        answer. Token by token needs an adapter with ``stream()``; with
        native tools, ``ForgeLLMAdapter(stream_tool_calls=True)``. See
        ``symbiote.runners.token_stream``.

        Raises:
            ValueError: for an unknown mode.
            RuntimeError: if no chat runner is registered.
        """
        runner = self._runner_registry.select("chat")
        if not isinstance(runner, ChatRunner):
            raise RuntimeError(
                "No chat runner available to set token_streaming "
                "(kernel was constructed without an LLM)."
            )
        runner.set_token_streaming(mode)
        long_run = self._runner_registry.get("long_run")
        if isinstance(long_run, LongRunRunner):
            long_run.set_token_streaming(mode)

    def set_agent_protocol(
        self,
        protocol: str | AgentProtocol | None,
    ) -> None:
        """Select the exact model-facing protocol on the chat and long-run runners.

        ``None`` restores the registry's backward-compatible legacy default.
        Model names are never inspected or guessed.
        Prefer constructor injection. This setter mutates the shared runner
        and is intended for setup before serving traffic; it is not
        thread-safe with concurrent active runs.

        Raises:
            RuntimeError: if the kernel was constructed without an LLM.
        """
        runner = self._runner_registry.select("chat")
        if not isinstance(runner, ChatRunner):
            raise RuntimeError(
                "No chat runner available to set agent_protocol "
                "(kernel was constructed without an LLM)."
            )
        runner.set_agent_protocol(protocol)
        long_run = self._runner_registry.get("long_run")
        if isinstance(long_run, LongRunRunner):
            long_run.set_agent_protocol(protocol)

    @property
    def environment(self) -> EnvironmentManager:
        return self._environment

    @property
    def skills_loader(self) -> SkillsLoader | None:
        """Read side of the GLOBAL skill library (list/inspect skills).

        ``None`` when skills wiring is disabled OR when ``skill_scope ==
        'per_symbiote'`` — in that mode there is no single library; use
        ``skills_loader_for(symbiote_id)`` instead.
        """
        return self._skills_loader

    @property
    def skills_store(self) -> SkillsStore | None:
        """Write side of the GLOBAL skill library (edit/patch/delete skills).

        ``None`` when skills wiring is disabled OR when ``skill_scope ==
        'per_symbiote'`` — use ``skills_store_for(symbiote_id)`` instead.
        """
        return self._skills_store

    def skills_loader_for(self, symbiote_id: str) -> SkillsLoader | None:
        """Read side of a SPECIFIC symbiote's skill library.

        In ``per_symbiote`` scope, returns that symbiote's own loader (built
        lazily). In ``global`` scope, returns the single shared loader (the
        ``symbiote_id`` is ignored) so hosts can use one call path regardless
        of scope. ``None`` when skills wiring is disabled.
        """
        if self._skill_scope_mgr is not None:
            return self._skill_scope_mgr.loader_for(symbiote_id)
        return self._skills_loader

    def skills_store_for(self, symbiote_id: str) -> SkillsStore | None:
        """Write side of a SPECIFIC symbiote's skill library.

        In ``per_symbiote`` scope, returns that symbiote's own store. In
        ``global`` scope, returns the single shared store (``symbiote_id``
        ignored). ``None`` when skills wiring is disabled.
        """
        if self._skill_scope_mgr is not None:
            return self._skill_scope_mgr.store_for(symbiote_id)
        return self._skills_store

    # ── Public API — thin delegation ──────────────────────────────────────

    def create_symbiote(
        self,
        name: str,
        role: str,
        persona: dict | None = None,
        owner_id: str | None = None,
    ) -> Symbiote:
        """Create a symbiote. ``owner_id`` is the tenant that owns it in the
        HTTP API; symbiotes without an owner are reachable by admin keys only."""
        return self._identity.create(
            name=name, role=role, persona=persona, owner_id=owner_id
        )

    def get_symbiote(self, symbiote_id: str) -> Symbiote | None:
        return self._identity.get(symbiote_id)

    def update_symbiote(
        self,
        symbiote_id: str,
        *,
        name: str | None = None,
        role: str | None = None,
        persona: dict | None = None,
    ) -> Symbiote:
        """Change name, role and/or persona (persona changes are audited in
        ``persona_audit``). Raises ``EntityNotFoundError`` if absent."""
        return self._identity.update(symbiote_id, name=name, role=role, persona=persona)

    def list_symbiotes(self) -> list[Symbiote]:
        """Every symbiote that has not been deleted."""
        return self._identity.list_all()

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
        subject_key: str | None = None,
    ) -> Session:
        """Start a session. Raises EntityNotFoundError for an unknown symbiote
        (instead of a bare FOREIGN KEY IntegrityError).

        ``subject_key`` names the end user on the other side when the
        symbiote talks to several people: an opaque string the host chooses
        (a user id, a visitor cookie), compared only for equality. The
        session then reads the symbiote's shared memories plus that user's
        own, and everything it learns is stored for that user alone; no
        other end user sees it. It cannot be changed after the session
        starts. Without it (the default) the session reads and writes the
        shared memory, as before. Whether such sessions also feed the
        harness and the skills is
        ``EnvironmentConfig.learn_from_subject_sessions``. Raises
        ``ValidationError`` for an empty or too long key.
        """
        if self._identity.get(symbiote_id) is None:
            raise EntityNotFoundError("Symbiote", symbiote_id)
        return self._sessions.start(
            symbiote_id=symbiote_id,
            goal=goal,
            external_key=external_key,
            subject_key=subject_key,
        )

    def get_or_create_session(
        self,
        symbiote_id: str,
        external_key: str,
        goal: str | None = None,
        subject_key: str | None = None,
    ) -> Session:
        """Find or create a session by external key (e.g. user_id:url_key).

        ``subject_key`` (see ``start_session``) is given to a session created
        here. A session that already exists keeps its own; asking for it with
        a different ``subject_key`` raises ``ValidationError``.
        """
        return self._sessions.get_or_create_by_external_key(
            symbiote_id=symbiote_id,
            external_key=external_key,
            goal=goal,
            subject_key=subject_key,
        )

    def get_session(self, session_id: str) -> Session | None:
        """Read a session. Does not reopen a closed one (use
        get_or_create_session / SessionManager.resume for that)."""
        return self._sessions.get(session_id)

    def message(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        llm_config: dict | None = None,
        *,
        host_context: Mapping[str, Any] | None = None,
    ) -> str | TurnReply:
        """Add user message, run chat capability, add assistant message, return response.

        Returns the reply text, or a :class:`TurnReply` dict (``text`` and
        ``tool_results``) when the turn called tools. ``turn()`` takes the
        same arguments and always returns a :class:`TurnOutcome`, which also
        says why the tool loop stopped.

        Uses per-session locking: concurrent requests on the same session are
        serialized, while different sessions process in parallel.

        Args:
            llm_config: Optional dict of per-call LLM overrides (e.g.
                ``{"mode": "high"}``). Propagated all the way to the
                runner's ``self._llm.stream(messages, config=...)`` call
                so a single message can use a different effort / model /
                temperature than the session's default — without
                affecting other sessions that share the same adapter.
            host_context: Opaque host data for this turn only (the caller's
                identity, a credential, a request id). Never rendered into
                the prompt, persisted or logged, unlike ``extra_context``.
                Tool handlers, the approval callback and the tool guard get
                it as ``ToolExecContext.host``; anything running in the turn
                (the LLM adapter, hooks) reads it with
                ``current_host_context()``. Concurrent turns never see each
                other's.
        """
        return self._message_locked(
            session_id, content, extra_context, llm_config, host_context
        )[0]

    def turn(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        llm_config: dict | None = None,
        *,
        host_context: Mapping[str, Any] | None = None,
    ) -> TurnOutcome:
        """Run a turn exactly like ``message()`` and return a :class:`TurnOutcome`.

        Besides the reply text and the tool results, it says why the tool
        loop stopped (``stop_reason``, one of
        ``symbiote.core.turn_result.STOP_REASONS``; ``stopped_early`` is True
        when a limit ended it rather than the model) and how many iterations
        and tool calls it took. Raises what ``message()`` raises.
        """
        return turn_outcome(
            *self._message_locked(
                session_id, content, extra_context, llm_config, host_context
            )
        )

    def _message_locked(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None,
        llm_config: dict | None,
        host_context: Mapping[str, Any] | None,
    ) -> tuple[str | TurnReply, LoopTrace | None]:
        """One turn under the session lock: the reply and its loop trace."""
        host_token = bind_host_context(host_context)
        try:
            ttl, wait = self._lock_ttl(), self._config.session_lock_wait
            token = self._session_lock.acquire(session_id, wait=wait, ttl=ttl)
            if token is None:
                raise SessionBusyError(session_id, wait)
            lease, control_token = self._begin_turn(session_id, token, ttl)
            try:
                return self._message_inner(
                    session_id, content, extra_context, llm_config=llm_config
                )
            except TurnStop as stop:
                raise self._stop_error(session_id, stop) from None
            finally:
                self._end_turn(session_id, lease, control_token)
                try:
                    self._session_lock.release(session_id, token)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "could not release the lock of session %s", session_id
                    )
        finally:
            reset_host_context(host_token)

    def _lock_ttl(self) -> float | None:
        """Validity of a held session lock: the configured one, or the lock
        implementation's default."""
        if self._config.session_lock_ttl is not None:
            return self._config.session_lock_ttl
        return getattr(self._session_lock, "default_ttl", None)

    def _begin_turn(
        self, session_id: str, token: str, ttl: float | None
    ) -> tuple[SessionLease, contextvars.Token]:
        """Bind the turn's control and start renewing its session lock."""
        control = TurnControl()
        lease = SessionLease(
            self._session_lock,
            session_id,
            token,
            ttl=ttl,
            interval=renew_interval(ttl, self._config.session_lock_renew_interval),
            on_lost=control.lose_lock,
            on_cancel=control.cancel,
            cancel_poll=self._config.turn_cancel_poll_interval,
        )
        control_token = bind_turn_control(control)
        with self._turns_lock:
            self._turns[session_id] = control
        lease.start()
        return lease, control_token

    def _end_turn(
        self, session_id: str, lease: SessionLease, control_token: contextvars.Token
    ) -> None:
        with self._turns_lock:
            self._turns.pop(session_id, None)
        reset_turn_control(control_token)
        lease.stop()

    @staticmethod
    def _stop_error(session_id: str, stop: TurnStop) -> Exception:
        """The exception the host gets for a turn that was stopped."""
        if stop.reason == CANCELLED:
            return TurnCancelledError(session_id)
        return SessionLockLostError(session_id)

    def cancel_turn(self, session_id: str) -> bool:
        """Ask the turn running on ``session_id`` to stop.

        From this moment the turn starts no new model call and no new tool
        call; the call that is running finishes. ``message()`` /
        ``message_async()`` then raise ``TurnCancelledError``. The user
        message stays in the session and the trace of what the turn did is
        stored with ``stop_reason="cancelled"``; no assistant message is
        stored.

        Returns True when the request reached a running turn. A turn of
        another replica is reached through the session lock when it relays
        cancellations (``StorageSessionLock`` does, within
        ``KernelConfig.turn_cancel_poll_interval``); otherwise call this on
        the replica that runs the turn. False: no turn is running there.
        """
        with self._turns_lock:
            control = self._turns.get(session_id)
        if control is not None:
            control.cancel()
            return True
        relay = getattr(self._session_lock, "request_cancel", None)
        return bool(relay(session_id)) if relay is not None else False

    def _message_inner(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        llm_config: dict | None = None,
    ) -> tuple[str | TurnReply, LoopTrace | None]:
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise EntityNotFoundError("Session", session_id)
        symbiote_id = row["symbiote_id"]

        # Bind the active symbiote so per-symbiote skill tools (skill_manage /
        # skill_view) resolve the right store/loader. The gateway's parallel
        # tool path copies contextvars into worker threads, so this propagates.
        _scope_token = _set_active_symbiote(symbiote_id)
        record, record_token = open_turn_record(symbiote_id, session_id)
        try:
            # Skills written by other replicas since this one's last turn.
            self._sync_skills(symbiote_id)
            # S-01: Inject previous handoff on session start (first message only)
            extra_context = self._inject_handoff_if_resuming(
                session_id, symbiote_id, extra_context
            )

            user_message = self._sessions.add_message(session_id, "user", content)

            turn_messages = [{"role": "user", "content": content}]
            if self._hooks.hooks:
                self._tool_gateway.run_coroutine_sync(
                    self._hooks.before_turn(turn_messages)
                )
            try:
                result = self._capabilities.run_chat(
                    symbiote_id, session_id, content,
                    extra_context=extra_context,
                    llm_config=llm_config,
                )
                response = self._finish_turn(session_id, symbiote_id, result)
            except BaseException as exc:
                if isinstance(exc, TurnStop) and exc.reason == CANCELLED:
                    # Cancelled by the host: the user message stays and the
                    # trace of what was done is stored.
                    self._record_cancelled_turn(session_id, symbiote_id, exc)
                    raise
                # No answer was stored: drop the user message too, so a
                # retried turn does not leave duplicates without replies.
                self._sessions.delete_message(user_message.id)
                raise
            if self._hooks.hooks:
                self._tool_gateway.run_coroutine_sync(
                    self._hooks.after_turn(turn_messages, _response_text(response))
                )
            return response, result.loop_trace
        finally:
            close_turn_record(record_token)
            self._persist_turn_context(record)
            _reset_active_symbiote(_scope_token)

    async def message_async(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
        *,
        host_context: Mapping[str, Any] | None = None,
    ) -> str | TurnReply:
        """Async variant of message() — supports async tool handlers and on_token streaming.

        Returns what ``message()`` returns: the reply text, or a
        :class:`TurnReply` dict when the turn called tools. ``turn_async()``
        returns a :class:`TurnOutcome` instead.

        Uses per-session async locking: concurrent requests on the same session
        are serialized, while different sessions process in parallel.

        ``llm_config`` and ``host_context`` mirror the sync ``message()``
        semantics.
        """
        reply, _trace = await self._message_async_locked(
            session_id, content, extra_context, on_token, llm_config, host_context
        )
        return reply

    async def turn_async(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
        *,
        host_context: Mapping[str, Any] | None = None,
    ) -> TurnOutcome:
        """Async variant of ``turn()``: runs like ``message_async()`` and
        returns a :class:`TurnOutcome`."""
        return turn_outcome(
            *await self._message_async_locked(
                session_id, content, extra_context, on_token, llm_config, host_context
            )
        )

    async def _message_async_locked(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None,
        on_token: Callable[[str], None] | None,
        llm_config: dict | None,
        host_context: Mapping[str, Any] | None,
    ) -> tuple[str | TurnReply, LoopTrace | None]:
        """One async turn under the session lock: the reply and its trace."""
        host_token = bind_host_context(host_context)
        try:
            lock = self._session_lock
            ttl, wait = self._lock_ttl(), self._config.session_lock_wait
            acquire_async = getattr(lock, "acquire_async", None)
            if acquire_async is not None:
                token = await acquire_async(session_id, wait=wait, ttl=ttl)
            else:
                token = await asyncio.to_thread(
                    lambda: lock.acquire(session_id, wait=wait, ttl=ttl)
                )
            if token is None:
                raise SessionBusyError(session_id, wait)
            lease, control_token = self._begin_turn(session_id, token, ttl)
            try:
                return await self._message_async_inner(
                    session_id, content, extra_context, on_token, llm_config=llm_config
                )
            except TurnStop as stop:
                raise self._stop_error(session_id, stop) from None
            finally:
                self._end_turn(session_id, lease, control_token)
                try:
                    release_async = getattr(lock, "release_async", None)
                    if release_async is not None:
                        await release_async(session_id, token)
                    else:
                        await asyncio.to_thread(lock.release, session_id, token)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "could not release the lock of session %s", session_id
                    )
        finally:
            reset_host_context(host_token)

    async def _message_async_inner(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
    ) -> tuple[str | TurnReply, LoopTrace | None]:
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise EntityNotFoundError("Session", session_id)
        symbiote_id = row["symbiote_id"]

        _scope_token = _set_active_symbiote(symbiote_id)
        record, record_token = open_turn_record(symbiote_id, session_id)
        try:
            if self._skills_sync is not None:  # disk and database I/O: off the loop
                await asyncio.to_thread(self._sync_skills, symbiote_id)
            # S-01: Inject previous handoff on session start (first message only)
            extra_context = self._inject_handoff_if_resuming(
                session_id, symbiote_id, extra_context
            )

            user_message = self._sessions.add_message(session_id, "user", content)

            turn_messages = [{"role": "user", "content": content}]
            await self._hooks.before_turn(turn_messages)
            try:
                result = await self._capabilities.run_chat_async(
                    symbiote_id,
                    session_id,
                    content,
                    extra_context=extra_context,
                    on_token=on_token,
                    llm_config=llm_config,
                )
                response = self._finish_turn(session_id, symbiote_id, result)
            except BaseException as exc:
                if isinstance(exc, TurnStop) and exc.reason == CANCELLED:
                    self._record_cancelled_turn(session_id, symbiote_id, exc)
                    raise
                self._sessions.delete_message(user_message.id)
                raise
            await self._hooks.after_turn(turn_messages, _response_text(response))
            return response, result.loop_trace
        finally:
            close_turn_record(record_token)
            self._persist_turn_context(record)
            _reset_active_symbiote(_scope_token)

    def _record_cancelled_turn(
        self, session_id: str, symbiote_id: str, stop: TurnStop
    ) -> None:
        """Store the trace of a cancelled turn (an empty one when it stopped
        before doing anything), so its final state is on record. Never
        raises: it runs while the turn unwinds."""
        trace = stop.trace if isinstance(stop.trace, LoopTrace) else LoopTrace()
        trace.stop_reason = CANCELLED
        try:
            self._persist_trace(session_id, symbiote_id, trace)
        except Exception:
            logging.getLogger(__name__).exception(
                "could not record the cancelled turn of session %s", session_id
            )

    def _sync_skills(self, symbiote_id: str) -> None:
        """Bring the local skill directory of this turn's scope up to date
        with the shared library. Never raises: a turn that cannot reach the
        library runs with the local copy."""
        sync = self._skills_sync
        if sync is None:
            return
        try:
            if self._skill_scope_mgr is not None:
                scope = symbiote_id
                store = self._skill_scope_mgr.store_for(symbiote_id)
                loader = self._skill_scope_mgr.loader_for(symbiote_id)
            elif self._skills_store is not None and self._skills_loader is not None:
                scope = GLOBAL_SKILL_SCOPE
                store, loader = self._skills_store, self._skills_loader
            else:
                return
            if sync.pull(scope, store.agent_root):
                loader.refresh()
        except Exception:
            logging.getLogger(__name__).exception(
                "could not sync the skills of symbiote %s with the shared library",
                symbiote_id,
            )

    def sweep_skill_cache(self) -> int:
        """Remove the local skill directories of symbiotes that no longer
        exist; returns how many were removed.

        Only meaningful with ``skills_storage`` and the per-symbiote skill
        scope: ``purge_symbiote`` erases the shared library and the directory
        of the replica that ran it, and the other replicas drop their local
        copy here. Runs once when the kernel starts; call it again after
        purges (or on a schedule) in long-lived replicas.
        """
        mgr = self._skill_scope_mgr
        if mgr is None or self._skills_sync is None:
            return 0
        existing = {row["id"] for row in self._storage.fetch_all("SELECT id FROM symbiotes")}
        removed = 0
        for directory in mgr.stale_directories(existing):
            try:
                removed += int(mgr.purge(directory.name))
            except (OSError, ValueError):
                logging.getLogger(__name__).warning(
                    "could not remove the stale skill directory %s", directory, exc_info=True
                )
        return removed

    def _finish_turn(self, session_id: str, symbiote_id: str, result: RunResult):  # noqa: ANN202
        """Persist what this turn produced; return its output or raise.

        Everything comes from this turn's own RunResult, never from state
        shared with other sessions. The trace is persisted even when the
        run failed, so a failing turn still shows up in scoring. The trace
        and the assistant message are one transaction: a reply is never
        stored without its trace, nor a successful trace without its reply.
        """
        response = result.output if result.success else None
        with self._storage.transaction():
            if result.loop_trace is not None:
                self._persist_trace(session_id, symbiote_id, result.loop_trace)
            if result.success:
                self._sessions.add_message(
                    session_id, "assistant", _response_text(response)
                )
        if result.handoff_data is not None:
            with self._handoffs_lock:
                self._handoffs[session_id] = result.handoff_data
        if not result.success:
            raise CapabilityError("chat", result.error or "Chat runner failed")
        return response

    def _persist_turn_context(self, record: TurnRecord) -> None:
        """Write what entered the context of a turn to ``turn_contexts``.

        Runs for every turn whose context was assembled, with or without
        tools and whether or not the run succeeded. Never raises: it is
        called while a turn unwinds.
        """
        if not record.assembled:
            return
        import json
        from datetime import UTC, datetime
        from uuid import uuid4

        try:
            self._storage.execute(
                "INSERT INTO turn_contexts "
                "(id, session_id, symbiote_id, memories_json, knowledge_json, "
                "skills_json, tokens_estimate, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    record.session_id,
                    record.symbiote_id,
                    json.dumps(record.memories),
                    json.dumps(record.knowledge),
                    json.dumps(record.skills),
                    record.tokens_estimate,
                    datetime.now(tz=UTC).isoformat(),
                ),
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "could not record the context of a turn of session %s",
                record.session_id,
            )

    def _latest_trace(self, session_id: str) -> LoopTrace | None:
        """Most recent loop trace persisted for ``session_id``."""
        import json

        from symbiote.runners.base import LoopStep

        row = self._storage.fetch_one(
            "SELECT total_iterations, total_tool_calls, total_elapsed_ms, "
            "stop_reason, steps_json, tool_mode FROM execution_traces "
            "WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        )
        if row is None:
            return None
        steps = [LoopStep(**s) for s in json.loads(row["steps_json"] or "[]")]
        return LoopTrace(
            steps=steps,
            total_iterations=row["total_iterations"] or 0,
            total_tool_calls=row["total_tool_calls"] or 0,
            total_elapsed_ms=row["total_elapsed_ms"] or 0,
            stop_reason=row["stop_reason"],
            tool_mode=row["tool_mode"] or "brief",
        )

    @staticmethod
    def _close_step(name: str, fn: Callable, *args) -> None:  # noqa: ANN001
        try:
            fn(*args)
        except Exception:
            logging.getLogger(__name__).exception(
                "close_session: %s step failed; continuing", name
            )

    def close_session(self, session_id: str) -> Session:
        """Run reflection, compute score, persist failure memory, then close."""
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is not None:
            symbiote_id = row["symbiote_id"]
            # Each post-processing step is isolated: one failing (bad LLM
            # output, a host adapter error) is logged and the others still
            # run; the session is always closed below.

            # 1. Compute and persist session score
            trace = self._latest_trace(session_id)
            self._close_step("score", self._persist_score, session_id, symbiote_id, trace)

            # 2. Generate failure MemoryEntry if loop didn't complete
            self._close_step(
                "failure memory", self._generate_failure_memory, session_id, symbiote_id, trace
            )

            # 3. Reflection — build engine matching this symbiote's config.
            # Skipped when the symbiote turned reflection or long-term memory
            # off (no LLM call, nothing written to reflection_audit). The
            # other memory writers below are stopped by the MemoryStore.
            cfg = self._environment.get_config(symbiote_id)
            if cfg is None or (cfg.memory_enabled and cfg.reflection_mode != "off"):
                self._close_step(
                    "reflection",
                    lambda: self._reflection_for(symbiote_id).reflect_session(
                        session_id, symbiote_id
                    ),
                )

            # 4. S-02: Persist long-run handoff as memory entry
            self._close_step("handoff", self._persist_handoff_memory, session_id, symbiote_id)

            # 5. Skill self-improvement (Sprint 4) — fire daemon thread if
            # the symbiote opted in via skill_review_enabled. Never blocks
            # close_session; failures are logged inside the engine.
            def _review() -> None:
                review = self._background_review_for(symbiote_id)
                if review is not None:
                    review.spawn_final(session_id, symbiote_id)

            self._close_step("skill review", _review)

        closed = self._sessions.close(session_id)
        if row is not None:
            # 6. Dream mode — after the close, so this session already counts
            # towards dream_min_sessions.
            self._close_step("dream", self._maybe_dream, row["symbiote_id"])
        return closed

    # ── Skills wiring ──────────────────────────────────────────────────

    def _setup_skills_wiring(self) -> None:
        """Build SkillsStore + SkillsLoader from config, register skill_manage.

        Resolution rules:
        - ``config.skills_root`` (if set) is the agent write root.
        - Otherwise, defaults to ``{db_path.parent}/skills/``. Human-curated
          and agent-created skills coexist in this directory; the
          ``.skill_meta.json`` sidecar (Sprint 3) distinguishes them via
          ``agent_created`` — no physical subfolder required.
        - ``config.skills_extra_roots`` are read+modify roots (e.g. a
          ``skills/`` directory in the project).
        - ``config.skills_protected_roots`` are read-only (e.g.
          ``process/skills``).

        Idempotent — safe to skip wiring on hosts that don't want skills:
        the agent root directory is created lazily; ``skill_manage`` is
        always registered with the gateway but never auto-authorized.

        Failure modes (Sprint 4.2):
        - Host EXPLICITLY set ``skills_root`` → ``SkillsStore`` init failure
          is re-raised so the host sees the PermissionError / OSError.
        - Default derived path → init failure logs a warning and disables
          the feature silently. ``_skills_store`` and ``_skills_loader``
          stay ``None``.

        All-or-nothing: every Sprint 4+ feature (skill_manage tool,
        background review, Dream skill-lifecycle, Sprint 5 auto-promote /
        auto-archive / skill_review_audit) depends on ``_skills_loader``
        being non-None. The guards in ``_background_review_for`` and in
        ``_build_dream_engine`` make all these features no-op
        when wiring is disabled — no silent partial activation.
        """
        cfg = self._config
        # Default: ``{db_path.parent}/skills/`` — same directory the
        # ``SkillsLoader`` scans natively. Human-curated and agent-created
        # skills coexist; the ``.skill_meta.json`` sidecar (Sprint 3)
        # provides the distinction, so we don't need a physical subfolder.
        host_requested_skills = cfg.skills_root is not None
        agent_root = cfg.skills_root or (Path(cfg.db_path).parent / "skills")

        # Per-symbiote scope (Option A): no single store/loader. Build a
        # SkillScopeManager that lazily produces a per-symbiote store/loader
        # rooted at {base}/<symbiote_id>/skills, plus the shared read-only
        # catalogue (skills_protected_roots). Tools resolve the active
        # symbiote's store/loader per call via the active-symbiote ContextVar.
        if getattr(cfg, "skill_scope", "global") == "per_symbiote":
            self._setup_skills_wiring_per_symbiote(agent_root, cfg)
            return

        roots: list[Path] = [agent_root, *cfg.skills_extra_roots]
        protected = list(cfg.skills_protected_roots)

        try:
            self._skills_store = SkillsStore(
                roots=roots, protected_roots=protected, read_only=not cfg.skill_writes
            )
            self._skills_store.redact = self._redactor.redact_text
        except Exception as exc:
            # Two failure regimes:
            # - Host EXPLICITLY set ``skills_root`` in KernelConfig — they
            #   want this feature. Re-raise so the host sees the
            #   PermissionError / OSError instead of a feature silently
            #   disabled in a log line they may never read.
            # - Host did NOT set ``skills_root`` (we derived the default
            #   under db_path.parent) — they may not care about skills.
            #   Log and continue with the feature disabled.
            if host_requested_skills:
                raise
            import logging
            logging.getLogger(__name__).warning(
                "Skills wiring disabled at default path %s (%s). "
                "Pass KernelConfig(skills_root=...) explicitly if you "
                "want this feature.",
                agent_root, exc,
            )
            return

        # Loader expects ``{root}/skills/{name}/SKILL.md``, so we pass each
        # root's PARENT. With the default agent_root of
        # ``.symbiote/skills/``, the loader root is ``.symbiote/``, which
        # scans ``.symbiote/skills/*/SKILL.md`` — exactly where the store
        # writes.
        loader_roots: list[Path] = []
        for r in roots:
            # Only useful if the directory name is exactly "skills".
            if r.name == "skills":
                loader_roots.append(r.parent)
            else:
                # Caller used a non-standard name; assume layout is
                # {r}/{name}/SKILL.md (skills sit directly in r). The
                # loader doesn't handle that natively, so we treat r itself
                # as the search dir by passing r.parent — same convention,
                # just renamed.
                loader_roots.append(r.parent)
        self._skills_loader = SkillsLoader(
            *loader_roots, read_only=not cfg.skill_writes
        )
        if self._skills_store is not None:
            sync, store, loader = self._skills_sync, self._skills_store, self._skills_loader
            if sync is None:
                store.on_change = loader.refresh
            else:
                sync.attach(GLOBAL_SKILL_SCOPE, store.agent_root)

                def changed() -> None:
                    sync.push(GLOBAL_SKILL_SCOPE, store.agent_root)
                    loader.refresh()

                store.on_change = changed

        # Wire the loader into the ContextAssembler so active skills can be
        # injected into the system prompt — but ONLY for symbiotes that opt in
        # via environment.configure(skill_injection_enabled=True). The
        # assembler consults get_skill_injection_enabled() per turn, so merely
        # attaching the loader does not change behaviour for anyone.
        setter = getattr(self._context_assembler, "set_skills_loader", None)
        if setter is not None:
            setter(self._skills_loader)

        # Always register the tools — but they stay unauthorized for each
        # symbiote until the host explicitly opts in via
        # `kernel.environment.configure(tools=[..., 'skill_manage'])` /
        # `[..., 'skill_view']`. skill_view (read-only) pairs with
        # skill_injection_mode='index' for progressive disclosure.
        skill_tool_module.register(self._tool_gateway, self._skills_store)
        register_view = getattr(skill_tool_module, "register_view", None)
        if register_view is not None:
            register_view(
                self._tool_gateway, self._skills_loader, self._skill_trial_policy
            )

    def _skill_trial_policy(self) -> tuple[int, int] | None:
        """For ``skill_view``: ``None`` when the calling symbiote hides its
        skills on trial, else ``(auto_promote_threshold, max_active_skills)``."""
        from symbiote.skills.scope import get_active_symbiote

        symbiote_id = get_active_symbiote()
        cfg = self._environment.get_config(symbiote_id) if symbiote_id else None
        if cfg is None or not cfg.skill_trial_listing:
            return None
        return cfg.skill_auto_promote_threshold, cfg.max_active_skills

    def _setup_skills_wiring_per_symbiote(self, base_root: Path, cfg) -> None:
        """Wire skills in per-symbiote scope (Option A).

        Builds a ``SkillScopeManager`` (factory + LRU cache of per-symbiote
        store/loader) and registers ``skill_manage`` / ``skill_view`` with
        resolver closures that read the active-symbiote ContextVar. The single
        ``_skills_store`` / ``_skills_loader`` stay None — every access is
        per-symbiote. The shared read-only catalogue is
        ``skills_protected_roots`` (may be empty).
        """
        from symbiote.skills.scope import SkillScopeManager, get_active_symbiote

        try:
            mgr = SkillScopeManager(
                base_root=base_root,
                protected_roots=list(cfg.skills_protected_roots),
                extra_roots=list(cfg.skills_extra_roots),
                max_cached=cfg.symbiote_cache_size,
                read_only=not cfg.skill_writes,
                redact=self._redactor.redact_text,
                sync=self._skills_sync,
            )
        except Exception as exc:
            if cfg.skills_root is not None:
                raise
            import logging
            logging.getLogger(__name__).warning(
                "Per-symbiote skills wiring disabled at %s (%s).", base_root, exc,
            )
            return

        self._skill_scope_mgr = mgr

        def _store_provider():
            sid = get_active_symbiote()
            return mgr.store_for(sid) if sid else None

        def _loader_provider():
            sid = get_active_symbiote()
            return mgr.loader_for(sid) if sid else None

        # ContextAssembler resolves the active symbiote's loader per turn.
        setter = getattr(
            self._context_assembler, "set_skills_loader_resolver", None,
        )
        if setter is not None:
            setter(lambda symbiote_id: mgr.loader_for(symbiote_id) if symbiote_id else None)

        # Tools resolve the active symbiote's store/loader per call.
        reg_resolved = getattr(skill_tool_module, "register_resolved", None)
        reg_view_resolved = getattr(skill_tool_module, "register_view_resolved", None)
        if reg_resolved is not None:
            reg_resolved(self._tool_gateway, _store_provider)
        if reg_view_resolved is not None:
            reg_view_resolved(
                self._tool_gateway, _loader_provider, self._skill_trial_policy
            )

    def _background_review_for(self, symbiote_id: str) -> BackgroundReviewEngine | None:
        """Return the engine used for ``skill_review_enabled`` symbiotes.

        None when the feature is disabled (default) or when prerequisites
        are not met (no evolver_llm, no SkillsStore, no SkillsLoader).
        Lazily constructed on first request and cached for the kernel
        lifetime.

        Sprint 5: also propagates ``skill_auto_promote_threshold`` to the
        loader. In single-user kernels (the common case) the threshold is
        stable per kernel; in multi-symbiote setups the last opt-in
        symbiote wins — acceptable because each symbiote sees only its own
        skills via path scoping anyway.
        """
        if not self._config.skill_writes:
            return None  # the review exists to write skills
        cfg = self._environment.get_config(symbiote_id)
        if cfg is None or not cfg.skill_review_enabled:
            return None
        if self._evolver_llm is None:
            # EnvironmentManager.configure() guards against this, but defend
            # in depth: a row inserted directly into the DB could bypass it.
            return None

        # Resolve the store/loader for this symbiote (scope-aware).
        store: SkillsStore | None
        loader: SkillsLoader | None
        if self._skill_scope_mgr is not None:
            store = self._skill_scope_mgr.store_for(symbiote_id)
            loader = self._skill_scope_mgr.loader_for(symbiote_id)
        else:
            store = self._skills_store
            loader = self._skills_loader
        if store is None or loader is None:
            return None
        # Keep the loader's auto-promote threshold in sync with cfg on each
        # call. Cheap (just sets an int) and idempotent.
        loader.set_auto_promote_threshold(cfg.skill_auto_promote_threshold)
        loader.set_max_active_skills(cfg.max_active_skills)

        # Per-symbiote scope: one engine per symbiote (each sees only its own
        # store/loader). Global scope: a single shared engine.
        if self._skill_scope_mgr is not None:
            with self._background_review_lock:
                eng = self._background_review_by_symbiote.get(symbiote_id)
                if eng is None:
                    eng = BackgroundReviewEngine(
                        llm=self._evolver_llm,
                        messages=self._message_repo,
                        store=store,
                        loader=loader,
                        max_active_skills=cfg.max_active_skills,
                        max_quarantine_skills=cfg.max_quarantine_skills,
                        storage=self._storage,
                        environment=self._environment,
                    )
                    self._background_review_by_symbiote[symbiote_id] = eng
                    self._evict_idle_review_engines(keep=symbiote_id)
                else:
                    self._background_review_by_symbiote.move_to_end(symbiote_id)
            return eng

        # Hot path: engine already built — return immediately, no lock.
        if self._background_review is not None:
            return self._background_review
        # Cold path: serialize the build to avoid two threads constructing
        # rival engines (mid-session nudge from ChatRunner vs close_session
        # firing the final pass on the same symbiote).
        with self._background_review_lock:
            if self._background_review is None:
                self._background_review = BackgroundReviewEngine(
                    llm=self._evolver_llm,
                    messages=self._message_repo,
                    store=store,  # == self._skills_store in global scope
                    loader=loader,
                    max_active_skills=cfg.max_active_skills,
                    max_quarantine_skills=cfg.max_quarantine_skills,
                    storage=self._storage,
                    environment=self._environment,
                )
        return self._background_review

    def _evict_idle_review_engines(self, *, keep: str) -> None:
        """Drop least recently used review engines beyond the cache size.

        Called with ``_background_review_lock`` held. An engine with a review
        still running is kept (``shutdown`` must be able to wait for it), so
        the cache can exceed the limit while that many reviews are running.
        """
        limit = self._config.symbiote_cache_size
        excess = len(self._background_review_by_symbiote) - limit
        if excess <= 0:
            return
        for sid in list(self._background_review_by_symbiote):
            if excess <= 0:
                break
            if sid == keep or self._background_review_by_symbiote[sid].active_threads():
                continue
            del self._background_review_by_symbiote[sid]
            excess -= 1

    def _reflection_for(self, symbiote_id: str) -> ReflectionEngine:
        """Build a ReflectionEngine for this symbiote's current config.

        Returns the default keyword-only engine when no config exists or
        when reflection_mode == "keyword" (the safe baseline). LLM modes
        pick the right backing LLM:
          - llm        -> evolver_llm (cheap aux model)
          - llm_main   -> main self._llm
          - hybrid     -> evolver_llm (runs both, persists keyword)
        """
        cfg = self._environment.get_config(symbiote_id)
        mode = cfg.reflection_mode if cfg else "keyword"
        max_tokens = cfg.reflection_max_tokens if cfg else 4000

        if mode in ("keyword", "off"):  # "off" is decided by close_session
            return self._reflection

        # llm/hybrid -> evolver_llm; llm_main -> main self._llm
        llm: LLMPort | None = self._llm if mode == "llm_main" else self._evolver_llm

        # Defensive: if EnvironmentManager.configure() was bypassed (e.g.
        # the row was written directly to the DB), still fall back to
        # keyword instead of crashing on a None LLM.
        if llm is None:
            return self._reflection

        return ReflectionEngine(
            self._memory,
            self._message_repo,
            llm=llm,
            mode=mode,
            storage=self._storage,
            max_tokens=max_tokens,
        )

    def _inject_handoff_if_resuming(
        self,
        session_id: str,
        symbiote_id: str,
        extra_context: dict | None,
    ) -> dict | None:
        """S-01: On first message of a session, inject the most recent handoff.

        Only for a symbiote in long_run mode: the planner is what reads it.
        A chat turn would get the whole JSON record in its system prompt.
        """
        if self._environment.get_tool_mode(symbiote_id) != "long_run":
            return extra_context
        msg_row = self._storage.fetch_one(
            "SELECT COUNT(*) as c FROM messages WHERE session_id = ?", (session_id,)
        )
        if msg_row is None or msg_row["c"] > 0:
            return extra_context

        # Only a handoff left by the same end user (or, without one, by the
        # shared sessions): one person's unfinished project is never resumed
        # in another person's conversation.
        subject = subject_kwargs(self._memory, session_id)
        handoff_entries = self._memory.get_by_category(
            symbiote_id, "handoff", limit=1,
            **({**subject, "include_shared": False} if subject else {}),
        )
        if not handoff_entries:
            return extra_context

        import json
        try:
            hd = json.loads(handoff_entries[0].content)
        except Exception:
            return extra_context
        # A finished project leaves nothing to resume. Injecting it told the
        # planner to "plan only the pending blocks" — none — so repeating a
        # completed request produced an empty plan and the turn failed.
        if not isinstance(hd, dict) or not hd.get("pending_blocks"):
            return extra_context
        return {**(extra_context or {}), "previous_handoff": hd}

    def _persist_handoff_memory(self, session_id: str, symbiote_id: str) -> None:
        """S-02: Persist long-run handoff_data as a MemoryEntry(category='handoff')."""
        with self._handoffs_lock:
            handoff = self._handoffs.pop(session_id, None)
        if handoff is None:
            return

        import json

        from symbiote.core.models import MemoryEntry
        from symbiote.core.scoring import _utcnow, _uuid

        entry = MemoryEntry(
            id=_uuid(),
            symbiote_id=symbiote_id,
            session_id=session_id,
            type="session_summary",
            category="handoff",
            scope="global",
            source="system",
            content=json.dumps(handoff, ensure_ascii=False),
            importance=1.0,
            created_at=_utcnow(),
        )
        self._memory.store(entry)

    # ── Dream Mode ─────────────────────────────────────────────────────

    def _skills_loader_for(self, symbiote_id: str):
        """Resolve the SkillsLoader for a symbiote (scope-aware).

        Per-symbiote scope → the symbiote's own loader; global scope → the
        single shared loader (may be None when skills wiring is disabled).
        """
        if self._skill_scope_mgr is not None:
            return self._skill_scope_mgr.loader_for(symbiote_id)
        return self._skills_loader

    def _build_dream_engine(
        self,
        cfg,  # noqa: ANN001
        symbiote_id: str,
        *,
        min_sessions: int | None = None,
        dry_run: bool = False,
    ) -> DreamEngine:
        """A DreamEngine configured for this symbiote, built per call.

        Never cached: each symbiote has its own dream settings and they may
        change between cycles. Concurrency is guarded by the kernel
        (``_dream_threads``), not by the engine.
        """
        return DreamEngine(
            storage=self._storage,
            memory=self._memory,
            llm=self._evolver_llm or self._llm,
            max_llm_calls=cfg.dream_max_llm_calls if cfg else 10,
            min_sessions=min_sessions if min_sessions is not None else cfg.dream_min_sessions,
            dry_run=dry_run,
            # Without a loader the skill lifecycle phases do nothing.
            skills_loader=(
                self._skills_loader_for(symbiote_id)
                if self._config.skill_writes
                else None
            ),
            skill_quarantine_timeout_days=(
                cfg.skill_quarantine_timeout_days if cfg else None
            ),
        )

    def _maybe_dream(self, symbiote_id: str) -> None:
        """Start a background dream cycle if due and none is running for the symbiote."""
        cfg = self._environment.get_config(symbiote_id)
        if cfg is None or cfg.dream_mode == "off" or not cfg.memory_enabled:
            return
        with self._dream_lock:
            running = self._dream_threads.get(symbiote_id)
            if running is not None and running.is_alive():
                return
            engine = self._build_dream_engine(cfg, symbiote_id)
            if not engine.should_dream(symbiote_id, cfg.dream_mode):
                return
            thread = threading.Thread(
                target=self._run_dream,
                args=(engine, symbiote_id, cfg.dream_mode),
                daemon=True,
                name=f"dream-{symbiote_id[:8]}",
            )
            self._dream_threads[symbiote_id] = thread
            thread.start()

    def _run_dream(self, engine: DreamEngine, symbiote_id: str, mode: str) -> None:
        try:
            engine.dream(symbiote_id, mode)
        except Exception:
            logging.getLogger(__name__).exception(
                "dream background cycle failed for %s", symbiote_id
            )
        finally:
            with self._dream_lock:
                if self._dream_threads.get(symbiote_id) is threading.current_thread():
                    del self._dream_threads[symbiote_id]

    def dream(self, symbiote_id: str, *, dry_run: bool = False) -> DreamReport:
        """Run a dream cycle synchronously (for CLI / manual invocation)."""
        cfg = self._environment.get_config(symbiote_id)
        mode = cfg.dream_mode if cfg and cfg.dream_mode != "off" else "light"
        # Manual trigger ignores min_sessions.
        engine = self._build_dream_engine(cfg, symbiote_id, min_sessions=1, dry_run=dry_run)
        return engine.dream(symbiote_id, mode)

    def report_feedback(
        self, session_id: str, score: float, source: str = "user"
    ) -> None:
        """Report user feedback for a session, updating the final score.

        The host calls this when it has a quality signal (user thumbs up,
        task completion, etc.).  The auto_score is preserved; final_score
        is recomputed as a weighted combination.
        """
        with self._storage.transaction():
            self._apply_feedback(session_id, score)

    def _apply_feedback(self, session_id: str, score: float) -> None:
        row = self._storage.fetch_one(
            "SELECT id, auto_score FROM session_scores WHERE session_id = ?",
            (session_id,),
        )
        if row is None:
            # No auto_score yet — store user score alone
            from symbiote.core.scoring import _utcnow, _uuid

            sid_row = self._storage.fetch_one(
                "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,),
            )
            symbiote_id = sid_row["symbiote_id"] if sid_row else ""
            final = compute_final_score(0.8, score)  # assume 0.8 auto
            self._storage.execute(
                "INSERT INTO session_scores "
                "(id, session_id, symbiote_id, auto_score, user_score, final_score, "
                "computed_at, scored_at_close) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (_uuid(), session_id, symbiote_id, 0.8, score, final,
                 _utcnow().isoformat()),
            )
            return

        auto = row["auto_score"]
        final = compute_final_score(auto, score)
        self._storage.execute(
            "UPDATE session_scores SET user_score = ?, final_score = ? WHERE id = ?",
            (score, final, row["id"]),
        )

    def register_http_tool(
        self, symbiote_id: str, descriptor: ToolDescriptor, http_config: HttpToolConfig
    ) -> None:
        """Register an HTTP tool for a symbiote, persist it and authorize it.

        The tool is stored, so it survives restarts and is visible to every
        process using this database (HTTP API, CLI). Raises
        ``DuplicateEntityError`` when ``tool_id`` belongs to a builtin, to a
        tool registered in code, or to another symbiote's HTTP tool.
        """
        tool_id = descriptor.tool_id
        owner = self._http_tools.owner(tool_id)
        if owner is not None and owner != symbiote_id:
            raise DuplicateEntityError("Tool", tool_id)
        if owner is None and self._tool_gateway.has_tool(tool_id):
            raise DuplicateEntityError("Tool", tool_id)
        descriptor = descriptor.model_copy(update={"handler_type": "http"})
        self._http_tools.save(symbiote_id, descriptor, http_config)
        self._tool_gateway.register_http_tool(descriptor, http_config)
        self._environment.add_tools(symbiote_id, [tool_id])

    def remove_http_tool(self, symbiote_id: str, tool_id: str) -> bool:
        """Take a tool away from a symbiote.

        Always removes it from the symbiote's authorized list. Only an HTTP
        tool this symbiote registered is also deleted and unregistered —
        builtins and other symbiotes' tools stay for everyone else.
        Returns True if the tool was deleted.
        """
        self._environment.remove_tools(symbiote_id, [tool_id])
        if self._http_tools.owner(tool_id) != symbiote_id:
            return False
        self._http_tools.delete(tool_id)
        self._tool_gateway.unregister_tool(tool_id)
        return True

    def prune(
        self,
        *,
        dry_run: bool = False,
        messages_days: int | None = None,
        traces_days: int | None = None,
        memories_days: int | None = None,
    ) -> dict[str, int]:
        """Apply the retention windows; returns rows deleted (or that would be).

        Removes the messages and the traces (``execution_traces`` and
        ``turn_contexts``) of sessions that are ``closed`` and ended more than
        N days ago; active sessions are never touched. Removes the memory
        entries created more than N days ago, whatever their session.
        Summaries and scores are kept. The windows default to
        ``KernelConfig.message_retention_days`` / ``trace_retention_days`` /
        ``memory_retention_days``; ``None`` (the default) leaves those tables
        alone. Not scheduled automatically.
        """
        trace_window = (
            traces_days if traces_days is not None
            else self._config.trace_retention_days
        )
        windows = {
            "messages": messages_days if messages_days is not None
            else self._config.message_retention_days,
            "execution_traces": trace_window,
            "turn_contexts": trace_window,
        }
        old_sessions = (
            "SELECT id FROM sessions WHERE status = 'closed' "
            "AND ended_at IS NOT NULL AND ended_at < ?"
        )
        memory_window = (
            memories_days if memories_days is not None
            else self._config.memory_retention_days
        )
        # One transaction: a prune that fails midway deletes nothing.
        with self._storage.transaction():
            return self._prune(windows, old_sessions, memory_window, dry_run)

    def _prune(
        self,
        windows: dict[str, int | None],
        old_sessions: str,
        memory_window: int | None,
        dry_run: bool,
    ) -> dict[str, int]:
        from datetime import UTC, datetime, timedelta

        counts: dict[str, int] = {}
        for table, days in windows.items():
            if days is None:
                counts[table] = 0
                continue
            cutoff = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()
            where = f"session_id IN ({old_sessions})"
            row = self._storage.fetch_one(
                f"SELECT COUNT(*) AS c FROM {table} WHERE {where}", (cutoff,)  # noqa: S608
            )
            counts[table] = int(row["c"]) if row else 0
            if not dry_run and counts[table]:
                self._storage.execute(f"DELETE FROM {table} WHERE {where}", (cutoff,))  # noqa: S608

        counts["memory_entries"] = 0
        if memory_window is not None:
            cutoff = (datetime.now(tz=UTC) - timedelta(days=memory_window)).isoformat()
            row = self._storage.fetch_one(
                "SELECT COUNT(*) AS c FROM memory_entries WHERE created_at < ?", (cutoff,)
            )
            counts["memory_entries"] = int(row["c"]) if row else 0
            if not dry_run and counts["memory_entries"]:
                self._storage.execute(
                    "DELETE FROM memory_entries WHERE created_at < ?", (cutoff,)
                )
        return counts

    # Rows that belong to a symbiote through one of its sessions, then the
    # rows that carry its id. Children first: foreign keys are enforced.
    _PURGE_BY_SESSION = (
        "messages",
        "decisions",
        "process_instances",
        "artifacts",
        "session_locks",
    )
    _PURGE_BY_SYMBIOTE = (
        "turn_contexts",
        "execution_traces",
        "session_scores",
        "reflection_audit",
        "skill_review_audit",
        "audit_log",
        "memory_entries",
        "knowledge_entries",
        "dream_reports",
        "harness_versions",
        "persona_audit",
        "discovered_tools",
        "environment_configs",
        "http_tools",
        "workspaces",
        "sessions",
    )

    def purge_symbiote(
        self, symbiote_id: str, *, dry_run: bool = False
    ) -> dict[str, int]:
        """Erase a symbiote and everything the kernel stored about it.

        Deletes its sessions, messages, memories, knowledge, traces, scores,
        audit rows, harness versions, configuration, the HTTP tools it
        registered and the symbiote row itself; with
        ``skill_scope="per_symbiote"`` also its skill directory (key
        ``"skills"`` in the result, 1 when a directory was removed). In the
        global skill scope the library is shared, so no skill is removed.
        Returns the rows deleted per table (or that would be, with
        ``dry_run``).

        Unlike ``IdentityManager.delete`` (a status change), this cannot be
        undone. Stop sending turns to the symbiote first: background work
        still running for it gets a few seconds to finish, and rows written by
        a turn that is still in flight are not covered. The database deletes
        are one transaction (all or nothing); the skill directory is removed
        after it commits.

        Raises ``EntityNotFoundError`` when the symbiote does not exist.
        """
        if self._storage.fetch_one(
            "SELECT id FROM symbiotes WHERE id = ?", (symbiote_id,)
        ) is None:
            raise EntityNotFoundError("Symbiote", symbiote_id)

        sessions = "SELECT id FROM sessions WHERE symbiote_id = ?"
        workspaces = "SELECT id FROM workspaces WHERE symbiote_id = ?"
        targets: list[tuple[str, str, tuple]] = [
            (table, f"session_id IN ({sessions})", (symbiote_id,))
            for table in self._PURGE_BY_SESSION
        ]
        # Artifacts can also hang from a workspace without a session.
        targets.append(("artifacts", f"workspace_id IN ({workspaces})", (symbiote_id,)))
        targets += [
            (table, "symbiote_id = ?", (symbiote_id,))
            for table in self._PURGE_BY_SYMBIOTE
        ]
        # Its scope of the shared skill library (per-symbiote skill scope).
        targets += [
            (table, "scope = ?", (symbiote_id,)) for table in ("skill_files", "skill_scopes")
        ]
        targets.append(("symbiotes", "id = ?", (symbiote_id,)))

        if not dry_run:
            self._quiesce_symbiote(symbiote_id)
            session_ids = [
                r["id"] for r in self._storage.fetch_all(sessions, (symbiote_id,))
            ]
            for tool in self._storage.fetch_all(
                "SELECT tool_id FROM http_tools WHERE symbiote_id = ?", (symbiote_id,)
            ):
                self._tool_gateway.unregister_tool(tool["tool_id"])

        # One transaction: the symbiote is erased from the database entirely
        # or, if a delete fails, not at all.
        counts: dict[str, int] = {}
        with self._storage.transaction():
            for table, where, params in targets:
                row = self._storage.fetch_one(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE {where}", params  # noqa: S608
                )
                found = int(row["c"]) if row else 0
                counts[table] = counts.get(table, 0) + found
                if not dry_run and found:
                    self._storage.execute(
                        f"DELETE FROM {table} WHERE {where}", params  # noqa: S608
                    )

        counts["skills"] = 0
        if self._skill_scope_mgr is not None:
            if dry_run:
                counts["skills"] = int(self._skill_scope_mgr.has_directory(symbiote_id))
            else:
                counts["skills"] = int(self._skill_scope_mgr.purge(symbiote_id))
                if self._skills_storage is not None:
                    # A library kept outside the kernel's database.
                    self._skills_storage.delete_scope(symbiote_id)
        if not dry_run:
            with self._handoffs_lock:
                for sid in session_ids:
                    self._handoffs.pop(sid, None)
        return counts

    # Every table that carries a session id: what a session of an end user
    # leaves behind besides its memories.
    _PURGE_SUBJECT_BY_SESSION = (
        *_PURGE_BY_SESSION,
        "turn_contexts",
        "execution_traces",
        "session_scores",
        "reflection_audit",
        "skill_review_audit",
        "audit_log",
    )

    def purge_subject(
        self, symbiote_id: str, subject_key: str, *, dry_run: bool = False
    ) -> dict[str, int]:
        """Erase what the kernel stored about one end user of a symbiote.

        Deletes the sessions started with ``subject_key`` for this symbiote,
        their messages, decisions, traces, scores and audit rows, and every
        memory of that end user's partition. The shared memories and the
        other end users are left untouched. Returns the rows deleted per
        table (or that would be, with ``dry_run``); an end user the symbiote
        never saw gives zeros.

        The same key under another symbiote is another partition: when the
        person also talked to symbiotes this one delegated to (``spawn``),
        call this for each of them.

        Cannot be undone. Not covered, because they are no longer tied to
        the person: skills written from their sessions and the scores
        already added to the harness versions, both of which exist only if
        ``learn_from_subject_sessions`` was on. Stop sending turns for the
        end user first: rows written by a turn still in flight are not
        covered. The deletes are one transaction (all or nothing).

        Raises ``EntityNotFoundError`` when the symbiote does not exist and
        ``ValidationError`` for an empty ``subject_key``.
        """
        subject = validate_subject_key(subject_key)
        if subject is None:
            raise ValidationError("purge_subject needs a subject_key")
        if self._storage.fetch_one(
            "SELECT id FROM symbiotes WHERE id = ?", (symbiote_id,)
        ) is None:
            raise EntityNotFoundError("Symbiote", symbiote_id)

        sessions = "SELECT id FROM sessions WHERE symbiote_id = ? AND subject_key = ?"
        owner = (symbiote_id, subject)
        targets: list[tuple[str, str, tuple]] = [
            (table, f"session_id IN ({sessions})", owner)
            for table in self._PURGE_SUBJECT_BY_SESSION
        ]
        targets.append((
            "memory_entries",
            f"symbiote_id = ? AND (subject_key = ? OR session_id IN ({sessions}))",
            (*owner, *owner),
        ))
        targets.append(("sessions", "symbiote_id = ? AND subject_key = ?", owner))

        session_ids = [r["id"] for r in self._storage.fetch_all(sessions, owner)]
        counts: dict[str, int] = {}
        with self._storage.transaction():
            for table, where, params in targets:
                row = self._storage.fetch_one(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE {where}", params  # noqa: S608
                )
                found = int(row["c"]) if row else 0
                counts[table] = found
                if not dry_run and found:
                    self._storage.execute(
                        f"DELETE FROM {table} WHERE {where}", params  # noqa: S608
                    )
        if not dry_run:
            with self._handoffs_lock:
                for sid in session_ids:
                    self._handoffs.pop(sid, None)
        return counts

    def _quiesce_symbiote(self, symbiote_id: str, timeout: float = 5.0) -> None:
        """Wait briefly for the symbiote's background work and forget its
        cached per-symbiote objects."""
        deadline = time.monotonic() + timeout
        with self._dream_lock:
            dream = self._dream_threads.get(symbiote_id)
        with self._background_review_lock:
            engine = self._background_review_by_symbiote.pop(symbiote_id, None)
        threads = [dream] if dream is not None else []
        if engine is not None:
            threads.extend(engine.active_threads())
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    @property
    def discovered_tools(self) -> DiscoveredToolRepository:
        """Tools found by discovery, with their pending/approved/disabled
        status (``set_status``, ``classify_by_tags``, ``list``)."""
        return DiscoveredToolRepository(self._storage)

    def discover_tools(
        self, symbiote_id: str, source_path: str = ".", *, url: str | None = None
    ):
        """Scan a repository (``source_path``) or a live server (``url``, whose
        ``/openapi.json`` is fetched) and store what it finds as ``pending``.

        Approve with ``discovered_tools.set_status(...)``, then
        ``load_discovered_tools``. Returns the ``DiscoveryResult``. The fetch
        is blocking: from async code, run it in a thread.
        """
        from symbiote.discovery.service import DiscoveryService

        return DiscoveryService(self.discovered_tools).discover(symbiote_id, source_path, url=url)

    def load_discovered_tools(self, symbiote_id: str, base_url: str = "") -> list[str]:
        """Load approved discovered tools into the ToolGateway and authorize them.

        Reads all tools with ``status=approved`` from ``discovered_tools`` for
        *symbiote_id*, registers each HTTP tool in the ToolGateway, and adds
        them to the symbiote's authorized tools (tools it already had stay
        authorized).

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
            self._environment.add_tools(symbiote_id, tool_ids)
        return tool_ids

    def load_mcp_tools(self, registry: object, symbiote_id: str) -> list[str]:
        """Register all tools from a forge_llm ToolRegistry into the ToolGateway.

        The *registry* must be a live ``forge_llm.application.tools.ToolRegistry``
        produced by ``McpToolset.from_stdio()`` or ``McpToolset.from_http()``.
        The caller is responsible for keeping the McpToolset context manager alive
        for as long as the registered tools are in use.

        Also adds the tools to *symbiote_id*'s authorized tools (keeping the
        ones it already had) so PolicyGate allows them::

            async with McpToolset.from_http("http://localhost:8000/mcp") as registry:
                tool_ids = kernel.load_mcp_tools(registry, symbiote_id="clark")

        Returns the list of tool_ids that were registered.
        """
        from symbiote.mcp.provider import McpToolProvider

        provider = McpToolProvider()
        tool_ids = provider.load(registry, self._tool_gateway)
        if tool_ids:
            self._environment.add_tools(symbiote_id, tool_ids)
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

    # ── Harness evolution ───────────────────────────────────────────

    def evolve_harness(
        self,
        symbiote_id: str,
        component: str,
        default_text: str | None = None,
        *,
        days: int = 7,
    ):
        """Run one evolution cycle for a harness component.

        Uses the evolver LLM (or main LLM as fallback) to propose an
        improved version of the specified text component. Without
        ``default_text`` the kernel's built-in text for the component is
        used (see ``harness_default_text``).

        Returns an EvolutionResult from HarnessEvolver.
        """
        from symbiote.harness.evolver import HarnessEvolver

        if default_text is None:
            default_text = self.harness_default_text(component)
        llm = self._evolver_llm or self._llm
        evolver = HarnessEvolver(
            storage=self._storage,
            versions=self._harness_versions,
            proposer_llm=llm,
        )
        return evolver.evolve(symbiote_id, component, default_text, days=days)

    @staticmethod
    def harness_default_text(component: str) -> str:
        """The built-in text an evolvable component starts from.

        Raises ``ValueError`` for a component that is not evolvable.
        """
        from symbiote.runners.chat import _TOOL_INSTRUCTIONS
        from symbiote.runners.loop_control import LoopController

        defaults = {
            "tool_instructions": _TOOL_INSTRUCTIONS,
            "injection_stagnation": LoopController._DEFAULT_STAGNATION_MSG,
            "injection_circuit_breaker": LoopController._DEFAULT_CIRCUIT_BREAKER_MSG,
        }
        if component not in defaults:
            raise ValueError(
                f"Unknown harness component {component!r}; expected one of {sorted(defaults)}"
            )
        return defaults[component]

    def tune_parameters(self, symbiote_id: str, *, days: int = 7, apply: bool = False):
        """Run the ``ParameterTuner`` over the last ``days`` of sessions.

        Returns its ``TuningResult`` (tier, adjustments, reasons). With
        ``apply=True`` the adjustments are written to the symbiote's config.
        """
        from symbiote.harness.tuner import ParameterTuner

        tuner = ParameterTuner(self._storage)
        result = tuner.analyze(symbiote_id, days=days)
        if apply and result.adjustments:
            result = tuner.apply(result, self._environment)
        return result

    def cross_learning_candidates(self, symbiote_id: str, *, min_overlap: float = 0.5):
        """Harness versions of other symbiotes with similar tools that
        ``symbiote_id`` could adopt (``CrossSymbioteLearner.find_candidates``).

        Always empty when ``KernelConfig.cross_symbiote_learning`` is False:
        nothing of another symbiote is read.
        """
        if not self._config.cross_symbiote_learning:
            return []
        from symbiote.harness.cross_learning import CrossSymbioteLearner

        learner = CrossSymbioteLearner(self._storage, self._harness_versions)
        return learner.find_candidates(symbiote_id, min_overlap=min_overlap)

    def check_harness_rollback(self, symbiote_id: str, component: str) -> bool:
        """Check and auto-rollback a harness version if it underperforms.

        Returns True if rollback was performed.
        """
        from symbiote.harness.evolver import HarnessEvolver

        evolver = HarnessEvolver(
            storage=self._storage,
            versions=self._harness_versions,
        )
        return evolver.auto_rollback_if_needed(symbiote_id, component)

    # ── Harness foundations (trace, score, failure memory) ───────────

    def _persist_trace(
        self, session_id: str, symbiote_id: str, trace: LoopTrace
    ) -> None:
        """Persist a LoopTrace to execution_traces table."""
        import json
        from datetime import UTC, datetime
        from uuid import uuid4

        self._storage.execute(
            "INSERT INTO execution_traces "
            "(id, session_id, symbiote_id, total_iterations, total_tool_calls, "
            "total_elapsed_ms, stop_reason, steps_json, tool_mode, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                session_id,
                symbiote_id,
                trace.total_iterations,
                trace.total_tool_calls,
                trace.total_elapsed_ms,
                trace.stop_reason,
                json.dumps(
                    [
                        {
                            **step,
                            "params": self._mask_secret_params(
                                step["tool_id"], step["params"]
                            ),
                        }
                        for step in (s.model_dump() for s in trace.steps)
                    ]
                ),
                trace.tool_mode,
                datetime.now(tz=UTC).isoformat(),
            ),
        )

    def _persist_score(
        self, session_id: str, symbiote_id: str, trace: LoopTrace | None
    ) -> None:
        """Compute and persist session score."""
        from datetime import UTC, datetime

        # Derive tool_mode and has_tools for mode-aware scoring
        tool_mode = trace.tool_mode if trace else "brief"
        has_tools = bool(self._tool_gateway and self._tool_gateway.list_tools())
        auto = compute_auto_score(trace, tool_mode=tool_mode, has_tools=has_tools)
        facts = (
            trace.stop_reason if trace else None,
            trace.total_iterations if trace else 0,
            trace.total_tool_calls if trace else 0,
            datetime.now(tz=UTC).isoformat(),
        )
        with self._storage.transaction():
            self._write_score(session_id, symbiote_id, auto, facts)

    def _write_score(
        self, session_id: str, symbiote_id: str, auto: float, facts: tuple
    ) -> None:
        from uuid import uuid4

        existing = self._storage.fetch_one(
            "SELECT id, user_score, scored_at_close FROM session_scores "
            "WHERE session_id = ?",
            (session_id,),
        )
        # The host's own score (``report_feedback``) given before the close
        # stays part of the final score.
        final = compute_final_score(
            auto, existing["user_score"] if existing is not None else None
        )
        if existing is not None:
            # A reopened session closed again, or a session the host scored
            # before it was closed: one score per session, updated.
            self._storage.execute(
                "UPDATE session_scores SET auto_score = ?, final_score = ?, "
                "stop_reason = ?, total_iterations = ?, total_tool_calls = ?, "
                "computed_at = ?, scored_at_close = 1 WHERE id = ?",
                (auto, final, *facts, existing["id"]),
            )
            if existing["scored_at_close"] is None or existing["scored_at_close"]:
                return
        else:
            self._storage.execute(
                "INSERT INTO session_scores "
                "(id, session_id, symbiote_id, auto_score, final_score, "
                "stop_reason, total_iterations, total_tool_calls, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), session_id, symbiote_id, auto, final, *facts),
            )

        # Track score per active harness version (for evolution rollback).
        # Only on the session's first close, so re-closing does not count it twice.
        # The session of an end user counts only when the symbiote learns
        # from those sessions.
        if not self._session_feeds_learning(session_id, symbiote_id):
            return
        from symbiote.harness.evolver import EVOLVABLE_COMPONENTS
        for component in EVOLVABLE_COMPONENTS:
            self._harness_versions.update_score(symbiote_id, component, final)

    def _session_feeds_learning(self, session_id: str, symbiote_id: str) -> bool:
        """Whether a session counts for what is learned for the symbiote as
        a whole (harness scores, tuner, evolver, dream, skill review).

        Always for a session without an end user; for the session of an end
        user, only when ``learn_from_subject_sessions`` is on.
        """
        if subject_of_session(self._storage, session_id) is None:
            return True
        return learns_from_subjects(self._storage, symbiote_id)

    def _generate_failure_memory(
        self, session_id: str, symbiote_id: str, trace: LoopTrace | None
    ) -> None:
        """Generate a procedural MemoryEntry when the tool loop fails."""
        from collections import Counter

        from symbiote.core.models import MemoryEntry

        if trace is None or trace.stop_reason in ("end_turn", CANCELLED, None):
            return  # nothing went wrong (a cancelled turn is the user's choice)

        if trace.stop_reason == "circuit_breaker":
            # Find the tool that triggered the breaker
            failed_tool = "unknown"
            for step in reversed(trace.steps):
                if not step.success:
                    failed_tool = step.tool_id
                    break
            content = (
                f"Tool '{failed_tool}' falhou múltiplas vezes consecutivas. "
                "Verificar pré-condições antes de chamar."
            )
        elif trace.stop_reason == "stagnation":
            last_tool = trace.steps[-1].tool_id if trace.steps else "unknown"
            content = (
                f"Loop estagnou chamando '{last_tool}' repetidamente. "
                "Verificar se a task já foi completada antes de chamar novamente."
            )
        elif trace.stop_reason == "max_iterations":
            tool_counts = Counter(s.tool_id for s in trace.steps)
            top_3 = ", ".join(t for t, _ in tool_counts.most_common(3))
            content = (
                f"Sessão esgotou {trace.total_iterations} iterações sem completar. "
                f"Tools mais usadas: {top_3}. "
                "Considerar decompor a task em passos menores."
            )
        else:
            return

        self._memory.store(MemoryEntry(
            symbiote_id=symbiote_id,
            session_id=session_id,
            type="procedural",
            scope="global",
            content=content,
            importance=0.7,
            source="system",
            tags=["harness_failure", trace.stop_reason],
        ))

    def on_shutdown(self, callback: Callable[[], None]) -> None:
        """Run ``callback()`` when the kernel shuts down, before the tool loop
        stops: for what an extension opened and must release (a browser and
        its tabs, a connection). Callbacks run in registration order; one
        that raises is logged and the others still run."""
        self._shutdown_callbacks.append(callback)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Wait for background work, then stop the tool loop and close storage.

        A storage adapter injected by the host is left open: the host owns it.

        Skill reviews and dream cycles run in daemon threads that write to
        the database. They get up to ``timeout`` seconds in total to finish;
        closing the storage under them lost their results (or, in a short
        CLI process, killed them before the LLM answered).
        """
        deadline = time.monotonic() + max(0.0, timeout)
        pending = self._background_threads()
        for thread in pending:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        still_running = [t.name for t in pending if t.is_alive()]
        if still_running:
            logging.getLogger(__name__).warning(
                "shutdown: background threads still running after %.1fs: %s",
                timeout,
                ", ".join(still_running),
            )
        if self._skills_sync is not None:
            skill_usage.remove_write_listener(self._skills_sync.on_sidecar_written)
            self._skills_sync.flush()  # usage counters not pushed yet
        callbacks, self._shutdown_callbacks = self._shutdown_callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logging.getLogger(__name__).exception("shutdown callback %r failed", callback)
        self._tool_gateway.close()
        if self._owns_storage:
            self._storage.close()

    def _background_threads(self) -> list[threading.Thread]:
        with self._dream_lock:
            threads = [t for t in self._dream_threads.values() if t.is_alive()]
        engines = list(self._background_review_by_symbiote.values())
        if self._background_review is not None:
            engines.append(self._background_review)
        for engine in engines:
            threads.extend(engine.active_threads())
        return threads
