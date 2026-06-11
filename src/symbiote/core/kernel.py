"""SymbioteKernel — the central orchestrator that composes all components."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from symbiote.adapters.export.markdown import ExportService
from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.background_review import BackgroundReviewEngine
from symbiote.core.capabilities import CapabilitySurface
from symbiote.core.context import ContextAssembler
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.hooks import CompositeHook
from symbiote.core.identity import IdentityManager
from symbiote.core.models import Session, Symbiote
from symbiote.core.ports import LLMPort, SessionRecallPort
from symbiote.core.reflection import ReflectionEngine
from symbiote.core.scoring import compute_auto_score, compute_final_score
from symbiote.core.session import SessionManager
from symbiote.core.session_lock import SessionLock
from symbiote.discovery.loader import DiscoveredToolLoader
from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.dream.engine import DreamEngine
from symbiote.dream.models import DreamReport
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.harness.versions import HarnessVersionRepository
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.process.engine import ProcessEngine
from symbiote.runners.base import LoopTrace, RunnerRegistry
from symbiote.runners.chat import ChatRunner
from symbiote.runners.process import ProcessRunner
from symbiote.runners.subagent import SubagentManager
from symbiote.skills import tool as skill_tool_module
from symbiote.skills.loader import SkillsLoader
from symbiote.skills.store import SkillsStore
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

        # Register memory/knowledge on-demand tools (always available,
        # authorized per-symbiote only when context_mode == "on_demand")
        self._tool_gateway.register_memory_tools(self._memory, self._knowledge)

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
        )

        # Runners
        self._runner_registry = RunnerRegistry()
        if llm is not None:
            self._runner_registry.register(
                ChatRunner(
                    llm,
                    tool_gateway=self._tool_gateway,
                    tool_result_max_chars=config.tool_result_max_chars,
                    # Resolver form: ChatRunner is per-kernel, but
                    # skill_review_enabled is per-symbiote. The resolver
                    # returns the engine (or None) for the symbiote driving
                    # the current turn.
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

        # Optional session recall (host provides implementation)
        self._session_recall: SessionRecallPort | None = None

        # Last loop trace — set by message(), consumed by close_session()
        self._last_trace: LoopTrace | None = None
        self._last_trace_session: str | None = None

        # Last long-run handoff — set by message(), persisted by close_session()
        self._last_handoff: dict | None = None
        self._last_handoff_session: str | None = None

        # Optional evolver LLM (host provides, can be different from main LLM)
        self._evolver_llm: LLMPort | None = None

        # Back-reference so EnvironmentManager.configure() can validate
        # reflection_mode='llm' against the kernel's _evolver_llm presence.
        self._environment._kernel = self

        # Dream engine (lazy — created on first use)
        self._dream_engine: DreamEngine | None = None

        # Skill self-improvement wiring (Sprint 4)
        # Lazily resolved when first needed so a kernel with no skills/ root
        # on disk still works (you_news pattern: host owns the layout).
        self._skills_store: SkillsStore | None = None
        self._skills_loader: SkillsLoader | None = None
        self._background_review: BackgroundReviewEngine | None = None
        # Guards the lazy construction of _background_review against the
        # race where ChatRunner (mid-session nudge) and close_session
        # both hit `_background_review_for` simultaneously for the same
        # symbiote — without the lock, both threads see None and build
        # two engines, leaking one. Acquired only on the cold path.
        self._background_review_lock = threading.Lock()
        self._setup_skills_wiring()

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
        callback: Callable[[str, dict, str], bool] | None,
    ) -> None:
        """Install a human-in-the-loop approval gate on the chat runner.

        The kernel builds its ChatRunner internally, so this is the supported
        way for an embedded host to wire an approval callback that ``message()``
        / ``message_async()`` actually use. Without it, the gate is inert and
        high-risk tools execute unchecked.

        The callback receives ``(tool_id, params, risk_level)`` and returns
        ``True`` to allow or ``False`` to deny. It is invoked **only** for
        tools whose ``risk_level == "high"`` (see ``ChatRunner._check_approval``).
        Pass ``None`` to remove a previously installed gate.

        Raises:
            RuntimeError: if no chat runner is registered (kernel built
                without an LLM).
        """
        runner = self._runner_registry.select("chat")
        if runner is None or not hasattr(runner, "_on_before_tool_call"):
            raise RuntimeError(
                "No chat runner available to attach an approval callback "
                "(kernel was constructed without an LLM)."
            )
        runner._on_before_tool_call = callback  # type: ignore[attr-defined]

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
        if runner is None or not hasattr(runner, "_native_tools"):
            raise RuntimeError(
                "No chat runner available to set native_tools "
                "(kernel was constructed without an LLM)."
            )
        runner._native_tools = enabled  # type: ignore[attr-defined]

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
        llm_config: dict | None = None,
    ) -> str:
        """Add user message, run chat capability, add assistant message, return response.

        Uses per-session locking: concurrent requests on the same session are
        serialized, while different sessions process in parallel.

        Args:
            llm_config: Optional dict of per-call LLM overrides (e.g.
                ``{"mode": "high"}``). Propagated all the way to the
                runner's ``self._llm.stream(messages, config=...)`` call
                so a single message can use a different effort / model /
                temperature than the session's default — without
                affecting other sessions that share the same adapter.
        """
        with self._session_lock.acquire(session_id):
            return self._message_inner(
                session_id, content, extra_context, llm_config=llm_config
            )

    def _message_inner(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        llm_config: dict | None = None,
    ) -> str:
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise EntityNotFoundError("Session", session_id)
        symbiote_id = row["symbiote_id"]

        # S-01: Inject previous handoff on session start (first message only)
        extra_context = self._inject_handoff_if_resuming(
            session_id, symbiote_id, extra_context
        )

        self._sessions.add_message(session_id, "user", content)

        response = self._capabilities.chat(
            symbiote_id, session_id, content,
            extra_context=extra_context,
            llm_config=llm_config,
        )

        if isinstance(response, dict):
            text = response.get("text", str(response))
        else:
            text = response

        self._sessions.add_message(session_id, "assistant", text)

        # Capture loop trace from last RunResult for close_session()
        trace = self._capabilities.last_loop_trace
        if trace is not None:
            self._last_trace = trace
            self._last_trace_session = session_id
            self._persist_trace(session_id, symbiote_id, trace)

        # Capture handoff data from last RunResult for close_session()
        handoff = self._capabilities.last_handoff_data
        if handoff is not None:
            self._last_handoff = handoff
            self._last_handoff_session = session_id

        return response

    async def message_async(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
    ) -> str:
        """Async variant of message() — supports async tool handlers and on_token streaming.

        Uses per-session async locking: concurrent requests on the same session
        are serialized, while different sessions process in parallel.

        ``llm_config`` mirrors the sync ``message()`` semantics.
        """
        async with self._session_lock.acquire_async(session_id):
            return await self._message_async_inner(
                session_id, content, extra_context, on_token, llm_config=llm_config
            )

    async def _message_async_inner(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        on_token: Callable[[str], None] | None = None,
        llm_config: dict | None = None,
    ) -> str:
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise EntityNotFoundError("Session", session_id)
        symbiote_id = row["symbiote_id"]

        # S-01: Inject previous handoff on session start (first message only)
        extra_context = self._inject_handoff_if_resuming(
            session_id, symbiote_id, extra_context
        )

        self._sessions.add_message(session_id, "user", content)

        response = await self._capabilities.chat_async(
            symbiote_id,
            session_id,
            content,
            extra_context=extra_context,
            on_token=on_token,
            llm_config=llm_config,
        )

        if isinstance(response, dict):
            text = response.get("text", str(response))
        else:
            text = response

        self._sessions.add_message(session_id, "assistant", text)

        # Capture loop trace from last RunResult for close_session()
        trace = self._capabilities.last_loop_trace
        if trace is not None:
            self._last_trace = trace
            self._last_trace_session = session_id
            self._persist_trace(session_id, symbiote_id, trace)

        # Capture handoff data from last RunResult for close_session()
        handoff = self._capabilities.last_handoff_data
        if handoff is not None:
            self._last_handoff = handoff
            self._last_handoff_session = session_id

        return response

    def close_session(self, session_id: str) -> Session:
        """Run reflection, compute score, persist failure memory, then close."""
        row = self._storage.fetch_one(
            "SELECT symbiote_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is not None:
            symbiote_id = row["symbiote_id"]

            # 1. Compute and persist session score
            trace = self._last_trace if self._last_trace_session == session_id else None
            self._persist_score(session_id, symbiote_id, trace)

            # 2. Generate failure MemoryEntry if loop didn't complete
            self._generate_failure_memory(session_id, symbiote_id, trace)

            # 3. Reflection — build engine matching this symbiote's config
            self._reflection_for(symbiote_id).reflect_session(session_id, symbiote_id)

            # 4. S-02: Persist long-run handoff as memory entry
            self._persist_handoff_memory(session_id, symbiote_id)

            # 5. Skill self-improvement (Sprint 4) — fire daemon thread if
            # the symbiote opted in via skill_review_enabled. Never blocks
            # close_session; failures are logged inside the engine.
            review = self._background_review_for(symbiote_id)
            if review is not None:
                review.spawn_final(session_id, symbiote_id)

            # 6. Dream mode — maybe trigger background dream
            self._maybe_dream(symbiote_id)

            # Clear trace state
            if self._last_trace_session == session_id:
                self._last_trace = None
                self._last_trace_session = None

        return self._sessions.close(session_id)

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
        ``_get_or_create_dream_engine`` make all these features no-op
        when wiring is disabled — no silent partial activation.
        """
        cfg = self._config
        # Default: ``{db_path.parent}/skills/`` — same directory the
        # ``SkillsLoader`` scans natively. Human-curated and agent-created
        # skills coexist; the ``.skill_meta.json`` sidecar (Sprint 3)
        # provides the distinction, so we don't need a physical subfolder.
        host_requested_skills = cfg.skills_root is not None
        agent_root = cfg.skills_root or (Path(cfg.db_path).parent / "skills")
        roots: list[Path] = [agent_root, *cfg.skills_extra_roots]
        protected = list(cfg.skills_protected_roots)

        try:
            self._skills_store = SkillsStore(roots=roots, protected_roots=protected)
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
        self._skills_loader = SkillsLoader(*loader_roots)

        # Always register the tool — but it stays unauthorized for each
        # symbiote until the host explicitly opts in via
        # `kernel.environment.configure(tools=[..., 'skill_manage'])`.
        skill_tool_module.register(self._tool_gateway, self._skills_store)

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
        cfg = self._environment.get_config(symbiote_id)
        if cfg is None or not cfg.skill_review_enabled:
            return None
        if self._evolver_llm is None:
            # EnvironmentManager.configure() guards against this, but defend
            # in depth: a row inserted directly into the DB could bypass it.
            return None
        if self._skills_store is None or self._skills_loader is None:
            return None
        # Keep the loader's auto-promote threshold in sync with cfg on each
        # call. Cheap (just sets an int) and idempotent.
        self._skills_loader.set_auto_promote_threshold(
            cfg.skill_auto_promote_threshold
        )
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
                    store=self._skills_store,
                    loader=self._skills_loader,
                    max_active_skills=cfg.max_active_skills,
                    max_quarantine_skills=cfg.max_quarantine_skills,
                    storage=self._storage,
                )
        return self._background_review

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

        if mode == "keyword":
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
        """S-01: On first message of a session, inject the most recent handoff."""
        msg_row = self._storage.fetch_one(
            "SELECT COUNT(*) as c FROM messages WHERE session_id = ?", (session_id,)
        )
        if msg_row is None or msg_row["c"] > 0:
            return extra_context

        handoff_entries = self._memory.get_by_category(symbiote_id, "handoff", limit=1)
        if not handoff_entries:
            return extra_context

        import json
        try:
            hd = json.loads(handoff_entries[0].content)
            return {**(extra_context or {}), "previous_handoff": hd}
        except Exception:
            return extra_context

    def _persist_handoff_memory(self, session_id: str, symbiote_id: str) -> None:
        """S-02: Persist long-run handoff_data as a MemoryEntry(category='handoff')."""
        if self._last_handoff is None or self._last_handoff_session != session_id:
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
            content=json.dumps(self._last_handoff, ensure_ascii=False),
            importance=1.0,
            created_at=_utcnow(),
        )
        self._memory.store(entry)
        self._last_handoff = None
        self._last_handoff_session = None

    # ── Dream Mode ─────────────────────────────────────────────────────

    def _get_or_create_dream_engine(self, cfg) -> DreamEngine:
        if self._dream_engine is None:
            self._dream_engine = DreamEngine(
                storage=self._storage,
                memory=self._memory,
                llm=self._evolver_llm or self._llm,
                max_llm_calls=cfg.dream_max_llm_calls,
                min_sessions=cfg.dream_min_sessions,
                skills_loader=self._skills_loader,
                skill_quarantine_timeout_days=cfg.skill_quarantine_timeout_days,
            )
        return self._dream_engine

    def _maybe_dream(self, symbiote_id: str) -> None:
        """Trigger a background dream cycle if conditions are met."""
        cfg = self._environment.get_config(symbiote_id)
        if cfg is None or cfg.dream_mode == "off":
            return
        engine = self._get_or_create_dream_engine(cfg)
        if engine.should_dream(symbiote_id, cfg.dream_mode):
            engine.dream_async(symbiote_id, cfg.dream_mode)

    def dream(self, symbiote_id: str, *, dry_run: bool = False) -> DreamReport:
        """Run a dream cycle synchronously (for CLI / manual invocation)."""

        cfg = self._environment.get_config(symbiote_id)
        mode = cfg.dream_mode if cfg and cfg.dream_mode != "off" else "light"
        engine = DreamEngine(
            storage=self._storage,
            memory=self._memory,
            llm=self._evolver_llm or self._llm,
            max_llm_calls=cfg.dream_max_llm_calls if cfg else 10,
            min_sessions=1,  # manual trigger ignores min_sessions
            dry_run=dry_run,
            skills_loader=self._skills_loader,
            skill_quarantine_timeout_days=(
                cfg.skill_quarantine_timeout_days if cfg else None
            ),
        )
        return engine.dream(symbiote_id, mode)

    def report_feedback(
        self, session_id: str, score: float, source: str = "user"
    ) -> None:
        """Report user feedback for a session, updating the final score.

        The host calls this when it has a quality signal (user thumbs up,
        task completion, etc.).  The auto_score is preserved; final_score
        is recomputed as a weighted combination.
        """
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
                "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
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

    # ── Harness evolution ───────────────────────────────────────────

    def evolve_harness(
        self, symbiote_id: str, component: str, default_text: str, *, days: int = 7
    ):
        """Run one evolution cycle for a harness component.

        Uses the evolver LLM (or main LLM as fallback) to propose an
        improved version of the specified text component.

        Returns an EvolutionResult from HarnessEvolver.
        """
        from symbiote.harness.evolver import HarnessEvolver

        llm = self._evolver_llm or self._llm
        evolver = HarnessEvolver(
            storage=self._storage,
            versions=self._harness_versions,
            proposer_llm=llm,
        )
        return evolver.evolve(symbiote_id, component, default_text, days=days)

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
                json.dumps([s.model_dump() for s in trace.steps]),
                trace.tool_mode,
                datetime.now(tz=UTC).isoformat(),
            ),
        )

    def _persist_score(
        self, session_id: str, symbiote_id: str, trace: LoopTrace | None
    ) -> None:
        """Compute and persist session score."""
        from datetime import UTC, datetime
        from uuid import uuid4

        # Derive tool_mode and has_tools for mode-aware scoring
        tool_mode = trace.tool_mode if trace else "brief"
        has_tools = bool(self._tool_gateway and self._tool_gateway.list_tools())
        auto = compute_auto_score(trace, tool_mode=tool_mode, has_tools=has_tools)
        final = compute_final_score(auto)
        self._storage.execute(
            "INSERT OR REPLACE INTO session_scores "
            "(id, session_id, symbiote_id, auto_score, final_score, "
            "stop_reason, total_iterations, total_tool_calls, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                session_id,
                symbiote_id,
                auto,
                final,
                trace.stop_reason if trace else None,
                trace.total_iterations if trace else 0,
                trace.total_tool_calls if trace else 0,
                datetime.now(tz=UTC).isoformat(),
            ),
        )

        # Track score per active harness version (for evolution rollback)
        from symbiote.harness.evolver import EVOLVABLE_COMPONENTS
        for component in EVOLVABLE_COMPONENTS:
            self._harness_versions.update_score(symbiote_id, component, final)

    def _generate_failure_memory(
        self, session_id: str, symbiote_id: str, trace: LoopTrace | None
    ) -> None:
        """Generate a procedural MemoryEntry when the tool loop fails."""
        from collections import Counter

        from symbiote.core.models import MemoryEntry

        if trace is None or trace.stop_reason in ("end_turn", None):
            return

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

    def shutdown(self) -> None:
        """Close the storage adapter."""
        self._storage.close()
