"""SSRF protection — validate URLs before making HTTP requests.

Resolves DNS and blocks requests to private/internal IP ranges,
cloud metadata endpoints, and loopback addresses.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Private and internal IP ranges that must never be accessed by tools
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),      # Carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),         # Loopback
    ipaddress.ip_network("169.254.0.0/16"),      # Link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),       # Private
    ipaddress.ip_network("192.168.0.0/16"),      # Private
    ipaddress.ip_network("::1/128"),             # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),            # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),           # IPv6 link-local
]


class SSRFError(Exception):
    """Raised when a URL targets a blocked network."""


def validate_url(url: str) -> str:
    """Validate that a URL does not resolve to a private/internal IP.

    Args:
        url: The URL to validate.

    Returns:
        The validated URL (unchanged).

    Raises:
        SSRFError: If the URL resolves to a blocked IP range.
        ValueError: If the URL is malformed.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname

    if not hostname:
        raise ValueError(f"No hostname in URL: {url}")

    # Block obvious private hostnames
    if hostname in ("localhost", "metadata.google.internal"):
        raise SSRFError(f"Blocked hostname: {hostname}")

    # Resolve DNS and check IP
    try:
        addr_infos = socket.getaddrinfo(hostname, parsed.port or 80)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for {hostname}: {exc}") from exc

    for _family, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise SSRFError(
                    f"URL {url} resolves to blocked IP {ip} ({network})"
                )

    return url
