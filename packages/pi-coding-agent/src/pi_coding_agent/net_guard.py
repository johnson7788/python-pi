"""Outbound address safety: decide whether a URL may reach where it points.

Why this exists
---------------
Two places hand a URL to an HTTP client: the sandbox provisioner (operator config)
and, in future, any fetch tool a plugin exposes.  A plain string check is not
enough, because the dangerous destinations are spelled many ways — ``127.0.0.1``,
``[::1]``, ``[::ffff:127.0.0.1]``, ``169.254.169.254`` (the cloud metadata
service), ``100.64.0.0/10`` (CGNAT), or a hostname that resolves to one of them.
``ipaddress.is_private`` also misses link-local IPv6 and translated forms.

So the unit of judgement is a **resolved address class**, not a string:

* ``public``      — routable on the internet
* ``private``     — RFC1918 / ULA / loopback / CGNAT
* ``link_local``  — 169.254/16 and fe80::/10, i.e. the metadata service
* ``reserved``    — 0/8, 240/4, documentation ranges, Teredo, multicast
* ``invalid``     — not an address at all

A policy says which classes are allowed; **unknown is denied**, including a DNS
name that cannot be resolved, because "we could not check" is not "it is safe".
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

#: ``100.64.0.0/10``: carrier-grade NAT.  ``ipaddress`` calls it neither private
#: nor reserved, but it is not reachable from the public internet either.
_CARRIER_NAT = ipaddress.ip_network("100.64.0.0/10")
#: ``64:ff9b::/96``: NAT64, whose low 32 bits are an embedded IPv4 address.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

ADDRESS_CLASSES = (
    "public",
    "private",
    "loopback",
    "link_local",
    "reserved",
    "invalid",
)


class OutboundURLRejected(ValueError):
    """The URL points somewhere the policy does not allow."""


@dataclass(frozen=True)
class OutboundPolicy:
    """Which address classes an outbound client may reach."""

    allow_public: bool = True
    allow_private: bool = False
    #: The metadata service lives here; allowing it should be a deliberate act even
    #: in a deployment that allows private ranges.
    allow_link_local: bool = False
    schemes: tuple[str, ...] = ("http", "https")
    #: Ports allowed when the URL specifies one (empty = any).
    ports: tuple[int, ...] = field(default_factory=tuple)

    def allows(self, address_class: str) -> bool:
        if address_class == "public":
            return self.allow_public
        if address_class in ("private", "loopback"):
            return self.allow_private
        if address_class == "link_local":
            return self.allow_link_local
        return False


def _embedded_address(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address tunnels, when it tunnels one."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def classify_address(address: str) -> str:
    """The policy class of one IP literal.

    ``invalid`` is its own answer rather than an exception: a caller that forgets
    to check must fail closed, and that is what ``OutboundPolicy.allows`` does with
    an unknown class.
    """
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return "invalid"

    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_address(ip)
        if embedded is not None:
            return classify_address(str(embedded))
        # Teredo embeds the client in a way that is not worth decoding: the packet
        # still leaves through a tunnel, so treat it as reserved.
        if ip in ipaddress.ip_network("2001::/32"):
            return "reserved"
        if ip.is_unspecified:
            return "reserved"
        if ip.is_loopback:
            return "loopback"
        if ip.is_link_local:
            return "link_local"
        # ``is_reserved`` first: ipaddress also calls 240/4 "private", and a
        # deployment that allows private networks must still not reach it.
        if ip.is_reserved or ip.is_multicast:
            return "reserved"
        if ip.is_private:
            return "private"
        return "public"

    if ip.is_unspecified:
        return "reserved"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_reserved or ip.is_multicast:
        return "reserved"
    if ip.is_private or ip in _CARRIER_NAT:
        return "private"
    return "public"


def resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    """Every address ``host`` resolves to, or ``()`` when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return ()
    return tuple(dict.fromkeys(str(info[4][0]) for info in infos))


def validate_outbound_url(
    url: str, policy: OutboundPolicy | None = None, *, resolve: bool = True
) -> str:
    """Return ``url`` unchanged when every address it reaches is allowed.

    DNS names are resolved here and **every** answer must pass, so a host that
    round-robins a public and a private address is rejected.  This is a
    pre-request check; a determined attacker can still re-resolve between this call
    and the request (DNS rebinding), which is why callers should also validate at
    connect time when the client supports it.

    ``resolve=False`` checks only what is knowable without DNS: scheme, credentials,
    port and a literal IP.  A caller that must not depend on name resolution yet
    (startup, or a URL whose DNS is expected to appear later) uses that form and
    then calls again with ``resolve=True`` before dialling.
    """
    resolved_policy = policy or OutboundPolicy()
    text = (url or "").strip()
    if not text:
        raise OutboundURLRejected("empty URL")

    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme not in resolved_policy.schemes:
        raise OutboundURLRejected(f"scheme {scheme or '(none)'!r} is not allowed")
    if parts.username or parts.password:
        # Credentials in a URL end up in logs, proxies and error messages.
        raise OutboundURLRejected("URLs with embedded credentials are not allowed")
    host = (parts.hostname or "").strip()
    if not host:
        raise OutboundURLRejected("URL has no host")
    port = parts.port or (443 if scheme == "https" else 80)
    if resolved_policy.ports and port not in resolved_policy.ports:
        raise OutboundURLRejected(f"port {port} is not allowed")

    if not resolve:
        # A literal IP still gets classified; a name is trusted to the dial-time check.
        literal = classify_address(host)
        if literal == "invalid":
            return text
        if not resolved_policy.allows(literal):
            raise OutboundURLRejected(f"{host!r} is a forbidden address ({literal})")
        return text

    addresses = resolve_addresses(host, port)
    if not addresses:
        # Unresolvable is not safe: we could not classify it.
        raise OutboundURLRejected(f"{host!r} does not resolve")

    rejected: list[str] = []
    for address in addresses:
        address_class = classify_address(address)
        if not resolved_policy.allows(address_class):
            rejected.append(f"{address} ({address_class})")
    if rejected:
        raise OutboundURLRejected(f"{host!r} resolves to a forbidden address: {', '.join(rejected)}")
    return text


def is_allowed(url: str, policy: OutboundPolicy | None = None) -> bool:
    """Boolean form, for call sites that prefer a check to an exception."""
    try:
        validate_outbound_url(url, policy)
    except OutboundURLRejected:
        return False
    return True


def local_only_policy(*, allow_public: bool) -> OutboundPolicy:
    """The provisioner's policy: private networks are the normal case.

    A sandbox provisioner almost always lives on a private address or localhost, so
    private is allowed by default and **public is opt-in** — the reverse default
    would let a misconfigured URL ship the shared token to the internet.
    """
    return OutboundPolicy(allow_public=allow_public, allow_private=True, allow_link_local=False)
