"""Tests for SkillsLoader — B-13."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.skills.loader import Skill, SkillsLoader


def _create_skill(
    root: Path, name: str, *, description: str = "A test skill", always: bool = False,
    content: str = "Skill instructions here.", requires: str = "",
) -> Path:
    """Create a skill directory with SKILL.md."""
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"

    always_line = f"always: {'true' if always else 'false'}"
    requires_block = f"\n{requires}" if requires else ""

    skill_file.write_text(
        f"---\nname: {name}\ndescription: {description}\n{always_line}{requires_block}\n---\n\n{content}",
        encoding="utf-8",
    )
    return skill_file


class TestSkillDiscovery:
    def test_discovers_skills_in_directory(self, tmp_path: Path) -> None:
        _create_skill(tmp_path, "git-helper", description="Git operations")
        _create_skill(tmp_path, "search", description="Search the web")

        loader = SkillsLoader(tmp_path)
        skills = loader.list_skills()

        assert len(skills) == 2
        names = {s.name for s in skills}
        assert "git-helper" in names
        assert "search" in names

    def test_empty_directory(self, tmp_path: Path) -> None:
        loader = SkillsLoader(tmp_path)
        assert loader.list_skills() == []

    def test_no_skills_dir(self, tmp_path: Path) -> None:
        loader = SkillsLoader(tmp_path)
        assert loader.list_skills() == []

    def test_skips_invalid_skill_files(self, tmp_path: Path) -> None:
        # Create a skill dir without SKILL.md
        (tmp_path / "skills" / "broken").mkdir(parents=True)
        _create_skill(tmp_path, "valid", description="Works")

        loader = SkillsLoader(tmp_path)
        assert len(loader.list_skills()) == 1

    def test_skips_missing_frontmatter(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills" / "no-front"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("No frontmatter here", encoding="utf-8")

        loader = SkillsLoader(tmp_path)
        assert len(loader.list_skills()) == 0


class TestSkillMetadata:
    def test_parses_description(self, tmp_path: Path) -> None:
        _create_skill(tmp_path, "my-skill", description="Does cool things")
        loader = SkillsLoader(tmp_path)
        skill = loader.get_skill("my-skill")
        assert skill is not None
        assert skill.description == "Does cool things"

    def test_parses_always_flag(self, tmp_path: Path) -> None:
        _create_skill(tmp_path, "always-on", always=True)
        _create_skill(tmp_path, "on-demand", always=False)

        loader = SkillsLoader(tmp_path)
        assert loader.get_skill("always-on").always is True
        assert loader.get_skill("on-demand").always is False

    def test_parses_requires(self, tmp_path: Path) -> None:
        requires = "requires:\n  bins: [git, npm]\n  env: [GITHUB_TOKEN]"
        _create_skill(tmp_path, "needs-stuff", requires=requires)

        loader = SkillsLoader(tmp_path)
        skill = loader.get_skill("needs-stuff")
        assert skill is not None
        assert "git" in skill.requires_bins
        assert "npm" in skill.requires_bins
        assert "GITHUB_TOKEN" in skill.requires_env


class TestLazyLoading:
    def test_content_not_loaded_on_list(self, tmp_path: Path) -> None:
        _create_skill(tmp_path, "lazy", content="Full instructions")
        loader = SkillsLoader(tmp_path)
        skills = loader.list_skills()
        assert skills[0].content is None  # not loaded yet

    def test_content_loaded_on_get(self, tmp_path: Path) -> None:
        _create_skill(tmp_path, "lazy", content="Full instructions")
        loader = SkillsLoader(tmp_path)
        skill = loader.get_skill("lazy")
        assert skill.content == "Full instructions"

    def test_always_skills_have_content(self, tmp_path: Path) -> None:
        _create_skill(tmp_path, "always-skill", always=True, content="Always loaded")
        loader = SkillsLoader(tmp_path)
        always = loader.get_always_skills()
        assert len(always) == 1
        assert always[0].content == "Always loaded"


class TestBuildSummary:
    def test_empty_summary(self, tmp_path: Path) -> None:
        loader = SkillsLoader(tmp_path)
        assert loader.build_summary() == ""

    def test_summary_has_xml_format(self, tmp_path: Path) -> None:
        _create_skill(tmp_path, "search", description="Search the web")
        _create_skill(tmp_path, "code", description="Write code", always=True)

        loader = SkillsLoader(tmp_path)
        summary = loader.build_summary()

        assert "<available-skills>" in summary
        assert "</available-skills>" in summary
        assert 'name="search"' in summary
        assert 'name="code"' in summary
        assert 'always="true"' in summary

    def test_summary_excludes_content(self, tmp_path: Path) -> None:
        _create_skill(tmp_path, "verbose", content="Very long instructions here")
        loader = SkillsLoader(tmp_path)
        summary = loader.build_summary()
        assert "Very long instructions" not in summary


class TestMultipleRoots:
    def test_discovers_from_multiple_roots(self, tmp_path: Path) -> None:
        root1 = tmp_path / "ws1"
        root2 = tmp_path / "ws2"
        _create_skill(root1, "skill-a")
        _create_skill(root2, "skill-b")

        loader = SkillsLoader(root1, root2)
        names = {s.name for s in loader.list_skills()}
        assert names == {"skill-a", "skill-b"}

    def test_add_root_discovers_new_skills(self, tmp_path: Path) -> None:
        root1 = tmp_path / "ws1"
        root2 = tmp_path / "ws2"
        _create_skill(root1, "existing")
        _create_skill(root2, "new-skill")

        loader = SkillsLoader(root1)
        assert len(loader.list_skills()) == 1

        added = loader.add_root(root2)
        assert added == 1
        assert len(loader.list_skills()) == 2

    def test_duplicate_names_first_wins(self, tmp_path: Path) -> None:
        root1 = tmp_path / "ws1"
        root2 = tmp_path / "ws2"
        _create_skill(root1, "shared", description="From root1")
        _create_skill(root2, "shared", description="From root2")

        loader = SkillsLoader(root1, root2)
        skills = loader.list_skills()
        assert len(skills) == 1
        assert skills[0].description == "From root1"
