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


def tls_verify() -> bool:
    """Whether outbound integration calls should verify TLS certificates.

    Controlled by INTEGRATION_TLS_VERIFY (default true). Closed networks with
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
            detail=f"{integration}: {SERVICE_UNAVAILABLE_MSG}",
        ) from exc
    except (httpx.TimeoutException, httpx.TransportError, httpx.NetworkError) as exc:
        log.warning("%s: network error: %s: %s", integration, type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{integration}: {SERVICE_UNAVAILABLE_MSG}",
        ) from exc
    except Exception as exc:
        log.exception("%s: unexpected error: %s", integration, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{integration}: {SERVICE_UNAVAILABLE_MSG}",
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
