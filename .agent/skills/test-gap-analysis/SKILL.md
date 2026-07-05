---
name: test-gap-analysis
description: "Map a module's behaviors against what tests actually assert, ranked by money/latency risk. Use before test-writer delegation, after a bug reveals a coverage hole, or when a module's risk tier rises."
---

# Skill: test-gap-analysis

## When to use
Before test-writer delegation; after a bug reveals a coverage hole; when a
module's risk tier rises (e.g., becomes hot-path).

## Required inputs
Target module path(s); recent related incidents/bugs if any.

## Procedure
1. Map behaviors: read the module; list public behaviors, state transitions,
   failure paths, and config branches (env-var toggles are behaviors too).
2. Map tests: `rg` for the module across `tests/`; note which behaviors each
   test actually asserts (not just executes).
3. Run `uv run pytest <tests> --no-cov -q` to confirm the baseline is green.
4. Optional: targeted coverage run `uv run pytest <tests> --cov=<module>`.
5. Diff behaviors vs assertions. Prioritize: money/precision paths >
   fail-closed paths > state transitions > happy paths.
6. Check HFT-specific gaps: scaled-int boundaries, monotonic time, one-sided
   books, zero prices, queue overflow, thread-handoff.

## Safety rules
Read-only plus test runs. Do not write tests in this skill (hand off).

## Output format
Table: behavior | tested? | asserting test(s) | gap severity | suggested test
name (`test_<behavior>_<scenario>`), followed by a prioritized top-5 list.

## Validation checklist
- [ ] Every "tested" claim names the asserting test
- [ ] Failure paths and config branches included, not just happy paths
- [ ] Priorities reflect money/latency risk

## Example prompt
"test-gap-analysis on gateway/service.py — the 7-step pipeline; I want gaps
ranked before handing to a test-writer."
