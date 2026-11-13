"""Evolvable harness text versions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKey
from symbiote.api.http import (
    _require_symbiote_access,
    get_adapter,
)
from symbiote.api.middleware import (
    require_admin,
)
from symbiote.harness.evolver import EVOLVABLE_COMPONENTS
from symbiote.harness.versions import HarnessVersionRepository

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════
# HARNESS — Evolvable text versions (admin console, item 4)
# ══════════════════════════════════════════════════════════════════════════


def get_harness_repo(
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> HarnessVersionRepository:
    return HarnessVersionRepository(storage=adapter)


class HarnessVersionBody(BaseModel):
    """Body for creating a new harness text version (manual edit)."""

    content: str


@router.get("/api/symbiotes/{symbiote_id}/harness")
def api_symbiote_harness(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
    repo: Annotated[HarnessVersionRepository, Depends(get_harness_repo)],
) -> list[dict]:
    """List harness components for a symbiote with their active version.

    Returns one row per component that has versions, plus the base
    ``EVOLVABLE_COMPONENTS`` (even without versions) so the UI can offer them.
    """
    _require_symbiote_access(adapter, symbiote_id, auth)
    present = adapter.fetch_all(
        "SELECT DISTINCT component FROM harness_versions WHERE symbiote_id = ?",
        (symbiote_id,),
    )
    components = {r["component"] for r in present} | set(EVOLVABLE_COMPONENTS)
    out: list[dict] = []
    for component in sorted(components):
        active = repo.get_active_version(symbiote_id, component)
        out.append(
            {
                "component": component,
                "active_version": active["version"] if active else None,
                "avg_score": active["avg_score"] if active else None,
                "session_count": active["session_count"] if active else None,
                "has_versions": active is not None,
            }
        )
    return out


@router.get("/api/symbiotes/{symbiote_id}/harness/{component}")
def api_symbiote_harness_versions(
    symbiote_id: str,
    component: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
    repo: Annotated[HarnessVersionRepository, Depends(get_harness_repo)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    """Version history for a harness component, most recent first."""
    _require_symbiote_access(adapter, symbiote_id, auth)
    return [dict(r) for r in repo.list_versions(symbiote_id, component, limit=limit)]


@router.post("/symbiotes/{symbiote_id}/harness/{component}", status_code=201)
def create_harness_version(
    symbiote_id: str,
    component: str,
    body: HarnessVersionBody,
    auth: Annotated[APIKey, Depends(require_admin)],
    repo: Annotated[HarnessVersionRepository, Depends(get_harness_repo)],
) -> dict:
    """Create a new (active) harness text version from a manual edit."""
    version = repo.create_version(symbiote_id, component, body.content)
    return {"symbiote_id": symbiote_id, "component": component, "version": version}


@router.post("/symbiotes/{symbiote_id}/harness/{component}/rollback")
def rollback_harness_version(
    symbiote_id: str,
    component: str,
    auth: Annotated[APIKey, Depends(require_admin)],
    repo: Annotated[HarnessVersionRepository, Depends(get_harness_repo)],
) -> dict:
    """Roll the active version back to its parent (or to the default text)."""
    ok = repo.rollback(symbiote_id, component)
    if not ok:
        raise HTTPException(status_code=404, detail="No active version to roll back")
    active = repo.get_active_version(symbiote_id, component)
    return {"rolled_back": True, "active_version": active["version"] if active else None}
