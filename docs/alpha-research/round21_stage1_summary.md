# Round 21 Stage 1 Summary — VPIN Gamma Scaling (4.2.2) + Conditional Spread Capture (4.3)

**Date**: 2026-03-27
**Team**: Researcher (Opus) + Challenger (Opus) + Execution (Opus)

---

## Verdicts

| Role | Candidate A (VPIN Gamma) | Candidate B (Dynamic Threshold) | Candidate C (Hybrid) |
|------|--------------------------|--------------------------------|---------------------|
| Researcher | DEFER | Prototype first | Evolution of B |
| Challenger | — | **REJECT** (5 challenges, 3 structural) | — |
| Execution | **REJECT** (3 kills) | **CONDITIONAL APPROVE** (4 blocking fixes) | **REJECT** (premature) |

## Candidate A: VPIN Gamma Scaling — DEAD

Three independent kill signals (unanimous):
1. **Math**: gamma-dependent spread = 7.5e-8 vs tick 0.0001 (4 orders of magnitude sub-tick)
2. **Latency**: AS continuous quoting incompatible with 36ms RTT
3. **Economics**: R16 proved ALL 1,080 configs negative on March data — no gamma fixes this

**Decision**: KILL Direction 4.2.2 on TMFD6.

## Candidate B: Dynamic Spread Threshold — ALIVE with fixes

### Unresolved Challenges (Challenger)

| # | Challenge | Severity | Resolution Required |
|---|-----------|----------|-------------------|
| C1 | VPIN-LOW threshold (4 pts) contradicts Section 6.3 adverse selection analysis | CRITICAL | Remove VPIN-LOW subtractor OR provide fill-level evidence |
| C2 | D1 diagnostic uses wrong metric (60s hold vs maker fill PnL) | HIGH | Redesign D1: mid-price displacement post-fill by VPIN regime |
| C4 | No R12 failure decomposition; VPIN-LOW aggression unjustified | HIGH | Use VPIN defensively only (TOXIC avoidance, not LOW seeking) |
| C3 | Gamma=0.01 is arbitrary (but R16 makes point moot) | MEDIUM | Acknowledge |
| C5 | D2 should measure spread at T+36ms, not episode duration | MEDIUM | Augment D2 |

### Execution Blocking Fixes

| # | Issue | Fix |
|---|-------|-----|
| E1 | Threshold floor 4 pts = expected-loss after adverse selection | Floor = 5 pts (not 4) |
| E2 | No cross-strategy signal bus — OpMM can't read VPIN signal | Option (c): drop VPIN for initial prototype, use ToD + volatility only |
| E3 | VpinRegimeSwitchStrategy not in strategies.yaml | Add config entry if VPIN conditioning kept |
| E4 | VPIN warmup = 1.4-2.9 hours, opening session uncovered | ToD + volatility must work independently of VPIN |

### Convergent Finding (Challenger + Execution agree)

**VPIN-LOW is dangerous**. Both reviewers independently concluded:
- Section 6.3 proves 5-6 pts minimum needed (RT cost + adverse selection)
- Lowering to 4 pts during LOW regime = trading at expected-loss
- VPIN should be used DEFENSIVELY ONLY: raise threshold during TOXIC, never lower during LOW
- The simplest viable prototype: ToD + volatility conditioning WITHOUT VPIN initially

## Candidate C: Hybrid — PREMATURE

Execution found that aggression +-1 tick on discrete LOB is binary (join queue vs don't), not smooth. Depends on B showing value first.

## Recommended Next Steps (pending your approval)

### Option 1: Simplified Candidate B (ToD + Volatility only, no VPIN)
- Remove VPIN conditioning entirely for Stage 2
- Dynamic threshold = base(5) + ToD(+1 opening/close) + vol(+1 high RV)
- Floor = 5 pts (never below), ceiling = 8 pts
- ~20 LOC change to OpMM
- Run diagnostics D1' (adverse selection by ToD) and D2 (spread duration at T+36ms)

### Option 2: Full Candidate B with VPIN (defensive only)
- VPIN adds threshold during TOXIC only (+2 pts), never subtracts
- Requires: signal bus OR embedded VPIN OR FE v3 feature
- More complex, uncertain value-add given R12 failure

### Option 3: Abandon both directions
- TMFD6 OpMM may be structurally unviable (R16 evidence)
- Focus resources elsewhere

---

## Artifacts

| File | Content |
|------|---------|
| `docs/alpha-research/round21_stage1_survey.md` | Full literature survey (474 lines, 16 papers) |
| `docs/alpha-research/round21_stage1_challenger_review.md` | Challenger review (5 challenges) |
| `docs/alpha-research/round21_stage1_execution_review.md` | Execution review (6 checks, 4 blocking fixes) |
| `docs/alpha-research/round21_stage1_summary.md` | This file |
