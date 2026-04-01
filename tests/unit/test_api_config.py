"""Tests for API config endpoints — PUT/GET /symbiotes/{id}/config."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKeyManager
from symbiote.api.http import app as fastapi_app
from symbiote.api.http import get_adapter, get_kernel
from symbiote.api.middleware import set_key_manager
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel


@pytest.fixture()
def setup(tmp_path: Path):
    """Set up test DB, kernel, and auth."""
    db = tmp_path / "test.db"
    adapter = SQLiteAdapter(db_path=db, check_same_thread=False)
    adapter.init_schema()

    key_mgr = APIKeyManager(adapter)
    key_mgr.init_schema()
    set_key_manager(key_mgr)

    admin_key_obj, admin_raw = key_mgr.create_key("test-tenant", "Admin", role="admin")

    config = KernelConfig(db_path=db)
    llm = MockLLMAdapter(default_response="ok")
    kernel = SymbioteKernel(config=config, llm=llm)

    fastapi_app.dependency_overrides[get_adapter] = lambda: adapter
    fastapi_app.dependency_overrides[get_kernel] = lambda: kernel

    client = TestClient(fastapi_app)

    # Create a symbiote
    resp = client.post(
        "/symbiotes",
        json={"name": "TestBot", "role": "assistant"},
        headers={"Authorization": f"Bearer {admin_raw}"},
    )
    assert resp.status_code in (200, 201), resp.text
    sym_id = resp.json()["id"]

    yield {
        "client": client,
        "api_key": admin_raw,
        "symbiote_id": sym_id,
        "kernel": kernel,
    }

    fastapi_app.dependency_overrides.clear()


class TestConfigEndpoints:
    """PUT/GET /symbiotes/{id}/config."""

    def test_get_config_defaults(self, setup):
        c, key, sid = setup["client"], setup["api_key"], setup["symbiote_id"]
        resp = c.get(f"/symbiotes/{sid}/config", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tool_mode"] == "brief"
        assert data["max_tool_iterations"] == 10
        assert data["context_mode"] == "packed"
        assert data["planner_prompt"] is None
        assert data["evaluator_prompt"] is None
        assert data["context_strategy"] == "hybrid"
        assert data["max_blocks"] == 20

    def test_set_tool_mode(self, setup):
        c, key, sid = setup["client"], setup["api_key"], setup["symbiote_id"]
        resp = c.put(
            f"/symbiotes/{sid}/config",
            json={"tool_mode": "instant"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        assert resp.json()["tool_mode"] == "instant"

        resp = c.get(f"/symbiotes/{sid}/config", headers={"Authorization": f"Bearer {key}"})
        assert resp.json()["tool_mode"] == "instant"

    def test_set_long_run_config(self, setup):
        c, key, sid = setup["client"], setup["api_key"], setup["symbiote_id"]
        resp = c.put(
            f"/symbiotes/{sid}/config",
            json={
                "tool_mode": "long_run",
                "planner_prompt": "Plan this project",
                "evaluator_prompt": "Evaluate strictly",
                "evaluator_criteria": [{"name": "quality", "weight": 1.0, "threshold": 0.7}],
                "context_strategy": "reset",
                "max_blocks": 10,
            },
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tool_mode"] == "long_run"
        assert data["planner_prompt"] == "Plan this project"
        assert data["evaluator_prompt"] == "Evaluate strictly"
        assert len(data["evaluator_criteria"]) == 1
        assert data["context_strategy"] == "reset"
        assert data["max_blocks"] == 10

    def test_partial_update(self, setup):
        c, key, sid = setup["client"], setup["api_key"], setup["symbiote_id"]
        c.put(f"/symbiotes/{sid}/config", json={"tool_mode": "long_run"},
              headers={"Authorization": f"Bearer {key}"})
        resp = c.put(f"/symbiotes/{sid}/config", json={"planner_prompt": "Custom"},
                     headers={"Authorization": f"Bearer {key}"})
        data = resp.json()
        assert data["tool_mode"] == "long_run"
        assert data["planner_prompt"] == "Custom"

    def test_set_timeouts(self, setup):
        c, key, sid = setup["client"], setup["api_key"], setup["symbiote_id"]
        resp = c.put(
            f"/symbiotes/{sid}/config",
            json={"tool_call_timeout": 15.0, "loop_timeout": 120.0, "max_tool_iterations": 6},
            headers={"Authorization": f"Bearer {key}"},
        )
        data = resp.json()
        assert data["tool_call_timeout"] == 15.0
        assert data["loop_timeout"] == 120.0
        assert data["max_tool_iterations"] == 6

    def test_set_memory_shares(self, setup):
        c, key, sid = setup["client"], setup["api_key"], setup["symbiote_id"]
        resp = c.put(
            f"/symbiotes/{sid}/config",
            json={"memory_share": 0.30, "knowledge_share": 0.20},
            headers={"Authorization": f"Bearer {key}"},
        )
        data = resp.json()
        assert data["memory_share"] == 0.30
        assert data["knowledge_share"] == 0.20

    def test_404_unknown_symbiote(self, setup):
        c, key = setup["client"], setup["api_key"]
        resp = c.get("/symbiotes/nonexistent/config", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 404
        resp = c.put("/symbiotes/nonexistent/config", json={"tool_mode": "instant"},
                     headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 404


class TestToolTagsBackwardCompat:
    """Existing tool-tags endpoint returns tool_mode."""

    def test_get_tool_tags_includes_mode(self, setup):
        c, key, sid = setup["client"], setup["api_key"], setup["symbiote_id"]
        c.put(f"/symbiotes/{sid}/config", json={"tool_mode": "long_run"},
              headers={"Authorization": f"Bearer {key}"})
        resp = c.get(f"/symbiotes/{sid}/tool-tags", headers={"Authorization": f"Bearer {key}"})
        data = resp.json()
        assert "tool_mode" in data
        assert data["tool_mode"] == "long_run"
        assert "loop" in data

    def test_put_tool_tags_with_mode(self, setup):
        c, key, sid = setup["client"], setup["api_key"], setup["symbiote_id"]
        resp = c.put(
            f"/symbiotes/{sid}/tool-tags",
            json={"tags": ["news"], "loading": "index", "tool_mode": "instant"},
            headers={"Authorization": f"Bearer {key}"},
        )
        data = resp.json()
        assert data["tool_mode"] == "instant"
        assert data["tags"] == ["news"]
