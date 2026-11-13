"""Regression: the Console escapes data for HTML attributes and never builds
inline event handlers from data (report item S-6)."""

from __future__ import annotations

import re
import shutil
import subprocess
from importlib.resources import files

import pytest

HTML = (files("symbiote.api") / "dashboard.html").read_text(encoding="utf-8")


def _esc_source() -> str:
    match = re.search(r"function esc\(s\) \{.*?\n\}", HTML, re.S)
    assert match, "esc() not found in dashboard.html"
    return match.group(0)


def test_no_inline_handler_interpolates_data() -> None:
    offenders = [
        line.strip()[:120]
        for line in HTML.splitlines()
        if re.search(r'\bon[a-z]+="[^"]*\$\{', line)
    ]
    assert offenders == []


def test_esc_escapes_quotes() -> None:
    src = _esc_source()
    assert "&quot;" in src
    assert "&#39;" in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_esc_output_is_safe_inside_attributes() -> None:
    payload = """nota" onmouseover="alert(1)" x='<b>&"""
    script = _esc_source() + "\nprocess.stdout.write(esc(" + repr(payload) + "));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    escaped = out.stdout
    for ch in "\"'<>":
        assert ch not in escaped
    assert escaped == (
        "nota&quot; onmouseover=&quot;alert(1)&quot; x=&#39;&lt;b&gt;&amp;"
    )
