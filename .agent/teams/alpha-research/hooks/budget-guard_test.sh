#!/bin/bash
# Test harness for budget-guard.sh — 9 cases.
# Each case prepares a temp artifacts dir, pipes a synthetic TaskCompleted
# JSON into the hook with cwd set to the temp dir's grandparent, and asserts
# the exit code + stderr content.

set -euo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/budget-guard.sh"

pass=0
fail=0
trap 'rm -rf "$TMP"' EXIT
TMP="$(mktemp -d)"

run_case() {
    local name="$1"
    local json="$2"
    local expected_exit="$3"
    local expected_stderr_grep="$4"

    # Set up artifacts dir under a fake project root inside TMP
    local root="$TMP/$name"
    mkdir -p "$root/outputs/team_artifacts/alpha-research"
    "$PREP_FN" "$root/outputs/team_artifacts/alpha-research"

    local actual_stderr
    set +e
    actual_stderr=$(cd "$root" && echo "$json" | bash "$HOOK" 2>&1 >/dev/null)
    local actual_exit=$?
    set -e

    if [[ "$actual_exit" == "$expected_exit" ]] \
       && { [[ -z "$expected_stderr_grep" ]] || echo "$actual_stderr" | grep -q "$expected_stderr_grep"; }; then
        echo "PASS: $name"
        pass=$((pass + 1))
    else
        echo "FAIL: $name — expected exit=$expected_exit stderr~/$expected_stderr_grep/, got exit=$actual_exit stderr='$actual_stderr'"
        fail=$((fail + 1))
    fi
}

# ---- Fixtures ----
prep_empty()        { :; }
prep_stop()         { touch "$1/STOP"; }
prep_budget_only()  { cat > "$1/budget.json" <<EOF
{"started_at":"$(date -Iseconds)","max_runtime_hours":24,"max_rounds":20,"max_promotes":3,"max_consecutive_kills":8}
EOF
}
prep_runtime_over() { cat > "$1/budget.json" <<EOF
{"started_at":"$(date -Iseconds -d '25 hours ago')","max_runtime_hours":24,"max_rounds":20,"max_promotes":3,"max_consecutive_kills":8}
EOF
}
prep_rounds_over()  { prep_budget_only "$1"; for i in $(seq 1 20); do echo "{\"round\":$i,\"verdict\":\"KILL\"}" >> "$1/progress.jsonl"; done; }
prep_promotes_over(){ prep_budget_only "$1"; for i in 1 2 3; do echo "{\"round\":$i,\"verdict\":\"PROMOTE\"}" >> "$1/progress.jsonl"; done; }
prep_consec_kills() { prep_budget_only "$1"; echo '{"round":1,"verdict":"PROMOTE"}' >> "$1/progress.jsonl"; for i in $(seq 2 9); do echo "{\"round\":$i,\"verdict\":\"KILL\"}" >> "$1/progress.jsonl"; done; }
prep_healthy()      { prep_budget_only "$1"; echo '{"round":1,"verdict":"KILL"}' >> "$1/progress.jsonl"; }
prep_progress_only(){ echo '{"round":1,"verdict":"KILL"}' > "$1/progress.jsonl"; }
prep_bad_budget()   { cat > "$1/budget.json" <<EOF
{"started_at":"not-a-date","max_runtime_hours":24,"max_rounds":20,"max_promotes":3,"max_consecutive_kills":8}
EOF
}

# ---- Cases ----
PREP_FN=prep_empty
run_case "scope-guard-non-alpha-team" \
    '{"team_name":"other-team","teammate_name":"lead"}' \
    0 ""

PREP_FN=prep_stop
run_case "stop-file-present" \
    '{"team_name":"alpha-research-R99","teammate_name":"lead"}' \
    2 "STOP file present"

PREP_FN=prep_empty
run_case "no-budget-no-progress-allows" \
    '{"team_name":"alpha-research-R99","teammate_name":"lead"}' \
    0 ""

PREP_FN=prep_progress_only
run_case "progress-without-budget-halts" \
    '{"team_name":"alpha-research-R99","teammate_name":"lead"}' \
    2 "budget.json missing"

PREP_FN=prep_healthy
run_case "healthy-allows" \
    '{"team_name":"alpha-research-R99","teammate_name":"lead"}' \
    0 ""

PREP_FN=prep_runtime_over
run_case "runtime-exceeded" \
    '{"team_name":"alpha-research-R99","teammate_name":"lead"}' \
    2 "runtime"

PREP_FN=prep_rounds_over
run_case "rounds-exceeded" \
    '{"team_name":"alpha-research-R99","teammate_name":"lead"}' \
    2 "rounds"

PREP_FN=prep_promotes_over
run_case "promotes-exceeded" \
    '{"team_name":"alpha-research-R99","teammate_name":"lead"}' \
    2 "PROMOTEs"

PREP_FN=prep_consec_kills
run_case "consecutive-kills" \
    '{"team_name":"alpha-research-R99","teammate_name":"lead"}' \
    2 "consecutive KILLs"

PREP_FN=prep_bad_budget
run_case "unparseable-started-at" \
    '{"team_name":"alpha-research-R99","teammate_name":"lead"}' \
    2 "unparseable"

# ---- Summary ----
echo
echo "Results: $pass passed, $fail failed"
(( fail == 0 ))
