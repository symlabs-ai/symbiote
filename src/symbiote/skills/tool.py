"""skill_manage tool — exposes ``SkillsStore`` actions to the LLM.

Registered with a ``ToolGateway`` but NOT auto-authorized for any symbiote.
Hosts opt in by including ``"skill_manage"`` in
``kernel.environment.configure(tools=[...])``. This keeps the Sprint 3 rollout
conservative: the tool exists, but agents only see it after the host says so.

The tool is the agent-facing wrapper around ``SkillsStore``. Provenance is
applied UPSTREAM (by ``BackgroundReviewEngine`` in Sprint 4); when called
foreground via the chat tool loop, ``write_origin`` defaults to ``foreground``
and resulting skills get ``agent_created=false`` — they belong to the user
who explicitly asked.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.tools import ToolGateway
from symbiote.skills.store import (
    SkillError,
    SkillExistsError,
    SkillNotFoundError,
    SkillProtectedError,
    SkillsStore,
    SkillValidationError,
)

_log = logging.getLogger(__name__)


SKILL_MANAGE_TOOL_ID = "skill_manage"

_PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "edit", "patch", "delete", "write_file", "remove_file"],
            "description": (
                "create: new skill (SKILL.md + sidecar). "
                "edit: full rewrite of SKILL.md. "
                "patch: find-and-replace inside SKILL.md or a supporting file. "
                "delete: remove the skill directory. "
                "write_file: add/overwrite a supporting file "
                "(references/templates/scripts/assets). "
                "remove_file: remove a supporting file."
            ),
        },
        "name": {
            "type": "string",
            "description": (
                "Skill name (lowercase, letters/digits/./-/_, max 64 chars, "
                "must start with letter or digit)."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "REQUIRED for create and edit. Full SKILL.md body including "
                "YAML frontmatter (--- name/description/... ---)."
            ),
        },
        "old_string": {
            "type": "string",
            "description": "REQUIRED for patch. Exact substring to find.",
        },
        "new_string": {
            "type": "string",
            "description": "REQUIRED for patch. Replacement (empty string to delete).",
        },
        "file_path": {
            "type": "string",
            "description": (
                "Relative path within the skill dir, starting with "
                "references/, templates/, scripts/, or assets/. Used by "
                "patch (optional, defaults to SKILL.md), write_file, remove_file."
            ),
        },
        "file_content": {
            "type": "string",
            "description": "REQUIRED for write_file. UTF-8 text content.",
        },
        "replace_all": {
            "type": "boolean",
            "description": (
                "Patch only: if true, replaces every occurrence. Default false "
                "(refuses ambiguous matches)."
            ),
        },
    },
    "required": ["action", "name"],
}


def _to_json(success: bool, action: str, name: str, **extra: Any) -> str:
    """JSON response in a shape friendly to LLM consumption + audit logs."""
    payload: dict[str, Any] = {"success": success, "action": action, "name": name}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def make_handler(store: SkillsStore):
    """Build a ToolGateway handler bound to a specific SkillsStore."""

    def _handler(params: dict) -> str:
        action = params.get("action")
        name = params.get("name", "")
        try:
            if action == "create":
                result = store.create(name=name, content=params.get("content", ""))
            elif action == "edit":
                result = store.edit(name=name, content=params.get("content", ""))
            elif action == "patch":
                result = store.patch(
                    name=name,
                    old_string=params.get("old_string", ""),
                    new_string=params.get("new_string", ""),
                    file_path=params.get("file_path"),
                    replace_all=bool(params.get("replace_all", False)),
                )
            elif action == "delete":
                result = store.delete(name=name)
            elif action == "write_file":
                result = store.write_file(
                    name=name,
                    file_path=params.get("file_path", ""),
                    file_content=params.get("file_content", ""),
                )
            elif action == "remove_file":
                result = store.remove_file(
                    name=name,
                    file_path=params.get("file_path", ""),
                )
            else:
                return _to_json(
                    False, str(action or ""), name,
                    error=f"Unknown action {action!r}. "
                    f"Use: create, edit, patch, delete, write_file, remove_file.",
                )
        except SkillValidationError as exc:
            return _to_json(False, str(action), name, error=str(exc), kind="validation")
        except SkillNotFoundError as exc:
            return _to_json(False, str(action), name, error=str(exc), kind="not_found")
        except SkillExistsError as exc:
            return _to_json(False, str(action), name, error=str(exc), kind="exists")
        except SkillProtectedError as exc:
            return _to_json(False, str(action), name, error=str(exc), kind="protected")
        except SkillError as exc:
            return _to_json(False, str(action), name, error=str(exc), kind="error")
        except Exception as exc:  # pragma: no cover — defensive
            _log.exception("skill_manage unexpected failure")
            return _to_json(
                False, str(action), name,
                error=f"{type(exc).__name__}: {exc}", kind="internal",
            )

        return _to_json(
            result.success,
            result.action,
            result.name,
            path=str(result.path) if result.path else None,
            message=result.message,
        )

    return _handler


def register(gateway: ToolGateway, store: SkillsStore) -> None:
    """Register skill_manage on a ToolGateway.

    The tool is registered (so the gateway can dispatch it) but NOT
    auto-authorized for any symbiote. Hosts opt in symbiote-by-symbiote
    via ``kernel.environment.configure(tools=[..., 'skill_manage'])``.
    """
    descriptor = ToolDescriptor(
        tool_id=SKILL_MANAGE_TOOL_ID,
        name="Manage Skills",
        description=(
            "Create, edit, patch, or delete skills (procedural playbooks "
            "stored as markdown files with YAML frontmatter). Use when a "
            "non-trivial technique or workflow emerged that future sessions "
            "would benefit from, or when a loaded skill turned out wrong. "
            "Do NOT use for env-dependent failures, transient errors, or "
            "single-task narratives."
        ),
        parameters=_PARAMETERS_SCHEMA,
        tags=["skills", "self_improvement"],
        risk_level="medium",
    )
    gateway.register_descriptor(descriptor, make_handler(store))


__all__ = [
    "SKILL_MANAGE_TOOL_ID",
    "make_handler",
    "register",
]
