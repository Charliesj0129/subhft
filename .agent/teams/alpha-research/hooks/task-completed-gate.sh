#!/bin/bash
# task-completed-gate.sh — Claude Code TaskCompleted hook for alpha-research teams
#
# HOOK PROTOCOL (Claude Code):
#   Trigger: TaskCompleted event
#   Input:   JSON on stdin with fields:
#              task_id          - unique task identifier
#              task_subject     - short title of the task
#              task_description - full output/description produced by the teammate
#              teammate_name    - name of the agent that completed the task
#              team_name        - name of the team (e.g. "alpha-research-R32")
#   Exit 0:  Allow task completion
#   Exit 2:  Reject completion; stderr is sent back as feedback to the teammate

set -euo pipefail

# ---------------------------------------------------------------------------
# 1. Read and parse stdin
# ---------------------------------------------------------------------------
INPUT="$(cat)"

team_name="$(echo "$INPUT"     | jq -r '.team_name     // ""')"
teammate_name="$(echo "$INPUT" | jq -r '.teammate_name // ""')"
task_subject="$(echo "$INPUT"  | jq -r '.task_subject  // ""')"
task_description="$(echo "$INPUT" | jq -r '.task_description // ""')"

# ---------------------------------------------------------------------------
# 2. Scope guard — only enforce for alpha-research teams
# ---------------------------------------------------------------------------
if [[ "$team_name" != alpha-research* ]]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. RESEARCHER — literature / proposal / search tasks
#    Required sections: Expected Edge, Horizon, Data Needed, Overlap Check
# ---------------------------------------------------------------------------
if [[ "$teammate_name" == "researcher" ]]; then
    if echo "$task_subject" | grep -qiE "iterature|roposal|earch"; then
        missing=()
        echo "$task_description" | grep -qi "Expected Edge"  || missing+=("Expected Edge")
        echo "$task_description" | grep -qi "Horizon"        || missing+=("Horizon")
        echo "$task_description" | grep -qi "Data Needed"    || missing+=("Data Needed")
        echo "$task_description" | grep -qi "Overlap Check"  || missing+=("Overlap Check")

        if [[ ${#missing[@]} -gt 0 ]]; then
            echo "QUALITY GATE FAILED: researcher proposal is missing required sections: ${missing[*]}." \
                 "Please add: ${missing[*]}." >&2
            exit 2
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 4. DEVIL'S ADVOCATE — kill-checklist reviews
#    Required markers: [H1] through [H6]
# ---------------------------------------------------------------------------
if [[ "$teammate_name" == devil* ]]; then
    missing=()
    for h in H1 H2 H3 H4 H5 H6; do
        echo "$task_description" | grep -qF "[$h]" || missing+=("[$h]")
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "QUALITY GATE FAILED: devil's-advocate review is missing checklist markers: ${missing[*]}." \
             "Each hypothesis must be addressed with [H1]…[H6]." >&2
        exit 2
    fi
fi

# ---------------------------------------------------------------------------
# 5. EXECUTOR — backtest / scorecard tasks
#    Required sections: Sharpe, Drawdown, Win Rate, Edge
# ---------------------------------------------------------------------------
if [[ "$teammate_name" == "executor" ]]; then
    if echo "$task_subject" | grep -qiE "acktest|corecard"; then
        missing=()
        echo "$task_description" | grep -qi "Sharpe"   || missing+=("Sharpe")
        echo "$task_description" | grep -qi "Drawdown" || missing+=("Drawdown")
        echo "$task_description" | grep -qi "Win Rate" || missing+=("Win Rate")
        echo "$task_description" | grep -qi "Edge"     || missing+=("Edge")

        if [[ ${#missing[@]} -gt 0 ]]; then
            echo "QUALITY GATE FAILED: executor scorecard is missing required metrics: ${missing[*]}." \
                 "Please include all of: Sharpe, Drawdown, Win Rate, Edge." >&2
            exit 2
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 6. All checks passed — allow completion
# ---------------------------------------------------------------------------
exit 0
