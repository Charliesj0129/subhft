# Alpha Research Agent Team Redesign

**Date**: 2026-04-02
**Status**: Approved
**Scope**: Optimize alpha-research team structure — role definitions, model allocation, prompt templating, process automation

## Problem Statement

After 26 rounds of alpha research (R6-R31), the current Researcher/Challenger/Execution triangle has three structural problems:

1. **Challenger is too weak**: Challenges are not sharp enough. Strategies that should die early survive to Stage 3+ before being killed by cost structure or data quality issues (R31: 10x cost error, R25: CK cumulative volume bug, R13: MM at 36ms RTT).
2. **Role boundaries are blurred**: Researcher writes `impl.py` (Executor's job), Execution challenges statistical methods (Challenger's job). Overlap wastes tokens and dilutes accountability.
3. **Prompts are round-specific**: Every round requires writing prompts from scratch. R14 Researcher prompt hardcodes CTR strategy details, R20 investigators hardcode L1 data questions. No reuse.

## Design

### Team Structure: Lead + 3 Teammates

| Role | Model | Primary Responsibility |
|------|-------|----------------------|
| **Lead** | Opus (user session) | Orchestrate stages, inject round context, final KILL/PROMOTE |
| **Researcher** | Opus | Literature search, hypothesis formation, `explore.py` |
| **Devil's Advocate** | Opus | Adversarial review via quantitative Kill Checklist |
| **Executor** | Sonnet | `impl.py` prototype, backtest execution, scorecard |

### Role Definitions and Boundaries

#### Researcher (Opus)

**Does:**
- arXiv search (multi-angle queries)
- Screen papers against constraints
- Propose 2-3 candidate directions with structured output
- Write `explore.py` for initial signal exploration

**Does NOT:**
- Write `impl.py` (Executor's job)
- Judge cost/feasibility (Devil's Advocate's job)
- Challenge statistical methods

**Output format per candidate:**
- Name, Paper references (arxiv IDs), Hypothesis, Expected horizon, Estimated IC/edge, Data requirements, Overlap check against killed directions

#### Devil's Advocate (Opus)

**Does:**
- Execute full Kill Checklist on every proposal (Tier 1 + Tier 2)
- Provide quantitative PASS/FAIL per check item
- Execute Gate C Statistical Review on backtest results
- Read data files to verify claims (array shapes, CK queries)
- Write validation scripts (cost arithmetic, spread checks)

**Does NOT:**
- Write strategy code
- Do literature search
- Suggest improvements — only PASS or KILL

**Core principle:** `Unresolved FAIL > 0 = REJECT. No exceptions.`

#### Executor (Sonnet)

**Does:**
- Implement approved candidate as `impl.py`
- Run backtest, produce scorecard (Sharpe, DD, win rate, edge vs cost)
- Platform integration check (FeatureEngine slots, config schema, latency profile)

**Does NOT:**
- Judge whether strategy is worth pursuing
- Challenge statistical methods (only reports numbers)

**Why Sonnet:** `impl.py` is a structured coding task. Opus-level reasoning is not needed. Token savings redirected to Devil's Advocate.

### Devil's Advocate Kill Checklist

#### Tier 1: Hard Kill (any FAIL = immediate REJECT)

| ID | Check | Kill Criteria | Historical Case |
|----|-------|---------------|-----------------|
| H1 | Cost arithmetic | `expected_edge_bps < 2 * RT_cost_bps` | R31: 10x cost error |
| H2 | Spread vs edge | `expected_edge_pts < median_spread + RT_cost_pts` | R18: spread < cost |
| H3 | Killed direction overlap | Core mechanism matches blacklist entry | R26: repackaged lead-lag |
| H4 | Data sufficiency | `available_days < 20` or `rows < 500k` | R18: 1min data sparsity |
| H5 | Latency feasibility | `signal_half_life_s < 2 * broker_RTT_P95_s` | R13: 36ms RTT |
| H6 | Execution model | Edge < 2x spread without bid/ask execution | Feedback rule |

#### Tier 2: Statistical Rigor (2+ FAIL = REJECT)

| ID | Check | Kill Criteria |
|----|-------|---------------|
| S1 | IC detrending | Monotonically increasing IC = trend contamination |
| S2 | OOS validation | IS-only results = REJECT |
| S3 | Sample size | Independent trades < 30 = insufficient power |
| S4 | Recency bias | Not validated on most recent data = REJECT |
| S5 | Paper-to-code fidelity | Formula diff > 0 without justification = REJECT |
| S6 | Regime dependency | Edge in single sub-period only = WARN |

#### Tier 3: Platform Compatibility (WARN only)

| ID | Check | Criteria |
|----|-------|----------|
| P1 | FeatureEngine slot | New index needed? Available? |
| P2 | Config drift | Research params vs platform config consistent? |
| P3 | Concurrent position | Max combined position with existing strategies |

#### Required Output Format

```
## Kill Checklist Result — [Candidate Name]

### Tier 1: Hard Kill
- [H1] Cost arithmetic: PASS/FAIL — {number} vs threshold {number}
- [H2] Spread vs edge: PASS/FAIL — {number} vs {number}
- [H3] Overlap: PASS/FAIL — {reason}
- [H4] Data sufficiency: PASS/FAIL — {days} days, {rows} rows
- [H5] Latency feasibility: PASS/FAIL — {half_life}s vs {2 * RTT}s
- [H6] Execution model: PASS/FAIL — {reason}

### Tier 2: Statistical Rigor
- [S1] IC detrending: PASS/FAIL — {evidence}
- [S2] OOS validation: PASS/FAIL — {evidence}
- [S3] Sample size: PASS/FAIL — {N} trades
- [S4] Recency bias: PASS/FAIL — {date range}
- [S5] Paper-to-code: PASS/FAIL — {diff count}
- [S6] Regime dependency: PASS/WARN — {evidence}

### Tier 3: Platform
- [P1] FE slot: OK/WARN — {detail}
- [P2] Config drift: OK/WARN — {detail}
- [P3] Position: OK/WARN — {detail}

### Verdict: APPROVE / REJECT ({reason})
```

### Prompt Templating Architecture

Each role prompt = 3 blocks:

```
[ROLE_TEMPLATE]      — Cross-round constant. Defines role, boundaries, output format.
[SHARED_CONTEXT]     — Updated once per round. All roles share.
[STAGE_TASK]         — Injected by Lead at each stage transition.
```

#### SHARED_CONTEXT Template (YAML)

```yaml
round_id: R__
target_instrument: ___
research_goal: "___"

cost_model:
  rt_cost_ntd: 40
  rt_cost_pts: 4
  rt_cost_bps: 1.33
  point_value_ntd: 10
  tax_per_side_ntd: 7
  commission_per_side_ntd: 13

latency_profile:
  submit_p95_ms: 36
  modify_p95_ms: 43
  cancel_p95_ms: 47
  internal_pipeline_us: 250

data_inventory:
  TMFD6: { rows: "9.16M", days: 58, range: "2026-01-27 ~ 2026-03-26", levels: "L1-L5" }
  TXO:   { rows: "33M",   days: 58, levels: "L1", note: "UNTAPPED" }
  # ... add per round

market_microstructure:
  median_spread_pts: 4
  p75_spread_pts: 19
  profitable_spread_pct: 45.5
  tick_rate_per_sec: 1.8
  l1_queue_depth_lots: 4.1

killed_directions:
  - { id: "L1-micro-short",         rounds: "R14-R17", reason: "Signal-horizon mismatch" }
  - { id: "bidirectional-MM",        rounds: "R12-R13", reason: "Adverse selection at 36ms RTT" }
  - { id: "OFI-variants",           rounds: "R9-R11,R16,R26-R27", reason: "Cost or regime kills all" }
  - { id: "spread-conditional-maker", rounds: "R16", reason: "Adverse selection, -8 pts/fill" }
  - { id: "LOB-KE-gravity",         rounds: "R15", reason: "IC too weak" }
  - { id: "VPIN-regime",            rounds: "R12", reason: "DD -30.6%" }
  - { id: "TX-TMF-leadlag",         rounds: "R26,R28", reason: "2.47 pts vs 7.4 pts cost" }
  - { id: "CBS-mean-reversion",     rounds: "R14-R17", reason: "Mid-price artifact" }
  - { id: "1min-strategies",        rounds: "R18", reason: "Data sparsity" }
  - { id: "TWSE-stocks",            rounds: "R31", reason: "58.5 bps RT cost" }
  - { id: "MLOFI-Hawkes",           rounds: "R27", reason: "R^2=0.000001" }
  - { id: "Omori-aftershock",       rounds: "R30", reason: "4.7 pts cost consumes 92% edge" }
  - { id: "institutional-flow",     rounds: "R29", reason: "No meta-orders on retail TMFD6" }
  # ... extend per round
```

### Process Automation

#### Task Dependency Chain

```
T1: [Researcher]       Literature search + candidate proposals
T2: [Devil's Advocate]  Kill Checklist on proposals              (blockedBy: T1)
T3: [Researcher]        Revise candidates if WARN only           (blockedBy: T2, conditional)
T4: [Executor]          Implement approved candidate             (blockedBy: T2 PASS)
T5: [Executor]          Run backtest + scorecard                 (blockedBy: T4)
T6: [Devil's Advocate]  Gate C statistical review                (blockedBy: T5)
T7: [Lead]              Final KILL/PROMOTE decision              (blockedBy: T6)
```

REJECT at T2 = Lead decides: KILL candidate (try next) or KILL round. No feedback loop back to Researcher to "fix" a rejected proposal.

#### Stage Flow

```
Stage 1: Research          Stage 2: Challenge         Stage 3: Implement
┌──────────────┐          ┌──────────────┐           ┌──────────────┐
│  Researcher   │─proposal→│Devil's Advocate│─approved→│   Executor    │
│  (Opus)       │          │  (Opus)       │           │  (Sonnet)     │
│               │          │               │           │               │
│ arXiv search  │          │ Kill Checklist │           │ impl.py       │
│ explore.py    │          │ PASS/KILL      │           │ backtest      │
│ 2-3 candidates│          │               │           │ scorecard     │
└──────────────┘          └───────┬───────┘           └───────┬───────┘
                                  │                           │
                           if REJECT:                  Stage 4: Gate C
                           Lead decides               ┌──────────────┐
                           KILL or next candidate     │Devil's Advocate│
                                                      │ Statistical    │
                                                      │ Review (S1-S6) │
                                                      └───────┬───────┘
                                                              │
                                                       Lead: KILL/PROMOTE
```

#### Hook: TaskCompleted — Quality Gate

Validates required fields in task outputs before allowing completion.

- Researcher proposals must contain: `expected_edge`, `horizon`, `data_needed`, `overlap_check`
- Devil's Advocate reviews must contain: all H1-H6 check IDs
- Executor backtests must contain: `sharpe`, `drawdown`, `win_rate`, `edge_bps`

Exit code 2 rejects the completion and sends feedback to the teammate.

#### Hook: TeammateIdle — Prevent Idle Drift

When a teammate goes idle, checks for unclaimed pending tasks and directs the teammate to claim the next one.

### Mapping to Alpha Governance Pipeline

| Team Stage | Alpha Gate | Owner |
|------------|-----------|-------|
| T1-T2 | Gate A (manifest + feasibility) | Researcher -> Devil's Advocate |
| T4-T5 | Gate B (pytest) + Gate C (backtest + scorecard) | Executor -> Devil's Advocate |
| T7 | Gate D (Sharpe/DD thresholds) | Lead |
| Post-team | Gate E (shadow session) | Live runtime (out of team scope) |

### File Layout

```
.agent/teams/alpha-research/
├── README.md                      # Usage instructions
├── shared-context.template.yaml   # SHARED_CONTEXT template (fill per round)
├── roles/
│   ├── researcher.md              # Researcher ROLE_TEMPLATE
│   ├── devils-advocate.md         # Devil's Advocate ROLE_TEMPLATE + Kill Checklist
│   └── executor.md                # Executor ROLE_TEMPLATE
└── hooks/
    ├── task-completed-gate.sh     # TaskCompleted quality gate
    └── teammate-idle-check.sh     # TeammateIdle anti-drift
```

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Team size | 4 (Lead + 3) | Balance token cost and coverage |
| Researcher model | Opus | Deep reasoning for novel directions |
| Devil's Advocate model | Opus | Must understand complex stats to challenge |
| Executor model | Sonnet | Structured coding, Opus not needed |
| Kill on REJECT | Direct KILL, no revision loop | Bad directions can't be fixed, kill fast |
| Checklist enforcement | TaskCompleted hook | Prevent skipping required checks |
| Stage timing | Gate C (Stage 3) | Confirmed sufficient by user |
| Prompt architecture | ROLE_TEMPLATE + SHARED_CONTEXT + STAGE_TASK | Only update context per round |
| Template location | `.agent/teams/alpha-research/` | Version controlled with codebase |

### Changes from Current System

| Before | After |
|--------|-------|
| Write 3 prompts from scratch each round | Template + inject context |
| Challenger does vague "review" | Devil's Advocate with hard Kill Checklist (H1-H6, S1-S6) |
| Researcher writes `impl.py` | Researcher stops at `explore.py`, Executor writes `impl.py` |
| Execution does statistical challenges | Executor reports numbers only, DA does all challenges |
| All teammates on Opus | Executor on Sonnet (~33% token savings) |
| No hooks | TaskCompleted + TeammateIdle quality gates |
| REJECT leads to revision cycle | REJECT = KILL, move to next candidate |
