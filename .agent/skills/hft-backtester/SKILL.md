---
name: hft-backtester
description: Use when configuring or debugging HftBacktestAdapter-based research runs, modeling realistic broker latency, or investigating backtest/live parity issues in the governed alpha pipeline.
---

# HFT Backtester

Use this skill for the platform wrapper around `hftbacktest`, not for generic engine API questions. Focus on adapter behavior, governed research inputs, and live-parity assumptions.

## Core Model

Treat the stack like this:

```text
HftBacktestAdapter -> hftbacktest engine -> strategy logic via StrategyContext
```

When the feature plane is enabled (`HFT_FEATURE_ENGINE_ENABLED=1`, default), the adapter supplies the shared FeatureEngine v3 (27 features across 3 schema versions: v1:16, v2:22, v3:27) so research and live strategy code read the same feature contract.

Feature parity requirements:
- Live and backtest must use the **same feature_set_id** (default: `lob_shared_v3`)
- Feature indices must match across environments (v3: [0-26])
- Quality flags (`GAP`, `STALE_INPUT`, `PARTIAL`) must be handled identically

## Non-Negotiable Inputs

Check these first when a backtest result looks wrong:

- use a real latency profile from `config/research/latency_profiles.yaml`
- preserve `local_ts` in the dataset
- keep timestamp units in nanoseconds
- verify tick cadence before interpreting latency-derived metrics

Treat missing `local_ts` as a parity bug. Step estimation will fall back and distort submit/cancel latency.

## Commands

Run the governed Gate C lane:

```bash
uv run python -m research.factory run-gate-c \
  <alpha_id> \
  --data research/data/processed/<alpha_id>/<file>.npy \
  --latency-profile <profile>
```

Load a latency profile in code:

```python
from research.tools.latency_profiles import load_latency_profile

profile = load_latency_profile("<profile>")
```

## Data Contract

Expect structured arrays with event and timestamp fields suitable for `hftbacktest`. Preserve:

- `ev`
- `exch_ts`
- `local_ts`
- price and quantity fields needed by the adapter

Generate an end-of-day snapshot before running book-based backtests that need initialization.

## Failure Patterns

| Symptom | Action |
| --- | --- |
| Sharpe is implausibly high | verify latency profile and `local_ts` cadence |
| Sharpe collapses to zero | inspect latency application and position-change triggers |
| Fill behavior diverges from live | check queue model, latency model, and timestamp realism |
| Feature-backed strategies fail in replay | confirm feature engine v3 wiring, feature_set_id=lob_shared_v3, indices [0-26] |
| Execution optimizer not applied in backtest | ExecutionOptimizer/ImbalanceTimer are live-only (not in backtest adapter) |

## Boundaries

- Use `hft-backtest` for low-level `hftbacktest` V2 API semantics.
- Use `validation-gate` for promotion interpretation.
- Use `hft-strategy-dev` for live strategy contract issues rather than adapter configuration.

## Latency Profile Measurement SOP

1. Run shadow session: `uv run hft run sim --shadow --broker <broker>`
2. Collect RTT samples from order round-trips (minimum 1000 samples)
3. Compute P50, P95, P99 from the collected samples
4. Add entry to `config/research/latency_profiles.yaml` with date stamp
5. Use P95 for standard backtests, P99 for stress tests

Profiles must cover `place_order`, `update_order`, and `cancel_order` independently. Each broker has a separate profile section.

## Gate C Parallel Optimization

```bash
# Parallel parameter sweep (default: min(grid_size, cpu_count, 8) workers)
uv run python -m research.factory run-gate-c <alpha_id> \
  --data <path.npy> --latency-profile <profile>

# Override worker count
export HFT_GATE_C_PARALLEL_WORKERS=4
```

Each worker deep-copies the alpha instance for isolation. The base threshold result is reused if it already exists from a previous run.

## Stress Test Configuration

- UL6 profile tightens latency and cost assumptions and increases stress multipliers
- Minimum stress scenarios: trending, mean-reverting, volatile, low-liquidity
- Robustness sweep across the full parameter grid
- Walk-forward validation required for promotion readiness

## Latency Realism Guard

CRITICAL: Internal system latency (tens of microseconds) vs broker API RTT (tens of milliseconds) represents a roughly 500x difference.

- Model `place_order`, `update_order`, `cancel_order` latencies separately in research and backtests
- Use at least P95 latency assumptions for promotion decisions, P99 for stress tests
- Sub-broker-RTT alpha half-lives are optimistic until validated via shadow or live evidence
- Missing latency profile in a backtest result means the result is non-promotion-ready

See `docs/architecture/latency-baseline-shioaji-sim-vs-system.md` for measured baselines.
