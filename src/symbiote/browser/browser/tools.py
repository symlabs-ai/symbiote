"""Browser tools — descriptors and handlers wired into ToolGateway.

Each handler takes a params dict (matching the descriptor's JSON schema) and
returns a plain serializable result (str, dict, list, bytes). The ToolGateway
wraps every call in a PolicyGate execution that logs to audit_log.

The provider instance is captured by closure when `build_handlers(provider)`
is called from `register()`.
"""

from __future__ import annotations

from typing import Any

from symbiote.browser.browser.providers.base import BrowserProvider
from symbiote.environment.descriptors import ToolDescriptor

# ── Descriptors ────────────────────────────────────────────────────────────

NAVIGATE_DESCRIPTOR = ToolDescriptor(
    tool_id="browser_navigate",
    name="Browser Navigate",
    description=(
        "Open a URL in an isolated browser session and return the final URL "
        "after redirects. Use task_id to keep multiple sessions independent."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL to navigate to."},
            "task_id": {
                "type": "string",
                "description": "Identifier for this browser session. "
                "Reuse the same task_id across subsequent calls to stay on the same page.",
                "default": "default",
            },
        },
        "required": ["url"],
    },
    handler_type="builtin",
    risk_level="medium",
    tags=["browser", "navigation"],
)

SNAPSHOT_DESCRIPTOR = ToolDescriptor(
    tool_id="browser_snapshot",
    name="Browser Snapshot",
    description=(
        "Capture the current page as a text-based accessibility tree. "
        "Each interactive element gets a ref (@e1, @e2, ...) that you can pass "
        "to browser_click / browser_fill."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "default": "default"},
        },
    },
    handler_type="builtin",
    risk_level="low",
    tags=["browser", "inspection"],
)

CLICK_DESCRIPTOR = ToolDescriptor(
    tool_id="browser_click",
    name="Browser Click",
    description=(
        "Click an element by its ref selector (@e1, @e2, ...) from the most "
        "recent snapshot of the same task_id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "Reference selector, e.g. '@e3'."},
            "task_id": {"type": "string", "default": "default"},
        },
        "required": ["ref"],
    },
    handler_type="builtin",
    risk_level="medium",
    tags=["browser", "interaction"],
)

FILL_DESCRIPTOR = ToolDescriptor(
    tool_id="browser_fill",
    name="Browser Fill",
    description=(
        "Fill an input field identified by ref selector with the given value."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": {"type": "string"},
            "value": {"type": "string"},
            "task_id": {"type": "string", "default": "default"},
        },
        "required": ["ref", "value"],
    },
    handler_type="builtin",
    risk_level="medium",
    tags=["browser", "interaction"],
)

CLOSE_DESCRIPTOR = ToolDescriptor(
    tool_id="browser_close",
    name="Browser Close",
    description="Close the browser session associated with task_id, freeing resources.",
    parameters={
        "type": "object",
        "properties": {"task_id": {"type": "string", "default": "default"}},
    },
    handler_type="builtin",
    risk_level="low",
    tags=["browser", "lifecycle"],
)


ALL_DESCRIPTORS = [
    NAVIGATE_DESCRIPTOR,
    SNAPSHOT_DESCRIPTOR,
    CLICK_DESCRIPTOR,
    FILL_DESCRIPTOR,
    CLOSE_DESCRIPTOR,
]


# ── Handler factories ──────────────────────────────────────────────────────


def build_handlers(provider: BrowserProvider) -> dict[str, Any]:
    """Return a {tool_id: async-handler} dict ready to register on ToolGateway.

    Handlers are async so they run directly in the event loop (no thread switch),
    matching Playwright's async API thread-affinity model.
    """

    def _task_id(params: dict[str, Any]) -> str:
        return params.get("task_id") or "default"

    async def navigate(params: dict[str, Any]) -> dict[str, Any]:
        url = params["url"]
        from symbiote.security.network import validate_url

        validate_url(url)  # SSRF guard
        session = await provider.get_or_create_session(_task_id(params))
        final_url = await session.navigate(url)
        return {"url": final_url}

    async def snapshot(params: dict[str, Any]) -> dict[str, Any]:
        session = await provider.get_or_create_session(_task_id(params))
        snap = await session.snapshot()
        return {"text": snap.text, "refs": sorted(snap.refs.keys())}

    async def click(params: dict[str, Any]) -> dict[str, Any]:
        session = await provider.get_or_create_session(_task_id(params))
        await session.click(params["ref"])
        return {"clicked": params["ref"]}

    async def fill(params: dict[str, Any]) -> dict[str, Any]:
        session = await provider.get_or_create_session(_task_id(params))
        await session.fill(params["ref"], params["value"])
        return {"filled": params["ref"]}

    async def close(params: dict[str, Any]) -> dict[str, Any]:
        await provider.close_session(_task_id(params))
        return {"closed": _task_id(params)}

    return {
        NAVIGATE_DESCRIPTOR.tool_id: navigate,
        SNAPSHOT_DESCRIPTOR.tool_id: snapshot,
        CLICK_DESCRIPTOR.tool_id: click,
        FILL_DESCRIPTOR.tool_id: fill,
        CLOSE_DESCRIPTOR.tool_id: close,
    }
