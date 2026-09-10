"""Provider URL egress policy shared by Core validation and Workers."""

from __future__ import annotations

import socket
from ipaddress import IPv4Address, IPv6Address, ip_address
from urllib.parse import urlsplit


class ProviderEgressError(ValueError):
    """Provider target is outside the permitted egress policy."""


def validate_provider_egress(url: str, *, resolve_dns: bool = True) -> None:
    try:
        addresses = resolve_provider_addresses(url, resolve_dns=resolve_dns)
    except ProviderEgressError as error:
        if not resolve_dns and "DNS resolution failed" in str(error):
            return
        raise
    if not addresses and not resolve_dns:
        return


def resolve_provider_addresses(url: str, *, resolve_dns: bool = True) -> tuple[str, ...]:
    """Resolve and validate a provider target for the socket that will connect."""
    parsed = urlsplit(url)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.netloc
    ):
        raise ProviderEgressError("Provider target URL contains unsupported components")
    try:
        port = parsed.port
    except ValueError as error:
        raise ProviderEgressError("Provider target port is invalid") from error
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    if scheme not in {"http", "https"} or not host:
        raise ProviderEgressError("Provider targets must use HTTPS")
    if port is None:
        port = 443 if scheme == "https" else 80
    addresses: tuple[IPv4Address | IPv6Address, ...]
    try:
        literal = ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (literal,)
    else:
        if scheme == "https" and host.lower() == "localhost":
            raise ProviderEgressError("HTTPS provider targets cannot be loopback")
        if scheme == "http" and host.lower() != "localhost":
            raise ProviderEgressError("HTTP provider targets must be loopback")
        try:
            addresses = tuple(
                {ip_address(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
            )
        except OSError as error:
            if not resolve_dns:
                return ()
            raise ProviderEgressError("Provider target DNS resolution failed") from error
    if not addresses:
        raise ProviderEgressError("Provider target DNS resolution returned no addresses")
    if scheme == "https" and any(not address.is_global for address in addresses):
        raise ProviderEgressError("Provider target resolves to a private or reserved address")
    if scheme == "http" and any(not address.is_loopback for address in addresses):
        raise ProviderEgressError("HTTP provider targets must be loopback")
    return tuple(str(address) for address in addresses)
