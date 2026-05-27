"""SkillsLoader — discover and load skills from workspace markdown files.

Skills are markdown files with YAML frontmatter in a workspace's ``skills/`` directory.
The system prompt includes a compact summary of available skills; the full content
is loaded on demand when the agent reads the skill file via ``fs_read``.

Skill file format::

    ---
    name: my-skill
    description: What this skill does
    always: false
    requires:
      bins: [git]
      env: [GITHUB_TOKEN]
    ---

    Full skill instructions here...

Sprint 3 addition — lifecycle awareness via ``skills/usage.py``:

The loader now consults each skill's ``.skill_meta.json`` sidecar:

* Skills with ``status='quarantine'`` are DISCOVERED (so the agent can still
  read them on demand by name) but EXCLUDED from ``build_summary`` — they
  don't appear in ``<available-skills>``. This prevents agent-created skills
  from polluting the LLM's surface until promoted.
* Skills with ``status='archived'`` are skipped entirely.
* Skills with no sidecar are treated as ``status='active'``. This preserves
  every human-curated skill (including ``process/skills/feature.md`` etc.)
  without requiring migration.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from symbiote.skills import usage

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Skill:
    """A discovered skill with metadata and optional content."""

    name: str
    description: str
    path: Path
    always: bool = False
    requires_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    content: str | None = None  # Loaded lazily
    # Lifecycle status from .skill_meta.json sidecar (default 'active' for
    # skills without a sidecar — preserves all human-curated skills).
    status: str = usage.STATUS_ACTIVE
    agent_created: bool = False


class SkillsLoader:
    """Discovers and manages skills from workspace directories.

    Skills live at ``{workspace_root}/skills/{skill-name}/SKILL.md``.
    """

    def __init__(self, *roots: Path) -> None:
        self._roots = list(roots)
        self._skills: dict[str, Skill] = {}
        self._discover()

    # ── Public API ────────────────────────────────────────────────────────

    def list_skills(self) -> list[Skill]:
        """Return all discovered skills (without content), including quarantine.

        Use ``listable_skills()`` for the LLM-facing subset (active only).
        """
        return list(self._skills.values())

    def listable_skills(self) -> list[Skill]:
        """Subset of skills that should appear in ``<available-skills>``.

        Excludes quarantine and archived. This is the view the LLM sees.
        """
        return [
            s for s in self._skills.values()
            if s.status == usage.STATUS_ACTIVE
        ]

    def get_skill(self, name: str) -> Skill | None:
        """Get a skill by name, loading content lazily.

        Bumps ``use_count`` on the sidecar — this is the telemetry the future
        SkillCuratorPhase uses to decide stale/archived transitions.
        """
        skill = self._skills.get(name)
        if skill is None:
            return None
        if skill.content is None:
            skill.content = self._load_content(skill.path)
        # Best-effort usage telemetry. Never fails the load.
        with contextlib.suppress(Exception):
            usage.mark_used(skill.path.parent)
        return skill

    def get_always_skills(self) -> list[Skill]:
        """Return active skills marked as always=true, with content loaded.

        Quarantine/archived skills are never auto-loaded, even with always=true.
        """
        result = []
        for skill in self._skills.values():
            if skill.always and skill.status == usage.STATUS_ACTIVE:
                if skill.content is None:
                    skill.content = self._load_content(skill.path)
                result.append(skill)
        return result

    def build_summary(self) -> str:
        """Build an XML summary of available skills for the system prompt.

        Quarantine and archived skills are excluded — only ``active`` skills
        reach the LLM via the system prompt. Quarantine skills can still be
        loaded by name via ``get_skill`` (useful for testing and CLI).
        """
        listable = self.listable_skills()
        if not listable:
            return ""

        lines = ["<available-skills>"]
        for skill in listable:
            always_attr = ' always="true"' if skill.always else ""
            lines.append(
                f'  <skill name="{skill.name}"{always_attr}>'
                f"{skill.description}</skill>"
            )
        lines.append("</available-skills>")
        return "\n".join(lines)

    def add_root(self, root: Path) -> int:
        """Add a new root directory and discover skills in it.

        Returns the number of new skills found.
        """
        before = len(self._skills)
        self._roots.append(root)
        # Re-build atomically so concurrent readers never see a torn dict.
        fresh = dict(self._skills)
        self._discover_root_into(root, fresh)
        self._skills = fresh
        return len(self._skills) - before

    def refresh(self) -> None:
        """Re-scan all roots from scratch — thread-safe atomic swap.

        Called by ``SkillsStore`` after a successful write so the next
        ``build_summary()`` reflects the new state. Idempotent.

        Implementation note: building a fresh dict and rebinding ``self._skills``
        with a single attribute assignment is atomic under CPython's GIL —
        concurrent readers (``list_skills``, ``build_summary``, ``get_skill``)
        see either the old dict or the new, never a partially-mutated one.
        The legacy ``self._skills.clear() + _discover()`` would let an
        iterating reader hit ``RuntimeError: dictionary changed size during
        iteration``.
        """
        fresh: dict[str, Skill] = {}
        for root in self._roots:
            self._discover_root_into(root, fresh)
        self._skills = fresh

    # ── Internal ──────────────────────────────────────────────────────────

    def discover(self) -> None:
        """Public alias for ``_discover``. Re-runs the directory scan."""
        self._discover()

    def _discover(self) -> None:
        fresh: dict[str, Skill] = {}
        for root in self._roots:
            self._discover_root_into(root, fresh)
        self._skills = fresh

    def _discover_root_into(self, root: Path, target: dict[str, Skill]) -> None:
        """Populate ``target`` with skills found under ``root``.

        Earlier roots win on name collision (first-wins, mirrors the legacy
        ``if skill.name not in self._skills`` behavior).
        """
        skills_dir = root / "skills"
        if not skills_dir.is_dir():
            return

        for skill_dir in sorted(skills_dir.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue

            skill = self._parse_skill(skill_file)
            if skill is not None and skill.name not in target:
                target[skill.name] = skill

    # Back-compat shim — older external callers might import this name.
    def _discover_root(self, root: Path) -> None:
        """Legacy: discover into ``self._skills`` directly. Prefer ``refresh()``.

        Kept because ``add_root`` used it; new code should use the atomic
        ``_discover_root_into`` against a local dict + swap.
        """
        self._discover_root_into(root, self._skills)

    def _parse_skill(self, path: Path) -> Skill | None:
        """Parse frontmatter from a SKILL.md file.

        Also reads the sidecar metadata (``.skill_meta.json``) to populate
        ``status`` and ``agent_created``. Skills with ``status='archived'``
        are filtered out at discovery time — they don't even count for
        ``list_skills()``. Quarantine/active reach the dict.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None

        match = _FRONTMATTER_RE.match(text)
        if not match:
            return None

        frontmatter = self._parse_yaml_simple(match.group(1))
        name = frontmatter.get("name", path.parent.name)
        description = frontmatter.get("description", "")

        if not name:
            return None

        requires = frontmatter.get("requires", {})
        if isinstance(requires, str):
            requires = {}

        # Sidecar-driven lifecycle
        skill_dir = path.parent
        status = usage.get_effective_status(skill_dir)
        if status == usage.STATUS_ARCHIVED:
            return None  # filter out at discovery — invisible everywhere
        agent_created = usage.is_agent_created(skill_dir)

        return Skill(
            name=name,
            description=description,
            path=path,
            always=_to_bool(frontmatter.get("always", False)),
            requires_bins=requires.get("bins", []) if isinstance(requires, dict) else [],
            requires_env=requires.get("env", []) if isinstance(requires, dict) else [],
            status=status,
            agent_created=agent_created,
        )

    @staticmethod
    def _parse_yaml_simple(text: str) -> dict:
        """Minimal YAML-like parser for frontmatter (avoids PyYAML dependency).

        Handles: key: value, key: [list], key: true/false, nested single-level.
        """
        result: dict = {}
        current_key: str | None = None

        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            indent = len(line) - len(line.lstrip())

            if ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip()

                if indent > 0 and current_key is not None:
                    # Nested key
                    if not isinstance(result.get(current_key), dict):
                        result[current_key] = {}
                    result[current_key][key] = _parse_value(value)
                else:
                    current_key = key
                    if value:
                        result[key] = _parse_value(value)
                    else:
                        result[key] = {}

        return result

    @staticmethod
    def _load_content(path: Path) -> str:
        """Load full skill content (stripping frontmatter)."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""

        match = _FRONTMATTER_RE.match(text)
        if match:
            return text[match.end():].strip()
        return text.strip()


def _parse_value(value: str):
    """Parse a YAML-like value string."""
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        items = value[1:-1].split(",")
        return [item.strip().strip("'\"") for item in items if item.strip()]
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value.strip("'\"")


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "yes", "1")
    return bool(value)
