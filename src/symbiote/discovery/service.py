"""DiscoveryService — scan a repository and register discovered tools."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from symbiote.discovery.models import DiscoveredTool
from symbiote.discovery.repository import DiscoveredToolRepository


class DiscoveryResult:
    """Summary of a discovery run."""

    def __init__(self, symbiote_id: str, source_path: str) -> None:
        self.symbiote_id = symbiote_id
        self.source_path = source_path
        self.discovered: list[DiscoveredTool] = []
        self.errors: list[str] = []

    @property
    def count(self) -> int:
        return len(self.discovered)


class DiscoveryService:
    """Scans a repository for APIs, routes and CLI scripts, persists as discovered_tools.

    Strategies (applied in order):
    1. OpenAPI / Swagger specs (openapi.json, swagger.yaml, openapi.yaml)
    2. FastAPI route decorators (@app.get, @router.post, etc.)
    3. Flask route decorators (@app.route)
    4. pyproject.toml [project.scripts] entries
    """

    def __init__(self, repository: DiscoveredToolRepository) -> None:
        self._repo = repository

    def discover(
        self,
        symbiote_id: str,
        source_path: str,
        url: str | None = None,
    ) -> DiscoveryResult:
        """Scan *source_path* and persist discovered tools for *symbiote_id*.

        When *url* is provided (e.g. ``"http://localhost:8000"``), the service
        fetches ``{url}/openapi.json`` from the running server and uses its
        ``operationId`` values as tool_ids and full Pydantic schemas as
        parameter schemas.  Live OpenAPI results take priority over file-based
        scanning in deduplication (first-seen wins).

        Existing tools (same symbiote_id + tool_id) keep their approval status
        unless their method, URL, parameters or risk changed: then they go
        back to "pending" and need to be approved again.
        """
        root = Path(source_path).resolve()
        result = DiscoveryResult(symbiote_id=symbiote_id, source_path=str(root))
        now = datetime.now(tz=UTC).isoformat()

        tools: list[DiscoveredTool] = []

        # Strategy 0: Live OpenAPI from running server (highest priority)
        # When a live URL is provided, skip file-based scanning (strategies 1-3)
        # since the live spec is authoritative and has richer data (operationId,
        # full Pydantic schemas, tags).
        if url:
            tools.extend(self._scan_openapi_url(symbiote_id, url, now, result))
        else:
            # Strategy 1: OpenAPI specs in repository files
            tools.extend(self._scan_openapi(symbiote_id, root, now, result))

            # Strategy 2: FastAPI routes
            tools.extend(self._scan_fastapi(symbiote_id, root, now, result))

            # Strategy 3: Flask routes
            tools.extend(self._scan_flask(symbiote_id, root, now, result))

        # Strategy 4: pyproject.toml scripts (always — CLI tools complement HTTP)
        tools.extend(self._scan_pyproject_scripts(symbiote_id, root, now, result))

        # Deduplicate by tool_id (first wins); a different operation that
        # maps to the same id is reported instead of silently dropped.
        seen: dict[str, DiscoveredTool] = {}
        for tool in tools:
            first = seen.get(tool.tool_id)
            if first is None:
                seen[tool.tool_id] = tool
                self._repo.save(tool)
                result.discovered.append(tool)
            elif (first.method, first.url_template) != (tool.method, tool.url_template):
                result.errors.append(
                    f"tool_id collision: {tool.tool_id!r} for {tool.method} "
                    f"{tool.url_template} skipped (already used by {first.method} "
                    f"{first.url_template})"
                )

        return result

    # ── strategies ────────────────────────────────────────────────────────

    def _scan_openapi_url(
        self,
        symbiote_id: str,
        url: str,
        now: str,
        result: DiscoveryResult,
    ) -> list[DiscoveredTool]:
        """Fetch /openapi.json from a live server and extract tools.

        Uses ``operationId`` as ``tool_id`` (semantic, set by the framework)
        and captures full parameter schemas from the Pydantic-generated spec.
        """
        import urllib.error
        import urllib.request
        from urllib.parse import urlsplit

        if urlsplit(url).scheme not in ("http", "https"):
            result.errors.append(f"openapi_url:{url}: only http(s) URLs are allowed")
            return []

        base_url = url.rstrip("/")
        openapi_url = f"{base_url}/openapi.json"
        try:
            with urllib.request.urlopen(openapi_url, timeout=10) as resp:
                spec = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            result.errors.append(f"openapi_url:{openapi_url}: {exc}")
            return []
        except Exception as exc:
            result.errors.append(f"openapi_url:{openapi_url}: {exc}")
            return []

        if not isinstance(spec, dict) or "paths" not in spec:
            result.errors.append(f"openapi_url:{openapi_url}: invalid OpenAPI spec")
            return []

        tools: list[DiscoveredTool] = []
        for path, method, op in _iter_operations(spec, result.errors):
            op_id = op.get("operationId") or _path_to_tool_id(method, path)
            summary = op.get("summary") or op.get("description") or f"{method.upper()} {path}"
            parameters = _openapi_params_to_schema(
                op.get("parameters", []), op.get("requestBody"), spec
            )
            tools.append(DiscoveredTool(
                id=str(uuid4()),
                symbiote_id=symbiote_id,
                tool_id=_slugify(op_id),
                name=summary[:80],
                description=op.get("description") or summary,
                method=method.upper(),
                url_template=f"{base_url}{_normalize_path(path)}{_query_template(op, spec)}",
                parameters=parameters,
                tags=op.get("tags", []),
                risk_level=_risk_for_operation(method, op),
                source_path=openapi_url,
                discovered_at=now,
            ))
        return tools

    def _scan_openapi(
        self,
        symbiote_id: str,
        root: Path,
        now: str,
        result: DiscoveryResult,
    ) -> list[DiscoveredTool]:
        """Find OpenAPI/Swagger specs and extract endpoints."""
        tools: list[DiscoveredTool] = []
        candidates = [
            *root.rglob("openapi.json"),
            *root.rglob("openapi.yaml"),
            *root.rglob("openapi.yml"),
            *root.rglob("swagger.json"),
            *root.rglob("swagger.yaml"),
            *root.rglob("swagger.yml"),
        ]
        for spec_file in candidates:
            try:
                tools.extend(
                    self._parse_openapi_file(symbiote_id, spec_file, now, result.errors)
                )
            except Exception as exc:
                result.errors.append(f"openapi:{spec_file}: {exc}")
        return tools

    def _parse_openapi_file(
        self,
        symbiote_id: str,
        spec_file: Path,
        now: str,
        errors: list[str] | None = None,
    ) -> list[DiscoveredTool]:
        import yaml  # optional dep — graceful fallback below

        text = spec_file.read_text(encoding="utf-8")
        if spec_file.suffix in (".yaml", ".yml"):
            try:
                spec = yaml.safe_load(text)
            except Exception:
                return []
        else:
            try:
                spec = json.loads(text)
            except Exception:
                return []

        if not isinstance(spec, dict) or "paths" not in spec:
            return []

        servers = spec.get("servers", [{}])
        base_url = servers[0].get("url", "") if servers else ""
        tools: list[DiscoveredTool] = []
        for path, method, op in _iter_operations(spec, errors):
            op_id = op.get("operationId") or _path_to_tool_id(method, path)
            summary = op.get("summary") or op.get("description") or f"{method.upper()} {path}"
            parameters = _openapi_params_to_schema(op.get("parameters", []), op.get("requestBody"), spec)
            tools.append(DiscoveredTool(
                id=str(uuid4()),
                symbiote_id=symbiote_id,
                tool_id=_slugify(op_id),
                name=summary[:80],
                description=op.get("description") or summary,
                method=method.upper(),
                url_template=f"{base_url}{_normalize_path(path)}{_query_template(op, spec)}",
                parameters=parameters,
                tags=op.get("tags", []),
                risk_level=_risk_for_operation(method, op),
                source_path=str(spec_file),
                discovered_at=now,
            ))
        return tools

    def _scan_fastapi(
        self,
        symbiote_id: str,
        root: Path,
        now: str,
        result: DiscoveryResult,
    ) -> list[DiscoveredTool]:
        """Scan Python files for FastAPI/APIRouter route decorators."""
        tools: list[DiscoveredTool] = []
        pattern = re.compile(
            r'@(?:app|router|api_router)\.(get|post|put|patch|delete)\s*\(\s*["\']([^"\']+)["\']',
            re.IGNORECASE,
        )
        for py_file in root.rglob("*.py"):
            if ".venv" in py_file.parts or "site-packages" in py_file.parts:
                continue
            try:
                text = py_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for match in pattern.finditer(text):
                method, path = match.group(1).upper(), match.group(2)
                tool_id = _path_to_tool_id(method, path)
                tools.append(DiscoveredTool(
                    id=str(uuid4()),
                    symbiote_id=symbiote_id,
                    tool_id=tool_id,
                    name=f"{method} {path}",
                    description=f"FastAPI route: {method} {path}",
                    method=method,
                    url_template="{base_url}" + _normalize_path(path),
                    source_path=str(py_file),
                    discovered_at=now,
                ))
        return tools

    def _scan_flask(
        self,
        symbiote_id: str,
        root: Path,
        now: str,
        result: DiscoveryResult,
    ) -> list[DiscoveredTool]:
        """Scan Python files for Flask @app.route decorators."""
        tools: list[DiscoveredTool] = []
        pattern = re.compile(
            r'@\w+\.route\s*\(\s*["\']([^"\']+)["\'][^)]*methods\s*=\s*\[([^\]]+)\]',
            re.IGNORECASE,
        )
        for py_file in root.rglob("*.py"):
            if ".venv" in py_file.parts or "site-packages" in py_file.parts:
                continue
            try:
                text = py_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for match in pattern.finditer(text):
                path = match.group(1)
                methods_raw = match.group(2)
                for method in re.findall(r'["\'](\w+)["\']', methods_raw):
                    method = method.upper()
                    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                        continue
                    tool_id = _path_to_tool_id(method, path)
                    tools.append(DiscoveredTool(
                        id=str(uuid4()),
                        symbiote_id=symbiote_id,
                        tool_id=tool_id,
                        name=f"{method} {path}",
                        description=f"Flask route: {method} {path}",
                        method=method,
                        url_template="{base_url}" + _normalize_path(path),
                        source_path=str(py_file),
                        discovered_at=now,
                    ))
        return tools

    def _scan_pyproject_scripts(
        self,
        symbiote_id: str,
        root: Path,
        now: str,
        result: DiscoveryResult,
    ) -> list[DiscoveredTool]:
        """Extract [project.scripts] entries from pyproject.toml as CLI tools."""
        tools: list[DiscoveredTool] = []
        pyproject = root / "pyproject.toml"
        if not pyproject.exists():
            return tools
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return tools

        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception:
            return tools

        scripts = data.get("project", {}).get("scripts", {})
        for script_name, entry_point in scripts.items():
            tool_id = _slugify(script_name)
            tools.append(DiscoveredTool(
                id=str(uuid4()),
                symbiote_id=symbiote_id,
                tool_id=tool_id,
                name=script_name,
                description=f"CLI script: {script_name} ({entry_point})",
                handler_type="custom",
                source_path=str(pyproject),
                discovered_at=now,
            ))
        return tools


# ── helpers ──────────────────────────────────────────────────────────────────


def _slugify(s: str) -> str:
    """Convert an arbitrary string to a safe tool_id slug."""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower()


# Heuristic fallback: HTTP method → risk_level when no explicit annotation.
# GET/HEAD are read-only (low); POST/PUT/PATCH mutate state (medium);
# DELETE is destructive (high).
_METHOD_RISK: dict[str, str] = {
    "GET": "low",
    "HEAD": "low",
    "OPTIONS": "low",
    "POST": "medium",
    "PUT": "medium",
    "PATCH": "medium",
    "DELETE": "high",
}

RiskLevel = Literal["low", "medium", "high"]
_VALID_RISK = {"low", "medium", "high"}


def _risk_for_operation(method: str, op: dict[str, Any]) -> RiskLevel:
    """Determine the risk_level for an OpenAPI operation.

    Precedence:
        1. Explicit ``x-risk-level`` extension on the operation
           (values: ``low`` | ``medium`` | ``high``, case-insensitive).
        2. Heuristic fallback by HTTP method (see ``_METHOD_RISK``).

    An invalid/unknown ``x-risk-level`` value falls back to the heuristic.
    """
    raw = op.get("x-risk-level")
    if isinstance(raw, str) and raw.lower() in _VALID_RISK:
        return cast(RiskLevel, raw.lower())
    return cast(RiskLevel, _METHOD_RISK.get(method.upper(), "medium"))


def _iter_operations(spec: dict, errors: list[str] | None = None):  # noqa: ANN202
    """(path, method, operation) for each valid operation of an OpenAPI spec.

    Malformed entries (a null path item, an operation that is not an
    object) are reported in ``errors`` and skipped instead of raising.
    """
    errors = errors if errors is not None else []
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        errors.append("openapi: 'paths' is not an object")
        return
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            errors.append(f"openapi: {path}: path item is not an object")
            continue
        for method, op in methods.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            if not isinstance(op, dict):
                errors.append(f"openapi: {method.upper()} {path}: operation is not an object")
                continue
            yield path, method, op


def _path_to_tool_id(method: str, path: str) -> str:
    """Convert HTTP method + path to a tool_id slug.

    Path parameters are kept as ``by_<name>`` so ``/items``, ``/items/{id}``
    and ``/items/{id}/{rev}`` get distinct ids.
    """
    parts = [method.lower()]
    for segment in _normalize_path(path).strip("/").split("/"):
        if not segment:
            continue
        parts.append(re.sub(r"\{([^{}]+)\}", r"by_\1", segment))
    return _slugify("_".join(parts))


def _normalize_path(path: str) -> str:
    """Route converters to plain placeholders.

    Starlette ``{file_path:path}`` and Flask ``<int:user_id>`` / ``<name>``
    become ``{file_path}``, ``{user_id}`` and ``{name}`` — the ``:spec`` form
    is not a valid ``str.format`` placeholder for our values.
    """
    path = re.sub(r"\{(\w+):[^{}]*\}", r"{\1}", path)
    return re.sub(r"<(?:[^<>:]+:)?(\w+)>", r"{\1}", path)


# Query parameter names that can be a ``str.format`` placeholder.
_QUERY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _query_template(op: dict[str, Any], spec_root: dict[str, Any] | None) -> str:
    """``?a={a}&b={b}`` for the operation's ``in: query`` parameters.

    The gateway percent-encodes each value and drops the optional ones the
    caller leaves out (``optional_params``, set by the loader).
    """
    names = []
    for p in op.get("parameters") or []:
        p = _resolve_ref(p, spec_root) if isinstance(p, dict) else {}
        name = p.get("name", "")
        if p.get("in") == "query" and _QUERY_NAME.match(name):
            names.append(name)
    if not names:
        return ""
    return "?" + "&".join(f"{n}={{{n}}}" for n in names)


def _openapi_params_to_schema(
    params: list[dict[str, Any]],
    request_body: dict[str, Any] | None,
    spec_root: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert OpenAPI parameters + requestBody to a JSON Schema object.

    Resolves ``$ref`` pointers against *spec_root* (the full OpenAPI spec)
    so that Pydantic-generated request bodies are properly extracted.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    for p in params:
        p = _resolve_ref(p, spec_root) if isinstance(p, dict) else {}
        name = p.get("name", "")
        if not name or p.get("in") in ("header", "cookie"):
            # Header/cookie parameters cannot be sent by the HTTP tool.
            continue
        schema = p.get("schema", {"type": "string"})
        schema = _resolve_ref(schema, spec_root)
        properties[name] = {
            "type": schema.get("type", "string"),
            "description": p.get("description", ""),
        }
        if p.get("required"):
            required.append(name)

    body_content_type = "application/json"
    if request_body:
        content = request_body.get("content", {})
        # Try JSON first, then form variants (FastAPI uses all three)
        if "application/json" in content:
            json_content = content["application/json"]
        elif "application/x-www-form-urlencoded" in content:
            json_content = content["application/x-www-form-urlencoded"]
            body_content_type = "application/x-www-form-urlencoded"
        elif "multipart/form-data" in content:
            json_content = content["multipart/form-data"]
            body_content_type = "application/x-www-form-urlencoded"  # treat as form
        else:
            json_content = {}
        body_schema = _resolve_ref(json_content.get("schema", {}), spec_root)
        body_props = body_schema.get("properties", {})
        for name, prop in body_props.items():
            properties[name] = _resolve_ref(prop, spec_root)
        if request_body.get("required") or body_schema.get("required"):
            required.extend(body_schema.get("required", []))

    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    if body_content_type != "application/json":
        result["_content_type"] = body_content_type
    return result


def _resolve_ref(
    schema: dict[str, Any],
    spec_root: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve a ``$ref`` pointer to its target in the OpenAPI spec."""
    ref = schema.get("$ref")
    if not ref or not spec_root:
        return schema
    # "#/components/schemas/MyModel" → navigate from spec root
    parts = ref.split("/")
    target = spec_root
    for part in parts[1:]:  # skip leading "#"
        target = target.get(part, {})
        if not isinstance(target, dict):
            return schema
    return target
