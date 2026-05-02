# Standardized Backtest Engine — Design Spec

**Date**: 2026-04-15
**Status**: Approved
**Problem**: R6-R54 used 6+ backtest methods with biases from 14x pessimistic to 577x optimistic. Results scattered across console prints, ad-hoc CSVs, and inconsistent JSON. Memory accumulated contradictory PnL claims for the same strategy (R47: -27K to +61K). Root cause: no unified backtest engine for maker strategies, no enforced result persistence.

## Design Decisions (from brainstorming)

1. **Strategy types**: Both taker and maker; maker is primary direction
2. **Fill model**: CK-direct (queue depletion, `qf` parameter) is the standard for maker
3. **Output**: Unified JSON to `research/experiments/runs/<run_id>/` with full metadata. `make research` is the sole official entrance.
4. **Pipeline**: Single pipeline with auto-detection via `manifest.yaml` `strategy_type` field
5. **Data source**: ClickHouse only. Pipeline checks CK health at startup and errors with actionable message.

## Architecture

### BacktestEngine Protocol

```python
# research/backtest/engine.py

class BacktestEngine(Protocol):
    def run(self, config: BacktestConfig, data_source: DataSource) -> BacktestResult: ...

    @property
    def engine_type(self) -> str: ...  # "taker" | "maker"

    @property
    def fill_model_name(self) -> str: ...
```

Engine selection in pipeline Stage 4:

```python
strategy_type = manifest["strategy_type"]  # "taker" | "maker"
if strategy_type == "maker":
    engine = MakerEngine(fill_model=QueueDepletionFill(qf=config.queue_fraction))
else:
    engine = TakerEngine()  # wraps existing hft_native_runner
result = engine.run(config, data_source)
```

### MakerEngine Internal Architecture

```
MakerEngine
├── MakerStrategy (Protocol)  — injected, decides what to quote
├── FillModel (Protocol)      — determines fill logic
│   └── QueueDepletionFill(qf=0.5)  — default
├── CostModel                 — per-instrument fees
│   └── TAIFEXCost            — reads cost_profiles.yaml
└── ClickHouseSource          — data fetch + health check
```

#### MakerStrategy Protocol

```python
class MakerStrategy(Protocol):
    """Strategy decides quotes. Engine decides fills."""
    def on_tick(self, tick: TickData) -> list[QuoteAction]: ...
    def on_fill(self, fill: FillEvent) -> None: ...

# QuoteAction = PostQuote(side, price, qty) | CancelQuote(order_id) | Hold
```

This separates R47-specific logic (spread gate, QI skew, position management) from the backtest engine. R47MakerStrategy implements MakerStrategy. Future maker strategies implement the same protocol.

#### FillModel Protocol

```python
class FillModel(Protocol):
    def post_quote(self, side, price, qty, book_state) -> QueuePosition: ...
    def check_fills(self, positions: list[QueuePosition], tick: TickData) -> list[Fill]: ...

class QueueDepletionFill(FillModel):
    """Posts at best bid/ask, tracks queue ahead, volume consumes queue, queue=0 -> fill."""
    def __init__(self, queue_fraction: float = 0.5): ...
```

#### CostModel

```python
class TAIFEXCost(CostModel):
    """Loaded from config/research/cost_profiles.yaml"""
    # TMFD6: comm=1.3 pts/side, tax=0.7 pts/side -> RT=4.0 pts
    # TXFD6: comm=0.24 pts/side, tax=0.24 pts/side -> RT~0.48 pts
```

#### MakerEngine.run() Flow

```
1. CK health check (SELECT 1, fail -> raise with "docker compose up -d clickhouse")
2. Query tick + bidask data from ClickHouse for instrument + date range
3. Per-tick loop:
   a. strategy.on_tick(tick) -> QuoteActions
   b. fill_model.check_fills(outstanding_quotes, book_state) -> fills
   c. cost_model.apply(fills) -> net PnL per fill
   d. strategy.on_fill(fill) for each fill
   e. Record equity, position, fill details
4. Call metrics.py for Sharpe/IC/Sortino/max_dd
5. Call maker_scorecard.py for maker-specific metrics
6. Package into BacktestResult
```

### TakerEngine

Thin wrapper around existing `hft_native_runner.py`. No changes to the runner itself.

```python
class TakerEngine:
    def run(self, config, data_source) -> BacktestResult:
        runner = HftNativeRunner(config)
        raw_result = runner.run(data_source)
        return self._to_unified_result(raw_result)
```

### Unified BacktestResult

Extends existing `types.py` BacktestResult:

```python
@dataclass(frozen=True)
class BacktestResult:
    # === Existing fields (unchanged) ===
    signals: np.ndarray
    equity_curve: np.ndarray
    positions: np.ndarray
    sharpe_is: float
    sharpe_oos: float
    ic_mean: float
    ic_std: float
    ic_tstat: float
    ic_pvalue: float
    sortino: float
    cvar_5pct: float
    turnover: float
    max_drawdown: float
    regime_metrics: dict

    # === New: method provenance (solves memory confusion) ===
    run_id: str                  # UUID
    engine_type: str             # "taker" | "maker"
    fill_model: str              # "PowerProbQueue(3.0)" | "QueueDepletion(qf=0.5)"
    cost_model: str              # "TMFD6(comm=1.3,tax=0.7)"
    instrument: str              # "TMFD6" | "TXFD6"
    data_period: str             # "2026-03-01..2026-03-31"
    data_source: str             # "clickhouse://localhost:8123/hft"
    config_hash: str             # SHA256 of full config
    pipeline_mode: str           # "strict" | "triage"
    created_at: str              # ISO 8601

    # === New: maker-specific (None for taker) ===
    maker_scorecard: dict | None
    per_spread_breakdown: dict | None
    queue_fraction: float | None

    # === New: daily detail ===
    daily_pnl: list[dict] | None  # [{date, pnl, fills, max_dd}, ...]
```

### ResultStore

```python
class ResultStore:
    """Sole official write path for backtest results."""
    base_dir = Path("research/experiments/runs")

    def save(self, result: BacktestResult, alpha_id: str) -> Path:
        run_dir = self.base_dir / result.run_id
        run_dir.mkdir(parents=True)
        # backtest_report.json  — full result with metadata + metrics
        # config.json           — reproducible config snapshot
        # equity_curve.npy      — large arrays stored separately
        return run_dir

    def load(self, run_id: str) -> BacktestResult: ...

    def query(self, alpha_id: str = None, instrument: str = None,
              engine_type: str = None) -> list[BacktestResult]: ...
```

**Rule**: `ResultStore.save()` is only called in `pipeline.py` Stage 4. Ad-hoc scripts do not import ResultStore. They can run exploratory analysis but results are not official.

### Pipeline Integration

**Stage 4 changes in pipeline.py**:

```python
def _run_backtest(self, alpha_id, manifest, config):
    # Step 0: CK health check
    ClickHouseSource.health_check()

    # Step 1: Read manifest
    strategy_type = manifest["strategy_type"]  # required
    instrument = manifest["instrument"]        # required

    # Step 2: Auto-select engine + cost model
    cost = load_cost_profile(instrument)  # from config/research/cost_profiles.yaml

    if strategy_type == "maker":
        fill = QueueDepletionFill(qf=config.get("queue_fraction", 0.5))
        engine = MakerEngine(fill_model=fill, cost_model=cost)
    else:
        engine = TakerEngine()

    # Step 3: Execute
    result = engine.run(config, data_source)

    # Step 4: Persist (sole write point)
    ResultStore().save(result, alpha_id)

    return result
```

**manifest.yaml new required fields**:

```yaml
strategy_type: maker | taker   # NEW required
instrument: TMFD6               # NEW required
```

### Gate C Thresholds

Stored in `config/research/gate_thresholds.yaml`:

```yaml
taker:
  sharpe_is_min: 1.0
  sharpe_oos_min: 0.5
  is_oos_gap_max_pct: 50
  winning_day_pct_min: 55
  max_drawdown_pct: 30

maker:
  sharpe_is_min: 0.5        # maker has naturally lower Sharpe
  sharpe_oos_min: 0.3
  is_oos_gap_max_pct: 50
  winning_day_pct_min: 55
  max_drawdown_pct: 30
  pnl_per_fill_min_pts: 0   # must be net positive per fill
  adverse_fill_pct_max: 50
```

### Config Files

**New**: `config/research/cost_profiles.yaml`

```yaml
TMFD6:
  commission_pts_per_side: 1.3
  tax_pts_per_side: 0.7
  point_value_nwd: 10

TXFD6:
  commission_pts_per_side: 0.24
  tax_pts_per_side: 0.24
  point_value_nwd: 200
```

**New**: `config/research/gate_thresholds.yaml` (as above)

## File Changes Summary

### New files (6):
| File | Est. Lines | Purpose |
|------|-----------|---------|
| `research/backtest/engine.py` | ~60 | BacktestEngine Protocol + registry |
| `research/backtest/taker_engine.py` | ~80 | Wraps existing hft_native_runner |
| `research/backtest/maker_engine.py` | ~300 | CK-direct maker backtest |
| `research/backtest/fill_models.py` | ~120 | FillModel Protocol + QueueDepletionFill |
| `research/backtest/cost_models.py` | ~60 | CostModel + TAIFEX, reads cost_profiles.yaml |
| `research/backtest/result_store.py` | ~100 | JSON persistence to runs/<run_id>/ |

### Modified files (2):
| File | Change |
|------|--------|
| `research/backtest/types.py` | Extend BacktestResult with metadata fields |
| `research/pipeline.py` | Stage 4: engine selection branch + CK health check |

### New config files (2):
- `config/research/cost_profiles.yaml`
- `config/research/gate_thresholds.yaml`

### Retired files (moved to `research/tools/legacy/`):
- `research/tools/r47_ck_direct_backtest_v2.py` — logic absorbed into maker_engine.py
- `research/backtest/r47_maker_backtest.py` — replaced by MakerEngine + MakerStrategy

### Untouched files (8):
- `research/backtest/metrics.py`
- `research/backtest/auditor.py`
- `research/backtest/regime_splitter.py`
- `research/backtest/maker_scorecard.py`
- `research/backtest/hft_native_runner.py`
- `research/backtest/alpha_strategy_bridge.py`
- `research/backtest/feature_precompute.py`
- `research/backtest/lap_auditor.py`

### Total: ~720 lines new + ~200 lines modified + 2 YAML configs

## R47 Migration Path

1. Extract R47-specific strategy logic from `r47_ck_direct_backtest_v2.py` into `R47MakerStrategy(MakerStrategy)` — this maps to the existing `strategies/r47_maker.py` with a `MakerStrategy` protocol adapter
2. Update `research/alphas/r47_maker_pivot/manifest.yaml` to add `strategy_type: maker`, `instrument: TMFD6`
3. Run `make research ALPHA=r47_maker_pivot` — should produce identical results to CK-direct v2 (qf=0.5)
4. Regression test: compare new engine output vs stored R53 baseline (+29,747 pts / 25 TMFD6 days, CK-direct)

## Success Criteria

1. `make research ALPHA=r47_maker_pivot` produces a `backtest_report.json` with full metadata
2. Result includes: engine_type, fill_model, cost_model, instrument, data_period, config_hash
3. Any future agent reading memory can trace a PnL claim back to a specific `run_id` and reproduce it
4. Ad-hoc scripts cannot write to `runs/` — only `make research` can
5. CK health check blocks pipeline start with actionable error message
