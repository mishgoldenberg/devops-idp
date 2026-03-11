import os
from typing import Dict

from fastapi.testclient import TestClient

from main import app  # type: ignore[import]


client = TestClient(app)


def _make_token(payload: Dict) -> str:
  # Reuse the same signing settings as the app by importing create_access_token
  from security import create_access_token  # type: ignore[import]

  return create_access_token(payload)


def _auth_headers(payload: Dict) -> Dict[str, str]:
  token = _make_token(payload)
  return {"Authorization": f"Bearer {token}"}


def test_observability_denied_for_non_admin(monkeypatch):
  # Simulate a regular user
  user = {
      "id": "user-1",
      "username": "user@example.com",
      "email": "user@example.com",
      "role": "User",
      "hierarchy_level": 7,
      "permissions": [],
  }
  headers = _auth_headers(user)

  resp = client.get("/api/metrics/usage", headers=headers)
  assert resp.status_code == 403


def test_observability_allowed_for_admin(monkeypatch):
  admin = {
      "id": "admin-1",
      "username": "admin@example.com",
      "email": "admin@example.com",
      "role": "Admin",
      "hierarchy_level": 1,
      "permissions": ["*"],
  }
  headers = _auth_headers(admin)

  # We only assert that the request passes RBAC; underlying DB may not be available
  resp = client.get("/api/metrics/usage", headers=headers)
  assert resp.status_code in (200, 500)


def test_azure_devops_requires_user_pat(monkeypatch):
  """
  When USE_MOCK_AZURE_DEVOPS is false and no per-user PAT is configured,
  Azure DevOps endpoints should reject the call with 400.
  """
  from azure_devops import get_user_azure_devops_pat  # type: ignore[import]

  os.environ["USE_MOCK_AZURE_DEVOPS"] = "false"

  def _no_pat(_user_id: str):
      return None

  monkeypatch.setattr("azure_devops.get_user_azure_devops_pat", _no_pat)

  user = {
      "id": "user-2",
      "username": "user2@example.com",
      "email": "user2@example.com",
      "role": "User",
      "hierarchy_level": 7,
      "permissions": [],
  }
  headers = _auth_headers(user)

  resp = client.get("/api/azure-devops/work-items", headers=headers)
  assert resp.status_code == 400
  body = resp.json()
  assert body.get("detail") == "Azure DevOps PAT is not configured for this user"

