"""Small helpers that more than one module needs. Import these; never copy them."""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request, status

from security import AuthUser, get_current_user, has_effective_admin_access_live


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def truthy(value: Any) -> bool:
    """A flag from JSON or a form. Never bool(): these APIs answer with the string "false"."""
    return str(value).strip().lower() in ("true", "1", "yes")


def looks_like_sys_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", (value or "").strip()))


def require_admin(user: AuthUser) -> AuthUser:
    """403 unless the caller is an admin right now (security.has_effective_admin_access_live)."""
    if not has_effective_admin_access_live(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return user


def admin_user(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    """The signed-in admin, as a route dependency (require_admin)."""
    return require_admin(user)


def caller_email(user: Any) -> str:
    email = (user.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User email missing from auth context")
    return email


def failure_text(system: str, exc: BaseException) -> str:
    """A failure as a sentence for a person: the system and what went wrong, never the
    exception's class or text (the caller logs those)."""
    import httpx
    from resilient_http import explain_integration_failure

    if isinstance(exc, httpx.HTTPError):
        return explain_integration_failure(system, exc)
    return f"{system} failed unexpectedly; the details are in the Hub's log."


def audited_error(request: Request, code: int, detail: str) -> HTTPException:
    """An HTTPException whose reason also reaches the Logs page row (request_audit)."""
    try:
        request.state.audit_detail = detail
    except Exception:
        pass
    return HTTPException(status_code=code, detail=detail)


# ── Actions written to another system (Azure DevOps, GitLab, Confluence) ────

def dry_run(summary: str, checks: list, **extra: Any) -> Dict[str, Any]:
    return {"success": True, "dry_run": True, "summary": summary, "checks": checks, **extra}


def simulated(system: str, summary: str, **extra: Any) -> Dict[str, Any]:
    return {"success": True, "simulated": True, "summary": summary,
            "result": f"Safe Mode is on: nothing was sent to {system}.", **extra}


# ── Links and pictures people save into the portal ──────────────────────────

# A link may point anywhere a browser can go, except at code it would run.
_EXECUTABLE_SCHEMES = ("javascript:", "vbscript:", "data:")

# Raster formats only, recognised by their first bytes. SVG is refused because it
# can carry script; anything else is refused because the declared type proves nothing.
_IMAGE_SIGNATURES = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": (b"RIFF",),
}
_DATA_URL = re.compile(r"data:image/(png|jpe?g|gif|webp);base64,([A-Za-z0-9+/=\s]+)", re.IGNORECASE)


def safe_link(value: Optional[str]) -> str:
    """A link's address, or ValueError if a click on it would run code."""
    url = (value or "").strip()
    squashed = re.sub(r"[\s\x00-\x1f]", "", url).lower()
    if squashed.startswith(_EXECUTABLE_SCHEMES):
        raise ValueError("Links must be web or network addresses, not scripts or embedded data.")
    return url


def safe_image(value: Optional[str], max_bytes: int) -> str:
    """An icon or avatar: an http(s) address, or an uploaded PNG/JPEG/GIF/WebP.

    Checked by its bytes, not by what it says it is, because an upload is stored and
    served back to every viewer of the page. Raises ValueError with the reason."""
    raw = (value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://")):
        return raw
    match = _DATA_URL.fullmatch(raw)
    if not match:
        raise ValueError("Pictures must be PNG, JPEG, GIF or WebP images, or a web address.")
    kind = match.group(1).lower().replace("jpg", "jpeg")
    try:
        data = base64.b64decode(re.sub(r"\s", "", match.group(2)), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("The picture could not be read.") from None
    if len(data) > max_bytes:
        raise ValueError(f"Pictures can be at most {max_bytes // 1024} KB.")
    is_webp = kind != "webp" or data[8:12] == b"WEBP"
    if not (data.startswith(_IMAGE_SIGNATURES[kind]) and is_webp):
        raise ValueError("That file is not the picture it claims to be.")
    return raw
