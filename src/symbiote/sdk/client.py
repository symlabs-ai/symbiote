"""SymbioteClient — thin HTTP client for the Symbiote API.

Usage::

    from symbiote.sdk import SymbioteClient

    client = SymbioteClient("https://symbiote.example.com", api_key="sk-symbiote_...")

    # Create a Symbiota
    sym = client.create_symbiote(name="Assistant", role="helper")

    # Start a session
    session = client.create_session(symbiote_id=sym["id"])

    # Chat
    response = client.chat(session_id=session["id"], content="Hello!")
    print(response["response"])
"""

from __future__ import annotations

from typing import Any

import httpx


class SymbioteClient:
    """HTTP client for the Symbiote API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Health ────────────────────────────────────────────────────────────

    def health(self) -> dict:
        """Check API health."""
        return self._get("/health")

    # ── Symbiotes ─────────────────────────────────────────────────────────

    def create_symbiote(
        self, name: str, role: str, persona: dict | None = None
    ) -> dict:
        """Create a new Symbiota."""
        body: dict[str, Any] = {"name": name, "role": role}
        if persona is not None:
            body["persona_json"] = persona
        return self._post("/symbiotes", body)

    def get_symbiote(self, symbiote_id: str) -> dict:
        """Get a Symbiota by ID."""
        return self._get(f"/symbiotes/{symbiote_id}")

    # ── Sessions ──────────────────────────────────────────────────────────

    def create_session(
        self,
        symbiote_id: str,
        goal: str | None = None,
        external_key: str | None = None,
    ) -> dict:
        """Create or resume a session."""
        body: dict[str, Any] = {"symbiote_id": symbiote_id}
        if goal is not None:
            body["goal"] = goal
        if external_key is not None:
            body["external_key"] = external_key
        return self._post("/sessions", body)

    def get_session(self, session_id: str) -> dict:
        """Get session details."""
        return self._get(f"/sessions/{session_id}")

    def close_session(self, session_id: str) -> dict:
        """Close a session."""
        return self._post(f"/sessions/{session_id}/close", {})

    # ── Chat ──────────────────────────────────────────────────────────────

    def chat(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        generation_settings: dict | None = None,
    ) -> dict:
        """Send a message and get an LLM response.

        This is the main method for conversational interaction.
        """
        body: dict[str, Any] = {"content": content}
        if extra_context is not None:
            body["extra_context"] = extra_context
        if generation_settings is not None:
            body["generation_settings"] = generation_settings
        return self._post(f"/sessions/{session_id}/chat", body)

    # ── Memory ────────────────────────────────────────────────────────────

    def search_memory(
        self, query: str, scope: str | None = None, limit: int = 10
    ) -> list[dict]:
        """Search memory entries."""
        params: dict[str, Any] = {"query": query, "limit": limit}
        if scope:
            params["scope"] = scope
        return self._get("/memory/search", params=params)

    # ── Tools ─────────────────────────────────────────────────────────────

    def register_tool(
        self,
        symbiote_id: str,
        tool_id: str,
        name: str,
        description: str,
        url_template: str,
        *,
        method: str = "GET",
        parameters: dict | None = None,
    ) -> dict:
        """Register an HTTP tool for a Symbiota."""
        body: dict[str, Any] = {
            "tool_id": tool_id,
            "name": name,
            "description": description,
            "url_template": url_template,
            "http_method": method,
        }
        if parameters is not None:
            body["parameters"] = parameters
        return self._post(f"/symbiotes/{symbiote_id}/tools", body)

    def list_tools(self, symbiote_id: str) -> list[dict]:
        """List tools available to a Symbiota."""
        return self._get(f"/symbiotes/{symbiote_id}/tools")

    # ── Internal ──────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> Any:
        resp = self._client.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> Any:
        resp = self._client.delete(path)
        resp.raise_for_status()
        return resp.json()
