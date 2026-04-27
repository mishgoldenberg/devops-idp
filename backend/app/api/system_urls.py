"""
Exposes the external URLs for each third-party system the portal links out to.

The portal header shows a "Go to <System>" button on each system page that
deep-links into the real external console (Azure DevOps, ServiceNow, etc.).
We want those URLs to be driven by configuration rather than hardcoded in
templates so the same build can target different environments.

Resolution order for each system:
  1. The configured integration base URL env var.
  2. ``None`` — the frontend renders the button disabled when no URL resolves.

The endpoint is authenticated (any logged-in user) because the URLs themselves
are not secrets, but we don't want unauthenticated visitors probing the
config.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends

from security import AuthUser, get_current_user


router = APIRouter()


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        val = os.getenv(name)
        if val and val.strip():
            return val.strip()
    return None


def _strip_api_suffix(url: str) -> str:
    """Best-effort: turn an API base URL into a human console URL."""
    lowered = url.rstrip("/")
    for suffix in ("/api", "/rest", "/_apis"):
        if lowered.lower().endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def _resolve_system_urls() -> Dict[str, Optional[str]]:
    ado = _first_env("AZURE_DEVOPS_BASE_URL")
    snow = _first_env("SNOW_BASE_URL")
    sonar = _first_env("SONARQUBE_BASE_URL")
    artifactory = _first_env("ARTIFACTORY_BASE_URL")

    return {
        "azure_devops": _strip_api_suffix(ado) if ado else None,
        "servicenow": _strip_api_suffix(snow) if snow else None,
        "sonarqube": _strip_api_suffix(sonar) if sonar else None,
        "artifactory": _strip_api_suffix(artifactory) if artifactory else None,
    }


@router.get("/system-urls")
def get_system_urls(
    _current_user: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the configured external console URL for each system."""
    return {"success": True, "data": _resolve_system_urls()}


def get_system_urls_for_template() -> Dict[str, Optional[str]]:
    """Server-side helper: used by UI routes that render system pages so the
    ``Open`` button can be templated without an extra round-trip."""
    return _resolve_system_urls()
