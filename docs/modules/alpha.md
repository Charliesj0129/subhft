# alpha — Alpha Governance Pipeline

> **Package**: `src/hft_platform/alpha/`
> **Runtime Plane**: Research/Operations
> **Files**: 18+

## Overview

6-gate alpha governance pipeline: from data validation through paper trading to production canary. Float permitted in this module (offline-only, Alpha Module Float Exception).

## Gate Pipeline

| Gate | Check | Module |
|------|-------|--------|
| **A** | Manifest + data-field + complexity | `validation.py::run_gate_a` |
| **B** | Per-alpha pytest execution | `validation.py::run_gate_b` |
| **C** | Standardized backtest + scorecard | `validation.py::run_gate_c` |
| **D** | Sharpe/drawdown thresholds | `promotion.py::_evaluate_gate_d` |
| **E** | Shadow session + execution quality | `promotion.py::_evaluate_gate_e` |
| **Canary** | Hold/escalate/rollback/graduate | `canary.py` |

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `validation.py` | `run_gate_a/b/c` | Gates A-C (57KB) |
| `promotion.py` | `_evaluate_gate_d/e` | Gates D-E + promotion (39KB) |
| `canary.py` | `CanaryManager` | Canary deployment management |
| `experiments.py` | `ExperimentRunner` | A-B experiment framework |
| `pool.py` | `AlphaPool` | Active alpha management |
| `paper_trade_runner.py` | `PaperTradeRunner` | Paper trading execution |
| `screener.py` | `AlphaScreener` | Alpha screening tool |
| `audit.py` | `AlphaAudit` | Audit trail |
| `latency_audit.py` | `LatencyAudit` | Execution latency profiling |

## Usage

```bash
uv run hft alpha validate <alpha_id>  # Run gates A-C
uv run hft alpha promote <alpha_id>   # Run gates D-E
uv run hft alpha canary <alpha_id>    # Start canary deployment
```

## Research Artifacts

Located in `research/alphas/<alpha_id>/`:
- `manifest.yaml` — Alpha metadata
- `backtest_results/` — Gate C outputs
- `scorecard.json` — Performance metrics

## Float Exception

Per Architecture Governance Rule 11:
- `float` permitted in `alpha/` and `research/` modules
- These are offline-only (CLI-invoked research pipeline)
- Precision Law applies exclusively to live trading paths
