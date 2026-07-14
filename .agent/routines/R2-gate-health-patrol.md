---
name: R2-gate-health-patrol
schedule: "0 8 * * 1"   # Monday 08:00 Taipei
write_scope: none
notify: telegram
venue: routine-worktree
---

# Weekly gate-health patrol (read-only)

You are a read-only patrol over the local quality gates, running in a clean
routine worktree (never the primary tree).

1. Run and record exit codes (read directly, never through a pipe):
   - `make agent-docs-check`
   - `make shioaji-guard`
   - `git status --short` (this worktree must be clean; any output = finding)
2. Compare the agent-docs known-drift count against the number stated in the
   most recent `.agent/CHANGELOG.md` entries — growth without a CHANGELOG line
   is a finding.
3. Final message = report: gate / exit code / one-line status, plus findings
   with evidence excerpts. Suggestions only; you fix nothing.
4. Forbidden: any Edit/Write, any git state change, any install/build beyond
   the listed make targets.
