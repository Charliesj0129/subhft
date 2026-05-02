# Alpha Research Autonomous Maker/Taker Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewire `/alpha-research` into an autonomous 24-hour maker/taker research loop with a checkpoint-resumable Team Lead, autonomous candidate-pool regeneration, evidence-weighted tie-break authority, and a budget-guard hook that halts the loop on STOP file or budget exhaustion.

**Architecture:** One new shell hook + one new YAML section in the shared-context template + targeted edits to two role prompts, the team README, and the slash command. State lives on disk under `outputs/team_artifacts/alpha-research/` (budget.json, candidate_pool.json, progress.jsonl, resume_checkpoint.json, round-N/summary.md). The hook fires on every `TaskCompleted` event in alpha-research teams and hard-halts when any of 5 budget triggers (STOP file, runtime, rounds, promotes, consecutive kills) are hit.

**Tech Stack:** Bash (hook) + YAML (shared context) + Markdown (role prompts, README, command file) + JSON (settings.local.json). Dependency: `jq` 1.7.1 (already installed at `/home/charlie/.local/bin/jq`).

**Spec:** `docs/superpowers/specs/2026-04-17-alpha-research-autonomous-loop-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `.agent/teams/alpha-research/shared-context.template.yaml` | Add `scope` section | Declares allowed / forbidden candidate types for the loop |
| `.agent/teams/alpha-research/hooks/budget-guard.sh` | **New** | TaskCompleted hook; halts loop on STOP / budget triggers |
| `.agent/teams/alpha-research/hooks/budget-guard_test.sh` | **New** | Test harness for budget-guard.sh (9 cases) |
| `.agent/teams/alpha-research/roles/researcher.md` | Edit | Add scope read + regen behavior note |
| `.agent/teams/alpha-research/roles/devils-advocate.md` | Edit | Add Kill Checklist item 0 (scope) + regen sanity pass |
| `.agent/teams/alpha-research/README.md` | Edit | T0–T9 task chain + Resume/Regen/Tie-break/Budget sections |
| `.claude/commands/alpha-research.md` | Rewrite | Autonomous-loop description, `--resume` flag, Lead-as-driver role |
| `.claude/settings.local.json` | Edit | Register `budget-guard.sh` in `hooks.TaskCompleted` |

---

## Task 1: Add `scope` section to shared-context.template.yaml

**Files:**
- Modify: `.agent/teams/alpha-research/shared-context.template.yaml` (insert after line 28, before `cost_model`)

- [ ] **Step 1: Read current file to confirm insertion point**

Run: `sed -n '24,32p' .agent/teams/alpha-research/shared-context.template.yaml`

Expected output shows the `lead_post_promote` skills entries ending around line 28 and `cost_model:` starting around line 30.

- [ ] **Step 2: Insert the scope section using Edit tool**

Use Edit with:
- `old_string`: `    - .agent/skills/hft-production-audit/SKILL.md        # Post-deploy 7-plane sweep

# --- Cost Model (update if fee structure changes) ---`
- `new_string`: `    - .agent/skills/hft-production-audit/SKILL.md        # Post-deploy 7-plane sweep

# --- Scope (Autonomous Loop — maker/taker research) ---
# Referenced by Researcher T1 (proposal filtering) and Devil's Advocate T2 Kill Checklist item 0.
scope:
  mode: "execution_broad"           # Scope C from design spec Q4
  allowed_types:
    - pure_maker                    # R47 variants, spread-gate, depth-skew, queue-position-aware
    - pure_taker                    # impact-aware, latency-budget, burst-triggered, news-window
    - hybrid                        # maker-then-taker conversion, taker-cover-maker
    - exec_support_signal           # fill-rate predictor, adverse-selection filter, queue-decay forecaster
    - options_mm                    # TXO MM variants (ElectronicEye-class)
    - cross_instrument_mm           # TXF/TMF pair MM, cross-instrument MM
  forbidden:
    - pure_directional_alpha        # tick-to-hour TAIFEX exhausted (see taifex-alpha-kill-criteria)
    - daily_horizon_directional     # long hold not in loop scope
    - twse_stock_arbitrage          # R31 structural kill (58.5 bps RT cost)
    - any_match_in_killed_directions  # blacklist enforced via overlap check

# --- Cost Model (update if fee structure changes) ---`

- [ ] **Step 3: Verify insertion**

Run: `grep -A 2 "^scope:" .agent/teams/alpha-research/shared-context.template.yaml`

Expected output:
```
scope:
  mode: "execution_broad"           # Scope C from design spec Q4
  allowed_types:
```

Also run: `grep -c "forbidden:" .agent/teams/alpha-research/shared-context.template.yaml` — expected: `1`.

- [ ] **Step 4: Commit**

```bash
git add .agent/teams/alpha-research/shared-context.template.yaml
git commit -m "feat(alpha-research): add scope section to shared-context template

Scope C (execution_broad) declares allowed types (pure_maker, pure_taker,
hybrid, exec_support_signal, options_mm, cross_instrument_mm) and forbidden
types (directional, daily-horizon, twse-stock-arb, killed-directions match).
Referenced by Researcher T1 proposal filtering and Devil's Advocate T2
Kill Checklist item 0."
```

---

## Task 2: Update researcher.md (scope read + regen behavior)

**Files:**
- Modify: `.agent/teams/alpha-research/roles/researcher.md`

- [ ] **Step 1: Read current file tail to find insertion point**

Run: `sed -n '28,40p' .agent/teams/alpha-research/roles/researcher.md`

Expected to see the "Your Boundaries" section that ends with "❌ Do NOT propose tick-to-hour directional alphas on TAIFEX (structurally exhausted)".

- [ ] **Step 2: Strengthen the Your Boundaries section with scope reference**

Use Edit with:
- `old_string`: `- ❌ Do NOT propose tick-to-hour directional alphas on TAIFEX (structurally exhausted)`
- `new_string`: `- ❌ Do NOT propose tick-to-hour directional alphas on TAIFEX (structurally exhausted)
- ❌ Do NOT propose any candidate whose type is not in the shared-context \`scope.allowed_types\` list or which matches a rule in \`scope.forbidden\`. The \`scope\` section is the declarative source of truth for what is in-scope for the autonomous loop; read it before every proposal.`

- [ ] **Step 3: Add the regen behavior section at the end of the file**

Use Edit with:
- `old_string`: `## Round Context

{SHARED_CONTEXT}`
- `new_string`: `## Round Context

{SHARED_CONTEXT}

## Regen Sub-Task (T8-REGEN, when invoked by Lead)

When the Team Lead invokes the regen sub-flow (pool ≤ 2 and regen_count < 3), you are given a **regen context** containing: the last 5 rounds' kill_reasons, the last 3 PROMOTEd candidate IDs, and the full \`killed_directions\` blacklist.

In regen mode your output is exactly 5–10 new candidates across \`scope.allowed_types\`. Each must still pass the 3-question pre-research gate from \`taifex-alpha-kill-criteria\`. Do not rehash any PROMOTEd or recently KILLed candidate. Output format is identical to the initial-proposal format above. The Devil's Advocate runs a quick sanity pass (not the full Kill Checklist) on each candidate; individual candidate rejection does not abort the regen.`

- [ ] **Step 4: Verify**

Run: `grep -c "scope.allowed_types" .agent/teams/alpha-research/roles/researcher.md`

Expected: `1` or higher.

Run: `grep "T8-REGEN" .agent/teams/alpha-research/roles/researcher.md`

Expected: one line containing `## Regen Sub-Task (T8-REGEN, when invoked by Lead)`.

- [ ] **Step 5: Commit**

```bash
git add .agent/teams/alpha-research/roles/researcher.md
git commit -m "feat(alpha-research): add scope enforcement and regen sub-task to researcher role

- Ban candidates outside shared-context scope.allowed_types or matching
  scope.forbidden rules.
- Add T8-REGEN sub-task: when Lead triggers pool regen, produce 5-10 new
  candidates under scope constraints, avoiding recent PROMOTEs and KILLs."
```

---

## Task 3: Update devils-advocate.md (Tier 0 scope check + regen sanity pass)

**Files:**
- Modify: `.agent/teams/alpha-research/roles/devils-advocate.md`

The existing file has Tier 1 (Hard Kill, H1–H6), Tier 2 (Statistical Rigor, S1–S6), Tier 3 (Platform Compatibility, P1–P3), then the Required Output Format block ending at `Tier 2 FAIL count: {N}`, then `## Round Context` and `{SHARED_CONTEXT}`. We add a new **Tier 0: Scope** above Tier 1, a matching output line, and a `## Regen Sanity Pass` section after the existing `{SHARED_CONTEXT}` placeholder.

- [ ] **Step 1: Insert Tier 0 table above Tier 1**

Use Edit with:
- `old_string`:
```
## Kill Checklist

### Tier 1: Hard Kill (ANY single FAIL = immediate REJECT)
```
- `new_string`:
```
## Kill Checklist

### Tier 0: Scope (ANY FAIL = immediate REJECT, skip T3 revision)

| ID | Check | Kill Criteria |
|----|-------|---------------|
| S0 | **Scope compliance** | Candidate's type is NOT in shared-context `scope.allowed_types`, OR matches any rule in `scope.forbidden` (including `any_match_in_killed_directions`). An out-of-scope candidate returns to the Researcher as a hard REJECT — no revision loop. |

### Tier 1: Hard Kill (ANY single FAIL = immediate REJECT)
```

- [ ] **Step 2: Add Tier 0 line to the Required Output Format block**

Use Edit with:
- `old_string`:
```
## Kill Checklist Result — [Candidate Name]

### Tier 1: Hard Kill
```
- `new_string`:
```
## Kill Checklist Result — [Candidate Name]

### Tier 0: Scope
- [S0] Scope compliance: PASS/FAIL — {candidate type} in allowed_types? forbidden rules tripped? {specific rule if FAIL}

### Tier 1: Hard Kill
```

- [ ] **Step 3: Append the Regen Sanity Pass section after `{SHARED_CONTEXT}`**

Use Edit with:
- `old_string`:
```
## Round Context

{SHARED_CONTEXT}
```
- `new_string`:
```
## Round Context

{SHARED_CONTEXT}

## Regen Sanity Pass (T8-REGEN-3, quick triage)

When the Team Lead invokes the regen sub-flow, you run a **3-check sanity pass** on each candidate proposed by the Researcher — this is **not** the full Kill Checklist.

1. **Killed-directions check**: does the candidate hit any entry in `shared-context.killed_directions`? If yes → REJECT this candidate.
2. **Scope check**: is the candidate's type in `scope.allowed_types`, and does it clear all `scope.forbidden` rules? If no → REJECT this candidate.
3. **Quantitative-edge check**: does the candidate include a numeric edge estimate (bps / pts / IC)? If no → REJECT this candidate.

Individual rejection does not abort the regen; only surviving candidates are appended to `candidate_pool.json`. The full Kill Checklist (Tier 0 + H1–H6 + S1–S6) still runs at T2 when each candidate is picked for a real round.
```

- [ ] **Step 4: Verify**

Run:
```bash
grep -c "Tier 0" .agent/teams/alpha-research/roles/devils-advocate.md
grep -c "\[S0\]" .agent/teams/alpha-research/roles/devils-advocate.md
grep -c "Regen Sanity Pass" .agent/teams/alpha-research/roles/devils-advocate.md
```

Expected: `2` (header + Required Output line), `2` (table row + output line), `1`.

- [ ] **Step 5: Commit**

```bash
git add .agent/teams/alpha-research/roles/devils-advocate.md
git commit -m "feat(alpha-research): add Tier 0 scope check and regen sanity pass

- Tier 0 runs above Tier 1: out-of-scope candidates are rejected immediately,
  no T3 revision loop.
- Required Output Format gains a Tier 0 Scope block with S0 line.
- Regen Sanity Pass (3 checks: killed-directions, scope, quantitative edge)
  triages candidates during T8-REGEN; full Kill Checklist still runs at T2."
```

---

## Task 4: Create budget-guard.sh and its test harness

**Files:**
- Create: `.agent/teams/alpha-research/hooks/budget-guard.sh`
- Create: `.agent/teams/alpha-research/hooks/budget-guard_test.sh`

- [ ] **Step 1: Write the failing test harness first (TDD)**

Create `.agent/teams/alpha-research/hooks/budget-guard_test.sh` with:

```bash
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

# ---- Summary ----
echo
echo "Results: $pass passed, $fail failed"
(( fail == 0 ))
```

Make executable:

```bash
chmod +x .agent/teams/alpha-research/hooks/budget-guard_test.sh
```

- [ ] **Step 2: Run the test harness to confirm all cases fail (no hook yet)**

Run: `bash .agent/teams/alpha-research/hooks/budget-guard_test.sh`

Expected: All 9 cases fail (because `budget-guard.sh` does not exist yet). The harness itself should not crash; it should print FAIL lines and a summary like `Results: 0 passed, 9 failed` and exit 1.

If the harness crashes with "No such file" for the hook, that is also an acceptable red state — proceed to implementation.

- [ ] **Step 3: Write the hook implementation**

Create `.agent/teams/alpha-research/hooks/budget-guard.sh`:

```bash
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

# jq resolution (matches the pattern used by teammate-idle-check.sh)
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
if [[ -n "$started_at" ]]; then
    elapsed_h=$(( ( $(date +%s) - $(date -d "$started_at" +%s) ) / 3600 ))
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
```

Make executable:

```bash
chmod +x .agent/teams/alpha-research/hooks/budget-guard.sh
```

- [ ] **Step 4: Run the test harness to confirm all 9 cases pass**

Run: `bash .agent/teams/alpha-research/hooks/budget-guard_test.sh`

Expected output ending: `Results: 9 passed, 0 failed` and exit code 0.

If any case fails, fix the hook (or the test fixture that tripped it — the hook is the source of truth for behavior the spec describes). Re-run until green.

- [ ] **Step 5: Sanity syntax check**

Run: `bash -n .agent/teams/alpha-research/hooks/budget-guard.sh`

Expected: no output, exit 0.

- [ ] **Step 6: Commit**

```bash
git add .agent/teams/alpha-research/hooks/budget-guard.sh \
        .agent/teams/alpha-research/hooks/budget-guard_test.sh
git commit -m "feat(alpha-research): add budget-guard TaskCompleted hook

- Halts the autonomous loop when STOP file is present or any of 5 budget
  triggers fires (runtime, rounds, PROMOTEs, consecutive KILLs, missing
  budget.json despite progress).
- Follows the existing hook protocol (stdin JSON, exit 2 = reject) used by
  task-completed-gate.sh and teammate-idle-check.sh.
- Ships with a 9-case bash test harness (budget-guard_test.sh)."
```

---

## Task 5: Update README.md — T0–T9 chain + new sections

**Files:**
- Modify: `.agent/teams/alpha-research/README.md`

- [ ] **Step 1: Replace the Task Chain block (lines 49–61 in current file)**

Use Edit with:
- `old_string`:
```
## Task Chain

```
T1: [Researcher]       Literature search + proposals
T2: [Devil's Advocate]  Kill Checklist review            (blockedBy: T1)
T3: [Researcher]        Revise if WARN only              (blockedBy: T2, conditional)
T4: [Executor]          Implement approved candidate     (blockedBy: T2 PASS)
T5: [Executor]          Backtest + scorecard             (blockedBy: T4)
T6: [Devil's Advocate]  Gate C statistical review        (blockedBy: T5)
T7: [Lead]              Final KILL/PROMOTE               (blockedBy: T6)
```

REJECT at T2 = KILL candidate or KILL round. No revision loop.
```
- `new_string`:
```
## Task Chain (Autonomous Loop — Maker/Taker 24h Mode)

```
T0: [Lead]              Init or Resume; write budget.json / candidate_pool.json (fresh run)
                        or read resume_checkpoint.json (--resume)
T1: [Researcher]        Literature search + proposals (within scope.allowed_types)
T2: [Devil's Advocate]  Kill Checklist review — item S0 (scope) + H1-H6 + S1-S6  (blockedBy: T1)
T3: [Researcher]        Revise if WARN only                                      (blockedBy: T2, conditional)
T4: [Executor]          Implement approved candidate                             (blockedBy: T2 PASS)
T5: [Executor]          Backtest + scorecard                                     (blockedBy: T4)
T6: [Devil's Advocate]  Gate C statistical review                                (blockedBy: T5)
T7: [Lead]              Record round verdict (KILL / PROMOTE / POST_PROMOTE);
                        run Tie-break protocol if 2-round deadlock               (blockedBy: T6)
T8: [Lead]              Pool maintenance:
                          - PROMOTE → run hft-strategy-lifecycle scaffold → shadow
                          - pool ≤ 2 and regen_count < 3 → invoke T8-REGEN
                          - update candidate_pool.json                           (blockedBy: T7)
T9: [Lead]              Next-round or halt (budget-guard hook enforces):
                          - trigger hit → write final_summary.md, PAUSE
                          - else → update resume_checkpoint.json, pop next
                                   candidate, loop back to T1                    (blockedBy: T8)
```

REJECT at T2 = KILL candidate → skip to T8 (no revision loop within a round).
Lead does NOT wait for user confirmation between stages. Each stage emits
artifacts under `outputs/team_artifacts/alpha-research/round-<N>/artifacts/`
so completed stages can be detected and skipped on --resume.
```

- [ ] **Step 2: Add Resume section right after Task Chain**

Use Edit with:
- `old_string`: the closing of the new Task Chain block (the paragraph ending with "so completed stages can be detected and skipped on --resume.") followed by `

## Post-Team: Promotion Path`

- `new_string`: same closing, then the Resume section, then `

## Post-Team: Promotion Path`:

```

## Resume Semantics

`/alpha-research --resume` reads `outputs/team_artifacts/alpha-research/resume_checkpoint.json` at T0:

- **Present and fresh** (`updated_at` within 24 h, `budget.json.started_at` matches): restart at `current_round` / `current_stage`; stages with existing artifacts under `round-<N>/artifacts/` are skipped.
- **Stale or inconsistent** (missing, > 24 h, or budget mismatch): Lead prints a chat-visible warning and starts a fresh run (new `budget.json`, empty pool/progress). No silent recovery.

Resume does not re-run stages that produced artifacts; it resumes the first incomplete stage of `current_round`.

## Pool Regen Protocol (T8-REGEN)

Triggered when `candidate_pool.json.pool` has ≤ 2 unused candidates and `regen_count < 3`.

1. **Lead** builds regen context: last 5 rounds' kill_reasons, last 3 PROMOTEd IDs, full killed_directions blacklist.
2. **Researcher** reads regen context, re-runs arXiv search, produces 5–10 candidates under `scope.allowed_types`. Each must pass the 3-question pre-research gate.
3. **Devil's Advocate** runs a 3-check sanity pass per candidate: killed-directions, scope, quantitative edge. Per-candidate rejection; does not abort regen.
4. **Lead** appends surviving candidates to `candidate_pool.json`, increments `regen_count`, updates `last_regen_at`. If 0 surviving and `regen_count == 3`: hard-stop → write `final_summary.md` and PAUSE.

## Tie-break Protocol (T7 deadlock)

Triggered only when Devil's Advocate and Executor give opposite verdicts on the same gate and 2 rounds of dialog fail to resolve.

1. Lead declares deadlock and requires both parties to submit `round-<N>/artifacts/<role>_final_position.md` (core claim + ≤ 3 evidence bullets, each tagged with source type) within 60 minutes.
2. Lead scores evidence: `code_output` = 3, `named_trap` = 2, `logic` = 1, `speculation` = 0. If either side has > 1 speculation bullets, the other side auto-wins.
3. Lead writes the Tie-break section in `round-<N>/summary.md`: both positions verbatim, weight breakdown table, verdict + rationale. The tie-break binds only the current round — no precedent.

User may override any tie-break post-hoc by editing `round-<N>/summary.md` and appending a `tie_break_override` event to `progress.jsonl`.

## Budget-guard Hook

`.agent/teams/alpha-research/hooks/budget-guard.sh` is a `TaskCompleted` hook registered in `.claude/settings.local.json`. It runs on every task completion within `alpha-research*` teams and halts (exit 2) when any of these fire:

- STOP file present at `outputs/team_artifacts/alpha-research/STOP`
- Runtime ≥ `budget.json.max_runtime_hours` (default 24)
- Completed rounds ≥ `budget.json.max_rounds` (default 20)
- Cumulative PROMOTEs ≥ `budget.json.max_promotes` (default 3)
- Last N rows of `progress.jsonl` are all KILLs where N = `max_consecutive_kills` (default 8)

To stop the loop on demand: `echo STOP > outputs/team_artifacts/alpha-research/STOP`.

Tests: `bash .agent/teams/alpha-research/hooks/budget-guard_test.sh` (9 cases).
```

- [ ] **Step 3: Verify all new headers**

Run:
```bash
for section in "Autonomous Loop — Maker/Taker 24h Mode" "Resume Semantics" "Pool Regen Protocol" "Tie-break Protocol" "Budget-guard Hook"; do
    grep -c "$section" .agent/teams/alpha-research/README.md
done
```

Expected: each prints `1` (a `0` means the section is missing).

- [ ] **Step 4: Commit**

```bash
git add .agent/teams/alpha-research/README.md
git commit -m "docs(alpha-research): document autonomous loop task chain and protocols

- Task chain grows from T1-T7 to T0-T9 with Lead-owned init/maintenance/halt stages.
- New sections: Resume Semantics, Pool Regen Protocol (T8-REGEN), Tie-break
  Protocol (T7 deadlock), Budget-guard Hook.
- Explicit: Lead does not wait for user confirmation between stages in loop mode."
```

---

## Task 6: Rewrite `.claude/commands/alpha-research.md`

**Files:**
- Rewrite: `.claude/commands/alpha-research.md`

- [ ] **Step 1: Read the current file to preserve anything worth keeping**

Run: `cat .claude/commands/alpha-research.md`

Compare section-by-section against the new content below to confirm you are not losing any instruction the new version does not cover.

- [ ] **Step 2: Write the new command file**

Use Write to replace the entire file contents with:

````markdown
---
description: Launch Alpha Research agent team — autonomous 24h maker/taker research loop. Team Lead actively drives candidate queue and coordinates triangular checks (Researcher ↔ Challenger ↔ Execution) without per-stage user confirmation. Use `--resume` to continue after interruption.
---

# Alpha Research Team (Autonomous Maker/Taker Loop)

建立 Alpha Research team:
方向 / 第一個候選: $ARGUMENTS  （留空 → Team Lead 自主從 maker/taker 候選池挑第一個）

支援 `--resume` 旗標（寫在 `$ARGUMENTS` 最前面）：Lead 在 T0 讀 `outputs/team_artifacts/alpha-research/resume_checkpoint.json`，若新鮮（updated_at < 24h 且 budget.json 匹配）則從 `current_round` / `current_stage` 繼續；否則在 chat 警告並開 fresh run。

## 運行模式

**Autonomous Continuous Mode（預設）** — Team Lead 持續推進研究，不需要每個 stage 等使用者確認。設計運行時間：最長 **24 小時** 或 budget 用盡（見 README.md 的 Budget-guard Hook 章節）。

**Scope 硬性限制**（scope C from design spec Q4）：本指令只處理 maker / taker / hybrid / exec-support signals / options MM / cross-instrument MM。禁止的類別由 `shared-context.template.yaml` 的 `scope.forbidden` 定義（pure_directional_alpha、daily_horizon_directional、twse_stock_arbitrage、any_match_in_killed_directions）。

## Skill-Integrated Team Structure

每個角色啟動前必須讀取指定 skills（見 `.agent/teams/alpha-research/README.md` 的 Skill Pipeline 章節）。

### Team Lead (Sonnet, Active Driver)

**必讀 skills**: `hft-mm-design`, `hft-strategy-lifecycle`, `hft-backtest-calibration`, `taifex-alpha-kill-criteria`, `taifex-market-structure`

職責：
1. **啟動時建立 maker/taker 候選池**（≥ 5, ≤ 15）寫入 `outputs/team_artifacts/alpha-research/candidate_pool.json`；若 `$ARGUMENTS` 非空用它作第一個 round，否則從池頂 pop。
2. **主動驅動**：每 stage 結束後直接進下一 stage，不向使用者確認。
3. **Context 注入**：Researcher T1 開始前附上 R47 maker 三層架構（`hft-mm-design`）、taker 成本牆（`taifex-market-structure`，RT 4.68 pts、TMFD6 median spread 4 pts）、最近 3 round KILL 摘要。
4. **Checkpoint**：每 round 結束寫 `round-<N>/summary.md` + append `progress.jsonl`，每 stage 結束更新 `resume_checkpoint.json`。
5. **Budget guard**：budget-guard hook 會在每個 TaskCompleted 觸發；若 hook exit 2，Lead 必須寫 `final_summary.md` 並 PAUSE。
6. **Tie-break（有限授權）**：Challenger vs Executor 同 gate 2 輪仍無共識時，跑 Tie-break 協定（見 README）——evidence-weighted 裁定並寫入 round summary。
7. **PROMOTE 路徑**：Shadow scaffold 完成後繼續 pop 下一候選（shadow → live 維持手動）；達 `max_promotes` 則 PAUSE。
8. **T8-REGEN**：pool ≤ 2 且 regen_count < 3 → 觸發 Researcher 再生子流程（見 README 的 Pool Regen Protocol）。

禁止（硬規則）：
- ❌ 單方面宣告 APPROVE/REJECT/PASS/FAIL — 那是 Researcher/Challenger/Execution 的裁定
- ❌ 跳過 Challenger 的 Kill Checklist (S0 + H1-H6 + S1-S6)
- ❌ 篡改或過濾 `make research` 的程式碼輸出
- ❌ 為了跑滿 24h 而硬推已被 scope.forbidden 排除或 killed_directions 命中的方向

### Researcher (Opus)

**必讀 skills**: `taifex-alpha-kill-criteria`, `taifex-market-structure`；maker 候選再加讀 `hft-mm-design`
讀取 `.agent/teams/alpha-research/roles/researcher.md`。候選必須符合 `shared-context.template.yaml` 的 `scope` 節，Overlap check 對照 `killed_directions`。T8-REGEN 時只產 5–10 個新候選，不鋪陳完整提案。

### Devil's Advocate (Opus)

**必讀 skills**: `taifex-alpha-kill-criteria`, `hft-backtest-calibration`
讀取 `.agent/teams/alpha-research/roles/devils-advocate.md`。Kill Checklist 第一項 S0 先判 scope，再走 H1-H6 + S1-S6。T8-REGEN 時跑 Regen Sanity Pass（3 項）。

### Executor (Opus)

**必讀 skills**: `hft-strategy-sdk`, `hft-backtest-calibration`, `hft-test-hft`, `taifex-market-structure`；maker 候選再加讀 `hft-mm-design`
讀取 `.agent/teams/alpha-research/roles/executor.md`。契約不變。

## Rules

1. Challenger 和 Executor 各自獨立 APPROVE 才能推進。
2. 所有 gate 結果來自 `make research` 程式碼輸出，不是任何人的判斷。
3. 每 stage 結束自動進下一 stage；每 round 結束寫 summary + progress。
4. Team Lead 禁止覆寫個別 gate 判定；僅在 2-round 僵局時做 evidence-weighted tie-break（rationale 必須列點並寫入 round summary）。
5. 僵局處理：見 Tie-break Protocol 章節。
6. 每 round 結束產出 `outputs/team_artifacts/alpha-research/round-<N>/summary.md` + append `progress.jsonl`。
7. PROMOTE 後 Lead 跑 post-round 流程（`hft-strategy-lifecycle` → `hft-release-gate` → `hft-production-audit`），完成後自動進下一 round（未達 `max_promotes` 時）。
8. Budget-guard hook 觸發 → Lead 寫 `final_summary.md`（aggregate verdicts、KILL reasons、下輪建議）並 PAUSE（不自動進下一 round）。
9. 使用者隨時可 `echo STOP > outputs/team_artifacts/alpha-research/STOP`；Lead 下一個 TaskCompleted 就會被 hook 擋下。
10. 連續 `max_consecutive_kills` KILL → hook 自動觸發停止（「方向性耗盡」訊號，需人類介入調整 scope 或候選池）。
````

- [ ] **Step 3: Verify**

Run: `grep -c "Autonomous Maker/Taker Loop\|--resume\|T8-REGEN\|Tie-break" .claude/commands/alpha-research.md`

Expected: `4` or higher (one match per distinct marker; file may mention some markers twice).

Run: `head -3 .claude/commands/alpha-research.md`

Expected first line: `---` and second line starts with `description: Launch Alpha Research agent team — autonomous 24h maker/taker research loop`.

- [ ] **Step 4: Commit**

```bash
git add .claude/commands/alpha-research.md
git commit -m "feat(alpha-research): rewrite slash command as autonomous maker/taker loop

- New description with --resume flag.
- Team Lead becomes active driver: maintains candidate pool, auto-advances
  stages, tie-breaks 2-round deadlocks (evidence-weighted), runs T8-REGEN
  when pool low.
- Scope C enforced declaratively via shared-context.scope section.
- 10 rules; Lead cannot override individual gates, cannot skip Kill Checklist."
```

---

## Task 7: Register `budget-guard.sh` in settings.local.json

**Files:**
- Modify: `.claude/settings.local.json` (hooks.TaskCompleted array)

- [ ] **Step 1: Confirm the existing hooks block structure**

Run: `jq '.hooks' .claude/settings.local.json`

Expected structure:
```json
{
  "TaskCompleted": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "bash .agent/teams/alpha-research/hooks/task-completed-gate.sh"
        }
      ]
    }
  ],
  "TeammateIdle": [...]
}
```

- [ ] **Step 2: Add `budget-guard.sh` to the TaskCompleted inner hooks array**

Use Edit with:
- `old_string`:
```
    "TaskCompleted": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash .agent/teams/alpha-research/hooks/task-completed-gate.sh"
          }
        ]
      }
    ],
```
- `new_string`:
```
    "TaskCompleted": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash .agent/teams/alpha-research/hooks/task-completed-gate.sh"
          },
          {
            "type": "command",
            "command": "bash .agent/teams/alpha-research/hooks/budget-guard.sh"
          }
        ]
      }
    ],
```

- [ ] **Step 3: Validate JSON**

Run: `jq 'empty' .claude/settings.local.json && echo "OK"`

Expected: prints `OK`. If jq reports a parse error, the edit broke JSON — fix before proceeding.

Run: `jq '.hooks.TaskCompleted[0].hooks | length' .claude/settings.local.json`

Expected: `2`.

Run: `jq -r '.hooks.TaskCompleted[0].hooks[].command' .claude/settings.local.json`

Expected output:
```
bash .agent/teams/alpha-research/hooks/task-completed-gate.sh
bash .agent/teams/alpha-research/hooks/budget-guard.sh
```

- [ ] **Step 4: Commit**

```bash
git add .claude/settings.local.json
git commit -m "chore(claude): register budget-guard TaskCompleted hook

Fires alongside task-completed-gate.sh on every TaskCompleted event inside
alpha-research* teams. Halts the autonomous loop on STOP file or budget
exhaustion."
```

---

## Task 8: Smoke test (manual)

**Files:**
- Create: `outputs/team_artifacts/alpha-research/smoke-test-2026-04-17.md` (ephemeral test log — not committed)

- [ ] **Step 1: Prepare ephemeral artifacts dir**

Run:
```bash
mkdir -p outputs/team_artifacts/alpha-research
rm -f outputs/team_artifacts/alpha-research/STOP
ls outputs/team_artifacts/alpha-research/
```

Expected: empty (or only files from prior unrelated runs).

- [ ] **Step 2: Direct invocation smoke test of the hook**

Run:
```bash
# Non-alpha team → exit 0
echo '{"team_name":"other","teammate_name":"x"}' | bash .agent/teams/alpha-research/hooks/budget-guard.sh
echo "exit=$?"
```

Expected: `exit=0`.

```bash
# STOP file → exit 2
touch outputs/team_artifacts/alpha-research/STOP
echo '{"team_name":"alpha-research-R99","teammate_name":"lead"}' | bash .agent/teams/alpha-research/hooks/budget-guard.sh
echo "exit=$?"
rm -f outputs/team_artifacts/alpha-research/STOP
```

Expected stderr: `HALT: STOP file present ...`; `exit=2`.

- [ ] **Step 3: End-to-end smoke test via the slash command (manual)**

Launch a fresh Claude Code session and run:
```
/alpha-research r47-spread-gate-tune
```

Observe:
- Lead writes `outputs/team_artifacts/alpha-research/budget.json` with current timestamp.
- Lead writes `outputs/team_artifacts/alpha-research/candidate_pool.json` with ≥ 5 candidates.
- Researcher T1 output includes `## Regen Sub-Task` acknowledgement (or at minimum references `scope.allowed_types`).

Then trigger halt:
```bash
echo STOP > outputs/team_artifacts/alpha-research/STOP
```

On the Lead's next task completion, the hook must fire exit 2 and Lead must write `final_summary.md`.

- [ ] **Step 4: Clean up STOP file and write smoke-test log**

Run:
```bash
rm -f outputs/team_artifacts/alpha-research/STOP
```

Write a short log to `outputs/team_artifacts/alpha-research/smoke-test-2026-04-17.md` recording:
- Which manual steps passed / failed
- The final PAUSE trigger (STOP file) behavior
- Any issues observed

Do not commit `smoke-test-2026-04-17.md` (it's an ephemeral run log — add to `.gitignore` if the file pollutes git status).

- [ ] **Step 5: Verify `outputs/team_artifacts/alpha-research/` is gitignored**

Run: `git check-ignore -v outputs/team_artifacts/alpha-research/budget.json`

Expected: a line citing `outputs/` or `outputs/team_artifacts/` in `.gitignore` (path is already ignored).

If not ignored, add `outputs/team_artifacts/alpha-research/` to `.gitignore` and commit:

```bash
git add .gitignore
git commit -m "chore: gitignore alpha-research autonomous loop artifacts"
```

---

## Self-review (run after writing — already completed)

1. **Spec coverage** — every spec section has a task:
   - Scope C YAML → Task 1
   - Researcher scope read + regen → Task 2
   - Devil's Advocate S0 + regen sanity → Task 3
   - Budget-guard hook (spec §Budget-guard) → Task 4
   - Task chain T0–T9, Resume, Regen, Tie-break, Budget-guard sections → Task 5
   - Command file rewrite → Task 6
   - Hook registration → Task 7
   - Manual smoke test → Task 8

2. **Placeholder scan** — no TBD / TODO / "implement later". Every Edit step now ships with concrete `old_string` / `new_string` anchors that match the current file contents verbatim (verified against Read output during plan authoring).

3. **Type consistency** — file paths, hook name (`budget-guard.sh`), scope field names (`allowed_types`, `forbidden`, `mode`), budget field names (`started_at`, `max_runtime_hours`, `max_rounds`, `max_promotes`, `max_consecutive_kills`), and artifact-dir path (`outputs/team_artifacts/alpha-research/`) are identical across all tasks and match the spec verbatim.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-17-alpha-research-autonomous-loop.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
