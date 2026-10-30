"""Access control helpers for remote (tunnel / reverse-proxy) requests.

Pure functions without Streamlit dependencies so they can be unit-tested.
"""
from __future__ import annotations

import hmac
from collections.abc import Mapping

LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})

# Headers added by Cloudflare tunnels or generic reverse proxies.
_PROXY_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "X-Forwarded-Host")


def _get_header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup."""
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        for key, val in headers.items():
            if str(key).lower() == lowered:
                value = val
                break
    return str(value or "").strip()


def _hostname(host_header: str) -> str:
    """Strip the port from a Host header value (IPv6 aware)."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_remote_request(headers: Mapping[str, str] | None) -> bool:
    """Return True when the request does not come directly from the host machine.

    A request is considered remote if it carries proxy headers (Cloudflare tunnel,
    reverse proxy) or if its Host header is not a loopback name. Missing headers
    (e.g. bare script execution or tests) are treated as local.
    """
    if not headers:
        return False
    if any(_get_header(headers, name) for name in _PROXY_HEADERS):
        return True
    host = _get_header(headers, "Host")
    if not host:
        return False
    return _hostname(host) not in LOCAL_HOSTNAMES


def check_access_password(provided: str | None, expected: str | None) -> bool:
    """Constant-time password comparison. An empty expected password never matches."""
    if not expected or provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))

