"""Accessibility-tree snapshot rendering for browser pages.

Playwright >=1.40 exposes `page.locator('body').aria_snapshot()` which returns
a YAML-like text representation of the accessibility tree, e.g.:

    - heading "Hello" [level=1]
    - link "Next":
      - /url: /next
    - button "Submit"

We post-process that to add `@eN` reference markers to every interactive
element, so the LLM can click/fill by ref. The mapping `ref → (role, name)`
is kept on the BrowserSession so subsequent `browser_click(ref="@e3")` can
resolve to the right element via Playwright's `get_by_role(role, name=name)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Roles that should get a @e<N> ref because they're directly interactable.
_INTERACTIVE_ROLES = {
    "link",
    "button",
    "checkbox",
    "radio",
    "textbox",
    "combobox",
    "searchbox",
    "menuitem",
    "tab",
    "switch",
    "slider",
    "spinbutton",
    "option",
}

# Matches one node in the aria_snapshot YAML output:
#   "- role \"name\""    | "- role"   | "- role:"   etc.
_NODE_RE = re.compile(
    r'^(?P<indent>\s*)-\s*(?P<role>[a-zA-Z][\w-]*)'
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'  # optional "name", may contain escaped quotes
    r'(?:\s*\[(?P<attrs>[^\]]*)\])?'  # optional [level=1, expanded, …]
    r'(?P<rest>.*)$'
)

_MAX_NAME_LEN = 120


@dataclass
class SnapshotResult:
    """Output of taking a snapshot.

    Attributes:
        text: aria_snapshot YAML annotated with @e<N> markers for interactive nodes.
        refs: mapping from "@eN" to {"role": ..., "name": ...}.
    """

    text: str
    refs: dict[str, dict[str, Any]] = field(default_factory=dict)


def render_snapshot(aria_text: str | None) -> SnapshotResult:
    """Annotate Playwright aria_snapshot output with @eN refs for interactive nodes.

    Args:
        aria_text: Raw output of `page.locator('body').aria_snapshot()`.

    Returns:
        SnapshotResult with annotated text and ref → {role, name} mapping.
    """
    if not aria_text or not aria_text.strip():
        return SnapshotResult(text="(empty page)")

    refs: dict[str, dict[str, Any]] = {}
    counter = 0
    annotated_lines: list[str] = []

    for line in aria_text.splitlines():
        match = _NODE_RE.match(line)
        if not match:
            annotated_lines.append(line)
            continue

        role = match.group("role").lower()
        name = match.group("name") or ""
        if len(name) > _MAX_NAME_LEN:
            name = name[: _MAX_NAME_LEN - 1] + "…"

        if role in _INTERACTIVE_ROLES:
            counter += 1
            ref = f"@e{counter}"
            refs[ref] = {"role": role, "name": name}
            # Insert ref marker right after the role token.
            indent = match.group("indent")
            attrs = match.group("attrs")
            rest = match.group("rest")
            name_part = f' "{name}"' if name else ""
            attrs_part = f" [{attrs}]" if attrs else ""
            annotated_lines.append(
                f"{indent}- {role} {ref}{name_part}{attrs_part}{rest}"
            )
        else:
            annotated_lines.append(line)

    return SnapshotResult(text="\n".join(annotated_lines), refs=refs)
