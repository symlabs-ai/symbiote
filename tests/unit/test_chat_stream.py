"""POST /sessions/{id}/chat/stream (B-69): the answer as Server-Sent Events."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import symbiote.api.http as http_module
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKeyManager
from symbiote.api.http import app as fastapi_app
from symbiote.api.http import get_adapter, get_kernel
from symbiote.api.middleware import set_key_manager
from symbiote.api.routes.sessions import _chat_events
from symbiote.api.schemas import ChatRequest
from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import SessionBusyError
from symbiote.core.kernel import SymbioteKernel
from symbiote.sdk import SymbioteClient, SymbioteStreamError


class _StreamingLLM:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    def complete(self, messages, config=None, tools=None) -> str:
        return "".join(self._chunks)

    def stream(self, messages, config=None, tools=None) -> Iterator[str]:
        yield from self._chunks


@pytest.fixture()
def env(tmp_path: Path):
    db = tmp_path / "stream.db"
    adapter = SQLiteAdapter(db_path=db, check_same_thread=False)
    adapter.init_schema()
    key_mgr = APIKeyManager(adapter)
    key_mgr.init_schema()
    set_key_manager(key_mgr)
    _, key = key_mgr.create_key("t1", "stream", role="user")
    kernel = SymbioteKernel(
        config=KernelConfig(db_path=db), llm=_StreamingLLM(["Olá, ", "tudo ", "bem?"])
    )
    kernel.set_token_streaming("live")
    fastapi_app.dependency_overrides[get_adapter] = lambda: adapter
    fastapi_app.dependency_overrides[get_kernel] = lambda: kernel
    http_module._key_manager = key_mgr

    sdk = SymbioteClient.__new__(SymbioteClient)
    sdk._base_url = "http://testserver"
    sdk._client = TestClient(fastapi_app)
    sdk._client.headers["Authorization"] = f"Bearer {key}"
    sym = sdk.create_symbiote(name="Streamer", role="assistant")
    session = sdk.create_session(symbiote_id=sym["id"])

    yield {"sdk": sdk, "kernel": kernel, "session_id": session["id"]}

    fastapi_app.dependency_overrides.clear()
    set_key_manager(None)
    kernel.shutdown()


def test_tokens_then_done(env) -> None:
    events = list(env["sdk"].chat_stream(session_id=env["session_id"], content="Oi"))

    assert [e["text"] for e in events if e["event"] == "token"] == ["Olá, ", "tudo ", "bem?"]
    assert events[-1] == {"event": "done", "response": "Olá, tudo bem?", "session_id": env["session_id"]}
    messages = env["kernel"].inspector.list_messages(env["session_id"])
    assert [m["content"] for m in messages] == ["Oi", "Olá, tudo bem?"]


def test_raw_wire_format(env) -> None:
    resp = env["sdk"]._client.post(f"/sessions/{env['session_id']}/chat/stream", json={"content": "Oi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text.startswith('event: token\ndata: {"text": "Olá, "}\n\n')
    assert 'event: done\ndata: {"response": "Olá, tudo bem?"' in resp.text


def test_a_failed_turn_becomes_an_error_event(env, monkeypatch) -> None:
    async def busy(**kwargs):
        raise SessionBusyError(env["session_id"], 0.5)

    monkeypatch.setattr(env["kernel"], "message_async", busy)
    with pytest.raises(SymbioteStreamError) as caught:
        list(env["sdk"].chat_stream(session_id=env["session_id"], content="Oi"))
    assert caught.value.status == 409


def test_an_unexpected_error_does_not_leak_its_message(env, monkeypatch) -> None:
    async def boom(**kwargs):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(env["kernel"], "message_async", boom)
    with pytest.raises(SymbioteStreamError) as caught:
        list(env["sdk"].chat_stream(session_id=env["session_id"], content="Oi"))
    assert (caught.value.status, caught.value.detail) == (500, "Internal Server Error")


def test_refusals_happen_before_the_stream(env) -> None:
    # HTTPStatusError of the transport (TestClient may ship its own httpx).
    with pytest.raises(Exception, match="404") as caught:
        list(env["sdk"].chat_stream(session_id="no-such-session", content="Oi"))
    assert caught.value.response.status_code == 404  # type: ignore[attr-defined]

    env["kernel"]._llm = None
    with pytest.raises(Exception, match="503") as caught:
        list(env["sdk"].chat_stream(session_id=env["session_id"], content="Oi"))
    assert caught.value.response.status_code == 503  # type: ignore[attr-defined]


def test_client_leaving_mid_turn_cancels_it() -> None:
    kernel = MagicMock()
    release = asyncio.Event()

    async def message_async(**kwargs):
        kwargs["on_token"]("primeiro")
        await release.wait()
        return "fim"

    kernel.message_async = message_async

    async def go() -> None:
        events = _chat_events(kernel, "s1", ChatRequest(content="Oi"))
        assert (await anext(events)).startswith("event: token")
        await events.aclose()  # what Starlette does when the client disconnects
        kernel.cancel_turn.assert_called_once_with("s1")
        release.set()
        await asyncio.sleep(0)

    asyncio.run(go())
