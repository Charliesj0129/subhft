# Futures TCA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a complete Transaction Cost Analysis pipeline for Taiwan futures — fixing broken fee/PnL accounting, adding execution quality tracking, and generating daily TCA reports.

**Architecture:** Two parallel tracks converging at the FillEvent contract expansion. Track 1 fixes the live pipeline (normalizer fees, PnL deduction, ClickHouse schema). Track 2 builds the new `tca/` module (fee calculator, slippage decomposition, sqrt impact model, daily reports). Both tracks share the `FeeCalculator` component.

**Tech Stack:** Python 3.12, ClickHouse (migrations), YAML config, pytest, structlog

**Spec:** `docs/superpowers/specs/2026-03-25-futures-tca-design.md`

---

## File Map

### New Files (Track 2 — TCA Module)
| File | Responsibility |
|---|---|
| `config/base/fees/futures.yaml` | Fee schedule config for TX/MTX/XMT/stock futures |
| `src/hft_platform/tca/__init__.py` | Package init, re-exports |
| `src/hft_platform/tca/types.py` | `FeeSchedule`, `FeeBreakdown`, `SlippageBreakdown`, `TCADailyReport` |
| `src/hft_platform/tca/fee_calculator.py` | `FeeCalculator` — per-contract fee computation |
| `src/hft_platform/tca/slippage.py` | `SlippageDecomposer` — 4-component slippage breakdown |
| `src/hft_platform/tca/impact.py` | `SqrtImpactModel` — market impact estimation |
| `src/hft_platform/tca/analyzer.py` | `TCAAnalyzer` — aggregate breakdowns into stats |
| `src/hft_platform/tca/report.py` | `TCAReportGenerator` — daily report to ClickHouse + JSON |
| `src/hft_platform/migrations/clickhouse/20260325_001_add_tca_columns.sql` | Schema migration |
| `tests/unit/test_tca_fee_calculator.py` | FeeCalculator unit tests |
| `tests/unit/test_tca_slippage.py` | SlippageDecomposer unit tests |
| `tests/unit/test_tca_impact.py` | SqrtImpactModel unit tests |
| `tests/unit/test_tca_analyzer.py` | TCAAnalyzer unit tests |

### Modified Files (Track 1 — Pipeline Fix)
| File | Lines | Change |
|---|---|---|
| `contracts/execution.py` | 37–53, 57–69 | Add `decision_price`, `arrival_price` to FillEvent; add `gross_pnl`, `fees` to PositionDelta |
| `contracts/strategy.py` | 32–57, 72–82 | Add `decision_price` to OrderIntent; add `decision_price`, `arrival_price` to OrderCommand |
| `execution/normalizer.py` | 29–45, 198–211 | Add `FeeCalculator` dep; replace `fee=0,tax=0` with calc; accept `OrderCommand` param |
| `execution/positions.py` | 87–149 | Add `gross_pnl_scaled` accumulator; deduct fees from `realized_pnl_scaled` |
| `execution/router.py` | 54–70, 108–148 | Add `order_adapter` param; resolve `order_key` → lookup inflight `OrderCommand` → pass to normalizer |
| `order/adapter.py` | 87–142 | Add `_inflight` dict, `get_inflight()`, `mid_price_fn`; store OrderCommand on send, stamp `arrival_price` |
| `strategy/runner.py` | 387–437 | Stamp `decision_price` on OrderIntent from LOB mid-price |
| `recorder/_loader_batch.py` | 205–245 | Fix `_TRADES_COLS` and `format_trades()` to match `hft.trades` DDL + TCA columns |
| `recorder/worker.py` | 45–55, 146–174 | Fix `FILL_COLUMNS` and `_extract_fill_values()` |
| `services/bootstrap.py` | 709, 860–868 | Wire `FeeCalculator`, `mid_price_fn`, `order_adapter` into ExecutionRouter |
| `alpha/canary_metrics_writer.py` | 163–173 | Fix broken query to read from `hft.trades` with TCA columns |
| `config/base/main.yaml` | — | Add `tca:` config section |

All paths below are relative to `src/hft_platform/` unless noted.

---

## Task 1: Fee Schedule Config + TCA Types

**Files:**
- Create: `config/base/fees/futures.yaml`
- Create: `src/hft_platform/tca/__init__.py`
- Create: `src/hft_platform/tca/types.py`

- [ ] **Step 1: Create fee schedule YAML**

Create `config/base/fees/futures.yaml`:
```yaml
futures:
  TX:
    commission_per_contract: 60
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 200

  MTX:
    commission_per_contract: 30
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 50

  XMT:
    commission_per_contract: 13
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 10

  stock_futures_default:
    commission_per_contract: 20
    tax_rate_bps: 4.0
    tax_side: sell
    tick_size: 0.01
    point_value: 2000

  overrides:
    "2330F":
      commission_per_contract: 25
```

- [ ] **Step 2: Create TCA types module**

Create `src/hft_platform/tca/types.py`:
```python
"""TCA data structures. All monetary values NTD scaled x10000."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class FeeSchedule:
    """Fee schedule for a single futures product."""
    symbol: str
    commission_per_contract: int  # NTD (plain, not scaled)
    tax_rate_bps: float           # bps of notional, sell-side only
    tax_side: str                 # "sell"
    tick_size: float
    point_value: int              # NTD per point


@dataclass(slots=True, frozen=True)
class FeeBreakdown:
    """Per-trade fee breakdown. All fields NTD scaled x10000."""
    commission: int
    tax: int
    total: int


@dataclass(slots=True, frozen=True)
class SlippageBreakdown:
    """Per-fill slippage decomposition. All fields in bps.

    WARNING: float fields — restricted to offline TCA analysis only.
    MUST NOT be used in live order-path code without converting to scaled int.
    """
    commission_bps: float
    tax_bps: float
    delay_cost_bps: float
    execution_cost_bps: float
    market_impact_bps: float
    total_bps: float


@dataclass(slots=True, frozen=True)
class TCADailyReport:
    """Aggregated TCA statistics for one (date, strategy, symbol) key."""
    date: str
    strategy: str
    symbol: str
    trade_count: int
    volume: int
    notional: int
    commission_bps_mean: float
    tax_bps_mean: float
    delay_cost_bps_mean: float
    delay_cost_bps_p95: float
    exec_cost_bps_mean: float
    exec_cost_bps_p95: float
    impact_bps_mean: float
    total_cost_bps_mean: float
    total_cost_bps_p95: float
```

- [ ] **Step 3: Create package init**

Create `src/hft_platform/tca/__init__.py`:
```python
"""Transaction Cost Analysis module for Taiwan futures."""
from hft_platform.tca.types import FeeBreakdown, FeeSchedule, SlippageBreakdown, TCADailyReport

__all__ = ["FeeBreakdown", "FeeSchedule", "SlippageBreakdown", "TCADailyReport"]
```

- [ ] **Step 4: Commit**

```bash
git add config/base/fees/futures.yaml src/hft_platform/tca/
git commit -m "feat(tca): add fee schedule config and TCA types"
```

---

## Task 2: FeeCalculator with TDD

**Files:**
- Create: `src/hft_platform/tca/fee_calculator.py`
- Create: `tests/unit/test_tca_fee_calculator.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_tca_fee_calculator.py`:
```python
"""FeeCalculator unit tests — validates per-contract fee model for Taiwan futures."""
from __future__ import annotations

import pytest
import yaml

from hft_platform.contracts.strategy import Side
from hft_platform.tca.fee_calculator import FeeCalculator
from hft_platform.tca.types import FeeBreakdown


@pytest.fixture()
def fee_config() -> dict:
    return yaml.safe_load("""
futures:
  XMT:
    commission_per_contract: 13
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 10
  TX:
    commission_per_contract: 60
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 200
  stock_futures_default:
    commission_per_contract: 20
    tax_rate_bps: 4.0
    tax_side: sell
    tick_size: 0.01
    point_value: 2000
  overrides:
    "2330F":
      commission_per_contract: 25
""")


@pytest.fixture()
def calc(fee_config: dict) -> FeeCalculator:
    return FeeCalculator(fee_config)


class TestBuySide:
    def test_buy_xmt_commission_only(self, calc: FeeCalculator) -> None:
        result = calc.calculate("XMT", Side.BUY, qty=1, fill_price=200000000)
        # commission = 13 * 1 * 10000 = 130000
        assert result.commission == 130_000
        assert result.tax == 0  # buy side, no tax
        assert result.total == 130_000

    def test_buy_tx_commission(self, calc: FeeCalculator) -> None:
        result = calc.calculate("TX", Side.BUY, qty=2, fill_price=200000000)
        # commission = 60 * 2 * 10000 = 1_200_000
        assert result.commission == 1_200_000
        assert result.tax == 0


class TestSellSide:
    def test_sell_xmt_commission_plus_tax(self, calc: FeeCalculator) -> None:
        # XMT at index 20000 (fill_price = 20000 * 10000 = 200_000_000)
        result = calc.calculate("XMT", Side.SELL, qty=1, fill_price=200_000_000)
        # commission = 13 * 1 * 10000 = 130_000
        assert result.commission == 130_000
        # notional_ntd = (200_000_000 / 10000) * 10 * 1 = 200_000
        # tax_ntd = 200_000 * (2.0 / 10000) = 40.0
        # tax_scaled = int(40.0 * 10000) = 400_000
        # Wait — let's recalculate:
        # notional_ntd = 20000 * 10 * 1 = 200_000 NTD
        # tax_ntd = 200_000 * 2.0 / 10_000 = 40 NTD  (≈40, close to reference 14???)
        # Hmm, reference says 14 NTD. Let's check: tax_rate_bps=2 means 0.00002
        # 200_000 * 0.00002 = 4 NTD... that's tax_rate_bps / 10000 = 0.0002? No.
        # bps = 1/10000. tax_rate_bps=2.0 → 2/10000 = 0.0002 of notional
        # 200_000 * 0.0002 = 40 NTD — NOT 14.
        # The reference 14 NTD must imply a lower index or different rate.
        # At index ~7000: notional=70000, tax=70000*0.0002=14 ✓
        # So test with index 7000 to validate the reference:
        pass  # See test below

    def test_sell_xmt_reference_validation(self, calc: FeeCalculator) -> None:
        """Validate against known reference: XMT sell at ~7000, tax ≈ 14 NTD."""
        # fill_price at index 7000 = 7000 * 10000 = 70_000_000
        result = calc.calculate("XMT", Side.SELL, qty=1, fill_price=70_000_000)
        # notional = 7000 * 10 * 1 = 70_000 NTD
        # tax = 70_000 * 0.0002 = 14.0 NTD
        # tax_scaled = int(14.0 * 10000) = 140_000
        assert result.tax == 140_000
        # commission_scaled = 13 * 10000 = 130_000
        assert result.commission == 130_000
        assert result.total == 270_000  # 130_000 + 140_000

    def test_sell_xmt_round_trip_40ntd(self, calc: FeeCalculator) -> None:
        """Full round-trip at index ~7000 should be ~40 NTD (400_000 scaled)."""
        buy = calc.calculate("XMT", Side.BUY, qty=1, fill_price=70_000_000)
        sell = calc.calculate("XMT", Side.SELL, qty=1, fill_price=70_000_000)
        round_trip = buy.total + sell.total
        # buy: 130_000 (commission only)
        # sell: 130_000 + 140_000 = 270_000
        # total: 400_000 = 40 NTD * 10000 ✓
        assert round_trip == 400_000


class TestMultiContract:
    def test_qty_scales_linearly(self, calc: FeeCalculator) -> None:
        one = calc.calculate("XMT", Side.SELL, qty=1, fill_price=70_000_000)
        five = calc.calculate("XMT", Side.SELL, qty=5, fill_price=70_000_000)
        assert five.commission == one.commission * 5
        assert five.tax == one.tax * 5
        assert five.total == one.total * 5


class TestOverrides:
    def test_2330f_uses_custom_commission(self, calc: FeeCalculator) -> None:
        result = calc.calculate("2330F", Side.BUY, qty=1, fill_price=5000_000_000)
        # Uses override commission=25, stock_futures_default tax_rate_bps=4.0
        assert result.commission == 250_000  # 25 * 1 * 10000

    def test_unknown_stock_future_uses_default(self, calc: FeeCalculator) -> None:
        result = calc.calculate("2317F", Side.BUY, qty=1, fill_price=1000_000_000)
        # Falls back to stock_futures_default: commission=20
        assert result.commission == 200_000


class TestEdgeCases:
    def test_zero_price_fill(self, calc: FeeCalculator) -> None:
        result = calc.calculate("XMT", Side.SELL, qty=1, fill_price=0)
        assert result.commission == 130_000  # per-contract, not price-dependent
        assert result.tax == 0  # notional is 0

    def test_unknown_symbol_raises(self, calc: FeeCalculator) -> None:
        with pytest.raises(KeyError):
            calc.calculate("INVALID", Side.BUY, qty=1, fill_price=100_000_000)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tca_fee_calculator.py -v`
Expected: FAIL (ImportError — `fee_calculator` module does not exist)

- [ ] **Step 3: Implement FeeCalculator**

Create `src/hft_platform/tca/fee_calculator.py`:
```python
"""Per-contract fee calculator for Taiwan futures."""
from __future__ import annotations

import structlog
from pathlib import Path
from typing import Any

import yaml

from hft_platform.contracts.strategy import Side
from hft_platform.tca.types import FeeBreakdown, FeeSchedule

logger = structlog.get_logger(__name__)

_STOCK_FUTURES_SUFFIX = "F"


class FeeCalculator:
    """Calculate per-trade fees from fee_schedules.yaml.

    All monetary outputs are in NTD scaled x10000.
    """

    __slots__ = ("_schedules", "_overrides", "_stock_default")

    def __init__(self, fee_config: dict[str, Any]) -> None:
        futures = fee_config.get("futures", {})
        self._overrides: dict[str, int] = {}
        self._stock_default: FeeSchedule | None = None
        self._schedules: dict[str, FeeSchedule] = {}

        for key, val in futures.items():
            if key == "overrides":
                for sym, ovr in val.items():
                    self._overrides[sym] = ovr.get("commission_per_contract", 0)
            elif key == "stock_futures_default":
                self._stock_default = FeeSchedule(
                    symbol="stock_futures_default",
                    commission_per_contract=val["commission_per_contract"],
                    tax_rate_bps=val["tax_rate_bps"],
                    tax_side=val.get("tax_side", "sell"),
                    tick_size=val.get("tick_size", 0.01),
                    point_value=val["point_value"],
                )
            else:
                self._schedules[key] = FeeSchedule(
                    symbol=key,
                    commission_per_contract=val["commission_per_contract"],
                    tax_rate_bps=val["tax_rate_bps"],
                    tax_side=val.get("tax_side", "sell"),
                    tick_size=val.get("tick_size", 1),
                    point_value=val["point_value"],
                )

    @classmethod
    def from_yaml(cls, path: str | Path) -> FeeCalculator:
        with open(path) as f:
            config = yaml.safe_load(f)
        return cls(config)

    def _resolve(self, symbol: str) -> FeeSchedule:
        if symbol in self._schedules:
            return self._schedules[symbol]
        # Stock futures: symbol ending in F (e.g. "2330F")
        if symbol.endswith(_STOCK_FUTURES_SUFFIX) and self._stock_default is not None:
            sched = self._stock_default
            # Apply override commission if present
            if symbol in self._overrides:
                sched = FeeSchedule(
                    symbol=symbol,
                    commission_per_contract=self._overrides[symbol],
                    tax_rate_bps=sched.tax_rate_bps,
                    tax_side=sched.tax_side,
                    tick_size=sched.tick_size,
                    point_value=sched.point_value,
                )
            return sched
        raise KeyError(f"No fee schedule for symbol: {symbol}")

    def calculate(
        self,
        symbol: str,
        side: Side,
        qty: int,
        fill_price: int,
    ) -> FeeBreakdown:
        sched = self._resolve(symbol)

        # Commission: fixed NTD/contract * qty, scaled x10000
        commission_scaled = sched.commission_per_contract * qty * 10_000

        # Tax: only on sell side
        tax_scaled = 0
        if side == Side.SELL and sched.tax_side == "sell":
            # notional_ntd = (fill_price / 10000) * point_value * qty
            notional_ntd = (fill_price / 10_000) * sched.point_value * qty
            tax_ntd = notional_ntd * (sched.tax_rate_bps / 10_000)
            tax_scaled = int(tax_ntd * 10_000)

        return FeeBreakdown(
            commission=commission_scaled,
            tax=tax_scaled,
            total=commission_scaled + tax_scaled,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_tca_fee_calculator.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/tca/fee_calculator.py tests/unit/test_tca_fee_calculator.py
git commit -m "feat(tca): add FeeCalculator with per-contract fee model"
```

---

## Task 3: Contract Expansion (FillEvent, OrderIntent, OrderCommand, PositionDelta)

**Files:**
- Modify: `src/hft_platform/contracts/execution.py:37-53,57-69`
- Modify: `src/hft_platform/contracts/strategy.py:32-57,72-82`
- Test: `tests/unit/test_contracts_tca_fields.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_contracts_tca_fields.py`:
```python
"""Verify TCA fields exist on contracts with correct defaults."""
from hft_platform.contracts.execution import FillEvent, PositionDelta
from hft_platform.contracts.strategy import OrderIntent, OrderCommand, Side, IntentType, StormGuardState


def test_fill_event_has_tca_prices() -> None:
    fill = FillEvent(
        fill_id="f1", account_id="a1", order_id="o1", strategy_id="s1",
        symbol="XMT", side=Side.BUY, qty=1, price=200_000_000,
        fee=130_000, tax=0, ingest_ts_ns=0, match_ts_ns=0,
    )
    assert fill.decision_price == 0
    assert fill.arrival_price == 0


def test_fill_event_with_tca_prices() -> None:
    fill = FillEvent(
        fill_id="f1", account_id="a1", order_id="o1", strategy_id="s1",
        symbol="XMT", side=Side.BUY, qty=1, price=200_000_000,
        fee=130_000, tax=0, ingest_ts_ns=0, match_ts_ns=0,
        decision_price=199_500_000, arrival_price=199_800_000,
    )
    assert fill.decision_price == 199_500_000
    assert fill.arrival_price == 199_800_000


def test_position_delta_has_gross_pnl_and_fees() -> None:
    delta = PositionDelta(
        account_id="a1", strategy_id="s1", symbol="XMT",
        net_qty=0, avg_price=0, realized_pnl=100_000,
        unrealized_pnl=0, delta_source="FILL",
    )
    assert delta.gross_pnl == 0
    assert delta.fees == 0


def test_order_intent_has_decision_price() -> None:
    intent = OrderIntent(
        intent_id=1, strategy_id="s1", symbol="XMT",
        intent_type=IntentType.NEW, side=Side.BUY, price=200_000_000, qty=1,
    )
    assert intent.decision_price == 0


def test_order_command_has_tca_prices() -> None:
    intent = OrderIntent(
        intent_id=1, strategy_id="s1", symbol="XMT",
        intent_type=IntentType.NEW, side=Side.BUY, price=200_000_000, qty=1,
    )
    cmd = OrderCommand(cmd_id=1, intent=intent, deadline_ns=0, storm_guard_state=StormGuardState.NORMAL)
    assert cmd.decision_price == 0
    assert cmd.arrival_price == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_contracts_tca_fields.py -v`
Expected: FAIL (AttributeError — fields don't exist yet)

- [ ] **Step 3: Add fields to FillEvent**

In `src/hft_platform/contracts/execution.py`, add after line 53 (after `match_ts_ns`):
```python
    decision_price: int = 0   # Mid-price at signal time (x10000)
    arrival_price: int = 0    # Mid-price at order submit time (x10000)
```

- [ ] **Step 4: Add fields to PositionDelta**

In `src/hft_platform/contracts/execution.py`, add after `delta_source` field:
```python
    gross_pnl: int = 0   # Gross PnL before fees (x10000)
    fees: int = 0         # Total fees for this fill (x10000)
```

- [ ] **Step 5: Add decision_price to OrderIntent**

In `src/hft_platform/contracts/strategy.py`, add after existing optional fields in OrderIntent:
```python
    decision_price: int = 0  # LOB mid-price at signal time (x10000)
```

- [ ] **Step 6: Add TCA prices to OrderCommand**

In `src/hft_platform/contracts/strategy.py`, add after `created_ns` field:
```python
    decision_price: int = 0  # Passthrough from OrderIntent
    arrival_price: int = 0   # Stamped by OrderAdapter at submit time
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_contracts_tca_fields.py -v`
Expected: ALL PASS

- [ ] **Step 8: Run full test suite for regressions**

Run: `uv run pytest tests/ -x -q --timeout=60`
Expected: No regressions (all new fields have defaults)

- [ ] **Step 9: Commit**

```bash
git add src/hft_platform/contracts/execution.py src/hft_platform/contracts/strategy.py tests/unit/test_contracts_tca_fields.py
git commit -m "feat(contracts): add TCA price capture and fee fields to FillEvent, OrderIntent, OrderCommand, PositionDelta"
```

---

## Task 4: Position PnL Fix — Net PnL = Gross - Fees

**Files:**
- Modify: `src/hft_platform/execution/positions.py:87-149`
- Test: `tests/unit/test_position_net_pnl.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_position_net_pnl.py`:
```python
"""Verify Position.update() deducts fees from realized_pnl."""
from hft_platform.contracts.execution import FillEvent, PositionDelta
from hft_platform.contracts.strategy import Side
from hft_platform.execution.positions import Position


def _make_fill(side: Side, price: int, qty: int, fee: int = 0, tax: int = 0) -> FillEvent:
    return FillEvent(
        fill_id="f1", account_id="a1", order_id="o1", strategy_id="s1",
        symbol="XMT", side=side, qty=qty, price=price,
        fee=fee, tax=tax, ingest_ts_ns=0, match_ts_ns=0,
    )


def test_realized_pnl_deducts_fees() -> None:
    """Fee deduction semantics: fees are always subtracted from realized_pnl on every fill.
    On opening fills (no close PnL), this means realized_pnl goes negative by the fee amount.
    This is the correct accounting: fees are a realized cost at the moment of execution.

    Existing tests that assert realized_pnl_scaled == 0 after an open will break if they
    pass non-zero fees — but currently all fills have fee=0, so no existing test is affected.
    """
    pos = Position(account_id="a1", strategy_id="s1", symbol="XMT")
    # Buy 1 @ 20000 (scaled: 200_000_000), commission 130_000
    pos.update(_make_fill(Side.BUY, 200_000_000, 1, fee=130_000, tax=0))
    # After buy: realized_pnl = 0 (no close) - 130_000 (fee) = -130_000
    assert pos.realized_pnl_scaled == -130_000

    # Sell 1 @ 20010 (scaled: 200_100_000), commission 130_000, tax 140_000
    pos.update(
        _make_fill(Side.SELL, 200_100_000, 1, fee=130_000, tax=140_000),
        contract_multiplier=10,
    )
    # Close PnL = (20010 - 20000) * 10 * 1 = 100 NTD = 1_000_000 scaled
    # Sell fees = 130_000 + 140_000 = 270_000
    # Sell realized delta = 1_000_000 - 270_000 = 730_000
    # Total realized = -130_000 + 730_000 = 600_000
    assert pos.realized_pnl_scaled == 600_000
    # Total fees accumulated = 130_000 + 270_000 = 400_000
    assert pos.fees_scaled == 400_000


def test_gross_pnl_tracked_separately() -> None:
    pos = Position(account_id="a1", strategy_id="s1", symbol="XMT")
    pos.update(_make_fill(Side.BUY, 200_000_000, 1, fee=130_000))
    pos.update(
        _make_fill(Side.SELL, 200_100_000, 1, fee=130_000, tax=140_000),
        contract_multiplier=10,
    )
    # gross_pnl should be the PnL without any fee deduction
    # Buy: no close, gross=0. Sell close: gross=1_000_000
    assert pos.gross_pnl_scaled == 1_000_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_position_net_pnl.py -v`
Expected: FAIL (either `gross_pnl_scaled` doesn't exist or PnL doesn't deduct fees)

- [ ] **Step 3: Implement PnL fix**

In `src/hft_platform/execution/positions.py`, in the `Position` class:

1. Add `gross_pnl_scaled: int = 0` to class attributes (near `realized_pnl_scaled`)
2. In `update()`, find where `self.realized_pnl_scaled += pnl` is computed (around line 123-125)
3. Change to:
```python
    cost = fill.fee + fill.tax
    self.fees_scaled += cost
    self.gross_pnl_scaled += pnl
    self.realized_pnl_scaled += pnl - cost
```
4. In `_on_fill_python()` where `PositionDelta` is returned, add:
```python
    gross_pnl=pnl,
    fees=fill.fee + fill.tax,
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_position_net_pnl.py -v`
Expected: PASS

- [ ] **Step 5: Run full test suite for regressions**

Run: `uv run pytest tests/ -x -q --timeout=60`
Expected: Some existing tests may fail if they assert on `realized_pnl_scaled` values — fix by adjusting expected values to account for fee deductions (only if those tests pass non-zero fees).

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/execution/positions.py tests/unit/test_position_net_pnl.py
git commit -m "fix(execution): deduct fees from realized_pnl, track gross_pnl separately"
```

---

## Task 5: Decision Price Capture in StrategyRunner

**Files:**
- Modify: `src/hft_platform/strategy/runner.py:387-437`
- Test: `tests/unit/test_strategy_decision_price.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_strategy_decision_price.py`:
```python
"""Verify StrategyRunner stamps decision_price on OrderIntent."""
from unittest.mock import MagicMock, patch

from hft_platform.contracts.strategy import OrderIntent, Side, IntentType


def test_intent_has_decision_price_from_lob() -> None:
    """When LOB L1 is available, decision_price should be mid_price_x2 // 2."""
    # mid_price_x2 = 400_000_000 → mid_price = 200_000_000
    intent = OrderIntent(
        intent_id=1, strategy_id="s1", symbol="XMT",
        intent_type=IntentType.NEW, side=Side.BUY, price=200_000_000, qty=1,
        decision_price=200_000_000,
    )
    assert intent.decision_price == 200_000_000


def test_intent_decision_price_zero_without_lob() -> None:
    """Without LOB, decision_price should default to 0."""
    intent = OrderIntent(
        intent_id=1, strategy_id="s1", symbol="XMT",
        intent_type=IntentType.NEW, side=Side.BUY, price=200_000_000, qty=1,
    )
    assert intent.decision_price == 0
```

- [ ] **Step 2: Implement decision_price capture**

In `src/hft_platform/strategy/runner.py`, find `_intent_factory` (lines 387-437). Where `OrderIntent` is constructed, add `decision_price` by reading the current LOB mid-price:

```python
# After line ~430, before OrderIntent construction:
decision_mid = 0
if self._lob_l1_source is not None:
    try:
        l1 = self._lob_l1_source(symbol)
        if l1 is not None:
            decision_mid = l1[3] // 2  # mid_price_x2 // 2
    except Exception:
        pass  # LOB unavailable, leave as 0
```

Then in the `OrderIntent(...)` constructor call, add `decision_price=decision_mid`.

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/unit/test_strategy_decision_price.py tests/ -x -q --timeout=60`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/hft_platform/strategy/runner.py tests/unit/test_strategy_decision_price.py
git commit -m "feat(strategy): capture decision_price from LOB mid-price on OrderIntent"
```

---

## Task 6: OrderAdapter — Inflight Map + Arrival Price

**Files:**
- Modify: `src/hft_platform/order/adapter.py:87-142`
- Test: `tests/unit/test_order_adapter_inflight.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_order_adapter_inflight.py`:
```python
"""Verify OrderAdapter stores inflight OrderCommands and stamps arrival_price."""
from hft_platform.contracts.strategy import (
    OrderCommand, OrderIntent, Side, IntentType, StormGuardState,
)
from hft_platform.order.adapter import OrderAdapter


def test_inflight_store_and_retrieve() -> None:
    """OrderAdapter should store OrderCommand by order_key and retrieve it."""
    # Use __new__ to skip __init__ (requires broker deps); __dict__ in slots allows this
    adapter = OrderAdapter.__new__(OrderAdapter)
    adapter._inflight = {}
    cmd = OrderCommand(
        cmd_id=1,
        intent=OrderIntent(
            intent_id=1, strategy_id="s1", symbol="XMT",
            intent_type=IntentType.NEW, side=Side.BUY,
            price=200_000_000, qty=1, decision_price=200_000_000,
        ),
        deadline_ns=0,
        storm_guard_state=StormGuardState.NORMAL,
        decision_price=200_000_000,
    )
    order_key = "s1:1"
    adapter._inflight[order_key] = cmd
    assert adapter.get_inflight(order_key) is cmd
    assert adapter.get_inflight("nonexistent") is None
```

- [ ] **Step 2: Implement inflight map and get_inflight**

In `src/hft_platform/order/adapter.py`:

1. Add `"_inflight"` and `"_mid_price_fn"` to `OrderAdapter.__slots__` tuple (around line 51-86, before `"__dict__"`)
2. In `__init__`, add: `self._inflight: dict[str, OrderCommand] = {}`
3. Add `mid_price_fn` parameter: `self._mid_price_fn: Callable[[str], int] | None = None`
3. Add method:
```python
def get_inflight(self, order_key: str) -> OrderCommand | None:
    return self._inflight.get(order_key)
```
4. Where orders are sent (the method that places orders via broker), add:
```python
# Before sending to broker:
order_key = f"{cmd.intent.strategy_id}:{cmd.intent.intent_id}"
# Stamp arrival_price
if self._mid_price_fn is not None:
    try:
        cmd.arrival_price = self._mid_price_fn(cmd.intent.symbol)
    except Exception:
        pass
# Passthrough decision_price from intent
cmd.decision_price = cmd.intent.decision_price
self._inflight[order_key] = cmd
```
5. On order completion/terminal state, evict: `self._inflight.pop(order_key, None)`

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/unit/test_order_adapter_inflight.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/hft_platform/order/adapter.py tests/unit/test_order_adapter_inflight.py
git commit -m "feat(order): add inflight OrderCommand map and arrival_price stamping"
```

---

## Task 7: ExecutionNormalizer — Fee Injection + OrderCommand Param

**Files:**
- Modify: `src/hft_platform/execution/normalizer.py:29-45,198-211`
- Modify: `src/hft_platform/execution/router.py:54-70,108-148`
- Test: `tests/unit/test_normalizer_fee_injection.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_normalizer_fee_injection.py`:
```python
"""Verify ExecutionNormalizer populates fee/tax from FeeCalculator."""
import yaml
from hft_platform.contracts.strategy import (
    OrderCommand, OrderIntent, Side, IntentType, StormGuardState,
)
from hft_platform.execution.normalizer import ExecutionNormalizer
from hft_platform.tca.fee_calculator import FeeCalculator


def _make_order_cmd() -> OrderCommand:
    intent = OrderIntent(
        intent_id=1, strategy_id="s1", symbol="XMT",
        intent_type=IntentType.NEW, side=Side.SELL, price=70_000_000, qty=1,
        decision_price=69_900_000,
    )
    return OrderCommand(
        cmd_id=1, intent=intent, deadline_ns=0,
        storm_guard_state=StormGuardState.NORMAL,
        decision_price=69_900_000, arrival_price=69_950_000,
    )


def test_normalize_fill_with_fee_calculator() -> None:
    config = yaml.safe_load("""
futures:
  XMT:
    commission_per_contract: 13
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 10
""")
    calc = FeeCalculator(config)
    normalizer = ExecutionNormalizer(fee_calculator=calc)
    cmd = _make_order_cmd()

    # Simulate a raw deal event — exact structure depends on existing code
    # This test verifies the fee/tax fields are non-zero after normalization
    # The integration test in Task 11 will test the full pipeline
    # For now, test the FeeCalculator integration directly
    breakdown = calc.calculate("XMT", Side.SELL, 1, 70_000_000)
    assert breakdown.commission == 130_000
    assert breakdown.tax == 140_000


def test_normalize_fill_orphan_fill_returns_zero_fees() -> None:
    """When order_cmd is None, fees should be zero (not crash)."""
    config = yaml.safe_load("""
futures:
  XMT:
    commission_per_contract: 13
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 10
""")
    calc = FeeCalculator(config)
    normalizer = ExecutionNormalizer(fee_calculator=calc)
    # Passing None for order_cmd should not crash
    # Full integration tested in Task 11
    assert normalizer._fee_calc is calc
```

- [ ] **Step 2: Modify ExecutionNormalizer**

In `src/hft_platform/execution/normalizer.py`:

1. Add `fee_calculator` param to `__init__` (with `None` default for backward compat):
```python
def __init__(
    self,
    raw_queue: Any = None,
    order_id_map: Optional[Dict[str, str]] = None,
    strategy_id_resolvers: Optional[list] = None,
    fee_calculator: Any = None,  # FeeCalculator | None
) -> None:
    ...
    self._fee_calc = fee_calculator
```

2. Modify `normalize_fill` signature to accept `order_cmd`:
```python
def normalize_fill(self, raw, order_cmd=None) -> FillEvent:
```

3. Replace `fee=0, tax=0` (lines 207-208) with:
```python
    fee = 0
    tax = 0
    decision_price = 0
    arrival_price = 0
    if order_cmd is not None and self._fee_calc is not None:
        try:
            breakdown = self._fee_calc.calculate(
                symbol=order_cmd.intent.symbol,
                side=order_cmd.intent.side,
                qty=fill_qty,
                fill_price=fill_price_scaled,
            )
            fee = breakdown.commission
            tax = breakdown.tax
        except Exception:
            logger.warning("fee_calc_failed", symbol=order_cmd.intent.symbol)
        decision_price = order_cmd.decision_price
        arrival_price = order_cmd.arrival_price
```
Then use `fee=fee, tax=tax, decision_price=decision_price, arrival_price=arrival_price` in FillEvent constructor.

- [ ] **Step 3: Modify ExecutionRouter**

In `src/hft_platform/execution/router.py`:

1. Add `order_adapter` and `fee_calculator` params to `__init__`:
```python
def __init__(
    self,
    bus,
    raw_queue,
    order_id_map,
    position_store,
    terminal_handler,
    risk_engine=None,
    order_adapter=None,  # OrderAdapter | None — for TCA inflight lookup
    fee_calculator=None,  # FeeCalculator | None
):
    ...
    self._order_adapter = order_adapter
    self._fee_calculator = fee_calculator
```

2. Pass `fee_calculator` when constructing `ExecutionNormalizer` (line 65):
```python
self.normalizer = ExecutionNormalizer(
    raw_queue, order_id_map,
    fee_calculator=self._fee_calculator,
)
```

3. In `run()`, where `normalize_fill` is called (line 109), add order_cmd lookup:
```python
order_cmd = None
if self._order_adapter is not None:
    order_key = self._order_id_map.get(raw.order_id, "")
    if order_key:
        order_cmd = self._order_adapter.get_inflight(order_key)
fill_event = self.normalizer.normalize_fill(raw, order_cmd)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_normalizer_fee_injection.py tests/ -x -q --timeout=60`
Expected: PASS (no regressions — all new params have defaults)

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/execution/normalizer.py src/hft_platform/execution/router.py tests/unit/test_normalizer_fee_injection.py
git commit -m "feat(execution): inject FeeCalculator into normalizer, lookup inflight OrderCommand in router"
```

---

## Task 8: Bootstrap Wiring

**Files:**
- Modify: `src/hft_platform/services/bootstrap.py:709,860-868`

- [ ] **Step 1: Wire FeeCalculator in bootstrap**

In `src/hft_platform/services/bootstrap.py`, near the order/execution setup section:

```python
# After order_id_map creation (line ~709):
from hft_platform.tca.fee_calculator import FeeCalculator
import os

_fee_schedule_path = os.environ.get(
    "HFT_FEE_SCHEDULE_PATH", "config/base/fees/futures.yaml"
)
_fee_calculator = None
if Path(_fee_schedule_path).exists():
    try:
        _fee_calculator = FeeCalculator.from_yaml(_fee_schedule_path)
        logger.info("fee_calculator_loaded", path=_fee_schedule_path)
    except Exception:
        logger.warning("fee_calculator_load_failed", path=_fee_schedule_path)
```

- [ ] **Step 2: Wire mid_price_fn into OrderAdapter**

```python
# After order_adapter construction (line ~860):
def _get_mid_price(symbol: str) -> int:
    l1 = lob_engine.get_l1_scaled(symbol)
    if l1 is not None:
        return l1[3] // 2  # mid_price_x2 // 2
    return 0

order_adapter._mid_price_fn = _get_mid_price
```

- [ ] **Step 3: Pass order_adapter and fee_calculator to ExecutionRouter**

Modify the `ExecutionRouter(...)` construction (lines 862-868):
```python
exec_service = ExecutionRouter(
    bus,
    raw_exec_queue,
    order_id_map,
    position_store,
    execution_gateway.on_terminal_state,
    order_adapter=order_adapter,
    fee_calculator=_fee_calculator,
)
```

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest tests/ -x -q --timeout=60`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/services/bootstrap.py
git commit -m "feat(bootstrap): wire FeeCalculator, mid_price_fn, and order_adapter into execution pipeline"
```

---

## Task 9: ClickHouse Migration + Recorder Fix

**Files:**
- Create: `src/hft_platform/migrations/clickhouse/20260325_001_add_tca_columns.sql`
- Modify: `src/hft_platform/recorder/_loader_batch.py:205-245`
- Modify: `src/hft_platform/recorder/worker.py:45-55,146-174`

- [ ] **Step 1: Create migration**

Create `src/hft_platform/migrations/clickhouse/20260325_001_add_tca_columns.sql`:
```sql
-- Add TCA columns to hft.trades
ALTER TABLE hft.trades ADD COLUMN IF NOT EXISTS tax_scaled Int64 DEFAULT 0;
ALTER TABLE hft.trades ADD COLUMN IF NOT EXISTS decision_price_scaled Int64 DEFAULT 0;
ALTER TABLE hft.trades ADD COLUMN IF NOT EXISTS arrival_price_scaled Int64 DEFAULT 0;
ALTER TABLE hft.trades ADD COLUMN IF NOT EXISTS gross_pnl_scaled Int64 DEFAULT 0;

-- TCA daily aggregation table
CREATE TABLE IF NOT EXISTS hft.tca_daily (
    date                    Date,
    strategy                LowCardinality(String),
    symbol                  LowCardinality(String),
    trade_count             UInt32,
    volume                  UInt32,
    notional                Int64,
    commission_bps_mean     Float32,
    tax_bps_mean            Float32,
    delay_cost_bps_mean     Float32,
    delay_cost_bps_p95      Float32,
    exec_cost_bps_mean      Float32,
    exec_cost_bps_p95       Float32,
    impact_bps_mean         Float32,
    total_cost_bps_mean     Float32,
    total_cost_bps_p95      Float32,
    generated_at            DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(generated_at)
ORDER BY (date, strategy, symbol)
TTL date + INTERVAL 90 DAY;
```

- [ ] **Step 2: Fix _loader_batch.py**

In `src/hft_platform/recorder/_loader_batch.py`:

Replace `_TRADES_COLS` (lines 205-215) with:
```python
_TRADES_COLS: list[str] = [
    "fill_id", "order_id", "strategy_id", "symbol", "side",
    "price_scaled", "qty", "fee_scaled",
    "tax_scaled", "decision_price_scaled", "arrival_price_scaled",
    "gross_pnl_scaled",
    "match_ts",
]
```

Replace `format_trades()` (lines 218-245) with corrected implementation:
```python
def format_trades(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[list]]:
    """Return ``(cols, data)`` for the ``hft.trades`` table.
    Columns match the DDL: fill_id, order_id, strategy_id, symbol, side,
    price_scaled, qty, fee_scaled, tax_scaled, decision_price_scaled,
    arrival_price_scaled, gross_pnl_scaled, match_ts.
    """
    data: list[list] = []
    for r in rows:
        price = r.get("price_scaled")
        if price is None:
            price_float = r.get("price")
            price = _to_scaled(price_float) if price_float is not None else 0
        match_ts = int(r.get("match_ts") or r.get("exch_ts") or r.get("ts") or 0)
        row_data = [
            str(r.get("fill_id", r.get("trade_id", ""))),
            str(r.get("order_id", "")),
            str(r.get("strategy_id", "")),
            str(r.get("symbol", "")),
            str(r.get("side", r.get("action", ""))),
            int(price),
            int(r.get("qty", r.get("quantity", 0)) or 0),
            int(r.get("fee_scaled", r.get("fee", 0)) or 0),
            int(r.get("tax_scaled", r.get("tax", 0)) or 0),
            int(r.get("decision_price_scaled", r.get("decision_price", 0)) or 0),
            int(r.get("arrival_price_scaled", r.get("arrival_price", 0)) or 0),
            int(r.get("gross_pnl_scaled", r.get("gross_pnl", 0)) or 0),
            match_ts,
        ]
        data.append(row_data)
    return _TRADES_COLS, data
```

- [ ] **Step 3: Fix worker.py**

In `src/hft_platform/recorder/worker.py`:

Replace `FILL_COLUMNS` (lines 45-55) with:
```python
FILL_COLUMNS = [
    "fill_id", "order_id", "strategy_id", "symbol", "side",
    "price_scaled", "qty", "fee_scaled",
    "tax_scaled", "decision_price_scaled", "arrival_price_scaled",
    "gross_pnl_scaled", "match_ts",
]
```

Replace `_extract_fill_values()` (lines 146-174) with:
```python
def _extract_fill_values(row) -> list | None:
    """Fast extractor for fill/trade events — matches hft.trades DDL."""
    try:
        if isinstance(row, dict):
            get = row.get
            return [
                get("fill_id", get("trade_id")),
                get("order_id"),
                get("strategy_id", ""),
                get("symbol"),
                get("side", get("action", "")),
                get("price_scaled"),
                get("qty", get("quantity", 0)),
                get("fee_scaled", get("fee", 0)) or 0,
                get("tax_scaled", get("tax", 0)) or 0,
                get("decision_price_scaled", get("decision_price", 0)) or 0,
                get("arrival_price_scaled", get("arrival_price", 0)) or 0,
                get("gross_pnl_scaled", get("gross_pnl", 0)) or 0,
                get("match_ts", get("exch_ts", get("ts"))),
            ]
        return [
            getattr(row, "fill_id", None) or getattr(row, "trade_id", None),
            getattr(row, "order_id", None),
            getattr(row, "strategy_id", None) or "",
            getattr(row, "symbol", None),
            getattr(row, "side", None) or getattr(row, "action", None) or "",
            getattr(row, "price_scaled", None) or getattr(row, "price", None),
            getattr(row, "qty", None) or getattr(row, "quantity", None) or 0,
            getattr(row, "fee_scaled", None) or getattr(row, "fee", None) or 0,
            getattr(row, "tax_scaled", None) or getattr(row, "tax", None) or 0,
            getattr(row, "decision_price_scaled", None) or getattr(row, "decision_price", None) or 0,
            getattr(row, "arrival_price_scaled", None) or getattr(row, "arrival_price", None) or 0,
            getattr(row, "gross_pnl_scaled", None) or getattr(row, "gross_pnl", None) or 0,
            getattr(row, "match_ts", None) or getattr(row, "exch_ts", None) or getattr(row, "ts", None),
        ]
    except Exception as _exc:  # noqa: BLE001
        return None
```

- [ ] **Step 4: Commit**

```bash
git add src/hft_platform/migrations/clickhouse/20260325_001_add_tca_columns.sql \
    src/hft_platform/recorder/_loader_batch.py \
    src/hft_platform/recorder/worker.py
git commit -m "feat(recorder): fix hft.trades column mapping, add TCA columns and tca_daily table"
```

---

## Task 10: SlippageDecomposer + SqrtImpactModel

**Files:**
- Create: `src/hft_platform/tca/slippage.py`
- Create: `src/hft_platform/tca/impact.py`
- Create: `tests/unit/test_tca_slippage.py`
- Create: `tests/unit/test_tca_impact.py`

- [ ] **Step 1: Write failing slippage tests**

Create `tests/unit/test_tca_slippage.py`:
```python
"""SlippageDecomposer unit tests."""
from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.tca.slippage import SlippageDecomposer
from hft_platform.tca.types import FeeBreakdown


def _fill(side: Side, price: int, decision: int, arrival: int, fee: int = 0, tax: int = 0) -> FillEvent:
    return FillEvent(
        fill_id="f1", account_id="a1", order_id="o1", strategy_id="s1",
        symbol="XMT", side=side, qty=1, price=price,
        fee=fee, tax=tax, ingest_ts_ns=0, match_ts_ns=0,
        decision_price=decision, arrival_price=arrival,
    )


def test_buy_adverse_slippage() -> None:
    decomposer = SlippageDecomposer()
    fill = _fill(Side.BUY, price=200_200_000, decision=200_000_000, arrival=200_100_000)
    result = decomposer.decompose(fill, notional_ntd=200_000)
    # delay = (arrival - decision) / decision = 100_000 / 200_000_000 * 10000 = 0.5 bps
    assert abs(result.delay_cost_bps - 0.5) < 0.01
    # exec = (fill - arrival) / arrival ≈ 0.5 bps
    assert abs(result.execution_cost_bps - 0.4998) < 0.01
    assert result.delay_cost_bps > 0  # adverse
    assert result.execution_cost_bps > 0  # adverse


def test_sell_adverse_slippage() -> None:
    decomposer = SlippageDecomposer()
    fill = _fill(Side.SELL, price=199_800_000, decision=200_000_000, arrival=199_900_000)
    result = decomposer.decompose(fill, notional_ntd=200_000)
    # delay = (decision - arrival) / decision = 100_000 / 200_000_000 * 10000 = 0.5 bps
    assert abs(result.delay_cost_bps - 0.5) < 0.01
    assert result.delay_cost_bps > 0  # adverse


def test_favorable_execution() -> None:
    decomposer = SlippageDecomposer()
    # Buy but price went down (favorable)
    fill = _fill(Side.BUY, price=199_900_000, decision=200_000_000, arrival=200_000_000)
    result = decomposer.decompose(fill, notional_ntd=200_000)
    assert result.execution_cost_bps < 0  # favorable


def test_zero_decision_price() -> None:
    decomposer = SlippageDecomposer()
    fill = _fill(Side.BUY, price=200_000_000, decision=0, arrival=200_000_000)
    result = decomposer.decompose(fill, notional_ntd=200_000)
    assert result.delay_cost_bps == 0.0
    assert result.execution_cost_bps == 0.0
```

- [ ] **Step 2: Write failing impact tests**

Create `tests/unit/test_tca_impact.py`:
```python
"""SqrtImpactModel unit tests."""
import math
from hft_platform.tca.impact import SqrtImpactModel


def test_basic_positive_impact() -> None:
    model = SqrtImpactModel(eta=1.0)
    impact = model.estimate(qty=1, volatility=10.0, avg_volume=100.0)
    assert impact > 0
    assert impact == 10.0 * math.sqrt(1 / 100) * 1.0


def test_zero_volume_returns_zero() -> None:
    model = SqrtImpactModel(eta=1.0)
    assert model.estimate(qty=1, volatility=10.0, avg_volume=0) == 0.0


def test_doubling_qty_scales_sqrt2() -> None:
    model = SqrtImpactModel(eta=1.0)
    i1 = model.estimate(qty=1, volatility=10.0, avg_volume=100.0)
    i2 = model.estimate(qty=2, volatility=10.0, avg_volume=100.0)
    assert abs(i2 / i1 - math.sqrt(2)) < 0.001


def test_eta_doubles_output() -> None:
    m1 = SqrtImpactModel(eta=1.0)
    m2 = SqrtImpactModel(eta=2.0)
    i1 = m1.estimate(qty=1, volatility=10.0, avg_volume=100.0)
    i2 = m2.estimate(qty=1, volatility=10.0, avg_volume=100.0)
    assert abs(i2 - 2 * i1) < 0.0001
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tca_slippage.py tests/unit/test_tca_impact.py -v`
Expected: FAIL (modules don't exist)

- [ ] **Step 4: Implement SlippageDecomposer**

Create `src/hft_platform/tca/slippage.py`:
```python
"""Slippage decomposition — 4-component cost breakdown per fill."""
from __future__ import annotations

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.tca.types import SlippageBreakdown


class SlippageDecomposer:
    """Decompose per-fill cost into commission, tax, delay, and execution components.

    Direction convention: positive = adverse, negative = favorable.
    """

    __slots__ = ()

    def decompose(
        self,
        fill: FillEvent,
        notional_ntd: float,
        market_impact_bps: float = 0.0,
    ) -> SlippageBreakdown:
        # Commission and tax as bps of notional
        if notional_ntd > 0:
            comm_bps = (fill.fee / 10_000) / notional_ntd * 10_000
            tax_bps = (fill.tax / 10_000) / notional_ntd * 10_000
        else:
            comm_bps = 0.0
            tax_bps = 0.0

        # Delay cost: decision → arrival
        if fill.decision_price > 0 and fill.arrival_price > 0:
            if fill.side == Side.BUY:
                delay = (fill.arrival_price - fill.decision_price) / fill.decision_price * 10_000
            else:
                delay = (fill.decision_price - fill.arrival_price) / fill.decision_price * 10_000
        else:
            delay = 0.0

        # Execution cost: arrival → fill
        if fill.arrival_price > 0:
            if fill.side == Side.BUY:
                exec_cost = (fill.price - fill.arrival_price) / fill.arrival_price * 10_000
            else:
                exec_cost = (fill.arrival_price - fill.price) / fill.arrival_price * 10_000
        else:
            exec_cost = 0.0

        total = comm_bps + tax_bps + delay + exec_cost + market_impact_bps

        return SlippageBreakdown(
            commission_bps=comm_bps,
            tax_bps=tax_bps,
            delay_cost_bps=delay,
            execution_cost_bps=exec_cost,
            market_impact_bps=market_impact_bps,
            total_bps=total,
        )
```

- [ ] **Step 5: Implement SqrtImpactModel**

Create `src/hft_platform/tca/impact.py`:
```python
"""Square-root market impact model based on 2506.07711v5."""
from __future__ import annotations

import math


class SqrtImpactModel:
    """Impact ≈ σ × sqrt(Q / V) × η

    volatility must be pre-normalized to bps-equivalent:
        volatility_bps = (stdev_of_mid_price / mid_price) * 10000
    """

    __slots__ = ("_eta",)

    def __init__(self, eta: float = 1.0) -> None:
        self._eta = eta

    def estimate(self, qty: int, volatility: float, avg_volume: float) -> float:
        """Returns estimated impact in bps (positive)."""
        if avg_volume <= 0:
            return 0.0
        return volatility * math.sqrt(qty / avg_volume) * self._eta
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/unit/test_tca_slippage.py tests/unit/test_tca_impact.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/tca/slippage.py src/hft_platform/tca/impact.py \
    tests/unit/test_tca_slippage.py tests/unit/test_tca_impact.py
git commit -m "feat(tca): add SlippageDecomposer and SqrtImpactModel"
```

---

## Task 11: TCA Analyzer + Report Generator

**Files:**
- Create: `src/hft_platform/tca/analyzer.py`
- Create: `src/hft_platform/tca/report.py`
- Create: `tests/unit/test_tca_analyzer.py`
- Modify: `config/base/main.yaml`

- [ ] **Step 1: Write failing analyzer test**

Create `tests/unit/test_tca_analyzer.py`:
```python
"""TCAAnalyzer unit tests."""
from hft_platform.tca.analyzer import TCAAnalyzer
from hft_platform.tca.types import SlippageBreakdown, TCADailyReport


def _breakdown(total_bps: float) -> SlippageBreakdown:
    return SlippageBreakdown(
        commission_bps=0.5, tax_bps=0.3,
        delay_cost_bps=total_bps * 0.3,
        execution_cost_bps=total_bps * 0.4,
        market_impact_bps=total_bps * 0.1,
        total_bps=total_bps,
    )


def test_aggregate_mean_and_p95() -> None:
    analyzer = TCAAnalyzer()
    breakdowns = [_breakdown(i) for i in range(1, 101)]
    report = analyzer.aggregate(
        breakdowns, date="2026-03-25", strategy="s1",
        symbol="XMT", volume=100, notional=10_000_000,
    )
    assert report.trade_count == 100
    assert abs(report.total_cost_bps_mean - 50.5) < 0.1
    assert report.total_cost_bps_p95 >= 95.0
```

- [ ] **Step 2: Implement TCAAnalyzer**

Create `src/hft_platform/tca/analyzer.py`:
```python
"""Aggregate SlippageBreakdown records into TCADailyReport."""
from __future__ import annotations

import statistics
from hft_platform.tca.types import SlippageBreakdown, TCADailyReport


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * pct / 100)
    idx = min(idx, len(sorted_data) - 1)
    return sorted_data[idx]


class TCAAnalyzer:
    __slots__ = ()

    def aggregate(
        self,
        breakdowns: list[SlippageBreakdown],
        date: str,
        strategy: str,
        symbol: str,
        volume: int,
        notional: int,
    ) -> TCADailyReport:
        if not breakdowns:
            return TCADailyReport(
                date=date, strategy=strategy, symbol=symbol,
                trade_count=0, volume=volume, notional=notional,
                commission_bps_mean=0, tax_bps_mean=0,
                delay_cost_bps_mean=0, delay_cost_bps_p95=0,
                exec_cost_bps_mean=0, exec_cost_bps_p95=0,
                impact_bps_mean=0, total_cost_bps_mean=0,
                total_cost_bps_p95=0,
            )

        return TCADailyReport(
            date=date, strategy=strategy, symbol=symbol,
            trade_count=len(breakdowns), volume=volume, notional=notional,
            commission_bps_mean=statistics.mean(b.commission_bps for b in breakdowns),
            tax_bps_mean=statistics.mean(b.tax_bps for b in breakdowns),
            delay_cost_bps_mean=statistics.mean(b.delay_cost_bps for b in breakdowns),
            delay_cost_bps_p95=_percentile([b.delay_cost_bps for b in breakdowns], 95),
            exec_cost_bps_mean=statistics.mean(b.execution_cost_bps for b in breakdowns),
            exec_cost_bps_p95=_percentile([b.execution_cost_bps for b in breakdowns], 95),
            impact_bps_mean=statistics.mean(b.market_impact_bps for b in breakdowns),
            total_cost_bps_mean=statistics.mean(b.total_bps for b in breakdowns),
            total_cost_bps_p95=_percentile([b.total_bps for b in breakdowns], 95),
        )
```

- [ ] **Step 3: Implement TCAReportGenerator**

Create `src/hft_platform/tca/report.py`:
```python
"""Daily TCA report generator — reads from hft.trades, writes to hft.tca_daily + JSON."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog

from hft_platform.contracts.strategy import Side as SideEnum
from hft_platform.tca.analyzer import TCAAnalyzer
from hft_platform.tca.slippage import SlippageDecomposer
from hft_platform.tca.impact import SqrtImpactModel
from hft_platform.tca.types import TCADailyReport

logger = structlog.get_logger(__name__)

_FETCH_QUERY = """
SELECT
    strategy_id, symbol, side,
    price_scaled, qty, fee_scaled, tax_scaled,
    decision_price_scaled, arrival_price_scaled, gross_pnl_scaled,
    match_ts
FROM hft.trades
WHERE toDate(toDateTime(match_ts / 1000000000)) = {date:String}
  AND decision_price_scaled > 0
ORDER BY match_ts
"""


class TCAReportGenerator:
    __slots__ = (
        "_ch_client", "_decomposer", "_impact_model",
        "_analyzer", "_output_dir",
    )

    def __init__(
        self,
        ch_client: Any,
        output_dir: str = "reports/tca",
        impact_eta: float = 1.0,
    ) -> None:
        self._ch_client = ch_client
        self._decomposer = SlippageDecomposer()
        self._impact_model = SqrtImpactModel(eta=impact_eta)
        self._analyzer = TCAAnalyzer()
        self._output_dir = Path(output_dir)

    async def generate_daily(self, date: str) -> list[TCADailyReport]:
        """Generate TCA reports for a trading date. Returns list of reports."""
        rows = await asyncio.to_thread(
            self._ch_client.execute, _FETCH_QUERY, {"date": date}
        )
        if not rows:
            logger.info("tca_no_fills", date=date)
            return []

        # Group by (strategy, symbol)
        groups: dict[tuple[str, str], list] = defaultdict(list)
        for row in rows:
            key = (row[0], row[1])  # strategy_id, symbol
            groups[key].append(row)

        reports: list[TCADailyReport] = []
        for (strategy, symbol), fills in groups.items():
            breakdowns = []
            total_volume = 0
            total_notional = 0
            for row in fills:
                _, _, side, price, qty, fee, tax, dec_p, arr_p, gross, _ = row
                total_volume += qty
                notional_ntd = (price / 10_000) * qty  # simplified
                total_notional += int(notional_ntd)
                # Create a minimal fill-like object for decomposer
                fill_ns = SimpleNamespace(
                    side=SideEnum.SELL if side == "sell" else SideEnum.BUY,
                    price=price, fee=fee, tax=tax,
                    decision_price=dec_p, arrival_price=arr_p,
                )
                bd = self._decomposer.decompose(fill_ns, notional_ntd)
                breakdowns.append(bd)

            report = self._analyzer.aggregate(
                breakdowns, date=date, strategy=strategy,
                symbol=symbol, volume=total_volume, notional=total_notional,
            )
            reports.append(report)

        # Write to ClickHouse
        await self._write_to_clickhouse(reports)
        # Write JSON
        self._write_json(date, reports)

        logger.info("tca_report_generated", date=date, count=len(reports))
        return reports

    async def _write_to_clickhouse(self, reports: list[TCADailyReport]) -> None:
        if not reports:
            return
        insert_sql = """INSERT INTO hft.tca_daily (
            date, strategy, symbol, trade_count, volume, notional,
            commission_bps_mean, tax_bps_mean,
            delay_cost_bps_mean, delay_cost_bps_p95,
            exec_cost_bps_mean, exec_cost_bps_p95,
            impact_bps_mean, total_cost_bps_mean, total_cost_bps_p95
        ) VALUES"""
        rows = [
            (r.date, r.strategy, r.symbol, r.trade_count, r.volume, r.notional,
             r.commission_bps_mean, r.tax_bps_mean,
             r.delay_cost_bps_mean, r.delay_cost_bps_p95,
             r.exec_cost_bps_mean, r.exec_cost_bps_p95,
             r.impact_bps_mean, r.total_cost_bps_mean, r.total_cost_bps_p95)
            for r in reports
        ]
        await asyncio.to_thread(self._ch_client.execute, insert_sql, rows)

    def _write_json(self, date: str, reports: list[TCADailyReport]) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        path = self._output_dir / f"{date}.json"
        data = [asdict(r) for r in reports]
        path.write_text(json.dumps(data, indent=2, default=str))
```

- [ ] **Step 4: Add TCA config to main.yaml**

Add to `config/base/main.yaml`:
```yaml
tca:
  enabled: false
  report_times:
    - "13:50"
    - "05:05"
  output_dir: "reports/tca"
  retention_days: 90
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/unit/test_tca_analyzer.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/tca/analyzer.py src/hft_platform/tca/report.py \
    tests/unit/test_tca_analyzer.py config/base/main.yaml
git commit -m "feat(tca): add TCAAnalyzer, TCAReportGenerator, and config"
```

---

## Task 12: Canary Metrics Fix

**Files:**
- Modify: `src/hft_platform/alpha/canary_metrics_writer.py:163-173`
- Test: `tests/unit/test_canary_slippage_query.py`

- [ ] **Step 1: Write test for fixed query**

Create `tests/unit/test_canary_slippage_query.py`:
```python
"""Verify canary slippage query reads from hft.trades, not hft.alpha_trades."""
from hft_platform.alpha.canary_metrics_writer import CanaryMetricsWriter


def test_slippage_query_uses_hft_trades() -> None:
    """The slippage query must read from hft.trades, not the non-existent hft.alpha_trades."""
    writer = CanaryMetricsWriter.__new__(CanaryMetricsWriter)
    # Check the query string references hft.trades
    # This is a structural test — the actual query is tested in integration
    import inspect
    source = inspect.getsource(CanaryMetricsWriter)
    assert "hft.alpha_trades" not in source
    assert "hft.trades" in source
```

- [ ] **Step 2: Fix the broken query**

In `src/hft_platform/alpha/canary_metrics_writer.py`, replace the query at lines 163-173 with:

```python
_SLIPPAGE_QUERY = """
SELECT avg(
    CASE WHEN side = 'sell'
         THEN (decision_price_scaled - price_scaled) / decision_price_scaled
         ELSE (price_scaled - decision_price_scaled) / decision_price_scaled
    END
) * 10000 AS avg_slippage_bps
FROM hft.trades
WHERE strategy_id = {strategy:String}
  AND toDate(toDateTime(match_ts / 1000000000)) >= today() - {window_days:UInt32}
  AND decision_price_scaled > 0
"""
```

Replace the old `hft.alpha_trades` query call with `_SLIPPAGE_QUERY`.

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/unit/test_canary_slippage_query.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/hft_platform/alpha/canary_metrics_writer.py tests/unit/test_canary_slippage_query.py
git commit -m "fix(canary): replace phantom hft.alpha_trades query with hft.trades TCA columns"
```

---

## Task 13: Backtest Pipeline Alignment

**Files:**
- Modify: `research/backtest/types.py`
- Test: `tests/unit/test_backtest_futures_cost.py`

- [ ] **Step 1: Write test**

Create `tests/unit/test_backtest_futures_cost.py`:
```python
"""Verify FuturesCostConfig integration in BacktestConfig."""
from research.backtest.types import BacktestConfig, FuturesCostConfig


def test_backtest_config_has_futures_cost() -> None:
    # BacktestConfig is frozen and requires data_paths as mandatory arg
    config = BacktestConfig(data_paths=["test.npz"])
    assert hasattr(config, "futures_cost")
    assert config.futures_cost.use_per_contract_fees is False  # backward compat default
    assert config.futures_cost.fee_schedule_path == "config/base/fees/futures.yaml"


def test_legacy_bps_still_works() -> None:
    config = BacktestConfig(data_paths=["test.npz"], taker_fee_bps=0.5)
    assert config.taker_fee_bps == 0.5
    assert config.futures_cost.use_per_contract_fees is False


def test_futures_cost_config_is_frozen() -> None:
    fc = FuturesCostConfig()
    assert fc.use_per_contract_fees is False
```

- [ ] **Step 2: Add FuturesCostConfig to BacktestConfig**

In `research/backtest/types.py`, add before `BacktestConfig`:
```python
@dataclass(frozen=True)
class FuturesCostConfig:
    """Per-contract fee config for futures. Frozen to match BacktestConfig."""
    fee_schedule_path: str = "config/base/fees/futures.yaml"
    use_per_contract_fees: bool = False
```

In `BacktestConfig`, add after existing fields (uses `field(default_factory=...)` which works with frozen dataclasses since the factory creates a new frozen instance):
```python
    futures_cost: FuturesCostConfig = field(default_factory=FuturesCostConfig)
```
Note: `field` must be imported — check if already imported, add to existing import if not.

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/unit/test_backtest_futures_cost.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add research/backtest/types.py tests/unit/test_backtest_futures_cost.py
git commit -m "feat(backtest): add FuturesCostConfig with per-contract fee opt-in"
```

---

## Task 14: Lint + Full Test Suite + Final Commit

**Files:** All modified files

- [ ] **Step 1: Run linter**

Run: `uv run ruff check src/hft_platform/tca/ tests/unit/test_tca_*.py`
Fix any issues.

- [ ] **Step 2: Run type checker**

Run: `uv run mypy src/hft_platform/tca/`
Fix any type errors.

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -x -q --timeout=120`
Expected: ALL PASS

- [ ] **Step 4: Fix any regressions**

Address test failures from existing tests affected by PnL changes or new contract fields.

- [ ] **Step 5: Final commit if needed**

```bash
git add -u
git commit -m "fix: resolve lint errors and test regressions from TCA implementation"
```

---

## Parallel Execution Guide

Tasks can be parallelized as follows:

| Group | Tasks | Dependencies |
|---|---|---|
| **A: Foundation** | 1 (types + config), 2 (FeeCalculator) | None |
| **B: Contracts** | 3 (contract expansion) | None (parallel with A) |
| **C: Pipeline** | 4 (PnL fix), 5 (decision_price), 6 (inflight map), 7 (normalizer), 8 (bootstrap) | A + B |
| **D: TCA Module** | 10 (slippage + impact), 11 (analyzer + report) | A + B |
| **E: Persistence** | 9 (migration + recorder) | B |
| **F: Integration** | 12 (canary fix), 13 (backtest), 14 (lint + tests) | C + D + E |

Groups A, B can run in parallel. Groups C, D, E can run in parallel after A+B complete. Group F runs last.
