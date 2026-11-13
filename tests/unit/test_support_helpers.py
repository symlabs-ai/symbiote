"""Smoke tests for the shared helpers in tests/support."""

from __future__ import annotations

from pathlib import Path

from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


def test_scripted_llm_drives_a_tool_loop(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        lambda i, _m: tool_call_block("echo", value="X1") if i == 0 else "done"
    )
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    seen: list[dict] = []
    add_tool(kernel, sym.id, "echo", lambda p: seen.append(p) or {"echo": p["value"]})
    session = kernel.start_session(sym.id)

    out = kernel.message(session.id, "go")

    assert seen == [{"value": "X1"}]
    assert len(llm.calls) == 2
    assert "X1" in llm.prompt_text(1)
    text = out["text"] if isinstance(out, dict) else out
    assert text == "done"
    kernel.shutdown()


def test_api_client_enforces_real_auth(api_client) -> None:
    api = api_client()
    assert api.client.post("/symbiotes", json={"name": "a", "role": "b"}).status_code == 401
    key = api.new_key("tenant-a")
    resp = api.client.post(
        "/symbiotes", headers=api.auth(key), json={"name": "a", "role": "b"}
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "a"
