# SonarQube integration

File: `backend/app/api/integrations.py` (with `sonar_insights.py`) · Prefix: `/api/integrations/sonarqube`

## Purpose

Real reads of a SonarQube server, for the six SonarQube dashboard widgets (Projects,
Quality Gates, New Code, Security Hotspots, Issues (yours), Gate on my pull requests),
the SonarQube page, and DevBot's SonarQube tools.

Each person's own token (Connections page, stored encrypted in `user_integrations`,
system `sonarqube`). Without one, the instance's **public** projects are still read,
and the page says it is only those.

## Main endpoints

| Method | Path | What it returns |
|---|---|---|
| GET | `/sonarqube/projects` | the projects the token can see |
| GET | `/sonarqube/overview` | every project with its quality gate and numbers, in one read (four widgets are views over it) |
| GET | `/sonarqube/project-details` | one project's conditions and measures, for an expanded row |
| GET | `/sonarqube/hotspots` | hotspots still to review on one project |
| GET | `/sonarqube/my-issues` | open issues on lines this person last touched |
| GET | `/sonarqube/pull-requests` · `/pull-request-details` | the quality gate on the pull requests this person opened |
| GET/POST/DELETE | `/sonarqube/pins` | pinned projects |

## External calls

SonarQube Web API (`/api/components/search_projects`, `/api/measures/*`,
`/api/qualitygates/project_status`, `/api/issues/search`, `/api/hotspots/search`),
cached per person in `integrations_cache` and answered stale while it refreshes.

## Environment

`SONARQUBE_BASE_URL`. `INTEGRATION_TLS_VERIFY` for an internal CA.
