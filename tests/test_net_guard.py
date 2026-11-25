"""Outbound address classes and URL validation.

The payloads are the ones that make string-based checks fail: the metadata service,
its IPv6 and translated spellings, a public name that resolves to loopback, and a
name that does not resolve at all (which must be refused, not assumed safe).

DNS is monkeypatched everywhere: this suite is offline and deterministic, and the
resolver is the only non-deterministic part of the module.
"""

from __future__ import annotations

import pytest

from pi_coding_agent import net_guard
from pi_coding_agent.net_guard import (
    OutboundPolicy,
    OutboundURLRejected,
    classify_address,
    is_allowed,
    local_only_policy,
    validate_outbound_url,
)

#: ``(address, expected class)`` — each row is a distinct way to spell a target.
ADDRESSES = [
    ("8.8.8.8", "public"),
    ("93.184.216.34", "public"),
    ("127.0.0.1", "loopback"),
    ("127.1.2.3", "loopback"),
    ("10.1.2.3", "private"),
    ("172.16.0.1", "private"),
    ("192.168.1.1", "private"),
    ("100.64.0.1", "private"),  # CGNAT: neither public nor ordinary private
    ("169.254.169.254", "link_local"),  # cloud metadata
    ("0.0.0.0", "reserved"),
    ("240.0.0.1", "reserved"),
    ("224.0.0.1", "reserved"),
    ("::1", "loopback"),
    ("fc00::1", "private"),
    ("fd12:3456::1", "private"),
    ("fe80::1", "link_local"),
    ("::", "reserved"),
    ("::ffff:127.0.0.1", "loopback"),  # IPv4-mapped
    ("::ffff:169.254.169.254", "link_local"),
    ("2002:7f00:1::", "loopback"),  # 6to4 of 127.0.0.1
    ("2002:a9fe:a9fe::", "link_local"),  # 6to4 of 169.254.169.254
    ("64:ff9b::7f00:1", "loopback"),  # NAT64 of 127.0.0.1
    ("2001::1", "reserved"),  # Teredo
    ("not-an-address", "invalid"),
    ("", "invalid"),
]


@pytest.mark.parametrize(("address", "expected"), ADDRESSES, ids=[row[0] for row in ADDRESSES])
def test_address_classes(address: str, expected: str) -> None:
    assert classify_address(address) == expected


def _resolves_to(*addresses: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(net_guard, "resolve_addresses", lambda host, port: tuple(addresses))


def test_a_public_https_url_is_allowed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to("93.184.216.34", monkeypatch=monkeypatch)

    assert validate_outbound_url("https://example.com/path?q=1") == "https://example.com/path?q=1"


def test_a_public_name_that_resolves_to_loopback_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSRF shape a hostname check cannot see."""
    _resolves_to("127.0.0.1", monkeypatch=monkeypatch)

    with pytest.raises(OutboundURLRejected, match="forbidden address"):
        validate_outbound_url("https://innocent-looking.example")


def test_one_bad_answer_rejects_the_whole_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to("93.184.216.34", "169.254.169.254", monkeypatch=monkeypatch)

    with pytest.raises(OutboundURLRejected):
        validate_outbound_url("https://mixed.example")


def test_an_unresolvable_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to(monkeypatch=monkeypatch)

    with pytest.raises(OutboundURLRejected, match="does not resolve"):
        validate_outbound_url("https://nope.invalid")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com",
        "https://user:pass@example.com/",
        "https://",
    ],
)
def test_unsafe_url_shapes_are_refused(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to("93.184.216.34", monkeypatch=monkeypatch)

    with pytest.raises(OutboundURLRejected):
        validate_outbound_url(url)


def test_a_literal_address_is_classified_without_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(net_guard, "resolve_addresses", lambda host, port: (host,))

    with pytest.raises(OutboundURLRejected):
        validate_outbound_url("http://169.254.169.254/latest/meta-data/")
    assert is_allowed("http://10.0.0.5:8080", local_only_policy(allow_public=False)) is True


def test_the_provisioner_policy_allows_private_but_not_public(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = local_only_policy(allow_public=False)
    _resolves_to("10.0.0.5", monkeypatch=monkeypatch)
    assert is_allowed("http://provisioner.internal:8080", policy) is True

    _resolves_to("93.184.216.34", monkeypatch=monkeypatch)
    assert is_allowed("https://provisioner.example.com", policy) is False


def test_allowing_public_is_an_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to("93.184.216.34", monkeypatch=monkeypatch)

    assert is_allowed("https://provisioner.example.com", local_only_policy(allow_public=True))


def test_link_local_stays_forbidden_even_when_private_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to("169.254.169.254", monkeypatch=monkeypatch)

    assert is_allowed("http://metadata.internal", local_only_policy(allow_public=True)) is False


def test_a_port_allowlist_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to("10.0.0.5", monkeypatch=monkeypatch)
    policy = OutboundPolicy(allow_private=True, ports=(8080,))

    assert is_allowed("http://box:8080/x", policy) is True
    assert is_allowed("http://box:9090/x", policy) is False


def test_the_exception_is_a_value_error():
    """Callers that only catch ValueError must still be safe."""
    assert issubclass(OutboundURLRejected, ValueError)
