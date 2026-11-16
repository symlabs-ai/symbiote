"""The demo's FastAPI app: page, setup, live events and the chat stream.

Local only. The server binds to 127.0.0.1, rejects other Host headers (DNS
rebinding) and requires a per-run token on every state-changing /api call, so
another page open in the same browser cannot drive the assistant.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from demo import settings as settings_mod
from demo.events import EventBus
from demo.personal_api import create_personal_api
from demo.previews import LinkPreviews
from demo.runtime import (
    DemoRuntime,
    check_connection,
    list_gateway_models,
    validate_key,
)

logger = logging.getLogger("demo")

STATIC = Path(__file__).with_name("static")
CHAT_JS = Path(__file__).resolve().parents[1] / "symbiote-ui" / "dist" / "symbiote-chat.js"
SYMBIOTES = ("sol", "researcher")


def sse(event: str, data: Any) -> str:
    return f"event:{event}\ndata:{json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def get_runtime(request: Request) -> DemoRuntime:
    return request.app.state.runtime


def get_kernel(request: Request) -> Any:
    runtime: DemoRuntime = request.app.state.runtime
    if not runtime.ready:
        raise HTTPException(409, "Demo ainda não configurado: informe a chave do LLM.")
    return runtime.kernel


def symbiote_id(runtime: DemoRuntime, which: str) -> str:
    ids = runtime.symbiote_ids()
    if which not in ids:
        raise HTTPException(404, f"symbiote desconhecido: {which}")
    return ids[which]


core = APIRouter(prefix="/api")


# ── status & setup ─────────────────────────────────────────────────────


@core.get("/status")
def status(request: Request) -> dict:
    rt = get_runtime(request)
    out: dict[str, Any] = {
        "ready": rt.ready,
        "settings": rt.settings.public(),
        "providers": list(settings_mod.PROVIDERS),
        "default_models": settings_mod.DEFAULT_MODELS,
        "env_path": str(settings_mod.ENV_PATH),
        "data_dir": str(rt.data_dir),
    }
    if rt.ready:
        kernel = rt.kernel
        out.update(
            symbiotes={
                key: {"id": sid, "name": kernel.get_symbiote(sid).name,
                      "session_id": rt.current_session(sid)}
                for key, sid in rt.symbiote_ids().items()
            },
            approvals_enabled=rt.approvals_enabled,
            approval_mode=rt.approval_mode,
            native_tools=rt.native_tools,
            web_tools=rt.web_tools,
            web_error=rt.web_error,
            chrome_tools=rt.chrome_tools,
            chrome_error=rt.chrome_error,
            mcp_tools=rt.mcp_tools,
            mcp_error=rt.mcp_error,
            pending_approvals=rt.broker.pending(),
        )
    return out


class SetupBody(BaseModel):
    provider: str
    api_key: str = ""
    model: str = ""
    aux_model: str = ""
    base_url: str = ""  # SymGateway only; empty = the public gateway
    project: str = ""   # SymGateway only: X-Project-Slug when the key is in a project


def _candidate(body: SetupBody) -> settings_mod.Settings:
    """Settings built from the form; an empty key reuses the saved one."""
    provider = body.provider.lower().strip()
    if provider not in settings_mod.PROVIDERS:
        raise HTTPException(422, "Escolha anthropic, openai ou symgateway.")
    main, aux = settings_mod.DEFAULT_MODELS[provider]
    current = settings_mod.load()
    api_key = body.api_key.strip() or (current.api_key if current.provider == provider else "")
    if not api_key:
        raise HTTPException(422, "Informe a chave da API.")
    gateway = provider == "symgateway"
    model = body.model.strip() or main
    base_url = body.base_url.strip().rstrip("/")
    if gateway and base_url and not base_url.startswith(("https://", "http://")):
        raise HTTPException(422, "A URL do SymGateway deve começar com https://.")
    return settings_mod.Settings(
        provider=provider,
        api_key=api_key,
        model=model,
        aux_model=body.aux_model.strip() or aux or model,
        port=current.port,
        symgateway_api_key=api_key if gateway else current.symgateway_api_key,
        symgateway_base_url=base_url if gateway else current.symgateway_base_url,
        symgateway_project=body.project.strip() if gateway else current.symgateway_project,
        search_objective=current.search_objective,
    )


@core.post("/setup/models")
async def setup_models(body: SetupBody) -> dict:
    """Models a SymGateway key may use (GET /v1/models)."""
    candidate = _candidate(body)
    if candidate.provider != "symgateway":
        return {"models": []}
    try:
        models = await asyncio.wait_for(asyncio.to_thread(list_gateway_models, candidate), timeout=30)
    except Exception as exc:
        raise HTTPException(400, f"Não consegui listar os modelos: {exc}") from exc
    return {"models": models}


@core.post("/setup/test")
async def setup_test(body: SetupBody) -> dict:
    """Try the form's settings with real calls. Nothing is saved or applied."""
    candidate = _candidate(body)
    try:
        return await asyncio.wait_for(asyncio.to_thread(check_connection, candidate), timeout=90)
    except TimeoutError as exc:
        raise HTTPException(504, "O teste passou de 90 s sem resposta do servidor.") from exc


@core.post("/setup")
async def setup(body: SetupBody, request: Request) -> dict:
    rt = get_runtime(request)
    candidate = _candidate(body)
    if candidate.provider == "symgateway" and not candidate.model:
        raise HTTPException(422, "Escolha o modelo principal na lista do SymGateway.")
    try:
        await asyncio.wait_for(asyncio.to_thread(validate_key, candidate), timeout=45)
    except Exception as exc:
        raise HTTPException(400, f"A chave não funcionou: {exc}") from exc
    updates = {
        "DEMO_PROVIDER": candidate.provider,
        candidate.key_var: candidate.api_key,
        "DEMO_MODEL": candidate.model,
        "DEMO_AUX_MODEL": candidate.aux_model,
    }
    if candidate.provider == "symgateway":
        updates["SYMGATEWAY_BASE_URL"] = candidate.symgateway_base_url
        updates["SYMGATEWAY_PROJECT_SLUG"] = candidate.symgateway_project
    settings_mod.save(updates)
    await rt.reconfigure(candidate)
    return {"ok": True, "settings": candidate.public()}


class ChromeBody(BaseModel):
    allowlist: str = ""  # comma- or line-separated domains
    session: str = "open"  # "open" (the Chrome already open) or "own"


@core.post("/setup/chrome/test")
async def setup_chrome_test(request: Request) -> dict:
    """Can Sol reach its Chrome? Nothing is saved, opened or connected."""
    return await asyncio.to_thread(get_runtime(request).chrome_status)


@core.post("/setup/chrome")
async def setup_chrome(body: ChromeBody, request: Request, _k: Any = Depends(get_kernel)) -> dict:
    """Change the sites Sol's Chrome may open (restarts the demo's kernel)."""
    rt = get_runtime(request)
    allowlist = settings_mod.parse_allowlist(body.allowlist)
    if not allowlist:
        raise HTTPException(422, "Diga ao menos um site que o Chrome do Sol pode abrir.")
    if body.session not in settings_mod.CHROME_SESSIONS:
        raise HTTPException(422, "Escolha o Chrome já aberto (open) ou um separado (own).")
    settings_mod.save({"DEMO_CHROME_ALLOWLIST": ",".join(allowlist), "DEMO_CHROME_SESSION": body.session})
    await rt.reconfigure(dataclasses.replace(rt.settings, chrome_allowlist=allowlist, chrome_session=body.session))
    return {"ok": True, "settings": rt.settings.public(), "chrome_error": rt.chrome_error}


@core.post("/reset")
async def reset(request: Request, _k: Any = Depends(get_kernel)) -> dict:
    await get_runtime(request).reset()
    return {"ok": True}


# ── live events ────────────────────────────────────────────────────────


@core.get("/events")
async def events(request: Request, since: int = 0) -> StreamingResponse:
    bus: EventBus = request.app.state.bus

    async def stream():
        yield "retry: 2000\n\n"
        async for event in bus.subscribe(since):
            if await request.is_disconnected():
                break
            # No "event:" field: the page gets every type in one onmessage.
            yield f"id:{event['seq']}\ndata:{json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── chat ───────────────────────────────────────────────────────────────


class ChatBody(BaseModel):
    message: str
    symbiote: str = "sol"


@core.post("/chat/stream")
async def chat_stream(body: ChatBody, request: Request, kernel: Any = Depends(get_kernel)) -> StreamingResponse:
    """SSE in the format <symbiote-chat> understands: text_delta (live token
    streaming) and tool_start / tool_done while the turn runs, then
    response_done (or error), whose text replaces the deltas."""
    rt = get_runtime(request)
    sid = symbiote_id(rt, body.symbiote)
    bus: EventBus = request.app.state.bus
    out: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
    turn_ids: set[str] = set()
    awaiting: set[str] = set()  # tools whose badge the approval request already created

    async def relay() -> None:
        async for event in bus.subscribe(since=bus.recent(limit=1)[-1]["seq"] if bus.recent(limit=1) else 0):
            if event["type"] == "turn_start" and event.get("symbiote_id") == sid and not turn_ids:
                turn_ids.add(event["turn_id"])
                continue
            if event.get("turn_id") not in turn_ids:
                continue
            if event["type"] == "tool_start":
                if event["tool_id"] in awaiting:
                    awaiting.discard(event["tool_id"])
                    continue
                out.put_nowait(("tool_start", {"tool_id": event["tool_id"], "name": event["tool_id"],
                                               "detail": json.dumps(event.get("params"), ensure_ascii=False)[:300]}))
            elif event["type"] == "tool_end":
                out.put_nowait(("tool_done", {"tool_id": event["tool_id"], "name": event["tool_id"],
                                              "success": event["success"], "error": event.get("error")}))
            elif event["type"] == "approval_request":
                awaiting.add(event["tool_id"])
                out.put_nowait(("tool_start", {"tool_id": event["tool_id"], "name": f"{event['tool_id']} ⏳ aprovação",
                                               "detail": "aguardando aprovação"}))
            elif event["type"] == "answer_delta":
                out.put_nowait(("text_delta", {"text": event["text"]}))
            elif event["type"] == "approval_result" and not event["approved"]:
                awaiting.discard(event["tool_id"])
                # A denied call never reaches the gateway, so no tool_end follows.
                out.put_nowait(("tool_done", {"tool_id": event["tool_id"], "name": f"{event['tool_id']} (negada)",
                                              "success": False, "error": "negada na aprovação"}))

    async def run() -> None:
        relay_task = asyncio.create_task(relay())
        await asyncio.sleep(0)  # let the relay subscribe before the turn starts
        try:
            summary = await rt.run_turn(sid, body.message)
            await asyncio.sleep(0.05)  # flush tool events published from threads
            out.put_nowait(("response_done", {"text": summary["text"], "tool_results": summary["tool_results"],
                                              "turn_id": summary["turn_id"]}))
        except Exception as exc:
            logger.exception("turn failed")
            out.put_nowait(("error", {"message": f"Erro no turno: {type(exc).__name__}: {exc}"}))
        finally:
            relay_task.cancel()
            out.put_nowait(None)

    task = asyncio.create_task(run())
    request.app.state.turn_tasks.add(task)
    task.add_done_callback(request.app.state.turn_tasks.discard)

    async def stream():
        while True:
            item = await out.get()
            if item is None:
                break
            yield sse(*item)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@core.get("/chat/history")
def chat_history(request: Request, symbiote: str = "sol", kernel: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    session_id = rt.current_session(symbiote_id(rt, symbiote))
    return {"session_id": session_id,
            "messages": [{"role": m["role"], "content": m["content"]}
                         for m in kernel.inspector.list_messages(session_id)]}


class SymbioteBody(BaseModel):
    symbiote: str = "sol"


@core.post("/session/new")
def session_new(body: SymbioteBody, request: Request, _k: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    return {"session_id": rt.new_session(symbiote_id(rt, body.symbiote))}


@core.post("/session/close")
async def session_close(body: SymbioteBody, request: Request, _k: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    result = await rt.close_session(symbiote_id(rt, body.symbiote))
    rt.last_close = result
    return result


class FeedbackBody(BaseModel):
    session_id: str
    score: float


@core.post("/feedback")
def feedback(body: FeedbackBody, kernel: Any = Depends(get_kernel)) -> dict:
    kernel.report_feedback(body.session_id, max(0.0, min(1.0, body.score)), source="demo")
    return {"ok": True}


class ToggleBody(BaseModel):
    enabled: bool


class ApprovalModeBody(BaseModel):
    mode: Literal["low", "medium", "high", "none", "allow_all"]


@core.post("/approvals/mode")
def approvals_mode(body: ApprovalModeBody, request: Request, _k: Any = Depends(get_kernel)) -> dict:
    rt = get_runtime(request)
    rt.set_approval_mode(body.mode)
    return {"mode": rt.approval_mode, "enabled": rt.approvals_enabled}


# Declared before /approvals/{request_id}: routes match in order, and that
# one would take "toggle" as the id of an approval request.
@core.post("/approvals/toggle")
def approvals_toggle(body: ToggleBody, request: Request, _k: Any = Depends(get_kernel)) -> dict:
    get_runtime(request).set_approvals(body.enabled)
    return {"enabled": body.enabled}


class DecisionBody(BaseModel):
    approved: bool


@core.post("/approvals/{request_id}")
def decide(request_id: str, body: DecisionBody, request: Request) -> dict:
    if not get_runtime(request).broker.decide(request_id, body.approved):
        raise HTTPException(404, "pedido de aprovação não encontrado (já expirou?)")
    return {"ok": True}


# ── link previews ──────────────────────────────────────────────────────


class PreviewBody(BaseModel):
    url: str


@core.post("/preview")
async def preview(body: PreviewBody, request: Request) -> dict:
    """Title, site and thumbnail of an external link shown in the chat."""
    return {"preview": await request.app.state.previews.resolve(body.url)}


@core.get("/preview/image/{key}")
def preview_image(key: str, request: Request) -> FileResponse:
    """A thumbnail already resolved by /preview; never fetches anything."""
    found = request.app.state.previews.image(key)
    if found is None:
        raise HTTPException(404, "miniatura não encontrada")
    path, content_type = found
    return FileResponse(path, media_type=content_type, headers={
        "Cache-Control": "private, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    })


# ── app ────────────────────────────────────────────────────────────────


def create_app(*, port: int, data_dir: Path = settings_mod.DATA_DIR) -> FastAPI:
    data_dir.mkdir(parents=True, exist_ok=True)
    bus = EventBus()
    base_url = f"http://127.0.0.1:{port}"
    runtime = DemoRuntime(bus, data_dir=data_dir, base_url=base_url)
    personal_api = create_personal_api(data_dir / "pa.db")
    runtime.pa_store = personal_api.state.store
    token = secrets.token_urlsafe(24)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.bind(asyncio.get_running_loop())
        try:
            await runtime.start()
        except Exception:
            logger.exception("runtime failed to start; the setup screen will show")
        yield
        for task in list(app.state.turn_tasks):
            task.cancel()
        await runtime.stop()
        personal_api.state.store.close()

    app = FastAPI(title="Symbiote Demo", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.bus = bus
    app.state.runtime = runtime
    app.state.token = token
    app.state.turn_tasks = set()
    app.state.previews = LinkPreviews(data_dir / "previews")
    runtime.last_close = None
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"detail": "host não permitido"}, status_code=403)
        if (
            request.url.path.startswith("/api/")
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and not secrets.compare_digest(request.headers.get("x-demo-token", ""), token)
        ):
            return JSONResponse({"detail": "token do demo ausente ou inválido"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith(("/static/", "/vendor/")):
            # Revalidate every load: a stale module after an update breaks the page.
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ValueError)
    async def value_error(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    from demo import routes

    app.include_router(core)
    app.include_router(routes.router)
    app.mount("/pa", personal_api)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    (data_dir / "shots").mkdir(exist_ok=True)
    app.mount("/shots", StaticFiles(directory=data_dir / "shots"), name="shots")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__DEMO_TOKEN__", token),
                            headers={"Cache-Control": "no-store"})

    @app.get("/vendor/symbiote-chat.js")
    def chat_js() -> FileResponse:
        return FileResponse(CHAT_JS, media_type="application/javascript",
                            headers={"Cache-Control": "no-store"})

    return app
