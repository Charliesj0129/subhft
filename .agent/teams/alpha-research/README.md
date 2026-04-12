# Alpha Research Agent Team

Reusable team structure for alpha research rounds (R32+).

## Team Structure

| Role | Model | Job | Key Skills |
|------|-------|-----|------------|
| Lead (your session) | Opus | Orchestrate, inject context, KILL/PROMOTE | `hft-strategy-lifecycle`, `hft-release-gate` |
| Researcher | Opus | Literature search, hypothesis, `explore.py` | `taifex-alpha-kill-criteria`, `taifex-market-structure` |
| Devil's Advocate | Opus | Kill Checklist (H1-H6, S1-S6), adversarial review | `taifex-alpha-kill-criteria`, `hft-backtest-calibration` |
| Executor | Sonnet | `impl.py`, backtest, scorecard | `hft-strategy-sdk`, `hft-backtest-calibration`, `hft-test-hft` |

## Skill Pipeline (per stage)

```
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

## Post-Team: Promotion Path

If T7 = PROMOTE, the Lead follows `hft-strategy-lifecycle`:
1. Implement as `BaseStrategy` (use `hft-strategy-sdk`)
2. If MM: apply R47 patterns from `hft-mm-design`
3. Configure in `strategies.yaml` + `strategy_limits.yaml`
4. Shadow trade with `HFT_ORDER_SHADOW_MODE=1`
5. Run `hft-release-gate` before enabling live
6. Run `hft-production-audit` after first live session

## Hooks

Two hooks auto-enforce quality (configured in `.claude/settings.local.json`):
- **TaskCompleted**: Validates required fields before task can close
- **TeammateIdle**: Directs idle teammates to claim pending tasks
