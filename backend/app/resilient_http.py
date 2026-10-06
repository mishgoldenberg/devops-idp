"""Outbound calls: whether to verify TLS, and what to tell a person when one fails.

Each integration builds its own httpx.Client with an explicit timeout and handles its
own responses; this module holds the two things they all share.
"""

from __future__ import annotations

import httpx


def _host_of(exc: Exception) -> str:
    """The host the failed call was aimed at, when the exception carries a request."""
    request = getattr(exc, "request", None)
    try:
        return request.url.host if request is not None else ""
    except Exception:
        return ""


def explain_integration_failure(integration: str, exc: Exception) -> str:
    """One sentence a person can act on: the system, the host where known, the likely
    cause and who can fix it. Never the exception's class or text -- log those."""
    host = _host_of(exc)
    where = f" ({host})" if host else ""
    text = str(exc).lower()

    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        code = exc.response.status_code
        if code in (401, 403):
            return (
                f"{integration} rejected the portal's credentials{where}. "
                "The stored account or token is wrong, expired, or lacks permission — "
                "check it under Connections."
            )
        if code == 404:
            return (
                f"{integration} answered 'not found'{where}. "
                "The configured base URL or path is probably wrong — check it under Connections."
            )
        if code == 429:
            return f"{integration} is rate-limiting the portal{where}. Try again in a minute."
        if code >= 500:
            return (
                f"{integration} returned a server error (HTTP {code}){where}. "
                "The problem is on that system, not in the portal."
            )
        return f"{integration} refused the request (HTTP {code}){where}."

    if "name or service not known" in text or "nodename nor servname" in text \
            or "getaddrinfo" in text or "name does not resolve" in text:
        return (
            f"The address for {integration}{where} could not be resolved. "
            "Check the hostname under Connections, or whether DNS is reachable from the cluster."
        )

    if "certificate" in text or "ssl" in text or "tls" in text:
        return (
            f"The portal reached {integration}{where} but could not establish a secure "
            "connection — its certificate was not accepted. An internal CA may need trusting."
        )

    # In a closed network this is nearly always a missing firewall rule, which no
    # amount of retrying fixes -- so say who has to act.
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)) or "connection refused" in text:
        return (
            f"The portal could not reach {integration}{where}. "
            "Nothing answered on that address — usually a firewall rule that does not allow "
            "the portal to reach it, or the system being down. A platform admin needs to "
            "open the route; retrying will not help."
        )

    if isinstance(exc, httpx.TimeoutException):
        return (
            f"{integration}{where} accepted the connection but did not answer in time. "
            "It is reachable but slow or overloaded."
        )

    if isinstance(exc, (httpx.TransportError, httpx.NetworkError)):
        return (
            f"The network connection to {integration}{where} failed mid-request. "
            "If this repeats, the route to that system is unstable or blocked."
        )

    return f"{integration} could not be reached{where}. Please try again shortly."


def tls_verify() -> bool:
    """Whether outbound calls verify TLS certificates (INTEGRATION_TLS_VERIFY, default
    true; an installation behind an internal CA it cannot trust sets it false)."""
    try:
        from config import get_settings

        return get_settings().integration_tls_verify
    except Exception:
        return True
