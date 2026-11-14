"""Offline tests of the demo: a real server on an ephemeral port, the real
kernel, MCP server and personal API, and a scripted LLM in place of the
provider. They check the wiring the presentation depends on."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from demo import runtime as runtime_mod
from demo import server as server_mod
from demo import settings as settings_mod
from symbiote.environment.descriptors import LLMResponse, NativeToolCall
from tests.support import ScriptedLLM


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _last(messages: list[dict]) -> dict:
    return messages[-1]


def scripted(i: int, messages: list[dict]) -> Any:
    """Main-model script keyed on the user's words."""
    last = _last(messages)
    if last.get("role") == "tool":
        return "feito"
    text = str(last.get("content", ""))
    if "que horas" in text:
        return LLMResponse(tool_calls=[NativeToolCall(call_id="c1", tool_id="agora", params={})])
    if "apague tudo" in text:
        return LLMResponse(tool_calls=[NativeToolCall(call_id="c2", tool_id="bash", params={"command": "echo apagado"})])
    if "navegador" in text:
        return LLMResponse(tool_calls=[NativeToolCall(call_id="c3", tool_id="browser_navigate", params={"url": "https://example.com"})])
    return "ok, anotado"


def aux_scripted(i: int, messages: list[dict]) -> Any:
    """Auxiliary-model script: the LLM reflection gets two facts, the rest []."""
    if "JSON array:" in str(_last(messages).get("content", "")):
        return json.dumps([
            {"action": "create", "type": "preference", "content": "Prefere respostas curtas.",
             "importance": 0.7, "tags": ["formato"], "reasoning": "preferência explícita"},
            {"action": "create", "type": "constraint", "content": "Nunca marcar ligações antes das 9h.",
             "importance": 0.8, "tags": ["agenda"], "reasoning": "regra absoluta"},
        ])
    if "Generalized procedure" in str(_last(messages).get("content", "")):
        return "Para reservas de viagem: comparar opções, conferir as condições e salvar o comprovante em viagem/."
    return "[]"


class Demo:
    def __init__(self, base: str, token: str, app: Any) -> None:
        self.base = base
        self.token = token
        self.app = app
        self.http = httpx.Client(base_url=base, timeout=30)

    @property
    def runtime(self) -> runtime_mod.DemoRuntime:
        return self.app.state.runtime

    def get(self, path: str) -> Any:
        r = self.http.get(path)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict | None = None, *, token: bool = True) -> httpx.Response:
        headers = {"X-Demo-Token": self.token} if token else {}
        return self.http.post(path, json=body or {}, headers=headers)

    def chat(self, message: str, symbiote: str = "sol") -> list[tuple[str, dict]]:
        events = []
        with self.http.stream("POST", "/api/chat/stream", json={"message": message, "symbiote": symbiote},
                              headers={"X-Demo-Token": self.token}, timeout=60) as r:
            r.raise_for_status()
            event = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line[6:]
                elif line.startswith("data:"):
                    events.append((event, json.loads(line[5:])))
        return events

    def events(self) -> list[dict]:
        return self.app.state.bus.recent(limit=800)


def _serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, configured: bool) -> Iterator[Demo]:
    fake = settings_mod.Settings(
        provider="anthropic" if configured else "",
        api_key="test-key" if configured else "",
        model="m-main",
        aux_model="m-aux",
    )
    monkeypatch.setattr(settings_mod, "load", lambda *a, **k: fake)
    monkeypatch.setattr(settings_mod, "save", lambda *a, **k: None)
    monkeypatch.setattr(runtime_mod, "validate_key", lambda s: None)
    monkeypatch.setattr(server_mod, "validate_key", lambda s: None)
    monkeypatch.setattr(
        runtime_mod, "build_adapter",
        lambda s, model: ScriptedLLM(scripted) if model == "m-main" else ScriptedLLM(aux_scripted),
    )
    port = _free_port()
    app = server_mod.create_app(port=port, data_dir=tmp_path / "data")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    html = httpx.get(base + "/").text
    token = re.search(r'name="demo-token" content="([^"]+)"', html).group(1)
    demo = Demo(base, token, app)
    try:
        yield demo
    finally:
        demo.http.close()
        server.should_exit = True
        thread.join(timeout=20)


@pytest.fixture()
def demo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Demo]:
    yield from _serve(monkeypatch, tmp_path, configured=True)


@pytest.fixture()
def bare_demo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Demo]:
    yield from _serve(monkeypatch, tmp_path, configured=False)


# ── setup and local protection ─────────────────────────────────────────


def test_without_a_key_the_page_asks_for_one(bare_demo: Demo) -> None:
    status = bare_demo.get("/api/status")
    assert status["ready"] is False
    assert bare_demo.post("/api/chat/stream", {"message": "oi"}).status_code == 409

    r = bare_demo.post("/api/setup", {"provider": "anthropic", "api_key": "sk-test"})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["api_key"] != "sk-test"  # masked
    assert bare_demo.get("/api/status")["ready"] is True


def test_symgateway_setup_lists_models_and_requires_one(
    bare_demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(server_mod, "list_gateway_models", lambda s: seen.setdefault("s", s) and ["gw-a", "gw-b"])
    monkeypatch.setattr(settings_mod, "save", lambda updates, *a, **k: seen.setdefault("saved", updates))
    form = {"provider": "symgateway", "api_key": "sk-gw", "project": "acme"}

    r = bare_demo.post("/api/setup/models", form)
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["gw-a", "gw-b"]
    assert seen["s"].gateway_url == settings_mod.SYMGATEWAY_DEFAULT_URL
    assert seen["s"].symgateway_project == "acme"

    assert bare_demo.post("/api/setup", form).status_code == 422  # no model picked
    r = bare_demo.post("/api/setup", {**form, "model": "gw-a"})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["aux_model"] == "gw-a"  # aux defaults to main
    assert seen["saved"]["SYMGATEWAY_API_KEY"] == "sk-gw"
    assert seen["saved"]["SYMGATEWAY_PROJECT_SLUG"] == "acme"
    status = bare_demo.get("/api/status")
    assert status["ready"] is True
    assert status["settings"]["provider"] == "symgateway"


def test_gateway_work_session_follows_the_symbiote_session() -> None:
    from demo.events import TURN, Turn

    assert runtime_mod.gateway_session_key() is None
    token = TURN.set(Turn(turn_id="t1", session_id="sess-1", symbiote_id="sym"))
    try:
        assert runtime_mod.gateway_session_key() == "sess-1"
    finally:
        TURN.reset(token)
    scope = runtime_mod.SCOPE.set("sess-2")  # close_session's background work
    try:
        assert runtime_mod.gateway_session_key() == "sess-2"
    finally:
        runtime_mod.SCOPE.reset(scope)


def test_state_changes_need_the_token_and_a_local_host(demo: Demo) -> None:
    assert demo.post("/api/session/new", {"symbiote": "sol"}, token=False).status_code == 403
    r = demo.http.get("/api/status", headers={"Host": "evil.example:80"})
    assert r.status_code == 403
    assert demo.post("/api/session/new", {"symbiote": "sol"}).status_code == 200


# ── chat turn and live events ──────────────────────────────────────────


def test_a_turn_with_a_tool_streams_badges_and_publishes_the_timeline(demo: Demo) -> None:
    events = demo.chat("que horas são?")

    kinds = [e for e, _ in events]
    assert kinds[0] == "tool_start" and "tool_done" in kinds and kinds[-1] == "response_done"
    assert events[-1][1]["text"] == "feito"

    turn_id = events[-1][1]["turn_id"]
    timeline = [e["type"] for e in demo.events() if e.get("turn_id") == turn_id]
    for expected in ("turn_start", "context", "llm_start", "llm_end", "tool_start", "tool_end", "turn_end"):
        assert expected in timeline, (expected, timeline)
    assert timeline.index("context") < timeline.index("llm_start") < timeline.index("tool_start")

    ctx = demo.get("/api/context?symbiote=sol")
    assert ctx["context"]["tool_mode"] == "brief"
    assert ctx["last_call"]["messages"][0]["role"] == "system"


def test_history_is_injected_by_the_host_on_the_next_turn(demo: Demo) -> None:
    demo.chat("primeira mensagem")
    demo.chat("segunda mensagem")
    ctx = demo.get("/api/context?symbiote=sol")["context"]
    assert "Usuário: primeira mensagem" in ctx["history"]
    assert "Assistant: ok, anotado" in ctx["history"]


def test_approval_gate_waits_for_the_page(demo: Demo) -> None:
    decided: list[str] = []

    def approve_when_asked() -> None:
        for _ in range(200):
            pending = demo.runtime.broker.pending()
            if pending:
                # A page loaded now counts down from the request's own time.
                shown = demo.get("/api/status")["pending_approvals"][0]
                assert shown["timeout_s"] == 180 and time.time() - 5 < shown["ts"] <= time.time()
                r = demo.post(f"/api/approvals/{pending[0]['id']}", {"approved": False})
                decided.append(r.status_code)
                return
            time.sleep(0.05)

    helper = threading.Thread(target=approve_when_asked)
    helper.start()
    events = demo.chat("apague tudo")
    helper.join()

    assert decided == [200]
    done = [d for e, d in events if e == "tool_done"]
    assert done and done[0]["success"] is False
    results = [e for e in demo.events() if e["type"] == "approval_result"]
    assert results[-1]["approved"] is False


def test_unauthorized_tool_is_blocked_by_policy(demo: Demo) -> None:
    demo.chat("abra o navegador")
    ends = [e for e in demo.events() if e["type"] == "tool_end" and e["tool_id"] == "browser_navigate"]
    assert ends and ends[-1]["blocked"] is True
    audit = demo.get("/api/tools?symbiote=sol")["audit"]
    assert audit[0]["action"] == "blocked"


# ── discovery against the personal API ────────────────────────────────


def test_discover_approve_load_and_call_the_personal_api(demo: Demo) -> None:
    found = demo.post("/api/discovery/run", {"symbiote": "sol"}).json()["found"]
    assert {"list_tasks", "delete_task", "send_email"} <= set(found)

    tools = demo.get("/api/tools?symbiote=sol")
    risks = {t["tool_id"]: t["risk_level"] for t in tools["discovered"]}
    assert risks["delete_task"] == "high" and risks["list_tasks"] == "low"
    assert all(t["status"] == "pending" for t in tools["discovered"])

    assert demo.post("/api/discovery/status", {"symbiote": "sol", "tag": "tarefas", "status": "approved"}).status_code == 200
    loaded = demo.post("/api/discovery/load", {"symbiote": "sol"}).json()["loaded"]
    assert "list_tasks" in loaded and "send_email" not in loaded

    out = demo.post("/api/tools/try", {"symbiote": "sol", "tool_id": "list_tasks", "params": {}}).json()
    assert out["success"], out
    assert "Renovar o seguro do carro" in json.dumps(out["output"], ensure_ascii=False)


def test_mcp_and_custom_tools_are_registered_and_authorized(demo: Demo) -> None:
    rows = {t["tool_id"]: t for t in demo.get("/api/tools?symbiote=sol")["tools"]}
    assert rows["converter_unidades"]["source"] == "mcp" and rows["converter_unidades"]["authorized"]
    assert rows["agora"]["source"] == "python"
    out = demo.post("/api/tools/try", {"symbiote": "sol", "tool_id": "converter_unidades",
                                       "params": {"valor": 10, "de": "km", "para": "mi"}}).json()
    assert out["success"] and "6.214" in str(out["output"])


# ── learning ───────────────────────────────────────────────────────────


def test_closing_opens_a_fresh_session_not_an_older_open_one(demo: Demo) -> None:
    older = demo.get("/api/status")["symbiotes"]["sol"]["session_id"]
    demo.chat("oi")
    newer = demo.post("/api/session/new", {"symbiote": "sol"}).json()["session_id"]
    demo.chat("tudo bem?")

    result = demo.post("/api/session/close", {"symbiote": "sol"}).json()

    assert result["closed_session"] == newer
    assert result["new_session"] not in (older, newer)


def test_closing_a_session_scores_it_and_learns_preferences(demo: Demo) -> None:
    # The demo reflects with the auxiliary model (scripted in aux_scripted).
    demo.chat("Eu prefiro respostas curtas.")
    demo.chat("Nunca marque ligações antes das 9h.")
    first = demo.get("/api/status")["symbiotes"]["sol"]["session_id"]

    result = demo.post("/api/session/close", {"symbiote": "sol"}).json()

    assert result["closed_session"] == first
    assert result["new_session"] != first
    assert result["score"]["final_score"] is not None
    types = {m["type"] for m in result["learned"]}
    assert {"preference", "constraint"} <= types
    recall = demo.post("/api/memory/recall", {"symbiote": "sol", "query": "como prefiro as respostas?"}).json()
    assert recall["results"] and recall["other_symbiote"] == []


def test_security_checks_hold(demo: Demo) -> None:
    for test in ("ssrf", "fs", "policy"):
        out = demo.post("/api/security", {"test": test}).json()
        assert out["blocked"] is True, (test, out)


def test_tabs_endpoints_answer(demo: Demo) -> None:
    for path in ("/api/memory?symbiote=sol", "/api/learning?symbiote=sol", "/api/config?symbiote=sol",
                 "/api/traces?symbiote=sol", "/api/skills?symbiote=sol", "/api/harness?symbiote=sol",
                 "/api/web", "/api/sessions?symbiote=sol", "/api/knowledge?symbiote=sol",
                 "/api/persona?symbiote=sol", "/api/system", "/api/pa", "/api/llm/calls"):
        demo.get(path)
    harness = demo.get("/api/harness?symbiote=sol")
    assert harness["tuning"]["tier"] == 0
    evolve = demo.post("/api/harness/evolve", {"symbiote": "sol"}).json()
    assert evolve["success"] is False and evolve["reason"]
    dream = demo.post("/api/dream", {"symbiote": "sol", "dry_run": True}).json()
    assert dream["dry_run"] is True
    cfg = demo.post("/api/config", {"symbiote": "sol", "fields": {"tool_loading": "index", "planner_prompt": None}}).json()
    assert cfg["config"]["tool_loading"] == "index" and cfg["config"]["planner_prompt"] is None


def test_seeded_memories_give_every_dream_phase_work(demo: Demo) -> None:
    assert demo.get("/api/learning?symbiote=sol")["config"]["dream_mode"] == "off"
    demo.post("/api/learning/config", {"symbiote": "sol", "dream_mode": "full"}).raise_for_status()

    report = demo.post("/api/dream", {"symbiote": "sol"}).json()

    applied = {p["phase"]: p["actions_applied"] for p in report["phases"]}
    assert applied["prune"] == 2
    assert applied["reconcile"] == 1
    assert applied["generalize"] == 1
    contents = [m["content"] for m in demo.runtime.kernel.inspector.list_memories(demo.runtime.sol_id)]
    assert not any("Ubatuba" in c for c in contents)
    assert sum("lembretes de aniversário" in c for c in contents) == 1
    assert any(c.startswith("Para reservas de viagem") for c in contents)
