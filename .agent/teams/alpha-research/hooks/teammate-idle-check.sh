#!/bin/bash
# teammate-idle-check.sh — Claude Code TeammateIdle hook for alpha-research teams
#
# HOOK PROTOCOL (Claude Code):
#   Trigger: TeammateIdle event (teammate is about to go idle)
#   Input:   JSON on stdin with fields:
#              teammate_name - name of the agent about to go idle
#              team_name     - name of the team (e.g. "alpha-research-R32")
#   Exit 0:  Allow teammate to go idle
#   Exit 2:  Keep teammate working; stderr message is sent back as feedback

set -euo pipefail

# ---------------------------------------------------------------------------
# 1. Read and parse stdin
# ---------------------------------------------------------------------------
INPUT="$(cat)"

JQ="${HOME}/.local/bin/jq"
if ! command -v jq &>/dev/null && [[ -x "$JQ" ]]; then
    # use local jq if system jq is absent
    alias jq="$JQ"
elif command -v jq &>/dev/null; then
    JQ="$(command -v jq)"
fi

teammate_name="$("$JQ" -r '.teammate_name // ""' <<<"$INPUT")"
team_name="$("$JQ" -r '.team_name     // ""' <<<"$INPUT")"

# ---------------------------------------------------------------------------
# 2. Scope guard — only enforce for alpha-research teams
# ---------------------------------------------------------------------------
if [[ "$team_name" != alpha-research* ]]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. Locate the task directory for this team
#    Convention: $HOME/.claude/tasks/<team_name>/
# ---------------------------------------------------------------------------
TASK_DIR="${HOME}/.claude/tasks/${team_name}"

if [[ ! -d "$TASK_DIR" ]]; then
    # No task directory means no managed tasks — allow idle
    exit 0
fi

# ---------------------------------------------------------------------------
# 4. Count files that contain a pending task entry
#    A task file is considered pending when it contains the literal string
#    "status":"pending" (compact JSON) or "status": "pending" (pretty JSON).
# ---------------------------------------------------------------------------
pending_count=0
while IFS= read -r -d '' task_file; do
    if grep -qE '"status"\s*:\s*"pending"' "$task_file" 2>/dev/null; then
        pending_count=$((pending_count + 1))
    fi
done < <(find "$TASK_DIR" -maxdepth 1 -type f -print0 2>/dev/null)

# ---------------------------------------------------------------------------
# 5. Block idle if there are pending tasks
# ---------------------------------------------------------------------------
if [[ "$pending_count" -gt 0 ]]; then
    echo "You have ${pending_count} pending tasks in team ${team_name}. Claim the next unblocked task before going idle." >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# 6. No pending tasks — allow idle
# ---------------------------------------------------------------------------
exit 0
