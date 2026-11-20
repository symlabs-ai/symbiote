"""Release contracts for version metadata and immutable VCS dependencies."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELEASE_VERSION = "0.8.15"
FORGE_LLM_COMMIT = "b26b5e36e5d5be301e60cedeb13b4b434815a1f7"
FORGE_SCRAPER_COMMIT = "632e4ce5d218bedebad3411df48625f4b34d7f29"
FORGE_LLM_PIN = (
    "forge-llm @ git+https://github.com/symlabs-ai/forge_llm.git"
    f"@{FORGE_LLM_COMMIT}"
)
FORGE_SCRAPER_PIN = (
    "forge-scraper[article,transcript,sofascore] @ "
    "git+https://github.com/symlabs-ai/forge_scraper.git"
    f"@{FORGE_SCRAPER_COMMIT}"
)


def _project_metadata() -> dict:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]


def test_release_version_matches_expected_patch() -> None:
    assert _project_metadata()["version"] == RELEASE_VERSION


def test_optional_vcs_dependencies_use_expected_release_commits() -> None:
    extras = _project_metadata()["optional-dependencies"]

    assert extras["llm"] == [FORGE_LLM_PIN]
    assert extras["extract"] == [FORGE_SCRAPER_PIN]


def test_every_declared_vcs_dependency_is_pinned_by_full_commit() -> None:
    project = _project_metadata()
    dependencies = list(project["dependencies"])
    for extra_dependencies in project["optional-dependencies"].values():
        dependencies.extend(extra_dependencies)

    vcs_dependencies = [dependency for dependency in dependencies if " @ git+" in dependency]

    assert set(vcs_dependencies) == {FORGE_LLM_PIN, FORGE_SCRAPER_PIN}
    for dependency in vcs_dependencies:
        revision = dependency.rsplit("@", maxsplit=1)[-1]
        assert re.fullmatch(r"[0-9a-f]{40}", revision), dependency


def test_forge_llm_lockfiles_preserve_immutable_release() -> None:
    for relative_path in (
        "requirements/runtime-linux.lock",
        "requirements/ci-linux.lock",
    ):
        dependencies = {
            line.strip()
            for line in (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        assert FORGE_LLM_PIN in dependencies, relative_path
