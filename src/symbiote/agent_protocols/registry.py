"""Explicit registry for versioned agent protocols."""

from __future__ import annotations

from collections.abc import Iterable

from symbiote.agent_protocols.base import AgentProtocol, AgentProtocolError
from symbiote.agent_protocols.legacy import LegacyTextProtocol
from symbiote.agent_protocols.poolside import PoolsideLagunaProtocol

DEFAULT_AGENT_PROTOCOL_ID = "legacy_text_v1"


class AgentProtocolRegistry:
    """Resolve protocols by exact ID or explicit injected implementation."""

    def __init__(
        self,
        protocols: Iterable[AgentProtocol] | None = None,
        *,
        default_protocol_id: str = DEFAULT_AGENT_PROTOCOL_ID,
    ) -> None:
        self._protocols: dict[str, AgentProtocol] = {}
        self._default_protocol_id = default_protocol_id
        for protocol in protocols or ():
            self.register(protocol)

    def register(
        self,
        protocol: AgentProtocol,
        *,
        replace: bool = False,
    ) -> None:
        """Register one exact protocol ID."""
        protocol_id = protocol.protocol_id
        if not protocol_id:
            raise AgentProtocolError("agent protocol ID must not be empty")
        if protocol_id in self._protocols and not replace:
            raise AgentProtocolError(
                f"agent protocol {protocol_id!r} is already registered"
            )
        self._protocols[protocol_id] = protocol

    def resolve(
        self,
        override: str | AgentProtocol | None = None,
    ) -> AgentProtocol:
        """Resolve an explicit override, or the configured legacy fallback."""
        if isinstance(override, AgentProtocol):
            return override
        protocol_id = (
            self._default_protocol_id
            if override is None
            else override
        )
        try:
            return self._protocols[protocol_id]
        except KeyError as exc:
            raise AgentProtocolError(
                f"unknown agent protocol {protocol_id!r}; "
                f"registered={self.list_protocols()}"
            ) from exc

    def list_protocols(self) -> list[str]:
        """Return registered IDs in deterministic order."""
        return sorted(self._protocols)


def create_default_protocol_registry() -> AgentProtocolRegistry:
    """Build the explicit built-in registry with legacy as fallback."""
    return AgentProtocolRegistry(
        protocols=(
            LegacyTextProtocol(),
            PoolsideLagunaProtocol(),
        ),
        default_protocol_id=DEFAULT_AGENT_PROTOCOL_ID,
    )
