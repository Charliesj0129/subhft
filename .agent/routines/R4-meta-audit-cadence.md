---
name: R4-meta-audit-cadence
schedule: "0 9 1 */3 *"   # quarterly, 1st of the quarter 09:00 Taipei (or run when the ledger grows by ~10)
write_scope: none
notify: telegram
venue: routine-worktree
---

# Meta-audit cadence draft (read-only)

You draft the periodic agent-system self-audit. The FORMAL report is written
and committed only by an orchestrator session — your output is a draft to
stdout; write_scope stays none.

1. Follow the procedure in `.agent/skills/agent-meta-audit/SKILL.md` steps 1-6
   in read-only form: cadence check (last report date in `.agent/reports/`,
   ledger growth in `.agent/memory/model-routing.md`), scoreboard trend,
   intervention rate, stale-skill sweep (`make agent-docs-check` output only),
   gate-failure review (`.agent/CHANGELOG.md` + git log), prior-actions
   follow-up against the previous report.
2. Final message = the draft report: findings with evidence citations and AT
   MOST 3 proposed actions (owner + done-condition each), clearly labeled
   "DRAFT — formal report requires an orchestrator session".
3. Forbidden: any Edit/Write, any git state change. If the cadence threshold
   is not met, report "cadence not due" with the numbers and stop.
