"""A new version announces itself: the first pod to record it on an environment posts
"New in version X" on the billboard for two days, linking to What's New.

The database parts run against a real Postgres (skipped where none is reachable).

Run with:  cd backend && DATABASE_URL=postgresql://... python -m pytest tests -q
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("psycopg2", reason="backend deps not installed")

import changelog  # noqa: E402


def test_teaser_is_short_and_never_the_generic_line():
    for entry in changelog.all_releases():
        text = changelog.teaser(entry)
        assert len(text) <= 240
        assert changelog.GENERIC not in text
        assert text.startswith(entry["headline"])


@pytest.fixture()
def database(monkeypatch):
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is not set")
    import db

    try:
        db.query_one("SELECT 1 AS ok")
        db.ensure_announcements_tables()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"no database: {exc}")
    import release_notes

    release_notes.ensure_release_notes_table()
    env = "test-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("PORTAL_ENVIRONMENT", env)
    yield db, env
    db.execute("DELETE FROM release_notes WHERE environment = %s", [env])


def _announcements(db, version):
    return db.query_all(
        "SELECT *, (ends_at - starts_at) AS span FROM announcements WHERE title = %s",
        [f"New in version {version}"],
    )


def test_a_new_version_posts_one_announcement_for_two_days(database):
    db, _env = database
    import release_notes

    version = changelog.version()
    before = {row["id"] for row in _announcements(db, version)}
    try:
        assert release_notes.record_current_deploy() is not None
        # A restart, a second pod, a scale-up: already recorded, nothing posted.
        assert release_notes.record_current_deploy() is None

        new = [row for row in _announcements(db, version) if row["id"] not in before]
        assert len(new) == 1
        row = new[0]
        assert row["link_url"] == "/ui/changelog"
        assert row["is_active"] and row["kind"] == "success"
        assert row["span"].days == release_notes.ANNOUNCE_DAYS
        assert row["body"] == changelog.teaser(changelog.current())
    finally:
        for row in _announcements(db, version):
            if row["id"] not in before:
                db.execute("DELETE FROM announcements WHERE id = %s", [row["id"]])


def test_a_generic_only_release_posts_nothing(database):
    import release_notes

    entry = {"version": "9.9.9", "headline": "x", "features": [], "improvements": [],
             "fixes": [changelog.GENERIC]}
    assert release_notes.announce_release(entry) is None
