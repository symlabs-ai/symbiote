"""HTTP API — FastAPI application for Symbiote kernel."""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query

# ── FastAPI app ───────────────────────────────────────────────────────────
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKey, APIKeyManager
from symbiote.api.middleware import require_admin, require_auth, set_key_manager
from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import EntityNotFoundError, SymbioteError, ValidationError
from symbiote.core.identity import IdentityManager
from symbiote.core.kernel import SymbioteKernel
from symbiote.core.ports import LLMPort
from symbiote.core.session import SessionManager
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.memory.store import MemoryStore

app = FastAPI(title="Symbiote API", version="0.2.0")


@app.get("/health")
async def health() -> dict:
    """Health check endpoint for Docker/load balancers."""
    return {"status": "ok"}


@app.exception_handler(EntityNotFoundError)
async def entity_not_found_handler(request: Request, exc: EntityNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(SymbioteError)
async def symbiote_error_handler(request: Request, exc: SymbioteError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ── Request / Response schemas ────────────────────────────────────────────


class CreateSymbioteRequest(BaseModel):
    name: str
    role: str
    persona_json: dict | None = None


class SymbioteResponse(BaseModel):
    id: str
    name: str
    role: str
    status: str


class CreateSessionRequest(BaseModel):
    symbiote_id: str
    goal: str | None = None
    external_key: str | None = None


class SessionResponse(BaseModel):
    id: str
    symbiote_id: str
    goal: str | None = None
    external_key: str | None = None
    status: str
    summary: str | None = None


class CreateMessageRequest(BaseModel):
    role: str
    content: str


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    created_at: str


class MemoryEntryResponse(BaseModel):
    id: str
    symbiote_id: str
    session_id: str | None = None
    type: str
    scope: str
    content: str
    tags: list[str] = []
    importance: float
    source: str


class RegisterToolRequest(BaseModel):
    tool_id: str
    name: str
    description: str
    parameters: dict = {}
    http_method: str = "GET"
    url_template: str
    headers: dict[str, str] = {}
    timeout: float = 30.0
    body_template: dict | None = None


class ToolDescriptorResponse(BaseModel):
    tool_id: str
    name: str
    description: str
    parameters: dict = {}
    handler_type: str


class ChatRequest(BaseModel):
    content: str
    extra_context: dict | None = None
    generation_settings: dict | None = None


class ChatResponse(BaseModel):
    response: str | dict
    session_id: str


class ToolExecRequest(BaseModel):
    params: dict[str, Any] = {}


class ToolExecResponse(BaseModel):
    tool_id: str
    success: bool
    output: Any = None
    error: str | None = None


# ── Dependency injection ──────────────────────────────────────────────────

_adapter: SQLiteAdapter | None = None
_tool_gateway: ToolGateway | None = None
_kernel: SymbioteKernel | None = None
_key_manager: APIKeyManager | None = None


def _resolve_llm() -> LLMPort | None:
    """Resolve LLM adapter from env."""
    provider = os.environ.get("SYMBIOTE_LLM_PROVIDER")
    if not provider or provider == "mock":
        return None
    from symbiote.adapters.llm.forge import ForgeLLMAdapter

    return ForgeLLMAdapter(provider=provider)


def get_adapter() -> SQLiteAdapter:
    """Return the singleton SQLiteAdapter, creating it on first call."""
    global _adapter
    if _adapter is None:
        config = KernelConfig()
        _adapter = SQLiteAdapter(db_path=config.db_path)
        _adapter.init_schema()

        # Init API key schema
        global _key_manager
        _key_manager = APIKeyManager(_adapter)
        _key_manager.init_schema()
        set_key_manager(_key_manager)

    return _adapter


def get_kernel() -> SymbioteKernel:
    """Return the singleton SymbioteKernel with LLM."""
    global _kernel
    if _kernel is None:
        config = KernelConfig()
        llm = _resolve_llm()
        _kernel = SymbioteKernel(config=config, llm=llm)

        # Init API key schema on the kernel's storage
        global _key_manager
        _key_manager = APIKeyManager(_kernel._storage)
        _key_manager.init_schema()
        set_key_manager(_key_manager)

    return _kernel


def get_identity_manager(
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> IdentityManager:
    return IdentityManager(storage=adapter)


def get_session_manager(
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> SessionManager:
    return SessionManager(storage=adapter)


def get_memory_store(
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> MemoryStore:
    return MemoryStore(storage=adapter)


def get_env_manager(
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


def get_tool_gateway(
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> ToolGateway:
    global _tool_gateway
    if _tool_gateway is None:
        env = EnvironmentManager(storage=adapter)
        gate = PolicyGate(env_manager=env, storage=adapter)
        _tool_gateway = ToolGateway(policy_gate=gate)
    return _tool_gateway


# ── Symbiote endpoints ───────────────────────────────────────────────────


@app.post("/symbiotes", status_code=201, response_model=SymbioteResponse)
def create_symbiote(
    body: CreateSymbioteRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
) -> SymbioteResponse:
    sym = identity.create(
        name=body.name,
        role=body.role,
        persona=body.persona_json,
        owner_id=auth.tenant_id,
    )
    return SymbioteResponse(
        id=sym.id,
        name=sym.name,
        role=sym.role,
        status=sym.status,
    )


@app.get("/symbiotes/{symbiote_id}", response_model=SymbioteResponse)
def get_symbiote(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
) -> SymbioteResponse:
    sym = identity.get(symbiote_id)
    if sym is None:
        raise HTTPException(status_code=404, detail="Symbiote not found")
    if sym.owner_id and sym.owner_id != auth.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return SymbioteResponse(
        id=sym.id,
        name=sym.name,
        role=sym.role,
        status=sym.status,
    )


# ── Session endpoints ────────────────────────────────────────────────────


@app.post("/sessions", status_code=201, response_model=SessionResponse)
def create_session(
    body: CreateSessionRequest,
    sessions: Annotated[SessionManager, Depends(get_session_manager)],
) -> SessionResponse:
    if body.external_key:
        sess = sessions.get_or_create_by_external_key(
            symbiote_id=body.symbiote_id,
            external_key=body.external_key,
            goal=body.goal,
        )
    else:
        sess = sessions.start(symbiote_id=body.symbiote_id, goal=body.goal)
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
        external_key=sess.external_key,
        status=sess.status,
    )


@app.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: str,
    sessions: Annotated[SessionManager, Depends(get_session_manager)],
) -> SessionResponse:
    sess = sessions.resume(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found") from None
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
        external_key=sess.external_key,
        status=sess.status,
        summary=sess.summary,
    )


@app.get("/sessions/by-key/{external_key}", response_model=SessionResponse)
def get_session_by_key(
    external_key: str,
    sessions: Annotated[SessionManager, Depends(get_session_manager)],
) -> SessionResponse:
    sess = sessions.find_by_external_key(external_key)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found") from None
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
        external_key=sess.external_key,
        status=sess.status,
        summary=sess.summary,
    )


@app.post(
    "/sessions/{session_id}/messages",
    status_code=201,
    response_model=MessageResponse,
)
def add_message(
    session_id: str,
    body: CreateMessageRequest,
    sessions: Annotated[SessionManager, Depends(get_session_manager)],
) -> MessageResponse:
    try:
        msg = sessions.add_message(session_id, role=body.role, content=body.content)
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found") from None
    return MessageResponse(
        id=msg.id,
        role=msg.role,
        content=msg.content,
        created_at=msg.created_at.isoformat(),
    )


@app.post("/sessions/{session_id}/close", response_model=SessionResponse)
def close_session(
    session_id: str,
    sessions: Annotated[SessionManager, Depends(get_session_manager)],
) -> SessionResponse:
    try:
        sess = sessions.close(session_id)
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found") from None
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
        external_key=sess.external_key,
        status=sess.status,
        summary=sess.summary,
    )


# ── Memory endpoints ─────────────────────────────────────────────────────


@app.get("/memory/search", response_model=list[MemoryEntryResponse])
def search_memory(
    query: Annotated[str, Query()],
    memory: Annotated[MemoryStore, Depends(get_memory_store)],
    scope: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> list[MemoryEntryResponse]:
    entries = memory.search(query=query, scope=scope, limit=limit)
    return [
        MemoryEntryResponse(
            id=e.id,
            symbiote_id=e.symbiote_id,
            session_id=e.session_id,
            type=e.type,
            scope=e.scope,
            content=e.content,
            tags=e.tags,
            importance=e.importance,
            source=e.source,
        )
        for e in entries
    ]


# ── Tool endpoints ───────────────────────────────────────────────────────


@app.post(
    "/symbiotes/{symbiote_id}/tools",
    status_code=201,
    response_model=ToolDescriptorResponse,
)
def register_tool(
    symbiote_id: str,
    body: RegisterToolRequest,
    env: Annotated[EnvironmentManager, Depends(get_env_manager)],
    gw: Annotated[ToolGateway, Depends(get_tool_gateway)],
) -> ToolDescriptorResponse:
    """Register an HTTP tool for a symbiote."""
    descriptor = ToolDescriptor(
        tool_id=body.tool_id,
        name=body.name,
        description=body.description,
        parameters=body.parameters,
        handler_type="http",
    )
    http_config = HttpToolConfig(
        method=body.http_method,
        url_template=body.url_template,
        headers=body.headers,
        timeout=body.timeout,
        body_template=body.body_template,
    )
    gw.register_http_tool(descriptor, http_config)

    # Authorize the tool for this symbiote
    current = env.list_tools(symbiote_id)
    if body.tool_id not in current:
        env.configure(symbiote_id=symbiote_id, tools=current + [body.tool_id])

    return ToolDescriptorResponse(
        tool_id=descriptor.tool_id,
        name=descriptor.name,
        description=descriptor.description,
        parameters=descriptor.parameters,
        handler_type=descriptor.handler_type,
    )


@app.get(
    "/symbiotes/{symbiote_id}/tools",
    response_model=list[ToolDescriptorResponse],
)
def list_tools(
    symbiote_id: str,
    env: Annotated[EnvironmentManager, Depends(get_env_manager)],
    gw: Annotated[ToolGateway, Depends(get_tool_gateway)],
) -> list[ToolDescriptorResponse]:
    """List tools available to a symbiote (authorized ones only)."""
    authorized = set(env.list_tools(symbiote_id))
    return [
        ToolDescriptorResponse(
            tool_id=d.tool_id,
            name=d.name,
            description=d.description,
            parameters=d.parameters,
            handler_type=d.handler_type,
        )
        for d in gw.get_descriptors()
        if d.tool_id in authorized
    ]


@app.delete("/symbiotes/{symbiote_id}/tools/{tool_id}", status_code=200)
def remove_tool(
    symbiote_id: str,
    tool_id: str,
    env: Annotated[EnvironmentManager, Depends(get_env_manager)],
    gw: Annotated[ToolGateway, Depends(get_tool_gateway)],
) -> dict:
    """Remove a tool from a symbiote."""
    current = env.list_tools(symbiote_id)
    updated = [t for t in current if t != tool_id]
    env.configure(symbiote_id=symbiote_id, tools=updated)
    gw.unregister_tool(tool_id)
    return {"removed": tool_id}


@app.post(
    "/symbiotes/{symbiote_id}/tools/{tool_id}/exec",
    response_model=ToolExecResponse,
)
def exec_tool(
    symbiote_id: str,
    tool_id: str,
    body: ToolExecRequest,
    gw: Annotated[ToolGateway, Depends(get_tool_gateway)],
) -> ToolExecResponse:
    """Execute a tool manually (for testing)."""
    result = gw.execute(
        symbiote_id=symbiote_id,
        session_id=None,
        tool_id=tool_id,
        params=body.params,
    )
    return ToolExecResponse(
        tool_id=tool_id,
        success=result.success,
        output=result.output,
        error=result.error,
    )


# ══════════════════════════════════════════════════════════════════════════
# CHAT — LLM-powered conversation endpoint (B-20)
# ══════════════════════════════════════════════════════════════════════════


@app.post("/sessions/{session_id}/chat", response_model=ChatResponse)
def chat(
    session_id: str,
    body: ChatRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> ChatResponse:
    """Send a message and get an LLM response with tool execution.

    This is the main endpoint for conversational interaction with a Symbiota.
    The kernel assembles context, calls the LLM, executes any tool calls,
    and returns the response. Tenant isolation is enforced via session ownership.
    """
    # Tenant isolation: verify session belongs to a symbiote owned by this tenant
    row = kernel._storage.fetch_one(
        "SELECT s.symbiote_id, sym.owner_id FROM sessions s "
        "JOIN symbiotes sym ON s.symbiote_id = sym.id "
        "WHERE s.id = ?",
        (session_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if row["owner_id"] and row["owner_id"] != auth.tenant_id:
        raise HTTPException(status_code=403, detail="Session belongs to another tenant")

    response = kernel.message(
        session_id=session_id,
        content=body.content,
        extra_context=body.extra_context,
    )
    return ChatResponse(response=response, session_id=session_id)


# ══════════════════════════════════════════════════════════════════════════
# API KEY MANAGEMENT (B-19)
# ══════════════════════════════════════════════════════════════════════════


class CreateAPIKeyRequest(BaseModel):
    tenant_id: str
    name: str
    role: str = "user"


class APIKeyResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    key_prefix: str
    role: str
    raw_key: str | None = None  # Only returned on create


@app.post("/admin/api-keys", status_code=201, response_model=APIKeyResponse)
def create_api_key(
    body: CreateAPIKeyRequest,
    auth: Annotated[APIKey, Depends(require_admin)],
) -> APIKeyResponse:
    """Create a new API key (admin only)."""
    if _key_manager is None:
        raise HTTPException(status_code=500, detail="Key manager not initialized")
    api_key, raw_key = _key_manager.create_key(
        tenant_id=body.tenant_id, name=body.name, role=body.role
    )
    return APIKeyResponse(
        id=api_key.id,
        tenant_id=api_key.tenant_id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        role=api_key.role,
        raw_key=raw_key,
    )


@app.get("/admin/api-keys/{tenant_id}", response_model=list[APIKeyResponse])
def list_api_keys(
    tenant_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
) -> list[APIKeyResponse]:
    """List API keys for a tenant (admin only)."""
    if _key_manager is None:
        raise HTTPException(status_code=500, detail="Key manager not initialized")
    keys = _key_manager.list_keys(tenant_id)
    return [
        APIKeyResponse(
            id=k.id, tenant_id=k.tenant_id, name=k.name,
            key_prefix=k.key_prefix, role=k.role,
        )
        for k in keys
    ]


@app.delete("/admin/api-keys/{key_id}", status_code=200)
def revoke_api_key(
    key_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
) -> dict:
    """Revoke an API key (admin only)."""
    if _key_manager is None:
        raise HTTPException(status_code=500, detail="Key manager not initialized")
    if not _key_manager.revoke_key(key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"revoked": key_id}


# ══════════════════════════════════════════════════════════════════════════
# DASHBOARD — Admin UI (B-23)
# ══════════════════════════════════════════════════════════════════════════


@app.get("/api/dashboard", response_model=dict)
def dashboard_data(
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> dict:
    """Aggregate data for the admin dashboard (no auth — read-only summary)."""
    symbiotes = adapter.fetch_all(
        "SELECT id, name, role, status, created_at FROM symbiotes ORDER BY created_at DESC"
    )
    sessions = adapter.fetch_all(
        "SELECT s.id, s.symbiote_id, s.status, s.goal, s.started_at, sym.name as symbiote_name "
        "FROM sessions s JOIN symbiotes sym ON s.symbiote_id = sym.id "
        "ORDER BY s.started_at DESC LIMIT 20"
    )
    tenant_counts = adapter.fetch_all(
        "SELECT tenant_id, COUNT(*) as key_count FROM api_keys "
        "WHERE is_active = 1 GROUP BY tenant_id ORDER BY key_count DESC"
    )
    stats = {
        "symbiotes": adapter.fetch_one("SELECT COUNT(*) as c FROM symbiotes")["c"],
        "sessions": adapter.fetch_one("SELECT COUNT(*) as c FROM sessions")["c"],
        "sessions_active": adapter.fetch_one(
            "SELECT COUNT(*) as c FROM sessions WHERE status = 'active'"
        )["c"],
        "memories": adapter.fetch_one("SELECT COUNT(*) as c FROM memory_entries WHERE is_active = 1")["c"],
        "api_keys": adapter.fetch_one("SELECT COUNT(*) as c FROM api_keys WHERE is_active = 1")["c"],
    }
    return {
        "symbiotes": [dict(r) for r in symbiotes],
        "sessions": [dict(r) for r in sessions],
        "tenants": [dict(r) for r in tenant_counts],
        "stats": stats,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    """Serve the admin dashboard UI."""
    import importlib.resources

    html_path = importlib.resources.files("symbiote.api") / "dashboard.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
