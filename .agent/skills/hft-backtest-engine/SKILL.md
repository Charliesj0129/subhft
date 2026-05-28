---
name: hft-backtest-engine
description: Use when configuring the hftbacktest V2 engine, HftBacktestAdapter, MakerEngine, HftNativeRunner, queue/exchange models, latency profiles, or the raw event-array contract. Supersedes the deprecated `hft-backtest` (raw-engine) and `hft-backtester` (adapter) skills.
---

# HFT Backtest Engine

Single configuration surface for the three backtest engines the platform uses
for research:

| Engine | Source | Best for |
|---|---|---|
| `MakerEngine` | `research/backtest/maker_engine.py` | CK-direct maker P&L; cost sweeps. |
| `HftNativeRunner` | `research/backtest/hft_native_runner.py` | hftbacktest-V2-native taker IC research. |
| `HftBacktestAdapter` | `src/hft_platform/backtest/adapter.py` | live-grade `BaseStrategy` rehearsals; replay parity. |

**Canonical configuration object** (Stage 4 of the 2026-05-28 consolidation):
`research.backtest.contract.BacktestContractSpec`.

```python
from research.backtest.contract import BacktestContractSpec
from research.registry.alpha_registry import AlphaRegistry

manifest = AlphaRegistry().load("c33_txfd6_solo_passive_maker").manifest
spec = BacktestContractSpec.from_manifest(manifest)
maker  = MakerEngine(**spec.maker_engine_kwargs())
native = HftNativeRunner(BacktestConfig(**spec.hft_native_runner_kwargs()))
adapter = HftBacktestAdapter(strategy=..., asset_symbol=spec.instrument,
                             data=path, **spec.hft_backtest_adapter_kwargs())
```

Full engine catalogue and the calibrated/pessimistic/optimistic bias matrix
live in `docs/runbooks/backtest-engine-selection.md`.

## V2 Rules (raw hftbacktest semantics)

Apply whenever you touch `hbt.elapse`, `hbt.submit_buy_order`, etc. directly:

- treat return values as status codes, test success with `== 0`;
- timestamps in nanoseconds;
- structured event arrays only — no ad-hoc column layouts;
- preserve `ev`, `exch_ts`, `local_ts` through every transform.

```python
while hbt.elapse(10_000_000) == 0:
    if hbt.submit_buy_order(...) == 0:
        ...
```

Missing `local_ts` is a parity bug: step estimation falls back and distorts
submit/cancel latency.

## Adapter contract (`HftBacktestAdapter`)

```
HftBacktestAdapter -> hftbacktest engine -> strategy logic via StrategyContext
```

When `HFT_FEATURE_ENGINE_ENABLED=1` (default), the adapter supplies
FeatureEngine v3 (27 features, `lob_shared_v3`) so research and live read
the same feature contract.

Parity requirements:
- same `feature_set_id` across live and backtest (default `lob_shared_v3`);
- feature indices must align (v3: [0-26]);
- quality flags (`GAP`, `STALE_INPUT`, `PARTIAL`) handled identically;
- `BacktestRiskConfig` opt-in for `BacktestRiskEvaluator`-based gating.

## Latency profile (mandatory)

Every backtest declares a profile from `config/research/latency_profiles.yaml`.
Canonical Shioaji production profile: **`v2026-04-24_measured`** (P95 ~395 ms
place, ~59 ms cancel — 6.7× asymmetric, see memory
`shioaji_broker_asymmetric_latency_2026_04_24.md`).

Latency profile measurement SOP:

1. `uv run hft run sim --shadow --broker <broker>`;
2. ≥ 1000 RTT samples per side (place / update / cancel — independent);
3. Compute P50, P95, P99 per side;
4. Add entry to `config/research/latency_profiles.yaml` with date stamp;
5. P95 for standard backtests, P99 for stress tests.

A backtest without a declared latency profile is a Gate D blocker.

## Data contract

Structured arrays with `ev`, `exch_ts`, `local_ts`, and price/qty fields.
Generate an end-of-day snapshot before any book-initialization-dependent
backtest. Golden-parquet prices are **x1,000,000** (platform is x10,000);
divide by 100 to convert. Stale or pre-2026-04-10 hftbacktest outputs are
invalidated by the depth-export bug — re-export.

## Model selection (deliberate, not defaults)

- constant latency only for early scaffolding;
- measured / interpolated latency for any scoring claim;
- conservative queue models for stress tests;
- partial-fill exchanges overstate realism when market impact is ignored.

For interpreting the resulting numbers — bias matrix, walk-forward, statistical
traps — use **`hft-backtest-validation`** (Stage-7 rename of the former
`hft-backtest-calibration`).

## Boundaries

- For interpreting backtest results, calibration drift, or statistical traps →
  `hft-backtest-validation`.
- For promotion-gate interpretation → `validation-gate`.
- For live strategy contracts → `hft-strategy-dev`.

## Latency Realism Guard

CRITICAL: internal system latency (tens of μs) vs broker API RTT (tens of ms)
is roughly 500× — sub-broker-RTT alpha half-lives are optimistic until
shadow-validated. See `docs/architecture/latency-baseline-shioaji-sim-vs-system.md`.
