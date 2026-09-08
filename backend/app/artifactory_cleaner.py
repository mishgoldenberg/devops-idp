"""
The three files an Artifactory cleaner is made of.

A cleaner is a folder in the artifact-cleaner-automation repository holding

    <folder>/configmap.yaml    the AQL spec saying what to delete
    <folder>/cronjob.yaml      the job that runs "jf rt del" against it
    <folder>/pipeline.yaml     the pipeline that applies both, via the shared
                               template.yaml at the repository root

Rendering them here rather than inside the request executor keeps the YAML in one
place that can be read and checked on its own -- and the spec is the part worth
reading, because it is a delete that runs on a schedule.

WHAT A CLEANER CAN BE TOLD TO DELETE

Four rules, combined with AND, and at least one is required. A spec with no rule at
all matches every artifact in the repository, so "no rules" is refused rather than
treated as "everything":

    older_than_days   created before N days ago
    name_pattern      the artifact's name matches a wildcard
    file_types        the extension is one of these
    keep_last         keep the newest N and delete the rest

keep_last is not an AQL condition -- AQL has no "except the newest five". It is the
file spec's own sort and offset: sort by creation date, newest first, then skip the
first N. Everything the spec still selects is what gets deleted. Getting that
backwards deletes exactly the artifacts the rule exists to protect, which is why it
is written once here and asserted in the tests.

Names are all derived from one slug, so a CronJob can never mount another cleaner's
spec, and an edit re-renders the same paths it wrote the first time.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

DEFAULT_SCHEDULE = "0 22 * * *"
DEFAULT_NAMESPACE = "devops-monitor"
# The image the cleaner CronJob runs. Override with ADO_CLEANER_IMAGE to pull
# from your own registry -- an installation without egress cannot reach this one.
DEFAULT_IMAGE = "releases-docker.jfrog.io/jfrog/jfrog-cli-v2:2.42.0"

# The OpenShift web console the cleaner's CronJob and ConfigMap live in. NOT this
# portal's own cluster: cleaners are applied to a different one, and the firewall
# between them means this backend can neither read nor delete those objects. A link
# is the whole of what it can offer, so the link has to be right.
# Set OPENSHIFT_CONSOLE_URL to your cluster's console. Empty means the portal
# names the objects and their namespace instead of linking to them -- a link to a
# console that is not yours is worse than no link.
DEFAULT_CONSOLE = ""

# 5 fields, each a cron token. Deliberately permissive about the token itself and
# strict about the shape: the shape is what turns a CronJob into an admission error.
_CRON_RE = re.compile(r"^\s*([^\s]+\s+){4}[^\s]+\s*$")

# Artifactory repository keys are alphanumerics with - and _ (and . in some setups).
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_DAY_NAMES = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday",
              4: "Thursday", 5: "Friday", 6: "Saturday"}


def namespace() -> str:
    return (os.getenv("ADO_CLEANER_NAMESPACE") or DEFAULT_NAMESPACE).strip()


def image() -> str:
    return (os.getenv("ADO_CLEANER_IMAGE") or DEFAULT_IMAGE).strip()


def artifactory_url() -> str:
    """The URL the CronJob passes to the JFrog CLI.

    Written into the file literally, the way the existing cleaners have it, rather
    than read from the secret at run time -- the file is reviewed in a pull request,
    and a reviewer can only check a URL that is visible in it.
    """
    configured = (os.getenv("ADO_CLEANER_ARTIFACTORY_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    base = (os.getenv("ARTIFACTORY_BASE_URL") or "").strip().rstrip("/")
    return base


def _int_in_range(value: Any, label: str, low: int, high: int, *, allow_blank: bool = True) -> Optional[int]:
    raw = str(value if value is not None else "").strip()
    if not raw:
        if allow_blank:
            return None
        raise ValueError(f"{label} is required.")
    try:
        number = int(raw)
    except ValueError:
        raise ValueError(f"{label} must be a whole number.")
    if number < low or number > high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return number


def validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise and check a cleaner request. Raises ValueError with one message."""
    from ado_repo import slugify_folder

    name = str(payload.get("cleaner_name") or "").strip()
    if not name:
        raise ValueError("A name for the cleaner is required.")
    slug = str(payload.get("slug") or "").strip() or slugify_folder(name)

    repo = str(payload.get("repository") or "").strip()
    if not repo:
        raise ValueError("An Artifactory repository is required.")
    if not _REPO_RE.match(repo):
        raise ValueError(f"'{repo}' is not a valid Artifactory repository key.")

    older_than = _int_in_range(payload.get("older_than_days"), "The age in days", 1, 3650)
    keep_last = _int_in_range(payload.get("keep_last"), "The number to keep", 1, 10000)
    name_pattern = str(payload.get("name_pattern") or "").strip()
    path_pattern = str(payload.get("path_pattern") or "").strip()
    # A LIST or a comma-separated string, because this validator runs twice on the
    # same request and has to accept its own output. The form sends "dump, zip"; that
    # is stored as ["dump"] in the payload, and the executor validates the payload
    # again before rendering -- at which point str(["dump"]) became "['dump']" and the
    # spec was given a file type of literally .['dump'], which matches nothing. A
    # cleaner that silently deletes nothing is the quiet half of the same bug that
    # would delete the wrong thing.
    raw_types = payload.get("file_types") or ""
    if isinstance(raw_types, (list, tuple, set)):
        candidates = [str(t) for t in raw_types]
    else:
        candidates = re.split(r"[,\s]+", str(raw_types).strip())
    file_types = [t.strip().lstrip(".").lower() for t in candidates if str(t).strip()]

    # No rule at all selects the entire repository. Refused, loudly: this is the one
    # mistake in this form that cannot be walked back once the job has run.
    if older_than is None and keep_last is None and not name_pattern and not file_types:
        raise ValueError(
            "A cleaner needs at least one rule -- an age, a name pattern, a file type "
            "or a number to keep. Without one it would delete the whole repository."
        )

    schedule = str(payload.get("schedule") or DEFAULT_SCHEDULE).strip()
    if not _CRON_RE.match(schedule):
        raise ValueError(
            f"'{schedule}' is not a cron schedule. It needs five fields, "
            f"for example '{DEFAULT_SCHEDULE}' (every day at 22:00)."
        )

    return {
        "cleaner_name": name,
        "slug": slug,
        "repository": repo,
        "older_than_days": older_than,
        "keep_last": keep_last,
        "name_pattern": name_pattern,
        "path_pattern": path_pattern,
        "file_types": file_types,
        "schedule": schedule,
    }


def names(slug: str) -> Dict[str, str]:
    """Every resource name a cleaner needs, from its slug."""
    return {
        "folder": slug,
        "configmap": f"{slug}-cleaner-spec",
        "cronjob": f"{slug}-cleaner",
        "volume": f"{slug}-spec",
        "spec_file": f"{slug}-spec.json",
    }


def console_url() -> str:
    return (os.getenv("OPENSHIFT_CONSOLE_URL") or DEFAULT_CONSOLE).strip().rstrip("/")


def cluster_objects(slug: str) -> List[Dict[str, str]]:
    """The two things a cleaner leaves behind on the cluster, each with a link to it.

    THIS IS THE WHOLE OF THE REMOVAL STORY ON THE CLUSTER SIDE, and it is worth being
    plain about why. Merging the removal pull request deletes the cleaner's files from
    git and the portal deletes its pipeline, and after both of those the CronJob is
    still there, still holding its ConfigMap, still deleting artifacts every night on
    the schedule somebody agreed to end. Nothing in git deletes a live object; only
    something talking to the cluster can, and a firewall stands between this portal's
    cluster and that one.

    So the portal does not pretend. It names the two objects exactly, links straight
    to each one in the console, and refuses to call the removal finished until a human
    says they are gone. The alternative -- marking it removed and saying nothing -- is
    a nightly DELETE job running against Artifactory with no file anywhere explaining
    why it exists.

    The `oc` line is there for whoever is already in a terminal. It is one command
    with both objects, because two commands is two chances to run one and stop.
    """
    ident = names(slug)
    space = namespace()
    base = console_url()

    def _link(kind: str, name: str, what: str, path: str) -> Dict[str, str]:
        # NO CONSOLE CONFIGURED, NO LINK. An empty OPENSHIFT_CONSOLE_URL would make
        # this a root-relative href, which resolves against the PORTAL's own host --
        # a link that looks live, opens this application, and teaches whoever clicks
        # it that the objects are already gone. The name and namespace are what the
        # operator actually needs; `cluster_command` below still does the work.
        row = {"kind": kind, "name": name, "what": what, "namespace": space}
        if base:
            row["url"] = f"{base}/k8s/ns/{space}/{path}/{name}"
        return row

    return [
        _link("CronJob", ident["cronjob"],
              "the schedule that runs the cleanup", "cronjobs"),
        _link("ConfigMap", ident["configmap"],
              "the rules saying what it deletes", "configmaps"),
    ]


def cluster_command(slug: str) -> str:
    """Both objects in one `oc delete`, for whoever prefers a terminal."""
    ident = names(slug)
    return (
        f"oc delete cronjob/{ident['cronjob']} configmap/{ident['configmap']} "
        f"-n {namespace()}"
    )


def _spec_json(request: Dict[str, Any]) -> str:
    """The file spec, built as data and serialised -- never string-formatted.

    This is the document that decides what gets deleted; assembling it by hand is how
    an unbalanced brace or an unescaped pattern turns into a spec that matches more
    than it should.
    """
    conditions: List[Dict[str, Any]] = []
    if request["older_than_days"] is not None:
        conditions.append({"created": {"$before": f"{request['older_than_days']}d"}})
    if request["file_types"]:
        # $or inside the $and: any one of the extensions, all of the other rules.
        conditions.append({"$or": [{"name": {"$match": f"*.{ext}"}} for ext in request["file_types"]]})

    find: Dict[str, Any] = {"repo": request["repository"]}
    if request["name_pattern"]:
        find["name"] = {"$match": request["name_pattern"]}
    if request["path_pattern"]:
        find["path"] = {"$match": request["path_pattern"]}
    if conditions:
        find["$and"] = conditions

    entry: Dict[str, Any] = {"aql": {"items.find": find}}
    if request["keep_last"] is not None:
        # Newest first, then skip that many. What is left is what gets deleted, so the
        # newest keep_last artifacts are the ones never selected.
        entry["sortBy"] = ["created"]
        entry["sortOrder"] = "desc"
        entry["offset"] = request["keep_last"]

    return json.dumps({"files": [entry]}, indent=4)


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def render_configmap(request: Dict[str, Any]) -> str:
    ident = names(request["slug"])
    spec = _spec_json(request)
    return (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        f"  name: {ident['configmap']}\n"
        f"  namespace: {namespace()}\n"
        "data:\n"
        f"  # {summary(request)}\n"
        f"  {ident['spec_file']}: |\n"
        f"{_indent(spec, 4)}\n"
    )


def render_cronjob(request: Dict[str, Any]) -> str:
    ident = names(request["slug"])
    return (
        "apiVersion: batch/v1\n"
        "kind: CronJob\n"
        "metadata:\n"
        f"  name: {ident['cronjob']}\n"
        f"  namespace: {namespace()}\n"
        "spec:\n"
        f"  # {schedule_text(request['schedule'])}\n"
        f"  schedule: \"{request['schedule']}\"\n"
        "  jobTemplate:\n"
        "    spec:\n"
        "      template:\n"
        "        spec:\n"
        "          containers:\n"
        "            - name: cleanup-artifactory\n"
        f"              image: '{image()}'\n"
        "              command: [\"/bin/sh\", \"-c\"]\n"
        "              args:\n"
        "                - |\n"
        "                  export JFROG_CLI_HOME_DIR=\"/tmp/.jfrog\"\n"
        "\n"
        f"                  # {summary(request)}\n"
        f"                  jf rt del --spec=/etc/config/{ident['spec_file']} "
        "--user=\"$ARTIFACTORY_USER\" --password=\"$ARTIFACTORY_PASSWORD\" "
        f"--url=\"{artifactory_url()}\" --insecure-tls\n"
        "              env:\n"
        "                - name: ARTIFACTORY_URL\n"
        "                  valueFrom:\n"
        "                    secretKeyRef:\n"
        "                      name: artifactory-credentials\n"
        "                      key: url\n"
        "                - name: ARTIFACTORY_USER\n"
        "                  valueFrom:\n"
        "                    secretKeyRef:\n"
        "                      name: artifactory-credentials\n"
        "                      key: user\n"
        "                - name: ARTIFACTORY_PASSWORD\n"
        "                  valueFrom:\n"
        "                    secretKeyRef:\n"
        "                      name: artifactory-credentials\n"
        "                      key: password\n"
        "              volumeMounts:\n"
        f"                - name: {ident['volume']}\n"
        "                  mountPath: /etc/config\n"
        "          volumes:\n"
        f"            - name: {ident['volume']}\n"
        "              configMap:\n"
        f"                name: {ident['configmap']}\n"
        "          restartPolicy: OnFailure\n"
        "      backoffLimit: 5\n"
    )


def schedule_text(schedule: str) -> str:
    """A cron expression in words, for the file, the form and the request card.

    Written once and read everywhere, because the schedule is the other half of what
    a cleaner does and "0 3 * * 5" is not something anyone should have to decode
    twice.
    """
    parts = schedule.split()
    if len(parts) != 5 or not parts[0].isdigit() or not parts[1].isdigit():
        return f"On the schedule {schedule}."
    when = f"{int(parts[1]):02d}:{int(parts[0]):02d}"
    minute, hour, dom, month, dow = parts
    if (dom, month, dow) == ("*", "*", "*"):
        return f"Every day at {when}."
    if (dom, month) == ("*", "*") and dow.isdigit():
        return f"Every {_DAY_NAMES.get(int(dow) % 7, 'week')} at {when}."
    if (month, dow) == ("*", "*") and dom.isdigit():
        return f"On day {dom} of each month at {when}."
    return f"On the schedule {schedule}, at {when}."


def render_pipeline(request: Dict[str, Any]) -> str:
    ident = names(request["slug"])
    group = (
        os.getenv("ADO_CLEANER_VARIABLE_GROUP")
        or "OpenShift cronjob-updater Service Account Credentials"
    ).strip()
    pool = (os.getenv("ADO_CLEANER_POOL") or "DevOpsOrder").strip()
    base_branch = (os.getenv("ADO_CLEANER_BASE_BRANCH") or "main").strip()
    return (
        "trigger:\n"
        "  branches:\n"
        "    include:\n"
        f"      - {base_branch}\n"
        "\n"
        "  paths:\n"
        "    include:\n"
        f"      - {ident['folder']}\n"
        "\n"
        "variables:\n"
        f"  - group: '{group}'\n"
        "\n"
        f"pool: {pool}\n"
        "\n"
        "steps:\n"
        "  - template: '../template.yaml'\n"
        "    parameters:\n"
        f"      directoryName: {ident['folder']}\n"
    )


def render_files(request: Dict[str, Any], *, include_pipeline: bool = True) -> Dict[str, str]:
    """Repo-absolute path -> content.

    An EDIT leaves pipeline.yaml alone: it only names the folder and the template, so
    it never changes, and rewriting an identical file makes a pull request that looks
    like it touched something it did not.
    """
    folder = names(request["slug"])["folder"]
    files = {
        f"/{folder}/configmap.yaml": render_configmap(request),
        f"/{folder}/cronjob.yaml": render_cronjob(request),
    }
    if include_pipeline:
        files[f"/{folder}/pipeline.yaml"] = render_pipeline(request)
    return files


def summary(request: Dict[str, Any]) -> str:
    """One sentence describing exactly what this cleaner deletes.

    Used by the form, the request card, the approval screen, the pull request and the
    files themselves -- one sentence from one place, so the description a reviewer
    reads cannot drift from the spec it describes.
    """
    rules: List[str] = []
    if request["older_than_days"] is not None:
        rules.append(f"older than {request['older_than_days']} day(s)")
    if request["file_types"]:
        rules.append("of type " + ", ".join("." + t for t in request["file_types"]))
    if request["name_pattern"]:
        rules.append(f"named like {request['name_pattern']}")
    if request["path_pattern"]:
        rules.append(f"under paths matching {request['path_pattern']}")
    where = request["repository"]
    what = " and ".join(rules) if rules else "everything"
    keep = ""
    if request["keep_last"] is not None:
        keep = f", keeping the newest {request['keep_last']}"
    return f"Delete artifacts in {where} {what}{keep}. {schedule_text(request['schedule'])}"
