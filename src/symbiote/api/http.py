"""HTTP API — FastAPI application for Symbiote kernel."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import EntityNotFoundError, SymbioteError, ValidationError
from symbiote.core.identity import IdentityManager
from symbiote.core.session import SessionManager
from symbiote.memory.store import MemoryStore

# ── FastAPI app ───────────────────────────────────────────────────────────

app = FastAPI(title="Symbiote API", version="0.1.0")


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


class SessionResponse(BaseModel):
    id: str
    symbiote_id: str
    goal: str | None = None
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


# ── Dependency injection ──────────────────────────────────────────────────

_adapter: SQLiteAdapter | None = None


def get_adapter() -> SQLiteAdapter:
    """Return the singleton SQLiteAdapter, creating it on first call."""
    global _adapter
    if _adapter is None:
        config = KernelConfig()
        _adapter = SQLiteAdapter(db_path=config.db_path)
        _adapter.init_schema()
    return _adapter


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


# ── Symbiote endpoints ───────────────────────────────────────────────────


@app.post("/symbiotes", status_code=201, response_model=SymbioteResponse)
def create_symbiote(
    body: CreateSymbioteRequest,
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
) -> SymbioteResponse:
    sym = identity.create(
        name=body.name,
        role=body.role,
        persona=body.persona_json,
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
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
) -> SymbioteResponse:
    sym = identity.get(symbiote_id)
    if sym is None:
        raise HTTPException(status_code=404, detail="Symbiote not found")
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
    sess = sessions.start(symbiote_id=body.symbiote_id, goal=body.goal)
    return SessionResponse(
        id=sess.id,
        symbiote_id=sess.symbiote_id,
        goal=sess.goal,
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
