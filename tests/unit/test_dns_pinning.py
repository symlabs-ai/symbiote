"""Regression: the HTTP tool connects to the IP that the SSRF check approved,
so a DNS answer that changes between validation and connection (DNS
rebinding) cannot reach loopback (report item S-11). No network: name
resolution and socket creation are faked."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor
from tests.support import ScriptedLLM, make_kernel

PUBLIC = "93.184.216.34"


@pytest.fixture()
def rebinding(monkeypatch):
    calls = {"resolve": 0, "connect": []}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls["resolve"] += 1
        ip = PUBLIC if calls["resolve"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80))]

    def fake_create_connection(address, *args, **kwargs):
        calls["connect"].append(address)
        raise ConnectionRefusedError("test: no network")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    return calls


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_connection_uses_the_validated_ip(tmp_path: Path, rebinding, scheme: str) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="a", role="r").id
    kernel.tool_gateway.register_http_tool(
        ToolDescriptor(tool_id="fetch", name="f", description="d"),
        HttpToolConfig(url_template=f"{scheme}://rebind.example:8443/data"),
    )
    kernel.environment.add_tools(sid, ["fetch"])

    result = kernel.tool_gateway.execute(sid, None, "fetch", {})

    assert result.success is False  # the fake socket refuses
    assert rebinding["connect"] == [(PUBLIC, 8443)]
    kernel.shutdown()
