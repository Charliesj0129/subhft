#!/usr/bin/env bash
# check_git_preconditions.sh — Agent workflow safety pre-checks (AWG-01/03)
#
# Usage:
#   scripts/check_git_preconditions.sh [--pre-merge|--post-merge|--session-start|--session-end]
#
# Exit codes:
#   0 = all checks pass
#   1 = check failed (unsafe to proceed)
#   2 = warning (proceed with caution)
set -uo pipefail

RED='\033[0;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
RST='\033[0m'

GIT_DIR="$(git rev-parse --git-dir 2>/dev/null)" || { echo "Not a git repo"; exit 1; }
ERRORS=0
WARNINGS=0

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
    if [ -f "$GIT_DIR/REBASE_HEAD" ] || [ -d "$GIT_DIR/rebase-merge" ] || [ -d "$GIT_DIR/rebase-apply" ]; then
        err "Rebase in progress (git rebase --abort to clear)"
        has_active=1
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
    local severity="${1:-warn}"
    local dirty
    dirty=$(git status --porcelain | wc -l)
    if [ "$dirty" -gt 0 ]; then
        if [ "$severity" = "error" ]; then
            err "Working tree has $dirty uncommitted changes"
        else
            warn "Working tree has $dirty uncommitted changes"
        fi
    else
        ok "Working tree is clean"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
MODE="${1:---pre-merge}"

echo "=== Git Precondition Check (mode: $MODE) ==="
echo ""

case "$MODE" in
    --pre-merge)
        check_no_active_ops
        check_clean_tree error
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
        check_clean_tree warn
        check_no_conflict_markers
        ;;
    --session-end)
        check_no_active_ops
        check_worktrees
        check_branches
        check_clean_tree error
        check_no_conflict_markers
        ;;
    --full)
        check_no_active_ops
        check_clean_tree error
        check_worktrees
        check_branches
        check_no_conflict_markers
        ;;
    *)
        echo "Usage: $0 [--pre-merge|--post-merge|--session-start|--session-end|--full]"
        exit 1
        ;;
esac

echo ""
if [ "$ERRORS" -gt 0 ]; then
    echo -e "${RED}BLOCKED${RST}: $ERRORS error(s), $WARNINGS warning(s) — resolve before proceeding"
    exit 1
elif [ "$WARNINGS" -gt 0 ]; then
    echo -e "${YEL}CAUTION${RST}: $WARNINGS warning(s) — proceed with awareness"
    exit 2
else
    echo -e "${GRN}ALL CLEAR${RST}: safe to proceed"
    exit 0
fi
