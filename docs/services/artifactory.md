# Artifactory integration

File: `backend/app/api/integrations.py` · Prefix: `/api/integrations/artifactory`

Self-service quota increases and cleaners live in `api/approvals.py` and
`catalog_forms.py`; see [approvals.md](./approvals.md).

## Purpose

Real reads of Artifactory for the Repositories and Storage widgets, the Artifactory
page and DevBot's Artifactory tools. Each person's own token (Connections page, stored
encrypted in `user_integrations`, system `artifactory`).

## Main endpoints

| Method | Path | What it returns |
|---|---|---|
| GET | `/artifactory/repos` | the repositories the token can see |
| GET | `/artifactory/repo-details` | a repository's latest artifacts, for an expanded row |
| GET | `/artifactory/storage` | storage per repository and the filestore (admin only: `/api/storageinfo` needs an admin token) |
| GET/POST/DELETE | `/artifactory/pins` | pinned repositories |

## External calls

Artifactory REST (`/api/repositories`, `/api/storage/*`, `/api/storageinfo`) and AQL
(`/api/search/aql`), cached per person in `integrations_cache`. A logical size
(summed repository usage) and the physical filestore are never put on one axis:
Artifactory de-duplicates.

## Environment

`ARTIFACTORY_BASE_URL`. `INTEGRATION_TLS_VERIFY` for an internal CA.
