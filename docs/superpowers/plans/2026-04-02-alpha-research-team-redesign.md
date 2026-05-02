# Alpha Research Agent Team Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the weak Researcher/Challenger/Execution triangle with a Researcher/Devil's Advocate/Executor structure that has hard quantitative kill gates, clear role boundaries, prompt templating, and process automation via hooks.

**Architecture:** Template files in `.agent/teams/alpha-research/` define reusable role prompts and shared context. Two Claude Code hooks (`TaskCompleted`, `TeammateIdle`) enforce quality gates. The Lead session reads templates, fills round-specific context, and spawns teammates.

**Tech Stack:** Claude Code Agent Teams, Claude Code Hooks (shell scripts + jq), YAML templates, Markdown role definitions.

---

## File Structure

```
.agent/teams/alpha-research/
├── README.md                      # Usage instructions for Lead
├── shared-context.template.yaml   # SHARED_CONTEXT template (copy + fill per round)
├── roles/
│   ├── researcher.md              # Researcher role template (Opus)
│   ├── devils-advocate.md         # Devil's Advocate role template (Opus) + Kill Checklist
│   └── executor.md                # Executor role template (Sonnet)
└── hooks/
    ├── task-completed-gate.sh     # TaskCompleted quality gate
    └── teammate-idle-check.sh     # TeammateIdle anti-drift

Modify:
  .claude/settings.local.json      # Add TaskCompleted + TeammateIdle hook entries
```

---

### Task 1: Create directory structure and README

**Files:**
- Create: `.agent/teams/alpha-research/README.md`
- Create: `.agent/teams/alpha-research/roles/` (directory)
- Create: `.agent/teams/alpha-research/hooks/` (directory)

- [ ] **Step 1: Create directories**

```bash
mkdir -p .agent/teams/alpha-research/roles
mkdir -p .agent/teams/alpha-research/hooks
```

- [ ] **Step 2: Write README.md**

Create `.agent/teams/alpha-research/README.md`:

```markdown
# Alpha Research Agent Team

Reusable team structure for alpha research rounds (R32+).

## Team Structure

| Role | Model | Job |
|------|-------|-----|
| Lead (your session) | Opus | Orchestrate, inject context, KILL/PROMOTE |
| Researcher | Opus | Literature search, hypothesis, `explore.py` |
| Devil's Advocate | Opus | Kill Checklist (H1-H6, S1-S6), adversarial review |
| Executor | Sonnet | `impl.py`, backtest, scorecard |

## Quick Start

1. Copy `shared-context.template.yaml` and fill in round-specific values
2. Tell Claude:

```
Create an agent team called alpha-research-R<N>.
Spawn 3 teammates using these role templates:
- Researcher (Opus): read .agent/teams/alpha-research/roles/researcher.md
- Devil's Advocate (Opus): read .agent/teams/alpha-research/roles/devils-advocate.md
- Executor (Sonnet): read .agent/teams/alpha-research/roles/executor.md

Shared context: <paste filled YAML>
Research goal: <your goal>
```

3. Create tasks T1-T7 with dependencies (see below)

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

## Hooks

Two hooks auto-enforce quality (configured in `.claude/settings.local.json`):
- **TaskCompleted**: Validates required fields before task can close
- **TeammateIdle**: Directs idle teammates to claim pending tasks
```

- [ ] **Step 3: Commit**

```bash
git add .agent/teams/alpha-research/README.md
git commit -m "feat(teams): scaffold alpha-research team directory and README"
```

---

### Task 2: Write shared-context.template.yaml

**Files:**
- Create: `.agent/teams/alpha-research/shared-context.template.yaml`

- [ ] **Step 1: Write the template file**

Create `.agent/teams/alpha-research/shared-context.template.yaml`:

```yaml
# =============================================================================
# Alpha Research Shared Context Template
# Copy this file, fill in the blanks, and paste into teammate spawn prompts.
# =============================================================================

round_id: R__                       # e.g., R32
target_instrument: ___              # e.g., TMFD6
research_goal: "___"                # e.g., "Explore TXO IV signals for TMF futures prediction"

# --- Cost Model (update if fee structure changes) ---
cost_model:
  rt_cost_ntd: 40                   # Round-trip cost in NTD
  rt_cost_pts: 4                    # Round-trip cost in points
  rt_cost_bps: 1.33                 # Round-trip cost in bps
  point_value_ntd: 10               # 1 point = 10 NTD for TMFD6
  tax_per_side_ntd: 7               # Fixed tax per side
  commission_per_side_ntd: 13       # Commission per side
  # Note: Retail trader — NO maker rebates, NO tax exemptions

# --- Latency Profile (from config/research/latency_profiles.yaml) ---
latency_profile:
  submit_p95_ms: 36
  modify_p95_ms: 43
  cancel_p95_ms: 47
  internal_pipeline_us: 250

# --- Data Inventory (update per round with latest CK counts) ---
data_inventory:
  TMFD6:
    rows: "9.16M"
    days: 58
    range: "2026-01-27 ~ 2026-03-26"
    levels: "L1-L5"
  TXO:
    rows: "33M"
    days: 58
    levels: "L1"
    note: "UNTAPPED — never researched"
  TMFC6:
    rows: "14.1M"
  TMFB6:
    rows: "10.8M"
  # Add new instruments here per round

# --- Market Microstructure (TMFD6 baseline) ---
market_microstructure:
  median_spread_pts: 4
  p75_spread_pts: 19
  profitable_spread_pct: 45.5       # % of time spread >= 5 pts
  avg_spread_when_profitable_pts: 19.7
  tick_rate_per_sec: 1.8
  l1_queue_depth_lots: 4.1

# --- Killed Directions Blacklist (append new kills, never remove) ---
killed_directions:
  - id: "L1-micro-short"
    rounds: "R14-R17"
    reason: "Signal-horizon mismatch: IC dead at 60s+ where cost is viable"
  - id: "bidirectional-MM"
    rounds: "R12-R13"
    reason: "Queue-back adverse selection, structurally unprofitable at 36ms RTT"
  - id: "OFI-variants"
    rounds: "R9-R11,R16,R26-R27"
    reason: "All killed by cost or regime dependency"
  - id: "spread-conditional-maker"
    rounds: "R16"
    reason: "Adverse selection trap, -8 pts/fill median"
  - id: "LOB-KE-gravity"
    rounds: "R15"
    reason: "IC too weak"
  - id: "VPIN-regime"
    rounds: "R12"
    reason: "DD -30.6% as MM overlay"
  - id: "TX-TMF-leadlag"
    rounds: "R26,R28"
    reason: "2.47 pts edge vs 7.4 pts cost; D6-only artifact"
  - id: "CBS-mean-reversion"
    rounds: "R14-R17"
    reason: "Mid-price artifact, no mean-reversion on TMFD6"
  - id: "1min-strategies"
    rounds: "R18"
    reason: "Data sparsity at 1min horizon"
  - id: "TWSE-stocks"
    rounds: "R31"
    reason: "58.5 bps RT cost (10x error in initial sim)"
  - id: "MLOFI-Hawkes"
    rounds: "R27"
    reason: "Predictive R²=0.000001, TXF→TMF lead-lag inverted"
  - id: "Omori-aftershock"
    rounds: "R30"
    reason: "4.7 pts RT cost consumes 92% of edge"
  - id: "institutional-flow"
    rounds: "R29"
    reason: "No meta-orders detectable on retail TMFD6"
  - id: "event-momentum-spike-fader"
    rounds: "R29b"
    reason: "StormGuard 60s cooldown blocks entry at T+0; T+90s residual marginal"
  - id: "SG-LP"
    rounds: "R18"
    reason: "March spread < cost"
  - id: "MLOFI-microprice"
    rounds: "R18"
    reason: "Trend contamination"
  - id: "MF-extension"
    rounds: "R19"
    reason: "HF→MF transfer impossible"
  - id: "VRR"
    rounds: "R22"
    reason: "vrr never registered, CBS filter killed"
  # Append new kills here each round
```

- [ ] **Step 2: Commit**

```bash
git add .agent/teams/alpha-research/shared-context.template.yaml
git commit -m "feat(teams): add shared-context template with cost model, data inventory, and kill blacklist"
```

---

### Task 3: Write Researcher role template

**Files:**
- Create: `.agent/teams/alpha-research/roles/researcher.md`

- [ ] **Step 1: Write the role template**

Create `.agent/teams/alpha-research/roles/researcher.md`:

```markdown
# Researcher Role Template

You are the **Researcher** in an Alpha Research team for the HFT platform.

## Your Mission

Find 2-3 candidate alpha strategy directions that fit the team's cost model,
latency constraints, and data availability. You are the creative engine —
propose novel directions, not rehashes of killed approaches.

## Hard Rules

1. Every candidate MUST include quantitative estimates (expected edge in bps/pts, horizon, IC)
2. You MUST check each candidate against the killed_directions blacklist
3. You MUST NOT write `impl.py` — that is the Executor's job
4. You MUST NOT judge cost feasibility — that is the Devil's Advocate's job
5. You MAY write `explore.py` for initial signal exploration only

## Your Boundaries

- ✅ arXiv search (use `mcp__arxiv__search_papers`, multiple query angles)
- ✅ Screen papers against constraints
- ✅ Propose 2-3 structured candidate directions
- ✅ Write `explore.py` for initial data exploration
- ❌ Do NOT write `impl.py` (Executor's job)
- ❌ Do NOT judge cost/feasibility (Devil's Advocate's job)
- ❌ Do NOT challenge statistical methods

## Search Strategy

Focus on:
1. Strategies that work at **60s+ horizons** (where cost is viable)
2. Strategies that **don't require sub-10ms latency**
3. Cross-asset signals (options → futures, cross-contract)
4. Mean-reversion at medium frequency
5. Regime-adaptive approaches
6. Unexploited data sources (TXO options if available)

Do multiple searches with different query angles. Categories: q-fin.TR, q-fin.ST,
q-fin.PM, q-fin.CP, q-fin.MF, stat.ML (applied to finance).

## Required Output Format (per candidate)

```
### Candidate [N]: [Name]

**Papers**: [arxiv IDs with titles]
**Hypothesis**: [What signal, what mechanism]
**Horizon**: [Expected holding period]
**Expected Edge**: [Estimated bps/pts, with reasoning]
**Estimated IC**: [If available from literature]
**Data Needed**: [Which instruments, what fields, how much history]
**Overlap Check**: [Explicitly list which killed directions this is NOT]
**Risk/Concern**: [What could kill this]
```

## Round Context

{SHARED_CONTEXT}
```

- [ ] **Step 2: Commit**

```bash
git add .agent/teams/alpha-research/roles/researcher.md
git commit -m "feat(teams): add Researcher role template"
```

---

### Task 4: Write Devil's Advocate role template

**Files:**
- Create: `.agent/teams/alpha-research/roles/devils-advocate.md`

- [ ] **Step 1: Write the role template**

Create `.agent/teams/alpha-research/roles/devils-advocate.md`:

```markdown
# Devil's Advocate Role Template

You are the **Devil's Advocate** in an Alpha Research team for the HFT platform.

## Your Mission

Ruthlessly challenge every proposal and backtest result. Your job is to KILL bad
ideas fast, not to help improve them. You are the last line of defense against
wasting weeks on strategies that will inevitably fail.

## Hard Rules

1. You MUST execute the FULL Kill Checklist for EVERY proposal or backtest result
2. Every check MUST have a QUANTITATIVE result (a number, not an opinion)
3. **Tier 1**: Any single FAIL = REJECT. No exceptions. No "consider fixing".
4. **Tier 2**: 2+ FAIL = REJECT.
5. You do NOT suggest improvements. You only PASS or KILL.
6. If the proposer's numbers are missing for a check, that check = FAIL
   (burden of proof is on the proposer, not on you)
7. You MUST NOT write strategy code
8. You MUST NOT do literature search
9. You MUST NOT suggest "how to fix" a rejected proposal

## Your Boundaries

- ✅ Execute Kill Checklist on every proposal (Tier 1 + Tier 2 + Tier 3)
- ✅ Read data files to verify claims (array shapes, ClickHouse queries, .npy headers)
- ✅ Write validation scripts (cost arithmetic, spread analysis, data sufficiency)
- ✅ Execute Gate C Statistical Review on backtest results
- ❌ Do NOT write strategy code
- ❌ Do NOT do literature search
- ❌ Do NOT suggest improvements — only PASS or KILL

## Kill Checklist

### Tier 1: Hard Kill (ANY single FAIL = immediate REJECT)

| ID | Check | Kill Criteria |
|----|-------|---------------|
| H1 | **Cost arithmetic** | `expected_edge_bps < 2 × RT_cost_bps`. Verify the math yourself — do NOT trust the proposer's arithmetic. |
| H2 | **Spread vs edge** | `expected_edge_pts < median_spread_pts + RT_cost_pts`. Edge must exceed BOTH spread AND cost. |
| H3 | **Killed direction overlap** | Core mechanism matches ANY entry in `killed_directions` blacklist. Repackaging a killed idea under a new name = FAIL. |
| H4 | **Data sufficiency** | `available_days < 20` OR `available_rows < 500,000` for the target instrument. |
| H5 | **Latency feasibility** | `signal_half_life_seconds < 2 × broker_RTT_P95_seconds`. Signal must live long enough to trade. |
| H6 | **Execution model** | Expected edge < 2× median spread BUT proposal does NOT use bid/ask execution model. |

### Tier 2: Statistical Rigor (2+ FAIL = REJECT)

| ID | Check | Kill Criteria |
|----|-------|---------------|
| S1 | **IC detrending** | IC increases monotonically with horizon = trend contamination, not alpha. |
| S2 | **OOS validation** | Only in-sample results presented = REJECT. Must show out-of-sample. |
| S3 | **Sample size** | Independent trades < 30 = insufficient statistical power. |
| S4 | **Recency bias** | Not validated on the most recent available data = REJECT. |
| S5 | **Paper-to-code fidelity** | Formula diff between paper and code > 0, without explicit justification = REJECT. |
| S6 | **Regime dependency** | Profitable edge exists in only a single sub-period = WARN (not FAIL unless combined). |

### Tier 3: Platform Compatibility (WARN only, does not kill)

| ID | Check | Criteria |
|----|-------|----------|
| P1 | **FeatureEngine slot** | Does this need a new feature index? Is there an available slot (current: 27 of v3)? |
| P2 | **Config drift** | Research parameters vs platform config — are they consistent? |
| P3 | **Concurrent position** | What is the max combined position if this runs alongside existing strategies? |

## Required Output Format

You MUST fill out EVERY line. Missing lines = incomplete review.

```
## Kill Checklist Result — [Candidate Name]

### Tier 1: Hard Kill
- [H1] Cost arithmetic: PASS/FAIL — expected {X} bps vs threshold {2 × RT_cost} bps
- [H2] Spread vs edge: PASS/FAIL — expected {X} pts vs {spread + cost} pts
- [H3] Overlap: PASS/FAIL — {reason, citing specific killed_direction id if FAIL}
- [H4] Data sufficiency: PASS/FAIL — {N} days, {N} rows available
- [H5] Latency feasibility: PASS/FAIL — half-life {X}s vs {2 × RTT}s threshold
- [H6] Execution model: PASS/FAIL — {reason}

### Tier 2: Statistical Rigor
- [S1] IC detrending: PASS/FAIL — {evidence}
- [S2] OOS validation: PASS/FAIL — {evidence}
- [S3] Sample size: PASS/FAIL — {N} independent trades
- [S4] Recency bias: PASS/FAIL — validated on {date range}
- [S5] Paper-to-code: PASS/FAIL — {N} formula diffs found
- [S6] Regime dependency: PASS/WARN — {evidence}

### Tier 3: Platform Compatibility
- [P1] FE slot: OK/WARN — {detail}
- [P2] Config drift: OK/WARN — {detail}
- [P3] Concurrent position: OK/WARN — {detail}

### Verdict: APPROVE / REJECT
Reason: {one-sentence summary}
Tier 1 FAIL count: {N}
Tier 2 FAIL count: {N}
```

## Round Context

{SHARED_CONTEXT}
```

- [ ] **Step 2: Commit**

```bash
git add .agent/teams/alpha-research/roles/devils-advocate.md
git commit -m "feat(teams): add Devil's Advocate role template with Kill Checklist"
```

---

### Task 5: Write Executor role template

**Files:**
- Create: `.agent/teams/alpha-research/roles/executor.md`

- [ ] **Step 1: Write the role template**

Create `.agent/teams/alpha-research/roles/executor.md`:

```markdown
# Executor Role Template

You are the **Executor** in an Alpha Research team for the HFT platform.

## Your Mission

Implement approved alpha candidates as working prototypes, run backtests, and
produce standardized scorecards. You are the builder — you translate hypotheses
into code and data into numbers.

## Hard Rules

1. You MUST NOT implement anything that hasn't been APPROVED by the Devil's Advocate
2. You MUST NOT judge whether a strategy is worth pursuing — just implement and report
3. You MUST NOT challenge statistical methods — only report numbers
4. You MUST produce a standardized scorecard for every backtest
5. You MUST check platform integration compatibility

## Your Boundaries

- ✅ Write `impl.py` following the alpha protocol in `research/registry/schemas.py`
- ✅ Write backtest scripts using `.agent/skills/hft-backtester/`
- ✅ Run backtests and produce scorecards
- ✅ Check platform integration (FeatureEngine slots, config schema, latency profile)
- ❌ Do NOT judge whether strategy is worth doing
- ❌ Do NOT challenge statistical methods (report numbers, don't evaluate them)
- ❌ Do NOT do literature search

## Implementation Checklist

Before marking your implementation task complete:

1. [ ] `impl.py` follows `AlphaProtocol` from `research/registry/schemas.py`
2. [ ] `manifest.yaml` exists with correct fields
3. [ ] Cost model uses scaled integers (x10000) for all prices
4. [ ] No `float` in financial arithmetic
5. [ ] `timebase.now_ns()` for all timestamps (never `datetime.now()`)
6. [ ] Uses `structlog` (never `print()`)
7. [ ] Backtest uses bid/ask execution if edge < 2× spread

## Scorecard Format

Every backtest MUST produce this scorecard:

```
## Backtest Scorecard — [Strategy Name]

**Period**: {start_date} to {end_date} ({N} trading days)
**Instrument**: {symbol}
**Trades**: {N} total ({N} long, {N} short)

### Performance
- Sharpe Ratio: {X.XX}
- Total Return: {X.XX} bps
- Max Drawdown: {X.XX} bps
- Win Rate: {X.X}%
- Avg Edge per Trade: {X.XX} bps
- Edge vs RT Cost: {edge_bps} vs {rt_cost_bps} ({ratio}x)

### Cost Analysis
- RT Cost: {X} pts ({X} bps)
- Avg Spread at Entry: {X} pts
- Slippage: {X} pts
- Net Edge after Cost: {X} bps

### Risk
- Max Position: {N} lots
- Avg Holding Period: {X}s
- Longest Drawdown: {N} trades

### Data
- Signal Count: {N}
- Signals per Day: {X.X}
- OOS Period: {date range}
```

## Platform Integration Check

After backtest, verify:

1. **Latency profile**: Signal half-life >> broker RTT P95
2. **FeatureEngine**: Which features needed? Available slots?
3. **Config**: What parameters need to be in `strategies.yaml`?
4. **Risk limits**: Max position, stop loss, compatible with existing strategies?
5. **Research-live parity**: Any differences between `impl.py` and live strategy?

## Round Context

{SHARED_CONTEXT}
```

- [ ] **Step 2: Commit**

```bash
git add .agent/teams/alpha-research/roles/executor.md
git commit -m "feat(teams): add Executor role template with scorecard format"
```

---

### Task 6: Write TaskCompleted hook script

**Files:**
- Create: `.agent/teams/alpha-research/hooks/task-completed-gate.sh`

The `TaskCompleted` hook receives JSON on stdin with fields: `task_id`, `task_subject`,
`task_description`, `teammate_name`, `team_name`. Exit code 2 + stderr message = reject
completion and send feedback to the teammate.

- [ ] **Step 1: Write the hook script**

Create `.agent/teams/alpha-research/hooks/task-completed-gate.sh`:

```bash
#!/bin/bash
# TaskCompleted quality gate for alpha-research teams.
#
# Exit 0 = allow task completion
# Exit 2 + stderr = reject completion, send stderr as feedback to teammate
#
# Input: JSON on stdin with task_id, task_subject, task_description,
#        teammate_name, team_name
#
# This hook validates that task outputs contain required fields before
# allowing completion. It reads the task description to determine which
# validation rules apply.

set -euo pipefail

INPUT=$(cat)
TASK_SUBJECT=$(echo "$INPUT" | jq -r '.task_subject // ""')
TASK_DESC=$(echo "$INPUT" | jq -r '.task_description // ""')
TEAMMATE=$(echo "$INPUT" | jq -r '.teammate_name // ""')
TEAM=$(echo "$INPUT" | jq -r '.team_name // ""')

# Only apply to alpha-research teams
if [[ "$TEAM" != alpha-research* ]]; then
  exit 0
fi

# --- Researcher: proposals must have structured candidates ---
if [[ "$TEAMMATE" == "researcher" && "$TASK_SUBJECT" == *"iterature"* ]] || \
   [[ "$TEAMMATE" == "researcher" && "$TASK_SUBJECT" == *"roposal"* ]] || \
   [[ "$TEAMMATE" == "researcher" && "$TASK_SUBJECT" == *"earch"* ]]; then

  # Check the teammate's recent transcript for required fields
  # We validate via task_description since transcript isn't directly available
  MISSING=""
  for field in "Expected Edge" "Horizon" "Data Needed" "Overlap Check"; do
    if ! echo "$TASK_DESC" | grep -qi "$field"; then
      MISSING="$MISSING $field,"
    fi
  done

  if [ -n "$MISSING" ]; then
    echo "REJECTED: Researcher output missing required fields:$MISSING" >&2
    echo "Every candidate must include: Expected Edge, Horizon, Data Needed, Overlap Check" >&2
    exit 2
  fi
fi

# --- Devil's Advocate: must include all checklist IDs ---
if [[ "$TEAMMATE" == "devils-advocate" || "$TEAMMATE" == "devil"* ]]; then
  MISSING=""
  for check in H1 H2 H3 H4 H5 H6; do
    if ! echo "$TASK_DESC" | grep -q "\[$check\]"; then
      MISSING="$MISSING $check,"
    fi
  done

  if [ -n "$MISSING" ]; then
    echo "REJECTED: Kill Checklist incomplete — missing checks:$MISSING" >&2
    echo "You MUST fill out every line of the Kill Checklist (H1-H6, S1-S6)." >&2
    exit 2
  fi
fi

# --- Executor: backtest must include scorecard metrics ---
if [[ "$TEAMMATE" == "executor" && "$TASK_SUBJECT" == *"acktest"* ]] || \
   [[ "$TEAMMATE" == "executor" && "$TASK_SUBJECT" == *"corecard"* ]]; then
  MISSING=""
  for metric in "Sharpe" "Drawdown" "Win Rate" "Edge"; do
    if ! echo "$TASK_DESC" | grep -qi "$metric"; then
      MISSING="$MISSING $metric,"
    fi
  done

  if [ -n "$MISSING" ]; then
    echo "REJECTED: Backtest scorecard missing metrics:$MISSING" >&2
    echo "Scorecard must include: Sharpe Ratio, Max Drawdown, Win Rate, Edge vs RT Cost" >&2
    exit 2
  fi
fi

exit 0
```

- [ ] **Step 2: Make executable**

```bash
chmod +x .agent/teams/alpha-research/hooks/task-completed-gate.sh
```

- [ ] **Step 3: Verify jq is available**

```bash
which jq || echo "WARNING: jq not found — hooks require jq for JSON parsing"
```

Expected: `/usr/bin/jq` or similar path.

- [ ] **Step 4: Test the hook manually with mock input**

```bash
# Test: Researcher with missing fields → should exit 2
echo '{"task_subject":"Literature search","task_description":"Found 3 papers about VRP","teammate_name":"researcher","team_name":"alpha-research-R32"}' | bash .agent/teams/alpha-research/hooks/task-completed-gate.sh
echo "Exit code: $?"
```

Expected: stderr shows "REJECTED: Researcher output missing required fields" and exit code 2.

```bash
# Test: Non-alpha-research team → should exit 0
echo '{"task_subject":"anything","task_description":"","teammate_name":"worker","team_name":"other-team"}' | bash .agent/teams/alpha-research/hooks/task-completed-gate.sh
echo "Exit code: $?"
```

Expected: exit code 0 (pass through).

- [ ] **Step 5: Commit**

```bash
git add .agent/teams/alpha-research/hooks/task-completed-gate.sh
git commit -m "feat(teams): add TaskCompleted quality gate hook for alpha-research"
```

---

### Task 7: Write TeammateIdle hook script

**Files:**
- Create: `.agent/teams/alpha-research/hooks/teammate-idle-check.sh`

The `TeammateIdle` hook receives JSON on stdin with `teammate_name` and `team_name`.
Exit code 2 + stderr = keep teammate working with feedback.

- [ ] **Step 1: Write the hook script**

Create `.agent/teams/alpha-research/hooks/teammate-idle-check.sh`:

```bash
#!/bin/bash
# TeammateIdle check for alpha-research teams.
#
# Exit 0 = allow teammate to go idle
# Exit 2 + stderr = send feedback, keep teammate working
#
# Input: JSON on stdin with teammate_name, team_name
#
# If there are pending tasks in the team's task directory that haven't been
# claimed, tell the teammate to pick one up.

set -euo pipefail

INPUT=$(cat)
TEAMMATE=$(echo "$INPUT" | jq -r '.teammate_name // ""')
TEAM=$(echo "$INPUT" | jq -r '.team_name // ""')

# Only apply to alpha-research teams
if [[ "$TEAM" != alpha-research* ]]; then
  exit 0
fi

# Check for pending tasks in the team's task storage
TASK_DIR="$HOME/.claude/tasks/$TEAM"

if [ ! -d "$TASK_DIR" ]; then
  exit 0
fi

# Count pending (unclaimed) tasks
PENDING=$(find "$TASK_DIR" -name "*.json" -exec grep -l '"status":"pending"' {} \; 2>/dev/null | wc -l)

if [ "$PENDING" -gt 0 ]; then
  echo "You have $PENDING pending tasks in team $TEAM. Claim the next unblocked task before going idle." >&2
  echo "Use TaskList to see available tasks, then TaskUpdate to claim one." >&2
  exit 2
fi

exit 0
```

- [ ] **Step 2: Make executable**

```bash
chmod +x .agent/teams/alpha-research/hooks/teammate-idle-check.sh
```

- [ ] **Step 3: Test the hook manually**

```bash
# Test: Non-alpha-research team → should exit 0
echo '{"teammate_name":"worker","team_name":"other-team"}' | bash .agent/teams/alpha-research/hooks/teammate-idle-check.sh
echo "Exit code: $?"
```

Expected: exit code 0.

- [ ] **Step 4: Commit**

```bash
git add .agent/teams/alpha-research/hooks/teammate-idle-check.sh
git commit -m "feat(teams): add TeammateIdle anti-drift hook for alpha-research"
```

---

### Task 8: Register hooks in settings.local.json

**Files:**
- Modify: `.claude/settings.local.json`

The hooks must be registered in settings for Claude Code to discover them. We use
`.claude/settings.local.json` (project-local, not committed) since hooks reference
absolute paths. `TaskCompleted` and `TeammateIdle` do not support matchers — they
fire on every occurrence.

- [ ] **Step 1: Read current settings.local.json**

```bash
cat .claude/settings.local.json 2>/dev/null || echo "{}"
```

- [ ] **Step 2: Add hook entries**

If the file doesn't exist or is empty, create it. If it has existing hooks, merge.
The file should contain:

```json
{
  "hooks": {
    "TaskCompleted": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/.agent/teams/alpha-research/hooks/task-completed-gate.sh\""
          }
        ]
      }
    ],
    "TeammateIdle": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR/.agent/teams/alpha-research/hooks/teammate-idle-check.sh\""
          }
        ]
      }
    ]
  }
}
```

Note: `$CLAUDE_PROJECT_DIR` is automatically expanded by Claude Code to the project root.

- [ ] **Step 3: Verify the JSON is valid**

```bash
python3 -c "import json; json.load(open('.claude/settings.local.json'))" && echo "Valid JSON"
```

Expected: "Valid JSON"

- [ ] **Step 4: Commit (if .claude/settings.local.json is not gitignored)**

Check first:

```bash
git check-ignore .claude/settings.local.json
```

If gitignored (expected), skip commit — the file is local-only.
If NOT gitignored, add to `.gitignore` rather than committing credentials-adjacent config.

---

### Task 9: Validate end-to-end with dry run

**Files:** None (validation only)

- [ ] **Step 1: Verify all files exist with correct permissions**

```bash
echo "=== File structure ==="
find .agent/teams/alpha-research -type f | sort

echo ""
echo "=== Permissions ==="
ls -la .agent/teams/alpha-research/hooks/
```

Expected output:
```
=== File structure ===
.agent/teams/alpha-research/README.md
.agent/teams/alpha-research/hooks/task-completed-gate.sh
.agent/teams/alpha-research/hooks/teammate-idle-check.sh
.agent/teams/alpha-research/roles/devils-advocate.md
.agent/teams/alpha-research/roles/executor.md
.agent/teams/alpha-research/roles/researcher.md
.agent/teams/alpha-research/shared-context.template.yaml

=== Permissions ===
-rwxr-xr-x ... task-completed-gate.sh
-rwxr-xr-x ... teammate-idle-check.sh
```

- [ ] **Step 2: Validate YAML template parses correctly**

```bash
python3 -c "
import yaml
with open('.agent/teams/alpha-research/shared-context.template.yaml') as f:
    data = yaml.safe_load(f)
print(f'round_id: {data[\"round_id\"]}')
print(f'killed_directions: {len(data[\"killed_directions\"])} entries')
print(f'cost_model.rt_cost_pts: {data[\"cost_model\"][\"rt_cost_pts\"]}')
print('YAML valid')
"
```

Expected: `killed_directions: 18 entries`, `cost_model.rt_cost_pts: 4`, `YAML valid`

- [ ] **Step 3: Run both hooks with valid input to ensure they pass**

```bash
# Researcher with all required fields → should pass
echo '{"task_subject":"Literature search","task_description":"Candidate 1: Expected Edge 5 bps, Horizon 300s, Data Needed TMFD6 L1, Overlap Check not L1-micro-short","teammate_name":"researcher","team_name":"alpha-research-R32"}' | bash .agent/teams/alpha-research/hooks/task-completed-gate.sh
echo "Researcher pass: exit $?"

# DA with all checks → should pass
echo '{"task_subject":"Kill Checklist review","task_description":"[H1] Cost: PASS [H2] Spread: PASS [H3] Overlap: PASS [H4] Data: PASS [H5] Latency: PASS [H6] Execution: PASS","teammate_name":"devils-advocate","team_name":"alpha-research-R32"}' | bash .agent/teams/alpha-research/hooks/task-completed-gate.sh
echo "DA pass: exit $?"
```

Expected: Both exit 0.

- [ ] **Step 4: Final commit with all files**

```bash
git status
# If any uncommitted files remain, add and commit:
git add .agent/teams/alpha-research/
git commit -m "feat(teams): complete alpha-research team redesign — templates, roles, hooks"
```
