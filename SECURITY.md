# Security Policy

`hft_platform` connects to live brokers and can place real orders, so security
reports are handled as a priority.

## Reporting a vulnerability

Use GitHub's private reporting:
<https://github.com/Charliesj0129/subhft/security/advisories/new>

Do **not** open a public issue, PR or discussion for anything involving
credentials, broker sessions, order routing, risk limits or the recorder/WAL.

Include the affected commit, how to reproduce it, and the impact you expect.
Redact any real account IDs, API keys, certificates or market data you hold.

## Scope

In scope: code, CI workflows, container images and configuration in this
repository, including the broker adapters (`feed_adapter/`), gateway/risk/order
path, recorder, and ops scripts.

Out of scope: vulnerabilities in upstream services or SDKs (report those to the
vendor; note the affected pinned version if it is one we ship), and findings that
need physical or already-privileged access to the production host.

## Secrets handling

- Secrets live only in `.env` or environment variables. Never in code, logs, CLI
  arguments or commits. The CI `security` job scans every PR with gitleaks
  (`.gitleaks.toml`).
- If a secret was committed, treat it as compromised: rotate it first, then
  remove it from history.

## Supported versions

Only `main` is supported. Fixes land there and are deployed through the normal
change-control path; there are no maintained release branches.
