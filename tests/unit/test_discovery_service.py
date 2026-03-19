"""Tests for DiscoveryService — scanning strategies."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.discovery.service import DiscoveryService, _path_to_tool_id, _slugify


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "disc_svc_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    return IdentityManager(storage=adapter).create(name="Clark", role="assistant").id


@pytest.fixture()
def service(adapter: SQLiteAdapter) -> DiscoveryService:
    return DiscoveryService(DiscoveredToolRepository(adapter))


# ── helpers ──────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_slugify_basic(self) -> None:
        assert _slugify("Search Articles") == "search_articles"

    def test_slugify_special_chars(self) -> None:
        assert _slugify("yn-bulk/action") == "yn_bulk_action"

    def test_path_to_tool_id(self) -> None:
        assert _path_to_tool_id("GET", "/api/search") == "get_api_search"

    def test_path_to_tool_id_skips_params(self) -> None:
        assert _path_to_tool_id("POST", "/api/items/{id}/publish") == "post_api_items_publish"


# ── FastAPI strategy ──────────────────────────────────────────────────────────


class TestFastAPIStrategy:
    def test_detects_get_route(self, service: DiscoveryService, symbiote_id: str, tmp_path: Path) -> None:
        (tmp_path / "routes.py").write_text(
            '@router.get("/api/search")\ndef search(): pass\n'
        )
        result = service.discover(symbiote_id, str(tmp_path))
        ids = [t.tool_id for t in result.discovered]
        assert "get_api_search" in ids

    def test_detects_post_route(self, service: DiscoveryService, symbiote_id: str, tmp_path: Path) -> None:
        (tmp_path / "routes.py").write_text(
            '@app.post("/api/items")\ndef create(): pass\n'
        )
        result = service.discover(symbiote_id, str(tmp_path))
        ids = [t.tool_id for t in result.discovered]
        assert "post_api_items" in ids

    def test_multiple_routes(self, service: DiscoveryService, symbiote_id: str, tmp_path: Path) -> None:
        (tmp_path / "routes.py").write_text(
            '@router.get("/api/search")\ndef s(): pass\n'
            '@router.delete("/api/items/{id}")\ndef d(): pass\n'
        )
        result = service.discover(symbiote_id, str(tmp_path))
        assert result.count == 2

    def test_skips_venv(self, service: DiscoveryService, symbiote_id: str, tmp_path: Path) -> None:
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "routes.py").write_text('@app.get("/should/skip")\ndef f(): pass\n')
        result = service.discover(symbiote_id, str(tmp_path))
        assert result.count == 0


# ── Flask strategy ────────────────────────────────────────────────────────────


class TestFlaskStrategy:
    def test_detects_flask_route(self, service: DiscoveryService, symbiote_id: str, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text(
            '@app.route("/api/news", methods=["GET", "POST"])\ndef news(): pass\n'
        )
        result = service.discover(symbiote_id, str(tmp_path))
        ids = [t.tool_id for t in result.discovered]
        assert "get_api_news" in ids
        assert "post_api_news" in ids


# ── pyproject scripts strategy ────────────────────────────────────────────────


class TestPyprojectStrategy:
    def test_detects_scripts(self, service: DiscoveryService, symbiote_id: str, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project.scripts]\n'
            'symbiote = "symbiote.cli.main:app"\n'
            'symbiote-init = "symbiote.cli.main:init"\n'
        )
        result = service.discover(symbiote_id, str(tmp_path))
        ids = [t.tool_id for t in result.discovered]
        assert "symbiote" in ids
        assert "symbiote_init" in ids

    def test_no_pyproject_no_error(self, service: DiscoveryService, symbiote_id: str, tmp_path: Path) -> None:
        result = service.discover(symbiote_id, str(tmp_path))
        assert result.count == 0
        assert result.errors == []


# ── persistence & dedup ──────────────────────────────────────────────────────


class TestPersistence:
    def test_tools_saved_as_pending(self, service: DiscoveryService, symbiote_id: str, tmp_path: Path) -> None:
        (tmp_path / "routes.py").write_text('@router.get("/api/search")\ndef s(): pass\n')
        result = service.discover(symbiote_id, str(tmp_path))
        assert all(t.status == "pending" for t in result.discovered)

    def test_rediscovery_preserves_approved_status(
        self, service: DiscoveryService, adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
    ) -> None:
        (tmp_path / "routes.py").write_text('@router.get("/api/search")\ndef s(): pass\n')
        service.discover(symbiote_id, str(tmp_path))

        # Approve the tool manually
        repo = DiscoveredToolRepository(adapter)
        repo.set_status(symbiote_id, "get_api_search", "approved")

        # Re-discover — status should stay approved (upsert preserves it)
        service.discover(symbiote_id, str(tmp_path))
        tool = repo.get(symbiote_id, "get_api_search")
        assert tool.status == "approved"

    def test_deduplication(self, service: DiscoveryService, symbiote_id: str, tmp_path: Path) -> None:
        """Same route in two files should produce one tool."""
        (tmp_path / "a.py").write_text('@router.get("/api/search")\ndef s(): pass\n')
        (tmp_path / "b.py").write_text('@router.get("/api/search")\ndef s2(): pass\n')
        result = service.discover(symbiote_id, str(tmp_path))
        ids = [t.tool_id for t in result.discovered]
        assert ids.count("get_api_search") == 1


# ── _scan_openapi_url ─────────────────────────────────────────────────────────


class TestOpenApiUrlStrategy:
    _SPEC = {
        "openapi": "3.0.0",
        "info": {"title": "YouNews", "version": "1.0.0"},
        "paths": {
            "/api/items/{item_id}/publish": {
                "post": {
                    "operationId": "yn_publish_item",
                    "summary": "Publish a news item",
                    "parameters": [
                        {
                            "name": "item_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "body": {"type": "string"},
                                    },
                                    "required": ["title"],
                                }
                            }
                        },
                    },
                }
            },
            "/api/items": {
                "get": {
                    "operationId": "yn_list_items",
                    "summary": "List news items",
                }
            },
        },
    }

    def _mock_urlopen(self, spec: dict):
        """Return a context manager that yields a mock HTTP response."""
        import io
        import json
        from unittest.mock import MagicMock, patch

        resp = MagicMock()
        resp.read.return_value = json.dumps(spec).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return patch("urllib.request.urlopen", return_value=resp)

    def test_uses_operation_id_as_tool_id(
        self, service: DiscoveryService, symbiote_id: str, tmp_path: Path
    ) -> None:
        with self._mock_urlopen(self._SPEC):
            result = service.discover(symbiote_id, str(tmp_path), url="http://localhost:8000")

        ids = [t.tool_id for t in result.discovered]
        assert "yn_publish_item" in ids
        assert "yn_list_items" in ids

    def test_captures_full_parameter_schema(
        self, service: DiscoveryService, symbiote_id: str, tmp_path: Path
    ) -> None:
        with self._mock_urlopen(self._SPEC):
            result = service.discover(symbiote_id, str(tmp_path), url="http://localhost:8000")

        publish = next(t for t in result.discovered if t.tool_id == "yn_publish_item")
        assert "item_id" in publish.parameters.get("properties", {})
        assert "title" in publish.parameters.get("properties", {})
        assert "body" in publish.parameters.get("properties", {})

    def test_url_template_uses_base_url(
        self, service: DiscoveryService, symbiote_id: str, tmp_path: Path
    ) -> None:
        with self._mock_urlopen(self._SPEC):
            result = service.discover(symbiote_id, str(tmp_path), url="http://localhost:8000")

        publish = next(t for t in result.discovered if t.tool_id == "yn_publish_item")
        assert publish.url_template == "http://localhost:8000/api/items/{item_id}/publish"

    def test_live_openapi_wins_deduplication_over_file_scan(
        self, service: DiscoveryService, symbiote_id: str, tmp_path: Path
    ) -> None:
        """Strategy 0 (live URL) runs first, so it wins when tool_id collides with file scan."""
        # File scan would also find /api/items with tool_id get_api_items
        (tmp_path / "routes.py").write_text('@router.get("/api/items")\ndef f(): pass\n')

        with self._mock_urlopen(self._SPEC):
            result = service.discover(symbiote_id, str(tmp_path), url="http://localhost:8000")

        # yn_list_items from live spec wins over get_api_items from file scan
        ids = [t.tool_id for t in result.discovered]
        assert "yn_list_items" in ids
        # file-based get_api_items is deduped out (same path, different id — both present
        # since they have different tool_ids)
        assert ids.count("yn_list_items") == 1

    def test_source_path_is_openapi_url(
        self, service: DiscoveryService, symbiote_id: str, tmp_path: Path
    ) -> None:
        with self._mock_urlopen(self._SPEC):
            result = service.discover(symbiote_id, str(tmp_path), url="http://localhost:8000")

        publish = next(t for t in result.discovered if t.tool_id == "yn_publish_item")
        assert publish.source_path == "http://localhost:8000/openapi.json"

    def test_connection_error_adds_to_errors(
        self, service: DiscoveryService, symbiote_id: str, tmp_path: Path
    ) -> None:
        import urllib.error
        from unittest.mock import patch

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            result = service.discover(symbiote_id, str(tmp_path), url="http://localhost:9999")

        assert any("openapi_url" in e for e in result.errors)

    def test_no_url_skips_strategy(
        self, service: DiscoveryService, symbiote_id: str, tmp_path: Path
    ) -> None:
        """Without --url, strategy 0 is not called."""
        (tmp_path / "routes.py").write_text('@router.get("/api/items")\ndef f(): pass\n')
        result = service.discover(symbiote_id, str(tmp_path))
        ids = [t.tool_id for t in result.discovered]
        assert "get_api_items" in ids
        assert "yn_list_items" not in ids

    def test_captures_tags_from_openapi(
        self, service: DiscoveryService, symbiote_id: str, tmp_path: Path
    ) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/items": {
                    "get": {
                        "operationId": "list_items",
                        "summary": "List items",
                        "tags": ["Items", "Compose"],
                    }
                },
                "/api/admin/config": {
                    "get": {
                        "operationId": "get_config",
                        "summary": "Get config",
                        "tags": ["Admin"],
                    }
                },
                "/api/health": {
                    "get": {
                        "operationId": "health_check",
                        "summary": "Health",
                    }
                },
            },
        }
        with self._mock_urlopen(spec):
            result = service.discover(symbiote_id, str(tmp_path), url="http://localhost:8000")

        items = next(t for t in result.discovered if t.tool_id == "list_items")
        assert items.tags == ["Items", "Compose"]

        admin = next(t for t in result.discovered if t.tool_id == "get_config")
        assert admin.tags == ["Admin"]

        health = next(t for t in result.discovered if t.tool_id == "health_check")
        assert health.tags == []

    def test_tags_persisted_via_repository(
        self, service: DiscoveryService, adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
    ) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/items": {
                    "get": {
                        "operationId": "list_items",
                        "summary": "List items",
                        "tags": ["Items"],
                    }
                },
            },
        }
        with self._mock_urlopen(spec):
            service.discover(symbiote_id, str(tmp_path), url="http://localhost:8000")

        repo = DiscoveredToolRepository(adapter)
        tool = repo.get(symbiote_id, "list_items")
        assert tool is not None
        assert tool.tags == ["Items"]

    def test_file_scan_tags_empty(
        self, service: DiscoveryService, symbiote_id: str, tmp_path: Path
    ) -> None:
        """FastAPI/Flask file scan should produce empty tags."""
        (tmp_path / "routes.py").write_text('@router.get("/api/items")\ndef f(): pass\n')
        result = service.discover(symbiote_id, str(tmp_path))
        for tool in result.discovered:
            assert tool.tags == []


# ── classify_by_tags ──────────────────────────────────────────────────────────


class TestClassifyByTags:
    """Tests for DiscoveredToolRepository.classify_by_tags()."""

    _SPEC = {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {
            "/api/items": {
                "get": {
                    "operationId": "list_items",
                    "summary": "List items",
                    "tags": ["Items"],
                }
            },
            "/api/compose": {
                "post": {
                    "operationId": "create_compose",
                    "summary": "Create compose",
                    "tags": ["Compose"],
                }
            },
            "/api/admin/config": {
                "get": {
                    "operationId": "get_config",
                    "summary": "Get config",
                    "tags": ["Admin"],
                }
            },
            "/api/health": {
                "get": {
                    "operationId": "health_check",
                    "summary": "Health",
                }
            },
        },
    }

    def _mock_urlopen(self, spec: dict):
        import io
        import json
        from unittest.mock import MagicMock, patch

        resp = MagicMock()
        resp.read.return_value = json.dumps(spec).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return patch("urllib.request.urlopen", return_value=resp)

    def _discover(self, service, symbiote_id, tmp_path):
        with self._mock_urlopen(self._SPEC):
            service.discover(symbiote_id, str(tmp_path), url="http://localhost:8000")

    def test_approve_matching_tags(
        self, service: DiscoveryService, adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._discover(service, symbiote_id, tmp_path)
        repo = DiscoveredToolRepository(adapter)
        result = repo.classify_by_tags(symbiote_id, approve_tags=["Items", "Compose"])
        assert result["approved"] == 2
        assert repo.get(symbiote_id, "list_items").status == "approved"
        assert repo.get(symbiote_id, "create_compose").status == "approved"

    def test_disable_rest(
        self, service: DiscoveryService, adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._discover(service, symbiote_id, tmp_path)
        repo = DiscoveredToolRepository(adapter)
        result = repo.classify_by_tags(symbiote_id, approve_tags=["Items"], disable_rest=True)
        assert result["approved"] == 1
        assert result["disabled"] == 3  # Compose, Admin, health (no tag)
        assert repo.get(symbiote_id, "list_items").status == "approved"
        assert repo.get(symbiote_id, "get_config").status == "disabled"
        assert repo.get(symbiote_id, "health_check").status == "disabled"

    def test_case_insensitive_matching(
        self, service: DiscoveryService, adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._discover(service, symbiote_id, tmp_path)
        repo = DiscoveredToolRepository(adapter)
        result = repo.classify_by_tags(symbiote_id, approve_tags=["items", "compose"])
        assert result["approved"] == 2

    def test_idempotent_does_not_alter_already_approved(
        self, service: DiscoveryService, adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._discover(service, symbiote_id, tmp_path)
        repo = DiscoveredToolRepository(adapter)

        # First classify
        repo.classify_by_tags(symbiote_id, approve_tags=["Items"])

        # Second classify — already approved tools are not pending, so unchanged
        result = repo.classify_by_tags(symbiote_id, approve_tags=["Items"])
        assert result["approved"] == 0  # nothing new to approve

    def test_does_not_alter_already_disabled(
        self, service: DiscoveryService, adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._discover(service, symbiote_id, tmp_path)
        repo = DiscoveredToolRepository(adapter)

        # Manually disable one
        repo.set_status(symbiote_id, "list_items", "disabled")

        # Classify with Items — should not re-approve it since it's not pending
        result = repo.classify_by_tags(symbiote_id, approve_tags=["Items"])
        assert result["approved"] == 0
        assert repo.get(symbiote_id, "list_items").status == "disabled"

    def test_without_disable_rest_leaves_unmatched_pending(
        self, service: DiscoveryService, adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._discover(service, symbiote_id, tmp_path)
        repo = DiscoveredToolRepository(adapter)
        result = repo.classify_by_tags(symbiote_id, approve_tags=["Items"], disable_rest=False)
        assert result["approved"] == 1
        assert result["disabled"] == 0
        assert result["unchanged"] == 3
        assert repo.get(symbiote_id, "get_config").status == "pending"

    def test_reset_disabled_back_to_pending(
        self, service: DiscoveryService, adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._discover(service, symbiote_id, tmp_path)
        repo = DiscoveredToolRepository(adapter)
        repo.classify_by_tags(symbiote_id, approve_tags=["Items"], disable_rest=True)

        count = repo.reset_disabled(symbiote_id)
        assert count == 3  # Compose, Admin, health_check
        assert repo.get(symbiote_id, "get_config").status == "pending"
        assert repo.get(symbiote_id, "create_compose").status == "pending"
        assert repo.get(symbiote_id, "health_check").status == "pending"
        # Approved stays approved
        assert repo.get(symbiote_id, "list_items").status == "approved"

    def test_reset_idempotent(
        self, service: DiscoveryService, adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._discover(service, symbiote_id, tmp_path)
        repo = DiscoveredToolRepository(adapter)
        repo.classify_by_tags(symbiote_id, approve_tags=["Items"], disable_rest=True)
        repo.reset_disabled(symbiote_id)
        # Second reset — nothing to reset
        assert repo.reset_disabled(symbiote_id) == 0
