"""Regression: filesystem tools stay inside the host-configured root and bash
timeouts kill the whole process tree (report item S-9)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from symbiote.environment import tools as tools_mod
from symbiote.environment.tools import _bash, _validate_path
from tests.support import make_kernel


@pytest.fixture()
def rooted(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "inside.txt").write_text("inside")
    evil = tmp_path / "ws-evil"
    evil.mkdir()
    (evil / "secret.txt").write_text("secret")
    kernel = make_kernel(tmp_path, fs_allowed_root=root)
    sym = kernel.create_symbiote(name="fs", role="r")
    kernel.environment.configure(
        symbiote_id=sym.id, tools=["fs_read", "fs_write", "fs_list", "bash"]
    )
    yield kernel, sym.id, root, tmp_path
    kernel.shutdown()


def _run(kernel, sym_id: str, tool: str, **params):
    return kernel.tool_gateway.execute(sym_id, None, tool, params)


def test_read_inside_root_works(rooted) -> None:
    kernel, sym, root, _ = rooted
    assert _run(kernel, sym, "fs_read", path=str(root / "inside.txt")).output == "inside"
    assert _run(kernel, sym, "fs_read", path="inside.txt").output == "inside"


@pytest.mark.parametrize("target", ["/etc/passwd", "../ws-evil/secret.txt"])
def test_read_outside_root_is_refused(rooted, target) -> None:
    kernel, sym, _root, tmp = rooted
    result = _run(kernel, sym, "fs_read", path=target)
    assert not result.success
    assert "PermissionError" in result.error


def test_sibling_with_same_prefix_is_refused(rooted) -> None:
    kernel, sym, _root, tmp = rooted
    result = _run(kernel, sym, "fs_read", path=str(tmp / "ws-evil" / "secret.txt"))
    assert not result.success


def test_symlink_escaping_root_is_refused(rooted) -> None:
    kernel, sym, root, tmp = rooted
    (root / "link").symlink_to(tmp / "ws-evil" / "secret.txt")
    result = _run(kernel, sym, "fs_read", path=str(root / "link"))
    assert not result.success


def test_allowed_root_param_cannot_widen(rooted) -> None:
    kernel, sym, _root, tmp = rooted
    result = _run(
        kernel, sym, "fs_read", path=str(tmp / "ws-evil" / "secret.txt"), allowed_root="/"
    )
    assert not result.success


def test_allowed_root_param_can_narrow(rooted) -> None:
    kernel, sym, root, _ = rooted
    (root / "sub").mkdir()
    result = _run(
        kernel, sym, "fs_read", path=str(root / "inside.txt"), allowed_root=str(root / "sub")
    )
    assert not result.success


def test_write_outside_root_is_refused(rooted) -> None:
    kernel, sym, _root, tmp = rooted
    result = _run(kernel, sym, "fs_write", path=str(tmp / "out.txt"), content="x")
    assert not result.success
    assert not (tmp / "out.txt").exists()


def test_unrooted_prefix_check_uses_path_semantics(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws-evil").mkdir()
    with pytest.raises(PermissionError):
        _validate_path(
            {"path": str(tmp_path / "ws-evil" / "f"), "allowed_root": str(tmp_path / "ws")}
        )


def test_bash_cwd_defaults_to_root_and_cannot_leave_it(rooted) -> None:
    kernel, sym, root, tmp = rooted
    ok = _run(kernel, sym, "bash", command="pwd")
    assert ok.output["stdout"].strip() == str(root.resolve())
    refused = _run(kernel, sym, "bash", command="pwd", _cwd=str(tmp))
    assert not refused.success


def test_bash_timeout_kills_child_processes(tmp_path: Path) -> None:
    marker = tmp_path / "late"
    start = time.monotonic()
    out = _bash({"command": f"(sleep 1.5; touch {marker}) & sleep 10", "timeout": 1})
    assert out.get("interrupted") is True
    assert time.monotonic() - start < 4
    time.sleep(2)
    assert not marker.exists()


def test_bash_timeout_is_capped(monkeypatch) -> None:
    seen: list[float] = []
    real_popen = subprocess.Popen

    class SpyPopen(real_popen):  # type: ignore[misc,valid-type]
        def communicate(self, *args, timeout=None, **kw):
            if timeout is not None:
                seen.append(timeout)
            return super().communicate(*args, timeout=timeout, **kw)

    monkeypatch.setattr(tools_mod.subprocess, "Popen", SpyPopen)
    _bash({"command": "true", "timeout": 10**9})
    assert seen and seen[0] <= tools_mod.MAX_BASH_TIMEOUT_SECONDS


def test_kernel_config_accepts_fs_allowed_root(tmp_path: Path) -> None:
    from symbiote.config.models import KernelConfig

    cfg = KernelConfig(db_path=tmp_path / "x.db", fs_allowed_root=tmp_path)
    assert cfg.fs_allowed_root == tmp_path
    assert os.path.isdir(cfg.fs_allowed_root)
