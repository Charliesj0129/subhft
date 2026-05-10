---
name: validation-gate
description: Use when running or interpreting alpha Gates A-E, diagnosing promotion blockers, or checking whether research evidence is sufficient for validation, paper trade, or canary promotion.
---

# Alpha Validation Gates

Use this skill to decide whether an alpha is allowed to move forward. Treat `src/hft_platform/alpha/validation.py`, `src/hft_platform/alpha/promotion.py`, and the active config as the source of truth for exact thresholds.

## Gate Map

| Gate | Purpose | Typical Evidence |
| --- | --- | --- |
| A | manifest, dataset, and governance validity | manifest, metadata, allowed roots |
| B | correctness and testability | alpha-specific tests and coverage |
| C | research performance under realistic backtest assumptions | scorecard, latency profile, stress run |
| D | promotion readiness against configured thresholds | scorecard plus feature-set parity |
| E | paper-trade execution quality | recorded sessions and governance report |
| Canary | controlled live exposure | canary config and evaluation output |

## Runbook

Run the standard validation entrypoint:

```bash
uv run hft alpha validate <alpha_id>
```

Run the factory gate lane when you need explicit control over data and latency:

```bash
uv run python -m research.factory run-gate-c \
  <alpha_id> \
  --data research/data/processed/<alpha_id>/<file>.npy \
  --latency-profile <profile>
```

Promote through Gate D and E:

```bash
uv run hft alpha promote <alpha_id>
```

Inspect canary state:

```bash
uv run hft alpha canary status
uv run hft alpha canary evaluate <alpha_id>
```

## What To Check

Check Gate A for:

- required manifest fields
- dataset path and metadata validity
- declared data fields and complexity budget

Check Gate B for:

- alpha-specific test discovery from the repository root
- passing tests
- required coverage for the alpha module

Check Gate C for:

- a real latency profile
- scorecard metrics under the configured threshold set
- stress behavior and walk-forward evidence
- warn-only diagnostics versus true blockers

Check Gate D for:

- configured promotion thresholds
- feature-set version parity with live runtime

Check Gate E for:

- required paper-trade session count
- execution reject-rate quality
- governance report completeness

## High-Value Failure Patterns

| Symptom | Likely Cause | Action |
| --- | --- | --- |
| Gate A rejects `data_ul` or metadata | missing or stale sidecar metadata | restamp and validate the dataset |
| Gate B fails from the wrong root | validation launched from `research/` instead of repo root | rerun from the repository root |
| Gate C Sharpe collapses to zero | latency wrapper keeps deferring fills | inspect position-latency logic with `hft-backtester` |
| Gate C inflates unrealistically | missing `local_ts` or wrong step cadence | rebuild data with proper timestamps |
| Gate D blocks on feature-set version | manifest and live feature registry diverged | align the manifest version |
| Gate E blocks on execution quality | paper sessions or reject-rate evidence incomplete | record more sessions and rerun governance checks |

## Cross-References

- Use `hft-alpha-research` for scaffold and dataset preparation.
- Use `hft-backtester` when Gate C issues look like latency-model or adapter problems.
- Use `paper-trader` for Gate E session recording and shadow-trading evidence.

## Gate E Paper-Trade Governance

```bash
# Record a paper trade session
make research-record-paper ALPHA=<id> ARGS='--trading-day YYYY-MM-DD \
  --started-at <ISO8601> --ended-at <ISO8601> \
  --execution-reject-rate 0.004 --reject-rate-p95 0.008 --regime trending'

# Summarize sessions
make research-summarize-paper ALPHA=<id> ARGS='--out outputs/paper_summary_<id>.json'

# Pre-check governance
make research-check-paper-governance ALPHA=<id> ARGS='--strict --out outputs/paper_governance_<id>.json'
```

Gate E requires `min_shadow_sessions=5` by default. Each session records execution quality metrics including reject rate, fill quality, and regime classification.

## Canary Configuration

- Weight: starts at 0.1, graduated by the canary evaluator
- States: `hold` / `escalate` / `rollback` / `graduate`
- Config-driven and reversible at every stage
- Runtime: `src/hft_platform/alpha/canary.py`

## Promotion Artifact Paths

| Artifact | Path |
| --- | --- |
| Validation report | `research/experiments/validations/<alpha_id>/<stamp>/` |
| Promotion decision | `research/experiments/promotions/<alpha_id>/<stamp>/` |
| Paper governance report | `research/experiments/promotions/<alpha_id>/<stamp>/paper_governance_report.json` |
| Canary config | Generated during `hft alpha promote` |

## UL6 Strict Thresholds

The `vm_ul6` validation profile enforces tighter requirements across all gates:

- **Gate A**: Mandatory data provenance fields (`source`, `generator`, `rng_seed`), paper linkage required
- **Gate C**: Tighter Sharpe/drawdown thresholds, additional stress scenarios (trending, mean-reverting, volatile, low-liquidity), walk-forward validation
- **Gate D**: Elevated Sharpe minimum and drawdown maximum, feature-set version parity enforced
- **Gate E**: Extended paper-trade session requirements, stricter reject-rate thresholds
- **Live promotion**: Rust benchmark gate required for hot-path alphas
