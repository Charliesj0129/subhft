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
