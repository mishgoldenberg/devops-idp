# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Self-improvement protocol
- After any correction from me, propose a concise rule and append it to the appropriate section of this CLAUDE.md.
- Rule format: one imperative sentence, no rationale, no examples — unless the case is ambiguous.
- Before adding a rule, search this file. If an existing rule already covers the case, refine it instead of duplicating.
- Keep the total size of CLAUDE.md within 10000 tokens. If exceeded, merge overlapping rules and remove outdated ones.
- After fixing a bug, record a rule about the root-cause class, not the specific symptom.

## Environment facts

Properties of THIS deployment, not advice. Each cost real time to discover; check here
before designing against an assumption.

- **Azure DevOps Graph API is not routed** (`_apis/graph/*` → 404, `_apis/resourceAreas` empty). Do identity reads and group writes through the legacy `/_api/_identity/` endpoints the web UI uses — `ReadScopedApplicationGroupsJson`, `ReadGroupMembers`, `AddIdentities` (`newUsersJson`/`existingUsersJson`/`groupsToJoinJson`, retry once with a scraped `__RequestVerificationToken`) — and write ACLs via `_apis/accesscontrollists` with an `acesDictionary`, not `_apis/accesscontrolentries`. Check any other ADO endpoint is routed before building on it.
- **`AddIdentities` is the whole grant**: it resolves `DOMAIN\user` against AD, binds the account into the collection and joins the group in one call. Existing in AD is not existing in the collection.
- **Project Administrator ≠ process Administer.** Editing an inherited process needs an ACL on `$PROCESS:<typeId>:`, never the bare `$PROCESS` root. ADO also auto-adds a project's creator to its Project Administrators group, so the admin PAT's owner always looks provisioned.
- **`output.css` ships precompiled and is never rebuilt.** A class not already in it does nothing. Change the palette by redeclaring DaisyUI's HSL properties in `theme.css` (loaded after it). `menu-compact`, `checkbox-sm`, `kbd-sm`, `file-input-sm` are not compiled in.
- **CI is `azure-pipelines.yml`** (ADO agent → OpenShift via `oc`). `.github/workflows/` never runs. The agent has no npm/node, so every step is pure Python. `node` exists locally only.
- **`POSTGRES_IMAGE` may be Bitnami or official and they differ.** `bitnami/postgresql` has no curl and no wget, and an offline install has no package mirror. The postgres pod carries `POSTGRESQL_PASSWORD`; `pg_restore` reads `PGPASSWORD`.
- **ServiceNow catalog items nest their real questions inside containers.** The item API returns a tree; the input variables sit under `children` of a Container Start, so reading only the top level finds boxes and no questions. Flatten before mapping — `snow_catalog._flatten`, matching the Support producer path.
- **htmx 1.9 rescans with the selector `[hx-trigger='revealed']`**, so `revealed` in a compound trigger never fires again — use `intersect once`.
- **The portal scrolls `.page-content`**, so `window.scrollY` is permanently 0. `.btn` carries an unconditional `animation: button-pop`. `--nc` is LIGHT in the light themes.
- **One catalogue per list:** widgets in `widget_registry.py`, self-service forms in `catalog_forms.py`. Import them; never copy.
- **Artifactory cleaners run on a DIFFERENT OpenShift cluster**, behind a firewall the backend cannot cross. Their CronJob/ConfigMap exist only after the pipeline's FIRST BUILD runs, so the merge queues one (`ado_pipeline.run`). Merging a removal deletes the files and the pipeline; the CronJob (`<slug>-cleaner`) and ConfigMap (`<slug>-cleaner-spec`) in `devops-monitor` keep running. Hand over console links (`OPENSHIFT_CONSOLE_URL`) and an admin confirmation — never a pipeline that deletes across the firewall.
- **The changelog is `backend/app/changelog.py`**, written by hand. CI stamps `build_info.json` (branch/build/commit) and tags images `<version>-test` / `<version>-prod`; the backend records only WHEN a version reached an environment.
- **`scripts/apply_docx.py`** applies a round, runs the checks, then commits with `git add -A` and pushes. A failing check refuses the commit; re-running after the fix commits the already-applied tree. `--no-commit` / `--no-push` / `--no-verify` opt out. Because it stages everything, `*.docx` must stay in `.gitignore` — otherwise every round pushes its own delivery document into the repository.

## Code rules

### Prove it happened
- Treat a 200 as failure when the body says so (`HasErrors`) or is HTML rather than JSON.
- Record a probe's HTTP status, not just its result count — "0 rows" and "401" are different problems.
- Make every best-effort side effect record its per-item outcome, log failures at WARNING with the server's own response, and read the write back -- "we sent it" was already true when the field was still wrong.
- Name what each write was FOR before deleting a class of them; the one that says who a record belongs to is not interchangeable with the ones that set its state, and it disappears without an error.
- Grant a pipeline its variable group when creating it: an unauthorised one does not fail, it PAUSES on its first run waiting for a human nobody has told.
- Verify a grant against the REQUESTED subject, never against "someone has access".
- A definition that DESCRIBES a resource has not created it: queue a run at the merge and treat that run's success as the only evidence the resource exists.
- Hand an operator a link only to something that exists; a link to a resource never created teaches people to confirm without looking.
- Keep the record that NAMES an external leftover until somebody confirms the leftover is gone; deleting it at the step that looks finished destroys the only thing that could have said otherwise.
- Record an unverifiable claim against the name of whoever made it, and never let the person who wanted the thing gone be the one who certifies it went.
- Give an operator a read-only UI diagnostic for anything whose failure is invisible, bounded by age AND by whether the failing code path still exists; an unbounded one keeps accusing after the fix.
- Treat "already exists" from a create as the answer, not a failure -- then confirm the thing that exists is the one you wanted before adopting it.
- Classify an outbound failure before showing it (`resilient_http.explain_integration_failure`); refused, DNS, TLS, 401, 404 and timeout need different actions.
- Put the HTTP STATUS and the account in the message the user sees, never the exception class: 401, 403 and 404 are three different people's jobs and `HTTPStatusError` names none of them.
- Add a new value to the `approval_request_type` ENUM in `db.py` from `request_types.py` before the application can emit it; a gate that says supported while Postgres rejects the value is a 500 with nothing pointing at the schema.
- Keep any list two layers must agree on (request types, widget catalogue) in one module both import.
- Send a PASSWORD only as Basic; only a token or API key may climb the bearer/API-key ladder.
- Prefer the credential that reaches the MOST services when several are configured, or the advice to add one changes nothing.
- Probe JFrog Access (`/access`) separately from Artifactory: one accepting the credential says nothing about the other.
- Catch a route's own failures and answer with the reason; reaching the app's global handler turns every one of them into "Something went wrong".
- Degrade a picker to the part that still works (quotas without usage) rather than emptying it when one admin-only probe is refused.
- Never end a deploy at `oc apply` — verify the live Deployment carries the new tag, `rollout status` completes, and the public hostname reports that build.
- Print a failing pod's own diagnosis (events, readiness payload, last logs), never just that it failed.
- Distinguish "no pod was created" (read the Deployment's `ReplicaFailure` and the ReplicaSet's events) from "the pod is not Ready" (read the pod).
- Never let a step that only RECORDS finished work raise; catch it, name it, and carry the reason in the result.
- Refresh an external state inside the function every page READS it through, never on the assumption that another page refreshed it first.
- Read a page's worth of external state in ONE call keyed on the thing, not one call per row keyed on the viewer.
- Surface a stored failure reason (`snow_error`) somewhere an admin looks; a column nothing renders is not a record.
- Hand back what a mapping DROPPED, not just log it: an answer that reached no field is invisible, because the target keeps its own default and that reads like a choice.
- Treat a not-editable error (TF401181) as proof the object already moved; read its real state and record THAT.
- Define every name before referencing it — a bare global as much as a `module.Class.CONSTANT`; `check_imports.py` resolves both, and a missing one raises where it is USED, after the work it names is done.
- When a rewrite replaces a mechanism, carry over every name the old one DEFINED, not only the lines that read them: a surviving reader with a deleted definer imports, starts and serves everything else.
- A guard that reads a file as TEXT has not checked that it PARSES; compile every artefact the runtime compiles.
- Render a UI change headlessly and look at it before calling it fixed — after the LAST edit to it, not after the last interesting one.
- Create the pipeline that executes a merged YAML file AT the merge, and record the name and path to build by hand when it cannot be created.

### Identity and provisioning
- Match ADO identities via normalised account forms, never raw string equality.
- Try every form of a principal (`DOMAIN\user`, UPN, bare account) against every filter, and pick by exact account — never `rows[0]`.
- Skip status 400 alongside 401/403/404 in a per-collection probe.
- Discover a security token by reading existing ACL tokens and substituting your object's GUID; never hardcode one.
- Provision into the ONE target the request names, and validate that target at submission, not only in the executor.
- Derive a catalog from live systems, never a hand-maintained list, reading ownership through the endpoints provisioning writes to.
- Never write generated metadata onto a created resource; take the text as an optional input.

### Auth and access control
- Enforce account state where the token is READ, not where it is issued.
- Fail open on an unreachable database, closed on a definite negative answer.
- Filter restricted rows in the endpoint, never in the template or browser, and authenticate any endpoint that becomes able to return them.
- Decide reviewer-only data by the ROLE THE PAGE IS IN (scope=all), not by whether the account is an admin.
- Re-check every side-effect guard (Safe Mode, authorization) inside the function that performs the effect.
- Keep privilege levels equal to the number the code tests; delete any permission store nothing reads.
- Name the function a guard calls in its docstring; never restate the threshold.
- Rate-limit local sign-in per account AND per `X-Forwarded-For`, and verify unknown usernames against a decoy hash.
- Never take a secret in a URL query parameter or echo a decoded token payload.
- Derive at-rest keys from a shared signing secret through HKDF with a distinct info label.
- Treat a data-encryption secret as part of the data: back it up with the dump, never rotate it casually, and self-check at startup that it still decrypts.
- Write CI intermediate secret files 0600 and delete them via `trap ... EXIT`.
- Detect an expired session centrally (401, or a fetch that landed on the sign-in page); never on 403.
- Have every layer consume a resolved identity/ownership decision through ONE helper, including every save handler, and normalise its case there — a key written lowercased and read mixed-case is a row that exists and cannot be found.
- Identify a field in another system by what it POINTS AT before what it is called; a name matcher finds nothing against labels written in another language.

### Proxy, hostnames, TLS
- Decide a cookie's `Secure` flag from `X-Forwarded-Proto`, never `request.url.scheme`, via one shared helper.
- Pass `X-Forwarded-Proto` through at every intermediate proxy (`map $http_x_forwarded_proto`); never re-derive it from `$scheme`.
- Confirm a config file is the copy that RUNS — nginx exists as `frontend/nginx.conf` in the image AND as `charts/frontend/templates/nginx-config.yaml`, mounted over it. `scripts/check_nginx_sync.py` enforces both.
- Set security headers at nginx with `always` so they survive error responses.
- Keep HSTS `max-age` short until the certificate has been renewed once — while remembered, a cert error is non-bypassable.
- Give an alias hostname a rule on EVERY Ingress the origin uses (`/` and `/api`) and a SAN entry; the browser sends the host it was TYPED.
- Treat an alias hostname as a second ORIGIN: a cookie `Domain` must be a suffix of the request host, so sign-in, sign-out and every OIDC callback are per-host. This portal deliberately runs two origins.

### Deploy and pipeline
- Stage with `git add -A`, never `git commit -am` — a missing router file is a CrashLoopBackOff.
- Ship the whole dependency closure, not just the new file.
- Never import a local module lazily inside `try/except`; it turns a delivery error into dead code.
- Map every secret explicitly under its step's `env:`, and re-check which step owns that block after inserting any step.
- Treat an unexpanded macro (`$(NAME)`) as unset, and never name a variable an environment will never define.
- Fail the build on an empty required secret at both ends — the assembling script and the chart (`required`, not `default ""`).
- Deploy only from a real branch ref (`refs/heads/*`); a PR build is `refs/pull/N/merge`.
- Never pipe a numeric chart value through Helm's `default` — 0 is empty.
- Size a rolling update against the namespace quota: `maxSurge: 1` needs headroom for one pod more than the replica count.
- Never resize a StatefulSet volume by editing the chart; `volumeClaimTemplates` is immutable, so expand the live PVC by name.
- Write generated infrastructure to a new branch and a pull request, never to the default branch.
- Give a router path relative to its `include_router(prefix=…)` mount.

### Startup and resilience
- Never do dependency work in a startup hook; bootstrap in a background thread and let readiness report the pending state.
- Keep liveness dependency-free; put dependency checks behind readiness, cached with a short TTL and skipped when one is in flight.
- Set an explicit connect timeout AND retry policy on every client — a 2s socket timeout measured 25s under default retries.
- After a failed pool construction, fail fast for a cooldown rather than queueing behind the next attempt.
- Give every container a `startupProbe`, and pair `readOnlyRootFilesystem` with a writable `/tmp` emptyDir.
- Cap every cache's memory, set an eviction policy, and turn persistence off.
- Never cache a FAILED external read: it holds the failure for the whole TTL and makes pressing Refresh the one thing that cannot help.
- Let an explicit Refresh bypass the read cache and a timed poll keep it; a button answered from cache is a button that does nothing, and a poll that skips every cache is not a poll.
- Fall back to a direct single read for a row the batched listing did not answer for; absent and unreadable both freeze a state otherwise.
- Bound every layer that can stall — `htmx.config.timeout` AND a fetch deadline — and render an error for a failed shell request.
- Never let `text=True` pick a subprocess's encoding: pass `encoding="utf-8", errors="replace"` and give children `PYTHONIOENCODING=utf-8`; treat captured streams as possibly `None`.

### Data and schema
- Guard `execute_returning(...)` with an emptiness check before indexing; raise 500 if empty.
- Make a validator accept its own OUTPUT: it runs at submission and again at execution, and `str(['dump'])` becomes a rule matching nothing.
- Catch and re-raise `HTTPException` before any broad `except Exception`.
- Verify a called function is in the module's imports.
- Never `bool()` a JSON flag: this API answers with the string `"false"`, and `bool("false")` is True.
- URL-encode every value put into a query string; a name with spaces matches nothing and reads as absent.
- Never compare a Python-rendered value with a Postgres-rendered one (`str(datetime)` ends `+00:00`, `::text` ends `+00`); compute both sides in SQL.
- Namespace ids when one endpoint merges two tables — two SERIALs collide at 1.
- Give a personal copy of a shared feature its own table, never a nullable owner column.
- Default a new visibility/permission column to the PERMISSIVE value.
- Version a stored user preference before adding an item to the list it records, or when a field it stores changes type or allowed values.
- Stash a failure's reason on `request.state` so the audit row says why, not just "502".
- Collapse repeated identical failures from a polling client into one row per window with a count.
- Bound every admin list: filter, fixed-height scroll container, render cap.

### Backups and scheduled jobs
- Enable a scheduled job from the presence of its credential, never a standalone flag, and refuse to render it when settings are blank.
- Give a scheduled job a row written at START and updated at exit under a trap, so "never ran", "running" and "died" stay distinguishable.
- Record a backup's checksum and a salted fingerprint of the secret that decrypts it — never the secret — and prove the pairing before restoring.
- Verify restores on a schedule into a scratch database, guarding its name three ways because the job runs DROP DATABASE.
- Never restate an automated job as an operator procedure: derive it from the job, ship a script rather than prose, and EXECUTE the generated block before shipping it.
- Assume nothing about the operator's shell — a trailing backslash is not a continuation in cmd, and `$VAR` does not expand.
- Split a job across the images that already hold its tools, hand off through an `emptyDir`, and let a phase that cannot report exit 0 and write its outcome to a file.
- Express retention as a COUNT over a listing of what is stored, and prune in the job that creates the artifact, not the pipeline.
- Exclude from any retention sweep the rows that are a LIVE resource's only handle, and count rather than delete anything still undecided.
- Reuse the existing endpoint variable when adding a second consumer of the same server.

### Self-service and forms
- Never mark a request COMPLETED unless a real executor did the work; keep executable types in one set and fail loudly for anything else.
- Check a name that becomes an IDENTIFIER for collision at submission, against stored records AND requests still in flight; the second one is a valid "add" until the first is merged.
- Delete a thing whose files live in a repository through a pull request, never by dropping its row — and treat merging that removal as the opposite of merging a change.
- Re-read a dependent picker from the server when its parent changes; the server knows which children belong to it and when none do.
- Load a dependent picker's list up front when its parent is OPTIONAL; otherwise leaving the parent blank leaves the child with no list at all.
- Never leave a fabricated-data endpoint mounted beside the real one, or gate mock data behind an always-false constant.
- Describe a form as data in `catalog_forms.py` and render it with the one renderer, `catalog-form.html`.
- Apply declared defaults on the SERVER before validating or storing.
- Reproduce a replaced form's REQUEST BODY exactly — every field, hidden ones included, unset as `""` — or the endpoint's mandatory-field autofill supplies its own defaults.
- Never send a file field's name as a scalar; a parameter typed as an upload gets a file or nothing.
- Keep client-side validation no stricter than the backend contract.
- Check a precondition while the form is being filled, not after approval.
- Apply a relative change to the value read at EXECUTION time, never the snapshot from when the form was filled.
- Order a ServiceNow catalog item through `order_now` (a REQ with a RITM), never `submit_producer` (an INC), and refuse a result whose table is `incident`.
- Check an external system's MANDATORY fields before sending: fill what the item itself can answer (its default, its first choice), log every value filled, and refuse only what names another record.
- Match a ServiceNow variable by name, by label, by the `u_`-stripped name AND by its WORDS, and give the operator an env-var map for what is left — a Hebrew label matches nothing and an apostrophe is one character between `pipeline_purpose` and `Pipeline's Purpose`.
- Say who a record is FOR when the integration account raises it, AT INSERT — the item's own reference variable, or `sysparm_requested_for` on the submit — never by a later update, and report the field as not set rather than correcting it afterwards.
- Treat an update as an EVENT the far system reacts to, not a way to set a value: where a rule fires on update, one PATCH costs the record its state and its assignee no matter how little it wrote.
- Restore a field a rule moved to the literal value READ before the write, never to a name resolved from a choice list — this account cannot read `sys_choice.value`, and the pre-write read already holds the code.
- Let a form declare where its own result lives and what that place is called; a result screen that infers its wording from the response shape hands one form another form's outcome.
- Never forward a payload as "scalars only": the nested dict you drop is the requester's details, and they are what the item makes mandatory.
- Show a requester only their own scope and an approver the aggregate; each quota looks reasonable alone and the sum is what overruns.
- Split an over-limit warning into ALREADY over and THIS ONE crosses it, and show real usage beside the promise; blaming a 10 GB request for a 5 TB overage argues for rejecting the wrong thing.
- Never put a LOGICAL size and a PHYSICAL one on one axis: Artifactory de-duplicates, so summed repository usage legitimately exceeds the disk (`fileStoreSummary` is the physical number).
- Never narrow a live-loaded picker to nothing: fall back to the whole list with the reason, and let the key be typed.
- Never render a picker with nothing in it; show the typing box and the reason instead of a dropdown that says there is nothing to pick.
- Copy an existing section field for field -- keys, labels, order, widgets -- and add a guard that fails when the two copies drift.
- Leave every state of a record with at least one available action; two individually correct refusals that overlap are a dead end.
- Decide in the form's own spec which requests reach an external system, and report "no ticket here" as a skip, never as a failure.
- Resolve membership through GROUPS as well as direct members.
- Track an asynchronous outcome (a pull request) on its own record refreshed on read, never overwrite a stored state with a failed lookup, and say which of merged, abandoned and waiting it is.
- Undo a side effect in BOTH directions: if approving a removal deletes something, abandoning that removal has to put it back.
- Decide add-versus-edit from that state, not from the form.

- Refuse a delete rule set that selects everything: require at least one of age, name, type or keep-newest-N.
- Express keep-the-newest-N as the file spec's `sortBy`/`sortOrder`/`offset`, never as an AQL condition.
- Keep the slug that names generated files in the stored record; never recompute it from an editable name.
- Raise the external ticket where the outcome is known, not at submission, so it carries the result instead of the request.
- Send the payload that respects the user's answers FIRST, and one that merely satisfies the far system only after a refusal: anything extra arrives as CONTENT and is acted on, a field the form ASKS about is answered even when blank, and a system enforcing a rule its own UI hides leaves no payload that is both.
- Answer a required question nobody answered from the far system's own declared default, then with something that reads as an answer to whoever opens the record — never `true`, an empty string, or the first option off a list when the answer decides ownership, state or severity.
- Test "is this filled in" the way the far system tests it, per type, and trust a declared default only where the form would have SUBMITTED it: an unticked mandatory checkbox has a value, is empty, and is not a default.
- When an integration refuses, produce what the user asked for by a worse route rather than handing back an error, and log the full inventory of what was sent.
- When a far system allows only two of three things the user wants, pick two, say which you gave up and what it buys back, and put the switch and the far-system fix in one comment — never re-decide it a round at a time.
- Restore the last version that WORKED before designing a replacement, read its payload out of git, and suspect what was added since rather than the mechanism it was added to — never diagnosing against a baseline that has itself changed.
- Verify the code DOES the thing before telling somebody to change another system for it; a helper that identifies what it would use is not one that uses it, and the log line reads the same either way.
- Convert a word to the foreign system's own choice code before sending it; a numeric field silently keeps its default and that reads as the user's answer.

### Templates and CSS
- Apply `| default(...)` BEFORE any Jinja filter that throws on `Undefined` (`tojson`, `length`, arithmetic).
- Never nest `{# … #}` or put one inside a Jinja expression (`{% set x = {…} %}`) — put the note above the statement; `check_imports.py` parses every template because reading one as text never catches this.
- Check that a class's SELECTOR applies, not just that the class exists — `.sk-line` is defined only as `.widget-skeleton .sk-line`; add missing base rules to `theme.css`.
- Express one measurement once, where the markup declares it — a CSS custom property (`--widget-row-h`, `--widget-gap`) or a `data-` attribute the script reads — never as a literal repeated in the script that resets it.
- Render variants of a widget from one shared partial parameterised by role, scoped by a per-variant root attribute.

### Front-end behaviour
- Never write a backtick inside markup inside a JS template literal; run `node --check` on the extracted block after editing it.
- Match a keyboard shortcut on the physical key (`event.code`) as well as the character; `event.key` is whatever the layout produced, so every shortcut silently stops existing in Hebrew.
- Defer any DOM-dependent decision to `DOMContentLoaded`, and bump the "already seen" key when fixing a first-run feature.
- Never let a CSS fade be the only thing making an element visible; pair rAF with a `setTimeout` fallback.
- Paint a selected tab from the state, never hardcode it in the markup, and give two filter strips that do the same job the same classes.
- Include the component a page's buttons call: `window.x?.open()` turns a missing component into a button that does nothing and says nothing.
- Give a dialog an explicit scrolling BODY (fixed head, `overflow-y:auto` body); a box that is both the frame and the scroller scrolls in Chrome and not in Edge.
- Hide an element with `display`, never the `hidden` attribute: any class that sets `display` beats it.
- Position a popover against its own `offsetParent` and re-run positioning after paint; use `position: fixed` on `<body>` when the trigger sits in a scrolling or overflow-hidden container.
- Open a detail panel from an explicit button rendering inline beneath its row, never on hover.
- Re-measure a highlight with a ResizeObserver on its target, not only on scroll and resize.
- Never animate a highlight that also tracks scroll.
- Scroll a highlight into view by scrolling ITS container, skip it when already visible, and top-align anything taller than the viewport.
- Point a walkthrough step at the element it describes, and skip a step whose target has vanished.
- Make one Refresh reload every list on the page, including lists owned by other script blocks.
- Skip off-screen work: `content-visibility:auto` with `contain-intrinsic-size`, and `intersect once` for a first fetch — lifted only while dragging.
- Express widget resizing as a `grid-column` span plus an explicit px height with `grid-auto-flow: dense`; never grow up or left.
- Pin a scrollable widget list with `absolute inset-0` in a `relative` parent, never `h-full`.

### Interface details
- Render a count as a fixed-size circle with a capped label, never a DaisyUI `badge`; use a coloured dot plus a word for a two-state setting.
- Give a status filter an All tab: the named tabs partition today's statuses, not tomorrow's.
- Scale a bar against the LARGER of the limit and the total, mark the limit, and give a non-zero segment a `min-width`.
- Label a payload key before showing it and hide the machine-only ones (`*_bytes`, snapshots); a raw key dump is not a UI.
- Give every user-entered value `dir="auto"` inside a shrink-wrapped `inline-block`, so Hebrew reads right-to-left beside its label instead of at the far edge.
- Make a count badge aggregate exactly like the list it summarises, and have one dismissal clear every row it collapsed.
- Group an inline tag with its action button in one `items-center` flex on the title line.
- Measure the padding chain before touching `justify-content` — centring cannot fix an element wider than its parent.
- Centre a glyph as an SVG; a text node centres by its line box.
- Dim an overlay with a fixed dark rgba, never a theme colour.
- Suppress only `transition` when making a theme swap atomic, never `animation`.
- Make a three-state control show the CURRENT state, and store a "follow the system" preference as the preference.
- Stamp a dismissal with the item's changed-date so it returns when the item is touched again.
- Give a destructive action an Undo in its toast rather than a confirm dialog, implemented as the same function with the flag flipped.
- Put shareable list state in the URL with `replaceState` and read it back on `popstate`.
- Have a palette read its navigation from the rendered sidebar, never a second hardcoded list.

### Documentation and rounds
- Generate every documentation inventory from the code with `scripts/gen_docs.py`.
- Add a `backend/app/changelog.py` entry in EVERY round and choose the bump: major relearns something, minor adds a capability, patch is everything else.
- Write a changelog line as the user's half of the sentence — the symptom they saw, never a file, a function or an HTTP status; `scripts/check_changelog.py` rejects the rest.
- Put a change in the changelog only if a regular user can SEE it or is AFFECTED by it; anything reached from the Admin section only — approvals, logs, observability, user management, roles, backups, secrets, deploy machinery — is left out entirely, and an admin-side fix that changes what a requester gets is written from the requester's side.
- Never name a system users have no account on: ServiceNow is admins-only here, so tickets, callers, the Support wizard and every word about them stay off the What's New page.
- Summarise admin-side work as one generic line (`changelog.GENERIC`) rather than dropping it: the release still happened and still has a version, and a hole in the numbers reads as a broken page — and collapse every such release into ONE card at the end of the page, so the newest card is always one with something in it.
- Never derive a changelog from commit subjects: they are addressed to whoever reviews the diff.
- Take a round's number from the `.docx` files in the repo and its date from today, never from the conversation.
- Keep a round `.docx` to what `apply_docx.py` consumes — title, summary, apply box, paths, payload — and put the reasoning in the reply. `PREAMBLE` carries extra text only on request.
- Decide "is there anything to commit" from the working tree, never from whether this run wrote something.
- Write a generated document's bulk as many ordinary paragraphs, never one paragraph of thousands of runs — the extractor strips whitespace, Word has to lay the paragraph out whole.

## What This Project Is

A "single pane of glass" DevOps portal — a FastAPI backend serving HTMX-driven Jinja2 templates. The browser never talks directly to external systems (Azure DevOps, ServiceNow, SonarQube, Artifactory, Confluence); all integration calls go through the backend, which caches responses in Redis. It is deployed to Kubernetes via a Helm umbrella chart.

## Commands

### Running the stack

```bash
# Start everything (Postgres 16, Redis 7, FastAPI backend, nginx frontend)
docker compose up -d

# UI: http://localhost:8000/ui/
# API: http://localhost:8000/api/health
```

### Running the backend directly (without Docker)

```bash
cd backend/app
pip install -r requirements.txt
# Set required env vars (see env.example)
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend CSS

The frontend has no JS build step. `frontend/static/css/output.css` is committed and shipped as-is — **it is never rebuilt in CI**. Only regenerate it locally when you have added a genuinely new Tailwind class, then commit the result:

```bash
cd frontend
npm run build:css          # one-shot (local only)
```

`scripts/check_css_classes.py` guards this: it fails if any template uses a class that is not compiled into `output.css`. It runs in CI and must pass.

### Database (local dev only — no npm on the CI agent)

```bash
npm run migrate    # node scripts/db.js migrate
npm run seed       # node scripts/db.js seed
npm run db:reset   # full reset
```

In the cluster there is no migration Job: the chart's `migration-job.yaml` was an empty file and has been removed. Schema is created and repaired by `ensure_tables()` / `ensure_observability_tables()` at backend startup, and the seed SQL under `deployment/charts/infrastructure/database/` runs only when Postgres initialises an empty data directory.

## Architecture

### Request flow

```
Browser (HTMX)
  ↓ /ui/…  → Jinja2 HTML responses
  ↓ /api/… → JSON responses
FastAPI (port 8000)
  ↓
PostgreSQL 16  — users, approvals, widgets, favorites, audit events
Redis 7        — ~60s TTL cache for all external integration reads
  ↓
External systems (backend only):
  Azure DevOps · ServiceNow · SonarQube · Artifactory · Confluence
```

### Key modules

| File | Role |
|---|---|
| `backend/app/main.py` | App factory, startup hooks, `ensure_tables()` |
| `backend/app/config.py` | All settings loaded from environment |
| `backend/app/db.py` | psycopg2 connection pool + DDL helpers |
| `backend/app/cache.py` | `get_cached(key, ttl, producer)` — Redis errors always swallowed |
| `backend/app/redis_client.py` | Raw Redis client |
| `backend/app/integrations_cache.py` | 60s-TTL wrapper scoping external-integration reads (`ext:<integration>:<owner>:<key>`) |
| `backend/app/resilient_http.py` | Resiliency helpers for all outbound integration calls |
| `backend/app/security.py` | JWT auth (`auth_token` cookie, HS256) |
| `backend/app/sso_config.py` | Admin-managed OIDC config; client secret encrypted at rest (key derived from `JWT_SECRET`) |
| `backend/app/secrets_manager.py` | Runtime integration credentials stored in the DB |
| `backend/app/safe_mode.py` | Admin toggle: turns every self-service action into a simulated success |
| `backend/app/request_audit.py` | Auto-logging of outbound HTTP + app log records; feeds the Logs page |
| `backend/app/ui.py` | All HTMX HTML routes (the main UI layer) |
| `backend/app/api/` | 25 domain routers (one file per integration/feature) |
| `backend/app/api/announcements.py` | Admin billboard on every dashboard; dismissals stamped with the announcement's version |
| `backend/app/widget_registry.py` | The one dashboard widget catalogue |
| `scripts/gen_docs.py` | Regenerates the endpoint/env/table inventories in `docs/` from the code |
| `docs/RUNBOOK.md` | What to do when it breaks; backup, restore, rollback, PVC resize |
| `docs/RED_TEAM_BRIEF.md` | Security review brief; `[CONFIRM]` marks facts only the owner knows |
| `frontend/templates/` | Jinja2 page templates |
| `frontend/templates/partials/components/` | Reusable HTMX component fragments |

### Auth

OIDC (RedHat SSO compatible) configured by admin at runtime, plus internal JWT stored as `auth_token` cookie. JWT secret comes from `JWT_SECRET` env var; it also derives the key that encrypts the stored OIDC client secret.

### Self-service (Azure DevOps project provisioning)

Project creation is **REST-only** (`_create_project_in_collection` / the orchestrator in `azure_devops.py`): the backend creates the project in each target collection directly via the Azure DevOps REST API using the admin PAT — no Terraform, no Kubernetes Job, no tfstate. Safe Mode (admin toggle) simulates actions without side effects.

### Caching

External-integration reads go through `integrations_cache` (default 60s TTL, keys namespaced `ext:<integration>:<owner>:<key>`). Redis failures are swallowed — a degraded cache never breaks the API, requests just hit upstream directly.

### Database schema management

Tables are auto-created on startup via `ensure_tables()`, which is idempotent — it is the only schema mechanism that runs in the cluster. The Node helpers in `scripts/` are local-development conveniences only. Never add a `DROP` to a startup or deploy-time DDL path: both ran `DROP TABLE widget_usage` before every `CREATE TABLE IF NOT EXISTS`, which emptied it on every pod start and every release.

### Audit logging

All user actions are written to the `audit_events` table. A background task purges entries older than 7 days every 6 hours. The Logs page surfaces app log records (root logger at WARNING) and audited outbound HTTP calls.

## Deployment

Helm umbrella chart at `deployment/`. Backend and frontend are **separate images**: the backend serves the rendered UI, and the frontend image is a thin nginx proxy to `backend-service`.

```
deployment/
  charts/backend/         FastAPI Deployment + Service + Ingress
  charts/frontend/        nginx proxy Deployment + Service + Ingress
  charts/infrastructure/  Postgres + Redis + migration Job + app-config + secrets
  values.yaml             Global overrides (image repo, endpoints, replicas)
```

CI/CD (`azure-pipelines.yml`) builds and pushes both images to Artifactory, prunes old image tags (`scripts/artifactory_image_retention.py`, keep last 5), then deploys with `helm template ./deployment | oc apply` to OpenShift. Secrets are assembled into a `helm-secrets-values.json` file and rendered into the `all-secrets` Kubernetes Secret; an Artifactory image-pull secret is created separately.

## Environment variables

See `env.example` for the full list. Required at minimum:

- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_HOST`, `REDIS_PORT`
- `JWT_SECRET`
- `HUB_ADMIN_USERNAME`, `HUB_ADMIN_PASSWORD`

Integration credentials (Azure DevOps PAT, ServiceNow service account, etc.) are configured at runtime through the admin UI and stored (encrypted) in the database, not as env vars.
