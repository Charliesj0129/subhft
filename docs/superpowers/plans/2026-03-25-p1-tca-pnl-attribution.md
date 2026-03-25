# P1-B+C: TCA Pipeline + PnL Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire fee calculation into the execution pipeline, build a TCA analysis engine for daily slippage reports, and add a PnL attribution panel to the TUI monitor.

**Architecture:** FeeCalculator loads fee schedules from YAML, computes per-fill fees, and patches FillEvent before it enters the recording pipeline. TCAAnalyzer queries ClickHouse `hft.fills` and `hft.slippage_records` to produce `TCADailyReport`. The TUI monitor gains a new `_pnl_panel.py` that queries ClickHouse for per-strategy PnL breakdown (gross/net/fees/slippage).

**Tech Stack:** Python 3.12, ClickHouse (clickhouse-connect), structlog, Rich (TUI rendering)

**Spec:** `docs/superpowers/specs/2026-03-25-operational-readiness-assessment.md` (items E1, E2, O1)

**Dependency chain:** Task 1 (E2) → Task 2 (E1) → Task 3 (O1)

---

## Task 1: E2 — FeeCalculator + Execution Pipeline Integration

Creates a `FeeCalculator` that loads fee schedules from `config/base/fees/futures.yaml`, computes per-fill commission + tax, and patches FillEvent in the execution normalizer.

**Files:**
- Create: `src/hft_platform/tca/fee_calculator.py`
- Modify: `src/hft_platform/execution/normalizer.py` (inject FeeCalculator, replace hardcoded fee=0/tax=0)
- Modify: `src/hft_platform/services/bootstrap.py` (create and inject FeeCalculator)
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


class TestFeeCalculator:
    @pytest.fixture
    def calc(self) -> FeeCalculator:
        """Create calculator with TX and MTX schedules."""
        schedules = {
            "TXF": {"commission_per_contract": 60, "tax_rate_bps": 2.0, "tax_side": "sell", "tick_size": 1, "point_value": 200},
            "MXF": {"commission_per_contract": 30, "tax_rate_bps": 2.0, "tax_side": "sell", "tick_size": 1, "point_value": 50},
        }
        return FeeCalculator(schedules)

    def test_buy_side_no_tax(self, calc: FeeCalculator) -> None:
        """Buy-side fills have commission but no tax (tax is sell-side only)."""
        result = calc.compute("TXF", side="B", qty=2, price_scaled=200_000_000)
        assert result.commission == 120 * 10000  # 60 NTD * 2 contracts * 10000
        assert result.tax == 0
        assert result.total == result.commission

    def test_sell_side_has_tax(self, calc: FeeCalculator) -> None:
        """Sell-side fills include tax based on notional."""
        # price=20000 (200_000_000 / 10000), point_value=200
        # notional = qty * price_unscaled * point_value / tick_size
        # But tax = notional * tax_rate_bps / 10000
        result = calc.compute("TXF", side="S", qty=1, price_scaled=200_000_000)
        assert result.commission == 60 * 10000  # 60 NTD * 1 contract
        assert result.tax > 0
        assert result.total == result.commission + result.tax

    def test_unknown_symbol_returns_zero(self, calc: FeeCalculator) -> None:
        """Unknown symbols return zero fees (no crash)."""
        result = calc.compute("UNKNOWN", side="B", qty=1, price_scaled=100_000_000)
        assert result.commission == 0
        assert result.tax == 0
        assert result.total == 0

    def test_mtx_commission_differs_from_tx(self, calc: FeeCalculator) -> None:
        """MTX has lower commission than TX."""
        tx = calc.compute("TXF", side="B", qty=1, price_scaled=200_000_000)
        mtx = calc.compute("MXF", side="B", qty=1, price_scaled=200_000_000)
        assert mtx.commission < tx.commission

    def test_from_yaml_file(self, tmp_path) -> None:
        """FeeCalculator.from_yaml loads config correctly."""
        yaml_content = """
futures:
  TX:
    commission_per_contract: 60
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 200
"""
        f = tmp_path / "fees.yaml"
        f.write_text(yaml_content)
        calc = FeeCalculator.from_yaml(str(f))
        result = calc.compute("TX", side="B", qty=1, price_scaled=200_000_000)
        assert result.commission == 60 * 10000

    def test_zero_qty_returns_zero(self, calc: FeeCalculator) -> None:
        """Zero quantity returns zero fees."""
        result = calc.compute("TXF", side="B", qty=0, price_scaled=200_000_000)
        assert result.total == 0

    def test_result_is_fee_breakdown(self, calc: FeeCalculator) -> None:
        """Result type is FeeBreakdown (frozen dataclass)."""
        result = calc.compute("TXF", side="B", qty=1, price_scaled=200_000_000)
        assert isinstance(result, FeeBreakdown)
```

- [ ] **Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_fee_calculator.py -v --no-header --no-cov 2>&1 | head -15`
Expected: FAIL with `ModuleNotFoundError`

### Step 1.2: Implement FeeCalculator

- [ ] **Create the module**

```python
# src/hft_platform/tca/fee_calculator.py
"""FeeCalculator — per-fill commission and tax computation.

Loads fee schedules from YAML config. All monetary outputs are scaled x10000.
"""
from __future__ import annotations

from typing import Any

import structlog

from hft_platform.tca.types import FeeBreakdown

logger = structlog.get_logger(__name__)

_ZERO = FeeBreakdown(commission=0, tax=0, total=0)


class FeeCalculator:
    """Compute per-fill fees from fee schedule configuration."""

    __slots__ = ("_schedules",)

    def __init__(self, schedules: dict[str, dict[str, Any]]) -> None:
        self._schedules = schedules

    @classmethod
    def from_yaml(cls, path: str) -> FeeCalculator:
        """Load fee schedules from a YAML file."""
        import yaml  # noqa: PLC0415

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        schedules: dict[str, dict[str, Any]] = {}
        futures = raw.get("futures", {})
        overrides = futures.pop("overrides", {})

        for symbol, conf in futures.items():
            if isinstance(conf, dict):
                schedules[symbol] = conf

        for symbol, conf in overrides.items():
            if isinstance(conf, dict):
                # Merge override with base (e.g., stock_futures_default)
                base = dict(schedules.get("stock_futures_default", {}))
                base.update(conf)
                schedules[symbol] = base

        return cls(schedules)

    def compute(
        self,
        symbol: str,
        *,
        side: str,
        qty: int,
        price_scaled: int,
    ) -> FeeBreakdown:
        """Compute fees for a single fill.

        Args:
            symbol: Contract symbol (e.g., "TXF", "MXF")
            side: "B" (buy) or "S" (sell)
            qty: Number of contracts
            price_scaled: Fill price scaled x10000

        Returns:
            FeeBreakdown with commission, tax, total (all scaled x10000)
        """
        if qty == 0:
            return _ZERO

        # Try exact match, then strip trailing digits for product lookup
        sched = self._schedules.get(symbol)
        if sched is None:
            # Try product code: TXF1 → TXF, TXFR1 → TXF, etc.
            base = self._strip_contract_month(symbol)
            sched = self._schedules.get(base)
        if sched is None:
            return _ZERO

        comm_per_contract = sched.get("commission_per_contract", 0)
        tax_rate_bps = sched.get("tax_rate_bps", 0.0)
        tax_side = sched.get("tax_side", "sell")
        point_value = sched.get("point_value", 1)
        tick_size = sched.get("tick_size", 1)

        # Commission: per contract * qty, scaled x10000
        commission = comm_per_contract * abs(qty) * 10000

        # Tax: only on sell side
        tax = 0
        if side == "S" and tax_side == "sell" and tax_rate_bps > 0:
            # Notional = price * qty * point_value / tick_size
            # price_unscaled = price_scaled / 10000
            # notional_ntd = price_unscaled * qty * point_value / tick_size
            # tax_ntd = notional_ntd * tax_rate_bps / 10000
            # tax_scaled = tax_ntd * 10000
            # Simplified: tax_scaled = price_scaled * qty * point_value * tax_rate_bps / (tick_size * 10000)
            notional_scaled = price_scaled * abs(qty) * point_value // tick_size
            tax = int(notional_scaled * tax_rate_bps / 10000)

        total = commission + tax
        return FeeBreakdown(commission=commission, tax=tax, total=total)

    @staticmethod
    def _strip_contract_month(symbol: str) -> str:
        """Strip contract month suffix: TXF202604 → TXF, MXFR1 → MXF."""
        # Common patterns: TXF, TXFL5, TXF202604
        for length in (3, 2):
            if len(symbol) > length and symbol[:length].isalpha():
                return symbol[:length]
        return symbol
```

- [ ] **Update `src/hft_platform/tca/__init__.py`** to export FeeCalculator:

```python
from hft_platform.tca.fee_calculator import FeeCalculator
from hft_platform.tca.types import FeeBreakdown, FeeSchedule, SlippageBreakdown, TCADailyReport
```

- [ ] **Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_fee_calculator.py -v --no-header --no-cov`
Expected: All 8 tests PASS

- [ ] **Commit**

```bash
git add src/hft_platform/tca/fee_calculator.py src/hft_platform/tca/__init__.py tests/unit/test_fee_calculator.py
git commit -m "feat(tca): add FeeCalculator with YAML config and per-fill computation (E2)"
```

### Step 1.3: Inject FeeCalculator into ExecutionNormalizer

- [ ] **Write integration test**

```python
# tests/unit/test_normalizer_fee_injection.py
"""Tests for FeeCalculator injection into ExecutionNormalizer."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.tca.fee_calculator import FeeCalculator
from hft_platform.tca.types import FeeBreakdown


class TestNormalizerFeeInjection:
    def test_fill_event_has_computed_fees(self) -> None:
        """After FeeCalculator injection, FillEvent.fee and .tax are non-zero."""
        schedules = {
            "TXF": {"commission_per_contract": 60, "tax_rate_bps": 2.0, "tax_side": "sell", "tick_size": 1, "point_value": 200},
        }
        calc = FeeCalculator(schedules)

        # Simulate what normalizer should do after computing fill
        result = calc.compute("TXF", side="S", qty=1, price_scaled=200_000_000)
        assert result.fee > 0 or result.commission > 0
        assert result.tax > 0  # Sell side has tax

    def test_fee_calculator_returns_zero_for_unknown(self) -> None:
        """Unknown symbols get zero fees — normalizer still works."""
        calc = FeeCalculator({})
        result = calc.compute("UNKNOWN", side="B", qty=1, price_scaled=100_000_000)
        assert result.total == 0
```

- [ ] **Modify ExecutionNormalizer**

In `src/hft_platform/execution/normalizer.py`:
1. Add `_fee_calculator` to `__slots__` and `__init__` (optional, default None)
2. In `normalize_fill()` (around line 198-211), after computing `scale_price`, compute fees:

```python
# After scale_price computation, before FillEvent construction:
fee_scaled = 0
tax_scaled = 0
if self._fee_calculator is not None:
    breakdown = self._fee_calculator.compute(
        sym, side=side_str, qty=qty, price_scaled=scale_price
    )
    fee_scaled = breakdown.commission
    tax_scaled = breakdown.tax

return FillEvent(
    ...
    fee=fee_scaled,
    tax=tax_scaled,
    ...
)
```

Where `side_str` is `"B"` or `"S"` derived from the `side` variable already in scope.

- [ ] **Wire FeeCalculator in bootstrap.py**

Create FeeCalculator from `config/base/fees/futures.yaml` and pass to ExecutionNormalizer:

```python
# In bootstrap.py, service creation section:
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

Pass `fee_calculator` to ExecutionNormalizer constructor.

- [ ] **Run tests**

Run: `uv run pytest tests/unit/test_fee_calculator.py tests/unit/test_normalizer_fee_injection.py -v --no-header --no-cov`
Expected: All tests PASS

- [ ] **Commit**

```bash
git add src/hft_platform/execution/normalizer.py src/hft_platform/services/bootstrap.py tests/unit/test_normalizer_fee_injection.py
git commit -m "feat(tca): inject FeeCalculator into ExecutionNormalizer (E2)"
```

---

## Task 2: E1 — TCA Analysis Engine

Creates `TCAAnalyzer` that queries ClickHouse for daily fill data and produces `TCADailyReport` aggregates. Also adds a `hft tca daily` CLI command.

**Files:**
- Create: `src/hft_platform/tca/analyzer.py`
- Create: `src/hft_platform/cli/_tca.py`
- Modify: `src/hft_platform/cli/_parser.py` (add tca subcommand)
- Test: `tests/unit/test_tca_analyzer.py`

### Step 2.1: Write failing tests for TCAAnalyzer

- [ ] **Create test file**

```python
# tests/unit/test_tca_analyzer.py
"""Tests for TCAAnalyzer — daily TCA report generation from ClickHouse data."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.tca.analyzer import TCAAnalyzer
from hft_platform.tca.types import TCADailyReport


@dataclass(slots=True)
class FakeCHClient:
    """Fake ClickHouse client returning canned results."""
    rows: list[tuple] = field(default_factory=list)
    column_names: list[str] = field(default_factory=list)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        result = MagicMock()
        result.result_rows = self.rows
        result.column_names = self.column_names
        return result


class TestTCAAnalyzer:
    def _make_analyzer(
        self,
        rows: list[tuple] | None = None,
    ) -> TCAAnalyzer:
        ch = FakeCHClient(
            rows=rows or [],
            column_names=["strategy_id", "symbol", "trade_count", "volume",
                          "notional_scaled", "total_commission", "total_tax"],
        )
        return TCAAnalyzer(ch_client=ch)

    def test_daily_report_aggregates_fills(self) -> None:
        """Produces TCADailyReport from fill aggregates."""
        rows = [
            ("mm1", "TXF", 10, 20, 4_000_000_000, 12_000_000, 800_000),
        ]
        analyzer = self._make_analyzer(rows=rows)
        reports = analyzer.daily_report("2026-03-25")

        assert len(reports) == 1
        r = reports[0]
        assert r.strategy == "mm1"
        assert r.symbol == "TXF"
        assert r.trade_count == 10
        assert r.volume == 20

    def test_empty_day_returns_empty_list(self) -> None:
        """Zero-fill day produces empty report list."""
        analyzer = self._make_analyzer(rows=[])
        reports = analyzer.daily_report("2026-03-25")
        assert reports == []

    def test_multiple_strategies(self) -> None:
        """Multiple strategies produce separate reports."""
        rows = [
            ("mm1", "TXF", 5, 10, 2_000_000_000, 6_000_000, 400_000),
            ("alpha1", "MXF", 3, 6, 600_000_000, 1_800_000, 120_000),
        ]
        analyzer = self._make_analyzer(rows=rows)
        reports = analyzer.daily_report("2026-03-25")
        assert len(reports) == 2
        strategies = {r.strategy for r in reports}
        assert strategies == {"mm1", "alpha1"}

    def test_ch_failure_returns_empty(self) -> None:
        """ClickHouse failure returns empty list, does not crash."""
        analyzer = self._make_analyzer()
        analyzer._ch_client = MagicMock()
        analyzer._ch_client.query.side_effect = Exception("connection refused")
        reports = analyzer.daily_report("2026-03-25")
        assert reports == []

    def test_report_has_cost_bps(self) -> None:
        """Reports include commission_bps_mean calculated from notional."""
        rows = [
            ("mm1", "TXF", 10, 20, 4_000_000_000, 12_000_000, 800_000),
        ]
        analyzer = self._make_analyzer(rows=rows)
        reports = analyzer.daily_report("2026-03-25")
        r = reports[0]
        # commission_bps = total_commission / notional * 10000
        assert r.commission_bps_mean > 0
        assert r.tax_bps_mean > 0
```

- [ ] **Run tests to verify they fail**

### Step 2.2: Implement TCAAnalyzer

- [ ] **Create the module**

```python
# src/hft_platform/tca/analyzer.py
"""TCAAnalyzer — daily TCA report generation from ClickHouse fill data."""
from __future__ import annotations

from typing import Any

import structlog

from hft_platform.tca.types import TCADailyReport

logger = structlog.get_logger(__name__)

_DAILY_FILL_SQL = """\
SELECT
    strategy_id,
    symbol,
    count(*) AS trade_count,
    sum(qty) AS volume,
    sum(price_scaled * qty) AS notional_scaled,
    sum(fee) AS total_commission,
    sum(tax) AS total_tax
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
        """Generate per-(strategy, symbol) TCA reports for a given date."""
        try:
            result = self._ch_client.query(
                _DAILY_FILL_SQL,
                parameters={"date": date_str},
            )
            rows = getattr(result, "result_rows", None) or []
        except Exception:
            logger.warning("tca.daily_report_query_failed", exc_info=True)
            return []

        reports: list[TCADailyReport] = []
        for row in rows:
            strategy = str(row[0])
            symbol = str(row[1])
            trade_count = int(row[2])
            volume = int(row[3])
            notional = int(row[4])
            total_commission = int(row[5])
            total_tax = int(row[6])

            # Compute bps (float — offline analysis only)
            if notional > 0:
                comm_bps = total_commission / notional * 10000
                tax_bps = total_tax / notional * 10000
                total_cost_bps = (total_commission + total_tax) / notional * 10000
            else:
                comm_bps = tax_bps = total_cost_bps = 0.0

            reports.append(TCADailyReport(
                date=date_str,
                strategy=strategy,
                symbol=symbol,
                trade_count=trade_count,
                volume=volume,
                notional=notional,
                commission_bps_mean=comm_bps,
                tax_bps_mean=tax_bps,
                delay_cost_bps_mean=0.0,  # TODO: from slippage_records
                delay_cost_bps_p95=0.0,
                exec_cost_bps_mean=0.0,
                exec_cost_bps_p95=0.0,
                impact_bps_mean=0.0,
                total_cost_bps_mean=total_cost_bps,
                total_cost_bps_p95=0.0,  # TODO: needs per-fill data
            ))

        logger.info("tca.daily_report_generated", date=date_str, count=len(reports))
        return reports
```

- [ ] **Run tests**

Run: `uv run pytest tests/unit/test_tca_analyzer.py -v --no-header --no-cov`
Expected: All 5 tests PASS

- [ ] **Commit**

```bash
git add src/hft_platform/tca/analyzer.py tests/unit/test_tca_analyzer.py
git commit -m "feat(tca): add TCAAnalyzer for daily fill reports (E1)"
```

### Step 2.3: Add `hft tca daily` CLI command

- [ ] **Create CLI module**

```python
# src/hft_platform/cli/_tca.py
"""CLI commands for TCA analysis."""
from __future__ import annotations

import argparse
import datetime
import os

import structlog

logger = structlog.get_logger(__name__)


def cmd_tca_daily(args: argparse.Namespace) -> None:
    """Generate and print daily TCA report."""
    import clickhouse_connect  # noqa: PLC0415

    from hft_platform.tca.analyzer import TCAAnalyzer

    date_str = getattr(args, "date", None) or datetime.date.today().isoformat()  # date-label-ok

    ch_host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    ch_port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    ch_user = os.getenv("HFT_CLICKHOUSE_USER", "default")
    ch_password = os.getenv("HFT_CLICKHOUSE_PASSWORD", "")

    try:
        client = clickhouse_connect.get_client(
            host=ch_host, port=ch_port,
            username=ch_user, password=ch_password,
        )
    except Exception as exc:
        print(f"ClickHouse connection failed: {exc}")
        raise SystemExit(1) from exc

    analyzer = TCAAnalyzer(ch_client=client)
    reports = analyzer.daily_report(date_str)

    if not reports:
        print(f"No fills for {date_str}")
        return

    # Print table
    print(f"\nTCA Daily Report — {date_str}")
    print(f"{'Strategy':<12} {'Symbol':<8} {'Trades':>7} {'Volume':>8} {'Comm bps':>9} {'Tax bps':>8} {'Total bps':>10}")
    print("-" * 68)
    for r in reports:
        print(
            f"{r.strategy:<12} {r.symbol:<8} {r.trade_count:>7} {r.volume:>8} "
            f"{r.commission_bps_mean:>9.2f} {r.tax_bps_mean:>8.2f} {r.total_cost_bps_mean:>10.2f}"
        )
    print()
```

- [ ] **Register in parser**

In `src/hft_platform/cli/_parser.py`, add after existing subcommand registrations:

```python
# TCA subcommands
tca_parser = subparsers.add_parser("tca", help="Transaction Cost Analysis")
tca_sub = tca_parser.add_subparsers(dest="tca_cmd")
tca_daily = tca_sub.add_parser("daily", help="Daily TCA report")
tca_daily.add_argument("--date", default=None, help="Date (YYYY-MM-DD, default today)")
tca_daily.set_defaults(func=cmd_tca_daily)
```

Import `cmd_tca_daily` from `hft_platform.cli._tca`.

- [ ] **Commit**

```bash
git add src/hft_platform/cli/_tca.py src/hft_platform/cli/_parser.py
git commit -m "feat(tca): add hft tca daily CLI command (E1)"
```

---

## Task 3: O1 — PnL Attribution Panel in TUI Monitor

Adds a PnL attribution panel to the TUI monitor that shows per-strategy daily PnL breakdown (gross, fees, net) by querying ClickHouse.

**Files:**
- Create: `src/hft_platform/monitor/_pnl_panel.py`
- Modify: `src/hft_platform/monitor/_engine.py` (integrate PnL panel)
- Modify: `src/hft_platform/monitor/_renderer.py` (render PnL panel)
- Test: `tests/unit/test_pnl_panel.py`

### Step 3.1: Write failing tests for PnL panel data

- [ ] **Create test file**

```python
# tests/unit/test_pnl_panel.py
"""Tests for PnL attribution panel data fetching."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.monitor._pnl_panel import PnlPanelData, fetch_pnl_attribution


@dataclass(slots=True)
class FakeCHClient:
    rows: list[tuple] = field(default_factory=list)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        result = MagicMock()
        result.result_rows = self.rows
        return result


class TestPnlPanel:
    def test_fetch_returns_per_strategy_breakdown(self) -> None:
        """Query returns per-strategy gross/fees/net."""
        ch = FakeCHClient(rows=[
            ("mm1", "TXF", 10, 50_000_000, 6_000_000, 44_000_000),
            ("alpha1", "MXF", 3, 10_000_000, 1_800_000, 8_200_000),
        ])
        data = fetch_pnl_attribution(ch, "2026-03-25")
        assert len(data) == 2
        assert data[0].strategy == "mm1"
        assert data[0].gross_pnl_scaled == 50_000_000
        assert data[0].net_pnl_scaled == 44_000_000

    def test_fetch_empty_returns_empty_list(self) -> None:
        """No fills returns empty list."""
        ch = FakeCHClient(rows=[])
        data = fetch_pnl_attribution(ch, "2026-03-25")
        assert data == []

    def test_fetch_handles_ch_failure(self) -> None:
        """ClickHouse failure returns empty list."""
        ch = MagicMock()
        ch.query.side_effect = Exception("timeout")
        data = fetch_pnl_attribution(ch, "2026-03-25")
        assert data == []

    def test_total_net_sums_correctly(self) -> None:
        """Net PnL = gross - fees across all strategies."""
        ch = FakeCHClient(rows=[
            ("mm1", "TXF", 5, 30_000_000, 3_000_000, 27_000_000),
        ])
        data = fetch_pnl_attribution(ch, "2026-03-25")
        d = data[0]
        assert d.net_pnl_scaled == d.gross_pnl_scaled - d.total_fees_scaled
```

- [ ] **Run tests to verify they fail**

### Step 3.2: Implement PnL panel data layer

- [ ] **Create the module**

```python
# src/hft_platform/monitor/_pnl_panel.py
"""PnL attribution panel for the TUI monitor.

Queries ClickHouse for per-strategy daily PnL breakdown (gross, fees, net).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_PNL_ATTRIBUTION_SQL = """\
SELECT
    strategy_id,
    symbol,
    count(*) AS fill_count,
    sum(CASE WHEN side = 'B' THEN -price_scaled * qty ELSE price_scaled * qty END) AS gross_pnl_scaled,
    sum(fee + tax) AS total_fees_scaled,
    sum(CASE WHEN side = 'B' THEN -price_scaled * qty ELSE price_scaled * qty END) - sum(fee + tax) AS net_pnl_scaled
FROM hft.fills
WHERE toDate(toDateTime(ts_exchange / 1000000000)) = {date:String}
GROUP BY strategy_id, symbol
ORDER BY net_pnl_scaled DESC
"""


@dataclass(slots=True, frozen=True)
class PnlPanelData:
    """Per-(strategy, symbol) PnL attribution row."""

    strategy: str
    symbol: str
    fill_count: int
    gross_pnl_scaled: int
    total_fees_scaled: int
    net_pnl_scaled: int


def fetch_pnl_attribution(
    ch_client: Any,
    date_str: str,
) -> list[PnlPanelData]:
    """Query ClickHouse for daily PnL attribution."""
    try:
        result = ch_client.query(
            _PNL_ATTRIBUTION_SQL,
            parameters={"date": date_str},
        )
        rows = getattr(result, "result_rows", None) or []
    except Exception:
        logger.warning("pnl_panel.query_failed", exc_info=True)
        return []

    return [
        PnlPanelData(
            strategy=str(row[0]),
            symbol=str(row[1]),
            fill_count=int(row[2]),
            gross_pnl_scaled=int(row[3]),
            total_fees_scaled=int(row[4]),
            net_pnl_scaled=int(row[5]),
        )
        for row in rows
    ]


def render_pnl_table(data: list[PnlPanelData]) -> list[str]:
    """Render PnL attribution as text lines for TUI display.

    Returns list of pre-formatted strings ready for TUI panel.
    """
    if not data:
        return ["  No fills today"]

    lines: list[str] = []
    lines.append(
        f"  {'Strategy':<10} {'Symbol':<7} {'Fills':>5} "
        f"{'Gross':>10} {'Fees':>8} {'Net':>10}"
    )
    lines.append("  " + "-" * 54)

    total_gross = 0
    total_fees = 0
    total_net = 0

    for d in data:
        gross_ntd = d.gross_pnl_scaled / 10000
        fees_ntd = d.total_fees_scaled / 10000
        net_ntd = d.net_pnl_scaled / 10000
        total_gross += d.gross_pnl_scaled
        total_fees += d.total_fees_scaled
        total_net += d.net_pnl_scaled

        sign = "+" if net_ntd >= 0 else ""
        lines.append(
            f"  {d.strategy:<10} {d.symbol:<7} {d.fill_count:>5} "
            f"{gross_ntd:>+10,.0f} {fees_ntd:>8,.0f} {sign}{net_ntd:>9,.0f}"
        )

    lines.append("  " + "-" * 54)
    tg = total_gross / 10000
    tf = total_fees / 10000
    tn = total_net / 10000
    sign = "+" if tn >= 0 else ""
    lines.append(
        f"  {'TOTAL':<10} {'':<7} {'':>5} "
        f"{tg:>+10,.0f} {tf:>8,.0f} {sign}{tn:>9,.0f}"
    )
    return lines
```

- [ ] **Run tests**

Run: `uv run pytest tests/unit/test_pnl_panel.py -v --no-header --no-cov`
Expected: All 4 tests PASS

- [ ] **Commit**

```bash
git add src/hft_platform/monitor/_pnl_panel.py tests/unit/test_pnl_panel.py
git commit -m "feat(monitor): add PnL attribution panel data layer (O1)"
```

### Step 3.3: Integrate PnL panel into TUI engine

- [ ] **Add PnL panel to MonitorEngine**

In `src/hft_platform/monitor/_engine.py`:
1. Add import: `from hft_platform.monitor._pnl_panel import fetch_pnl_attribution, render_pnl_table`
2. Add `_pnl_lines` to `MonitorEngine.__slots__` and `_pnl_last_fetch_ns`
3. Initialize in `__init__`: `self._pnl_lines: list[str] = []`, `self._pnl_last_fetch_ns = 0`
4. In the poll loop (where health panel is updated), add PnL refresh every 60 seconds:

```python
# After health panel update, add PnL attribution refresh:
now_ns = timebase.now_ns()
if now_ns - self._pnl_last_fetch_ns > 60_000_000_000:  # Every 60s
    if self._data_source and hasattr(self._data_source, '_ch_client'):
        import datetime  # noqa: PLC0415
        date_str = datetime.date.today().isoformat()  # date-label-ok
        data = fetch_pnl_attribution(self._data_source._ch_client, date_str)
        self._pnl_lines = render_pnl_table(data)
    self._pnl_last_fetch_ns = now_ns
```

- [ ] **Add PnL section to renderer**

In `src/hft_platform/monitor/_renderer.py`, add a function to render PnL lines into the health panel or as a separate section below the main table:

```python
def build_pnl_section(pnl_lines: list[str], width: int) -> list[str]:
    """Build PnL attribution section for TUI output."""
    if not pnl_lines:
        return []
    lines = ["", " PnL Attribution:"]
    lines.extend(pnl_lines)
    return lines
```

Call this from the main render loop, appending after the health panel.

- [ ] **Run full monitor test suite**

Run: `uv run pytest tests/unit/test_pnl_panel.py -v --no-header --no-cov`
Expected: PASS

- [ ] **Commit**

```bash
git add src/hft_platform/monitor/_engine.py src/hft_platform/monitor/_renderer.py
git commit -m "feat(monitor): integrate PnL attribution panel into TUI (O1)"
```

---

## Summary

| Task | Item | Files Created | Files Modified | Tests |
|------|------|---------------|----------------|-------|
| 1 | E2 FeeCalculator | `tca/fee_calculator.py` | `execution/normalizer.py`, `bootstrap.py`, `tca/__init__.py` | 10 |
| 2 | E1 TCAAnalyzer | `tca/analyzer.py`, `cli/_tca.py` | `cli/_parser.py` | 5 |
| 3 | O1 PnL Panel | `monitor/_pnl_panel.py` | `monitor/_engine.py`, `monitor/_renderer.py` | 4 |
| **Total** | | **4 new files** | **6 modified** | **19 tests** |

**Exit criteria:**
- `uv run pytest tests/unit/test_fee_calculator.py tests/unit/test_normalizer_fee_injection.py tests/unit/test_tca_analyzer.py tests/unit/test_pnl_panel.py -v --no-cov` → ALL PASS
- `hft tca daily --date 2026-03-25` → prints per-strategy TCA breakdown
- TUI monitor shows PnL attribution section (refreshed every 60s)
- FillEvent.fee and .tax populated from FeeCalculator (not hardcoded 0)
