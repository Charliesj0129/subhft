---
name: pipeline-implement
description: "First-class implement pipeline: packet -> hft-executor -> hft-reviewer -> narrow commit. Use AFTER task-intake has already decided DELEGATE for a Tier-1/2 implementation task. Not a routing rule: ROI-first delegation decides IF; this pipeline defines HOW."
---

# Skill: pipeline-implement

## When to use
task-intake has decided delegate for Tier-1/2 code+test work. Tier-3/X never
enters the pipeline (AGENTS.md routing). The pipeline changes no routing:
direct-by-default still wins for small serial tasks.

## Stages (Task-system chain; each stage blockedBy the previous)

1. **PACKET** — orchestrator fills a `small-model-handoff` packet (it becomes
   section 1 of the archive file, `.agent/memory/delegations/README.md`
   convention: one file per delegation); writes the transient marker
   .agent/runtime/active-packet.json (scope_guard hook enforces ALLOWED FILES
   from this moment).
2. **EXECUTE** — spawn hft-executor (venue per packet;
   `run_in_background: false` unless fanning out); the executor's 4-section
   report goes verbatim into archive section 2.
3. **REVIEW** — spawn hft-reviewer (sync, diff-scoped, verdict mandatory —
   the three hard rules in `small-model-handoff`
   §Independent review packets); verdict goes into archive section 3.
   REQUEST-CHANGES -> loop to stage 2 with a patch packet, or orchestrator
   fixes directly; two failed attempts on one task -> stop, ask the user.
4. **LAND** — orchestrator personally re-verifies (`strict-code-review`
   Step 0), mirrors the allowlist into .agent/runtime/commit-allowlist.json,
   runs the narrow-commit gate (exit code read directly, never via pipe),
   commits, then deletes BOTH runtime markers.
5. **LEDGER** — record outcome + realized net-win in
   `.agent/memory/model-routing.md`, linking the archive file (no second copy).

## Hard rules
- Marker lifecycle: written at PACKET, deleted at LAND. A stale
  active-packet.json blocks unrelated work — clear it first in any new session.
- Silence = FAIL, never approval; budget exhaustion = verdict first, checks cut.
- One pipeline run = one packet = one concern; scope growth stops the pipeline.

## Validation checklist
- [ ] 4 artifacts present (packet / executor-report / review-verdict / ledger row)
- [ ] Both runtime markers deleted
- [ ] Narrow gate exit 0 (not read through a pipe)

## Example prompt
"Run pipeline-implement for the docs drift fix in ops runbooks — Tier 1,
packet already scoped to two files."
