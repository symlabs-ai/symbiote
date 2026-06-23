"""Tests for HTTP API — T-22."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import symbiote.api.http as http_mod
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKeyManager
from symbiote.api.http import _ensure_local_admin_key, _resolve_config, app, get_adapter
from symbiote.core.identity import IdentityManager
from symbiote.core.models import MemoryEntry
from symbiote.core.session import SessionManager
from symbiote.memory.store import MemoryStore


class TestLocalAdminMode:
    """SYMBIOTE_LOCAL_ADMIN auto-provisions an admin key for the Console."""

    @pytest.fixture()
    def key_manager(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> APIKeyManager:
        # Always start from a clean module global.
        monkeypatch.setattr(http_mod, "_local_admin_key", None)
        adp = SQLiteAdapter(db_path=tmp_path / "admin.db", check_same_thread=False)
        adp.init_schema()
        mgr = APIKeyManager(adp)
        mgr.init_schema()
        yield mgr
        adp.close()

    def test_no_provision_without_flag(
        self, key_manager: APIKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SYMBIOTE_LOCAL_ADMIN", raising=False)
        _ensure_local_admin_key(key_manager)
        assert http_mod._local_admin_key is None
        assert key_manager.list_keys("default") == []

    def test_provisions_admin_key_with_flag(
        self, key_manager: APIKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYMBIOTE_LOCAL_ADMIN", "1")
        _ensure_local_admin_key(key_manager)
        raw = http_mod._local_admin_key
        assert raw and raw.startswith("sk-symbiote_")
        keys = key_manager.list_keys("default")
        assert len(keys) == 1
        assert keys[0].role == "admin"
        assert keys[0].name == "local-console"
        # the injected raw key actually validates as admin
        validated = key_manager.validate_key(raw)
        assert validated is not None
        assert validated.role == "admin"

    def test_revokes_stale_local_console_keys(
        self, key_manager: APIKeyManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYMBIOTE_LOCAL_ADMIN", "1")
        stale, _ = key_manager.create_key("default", "local-console", role="admin")
        _ensure_local_admin_key(key_manager)
        active = [k for k in key_manager.list_keys("default") if k.is_active]
        assert len(active) == 1
        assert active[0].id != stale.id


_SKILL_ENV = (
    "SYMBIOTE_SKILL_SCOPE",
    "SYMBIOTE_SKILLS_ROOT",
    "SYMBIOTE_SKILLS_PROTECTED_ROOTS",
)


class TestResolveConfig:
    def test_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SYMBIOTE_DB_PATH", raising=False)
        assert _resolve_config().db_path == Path(".symbiote/symbiote.db")

    def test_db_path_overridden_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYMBIOTE_DB_PATH", "/tmp/embedded/symbiote.db")
        assert _resolve_config().db_path == Path("/tmp/embedded/symbiote.db")

    # ── skill scope (multi-tenant safety) ──────────────────────────────

    def test_api_defaults_to_per_symbiote(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # API entrypoint is multi-tenant → secure default, unlike KernelConfig.
        for e in _SKILL_ENV:
            monkeypatch.delenv(e, raising=False)
        assert _resolve_config().skill_scope == "per_symbiote"

    def test_kernel_config_default_still_global(self) -> None:
        # The model default itself is unchanged (CLI / embedded untouched).
        from symbiote.config.models import KernelConfig
        assert KernelConfig().skill_scope == "global"

    def test_scope_env_global_honored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYMBIOTE_SKILL_SCOPE", "global")
        assert _resolve_config().skill_scope == "global"

    def test_scope_env_per_symbiote_honored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYMBIOTE_SKILL_SCOPE", "per_symbiote")
        assert _resolve_config().skill_scope == "per_symbiote"

    def test_skills_root_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYMBIOTE_SKILLS_ROOT", "/data/skills")
        assert _resolve_config().skills_root == Path("/data/skills")

    def test_protected_roots_comma_separated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "SYMBIOTE_SKILLS_PROTECTED_ROOTS", "/a/skills,/b/skills"
        )
        assert _resolve_config().skills_protected_roots == [
            Path("/a/skills"), Path("/b/skills")
        ]

    def test_protected_roots_pathsep_separated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os
        monkeypatch.setenv(
            "SYMBIOTE_SKILLS_PROTECTED_ROOTS",
            f"/a/skills{os.pathsep}/b/skills",
        )
        assert _resolve_config().skills_protected_roots == [
            Path("/a/skills"), Path("/b/skills")
        ]

    def test_default_emits_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        for e in _SKILL_ENV:
            monkeypatch.delenv(e, raising=False)
        import logging
        with caplog.at_level(logging.WARNING):
            _resolve_config()
        assert any("SYMBIOTE_SKILL_SCOPE" in r.message for r in caplog.records)

    def test_explicit_scope_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("SYMBIOTE_SKILL_SCOPE", "per_symbiote")
        import logging
        with caplog.at_level(logging.WARNING):
            _resolve_config()
        assert not any(
            "SYMBIOTE_SKILL_SCOPE not set" in r.message for r in caplog.records
        )

    def test_api_config_yields_tenant_isolated_kernel(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # End-to-end: a kernel built from the API's resolved config (default
        # per_symbiote) isolates skills across tenants.
        from symbiote.core.kernel import SymbioteKernel
        from symbiote.skills import usage

        for e in _SKILL_ENV:
            monkeypatch.delenv(e, raising=False)
        monkeypatch.setenv("SYMBIOTE_DB_PATH", str(tmp_path / "api.db"))
        monkeypatch.setenv("SYMBIOTE_SKILLS_ROOT", str(tmp_path / "skills"))

        cfg = _resolve_config()
        assert cfg.skill_scope == "per_symbiote"
        kernel = SymbioteKernel(cfg, llm=None)
        try:
            t1 = kernel.create_symbiote(name="Tenant1", role="a").id
            t2 = kernel.create_symbiote(name="Tenant2", role="a").id
            kernel.skills_store_for(t1).create(
                "t1-skill", "---\nname: t1-skill\ndescription: x\n---\nbody"
            )
            kernel.skills_loader_for(t1).refresh()
            kernel.skills_loader_for(t2).refresh()
            assert "t1-skill" in {
                s.name for s in kernel.skills_loader_for(t1).list_skills()
            }
            assert "t1-skill" not in {
                s.name for s in kernel.skills_loader_for(t2).list_skills()
            }
            # single (global) handle is None in scoped mode
            assert kernel.skills_loader is None
        finally:
            kernel.shutdown()


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "http_test.db"
    adp = SQLiteAdapter(db_path=db, check_same_thread=False)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def client(adapter: SQLiteAdapter) -> TestClient:
    """TestClient that overrides the DB dependency with a tmp_path adapter."""

    def _override_adapter() -> SQLiteAdapter:
        return adapter

    app.dependency_overrides[get_adapter] = _override_adapter
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── POST /symbiotes ───────────────────────────────────────────────────────


class TestCreateSymbiote:
    def test_create_symbiote_returns_201(self, client: TestClient) -> None:
        resp = client.post(
            "/symbiotes",
            json={"name": "Cody", "role": "coder"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["name"] == "Cody"
        assert data["role"] == "coder"
        assert data["status"] == "active"

    def test_create_symbiote_with_persona(self, client: TestClient) -> None:
        resp = client.post(
            "/symbiotes",
            json={
                "name": "Sage",
                "role": "advisor",
                "persona_json": {"tone": "formal"},
            },
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "Sage"


# ── GET /symbiotes/{id} ──────────────────────────────────────────────────


class TestGetSymbiote:
    def test_get_symbiote_returns_200(self, client: TestClient) -> None:
        create_resp = client.post(
            "/symbiotes",
            json={"name": "Bot", "role": "helper"},
        )
        sym_id = create_resp.json()["id"]

        resp = client.get(f"/symbiotes/{sym_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sym_id
        assert data["name"] == "Bot"

    def test_get_symbiote_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.get("/symbiotes/nonexistent-id")
        assert resp.status_code == 404


# ── POST /sessions ────────────────────────────────────────────────────────


class TestCreateSession:
    def test_create_session_returns_201(self, client: TestClient) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()

        resp = client.post(
            "/sessions",
            json={"symbiote_id": sym["id"]},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["symbiote_id"] == sym["id"]
        assert data["status"] == "active"

    def test_create_session_with_goal(self, client: TestClient) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()

        resp = client.post(
            "/sessions",
            json={"symbiote_id": sym["id"], "goal": "Fix bug"},
        )
        assert resp.status_code == 201
        assert resp.json()["goal"] == "Fix bug"


# ── POST /sessions/{id}/messages ──────────────────────────────────────────


class TestAddMessage:
    def test_add_message_returns_201(self, client: TestClient) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()
        sess = client.post(
            "/sessions", json={"symbiote_id": sym["id"]}
        ).json()

        resp = client.post(
            f"/sessions/{sess['id']}/messages",
            json={"role": "user", "content": "Hello!"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["role"] == "user"
        assert data["content"] == "Hello!"
        assert "id" in data
        assert "created_at" in data

    def test_add_message_session_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/sessions/nonexistent-id/messages",
            json={"role": "user", "content": "Hello!"},
        )
        assert resp.status_code == 404


# ── POST /sessions/{id}/close ────────────────────────────────────────────


class TestCloseSession:
    def test_close_session_returns_200(self, client: TestClient) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()
        sess = client.post(
            "/sessions", json={"symbiote_id": sym["id"]}
        ).json()

        resp = client.post(f"/sessions/{sess['id']}/close")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sess["id"]
        assert data["status"] == "closed"
        assert "summary" in data

    def test_close_session_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.post("/sessions/nonexistent-id/close")
        assert resp.status_code == 404


# ── GET /sessions/{id} ───────────────────────────────────────────────────


class TestGetSession:
    def test_get_session_returns_200(self, client: TestClient) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()
        sess = client.post(
            "/sessions", json={"symbiote_id": sym["id"]}
        ).json()

        resp = client.get(f"/sessions/{sess['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sess["id"]
        assert data["symbiote_id"] == sym["id"]

    def test_get_session_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.get("/sessions/nonexistent-id")
        assert resp.status_code == 404


# ── GET /memory/search ───────────────────────────────────────────────────


class TestMemorySearch:
    def test_search_returns_200_empty_list(self, client: TestClient) -> None:
        resp = client.get("/memory/search", params={"query": "anything"})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_search_returns_matching_entries(
        self, client: TestClient, adapter: SQLiteAdapter
    ) -> None:
        # Seed a symbiote and a memory entry directly
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()

        store = MemoryStore(storage=adapter)
        entry = MemoryEntry(
            symbiote_id=sym["id"],
            type="factual",
            scope="global",
            content="Python is great",
            source="user",
        )
        store.store(entry)

        resp = client.get("/memory/search", params={"query": "Python"})
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        assert results[0]["content"] == "Python is great"

    def test_search_with_scope_filter(
        self, client: TestClient, adapter: SQLiteAdapter
    ) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()

        store = MemoryStore(storage=adapter)
        entry = MemoryEntry(
            symbiote_id=sym["id"],
            type="factual",
            scope="project",
            content="Rust is fast",
            source="user",
        )
        store.store(entry)

        resp = client.get(
            "/memory/search",
            params={"query": "Rust", "scope": "project"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

        # Different scope should not match
        resp2 = client.get(
            "/memory/search",
            params={"query": "Rust", "scope": "session"},
        )
        assert resp2.status_code == 200
        assert len(resp2.json()) == 0

    def test_search_respects_limit(
        self, client: TestClient, adapter: SQLiteAdapter
    ) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()

        store = MemoryStore(storage=adapter)
        for i in range(5):
            entry = MemoryEntry(
                symbiote_id=sym["id"],
                type="factual",
                scope="global",
                content=f"fact number {i}",
                source="user",
            )
            store.store(entry)

        resp = client.get(
            "/memory/search",
            params={"query": "fact", "limit": 2},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2


class TestPersonaRoundTrip:
    """persona_json composed by the Console editor round-trips through the API."""

    def test_create_get_update_persona(self, client: TestClient) -> None:
        persona = {"system_prompt": "You are Atlas.", "tone": "professional"}
        created = client.post(
            "/symbiotes", json={"name": "Atlas", "role": "assistant", "persona_json": persona}
        )
        assert created.status_code == 201
        sid = created.json()["id"]

        got = client.get(f"/symbiotes/{sid}")
        assert got.status_code == 200
        assert got.json()["persona_json"] == persona

        # Edit the system prompt (what the structured editor does) and persist.
        updated = {"system_prompt": "You are Atlas v2.", "tone": "professional"}
        put = client.put(
            f"/symbiotes/{sid}",
            json={"name": "Atlas", "role": "assistant", "persona_json": updated},
        )
        assert put.status_code == 200
        assert client.get(f"/symbiotes/{sid}").json()["persona_json"] == updated


class TestDebugEndpoints:
    """Read endpoints feeding the Memory/Activity console tab (item 3)."""

    @staticmethod
    def _seed_symbiote(adapter: SQLiteAdapter, sid: str) -> None:
        adapter.execute(
            "INSERT INTO symbiotes (id, name, role, status) VALUES (?, ?, ?, ?)",
            (sid, "Dbg", "assistant", "active"),
        )

    def test_memory_endpoint(self, client: TestClient, adapter: SQLiteAdapter) -> None:
        sid = "sym-dbg-mem"
        self._seed_symbiote(adapter, sid)
        adapter.execute(
            "INSERT INTO memory_entries (id, symbiote_id, type, scope, content, "
            "created_at, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
            ("m1", sid, "preference", "long_term", "likes dark mode", "2026-01-01"),
        )
        # inactive row must be excluded
        adapter.execute(
            "INSERT INTO memory_entries (id, symbiote_id, type, scope, content, "
            "created_at, is_active) VALUES (?, ?, ?, ?, ?, ?, 0)",
            ("m2", sid, "factual", "long_term", "stale", "2026-01-02"),
        )
        resp = client.get(f"/api/symbiotes/{sid}/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["content"] == "likes dark mode"

    def test_reflections_endpoint(self, client: TestClient, adapter: SQLiteAdapter) -> None:
        sid = "sym-dbg-ref"
        adapter.execute(
            "INSERT INTO reflection_audit (id, session_id, symbiote_id, mode, "
            "keyword_facts_json, llm_facts_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r1", "s1", sid, "full", '["a", "b"]', "[]", "2026-01-01"),
        )
        resp = client.get(f"/api/symbiotes/{sid}/reflections")
        assert resp.status_code == 200
        assert resp.json()[0]["mode"] == "full"

    def test_activity_endpoint(self, client: TestClient, adapter: SQLiteAdapter) -> None:
        sid = "sym-dbg-act"
        self._seed_symbiote(adapter, sid)
        adapter.execute(
            "INSERT INTO audit_log (id, symbiote_id, session_id, tool_id, action, "
            "result, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a1", sid, "s1", "fs_read", "read", "ok", "2026-01-01"),
        )
        resp = client.get(f"/api/symbiotes/{sid}/activity")
        assert resp.status_code == 200
        assert resp.json()[0]["tool_id"] == "fs_read"


class TestHarnessEndpoints:
    """Harness version list/create/rollback feeding the Harness tab (item 4)."""

    def test_create_list_summary_rollback(self, client: TestClient) -> None:
        sid = "sym-harness"
        comp = "tool_instructions"

        r1 = client.post(f"/symbiotes/{sid}/harness/{comp}", json={"content": "v1 text"})
        assert r1.status_code == 201
        assert r1.json()["version"] == 1
        r2 = client.post(f"/symbiotes/{sid}/harness/{comp}", json={"content": "v2 text"})
        assert r2.json()["version"] == 2

        versions = client.get(f"/api/symbiotes/{sid}/harness/{comp}").json()
        assert len(versions) == 2
        active = [v for v in versions if v["is_active"]]
        assert len(active) == 1
        assert active[0]["version"] == 2

        summary = client.get(f"/api/symbiotes/{sid}/harness").json()
        comp_row = next(c for c in summary if c["component"] == comp)
        assert comp_row["active_version"] == 2
        assert comp_row["has_versions"] is True

        rb = client.post(f"/symbiotes/{sid}/harness/{comp}/rollback")
        assert rb.status_code == 200
        assert rb.json()["active_version"] == 1

    def test_summary_lists_base_components_without_versions(self, client: TestClient) -> None:
        sid = "sym-harness-empty"
        summary = client.get(f"/api/symbiotes/{sid}/harness").json()
        comps = {c["component"]: c for c in summary}
        assert "tool_instructions" in comps
        assert comps["tool_instructions"]["has_versions"] is False

    def test_rollback_without_previous_returns_404(self, client: TestClient) -> None:
        sid = "sym-harness-single"
        client.post(f"/symbiotes/{sid}/harness/tool_instructions", json={"content": "only"})
        rb = client.post(f"/symbiotes/{sid}/harness/tool_instructions/rollback")
        assert rb.status_code == 404
