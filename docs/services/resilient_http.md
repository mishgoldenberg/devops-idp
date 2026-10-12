# `resilient_http` (cross-cutting)

File: `backend/app/resilient_http.py`

Every outbound HTTP call the backend makes goes through this module's client;
`scripts/check_security_rules.py` fails the build on an `httpx.Client`, `httpx.AsyncClient`
or module-level `httpx.get/post/...` anywhere else.

- `Client(**kwargs)` / `AsyncClient(**kwargs)` — `httpx.Client` / `httpx.AsyncClient`
  that verify TLS as configured (unless `verify=` is passed) and refuse, before
  anything is sent, an address whose path holds a `.` or `..` segment, raw or
  percent-encoded (`%2e%2e`, `..%2F`, `%252e%252e`). httpx itself collapses
  `/table/incident/../../sys_user` to `/sys_user` without a word, so a value placed in
  a path could otherwise reach any endpoint the account can. The query string is not
  checked: a path inside a query parameter is data. A refusal raises `UnsafeURL`
  (a `ValueError`) and is logged at WARNING with the address.
- `far_message(resp) -> str` — the other system's own error message from a JSON answer
  (`message`, `error.message`, `errors[0].message`), at most 200 characters, or `""`.
  Never the raw body, which can be an HTML page or echo the request.
- `tls_verify() -> bool` — whether outbound calls verify certificates
  (`INTEGRATION_TLS_VERIFY`, default true).
- `explain_integration_failure(integration, exc) -> str` — one sentence a person can
  act on: the system, the host where known, the likely cause (credentials, wrong
  address, DNS, certificate, firewall, timeout) and who can fix it. Never the
  exception's class or text. Callers use it through `common.failure_text`, which logs
  the class and text at WARNING.

Values that go INTO another system's query language have their own helpers in
`common.py`: `wiql_text` (Azure DevOps WIQL), `quoted_text` (Confluence CQL,
Artifactory AQL) and `snow_catalog.query_value` (ServiceNow encoded queries, where `^`
would add a condition).
