# Security Policy

## Supported versions

This project is developed on `main`. Fixes land there; there are no long-lived
maintenance branches. If you are running an older build, the upgrade path is
forward.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting instead:

**[Security tab](https://github.com/mishgoldenberg/devops-idp/security/advisories/new) → Report a vulnerability**

That opens a private thread visible only to the maintainers. If the tab is not
available to you, open a normal issue titled "Security contact request" with no
detail in it, and a maintainer will follow up privately.

### What helps

- The version or commit you are running
- What an attacker gains — read another user's data, escalate a role, reach an
  integration credential, act as another account
- Steps to reproduce, or a request that demonstrates it
- Whether it needs an authenticated session, and at what role

### What to expect

- An acknowledgement within a few days
- An assessment of severity and scope, shared with you
- A fix on `main`, with credit in the release notes unless you prefer otherwise

Please give us a reasonable window to ship a fix before disclosing publicly.

## Scope

This portal holds credentials for the systems it integrates with, so the findings
that matter most are the ones that reach them:

| Area | Why it matters |
|---|---|
| **Authentication and sessions** | JWT handling, the `auth_token` cookie, OIDC callback validation, sign-out |
| **Authorization** | Anything that returns another user's data, or lets a role act above itself |
| **Stored credentials** | Integration secrets are encrypted at rest with a key derived from `JWT_SECRET`; anything that exposes one, in a response or a log, is in scope |
| **Self-service execution** | The approval path executes real changes against Azure DevOps and Artifactory — anything that lets a request execute without approval, or execute as somebody else |
| **Injection** | SQL, template, and anything that reaches an outbound request URL |

### Out of scope

- Findings that require an already-compromised administrator account
- Missing hardening headers on endpoints that serve no content
- Denial of service through volume alone
- Vulnerabilities in a deployment's own configuration — a blank `JWT_SECRET`, a
  disabled `INTEGRATION_TLS_VERIFY`, an ingress without TLS — rather than in this
  code

## Deploying it safely

A few settings decide most of the security posture of an installation:

- **`JWT_SECRET`** signs sessions *and* derives the key that encrypts stored
  integration credentials. Treat it as part of the data: back it up alongside the
  database, and do not rotate it casually — a new one makes every stored
  credential unreadable and signs everybody out.
- **`INTEGRATION_TLS_VERIFY`** defaults to `true`. Setting it to `false` disables
  certificate verification on every outbound integration call. If you have an
  internal CA, install it in the image instead.
- **`HUB_ADMIN_PASSWORD`** creates the bootstrap administrator. Change it after
  first sign-in and prefer OIDC for real accounts.
- **Ingress** should terminate TLS. The chart refuses to render an Ingress with an
  empty host, because a host-less Ingress matches every hostname.
