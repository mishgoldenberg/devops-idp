"""The Support page's ticket endpoints reach ServiceNow with the Hub's service account,
which can open ANY ticket: each one must refuse a ticket that is not the caller's own.

Run with:  cd backend && python -m pytest tests -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("httpx", reason="backend deps not installed")

from fastapi import HTTPException  # noqa: E402

from api import servicenow  # noqa: E402

ME = {"id": "u1", "email": "me@example.com"}


@pytest.fixture
def mine(monkeypatch):
    calls = []

    def get_tickets(refresh=False, current_user=None):
        calls.append(refresh)
        return {"data": [{"sys_id": "abc123"}]}

    monkeypatch.setattr(servicenow, "get_tickets", get_tickets)
    return calls


def test_someone_elses_ticket_is_refused_before_anything_is_read(mine, monkeypatch):
    monkeypatch.setattr(servicenow, "_snow_client", lambda: pytest.fail("ServiceNow must not be called"))
    for call in (lambda: servicenow.get_ticket_detail("zzz999", current_user=ME),
                 lambda: servicenow.get_attachment("zzz999", "att1", current_user=ME),
                 lambda: servicenow.reply_to_ticket(sys_id="zzz999", message="hi", attachments=None, current_user=ME)):
        with pytest.raises(HTTPException) as err:
            call()
        assert err.value.status_code == 404
    # Refused only after the list was read again, uncached: a new ticket is not turned away.
    assert True in mine


def test_an_odd_id_is_refused_without_a_lookup(mine):
    with pytest.raises(HTTPException):
        servicenow._require_own_ticket("../incident", ME)
    assert mine == []


def test_ones_own_ticket_passes(mine):
    servicenow._require_own_ticket("abc123", ME)
    assert mine == [False]
