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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

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
        """Return all discovered skills (without content)."""
        return list(self._skills.values())

    def get_skill(self, name: str) -> Skill | None:
        """Get a skill by name, loading content lazily."""
        skill = self._skills.get(name)
        if skill is not None and skill.content is None:
            skill.content = self._load_content(skill.path)
        return skill

    def get_always_skills(self) -> list[Skill]:
        """Return skills marked as always=true, with content loaded."""
        result = []
        for skill in self._skills.values():
            if skill.always:
                if skill.content is None:
                    skill.content = self._load_content(skill.path)
                result.append(skill)
        return result

    def build_summary(self) -> str:
        """Build an XML summary of available skills for the system prompt.

        Returns a compact listing so the LLM knows what skills exist
        without loading their full content.
        """
        if not self._skills:
            return ""

        lines = ["<available-skills>"]
        for skill in self._skills.values():
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
        self._discover_root(root)
        return len(self._skills) - before

    # ── Internal ──────────────────────────────────────────────────────────

    def _discover(self) -> None:
        for root in self._roots:
            self._discover_root(root)

    def _discover_root(self, root: Path) -> None:
        skills_dir = root / "skills"
        if not skills_dir.is_dir():
            return

        for skill_dir in sorted(skills_dir.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue

            skill = self._parse_skill(skill_file)
            if skill is not None and skill.name not in self._skills:
                self._skills[skill.name] = skill

    def _parse_skill(self, path: Path) -> Skill | None:
        """Parse frontmatter from a SKILL.md file."""
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

        return Skill(
            name=name,
            description=description,
            path=path,
            always=_to_bool(frontmatter.get("always", False)),
            requires_bins=requires.get("bins", []) if isinstance(requires, dict) else [],
            requires_env=requires.get("env", []) if isinstance(requires, dict) else [],
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
