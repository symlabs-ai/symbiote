"""Regression: closing a session through the API runs the same pipeline as
kernel.close_session (score, reflection) (report item M-2)."""

from __future__ import annotations

from tests.support import ScriptedLLM


def test_api_close_scores_and_reflects(api_client) -> None:
    api = api_client(llm=ScriptedLLM("Understood."))
    h = api.auth(api.new_key("tenant-a"))
    sym = api.client.post("/symbiotes", headers=h, json={"name": "a", "role": "r"}).json()
    sess = api.client.post("/sessions", headers=h, json={"symbiote_id": sym["id"]}).json()
    for text in (
        "I prefer short and direct answers.",
        "Remember: I always use Python 3.12 and never use tabs.",
    ):
        assert api.client.post(
            f"/sessions/{sess['id']}/chat", headers=h, json={"content": text}
        ).status_code == 200

    resp = api.client.post(f"/sessions/{sess['id']}/close", headers=h)

    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"
    storage = api.kernel._storage
    score = storage.fetch_one("SELECT * FROM session_scores WHERE session_id = ?", (sess["id"],))
    memories = storage.fetch_all(
        "SELECT content FROM memory_entries WHERE session_id = ?", (sess["id"],)
    )
    assert score is not None
    assert memories
