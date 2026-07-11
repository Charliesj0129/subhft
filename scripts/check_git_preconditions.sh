#!/usr/bin/env bash
# check_git_preconditions.sh — Agent workflow safety pre-checks (AWG-01/03)
#
# Usage:
#   scripts/check_git_preconditions.sh [--pre-merge|--post-merge|--session-start|--session-end|--narrow-commit|--full]
#
# --narrow-commit (SAFE-WITH-CARE support, see .agent/skills/branch-safety-check):
#   gate for a path-scoped local commit in a legitimately dirty tree. Dirty
#   tree is a warning, not a blocker; instead the staged set must exactly
#   match ALLOWED_PATHS (space-separated, exported by the caller). Fail-closed
#   when ALLOWED_PATHS is unset. Repo paths contain no spaces; paths with
#   spaces are unsupported here.
#
# Exit codes:
#   0 = all checks pass
#   1 = check failed (unsafe to proceed)
#   2 = warning (proceed with caution)
#   In --narrow-commit mode, warnings are informational only: the gate exits 0
#   whenever there are zero errors (a dirty tree is expected there — staged-set
#   equality is the enforced invariant instead). Callers gate on exit != 0.
set -uo pipefail

RED='\033[0;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
RST='\033[0m'

GIT_DIR="$(git rev-parse --git-dir 2>/dev/null)" || { echo "Not a git repo"; exit 1; }
ERRORS=0
WARNINGS=0
STRICT_CLEAN="${STRICT_CLEAN:-0}"

err()  { echo -e "${RED}FAIL${RST}: $1"; ((ERRORS++)); }
warn() { echo -e "${YEL}WARN${RST}: $1"; ((WARNINGS++)); }
ok()   { echo -e "${GRN}  OK${RST}: $1"; }

# ---------------------------------------------------------------------------
# AWG-01: No in-progress git operations
# ---------------------------------------------------------------------------
check_no_active_ops() {
    local has_active=0
    if [ -f "$GIT_DIR/MERGE_HEAD" ]; then
        err "MERGE_HEAD exists — merge in progress (git merge --abort to clear)"
        has_active=1
    fi
    if [ -f "$GIT_DIR/CHERRY_PICK_HEAD" ]; then
        err "CHERRY_PICK_HEAD exists — cherry-pick in progress (git cherry-pick --abort)"
        has_active=1
    fi
    # Active rebase is defined by the state dirs (as git itself checks);
    # REBASE_HEAD alone is a leftover marker from an aborted/interrupted rebase.
    if [ -d "$GIT_DIR/rebase-merge" ] || [ -d "$GIT_DIR/rebase-apply" ]; then
        err "Rebase in progress (git rebase --abort to clear)"
        has_active=1
    elif [ -f "$GIT_DIR/REBASE_HEAD" ]; then
        warn "Stale REBASE_HEAD with no rebase state dirs — leftover marker, safe to remove"
    fi
    if [ -f "$GIT_DIR/BISECT_LOG" ]; then
        warn "Bisect in progress (git bisect reset to clear)"
    fi
    if [ -f "$GIT_DIR/index.lock" ]; then
        if command -v lsof &>/dev/null; then
            local pid
            pid=$(lsof "$GIT_DIR/index.lock" 2>/dev/null | awk 'NR==2{print $2}')
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                err "index.lock held by PID $pid — another git operation is running"
            else
                warn "Stale index.lock — no process holds it (safe to remove)"
            fi
        else
            warn "index.lock exists — verify no other git operation is running"
        fi
    fi
    [ "$has_active" -eq 0 ] && ok "No in-progress git operations"
}

# ---------------------------------------------------------------------------
# AWG-03: No conflict markers in source files
# ---------------------------------------------------------------------------
check_no_conflict_markers() {
    local conflicts
    conflicts=$(grep -rn '<<<<<<< ' src/ tests/ docs/ 2>/dev/null | head -20 || true)
    if [ -n "$conflicts" ]; then
        err "Conflict markers found in tracked files:"
        echo "$conflicts" | head -10
        local count
        count=$(echo "$conflicts" | wc -l)
        [ "$count" -gt 10 ] && echo "  ... and $((count - 10)) more"
    else
        ok "No conflict markers in src/tests/docs"
    fi
}

# ---------------------------------------------------------------------------
# AWG-05: Worktree state
# ---------------------------------------------------------------------------
check_worktrees() {
    local wt_count
    wt_count=$(git worktree list | wc -l)
    if [ "$wt_count" -gt 1 ]; then
        warn "Active worktrees: $wt_count (expected 1)"
        git worktree list | tail -n +2 | while read -r line; do
            echo "  $line"
        done
    else
        ok "Single worktree (clean)"
    fi

    # Check for orphaned worktree directories
    if [ -d ".claude/worktrees" ]; then
        local orphans
        orphans=$(find .claude/worktrees -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
        if [ "$orphans" -gt 0 ]; then
            warn "Orphaned worktree dirs in .claude/worktrees/: $orphans"
        fi
    fi
}

# ---------------------------------------------------------------------------
# AWG-09: Branch hygiene
# ---------------------------------------------------------------------------
check_branches() {
    local branch_count
    branch_count=$(git branch | wc -l)
    if [ "$branch_count" -gt 10 ]; then
        warn "Local branches: $branch_count (max recommended: 10)"
    else
        ok "Local branches: $branch_count"
    fi

    local agent_branches
    agent_branches=$(git branch | grep -c 'worktree-agent\|hook-agent' || true)
    if [ "$agent_branches" -gt 0 ]; then
        err "Agent branches not cleaned up: $agent_branches"
        git branch | grep 'worktree-agent\|hook-agent'
    else
        ok "No stale agent branches"
    fi

    local stash_count
    stash_count=$(git stash list | wc -l)
    if [ "$stash_count" -gt 3 ]; then
        warn "Stash entries: $stash_count (max recommended: 3)"
    else
        ok "Stash entries: $stash_count"
    fi
}

# ---------------------------------------------------------------------------
# AWG-03: Clean working tree
# ---------------------------------------------------------------------------
check_clean_tree() {
    local dirty
    dirty=$(git status --porcelain | wc -l)
    if [ "$dirty" -gt 0 ]; then
        if [ "$STRICT_CLEAN" = "1" ]; then
            err "Working tree has $dirty uncommitted changes (strict mode)"
        else
            warn "Working tree has $dirty uncommitted changes"
        fi
    else
        ok "Working tree is clean"
    fi
}

# ---------------------------------------------------------------------------
# Repo Gate: No generated artifacts left behind
# ---------------------------------------------------------------------------
check_no_generated_artifacts() {
    local found=0
    local artifacts=""

    # Coverage reports
    for f in coverage.json coverage_*.json; do
        if [ -f "$f" ]; then
            artifacts="$artifacts  $f\n"
            ((found++))
        fi
    done

    # Benchmark baselines
    if [ -d "tests/benchmark/baselines" ] && [ "$(ls -A tests/benchmark/baselines 2>/dev/null)" ]; then
        artifacts="$artifacts  tests/benchmark/baselines/\n"
        ((found++))
    fi

    # Stray .baseline.json files
    local baseline_files
    baseline_files=$(find tests/ -name "*.baseline.json" 2>/dev/null | head -5)
    if [ -n "$baseline_files" ]; then
        artifacts="$artifacts  $(echo "$baseline_files" | head -3 | tr '\n' ' ')\n"
        ((found++))
    fi

    if [ "$found" -gt 0 ]; then
        err "Generated artifacts found in working tree ($found):"
        echo -e "$artifacts"
    else
        ok "No generated artifacts in working tree"
    fi
}

# ---------------------------------------------------------------------------
# Narrow-commit gate: commits must land on a named branch (a detached-HEAD
# commit is orphaned — unacceptable with irreplaceable local-only commits).
# ---------------------------------------------------------------------------
check_named_branch() {
    if git symbolic-ref -q HEAD >/dev/null; then
        ok "On named branch: $(git branch --show-current)"
    else
        err "Detached HEAD — a narrow commit requires a named branch"
    fi
}

# ---------------------------------------------------------------------------
# Narrow-commit gate (SAFE-WITH-CARE): staged set must exactly match
# ALLOWED_PATHS — no extra staged files, no approved-but-unstaged files.
# ---------------------------------------------------------------------------
check_staged_allowlist() {
    if [ -z "${ALLOWED_PATHS:-}" ]; then
        err "ALLOWED_PATHS not set — narrow-commit mode is fail-closed without an explicit allowlist"
        return
    fi

    local staged
    staged=$(git diff --cached --name-only)
    if [ -z "$staged" ]; then
        err "Staged set is empty — nothing to gate"
        return
    fi

    local mismatch=0
    local f a found
    while IFS= read -r f; do
        found=0
        for a in $ALLOWED_PATHS; do
            [ "$f" = "$a" ] && { found=1; break; }
        done
        if [ "$found" -eq 0 ]; then
            err "Staged file outside ALLOWED_PATHS: $f"
            ((mismatch++))
        fi
    done <<< "$staged"

    for a in $ALLOWED_PATHS; do
        found=0
        while IFS= read -r f; do
            [ "$f" = "$a" ] && { found=1; break; }
        done <<< "$staged"
        if [ "$found" -eq 0 ]; then
            err "Approved file not staged: $a (staged set must exactly match ALLOWED_PATHS)"
            ((mismatch++))
        fi
    done

    [ "$mismatch" -eq 0 ] && ok "Staged set exactly matches ALLOWED_PATHS ($(echo "$staged" | wc -l) file(s))"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
MODE="${1:---pre-merge}"

echo "=== Git Precondition Check (mode: $MODE) ==="
echo ""

case "$MODE" in
    --pre-merge)
        STRICT_CLEAN=1
        check_no_active_ops
        check_clean_tree
        check_no_generated_artifacts
        check_no_conflict_markers
        ;;
    --post-merge)
        check_no_conflict_markers
        check_no_active_ops
        ;;
    --session-start)
        check_no_active_ops
        check_worktrees
        check_branches
        check_no_conflict_markers
        ;;
    --session-end)
        STRICT_CLEAN=1
        check_no_active_ops
        check_worktrees
        check_branches
        check_clean_tree
        check_no_generated_artifacts
        check_no_conflict_markers
        ;;
    --full)
        check_no_active_ops
        check_clean_tree
        check_worktrees
        check_branches
        check_no_conflict_markers
        ;;
    --narrow-commit)
        # SAFE-WITH-CARE (branch-safety-check skill): a legitimately dirty
        # tree warns instead of blocking; the gate enforces staged-set
        # equality with the explicit per-ceremony allowlist instead.
        STRICT_CLEAN=0
        check_no_active_ops
        check_named_branch
        check_staged_allowlist
        check_clean_tree
        check_no_conflict_markers
        ;;
    *)
        echo "Usage: $0 [--pre-merge|--post-merge|--session-start|--session-end|--narrow-commit|--full]"
        exit 1
        ;;
esac

echo ""
if [ "$ERRORS" -gt 0 ]; then
    echo -e "${RED}BLOCKED${RST}: $ERRORS error(s), $WARNINGS warning(s) — resolve before proceeding"
    exit 1
elif [ "$MODE" = "--narrow-commit" ]; then
    # Warnings (expected dirty tree) are informational here; the gate itself
    # passed. Exit 0 so shell workflows treat SAFE-WITH-CARE as success.
    if [ "$WARNINGS" -gt 0 ]; then
        echo -e "${GRN}GATE PASSED${RST} (SAFE-WITH-CARE): $WARNINGS informational warning(s) — narrow commit permitted"
    else
        echo -e "${GRN}ALL CLEAR${RST}: safe to proceed"
    fi
    exit 0
elif [ "$WARNINGS" -gt 0 ]; then
    echo -e "${YEL}CAUTION${RST}: $WARNINGS warning(s) — proceed with awareness"
    exit 2
else
    echo -e "${GRN}ALL CLEAR${RST}: safe to proceed"
    exit 0
fi
