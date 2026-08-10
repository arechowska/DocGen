from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit


class UntrustedEndpoint(ValueError):
    """An outbound endpoint is outside the explicitly trusted origin policy."""


def validate_trusted_endpoint(endpoint: str, trusted_hosts: Iterable[str]) -> str:
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        _ = parsed.port
    except (TypeError, ValueError):
        raise UntrustedEndpoint from None

    allowed_hosts = {candidate.strip().lower() for candidate in trusted_hosts}
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or host is None
        or host.lower() not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
        or "\\" in endpoint
    ):
        raise UntrustedEndpoint
    return endpoint.rstrip("/")


__all__ = ["UntrustedEndpoint", "validate_trusted_endpoint"]
