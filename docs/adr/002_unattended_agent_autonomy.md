# ADR-002: Unattended Agent Autonomy — Read-Only Default with Whitelist Escalation

## Status
Accepted (2026-07-14)

## Context
Agent System v3 W3 introduces unattended execution: scheduled routines run
headless `claude -p` with no human watching. AGENTS.md had no authority model
for this surface — its tables assume an interactive orchestrator session.
Evidence driving the wave: a scheduled CI gate sat red for 77 days
(2026-04-27→07-13) because no gate ran in front of an agent (meta-audit
2026-07-14). This repo is money-facing: an unattended writer is a much larger
hazard than an unattended reader.

## Decision
1. Unattended agents are read-only by default. The runner
   (`scripts/agent_routines/run_routine.sh`) is the only entry point; it
   refuses any routine whose `write_scope` is not `none` and disallows
   Edit/Write/NotebookEdit tools outright.
2. Write access is escalated per routine, by whitelist: changing a routine's
   `write_scope` requires a new ADR plus a `.agent/CHANGELOG.md` entry BEFORE
   the runner may honor it.
3. push / merge / live-production operations / host-scheduler installation
   remain per-operation human approvals, forever outside routine scope.
4. Routines run only in a dedicated worktree (never the primary tree) and
   notify via the shared Telegram helper; reports carry no secrets.

## Consequences
### Positive
- Red-gate detection latency drops from "whenever an agent happens to look"
  (worst case 77 days) to <24h (R1 nightly triage).
- Autonomy grows without touching the AGENTS.md interactive authority tables.

### Negative
- Depends on the local host being awake (local-first was chosen deliberately:
  proprietary trading code does not go to cloud sandboxes).
- Read-only routines can only report; a found problem still waits for an
  interactive session to fix it.
- Routine noise (false findings) is a new failure mode; the whitelist gate
  keeps the blast radius at "annoying", not "destructive".

## Compliance
- [x] Allocator Law checked? N/A — no hot-path code; routines are ops tooling.
- [x] Async Law checked? N/A — runs outside the engine event loop entirely.
