"""HTTP API — FastAPI application for Symbiote kernel."""

from __future__ import annotations

import collections
import functools
import logging
import os
import sqlite3
import threading
import time as _time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException

# ── FastAPI app ───────────────────────────────────────────────────────────
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKey, APIKeyManager
from symbiote.api.limits import (
    BodySizeLimitMiddleware,
)
from symbiote.api.middleware import (
    enforce_auth,
    set_auth_initializer,
    set_key_manager,
)
from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    SymbioteError,
    ValidationError,
)
from symbiote.core.identity import IdentityManager
from symbiote.core.kernel import SymbioteKernel
from symbiote.core.ports import LLMPort, StoragePort
from symbiote.core.session import SessionManager
from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.discovery.service import DiscoveryService
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.tools import ToolGateway
from symbiote.memory.store import MemoryStore

# ── In-memory log ring buffer ────────────────────────────────────────────

class _LogRingBuffer(logging.Handler):
    """Captures log records into a fixed-size deque for the admin console."""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self.records: collections.deque[dict] = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append({
            "ts": record.created,
            "time": _time.strftime("%H:%M:%S", _time.localtime(record.created))
                    + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
        })


_log_buffer = _LogRingBuffer(capacity=2000)
_log_buffer.setLevel(logging.DEBUG)
_log_buffer.setFormatter(logging.Formatter("%(message)s"))


def _configure_logging() -> None:
    """Server-only logging: console + the Console's ring buffer at DEBUG.

    Called from the app lifespan (i.e. when this app is *served*), never at
    import time, so a host that imports symbiote.api keeps its own logging.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.DEBUG, handlers=[logging.StreamHandler()])
    if _log_buffer not in root.handlers:
        root.addHandler(_log_buffer)
    for ns in ("symbiote", "forge_llm", "httpx", "openai"):
        logging.getLogger(ns).setLevel(logging.DEBUG)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create the kernel (storage, API keys) once at startup; close it at exit."""
    global _kernel, _adapter, _tool_gateway
    _configure_logging()
    _build_info()  # resolve version/commit once, at startup
    get_kernel()
    try:
        yield
    finally:
        kernel = _kernel
        _kernel = _adapter = _tool_gateway = None
        if kernel is not None:
            kernel.shutdown()


# Every route requires an API key except the public allowlist in
# ``symbiote.api.middleware._PUBLIC_PATHS``. Route-level ``require_auth`` /
# ``require_admin`` dependencies still decide *who* may call each route.
app = FastAPI(
    title="Symbiote API", dependencies=[Depends(enforce_auth)], lifespan=_lifespan
)
app.add_middleware(BodySizeLimitMiddleware)


@functools.cache
def _build_info() -> tuple[str, str]:
    """(version, commit) of this process — resolved once, not per request.

    ``/health`` is polled by load balancers; spawning ``git`` on every call
    cost a subprocess each time (and up to its 2 s timeout).
    """
    import importlib.metadata
    import subprocess

    try:
        version = importlib.metadata.version("symbiote")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip() or "unknown"
    except Exception:
        # Containers don't ship the .git directory; CI injects GIT_COMMIT as a
        # build-arg / env var. systemd-managed deployments still get the right
        # answer from `git rev-parse` above.
        commit = os.environ.get("GIT_COMMIT", "unknown")
    return version, commit


@app.get("/health")
async def health() -> dict:
    """Health check endpoint for Docker/load balancers."""
    version, commit = _build_info()
    return {"status": "ok", "service": "symbiote", "version": version, "commit": commit}


@app.exception_handler(EntityNotFoundError)
async def entity_not_found_handler(request: Request, exc: EntityNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(DuplicateEntityError)
async def duplicate_entity_handler(request: Request, exc: DuplicateEntityError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(SymbioteError)
async def symbiote_error_handler(request: Request, exc: SymbioteError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(PydanticValidationError)
async def model_validation_error_handler(
    request: Request, exc: PydanticValidationError
) -> JSONResponse:
    """Domain models (EnvironmentConfig, Message, ...) rejecting a value the
    request carried. FastAPI's own RequestValidationError covers the request
    body; this covers values validated later, inside the kernel."""
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(include_url=False, include_context=False)},
    )


@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error_handler(request: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "Conflict with existing data"})


# ── Dependency injection ──────────────────────────────────────────────────

_adapter: StoragePort | None = None
_tool_gateway: ToolGateway | None = None
_kernel: SymbioteKernel | None = None
_key_manager: APIKeyManager | None = None
_local_admin_key: str | None = None


def _ensure_local_admin_key(key_manager: APIKeyManager) -> None:
    """Provision a local admin API key when ``SYMBIOTE_LOCAL_ADMIN=1``.

    Against a fresh/embedded database there are no API keys, and creating the
    first one requires admin auth — a chicken-and-egg that locks the admin
    Console out of every authenticated endpoint. With the flag enabled we mint
    a real admin key (revoking any previous ``local-console`` key so they don't
    accumulate across restarts) and stash the raw value so ``dashboard_page()``
    can hand it to the Console. Auth stays ON: the key is real and auditable in
    ``api_keys``; it is merely auto-resolved for local use.

    Intended for a Console bound to ``127.0.0.1`` — the raw key is embedded in
    the served HTML. Off by default, so production auth is unchanged.
    """
    global _local_admin_key
    if os.environ.get("SYMBIOTE_LOCAL_ADMIN") != "1":
        return
    if _local_admin_key is not None:
        return
    # Revoke stale local-console keys so restarts don't pile them up.
    for existing in key_manager.list_keys("default"):
        if existing.name == "local-console" and existing.is_active:
            key_manager.revoke_key(existing.id)
    _, raw_key = key_manager.create_key(
        tenant_id="default", name="local-console", role="admin"
    )
    _local_admin_key = raw_key


def _resolve_llm() -> LLMPort | None:
    """Resolve LLM adapter from env."""
    provider = os.environ.get("SYMBIOTE_LLM_PROVIDER")
    if not provider or provider == "mock":
        return None
    from symbiote.adapters.llm.forge import ForgeLLMAdapter

    model = os.environ.get("SYMBIOTE_LLM_MODEL") or None
    return ForgeLLMAdapter(provider=provider, model=model)


def _env_path_list(name: str) -> list[Path]:
    """Parse an env var holding paths split by os.pathsep or ',' into Paths."""
    raw = os.environ.get(name)
    if not raw:
        return []
    # Accept both the platform separator (':' / ';') and comma.
    parts: list[str] = []
    for chunk in raw.split(os.pathsep):
        parts.extend(chunk.split(","))
    return [Path(p.strip()) for p in parts if p.strip()]


def _resolve_config() -> KernelConfig:
    """Build the kernel config from env.

    ``SYMBIOTE_DB_PATH`` overrides the default ``db_path``, letting the
    server (and its admin Console at ``/``) open the SQLite file of *any*
    Symbiote instance — e.g. one embedded in a host app like SymTalk —
    without having to run the server from that app's working directory.

    Skill scoping (multi-tenant safety):
      ``SYMBIOTE_SKILL_SCOPE``            "global" | "per_symbiote"
      ``SYMBIOTE_SKILLS_ROOT``            optional skill library root
      ``SYMBIOTE_SKILLS_PROTECTED_ROOTS`` optional shared read-only catalogue
                                          roots (split by os.pathsep or ',')

    The API is a multi-tenant entrypoint: one kernel singleton serves many
    symbiotes/tenants via ``/symbiotes/{symbiote_id}``. With global skill
    scope, learned skills are a shared pool → cross-tenant leakage. So the
    API DEFAULTS to ``per_symbiote`` (unlike ``KernelConfig`` itself, which
    stays ``global`` to preserve CLI / embedded single-symbiote behaviour).
    When the scope was NOT set explicitly via env, we log a WARNING describing
    the default and how to override — so an operator with a pre-existing
    global skill pool (now invisible under per_symbiote, see ROLLOUT.md)
    knows exactly why and how to restore it (``SYMBIOTE_SKILL_SCOPE=global``).

    Both ``get_adapter()`` and ``get_kernel()`` route through here so they
    always open the *same* file.
    """
    kwargs: dict[str, Any] = {}

    db_path = os.environ.get("SYMBIOTE_DB_PATH")
    if db_path:
        kwargs["db_path"] = Path(db_path)

    skills_root = os.environ.get("SYMBIOTE_SKILLS_ROOT")
    if skills_root:
        kwargs["skills_root"] = Path(skills_root)

    protected = _env_path_list("SYMBIOTE_SKILLS_PROTECTED_ROOTS")
    if protected:
        kwargs["skills_protected_roots"] = protected

    scope_env = os.environ.get("SYMBIOTE_SKILL_SCOPE")
    if scope_env:
        kwargs["skill_scope"] = scope_env.strip()
    else:
        # Secure default for the multi-tenant API; loud about it so a legacy
        # global pool isn't silently hidden.
        kwargs["skill_scope"] = "per_symbiote"
        logging.getLogger(__name__).warning(
            "SYMBIOTE_SKILL_SCOPE not set; defaulting the API to "
            "'per_symbiote' so learned skills are isolated per tenant "
            "(no cross-tenant leakage). If this deployment relied on a "
            "shared GLOBAL skill pool, those skills are now invisible — set "
            "SYMBIOTE_SKILL_SCOPE=global to restore (see docs/ROLLOUT.md)."
        )

    return KernelConfig(**kwargs)


def get_adapter() -> StoragePort:
    """The kernel's storage: one SQLite connection per process.

    Kept as a separate dependency so tests can override it.
    """
    return get_kernel()._storage


_init_lock = threading.Lock()


def get_kernel() -> SymbioteKernel:
    """Return the process-wide SymbioteKernel, creating it on first use.

    Normally created by the app lifespan; the lazy path covers TestClient
    without ``with`` and embedded use. The API key manager lives on the same
    storage.
    """
    global _kernel, _adapter, _key_manager
    if _kernel is not None:
        return _kernel
    with _init_lock:
        if _kernel is None:
            config = _resolve_config()
            try:
                llm = _resolve_llm()
            except Exception:
                logging.getLogger(__name__).exception(
                    "LLM adapter failed to initialise; chat is unavailable until fixed"
                )
                llm = None
            kernel = SymbioteKernel(config=config, llm=llm)
            key_manager = APIKeyManager(kernel._storage)
            key_manager.init_schema()
            set_key_manager(key_manager)
            _key_manager = key_manager
            _ensure_local_admin_key(key_manager)
            _adapter = kernel._storage
            _kernel = kernel
    return _kernel


def _ensure_initialized() -> None:
    get_kernel()


set_auth_initializer(_ensure_initialized)


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


def get_tool_gateway() -> ToolGateway:
    """The kernel's gateway — the same one the chat loop executes tools on."""
    return get_kernel().tool_gateway


def get_discovery_service(
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> DiscoveryService:
    return DiscoveryService(DiscoveredToolRepository(adapter))


# ── Authorization helpers ───────────────────────────────────────────────
#
# Ownership rule (applies to every route that names a symbiote or session):
#   * admin keys pass;
#   * otherwise the symbiote's owner_id must equal the key's tenant;
#   * symbiotes without an owner are visible to admins only;
#   * soft-deleted symbiotes (and their sessions) are not found.


def _check_owner(owner_id: str | None, auth: APIKey) -> None:
    if auth.is_admin:
        return
    if not owner_id or owner_id != auth.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")


def _require_symbiote(identity: IdentityManager, symbiote_id: str, auth: APIKey):  # noqa: ANN202
    sym = identity.get(symbiote_id)
    if sym is None or sym.status == "deleted":
        raise HTTPException(status_code=404, detail="Symbiote not found")
    _check_owner(sym.owner_id, auth)
    return sym


def _require_symbiote_access(
    storage: StoragePort, symbiote_id: str, auth: APIKey
) -> dict:
    row = storage.fetch_one(
        "SELECT id, owner_id, status FROM symbiotes WHERE id = ?", (symbiote_id,)
    )
    if row is None or row["status"] == "deleted":
        raise HTTPException(status_code=404, detail="Symbiote not found")
    _check_owner(row["owner_id"], auth)
    return row


def _require_session_access(
    storage: StoragePort, session_id: str, auth: APIKey
) -> dict:
    row = storage.fetch_one(
        "SELECT s.id, s.symbiote_id, sym.owner_id, sym.status AS symbiote_status "
        "FROM sessions s JOIN symbiotes sym ON s.symbiote_id = sym.id "
        "WHERE s.id = ?",
        (session_id,),
    )
    if row is None or row["symbiote_status"] == "deleted":
        raise HTTPException(status_code=404, detail="Session not found")
    _check_owner(row["owner_id"], auth)
    return row


# ── Routes (one router per area; registration order kept) ──────────────────

from symbiote.api.routes import (  # noqa: E402
    admin,
    console,
    discovery,
    harness,
    sessions,
    symbiotes,
    tools,
)
from symbiote.api.schemas import *  # noqa: E402,F403 — names stay importable from here

for _routes in (symbiotes, sessions, tools, discovery, admin, console, harness):
    app.include_router(_routes.router)
