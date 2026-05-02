# Agent Workflow Governance

Behavioral rules for agents operating on the repository (complements code-quality rules 50/55). Prevents race conditions, scope creep, state pollution, and multi-agent cascades on git state.

## AWG-01: Git State Lock (Mutual Exclusion)

Only ONE agent may mutate git state (HEAD, index, working tree) at a time. Before any merge/cherry-pick/rebase/commit, verify none of these exist: `.git/MERGE_HEAD`, `.git/CHERRY_PICK_HEAD`, `.git/REBASE_HEAD`, `.git/rebase-merge/done`, `.git/rebase-apply/applying`.

If any exist: do NOT proceed. Resolve explicitly (`git merge --abort`, etc.), then retry. Enforcement: `scripts/check_git_preconditions.sh`.

## AWG-02: Agent Blast Radius Classification

Every agent invocation MUST have an explicit blast radius tier:

| Tier | Allowed |
|------|---------|
| Read-Only | `git log/diff/status`, file reads, grep |
| Local-Write | File edits, `git add`, local `git commit` |
| Branch-Write | Local branch create/delete, worktree ops |
| Remote-Write | `git push`, `git fetch`, PR creation |
| Destructive | `push --force`, `reset --hard`, remote branch deletion (requires explicit user confirmation) |

Default tier: Local-Write. Hook agents (automated, non-interactive): ALWAYS Read-Only unless user-configured otherwise. Sub-agents inherit parent tier unless escalated; worktree-isolated agents may be Branch-Write.

## AWG-03: Pre-Operation State Verification

Before any HEAD-modifying op, verify:
1. Clean working tree (`git status --porcelain` empty or only expected)
2. AWG-01 check passes
3. On expected branch
4. No conflict markers in tracked files (`grep -rn '<<<<<<< ' src/ tests/` empty)

After merge/cherry-pick/rebase, verify: no conflict markers in `src/ tests/ docs/`, `ruff check --select E,F src/` exits 0, still on intended branch.

## AWG-04: Hook Agent Constraints

Automated hook agents (PreToolUse, PostToolUse, SessionStart, SessionEnd, Stop) MUST NOT: create branches, run `git push`, run `git checkout`/`switch`, run merge/cherry-pick/rebase, modify files outside designated scope, or spawn sub-agents that do any of the above.

Hook agents run without interactive confirmation, so they must be side-effect-free on git state. Exception: SessionEnd hooks may commit to `.agent/memory/` only.

## AWG-05: Worktree Lifecycle Protocol

Every agent-created worktree: `CREATE → WORK → MERGE/ABANDON → CLEANUP → VERIFY`, all within the same session.

- CREATE: `git worktree add` with unique name
- WORK: All mutations in worktree, not main repo
- MERGE: Cherry-pick/merge back before session end
- CLEANUP: `git worktree remove <path>` + `git branch -d <branch>` immediately after merge
- VERIFY: `git worktree list` shows only main before session end

NEVER leave worktrees across sessions. Session-end hook SHOULD warn on non-main worktrees.

## AWG-06: Conflict Resolution Protocol

On merge/cherry-pick/rebase conflicts:
1. NEVER blindly accept "ours" or "theirs" — evaluate each conflict
2. Count first (`git diff --name-only --diff-filter=U | wc -l`): ≤5 resolve inline, 6-20 use worktree isolation, >20 STOP and report to user
3. After resolution, run AWG-03 post-op verification
4. On resolution failure: `--abort`. NEVER leave partial state.

Committing files with `<<<<<<<`/`=======`/`>>>>>>>` markers is a CRITICAL violation.

## AWG-07: Remote Operation Safety

Before any `git push`:
1. Verify target branch (`git rev-parse --abbrev-ref HEAD`)
2. Verify no `worktree-agent-*` branches being pushed
3. Reject push target `main`/`master` with `--force`
4. Run `make lint` (at minimum `ruff check`)

Before remote branch deletion: verify merged into main, confirm with user if unmerged commits exist. NEVER delete `main`, `develop`, or `release/*`.

## AWG-08: Multi-Agent Coordination

When multiple agents run in one session:
- Independent (no shared state): parallel OK with worktree isolation
- Dependent (shared git state): sequential with AWG-03 verification between each
- NEVER run two agents modifying the same files without explicit coordination

Worktree-isolated parallel agents merge back to parent branch ONE AT A TIME, with AWG-03 verification between each merge.

## AWG-09: Session Boundary Protocol

**Session Start**: verify no stale MERGE_HEAD/CHERRY_PICK_HEAD/REBASE_HEAD, `git worktree list` shows only main, stash count ≤3, branch count ≤10, zero `worktree-agent-*` branches.

**Session End (MANDATORY)**: `git worktree prune` + remove orphaned `.claude/worktrees/agent-*` dirs; delete merged local branches (`git branch --merged main | grep -v main | xargs -r git branch -d`); `git fetch --prune`; verify `git status --short` clean or intentional; stash count ≤3.

## AWG-10: Error Recovery Protocol

| Failure State | Recovery |
|---------------|----------|
| Failed merge | `git merge --abort` → report |
| Failed cherry-pick | `git cherry-pick --abort` → report |
| Failed rebase | `git rebase --abort` → report |
| Dirty tree after failure | `git checkout -- .` only if ALL changes are from the failed op |
| Stale `.git/index.lock` | Check process; remove if none |
| Broken worktree | `git worktree remove --force <path>` |

NEVER use destructive recovery (`reset --hard`, `clean -fd`, `checkout -- .`) without explicit user confirmation unless operating in a disposable worktree.
