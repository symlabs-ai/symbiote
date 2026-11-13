"""Regression: HTTP tools registered through the API or the CLI live in the
kernel's gateway and survive a restart (report item T-1)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from symbiote.cli.main import app as cli_app
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor
from tests.support import ScriptedLLM, make_kernel, tool_call_block

TOOL = {
    "tool_id": "get_weather",
    "name": "Weather",
    "description": "Weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    "http_method": "GET",
    "url_template": "http://example.invalid/weather?city={city}",
}


def test_api_registered_tool_is_offered_to_chat(api_client) -> None:
    llm = ScriptedLLM(lambda i, m: tool_call_block("get_weather", city="Rio") if i == 0 else "done")
    api = api_client(llm=llm)
    h = api.auth(api.new_key("tenant-a"))
    sym = api.client.post("/symbiotes", headers=h, json={"name": "a", "role": "r"}).json()
    sess = api.client.post("/sessions", headers=h, json={"symbiote_id": sym["id"]}).json()

    assert api.client.post(f"/symbiotes/{sym['id']}/tools", headers=h, json=TOOL).status_code == 201
    out = api.client.post(f"/sessions/{sess['id']}/chat", headers=h, json={"content": "weather?"}).json()

    assert api.kernel.tool_gateway.has_tool("get_weather")
    result = out["response"]["tool_results"][0]
    assert result["tool_id"] == "get_weather"
    assert "not registered" not in str(result.get("error"))


def test_registered_tool_survives_restart(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.register_http_tool(
        sym.id,
        ToolDescriptor(tool_id="get_weather", name="w", description="d"),
        HttpToolConfig(url_template="http://example.invalid/w"),
    )
    kernel.shutdown()

    reopened = make_kernel(tmp_path, ScriptedLLM("ok"))
    assert reopened.tool_gateway.has_tool("get_weather")
    assert "get_weather" in reopened.environment.list_tools(sym.id)
    reopened.shutdown()


def test_cannot_register_over_builtin_or_other_owner(api_client) -> None:
    api = api_client()
    ha = api.auth(api.new_key("tenant-a"))
    hb = api.auth(api.new_key("tenant-b"))
    a = api.client.post("/symbiotes", headers=ha, json={"name": "a", "role": "r"}).json()
    b = api.client.post("/symbiotes", headers=hb, json={"name": "b", "role": "r"}).json()

    builtin = api.client.post(f"/symbiotes/{a['id']}/tools", headers=ha, json={**TOOL, "tool_id": "bash"})
    api.client.post(f"/symbiotes/{a['id']}/tools", headers=ha, json=TOOL)
    stolen = api.client.post(f"/symbiotes/{b['id']}/tools", headers=hb, json=TOOL)

    assert builtin.status_code == 409
    assert stolen.status_code == 409
    assert api.kernel.tool_gateway.get_descriptor("bash").handler_type == "builtin"


def test_cli_added_tool_exists_in_the_next_process(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    runner = CliRunner()
    created = runner.invoke(cli_app, ["--db-path", str(db), "create", "--name", "a", "--role", "r"])
    sym_id = next(tok for tok in created.output.split() if len(tok) == 36 and tok.count("-") == 4)

    add = runner.invoke(
        cli_app,
        ["--db-path", str(db), "tools", "add", sym_id, "--id", "echo_tool", "--name", "Echo",
         "--desc", "echo", "--url", "http://example.invalid/api/{id}"],
    )
    listed = runner.invoke(cli_app, ["--db-path", str(db), "tools", "list", sym_id])

    assert add.exit_code == 0, add.output
    assert "echo_tool" in listed.output
