"""The ``<symbiote-chat>`` widget ships inside the Python package (B-82)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.ui import WIDGET_FILENAME, widget_path, widget_version

_DIST = Path(__file__).resolve().parents[2] / "symbiote-ui" / "dist" / WIDGET_FILENAME


def test_widget_file_is_in_the_package() -> None:
    path = widget_path()

    assert path.is_file()
    assert path.name == "symbiote-chat.js"
    assert "customElements.define('symbiote-chat'" in path.read_text(encoding="utf-8")


def test_widget_version_follows_the_file() -> None:
    version = widget_version()

    assert len(version) == 12
    assert int(version, 16) >= 0


def test_packaged_widget_is_the_current_build() -> None:
    """``symbiote-ui/build.sh`` writes both files; a change in the widget's
    source that was built but not copied (or the other way round) shows here."""
    if not _DIST.is_file():
        pytest.skip("symbiote-ui/ is not part of this checkout (e.g. the Docker image)")

    assert widget_path().read_bytes() == _DIST.read_bytes()
