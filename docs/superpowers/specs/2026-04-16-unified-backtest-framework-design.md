# Unified hftbacktest Backtest Framework for TAIFEX

**Date**: 2026-04-16
**Status**: Approved
**Author**: Charlie + Claude
**Scope**: Replace CK-direct MakerEngine with calibrated hftbacktest, unify Gate C

## Problem Statement

The platform currently runs two independent backtest engines:

- **TakerEngine**: wraps `HftBacktestAdapter` with `PowerProbQueueModel(3.0)` — uncalibrated default
- **MakerEngine**: custom CK-direct engine with `QueueDepletionFill(qf=0.5)` — simple but unproven

Neither engine has been validated against live fill data. The R47 calibration crisis showed the same strategy producing -27K to +61K PnL depending on method. The 14x pessimism of `PowerProbQueueModel(3.0)` was our calibration failure, not a flaw in hftbacktest.

Additionally:
- Gate C has separate maker/taker paths — maker skips walk-forward, stress test, and parameter robustness
- The `.npz` export pipeline had a `DEPTH_EVENT` accumulation bug causing 577x PnL overestimate
- Two engines with different result formats create maintenance burden

## Goals

1. **Unified engine**: All strategies (maker + taker) run through `HftBacktestAdapter`
2. **Calibrated queue model**: Per-instrument `PowerProbQueueModel` exponent fitted to live fill data
3. **No `.npz` intermediate**: Direct ClickHouse streaming to hftbacktest `event_dtype` arrays
4. **Unified Gate C**: Single path with `strategy_type`-controlled sub-gate activation
5. **Per-instrument calibration framework**: Extensible to future instruments beyond TMFD6/TXFD6

## Non-Goals

- Regime-dependent exponent (future work if static exponent is insufficient)
- Automatic re-calibration in CI
- Multi queue model ensemble
- Taker path refactoring (already on hftbacktest)
- Forced migration of MakerEngine-style strategy interfaces

## Target Architecture

```
Before:
  manifest.yaml: strategy_type = ?
    +-- "taker" -> HftBacktestAdapter -> PowerProbQueueModel(3.0) -> .npz data
    +-- "maker" -> MakerEngine -> QueueDepletionFill(qf=0.5) -> CK SQL direct

After:
  manifest.yaml: strategy_type = ?
    +-- "taker" -> HftBacktestAdapter -> PowerProbQueueModel(calibrated) -> CK streaming
    +-- "maker" -> HftBacktestAdapter -> PowerProbQueueModel(calibrated) -> CK streaming
                                          ^ per-instrument exponent from calibration_profiles.yaml
```

Gate C unified:

```
run_gate_c(alpha, config)
  -> unified HftBacktestAdapter run
  -> sub-gate registry filters by strategy_type
  -> common:  sharpe, drawdown, winning_day, walk_forward, stress_test, param_robustness
  -> maker:   fill_quality, fill_rate_validation
  -> taker:   ic_evaluation, trend_contamination, oos_statistical
  -> unified GateCReport
```

## Phase Plan

| Phase | Deliverable | Exit Criteria | Dependencies |
|-------|------------|---------------|--------------|
| 1. Data Audit | Fill data inventory report | Per-instrument usable calibration days known | None |
| 2. Exponent Calibration | `calibration_profiles.yaml` + framework | Held-out validation composite score documented | Phase 1 |
| 3. CK Streaming Adapter | `backtest/ch_data_source.py` | Bit-identical results vs .npz path on same data | None (parallel with 1) |
| 4. Engine Replacement | Maker routes through HftBacktestAdapter | R47 produces results via hftbacktest path | Phase 2 + 3 |
| 5. Gate C Unification | Single `run_gate_c()` with sub-gate registry | All existing tests pass, maker + taker validated | Phase 4 |

---

## Phase 1: Data Audit

### Purpose

Before any calibration, precisely inventory: what fill data exists, where, quality, usability.

### Data Sources

| Source | Location | Known Status |
|--------|----------|-------------|
| CK Export Parquet | `research/data/ck_export/` | ~25 days TXFD6/TXFC6/TXFB6, schema unconfirmed |
| ClickHouse `hft.fills` | Docker ClickHouse | Schema exists, all calibration reports show null |
| ClickHouse `hft.trades` | Docker ClickHouse | Legacy parallel table |
| ClickHouse `hft.slippage_records` | Docker ClickHouse | Has `decision_mid`, `fill_price`, `latency_ns`, 90d TTL |
| ClickHouse `hft.shadow_orders` | Docker ClickHouse | Simulated orders, 30d TTL |
| R47 Live Deployment | Unknown | R47 deployed on TMFD6, fill persistence unconfirmed |

### Audit Script

New file: `research/calibration/audit.py`

Outputs per-instrument:
- `n_trading_days`, `n_fills`, `date_range`
- `n_fills_with_queue_position` (critical for calibration)
- `n_fills_with_decision_price` (needed for adverse fill calculation)
- `n_fills_with_latency` (needed for latency-aware calibration)
- `fill_rate_per_day`
- Quality flags: `missing_queue_pos`, `sparse_data`, etc.

### L2 Market Data Cross-Reference

Calibration requires fill data **aligned** with L2 market data. The intersection of days with both live fills AND L2 data is the usable calibration set.

### Output

`research/calibration/artifacts/data_audit_report.json`

### Exit Criteria

- Report generated
- Per-instrument usable calibration days known
- If < 5 days available, Phase 2 methodology adjusts (see degradation strategy)

---

## Phase 2: Exponent Calibration Framework

### Calibration Variables

**Primary** (must calibrate):

| Parameter | Search Range | Step | Notes |
|-----------|-------------|------|-------|
| `queue_model_type` | `power_prob` / `power_prob2` / `power_prob3` / `log_prob` | discrete | 4 candidate models from hftbacktest |
| `exponent` (n) | 0.5 - 3.0 | 0.25 | Only for `power_prob*` variants |

**Fixed** (from existing config):

| Parameter | Source |
|-----------|--------|
| `latency_us` | `latency_profiles.yaml` P95 |
| `maker_fee` / `taker_fee` | `cost_profiles.yaml` |
| `exchange_model` | `partial_fill_exchange` |
| `tick_size` / `lot_size` | per-instrument known |

### Calibration Methodology

```
For each instrument (TMFD6, TXFD6):
  1. Load usable calibration days (Phase 1 audit: fill intersection L2 data)
  2. Split: 70% calibration / 30% held-out validation
     (if < 10 days, use leave-one-out cross-validation)
  3. For each (queue_model_type, exponent) combination:
     a. Run hftbacktest replay on each calibration day with the strategy
        that generated the live fills (R47 for TMFD6; for other instruments,
        must match the strategy used during the live fill collection period)
     b. Extract simulated fills: count, timestamps, prices, sides
     c. Compare to live fills (same day, same instrument, daily aggregate)
     d. Compute fit score
  4. Select highest composite fit score
  5. Validate on held-out days
  6. Write to calibration_profiles.yaml
```

### Fit Score

Multi-dimensional weighted scoring, each dimension normalized 0-1:

| Dimension | Weight | Calculation |
|-----------|--------|-------------|
| `fill_rate_score` | 0.35 | `1 - |sim_fills/day - live_fills/day| / live_fills/day` |
| `adverse_fill_score` | 0.25 | `1 - |sim_adverse_pct - live_adverse_pct| / max(live_adverse_pct, 1)` |
| `pnl_direction_score` | 0.25 | Daily win/loss direction consistency (sim vs live same day) |
| `pnl_magnitude_score` | 0.15 | `1 - |sim_pnl - live_pnl| / |live_pnl|` (capped at 0) |

Weight rationale:
- Fill rate (0.35): most fundamental — if fill count is wrong, nothing else is trustworthy
- Adverse fill (0.25): core maker risk, must be accurately simulated
- PnL direction (0.25): direction consistency more important than magnitude precision
- PnL magnitude (0.15): least stable metric, lower weight to avoid overfitting

### Live Fill Alignment Method

Daily aggregate comparison (not per-fill timestamp matching):
- `fill_count_sim` vs `fill_count_live`
- Buy/sell fill count ratio
- `adverse_fill_pct`: fills where mid_price moved against our position within 1s
  (buy fill: mid_price dropped; sell fill: mid_price rose)
- Daily PnL (FIFO matching, after costs)

### Degradation Strategy (Insufficient Data)

| Usable Days | Calibration Method | Validation Method |
|-------------|-------------------|-------------------|
| >= 15 | 70/30 train/test split | Held-out test set |
| 8-14 | Leave-one-out CV | CV mean score |
| 5-7 | Leave-one-out CV | CV mean + sensitivity report (no hard threshold) |
| < 5 | Literature default + sensitivity sweep | Advisory only, no accuracy promise |

### Output

**`config/research/calibration_profiles.yaml`**:

```yaml
TMFD6:
  queue_model: power_prob
  exponent: 1.5                     # calibrated
  calibration_date: "2026-04-20"
  data_days_used: 12
  held_out_days: 5
  composite_score: 0.78
  validation_scores:
    fill_rate_score: 0.82
    adverse_fill_score: 0.75
    pnl_direction_score: 0.80
    pnl_magnitude_score: 0.65
  confidence: "medium"              # low/medium/high

TXFD6:
  queue_model: power_prob
  exponent: 1.25
  # ... same structure
```

**Artifacts**: `research/calibration/artifacts/<instrument>/sweep_results.json`, `validation_report.json`

### New Files

```
research/calibration/
  __init__.py
  audit.py              # Phase 1 data audit
  sweep.py              # Exponent grid sweep (per-instrument generic)
  validate.py           # Held-out validation
  scoring.py            # CalibrationScore computation
  config.py             # Load calibration profiles
```

### Exit Criteria

- Each target instrument has entry in `calibration_profiles.yaml`
- Held-out validation composite score documented (target >= 0.6, exact threshold after Phase 1)
- Sweep heatmap shows smooth score landscape (not noise-dominated)

---

## Phase 3: ClickHouse Streaming Adapter

### Purpose

Eliminate `.npz` intermediate file. Stream ClickHouse market data directly as hftbacktest-compatible numpy arrays.

### Event Type Mapping

```
ClickHouse hft.market_data row:

  event_type = "BidAsk":
    1. Emit DEPTH_CLEAR_EVENT (snapshot semantics: replace, not accumulate)
    2. Emit N rows: DEPTH_EVENT | EXCH_EVENT | BUY_EVENT  (bid side, per level)
    3. Emit N rows: DEPTH_EVENT | EXCH_EVENT | SELL_EVENT  (ask side, per level)
    px = price / price_scale  (descale to float for hftbacktest)

  event_type = "Tick":
    1. Emit 1 row: TRADE_EVENT | EXCH_EVENT | {BUY_EVENT|SELL_EVENT}
    px = price / price_scale
```

**Critical**: `DEPTH_CLEAR_EVENT` before each BidAsk snapshot prevents the accumulation bug (577x PnL overestimate root cause).

### Module Design

New file: `src/hft_platform/backtest/ch_data_source.py`

```python
class ChDataSource:
    """Streams ClickHouse market data as hftbacktest-compatible numpy arrays."""

    def __init__(
        self,
        ch_host: str = "localhost",
        ch_port: int = 9000,
        price_scale: int = 1_000_000,
    ): ...

    def load_day(
        self, instrument: str, date: str, max_depth_levels: int = 5,
    ) -> np.ndarray:
        """Load one trading day as hftbacktest event_dtype array."""
        ...

    def load_days(
        self, instrument: str, dates: list[str],
    ) -> list[np.ndarray]:
        """Load multiple days. Returns list (one array per day)."""
        ...
```

Implements `BacktestDataSource` protocol:

```python
class BacktestDataSource(Protocol):
    def load_day(self, instrument: str, date: str) -> np.ndarray: ...
    def load_days(self, instrument: str, dates: list[str]) -> list[np.ndarray]: ...
```

### Built-in Validation

Automatic post-load sanity checks on every `load_day()`:

1. **Event count**: `n_depth > 0` and `n_trade > 0`
2. **Timestamp monotonicity**: `exch_ts` strictly non-decreasing
3. **Price sanity**: No zeros, no negatives, within instrument price range
4. **Spread sanity**: Reconstructed `best_ask > best_bid` at each snapshot (catches accumulation bug)
5. **Event ratio**: `depth:trade` ratio within reasonable bounds

Raises `DataValidationError` on failure with diagnostic details.

### Performance

- Single-day estimate: TMFD6 ~375K rows -> ~2M event rows after depth expansion -> ~128MB numpy array
- One day at a time in memory
- ClickHouse native protocol (port 9000) with columnar numpy mode
- No caching (fresh query each time, calibration is run-once)

### HftBacktestAdapter Modification

`adapter.py` `data` parameter changes from `str` to `str | np.ndarray`:

```python
def __init__(self, ..., data: str | np.ndarray, ...):
    # str -> .npz file path (existing behavior, preserved as fallback)
    # ndarray -> direct from ChDataSource (new default)
```

`BacktestAsset.data()` already accepts `ndarray`, so no hftbacktest modification needed.

### Exit Criteria

- Same strategy + same parameters + same data: streaming adapter vs .npz path produce bit-identical fill sequences
- Float precision differences < 1e-10 are acceptable and documented

---

## Phase 4: Engine Replacement

### MakerStrategyBridge

Existing maker strategies use `on_tick(event) -> PostQuote|CancelQuote|Hold`. HftBacktestAdapter uses `handle_event(event) -> list[OrderIntent]`. Bridge translates between interfaces:

New file: `src/hft_platform/backtest/maker_bridge.py`

```python
class MakerStrategyBridge(BaseStrategy):
    """Wraps MakerEngine-style strategy for HftBacktestAdapter."""

    def __init__(self, inner: MakerStrategyProtocol): ...

    def handle_event(self, event) -> list[OrderIntent]:
        action = self._inner.on_tick(self._translate_event(event))
        match action:
            case PostQuote(side, price, qty):
                return [OrderIntent(intent_type=IntentType.NEW, side=side,
                                    price=price, qty=qty, tif=TIF.GTC)]
            case CancelQuote(order_id):
                return [OrderIntent(intent_type=IntentType.CANCEL,
                                    ref_order_id=order_id)]
            case Hold():
                return []
```

New maker strategies should use `BaseStrategy` directly. Bridge is for backward compatibility only.

### Calibration Profile Loading

New helper in adapter:

```python
def load_calibration_profile(instrument: str) -> dict:
    """Load calibrated queue model parameters for an instrument.
    Raises CalibrationNotFoundError if not calibrated."""
    ...
```

`HftBacktestAdapter.__init__` accepts `queue_model="auto"` which triggers calibration profile lookup by instrument.

### Routing Change in _gate_c.py

Maker path changes from:

```python
# Before:
source = ClickHouseSource(...)
fill_model = QueueDepletionFill(qf=config.queue_fraction)
engine = MakerEngine(source, fill_model, cost_model)
result = engine.run(strategy, instrument)
```

To:

```python
# After:
ch_source = ChDataSource()
data = ch_source.load_days(instrument, dates)
if hasattr(strategy, 'on_tick') and not hasattr(strategy, 'handle_event'):
    strategy = MakerStrategyBridge(strategy)
adapter = HftBacktestAdapter(
    strategy=strategy, data=data,
    queue_model="auto", instrument=instrument, ...)
result = adapter.run()
```

### Unified BacktestResult

Single result dataclass for both maker and taker:

```python
@dataclass(frozen=True)
class BacktestResult:
    # Identity
    run_id: str
    config_hash: str
    instrument: str
    strategy_name: str
    strategy_type: str                 # "maker" | "taker"
    # Provenance
    engine: str                        # "hftbacktest"
    queue_model: str                   # "PowerProbQueueModel(1.5)"
    calibration_profile_id: str
    data_source: str                   # "clickhouse_streaming"
    latency_profile: str
    # Core metrics
    pnl_pts: float
    n_fills: int
    n_trading_days: int
    equity_curve: np.ndarray
    # Maker-specific (None for taker)
    pnl_per_fill: float | None
    adverse_fill_pct: float | None
    fill_rate_per_day: float | None
    # Taker-specific (None for maker)
    ic_is: float | None
    ic_oos: float | None
```

### Archive

```
research/backtest/maker_engine.py  -> research/backtest/legacy/maker_engine.py
research/backtest/fill_models.py   -> research/backtest/legacy/fill_models.py
```

Preserved for reference, not deleted.

### Exit Criteria

- R47 maker strategy runs through MakerStrategyBridge + HftBacktestAdapter + calibrated exponent
- BacktestResult includes full provenance
- No production code calls MakerEngine directly

---

## Phase 5: Gate C Unification

### Sub-Gate Registry Architecture

Replace the 463-line if/else fork with a sub-gate registry:

```python
class SubGate(Protocol):
    name: str
    applies_to: set[str]     # {"maker"}, {"taker"}, {"maker", "taker"}

    def evaluate(
        self, result: BacktestResult, config: GateConfig, thresholds: dict,
    ) -> SubGateResult: ...

@dataclass(frozen=True)
class SubGateResult:
    name: str
    passed: bool
    metrics: dict[str, float]
    details: str
```

### Sub-Gate Matrix

| Sub-Gate | Maker | Taker | Notes |
|----------|:-----:|:-----:|-------|
| `sharpe_threshold` | Y | Y | IS/OOS Sharpe thresholds |
| `max_drawdown` | Y | Y | Maximum drawdown threshold |
| `winning_day_pct` | Y | Y | Daily win rate threshold |
| `walk_forward` | Y | Y | **NEW for maker** — rolling OOS validation |
| `stress_test` | Y | Y | **NEW for maker** — extreme spread/volatility |
| `parameter_robustness` | Y | Y | **NEW for maker** — parameter perturbation stability |
| `fill_quality` | Y | - | pnl_per_fill, adverse_fill_pct |
| `fill_rate_validation` | Y | - | **NEW** — consistency with calibration profile |
| `ic_evaluation` | - | Y | IC + detrended IC |
| `trend_contamination` | - | Y | Trend contamination detection |
| `oos_statistical` | - | Y | BH/Bonferroni multiple testing |

### Maker Walk-Forward Design

```
Split N trading days into K folds (5-fold if >= 15 days)
For each fold:
  Train: run full backtest on training days
  Test: run backtest on test days
  Compare: test PnL direction consistency, fill rate stability

Pass condition:
  - >= 60% of folds have positive test PnL
  - Test fill rate within 50% of train fill rate across all folds
```

### Updated gate_thresholds.yaml

New maker thresholds:

```yaml
maker:
  # Existing:
  sharpe_is_min: 0.5
  sharpe_oos_min: 0.3
  is_oos_gap_max_pct: 50
  winning_day_pct_min: 55
  max_drawdown_pct: 30
  pnl_per_fill_min_pts: 0
  adverse_fill_pct_max: 50
  # New:
  walk_forward_positive_fold_pct: 60
  fill_rate_deviation_max: 0.5
  stress_max_drawdown_multiplier: 2.0
  param_robustness_pnl_cv_max: 0.8
```

### Unified run_gate_c Flow

```python
def run_gate_c(alpha, config, root, resolved_data_paths, experiments_base):
    strategy_type = config.strategy_type

    # 1. Unified backtest engine
    ch_source = ChDataSource()
    data = ch_source.load_days(config.instrument, config.dates)
    strategy = load_strategy(alpha, config)
    if strategy_type == "maker" and hasattr(strategy, 'on_tick'):
        strategy = MakerStrategyBridge(strategy)
    adapter = HftBacktestAdapter(
        strategy=strategy, data=data,
        queue_model="auto", instrument=config.instrument, ...)
    result = adapter.run()

    # 2. Load thresholds
    thresholds = load_thresholds(strategy_type)

    # 3. Run applicable sub-gates
    sub_gates = get_registered_sub_gates()
    gate_results = []
    for gate in sub_gates:
        if strategy_type not in gate.applies_to:
            continue
        gate_results.append(gate.evaluate(result, config, thresholds))

    # 4. Unified verdict
    all_passed = all(r.passed for r in gate_results)

    # 5. Unified report
    report = GateCReport(
        alpha_id=alpha.alpha_id,
        strategy_type=strategy_type,
        engine="hftbacktest",
        calibration_profile=result.calibration_profile_id,
        overall_passed=all_passed,
        sub_gate_results=gate_results,
        backtest_result=result,
    )
    save_report(report, experiments_base)
    return report
```

### File Changes

```
src/hft_platform/alpha/_gate_c.py              <- REWRITE (~463 -> ~300 lines)
src/hft_platform/alpha/_sub_gates/             <- NEW directory
  __init__.py
  common.py         <- sharpe, drawdown, winning_day, walk_forward, stress_test, param_robustness
  maker.py          <- fill_quality, fill_rate_validation
  taker.py          <- ic_evaluation, trend_contamination, oos_statistical
  registry.py       <- sub-gate registration + discovery
config/research/gate_thresholds.yaml           <- UPDATE
```

### Exit Criteria

- Single `run_gate_c()` handles maker + taker
- All existing taker tests pass (no regression)
- Maker now includes walk-forward + stress test + parameter robustness sub-gates
- R47 maker passes unified Gate C (or fails with clear reason)
- Old if/else fork completely removed

---

## Cross-Cutting Concerns

### Dependency Graph

```
Phase 1 (Data Audit) --------+
                              +--> Phase 2 (Calibration) --+
Phase 3 (CK Streaming) ------+                             +--> Phase 4 (Replacement) --> Phase 5 (Gate C)
  (parallel with Phase 1)     +-----------------------------+
```

### Risk Matrix

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| Live fills < 5 days | Phase 2 cannot train/test split | Medium | Degrade to literature default + sensitivity report |
| CK export lacks queue position | Cannot do per-fill calibration | High | Daily aggregate comparison (fill count, PnL) — design accounts for this |
| Held-out score < 0.6 | Low calibration confidence | Medium | Report but don't block — calibrated exponent is still better than uncalibrated 3.0 |
| MakerStrategyBridge translation | R47 behavior differs vs MakerEngine | Low | Bridge is straightforward (PostQuote -> GTC limit); differences come from fill model |
| ClickHouse unavailable | All backtests blocked | Low | Clear error messages + fallback suggestion |
| hftbacktest API break | Adapter breaks | Low | Version pinned >=2.4,<3; regression tests before upgrade |

### Testing Strategy

| Level | Coverage | Method |
|-------|---------|--------|
| Unit | ChDataSource conversion | Known CK rows -> verify event_dtype output |
| Unit | MakerStrategyBridge | PostQuote/CancelQuote/Hold -> OrderIntent mapping |
| Unit | Sub-gate logic | Given BacktestResult -> verify pass/fail |
| Integration | CK -> ndarray -> hftbacktest -> fills | Full pipeline on known data |
| Integration | Gate C unified path | Maker + taker through single path |
| Regression | .npz vs streaming adapter | Same data, same params, bit-identical fills |
| Validation | Calibration held-out | Phase 2 model validation (not code test) |

### Rollback Plan

- Phase 1-2: Pure research artifacts, zero production risk
- Phase 3: New file only, deletable
- Phase 4: MakerEngine archived (not deleted), `_gate_c.py` revertible
- Phase 5: `_gate_c.py` rewrite is largest change; all Gate C tests must pass pre- and post-change

### Acceptance Criteria (Multi-Dimensional)

Exact thresholds set after Phase 1 data audit. Target structure:

- Fill rate: `|sim - live| / live < threshold` per instrument
- Adverse fill ratio: `|sim - live| < threshold` per instrument
- PnL direction: daily win/loss direction consistency > 70%

If data insufficient for all three dimensions, document which are validated and which are advisory.

---

## File Inventory

### New Files

| File | Phase | Purpose |
|------|-------|---------|
| `research/calibration/__init__.py` | 1 | Package init |
| `research/calibration/audit.py` | 1 | Data audit CLI tool |
| `research/calibration/sweep.py` | 2 | Exponent grid sweep |
| `research/calibration/validate.py` | 2 | Held-out validation |
| `research/calibration/scoring.py` | 2 | CalibrationScore computation |
| `research/calibration/config.py` | 2 | Load calibration profiles |
| `config/research/calibration_profiles.yaml` | 2 | Per-instrument calibrated params |
| `src/hft_platform/backtest/ch_data_source.py` | 3 | CK streaming adapter |
| `src/hft_platform/backtest/maker_bridge.py` | 4 | MakerEngine strategy bridge |
| `src/hft_platform/alpha/_sub_gates/__init__.py` | 5 | Sub-gate package |
| `src/hft_platform/alpha/_sub_gates/common.py` | 5 | Common sub-gates |
| `src/hft_platform/alpha/_sub_gates/maker.py` | 5 | Maker sub-gates |
| `src/hft_platform/alpha/_sub_gates/taker.py` | 5 | Taker sub-gates |
| `src/hft_platform/alpha/_sub_gates/registry.py` | 5 | Sub-gate registry |

### Modified Files

| File | Phase | Change |
|------|-------|--------|
| `src/hft_platform/backtest/adapter.py` | 3+4 | Accept `ndarray` data + `queue_model="auto"` |
| `src/hft_platform/alpha/_gate_c.py` | 4+5 | Rewrite: unified path + sub-gate registry |
| `config/research/gate_thresholds.yaml` | 5 | Add maker walk-forward/stress thresholds |

### Archived Files

| File | Phase | Destination |
|------|-------|-------------|
| `research/backtest/maker_engine.py` | 4 | `research/backtest/legacy/maker_engine.py` |
| `research/backtest/fill_models.py` | 4 | `research/backtest/legacy/fill_models.py` |
