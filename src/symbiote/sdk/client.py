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

    # Chat, with the text as it is produced
    for event in client.chat_stream(session_id=session["id"], content="Hello!"):
        if event["event"] == "token":
            print(event["text"], end="", flush=True)
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx


class SymbioteStreamError(Exception):
    """A streamed turn failed after the stream started.

    ``status`` is what ``POST /sessions/{id}/chat`` would have answered
    (409 session busy or turn cancelled, 404, 500, ...).
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


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
        subject_key: str | None = None,
    ) -> dict:
        """Create or resume a session.

        ``subject_key`` names the end user the session belongs to, when the
        symbiote talks to several people: the session reads the symbiote's
        shared memories plus that user's own, and what it learns is stored
        for that user alone. It cannot be changed afterwards.
        """
        body: dict[str, Any] = {"symbiote_id": symbiote_id}
        if goal is not None:
            body["goal"] = goal
        if external_key is not None:
            body["external_key"] = external_key
        if subject_key is not None:
            body["subject_key"] = subject_key
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

    def chat_stream(
        self,
        session_id: str,
        content: str,
        extra_context: dict | None = None,
        generation_settings: dict | None = None,
    ) -> Iterator[dict]:
        """``chat`` over Server-Sent Events.

        Yields ``{"event": "token", "text": ...}`` while the answer is
        produced, then ``{"event": "done", "response": ..., "session_id": ...}``
        (the body ``chat`` returns). ``response`` is the authoritative
        answer: with live streaming the tokens also carry the narration of
        tool rounds. A turn that fails raises ``SymbioteStreamError``; a
        request refused before the stream starts raises
        ``httpx.HTTPStatusError``, like ``chat``.
        """
        body: dict[str, Any] = {"content": content}
        if extra_context is not None:
            body["extra_context"] = extra_context
        if generation_settings is not None:
            body["generation_settings"] = generation_settings
        with self._client.stream(
            "POST", f"/sessions/{session_id}/chat/stream", json=body
        ) as resp:
            if resp.is_error:
                resp.read()
                resp.raise_for_status()
            event, data = "", []
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data.append(line[len("data:"):].strip())
                elif not line and event:
                    payload = json.loads("\n".join(data)) if data else {}
                    if event == "error":
                        raise SymbioteStreamError(
                            int(payload.get("status", 500)), str(payload.get("detail", ""))
                        )
                    yield {"event": event, **payload}
                    event, data = "", []

    # ── Memory ────────────────────────────────────────────────────────────

    def search_memory(
        self,
        query: str,
        scope: str | None = None,
        limit: int = 10,
        *,
        symbiote_id: str,
        subject_key: str | None = None,
    ) -> list[dict]:
        """Search one symbiote's memory entries: the shared ones, plus the
        ones of the end user ``subject_key`` when given."""
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "symbiote_id": symbiote_id,
        }
        if scope:
            params["scope"] = scope
        if subject_key:
            params["subject_key"] = subject_key
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

    # ── Approvals that wait for a person ──────────────────────────────────

    def list_approvals(
        self,
        symbiote_id: str,
        *,
        status: str = "pending",
        subject_key: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Tool calls of a Symbiota deferred by the host's approval callback.
        ``status="all"`` lists every state."""
        params: dict[str, Any] = {"status": status, "limit": limit}
        if subject_key:
            params["subject_key"] = subject_key
        return self._get(f"/symbiotes/{symbiote_id}/approvals", params=params)

    def resolve_approval(
        self, approval_id: str, approved: bool, *, note: str | None = None
    ) -> dict:
        """Approve (the call runs now) or deny a deferred tool call."""
        body: dict[str, Any] = {"approved": approved}
        if note is not None:
            body["note"] = note
        return self._post(f"/approvals/{approval_id}/resolve", body)

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
