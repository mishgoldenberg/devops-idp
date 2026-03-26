import json
import os
from typing import Optional

from db import query_one

_MEMORY_PATS: dict[str, str] = {}


def _use_vault() -> bool:
    return os.getenv("USE_VAULT", "false").lower() in {"true", "1", "yes"}


def _vault_client():
    """
    Lazily construct a Vault client if Vault is enabled.
    This is imported only when needed to avoid hard dependency in local dev.
    """
    import hvac  # type: ignore[import]

    addr = os.getenv("VAULT_ADDR")
    token = os.getenv("VAULT_TOKEN")
    if not addr or not token:
        raise RuntimeError("Vault is enabled but VAULT_ADDR or VAULT_TOKEN is not set")
    client = hvac.Client(url=addr, token=token)
    return client


def _vault_secret_path(user_id: str) -> str:
    base = os.getenv("VAULT_PATH", "secret/devops-control-center").rstrip("/")
    # Store per-user ADO PATs under a dedicated sub-path
    return f"{base}/azure-devops/{user_id}"


def store_user_azure_devops_pat(user_id: str, pat: str) -> None:
    """
    Store a user's Azure DevOps PAT in the configured secrets backend.

    - If USE_VAULT=true   -> HashiCorp Vault KV v2 (recommended for prod)
    - Else                -> PostgreSQL system_config (is_sensitive=true)
    """
    if not pat:
        raise ValueError("PAT must not be empty")

    if _use_vault():
        client = _vault_client()
        path = _vault_secret_path(user_id)
        # Do not log the PAT, only the target path if needed by operators.
        client.secrets.kv.v2.create_or_update_secret(
            path=path,
            secret={"azure_devops_pat": pat},
        )
        return

    # Fallback: encrypted configuration table (per-user key)
    key = f"user:{user_id}:azure_devops_pat"
    value = json.dumps({"pat": pat})
    try:
        query_one(
            """
            INSERT INTO system_config (key, value, description, is_sensitive)
            VALUES (%s, %s::jsonb, %s, true)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = CURRENT_TIMESTAMP
            RETURNING key
            """,
            [key, value, "Per-user Azure DevOps PAT"],
        )
    except Exception:
        # Fallback for transient DB problems: keep PAT in-process so widgets can still work.
        _MEMORY_PATS[user_id] = pat


def get_user_azure_devops_pat(user_id: str) -> Optional[str]:
    """
    Retrieve the Azure DevOps PAT for a given user, if configured.

    The PAT is never logged or returned to the frontend; this helper is
    used only by backend adapters when calling Azure DevOps APIs.
    """
    if _use_vault():
        client = _vault_client()
        path = _vault_secret_path(user_id)
        try:
            result = client.secrets.kv.v2.read_secret_version(path=path)
        except Exception:
            return None
        data = (result or {}).get("data", {}).get("data", {}) or {}
        pat = data.get("azure_devops_pat")
        return str(pat) if pat else None

    key = f"user:{user_id}:azure_devops_pat"
    try:
        row = query_one(
            "SELECT value FROM system_config WHERE key = %s AND is_sensitive = true",
            [key],
        )
    except Exception:
        # During transient DB issues we should degrade gracefully so callers can
        # present a "connect PAT" prompt instead of crashing widget requests.
        return None
    if not row:
        return _MEMORY_PATS.get(user_id)
    try:
        payload = row.get("value") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        pat = payload.get("pat")
        return str(pat) if pat else _MEMORY_PATS.get(user_id)
    except Exception:
        return _MEMORY_PATS.get(user_id)


def delete_user_azure_devops_pat(user_id: str) -> None:
    """
    Remove a user's Azure DevOps PAT from the configured secrets backend.
    """
    if _use_vault():
        client = _vault_client()
        path = _vault_secret_path(user_id)
        try:
            client.secrets.kv.v2.delete_metadata_and_all_versions(path=path)
        except Exception:
            # Best-effort cleanup; failing to delete should not break the app path
            return
        return

    key = f"user:{user_id}:azure_devops_pat"
    try:
        query_one("DELETE FROM system_config WHERE key = %s RETURNING key", [key])
    except Exception:
        pass
    _MEMORY_PATS.pop(user_id, None)

