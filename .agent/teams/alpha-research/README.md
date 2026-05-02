# Alpha Research Agent Team

Reusable team structure for alpha research rounds (R32+).

## Team Structure

| Role | Model | Job | Key Skills |
|------|-------|-----|------------|
| Lead (your session) | Sonnet | Orchestrate, inject context, KILL/PROMOTE | `hft-strategy-lifecycle`, `hft-release-gate` |
| Researcher | Opus | Literature search, hypothesis, `explore.py` | `taifex-alpha-kill-criteria`, `taifex-market-structure` |
| Devil's Advocate | Opus | Kill Checklist (H1-H6, S1-S6), adversarial review | `taifex-alpha-kill-criteria`, `hft-backtest-calibration` |
| Executor | Opus | `impl.py`, backtest, scorecard | `hft-strategy-sdk`, `hft-backtest-calibration`, `hft-test-hft` |

## Skill Pipeline (per stage)

```
T0 Lead:          (init) hft-mm-design, taifex-market-structure
                       (resume) none — re-reads artifacts
T1 Researcher:    taifex-alpha-kill-criteria → avoid dead ends
                  taifex-market-structure    → correct cost/spread assumptions
T2 Devil's Adv:   taifex-alpha-kill-criteria → 50+ killed directions reference
                  hft-backtest-calibration   → validate execution model claims
T4 Executor:      hft-strategy-sdk           → BaseStrategy hooks + order API
                  hft-backtest-calibration   → CK vs hftbacktest, latency profiles
                  hft-test-hft              → scaled int + monotonic time tests
T5 Executor:      hft-backtest-calibration   → scorecard interpretation + traps
                  (if MM) hft-mm-design      → R47 three-layer pattern
T6 Devil's Adv:   hft-backtest-calibration   → statistical validation checklist
T7 Lead:          hft-strategy-lifecycle     → promotion path (shadow → live)
                  hft-release-gate           → deployment readiness
T8 Lead:          hft-strategy-lifecycle    → post-PROMOTE scaffold
                  hft-backtest-calibration  → validate regen candidates
T9 Lead:          (halt path) hft-release-gate / hft-production-audit if final PROMOTE
```

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
- `budget.json` missing while `progress.jsonl` exists (invariant violation — rounds recorded without a baseline budget)

To stop the loop on demand: `echo STOP > outputs/team_artifacts/alpha-research/STOP`.

Tests: `bash .agent/teams/alpha-research/hooks/budget-guard_test.sh` (10 cases).

## Post-Team: Promotion Path

If T7 = PROMOTE, the Lead follows `hft-strategy-lifecycle`:
1. Implement as `BaseStrategy` (use `hft-strategy-sdk`)
2. If MM: apply R47 patterns from `hft-mm-design`
3. Configure in `strategies.yaml` + `strategy_limits.yaml`
4. Shadow trade with `HFT_ORDER_SHADOW_MODE=1`
5. Run `hft-release-gate` before enabling live
6. Run `hft-production-audit` after first live session

## Hooks

Three hooks auto-enforce discipline (configured in `.claude/settings.local.json`):
- **TaskCompleted** → `task-completed-gate.sh`: validates required fields (Expected Edge, H1-H6 markers, Sharpe/Drawdown, etc.) before a task can close.
- **TaskCompleted** → `budget-guard.sh`: halts the autonomous loop on STOP file or any of the 5 budget triggers (runtime, rounds, PROMOTEs, consecutive KILLs, missing budget with existing progress). See `## Budget-guard Hook` section above.
- **TeammateIdle** → `teammate-idle-check.sh`: directs idle teammates to claim pending tasks.
