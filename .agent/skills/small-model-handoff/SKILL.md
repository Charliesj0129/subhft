---
name: small-model-handoff
description: "Orchestrator prepares a self-contained delegation packet for a smaller-model coding executor. Use whenever implementation work is delegated to a Sonnet/Haiku-tier agent. Never for Tier-X tasks (see AGENTS.md routing)."
---

# Skill: small-model-handoff

## When to use
Orchestrator delegating implementation work to a Sonnet/Haiku executor.
Never for Tier-X tasks (see `AGENTS.md` routing table).

## Required inputs
Verified task scope (from `read-only-audit`); risk tier; target files.

## Procedure
1. Classify tier per `AGENTS.md`. Tier 3 → shrink scope until the packet lists
   exact functions/lines, or don't delegate.
2. Paste (don't link) the gotchas entries and law excerpts that apply.
3. Enumerate allowed files AND off-limits files (include the global
   Do-NOT-Edit list from `CLAUDE.md` for Tier 2+).
4. Write exact verification commands (`make test-file FILE=...`, `make lint`,
   etc.) — commands the executor CAN run without prompts.
5. Write stop-and-escalate conditions and the rollback note.
6. Spawn in a worktree if the task writes files. One packet = one agent.
7. On return: diff-review every changed file yourself; re-run verification
   yourself; never forward unverified executor claims to the user.

## Safety rules
Never include secrets/credentials in a packet. Never delegate git operations.
Packet must be self-contained — executor gets no implicit context.

## Output format
The Handoff Packet structure from `AGENTS.md`, as a single message.

## Validation checklist
- [ ] Tier stated; Tier-3 has Fable review planned
- [ ] Off-limits list present
- [ ] Verification commands runnable as written
- [ ] Rollback path stated
- [ ] Gotchas pasted inline

## Example prompt
"Prepare a small-model-handoff packet for adding the boot grace-period to the
recorder_data_loss probe — Tier 3, files ops/autonomy_monitor.py + its test."
