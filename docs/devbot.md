# DevBot and AdminBot: how the AI works

DevBot is the chat assistant built into the Hub (`/ui/devbot`). It answers questions
about a person's pipelines, work items, pull requests, code quality, artifacts,
Confluence pages and their own support tickets by reading those systems **with that
person's own tokens**, and it can look across several systems for one answer.
AdminBot (`/ui/adminbot`) is the same engine pointed at the Hub's own records, for
admins.

Code: `backend/app/devbot/` (the engine), `backend/app/api/devbot*.py` and
`api/adminbot.py` (the routes), `frontend/templates/partials/components/devbot-container.html`
(the page, shared by both bots) and `devbot-hover.html` (Ask DevBot in the widgets).

---

## 1. The pieces

```
 Browser ── POST /api/devbot/chat ──► orchestrator.py ──► llm.py ──► AI gateway (LiteLLM over vLLM)
   ▲          (answer streams back        │                          OpenAI-compatible /v1
   │           as server-sent events)     │
   │                                      ├──► tools/*  ──► Azure DevOps, SonarQube, Artifactory,
   │                                      │                 Confluence, support tickets
   │                                      │                 (each with the PERSON's token)
   │                                      │
   │                                      ├──► knowledge.py ── search by meaning (the index, §4)
   │                                      ├──► store.py      ── conversations, usage
   │                                      └──► monitor.py    ── admin figures, feedback
   │
   └── proposals (a change DevBot prepared) are reviewed by the person in the widgets'
       own dialog or the Hub's own form, confirmed in the big confirmation
       (action-confirm.html), and made in their name; DevBot never writes anything itself
```

| Module | What it does |
|---|---|
| `config.py` | Every DevBot setting (`DEVBOT_*`, see `docs/env.md`). DevBot is on when `DEVBOT_LLM_BASE_URL` is set. |
| `llm.py` | Talks to the gateway: lists models, reads limits, streams a chat answer, embeds text, one-shot `complete()` for background work. Turns every failure into an `LLMError` with a kind and a sentence a person can act on. |
| `models.py` | Which models a key may use, their context window, and whether each can call tools ("Live data"), learned from real answers and remembered. |
| `budget.py` | Plans each question to fit the key's per-minute limits and the model's context: how much history to send, how long the answer may be, how many rounds. |
| `prompts.py` | The system prompts (DevBot's and AdminBot's). Short on purpose: they are paid for on every request. |
| `orchestrator.py` | One question from start to finish (§3). |
| `tools/` | What the model may call (§2). |
| `knowledge.py` | The search index (§4). |
| `store.py` | Conversations and messages (`devbot_conversations`, `devbot_messages`, with a `bot` column keeping AdminBot's apart), and usage per model. |
| `monitor.py` | One row per question (numbers only, never the text) and the thumbs up/down feedback, for Admin → DevBot. |

### Keys

Each person uses **their own AI key**, entered on the DevBot page or on Connections and
stored encrypted like every other token (`user_integrations`, system `devbot`). The key
decides which models they may use and their requests and tokens per minute; the page
shows both, and where a limit comes from (the key, a model, a budget, the team).

The **Hub AI key** is separate: an admin sets it on Platform Managing → DevBot search
index. It builds and searches the index (§4) and it is the key AdminBot answers with,
so neither spends anybody's personal allowance.

---

## 2. Tools: what the model can call

A tool is a plain function over a `ToolContext` (the person, their tokens) that returns
a small JSON-able dict. A tool never raises: a system that is not connected, a refused
token or a bug comes back as `{"ok": false, "error": "..."}` in words, so one failed
lookup does not lose the others. Results are cut to each tool's `max_chars` before the
model sees them.

| Kind | Examples | Notes |
|---|---|---|
| **read** | `ado_pipeline_runs`, `ado_work_items`, `ado_pull_request_comments`, `sonar_quality_gate`, `art_docker_tags`, `conf_search`, `support_my_tickets`, `find_past_fixes` | One system, one question. |
| **investigate** | `investigate_pipeline_failure`, `investigate_test_failures`, `investigate_work_item`, `investigate_pull_request`, `investigate_project_health`, `investigate_artifact` | The whole chain runs in code, in one call: for a failed run, the failed step, the error lines from its log, Confluence pages with the fix (the best one read), past fixes, open bugs, the missing package in Artifactory or the failing SonarQube gate, and when it last passed. A small model would otherwise need five or six rounds, each against the per-minute limit. |
| **write** (`propose_*`) | re-run a pipeline, run one on a branch with parameters, cancel a run, open a bug, comment, move or assign a work item, vote on a pull request, write a Confluence page, draft a support ticket or a reply on one of the person's own, draft a self-service request (Artifactory quota or cleaner, a new pipeline) | Only **proposes**: the answer shows a card. Review opens the widgets' own dialog (`ado-actions.html`) filled with DevBot's draft, or -- for a ticket or a request -- the Hub's own form (`catalog-form.html`) filled in. The person checks and edits it, then confirms in **the big confirmation** (below); the change is made in their name and the outcome is written back onto the answer. |

**Which tools are offered** (`tools/base.py: specs_for`). Every tool description is
sent with every request and paid for, so a question gets at most 12 tools:
the systems it mentions (by words in English and Hebrew) and the systems the previous
answer used (so "and the tests?" keeps working), a system named outright first, a tool
whose own trigger words the question uses first of all, investigations when the
question is about a problem, writes only when it asks for a change. Only connected
systems count.

**Support tickets** are read through the same code as the Support page, with the Hub's
account, and a ticket is opened only if it is on the person's own list: a number typed
into the question is not proof it is theirs. A reply is drafted only on one of those,
and the reply endpoint checks it again (`_require_own_ticket`) before its check step and
before sending.

### Changes: the drafts, the confirmation, the caps

**What a draft may fill** (`tools/selfservice.py: prefill`). A ticket or request draft
names a form and answers for it. Only questions the form really has are kept; never a
file, never the requester's own details (the form remembers those, and a ticket carries
the identity provider's name), and a pick-list only with one of its own choices. What was
left out is told to the model, which says what the person still has to fill in. A
request still goes to the DevOps team's approval exactly like one typed by hand.

**The big confirmation** (`partials/components/action-confirm.html`,
`window.portalConfirm.ask`). Every change an assistant drafted ends here, after the
target system's own check: a coloured band (red for a harmful one: reject, cancel,
deactivate, hide), the change in large type, what will happen (the target system's own
check lines), "Drafted by DevBot/AdminBot -- it can be wrong", **"This is your
decision"**: it is made in the person's name, with their permissions, exactly as shown,
and DevOps Hub and its assistants carry out what is confirmed and are not responsible
for what follows from it. The button stays off until the person ticks "I have checked
it, and I take responsibility for this change". There is no click-outside-to-close,
and Escape does nothing while the change is on its way. A widget's own action, which
the person composed themselves, does not go through it.

**Caps** (`orchestrator.py`). At most **5** proposals in one answer and **30** in one
conversation; a proposal past either is refused to the model with the reason, never
silently dropped. A bulk card counts as one; an investigation's own suggested next
steps do not count. Write tools carry the words that name them (cancel, reply, quota,
open a ticket...), so the change a question asks for is offered first even where a
system has more tools than the 12 offered.

---

## 3. One question, start to finish

`orchestrator._turn`, streamed to the page as events (`start`, `notice`, `reasoning`,
`interim`, `step`, `substep`, `action`, `delta`, `done`, `error`) over one POST, with a
keep-alive every 10 seconds so a proxy never closes a quiet stream:

1. **Checks.** DevBot set up; the person has a key; the key may use the chosen model;
   at most 2 questions per person and `DEVBOT_MAX_STREAMS` per pod at once.
2. **Plan** (`budget.plan`). From the model's context window, the key's limits (live
   figures from the last answer's headers win over what the gateway describes) and
   `DEVBOT_MAX_*`: how much of the conversation fits, how long the answer may be, and
   how many rounds (`DEVBOT_MAX_TOOL_ROUNDS`, 4). Older messages that do not fit are
   left out, and the person is told.
3. **What the Hub already knows** (`grounding.py`). Before the model is asked, the
   question is looked up in the search index (§4): up to three Confluence pages that
   mean the same thing (only those the person can open) and up to three past fixes.
   What matches goes into the prompt as "what the Hub already knows", and the model
   answers from it first and cites it; when nothing matches it answers as usual. The
   person sees it as the first step ("Hub knowledge: Found 1 past fix"). Skipped for
   messages under 15 characters and when the index is empty; bounded to 20 seconds;
   one embedding request on the Hub's key.
4. **Rounds.** Each round is one request to the model with the tools offered. If it
   asks for tools, they run side by side on the thread pool, each reporting its
   progress live ("Reading the log of 'Build'"), and their compact results go back in
   the next round. **The last round is always sent without tools**, so a question
   ends in an answer, never in a lookup that ran out of room.
5. **When something goes wrong.** A per-minute limit that frees within 30 seconds is
   waited out once. A "too long" answer from the model teaches the Hub the model's
   real window and the round is retried smaller. A model that cannot call tools is
   remembered as "Chat only" and the question continues without them. If the model
   fails after lookups ran, the person still gets what was found.
6. **After the answer.** The answer is saved with its steps, sources, proposals, cost
   and duration. If an investigation found no Confluence page and no past fix, DevBot
   offers **Save this fix to Confluence** (a draft, in the first indexed space). The
   question is counted for Admin → DevBot (numbers only, never the text).

The lookups the model made are stored with the conversation exactly as it saw them, so
a follow-up question replays them instead of making them again. Every tool call gets
an id of its own, because some servers number calls afresh in every answer.

### Where DevBot can be asked from

- the DevBot page;
- **Explain** on a failed run in the Pipelines widget, **Ask DevBot** on an open work
  item;
- **Ask DevBot on hover**: rest the pointer on a row of a dashboard widget for 0.6
  seconds and the row lights up with an Ask DevBot chip on its edge
  (`devbot-hover.html`); from the keyboard, Tab onto the row and press Alt+A. A row
  takes part by carrying its question in `data-devbot-ask`, written with
  `window.portalDevbot.attr(question)`; Settings can switch the chip off.

All of these open `/ui/devbot?ask=<question>`, and the page asks it at once (the
`ask` is removed from the address, so a reload does not ask twice).

---

## 4. The search index (search by meaning)

### Why

Confluence's own search (CQL) finds a page only when it uses the words of the
question. A log says `NU1101: Unable to find package`; the page that fixes it may be
titled "Restoring from the internal feed" and be written in Hebrew. An **embedding
model** turns any text into a list of numbers (a vector) such that texts that mean the
same land close together, whatever their words or language. The index keeps a vector
for every passage of what the admin chose, and a question is answered by finding the
passages whose vectors are closest to the question's.

There is no vector database: Postgres here has no pgvector, and nothing new has to be
installed for it. Vectors are stored as bytes in ordinary tables and
compared in Python (below).

### What it holds

Three sources, each switched on and scoped on Platform Managing → **DevBot search index**:

| Source | What is read | With what |
|---|---|---|
| **Confluence pages** | every current page of the chosen spaces (by space key) | a read-only Confluence token set on the card |
| **Past fixes: work items** | closed work items of the chosen **types** in the chosen **collections** and **projects**, changed in the last N days (730 by default), that have a fix written | the Hub's admin PAT (`AZURE_DEVOPS_ADMIN_PAT`) |
| **Past fixes: tickets** | tickets of the chosen table (`incident`) that have resolution notes | the Hub's ServiceNow account (`SNOW_*`) |

The fields a work item's fix is written in are **found**, per collection, from the
process's field list, by their display NAME (custom fields are referenced by a GUID):
text fields named like *Solution*, *Resolution*, *פתרון* or *תיקון* first, then *Root
cause*, *Workaround* or *Corrective action*, and **System Info** last, since people
sometimes write the fix there. Fields that only look like one -- a resolved reason,
date or person, a *Found in* / *Fixed in* build or version -- are left out. For each
item the first filled field is taken; the card shows every field with how many items
have it filled, so a field that is never filled stands out. An admin can name the
fields outright ("Fix field": one or several, in order).

When no field is filled, the item's **discussion** is read instead (switchable on the
card): its last six comments, oldest first, at most 2,500 characters, and the review
takes the fix from what the discussion says was done. It is read only when the item
changed: an item whose discussion held no fix is remembered for 30 days at that
revision, and one indexed from its discussion is not read again until it changes.

The problem is read from the **Title**, the **Description** and the **Repro Steps**.

### How it is built

`knowledge.build()`, in a background thread, started by **Build now** or by the
schedule (checked hourly, due every `DEVBOT_INDEX_INTERVAL_HOURS`, 12). One build at a
time across all pods: a Redis lock that lives two minutes and that the building pod
renews every 30 seconds, with a heartbeat on the run's row. A pod that is killed (a
redeploy, an eviction) stops renewing it, so within two or three minutes the run shows
as **died** and a new build may start; a lock older than that with no heartbeat behind
it is cleared on sight. Each build writes its row in `devbot_index_runs` when it starts
and finishes it whatever the ending: "running", "done", "stopped", "failed" and "died"
are told apart. **Stop the build** ends a running one within seconds, whatever it is
doing (listing, reviewing, or waiting out a rate limit); what it did so far stays, and
the next build carries on from there. Stop on a build whose pod is already gone marks
it at once. While a build runs, the card says which phase it is in.

1. **Models.** The embedding model is the admin's choice, else `DEVBOT_EMBED_MODEL`,
   else a multilingual e5, else any embedding model the Hub key may use. Changing it
   empties the index: vectors of two models cannot be compared. When past fixes are
   on, a **review model** (a chat model) is chosen the same way: the admin's choice,
   else `DEVBOT_DEFAULT_MODEL`, else any.
2. **List every source on its own.** Each source yields its entries with a version (a
   Confluence page's version, a work item's revision, a ticket's update count). A
   source that cannot be read keeps its earlier entries untouched and says why on the
   card; entries that disappeared from a source that *was* read are removed; a
   switched-off source's entries are removed. A collection that does not exist is
   named on the card.
3. **Decide what to read.** Only entries that are new, whose version changed, that
   failed last time, or that are past fixes not yet reviewed. An unchanged index costs
   one listing per source and nothing else.
4. **Past fixes are reviewed first** (`knowledge.review_fix`). Resolution notes are
   written in a hurry, with typos, and some in words nobody should read out to the
   person asking. The review model gets the title, description and notes once and
   returns JSON: one neutral sentence of what the problem was, and the fix rewritten
   clearly and professionally with every command, name, path and version kept, blame,
   insults and people's names left out, in the note's own language. A note with no fix
   another engineer could apply ("fixed", "done", "works now", blame only) is **held
   back**: recorded so it is not reviewed again, never shown. So is a rewrite that
   names a command, path, flag, setting or version the original does not contain
   (`invented_details`): a model filling a gap with a plausible step is the one mistake
   nobody reading it would catch. Only the reviewed text is stored; the original note
   never leaves the build. The review also says **who can apply the fix**: *anyone*,
   or *the support team* when it needs rights on servers or infrastructure (restarting
   services on servers, freeing disk or adding storage there, permissions, admin
   consoles). A support-only fix is shown as how the support team fixed it, with the
   suggestion to open a support ticket, never as steps to follow; the card marks it
   "Support team only". Fixes reviewed before this was asked are asked on the next
   build, and stay shown meanwhile. An answer that is not the JSON
   asked for is asked once more, then treated as a failure: a note is never kept
   unreviewed.
5. **Cut into passages.** A Confluence page is cut along its paragraphs into passages of
   about 900 characters (at most 40 per page), each starting with the page title,
   because "Steps" means nothing without the page it is on. A past fix is embedded by
   its **problem only** (reference, title and description): people search with the
   error they see, and the fix is what is shown, not what is matched.
6. **Embed.** Passages go to the gateway in batches of 16 with the model's passage
   prefix (e5 was trained with `passage:` / `query:` prefixes, and without them a
   question lands among passages that merely look like it). A pacer reads the limits
   the gateway reports on each answer and waits before the minute runs out; a refused
   request (429, a server error, a timeout) is waited out and retried up to six times.
7. **Store.** Each vector is scaled to length 1 and kept as one signed byte per number
   (`BYTEA`, a quarter of the size of floats, same ranking to within noise) in
   `devbot_index_chunks`; the entry in `devbot_index_pages`. The entry's version is
   written **last**, so a build that dies half-way re-reads that entry next time. The
   index holds at most `DEVBOT_INDEX_MAX_CHUNKS` passages (40,000 ≈ 40 MB per pod).

### How it is searched

`knowledge.search(question)`:

1. The question is embedded with the query prefix, using the Hub key.
2. Each pod keeps every vector in memory, loaded once per build (a Redis generation
   number says when a new build finished).
3. Similarity is the dot product of the question's vector with every passage's (their
   cosine, since both have length 1): plain Python, well under a second for tens of
   thousands of passages.
4. Passages below `DEVBOT_INDEX_MIN_SCORE` (80 of 100; e5 scores even unrelated text
   around 70) are dropped; each page or past fix keeps its best passage.
5. Feedback nudges the order: a page behind answers people rated up gains a little
   (+0.015 per net vote, between −2 and +3 votes). It is added only to what already
   cleared the bar, so it never lifts an unrelated page into the results.

Where it is used: **every question** first (§3, what the Hub already knows); then
`conf_search` blends CQL results with meaning results, the investigations look for a
Confluence fix and past fixes, and `find_past_fixes` answers "has this happened
before?" with more of them. One question embeds each distinct search once.

### Who sees what

The index is built with service credentials, so it can hold things a given person may
not open.

- **Confluence pages** reach a person, or the model answering them, only after
  Confluence confirmed **with that person's own token** that they can read the page.
  The answer is remembered for 10 minutes; a page they cannot see is dropped as if it
  had never matched.
- **Past fixes** are shown to everyone, as the reviewed problem and fix: the fix is the
  useful part, and the person asking may not have access to the project it was written
  in. The link to the work item is added only for someone whose own Azure DevOps token
  can open it; a ticket never gets a link, since people here have no account to open it
  with.

### What it costs

On the Hub key: one embedding request per 16 passages, and one review per past fix,
**when an entry is new or changed**; nothing for an unchanged entry. Per question: one
embedding request per distinct search.

### The admin's view

**Review the past fixes**, on the card, lists every past fix exactly as people are
shown it, with tabs for Shown, Held back (and why), Hidden and Waiting for review, and a
search. **Hide** keeps one from everyone through every rebuild until it is shown again;
**Review again** takes it out of answers until the next build reviews it anew. The
original notes are not there, because they are never stored; **Open** shows the work
item.

The rest of Platform Managing → DevBot search index shows the settings (credentials only as "set"),
what is indexed per source, the reviewed / held back / waiting counts, per collection
which field the fixes were read from and how many items had none, the recent builds
with how many entries changed and why one failed, and the entries that could not be
read. `docs/RUNBOOK.md` §9a lists what each message means and what to do.

---

## 5. AdminBot

The same loop as DevBot (`orchestrator.stream_turn(..., bot="admin")`) with:

- the **Hub AI key** instead of the person's;
- the tools of system `hub` (`tools/hub.py`) instead of DevBot's: find a person, a
  person's full profile (role, activity, connections, requests, DevBot use, streak,
  recent warnings), a streak with the reason behind its number, requests and one
  request, the log, usage, DevBot's figures, and the Hub's health;
- proposals to **approve or reject a request** (or **several at once**, up to 20,
  each decided on its own so one that moved on does not stop the others, with the
  result per request), **deactivate or reactivate an account**, **change a role**,
  **post an announcement or take one down**, **start or stop the search index build**,
  and **hide, show or re-review a past fix** (read with `hub_announcements` and
  `hub_past_fixes`). Each opens the big confirmation, where the note to the requester
  can be changed and a refusal is shown in place; confirming calls
  `/api/adminbot/.../run`, which performs the proposal **as stored on the answer**
  (never what the page sends back) through the admin pages' own endpoint functions, so
  their guards apply: the last admin cannot be removed, an admin cannot deactivate
  themselves, a request someone already decided cannot be decided again.

Every AdminBot route, and every one of its tools, re-checks that the person is an admin
at that moment. `tools.run` refuses a `hub` tool outside an AdminBot conversation and a
DevBot tool inside one. AdminBot's conversations are stored apart (`bot = 'admin'`) and
are never reachable through DevBot's API. Its questions are recorded apart
(`devbot_events.bot = 'admin'`): the monitoring page counts them only in what the Hub's
AI key spent, beside the index builds' embedding requests and reviews, and they spend
no person's usage. Every confirmed change is also written to the log as
`admin.adminbot_action`, with what was proposed, beside the change's own event.

---

## 6. What is kept, and for how long

| What | Where | Kept |
|---|---|---|
| Conversations and their lookups | `devbot_conversations`, `devbot_messages` | `DEVBOT_HISTORY_DAYS` (30) after last use; the person can delete one at any time |
| Usage per model | `devbot_usage` | 90 days |
| One row per question: outcome, model, tokens, duration, which lookups ran and failed (never the question or the answer) | `devbot_events` | 2 years (the monitoring page's "All time") |
| Thumbs up/down with the question, the answer and the note, only when the person sent it | `devbot_feedback` | 180 days |
| The index | `devbot_index_*` | until the source changes or is switched off |

What is sent to the AI gateway: the question, the part of the conversation that fits,
and what the lookups returned. Tokens and keys never leave the Hub's server.

---

## 7. Setting it up

1. Set `DEVBOT_LLM_BASE_URL` (the gateway's OpenAI-compatible `/v1`) and
   `DEVBOT_DEFAULT_MODEL` in the pipeline's variables, and deploy.
   `scripts/check_llm_endpoint.py` shows what a key sees: models, limits, tool calling,
   embeddings.
2. People connect their own AI key on the DevBot page or on Connections.
3. For search by meaning and past fixes, an admin opens Platform Managing → DevBot
   search index, gives the spaces, a read-only Confluence token and the Hub AI key
   (with an embedding model and, for past fixes, a chat model), scopes the past fixes,
   presses **Check and save**, then **Build now**. Each setting is proven to work
   before it is kept.
4. AdminBot needs nothing more than the Hub AI key.
