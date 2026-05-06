"""
Terraform Container Runner

Manages Terraform execution inside Kubernetes Jobs for Azure DevOps project
provisioning. Each job:
  - Runs the official hashicorp/terraform:1.6 image
  - Uses a K8s ConfigMap to mount generated .tf files
  - Authenticates to GCS via GKE Workload Identity
    (GCP SA: devops-terraform-sa@devops-idp-489012.iam.gserviceaccount.com)
  - Stores tfstate in:  gs://devops-control-center-tfstate/terraform/state/<project>
  - Stores logs in:     gs://devops-control-center-tfstate/terraform/logs/<project>-YYYYMMDD-HHMM.log
  - Reads AZDO_PERSONAL_ACCESS_TOKEN from the 'all-secrets' K8s Secret

Prerequisites (must be provisioned in the cluster before use):
  - K8s ServiceAccount 'devops-terraform-sa' in namespace 'devops-control-center'
    annotated with iam.gke.io/gcp-service-account = devops-terraform-sa@...
  - Backend ClusterRole allowing Jobs/ConfigMaps/Pods read+write for the
    backend service account
  - GCP SA granted roles/storage.objectAdmin on devops-control-center-tfstate
"""

import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

GCS_BUCKET = "devops-control-center-tfstate"
TF_GCP_SA = "devops-terraform-sa@devops-idp-489012.iam.gserviceaccount.com"
TF_K8S_SA = "devops-terraform-sa"


def _detect_k8s_namespace() -> str:
    """
    Return the namespace Terraform Jobs should be submitted into.

    Priority:
      1. Explicit K8S_NAMESPACE env var — lets ops override without a rebuild.
      2. The pod's own namespace, auto-mounted at
         /var/run/secrets/kubernetes.io/serviceaccount/namespace. This is
         where the backend-sa RoleBinding (terraform-rbac.yaml) is scoped,
         so placing the Job here avoids cross-namespace RBAC issues.
      3. Hard fallback to the legacy control-plane namespace.
    """
    env_ns = os.getenv("K8S_NAMESPACE", "").strip()
    if env_ns:
        return env_ns
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r", encoding="utf-8") as fh:
            ns = fh.read().strip()
            if ns:
                return ns
    except Exception:
        pass
    return "devops-control-center"


K8S_NAMESPACE = _detect_k8s_namespace()
TF_IMAGE = "hashicorp/terraform:1.6"
# Dedicated fixed-name secret that lives in K8S_NAMESPACE and holds the PATs
# used by the Terraform container.  Unlike the main 'all-secrets' Secret this
# one is NOT managed by Kustomize so its name never gets a hash suffix and the
# programmatically-created K8s Job can always reference it by the same name.
# Create / update it with:
#   kubectl create secret generic terraform-ado-credentials \
#     -n devops-control-center \
#     --from-literal=AZURE_DEVOPS_ADMIN_PAT=<admin PAT> \
#     --dry-run=client -o yaml | kubectl apply -f -
ADO_SECRET_NAME = "terraform-ado-credentials"
# Key for the admin PAT. Requires at minimum:
#   Project and Team (Read, Write & Manage)
ADO_ADMIN_SECRET_KEY = "AZURE_DEVOPS_ADMIN_PAT"

# In-memory mock job store used by callers that explicitly request mock execution.
_mock_jobs: Dict[str, Dict[str, Any]] = {}


# ── Public API ────────────────────────────────────────────────────────────────


def sanitize_k8s_name(name: str) -> str:
    """Return a valid Kubernetes resource name (lowercase alphanum + hyphens, ≤52 chars)."""
    s = re.sub(r"[^a-z0-9-]", "-", name.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:52]


def sanitize_ado_name(name: str) -> str:
    """Return a name safe for Azure DevOps (letters, digits, hyphens, spaces, ≤64 chars)."""
    s = re.sub(r"[^a-zA-Z0-9 _.\-]", "-", name)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:64]


def generate_main_tf(project_name: str, process_name: str, ado_org: str) -> str:
    """Generate main.tf content for ADO project creation via Terraform."""
    state_prefix = sanitize_k8s_name(project_name)
    # Escape braces for f-string; TF uses double-braces
    return (
        'terraform {\n'
        '  required_providers {\n'
        '    azuredevops = {\n'
        '      source  = "microsoft/azuredevops"\n'
        '      version = ">= 1.1.0"\n'
        '    }\n'
        '  }\n'
        '  backend "gcs" {\n'
        f'    bucket = "{GCS_BUCKET}"\n'
        f'    prefix = "terraform/state/{state_prefix}"\n'
        '  }\n'
        '}\n\n'
        '# AZDO_PERSONAL_ACCESS_TOKEN is injected via environment variable\n'
        'provider "azuredevops" {\n'
        f'  org_service_url = "https://dev.azure.com/{ado_org}"\n'
        '}\n\n'
        'resource "azuredevops_project" "project" {\n'
        f'  name               = "{project_name}"\n'
        '  visibility         = "private"\n'
        '  version_control    = "Git"\n'
        f'  work_item_template = "{process_name}"\n'
        '}\n\n'
        'output "project_url" {\n'
        f'  value = "https://dev.azure.com/{ado_org}/{project_name}"\n'
        '}\n'
    )


def submit_terraform_job(
    project_name: str,
    process_name: str,
    ado_org: str,
    admin_username: str = "",
    use_mock: bool = False,
) -> str:
    """
    Submit a Terraform job. Returns a job_id string for status polling.
    In mock mode a fake job_id is returned immediately.
    """
    if use_mock:
        return _create_mock_job(project_name, process_name, ado_org)
    return _create_k8s_job(project_name, process_name, ado_org, admin_username)


def get_job_status(job_id: str, use_mock: bool = False) -> Dict[str, Any]:
    """
    Return the current status of a Terraform job.

    Possible status values:
      pending   — job created but not yet started
      running   — Terraform init/apply in progress
      succeeded — apply completed; project_url is populated
      failed    — apply failed; error contains a user-facing message
    """
    if use_mock or job_id.startswith("mock-"):
        return _get_mock_status(job_id)
    return _get_k8s_status(job_id)


def save_logs_to_gcs(job_id: str, project_name: str) -> Optional[str]:
    """
    Read pod logs from the completed job and persist them to GCS.
    Returns the GCS object path on success, None otherwise.
    Failures are logged as warnings but never raised.
    """
    if job_id.startswith("mock-"):
        return None

    try:
        from kubernetes import client as k8s, config as k8s_cfg  # type: ignore

        _load_k8s_config(k8s_cfg)
        core = k8s.CoreV1Api()
        log_lines = _collect_pod_logs(job_id, core)
        if not log_lines:
            return None

        sanitized = sanitize_k8s_name(project_name)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        log_path = f"terraform/logs/{sanitized}-{timestamp}.log"

        from google.cloud import storage as gcs_lib  # type: ignore

        gcs = gcs_lib.Client()
        bucket = gcs.bucket(GCS_BUCKET)
        bucket.blob(log_path).upload_from_string(
            "\n\n".join(log_lines), content_type="text/plain"
        )
        logger.info("Terraform logs saved → gs://%s/%s", GCS_BUCKET, log_path)
        return log_path
    except Exception as exc:
        logger.warning("Could not save Terraform logs to GCS: %s", exc)
        return None


# ── Mock implementation ───────────────────────────────────────────────────────


def _create_mock_job(project_name: str, process_name: str, ado_org: str) -> str:
    job_id = f"mock-{uuid.uuid4().hex[:12]}"
    _mock_jobs[job_id] = {
        "project_name": project_name,
        "project_url": f"https://dev.azure.com/{ado_org}/{project_name}",
        "created_at": time.monotonic(),
    }
    logger.info("[MOCK] Terraform job created: %s (project=%s)", job_id, project_name)
    return job_id


def _get_mock_status(job_id: str) -> Dict[str, Any]:
    job = _mock_jobs.get(job_id)
    if not job:
        return {"status": "failed", "error": "Job not found."}
    elapsed = time.monotonic() - job["created_at"]
    if elapsed < 3:
        return {"status": "pending"}
    if elapsed < 12:
        return {"status": "running"}
    return {"status": "succeeded", "project_url": job["project_url"]}


# ── Kubernetes implementation ─────────────────────────────────────────────────


def _load_k8s_config(k8s_cfg: Any) -> None:
    try:
        k8s_cfg.load_incluster_config()
    except Exception:
        k8s_cfg.load_kube_config()


def _create_k8s_job(project_name: str, process_name: str, ado_org: str, admin_username: str = "") -> str:
    """Create a K8s ConfigMap + Job that runs terraform apply."""
    from kubernetes import client as k8s, config as k8s_cfg  # type: ignore

    _load_k8s_config(k8s_cfg)
    core = k8s.CoreV1Api()
    batch = k8s.BatchV1Api()

    sanitized = sanitize_k8s_name(project_name)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    # Keep the resource name under the K8s 63-char limit
    resource_name = f"tf-{sanitized}-{ts[2:12]}"

    main_tf = generate_main_tf(project_name, process_name, ado_org)
    log_path = (
        f"terraform/logs/{sanitized}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.log"
    )

    # ConfigMap carries the generated .tf source
    configmap = k8s.V1ConfigMap(
        metadata=k8s.V1ObjectMeta(name=resource_name, namespace=K8S_NAMESPACE),
        data={"main.tf": main_tf},
    )
    core.create_namespaced_config_map(namespace=K8S_NAMESPACE, body=configmap)

    job = k8s.V1Job(
        metadata=k8s.V1ObjectMeta(
            name=resource_name,
            namespace=K8S_NAMESPACE,
            labels={"app": "terraform-runner", "project": sanitized},
            annotations={
                "devops-portal/project-name": project_name,
                "devops-portal/admin-username": admin_username,
                "devops-portal/log-path": log_path,
                "devops-portal/configmap": resource_name,
            },
        ),
        spec=k8s.V1JobSpec(
            ttl_seconds_after_finished=86400,
            backoff_limit=0,
            # Hard execution timeout so a stuck Terraform job never hangs the
            # approval thread indefinitely. Configurable via env so closed-network
            # installs can lengthen it without rebuilding the image.
            active_deadline_seconds=int(
                os.getenv("TERRAFORM_JOB_TIMEOUT_SECONDS", "1200") or 1200
            ),
            template=k8s.V1PodTemplateSpec(
                metadata=k8s.V1ObjectMeta(
                    annotations={"iam.gke.io/gcp-service-account": TF_GCP_SA}
                ),
                spec=k8s.V1PodSpec(
                    service_account_name=TF_K8S_SA,
                    restart_policy="Never",
                    containers=[
                        k8s.V1Container(
                            name="terraform",
                            image=TF_IMAGE,
                            working_dir="/workspace",
                            command=["/bin/sh", "-c"],
                            args=[
                                # /tf-config is read-only (ConfigMap mount).
                                # /workspace is a writable emptyDir where
                                # Terraform can create .terraform/, plan files,
                                # and the state lock.
                                "cp /tf-config/main.tf /workspace/main.tf && "
                                "cd /workspace && "
                                # The admin PAT needs at minimum:
                                #   Project and Team (Read, Write & Manage)
                                "export AZDO_PERSONAL_ACCESS_TOKEN="
                                '"${AZURE_DEVOPS_ADMIN_PAT}" && '
                                "terraform init -no-color && "
                                "terraform apply -auto-approve -no-color"
                            ],
                            resources=k8s.V1ResourceRequirements(
                                requests={"cpu": "100m", "memory": "128Mi"},
                                limits={"cpu": "500m", "memory": "512Mi"},
                            ),
                            volume_mounts=[
                                k8s.V1VolumeMount(
                                    name="tf-config",
                                    mount_path="/tf-config",
                                    read_only=True,
                                ),
                                k8s.V1VolumeMount(
                                    name="tf-workspace",
                                    mount_path="/workspace",
                                ),
                            ],
                            env=[
                                k8s.V1EnvVar(
                                    name="AZURE_DEVOPS_ADMIN_PAT",
                                    value_from=k8s.V1EnvVarSource(
                                        secret_key_ref=k8s.V1SecretKeySelector(
                                            name=ADO_SECRET_NAME,
                                            key=ADO_ADMIN_SECRET_KEY,
                                        )
                                    ),
                                ),
                                k8s.V1EnvVar(name="TF_LOG", value="INFO"),
                            ],
                        )
                    ],
                    volumes=[
                        k8s.V1Volume(
                            name="tf-config",
                            config_map=k8s.V1ConfigMapVolumeSource(name=resource_name),
                        ),
                        # Writable scratch space for .terraform/, plan files,
                        # and the GCS state lock.  ConfigMap mounts are
                        # read-only so Terraform cannot use them as workdir.
                        k8s.V1Volume(
                            name="tf-workspace",
                            empty_dir=k8s.V1EmptyDirVolumeSource(),
                        ),
                    ],
                ),
            ),
        ),
    )

    batch.create_namespaced_job(namespace=K8S_NAMESPACE, body=job)
    logger.info("K8s Terraform Job created: %s", resource_name)
    return resource_name


def _get_k8s_status(job_id: str) -> Dict[str, Any]:
    from kubernetes import client as k8s, config as k8s_cfg  # type: ignore

    _load_k8s_config(k8s_cfg)
    batch = k8s.BatchV1Api()
    core = k8s.CoreV1Api()

    try:
        job = batch.read_namespaced_job(name=job_id, namespace=K8S_NAMESPACE)
    except Exception as exc:
        logger.error("Cannot read K8s Job '%s': %s", job_id, exc)
        return {"status": "error", "error": "Could not read job status."}

    succeeded = job.status.succeeded or 0
    failed = job.status.failed or 0
    active = job.status.active or 0

    if succeeded > 0:
        project_url = _extract_project_url(job_id, core)
        if not project_url:
            proj = (job.metadata.annotations or {}).get("devops-portal/project-name", "")
            project_url = f"https://dev.azure.com/{proj}"
        return {"status": "succeeded", "project_url": project_url}

    if failed > 0:
        # Surface DeadlineExceeded specifically so the UI message is actionable.
        for cond in (job.status.conditions or []):
            if getattr(cond, "type", "") == "Failed" and getattr(cond, "reason", "") == "DeadlineExceeded":
                return {
                    "status": "failed",
                    "error": (
                        "Terraform job exceeded its maximum execution time and was "
                        "terminated. Increase TERRAFORM_JOB_TIMEOUT_SECONDS or check "
                        "the pod logs for the cause."
                    ),
                }
        return {"status": "failed", "error": _extract_error(job_id, core)}

    # Check pod-level conditions while the Job is still "active" or "pending".
    # This surfaces fast-fail situations (bad image, missing SA, OOM) long
    # before K8s' backoff timer would mark the Job itself as failed.
    if active > 0 or (succeeded == 0 and failed == 0):
        pod_failure = _check_pod_early_failure(job_id, core)
        if pod_failure:
            return {"status": "failed", "error": pod_failure}

    if active > 0:
        return {"status": "running"}

    return {"status": "pending"}


_POD_TERMINAL_REASONS = {
    "ImagePullBackOff": (
        "Terraform container image could not be pulled ({reason}). "
        "Ensure the cluster has Docker Hub access or mirror hashicorp/terraform:1.6 "
        "to your private registry."
    ),
    "ErrImagePull": (
        "Terraform container image pull failed ({reason}). "
        "Check network connectivity from the cluster to Docker Hub."
    ),
    "CrashLoopBackOff": (
        "Terraform container crashed on startup ({reason}). "
        "Check the pod logs: kubectl logs -n {ns} -l job-name={job} --tail=50"
    ),
    "OOMKilled": (
        "Terraform container was killed due to out-of-memory ({reason}). "
        "Consider increasing the Job resource limits."
    ),
    "Error": (
        "Terraform container exited with an error. "
        "Check logs: kubectl logs -n {ns} -l job-name={job} --tail=50"
    ),
}


def _check_pod_early_failure(job_id: str, core: Any) -> str:
    """
    Inspect pod conditions for terminal failure states that surface before
    the K8s Job itself is marked failed.  Returns an error string or ''.
    """
    try:
        pods = core.list_namespaced_pod(
            namespace=K8S_NAMESPACE, label_selector=f"job-name={job_id}"
        )
        for pod in pods.items:
            # Completed/failed pod phase
            if (pod.status.phase or "") == "Failed":
                return _extract_error(job_id, core) or "Terraform pod failed."

            for cs in pod.status.container_statuses or []:
                waiting = cs.state.waiting if cs.state else None
                terminated = cs.state.terminated if cs.state else None

                if waiting and waiting.reason in _POD_TERMINAL_REASONS:
                    tmpl = _POD_TERMINAL_REASONS[waiting.reason]
                    return tmpl.format(
                        reason=waiting.reason, ns=K8S_NAMESPACE, job=job_id
                    )

                if terminated and terminated.exit_code not in (None, 0):
                    reason = terminated.reason or "Error"
                    tmpl = _POD_TERMINAL_REASONS.get(reason, _POD_TERMINAL_REASONS["Error"])
                    return tmpl.format(
                        reason=reason, ns=K8S_NAMESPACE, job=job_id
                    )
    except Exception as exc:
        logger.warning("Pod early-failure check failed for '%s': %s", job_id, exc)
    return ""


# ── Log helpers ───────────────────────────────────────────────────────────────


def _collect_pod_logs(job_id: str, core: Any) -> list:
    """Return a list of log strings (one per pod) for the given job."""
    try:
        pods = core.list_namespaced_pod(
            namespace=K8S_NAMESPACE, label_selector=f"job-name={job_id}"
        )
        parts = []
        for pod in pods.items:
            try:
                logs = core.read_namespaced_pod_log(
                    name=pod.metadata.name, namespace=K8S_NAMESPACE
                )
                parts.append(f"=== Pod: {pod.metadata.name} ===\n{logs}")
            except Exception as e:
                parts.append(f"=== Pod: {pod.metadata.name} (log error: {e}) ===")
        return parts
    except Exception:
        return []


def _raw_logs(job_id: str, core: Any) -> str:
    return "\n".join(_collect_pod_logs(job_id, core))


def _extract_project_url(job_id: str, core: Any) -> str:
    for line in _raw_logs(job_id, core).splitlines():
        if "project_url" in line and "=" in line:
            candidate = line.split("=", 1)[1].strip().strip('"')
            if candidate.startswith("http"):
                return candidate
    return ""


def _extract_error(job_id: str, core: Any) -> str:
    raw = _raw_logs(job_id, core)
    lower = raw.lower()
    if "already exists" in lower:
        return "A project with this name already exists in Azure DevOps."
    if "not found" in lower or "does not exist" in lower:
        return "The specified admin user could not be found in Azure DevOps."
    for line in reversed(raw.splitlines()):
        s = line.strip()
        if s and "error" in s.lower() and len(s) > 12:
            return s
    return "Terraform execution failed. Check logs in the GCS bucket for details."
