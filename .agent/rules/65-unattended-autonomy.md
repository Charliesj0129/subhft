# Unattended Autonomy

Authority model: docs/adr/002. Registry: `.agent/routines/` (one file per
routine; frontmatter contract in its README).

- Default read-only: `write_scope: none`. The runner
  `scripts/agent_routines/run_routine.sh` is the ONLY entry point and refuses
  anything else; Edit/Write/NotebookEdit are tool-disallowed.
- Escalating a routine's write_scope = authority change: new ADR +
  `.agent/CHANGELOG.md` entry BEFORE the runner honors it.
- Venue: dedicated routine worktree only (HFT_ROUTINE_WORKTREE, default
  ~/hft_routines_wt) — never the primary working tree.
- Host scheduling (crontab / Task Scheduler) and worktree creation are
  per-operation human approvals; agents present the command, never install it.
- Notifications go through scripts/_notify.sh with DEPLOY_ROOT pointed at this
  repo; no secrets in routine bodies, reports, or logs. Logs live in the
  routine worktree (.routine-logs, 30-day retention).
- Failure is loud: the runner notifies FAILED status; a silent routine is a
  finding, not a success.
- Routines suggest; interactive sessions fix. A routine report never
  authorizes an action by itself.
