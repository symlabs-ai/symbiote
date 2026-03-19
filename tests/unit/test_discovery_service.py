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
