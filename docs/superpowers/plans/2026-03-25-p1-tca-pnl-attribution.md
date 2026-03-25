# P1-B+C: TCA Pipeline + PnL Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire fee calculation into the execution pipeline, build a TCA analysis engine for daily cost reports, and add a PnL attribution panel to the TUI monitor.

**Architecture:** FeeCalculator loads fee schedules from YAML, computes per-fill combined fee (commission + tax as a single `fee_scaled` value), and patches FillEvent before it enters the recording pipeline. TCAAnalyzer queries ClickHouse `hft.fills` to produce `TCADailyReport` with cost breakdown. The TUI monitor gains a `_pnl_panel.py` querying ClickHouse for per-strategy cost attribution. Note: true realized PnL requires matched entry/exit pairs — this plan uses net notional as a proxy and labels it accordingly.

**Tech Stack:** Python 3.12, ClickHouse (clickhouse-connect), structlog

**Spec:** `docs/superpowers/specs/2026-03-25-operational-readiness-assessment.md` (items E1, E2, O1)

**Dependency chain:** Task 1 (E2) → Task 2 (E1) → Task 3 (O1)

## Schema Notes

**`hft.fills` has these columns:** `ts_exchange`, `ts_local`, `client_order_id`, `broker_order_id`, `fill_id`, `strategy_id`, `symbol`, `side` (values: `"BUY"` or `"SELL"`), `qty`, `price_scaled`, `fee_scaled` (single combined column for commission+tax), `source`.

**No separate `fee` + `tax` columns.** FeeCalculator computes commission+tax combined and writes to `fee_scaled`. A new migration adds `tax_scaled` for TCA analysis to split the breakdown post-hoc.

**`Side` enum:** `Side.BUY=0`, `Side.SELL=1`. Mapper writes `Side.name` → `"BUY"` / `"SELL"` (NOT `"B"` / `"S"`).

**Fee config YAML keys:** `TX`, `MTX`, `XMT` (product codes). Broker symbols: `TXF`, `MXFR1`, `TXFL5`, etc. FeeCalculator needs a product code resolver mapping broker symbols → config keys.

---

## Task 1: E2 — FeeCalculator + Execution Pipeline Integration

Creates a `FeeCalculator` that loads fee schedules from `config/base/fees/futures.yaml`, computes per-fill fees using **integer arithmetic only** (Precision Law compliance), and patches FillEvent in the execution normalizer.

**Files:**
- Create: `src/hft_platform/tca/fee_calculator.py`
- Create: `src/hft_platform/migrations/clickhouse/20260325_002_add_tax_scaled.sql`
- Modify: `src/hft_platform/execution/normalizer.py` (inject FeeCalculator)
- Modify: `src/hft_platform/recorder/mapper.py` (write tax_scaled)
- Modify: `src/hft_platform/services/bootstrap.py` (create and inject FeeCalculator)
- Modify: `src/hft_platform/tca/__init__.py` (export FeeCalculator)
- Test: `tests/unit/test_fee_calculator.py`

### Step 1.1: Write failing tests for FeeCalculator

- [ ] **Create test file**

```python
# tests/unit/test_fee_calculator.py
"""Tests for FeeCalculator — per-fill commission and tax computation."""
from __future__ import annotations

import pytest

from hft_platform.tca.fee_calculator import FeeCalculator
from hft_platform.tca.types import FeeBreakdown


# Fee config uses product codes (TX, MTX), not broker symbols (TXF, MXFR1)
_SCHEDULES = {
    "TX": {
        "commission_per_contract": 60,
        "tax_rate_bps_x100": 200,  # 2.0 bps stored as int * 100
        "tax_side": "sell",
        "tick_size_x100": 100,  # 1.0 stored as int * 100
        "point_value": 200,
    },
    "MTX": {
        "commission_per_contract": 30,
        "tax_rate_bps_x100": 200,
        "tax_side": "sell",
        "tick_size_x100": 100,
        "point_value": 50,
    },
}

# Product code mapping: broker symbol → config key
_SYMBOL_MAP = {"TXF": "TX", "TXFL5": "TX", "MXF": "MTX", "MXFR1": "MTX"}


class TestFeeCalculator:
    @pytest.fixture
    def calc(self) -> FeeCalculator:
        return FeeCalculator(_SCHEDULES, symbol_to_product=_SYMBOL_MAP)

    def test_buy_side_no_tax(self, calc: FeeCalculator) -> None:
        """Buy-side: commission only, no tax."""
        result = calc.compute("TXF", side="BUY", qty=2, price_scaled=200_000_000)
        assert result.commission == 120 * 10000  # 60 * 2 * 10000
        assert result.tax == 0
        assert result.total == result.commission

    def test_sell_side_has_tax(self, calc: FeeCalculator) -> None:
        """Sell-side: commission + futures transaction tax."""
        result = calc.compute("TXF", side="SELL", qty=1, price_scaled=200_000_000)
        assert result.commission == 60 * 10000
        assert result.tax > 0
        assert result.total == result.commission + result.tax

    def test_unknown_symbol_returns_zero(self, calc: FeeCalculator) -> None:
        """Unknown symbol → zero fees, no crash."""
        result = calc.compute("UNKNOWN", side="BUY", qty=1, price_scaled=100_000_000)
        assert result.total == 0

    def test_mtx_lower_commission(self, calc: FeeCalculator) -> None:
        """MTX has lower commission than TX."""
        tx = calc.compute("TXF", side="BUY", qty=1, price_scaled=200_000_000)
        mtx = calc.compute("MXF", side="BUY", qty=1, price_scaled=200_000_000)
        assert mtx.commission < tx.commission

    def test_contract_month_symbol_resolves(self, calc: FeeCalculator) -> None:
        """TXFL5, MXFR1 resolve to TX, MTX via symbol map."""
        r1 = calc.compute("TXFL5", side="BUY", qty=1, price_scaled=200_000_000)
        r2 = calc.compute("TXF", side="BUY", qty=1, price_scaled=200_000_000)
        assert r1.commission == r2.commission

    def test_zero_qty(self, calc: FeeCalculator) -> None:
        result = calc.compute("TXF", side="BUY", qty=0, price_scaled=200_000_000)
        assert result.total == 0

    def test_result_type(self, calc: FeeCalculator) -> None:
        result = calc.compute("TXF", side="BUY", qty=1, price_scaled=200_000_000)
        assert isinstance(result, FeeBreakdown)

    def test_tax_uses_integer_arithmetic(self, calc: FeeCalculator) -> None:
        """Tax computation must be pure integer — no float intermediate."""
        result = calc.compute("TXF", side="SELL", qty=1, price_scaled=200_000_000)
        assert isinstance(result.tax, int)
        assert isinstance(result.commission, int)

    def test_from_yaml(self, tmp_path) -> None:
        """FeeCalculator.from_yaml loads config and symbol map."""
        yaml_content = """\
futures:
  TX:
    commission_per_contract: 60
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 200
symbol_map:
  TXF: TX
"""
        f = tmp_path / "fees.yaml"
        f.write_text(yaml_content)
        calc = FeeCalculator.from_yaml(str(f))
        result = calc.compute("TXF", side="BUY", qty=1, price_scaled=200_000_000)
        assert result.commission == 60 * 10000
```

- [ ] **Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_fee_calculator.py -v --no-header --no-cov 2>&1 | head -15`
Expected: FAIL with `ModuleNotFoundError`

### Step 1.2: Implement FeeCalculator

- [ ] **Create the module**

```python
# src/hft_platform/tca/fee_calculator.py
"""FeeCalculator — per-fill commission and tax computation.

All monetary outputs are scaled x10000. All internal arithmetic is pure integer
(Precision Law compliance — no float on live execution path).
"""
from __future__ import annotations

from typing import Any

import structlog

from hft_platform.tca.types import FeeBreakdown

logger = structlog.get_logger(__name__)

_ZERO = FeeBreakdown(commission=0, tax=0, total=0)


class FeeCalculator:
    """Compute per-fill fees from fee schedule configuration.

    Uses integer arithmetic only. tax_rate_bps is stored as int * 100
    internally to avoid float (e.g., 2.0 bps → 200).
    """

    __slots__ = ("_schedules", "_symbol_to_product")

    def __init__(
        self,
        schedules: dict[str, dict[str, Any]],
        *,
        symbol_to_product: dict[str, str] | None = None,
    ) -> None:
        self._schedules = schedules
        self._symbol_to_product = symbol_to_product or {}

    @classmethod
    def from_yaml(cls, path: str) -> FeeCalculator:
        """Load fee schedules from YAML. Converts float bps to int * 100."""
        import yaml  # noqa: PLC0415

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        futures = raw.get("futures", {})
        symbol_map = raw.get("symbol_map", {})

        schedules: dict[str, dict[str, Any]] = {}
        for product, conf in futures.items():
            if not isinstance(conf, dict):
                continue
            # Convert float fields to integer representations
            schedules[product] = {
                "commission_per_contract": int(conf.get("commission_per_contract", 0)),
                "tax_rate_bps_x100": int(round(conf.get("tax_rate_bps", 0.0) * 100)),
                "tax_side": conf.get("tax_side", "sell"),
                "tick_size_x100": int(round(conf.get("tick_size", 1) * 100)),
                "point_value": int(conf.get("point_value", 1)),
            }

        return cls(schedules, symbol_to_product=symbol_map)

    def compute(
        self,
        symbol: str,
        *,
        side: str,
        qty: int,
        price_scaled: int,
    ) -> FeeBreakdown:
        """Compute fees for a single fill. Pure integer arithmetic.

        Args:
            symbol: Broker symbol (e.g., "TXF", "MXFR1")
            side: "BUY" or "SELL"
            qty: Number of contracts
            price_scaled: Fill price scaled x10000
        """
        if qty == 0:
            return _ZERO

        # Resolve broker symbol → product code
        product = self._symbol_to_product.get(symbol, symbol)
        sched = self._schedules.get(product)
        if sched is None:
            return _ZERO

        abs_qty = abs(qty)
        comm_per = sched.get("commission_per_contract", 0)
        tax_rate_x100 = sched.get("tax_rate_bps_x100", 0)
        tax_side = sched.get("tax_side", "sell")
        point_value = sched.get("point_value", 1)
        tick_size_x100 = sched.get("tick_size_x100", 100)

        # Commission: per_contract * qty, scaled x10000
        commission = comm_per * abs_qty * 10000

        # Tax: sell-side only, pure integer
        # notional_scaled = price_scaled * qty * point_value * 100 / tick_size_x100
        # tax = notional_scaled * tax_rate_x100 / (10000 * 100)
        tax = 0
        if side == "SELL" and tax_side == "sell" and tax_rate_x100 > 0:
            notional_x100 = price_scaled * abs_qty * point_value * 100 // tick_size_x100
            tax = notional_x100 * tax_rate_x100 // (10000 * 100)

        total = commission + tax
        return FeeBreakdown(commission=commission, tax=tax, total=total)
```

- [ ] **Update `tca/__init__.py`** to export FeeCalculator
- [ ] **Run tests**
- [ ] **Commit**

```bash
git add src/hft_platform/tca/fee_calculator.py src/hft_platform/tca/__init__.py tests/unit/test_fee_calculator.py
git commit -m "feat(tca): add FeeCalculator with integer arithmetic (E2)"
```

### Step 1.3: Add migration + inject into normalizer

- [ ] **Create ClickHouse migration**

```sql
-- src/hft_platform/migrations/clickhouse/20260325_002_add_tax_scaled.sql
-- Add tax_scaled column for TCA fee/tax split analysis.
-- fee_scaled remains as combined (commission + tax) for backward compatibility.
-- tax_scaled stores tax portion separately for cost attribution.
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS tax_scaled Int64 DEFAULT 0 Codec(DoubleDelta, LZ4);
```

- [ ] **Modify ExecutionNormalizer** (`src/hft_platform/execution/normalizer.py`):

Add `_fee_calculator` to `__slots__` and `__init__` (optional, default None).
In `normalize_fill()` around line 207-208, replace hardcoded fee=0/tax=0:

```python
fee_scaled = 0
tax_scaled = 0
if self._fee_calculator is not None:
    side_str = "BUY" if side == Side.BUY else "SELL"
    breakdown = self._fee_calculator.compute(
        sym, side=side_str, qty=qty, price_scaled=scale_price
    )
    fee_scaled = breakdown.total  # combined commission + tax → fee_scaled column
    tax_scaled = breakdown.tax    # tax portion for TCA analysis

return FillEvent(
    ...
    fee=fee_scaled,
    tax=tax_scaled,
    ...
)
```

Note: FillEvent has `fee: int` and `tax: int` fields. The mapper writes `fee` to `fee_scaled` column. We need to also write `tax` to `tax_scaled` column.

- [ ] **Modify recorder mapper** (`src/hft_platform/recorder/mapper.py`):

Add `"tax_scaled"` to the fill record dict (around line 160):

```python
"tax_scaled": _to_ch_price_scaled(symbol, event.tax, metadata, price_codec),
```

- [ ] **Wire FeeCalculator in bootstrap.py**

```python
fee_calculator = None
try:
    from hft_platform.tca.fee_calculator import FeeCalculator
    fee_yaml = os.path.join(config_dir, "base", "fees", "futures.yaml")
    if os.path.exists(fee_yaml):
        fee_calculator = FeeCalculator.from_yaml(fee_yaml)
        logger.info("FeeCalculator loaded", path=fee_yaml)
except Exception as exc:
    logger.warning("FeeCalculator creation failed", error=str(exc))
```

Pass `fee_calculator` to ExecutionNormalizer.

- [ ] **Add `symbol_map` section to fees YAML** (`config/base/fees/futures.yaml`):

```yaml
symbol_map:
  TXF: TX
  TXFL5: TX
  TXFR1: TX
  MXF: MTX
  MXFR1: MTX
  XMT: XMT
```

- [ ] **Run all tests**
- [ ] **Commit**

```bash
git add src/hft_platform/execution/normalizer.py src/hft_platform/recorder/mapper.py \
  src/hft_platform/services/bootstrap.py src/hft_platform/migrations/clickhouse/20260325_002_add_tax_scaled.sql \
  config/base/fees/futures.yaml
git commit -m "feat(tca): inject FeeCalculator into execution pipeline + add tax_scaled migration (E2)"
```

---

## Task 2: E1 — TCA Analysis Engine

Creates `TCAAnalyzer` that queries ClickHouse for daily fill cost data and a `hft tca daily` CLI command.

**Files:**
- Create: `src/hft_platform/tca/analyzer.py`
- Create: `src/hft_platform/cli/_tca.py`
- Modify: `src/hft_platform/cli/_parser.py` (add tca subcommand)
- Test: `tests/unit/test_tca_analyzer.py`

### Step 2.1: Write failing tests

- [ ] **Create test file**

```python
# tests/unit/test_tca_analyzer.py
"""Tests for TCAAnalyzer — daily TCA report from ClickHouse."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.tca.analyzer import TCAAnalyzer
from hft_platform.tca.types import TCADailyReport


@dataclass(slots=True)
class FakeCHClient:
    rows: list[tuple] = field(default_factory=list)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        result = MagicMock()
        result.result_rows = self.rows
        return result


class TestTCAAnalyzer:
    def test_daily_report_produces_reports(self) -> None:
        # strategy, symbol, trade_count, volume, notional, fee_scaled_total, tax_scaled_total
        ch = FakeCHClient(rows=[("mm1", "TXF", 10, 20, 4_000_000_000, 12_000_000, 800_000)])
        analyzer = TCAAnalyzer(ch_client=ch)
        reports = analyzer.daily_report("2026-03-25")
        assert len(reports) == 1
        assert reports[0].strategy == "mm1"
        assert reports[0].trade_count == 10

    def test_empty_day(self) -> None:
        ch = FakeCHClient(rows=[])
        analyzer = TCAAnalyzer(ch_client=ch)
        assert analyzer.daily_report("2026-03-25") == []

    def test_multiple_strategies(self) -> None:
        ch = FakeCHClient(rows=[
            ("mm1", "TXF", 5, 10, 2_000_000_000, 6_000_000, 400_000),
            ("alpha1", "MXF", 3, 6, 600_000_000, 1_800_000, 120_000),
        ])
        analyzer = TCAAnalyzer(ch_client=ch)
        reports = analyzer.daily_report("2026-03-25")
        assert len(reports) == 2

    def test_ch_failure_returns_empty(self) -> None:
        ch = MagicMock()
        ch.query.side_effect = Exception("down")
        analyzer = TCAAnalyzer(ch_client=ch)
        assert analyzer.daily_report("2026-03-25") == []

    def test_cost_bps_computed(self) -> None:
        ch = FakeCHClient(rows=[("mm1", "TXF", 10, 20, 4_000_000_000, 12_000_000, 800_000)])
        analyzer = TCAAnalyzer(ch_client=ch)
        r = analyzer.daily_report("2026-03-25")[0]
        assert r.commission_bps_mean > 0  # fee analysis — float OK in offline
```

### Step 2.2: Implement TCAAnalyzer

- [ ] **Create the module**

```python
# src/hft_platform/tca/analyzer.py
"""TCAAnalyzer — daily TCA report generation from ClickHouse fill data.

Float arithmetic is used here — this is offline analysis code only (Alpha Module
Float Exception, Architecture Governance Rule 11).
"""
from __future__ import annotations

from typing import Any

import structlog

from hft_platform.tca.types import TCADailyReport

logger = structlog.get_logger(__name__)

# Uses actual column names from hft.fills schema.
# side values are "BUY" / "SELL" (Side.name).
# fee_scaled = combined commission+tax. tax_scaled = tax portion.
_DAILY_SQL = """\
SELECT
    strategy_id,
    symbol,
    count(*) AS trade_count,
    sum(qty) AS volume,
    sum(price_scaled * qty) AS notional_scaled,
    sum(fee_scaled) AS total_fee_scaled,
    sum(tax_scaled) AS total_tax_scaled
FROM hft.fills
WHERE toDate(toDateTime(ts_exchange / 1000000000)) = {date:String}
GROUP BY strategy_id, symbol
ORDER BY strategy_id, symbol
"""


class TCAAnalyzer:
    """Generates daily TCA reports from ClickHouse fill data."""

    __slots__ = ("_ch_client",)

    def __init__(self, *, ch_client: Any) -> None:
        self._ch_client = ch_client

    def daily_report(self, date_str: str) -> list[TCADailyReport]:
        """Generate per-(strategy, symbol) TCA reports for a date."""
        try:
            result = self._ch_client.query(_DAILY_SQL, parameters={"date": date_str})
            rows = getattr(result, "result_rows", None) or []
        except Exception:
            logger.warning("tca.query_failed", exc_info=True)
            return []

        reports: list[TCADailyReport] = []
        for row in rows:
            strategy, symbol = str(row[0]), str(row[1])
            trade_count, volume = int(row[2]), int(row[3])
            notional = int(row[4])
            total_fee = int(row[5])
            total_tax = int(row[6])
            total_commission = total_fee - total_tax  # fee_scaled = comm + tax

            # bps calculation (float — offline analysis only)
            if notional > 0:
                comm_bps = total_commission / notional * 10000
                tax_bps = total_tax / notional * 10000
                total_bps = total_fee / notional * 10000
            else:
                comm_bps = tax_bps = total_bps = 0.0

            reports.append(TCADailyReport(
                date=date_str, strategy=strategy, symbol=symbol,
                trade_count=trade_count, volume=volume, notional=notional,
                commission_bps_mean=comm_bps, tax_bps_mean=tax_bps,
                delay_cost_bps_mean=0.0, delay_cost_bps_p95=0.0,  # TODO: from slippage_records
                exec_cost_bps_mean=0.0, exec_cost_bps_p95=0.0,
                impact_bps_mean=0.0,
                total_cost_bps_mean=total_bps, total_cost_bps_p95=0.0,
            ))

        logger.info("tca.daily_report", date=date_str, count=len(reports))
        return reports
```

- [ ] **Create CLI** (`src/hft_platform/cli/_tca.py`) and register in parser
- [ ] **Run tests, commit**

---

## Task 3: O1 — PnL Cost Attribution Panel in TUI Monitor

Adds a cost attribution panel showing per-strategy fills, fees, and net cost to the TUI monitor.

**Files:**
- Create: `src/hft_platform/monitor/_pnl_panel.py`
- Modify: `src/hft_platform/monitor/_engine.py` (integrate panel)
- Modify: `src/hft_platform/monitor/_renderer.py` (render section)
- Test: `tests/unit/test_pnl_panel.py`

### Step 3.1: Write failing tests

- [ ] **Create test file**

```python
# tests/unit/test_pnl_panel.py
"""Tests for cost attribution panel data fetching."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.monitor._pnl_panel import CostPanelData, fetch_cost_attribution


@dataclass(slots=True)
class FakeCHClient:
    rows: list[tuple] = field(default_factory=list)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        result = MagicMock()
        result.result_rows = self.rows
        return result


class TestCostPanel:
    def test_fetch_returns_per_strategy_breakdown(self) -> None:
        ch = FakeCHClient(rows=[
            ("mm1", "TXF", 10, 6_000_000, 800_000),
            ("alpha1", "MXF", 3, 1_800_000, 120_000),
        ])
        data = fetch_cost_attribution(ch, "2026-03-25")
        assert len(data) == 2
        assert data[0].strategy == "mm1"
        assert data[0].total_fee_scaled == 6_000_000

    def test_empty_returns_empty(self) -> None:
        ch = FakeCHClient(rows=[])
        assert fetch_cost_attribution(ch, "2026-03-25") == []

    def test_ch_failure_returns_empty(self) -> None:
        ch = MagicMock()
        ch.query.side_effect = Exception("timeout")
        assert fetch_cost_attribution(ch, "2026-03-25") == []

    def test_net_fee_equals_commission_plus_tax(self) -> None:
        ch = FakeCHClient(rows=[("mm1", "TXF", 5, 3_000_000, 500_000)])
        data = fetch_cost_attribution(ch, "2026-03-25")
        d = data[0]
        assert d.total_fee_scaled == 3_000_000
        assert d.tax_scaled == 500_000
        assert d.commission_scaled == d.total_fee_scaled - d.tax_scaled
```

### Step 3.2: Implement cost attribution panel

- [ ] **Create the module**

```python
# src/hft_platform/monitor/_pnl_panel.py
"""Cost attribution panel for TUI monitor.

Queries ClickHouse for per-strategy daily fill cost breakdown.
Uses hft.fills columns: fee_scaled (combined), tax_scaled (tax portion).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_COST_SQL = """\
SELECT
    strategy_id,
    symbol,
    count(*) AS fill_count,
    sum(fee_scaled) AS total_fee_scaled,
    sum(tax_scaled) AS total_tax_scaled
FROM hft.fills
WHERE toDate(toDateTime(ts_exchange / 1000000000)) = {date:String}
GROUP BY strategy_id, symbol
ORDER BY total_fee_scaled DESC
"""


@dataclass(slots=True, frozen=True)
class CostPanelData:
    """Per-(strategy, symbol) cost attribution row."""

    strategy: str
    symbol: str
    fill_count: int
    total_fee_scaled: int  # commission + tax combined
    tax_scaled: int

    @property
    def commission_scaled(self) -> int:
        return self.total_fee_scaled - self.tax_scaled


def fetch_cost_attribution(ch_client: Any, date_str: str) -> list[CostPanelData]:
    """Query ClickHouse for daily cost attribution."""
    try:
        result = ch_client.query(_COST_SQL, parameters={"date": date_str})
        rows = getattr(result, "result_rows", None) or []
    except Exception:
        logger.warning("cost_panel.query_failed", exc_info=True)
        return []

    return [
        CostPanelData(
            strategy=str(row[0]),
            symbol=str(row[1]),
            fill_count=int(row[2]),
            total_fee_scaled=int(row[3]),
            tax_scaled=int(row[4]),
        )
        for row in rows
    ]


def render_cost_table(data: list[CostPanelData]) -> list[str]:
    """Render cost attribution as text lines for TUI display."""
    if not data:
        return ["  No fills today"]

    lines: list[str] = []
    lines.append(f"  {'Strategy':<10} {'Symbol':<7} {'Fills':>5} {'Comm':>9} {'Tax':>8} {'Total':>9}")
    lines.append("  " + "-" * 52)

    total_comm = 0
    total_tax = 0
    total_fee = 0

    for d in data:
        comm_ntd = d.commission_scaled / 10000
        tax_ntd = d.tax_scaled / 10000
        fee_ntd = d.total_fee_scaled / 10000
        total_comm += d.commission_scaled
        total_tax += d.tax_scaled
        total_fee += d.total_fee_scaled

        lines.append(
            f"  {d.strategy:<10} {d.symbol:<7} {d.fill_count:>5} "
            f"{comm_ntd:>9,.0f} {tax_ntd:>8,.0f} {fee_ntd:>9,.0f}"
        )

    lines.append("  " + "-" * 52)
    lines.append(
        f"  {'TOTAL':<10} {'':<7} {'':>5} "
        f"{total_comm / 10000:>9,.0f} {total_tax / 10000:>8,.0f} {total_fee / 10000:>9,.0f}"
    )
    return lines
```

- [ ] **Run tests, commit**

### Step 3.3: Integrate into TUI engine

- [ ] **Modify `_engine.py`**: Add import, add `_cost_lines` and `_cost_last_fetch_ns` to slots, refresh every 60s using ClickHouse client from data source.

- [ ] **Modify `_renderer.py`**: Add `build_cost_section(cost_lines, width)` function that returns the cost table lines as a section after the health panel.

- [ ] **Run tests, commit**

---

## Summary

| Task | Item | Files Created | Files Modified | Tests |
|------|------|---------------|----------------|-------|
| 1 | E2 FeeCalculator | `tca/fee_calculator.py`, migration SQL | `normalizer.py`, `mapper.py`, `bootstrap.py`, `tca/__init__.py`, fees YAML | 9 |
| 2 | E1 TCAAnalyzer | `tca/analyzer.py`, `cli/_tca.py` | `cli/_parser.py` | 5 |
| 3 | O1 Cost Panel | `monitor/_pnl_panel.py` | `monitor/_engine.py`, `monitor/_renderer.py` | 4 |
| **Total** | | **4 new files + 1 migration** | **7 modified** | **18 tests** |

**Exit criteria:**
- All 18 tests PASS
- `hft tca daily` prints per-strategy cost breakdown
- TUI monitor shows cost attribution section
- FillEvent.fee populated from FeeCalculator (not hardcoded 0)
- All fee arithmetic is pure integer (no float on live path)
