"""Ports — abstract interfaces for the kernel's external dependencies."""

from __future__ import annotations

from typing import Any, Protocol


class StoragePort(Protocol):
    """Structural interface every storage adapter must satisfy."""

    def execute(self, sql: str, params: tuple | None = None) -> Any: ...

    def fetch_one(self, sql: str, params: tuple | None = None) -> dict | None: ...

    def fetch_all(self, sql: str, params: tuple | None = None) -> list[dict]: ...

    def close(self) -> None: ...
