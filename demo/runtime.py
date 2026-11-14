"""DemoRuntime — one embedded SymbioteKernel and everything wired around it.

This is the host-integration code the demo exists to show: build the kernel
with a real LLM, register tools of every kind, install the approval gate and
the observability hooks, inject the conversation history each turn, and close
sessions so the symbiote learns.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from demo import seed as seed_mod
from demo import settings as settings_mod
from demo.approvals import ApprovalBroker
from demo.events import TURN, EventBus, Turn
from demo.hooks import DemoHook
from demo.llm import RecordingLLM
from demo.tools_custom import TOOLS as CUSTOM_TOOLS
from symbiote.adapters.llm.forge import ForgeLLMAdapter
from symbiote.adapters.llm.symgateway import SymGatewayLLMAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

logger = logging.getLogger("demo")

BUILTIN_FOR_SOL = [
    "fs_read", "fs_write", "fs_list", "bash", "search_memories", "search_knowledge",
    "spawn", "skill_view", "skill_manage",
]
HISTORY_MESSAGES = 12


# The Symbiote session a background job (close_session: reflection, dream,
# skill review) works for, so its gateway calls join that conversation's
# work session. Chat turns carry it in TURN.
SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar("demo_scope", default=None)


def gateway_session_key() -> str | None:
    """One SymGateway work session per Symbiote session."""
    turn = TURN.get()
    return turn.session_id if turn else SCOPE.get()


def gateway_adapter(
    settings: settings_mod.Settings, model: str = "", *, title: str = "Symbiote demo"
) -> SymGatewayLLMAdapter:
    return SymGatewayLLMAdapter(
        settings.api_key,
        model or settings.model or "-",
        base_url=settings.gateway_url,
        project_slug=settings.symgateway_project or None,
        session_key=gateway_session_key,
        session_title=title,
    )


def build_adapter(settings: settings_mod.Settings, model: str) -> ForgeLLMAdapter | SymGatewayLLMAdapter:
    if settings.provider == "symgateway":
        return gateway_adapter(settings, model)
    # stream_tool_calls: the chat types the answer out even with native tools.
    return ForgeLLMAdapter(
        provider=settings.provider, model=model or None, api_key=settings.api_key,
        stream_tool_calls=True,
    )


def build_adapters(settings: settings_mod.Settings) -> tuple[Any, Any]:
    """(main, aux). Through the gateway both share one client and the same
    work sessions, so a conversation's background work lands in its session."""
    if settings.provider == "symgateway":
        main = gateway_adapter(settings, settings.model)
        return main, main.with_model(settings.aux_model or settings.model)
    return build_adapter(settings, settings.model), build_adapter(settings, settings.aux_model)


def list_gateway_models(settings: settings_mod.Settings) -> list[str]:
    adapter = gateway_adapter(settings)
    try:
        return adapter.list_models()
    finally:
        adapter.close()


def validate_key(settings: settings_mod.Settings) -> None:
    """One tiny real call; raises with the provider's message if it fails."""
    if settings.provider == "symgateway":
        adapter = gateway_adapter(settings, settings.model, title="Symbiote demo · validação")
        try:
            models = adapter.list_models()
            for model in {settings.model, settings.aux_model} - {""}:
                if model not in models:
                    raise ValueError(
                        f"o modelo {model!r} não está liberado para esta chave. "
                        f"Disponíveis: {', '.join(models) or 'nenhum'}"
                    )
            adapter.complete(
                [{"role": "user", "content": "Responda apenas: ok"}], config={"max_tokens": 5}
            )
        finally:
            adapter.close()
        return
    adapter = build_adapter(settings, settings.model)
    adapter.complete(
        [{"role": "user", "content": "Responda apenas: ok"}], config={"max_tokens": 5}
    )


class DemoRuntime:
    def __init__(self, bus: EventBus, *, data_dir: Path = settings_mod.DATA_DIR, base_url: str) -> None:
        self.bus = bus
        self.data_dir = data_dir
        self.base_url = base_url.rstrip("/")
        self.settings = settings_mod.load()
        self.kernel: SymbioteKernel | None = None
        self.llm: RecordingLLM | None = None
        self.aux_llm: RecordingLLM | None = None
        self.hook: DemoHook | None = None
        self.broker = ApprovalBroker(bus)
        self.approvals_enabled = True
        self.native_tools = True
        self.sol_id = ""
        self.researcher_id = ""
        self.mcp_tools: list[str] = []
        self.web_tools: list[str] = []
        self.web_error: str | None = None
        self.mcp_error: str | None = None
        self._mcp_task: asyncio.Task | None = None
        self._mcp_stop = asyncio.Event()
        self._sessions: dict[str, str] = {}
        self.pa_store: Any = None  # set by the server (personal API store)

    # ── paths ──────────────────────────────────────────────────────────

    @property
    def sandbox(self) -> Path:
        return self.data_dir / "sandbox"

    @property
    def shots_dir(self) -> Path:
        return self.data_dir / "shots"

    @property
    def ready(self) -> bool:
        return self.kernel is not None

    def require(self) -> SymbioteKernel:
        if self.kernel is None:
            raise RuntimeError("demo ainda não configurado: informe a chave do LLM")
        return self.kernel

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.settings.complete:
            return
        for path in (self.data_dir, self.sandbox, self.shots_dir):
            path.mkdir(parents=True, exist_ok=True)
        s = self.settings
        main, aux = build_adapters(s)
        self.llm = RecordingLLM(main, self.bus, role="main", model=s.model)
        self.aux_llm = RecordingLLM(aux, self.bus, role="aux", model=s.aux_model)

        config = KernelConfig(
            db_path=self.data_dir / "demo.db",
            skills_root=self.data_dir / "skills",
            fs_allowed_root=self.sandbox,
            context_budget=6000,
            tool_result_max_chars=4000,
            loop_context_budget=32_000,
        )
        kernel = await asyncio.to_thread(SymbioteKernel, config, self.llm)
        kernel.set_evolver_llm(self.aux_llm)
        kernel.set_semantic_llm(self.aux_llm)
        kernel.set_native_tools(self.native_tools)
        kernel.set_token_streaming("live")  # text reaches the chat as it is written

        self.hook = DemoHook(self.bus, kernel, self.shots_dir)
        kernel.hooks.add(self.hook)
        kernel.set_progress_callbacks(
            on_progress=lambda event, i, n: self.bus.publish("progress", event=event, index=i, total=n),
            on_stream=lambda text, i: self.bus.publish("iteration_text", iteration=i, text=text[:4000]),
            on_plan=lambda plan: self.bus.publish("plan", blocks=plan.blocks),
            on_block_complete=lambda block: self.bus.publish("block", **block.model_dump()),
        )
        self._apply_approvals(kernel)

        for descriptor, handler in CUSTOM_TOOLS:
            kernel.tool_gateway.register_descriptor(descriptor, handler)
        self._register_web(kernel)
        self.kernel = kernel
        await self._load_mcp(kernel)

        self.sol_id, self.researcher_id = seed_mod.seed(
            kernel,
            sandbox=self.sandbox,
            sol_tools=[*BUILTIN_FOR_SOL, *(d.tool_id for d, _ in CUSTOM_TOOLS), *self.mcp_tools],
            researcher_tools=["agora", *self.web_tools],
            prompt_caching=s.provider == "anthropic",
        )
        # Tools the presenter discovered and approved earlier come back on restart.
        loaded = kernel.load_discovered_tools(self.sol_id, base_url=self.base_url)
        self.bus.publish("runtime", state="ready", discovered_loaded=len(loaded))

    async def stop(self) -> None:
        self.broker.deny_all()
        await self._stop_mcp()
        if self.kernel is not None:
            await asyncio.to_thread(self.kernel.shutdown)
            self.kernel = None
        self._sessions.clear()

    async def reconfigure(self, settings: settings_mod.Settings) -> None:
        await self.stop()
        self.settings = settings
        await self.start()

    async def reset(self) -> None:
        """Delete everything the demo stored and seed again."""
        await self.stop()
        for child in self.data_dir.glob("*"):
            if child.name == "pa.db":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        if self.pa_store is not None:
            self.pa_store.reset()
        self.bus.publish("runtime", state="reset")
        await self.start()

    # ── wiring helpers ─────────────────────────────────────────────────

    def _apply_approvals(self, kernel: SymbioteKernel) -> None:
        kernel.set_approval_callback(self.broker.callback if self.approvals_enabled else None)

    def set_approvals(self, enabled: bool) -> None:
        self.approvals_enabled = enabled
        self._apply_approvals(self.require())
        if not enabled:
            self.broker.deny_all()

    def set_native_tools(self, enabled: bool) -> None:
        self.native_tools = enabled
        self.require().set_native_tools(enabled)

    def _register_web(self, kernel: SymbioteKernel) -> None:
        from symbiote.browser import register

        s = self.settings
        has_gateway = bool(s.symgateway_api_key and s.symgateway_base_url)
        try:
            register(
                kernel,
                search_backend="brave" if has_gateway else None,
                search_options={
                    "symgateway_base_url": s.symgateway_base_url or None,
                    "symgateway_api_key": s.symgateway_api_key or None,
                } if has_gateway else None,
                extract_backend="forge_scraper",
                browser_backend="chromium",
                policy={"blocklist": ["*.doubleclick.net", "*.ads.example"]},
            )
        except Exception as exc:  # missing extra or browser
            self.web_error = f"{type(exc).__name__}: {exc}"
            logger.warning("web tools unavailable: %s", self.web_error)
            return
        self.web_error = None
        self.web_tools = [
            t for t in kernel.tool_gateway.list_tools()
            if t.startswith(("web_", "browser_"))
        ]

    async def _load_mcp(self, kernel: SymbioteKernel) -> None:
        """Start the example MCP server and register its tools.

        The MCP session is an anyio context that must be entered and exited
        by the same task, so a dedicated task holds it open until ``stop``.
        """
        from symbiote.mcp import McpToolProvider

        loop = asyncio.get_running_loop()
        ready: asyncio.Future[Any] = loop.create_future()
        self._mcp_stop = asyncio.Event()

        async def hold() -> None:
            try:
                from forge_llm.mcp import McpToolset

                server = str(Path(__file__).with_name("mcp_server.py"))
                async with McpToolset.from_stdio(sys.executable, [server]) as registry:
                    ready.set_result(registry)
                    await self._mcp_stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                elif not isinstance(exc, asyncio.CancelledError):
                    logger.warning("MCP server stopped: %s", exc)

        self._mcp_task = asyncio.create_task(hold())
        try:
            registry = await asyncio.wait_for(asyncio.shield(ready), timeout=30)
            self.mcp_tools = McpToolProvider().load(registry, kernel.tool_gateway)
            self.mcp_error = None
        except BaseException as exc:
            self.mcp_tools = []
            self.mcp_error = f"{type(exc).__name__}: {exc}"
            logger.warning("MCP server unavailable: %s", self.mcp_error)
            await self._stop_mcp()

    async def _stop_mcp(self) -> None:
        task = getattr(self, "_mcp_task", None)
        if task is None:
            return
        self._mcp_stop.set()
        try:
            await asyncio.wait_for(task, timeout=10)
        except (TimeoutError, asyncio.CancelledError, Exception):
            task.cancel()
        self._mcp_task = None

    # ── sessions & turns ───────────────────────────────────────────────

    def symbiote_ids(self) -> dict[str, str]:
        return {"sol": self.sol_id, "researcher": self.researcher_id}

    def current_session(self, symbiote_id: str) -> str:
        kernel = self.require()
        session_id = self._sessions.get(symbiote_id)
        if session_id:
            session = kernel.get_session(session_id)
            if session is not None and session.status == "active":
                return session_id
        active = kernel.inspector.list_sessions(symbiote_id, status="active", limit=1)
        if active:
            session_id = active[0]["id"]
        else:
            session_id = kernel.start_session(symbiote_id, goal="Conversa no demo").id
            self.bus.publish("session", action="started", session_id=session_id, symbiote_id=symbiote_id)
        self._sessions[symbiote_id] = session_id
        return session_id

    def new_session(self, symbiote_id: str) -> str:
        """Start a fresh session without closing the current one (no learning)."""
        session_id = self.require().start_session(symbiote_id, goal="Conversa no demo").id
        self._sessions[symbiote_id] = session_id
        self.bus.publish("session", action="started", session_id=session_id, symbiote_id=symbiote_id)
        return session_id

    def history(self, session_id: str) -> str:
        """The recent transcript, as the kernel expects it in
        ``extra_context['conversation_history']`` (it does not inject it)."""
        messages = self.require().inspector.list_messages(session_id, limit=HISTORY_MESSAGES)
        lines = []
        for m in messages:
            label = "Usuário" if m["role"] == "user" else "Assistant"
            lines.append(f"{label}: {m['content']}")
        return "\n".join(lines)

    async def run_turn(self, symbiote_id: str, message: str) -> dict:
        kernel = self.require()
        session_id = self.current_session(symbiote_id)
        history = self.history(session_id)
        turn = Turn(uuid.uuid4().hex[:10], session_id, symbiote_id)
        token = TURN.set(turn)
        traces_before = {t["id"] for t in kernel.inspector.list_traces(session_id=session_id, limit=5)}
        self.bus.publish("turn_start", symbiote_id=symbiote_id, message=message,
                         history_messages=history.count("\n") + 1 if history else 0)
        try:
            response = await kernel.message_async(
                session_id,
                message,
                extra_context={"conversation_history": history} if history else None,
                on_token=lambda text: self.bus.publish("answer_delta", text=text),
            )
        except Exception as exc:
            self.bus.publish("turn_end", ok=False, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            TURN.reset(token)

        text = response.get("text", "") if isinstance(response, dict) else str(response)
        tool_results = response.get("tool_results", []) if isinstance(response, dict) else []
        traces = kernel.inspector.list_traces(session_id=session_id, limit=1)
        trace = traces[0] if traces and traces[0]["id"] not in traces_before else None
        summary = {
            "turn_id": turn.turn_id,
            "session_id": session_id,
            "text": text,
            "tool_results": [
                {k: r.get(k) for k in ("tool_id", "success", "error")} for r in tool_results
            ],
            "stop_reason": trace["stop_reason"] if trace else "end_turn",
            "iterations": trace["total_iterations"] if trace else 1,
            "tool_calls": trace["total_tool_calls"] if trace else 0,
            "elapsed_ms": trace["total_elapsed_ms"] if trace else None,
        }
        self.bus.publish("turn_end", ok=True, session_id=session_id, turn_id=turn.turn_id,
                         **{k: v for k, v in summary.items() if k not in ("session_id", "turn_id")})
        return summary

    async def close_session(self, symbiote_id: str) -> dict:
        """Close the current session (score, failure memory, reflection,
        handoff, skill review, dream) and report what changed."""
        kernel = self.require()
        session_id = self.current_session(symbiote_id)
        before = {m["id"] for m in kernel.inspector.list_memories(symbiote_id, limit=1000)}
        self.bus.publish("close_start", session_id=session_id, symbiote_id=symbiote_id)
        scope = SCOPE.set(session_id)
        try:
            session = await asyncio.to_thread(kernel.close_session, session_id)
        finally:
            SCOPE.reset(scope)
        memories = kernel.inspector.list_memories(symbiote_id, limit=1000)
        learned = [m for m in memories if m["id"] not in before]
        score = next(
            (s for s in kernel.inspector.list_scores(symbiote_id, limit=20) if s["session_id"] == session_id),
            None,
        )
        reflections = [
            r for r in kernel.inspector.list_reflections(symbiote_id, limit=10)
            if r["session_id"] == session_id
        ]
        # Always a fresh session: picking "the newest active one" would resume
        # an older conversation left open by "Nova sessão".
        new_id = self.new_session(symbiote_id)
        result = {
            "closed_session": session_id,
            "summary": session.summary,
            "score": score,
            "learned": learned,
            "reflection": reflections[0] if reflections else None,
            "reflection_mode": kernel.environment.get_config(symbiote_id).reflection_mode,
            "new_session": new_id,
        }
        self.bus.publish("learned", session_id=session_id, symbiote_id=symbiote_id,
                         learned=len(learned), score=score)
        return result
