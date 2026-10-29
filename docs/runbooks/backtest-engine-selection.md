# Backtest Engine Selection

> Stage 4 of the research-workflow deep consolidation (2026-05-28).
> Picks the right engine for the question and records the bias profile each
> engine carries.

## TL;DR

| Question | Engine | Reason |
|---|---|---|
| "Does this maker quoting strategy survive TAIFEX cost structure on CK ticks?" | `MakerEngine` (CK-direct) | Direct on `hft.market_data`, queue-depletion fills, no synthetic exchange overhead. |
| "Does this taker signal cross the IC threshold and produce a positive backtest?" | `HftNativeRunner` | hftbacktest V2 with string-named queue / latency / exchange models; fastest for IC-based research. |
| "Does this `BaseStrategy` survive in the hftbacktest event loop with realistic queue + latency models, including risk gating?" | `HftBacktestAdapter` | Wraps a live-grade `BaseStrategy` inside hftbacktest; supports `BacktestRiskConfig`. The closest to production. |

## Engine catalogue

### 1. `research.backtest.maker_engine.MakerEngine`

* **Data source**: ClickHouse `hft.market_data` directly (`ClickHouseSource`).
* **Fill model**: `QueueDepletionFill` (or any `FillModel` Protocol implementor).
* **Cost model**: `TAIFEXCost` from `config/research/cost_profiles.yaml`.
* **Latency**: optional `LatencyProfile(place_ns, cancel_ns)`; `None` = instant RTT.
  Gate C never runs it that way: a maker manifest whose `latency_profile` is
  empty or does not resolve to an exact id (or a listed alias) in
  `config/research/latency_profiles.yaml` makes the run INVALID.
* **Best for**: maker P&L on CK ticks; cost-sensitivity sweeps; per-spread
  P&L breakdown.
* **Known bias**: see [calibration table](#bias-matrix) below — CK-direct is
  typically the most reliable for maker P&L when paired with a calibrated
  queue assumption.

### 2. `research.backtest.hft_native_runner.HftNativeRunner`

* **Data source**: `*_l2.hftbt.npz` raw event arrays.
* **Models**: string-named — `queue_model="PowerProbQueueModel(3.0)"`,
  `latency_model="IntpOrderLatency"`,
  `exchange_model="NoPartialFillExchange"` — all defaults in
  `research.backtest.types.BacktestConfig`.
* **Best for**: taker IC-based threshold research; Gate-C single-shot
  validation; walk-forward inner loops.
* **Known bias**: `PowerProbQueueModel(3.0)` historically ran ~14× pessimistic
  vs CK-direct ground truth on TMFD6 (see memory entry
  `backtest_method_reliability.md`); calibrate with
  `research.calibration` before claiming maker P&L from this engine.
* **Fills**: none. The bridge sends no orders; positions are rebuilt from the
  signal ladder and valued mid-to-mid, less `BacktestConfig.cost_per_side_pts`
  per contract changed (None = gross), so a taker PnL from this engine never
  pays the spread. It is a screening method (`mid_to_mid_screening_v1`): Gate C
  still runs its IC, optimizer, walk-forward and stress checks on it, but its
  Sharpe can never satisfy Gate C. Use `TakerSim` below for taker PnL.

### 2b. `research.backtest.taker_sim.TakerSim`

* **Data source**: one hftbacktest event file per trading day
  (`ensure_hftbt_npz`); each day starts flat and is force-closed at its last
  touch. `split_trading_days` cuts a multi-day file by exchange timestamp into
  trading days `[previous session 14:00, D 14:00)` Asia/Taipei on the XTAI
  calendar (a night session belongs to the next day); `simulate_files` runs
  every day of a list of files and rejects a day found in two files.
* **Fills**: the same signal ladder as `HftNativeRunner`, sent as one IOC order
  at a time at the touch; each fill is booked at the price the exchange model
  gave it (method `taker_ioc_touch_v1`).
* **Cost model**: per contract from `config/research/cost_profiles.yaml`
  (`load_cost_profile`), including the closing contracts; exchange fees off.
* **Output**: dated daily rows (`TakerSimResult.daily_rows()`) and a Sharpe on
  the daily net series.
* **Gate C**: the taker lane's `gate_c.sharpe_oos` is the out-of-sample daily
  Sharpe of this simulator (the last `1 - is_oos_split` of the trading days,
  at least two), at the selected signal threshold; its dated rows feed the
  day-axis sub-gates and its Sharpe is the scorecard `sharpe_oos` Gate D reads.
  No cost profile, a failed run, or no data is ERROR (INVALID); fewer than two
  out-of-sample days is INSUFFICIENT_SAMPLE.

### 3. `hft_platform.backtest.adapter.HftBacktestAdapter`

* **Data source**: `*.npz` or in-memory `np.ndarray`.
* **Models**: string-named (same family as `HftNativeRunner`) plus raw
  `latency_us` / `modify_latency_us` / `cancel_latency_us` overrides.
* **Strategy**: wraps any `BaseStrategy` instance (live-grade).
* **Risk**: optional `BacktestRiskConfig` for `BacktestRiskEvaluator`-based
  gating.
* **Best for**: pre-promotion realism checks; risk-gated rehearsals; replay
  parity comparison.
* **Known bias**: closest of the three to live behaviour but inherits the
  same queue/latency model bias profile as `HftNativeRunner`. The optimistic
  end of the calibration table comes from this engine when latency is
  zeroed out.

## Bias matrix

Documented per-method reliability for maker P&L on TAIFEX futures (TXF/TMF
families), as established by repeated R47 calibration runs (see memory
entries `backtest_method_reliability.md`,
`calibration_queue_model_fix.md`, `r47_backtest_data_regression.md`).

| Method | Direction | Magnitude | Notes |
|---|---|---|---|
| CK-direct + `PowerProbQueueModel(3.0)` | pessimistic | ~14× | Cost: legacy `_loader_batch` per-snapshot DEPTH_CLEAR; fixed 2026-04-18 (`calibration_queue_model_fix.md`). |
| `HftNativeRunner` raw with zero latency | optimistic | up to ~577× | Reported on TMFD6 microstructure cross-spread artifacts. |
| `HftBacktestAdapter` with measured Shioaji P95 latency | calibrated | ~1× | Use `v2026-04-24_measured` profile for live-faithful results. |
| `MakerEngine` + `QueueDepletionFill` + `shioaji_p95` LatencyProfile | calibrated | ~1× | Default for cost-sensitivity sweeps once latency is enabled. |

**Rule:** Any claim about absolute P&L MUST cite the engine + queue model +
latency profile combination that produced it. The `hft-latency-profiling` skill's Latency Realism
Guard already requires P95 latency for promotion decisions; this matrix is
the corresponding queue-model requirement.

## Latency-profile pinning

* The canonical Shioaji live profile is `v2026-04-24_measured` in
  `config/research/latency_profiles.yaml` (see memory
  `shioaji_broker_asymmetric_latency_2026_04_24.md` — quote-activation
  P95 ~395 ms vs cancel P95 ~59 ms, 6.7× asymmetric).

## Cross-references

* Stage-3 cost-profile-ref governance: `AlphaManifest.cost_profile_refs`
  (`research/registry/schemas.py`).
* Stage-2 validation profile (loads pipeline overrides):
  `config/research/profiles/vm_ul6_strict.yaml`.
* Skill: `.agent/skills/hft-backtest/SKILL.md` for engine configuration and
  bias diagnosis (`references/validation.md`).
* Memory: `backtest_method_reliability.md`,
  `calibration_queue_model_fix.md`, `unified_backtest_framework.md`,
  `r47_backtest_data_regression.md`,
  `shioaji_broker_asymmetric_latency_2026_04_24.md`.
