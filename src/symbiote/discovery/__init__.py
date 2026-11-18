"""Discovery — environment scanning and tool registration.

``parse_openapi`` and ``http_config_for`` are the two steps a host needs to
turn an OpenAPI description into tools it registers by itself: they read and
build, and store or authorize nothing.
"""

from symbiote.discovery.loader import http_config_for
from symbiote.discovery.service import OpenAPIOperation, parse_openapi

__all__ = ["OpenAPIOperation", "http_config_for", "parse_openapi"]
