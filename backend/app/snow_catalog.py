"""
Submit a ServiceNow CATALOG ITEM and get a RITM back.

The Support page submits a Record Producer, and a record producer on this instance
inserts an INCIDENT (INC...). The DevOps Support requests are catalog items and the
team works them as requested items, so these go through order_now instead, which
creates a REQ with one RITM under it.

This module refuses to report success for anything that came back as an incident.
That guard is the whole reason it exists as a separate path rather than another
branch inside the ticket flow: the two endpoints differ by one word in the URL, and
the difference is invisible until somebody goes looking for a RITM that is an INC.

Item sys_ids come from the environment when set, and are otherwise resolved by exact
name -- both attempts logged with their HTTP status, because "no item found" and
"401" are different problems and an empty result makes them identical.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from resilient_http import tls_verify

log = logging.getLogger(__name__)


def _instance() -> str:
    base = (os.getenv("SNOW_BASE_URL") or "").strip().rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    return base


def is_configured() -> bool:
    return bool(
        _instance()
        and (os.getenv("SNOW_API_USERNAME") or "").strip()
        and (os.getenv("SNOW_API_PASSWORD") or "")
    )


def _client(timeout: float = 30.0) -> httpx.Client:
    if not is_configured():
        raise RuntimeError(
            "ServiceNow is not configured. Set SNOW_BASE_URL, SNOW_API_USERNAME and "
            "SNOW_API_PASSWORD on the backend."
        )
    return httpx.Client(
        verify=tls_verify(),
        base_url=_instance(),
        auth=(
            (os.getenv("SNOW_API_USERNAME") or "").strip(),
            os.getenv("SNOW_API_PASSWORD") or "",
        ),
        headers={"Accept": "application/json"},
        timeout=httpx.Timeout(timeout, connect=5.0),
        follow_redirects=True,
    )


# ── Resolving an item ────────────────────────────────────────────────────────

def _looks_like_sys_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", str(value or "").strip()))


def resolve_item(name: str, env_var: str) -> str:
    """The catalog item's sys_id: the environment variable if it holds one, else the
    active item whose name matches exactly.

    Name matching is exact and case-insensitive, never "first result". A catalog
    search for "Enlarge Quota in Artifactory" also returns every item that mentions
    Artifactory, and ordering the wrong one is a request the team cannot even trace
    back to the person who filed it.
    """
    configured = (os.getenv(env_var) or "").strip()
    if _looks_like_sys_id(configured):
        return configured
    if configured:
        log.warning("%s is set but is not a sys_id (%r) -- resolving by name", env_var, configured[:40])

    wanted = str(name or "").strip().lower()
    with _client() as client:
        # The Table API is the exact one; it is also the one most likely to be
        # ACL-blocked for a service account, so its status is logged either way.
        table = client.get(
            "/api/now/table/sc_cat_item",
            params={
                "sysparm_query": f"name={name}^active=true",
                "sysparm_fields": "sys_id,name",
                "sysparm_limit": "5",
            },
        )
        rows = (table.json().get("result") or []) if table.status_code == 200 else []
        log.warning(
            "SNow catalog lookup (table): item=%r status=%s rows=%s",
            name, table.status_code, len(rows),
        )
        for row in rows:
            if str(row.get("name") or "").strip().lower() == wanted:
                return str(row.get("sys_id") or "")

        # Catalog search: readable by anyone who can order, so it works where the
        # table read does not.
        search = client.get(
            "/api/sn_sc/servicecatalog/items",
            params={"sysparm_text": name, "sysparm_limit": "20"},
        )
        found = (search.json().get("result") or []) if search.status_code == 200 else []
        log.warning(
            "SNow catalog lookup (sn_sc): item=%r status=%s rows=%s",
            name, search.status_code, len(found),
        )
        for row in found:
            if str(row.get("name") or "").strip().lower() == wanted:
                return str(row.get("sys_id") or "")

    raise RuntimeError(
        f"ServiceNow has no active catalog item named '{name}' that this account can "
        f"see. Set {env_var} to its sys_id if the name has changed."
    )


# A container is a layout box, not an input. Its NAME is never a variable ServiceNow
# accepts a value for, and sending one is a variable the order silently ignores.
_CONTAINER_TYPES = {"19", "20", "25", "26"}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _flatten(variables: Any) -> List[Dict[str, Any]]:
    """Every input variable in the tree, containers unwrapped.

    A catalog item lays its questions out in containers, and the API returns the tree:
    a Container Start with the real fields nested under ``children``. Reading only the
    top level therefore finds a handful of boxes and none of the questions inside
    them, so nothing maps, every mandatory variable arrives empty, and ServiceNow
    answers "Mandatory Variables are required" -- naming none of them, because from
    its side they simply were not sent.

    The Support ticket path has flattened since the day it was written, which is the
    entire reason Support tickets go through on this instance and these did not. Two
    modules read the same catalog API and only one of them knew the shape of it.
    """
    out: List[Dict[str, Any]] = []
    for variable in variables or []:
        if not isinstance(variable, dict):
            continue
        out.append(variable)
        if variable.get("children"):
            out.extend(_flatten(variable["children"]))
    return out


def item_variables(sys_id: str) -> List[Dict[str, Any]]:
    """The item's variable definitions (name, label, mandatory, type, choices).

    Ordering over REST enforces the dictionary-level mandatory flag and ignores UI
    policies, so a variable that is hidden in the ServiceNow form still has to carry
    a value here -- which is only knowable by reading the definitions first.
    """
    with _client() as client:
        resp = client.get(f"/api/sn_sc/servicecatalog/items/{sys_id}")
        log.warning("SNow catalog item read: sys_id=%s status=%s", sys_id, resp.status_code)
        if resp.status_code >= 400:
            return []
        result = (resp.json() or {}).get("result") or {}
    raw = _flatten(result.get("variables"))
    log.warning(
        "SNow catalog item %s declares %s variable(s) (%s at the top level)",
        sys_id, len(raw), len(result.get("variables") or []),
    )
    specs: List[Dict[str, Any]] = []
    for variable in raw:
        if str(variable.get("type") or "").strip() in _CONTAINER_TYPES:
            continue
        name = str(variable.get("name") or "").strip()
        if not name:
            continue
        specs.append({
            "name": name,
            "label": str(variable.get("label") or name),
            # Never bool(): this API answers with the STRING "false", and bool("false")
            # is True. Read that way every variable looks mandatory, and every one of
            # them gets filled with a made-up value.
            "mandatory": _truthy(variable.get("mandatory")),
            "type": variable.get("type"),
            "choices": variable.get("choices") or [],
            # The item's own default. A mandatory variable that ships with one is
            # already answered; sending nothing for it turns a working order into
            # "Mandatory variables are required" over a value ServiceNow itself
            # would have used.
            "default": str(
                variable.get("value")
                or variable.get("default_value")
                or variable.get("displayvalue")
                or ""
            ).strip(),
        })
    return specs


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


# Words that carry no identity in a field label. "The pipeline's purpose" and
# "Pipeline's Purpose" are the same question; "the" is the only thing between them.
_NOISE = {"the", "a", "an", "of", "for", "your", "please", "and", "is", "s"}


def _tokens(text: str) -> frozenset:
    """The meaningful words in a name or label, lowercased.

    The possessive is what this exists for. A field called pipeline_purpose and a
    variable labelled "Pipeline's Purpose" normalise to pipelinepurpose and
    pipelinespurpose -- one character apart, and not equal, so the answer somebody
    typed was dropped and the variable kept the item's own default. Nothing said so:
    the RITM simply had the wrong text in it.
    """
    words = re.split(r"[^a-z0-9]+", str(text or "").lower())
    return frozenset(w for w in words if w and w not in _NOISE)


def user_sys_id(email: str) -> str:
    """The sys_user this e-mail belongs to, or "" when it cannot be resolved.

    Needed because a catalog order is placed BY the service account. Without saying
    who it is for, every requested item lands with the integration account in
    "Requested for" -- so the team sees one person raising everything and the
    requester cannot find their own item. The Support path resolves the caller the
    same way before creating an incident.

    Never raises: a requested item under the service account is worse than one under
    the right person and far better than none at all.
    """
    wanted = str(email or "").strip()
    if not wanted:
        return ""
    try:
        with _client() as client:
            for query in (f"email={wanted}", f"user_name={wanted}",
                          f"user_name={wanted.split('@')[0]}"):
                resp = client.get(
                    "/api/now/table/sys_user",
                    params={"sysparm_query": query, "sysparm_fields": "sys_id,email,user_name",
                            "sysparm_limit": "1"},
                )
                rows = (resp.json().get("result") or []) if resp.status_code == 200 else []
                log.warning("SNow user lookup: %s status=%s rows=%s",
                            query, resp.status_code, len(rows))
                if rows and rows[0].get("sys_id"):
                    return str(rows[0]["sys_id"])
    except Exception as exc:
        log.warning("SNow user lookup failed for %r: %s: %s", wanted, type(exc).__name__, exc)
    return ""


def map_variables(
    specs: List[Dict[str, Any]],
    fields: Dict[str, Any],
    *,
    dropped_into: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Hub field keys -> the item's real variable names.

    Matched on the variable's name first and its label second, both normalised, so a
    field called pipeline_type finds a variable named pipeline_type OR labelled
    "Pipeline Type". Anything unmatched is dropped rather than sent under a guessed
    name, and what was dropped is logged: a variable ServiceNow does not know is
    silently ignored by order_now, so an unlogged mismatch is a field that vanishes.
    """
    by_key: Dict[str, str] = {}
    for spec in specs:
        by_key.setdefault(_norm(spec["name"]), spec["name"])
        by_key.setdefault(_norm(spec["label"]), spec["name"])
        # ServiceNow custom variables are conventionally u_-prefixed, and this
        # portal's fields are not. Nothing matched u_full_name, and the label that
        # would have caught it is written in Hebrew -- so a form that asks for a full
        # name sent nothing to the variable that demands one.
        if spec["name"].lower().startswith("u_"):
            by_key.setdefault(_norm(spec["name"][2:]), spec["name"])

    # Third pass, on WORDS rather than on the squashed string. Two names made of the
    # same words are the same question however they are punctuated, which is what
    # "pipeline_purpose" and "Pipeline's Purpose" are. Only ever accepted when exactly
    # one variable matches: an ambiguous guess puts an answer in the wrong box, which
    # is worse than leaving it out.
    by_words: Dict[frozenset, List[str]] = {}
    for spec in specs:
        for text in (spec["name"], spec["label"]):
            by_words.setdefault(_tokens(text), []).append(spec["name"])

    out: Dict[str, str] = {}
    dropped: List[str] = []
    for key, value in (fields or {}).items():
        target = by_key.get(_norm(key))
        if not target:
            candidates = {n for n in by_words.get(_tokens(key), [])}
            if len(candidates) == 1:
                target = candidates.pop()
                log.warning("SNow catalog: %r matched variable %r on its words", key, target)
        if not target:
            dropped.append(key)
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        out[target] = "" if value is None else str(value)
    if dropped:
        log.warning("SNow catalog: no variable for %s (dropped)", dropped)
    if dropped_into is not None:
        # Handed back rather than only logged. One matching implementation decides
        # what was dropped, and the caller shows it -- a second pass that worked it
        # out again would be a second chance to disagree with this one.
        dropped_into.extend(dropped)
    return out


# ── Ordering ─────────────────────────────────────────────────────────────────

class OrderRejected(RuntimeError):
    """ServiceNow refused the order, with the status and what was sent attached.

    A plain RuntimeError carrying a formatted sentence was enough to display and
    useless to act on: "Mandatory Variables are required" names no variable, so the
    one question worth answering -- WHICH ones -- had to be reconstructed from the
    body text by whoever read it. Keeping the pieces means the caller can retry
    intelligently and the message can say what the portal actually sent.
    """

    def __init__(self, status: int, body: str, sent: List[str]) -> None:
        self.status = int(status)
        self.body = str(body or "")
        self.sent = list(sent or [])
        super().__init__(
            f"ServiceNow rejected the request (HTTP {self.status}): {self.body[:300]}"
        )

    @property
    def mandatory_complaint(self) -> bool:
        return "mandatory variable" in self.body.lower()


def order(
    sys_id: str,
    variables: Dict[str, str],
    *,
    quantity: int = 1,
    requested_for: str = "",
) -> Dict[str, Any]:
    """Order the item and return the RITM it produced.

    order_now answers with the REQUEST (sc_request); the requested item is its child,
    so the RITM number is a second read. When that read is blocked the REQ number is
    still returned, with ritm_number empty -- never a REQ number relabelled as a RITM.
    """
    with _client() as client:
        body: Dict[str, Any] = {
            "sysparm_quantity": str(quantity),
            "variables": variables,
        }
        # WHO it is for. The order is placed by the integration account, so without
        # this every requested item shows the service account in "Requested for" --
        # the team sees one person raising everything and the requester cannot find
        # their own item. Omitted rather than sent empty when the user could not be
        # resolved, because an empty reference is rejected outright.
        if requested_for:
            body["sysparm_requested_for"] = requested_for
        resp = client.post(
            f"/api/sn_sc/servicecatalog/items/{sys_id}/order_now",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        log.warning(
            "SNow order_now: item=%s status=%s for=%s vars=%s body=%s",
            sys_id, resp.status_code, requested_for or "the service account",
            sorted(variables.keys()), resp.text[:400],
        )
        if resp.status_code >= 400:
            raise OrderRejected(resp.status_code, resp.text or "", sorted(variables.keys()))
        result = (resp.json() or {}).get("result") or {}

        table = str(result.get("table") or "sc_request")
        request_sys_id = str(result.get("sys_id") or "")
        request_number = str(result.get("number") or "")
        if not request_sys_id:
            raise RuntimeError("ServiceNow accepted the order but returned no record id.")

        # The one thing this module exists to prevent.
        if table == "incident" or request_number.startswith("INC"):
            raise RuntimeError(
                f"ServiceNow created an INCIDENT ({request_number or request_sys_id}) for "
                f"catalog item {sys_id}. These requests must be requested items -- check "
                "that the item is a catalog item and not a record producer."
            )

        ritm_number = ""
        ritm_sys_id = ""
        try:
            child = client.get(
                "/api/now/table/sc_req_item",
                params={
                    "sysparm_query": f"request={request_sys_id}",
                    "sysparm_fields": "number,sys_id",
                    "sysparm_limit": "1",
                },
            )
            rows = (child.json().get("result") or []) if child.status_code == 200 else []
            log.warning(
                "SNow RITM lookup: req=%s status=%s rows=%s",
                request_number or request_sys_id, child.status_code, len(rows),
            )
            if rows:
                ritm_number = str(rows[0].get("number") or "")
                ritm_sys_id = str(rows[0].get("sys_id") or "")
        except Exception as exc:
            log.warning("SNow RITM lookup failed: %s: %s", type(exc).__name__, exc)

        owner = _settle_requested_for(
            client, requested_for,
            [("sc_request", request_sys_id), ("sc_req_item", ritm_sys_id)],
        )

    return {
        "request_number": request_number,
        "request_sys_id": request_sys_id,
        "ritm_number": ritm_number,
        "ritm_sys_id": ritm_sys_id,
        # What to show a person: the RITM if we could read it, the REQ otherwise.
        "number": ritm_number or request_number,
        "table": table,
        "requested_for_ok": owner["ok"],
        "requested_for_detail": owner["detail"],
    }


# Setting it after the fact is opt-out-able, because a write by the service account
# is not free on this instance: on the incident side that same PATCH re-triggers the
# assignment rule and flips a new ticket straight to In Progress. Requested items have
# different rules, and the read-back below proves whether this one behaved -- but if
# it ever misbehaves, turning it off must not need a code change.
def _may_patch_requested_for() -> bool:
    return (os.getenv("SNOW_PATCH_REQUESTED_FOR") or "1").strip().lower() not in ("0", "false", "no")


def _settle_requested_for(
    client: httpx.Client, wanted: str, records: List[tuple]
) -> Dict[str, Any]:
    """Make sure "Requested for" is the person, not the integration account.

    sysparm_requested_for is sent on the order and this instance ignores it: the log
    showed the right sys_id going out and the requested item still arrived owned by
    the service account, because order_now only honours it when the catalog is
    configured to ask -- and it is not.

    So the value is READ BACK and corrected if it is wrong, then read back AGAIN. That
    second read is the whole point: "we sent it" was already true when the field was
    still wrong, so a write nobody verifies would report exactly the same success this
    is replacing.
    """
    if not wanted:
        return {"ok": False, "detail": "the requester could not be resolved to a ServiceNow user"}

    problems: List[str] = []
    fixed: List[str] = []
    for table, sys_id in records:
        if not sys_id:
            continue
        try:
            current = _read_requested_for(client, table, sys_id)
            if current == wanted:
                continue
            if not _may_patch_requested_for():
                problems.append(f"{table} is {current or 'unset'} (SNOW_PATCH_REQUESTED_FOR is off)")
                continue
            resp = client.patch(
                f"/api/now/table/{table}/{sys_id}",
                json={"requested_for": wanted},
                headers={"Content-Type": "application/json"},
            )
            log.warning("SNow requested_for PATCH: %s/%s status=%s", table, sys_id, resp.status_code)
            after = _read_requested_for(client, table, sys_id)
            if after == wanted:
                fixed.append(table)
            else:
                problems.append(
                    f"{table} still reads {after or 'unset'} after HTTP {resp.status_code}"
                )
        except Exception as exc:
            problems.append(f"{table}: {type(exc).__name__}: {exc}")

    if fixed:
        log.warning("SNow requested_for corrected on %s -> %s", ", ".join(fixed), wanted)
    if problems:
        log.warning("SNow requested_for NOT set: %s", "; ".join(problems))
        return {"ok": False, "detail": "; ".join(problems)}
    return {"ok": True, "detail": ""}


def _read_requested_for(client: httpx.Client, table: str, sys_id: str) -> str:
    resp = client.get(
        f"/api/now/table/{table}/{sys_id}",
        params={"sysparm_fields": "requested_for", "sysparm_display_value": "false"},
    )
    if resp.status_code >= 400:
        return ""
    value = ((resp.json() or {}).get("result") or {}).get("requested_for")
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return str(value or "")


def variable_overrides(form_key: str) -> Dict[str, str]:
    """Operator-set answers for variables the form cannot fill by itself.

    SNOW_VARS_<FORM KEY> holds JSON of {variable_name: value}. A value beginning with
    "@" names one of the form's own fields ("@branch"); anything else is a literal.

    This exists because the item's variables live in ServiceNow and the form lives
    here, and the two are maintained by different people. When an item gains a
    mandatory variable this portal has never heard of -- a cost centre, a department,
    a Hebrew-labelled field that matches nothing by name -- every request to it fails
    with the same unhelpful 400, and the only fix should not be a code change shipped
    into an offline environment.
    """
    import json as _json

    raw = (os.getenv(f"SNOW_VARS_{re.sub(r'[^A-Z0-9]+', '_', str(form_key or '').upper())}") or "").strip()
    if not raw:
        return {}
    try:
        parsed = _json.loads(raw)
    except ValueError as exc:
        log.warning("SNOW_VARS_%s is not valid JSON (%s) -- ignored", form_key, exc)
        return {}
    if not isinstance(parsed, dict):
        log.warning("SNOW_VARS_%s is not an object -- ignored", form_key)
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


# Variable types that name another RECORD. Everything else can be given a benign
# value; these cannot, because the only thing ServiceNow accepts is a real sys_id and
# a fabricated one points the request at nothing.
_REFERENCE_TYPES = {"8", "21", "reference", "list", "lookup", "glide_list"}

# Yes/No and Checkbox. These are the only ones for which "true" is a real answer.
_BOOLEAN_TYPES = {"1", "7", "boolean", "checkbox", "yes_no"}


def fallback_for(spec: Dict[str, Any]) -> str:
    """A value that satisfies a mandatory variable this portal has no field for.

    Ordering over REST enforces the dictionary-level mandatory flag and skips the UI
    policies that hide such a variable for the service actually being requested -- so
    a variable nobody filling the form in has ever SEEN still has to carry a value,
    and there is no field to add that would make it visible.

    This is what the Support ticket path already does (see _default_value_for_spec in
    api/servicenow.py), on the same instance, for the same reason. It is why Support
    tickets go through while these did not: the wizard was never asked for a division
    either, it simply sent the item's own first choice.

    A choice variable gets its first real choice; a checkbox gets "true"; a text
    variable gets a phrase that reads as deliberate, because somebody works this
    ticket and "true" in a box labelled Full Name is worse than useless to them; and a
    reference gets nothing -- for that one, refusing is the honest answer.
    """
    for choice in spec.get("choices") or []:
        value = choice.get("value") if isinstance(choice, dict) else choice
        if value not in (None, ""):
            return str(value)
    kind = str(spec.get("type") or "").strip().lower()
    if kind in _REFERENCE_TYPES:
        return ""
    if kind in _BOOLEAN_TYPES:
        return "true"
    return "Not provided"


def submit(
    name: str,
    env_var: str,
    fields: Dict[str, Any],
    *,
    overrides: Optional[Dict[str, str]] = None,
    requester_email: str = "",
) -> Dict[str, Any]:
    """Resolve the item, map the fields onto its variables, order it.

    Every mandatory variable ends up with a value, by whichever of five routes gets
    there first: a form field that matches it, an operator's SNOW_VARS map, the item's
    own default, a single unambiguous near-match on the name, or the item's first
    choice. Only a mandatory REFERENCE variable can still come up empty, and that one
    is refused by name rather than sent as a guess.

    The order matters and so does the last step. ServiceNow answers a missing
    mandatory variable with "Mandatory Variables are required" and does not say which,
    so the failure is unactionable from the outside -- and the previous version, which
    refused rather than filling, turned that into an unactionable failure from the
    inside as well: the item asked for a division nobody had heard of, the form grew a
    field for it, and the field was wrong twice over.
    """
    sys_id = resolve_item(name, env_var)
    specs = item_variables(sys_id)
    dropped: List[str] = []
    variables = map_variables(specs, fields, dropped_into=dropped) if specs else {
        k: ("" if v is None else str(v)) for k, v in (fields or {}).items()
    }
    if not specs:
        log.warning(
            "SNow catalog: item %s exposed no variable definitions -- sending field "
            "keys unmapped", sys_id,
        )

    # An operator's answer wins over anything derived: it was set precisely because
    # the derivation was wrong.
    for variable, value in (overrides or {}).items():
        if value.startswith("@"):
            source = (fields or {}).get(value[1:])
            if source is None:
                continue
            variables[variable] = str(source)
        else:
            variables[variable] = value

    # Then the item's own defaults, for anything still blank.
    for spec in specs:
        if spec.get("default") and not str(variables.get(spec["name"], "")).strip():
            variables[spec["name"]] = spec["default"]

    # Last resort, and only for variables that would otherwise make this fail: a
    # near-match on the name. u_phone and phone_number are the same field with two
    # naming conventions, and refusing to see that means every request to the item
    # fails until somebody sets a variable by hand. Ambiguity is NOT resolved by
    # luck -- two candidates means no guess -- and every guess is logged, because a
    # value in the wrong variable is worse than an empty one.
    for spec in specs:
        if not spec.get("mandatory") or str(variables.get(spec["name"], "")).strip():
            continue
        wanted = _norm(spec["name"][2:] if spec["name"].lower().startswith("u_") else spec["name"])
        if len(wanted) < 4:
            continue
        hits = [
            (k, v) for k, v in (fields or {}).items()
            if v not in (None, "") and len(_norm(k)) >= 4
            and (wanted in _norm(k) or _norm(k) in wanted)
        ]
        if len(hits) != 1:
            continue
        log.warning(
            "SNow catalog: filling mandatory %r from field %r by name similarity",
            spec["name"], hits[0][0],
        )
        variables[spec["name"]] = str(hits[0][1])

    # Anything mandatory that is STILL blank gets the item's own idea of an answer.
    # Logged one variable at a time with the value used, because this is the portal
    # putting words in a requester's mouth: it is the right trade against failing a
    # request outright, and it is not something to do quietly.
    filled: List[str] = []
    for spec in specs:
        if not spec.get("mandatory") or str(variables.get(spec["name"], "")).strip():
            continue
        fallback = fallback_for(spec)
        if not fallback:
            continue
        variables[spec["name"]] = fallback
        filled.append(f"{spec['name']} ({spec['label']}) = {fallback!r}")
    if filled:
        log.warning(
            "SNow catalog: %s mandatory variable(s) filled from the item's own "
            "definition, because no form field answers them: %s",
            len(filled), "; ".join(filled),
        )

    # What is left can only be a mandatory REFERENCE variable: it names another
    # record, and the only value ServiceNow accepts is a real sys_id. Guessing one
    # would file the request against the wrong thing, which is worse than not filing
    # it, so this is the one case that still refuses -- by name, with the fix.
    missing = [
        s for s in specs
        if s.get("mandatory") and not str(variables.get(s["name"], "")).strip()
    ]
    if missing:
        named = ", ".join(f"{s['name']} ({s['label']})" for s in missing)
        log.warning("SNow catalog: mandatory variables with no value: %s", named)
        raise RuntimeError(
            f"'{name}' cannot be ordered: {len(missing)} mandatory variable(s) point "
            f"at another record and have no answer here - {named}. Set "
            f"SNOW_VARS_{re.sub(r'[^A-Z0-9]+', '_', str(name or '').upper())} to a "
            f"JSON object mapping each one to a sys_id."
        )
    for_user = user_sys_id(requester_email)
    if requester_email and not for_user:
        log.warning(
            "SNow catalog: %r has no sys_user, so this item will be raised under the "
            "service account", requester_email,
        )
    try:
        result = order(sys_id, variables, requested_for=for_user)
    except OrderRejected as rejected:
        if not rejected.mandatory_complaint:
            raise
        # ServiceNow says something mandatory is missing and will not say what. Its
        # own definitions said everything mandatory was answered, so the two disagree
        # -- which happens when the dictionary enforces a flag the API does not report
        # on the variable. Rather than hand back a 400 nobody can act on, fill every
        # declared variable that is still empty and try once more.
        #
        # ONCE, and only after this specific complaint: a retry loop against an order
        # endpoint is how one request becomes five requested items.
        second = dict(variables)
        added: List[str] = []
        for spec in specs:
            if str(second.get(spec["name"], "")).strip():
                continue
            value = spec.get("default") or fallback_for(spec)
            if not value:
                continue
            second[spec["name"]] = value
            added.append(f"{spec['name']} ({spec['label']})")
        if not added:
            raise RuntimeError(
                f"{rejected}. The portal sent {len(variables)} variable(s) "
                f"({', '.join(rejected.sent) or 'none'}) and '{name}' declares "
                f"{len(specs)}: {', '.join(s['name'] for s in specs) or 'none readable'}. "
                "Nothing further can be filled in from the item itself."
            ) from rejected
        log.warning(
            "SNow catalog: retrying '%s' with %s further variable(s) after a "
            "mandatory-variable complaint that named none: %s",
            name, len(added), "; ".join(added),
        )
        try:
            result = order(sys_id, second, requested_for=for_user)
        except OrderRejected as again:
            raise RuntimeError(
                f"{again}. Sent on the second attempt: {', '.join(again.sent)}. "
                f"'{name}' declares {len(specs)} variable(s): "
                f"{', '.join(s['name'] for s in specs) or 'none readable'}. "
                f"Mandatory by its own definition: "
                f"{', '.join(s['name'] for s in specs if s.get('mandatory')) or 'none'}."
            ) from again
        filled.extend(added)
    result["item_sys_id"] = sys_id
    result["requested_for"] = for_user
    # What the form answered that reached no variable at all. Logged already, but a
    # log line is not somewhere anybody looks: this is how "Pipeline's Purpose kept
    # the item's default" becomes visible without opening the RITM to notice it.
    result["unmapped_fields"] = sorted(
        key for key in dropped if str((fields or {}).get(key) or "").strip()
    )
    if filled:
        result["filled_from_item"] = filled
    return result


def attach(sys_id: str, table: str, filename: str, content_type: str, data: bytes) -> bool:
    """Attach one file to a record. Best-effort, and it says so in the log.

    The submission is already recorded and the RITM already exists by the time this
    runs, so a failed upload must not fail the request -- but it must not be silent
    either, or a diagram the team was told to expect simply is not there.
    """
    try:
        with _client(timeout=60.0) as client:
            resp = client.post(
                "/api/now/attachment/file",
                params={
                    "table_name": table,
                    "table_sys_id": sys_id,
                    "file_name": filename or "attachment",
                },
                headers={"Content-Type": content_type or "application/octet-stream"},
                content=data,
            )
        log.warning(
            "SNow attachment: record=%s/%s name=%r bytes=%s status=%s",
            table, sys_id, filename, len(data or b""), resp.status_code,
        )
        return resp.status_code < 400
    except Exception as exc:
        log.warning("SNow attachment failed for %s/%s: %s: %s", table, sys_id, type(exc).__name__, exc)
        return False
