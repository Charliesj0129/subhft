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
