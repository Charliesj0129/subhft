---
name: git-safety
description: "Check git state before a state change, then stage and commit narrowly with a conventional message, or set up a worktree. Use when committing, branching, or creating a worktree. Not for push, merge, rebase, reset, or clean, which need the user's per-operation approval."
---

# Git safety

Red lines (push, merge, rebase, reset, clean, stash drop, `branch -D`, amend,
history edits, `.gitignore`) are in `AGENTS.md`: ask first, every time.
Creating branches and worktrees and committing on a feature branch are granted.

## Before a state change

1. `git status --short`: classify every dirty file (mine / user's / unknown).
   Unknown, or the user's files inside the blast radius -> stop and ask.
2. Count unpushed commits: `git log --oneline @{upstream}..HEAD`.
3. `bash scripts/check_git_preconditions.sh --pre-merge` (or `--full`): no
   merge/rebase/cherry-pick in progress, no conflict markers. `make git-precheck` wraps it.
4. Confirm the branch is the expected one. Never commit on the default branch; one branch per theme.
5. Write the rollback command down before you run the operation
   (`git restore --staged <paths>` before the commit, `git revert <hash>` after).

Smaller models never run git. Never push `worktree-agent-*` branches.

## Narrow commit in a dirty tree

A dirty tree does not block a local commit when: no unknown files; the user's
dirty files are outside your allowlist; staging is by explicit path (never
`-A`/`-u`); and the staged set equals your files exactly:

```bash
ALLOWED_PATHS="<files>" bash scripts/check_git_preconditions.sh --narrow-commit
```

Exit 0 = pass (unrelated dirty files print as warnings only); any nonzero exit
= stop. Mirror the allowlist into the runtime marker commit-allowlist.json
(in the .agent/runtime directory) before `git commit` so the `commit_audit` hook can re-check HEAD; delete the
marker afterward. `git add -f` only for known-ignored tracked paths (for
example `.agent/memory/`), with a stated reason.

## Commit

Split by theme (feature vs refactor, tests vs prod, formatting vs logic); use
`git add -p` for mixed files. Review `git diff --cached` for secrets, debug
output, and unrelated churn. Message: Conventional Commits
(`type(scope): summary`, then what and why), types listed in `AGENTS.md`. Run
the checks for the blast-radius row in `AGENTS.md` "Done means" first; a commit
spanning rows takes the strictest. Afterwards `make git-postcheck` and
`git show --stat` to confirm only intended files landed.

## Worktrees (parallel or risky work)

`git worktree add ~/hft_worktrees/<theme> -b <branch> origin/main`, work and
verify there, `git worktree remove` when done. Use one for anything larger
than a small edit or when the user's tree is dirty. Parallel agents never share files.

## Done when

The commit report lists the commit message(s), the commands used (at least
`git diff --cached` and the tests run), the blast-radius row, and a **Checks
NOT run** list (write "none" if empty).
