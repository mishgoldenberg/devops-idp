# `resilient_http` (cross-cutting)

File: `backend/app/resilient_http.py`

Each integration builds its own `httpx.Client` with an explicit connect and read
timeout and handles its own responses. This module holds the two things they share:

- `tls_verify() -> bool` — whether outbound calls verify certificates
  (`INTEGRATION_TLS_VERIFY`, default true).
- `explain_integration_failure(integration, exc) -> str` — one sentence a person can
  act on: the system, the host where known, the likely cause (credentials, wrong
  address, DNS, certificate, firewall, timeout) and who can fix it. Never the
  exception's class or text; log those.
