"""
Exposes the external URLs for each third-party system the portal links out to.

The portal header shows an "Open" button on each system page that deep-links
into the real external console (Azure DevOps, ServiceNow, etc.). We want those
URLs to be driven by configuration rather than hardcoded in templates so the
same build can target different environments.

Resolution order for each system:
  1. Dedicated UI URL env var (e.g. ``AZURE_DEVOPS_URL``) — preferred.
  2. Existing API URL env var, with ``/rest`` or ``/_apis`` stripped — best-effort fallback.
  3. ``None`` — the frontend hides the button when no URL is configured.

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
    ado = _first_env("AZURE_DEVOPS_URL", "ADO_URL")
    if not ado:
        api = _first_env("AZURE_DEVOPS_API_URL")
        ado = _strip_api_suffix(api) if api else None

    snow = _first_env("SERVICENOW_URL", "SERVICENOW_INSTANCE_URL")
    sonar = _first_env("SONARQUBE_URL")
    artifactory = _first_env("ARTIFACTORY_URL")
    confluence = _first_env("CONFLUENCE_URL")
    openshift = _first_env("OPENSHIFT_URL", "OPENSHIFT_CONSOLE_URL")
    grafana = _first_env("GRAFANA_URL")
    aws = _first_env("AWS_CONSOLE_URL", "INTERNAL_AWS_URL")

    return {
        "azure_devops": ado,
        "servicenow": snow,
        "sonarqube": sonar,
        "artifactory": artifactory,
        "confluence": confluence,
        "openshift": openshift,
        "grafana": grafana,
        "internal_aws": aws,
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
