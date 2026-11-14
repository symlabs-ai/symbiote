"""Admin Console data endpoints, dashboard and the Console page."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from symbiote.api import http as _api
from symbiote.api.auth import APIKey
from symbiote.api.http import (
    _require_session_access,
    _require_symbiote_access,
    get_adapter,
    get_session_manager,
)
from symbiote.api.middleware import (
    require_admin,
)
from symbiote.core.inspector import KernelInspector
from symbiote.core.ports import StoragePort
from symbiote.core.session import SessionManager

router = APIRouter()


def get_inspector(adapter: Annotated[StoragePort, Depends(get_adapter)]) -> KernelInspector:
    """Read-only views over the same storage the other console routes use."""
    return KernelInspector(adapter)


def _pick(rows: list[dict], *keys: str) -> list[dict]:
    """Project inspector rows onto the fields (and field order) a console
    endpoint has always returned — the inspector may return more."""
    return [{key: row[key] for key in keys} for row in rows]


_SESSION_FIELDS = (
    "id", "symbiote_id", "goal", "status", "started_at", "ended_at",
    "summary", "external_key", "symbiote_name",
)
_SCORE_FIELDS = (
    "id", "session_id", "symbiote_id", "auto_score", "user_score", "final_score",
    "stop_reason", "total_iterations", "total_tool_calls", "computed_at",
)


# ══════════════════════════════════════════════════════════════════════════
# ADMIN CONSOLE — Data endpoints (no auth, local admin UI)
# ══════════════════════════════════════════════════════════════════════════


@router.get("/api/logs")
def api_logs(
    auth: Annotated[APIKey, Depends(require_admin)],
    level: str | None = Query(default=None),
    logger_name: str | None = Query(default=None, alias="logger"),
    search: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[dict]:
    """Return recent log entries from the in-memory ring buffer (admin console)."""
    entries = list(_api._log_buffer.records)
    if level:
        levels = {lvl.strip().upper() for lvl in level.split(",")}
        entries = [e for e in entries if e["level"] in levels]
    if logger_name:
        entries = [e for e in entries if logger_name in e["logger"]]
    if search:
        q = search.lower()
        entries = [e for e in entries if q in e["message"].lower() or q in e["logger"].lower()]
    return entries[-limit:]


@router.get("/api/symbiotes")
def api_list_symbiotes(
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
) -> list[dict]:
    """List all symbiotes (admin console)."""
    rows = adapter.fetch_all(
        "SELECT id, name, role, owner_id, status, created_at, updated_at "
        "FROM symbiotes WHERE status != 'deleted' ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


@router.get("/api/sessions")
def api_list_sessions(
    auth: Annotated[APIKey, Depends(require_admin)],
    inspector: Annotated[KernelInspector, Depends(get_inspector)],
    symbiote_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    """List sessions with optional filters (admin console)."""
    rows = inspector.list_sessions(symbiote_id or None, status=status or None, limit=limit)
    return _pick(rows, *_SESSION_FIELDS)


@router.get("/api/sessions/{session_id}/messages")
def api_session_messages(
    session_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    sessions: Annotated[SessionManager, Depends(get_session_manager)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Get message history for a session (admin console)."""
    _require_session_access(adapter, session_id, auth)
    msgs = sessions.get_messages(session_id, limit=limit)
    return [
        {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
        for m in reversed(msgs)  # chronological order
    ]


@router.get("/api/sessions/{session_id}/trace")
def api_session_trace(
    session_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
) -> list[dict]:
    """Get execution traces for a session (admin console)."""
    _require_session_access(adapter, session_id, auth)
    rows = adapter.fetch_all(
        "SELECT id, session_id, symbiote_id, total_iterations, total_tool_calls, "
        "total_elapsed_ms, stop_reason, steps_json, created_at "
        "FROM execution_traces WHERE session_id = ? ORDER BY created_at DESC",
        (session_id,),
    )
    return [dict(r) for r in rows]


@router.get("/api/sessions/{session_id}/score")
def api_session_score(
    session_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    inspector: Annotated[KernelInspector, Depends(get_inspector)],
) -> list[dict]:
    """Get session scores (admin console)."""
    _require_session_access(adapter, session_id, auth)
    # The kernel keeps one score row per session (it updates in place), so the
    # inspector's default limit never truncates this list.
    return _pick(inspector.list_scores(session_id=session_id), *_SCORE_FIELDS)


@router.get("/api/symbiotes/{symbiote_id}/traces")
def api_symbiote_traces(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    inspector: Annotated[KernelInspector, Depends(get_inspector)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    """Get recent execution traces for a symbiote (admin console)."""
    _require_symbiote_access(adapter, symbiote_id, auth)
    rows = inspector.list_traces(symbiote_id=symbiote_id, limit=limit)
    return _pick(
        rows, "id", "session_id", "symbiote_id", "total_iterations", "total_tool_calls",
        "total_elapsed_ms", "stop_reason", "created_at",
    )


@router.get("/api/symbiotes/{symbiote_id}/scores")
def api_symbiote_scores(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    inspector: Annotated[KernelInspector, Depends(get_inspector)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    """Get recent session scores for a symbiote (admin console)."""
    _require_symbiote_access(adapter, symbiote_id, auth)
    return _pick(inspector.list_scores(symbiote_id, limit=limit), *_SCORE_FIELDS)


@router.get("/api/symbiotes/{symbiote_id}/memory")
def api_symbiote_memory(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    """Get active memory entries for a symbiote (admin console — what it knows)."""
    _require_symbiote_access(adapter, symbiote_id, auth)
    rows = adapter.fetch_all(
        "SELECT id, session_id, type, scope, content, importance, source, "
        "tags_json, created_at, last_used_at "
        "FROM memory_entries WHERE symbiote_id = ? AND is_active = 1 "
        "ORDER BY created_at DESC LIMIT ?",
        (symbiote_id, limit),
    )
    return [dict(r) for r in rows]


@router.get("/api/symbiotes/{symbiote_id}/reflections")
def api_symbiote_reflections(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    """Get reflection audit rows for a symbiote (admin console — what it learned)."""
    _require_symbiote_access(adapter, symbiote_id, auth)
    rows = adapter.fetch_all(
        "SELECT id, session_id, mode, keyword_facts_json, llm_facts_json, "
        "llm_error, created_at "
        "FROM reflection_audit WHERE symbiote_id = ? ORDER BY created_at DESC LIMIT ?",
        (symbiote_id, limit),
    )
    return [dict(r) for r in rows]


@router.get("/api/symbiotes/{symbiote_id}/activity")
def api_symbiote_activity(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    """Get tool-execution audit log for a symbiote (admin console — what it did)."""
    _require_symbiote_access(adapter, symbiote_id, auth)
    rows = adapter.fetch_all(
        "SELECT id, session_id, tool_id, action, params_json, result, created_at "
        "FROM audit_log WHERE symbiote_id = ? ORDER BY created_at DESC LIMIT ?",
        (symbiote_id, limit),
    )
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════
# DASHBOARD — Admin UI (B-23)
# ══════════════════════════════════════════════════════════════════════════


@router.get("/api/dashboard", response_model=dict)
def dashboard_data(
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[StoragePort, Depends(get_adapter)],
    inspector: Annotated[KernelInspector, Depends(get_inspector)],
) -> dict:
    """Aggregate data for the admin dashboard (no auth — read-only summary)."""
    symbiotes = adapter.fetch_all(
        "SELECT id, name, role, status, created_at FROM symbiotes "
        "WHERE status != 'deleted' ORDER BY created_at DESC"
    )
    sessions = _pick(
        inspector.list_sessions(limit=20),
        "id", "symbiote_id", "status", "goal", "started_at", "symbiote_name",
    )
    tenant_counts = adapter.fetch_all(
        "SELECT tenant_id, COUNT(*) as key_count FROM api_keys "
        "WHERE is_active = 1 GROUP BY tenant_id ORDER BY key_count DESC"
    )
    discovered_tools = adapter.fetch_all(
        "SELECT dt.tool_id, dt.name, dt.method, dt.url_template, dt.status, "
        "dt.source_path, dt.discovered_at, dt.approved_at, dt.symbiote_id, "
        "sym.name as symbiote_name "
        "FROM discovered_tools dt "
        "JOIN symbiotes sym ON dt.symbiote_id = sym.id "
        "ORDER BY dt.discovered_at DESC"
    )
    def _count(sql: str) -> int:
        row = adapter.fetch_one(sql)
        return int(row["c"]) if row else 0

    stats = {
        "symbiotes": _count("SELECT COUNT(*) as c FROM symbiotes WHERE status != 'deleted'"),
        "sessions": _count("SELECT COUNT(*) as c FROM sessions"),
        "sessions_active": _count("SELECT COUNT(*) as c FROM sessions WHERE status = 'active'"),
        "memories": _count("SELECT COUNT(*) as c FROM memory_entries WHERE is_active = 1"),
        "api_keys": _count("SELECT COUNT(*) as c FROM api_keys WHERE is_active = 1"),
        "discovered_tools": _count("SELECT COUNT(*) as c FROM discovered_tools"),
        "pending_tools": _count("SELECT COUNT(*) as c FROM discovered_tools WHERE status = 'pending'"),
    }
    return {
        "symbiotes": [dict(r) for r in symbiotes],
        "sessions": sessions,
        "tenants": [dict(r) for r in tenant_counts],
        "discovered_tools": [dict(r) for r in discovered_tools],
        "stats": stats,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    """Serve the admin dashboard UI.

    In local admin mode the auto-provisioned admin key is injected as
    ``window.__SYMBIOTE_KEY__`` so the Console authenticates without the
    operator having to mint and paste a key by hand.
    """
    import importlib.resources
    import json as _json

    # Trigger key-manager init (and local-admin provisioning) if not yet done.
    get_adapter()

    html_path = importlib.resources.files("symbiote.api") / "dashboard.html"
    html = html_path.read_text(encoding="utf-8")
    if _api._local_admin_key:
        inject = f"<script>window.__SYMBIOTE_KEY__ = {_json.dumps(_api._local_admin_key)};</script>"
        html = html.replace("<head>", f"<head>\n{inject}", 1)
    return HTMLResponse(content=html)
