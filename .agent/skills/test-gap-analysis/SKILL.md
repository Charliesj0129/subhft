---
name: test-gap-analysis
description: "Map a module's behaviors against what its tests assert, ranked by money and latency risk, as input for writing tests. Use before delegating to hft-test-writer, after a bug exposes a coverage hole, or when a module's risk tier rises. Not for writing the tests."
---

# Test-gap analysis

Read-only plus test runs; hand the writing to `hft-test-writer`.

1. **Behaviors.** Read the module; list public behaviors, state transitions,
   failure paths, and config branches (env toggles are behaviors).
2. **Tests.** `rg` the module across `tests/`; note which behaviors a test
   asserts, not merely executes.
3. **Baseline.** Run the module's tests green first: `make test-file FILE=...`.
   Optional targeted coverage: `uv run pytest <tests> --cov=<module>`.
4. **Gaps.** Rank money and precision paths > fail-closed paths > state
   transitions > happy paths. HFT-specific holes to look for: scaled-int
   boundaries, monotonic time, one-sided books, zero prices, queue overflow,
   thread handoff, replay idempotence.

## Output

Table: behavior | tested? | asserting test(s) | gap severity | suggested name
(`test_<behavior>_<scenario>`), then a prioritized top 5.

## Done when

Every "tested" claim names the asserting test, failure paths and config
branches are included, and the ranking reflects money and latency risk.
