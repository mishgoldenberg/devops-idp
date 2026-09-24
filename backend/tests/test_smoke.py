"""Smoke tests.

Deliberately thin. These do not test business logic — they test the things that have
ACTUALLY broken this app, each of which shipped silently and was found by a user:

  * a router that doesn't import (a missing `import logging`, a bad relative import);
  * an endpoint function called directly from ui.py with an argument omitted, which
    receives FastAPI's ``Query(...)`` OBJECT instead of None and then blows up on
    ``.strip()``;
  * a widget's JS reading a key the endpoint never returns;
  * a route quietly disappearing from the app.

Run with:  cd backend && python -m pytest tests -q
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

fastapi = pytest.importorskip("fastapi", reason="backend deps not installed")


@pytest.fixture(scope="module")
def app():
    from main import create_app

    return create_app()


@pytest.fixture(scope="module")
def paths(app):
    return {getattr(r, "path", "") for r in app.routes}


def test_app_builds(app):
    """The whole app — every router — imports and mounts."""
    assert app is not None


@pytest.mark.parametrize(
    "path",
    [
        "/api/health/live",
        "/api/health/ready",
        "/api/search",
        "/api/integrations/health",
        "/api/azure-devops/work-items",
        "/api/azure-devops/iterations",
        "/api/azure-devops/area-paths",
        "/api/azure-devops/collections",
        "/api/azure-devops/pipelines",
        "/api/support/tickets",
        "/api/dashboard/snow-items",
        "/ui/search",
        "/ui/connections",
        "/ui/devbot",
        "/api/devbot/status",
        "/api/devbot/chat",
        "/api/devbot/conversations",
        "/api/devbot/conversations/{conversation_id}",
        "/api/devbot/models/check",
    ],
)
def test_route_exists(paths, path):
    """Routes the UI depends on are registered. A typo'd prefix is a 404 in production."""
    assert path in paths, f"{path} is not registered"


def test_query_defaults_are_not_leaked_to_direct_callers():
    """Endpoints that ui.py / search.py call DIRECTLY must tolerate omitted arguments.

    Calling an endpoint as a plain Python function does not run FastAPI's dependency
    machinery, so an omitted parameter arrives as the ``Query(...)`` object itself, not
    as None. That is exactly what produced "'Query' object has no attribute 'strip'".
    Any such function must coerce its inputs before using them.
    """
    from api.azure_devops import _query_str, get_work_items

    sig = inspect.signature(get_work_items)
    for name in ("project", "work_item_type", "iteration", "area_path"):
        default = sig.parameters[name].default
        # This is what a direct caller would pass in by omission...
        assert _query_str(default) is None, (
            f"{name}: a direct caller omitting this would leak a Query object"
        )
    # ...and a real value must survive untouched.
    assert _query_str("MyProject") == "MyProject"


def test_pipeline_status_mapping_covers_every_ado_state():
    """ADO splits a run's state across `status` and `result`, and `result` is only set
    once the run finishes — so neither field alone can tell you what happened. This is
    the mapping that made the widget show anything at all."""
    from api.azure_devops import _run_status

    assert _run_status({"status": "notStarted"}) == "pending"
    assert _run_status({"status": "inProgress"}) == "running"
    assert _run_status({"status": "completed", "result": "succeeded"}) == "succeeded"
    assert _run_status({"status": "completed", "result": "failed"}) == "failed"
    assert _run_status({"status": "completed", "result": "canceled"}) == "canceled"
    assert _run_status({"status": "completed", "result": "partiallySucceeded"}) == "partial"


def test_build_is_mine_matches_on_ado_identity_not_hub_username():
    """The pipelines widget was ALWAYS empty because it compared ADO's `requestedFor`
    (a domain account) with the HUB's username — two identity namespaces that never
    match. Ownership must be decided against the identity ADO itself reports."""
    from api.azure_devops import _is_mine

    me = {"id": "abc-123", "unique_name": "DOMAIN\\mish", "display_name": "Mish G"}
    mine = {"requestedFor": {"uniqueName": "DOMAIN\\mish", "displayName": "Mish G"}}
    theirs = {"requestedFor": {"uniqueName": "DOMAIN\\someone", "displayName": "Someone"}}

    assert _is_mine(mine, me, "mish@corp.example") is True
    assert _is_mine(theirs, me, "mish@corp.example") is False
    # Matching on e-mail alone must also work (identity lookup can fail).
    assert _is_mine(
        {"requestedFor": {"uniqueName": "mish@corp.example"}}, {}, "mish@corp.example"
    ) is True


def test_unread_needs_a_real_change_not_just_never_opened():
    """The dot means 'there is an update you haven't seen', not 'you never clicked this'.
    Treating never-opened as unread put a red dot on every row and made it meaningless."""
    from api.dashboards import _apply_user_flags

    fresh = {"id": "1", "created_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-01T10:00:00Z"}
    changed = {"id": "2", "created_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-02T09:00:00Z"}

    items = _apply_user_flags([fresh, changed], user_id="", source="servicenow")
    by_id = {i["id"]: i for i in items}

    assert by_id["1"]["has_update"] is False, "an untouched ticket must not wear a dot"
    assert by_id["2"]["has_update"] is True, "a ticket updated after creation is unread"


def test_search_groups_survive_a_dead_source():
    """A user with no SonarQube token, or an Artifactory that is down, must still get
    their tickets. One failing source may never sink the whole search."""
    from api.search import _collect

    def explode(_q, _user):
        raise RuntimeError("integration is down")

    group = _collect(("boom", "Boom", explode), "test", {"email": "a@b.c"})
    assert group["unavailable"] is True
    assert group["items"] == []
    assert group["total"] == 0
