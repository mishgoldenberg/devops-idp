"""
Thin resiliency helpers for outbound integrations.

Every external call (Azure DevOps, ServiceNow, SonarQube, Artifactory, …) should
flow through ``resilient_get``/``resilient_request`` or the ``safe_integration``
context manager so the portal has:

  * Connect + read timeouts on every HTTP call (default 10s / 15s)
  * One or two retries on transient failures (network + 5xx)
  * A single, user-friendly fallback message bubble up to the UI
  * Full original error logged server-side for operators

This module intentionally keeps zero hard dependencies beyond ``httpx`` and
``fastapi`` so it can be imported from any integration router.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional, TypeVar

import httpx
from fastapi import HTTPException, status


log = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_READ_TIMEOUT_S = 10.0
DEFAULT_RETRIES = 2
SERVICE_UNAVAILABLE_MSG = "Service temporarily unavailable. Please try again shortly."

T = TypeVar("T")


def default_timeout() -> httpx.Timeout:
    """Return the standard integration timeout bundle."""
    return httpx.Timeout(DEFAULT_READ_TIMEOUT_S, connect=DEFAULT_CONNECT_TIMEOUT_S)


def _host_of(exc: Exception) -> str:
    """The host the failed call was aimed at, when the exception carries a request."""
    request = getattr(exc, "request", None)
    try:
        return request.url.host if request is not None else ""
    except Exception:
        return ""


def explain_integration_failure(integration: str, exc: Exception) -> str:
    """Turn an outbound failure into something a person can act on.

    Every failure used to collapse into "Service temporarily unavailable. Please try
    again shortly." — which is wrong about most of them and useless about all of them.
    In a restricted network the overwhelmingly common cause is that the portal cannot open
    the connection at all: the host is not reachable from the cluster because a firewall
    rule is missing. "Try again shortly" sends someone to wait for a problem that will
    never resolve on its own.

    So the message names the system, the host where we know it, the likely cause, and
    who can fix it. The exception itself is still logged in full for the operator; this
    is only the sentence the user sees.
    """
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

    # Name resolution — the address is wrong or there is no DNS entry for it.
    if "name or service not known" in text or "nodename nor servname" in text \
            or "getaddrinfo" in text or "name does not resolve" in text:
        return (
            f"The address for {integration}{where} could not be resolved. "
            "Check the hostname under Connections, or whether DNS is reachable from the cluster."
        )

    # TLS — reachable, but the certificate chain is not accepted.
    if "certificate" in text or "ssl" in text or "tls" in text:
        return (
            f"The portal reached {integration}{where} but could not establish a secure "
            "connection — its certificate was not accepted. An internal CA may need trusting."
        )

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

    return f"{integration} could not be reached{where}. {SERVICE_UNAVAILABLE_MSG}"


def tls_verify() -> bool:
    """Whether outbound integration calls should verify TLS certificates.

    Controlled by INTEGRATION_TLS_VERIFY (default true). Installations with
    internal CAs / self-signed certs set it to "false".
    """
    try:
        from config import get_settings

        return get_settings().integration_tls_verify
    except Exception:
        return True


def resilient_request(
    method: str,
    url: str,
    *,
    integration: str,
    client: Optional[httpx.Client] = None,
    retries: int = DEFAULT_RETRIES,
    **kwargs: Any,
) -> httpx.Response:
    """
    Execute a single HTTP request with retries on transient errors.

    ``integration`` is a short label used only for log messages and the error
    bubbled up to the frontend (e.g. "ServiceNow", "Azure DevOps").  The caller
    is still responsible for translating 4xx responses into domain-specific
    exceptions — this helper only retries on network errors and 5xx.
    """
    attempt = 0
    last_exc: Optional[BaseException] = None
    _client = client or httpx.Client(
        timeout=default_timeout(), follow_redirects=True, verify=tls_verify()
    )
    _owns_client = client is None
    try:
        while attempt <= retries:
            try:
                resp = _client.request(method, url, **kwargs)
                if resp.status_code >= 500 and attempt < retries:
                    log.warning(
                        "%s: %s %s returned %s, retry %s/%s",
                        integration, method, url, resp.status_code, attempt + 1, retries,
                    )
                    attempt += 1
                    time.sleep(_backoff(attempt))
                    continue
                return resp
            except (httpx.TimeoutException, httpx.TransportError, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt >= retries:
                    log.warning(
                        "%s: %s %s failed after %s attempts: %s: %s",
                        integration, method, url, attempt + 1, type(exc).__name__, exc,
                    )
                    break
                log.info(
                    "%s: %s %s transient error (%s), retry %s/%s",
                    integration, method, url, type(exc).__name__, attempt + 1, retries,
                )
                attempt += 1
                time.sleep(_backoff(attempt))
    finally:
        if _owns_client:
            _client.close()

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{integration}: {SERVICE_UNAVAILABLE_MSG}",
    ) from last_exc


def resilient_get(url: str, *, integration: str, **kwargs: Any) -> httpx.Response:
    return resilient_request("GET", url, integration=integration, **kwargs)


@contextmanager
def safe_integration(integration: str) -> Iterator[None]:
    """
    Wrap a block that calls external systems. Unhandled exceptions are logged
    in full and re-raised as a user-friendly HTTP 502 with a single-line detail.

    ``HTTPException`` instances are passed through unchanged so callers can
    still raise 4xx/409/etc. directly when the remote system gave a meaningful
    domain error.
    """
    try:
        yield
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        log.warning(
            "%s: upstream returned %s for %s %s: %s",
            integration,
            exc.response.status_code,
            exc.request.method if exc.request else "?",
            str(exc.request.url) if exc.request else "?",
            exc.response.text[:400] if exc.response is not None else "",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=explain_integration_failure(integration, exc),
        ) from exc
    except (httpx.TimeoutException, httpx.TransportError, httpx.NetworkError) as exc:
        log.warning("%s: network error: %s: %s", integration, type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=explain_integration_failure(integration, exc),
        ) from exc
    except Exception as exc:
        log.exception("%s: unexpected error: %s", integration, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=explain_integration_failure(integration, exc),
        ) from exc


def safe_call(
    integration: str,
    fn: Callable[[], T],
    *,
    default: Optional[T] = None,
) -> Optional[T]:
    """
    Run ``fn`` and, on any exception, log it and return ``default``.

    Use this for *best-effort* reads where a failure should degrade the widget
    gracefully (show an empty state) rather than raise to the user. Prefer
    ``safe_integration`` for write paths that need to surface errors.
    """
    try:
        return fn()
    except Exception as exc:
        log.warning("%s: best-effort call failed: %s: %s", integration, type(exc).__name__, exc)
        return default


def _backoff(attempt: int) -> float:
    """Modest exponential backoff with jitter-free ceiling (keep it boring)."""
    return min(0.5 * (2 ** (attempt - 1)), 2.0)
