---
name: hft-release-gate
description: "Release-readiness check rolling quality, safety, and operational gates into one pass/fail report, plus a seven-plane runtime audit. Use before proposing a deploy or merge with runtime changes, or after an incident. Not for performing the deploy (rules/41-deployment.md)."
---

# Release gate

This produces evidence; it never deploys, enables live trading, or pushes. A
deploy follows `.agent/rules/41-deployment.md` with the user's per-batch
approval. The live registry is frozen (`r47_tmf_v1`, loop_v1 L11): enabling any
other strategy is a red line.

## Gates

| # | Gate | Command | Pass |
|---|---|---|---|
| 1 | Code quality | `make check` | format, lint, typecheck, discipline, dependency-boundary, hygiene all green |
| 2 | Tests and coverage | `make coverage` | >= 70% line; hot-path files >= 90% (`make coverage-html`) |
| 3 | Test hygiene | `make test-assertion-check`, `test-name-check`, `test-quality-pattern-check` | clean |
| 4 | Architecture | `make arch-gate`, `make dependency-boundary` | no forbidden imports |
| 5 | Security | `make security-audit`; no secrets in the diff; no new `type: ignore` without a reason | clean |
| 6 | Runtime health | `make pre-market-check` (on the target host's environment, not only CI) | healthy |
| 7 | Latency | `make hotpath-profile`, `make benchmark-compare` | no stage regresses > 20% |
| 8 | Drift | `make deploy-drift-snapshot` before, `make deploy-drift-check` after | no unexplained diff |

One-shot for the automated part: `make ci && make pre-market-check && make hotpath-profile`.

Also check CI on `main` is green including scheduled runs, not only the PR
(`gh run list`): a green merge proves nothing about a scheduled run.

## Before a strategy runs beyond sim

At least one full session in shadow, with a latency profile in
`config/research/latency_profiles.yaml`, a conservative `max_pos`, a documented
rollback, and `docs/runbooks/live-trading-activation-sop.md` followed. All of it
needs the user's explicit instruction.

## Avoid

Skipping gate 6 for "code-only" changes (config drifts); relying on CI alone;
releasing without a recovery window before the next session (Friday, before
holidays); counting a green run as proof of runtime behavior without the
named metrics.

## Output

```
Release readiness: N/8 gates PASS
| gate | command | result |
```
List gates NOT run and why; verdict SHIP / HOLD / BLOCKED.

## Done when

Every gate is PASS, FAILED (with output), or NOT RUN (with reason).

## References

- `references/plane-audit.md` — read for the 7-plane runtime safety sweep after an incident, before a go-live window, or for the quarterly audit.
