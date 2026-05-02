# Backtest Risk Engine Integration

**Date**: 2026-04-05
**Status**: Draft
**Author**: Claude (Debugging Team audit follow-up)

## Problem

The backtest pipeline (`src/hft_platform/backtest/`) executes strategy intents directly via `adapter.execute_intent(intent)` without any risk checks. In the live pipeline, intents flow through `RiskEngine.evaluate()` which enforces price bands, position limits, max notional, and StormGuard state before reaching OrderAdapter.

This gap means:
- Backtest PnL is optimistic — it does not reflect risk rejections that would occur in live trading
- Gate C scorecards lack rejection rate data
- Gate D promotion decisions are based on unrealistic performance

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Fidelity | Configurable layers | Different research scenarios need different risk fidelity |
| Rejection behavior | Hard reject | Backtest PnL must reflect actual risk-filtered execution |
| Integration approach | New BacktestRiskEvaluator | Avoids entangling backtest with live runtime dependencies |
| Position tracking | Callback injection | Decouples evaluator from adapter internals |

## Architecture

### Flow (with risk enabled)

```
Strategy.handle_event()
  → OrderIntent[]
  → BacktestRiskEvaluator.evaluate(intent)
      → RiskDecision(approved=True/False)
  → if approved: adapter.execute_intent(intent)
  → if rejected: adapter._record_rejection(intent, reason)
```

### Flow (risk disabled — current default, backward compatible)

```
Strategy.handle_event()
  → OrderIntent[]
  → adapter.execute_intent(intent)
```

### New Components

| Component | File | Responsibility |
|-----------|------|----------------|
| `BacktestRiskConfig` | `backtest/risk_evaluator.py` | Dataclass controlling which validators are active |
| `BacktestRiskEvaluator` | `backtest/risk_evaluator.py` | Synchronous risk evaluation using live validator classes |

### Modified Components

| Component | Change |
|-----------|--------|
| `_hbt_utils.dispatch_strategy` | Insert `evaluate()` check before `execute_intent()` |
| `HftBacktestAdapter.__init__` | Accept `risk_config`, create evaluator, add rejection SoA buffer |
| `HftBacktestRunResult` | Add `risk_rejection_count` and `risk_rejection_breakdown` fields |
| `HftBacktestRunner` | Pass `risk_config` through to adapter |

### Unchanged Components

- `risk/engine.py` — live RiskEngine is not modified
- `risk/validators.py` — validator classes are reused directly, not modified

## Detailed Design

### BacktestRiskConfig

```python
@dataclass(frozen=True)
class BacktestRiskConfig:
    enabled: bool = True
    # Static validators (default ON)
    price_band: bool = True
    max_notional: bool = True
    per_symbol_notional: bool = True
    position_limit: bool = True
    # Advanced validators (default OFF)
    daily_loss_limit: bool = False   # Requires real-time PnL; backtest equity tracker is sampled, not tick-level
    storm_guard: bool = False        # Feed gap detection is meaningless in backtest
    # Risk config YAML path (reuse live risk config for threshold consistency)
    config_path: str = "config/base/risk.yaml"
```

**Why `daily_loss_limit` defaults OFF**: The live DailyLossLimitValidator relies on a real-time PnL accumulator updated on every fill. The backtest equity tracker samples at configurable intervals (`equity_sample_ns`), creating gaps where the PnL state is stale. Enabling it would produce false positives (premature halt) or false negatives (missed halt).

**Why `storm_guard` defaults OFF**: StormGuard transitions are triggered by feed gaps (`HFT_STORMGUARD_FEED_GAP_HALT_S`), which don't occur in backtest (data is pre-recorded, no network delays). Enabling it adds nondeterminism without modeling anything real.

### BacktestRiskEvaluator

```python
class BacktestRiskEvaluator:
    __slots__ = ("_validators", "_rejection_count", "_rejection_breakdown")

    def __init__(
        self,
        config: BacktestRiskConfig,
        position_provider: Callable[[str], int],
        price_scale_provider: PriceScaleProvider | None = None,
    ):
        # Load risk YAML (same loader as live RiskEngine)
        risk_config = self._load_risk_config(config.config_path)

        # Build validator list based on BacktestRiskConfig flags
        self._validators: list = []
        if config.price_band:
            self._validators.append(PriceBandValidator(risk_config, price_scale_provider))
        if config.max_notional:
            self._validators.append(MaxNotionalValidator(risk_config, price_scale_provider))
        if config.per_symbol_notional:
            self._validators.append(PerSymbolNotionalValidator(risk_config, price_scale_provider))
        if config.position_limit:
            self._validators.append(PositionLimitValidator(
                risk_config, price_scale_provider,
                position_provider=position_provider,
            ))
        if config.daily_loss_limit:
            self._validators.append(DailyLossLimitValidator(risk_config, price_scale_provider))

        self._rejection_count: int = 0
        self._rejection_breakdown: dict[str, int] = {}

    def evaluate(self, intent: OrderIntent) -> RiskDecision:
        """Synchronous risk evaluation. First rejecting validator wins."""
        # Type check (same as live RiskEngine line 546-549)
        price = getattr(intent, "price", None)
        if price is not None and not isinstance(price, int):
            return self._reject(intent, "FLOAT_PRICE")

        for v in self._validators:
            ok, reason = v.check(intent)
            if not ok:
                return self._reject(intent, reason)

        return RiskDecision(True, intent)

    def _reject(self, intent: OrderIntent, reason: str) -> RiskDecision:
        self._rejection_count += 1
        self._rejection_breakdown[reason] = self._rejection_breakdown.get(reason, 0) + 1
        return RiskDecision(False, intent, reason=reason)

    @property
    def rejection_count(self) -> int:
        return self._rejection_count

    @property
    def rejection_breakdown(self) -> dict[str, int]:
        return dict(self._rejection_breakdown)

    @staticmethod
    def _load_risk_config(config_path: str) -> dict:
        """Load risk YAML. Returns empty dict if file not found."""
        from pathlib import Path
        import yaml
        p = Path(config_path)
        if not p.exists():
            return {}
        with p.open() as f:
            return yaml.safe_load(f) or {}
```

### dispatch_strategy Integration

```python
# _hbt_utils.py
def dispatch_strategy(adapter, event, feature_event):
    intents = adapter.strategy.handle_event(adapter.ctx, event)
    if feature_event is not None and adapter.dispatch_feature_events:
        more = adapter.strategy.handle_event(adapter.ctx, feature_event)
        if more:
            intents.extend(more)
    for intent in intents:
        if adapter._risk_evaluator is not None:
            decision = adapter._risk_evaluator.evaluate(intent)
            if not decision.approved:
                adapter._record_rejection(intent, decision.reason)
                continue
        adapter.execute_intent(intent)
```

### Rejection Recording (Allocator Law compliant)

```python
# In HftBacktestAdapter.__init__:
self._reject_ts_ns = np.zeros(_REJECT_CAPACITY, dtype=np.int64)
self._reject_reasons: list[str] = []  # cold path, list is fine
self._reject_count: int = 0

def _record_rejection(self, intent: OrderIntent, reason: str) -> None:
    if self._reject_count >= len(self._reject_ts_ns):
        self._reject_ts_ns = np.resize(self._reject_ts_ns, len(self._reject_ts_ns) * 2)
    self._reject_ts_ns[self._reject_count] = timebase.now_ns()
    self._reject_reasons.append(reason)
    self._reject_count += 1
```

### Scorecard Output

`HftBacktestRunResult` adds:
```python
risk_rejection_count: int = 0
risk_rejection_breakdown: dict[str, int] = field(default_factory=dict)
```

Gate C scorecard can compute: `rejection_rate = rejection_count / (rejection_count + total_fills)`

### Backward Compatibility

- `risk_config=None` (default for `HftBacktestAdapter` and `HftBacktestRunner`) = no risk evaluation, behavior identical to current code
- All existing tests and backtests are unaffected
- Risk integration is opt-in only

## Test Plan

### Unit Tests (`test_backtest_risk_evaluator.py`)

| Test | Verifies |
|------|----------|
| `test_reject_price_band_exceeded` | PriceBandValidator rejects out-of-band price |
| `test_reject_position_limit` | PositionLimitValidator rejects via position_provider callback |
| `test_approve_valid_intent` | All validators pass → approved |
| `test_disabled_always_approves` | `enabled=False` → evaluate() always returns approved |
| `test_rejection_breakdown_accumulates` | Multiple rejections → correct reason counts |
| `test_loads_live_risk_yaml` | config_path loads thresholds from YAML |
| `test_float_price_rejected` | Float price type check matches live behavior |
| `test_selective_validators` | Only enabled validators are instantiated |

### Integration Tests (`test_backtest_risk_integration.py`)

| Test | Verifies |
|------|----------|
| `test_rejected_intent_not_submitted` | execute_intent not called for rejected intents |
| `test_approved_intent_submitted` | execute_intent called for approved intents |
| `test_no_risk_config_backward_compatible` | risk_config=None → all intents execute (current behavior) |
| `test_run_result_includes_rejection_data` | RunResult has rejection_count and breakdown |
| `test_position_provider_reflects_fills` | After fills update positions, position_limit validator sees new qty |

## Files Changed Summary

| File | Action | Lines (est.) |
|------|--------|-------------|
| `backtest/risk_evaluator.py` | **New** | ~90 |
| `backtest/_hbt_utils.py` | Modify `dispatch_strategy` | ~6 |
| `backtest/adapter.py` | Add risk_config param, rejection buffer, _record_rejection | ~25 |
| `backtest/runner.py` | Pass risk_config to adapter | ~5 |
| `tests/unit/test_backtest_risk_evaluator.py` | **New** | ~120 |
| `tests/unit/test_backtest_risk_integration.py` | **New** | ~100 |

**Total**: ~1 new file (production), 3 modified files, 2 new test files. Estimated ~350 lines.
