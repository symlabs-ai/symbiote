"""Sprint 4.1 hardening tests — race conditions and concurrency caps.

Pins the 4 fixes from code review:
- H1: kernel._background_review_for lazy-build is serialized (Lock).
- H2: BackgroundReviewEngine.spawn deduplicates by session_id.
- H3: SkillsLoader.refresh swaps atomically (no dict-changed-size during iter).
- H4: max_active vs max_quarantine bookkeeping is separate.

H4 is covered in test_background_review.py::TestQuarantineCap. This file covers
H1, H2, H3.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.background_review import BackgroundReviewEngine
from symbiote.core.kernel import SymbioteKernel
from symbiote.skills import usage
from symbiote.skills.loader import SkillsLoader
from symbiote.skills.store import SkillsStore

_VALID_SKILL_MD = """\
---
name: {name}
description: {desc}
---
# {name}

{body}
"""


# ── H1 — Lock on _background_review_for ───────────────────────────────────


class _CountingLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, config=None, tools=None):
        self.calls += 1
        return "ok"


class TestKernelLazyBuildLock:
    def test_concurrent_calls_build_engine_once(self, tmp_path):
        """N threads calling _background_review_for simultaneously must
        receive the SAME engine instance. Without the lock, multiple would
        observe None on the first read and build rival engines.

        We monkey-patch ``_environment.get_config`` to a fast in-memory
        function so the test isolates the lock's contract from SQLite's
        single-cursor thread safety (an independent concern of the adapter).
        """
        cfg = KernelConfig(db_path=tmp_path / "h1.db", context_budget=4000)
        llm = _CountingLLM()
        kernel = SymbioteKernel(config=cfg, llm=llm)
        try:
            kernel.set_evolver_llm(_CountingLLM())
            sym = kernel.create_symbiote(name="h1bot", role="assistant")
            kernel._environment.configure(
                symbiote_id=sym.id,
                skill_review_enabled=True,
            )

            # Snapshot the config once (outside the race) and serve it from
            # memory. Real callers hit get_config from a single thread.
            cached_cfg = kernel._environment.get_config(sym.id)
            kernel._environment.get_config = lambda *a, **kw: cached_cfg

            # Skip the per-call get_messages SQLite touch the engine would
            # do later — not exercised by this test.

            results: list[object] = []
            results_lock = threading.Lock()
            barrier = threading.Barrier(8)

            def race():
                barrier.wait()  # all threads release simultaneously
                engine = kernel._background_review_for(sym.id)
                with results_lock:
                    results.append(engine)

            threads = [threading.Thread(target=race) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

            assert all(r is not None for r in results)
            assert len({id(r) for r in results}) == 1, (
                f"Expected one engine instance, got {len({id(r) for r in results})}"
            )
        finally:
            kernel.shutdown()


# ── H2 — BackgroundReviewEngine deduplicates spawn by session_id ──────────


class _SlowLLM:
    """Sleeps for `delay` before returning, so spawned threads stay alive
    long enough to observe in tests."""

    def __init__(self, response: str, delay: float = 0.5):
        self.response = response
        self.delay = delay
        self.calls = 0

    def complete(self, messages, config=None, tools=None):
        self.calls += 1
        time.sleep(self.delay)
        return self.response


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "h2.db"
    # check_same_thread=False because the test runs the daemon threads
    # against this SQLite connection (production wiring sets the same
    # flag — see core/kernel.py:SQLiteAdapter(check_same_thread=False)).
    adp = SQLiteAdapter(db_path=db, check_same_thread=False)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def session_id_h2(adapter):
    from symbiote.core.identity import IdentityManager
    sym = IdentityManager(storage=adapter).create(name="h2", role="assistant")
    sid = str(uuid4())
    adapter.execute(
        "INSERT INTO sessions (id, symbiote_id, status, started_at) "
        "VALUES (?, ?, 'active', datetime('now'))",
        (sid, sym.id),
    )
    adapter.execute(
        "INSERT INTO messages (id, session_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (str(uuid4()), sid, "user", "any message"),
    )
    return sid, sym.id


class TestSpawnDeduplication:
    def test_concurrent_spawn_for_same_session_returns_single_thread(
        self, tmp_path, adapter, session_id_h2
    ):
        sid, sym_id = session_id_h2
        store = SkillsStore(roots=[tmp_path / "skills"])
        loader = SkillsLoader(tmp_path)
        engine = BackgroundReviewEngine(
            llm=_SlowLLM("[]", delay=0.3),
            messages=MessageRepository(adapter),
            store=store, loader=loader,
        )

        t1 = engine.spawn(sid, sym_id)
        t2 = engine.spawn(sid, sym_id)  # while t1 is still in flight
        t3 = engine.spawn(sid, sym_id)

        # All three calls must reuse the same in-flight Thread.
        assert t1 is t2
        assert t2 is t3
        t1.join(timeout=5.0)
        assert not t1.is_alive()

    def test_different_sessions_get_independent_threads(
        self, tmp_path, adapter
    ):
        from symbiote.core.identity import IdentityManager
        sym = IdentityManager(storage=adapter).create(name="multi", role="assistant")
        sids = []
        for _ in range(2):
            s = str(uuid4())
            adapter.execute(
                "INSERT INTO sessions (id, symbiote_id, status, started_at) "
                "VALUES (?, ?, 'active', datetime('now'))",
                (s, sym.id),
            )
            adapter.execute(
                "INSERT INTO messages (id, session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (str(uuid4()), s, "user", "msg"),
            )
            sids.append(s)

        store = SkillsStore(roots=[tmp_path / "skills"])
        loader = SkillsLoader(tmp_path)
        engine = BackgroundReviewEngine(
            llm=_SlowLLM("[]", delay=0.2),
            messages=MessageRepository(adapter),
            store=store, loader=loader,
        )

        t_a = engine.spawn(sids[0], sym.id)
        t_b = engine.spawn(sids[1], sym.id)
        assert t_a is not t_b
        t_a.join(timeout=5.0)
        t_b.join(timeout=5.0)

    def test_slot_freed_after_thread_completes(
        self, tmp_path, adapter, session_id_h2
    ):
        """After the thread finishes, the session slot must be released so
        the next spawn creates a fresh thread (not reuse a dead one)."""
        sid, sym_id = session_id_h2
        store = SkillsStore(roots=[tmp_path / "skills"])
        loader = SkillsLoader(tmp_path)
        engine = BackgroundReviewEngine(
            llm=_SlowLLM("[]", delay=0.1),
            messages=MessageRepository(adapter),
            store=store, loader=loader,
        )

        t1 = engine.spawn(sid, sym_id)
        t1.join(timeout=5.0)
        # First slot released.
        assert sid not in engine._active

        t2 = engine.spawn(sid, sym_id)
        assert t2 is not t1
        t2.join(timeout=5.0)


# ── H3 — SkillsLoader.refresh atomic ref swap ─────────────────────────────


class TestLoaderAtomicRefresh:
    def test_concurrent_refresh_and_iteration_no_runtime_error(self, tmp_path):
        """Build 30 skills, run a tight loop calling list_skills/build_summary
        in one thread while another thread spams refresh(). Without the atomic
        swap, the iterating thread used to crash with
        'dictionary changed size during iteration'."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        for i in range(30):
            d = skills_dir / f"skill-{i:02d}"
            d.mkdir()
            (d / "SKILL.md").write_text(
                _VALID_SKILL_MD.format(name=f"skill-{i:02d}", desc="d", body="b")
            )
        loader = SkillsLoader(tmp_path)

        errors: list[Exception] = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    _ = loader.list_skills()
                    _ = loader.build_summary()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def refresher():
            try:
                for _ in range(50):
                    loader.refresh()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        reader_threads = [threading.Thread(target=reader) for _ in range(3)]
        refresher_thread = threading.Thread(target=refresher)

        for t in reader_threads:
            t.start()
        refresher_thread.start()

        refresher_thread.join(timeout=10.0)
        stop.set()
        for t in reader_threads:
            t.join(timeout=2.0)

        assert errors == [], f"concurrent refresh/iter raised: {errors}"
        # And the final state is correct.
        assert len(loader.list_skills()) == 30

    def test_refresh_picks_up_new_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "first").mkdir()
        (skills_dir / "first" / "SKILL.md").write_text(
            _VALID_SKILL_MD.format(name="first", desc="d", body="b")
        )
        loader = SkillsLoader(tmp_path)
        assert {s.name for s in loader.list_skills()} == {"first"}

        (skills_dir / "second").mkdir()
        (skills_dir / "second" / "SKILL.md").write_text(
            _VALID_SKILL_MD.format(name="second", desc="d", body="b")
        )
        loader.refresh()
        assert {s.name for s in loader.list_skills()} == {"first", "second"}

    def test_refresh_drops_archived(self, tmp_path):
        """Atomic swap must also drop skills whose status flipped to archived."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        d = skills_dir / "doomed"
        d.mkdir()
        (d / "SKILL.md").write_text(
            _VALID_SKILL_MD.format(name="doomed", desc="d", body="b")
        )
        # Quarantine status (agent_created) — visible to list_skills.
        usage.write_meta(d, usage.default_meta(agent_created=True))
        loader = SkillsLoader(tmp_path)
        assert loader.get_skill("doomed") is not None

        usage.set_status(d, usage.STATUS_ARCHIVED)
        loader.refresh()
        assert loader.get_skill("doomed") is None
