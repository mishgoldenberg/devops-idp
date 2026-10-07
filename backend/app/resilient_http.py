"""Outbound calls: the one client every integration uses, whether it verifies TLS,
and what to tell a person when a call fails.

Every outbound request is made through `Client` or `AsyncClient` (scripts/check_code_hygiene.py
fails a direct httpx client). They refuse an address that climbs out of its path, which
httpx would otherwise collapse without a word: "/table/incident/../../sys_user" is sent
as "/sys_user", so a value placed in a path could reach any endpoint the account can.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote

import httpx

log = logging.getLogger(__name__)


class UnsafeURL(ValueError):
    """An outbound address with a "." or ".." path segment, raw or percent-encoded."""


def _climbs(url: str) -> bool:
    path = re.split(r"[?#]", str(url), maxsplit=1)[0]
    path = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*", "", path)
    for _ in range(3):  # encoded twice is still a climb once a server decodes it
        if any(part in (".", "..") for part in re.split(r"[/\\]", path)):
            return True
        decoded = unquote(path)
        if decoded == path:
            return False
        path = decoded
    return True


def _checked(url: Any) -> Any:
    if isinstance(url, str) and _climbs(url):
        log.warning("Outbound request refused: %r leaves its path", url[:300])
        raise UnsafeURL("That address is not allowed.")
    return url


class Client(httpx.Client):
    """httpx.Client that verifies TLS as configured and refuses a climbing path."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("verify", tls_verify())
        super().__init__(**kwargs)

    def build_request(self, method: str, url: Any, **kwargs: Any) -> httpx.Request:
        return super().build_request(method, _checked(url), **kwargs)


class AsyncClient(httpx.AsyncClient):
    """httpx.AsyncClient that verifies TLS as configured and refuses a climbing path."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("verify", tls_verify())
        super().__init__(**kwargs)

    def build_request(self, method: str, url: Any, **kwargs: Any) -> httpx.Request:
        return super().build_request(method, _checked(url), **kwargs)


def far_message(resp: httpx.Response) -> str:
    """The other system's own error message from a JSON answer, at most 200 characters,
    or "" -- never the raw body, which can be an HTML page or echo the request."""
    try:
        body = resp.json()
    except Exception:
        return ""
    candidates = [body.get("message"), body.get("error"), body.get("errors")] if isinstance(body, dict) else []
    for value in candidates:
        if isinstance(value, list) and value:
            value = value[0]
        if isinstance(value, dict):
            value = value.get("message") or value.get("detail")
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


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
