# `terraform_runner` (cross-cutting)

File: `backend/app/terraform_runner.py`

## Purpose

Given a self-service payload (today only `ADO_PROJECT_CREATE`), produce
a Terraform working directory, ship it into the cluster as a `ConfigMap`,
and run it with a `hashicorp/terraform:1.6` container inside a Kubernetes
`BatchV1 Job`. State is stored in GCS so runs are recoverable.

All of the approvals system's "actually do the thing" calls go through
here.

## Public functions

- `submit_terraform_job(payload) -> job_id`

  Renders `main.tf` (ADO provider, backend = GCS, resources), creates a
  `ConfigMap` with the module, creates a `Job`, returns an opaque job
  id. In-memory mock mode kicks in when `USE_MOCK_AZURE_DEVOPS=true`.

- `get_job_status(job_id) -> {status, error?}`

  Reads the Job's status and the pod's last logs. Returns one of
  `pending`, `running`, `succeeded`, `failed`. Used by
  `approvals._poll_terraform_job`.

## Environment variables

- `K8S_NAMESPACE` — namespace to submit into. Defaults to the pod's own
  namespace (via in-cluster SA file).
- `TERRAFORM_JOB_TIMEOUT_SECONDS` — wall-clock max. Translates into
  `Job.spec.activeDeadlineSeconds` (default 1200 s).

## K8s objects created

- `Job` — one per run. Owns the pod, is GC'd via
  `ttl_seconds_after_finished=86400`.
- `ConfigMap` — contains `main.tf` and (optionally) `variables.tf` /
  `backend.tf`. Mounted at `/work`.

ServiceAccount and RBAC for these objects are defined in
`deployment/charts/backend/templates/terraform-rbac.yaml`.

## State storage (GCS)

- Bucket: `devops-control-center-tfstate` (hardcoded).
- Authentication: **GKE Workload Identity** — the Job's ServiceAccount
  is annotated with a GCP service account that has
  `roles/storage.objectAdmin` on the bucket.
- Layout: `terraform/state/<request_id>/terraform.tfstate` +
  `terraform/logs/<request_id>/run.log`.

## Failure modes

- **Process crash inside the pod**: `failed > 0` → the runner reads
  `pod.status.containerStatuses[*].state.terminated` for the reason and
  surfaces it verbatim as `error`.
- **Deadline exceeded**: we check the Job's conditions for
  `type=Failed, reason=DeadlineExceeded` and surface a human-readable
  message ("Terraform job exceeded its maximum execution time…").
- **Mock mode**: the in-memory store has no real pod; we still simulate
  success/failure for UI testing.

## Safe Mode

This module itself isn't Safe-Mode aware — callers check
`safe_mode.is_enabled()` **before** calling `submit_terraform_job`.
That keeps this module easy to reason about (it always tries to actually
run Terraform).
