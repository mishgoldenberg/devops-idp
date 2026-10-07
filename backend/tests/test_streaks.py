"""The streak's rules, without a database: what counts, what a freeze covers, and the
reason a streak ended, which the popover shows so a reset never reads as a bug.

Run with:  cd backend && python -m pytest tests -q
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("psycopg2", reason="backend deps not installed")

import streaks  # noqa: E402


def _days(*isos):
    return {date.fromisoformat(d): {"visit"} for d in isos}


def test_a_missed_workday_uses_a_freeze_and_the_streak_goes_on():
    # Thursday, (weekend), Sunday missed, Monday.
    got = streaks.compute(_days("2026-09-24", "2026-09-28"), date(2026, 9, 28), set())
    assert got["current"] == 2
    assert date(2026, 9, 27) in got["frozen"]
    assert got["ended"] is None


def test_with_the_months_freezes_used_a_missed_day_ends_it_and_says_so():
    # The 1st, then five missed workdays use September's five freezes, the 9th ends it;
    # a new streak on the 24th ends again on Sunday the 27th: the day the popover names.
    got = streaks.compute(_days("2026-09-01", "2026-09-24", "2026-09-28"), date(2026, 9, 28), set())
    assert got["current"] == 1
    assert got["ended"]["day"] == date(2026, 9, 27)
    assert got["ended"]["length"] == 1 and got["ended"]["freezes"] == 5
    assert got["ended"]["frozen_on"] == [date(2026, 9, d) for d in (2, 3, 6, 7, 8)]


def test_a_weekend_visit_adds_two_freezes_that_month():
    got = streaks.compute(_days("2026-09-01", "2026-09-24", "2026-09-28"), date(2026, 9, 28), {date(2026, 9, 1)})
    assert got["freezes"]["allowed"] == 7


def test_a_beat_with_nobody_at_the_page_does_not_save_the_day(monkeypatch):
    from api import usage

    seen = []
    monkeypatch.setattr(usage.usage_tracking, "record_beat", lambda *a, **k: None)
    monkeypatch.setattr(usage.streaks, "record_visit", lambda email: seen.append(email))
    me = {"email": "Me@Example.com"}
    # A restored tab, or a page that reloaded itself: it beats, but nobody touched it.
    usage.heartbeat(usage.BeatBody(page="home", active_seconds=0, view=True), current_user=me)
    assert seen == []
    usage.heartbeat(usage.BeatBody(page="home", active_seconds=40, human=True), current_user=me)
    assert seen == ["me@example.com"]
