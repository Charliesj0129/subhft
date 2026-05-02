# Backtest Risk Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in risk evaluation to the backtest pipeline so that Gate C/D scorecards reflect realistic risk-filtered PnL.

**Architecture:** New `BacktestRiskEvaluator` class wraps existing validator classes (`PriceBandValidator`, `MaxNotionalValidator`, `PerSymbolNotionalValidator`, `PositionLimitValidator`) in a synchronous `evaluate()` method. It is wired into `dispatch_strategy()` to filter intents before `execute_intent()`. Risk is opt-in via `risk_config` parameter — `None` (default) preserves current behavior.

**Tech Stack:** Python 3.12, existing `risk/validators.py` classes, `contracts/strategy.py` `RiskDecision` dataclass, numpy SoA buffers for rejection logging.

**Spec:** `docs/superpowers/specs/2026-04-05-backtest-risk-engine-design.md`

---

### Task 1: Create BacktestRiskConfig and BacktestRiskEvaluator

**Files:**
- Create: `src/hft_platform/backtest/risk_evaluator.py`
- Test: `tests/unit/test_backtest_risk_evaluator.py`

- [ ] **Step 1: Write failing tests for BacktestRiskEvaluator**

Create `tests/unit/test_backtest_risk_evaluator.py`:

```python
"""Tests for BacktestRiskEvaluator — synchronous risk evaluation for backtests."""
from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator
from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, TIF


def _make_intent(
    price: int = 1_000_000,
    qty: int = 1,
    side: Side = Side.BUY,
    symbol: str = "2330",
    strategy_id: str = "test_strat",
    intent_type: IntentType = IntentType.NEW,
) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
    )


class TestBacktestRiskEvaluatorUnit:
    def test_approve_valid_intent(self, tmp_path):
        """All validators pass → approved."""
        cfg_file = tmp_path / "risk.yaml"
        cfg_file.write_text("global_defaults:\n  max_price_cap: 99999\n  max_notional: 999999999\n  max_position_lots: 9999\n")
        config = BacktestRiskConfig(config_path=str(cfg_file))
        evaluator = BacktestRiskEvaluator(config, position_provider=lambda s: 0)
        intent = _make_intent()
        decision = evaluator.evaluate(intent)
        assert decision.approved is True

    def test_reject_float_price(self, tmp_path):
        """Float price type check matches live RiskEngine behavior."""
        cfg_file = tmp_path / "risk.yaml"
        cfg_file.write_text("global_defaults: {}\n")
        config = BacktestRiskConfig(config_path=str(cfg_file))
        evaluator = BacktestRiskEvaluator(config, position_provider=lambda s: 0)
        intent = _make_intent()
        # Monkey-patch price to float to trigger type check
        object.__setattr__(intent, "price", 100.5)
        decision = evaluator.evaluate(intent)
        assert decision.approved is False
        assert decision.reason_code == "FLOAT_PRICE"

    def test_reject_position_limit(self, tmp_path):
        """PositionLimitValidator rejects when position_provider returns high qty."""
        cfg_file = tmp_path / "risk.yaml"
        cfg_file.write_text("global_defaults:\n  max_position_lots: 5\n  max_price_cap: 99999\n  max_notional: 999999999\n")
        config = BacktestRiskConfig(config_path=str(cfg_file))
        # Provider says we already hold 5 lots
        evaluator = BacktestRiskEvaluator(config, position_provider=lambda s: 5)
        intent = _make_intent(qty=1)
        decision = evaluator.evaluate(intent)
        assert decision.approved is False
        assert "POSITION" in decision.reason_code.upper()

    def test_disabled_always_approves(self, tmp_path):
        """enabled=False → evaluate() always returns approved."""
        config = BacktestRiskConfig(enabled=False, config_path=str(tmp_path / "nope.yaml"))
        evaluator = BacktestRiskEvaluator(config, position_provider=lambda s: 0)
        intent = _make_intent()
        decision = evaluator.evaluate(intent)
        assert decision.approved is True

    def test_rejection_breakdown_accumulates(self, tmp_path):
        """Multiple rejections → correct reason counts."""
        cfg_file = tmp_path / "risk.yaml"
        cfg_file.write_text("global_defaults:\n  max_position_lots: 0\n  max_price_cap: 99999\n  max_notional: 999999999\n")
        config = BacktestRiskConfig(config_path=str(cfg_file))
        evaluator = BacktestRiskEvaluator(config, position_provider=lambda s: 100)
        for _ in range(3):
            evaluator.evaluate(_make_intent())
        assert evaluator.rejection_count == 3
        breakdown = evaluator.rejection_breakdown
        assert sum(breakdown.values()) == 3

    def test_selective_validators(self, tmp_path):
        """Only enabled validators are instantiated."""
        cfg_file = tmp_path / "risk.yaml"
        cfg_file.write_text("global_defaults: {}\n")
        config = BacktestRiskConfig(
            config_path=str(cfg_file),
            price_band=True,
            max_notional=False,
            per_symbol_notional=False,
            position_limit=False,
        )
        evaluator = BacktestRiskEvaluator(config, position_provider=lambda s: 0)
        assert len(evaluator._validators) == 1

    def test_missing_config_file_uses_empty_defaults(self):
        """Non-existent config_path → empty config, validators still instantiate."""
        config = BacktestRiskConfig(config_path="/nonexistent/risk.yaml")
        evaluator = BacktestRiskEvaluator(config, position_provider=lambda s: 0)
        # Should not raise; validators use their internal defaults
        intent = _make_intent()
        decision = evaluator.evaluate(intent)
        # With empty config, validators use permissive defaults → approve
        assert decision.approved is True

    def test_cancel_intent_always_approved(self, tmp_path):
        """CANCEL intents bypass all validators (same as live)."""
        cfg_file = tmp_path / "risk.yaml"
        cfg_file.write_text("global_defaults:\n  max_position_lots: 0\n")
        config = BacktestRiskConfig(config_path=str(cfg_file))
        evaluator = BacktestRiskEvaluator(config, position_provider=lambda s: 100)
        intent = _make_intent(intent_type=IntentType.CANCEL)
        decision = evaluator.evaluate(intent)
        assert decision.approved is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_backtest_risk_evaluator.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'hft_platform.backtest.risk_evaluator'`

- [ ] **Step 3: Implement BacktestRiskConfig and BacktestRiskEvaluator**

Create `src/hft_platform/backtest/risk_evaluator.py`:

```python
"""Synchronous risk evaluator for backtest — reuses live validator classes."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, RiskDecision
from hft_platform.core.pricing import PriceScaleProvider
from hft_platform.risk.validators import (
    DailyLossLimitValidator,
    MaxNotionalValidator,
    PerSymbolNotionalValidator,
    PositionLimitValidator,
    PriceBandValidator,
    RiskValidator,
)

logger = get_logger("backtest_risk")


@dataclass(frozen=True)
class BacktestRiskConfig:
    """Controls which risk validators are active during backtest."""

    enabled: bool = True
    # Static validators (default ON)
    price_band: bool = True
    max_notional: bool = True
    per_symbol_notional: bool = True
    position_limit: bool = True
    # Advanced validators (default OFF)
    daily_loss_limit: bool = False
    storm_guard: bool = False  # reserved for future use
    # Risk config YAML path (reuse live risk config for threshold consistency)
    config_path: str = "config/base/risk.yaml"


class BacktestRiskEvaluator:
    """Synchronous risk evaluation for backtests.

    Reuses the same validator classes as the live RiskEngine but without
    asyncio queues, StormGuard FSM, Rust fast paths, or MetricsRegistry.
    """

    __slots__ = ("_validators", "_rejection_count", "_rejection_breakdown", "_enabled")

    def __init__(
        self,
        config: BacktestRiskConfig,
        position_provider: Callable[[str], int],
        price_scale_provider: PriceScaleProvider | None = None,
    ) -> None:
        self._enabled = config.enabled
        self._rejection_count: int = 0
        self._rejection_breakdown: dict[str, int] = {}
        self._validators: list[RiskValidator] = []

        if not self._enabled:
            return

        risk_config = self._load_risk_config(config.config_path)

        if config.price_band:
            self._validators.append(PriceBandValidator(risk_config, price_scale_provider))
        if config.max_notional:
            self._validators.append(MaxNotionalValidator(risk_config, price_scale_provider))
        if config.per_symbol_notional:
            self._validators.append(PerSymbolNotionalValidator(risk_config, price_scale_provider))
        if config.position_limit:
            self._validators.append(PositionLimitValidator(
                risk_config,
                price_scale_provider,
                position_provider=position_provider,
            ))
        if config.daily_loss_limit:
            self._validators.append(DailyLossLimitValidator(risk_config, price_scale_provider))

    def evaluate(self, intent: OrderIntent) -> RiskDecision:
        """Synchronous risk evaluation. First rejecting validator wins."""
        if not self._enabled:
            return RiskDecision(True, intent)

        # Float price type check (same as live RiskEngine)
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
        return RiskDecision(False, intent, reason)

    @property
    def rejection_count(self) -> int:
        return self._rejection_count

    @property
    def rejection_breakdown(self) -> dict[str, int]:
        return dict(self._rejection_breakdown)

    @staticmethod
    def _load_risk_config(config_path: str) -> dict[str, Any]:
        """Load risk YAML. Returns empty dict if file not found."""
        p = Path(config_path)
        if not p.exists():
            logger.warning("backtest_risk_config_not_found", path=config_path)
            return {}
        with p.open() as f:
            return yaml.safe_load(f) or {}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/test_backtest_risk_evaluator.py -v
```

Expected: 8 passed

- [ ] **Step 5: Lint check**

```bash
uv run ruff check src/hft_platform/backtest/risk_evaluator.py tests/unit/test_backtest_risk_evaluator.py
```

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/backtest/risk_evaluator.py tests/unit/test_backtest_risk_evaluator.py
git commit -m "feat(backtest): add BacktestRiskEvaluator with configurable validators"
```

---

### Task 2: Wire risk evaluator into dispatch_strategy and adapter

**Files:**
- Modify: `src/hft_platform/backtest/_hbt_utils.py:146-154`
- Modify: `src/hft_platform/backtest/adapter.py:61-88` (init params) and add `_record_rejection`
- Test: `tests/unit/test_backtest_risk_integration.py`

- [ ] **Step 1: Write failing integration tests**

Create `tests/unit/test_backtest_risk_integration.py`:

```python
"""Integration tests for risk evaluator wired into backtest dispatch."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from hft_platform.backtest._hbt_utils import dispatch_strategy
from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator
from hft_platform.contracts.strategy import (
    IntentType,
    OrderIntent,
    RiskDecision,
    Side,
    TIF,
)


def _make_intent(price: int = 1_000_000, qty: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
    )


def _make_adapter(risk_evaluator=None):
    adapter = MagicMock()
    adapter._risk_evaluator = risk_evaluator
    adapter.dispatch_feature_events = False
    adapter.strategy.handle_event.return_value = [_make_intent()]
    adapter._reject_count = 0
    adapter._reject_reasons = []
    adapter._reject_ts_ns = np.zeros(64, dtype=np.int64)
    return adapter


class TestDispatchStrategyRiskGate:
    def test_no_risk_config_backward_compatible(self):
        """risk_evaluator=None → all intents execute (current behavior)."""
        adapter = _make_adapter(risk_evaluator=None)
        dispatch_strategy(adapter, MagicMock(), None)
        adapter.execute_intent.assert_called_once()

    def test_approved_intent_submitted(self):
        """Risk approves → execute_intent called."""
        evaluator = MagicMock()
        evaluator.evaluate.return_value = RiskDecision(True, _make_intent())
        adapter = _make_adapter(risk_evaluator=evaluator)
        dispatch_strategy(adapter, MagicMock(), None)
        adapter.execute_intent.assert_called_once()

    def test_rejected_intent_not_submitted(self):
        """Risk rejects → execute_intent NOT called, _record_rejection called."""
        evaluator = MagicMock()
        evaluator.evaluate.return_value = RiskDecision(False, _make_intent(), "POSITION_LIMIT")
        adapter = _make_adapter(risk_evaluator=evaluator)
        dispatch_strategy(adapter, MagicMock(), None)
        adapter.execute_intent.assert_not_called()
        adapter._record_rejection.assert_called_once()

    def test_mixed_intents_partial_execution(self):
        """Two intents: first approved, second rejected → one execute, one reject."""
        intent_ok = _make_intent(price=100_000)
        intent_bad = _make_intent(price=999_999_999)

        evaluator = MagicMock()
        evaluator.evaluate.side_effect = [
            RiskDecision(True, intent_ok),
            RiskDecision(False, intent_bad, "PRICE_BAND"),
        ]
        adapter = _make_adapter(risk_evaluator=evaluator)
        adapter.strategy.handle_event.return_value = [intent_ok, intent_bad]

        dispatch_strategy(adapter, MagicMock(), None)
        assert adapter.execute_intent.call_count == 1
        assert adapter._record_rejection.call_count == 1

    def test_position_provider_reflects_fills(self, tmp_path):
        """After position changes, position_limit validator sees updated qty."""
        cfg_file = tmp_path / "risk.yaml"
        cfg_file.write_text(
            "global_defaults:\n"
            "  max_position_lots: 3\n"
            "  max_price_cap: 99999\n"
            "  max_notional: 999999999\n"
        )
        positions = {"2330": 2}  # already hold 2
        config = BacktestRiskConfig(config_path=str(cfg_file))
        evaluator = BacktestRiskEvaluator(
            config,
            position_provider=lambda s: positions.get(s, 0),
        )
        # Intent to buy 1 more (total 3) — should be at limit
        intent = _make_intent(qty=1)
        decision = evaluator.evaluate(intent)
        # Depending on validator impl, may approve at exactly limit or reject
        # Update position to 3 and try again with qty=1
        positions["2330"] = 3
        intent2 = _make_intent(qty=1)
        decision2 = evaluator.evaluate(intent2)
        assert decision2.approved is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_backtest_risk_integration.py -v
```

Expected: FAIL (dispatch_strategy doesn't check `_risk_evaluator` yet)

- [ ] **Step 3: Modify dispatch_strategy in _hbt_utils.py**

In `src/hft_platform/backtest/_hbt_utils.py`, replace the `dispatch_strategy` function (lines 146-154):

```python
def dispatch_strategy(adapter: object, event: object, feature_event: object | None) -> None:
    """Call strategy.handle_event and execute returned intents.

    If adapter._risk_evaluator is set, each intent is evaluated before
    submission. Rejected intents are recorded but not executed.
    """
    intents = adapter.strategy.handle_event(adapter.ctx, event)  # type: ignore[attr-defined]
    if feature_event is not None and adapter.dispatch_feature_events:  # type: ignore[attr-defined]
        more = adapter.strategy.handle_event(adapter.ctx, feature_event)  # type: ignore[attr-defined]
        if more:
            intents.extend(more)
    risk_eval = getattr(adapter, "_risk_evaluator", None)
    for intent in intents:
        if risk_eval is not None:
            decision = risk_eval.evaluate(intent)
            if not decision.approved:
                adapter._record_rejection(intent, decision.reason_code)  # type: ignore[attr-defined]
                continue
        adapter.execute_intent(intent)  # type: ignore[attr-defined]
```

- [ ] **Step 4: Add risk_config param and rejection buffer to HftBacktestAdapter**

In `src/hft_platform/backtest/adapter.py`, add after the existing `feature_array_source` parameter (line 87):

Add parameter: `risk_config: BacktestRiskConfig | None = None,`

Add import at top of file:
```python
from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator
```

Add after `self._next_equity_sample_ns = 0` (around line 120):

```python
        # Risk evaluator (opt-in)
        _REJECT_CAPACITY = 256
        self._reject_ts_ns = np.zeros(_REJECT_CAPACITY, dtype=np.int64)
        self._reject_reasons: list[str] = []
        self._reject_count: int = 0
        if risk_config is not None and risk_config.enabled:
            self._risk_evaluator: BacktestRiskEvaluator | None = BacktestRiskEvaluator(
                risk_config,
                position_provider=lambda sym: self.positions.get(sym, 0),
                price_scale_provider=None,
            )
        else:
            self._risk_evaluator = None
```

Add method to `HftBacktestAdapter`:

```python
    def _record_rejection(self, intent: OrderIntent, reason: str) -> None:
        """Record a risk rejection in SoA buffers."""
        if self._reject_count >= len(self._reject_ts_ns):
            new_cap = len(self._reject_ts_ns) * 2
            new_buf = np.zeros(new_cap, dtype=np.int64)
            new_buf[: len(self._reject_ts_ns)] = self._reject_ts_ns
            self._reject_ts_ns = new_buf
        self._reject_ts_ns[self._reject_count] = timebase.now_ns()
        self._reject_reasons.append(reason)
        self._reject_count += 1
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/unit/test_backtest_risk_integration.py tests/unit/test_backtest_risk_evaluator.py -v
```

Expected: All pass

- [ ] **Step 6: Run existing backtest tests to verify no regressions**

```bash
uv run pytest tests/unit/ -k "backtest" --no-header -q 2>&1 | tail -5
```

Expected: All existing tests pass (risk_config defaults to None → no behavior change)

- [ ] **Step 7: Lint check**

```bash
uv run ruff check src/hft_platform/backtest/_hbt_utils.py src/hft_platform/backtest/adapter.py tests/unit/test_backtest_risk_integration.py
```

- [ ] **Step 8: Commit**

```bash
git add src/hft_platform/backtest/_hbt_utils.py src/hft_platform/backtest/adapter.py tests/unit/test_backtest_risk_integration.py
git commit -m "feat(backtest): wire BacktestRiskEvaluator into dispatch_strategy"
```

---

### Task 3: Add rejection data to HftBacktestRunResult and Runner

**Files:**
- Modify: `src/hft_platform/backtest/runner.py:34-45` (RunResult) and `run()` method
- Test: `tests/unit/test_backtest_risk_integration.py` (add test)

- [ ] **Step 1: Write failing test for RunResult rejection data**

Append to `tests/unit/test_backtest_risk_integration.py`:

```python
class TestRunResultRejectionData:
    def test_run_result_includes_rejection_fields(self):
        """HftBacktestRunResult has risk_rejection_count and breakdown."""
        from hft_platform.backtest.runner import HftBacktestRunResult

        result = HftBacktestRunResult(
            run_id="test",
            config_hash="abc",
            symbol="2330",
            strategy_name="demo",
            data_path="/tmp/test.npz",
            pnl=0.0,
            equity_points=0,
            used_synthetic_equity=False,
            report_path=None,
            risk_rejection_count=5,
            risk_rejection_breakdown={"POSITION_LIMIT": 3, "PRICE_BAND": 2},
        )
        assert result.risk_rejection_count == 5
        assert result.risk_rejection_breakdown["POSITION_LIMIT"] == 3

    def test_run_result_defaults_to_zero_rejections(self):
        """Default RunResult has zero rejections."""
        from hft_platform.backtest.runner import HftBacktestRunResult

        result = HftBacktestRunResult(
            run_id="test",
            config_hash="abc",
            symbol="2330",
            strategy_name="demo",
            data_path="/tmp/test.npz",
            pnl=0.0,
            equity_points=0,
            used_synthetic_equity=False,
            report_path=None,
        )
        assert result.risk_rejection_count == 0
        assert result.risk_rejection_breakdown == {}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_backtest_risk_integration.py::TestRunResultRejectionData -v
```

Expected: FAIL with `TypeError: unexpected keyword argument 'risk_rejection_count'`

- [ ] **Step 3: Add rejection fields to HftBacktestRunResult**

In `src/hft_platform/backtest/runner.py`, modify the `HftBacktestRunResult` dataclass (around line 34):

```python
@dataclass(frozen=True)
class HftBacktestRunResult:
    run_id: str
    config_hash: str
    symbol: str
    strategy_name: str
    data_path: str
    pnl: float
    equity_points: int
    used_synthetic_equity: bool
    report_path: str | None
    risk_rejection_count: int = 0
    risk_rejection_breakdown: dict[str, int] = field(default_factory=dict)
```

- [ ] **Step 4: Wire rejection data in Runner.run()**

In `src/hft_platform/backtest/runner.py`, in the `run()` method where `HftBacktestRunResult` is constructed (find the return statement), add the rejection data from the adapter:

```python
            # Extract risk rejection data if risk evaluator was active
            risk_rejection_count = 0
            risk_rejection_breakdown: dict[str, int] = {}
            if hasattr(adapter, "_risk_evaluator") and adapter._risk_evaluator is not None:
                risk_rejection_count = adapter._risk_evaluator.rejection_count
                risk_rejection_breakdown = adapter._risk_evaluator.rejection_breakdown
```

And add these to the `HftBacktestRunResult(...)` constructor call:

```python
                risk_rejection_count=risk_rejection_count,
                risk_rejection_breakdown=risk_rejection_breakdown,
```

- [ ] **Step 5: Add risk_config passthrough in Runner**

In `HftBacktestRunner.__init__`, store the risk config. In the `run()` method where `HftBacktestAdapter` is constructed, pass it through.

Add to `HftBacktestConfig`:
```python
    risk_config: Any = None  # BacktestRiskConfig or None
```

Where the adapter is constructed in `run()`, add `risk_config=self.cfg.risk_config`.

- [ ] **Step 6: Run all tests**

```bash
uv run pytest tests/unit/test_backtest_risk_integration.py tests/unit/test_backtest_risk_evaluator.py -v
```

Expected: All pass

- [ ] **Step 7: Run full backtest test suite for regressions**

```bash
uv run pytest tests/unit/ -k "backtest" --no-header -q 2>&1 | tail -5
```

Expected: All existing tests pass

- [ ] **Step 8: Lint check**

```bash
uv run ruff check src/hft_platform/backtest/runner.py
```

- [ ] **Step 9: Commit**

```bash
git add src/hft_platform/backtest/runner.py tests/unit/test_backtest_risk_integration.py
git commit -m "feat(backtest): add risk rejection data to HftBacktestRunResult"
```
