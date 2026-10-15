---
name: agent-meta-audit
description: "Audit the agent system (skills, prompts, delegation scoreboard, drift, gate failures) and write a dated report with at most 3 actions. Use quarterly, after ~10 delegations, or before authoring or revising a prompt or skill. Not for auditing product code."
---

# Agent meta-audit

## When to run

A quarter after the newest `agent-meta-audit-*.md` in `.agent/reports/`, or
~10 new ledger entries in `.agent/memory/model-routing.md`, or a smell:
repeated gate failures, skills contradicting reality, rising intervention
rate. Below every threshold and no smell: stop and write nothing.

To author or revise a prompt or skill, read `references/prompt-principles.md`
first; it is the standard this audit checks against.

## Procedure

1. Scoreboard trend per model tier versus the prior report (direction, not totals).
2. Intervention rate from `.agent/memory/delegations/`: which delegations
   needed correction, and the recurring causes.
3. Drift: `make agent-docs-check`; is `.agent/agent-docs-known-drift.txt`
   shrinking? Skills whose commands or paths no longer exist are findings
   (verify with `make -n`, `--help`, `ls`).
4. Prompt health against the principles: always-loaded size, duplicated rules,
   descriptions that miss their trigger or overreach, skills that restate
   generic knowledge.
5. Gate failures: `.agent/CHANGELOG.md` and git log since the last report, for
   gates that fired late or were bypassed.
6. Disposition every action from the previous report: done, rolled forward, or
   dropped with a reason.

## Output

Write `.agent/reports/agent-meta-audit-<YYYY-MM-DD>.md` with evidence-cited
findings and at most 3 actions, each with an owner and a done condition. Route
blocked actions to `.agent/memory/open-questions.md` through `memory-update`.
Audit, don't fix inline: fixes become actions.

## Done when

Every finding cites a file, commit, or ledger entry; there are at most 3
actions; the previous report's actions are all dispositioned.

## References

- `references/prompt-principles.md` — read when writing or reviewing any prompt, rule, subagent, or skill.
