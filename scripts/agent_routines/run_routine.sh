#!/usr/bin/env bash
# run_routine.sh — the ONLY entry point for unattended read-only routines
# (v3 W3; authority model: docs/adr/002, rules: .agent/rules/65-unattended-autonomy.md).
# Usage: run_routine.sh <routine-name> [--dry-run]
#
# Enforcement: refuses any routine whose write_scope is not `none`; runs
# headless claude with Edit/Write/NotebookEdit disallowed; executes in a
# dedicated routine worktree (HFT_ROUTINE_WORKTREE), never the primary tree.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RT_WT="${HFT_ROUTINE_WORKTREE:-$HOME/hft_routines_wt}"
NAME="${1:?usage: run_routine.sh <routine-name> [--dry-run]}"
DEF="$REPO/.agent/routines/${NAME}.md"

if [ ! -f "$DEF" ]; then
    echo "unknown routine: $NAME (no $DEF)" >&2
    exit 1
fi
if ! grep -q "^write_scope: none$" "$DEF"; then
    echo "refusing $NAME: write_scope is not 'none' — only read-only routines run headless (docs/adr/002)" >&2
    exit 1
fi

CLAUDE_ARGS=(--disallowedTools "Edit,Write,NotebookEdit" --max-turns 30)

if [ "${2:-}" = "--dry-run" ]; then
    echo "cd $RT_WT && claude -p <$DEF> ${CLAUDE_ARGS[*]}"
    exit 0
fi

if [ ! -d "$RT_WT" ]; then
    echo "routine worktree missing: $RT_WT (create it once, human-approved: git worktree add $RT_WT main)" >&2
    exit 1
fi

LOG_DIR="$RT_WT/.routine-logs"
mkdir -p "$LOG_DIR"
OUT="$LOG_DIR/${NAME}-$(date +%Y%m%dT%H%M%S).md"
STATUS=OK
( cd "$RT_WT" && claude -p "$(cat "$DEF")" "${CLAUDE_ARGS[@]}" ) > "$OUT" 2>&1 || STATUS=FAILED

# Reuse the shared Telegram helper; DEPLOY_ROOT must point at THIS repo so it
# reads the local .env (its default targets the deploy host). The token never
# appears in routine output or logs.
# shellcheck source=/dev/null
DEPLOY_ROOT="$REPO" source "$REPO/scripts/_notify.sh"
notify_telegram "🤖 routine ${NAME}: ${STATUS}
report: ${OUT}
$(tail -c 700 "$OUT")"

find "$LOG_DIR" -name "${NAME}-*.md" -mtime +30 -delete 2>/dev/null || true
[ "$STATUS" = OK ]
