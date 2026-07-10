# Agent Workflow Governance

Before HEAD/index/branch changes, verify no merge/cherry-pick/rebase state, expected branch, clean-or-intentional worktree, and no conflict markers in tracked source/tests/docs.

Blast radius tiers: Read-Only, Local-Write, Branch-Write, Remote-Write, Destructive. Default is Local-Write; hooks are Read-Only except explicit memory writes. Destructive operations require explicit user confirmation.

Multi-agent work:

- Parallel only when independent and worktree-isolated.
- Merge/cherry-pick back one at a time with verification between.
- No two agents modify the same files without coordination.

Conflict protocol: inspect each conflict; never blindly accept ours/theirs. If conflicts are large or unclear, stop and report. Never commit conflict markers.

Remote safety: verify branch and target before push; never force-push main/master; do not push ephemeral worktree branches.

Session hygiene: prune/cleanup agent-created worktrees, keep stash count small, delete merged local task branches when appropriate, leave `git status --short` clean or intentional.

Governance change control:

- Governing docs: `CLAUDE.md`, `AGENTS.md`, `.agent/rules/`, `.agent/skills/`, `.agent/evals/`, `.agent/templates/`, `.agent/00-MANIFEST.md`.
- Every governing-doc change commits as `docs(agents):` in the same session that makes it (never left dirty overnight), with a one-line entry in `.agent/CHANGELOG.md` (date / files / why) in the same commit.
- Changes that alter role authority, tier boundaries, or routing rules additionally require an ADR from `.agent/templates/ADR_TEMPLATE.md`, committed alongside.
- `make agent-docs-check` must pass before the commit.
