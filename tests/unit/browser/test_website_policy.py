"""Unit tests for WebsitePolicy — pure function, no network."""

from __future__ import annotations

import pytest

from symbiote.browser.config import PolicyConfig
from symbiote.browser.hooks.website_policy import (
    DomainBlockedError,
    WebsitePolicy,
)


def _allow(policy: WebsitePolicy, url: str) -> None:
    """Helper: assert URL is allowed by policy (raises if not)."""
    policy.check(url)


def _deny(policy: WebsitePolicy, url: str) -> None:
    """Helper: assert URL is denied by policy."""
    with pytest.raises(DomainBlockedError):
        policy.check(url)


class TestBlocklist:
    def test_exact_match_blocks(self):
        pol = WebsitePolicy(PolicyConfig(blocklist=["example.com"]))
        _deny(pol, "https://example.com/")

    def test_subdomain_also_blocked_by_default(self):
        pol = WebsitePolicy(PolicyConfig(blocklist=["example.com"]))
        _deny(pol, "https://sub.example.com/")
        _deny(pol, "https://deep.sub.example.com/")

    def test_wildcard_blocks_subdomains_only(self):
        pol = WebsitePolicy(PolicyConfig(blocklist=["*.ads.com"]))
        _deny(pol, "https://x.ads.com/")
        _deny(pol, "https://deep.tracker.ads.com/")
        # Bare host NOT in wildcard set
        _allow(pol, "https://ads.com/")

    def test_unrelated_domain_passes(self):
        pol = WebsitePolicy(PolicyConfig(blocklist=["bad.com"]))
        _allow(pol, "https://good.com/")
        _allow(pol, "https://example.org/")

    def test_case_insensitive(self):
        pol = WebsitePolicy(PolicyConfig(blocklist=["Example.com"]))
        _deny(pol, "https://EXAMPLE.COM/")
        _deny(pol, "https://Example.Com/")


class TestAllowlist:
    def test_only_listed_hosts_pass(self):
        pol = WebsitePolicy(PolicyConfig(allowlist=["wikipedia.org"]))
        _allow(pol, "https://wikipedia.org/")
        _allow(pol, "https://en.wikipedia.org/")
        _deny(pol, "https://example.com/")

    def test_wildcard_allowlist(self):
        pol = WebsitePolicy(PolicyConfig(allowlist=["*.gov.br"]))
        _allow(pol, "https://www.fazenda.gov.br/")
        _deny(pol, "https://gov.br/")  # bare host not matched by wildcard
        _deny(pol, "https://anything.com/")

    def test_blocklist_wins_over_allowlist(self):
        pol = WebsitePolicy(
            PolicyConfig(
                allowlist=["example.com"],
                blocklist=["bad.example.com"],
            )
        )
        _allow(pol, "https://example.com/")
        _allow(pol, "https://good.example.com/")
        _deny(pol, "https://bad.example.com/")


class TestEdgeCases:
    def test_no_hostname_raises(self):
        pol = WebsitePolicy(PolicyConfig(blocklist=["x"]))
        _deny(pol, "not-a-url")
        _deny(pol, "file:///etc/passwd")

    def test_allow_internal_unblocks_loopback(self):
        pol = WebsitePolicy(
            PolicyConfig(blocklist=["127.0.0.1"], allow_internal=True)
        )
        _allow(pol, "http://127.0.0.1:8080/")
        _allow(pol, "http://localhost/")

    def test_empty_policy_allows_everything(self):
        pol = WebsitePolicy(PolicyConfig())
        _allow(pol, "https://anything.example.com/")

    def test_blank_pattern_ignored(self):
        pol = WebsitePolicy(PolicyConfig(blocklist=["", "   ", "real.com"]))
        _deny(pol, "https://real.com/")
        _allow(pol, "https://x.com/")
