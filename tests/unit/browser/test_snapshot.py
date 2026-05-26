"""Snapshot renderer unit tests — pure function, no Playwright needed.

Input is the aria_snapshot YAML-like text that Playwright >=1.40 produces.
"""

from __future__ import annotations

from symbiote.browser.browser.snapshot import render_snapshot


def test_empty_input_yields_placeholder():
    assert render_snapshot(None).text == "(empty page)"
    assert render_snapshot("").text == "(empty page)"
    assert render_snapshot("   ").text == "(empty page)"


def test_simple_button_gets_ref():
    aria = '- button "Submit"'
    result = render_snapshot(aria)
    assert "@e1" in result.refs
    assert result.refs["@e1"]["role"] == "button"
    assert result.refs["@e1"]["name"] == "Submit"
    assert "@e1" in result.text


def test_multiple_interactive_elements_get_sequential_refs():
    aria = """- link "Home"
- link "About"
- button "Go"
"""
    result = render_snapshot(aria)
    assert set(result.refs) == {"@e1", "@e2", "@e3"}
    assert result.refs["@e1"]["name"] == "Home"
    assert result.refs["@e3"]["role"] == "button"


def test_non_interactive_roles_get_no_ref():
    aria = """- heading "Title" [level=1]
- paragraph "Some text"
- link "Click me"
"""
    result = render_snapshot(aria)
    assert len(result.refs) == 1
    assert "@e1" in result.refs
    assert result.refs["@e1"]["name"] == "Click me"


def test_indented_nested_structure_preserved():
    aria = """- main:
  - link "Go to target":
    - /url: /target
  - button "Submit"
"""
    result = render_snapshot(aria)
    assert "@e1" in result.refs
    assert result.refs["@e1"]["role"] == "link"
    assert result.refs["@e1"]["name"] == "Go to target"
    assert "@e2" in result.refs
    assert result.refs["@e2"]["role"] == "button"
    # Indentation preserved
    assert "  - link @e1" in result.text


def test_long_name_truncated():
    long = "x" * 500
    aria = f'- button "{long}"'
    result = render_snapshot(aria)
    assert "…" in result.text


def test_unrecognized_lines_passthrough():
    aria = """- main:
  - text: just a string
  - link "Hi"
"""
    result = render_snapshot(aria)
    assert "just a string" in result.text
    assert "@e1" in result.refs
