# Agent Workflow Governance

> **Purpose**: Prevent AI agent workflow failures that damage git state, leak scope,
> or create cascading conflicts. These rules supplement code-quality governance
> (rules 50/55) with **behavioral governance** for agents operating on the repository.
>
> **Effective**: 2026-03-20 (post-incident: hook agents interfered with manual merge session)

---

## Failure Taxonomy (Why These Rules Exist)

| ID | Category | Example Incident | Impact |
|----|----------|-------------------|--------|
| AWG-F1 | Race Condition | Hook agent created branch during manual merge | Merge aborted, state polluted |
| AWG-F2 | Scope Creep | Hook agent pushed to origin, deleted remote branches | Unintended remote state change |
| AWG-F3 | State Pollution | Conflict markers committed to main | Broken files on main branch |
| AWG-F4 | Stale State | `CHERRY_PICK_HEAD` blocked subsequent merge | Operation blocked until manual cleanup |
| AWG-F5 | Cleanup Failure | Worktree left behind after session | Disk waste, branch pollution |
| AWG-F6 | Cascade | Multiple agents modified same files simultaneously | Unpredictable merge conflicts |

---

## AWG-01: Git State Lock (Mutual Exclusion)

**Rule**: Only ONE agent may modify git state (HEAD, index, working tree) at a time.

**Mechanism**: Before any git-mutating operation, check for active state:

```bash
# Pre-condition check (MUST pass before any git merge/cherry-pick/rebase/commit)
! test -f .git/MERGE_HEAD && \
! test -f .git/CHERRY_PICK_HEAD && \
! test -f .git/REBASE_HEAD && \
! test -f .git/rebase-merge/done && \
! test -f .git/rebase-apply/applying
```

**If check fails**: Do NOT proceed. Diagnose the stale state, resolve it explicitly
(`git merge --abort`, `git cherry-pick --abort`, etc.), then retry.

**Enforcement**: `scripts/check_git_preconditions.sh` (run by agents before git-mutating ops).

---

## AWG-02: Agent Blast Radius Classification

**Rule**: Every agent invocation MUST have an explicit blast radius tier.

| Tier | Allowed Operations | Examples |
|------|-------------------|----------|
| **Read-Only** | `git log`, `git diff`, `git status`, file reads, grep | Explore agents, research |
| **Local-Write** | File edits, `git add`, `git commit` (local only) | Code generation, refactoring |
| **Branch-Write** | Create/delete LOCAL branches, worktree operations | Feature branch work |
| **Remote-Write** | `git push`, `git fetch`, PR creation | Deployment, PR workflow |
| **Destructive** | `git push --force`, `git reset --hard`, branch deletion on remote | Emergency only, requires explicit user confirmation |

**Default**: Agents are **Local-Write** unless explicitly escalated.

**Hook agents** (automated, non-interactive) are ALWAYS **Read-Only** unless the user
has explicitly configured them for higher tiers.

**Sub-agents** (spawned via Agent tool) inherit the parent's tier unless explicitly
escalated in the prompt. Worktree-isolated agents may be **Branch-Write**.

---

## AWG-03: Pre-Operation State Verification

**Rule**: Before any git operation that modifies HEAD, the agent MUST verify:

1. **Clean working tree**: `git status --porcelain` is empty (or only expected changes)
2. **No in-progress operations**: AWG-01 check passes
3. **On expected branch**: `git branch --show-current` matches the intended target
4. **No conflict markers in tracked files**: `grep -rn '<<<<<<< ' src/ tests/` returns empty

**Post-Operation Verification** (after merge/cherry-pick/rebase):

1. **No conflict markers**: `grep -rn '<<<<<<< ' src/ tests/ docs/` returns empty
2. **Lint passes**: `uv run ruff check --select E,F src/` exits 0
3. **Expected branch**: Still on the intended branch (no unexpected checkout)

---

## AWG-04: Hook Agent Constraints

**Rule**: Automated hook agents (PreToolUse, PostToolUse, SessionStart, SessionEnd, Stop)
MUST NOT:

1. Create git branches
2. Run `git push` or any remote-modifying command
3. Run `git checkout` or `git switch` (changes active branch)
4. Run `git merge`, `git cherry-pick`, or `git rebase`
5. Modify files outside their designated scope
6. Spawn sub-agents that perform any of the above

**Rationale**: Hook agents run automatically and cannot be interactively confirmed.
They must be side-effect-free with respect to git state.

**Exception**: SessionEnd hooks may commit to `.agent/memory/` files only (session state persistence).

---

## AWG-05: Worktree Lifecycle Protocol

**Rule**: Every worktree created by an agent MUST follow this lifecycle:

```
CREATE → WORK → MERGE/ABANDON → CLEANUP → VERIFY
```

| Phase | Required Action | Timeout |
|-------|----------------|---------|
| CREATE | `git worktree add` with unique name | — |
| WORK | All mutations happen in worktree, not main repo | — |
| MERGE | Cherry-pick or merge results back to parent branch | Before session end |
| CLEANUP | `git worktree remove <path>` + `git branch -d <branch>` | Immediate after merge |
| VERIFY | `git worktree list` shows only main worktree | Before session end |

**Maximum worktree lifetime**: Same session. NEVER leave worktrees across sessions.

**Cleanup enforcement**: Session-end hook SHOULD run `git worktree list` and warn
if non-main worktrees exist.

---

## AWG-06: Conflict Resolution Protocol

**Rule**: When a merge/cherry-pick/rebase encounters conflicts:

1. **NEVER auto-resolve by accepting "ours" or "theirs" blindly** — evaluate each conflict
2. **Count conflicts first**: `git diff --name-only --diff-filter=U | wc -l`
   - ≤ 5 files: Resolve inline
   - 6-20 files: Use worktree isolation
   - \> 20 files: STOP, report to user, propose strategy
3. **After resolution**: Run AWG-03 post-operation verification (no conflict markers + lint)
4. **If resolution fails**: `git merge --abort` / `git cherry-pick --abort` — NEVER leave
   partial state

**Anti-pattern**: Committing files with `<<<<<<<`, `=======`, `>>>>>>>` markers is a
**CRITICAL violation** equivalent to committing broken code to main.

---

## AWG-07: Remote Operation Safety

**Rule**: Before any `git push`:

1. Verify the target branch: `git rev-parse --abbrev-ref HEAD`
2. Verify no worktree-agent branches are being pushed
3. Verify the push target is not `main`/`master` with `--force`
4. Run `make lint` (at minimum `ruff check`)

**Before any remote branch deletion** (`git push origin --delete`):

1. Verify the branch has been merged: `git branch --merged main | grep <branch>`
2. Confirm with user if the branch has unmerged commits
3. NEVER delete `main`, `develop`, or `release/*` branches

---

## AWG-08: Multi-Agent Coordination

**Rule**: When multiple agents operate in the same session:

1. **Independent agents** (no shared state) → Run in parallel (worktree isolation)
2. **Dependent agents** (shared git state) → Run sequentially with state verification between each
3. **Never run two agents that modify the same files** without explicit coordination

**Coordination Protocol**:
```
Agent A finishes → verify clean state → Agent B starts
                    ↓ (if dirty)
                    STOP → diagnose → resolve → continue
```

**Worktree-isolated parallel agents**: Each gets its own worktree.
Results are merged back to the parent branch ONE AT A TIME, with AWG-03 verification
between each merge.

---

## AWG-09: Session Boundary Protocol

### Session Start Checks

```bash
# 1. Verify no stale git operations
! test -f .git/MERGE_HEAD
! test -f .git/CHERRY_PICK_HEAD
! test -f .git/REBASE_HEAD

# 2. Verify no stale worktrees
git worktree list  # Should show only main

# 3. Verify stash count
git stash list | wc -l  # Should be ≤ 3

# 4. Verify branch count
git branch | wc -l  # Should be ≤ 10

# 5. Verify no worktree-agent branches
git branch | grep -c worktree-agent  # Should be 0
```

### Session End Checks (MANDATORY)

```bash
# 1. Clean up worktrees
git worktree prune
rm -rf .claude/worktrees/agent-*  # Remove orphaned dirs

# 2. Delete merged local branches
git branch --merged main | grep -v main | xargs -r git branch -d

# 3. Prune remote tracking
git fetch --prune

# 4. Verify clean state
git status --short  # Should be empty or intentional
git stash list | wc -l  # ≤ 3
```

---

## AWG-10: Error Recovery Protocol

When an agent operation fails mid-way:

| State | Recovery Action |
|-------|----------------|
| Failed merge (conflicts) | `git merge --abort` → report to user |
| Failed cherry-pick | `git cherry-pick --abort` → skip or report |
| Failed rebase | `git rebase --abort` → report to user |
| Dirty working tree after failure | `git checkout -- .` only if ALL changes are from the failed op |
| Stale lock file (`.git/index.lock`) | Check if process exists; if not, remove |
| Worktree in broken state | `git worktree remove --force <path>` |

**NEVER use destructive recovery** (`git reset --hard`, `git clean -fd`, `git checkout -- .`)
**without explicit user confirmation** unless operating in a disposable worktree.

---

## Enforcement Roadmap

| Phase | What | When |
|-------|------|------|
| **Phase 1** (Now) | Rule documentation (this file) | 2026-03-20 |
| **Phase 2** | `scripts/check_git_preconditions.sh` validation script | 2026-03-20 |
| **Phase 3** | PreToolUse hook for git-mutating commands | Next sprint |
| **Phase 4** | CI check for conflict markers in tracked files | 2026-03-20 |
| **Phase 5** | SessionEnd hook for worktree/branch cleanup verification | Next sprint |
