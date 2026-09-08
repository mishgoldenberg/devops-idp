"""
The self-service request types, in one place.

This list is read by TWO layers that must agree:

  * api/approvals.py gates submission and execution on it -- a type not listed is
    refused rather than queued, and refused rather than reported as done;
  * db.py adds each one to the approval_request_type ENUM at startup.

They were separate before, and the failure that caused is worth keeping written
down: the application accepted ARTIFACTORY_CLEANER_CREATE, validated it, and handed
it to Postgres, which rejected it as "invalid input value for enum
approval_request_type". Every layer above the database was in agreement about a value
the database had never heard of, and the only symptom was a 500.

Adding a request type is one line here, plus an executor branch in api/approvals.py.
The database follows on the next start.
"""

from __future__ import annotations

from typing import FrozenSet

# Types the base schema created (deployment/charts/infrastructure/database/00_schema.sql).
# Listed so the sync is a superset check rather than an assumption about what is there.
BASE_REQUEST_TYPES: FrozenSet[str] = frozenset({
    "ADO_PROJECT_CREATE",
    "SONAR_PR_SCANNING_ENABLE",
    "AI_MODEL_ACCESS",
    "CUSTOM",
})

# Everything the portal can carry out today.
SUPPORTED_REQUEST_TYPES: FrozenSet[str] = frozenset({
    "ADO_PROJECT_CREATE",
    "ARTIFACTORY_QUOTA_INCREASE",
    "ARTIFACTORY_CLEANER_CREATE",
    "ARTIFACTORY_CLEANER_UPDATE",
    "ARTIFACTORY_CLEANER_DELETE",
})

# The cleaner request types, in one place: three things happen to a cleaner and every
# one of them is a change to the same folder in the same repository, so no two of them
# may be in flight at once.
CLEANER_REQUEST_TYPES: FrozenSet[str] = frozenset({
    "ARTIFACTORY_CLEANER_CREATE",
    "ARTIFACTORY_CLEANER_UPDATE",
    "ARTIFACTORY_CLEANER_DELETE",
})

# Every value the enum needs to hold: what the schema shipped with, plus what the
# application can now produce. Old values stay -- rows created under them are still
# rows, and dropping an enum value is not something a startup path should attempt.
ALL_REQUEST_TYPES: FrozenSet[str] = BASE_REQUEST_TYPES | SUPPORTED_REQUEST_TYPES
