"""Regression: smaller API items (report item A-3) — chat without an LLM is a
clear 503, /health does not shell out per request, empty names are refused,
and generation_settings reaches the LLM call."""

from __future__ import annotations

import subprocess

import pytest

from tests.support import ScriptedLLM


def _session(api, headers: dict) -> str:
    sym = api.client.post("/symbiotes", headers=headers, json={"name": "a", "role": "r"}).json()
    return api.client.post("/sessions", headers=headers, json={"symbiote_id": sym["id"]}).json()["id"]


def test_chat_without_llm_is_503_with_a_hint(api_client) -> None:
    api = api_client(llm=None)
    h = api.auth(api.new_key("t"))
    sid = _session(api, h)

    resp = api.client.post(f"/sessions/{sid}/chat", headers=h, json={"content": "hi"})

    assert resp.status_code == 503
    assert "SYMBIOTE_LLM_PROVIDER" in resp.json()["detail"]


def test_health_resolves_the_commit_once(api_client, monkeypatch) -> None:
    import symbiote.api.http as http_mod

    calls = []
    real = subprocess.check_output

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", counting)
    if hasattr(http_mod, "_build_info"):
        http_mod._build_info.cache_clear()
    api = api_client()

    bodies = [api.client.get("/health").json() for _ in range(3)]

    assert len(calls) <= 1
    assert len({b["commit"] for b in bodies}) == 1


@pytest.mark.parametrize("name", ["", "   "])
def test_empty_symbiote_name_is_refused(api_client, name: str) -> None:
    api = api_client()
    h = api.auth(api.new_key("t"))
    sym = api.client.post("/symbiotes", headers=h, json={"name": "ok", "role": "r"}).json()

    created = api.client.post("/symbiotes", headers=h, json={"name": name, "role": "r"})
    renamed = api.client.put(f"/symbiotes/{sym['id']}", headers=h, json={"name": name})

    assert created.status_code == 422
    assert renamed.status_code == 422


def test_generation_settings_reach_the_llm(api_client) -> None:
    llm = ScriptedLLM("ok")
    api = api_client(llm=llm)
    h = api.auth(api.new_key("t"))
    sid = _session(api, h)

    resp = api.client.post(
        f"/sessions/{sid}/chat", headers=h,
        json={"content": "hi", "generation_settings": {"temperature": 0.1}},
    )

    assert resp.status_code == 200
    assert (llm.calls[-1]["config"] or {}).get("temperature") == 0.1
