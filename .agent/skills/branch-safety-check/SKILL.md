---
name: branch-safety-check
description: "Pre-flight for ANY git state change (checkout, merge, rebase, commit, stash, worktree, push): classify dirty files, count unpushed commits, run precondition scripts, verdict SAFE/STOP. Wraps make git-precheck and .agent/rules/60-agent-workflow-governance.md."
---

# Skill: branch-safety-check

## When to use
Before ANY git state change (checkout, merge, rebase, commit, stash, worktree
ops, push); at session start; before and after delegated work returns.
Wraps and extends `make git-precheck` /
`.agent/rules/60-agent-workflow-governance.md`.

## Required inputs
Intended git operation; expected branch.

## Procedure
1. `git status --short` — classify every dirty file: mine / user's concurrent
   work / unknown. Unknown or user files in blast radius → STOP, ask.
2. `git log --oneline @{upstream}..HEAD 2>/dev/null || git log --branches
   --not --remotes --oneline | wc -l` — count unpushed/unbacked commits. This
   repo has had 25+ local-only commits; treat them as irreplaceable.
3. `bash scripts/check_git_preconditions.sh --pre-merge` (or `--full`) —
   no in-progress merge/rebase/cherry-pick, no conflict markers. For a
   commit-class operation in a legitimately dirty tree, run
   `ALLOWED_PATHS="<task files>" bash scripts/check_git_preconditions.sh
   --narrow-commit` instead (see SAFE-WITH-CARE below).
4. Confirm current branch == expected branch; on the default branch, branch
   first before committing.
5. Classify the operation and gate it:

   | Class | Operations | Authority |
   |---|---|---|
   | Read-only | status, log, diff, show | orchestrator, freely |
   | Local-Write | narrow-path add, commit on a feature branch, worktree create | orchestrator, after verdict SAFE — or SAFE-WITH-CARE per the narrow-commit rule below |
   | Remote-Write | push, PR creation, upstream changes | HUMAN approval, in-session, per push |
   | Destructive | merge, rebase, reset, clean, stash-drop, branch-delete, force-anything, history edits | HUMAN approval, per operation, plan presented first |

   Approval is per-operation, never blanket, never retroactive.
6. Write the rollback command down BEFORE executing the operation.
7. After the operation: `make git-postcheck`; confirm status matches intent;
   report exactly what ran.

## Safety rules
Never force-push; never push `worktree-agent-*` branches; never
reset/clean/checkout over dirty user files; destructive git only on explicit
user request. Stage narrowly by path — never `git add -A` in a dirty tree.
`git add -f` only for known-ignored tracked paths (e.g. `.agent/memory/`
files) with a stated reason. Smaller models never run git commands.

## SAFE-WITH-CARE (narrow local commit in a dirty tree)

A dirty working tree does not by itself block a local commit. SAFE-WITH-CARE
permits a **narrow local commit only** when ALL of the following hold:

1. Every dirty file is classified (mine / user's concurrent work / unknown);
   any *unknown* → STOP.
2. All user-owned dirty files are OUTSIDE the task allowlist.
3. Staging is path-scoped (`git add <explicit paths>`; never `-A`/`-u`).
4. `git diff --cached --name-only` exactly equals the approved task files —
   no more, no fewer (enforced by
   `ALLOWED_PATHS="<task files>" bash scripts/check_git_preconditions.sh
   --narrow-commit`).
5. No remote or history operation is part of the action (push, merge, rebase,
   reset, clean, stash remain their own authority classes).
6. The rollback command is written down BEFORE committing
   (`git restore --staged <paths>` pre-commit; `git revert <hash>`
   post-commit).
7. Postcheck confirms no unrelated files were touched: staged-set equality
   re-verified on the commit (`git show --stat`), user dirty files still
   present and unstaged.

SAFE-WITH-CARE never extends to Remote-Write or Destructive operations. If
any condition fails → STOP.

Exit-code contract (no output parsing): `--narrow-commit` exits **0** when the
gate passes — unrelated dirty files print as informational warnings only.
Any **nonzero** exit → STOP. (Other modes keep exit 2 = caution.)

## Output format
`## Branch` · `## Dirty-file classification` · `## Unpushed commits` ·
`## Precondition script result` · `## Verdict: SAFE / SAFE-WITH-CARE / STOP`.

## Validation checklist
- [ ] Every dirty file classified
- [ ] Unpushed count recorded
- [ ] Precondition script actually run (output pasted)
- [ ] Operation classified; Remote-Write/Destructive has in-session approval
- [ ] Rollback command written before executing
- [ ] SAFE-WITH-CARE commits: staged set equals approved task files (cached
      name-only diff pasted)

## Example prompt
"branch-safety-check before committing the validation-harness test files on
chore/shioaji-153-validation-harness — the tree has 9 modified files that
may include concurrent user research work."
