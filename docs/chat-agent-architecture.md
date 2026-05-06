# DevBot — AI Chat Agent: Infrastructure Reference

## 1. Overview

DevBot is an AI chat agent embedded in the Hub. Users interact with it via a floating chat button (available on all pages) and a dedicated `/chat` page. It connects to four internal systems using the user's own stored tokens — so the agent only sees what the user is allowed to see.

The agent uses a self-hosted LLM exposed via an OpenAI-compatible API (`/v1/chat/completions`) with function/tool calling support. Configured via `LLM_BASE_URL` and `LLM_MODEL` env vars.

---

## 2. Architecture

Rather than deploying separate microservices for each system integration, all tool logic lives as Python modules inside a single `chat-agent` service. This keeps the deployment simple — one new service alongside the existing Hub — while maintaining clean code separation per system.

```
Hub Frontend (Jinja2/HTMX)
        │
        ▼
   chat-agent  (single FastAPI service)
        │
        ├── tools/artifactory.py  → Artifactory REST API  (on-prem VM)
        ├── tools/ado.py          → Azure DevOps REST API (on-prem VM)
        ├── tools/sonarqube.py    → SonarQube Web API     (on-prem VM)
        └── tools/confluence.py   → Confluence Server API (on-prem VM)
                │
          self-hosted LLM
         (/v1/chat/completions)
```

**What exists (Hub):** Postgres, Redis, RH SSO auth, existing token storage for Artifactory, ADO, SonarQube.

**What's new:** one `chat-agent` deployment + Confluence token field added to Hub.

---

## 3. Project Structure

```
chat-agent/
├── app/
│   ├── main.py              # FastAPI app, /chat endpoint, SSE streaming
│   ├── orchestrator.py      # LLM tool-calling loop
│   ├── session.py           # Redis conversation history
│   ├── tokens.py            # Load user tokens from Postgres
│   ├── warnings.py          # Missing token detection logic
│   └── tools/
│       ├── __init__.py      # Tool registry — maps tool names to functions + schemas
│       ├── artifactory.py   # All Artifactory tool functions
│       ├── ado.py           # All ADO tool functions
│       ├── sonarqube.py     # All SonarQube tool functions
│       └── confluence.py    # All Confluence tool functions
├── Dockerfile
└── openshift/
    ├── deployment.yaml
    └── service.yaml
```

---

## 4. User Token Model

Users store their own tokens for each system via the Hub dashboard. Fields in Postgres per user:

| Field | Description |
|---|---|
| `artifactory_token` | JFrog Artifactory API key |
| `artifactory_url` | Base URL of on-prem Artifactory |
| `ado_pat` | Azure DevOps Personal Access Token |
| `ado_url` | Base URL of on-prem ADO |
| `sonarqube_token` | SonarQube user token |
| `sonarqube_url` | Base URL of on-prem SonarQube |
| `confluence_token` | Confluence Server personal access token **(new)** |
| `confluence_url` | Base URL of on-prem Confluence **(new)** |

The `chat-agent` loads all tokens from Postgres on every request using the authenticated `user_id`. Tool functions receive the token and base URL as arguments — no credentials are stored in the agent itself.

---

## 5. Tool Modules

Each file in `tools/` is a plain Python module. Every tool function has the same signature:

```python
async def tool_name(args: dict, token: str, base_url: str) -> dict:
    ...
```

The `tools/__init__.py` maintains a registry mapping each tool name to its function and its JSON schema (used to build the `tools` array sent to the LLM).

### `tools/artifactory.py` — Read only

| Tool | Description |
|---|---|
| `list_repositories` | List repos visible to the user |
| `search_artifacts` | Search by name/pattern, optional repo filter |
| `get_artifact_info` | Metadata, checksums, properties for a specific artifact |
| `get_storage_info` | Overall storage usage summary with per-repo breakdown |
| `list_docker_tags` | Docker image tags in a given repo |

Auth: `X-JFrog-Art-Api: <token>` header.

### `tools/ado.py` — Read + Write

| Tool | Description |
|---|---|
| `list_projects` | All projects visible to the PAT |
| `list_work_items` | WIQL query with optional type, state, assignee filters |
| `get_work_item` | Full detail for a specific work item ID |
| `create_work_item` | Create a new work item (JSON Patch format) |
| `list_pipelines` | All pipeline definitions in a project |
| `get_pipeline_runs` | Recent runs for a specific pipeline |
| `trigger_pipeline` | Queue a pipeline run on a given branch |
| `list_pull_requests` | PRs in a repo filtered by status |
| `list_repositories` | Git repos in a project |

Auth: Basic auth with `base64(":<PAT>")`.

### `tools/sonarqube.py` — Read only

| Tool | Description |
|---|---|
| `list_projects` | All SonarQube projects visible to the token |
| `get_project_quality_gate` | Quality gate status and individual conditions |
| `get_issues` | Issues filtered by severity and/or type |
| `get_measures` | Code metrics (coverage, bugs, vulnerabilities, duplications) |

Auth: Basic auth with token as username, empty password.

### `tools/confluence.py` — Read + Write (Confluence Server pre-8.x)

| Tool | Description |
|---|---|
| `search_pages` | CQL full-text search across pages |
| `get_page` | Page content, space, and version number |
| `list_recent_pages` | Recently modified pages |
| `list_spaces` | All spaces visible to the token |
| `create_page` | Create a new page in a given space |
| `update_page` | Update page content (requires current version number) |

Auth: `Authorization: Bearer <token>`.

---

## 6. Orchestrator (`orchestrator.py`)

The tool-calling loop that runs on every chat message:

```
1. Build tools array from registry (only tools for connected systems)
2. Call LLM: POST /v1/chat/completions with messages + tools
3. If response contains tool_calls:
     a. Look up the function in the tool registry
     b. Call it with (args, token, base_url) for the relevant system
     c. Append tool result to messages
     d. Call LLM again with updated messages
     e. Repeat until LLM returns a plain text response
4. Stream final response via SSE
```

The orchestrator never calls external APIs directly — it always goes through the tool registry. This keeps the loop clean and system-agnostic.

---

## 7. Missing Token Logic (`warnings.py`)

Checked before every LLM call. Three tiers:

**Tier 1 — All tokens missing:**

Return immediately without calling the LLM:

> "You haven't connected any systems yet. Add your tokens on the Settings page to get started."

**Tier 2 — Some tokens missing:**

Inject a note into the system prompt listing unavailable systems. LLM can still answer for connected systems and will tell the user what to do if asked about a missing one.

**Tier 3 — Message targets a disconnected system by name:**

Return immediately without calling the LLM, with a targeted message:

> "Your Confluence account is not connected. Add your token in Settings to enable this."

Keyword matching for Tier 3:

| System | Keywords |
|---|---|
| Artifactory | `artifactory`, `jfrog`, `artifact`, `docker image`, `repository` |
| ADO | `azure devops`, `ado`, `pipeline`, `work item`, `pull request`, `sprint` |
| SonarQube | `sonarqube`, `sonar`, `quality gate`, `coverage`, `code smell` |
| Confluence | `confluence`, `wiki`, `page`, `space`, `documentation` |

---

## 8. Session Management (`session.py`)

- Conversation history stored in Redis under key `chat:{session_id}`
- TTL: 24 hours, refreshed on every message
- History format: standard OpenAI messages array (`role` + `content`), including tool call and tool result messages so the LLM has full context
- `session_id` is a UUID generated client-side, stored in browser `sessionStorage`
- New conversation = new UUID = fresh history

---

## 9. System Prompt

```
You are DevBot, an AI assistant built into DevOps Hub. You help developers
and clients get information about their DevOps systems: Artifactory,
Azure DevOps, SonarQube, and Confluence.

Always use tools to fetch live data — never guess or make up information.
Be concise. Format responses clearly — use bullet points or short tables
where it helps readability.
If a system is not connected, tell the user to add their token in Settings.
Always confirm with the user before taking any write action (creating work
items, triggering pipelines, creating or editing Confluence pages).

Today's date: {date}. User: {display_name}.
```

---

## 10. `chat-agent` API

### `POST /chat`

- **Header:** `Authorization: Bearer <Hub JWT>`
- **Body:** `{ "message": str, "session_id": str }`
- **Response:** `text/event-stream` (SSE)
- **Flow:** validate auth → load tokens → check missing tokens → run orchestrator → stream response → save history

### `GET /chat/sessions/{session_id}/history`

Returns full message history array for the session.

### `DELETE /chat/sessions/{session_id}`

Clears session from Redis.

### Environment Variables

| Variable | Description |
|---|---|
| `REDIS_URL` | Redis connection string |
| `DATABASE_URL` | Postgres connection string |
| `LLM_BASE_URL` | Base URL of self-hosted LLM (`/v1/chat/completions`) |
| `LLM_MODEL` | Model name to pass in LLM requests |

No MCP service URLs — all tool calls are in-process.

---

## 11. Hub Frontend Changes

### New Hub backend endpoint

`GET /api/user/connected-systems` — returns `{ artifactory: bool, ado: bool, sonarqube: bool, confluence: bool }` based on which token fields are non-null for the current user. Single DB read, no external calls.

### Confluence widget (new dashboard widget)

- Confluence token + URL input in Settings (same pattern as existing token widgets)
- Widget shows last 10 recently modified pages: title, space, modified date, link
- Inline CQL search via HTMX (no page reload)
- Missing token state: "Connect Confluence" prompt linking to Settings
- Backend routes on existing Hub backend (not `chat-agent`): `GET /api/confluence/recent` and `GET /api/confluence/search?q=...`

### Floating chat button

- Injected into base Jinja2 template — appears on every Hub page
- Opens a slide-in drawer with the full chat interface
- Status bar: 4 dots per system (green = connected, grey = missing token, links to Settings)
- Streaming rendered via `EventSource` SSE
- Session ID stored in `sessionStorage`
- Plain JS only, no new libraries, respects Hub light/dark theme

### Full `/chat` page

- Route: `GET /chat`, protected by existing Hub auth
- Two-column layout: sidebar (new conversation button) + scrollable message area with fixed input at bottom
- Same streaming and session logic as the drawer (shared JS functions)
- Linked from Hub sidebar navigation

---

## 12. Deployment

One new service: `chat-agent`

```
openshift/
  deployment.yaml   # chat-agent deployment
  service.yaml      # ClusterIP, internal only
Dockerfile          # vendored packages from offline-deps/python/
```

**Shared with existing Hub (no new infra needed):**

- **Postgres** — user tokens already stored here
- **Redis** — reuse for chat session history

**Offline constraint:** Dockerfile must install all Python dependencies from `offline-deps/python/` — no `pip install` from the internet.

**Image registry:** on-prem Artifactory Docker registry, same as existing Hub CI/CD pipeline.

---

## 13. Testing Strategy

Test the agent in isolation before integrating into the Hub.

### Phase 1 — Per-tool testing

Write simple Python scripts that call each tool function directly with a real token and base URL. Validate against real on-prem systems before any LLM is involved.

### Phase 2 — Full agent standalone

Run `chat-agent` locally with a `.env` file containing real tokens. Use a minimal `test-ui/index.html` (plain HTML + `fetch` + `EventSource`) to chat with the agent manually. No Hub, no OpenShift, no SSO required.

### Phase 3 — Hub integration

Wire the Hub JWT auth, swap hardcoded test tokens for DB lookups, deploy to OpenShift, add the frontend chat widget and `/chat` page.
