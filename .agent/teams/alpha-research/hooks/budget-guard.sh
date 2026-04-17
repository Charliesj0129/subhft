#!/bin/bash
# budget-guard.sh — Claude Code TaskCompleted hook for alpha-research teams.
#
# HOOK PROTOCOL:
#   Trigger: TaskCompleted
#   Input:   JSON on stdin (fields: team_name, teammate_name, task_subject, task_description)
#   Exit 0:  Allow completion
#   Exit 2:  Reject completion; stderr is surfaced as feedback to the teammate.
#
# Purpose: halts the alpha-research autonomous loop when STOP file is present
# or any budget limit is exceeded. Referenced in spec
# docs/superpowers/specs/2026-04-17-alpha-research-autonomous-loop-design.md.

set -euo pipefail

# jq resolution — prefer system jq if in PATH, fall back to local install
JQ="${HOME}/.local/bin/jq"
if command -v jq &>/dev/null; then
    JQ="$(command -v jq)"
fi

INPUT="$(cat)"
team_name="$("$JQ" -r '.team_name // ""' <<<"$INPUT")"

# Scope guard: only enforce for alpha-research teams
if [[ "$team_name" != alpha-research* ]]; then
    exit 0
fi

ARTIFACTS_DIR="outputs/team_artifacts/alpha-research"
BUDGET="$ARTIFACTS_DIR/budget.json"
PROGRESS="$ARTIFACTS_DIR/progress.jsonl"
STOP_FILE="$ARTIFACTS_DIR/STOP"

# 1. STOP file
if [[ -f "$STOP_FILE" ]]; then
    echo "HALT: STOP file present at $STOP_FILE. Write final_summary.md and pause." >&2
    exit 2
fi

# 2. budget.json must exist before round 1 completes
if [[ ! -f "$BUDGET" ]]; then
    # Allow missing budget on first task (T0 init writes it); only enforce once progress starts
    [[ -f "$PROGRESS" ]] && { echo "HALT: budget.json missing but progress.jsonl exists" >&2; exit 2; }
    exit 0
fi

started_at=$("$JQ" -r '.started_at // empty' "$BUDGET")
max_hours=$("$JQ" -r '.max_runtime_hours // 24' "$BUDGET")
max_rounds=$("$JQ" -r '.max_rounds // 20' "$BUDGET")
max_promotes=$("$JQ" -r '.max_promotes // 3' "$BUDGET")
max_consec_kills=$("$JQ" -r '.max_consecutive_kills // 8' "$BUDGET")

# Runtime check
# GNU date required (Linux target). Fail-safe: unparseable started_at halts the hook.
if [[ -n "$started_at" ]]; then
    if ! started_ts=$(date -d "$started_at" +%s 2>/dev/null); then
        echo "HALT: budget.json started_at is unparseable ('$started_at'). Fix or delete budget.json." >&2
        exit 2
    fi
    elapsed_h=$(( ( $(date +%s) - started_ts ) / 3600 ))
    if (( elapsed_h >= max_hours )); then
        echo "HALT: runtime $elapsed_h h >= max $max_hours h. Write final_summary.md and pause." >&2
        exit 2
    fi
fi

# Rounds / promotes / consecutive kills
if [[ -f "$PROGRESS" ]]; then
    rounds=$(wc -l < "$PROGRESS")
    promotes=$(grep -c '"verdict":"PROMOTE"' "$PROGRESS" || true)
    lines_in_tail=$(tail -n "$max_consec_kills" "$PROGRESS" 2>/dev/null | wc -l)
    consec_kills=$(tail -n "$max_consec_kills" "$PROGRESS" 2>/dev/null \
                   | grep -c '"verdict":"KILL"' || true)

    (( rounds   >= max_rounds   )) && { echo "HALT: $rounds rounds >= max $max_rounds. Write final_summary.md and pause." >&2; exit 2; }
    (( promotes >= max_promotes )) && { echo "HALT: $promotes PROMOTEs >= max $max_promotes. Write final_summary.md and pause." >&2; exit 2; }
    if (( lines_in_tail == max_consec_kills && consec_kills == max_consec_kills )); then
        echo "HALT: $max_consec_kills consecutive KILLs detected — directional exhaustion signal. Write final_summary.md and pause." >&2
        exit 2
    fi
fi

exit 0
