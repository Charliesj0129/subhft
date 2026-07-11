---
name: agent-meta-audit
description: "Periodic audit of the agent system itself: scoreboard trends, intervention rate, stale skills, gate failures. Runs quarterly or every ~10 delegations; writes a dated report to .agent/reports/ with at most 3 improvement actions. Use on cadence or when the agent system seems to be drifting."
---

# Skill: agent-meta-audit

## When to use
A quarter has passed since the newest `agent-meta-audit-*.md` in
`.agent/reports/`, OR `.agent/memory/model-routing.md` gained ~10 delegation
ledger entries since it, OR a systemic smell appears (repeated gate failures,
skills contradicting reality, rising intervention rate).

## Required inputs
Newest prior report in `.agent/reports/` (or "none"); read access to
`.agent/memory/model-routing.md`, `.agent/memory/delegations/`,
`.agent/CHANGELOG.md`, `.agent/agent-docs-known-drift.txt`, git log.

## Procedure
1. Cadence check: date the newest `agent-meta-audit-*.md`; count ledger
   entries since. Below both thresholds and no smell trigger → stop, write
   nothing.
2. Scoreboard trend: per model tier, success/intervention counts now vs the
   prior report — report the direction, not just totals.
3. Intervention rate: from `.agent/memory/delegations/`, the fraction of
   delegations that needed orchestrator correction; name recurring causes.
4. Stale-skill sweep: run `make agent-docs-check`; is
   `.agent/agent-docs-known-drift.txt` shrinking (ratchet working) or being
   added to? Skills whose commands/paths no longer match reality are findings.
5. Gate-failure review: `.agent/CHANGELOG.md` + git log since the prior
   report for gates that fired late or were bypassed (e.g. a commit landing
   while a required check was red) — each one is a finding.
6. Prior-actions follow-up: disposition every action from the previous
   report as done / rolled forward / dropped-with-reason.
7. Write `.agent/reports/agent-meta-audit-<YYYY-MM-DD>.md`: evidence-cited
   findings + AT MOST 3 improvement actions, each with an owner and a
   done-condition. Route blocked actions into
   `.agent/memory/open-questions.md` via the memory-update skill.

## Safety rules
Writes ONLY a new dated file under `.agent/reports/` (plus memory routing via
memory-update). Everything else is read-only: audit, don't fix inline —
fixes become actions.

## Output format
Report sections: `## Cadence` · `## Scoreboard trend` · `## Intervention rate`
· `## Stale skills / drift` · `## Gate failures` · `## Prior actions
follow-up` · `## Actions (max 3: owner + done-condition)`.

## Validation checklist
- [ ] Every finding cites evidence (file / commit / ledger entry)
- [ ] At most 3 actions, each with owner + done-condition
- [ ] Prior report's actions all dispositioned
- [ ] No fixes applied inline

## Example prompt
"Run agent-meta-audit — a quarter has passed since the last report and the
ledger gained 11 delegations."
