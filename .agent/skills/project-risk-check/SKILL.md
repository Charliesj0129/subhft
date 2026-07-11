---
name: project-risk-check
description: "Go/no-go risk framing for an intended change: risk surfaces touched, tier, reversibility, required gates, user-decision blockers. Use before Tier-2/3 work, before commit/PR, when resuming after a gap, or before anything touching production, config, or dependencies."
---

# Skill: project-risk-check

## When to use
Before starting any Tier-2/3 task; before commit/PR; when resuming after a
gap; before anything touching production, config, or dependencies.

## Required inputs
The intended change (1-3 sentences).

## Procedure
1. Surface check: does the change touch the `CLAUDE.md` Do-NOT-Edit list, hot
   path, contracts, migrations, goldens, pins, or frozen research state? Each
   hit raises the tier and requires explicit justification.
2. State check: `git status`/`git log` — unpushed commits at risk? dirty user
   work in the blast radius?
3. Runtime check: is a live/sim engine running that this could affect? Any
   firing alerts? (Read `.agent/memory/current-risks.md`.)
4. Reversibility check: how is this undone? If the answer involves production
   or data, it needs user confirmation FIRST.
5. Verification plan: name the gates this change must pass (from `CLAUDE.md`
   validation matrix) before it can be called done.

## Safety rules
Read-only. This skill outputs a go/no-go framing, it does not start the work.

## Output format
`## Tier` · `## Touched risk surfaces` · `## Reversibility` ·
`## Required gates` · `## Blockers needing user decision`.

## Validation checklist
- [ ] Every touched path checked against the Do-NOT-Edit list
- [ ] Rollback stated concretely
- [ ] User-decision items separated out

## Example prompt
"project-risk-check: I want to bump prometheus_client to 0.25 to fix a
deprecation warning."  (Expected outcome: BLOCKED — pinned for corruption bug.)
