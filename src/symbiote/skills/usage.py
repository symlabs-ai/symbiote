"""Skill usage sidecar — track agent-vs-human authorship, lifecycle, recall stats.

Each skill directory may have a ``.skill_meta.json`` sidecar:

    {skill_dir}/
    ├── SKILL.md              # curated content (frontmatter + body)
    ├── .skill_meta.json      # mutable metadata (this file)
    ├── references/
    ├── templates/
    └── scripts/

Sidecar fields:
    agent_created: bool       # True only when created via background review
    status:        str        # 'quarantine' | 'active' | 'stale' | 'archived'
    pinned:        bool       # True blocks delete + curator archive (still patch-able)
    created_at:    str (ISO)
    last_used_at:  str (ISO) | None
    use_count:     int        # incremented when SkillsLoader.get_skill loads content
    patch_count:   int        # incremented on edit/patch/write_file

Backward-compat rule: a skill WITHOUT a sidecar is treated as foreground/active.
This preserves every human-curated skill (e.g. ``process/skills/feature.md``)
without requiring migration — only skills the agent creates write a sidecar.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

SIDECAR_NAME = ".skill_meta.json"

# Status lifecycle (mirrors Hermes curator):
#   quarantine — newly agent-created, not yet listed in <available-skills>
#   active     — listed and offered to the LLM
#   stale      — no use_count growth in N days (set by future DreamEngine pass)
#   archived   — moved out of the listing (still on disk, recoverable)
STATUS_QUARANTINE = "quarantine"
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_ARCHIVED = "archived"
ALL_STATUSES = frozenset({STATUS_QUARANTINE, STATUS_ACTIVE, STATUS_STALE, STATUS_ARCHIVED})


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _atomic_write(path: Path, data: str) -> None:
    """Write atomically: tmp file in the same dir, fsync, os.replace.

    Same-dir matters: ``os.replace`` requires source and dest to be on the
    same filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=".tmp-",
        suffix=".json",
    ) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
        tmp_path = Path(f.name)
    os.replace(tmp_path, path)


def default_meta(*, agent_created: bool, status: str | None = None) -> dict[str, Any]:
    """Build a fresh sidecar dict.

    Agent-created skills start in ``quarantine`` (not exposed to LLM in
    ``build_summary``) until a host action (CLI promote or N-use auto-promotion
    in Sprint 4) advances them. Human-created skills (or skills with no sidecar)
    are ``active`` by default — preserving every curated skill that exists today.
    """
    return {
        "agent_created": bool(agent_created),
        "status": status or (STATUS_QUARANTINE if agent_created else STATUS_ACTIVE),
        "pinned": False,
        "created_at": _utcnow_iso(),
        "last_used_at": None,
        "use_count": 0,
        "patch_count": 0,
    }


def read_meta(skill_dir: Path) -> dict[str, Any] | None:
    """Return the sidecar dict, or None if absent / unreadable.

    Absent is the common case for human skills — callers must treat
    ``None`` as ``{agent_created: false, status: active, pinned: false}``.
    """
    sidecar = skill_dir / SIDECAR_NAME
    if not sidecar.is_file():
        return None
    try:
        with sidecar.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("Could not read %s: %s", sidecar, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_meta(skill_dir: Path, meta: dict[str, Any]) -> None:
    """Persist the sidecar atomically."""
    sidecar = skill_dir / SIDECAR_NAME
    _atomic_write(sidecar, json.dumps(meta, ensure_ascii=False, indent=2))


def get_effective_status(skill_dir: Path) -> str:
    """Return the status the loader/runtime should treat this skill as having.

    Sidecar absent or unreadable -> 'active' (default for human skills).
    """
    meta = read_meta(skill_dir)
    if meta is None:
        return STATUS_ACTIVE
    status = meta.get("status", STATUS_ACTIVE)
    return status if status in ALL_STATUSES else STATUS_ACTIVE


def is_agent_created(skill_dir: Path) -> bool:
    """Sidecar absent -> human-created (False). Only background-review writes set True."""
    meta = read_meta(skill_dir)
    if meta is None:
        return False
    return bool(meta.get("agent_created", False))


def is_pinned(skill_dir: Path) -> bool:
    meta = read_meta(skill_dir)
    if meta is None:
        return False
    return bool(meta.get("pinned", False))


def mark_agent_created(skill_dir: Path) -> None:
    """Write a fresh agent-created sidecar (quarantine status). Idempotent."""
    existing = read_meta(skill_dir)
    if existing is not None and existing.get("agent_created"):
        return  # already marked
    write_meta(skill_dir, default_meta(agent_created=True))


def mark_used(skill_dir: Path) -> None:
    """Bump use_count and last_used_at. Creates a default sidecar if missing.

    For human skills (no sidecar), this is the moment one gets created — but
    with ``agent_created=false`` so the curator still leaves it alone. This is
    pure telemetry: the loader can call it on every successful skill load.
    """
    meta = read_meta(skill_dir) or default_meta(agent_created=False)
    meta["use_count"] = int(meta.get("use_count", 0)) + 1
    meta["last_used_at"] = _utcnow_iso()
    write_meta(skill_dir, meta)


def bump_patch(skill_dir: Path) -> None:
    """Bump patch_count on edit/patch/write_file. No-op if skill has no sidecar."""
    meta = read_meta(skill_dir)
    if meta is None:
        return
    meta["patch_count"] = int(meta.get("patch_count", 0)) + 1
    write_meta(skill_dir, meta)


def set_status(skill_dir: Path, status: str) -> None:
    """Move the skill across the lifecycle."""
    if status not in ALL_STATUSES:
        raise ValueError(f"Unknown status {status!r}; allowed: {sorted(ALL_STATUSES)}")
    meta = read_meta(skill_dir) or default_meta(agent_created=False)
    meta["status"] = status
    write_meta(skill_dir, meta)


def set_pinned(skill_dir: Path, pinned: bool) -> None:
    """Pin protects from delete + future curator archive (still patch-able)."""
    meta = read_meta(skill_dir) or default_meta(agent_created=False)
    meta["pinned"] = bool(pinned)
    write_meta(skill_dir, meta)


def forget(skill_dir: Path) -> None:
    """Remove the sidecar (after a skill is deleted). Best-effort."""
    sidecar = skill_dir / SIDECAR_NAME
    try:
        sidecar.unlink(missing_ok=True)
    except OSError as exc:
        _log.warning("Could not remove %s: %s", sidecar, exc)


__all__ = [
    "ALL_STATUSES",
    "SIDECAR_NAME",
    "STATUS_ACTIVE",
    "STATUS_ARCHIVED",
    "STATUS_QUARANTINE",
    "STATUS_STALE",
    "bump_patch",
    "default_meta",
    "forget",
    "get_effective_status",
    "is_agent_created",
    "is_pinned",
    "mark_agent_created",
    "mark_used",
    "read_meta",
    "set_pinned",
    "set_status",
    "write_meta",
]
