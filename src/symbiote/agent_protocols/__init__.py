"""Versioned model-facing agent protocols."""

from symbiote.agent_protocols.base import (
    AgentProtocol,
    AgentProtocolError,
    PreparedTurn,
    ProtocolCapabilities,
    ProtocolState,
)
from symbiote.agent_protocols.legacy import LegacyTextProtocol
from symbiote.agent_protocols.poolside import PoolsideLagunaProtocol
from symbiote.agent_protocols.registry import (
    DEFAULT_AGENT_PROTOCOL_ID,
    AgentProtocolRegistry,
    create_default_protocol_registry,
)

__all__ = [
    "DEFAULT_AGENT_PROTOCOL_ID",
    "AgentProtocol",
    "AgentProtocolError",
    "AgentProtocolRegistry",
    "LegacyTextProtocol",
    "PoolsideLagunaProtocol",
    "PreparedTurn",
    "ProtocolCapabilities",
    "ProtocolState",
    "create_default_protocol_registry",
]
