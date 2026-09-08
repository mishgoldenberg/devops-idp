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

from resilient_http import tls_verify
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from config import get_settings
from db import execute, query_all, query_one


_DISCOVERY_REQUIRED = {"authorization_endpoint", "token_endpoint", "jwks_uri"}

# Domain-separation label for the at-rest encryption subkey. JWT_SECRET is used to
# SIGN portal JWTs; here it must not be used *as* an encryption key directly. We run
# it through HKDF with this info string so the encryption key is an independent
# derivation — a signing-side weakness (alg confusion, a signing oracle) then does
# not hand an attacker the key that protects the OIDC client secret / stored PATs.
_ENC_SUBKEY_INFO = b"devops-hub:secret-encryption:v1"


def _derived_fernet() -> Fernet:
    """Fernet keyed by an HKDF-derived subkey of JWT_SECRET (current scheme)."""
    secret = get_settings().jwt_secret.encode("utf-8")
    subkey = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_ENC_SUBKEY_INFO
    ).derive(secret)
    return Fernet(base64.urlsafe_b64encode(subkey))


def _legacy_fernet() -> Fernet:
    """Fernet keyed by the old raw SHA-256(JWT_SECRET) scheme (decrypt-only).

    Kept so secrets encrypted before the KDF change stay readable; they are
    transparently re-encrypted under the derived subkey on the next save.
    """
    secret = get_settings().jwt_secret.encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


def _fernet() -> MultiFernet:
    # encrypt() uses the first key (derived); decrypt() accepts either, so existing
    # ciphertexts from the legacy scheme keep working during rotation.
    return MultiFernet([_derived_fernet(), _legacy_fernet()])


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Secret cannot be decrypted with the current JWT_SECRET") from exc


def encrypt_client_secret(client_secret: str) -> str:
    return encrypt_secret(client_secret)


def decrypt_client_secret(ciphertext: str) -> str:
    try:
        return decrypt_secret(ciphertext)
    except ValueError as exc:
        raise ValueError("SSO client secret cannot be decrypted with the current JWT_SECRET") from exc


def credential_key_check() -> Dict[str, Any]:
    """Can the CURRENT JWT_SECRET still read the credentials already stored?

    Why this exists, and why it is worth a startup check of its own:

    JWT_SECRET is not only the JWT signing key. Every per-user Azure DevOps PAT in
    ``user_integrations`` and the OIDC client secret are encrypted at rest with a
    key derived from it. So a JWT_SECRET that changes — a rotated Kubernetes
    secret, a namespace rebuilt, a database restored next to a secret from a
    different environment — does not fail loudly. It silently strands every stored
    credential: each user's Azure DevOps widgets go empty, each of them files a
    ticket, and nothing anywhere says why.

    It is also the reason a database backup is only half a backup. Restoring the
    dump without the matching JWT_SECRET gives you a table of unreadable strings.

    Returns a small dict rather than raising: this is a diagnosis, not a
    precondition. A portal whose stored PATs are unreadable still signs people in,
    still shows ServiceNow, and still lets them reconnect — refusing to start would
    take away the very screen they need.
    """
    result: Dict[str, Any] = {"checked": 0, "readable": 0, "unreadable": 0, "ok": True}
    try:
        rows = query_all(
            "SELECT token_encrypted FROM user_integrations "
            "WHERE token_encrypted IS NOT NULL AND token_encrypted <> '' LIMIT 5"
        )
    except Exception as exc:
        result["ok"] = None  # unknown, not broken
        result["error"] = f"could not read stored credentials: {exc}"
        return result

    for row in rows:
        result["checked"] += 1
        try:
            decrypt_secret(str(row.get("token_encrypted") or ""))
            result["readable"] += 1
        except Exception:
            result["unreadable"] += 1

    result["ok"] = result["unreadable"] == 0
    return result


def _normalize_issuer(issuer_uri: str) -> str:
    return (issuer_uri or "").strip().rstrip("/")


def fetch_openid_configuration(issuer_uri: str, timeout: float = 10.0) -> Dict[str, Any]:
    issuer = _normalize_issuer(issuer_uri)
    if not issuer:
        raise ValueError("Issuer URI is required")
    url = f"{issuer}/.well-known/openid-configuration"
    with httpx.Client(verify=tls_verify(), timeout=timeout, follow_redirects=True) as client:
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

