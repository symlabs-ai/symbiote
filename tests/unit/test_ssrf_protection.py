"""Tests for SSRF protection — B-14."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from symbiote.security.network import SSRFError, validate_url


class TestValidateUrl:
    def test_public_url_passes(self) -> None:
        # Mock DNS to return a public IP
        with patch("symbiote.security.network.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
            result = validate_url("https://example.com/api")
            assert result == "https://example.com/api"

    def test_blocks_localhost(self) -> None:
        with pytest.raises(SSRFError, match="Blocked hostname"):
            validate_url("http://localhost:8080/secret")

    def test_blocks_metadata_hostname(self) -> None:
        with pytest.raises(SSRFError, match="Blocked hostname"):
            validate_url("http://metadata.google.internal/v1/")

    def test_blocks_loopback_ip(self) -> None:
        with patch("symbiote.security.network.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 80))]
            with pytest.raises(SSRFError, match="127.0.0.1"):
                validate_url("http://evil-redirect.com/")

    def test_blocks_private_10_network(self) -> None:
        with patch("symbiote.security.network.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.1", 80))]
            with pytest.raises(SSRFError, match="10.0.0.1"):
                validate_url("http://internal.corp/api")

    def test_blocks_private_172_network(self) -> None:
        with patch("symbiote.security.network.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("172.16.0.5", 80))]
            with pytest.raises(SSRFError, match="172.16.0.5"):
                validate_url("http://docker-host/")

    def test_blocks_private_192_network(self) -> None:
        with patch("symbiote.security.network.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("192.168.1.1", 80))]
            with pytest.raises(SSRFError, match="192.168.1.1"):
                validate_url("http://router.local/admin")

    def test_blocks_cloud_metadata_ip(self) -> None:
        with patch("symbiote.security.network.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("169.254.169.254", 80))]
            with pytest.raises(SSRFError, match="169.254.169.254"):
                validate_url("http://cloud-metadata/latest/")

    def test_blocks_ipv6_loopback(self) -> None:
        with patch("symbiote.security.network.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(10, 1, 6, "", ("::1", 80, 0, 0))]
            with pytest.raises(SSRFError, match="::1"):
                validate_url("http://ipv6-loopback/")

    def test_blocks_carrier_grade_nat(self) -> None:
        with patch("symbiote.security.network.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("100.64.0.1", 80))]
            with pytest.raises(SSRFError, match="100.64.0.1"):
                validate_url("http://cgnat-host/")

    def test_no_hostname_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="No hostname"):
            validate_url("not-a-url")

    def test_dns_failure_raises_value_error(self) -> None:
        import socket

        with patch("symbiote.security.network.socket.getaddrinfo") as mock_dns:
            mock_dns.side_effect = socket.gaierror("DNS failed")
            with pytest.raises(ValueError, match="DNS resolution failed"):
                validate_url("http://nonexistent.invalid/")


class TestHttpToolSSRFIntegration:
    """Verify SSRF protection is wired into the HTTP tool handler."""

    def test_http_tool_blocks_internal_url(self) -> None:
        from symbiote.environment.descriptors import HttpToolConfig
        from symbiote.environment.tools import _make_http_handler

        config = HttpToolConfig(
            method="GET",
            url_template="http://localhost:9999/secret/{id}",
        )
        handler = _make_http_handler(config)

        with pytest.raises(Exception, match="[Bb]locked"):
            handler({"id": "42"})
