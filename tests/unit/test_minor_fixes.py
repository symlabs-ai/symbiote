"""Sprint 4.2 — tests for the [Minor] code-review fixes.

- M1: render_prompt is robust to literal `{` / `}` in the template.
- M4: _format_existing_skills caps the listing at _MAX_EXISTING_SKILLS_LISTED.
- M5: kernel re-raises when skills_root was explicit; logs+continues on default.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core._review_prompts import (
    REFLECTION_PROMPT,
    SKILL_REVIEW_PROMPT,
    render_prompt,
)
from symbiote.core.background_review import (
    _MAX_EXISTING_SKILLS_LISTED,
    BackgroundReviewEngine,
)
from symbiote.core.kernel import SymbioteKernel
from symbiote.skills.loader import SkillsLoader
from symbiote.skills.store import SkillsStore

# ── M1 — render_prompt ────────────────────────────────────────────────────


class TestRenderPrompt:
    def test_handles_literal_braces_in_template(self):
        """The whole point: literal `{` / `}` must NOT be treated as format
        placeholders. This was the bug that broke Sprint 1 once."""
        template = 'Schema: {"action": "create"} -- and {placeholder}'
        out = render_prompt(template, placeholder="FILLED")
        assert out == 'Schema: {"action": "create"} -- and FILLED'

    def test_unknown_placeholders_left_literal(self):
        """No KeyError on missing values — partial renders stay visible."""
        out = render_prompt("hello {a} {b}", a="X")
        assert out == "hello X {b}"

    def test_works_on_real_prompts(self):
        """REFLECTION_PROMPT contains a literal JSON schema with braces; the
        old .format() path used to break if any `{` was added unescaped."""
        out = render_prompt(
            REFLECTION_PROMPT,
            messages="user: hi",
            existing_memories="(none)",
        )
        assert "user: hi" in out
        assert "(none)" in out
        # Literal JSON braces from _OUTPUT_SCHEMA must survive intact.
        assert '"action": "create" | "patch"' in out

    def test_skill_review_prompt_renders_with_braces_in_schema(self):
        out = render_prompt(
            SKILL_REVIEW_PROMPT,
            messages="user: x",
            existing_skills="- foo :: bar",
        )
        assert "foo :: bar" in out
        assert '"action": "create"' in out


# ── M4 — cap on existing_skills listing ───────────────────────────────────


_VALID_SKILL_MD = """\
---
name: {name}
description: {desc}
---
# {name}

body
"""


def _seed_skills(skills_dir: Path, n: int, prefix: str = "skill") -> None:
    skills_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        d = skills_dir / f"{prefix}-{i:02d}"
        d.mkdir()
        (d / "SKILL.md").write_text(
            _VALID_SKILL_MD.format(name=f"{prefix}-{i:02d}", desc=f"desc {i}")
        )


@pytest.fixture()
def adapter_m4(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "m4.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


class TestExistingSkillsCap:
    def test_listing_capped_when_library_exceeds_limit(self, tmp_path, adapter_m4):
        _seed_skills(tmp_path / "skills", n=_MAX_EXISTING_SKILLS_LISTED + 5)
        loader = SkillsLoader(tmp_path)
        store = SkillsStore(roots=[tmp_path / "skills"])

        class _NoopLLM:
            def complete(self, messages, config=None, tools=None):
                return "[]"

        engine = BackgroundReviewEngine(
            llm=_NoopLLM(),
            messages=MessageRepository(adapter_m4),
            store=store, loader=loader,
        )
        rendered = engine._format_existing_skills()
        lines = rendered.split("\n")
        # 30 listed + 1 truncation marker
        assert len(lines) == _MAX_EXISTING_SKILLS_LISTED + 1
        assert "+ 5 more not shown" in lines[-1]

    def test_no_truncation_marker_below_cap(self, tmp_path, adapter_m4):
        _seed_skills(tmp_path / "skills", n=5)
        loader = SkillsLoader(tmp_path)
        store = SkillsStore(roots=[tmp_path / "skills"])

        class _NoopLLM:
            def complete(self, messages, config=None, tools=None):
                return "[]"

        engine = BackgroundReviewEngine(
            llm=_NoopLLM(),
            messages=MessageRepository(adapter_m4),
            store=store, loader=loader,
        )
        rendered = engine._format_existing_skills()
        assert "more not shown" not in rendered
        assert len(rendered.split("\n")) == 5


# ── M5 — re-raise on explicit skills_root vs silent default ───────────────


class TestExplicitVsDefaultSkillsRoot:
    def test_default_root_unwritable_logs_and_continues(self, tmp_path, caplog):
        """Host did NOT set skills_root — derived default unwritable → log + continue."""
        # Point db_path under an unwritable parent. We simulate failure by
        # passing a SkillsStore root we know SkillsStore.__init__ will reject.
        # SkillsStore raises ValueError when roots=[] — easiest hook.
        import symbiote.core.kernel as kernel_mod

        orig = kernel_mod.SkillsStore

        class _ExplodingStore(orig):
            def __init__(self, *args, **kwargs):
                raise PermissionError("simulated FS failure")

        kernel_mod.SkillsStore = _ExplodingStore
        try:
            with caplog.at_level("WARNING"):
                cfg = KernelConfig(db_path=tmp_path / "m5.db", context_budget=4000)
                # No skills_root → default derived → must NOT raise.
                kernel = SymbioteKernel(config=cfg)
            try:
                assert kernel._skills_store is None
                assert kernel._skills_loader is None
                assert any("Skills wiring disabled" in m for m in caplog.messages)
            finally:
                kernel.shutdown()
        finally:
            kernel_mod.SkillsStore = orig

    def test_explicit_root_unwritable_raises(self, tmp_path):
        """Host EXPLICITLY set skills_root — failure must surface, not swallowed."""
        import symbiote.core.kernel as kernel_mod

        orig = kernel_mod.SkillsStore

        class _ExplodingStore(orig):
            def __init__(self, *args, **kwargs):
                raise PermissionError("simulated FS failure")

        kernel_mod.SkillsStore = _ExplodingStore
        try:
            cfg = KernelConfig(
                db_path=tmp_path / "m5b.db",
                context_budget=4000,
                skills_root=tmp_path / "explicit",  # explicit ask
            )
            with pytest.raises(PermissionError, match="simulated"):
                SymbioteKernel(config=cfg)
        finally:
            kernel_mod.SkillsStore = orig
