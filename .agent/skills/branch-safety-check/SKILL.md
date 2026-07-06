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
   no in-progress merge/rebase/cherry-pick, no conflict markers.
4. Confirm current branch == expected branch; on the default branch, branch
   first before committing.
5. Classify the operation and gate it:

   | Class | Operations | Authority |
   |---|---|---|
   | Read-only | status, log, diff, show | orchestrator, freely |
   | Local-Write | narrow-path add, commit on a feature branch, worktree create | orchestrator, after verdict SAFE |
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

## Output format
`## Branch` · `## Dirty-file classification` · `## Unpushed commits` ·
`## Precondition script result` · `## Verdict: SAFE / SAFE-WITH-CARE / STOP`.

## Validation checklist
- [ ] Every dirty file classified
- [ ] Unpushed count recorded
- [ ] Precondition script actually run (output pasted)
- [ ] Operation classified; Remote-Write/Destructive has in-session approval
- [ ] Rollback command written before executing

## Example prompt
"branch-safety-check before committing the validation-harness test files on
chore/shioaji-153-validation-harness — the tree has 9 modified files that
may include concurrent user research work."
