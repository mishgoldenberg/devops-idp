"""Who a person IS, as opposed to what they call themselves in the portal.

TWO NAMES
---------
``users.full_name`` is the DISPLAY name. It starts as the identity provider's name and
the person may change it to anything on their profile -- a nickname, a joke, a blank.
That is fine for a greeting and useless on a support ticket: a ticket from "Batman"
reaches a support team that cannot tell who opened it.

``users.sso_name`` is the name the identity provider gives the person (mostly their
Hebrew name), refreshed at every SSO sign-in and never editable in the portal. Every
record raised IN someone's name outside the portal -- ServiceNow tickets, requested
items -- takes it from ``trusted_name`` here, whatever the browser sent.

A NAME THAT ONLY ARRIVES AT SIGN-IN
-----------------------------------
The provider's name reaches the portal in the ID token and nowhere else, so a session
that began before the portal kept it has none to give. Tokens minted by the SSO
callback carry ``security.SSO_NAME_CLAIM``; ``security.decode_access_token`` ends,
ONCE, a session without it whose account has no provider name on record
(``security.session_needs_provider_name``). The new token carries the claim whatever
happened, so nobody can be sent round twice.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from db import query_one

log = logging.getLogger(__name__)

_MAX = 255


def sso_name_from_claims(claims: Dict[str, Any]) -> str:
    """The person's name from an ID token: ``name``, else given + family, else
    ``preferred_username``, else the e-mail.

    The ONE rule for it: the SSO callback names a new account's display name with this
    too, so the name on a ticket is exactly the one the Hub greeted them with the first
    time -- and a provider that sends no ``name`` claim still yields something, instead
    of an empty value that leaves the ticket on the display name."""
    claims = claims or {}
    name = str(claims.get("name") or "").strip()
    if not name:
        parts = [str(claims.get(k) or "").strip() for k in ("given_name", "family_name")]
        name = " ".join(p for p in parts if p)
    if not name:
        name = str(claims.get("preferred_username") or claims.get("email") or "").strip()
    return name[:_MAX]


def record_sso_name(user_id: str, name: str) -> None:
    """Remember the provider's name for this account. Best-effort: a sign-in must never
    fail because this could not be written -- but it says so, because a missing SSO
    name is what puts a display name on the next ticket."""
    name = (name or "").strip()[:_MAX]
    if not user_id or not name:
        return
    try:
        query_one(
            "UPDATE users SET sso_name = %s WHERE id = %s AND sso_name IS DISTINCT FROM %s RETURNING id",
            [name, user_id, name],
        )
    except Exception as exc:
        log.warning("identity: could not record the SSO name for user %s: %s: %s",
                    user_id, type(exc).__name__, exc)


def trusted_name(*, user_id: Optional[str] = None, email: Optional[str] = None) -> str:
    """The name to put on anything raised in this person's name outside the portal.

    The identity provider's name when the portal has seen one. Otherwise -- a local
    account -- the account's login, which the person cannot change either. Never the
    display name: that is the one field here its owner can set to anything.
    """
    row: Optional[Dict[str, Any]] = None
    try:
        if user_id:
            row = query_one("SELECT sso_name, username, email FROM users WHERE id = %s", [user_id])
        if not row and email:
            row = query_one(
                "SELECT sso_name, username, email FROM users WHERE LOWER(TRIM(email)) = %s",
                [str(email).strip().lower()],
            )
    except Exception as exc:
        log.warning("identity: could not read the name for %s: %s: %s",
                    user_id or email, type(exc).__name__, exc)
    return name_from_row(row or {}, email=email)


def name_from_row(row: Dict[str, Any], *, email: Optional[str] = None) -> str:
    """trusted_name's rule, for a caller that already holds the users row
    (sso_name, username, email) and should not read it twice."""
    sso = str(row.get("sso_name") or "").strip()
    if sso:
        return sso[:_MAX]
    login = str(row.get("username") or "").strip()
    mail = str(row.get("email") or email or "").strip()
    if login and mail and mail.lower() not in login.lower():
        return f"{login} ({mail})"[:_MAX]
    return (login or mail or "Unknown user")[:_MAX]

