# Alpha Research Autonomous Maker/Taker Loop — Design

**Date**: 2026-04-17
**Status**: Brainstorming — Approved, pending spec review
**Scope**: `/alpha-research` slash command + `.agent/teams/alpha-research/` supporting files
**Supersedes (partially)**: `docs/superpowers/specs/2026-04-02-alpha-research-team-redesign.md`

## Context

The existing `/alpha-research` command spawns a 4-role team (Lead, Researcher, Devil's Advocate, Executor) that runs one alpha-research round (T1–T7) and then stops. Every stage transition requires the user's manual confirmation, and the Team Lead has no judgment authority — it can only dispatch and summarize.

The user wants `/alpha-research` to function as a long-running (up to 24 h) autonomous research loop focused on **maker/taker execution strategies** (scope C — see Q4 below). Team Lead becomes an active driver: it queues candidates, coordinates stage transitions without per-stage user confirmation, breaks deadlocks with bounded authority, and auto-regenerates the candidate pool when low. The design is checkpoint-resumable: if the Claude Code session dies, `/alpha-research --resume` picks up from the last completed stage.

Prior attempt at this change was reverted, signaling the need for a vetted design before touching files again. This spec records the seven key decisions captured during brainstorming (Q1–Q7) and the implementation architecture.

## Decisions (from brainstorming)

| # | Topic | Choice | Note |
|---|---|---|---|
| Q1 | Execution model | **C** — Checkpoint-resumable same-session loop | `/alpha-research --resume` recovers from disk state |
| Q2 | Candidate-pool refill | **B** — Autonomous regen when pool ≤ 2 | Max 3 refills, then hard-stop |
| Q3 | Team Lead authority | **B** — Evidence-weighted tie-break (2-round deadlock only) | Lead cannot override individual APPROVE/REJECT |
| Q4 | Scope | **C** — Broad: pure maker, pure taker, hybrid, exec-support signals, options MM, cross-instrument MM | Excludes pure directional, daily-horizon, TWSE arbitrage |
| Q5 | PROMOTE behavior | **B** — Continue loop; shadow → live stays manual | Matches the "no auto-deploy" feedback |
| Q6 | Budget defaults | 24 h / 20 rounds / 3 PROMOTEs / 8 consecutive KILLs / STOP file | |
| Q7 | State architecture | **C** — Heavy disk + budget-guard hook | Hook hard-enforces budget/STOP on every Lead task completion |

## Architecture

### State files

All state lives under `outputs/team_artifacts/alpha-research/`:

```
outputs/team_artifacts/alpha-research/
├── budget.json              # started_at, max_runtime_hours, max_rounds, max_promotes, max_consecutive_kills
├── candidate_pool.json      # pool, used, regen_count, last_regen_at
├── progress.jsonl           # append-only; one line per completed round
├── resume_checkpoint.json   # current_round, current_stage, next_candidate, written after every stage
├── STOP                     # user-created sentinel; its presence halts the loop
├── final_summary.md         # aggregate written when budget/STOP triggers
└── round-<N>/
    ├── summary.md           # verdict, kill/promote rationale, tie-break record if any
    └── artifacts/           # per-stage intermediate files (Researcher proposals, Challenger checklist, Executor scorecards)
```

`budget.json` schema:

```json
{
  "started_at": "2026-04-17T14:00:00+08:00",
  "max_runtime_hours": 24,
  "max_rounds": 20,
  "max_promotes": 3,
  "max_consecutive_kills": 8
}
```

`candidate_pool.json` schema:

```json
{
  "pool": [
    { "id": "r47-spread-gate-tune", "type": "pure_maker", "hypothesis": "...", "source": "initial" }
  ],
  "used": [
    { "id": "...", "round": 1, "verdict": "KILL", "kill_reason": "cost floor violation" }
  ],
  "regen_count": 0,
  "last_regen_at": null
}
```

`progress.jsonl` line schema (one JSON object per line, append-only):

```json
{"round":1,"candidate":"r47-spread-gate-tune","verdict":"KILL","kill_reason":"IC detrended < 0.01","elapsed_min":42,"cumulative_h":0.7,"tie_break":false}
```

`resume_checkpoint.json` schema:

```json
{
  "current_round": 5,
  "current_stage": "T4",
  "next_candidate_if_round_completes": "r47-depth-skew-maker",
  "updated_at": "2026-04-17T18:15:30+08:00"
}
```

### Lead loop (T0–T9, resume-aware)

```
T0  [Lead]   Init or Resume
             - No --resume: write budget.json + initial candidate_pool.json + empty progress.jsonl
             - --resume: read resume_checkpoint.json, jump to current_round / current_stage
             - Budget-guard hook runs before every Lead task completion (enforces budget + STOP)
T1  [Researcher]   Literature search + proposals (within scope C)    [blockedBy: T0]
T2  [Devil's Adv]  Kill Checklist (H1–H6 + S1–S6) + scope check (item 0)  [blockedBy: T1]
T3  [Researcher]   Revise if WARN only (conditional)                  [blockedBy: T2]
T4  [Executor]     Implement approved candidate                       [blockedBy: T2 PASS]
T5  [Executor]     Backtest + scorecard                               [blockedBy: T4]
T6  [Devil's Adv]  Gate C statistical review                          [blockedBy: T5]
T7  [Lead]         Verdict (KILL / PROMOTE / POST_PROMOTE)            [blockedBy: T6]
                   - Deadlock → Tie-break protocol (§Tie-break)
                   - Write round-<N>/summary.md + append progress.jsonl
T8  [Lead]         Pool maintenance                                   [blockedBy: T7]
                   - If PROMOTE: run hft-strategy-lifecycle scaffold → shadow
                   - If pool ≤ 2 and regen_count < 3: invoke T8-REGEN (§Pool regen)
                   - Update candidate_pool.json
T9  [Lead]         Next-round or halt                                 [blockedBy: T8]
                   - Budget-guard hook enforces: if any trigger hit → write final_summary.md, PAUSE
                   - Else: update resume_checkpoint.json → pop next candidate → jump to T1
```

### Resume semantics

`/alpha-research --resume` reads `resume_checkpoint.json` at T0:

- **Present and fresh** (`updated_at` within 24 h, `budget.json` still parseable and its `started_at` matches the run that wrote the checkpoint): restart at `current_round` / `current_stage`. Completed stages (detected via presence of their artifact files under `round-<N>/artifacts/`) are skipped.
- **Stale or inconsistent** (missing, `updated_at` > 24 h old, or `budget.json` mismatch): Lead emits a user-visible warning in chat (`"Resume requested but checkpoint is stale/inconsistent — starting fresh run"`) and starts a new run (writes new `budget.json`, empties pool/progress). No silent recovery.

Resume does **not** re-run stages that already produced artifacts; it resumes the first incomplete stage of `current_round`.

### Pool regen protocol (T8-REGEN, triggered when pool ≤ 2 and regen_count < 3)

```
T8-REGEN-1  [Lead]          Build regen context:
                            - Last 5 rounds' kill_reason (from progress.jsonl)
                            - Last 3 PROMOTEd candidate_ids (avoid repeats)
                            - Full killed_directions blacklist from shared-context.yaml
T8-REGEN-2  [Researcher]    Read regen context → re-run arXiv literature search (multi-angle queries)
                            Produce 5–10 new candidates across scope C types
                            Each candidate must pass 3-question pre-research gate
T8-REGEN-3  [Devil's Adv]   Quick sanity pass (not the full Kill Checklist — only 3 checks):
                            - Hits killed_directions? → reject that candidate
                            - Within scope C allowed_types? → reject if not
                            - Has quantitative edge estimate? → reject if missing
                            Rejection is per-candidate, not per-regen.
T8-REGEN-4  [Lead]          Append surviving candidates to candidate_pool.json
                            regen_count += 1; last_regen_at = now
                            If 0 surviving and regen_count == 3: hard-stop → write final_summary + PAUSE
```

### Scope enforcement

Scope rules live in `shared-context.template.yaml` under a new `scope` section:

```yaml
scope:
  mode: "execution_broad"          # scope C
  allowed_types:
    - pure_maker                   # R47 variants, spread-gate, depth-skew
    - pure_taker                   # impact-aware, latency-budget, burst-triggered
    - hybrid                       # maker-then-taker, taker-cover-maker
    - exec_support_signal          # fill-rate predictor, adverse-selection filter, queue-decay forecaster
    - options_mm                   # TXO MM variants (ElectronicEye-class)
    - cross_instrument_mm          # TXF/TMF pair, cross-instrument MM
  forbidden:
    - pure_directional_alpha       # tick-to-hour TAIFEX exhausted
    - daily_horizon_directional    # not in loop scope
    - twse_stock_arbitrage         # R31 structural kill
    - any_match_in_killed_directions  # blacklist enforced
```

Lead injects this section into Researcher's T1 prompt. Challenger's T2 Kill Checklist gains a new item 0: "Does the candidate's type violate `scope.allowed_types` or match any `forbidden` rule?" — a match is immediate REJECT and skips T3 (no revision loop for out-of-scope candidates).

### Tie-break protocol (T7, 2-round deadlock on same gate)

Triggered only when Challenger and Executor give opposite judgments on the same gate and 2 rounds of dialog fail to resolve.

```
TB-1  [Lead]   Declare deadlock. Require both parties to submit final_position.md
               (under round-<N>/artifacts/) within 60 min:
               - Core claim (APPROVE or REJECT)
               - Up to 3 evidence bullets
               - Each evidence tagged with source type (see table)
TB-2  [Lead]   Weight-scored sum:
               | source type    | weight | examples                                           |
               |----------------|--------|----------------------------------------------------|
               | code_output    |   3    | make research output, scorecard numbers            |
               | named_trap     |   2    | hft-backtest-calibration / taifex-alpha-kill-criteria named rules |
               | logic          |   1    | reasoning, analogy, experience                     |
               | speculation    |   0    | "I think...", "should be..."                       |
               If one side has > 1 speculation bullets: auto-win for the other side.
TB-3  [Lead]   Write Tie-break section in round-<N>/summary.md:
               - Both final positions verbatim
               - Weight breakdown table
               - Verdict + rationale (bulleted)
               - Note: tie-break binds only current round; no precedent.
```

User can override any tie-break post-hoc by editing `round-<N>/summary.md` and appending a `tie_break_override` event to `progress.jsonl`.

### Budget-guard hook

New file `.agent/teams/alpha-research/hooks/budget-guard.sh`, registered in `.claude/settings.local.json` alongside the existing `task-completed-gate.sh` and `teammate-idle-check.sh`. The hook follows the same Claude Code hook protocol as the two existing hooks:

- **Trigger**: `TaskCompleted` event
- **Input**: JSON on stdin with fields `team_name`, `teammate_name`, `task_subject`, `task_description`
- **Exit 0**: allow completion
- **Exit 2**: reject completion (stderr goes back to the teammate as feedback)
- **Scope guard**: short-circuit exit 0 when `team_name` does not start with `alpha-research` (identical pattern to `task-completed-gate.sh`)

Budget checks run for **every** task completion within `alpha-research*` teams (the checks are cheap — JSON read + arithmetic — and fire-on-all is more robust than owner-based filtering). If any budget limit is hit, exit 2 with a clear stderr message instructing the teammate (or the Lead) to write `final_summary.md` and pause.

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
# or any budget limit is exceeded.

set -euo pipefail

# jq resolution (matches pattern used by teammate-idle-check.sh)
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

**Dependencies**: `jq` (already present at `/home/charlie/.local/bin/jq`, version 1.7.1). No new package install.

## File change inventory

| File | Action | Purpose |
|---|---|---|
| `.claude/commands/alpha-research.md` | Rewrite | New description; `--resume` flag; Lead-as-driver role; scope C reference; rules updated for loop |
| `.agent/teams/alpha-research/README.md` | Update | Task Chain T0–T9; Resume section; Pool regen section; Tie-break section; Budget-guard section |
| `.agent/teams/alpha-research/shared-context.template.yaml` | Add `scope` section | Scope C enforcement source |
| `.agent/teams/alpha-research/hooks/budget-guard.sh` | New | §Budget-guard hook |
| `.claude/settings.local.json` | Edit | Register `budget-guard.sh` |
| `.agent/teams/alpha-research/roles/researcher.md` | Minor | Must read shared-context `scope`; regen behavior in T8-REGEN |
| `.agent/teams/alpha-research/roles/devils-advocate.md` | Minor | Kill Checklist item 0 (scope); regen sanity-pass criteria |
| `.agent/teams/alpha-research/roles/executor.md` | Unchanged | Contract unchanged |

## Non-goals (YAGNI)

- No Python/Rust CLI changes (`--resume` is interpreted by the Lead prompt, not shell argparse)
- No Prometheus/metrics integration (24h research loop ≠ production service)
- No cross-session queue synchronization (checkpoint + single-session loop is enough)
- No changes to existing `task-completed-gate.sh` or `teammate-idle-check.sh`
- No changes to Executor role

## Testing strategy

Manual smoke test plan (no automated test for prompt/skill changes):

1. Run `/alpha-research` without args → Lead writes initial `budget.json`, `candidate_pool.json`, invokes Researcher T1.
2. Kill session mid-round (e.g., during T4). Check `resume_checkpoint.json` has `current_stage: "T4"` (or whatever stage was active).
3. Run `/alpha-research --resume` → Lead reads checkpoint, skips T1–T3, resumes at T4.
4. Touch `STOP` file mid-round → on next Lead TaskCompleted the budget-guard hook halts; Lead writes `final_summary.md`.
5. Simulate 8 KILLs in progress.jsonl (manually) → hook halts on next Lead TaskCompleted.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Researcher regenerates candidates that duplicate killed directions | Challenger's regen sanity-pass checks `killed_directions` blacklist explicitly; plus full Kill Checklist still runs at T2 |
| Lead abuses tie-break authority to short-circuit legitimate disagreement | User can override by editing `summary.md` post-hoc and appending `tie_break_override` event; Tie-break rationale is written verbatim so abuse is visible |
| Session crashes mid-regen | `candidate_pool.json` only updates after T8-REGEN-4; partial regen leaves pool untouched |
| Budget-guard hook false-positive halts (e.g., date parse failure) | Hook fails-safe by exiting 2; false halt means you lose at most the current round's pending completion; just manually fix `budget.json` and resume |
| Scope creep — "maker/taker" scope broadens over rounds | `scope.allowed_types` is declarative; Challenger item 0 enforces; any scope expansion requires editing shared-context YAML (visible in git) |

## Open questions

None — all Q1–Q7 answered during brainstorming. Spec ready for user review.

## Implementation handoff

Once user approves this spec, hand off to `superpowers:writing-plans` to produce a step-by-step implementation plan covering the 8 files in the inventory.
