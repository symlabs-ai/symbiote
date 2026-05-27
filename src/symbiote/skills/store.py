"""SkillsStore — agent-managed CRUD for skills, with provenance + lifecycle.

Skills live as directories on disk:

    {skills_root}/{skill_name}/
    ├── SKILL.md                   # frontmatter + body
    ├── .skill_meta.json           # sidecar (see skills/usage.py)
    ├── references/<topic>.md      # session-specific detail, knowledge banks
    ├── templates/<name>.<ext>     # starter files for copy-modify
    ├── scripts/<name>.<ext>       # statically re-runnable actions
    └── assets/<name>              # binary / non-text assets

This module is the WRITE side; ``SkillsLoader`` is the READ side. The
``skill_manage`` tool (``skills/tool.py``) exposes a subset of this surface
to the LLM. Background-review writes are tagged ``agent_created=true`` via
``core/provenance.set_current_write_origin(BACKGROUND_REVIEW)``.

Validation, atomic writes, and protected-path guards mirror the Hermes
implementation (``~/dev/research/hermes-agent/tools/skill_manager_tool.py``)
but stay scoped: Symbiote is local-first per-symbiote, so no security scanner,
no hub-installed skills, no multi-tenant policy.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from symbiote.core.provenance import is_background_review
from symbiote.skills import usage

_log = logging.getLogger(__name__)


# ── Validation constants ───────────────────────────────────────────────────

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000     # ~36k tokens at 2.75 chars/token
MAX_SKILL_FILE_BYTES = 1_048_576      # 1 MiB per supporting file

# Filesystem-safe, URL-friendly. Must start with letter/digit.
_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Subdirectories agent is allowed to write into.
ALLOWED_SUBDIRS = frozenset({"references", "templates", "scripts", "assets"})


# ── Exceptions ─────────────────────────────────────────────────────────────


class SkillError(Exception):
    """Base for SkillsStore errors. Stable enough for callers to except."""


class SkillValidationError(SkillError):
    """Bad input — name / size / path."""


class SkillNotFoundError(SkillError):
    """Skill (or supporting file) not found."""


class SkillExistsError(SkillError):
    """create() called with a name that already exists."""


class SkillProtectedError(SkillError):
    """Skill is pinned or in a protected root — refused."""


# ── Result type ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillWriteResult:
    """Lightweight outcome record. Callers can serialize to JSON for tool responses."""

    success: bool
    action: str
    name: str
    path: Path | None = None
    message: str = ""


# ── Validation helpers ─────────────────────────────────────────────────────


def _validate_name(name: str) -> None:
    if not name:
        raise SkillValidationError("Skill name is required.")
    if len(name) > MAX_NAME_LENGTH:
        raise SkillValidationError(
            f"Skill name exceeds {MAX_NAME_LENGTH} characters."
        )
    if not _VALID_NAME_RE.match(name):
        raise SkillValidationError(
            f"Invalid skill name {name!r}. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )


def _validate_content_size(content: str, *, label: str = "content") -> None:
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        raise SkillValidationError(
            f"{label} is {len(content):,} chars "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). Split it across "
            f"references/<topic>.md files instead."
        )


def _validate_file_path(file_path: str) -> None:
    """Reject absolute paths, path traversal, and disallowed top-level dirs."""
    if not file_path:
        raise SkillValidationError("file_path is required.")
    p = Path(file_path)
    if p.is_absolute():
        raise SkillValidationError(f"file_path must be relative: {file_path!r}")
    # Normalize and reject any '..' segment.
    parts = p.parts
    if ".." in parts or any(part.startswith("/") for part in parts):
        raise SkillValidationError(f"Path traversal not allowed: {file_path!r}")
    if not parts:
        raise SkillValidationError("file_path is required.")
    top = parts[0]
    if top not in ALLOWED_SUBDIRS:
        raise SkillValidationError(
            f"file_path must start with one of {sorted(ALLOWED_SUBDIRS)}: "
            f"{file_path!r}"
        )


# ── Atomic write ───────────────────────────────────────────────────────────


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically in the same directory as ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=".tmp-",
        suffix=path.suffix or ".txt",
    ) as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
        tmp_path = Path(f.name)
    os.replace(tmp_path, path)


# ── Store ──────────────────────────────────────────────────────────────────


class SkillsStore:
    """Filesystem-backed CRUD with provenance tagging.

    Multiple roots are supported (``roots`` list). The FIRST root is the
    "agent write root" — where ``create()`` places new skills. Other roots
    are read-only as far as ``create`` is concerned, but ``patch`` / ``edit``
    / ``write_file`` / ``delete`` accept any root the skill lives in,
    provided it's not in ``protected_roots``.

    Typical setup::

        SkillsStore(
            roots=[
                Path(".symbiote/skills/agent"),    # agent writes here
                Path("skills"),                     # curated, read+modify
            ],
            protected_roots=[Path("process/skills")],
        )

    Tools registered by the LLM see ``SkillsStore`` via ``skills/tool.py``.
    """

    def __init__(
        self,
        roots: list[Path],
        *,
        protected_roots: list[Path] | None = None,
    ) -> None:
        if not roots:
            raise ValueError("SkillsStore needs at least one root path.")
        # Resolve so containment checks below work even with symlinks.
        self._roots: list[Path] = [Path(r).resolve() for r in roots]
        self._protected_roots: list[Path] = [
            Path(r).resolve() for r in (protected_roots or [])
        ]
        # Ensure the agent write root exists.
        self._roots[0].mkdir(parents=True, exist_ok=True)

    @property
    def agent_root(self) -> Path:
        """First root — where agent-created skills go."""
        return self._roots[0]

    # ── discovery ──────────────────────────────────────────────────────

    def find_skill_dir(self, name: str) -> Path | None:
        """Locate an existing skill directory across all roots."""
        _validate_name(name)
        for root in self._roots:
            candidate = root / name
            if (candidate / "SKILL.md").is_file():
                return candidate
        return None

    def _is_protected(self, skill_dir: Path) -> bool:
        """Is this skill inside a protected root?"""
        try:
            resolved = skill_dir.resolve()
        except OSError:
            return False
        for proot in self._protected_roots:
            try:
                resolved.relative_to(proot)
                return True
            except ValueError:
                continue
        return False

    # ── create ─────────────────────────────────────────────────────────

    def create(self, name: str, content: str) -> SkillWriteResult:
        """Create a new skill at ``{agent_root}/{name}/SKILL.md``.

        Tags the sidecar ``agent_created=true`` when called from a
        background-review context (see ``core/provenance``). Foreground
        calls produce ``agent_created=false`` — the user explicitly asked,
        so the skill belongs to the user and the future curator must not
        touch it.
        """
        _validate_name(name)
        if not content:
            raise SkillValidationError("content is required for create().")
        _validate_content_size(content, label="SKILL.md content")

        if self.find_skill_dir(name) is not None:
            raise SkillExistsError(
                f"Skill {name!r} already exists. Use edit() or patch() instead."
            )

        skill_dir = self.agent_root / name
        skill_dir.mkdir(parents=True, exist_ok=False)
        skill_file = skill_dir / "SKILL.md"
        _atomic_write_text(skill_file, content)

        # Sidecar: agent_created reflects who called us NOW.
        agent_created = is_background_review()
        usage.write_meta(skill_dir, usage.default_meta(agent_created=agent_created))

        return SkillWriteResult(
            success=True,
            action="create",
            name=name,
            path=skill_file,
            message=f"Skill {name!r} created at {skill_file}",
        )

    # ── edit (full rewrite) ────────────────────────────────────────────

    def edit(self, name: str, content: str) -> SkillWriteResult:
        """Replace SKILL.md content entirely. Forbidden on protected skills."""
        _validate_name(name)
        if not content:
            raise SkillValidationError("content is required for edit().")
        _validate_content_size(content, label="SKILL.md content")

        skill_dir = self.find_skill_dir(name)
        if skill_dir is None:
            raise SkillNotFoundError(f"Skill {name!r} not found.")
        if self._is_protected(skill_dir):
            raise SkillProtectedError(
                f"Skill {name!r} is in a protected root; cannot edit."
            )

        _atomic_write_text(skill_dir / "SKILL.md", content)
        usage.bump_patch(skill_dir)
        return SkillWriteResult(
            success=True, action="edit", name=name,
            path=skill_dir / "SKILL.md",
            message=f"Skill {name!r} replaced.",
        )

    # ── patch (find-and-replace) ───────────────────────────────────────

    def patch(
        self,
        name: str,
        old_string: str,
        new_string: str,
        *,
        file_path: str | None = None,
        replace_all: bool = False,
    ) -> SkillWriteResult:
        """Find-and-replace within SKILL.md (default) or a supporting file.

        Refuses if ``old_string`` is not found, or if ``replace_all=False``
        and ``old_string`` appears more than once (ambiguous edit).
        """
        _validate_name(name)
        if not old_string:
            raise SkillValidationError("old_string is required for patch().")
        if new_string is None:
            raise SkillValidationError(
                "new_string is required for patch() (use empty string to delete)."
            )

        skill_dir = self.find_skill_dir(name)
        if skill_dir is None:
            raise SkillNotFoundError(f"Skill {name!r} not found.")
        if self._is_protected(skill_dir):
            raise SkillProtectedError(
                f"Skill {name!r} is in a protected root; cannot patch."
            )

        if file_path is None:
            target = skill_dir / "SKILL.md"
        else:
            _validate_file_path(file_path)
            target = skill_dir / file_path
        if not target.is_file():
            raise SkillNotFoundError(f"File not found: {target}")

        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old_string)
        if occurrences == 0:
            raise SkillValidationError(
                f"old_string not found in {target.name}."
            )
        if occurrences > 1 and not replace_all:
            raise SkillValidationError(
                f"old_string appears {occurrences} times in {target.name}; "
                "pass replace_all=True or give more surrounding context."
            )

        new_text = text.replace(old_string, new_string)
        _validate_content_size(new_text, label=str(target.name))
        _atomic_write_text(target, new_text)
        usage.bump_patch(skill_dir)

        return SkillWriteResult(
            success=True, action="patch", name=name, path=target,
            message=f"Patched {target.name} ({occurrences} occurrences).",
        )

    # ── write_file (supporting file) ───────────────────────────────────

    def write_file(self, name: str, file_path: str, file_content: str) -> SkillWriteResult:
        """Create/overwrite a supporting file under references/templates/scripts/assets."""
        _validate_name(name)
        _validate_file_path(file_path)
        if file_content is None:
            raise SkillValidationError("file_content is required for write_file().")
        content_bytes = len(file_content.encode("utf-8"))
        if content_bytes > MAX_SKILL_FILE_BYTES:
            raise SkillValidationError(
                f"file_content is {content_bytes:,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,}). Split the file."
            )

        skill_dir = self.find_skill_dir(name)
        if skill_dir is None:
            raise SkillNotFoundError(
                f"Skill {name!r} not found. Create it with create() first."
            )
        if self._is_protected(skill_dir):
            raise SkillProtectedError(
                f"Skill {name!r} is in a protected root; cannot write supporting files."
            )

        target = skill_dir / file_path
        _atomic_write_text(target, file_content)
        usage.bump_patch(skill_dir)

        return SkillWriteResult(
            success=True, action="write_file", name=name, path=target,
            message=f"Wrote {file_path} to skill {name!r}.",
        )

    # ── remove_file ────────────────────────────────────────────────────

    def remove_file(self, name: str, file_path: str) -> SkillWriteResult:
        _validate_name(name)
        _validate_file_path(file_path)

        skill_dir = self.find_skill_dir(name)
        if skill_dir is None:
            raise SkillNotFoundError(f"Skill {name!r} not found.")
        if self._is_protected(skill_dir):
            raise SkillProtectedError(
                f"Skill {name!r} is in a protected root; cannot remove files."
            )

        target = skill_dir / file_path
        if not target.is_file():
            raise SkillNotFoundError(f"File not found: {file_path!r}")
        target.unlink()
        # Clean up empty subdir (but never the skill dir itself).
        parent = target.parent
        if parent != skill_dir and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        usage.bump_patch(skill_dir)

        return SkillWriteResult(
            success=True, action="remove_file", name=name, path=target,
            message=f"Removed {file_path} from skill {name!r}.",
        )

    # ── delete ─────────────────────────────────────────────────────────

    def delete(self, name: str) -> SkillWriteResult:
        """Remove the skill directory entirely. Refused if pinned or protected."""
        _validate_name(name)
        skill_dir = self.find_skill_dir(name)
        if skill_dir is None:
            raise SkillNotFoundError(f"Skill {name!r} not found.")
        if self._is_protected(skill_dir):
            raise SkillProtectedError(
                f"Skill {name!r} is in a protected root; cannot delete."
            )
        if usage.is_pinned(skill_dir):
            raise SkillProtectedError(
                f"Skill {name!r} is pinned; unpin first with set_pinned(False)."
            )

        shutil.rmtree(skill_dir)
        # Clean up an empty category dir, but not the root itself.
        parent = skill_dir.parent
        if parent.resolve() not in (r.resolve() for r in self._roots):
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

        return SkillWriteResult(
            success=True, action="delete", name=name, path=None,
            message=f"Skill {name!r} deleted.",
        )


__all__ = [
    "ALLOWED_SUBDIRS",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_SKILL_CONTENT_CHARS",
    "MAX_SKILL_FILE_BYTES",
    "SkillError",
    "SkillExistsError",
    "SkillNotFoundError",
    "SkillProtectedError",
    "SkillValidationError",
    "SkillWriteResult",
    "SkillsStore",
]
