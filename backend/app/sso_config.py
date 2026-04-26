"""Helpers for admin-managed OIDC SSO configuration.

The DB stores the client secret encrypted at rest. The encryption key is
derived from the existing JWT_SECRET, which is already delivered as a Kubernetes
secret, so SSO setup itself stays admin-managed in the database and does not
introduce a second secret-management path.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Dict, Optional

import httpx
from cryptography.fernet import Fernet, InvalidToken

from config import get_settings
from db import execute, query_one


_DISCOVERY_REQUIRED = {"authorization_endpoint", "token_endpoint", "jwks_uri"}


def _fernet() -> Fernet:
    secret = get_settings().jwt_secret.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_client_secret(client_secret: str) -> str:
    return _fernet().encrypt(client_secret.encode("utf-8")).decode("ascii")


def decrypt_client_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("SSO client secret cannot be decrypted with the current JWT_SECRET") from exc


def _normalize_issuer(issuer_uri: str) -> str:
    return (issuer_uri or "").strip().rstrip("/")


def fetch_openid_configuration(issuer_uri: str, timeout: float = 10.0) -> Dict[str, Any]:
    issuer = _normalize_issuer(issuer_uri)
    if not issuer:
        raise ValueError("Issuer URI is required")
    url = f"{issuer}/.well-known/openid-configuration"
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("OpenID configuration response is not a JSON object")
    missing = sorted(k for k in _DISCOVERY_REQUIRED if not data.get(k))
    if missing:
        raise ValueError(f"OpenID configuration missing: {', '.join(missing)}")
    return data


def get_enabled_sso_config() -> Optional[Dict[str, Any]]:
    row = query_one(
        """
        SELECT issuer_uri, client_id, client_secret_encrypted, enabled
        FROM sso_config
        WHERE id = 1 AND enabled = true
        """
    )
    return row or None


def get_sso_config_redacted() -> Dict[str, Any]:
    row = query_one(
        """
        SELECT issuer_uri, client_id, client_secret_encrypted, enabled, updated_at
        FROM sso_config
        WHERE id = 1
        """
    )
    if not row:
        return {
            "issuer_uri": "",
            "client_id": "",
            "enabled": False,
            "client_secret_configured": False,
            "updated_at": None,
        }
    return {
        "issuer_uri": row.get("issuer_uri") or "",
        "client_id": row.get("client_id") or "",
        "enabled": bool(row.get("enabled")),
        "client_secret_configured": bool(row.get("client_secret_encrypted")),
        "updated_at": row.get("updated_at"),
    }


def save_sso_config(
    issuer_uri: str,
    client_id: str,
    client_secret: str,
    enabled: bool = True,
) -> None:
    issuer = _normalize_issuer(issuer_uri)
    encrypted = encrypt_client_secret(client_secret)
    execute(
        """
        INSERT INTO sso_config (id, issuer_uri, client_id, client_secret_encrypted, enabled)
        VALUES (1, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
          issuer_uri = EXCLUDED.issuer_uri,
          client_id = EXCLUDED.client_id,
          client_secret_encrypted = EXCLUDED.client_secret_encrypted,
          enabled = EXCLUDED.enabled,
          updated_at = CURRENT_TIMESTAMP
        """,
        [issuer, client_id.strip(), encrypted, bool(enabled)],
    )

