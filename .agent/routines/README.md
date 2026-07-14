# Unattended Routines Registry

One routine per file. Frontmatter contract:

- `schedule`: cron expression (reference only — actual scheduling lives at the
  host layer; see `scripts/agent_routines/run_routine.sh`).
- `write_scope`: `none` | explicit path list. Anything other than `none` is an
  authority change: new ADR + `.agent/CHANGELOG.md` entry required, and the
  runner refuses to execute it until then (docs/adr/002).
- `notify`: `telegram` | `file`.
- `venue`: `routine-worktree` — routines NEVER run in the primary working tree.

The body below the frontmatter is the complete instruction handed to headless
`claude -p` — self-contained, no implicit context, no secrets.

Rules: `.agent/rules/65-unattended-autonomy.md`. Authority model: docs/adr/002.
Host scheduling install (crontab / Task Scheduler) is a per-operation human
approval — never installed by an agent.
