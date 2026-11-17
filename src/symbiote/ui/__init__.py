"""The ``<symbiote-chat>`` web component, shipped with the package.

A host that serves the chat widget reads it from here instead of keeping its
own copy::

    from symbiote.ui import widget_path, widget_version

    @app.get("/vendor/symbiote-chat.js")
    def chat_js():
        return FileResponse(widget_path(), media_type="application/javascript")

The file is the single-file build of ``symbiote-ui/`` (``symbiote-ui/build.sh``
writes it here too). See ``symbiote-ui/README.md`` for the component's API.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

WIDGET_FILENAME = "symbiote-chat.js"


def widget_path() -> Path:
    """Path of ``symbiote-chat.js`` inside the installed package."""
    return Path(__file__).with_name(WIDGET_FILENAME)


@lru_cache(maxsize=1)
def widget_version() -> str:
    """Twelve hex digits that change whenever the widget's file changes: use
    it in the script URL (``?v=...``) so browsers drop a cached old build."""
    return hashlib.sha256(widget_path().read_bytes()).hexdigest()[:12]


__all__ = ["WIDGET_FILENAME", "widget_path", "widget_version"]
