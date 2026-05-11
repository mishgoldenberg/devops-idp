# DevBot Chat Assistant — Requirements (v1)

## 1. Purpose

Add an AI chat assistant ("DevBot") to the DevOps Hub portal so users can
ask natural-language questions about their Azure DevOps work without
clicking through every widget. v1 is deliberately small: in-portal,
Azure DevOps only, read-only.

The original design (`docs/chat-agent-architecture.md`, v1 draft) proposed
a standalone microservice covering Artifactory, Azure DevOps, SonarQube
and Confluence. That scope is deferred. The shipped v1 is what this
document describes, and what `docs/chat-agent-architecture.md` now
documents.

## 2. Definition of Done

A user logged into the portal can:

1. See a floating chat button in the bottom-right of every authenticated
   page **and** a "DevBot" item in the sidebar.
2. Open the chat (drawer or `/ui/chat`) and ask Azure DevOps questions in
   plain English (e.g. "What work items are assigned to me?", "List
   pipelines in project X", "Open PRs in repo Y of project X").
3. Receive an answer grounded in live Azure DevOps data, fetched through
   the same Azure DevOps base URL (`AZURE_DEVOPS_BASE_URL` env) and the
   same per-user PAT that the rest of the portal already uses.
4. Continue the same conversation on refresh (Redis-backed 24h session)
   and start a clean conversation via the "New conversation" control.
5. Get a clear "connect your PAT" prompt — not an LLM hallucination — if
   they ask Azure DevOps questions before pasting a PAT.

The feature must ship as part of the **existing** portal Helm release. No
new pod, no new Service, no new Secret beyond the LLM env vars.

## 3. In scope for v1

### 3.1 Backend

* New FastAPI router `backend/app/api/ai_chatbot.py` mounted at
  `/api/chat/*`, protected by the same `get_current_user` JWT dependency
  every other API uses.
* Endpoints:
  * `GET /api/chat/health` — `{llm_configured, ado_connected, ado_base_url_configured}`.
  * `GET /api/chat/connected-systems` — which integrations the user can use through chat.
  * `POST /api/chat/message` — send a user message, run the
    tool-calling loop, return the final assistant reply as JSON.
  * `GET /api/chat/sessions/{session_id}/history` — full user/assistant
    message array for the session.
  * `DELETE /api/chat/sessions/{session_id}` — clear the session in
    Redis.
* Orchestrator: standard OpenAI-compatible `/v1/chat/completions`
  function-calling loop, capped at 6 iterations, temperature 0.2.
* Tool registry mimicking [`mcp-azure-devops`](https://github.com/microsoft/azure-devops-mcp):
  * `ado_list_projects`
  * `ado_list_repositories`
  * `ado_list_work_items` (raw WIQL or structured filters incl. `@Me`)
  * `ado_get_work_item`
  * `ado_list_pipelines`
  * `ado_get_pipeline_runs`
  * `ado_list_pull_requests`
* All tools call Azure DevOps with `httpx.BasicAuth("", pat)` against
  `AZURE_DEVOPS_BASE_URL` and follow the existing
  `_discover_ado_bases` pattern from `api/azure_devops.py` so that
  cloud-root configurations keep working.
* Session storage in Redis under `chat:{user_id}:{session_id}`, TTL
  24h, hard-capped to the last 40 messages.
* Missing-PAT short-circuit: if the user has no PAT and the message
  mentions ADO keywords, reply with a deterministic "connect your PAT"
  message without calling the LLM.
* Stub mode: if `LLM_API_KEY` is empty (or the base URL has been
  blanked out) the endpoint loads but every reply is the "AI backend
  not yet configured" message — never 500s.

### 3.2 Frontend

* `partials/components/chat-drawer.html` — floating button + slide-in
  drawer, included from `partials/components/banner.html` so every
  authenticated page picks it up automatically.
* `chat.html` + `partials/components/chat-container.html` — full chat
  page at `/ui/chat`, with the same JS logic and session-id key
  (`sessionStorage.devbot.session_id`).
* New sidebar item "DevBot" linking to `/ui/chat`.
* No new frontend libraries (Tailwind/DaisyUI + plain JS only).
* Lightweight Markdown rendering on assistant replies (bold, italic,
  inline code, links, bullet lists, line breaks). User messages are
  HTML-escaped, never rendered as Markdown.
* Status line in both surfaces: "AI: ready / not configured · ADO:
  connected / not connected" — refreshed via `GET /api/chat/health`.

### 3.3 Configuration

New env vars (documented in `backend/.env.example`):

| Variable        | Default                    | Source in cluster                          | Description                                             |
|-----------------|----------------------------|--------------------------------------------|---------------------------------------------------------|
| `LLM_BASE_URL`  | `https://api.openai.com`   | `infra-config` ConfigMap (`global.llm.baseUrl`) | OpenAI-compatible `/v1/chat/completions` root.    |
| `LLM_MODEL`     | `gpt-4o-mini`              | `infra-config` ConfigMap (`global.llm.model`)   | Model name passed to the LLM.                     |
| `LLM_API_KEY`   | _(unset)_                  | `all-secrets` Secret (GitHub secret `LLM_API_KEY`) | Bearer token for the LLM call. When empty the agent stays in stub mode. |

The single thing the operator has to do at install time is set the
`LLM_API_KEY` GitHub Actions secret. The CI workflow pipes it into the
backend `all-secrets` Secret automatically; the base URL and model are
non-secrets and live in `deployment/values.yaml -> global.llm.*`.

#### Changing the LLM endpoint or model

The base URL and model are plain Helm values, not secrets. To switch
provider or model, edit `deployment/values.yaml`:

```yaml
global:
  llm:
    baseUrl: "https://api.openai.com"   # OpenAI default
    model:   "gpt-4o-mini"
```

Examples for other backends (any OpenAI-compatible server works):

```yaml
# Azure OpenAI (use the deployment URL, OPENAI key is still the bearer token)
global:
  llm:
    baseUrl: "https://my-resource.openai.azure.com/openai/deployments/my-deployment"
    model:   "gpt-4o-mini"

# Self-hosted vLLM / Ollama OpenAI shim inside the cluster
global:
  llm:
    baseUrl: "http://vllm.llm.svc.cluster.local:8000"
    model:   "qwen2.5-coder-32b-instruct"

# Anthropic via an OpenAI-compatible proxy (e.g. LiteLLM)
global:
  llm:
    baseUrl: "http://litellm.llm.svc.cluster.local:4000"
    model:   "claude-sonnet-4-5"
```

After committing the change to `main` the `Deploy (Helm only)` workflow
re-renders the chart and restarts the backend pods, picking up the new
`LLM_BASE_URL` / `LLM_MODEL` env vars. No code or image rebuild needed.

For a one-off override without touching the file (e.g. testing a model
in a side environment), pass `--set` flags to the helm upgrade step:

```
helm upgrade devops-stack ./deployment \
  --set global.llm.baseUrl=http://vllm.llm.svc.cluster.local:8000 \
  --set global.llm.model=qwen2.5-coder-32b-instruct
```

`LLM_API_KEY` stays in GitHub Actions secrets either way — rotating it
means updating the GH secret and rerunning the deploy workflow.

Re-used (already in the portal):

| Variable                | Used for                                              |
|-------------------------|-------------------------------------------------------|
| `AZURE_DEVOPS_BASE_URL` | Base URL for every ADO tool call.                     |
| Per-user PAT (DB/Vault) | Same `secrets_manager.get_user_azure_devops_pat` that the widget endpoints already call. The chat agent does **not** read `AZURE_DEVOPS_ADMIN_PAT`. |
| `JWT_SECRET`            | Standard portal JWT auth.                             |
| `REDIS_HOST`/`REDIS_PORT` | Session storage.                                    |

### 3.4 Deployment

* No new Deployment, Service, Ingress, Secret, ServiceAccount, RBAC
  rule, image, or Helm release.
* The portal image already exposes `/api/*` and `/ui/*`; the new routes
  ship inside it.
* The only operational change is setting the `LLM_API_KEY` GitHub
  Actions secret. `LLM_BASE_URL` and `LLM_MODEL` ship with OpenAI
  defaults in `deployment/values.yaml -> global.llm.*` — override them
  there to point at a different provider (see §3.3).
* Without `LLM_API_KEY` the UI is fully functional in stub mode.
* Closed-network constraint unchanged: outbound calls are limited to
  `AZURE_DEVOPS_BASE_URL` (already allowed) and `LLM_BASE_URL` (must
  point at a cluster-internal or otherwise-allowlisted gateway).

## 4. Out of scope for v1

* Write operations (creating work items, triggering pipelines, editing
  pages). The current toolset is read-only. If a write tool is added it
  must require an explicit user confirmation turn before execution.
* Artifactory, SonarQube, Confluence, ServiceNow tools. The registry is
  structured to make these straightforward additions, but they are not
  shipped in v1.
* SSE streaming. v1 returns the full assistant reply in a single JSON
  response. The UI already renders a "typing" placeholder so the user
  perceives progress. Streaming can be added later without breaking the
  contract.
* Standalone `chat-agent` deployment. Explicitly rejected — see the note
  at the top of `docs/chat-agent-architecture.md`.
* Per-user LLM choice / model picker.
* Persistent multi-day chat history (TTL is 24h by design).

## 5. Security and privacy

* Every chat endpoint goes through `get_current_user`. No anonymous
  access, no service-account bypass.
* The user's PAT is read on demand from `secrets_manager`; it is **never**
  returned to the browser, never logged, never persisted in the Redis
  session, and never echoed in tool results sent back to the LLM.
* The LLM receives only: the system prompt, the user's chat messages,
  prior assistant turns, and tool JSON responses. Tool responses are
  truncated to 8 KB per call to bound prompt size.
* Tool functions return `{"error": "..."}` rather than raising, so a
  single failed Azure DevOps call cannot crash the orchestrator or leak
  a stack trace into the browser.
* The chat UI escapes all user-visible strings before rendering and uses
  a narrow Markdown subset (no inline HTML) for assistant replies.

## 6. Observability

* The orchestrator logs (at `DEBUG`) every tool name and its wall-clock
  duration. Tool error responses log as `WARNING`.
* LLM failures (HTTP error, timeout) log the upstream status + first 200
  bytes of the body and surface a generic message to the user.
* Existing audit/observability machinery is untouched in v1; we can add
  a `chat_message_sent` audit event in a follow-up if product wants
  per-user usage metrics.

## 7. Acceptance test plan

A reviewer can verify v1 by:

1. **Stub mode** — deploy without the `LLM_API_KEY` GitHub secret set
   (defaults still leave `LLM_BASE_URL`=`https://api.openai.com`,
   `LLM_MODEL`=`gpt-4o-mini`). Log into the portal:
   1. Floating button appears on every page.
   2. Drawer opens; status reads `AI: not configured · ADO: not connected`.
   3. Sending a message returns the "not yet configured" reply within
      one second.
   4. `/ui/chat` page loads, renders the same status banner, and shows a
      visible alert ribbon explaining the stub state.
2. **PAT connected, LLM stub** — paste a valid PAT on the Azure DevOps
   page. Status now reads `AI: not configured · ADO: connected`. The
   chat still returns the stub message.
3. **End-to-end** — set the `LLM_API_KEY` GitHub Actions secret (and
   override `global.llm.baseUrl` / `global.llm.model` in
   `deployment/values.yaml` if not using OpenAI), trigger a deploy:
   1. "List my Azure DevOps projects" — assistant calls
      `ado_list_projects` and returns the live list.
   2. "What work items are assigned to me?" — assistant calls
      `ado_list_work_items` with `assigned_to="@Me"` and returns the
      list with clickable URLs.
   3. "Show the latest 5 runs of pipeline 42 in project X" — assistant
      calls `ado_get_pipeline_runs` and renders the runs.
   4. "Open PRs in repo Y of project X" — assistant calls
      `ado_list_pull_requests`.
   5. "Tell me about Confluence" — assistant explains that Confluence
      isn't supported yet (system prompt rule).
4. **Sessions** — send a message, refresh the page, open the drawer:
   the same conversation is visible (also visible on `/ui/chat`).
   Click "New conversation" — history is cleared, session-id rotated.
5. **Resilience** — stop Redis temporarily. Sending messages still
   returns answers (single-turn, no history) without 500s.
