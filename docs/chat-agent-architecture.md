# DevBot — In-Portal AI Chat Assistant: Architecture Reference

> **Status update (2026-05-12).** The original design proposed a standalone
> `chat-agent` microservice with tools for Artifactory, Azure DevOps,
> SonarQube and Confluence. The shipped implementation is **in-portal,
> single-service, Azure DevOps only**. This document describes what
> actually lives in the codebase. The requirements that drove the
> rewrite are tracked in [chat-bot-requirements.md](chat-bot-requirements.md).

---

## 1. Overview

DevBot is an AI chat assistant embedded directly in the DevOps Hub. Users
interact with it via:

* A floating chat button in the bottom-right corner of every authenticated
  page (rendered through `banner.html`).
* A dedicated `/ui/chat` page reached from the sidebar.

It currently calls Azure DevOps tools only, using the existing per-user
PAT and the global `AZURE_DEVOPS_BASE_URL`. The behaviour and tool
surface intentionally mirror the public
[`mcp-azure-devops`](https://github.com/microsoft/azure-devops-mcp) server
so users get a familiar set of operations.

The agent uses an OpenAI-compatible LLM exposed via
`POST /v1/chat/completions` with function/tool calling.

---

## 2. Architecture

There is **no new service**. DevBot is a regular FastAPI router (`/api/chat/*`)
that lives in the existing portal image and shares the portal's Postgres,
Redis and Azure DevOps configuration.

```
Browser (Jinja2 + HTMX)
        │
        ▼
DevOps Hub backend (FastAPI, port 8000)
  ├── api/ai_chatbot.py         # /api/chat/* — orchestrator + tools + sessions
  ├── api/azure_devops.py       # existing widget endpoints (re-used)
  ├── secrets_manager.py        # existing per-user ADO PAT storage
  ├── redis_client.py           # existing Redis client (sessions)
  └── ui.py                     # /ui/chat page route
                │
                ▼
       OpenAI-compatible LLM
       (/v1/chat/completions)
                │
                ▼
       Azure DevOps REST API
       (per-user PAT, AZURE_DEVOPS_BASE_URL)
```

**Shared with the rest of the portal (no new infra needed):**

- **PostgreSQL** — already stores the per-user Azure DevOps PAT.
- **Redis** — used by `cache.py`; the chat module reuses the same client
  for session history.
- **JWT auth** — every chat endpoint depends on the standard
  `get_current_user` dependency, so the existing `auth_token` cookie/Bearer
  flow protects chat too.

---

## 3. Module layout

```
backend/app/
├── api/
│   └── ai_chatbot.py         # FastAPI router /api/chat/*
│                             #   - Tool registry (Azure DevOps)
│                             #   - Orchestrator (LLM tool-calling loop)
│                             #   - Redis session store
│                             #   - Missing-token guards
└── ui.py                     # GET /ui/chat → chat.html

frontend/templates/
├── chat.html                                # full DevBot page
└── partials/components/
    ├── chat-drawer.html                     # floating button + drawer (always rendered)
    └── chat-container.html                  # /ui/chat layout + JS
```

---

## 4. Credentials and configuration

| Source                    | Used for                                            |
|---------------------------|-----------------------------------------------------|
| `AZURE_DEVOPS_BASE_URL` (env) | Base URL passed to every Azure DevOps tool call |
| Per-user ADO PAT (DB/Vault)   | Authentication header on Azure DevOps API calls |
| `LLM_BASE_URL` (env)          | OpenAI-compatible chat completions endpoint     |
| `LLM_MODEL` (env)             | Model name (default `gpt-4o-mini`)              |
| `LLM_API_KEY` (env, optional) | `Authorization: Bearer …` for the LLM call      |
| `JWT_SECRET` (env)            | Same JWT auth as the rest of the portal         |

The PAT is read via `secrets_manager.get_user_azure_devops_pat(user_id)` —
the same helper the widget endpoints use. The agent never reads the
`AZURE_DEVOPS_ADMIN_PAT` (that one is reserved for self-service writes).

If `LLM_BASE_URL` is unset the agent loads cleanly but every reply is the
"AI backend is not yet configured" stub. This keeps the UI feature shippable
on environments without an LLM gateway.

---

## 5. Tool registry (Azure DevOps)

Each tool is a plain Python function with signature
`tool_*(args: dict, pat: str) -> dict`. The `_AZURE_DEVOPS_TOOLS` list pairs
each function with its OpenAI tool schema. Adding a tool means appending a
tuple — the orchestrator picks it up automatically.

| Tool name              | Description                                                       |
|------------------------|-------------------------------------------------------------------|
| `ado_list_projects`    | All projects accessible to the PAT                                |
| `ado_list_repositories`| Git repos in a project                                            |
| `ado_list_work_items`  | WIQL search (raw `wiql` arg or structured filters)                |
| `ado_get_work_item`    | Full detail for a single work item id                             |
| `ado_list_pipelines`   | Pipeline definitions in a project                                 |
| `ado_get_pipeline_runs`| Recent runs of a specific pipeline                                |
| `ado_list_pull_requests`| PRs in a repo, filtered by status (default `active`)             |

All tools return JSON-serialisable dicts. Failures are surfaced as
`{"error": "..."}` rather than raised so the LLM can summarise the error
to the user instead of the request crashing.

The shipped tools are **read-only** by design — see
[chat-bot-requirements.md §4](chat-bot-requirements.md#4-out-of-scope-for-v1).

---

## 6. Orchestrator

`_run_orchestrator(...)` runs the standard tool-calling loop:

```
1. messages = [system_prompt, *session_history, user_message]
2. for at most _MAX_TOOL_ITERATIONS (6):
     resp = POST {LLM_BASE_URL}/v1/chat/completions with messages + tool schemas
     if resp.choices[0].message has no tool_calls:
         return its content as the final reply
     for each tool_call:
         lookup function in registry
         call it with (args, pat)
         append {"role": "tool", ...} to messages
3. Persist [user_message, final_assistant_reply] to Redis history
```

Only the user/assistant turns are kept in the persisted history — the
intermediate tool plumbing is reconstructed via the system prompt on the
next turn. This keeps `GET /api/chat/sessions/{id}/history` clean enough
to render directly.

---

## 7. Sessions

* Storage key: `chat:{user_id}:{session_id}` in Redis.
* TTL: 24 hours, refreshed on every turn.
* Format: list of `{"role": "user"|"assistant", "content": str}`.
* `session_id` is a UUID generated in the browser and persisted in
  `sessionStorage` under the key `devbot.session_id`. Both the floating
  drawer and the `/ui/chat` page read from the same key so the same
  conversation is visible in both surfaces.

If Redis is unreachable the agent degrades gracefully: each turn is
stateless but still answered.

---

## 8. Missing-token guards

The orchestrator never calls the LLM if the user has no Azure DevOps PAT
**and** their question mentions Azure DevOps (`azure devops`, `ado`,
`pipeline`, `work item`, `pull request`, `sprint`, `wiql`, `repository`,
`branch`, `build`). Instead it returns a deterministic prompt directing
the user to the Azure DevOps page to paste their PAT. This saves an LLM
roundtrip on the most common cold-start failure mode.

If the user has no PAT but is asking a generic question (e.g. "what can
you do?"), the LLM is still called and is instructed by the system
prompt to ask the user to connect their PAT before attempting any tool
call.

---

## 9. System prompt

```
You are DevBot, an AI assistant built into DevOps Hub.
You help users investigate their Azure DevOps projects, work items, pipelines,
and pull requests by calling the provided tools.

Rules:
- Always call tools to fetch live data — never guess project names, work item
  ids, or pipeline results.
- Be concise. Use bullet points and short tables where they aid readability.
- Use Markdown for formatting; do not output HTML.
- Surface direct links (web_url, url) when the user might want to click through.
- If a tool returns an 'error' field, summarise the error to the user instead
  of retrying blindly.
- This deployment only supports Azure DevOps tools today. If the user asks
  about Artifactory, SonarQube, Confluence, or any other system, explain that
  these are coming soon and that you can only help with Azure DevOps for now.

User: {display_name}
```

---

## 10. HTTP API

All routes are mounted under `/api/chat` and require the standard
`auth_token` cookie or `Authorization: Bearer` JWT.

| Method | Path                                  | Description                                  |
|--------|---------------------------------------|----------------------------------------------|
| GET    | `/api/chat/health`                    | LLM-configured / ADO-connected flags         |
| GET    | `/api/chat/connected-systems`         | Which integrations are connected for the user |
| POST   | `/api/chat/message`                   | Send a message, get the final assistant reply |
| GET    | `/api/chat/sessions/{session_id}/history` | Full user/assistant message array       |
| DELETE | `/api/chat/sessions/{session_id}`     | Forget a session                              |

`POST /api/chat/message` request body:

```json
{ "message": "List my open work items", "session_id": "<uuid>" }
```

Response:

```json
{
  "success": true,
  "data": {
    "session_id": "<uuid>",
    "reply": "You have 3 open work items: …",
    "tools_available": true
  }
}
```

---

## 11. Frontend

* **Floating chat button** — rendered through
  `partials/components/chat-drawer.html`, included once from `banner.html`
  so every authenticated page shows it. The drawer opens a 26 × 34 rem
  panel anchored bottom-right, with a status line showing whether the LLM
  is configured and whether ADO is connected.
* **Full chat page** — `chat.html` + `partials/components/chat-container.html`,
  served from `GET /ui/chat`. Uses the same `sessionStorage` key, so the
  drawer and the full page share one conversation. Loads history on open.
* **Sidebar link** — `DevBot` item between Home and Systems.
* **Sessions** — UUID per browser tab, persisted in `sessionStorage`.
  "New conversation" calls `DELETE /api/chat/sessions/{id}` and rotates
  the UUID.
* No new frontend dependencies — same HTMX + plain JS as the rest of the
  portal.

---

## 12. Deployment

DevBot ships inside the existing portal image. No additional Helm
release, Docker image, ServiceAccount, Ingress or Secret is required.
Configuration is provided through the same `.env` / Helm values mechanism
as the rest of the portal — see `backend/.env.example` for the new
`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` keys.

The closed-network constraint is unchanged: the only outbound calls are
to `AZURE_DEVOPS_BASE_URL` (already whitelisted) and `LLM_BASE_URL`
(must be a cluster-internal or otherwise-allowed endpoint).

---

## 13. Testing strategy

1. **Tool functions in isolation** — each `tool_*` function can be invoked
   directly with a real PAT to verify the live Azure DevOps response
   shape before the LLM is involved.
2. **Stub mode** — leave `LLM_BASE_URL` unset and exercise the full UI to
   confirm health/status, session lifecycle, and the missing-token
   prompts render correctly.
3. **End-to-end with a small LLM** — point `LLM_BASE_URL` at any
   OpenAI-compatible server (vLLM, llama.cpp, Ollama with the OpenAI
   shim) and run the canonical prompts: "list my projects", "what work
   items am I working on?", "show recent runs of pipeline X in project
   Y", "what PRs are open in repo Z of project Y?".
