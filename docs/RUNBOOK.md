# Runbook

What to do when the portal is broken, and the procedures that are deliberately manual.

This is written for the person on call at 08:40 with users complaining, so every
section leads with the symptom. Commands assume `oc` is logged in and
`-n $NS` is the portal's namespace.

```bash
NS=devops-hub-prod          # or whichever namespace this release lives in
REL=devops-hub              # helm release name used by the pipeline
```

---

## 0. First two commands, always

```bash
oc get pods -n $NS
curl -s https://<portal-host>/api/health/ready | python -m json.tool
```

Readiness names what is wrong rather than making you guess:

```json
{ "ready": false,
  "services": { "database": "down", "redis": "up", "schema": "pending",
                "credentials": "ok" },
  "detail": "PostgreSQL is not reachable from this pod…" }
```

| Field | Meaning |
| --- | --- |
| `database: down` | The pod cannot reach Postgres. §2 |
| `schema: pending` | Postgres is reachable but the startup DDL has not finished. It retries forever; the pod stays up and NotReady on purpose. |
| `redis: down` | Cache only. The portal works, slower, and hits upstream on every read. §3 |
| `credentials: unreadable` | **`JWT_SECRET` does not match the stored tokens.** §4 — this one does not fix itself. |

Liveness (`/api/health/live`) is deliberately dependency-free: a restart cannot fix
Postgres, so it never reports a dependency failure. If the pod is being restarted in a
loop, that is *not* coming from a database outage.

---

## 1. The backend is CrashLoopBackOff

```bash
oc logs -n $NS deploy/backend --previous | tail -40
```

Almost always an import error at module scope — every router is imported when the
process starts, so one missing file or one missing symbol stops the process before
uvicorn binds a port.

```
ImportError: cannot import name 'cached_external' from 'integrations_cache'
```

* **missing module** → the file never reached the commit. `git add -A`, not
  `git commit -am`.
* **missing name** → the shipped module is an OLDER copy than the code importing it.
  Ship that module too, and anything *it* imports names from.

`python scripts/check_imports.py` catches both before a deploy and runs in CI.

**Roll back** (see §7) rather than debugging in production.

---

## 2. Postgres is unreachable

```bash
oc get pods -n $NS -l app=postgres
oc logs -n $NS statefulset/postgres --tail=50
oc exec -n $NS statefulset/postgres -- pg_isready
```

The backend does **not** crash-loop for this by design: it stays up, answers liveness,
reports NotReady, and retries the schema bootstrap forever. When Postgres comes back the
pods become Ready on their own — no restart needed, and restarting them does not help.

If the volume is full, go to §5.

### `fe_sendauth: no password supplied` — the Secret was deployed blank

A distinct failure with a distinct fingerprint. **`no password supplied`** is not a wrong
password: the client sent none, because `DATABASE_URL` was assembled with an empty
`POSTGRES_PASSWORD`.

The signature is unmistakable, and it is worth learning because everything about it says
"intermittent" when it is not:

* the **new** pod is `0/1` and never becomes Ready;
* the **old** pod is `1/1` and perfectly healthy — it holds the environment it was started
  with, and pooled connections that already authenticated;
* `redis` reports `up` in `/api/health/ready`, so it does not look like a network problem;
* nothing restarted, nothing crashed, and the deploy reported success.

Confirm it by asking the pod what it holds — lengths only, so nothing is printed:

```bash
oc exec -n $NS <new-pod> -- python3 -c \
  "import os;print({k:len(os.environ.get(k,'')) for k in ['POSTGRES_PASSWORD','JWT_SECRET','HUB_ADMIN_PASSWORD','AZURE_DEVOPS_ADMIN_PAT']})"
```

All zeros means the `all-secrets` Secret itself is blank. The cause is upstream, in the
pipeline: **Azure DevOps does not export secret variables into a script's environment** —
they arrive only through that step's explicit `env:` mapping. A step that loses its `env:`
block (it is the last child, so appending a new step after it reparents the whole block)
runs anyway, reads `""` for every secret, and rewrites the Secret with empty values while
exiting 0.

Two guards now stop it — the deploy step refuses to continue when a required secret is
empty, and `charts/backend/templates/secrets.yaml` marks `POSTGRES_PASSWORD` and
`JWT_SECRET` as `required`, so the chart will not render a blank one.

**Recovery is just a correct deploy** — re-run the pipeline. Nothing is lost: the database
is untouched, and stored PATs are still encrypted under the real `JWT_SECRET`, unreadable
only while the blank one is in place. Do not "fix" it by rotating `JWT_SECRET` — see §4.

If you need the environment back before a pipeline run, patch the one key:

```bash
oc patch secret all-secrets$SUFFIX -n $NS -p "{\"stringData\":{\"POSTGRES_PASSWORD\":\"<value>\"}}"
oc rollout restart deploy/backend$SUFFIX -n $NS
```

---

## 3. Redis is down or was restarted

Nothing to do. Cache failures are swallowed (`cache.py`), reads fall through to upstream,
and the portal is slower for 60 seconds at a time. Redis holds no state worth recovering:
it runs with `--appendonly no`, `--maxmemory 256mb` and `allkeys-lru`, so it forgets old
keys instead of filling its disk or being OOM-killed.

---

## 4. `credentials: unreadable` — every Azure DevOps widget is empty for everyone

**This is the one that looks like five different problems.** Users report empty widgets,
"not connected" prompts, and PATs that "stopped working" — all at once, and reconnecting
does fix it for the person who bothers.

`JWT_SECRET` is not only the JWT signing key. It is the root of the key that encrypts
every per-user Azure DevOps PAT in `user_integrations` and the OIDC client secret. If it
changes, the stored ciphertext can no longer be read.

```
CRITICAL  JWT_SECRET DOES NOT MATCH THE STORED CREDENTIALS: 5 of 5 sampled
          Azure DevOps tokens could not be decrypted.
```

Causes, in order of likelihood:

1. The `all-secrets` Secret was recreated with a fresh random value.
2. A database dump was restored beside a *different* environment's secret.
3. Someone rotated `JWT_SECRET` deliberately, not knowing it was a data key.

**Recovery**

* If the old value still exists anywhere — the variable group, a previous Secret
  revision, another cluster — put it back. Everything decrypts again immediately.
* If it is genuinely lost, the tokens are unrecoverable. Tell users to reconnect on
  the Connections page, and post an announcement from Platform Managing saying so.
  The OIDC client secret must be re-entered by an admin.

**Prevention:** `JWT_SECRET` and the database dump are ONE backup, not two. A dump
restored without its matching secret is a table of unreadable strings.

---

## 5. The Postgres volume is filling up

The log warns before it matters, every six hours:

```
WARNING  portal database is 780 MB of a 1024 MB volume (warning threshold 600 MB).
```

Tune with `DB_SIZE_WARN_MB` / `DB_VOLUME_MB`.

**Immediate relief** — the log table is almost always what grew. Admin → Logs → filter
to what you do not need (a level, a date range, one noisy action) → **Clear logs**.
Retention also runs on its own: 7 days for INFO/DEBUG, 30 for WARNING and above.

```sql
-- what is actually big
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
  FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;
```

A `DELETE` does not return space to the volume by itself. After a large clear:

```bash
oc exec -n $NS statefulset/postgres -- \
  psql -U devops -d devops_control_center -c "VACUUM (FULL, ANALYZE) audit_events;"
```

`VACUUM FULL` takes an exclusive lock and needs free space equal to the table — do it
when the portal is quiet, and only if plain retention did not free enough.

### Making the volume bigger

**You cannot do this by editing the chart**, and you must not try. A StatefulSet's
`volumeClaimTemplates` is immutable, so raising `storageSize` does not grow anything —
`oc apply` rejects the whole StatefulSet update. What happens next is the actual
hazard: the standard way to unstick a rejected deploy is to delete the StatefulSet,
the default cascade takes the PVC with it, Postgres runs initdb on an empty directory,
and the seed SQL under `deployment/charts/infrastructure/database/` fires because that
only ever runs on an empty data dir. The result looks like a fresh install. That is
how the database was lost the first time this was attempted.

**Patching the PVC is the entire operation.** Nothing redeploys, no pod restarts, and
the chart is not touched — before or after.

Prod and test share the `devops-hub` namespace and are distinguished only by name, so
patching by exact name is what makes this prod-only:

| | PVC |
|---|---|
| prod | `postgres-storage-postgres-0` |
| test | `postgres-storage-postgres-test-0` |

```bash
# 1. Confirm which volumes exist and that you may patch them
oc get pvc -n devops-hub
oc auth can-i patch pvc -n devops-hub

# 2. Take a dump first (§6). This is the operation it exists for.

# 3. Expand the live PVC. A patch file avoids the quoting differences between
#    cmd.exe, PowerShell and bash.
echo {"spec":{"resources":{"requests":{"storage":"50Gi"}}}} > patch.json
oc patch pvc postgres-storage-postgres-0 -n devops-hub --type=merge --patch-file patch.json

# 4. Verify from inside the pod, not from the PVC object
oc exec statefulset/postgres -n devops-hub -- df -h /bitnami/postgresql
```

The backing store is NetApp Trident over NFS, which expands **online** — `df` shows the
new size within seconds, with no downtime and no restart. On a block-backed storage
class you may instead see `FileSystemResizePending`, which needs the pod recycled:
`oc scale statefulset/postgres -n devops-hub --replicas=0` and back to `1`. **Scale,
never delete.**

Reading the reclaim policy needs cluster-scoped rights the namespace accounts do not
have — `oc get pv` returns `Forbidden` — so ask the OpenShift admins to confirm the PV
behind the PVC is `persistentVolumeReclaimPolicy: Retain`. That is what makes an
accidental PVC deletion survivable rather than terminal.

**Afterwards, do not update `storageSize` in the values files.** Expanding the PVC does
not update the StatefulSet, so the live StatefulSet still says `1Gi` and the values must
keep matching *it*, not the disk. The chart and the disk disagreeing is the correct
state; that value is only read when creating a volume that does not exist yet. If parity
is ever genuinely required, it is `oc delete statefulset postgres -n devops-hub
--cascade=orphan` (pods and PVCs survive; the next deploy re-creates the StatefulSet
around them) performed **together** with the values change — never separately, and never
without a restore you have actually tested.

**Current state:** prod was expanded 1Gi → 30Gi on 2026-08-17. `values-prod.yaml` still
reads `1Gi`, deliberately.

---

## 6. Backup and restore

Backups are **automated, off-cluster, and verified**. Two CronJobs, production only:

| Job | Schedule | What it does |
|---|---|---|
| `postgres-backup` | 01:15 daily | `pg_dump -Fc` → uploads to Artifactory → prunes to the newest 3 dumps |
| `postgres-restore-verify` | 03:40 Sunday | Downloads the newest dump, checks its checksum and secret fingerprint, restores it into a scratch database, counts what came back, drops it |

Both write a row into `backup_runs`. **Platform Managing → Database Backups** reads
that table and is the fastest answer to "are we backed up?" — check it there before
running anything below.

Each job runs **three containers across two images**, because neither image can do the
whole job: the Postgres image has no `curl` and no `wget`, so it cannot upload; the
backend image has no `pg_dump`, so it cannot produce anything to upload. Building a
combined one would need an OS package mirror the offline does not have.

| Phase | Image | Does |
|---|---|---|
| 1 (init) | backend | opens the `backup_runs` row / downloads + verifies the dump |
| 2 (init) | Postgres | `pg_dump` / `pg_restore` — and nothing else |
| 3 (main) | backend | uploads + prunes + closes the row / closes the row |

The Postgres phase **always exits 0** and reports through a file. A non-zero exit there
would fail the pod before the reporting phase ran, and the card would show "Error" with
no reason — the exact thing the status row exists to prevent. `backup_runs` therefore
has exactly one writer, in one language, for both jobs.

`oc logs job/<name>` shows only the last container; use `oc logs job/<name> -c dump`
(or `-c fetch`, `-c restore`) to see the others.

The dumps live in Artifactory, not on a PVC in this namespace, deliberately: a volume
here survives a dropped table but not the namespace, and the namespace is what disaster
recovery is about.

**Retention is a count, not an age: the newest 3 dumps.** Same policy shape as
`scripts/artifactory_image_retention.py`, which keeps the newest 5 image tags. Pruning
runs inside the backup job right after each upload — not as a pipeline step like the
image cleaner — because dumps appear nightly whether or not anybody deploys. At three
nightly dumps the oldest restore point is about three days old; **that is the real
recovery window, and it is short.** Raise
`infrastructure.postgres.backup.keepLast` if you need longer.

### What is stored

Per dump, two objects under `<ARTIFACTORY_BASE_URL>/devops-hub-backups/postgres/`:

```
devops-hub-prod-YYYY-MM-DD.dump                 # pg_dump -Fc
devops-hub-prod-YYYY-MM-DD.dump.manifest.json   # size, sha256, jwt_secret_fingerprint
```

`jwt_secret_fingerprint` is `sha256("devops-hub-backup-fingerprint-v1:" + JWT_SECRET)`.
**The secret itself is never written to the backup store.** The fingerprint exists so a
restore can prove the dump and the secret belong together before the backend starts —
see §4, where the mismatch case costs every user their stored PATs.

Escrowing the actual `JWT_SECRET` is still a human task, and it is the one part of this
that is not automated:

```bash
oc get secret all-secrets -n $NS -o jsonpath='{.data.JWT_SECRET}' | base64 -d
```

Keep it wherever your team keeps break-glass credentials. **A dump without it restores
into a database whose stored credentials nobody can read.**

### Is it working?

```bash
oc get cronjob -n $NS                     # both should exist, neither SUSPENDED
oc get jobs -n $NS -l app.kubernetes.io/component=postgres-backup
oc logs -n $NS job/<name>                 # the job narrates every step
```

The Platform Managing card reports `critical` if the newest good dump is over 48h old,
and `warning` if the weekly restore test has not passed in 15 days.

### Take a dump by hand

Same script the CronJob runs, so it cannot drift from it:

```bash
oc create job -n $NS --from=cronjob/postgres-backup backup-manual-$(date +%s)
```

### Restore

**Read this whole block before starting.** `--clean` drops every object in the target
database; there is no undo.

Use the script. It performs the same steps the weekly verify job performs, so there is
one description of a restore rather than two that drift:

```
python scripts/restore_backup.py --namespace $NS --dry-run     # verifies, changes nothing
python scripts/restore_backup.py --namespace $NS               # asks you to type RESTORE
```

It reads the backup credentials out of the `all-secrets` Secret, picks the newest dump
(or `--file`), checks the dump against its manifest checksum **and** its fingerprint
against the running `JWT_SECRET`, refuses on a mismatch, scales the backend to zero,
restores, scales back, and polls readiness until it reports `credentials: ok`.

Every command is a single line and nothing depends on the shell. That matters more than
it sounds: the previous version of this section was written in bash and run in cmd,
where a trailing `\` is not a line continuation and `$VAR` does not expand — which
produced a 401 saved as the dump, and then a restore that failed on a password it was
never given.

**Why a script and not a list of commands.** The first real drill found four defects
here, and every one was a place where the written procedure restated what the CronJob
does instead of deriving from it: `curl` with no `-f`, no checksum comparison, no source
for the backup credentials, and no `PGPASSWORD` (the postgres pod carries
`POSTGRESQL_PASSWORD`; `pg_restore` reads `PGPASSWORD`). The automated path runs nightly
and is continuously tested. A procedure in a document runs once, during an incident, in
front of somebody who has never run it.

Two of those four do not look like operator error, which is the dangerous part:
`input file does not appear to be a valid archive (too short)` reads as a corrupt
backup, and `broken pipe` reads as a network fault. Both send you hunting a second
disaster while the first is still running.

#### By hand, if the portal and the repo are both unavailable

Each of these is one line.

```
oc extract secret/all-secrets -n $NS --keys=ARTIFACTORY_BACKUP_USERNAME,ARTIFACTORY_BACKUP_TOKEN --to=-
oc exec -n $NS deploy/backend -- python -c "import hashlib,os;print(hashlib.sha256(('devops-hub-backup-fingerprint-v1:'+os.environ['JWT_SECRET']).encode()).hexdigest())"
curl -f -k -u USER:TOKEN -O $ARTIFACTORY_BASE_URL/devops-hub-backups/postgres/devops-hub-prod-2026-08-16.dump
curl -f -k -u USER:TOKEN -O $ARTIFACTORY_BASE_URL/devops-hub-backups/postgres/devops-hub-prod-2026-08-16.dump.manifest.json
certutil -hashfile devops-hub-prod-2026-08-16.dump SHA256
oc scale deploy/backend -n $NS --replicas=0
oc exec -i -n $NS statefulset/postgres -- sh -c "PGPASSWORD=$POSTGRESQL_PASSWORD pg_restore -U devops -d devops_control_center --clean --if-exists --no-owner --no-privileges" < devops-hub-prod-2026-08-16.dump
oc scale deploy/backend -n $NS --replicas=2
curl -s https://<portal-host>/api/health/ready
```

The `certutil` hash must equal `sha256` in the manifest, and the fingerprint from the
second line must equal `jwt_secret_fingerprint` in it. If the fingerprint differs,
**stop** and read §4 — restoring anyway leaves every stored credential and the OIDC
client secret unreadable.

`credentials: ok` from the last line is the real success criterion, not `pg_restore`'s
exit code: it is the backend confirming it can still decrypt what came back.

### When backups are switched off

The CronJobs render only when `ARTIFACTORY_BASE_URL`, `ARTIFACTORY_BACKUP_USERNAME` and
`ARTIFACTORY_BACKUP_TOKEN` are all set in the pipeline variable group, and only for
production. If any is missing the deploy logs a warning and the Platform Managing card
reports that no backup has ever run. That is the intended behaviour — the alternative is
a CronJob authenticating with an empty string every night into a log nobody reads.

`ARTIFACTORY_BASE_URL` is the variable the portal's Artifactory widgets and the image
cleaner already use; there is deliberately no second URL to keep in step with it. The
repository name is a chart value (`infrastructure.postgres.backup.artifactory.repo`,
default `devops-hub-backups`), not a variable.

The Artifactory account needs **read, deploy and delete** on that generic repository:
read lists the dumps to decide what to prune, delete removes them. Without read,
retention silently does nothing and the job says so at WARNING. It should not be the
read-only account the portal's Artifactory widgets use.

---

## 7. Rolling back a release

There is no Helm release history: the pipeline renders the chart and pipes it into
`oc apply`, so **`helm rollback` does not work here.** Rolling back means deploying an
older image tag.

```bash
# what is running now
oc get deploy/backend -n $NS -o jsonpath='{.spec.template.spec.containers[0].image}'

# what is still available — retention keeps the last 5 tags
# (scripts/artifactory_image_retention.py). Older than that is gone.
```

Re-run the pipeline pinned to the previous tag, or for an emergency stop-gap:

```bash
oc set image deploy/backend backend=<repo>/devops-hub-backend:<older-tag> -n $NS
oc rollout status deploy/backend -n $NS
```

`oc set image` is overwritten by the next pipeline run — treat it as first aid, then
deploy the older tag properly.

Two things `oc apply` does not do: it never deletes resources that were removed from the
chart (they linger until deleted by hand), and it cannot undo a schema change, because
the startup DDL is additive-only and has no down-migration. A release that changed the
schema is rolled back by rolling back the image; the columns stay.

---

## 7a. The pipeline went green but the site still shows the old code

This happened to production and went unnoticed for weeks. `oc apply` exits 0 when it
changed nothing, so a green run never meant "the new code is running".

Ask the site itself first:

```bash
curl -s https://<portal-host>/api/health/live      # {"alive":true,"build":"2451"}
```

Compare that `build` with the Build ID of the pipeline run you expect. Then:

```bash
NS=<namespace>
oc get deploy backend -n $NS -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
oc get pods -n $NS -o custom-columns=POD:.metadata.name,IMAGE:.spec.containers[0].image | sort
oc rollout history deploy/backend -n $NS
```

| What you see | Cause |
| --- | --- |
| Deployment has the old tag | **No deploy reached this environment.** Check the run's branch — see below |
| Deployment has the new tag, pods have the old one | Rollout stuck: no room for the surge pod, image cannot be pulled, or readiness never passes. `oc get pods` shows a Pending/CrashLoop extra pod |
| Pods have the new tag, the URL reports an older `build` | Something else owns this hostname. `oc get route -n $NS` — two routes claiming one host, older wins |
| The URL reports the right build, the page still looks old | Browser cache. Ctrl+Shift+R |

**The usual cause of the first row: the release was merged through a pull request.** A PR
build has `Build.SourceBranch = refs/pull/N/merge`, which is not `refs/heads/main`, so the
environment expression falls through to its else branch. Before this was fixed, that
meant a PR into main **redeployed test** and reported success. The Deploy stage now
refuses to run at all for a non-branch ref, and the first step of the deploy prints the
branch, environment, hostname and tag it is about to use.

To ship after a PR merge, make sure a build runs on the **`main` branch itself** — the
merge commit triggers it, or run the pipeline manually with the branch set to `main`.

### Promoting a build by hand (first aid only)

When `dev` and `main` hold the same commit, the image built from `dev` is byte-identical
to what `main` would produce, so it can be promoted directly:

```bash
oc set image deploy/backend  backend=<repo>/backend:<tag>  -n $NS
oc set image deploy/frontend frontend=<repo>/frontend:<tag> -n $NS
oc rollout status deploy/backend -n $NS
curl -s https://<portal-host>/api/health/live
```

The next pipeline run overwrites this. Fix the pipeline run, do not live on `oc set image`.

---

## 7b. `did not finish rolling out` — no new pod was ever created

Read the Deployment's conditions first. They separate two problems that look identical
from `oc get pods`:

```bash
oc describe deploy backend$SUFFIX -n $NS | sed -n '/Conditions:/,/Events:/p'
```

| What you see | What it is |
|---|---|
| `ReplicaFailure  True  FailedCreate` | the pod was **rejected**, never scheduled. Quota or admission. Go on. |
| no `ReplicaFailure`, new pod exists but `0/1` | the pod started and is failing its probes. Go to §1 / §2. |
| new pod `Pending` | created, but nothing can schedule it. Node capacity, not quota. |

`FailedCreate` means the API server refused. `describe deploy` shows the condition but
**not its message**, which is the whole diagnosis — get it from either end:

```bash
oc get deploy backend$SUFFIX -n $NS -o jsonpath="{range .status.conditions[?(@.type=='ReplicaFailure')]}{.message}{'\n'}{end}"
oc describe rs <NewReplicaSet from describe deploy> -n $NS | sed -n '/Events:/,$p'
oc describe quota -n $NS
```

Expect something like `exceeded quota: compute-resources, requested: limits.cpu=2,
used: limits.cpu=6, limited: limits.cpu=6`.

**The cause is almost always the surge pod.** A rolling update with `maxSurge: 1` needs
headroom for one MORE pod than the Deployment's replica count, and prod's backend asks
for 2 CPU / 1Gi of limits apiece. The frontend rolls out fine in the same run because
nginx asks for a fraction of that — an asymmetry that makes it look like a backend bug.

Unblock without touching quota:

```bash
oc patch deploy backend$SUFFIX -n $NS -p '{"spec":{"strategy":{"rollingUpdate":{"maxUnavailable":1,"maxSurge":0}}}}'
oc rollout status deploy/backend$SUFFIX -n $NS
```

This frees a slot before filling one, so it needs no extra room; at two replicas one pod
keeps serving throughout. It is now the permanent setting for prod
(`deployment/values-prod.yaml` → `backend.strategy`), so the patch is only needed on a
cluster that has not taken that release yet.

## 8. Azure DevOps calls are failing

Look at Admin → Logs first: since round 59 the failure row carries the reason, not just
the status.

```
Viewed: azure-devops projects   Failed · 502 · No Azure DevOps collection could be
read with this token (DefaultCollection: HTTP 401; TikshuvCollection: ConnectTimeout)
· repeated 10×
```

| Reason | What it means |
| --- | --- |
| `HTTP 401` / `403` on every collection | The token is dead. The user's own PAT for widget reads; `AZURE_DEVOPS_ADMIN_PAT` for provisioning. |
| `HTTP 401` on ONE collection | Normal. On-prem PATs are scoped to a collection; foreign ones answer 401/400 and are skipped. |
| `ConnectTimeout` / `ConnectError` | Network or the server. Not a credential problem — do not tell people to reconnect. |
| `repeated N×` | The same failure collapsed over five minutes. One broken dependency, not N incidents. |

A user whose PAT is rejected now gets a notification once a day pointing at
Connections. The check is cached for ten minutes per user.

---

## 9. Someone signed in with the local admin account

```
WARNING  Signed in with the local ADMIN account (break-glass)
```

Once SSO is live, that account is break-glass only: its password comes from a pipeline
variable, never rotates, and is shared. Every successful local admin sign-in is a
WARNING row in the Logs page with the client address. If you did not do it, find out who
did, then rotate `HUB_ADMIN_PASSWORD` in the variable group and redeploy.

Repeated failures against it are rate-limited per account and per address
(`LOGIN_MAX_FAILURES`, default 5 / `LOGIN_LOCKOUT_SECONDS`, default 900).

---

## 10. Deploy-time checks

The three guards that gate a commit, and what each failure means in the cluster:

```bash
python scripts/check_imports.py      # unresolved import  -> CrashLoopBackOff
python scripts/check_css_classes.py  # uncompiled class   -> a control that does nothing
python scripts/check_inline_js.py    # broken template literal -> a dead widget
python scripts/gen_docs.py --check   # the reference docs no longer match the code
```

`scripts/apply_docx.py` runs the first three before it commits, and refuses the commit
if any of them fails.
