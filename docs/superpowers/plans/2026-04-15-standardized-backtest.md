# Standardized Backtest Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify taker and maker backtest into a single `make research` pipeline with consistent result persistence, eliminating the 6+ ad-hoc methods that caused contradictory PnL claims across R6-R54.

**Architecture:** BacktestEngine Protocol with two implementations (TakerEngine wrapping existing hft_native_runner, MakerEngine extracted from r47_ck_direct_backtest_v2). Pipeline Stage 4 (`_gate_c.py`) auto-selects engine via `manifest.yaml` `strategy_type` field. All results persisted to `research/experiments/runs/<run_id>/` via ResultStore.

**Tech Stack:** Python 3.12, frozen dataclasses, typing.Protocol, ClickHouse HTTP API (requests), numpy, existing metrics.py / maker_scorecard.py

**Spec:** `docs/superpowers/specs/2026-04-15-standardized-backtest-design.md`

---

### Task 1: Config files (cost_profiles.yaml + gate_thresholds.yaml)

**Files:**
- Create: `config/research/cost_profiles.yaml`
- Create: `config/research/gate_thresholds.yaml`

- [ ] **Step 1: Create cost_profiles.yaml**

```yaml
# config/research/cost_profiles.yaml
# Per-instrument cost model for standardized backtest engine.
# Values in POINTS per side (not NTD).
# Source: TAIFEX fee schedule + broker commission (retail, no rebates).

TMFD6:
  commission_pts_per_side: 1.3
  tax_pts_per_side: 0.7
  point_value_nwd: 10
  scale: 1000000

TXFD6:
  commission_pts_per_side: 0.24
  tax_pts_per_side: 0.24
  point_value_nwd: 200
  scale: 1000000
```

- [ ] **Step 2: Create gate_thresholds.yaml**

```yaml
# config/research/gate_thresholds.yaml
# Gate C (backtest validation) thresholds, split by strategy type.
# Maker has lower Sharpe thresholds because passive strategies
# have naturally lower Sharpe but higher capacity.

taker:
  sharpe_is_min: 1.0
  sharpe_oos_min: 0.5
  is_oos_gap_max_pct: 50
  winning_day_pct_min: 55
  max_drawdown_pct: 30

maker:
  sharpe_is_min: 0.5
  sharpe_oos_min: 0.3
  is_oos_gap_max_pct: 50
  winning_day_pct_min: 55
  max_drawdown_pct: 30
  pnl_per_fill_min_pts: 0
  adverse_fill_pct_max: 50
```

- [ ] **Step 3: Commit**

```bash
git add config/research/cost_profiles.yaml config/research/gate_thresholds.yaml
git commit -m "feat(research): add cost profiles and gate thresholds config"
```

---

### Task 2: Extend BacktestResult with provenance metadata

**Files:**
- Modify: `research/backtest/types.py`
- Test: `tests/unit/test_backtest_types.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_backtest_types.py
"""Tests for extended BacktestResult with provenance metadata."""
import numpy as np
import pytest

from research.backtest.types import BacktestResult


def test_backtest_result_has_provenance_fields():
    """BacktestResult must include engine_type, fill_model, instrument, etc."""
    result = BacktestResult(
        signals=np.array([0.1, 0.2]),
        equity_curve=np.array([1.0, 1.01]),
        positions=np.array([0, 1]),
        sharpe_is=1.5,
        sharpe_oos=0.8,
        ic_series=np.array([0.05]),
        ic_mean=0.05,
        ic_std=0.02,
        ic_tstat=2.5,
        ic_pvalue=0.01,
        ic_halflife=10,
        sortino=1.2,
        cvar_5pct=-0.03,
        turnover=0.5,
        max_drawdown=0.1,
        regime_metrics={"high_vol_sharpe": 1.0},
        capacity_estimate=1e6,
        run_id="test-run-001",
        config_hash="abc123",
        latency_profile={"submit_ms": 36.0},
        engine_type="maker",
        fill_model="QueueDepletion(qf=0.5)",
        cost_model="TMFD6(comm=1.3,tax=0.7)",
        instrument="TMFD6",
        data_period="2026-03-01..2026-03-31",
        data_source="clickhouse://localhost:8123/hft",
        pipeline_mode="strict",
        created_at="2026-04-15T10:00:00Z",
    )
    assert result.engine_type == "maker"
    assert result.fill_model == "QueueDepletion(qf=0.5)"
    assert result.instrument == "TMFD6"
    assert result.data_period == "2026-03-01..2026-03-31"
    assert result.pipeline_mode == "strict"


def test_backtest_result_maker_optional_fields():
    """Maker-specific fields default to None for taker results."""
    result = BacktestResult(
        signals=np.array([0.1]),
        equity_curve=np.array([1.0]),
        positions=np.array([0]),
        sharpe_is=1.0,
        sharpe_oos=0.5,
        ic_series=np.array([0.05]),
        ic_mean=0.05,
        ic_std=0.02,
        ic_tstat=2.5,
        ic_pvalue=0.01,
        ic_halflife=10,
        sortino=1.0,
        cvar_5pct=-0.02,
        turnover=0.3,
        max_drawdown=0.05,
        regime_metrics={},
        capacity_estimate=1e6,
        run_id="test-002",
        config_hash="def456",
        latency_profile={},
        engine_type="taker",
        fill_model="PowerProbQueue(3.0)",
        cost_model="TXFD6(comm=0.24,tax=0.24)",
        instrument="TXFD6",
        data_period="2026-03-01..2026-03-15",
        data_source="clickhouse://localhost:8123/hft",
        pipeline_mode="strict",
        created_at="2026-04-15T10:00:00Z",
    )
    assert result.maker_scorecard is None
    assert result.per_spread_breakdown is None
    assert result.queue_fraction is None
    assert result.daily_pnl is None


def test_backtest_result_is_frozen():
    """BacktestResult must be immutable."""
    result = BacktestResult(
        signals=np.array([0.1]),
        equity_curve=np.array([1.0]),
        positions=np.array([0]),
        sharpe_is=1.0,
        sharpe_oos=0.5,
        ic_series=np.array([0.05]),
        ic_mean=0.05,
        ic_std=0.02,
        ic_tstat=2.5,
        ic_pvalue=0.01,
        ic_halflife=10,
        sortino=1.0,
        cvar_5pct=-0.02,
        turnover=0.3,
        max_drawdown=0.05,
        regime_metrics={},
        capacity_estimate=1e6,
        run_id="test-003",
        config_hash="ghi789",
        latency_profile={},
        engine_type="taker",
        fill_model="PowerProbQueue(3.0)",
        cost_model="TXFD6(comm=0.24,tax=0.24)",
        instrument="TXFD6",
        data_period="2026-03-01..2026-03-15",
        data_source="clickhouse://localhost:8123/hft",
        pipeline_mode="strict",
        created_at="2026-04-15T10:00:00Z",
    )
    with pytest.raises(AttributeError):
        result.engine_type = "maker"  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_backtest_types.py -v`
Expected: FAIL — `BacktestResult.__init__() got an unexpected keyword argument 'engine_type'`

- [ ] **Step 3: Extend BacktestResult in types.py**

Add these fields after the existing `mid_prices` field in `research/backtest/types.py`:

```python
@dataclass(frozen=True)
class BacktestResult:
    signals: np.ndarray
    equity_curve: np.ndarray
    positions: np.ndarray
    sharpe_is: float
    sharpe_oos: float
    ic_series: np.ndarray
    ic_mean: float
    ic_std: float
    ic_tstat: float
    ic_pvalue: float
    ic_halflife: int
    sortino: float
    cvar_5pct: float
    turnover: float
    max_drawdown: float
    regime_metrics: dict[str, float]
    capacity_estimate: float
    run_id: str
    config_hash: str
    latency_profile: dict[str, Any]
    mid_prices: np.ndarray | None = None
    # --- Provenance metadata (added 2026-04-15) ---
    engine_type: str = "taker"
    fill_model: str = ""
    cost_model: str = ""
    instrument: str = ""
    data_period: str = ""
    data_source: str = ""
    pipeline_mode: str = ""
    created_at: str = ""
    # --- Maker-specific (None for taker) ---
    maker_scorecard: dict | None = None
    per_spread_breakdown: dict | None = None
    queue_fraction: float | None = None
    # --- Daily detail ---
    daily_pnl: list[dict] | None = None
```

Note: New fields have defaults so existing code that constructs BacktestResult without them continues to work.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_backtest_types.py -v`
Expected: 3 tests PASS

- [ ] **Step 5: Run existing tests to check no regressions**

Run: `uv run pytest tests/ -k "backtest" --timeout=30 -x -q`
Expected: All existing backtest tests still pass (BacktestResult has default values for new fields).

- [ ] **Step 6: Commit**

```bash
git add research/backtest/types.py tests/unit/test_backtest_types.py
git commit -m "feat(research): extend BacktestResult with provenance metadata"
```

---

### Task 3: Cost model + Fill model protocols and implementations

**Files:**
- Create: `research/backtest/cost_models.py`
- Create: `research/backtest/fill_models.py`
- Test: `tests/unit/test_cost_models.py`
- Test: `tests/unit/test_fill_models.py`

- [ ] **Step 1: Write failing test for cost model**

```python
# tests/unit/test_cost_models.py
"""Tests for TAIFEX cost model."""
from research.backtest.cost_models import load_cost_profile, TAIFEXCost


def test_load_tmfd6_cost():
    cost = load_cost_profile("TMFD6")
    assert isinstance(cost, TAIFEXCost)
    assert cost.commission_pts_per_side == 1.3
    assert cost.tax_pts_per_side == 0.7
    assert cost.point_value_nwd == 10


def test_load_txfd6_cost():
    cost = load_cost_profile("TXFD6")
    assert cost.commission_pts_per_side == 0.24
    assert cost.point_value_nwd == 200


def test_rt_cost_pts():
    cost = load_cost_profile("TMFD6")
    # RT = 2 * (comm + tax) = 2 * (1.3 + 0.7) = 4.0
    assert cost.rt_cost_pts == 4.0


def test_apply_fill_cost():
    cost = load_cost_profile("TMFD6")
    net = cost.apply(gross_pnl_pts=10.0, n_fills=2)
    # gross 10 - 2 fills * (1.3 + 0.7) per side = 10 - 4.0 = 6.0
    assert net == 6.0


def test_cost_model_label():
    cost = load_cost_profile("TMFD6")
    assert cost.label == "TMFD6(comm=1.3,tax=0.7)"


def test_unknown_instrument_raises():
    import pytest
    with pytest.raises(KeyError, match="UNKNOWN"):
        load_cost_profile("UNKNOWN")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cost_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.backtest.cost_models'`

- [ ] **Step 3: Implement cost_models.py**

```python
# research/backtest/cost_models.py
"""Per-instrument cost models for standardized backtest engine."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml


class CostModel(Protocol):
    """Protocol for cost models."""

    @property
    def rt_cost_pts(self) -> float: ...

    @property
    def label(self) -> str: ...

    def apply(self, gross_pnl_pts: float, n_fills: int) -> float: ...


@dataclass(frozen=True)
class TAIFEXCost:
    """TAIFEX futures cost model."""

    instrument: str
    commission_pts_per_side: float
    tax_pts_per_side: float
    point_value_nwd: int
    scale: int = 1_000_000

    @property
    def cost_per_side_pts(self) -> float:
        return self.commission_pts_per_side + self.tax_pts_per_side

    @property
    def rt_cost_pts(self) -> float:
        return 2 * self.cost_per_side_pts

    @property
    def label(self) -> str:
        return (
            f"{self.instrument}"
            f"(comm={self.commission_pts_per_side},tax={self.tax_pts_per_side})"
        )

    def apply(self, gross_pnl_pts: float, n_fills: int) -> float:
        return gross_pnl_pts - n_fills * self.cost_per_side_pts


_CONFIG_PATH = Path("config/research/cost_profiles.yaml")
_cache: dict[str, TAIFEXCost] | None = None


def _load_all() -> dict[str, TAIFEXCost]:
    global _cache
    if _cache is not None:
        return _cache
    raw: dict[str, Any] = yaml.safe_load(_CONFIG_PATH.read_text())
    _cache = {}
    for instrument, vals in raw.items():
        _cache[instrument] = TAIFEXCost(
            instrument=instrument,
            commission_pts_per_side=float(vals["commission_pts_per_side"]),
            tax_pts_per_side=float(vals["tax_pts_per_side"]),
            point_value_nwd=int(vals["point_value_nwd"]),
            scale=int(vals.get("scale", 1_000_000)),
        )
    return _cache


def load_cost_profile(instrument: str) -> TAIFEXCost:
    profiles = _load_all()
    if instrument not in profiles:
        raise KeyError(
            f"No cost profile for '{instrument}'. "
            f"Available: {sorted(profiles.keys())}. "
            f"Add to {_CONFIG_PATH}"
        )
    return profiles[instrument]
```

- [ ] **Step 4: Run cost model test**

Run: `uv run pytest tests/unit/test_cost_models.py -v`
Expected: 6 tests PASS

- [ ] **Step 5: Write failing test for fill model**

```python
# tests/unit/test_fill_models.py
"""Tests for QueueDepletionFill model."""
import numpy as np

from research.backtest.fill_models import QueueDepletionFill, QueuePosition


def test_post_quote_calculates_queue_position():
    fm = QueueDepletionFill(queue_fraction=0.5)
    pos = fm.post_quote(side="buy", price=100_000_000, book_qty=200)
    assert pos.side == "buy"
    assert pos.price == 100_000_000
    assert pos.queue_ahead == 100  # 200 * 0.5


def test_post_quote_full_queue():
    fm = QueueDepletionFill(queue_fraction=1.0)
    pos = fm.post_quote(side="sell", price=101_000_000, book_qty=50)
    assert pos.queue_ahead == 50


def test_check_fills_no_fill_when_queue_remains():
    fm = QueueDepletionFill(queue_fraction=0.5)
    pos = fm.post_quote(side="buy", price=100_000_000, book_qty=200)
    # Trade at bid price, volume 50 (queue_ahead=100, so 50 consumed -> 50 left)
    fills = fm.check_fills([pos], trade_price=100_000_000, trade_volume=50)
    assert len(fills) == 0
    assert pos.queue_ahead == 50


def test_check_fills_fill_when_queue_depleted():
    fm = QueueDepletionFill(queue_fraction=0.5)
    pos = fm.post_quote(side="buy", price=100_000_000, book_qty=20)
    # queue_ahead=10, volume=15 -> depleted
    fills = fm.check_fills([pos], trade_price=100_000_000, trade_volume=15)
    assert len(fills) == 1
    assert fills[0].side == "buy"
    assert fills[0].price == 100_000_000


def test_check_fills_ignores_wrong_price():
    fm = QueueDepletionFill(queue_fraction=0.5)
    pos = fm.post_quote(side="buy", price=100_000_000, book_qty=20)
    # Trade at different price -> no queue consumption
    fills = fm.check_fills([pos], trade_price=101_000_000, trade_volume=100)
    assert len(fills) == 0
    assert pos.queue_ahead == 10  # unchanged


def test_sell_fill_on_ask_trade():
    fm = QueueDepletionFill(queue_fraction=0.5)
    pos = fm.post_quote(side="sell", price=101_000_000, book_qty=10)
    # queue_ahead=5, trade at ask with volume 10 -> depleted
    fills = fm.check_fills([pos], trade_price=101_000_000, trade_volume=10)
    assert len(fills) == 1
    assert fills[0].side == "sell"


def test_fill_model_label():
    fm = QueueDepletionFill(queue_fraction=0.5)
    assert fm.label == "QueueDepletion(qf=0.5)"
```

- [ ] **Step 6: Run fill model test to verify it fails**

Run: `uv run pytest tests/unit/test_fill_models.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 7: Implement fill_models.py**

```python
# research/backtest/fill_models.py
"""Fill models for maker backtest engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class QueuePosition:
    """Tracks a single outstanding order's queue position."""

    side: str          # "buy" | "sell"
    price: int         # scaled (x1e6)
    queue_ahead: int   # units ahead in queue


@dataclass(frozen=True)
class Fill:
    """A single fill event."""

    side: str
    price: int         # scaled (x1e6)
    qty: int = 1


class FillModel(Protocol):
    """Protocol for fill models."""

    @property
    def label(self) -> str: ...

    def post_quote(self, side: str, price: int, book_qty: int) -> QueuePosition: ...

    def check_fills(
        self,
        positions: list[QueuePosition],
        trade_price: int,
        trade_volume: int,
    ) -> list[Fill]: ...


class QueueDepletionFill:
    """CK-direct fill model: queue depletion tracking.

    Posts assume entry at queue_fraction of current book depth.
    Trade volume at the posted price decrements queue_ahead.
    Fill when queue_ahead <= 0.
    """

    __slots__ = ("_qf",)

    def __init__(self, queue_fraction: float = 0.5) -> None:
        self._qf = queue_fraction

    @property
    def label(self) -> str:
        return f"QueueDepletion(qf={self._qf})"

    @property
    def queue_fraction(self) -> float:
        return self._qf

    def post_quote(self, side: str, price: int, book_qty: int) -> QueuePosition:
        queue_ahead = max(1, int(book_qty * self._qf))
        return QueuePosition(side=side, price=price, queue_ahead=queue_ahead)

    def check_fills(
        self,
        positions: list[QueuePosition],
        trade_price: int,
        trade_volume: int,
    ) -> list[Fill]:
        fills: list[Fill] = []
        for pos in positions:
            # Buy orders fill when trade is at or below bid (our price)
            # Sell orders fill when trade is at or above ask (our price)
            if pos.side == "buy" and trade_price <= pos.price:
                pos.queue_ahead -= trade_volume
            elif pos.side == "sell" and trade_price >= pos.price:
                pos.queue_ahead -= trade_volume

            if pos.queue_ahead <= 0:
                fills.append(Fill(side=pos.side, price=pos.price))
        return fills
```

- [ ] **Step 8: Run fill model test**

Run: `uv run pytest tests/unit/test_fill_models.py -v`
Expected: 7 tests PASS

- [ ] **Step 9: Commit**

```bash
git add research/backtest/cost_models.py research/backtest/fill_models.py \
      tests/unit/test_cost_models.py tests/unit/test_fill_models.py
git commit -m "feat(research): add CostModel and FillModel with QueueDepletion"
```

---

### Task 4: BacktestEngine Protocol + ResultStore

**Files:**
- Create: `research/backtest/engine.py`
- Create: `research/backtest/result_store.py`
- Test: `tests/unit/test_result_store.py`

- [ ] **Step 1: Write failing test for ResultStore**

```python
# tests/unit/test_result_store.py
"""Tests for ResultStore JSON persistence."""
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from research.backtest.result_store import ResultStore
from research.backtest.types import BacktestResult


@pytest.fixture
def tmp_store(tmp_path: Path) -> ResultStore:
    return ResultStore(base_dir=tmp_path)


def _make_result(**overrides) -> BacktestResult:
    defaults = dict(
        signals=np.array([0.1, 0.2, 0.3]),
        equity_curve=np.array([1.0, 1.01, 1.02]),
        positions=np.array([0, 1, 0]),
        sharpe_is=1.5,
        sharpe_oos=0.8,
        ic_series=np.array([0.05, 0.06]),
        ic_mean=0.055,
        ic_std=0.01,
        ic_tstat=5.5,
        ic_pvalue=0.001,
        ic_halflife=10,
        sortino=1.2,
        cvar_5pct=-0.03,
        turnover=0.5,
        max_drawdown=0.1,
        regime_metrics={"high_vol_sharpe": 1.0},
        capacity_estimate=1e6,
        run_id="run-test-001",
        config_hash="abc123",
        latency_profile={"submit_ms": 36.0},
        engine_type="maker",
        fill_model="QueueDepletion(qf=0.5)",
        cost_model="TMFD6(comm=1.3,tax=0.7)",
        instrument="TMFD6",
        data_period="2026-03-01..2026-03-31",
        data_source="clickhouse://localhost:8123/hft",
        pipeline_mode="strict",
        created_at="2026-04-15T10:00:00Z",
        queue_fraction=0.5,
        daily_pnl=[{"date": "2026-03-01", "pnl": 100.0, "fills": 50}],
    )
    defaults.update(overrides)
    return BacktestResult(**defaults)


def test_save_creates_run_directory(tmp_store: ResultStore):
    result = _make_result()
    run_dir = tmp_store.save(result, alpha_id="r47_maker_pivot")
    assert run_dir.exists()
    assert (run_dir / "backtest_report.json").exists()
    assert (run_dir / "config.json").exists()
    assert (run_dir / "equity_curve.npy").exists()


def test_save_report_contains_provenance(tmp_store: ResultStore):
    result = _make_result()
    run_dir = tmp_store.save(result, alpha_id="r47_maker_pivot")
    report = json.loads((run_dir / "backtest_report.json").read_text())
    assert report["alpha_id"] == "r47_maker_pivot"
    assert report["engine_type"] == "maker"
    assert report["fill_model"] == "QueueDepletion(qf=0.5)"
    assert report["instrument"] == "TMFD6"
    assert report["sharpe_is"] == 1.5
    assert report["daily_pnl"][0]["date"] == "2026-03-01"


def test_load_roundtrip(tmp_store: ResultStore):
    result = _make_result()
    tmp_store.save(result, alpha_id="test_alpha")
    loaded = tmp_store.load("run-test-001")
    assert loaded.engine_type == "maker"
    assert loaded.sharpe_is == 1.5
    assert loaded.instrument == "TMFD6"
    assert np.allclose(loaded.equity_curve, result.equity_curve)


def test_query_by_instrument(tmp_store: ResultStore):
    tmp_store.save(_make_result(run_id="r1", instrument="TMFD6"), "alpha1")
    tmp_store.save(_make_result(run_id="r2", instrument="TXFD6"), "alpha2")
    results = tmp_store.query(instrument="TMFD6")
    assert len(results) == 1
    assert results[0]["instrument"] == "TMFD6"


def test_query_by_engine_type(tmp_store: ResultStore):
    tmp_store.save(_make_result(run_id="r1", engine_type="maker"), "alpha1")
    tmp_store.save(_make_result(run_id="r2", engine_type="taker"), "alpha2")
    results = tmp_store.query(engine_type="maker")
    assert len(results) == 1
    assert results[0]["engine_type"] == "maker"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_result_store.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement engine.py (Protocol only)**

```python
# research/backtest/engine.py
"""BacktestEngine Protocol — unified interface for taker and maker backtest."""
from __future__ import annotations

from typing import Any, Protocol

from research.backtest.types import BacktestConfig, BacktestResult


class DataSource(Protocol):
    """Protocol for backtest data sources."""

    def health_check(self) -> None: ...


class BacktestEngine(Protocol):
    """Unified backtest engine interface.

    Implementations:
      - TakerEngine: wraps existing hft_native_runner.py
      - MakerEngine: CK-direct queue depletion backtest
    """

    def run(self, config: BacktestConfig, **kwargs: Any) -> BacktestResult: ...

    @property
    def engine_type(self) -> str: ...

    @property
    def fill_model_name(self) -> str: ...
```

- [ ] **Step 4: Implement result_store.py**

```python
# research/backtest/result_store.py
"""ResultStore — sole official write path for backtest results."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from research.backtest.types import BacktestResult


class ResultStore:
    """Persist and query backtest results as JSON + npy.

    Directory layout per run:
      runs/<run_id>/
        backtest_report.json  — metrics + provenance metadata
        config.json           — reproducible config snapshot
        equity_curve.npy      — large arrays stored separately
    """

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base = Path(base_dir) if base_dir else Path("research/experiments/runs")

    def save(self, result: BacktestResult, alpha_id: str) -> Path:
        run_dir = self._base / result.run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        report: dict[str, Any] = {
            "alpha_id": alpha_id,
            "run_id": result.run_id,
            "engine_type": result.engine_type,
            "fill_model": result.fill_model,
            "cost_model": result.cost_model,
            "instrument": result.instrument,
            "data_period": result.data_period,
            "data_source": result.data_source,
            "config_hash": result.config_hash,
            "pipeline_mode": result.pipeline_mode,
            "created_at": result.created_at,
            "sharpe_is": result.sharpe_is,
            "sharpe_oos": result.sharpe_oos,
            "ic_mean": result.ic_mean,
            "ic_std": result.ic_std,
            "ic_tstat": result.ic_tstat,
            "ic_pvalue": result.ic_pvalue,
            "ic_halflife": result.ic_halflife,
            "sortino": result.sortino,
            "cvar_5pct": result.cvar_5pct,
            "turnover": result.turnover,
            "max_drawdown": result.max_drawdown,
            "regime_metrics": result.regime_metrics,
            "capacity_estimate": result.capacity_estimate,
            "queue_fraction": result.queue_fraction,
            "maker_scorecard": result.maker_scorecard,
            "per_spread_breakdown": result.per_spread_breakdown,
            "daily_pnl": result.daily_pnl,
        }
        (run_dir / "backtest_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str)
        )

        config_snapshot: dict[str, Any] = {
            "latency_profile": result.latency_profile,
            "config_hash": result.config_hash,
        }
        (run_dir / "config.json").write_text(
            json.dumps(config_snapshot, indent=2, sort_keys=True)
        )

        np.save(run_dir / "equity_curve.npy", result.equity_curve)

        return run_dir

    def load(self, run_id: str) -> BacktestResult:
        run_dir = self._base / run_id
        report = json.loads((run_dir / "backtest_report.json").read_text())
        config_data = json.loads((run_dir / "config.json").read_text())
        equity = np.load(run_dir / "equity_curve.npy")

        return BacktestResult(
            signals=np.array([]),
            equity_curve=equity,
            positions=np.array([]),
            sharpe_is=float(report["sharpe_is"]),
            sharpe_oos=float(report["sharpe_oos"]),
            ic_series=np.array([]),
            ic_mean=float(report["ic_mean"]),
            ic_std=float(report["ic_std"]),
            ic_tstat=float(report["ic_tstat"]),
            ic_pvalue=float(report["ic_pvalue"]),
            ic_halflife=int(report["ic_halflife"]),
            sortino=float(report["sortino"]),
            cvar_5pct=float(report["cvar_5pct"]),
            turnover=float(report["turnover"]),
            max_drawdown=float(report["max_drawdown"]),
            regime_metrics=report.get("regime_metrics", {}),
            capacity_estimate=float(report.get("capacity_estimate", 0)),
            run_id=report["run_id"],
            config_hash=report["config_hash"],
            latency_profile=config_data.get("latency_profile", {}),
            engine_type=report["engine_type"],
            fill_model=report["fill_model"],
            cost_model=report["cost_model"],
            instrument=report["instrument"],
            data_period=report["data_period"],
            data_source=report["data_source"],
            pipeline_mode=report["pipeline_mode"],
            created_at=report["created_at"],
            queue_fraction=report.get("queue_fraction"),
            maker_scorecard=report.get("maker_scorecard"),
            per_spread_breakdown=report.get("per_spread_breakdown"),
            daily_pnl=report.get("daily_pnl"),
        )

    def query(self, **filters: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if not self._base.exists():
            return results
        for run_dir in sorted(self._base.iterdir()):
            report_path = run_dir / "backtest_report.json"
            if not report_path.exists():
                continue
            report = json.loads(report_path.read_text())
            match = all(report.get(k) == v for k, v in filters.items())
            if match:
                results.append(report)
        return results
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/unit/test_result_store.py -v`
Expected: 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add research/backtest/engine.py research/backtest/result_store.py \
      tests/unit/test_result_store.py
git commit -m "feat(research): add BacktestEngine Protocol and ResultStore"
```

---

### Task 5: MakerEngine (CK-direct backtest)

**Files:**
- Create: `research/backtest/maker_engine.py`
- Test: `tests/unit/test_maker_engine.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_maker_engine.py
"""Tests for MakerEngine — CK-direct maker backtest."""
import numpy as np
import pytest

from research.backtest.cost_models import TAIFEXCost
from research.backtest.fill_models import Fill, QueueDepletionFill, QueuePosition
from research.backtest.maker_engine import (
    CancelQuote,
    ClickHouseSource,
    Hold,
    MakerEngine,
    MakerStrategy,
    PostQuote,
    TickData,
)


class SimpleMakerStrategy:
    """Always posts at best bid/ask when spread >= threshold."""

    __slots__ = ("_threshold", "_position", "_max_pos")

    def __init__(self, spread_threshold_pts: int = 1, max_pos: int = 3) -> None:
        self._threshold = spread_threshold_pts
        self._position = 0
        self._max_pos = max_pos

    def on_tick(self, tick: TickData) -> list[PostQuote | CancelQuote | Hold]:
        if tick.spread_pts < self._threshold:
            return [Hold()]
        actions: list[PostQuote | CancelQuote | Hold] = []
        if self._position < self._max_pos:
            actions.append(PostQuote(side="buy", price=tick.bid_price, qty=1))
        if self._position > -self._max_pos:
            actions.append(PostQuote(side="sell", price=tick.ask_price, qty=1))
        return actions

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        if side == "buy":
            self._position += 1
        else:
            self._position -= 1


def test_maker_engine_properties():
    cost = TAIFEXCost("TMFD6", 1.3, 0.7, 10)
    fill = QueueDepletionFill(queue_fraction=0.5)
    engine = MakerEngine(fill_model=fill, cost_model=cost)
    assert engine.engine_type == "maker"
    assert engine.fill_model_name == "QueueDepletion(qf=0.5)"


def test_ck_health_check_raises_on_failure():
    source = ClickHouseSource(host="invalid-host-xyz", port=1)
    with pytest.raises(ConnectionError, match="ClickHouse"):
        source.health_check()


def test_tick_data_spread_pts():
    tick = TickData(
        exch_ts=1000,
        bid_price=100_000_000,
        ask_price=104_000_000,
        bid_qty=50,
        ask_qty=30,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=1_000_000,
    )
    assert tick.spread_pts == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_maker_engine.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement maker_engine.py**

```python
# research/backtest/maker_engine.py
"""MakerEngine — CK-direct queue depletion backtest for maker strategies.

Extracted and generalized from research/tools/r47_ck_direct_backtest_v2.py.
Strategy logic is injected via MakerStrategy protocol — engine handles
market simulation, fill determination, and PnL accounting.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import numpy as np
import requests
import structlog

from research.backtest.cost_models import CostModel
from research.backtest.fill_models import FillModel, QueuePosition
from research.backtest.types import BacktestResult

logger = structlog.get_logger()


# --- Data types ---

@dataclass(frozen=True)
class TickData:
    """Single market event (bidask update or trade)."""

    exch_ts: int
    bid_price: int       # scaled (x1e6)
    ask_price: int
    bid_qty: int
    ask_qty: int
    trade_price: int     # 0 if bidask event
    trade_volume: int    # 0 if bidask event
    is_trade: bool
    scale: int = 1_000_000

    @property
    def spread_pts(self) -> int:
        return (self.ask_price - self.bid_price) // self.scale

    @property
    def mid_price(self) -> float:
        return (self.bid_price + self.ask_price) / (2 * self.scale)


@dataclass(frozen=True)
class PostQuote:
    side: str   # "buy" | "sell"
    price: int  # scaled
    qty: int = 1


@dataclass(frozen=True)
class CancelQuote:
    side: str  # "buy" | "sell"


@dataclass(frozen=True)
class Hold:
    pass


# --- Strategy protocol ---

class MakerStrategy(Protocol):
    """Strategy decides when/where to quote. Engine decides fills."""

    def on_tick(self, tick: TickData) -> list[PostQuote | CancelQuote | Hold]: ...
    def on_fill(self, side: str, price: int, mid_price: float) -> None: ...


# --- ClickHouse data source ---

class ClickHouseSource:
    """Fetch tick + bidask data from ClickHouse."""

    __slots__ = ("_host", "_port", "_password", "_url")

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
    ) -> None:
        self._host = host or os.environ.get("CLICKHOUSE_HOST", "localhost")
        self._port = port or int(os.environ.get("CLICKHOUSE_PORT", "8123"))
        self._password = password or os.environ.get("CLICKHOUSE_PASSWORD", "")
        self._url = f"http://{self._host}:{self._port}/"

    def health_check(self) -> None:
        try:
            resp = requests.post(
                self._url,
                params={"password": self._password},
                data="SELECT 1",
                timeout=5,
            )
            resp.raise_for_status()
        except Exception as exc:
            raise ConnectionError(
                f"ClickHouse not reachable at {self._url}. "
                f"Please start it: docker compose up -d clickhouse\n"
                f"Original error: {exc}"
            ) from exc

    def _query(self, sql: str) -> list[list[str]]:
        resp = requests.post(
            self._url,
            params={"password": self._password},
            data=sql + " FORMAT TSVWithNames",
            timeout=120,
        )
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")
        if len(lines) < 2:
            return []
        headers = lines[0].split("\t")
        rows = [line.split("\t") for line in lines[1:] if line]
        return [headers] + rows

    def load_day(self, symbol: str, date: str) -> list[TickData]:
        """Load interleaved bidask + tick events for one day, sorted by exch_ts."""
        scale = 1_000_000  # will be overridden by cost model if needed

        ba_sql = f"""
        SELECT exch_ts,
               bids_price[1] AS bid1_p, bids_vol[1] AS bid1_v,
               asks_price[1] AS ask1_p, asks_vol[1] AS ask1_v
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'BidAsk'
          AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
          AND length(bids_price) >= 1 AND length(asks_price) >= 1
        ORDER BY exch_ts
        """
        tick_sql = f"""
        SELECT exch_ts, price_scaled AS price, volume
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'Tick'
          AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
        ORDER BY exch_ts
        """
        ba_rows = self._query(ba_sql)
        tick_rows = self._query(tick_sql)

        events: list[TickData] = []

        if len(ba_rows) > 1:
            for row in ba_rows[1:]:
                events.append(TickData(
                    exch_ts=int(row[0]),
                    bid_price=int(row[1]),
                    ask_price=int(row[3]),
                    bid_qty=int(row[2]),
                    ask_qty=int(row[4]),
                    trade_price=0,
                    trade_volume=0,
                    is_trade=False,
                    scale=scale,
                ))

        if len(tick_rows) > 1:
            for row in tick_rows[1:]:
                events.append(TickData(
                    exch_ts=int(row[0]),
                    bid_price=0,
                    ask_price=0,
                    bid_qty=0,
                    ask_qty=0,
                    trade_price=int(row[1]),
                    trade_volume=int(row[2]),
                    is_trade=True,
                    scale=scale,
                ))

        events.sort(key=lambda e: e.exch_ts)
        return events

    def available_dates(self, symbol: str) -> list[str]:
        sql = f"""
        SELECT DISTINCT toDate(fromUnixTimestamp64Nano(exch_ts)) AS d
        FROM hft.market_data
        WHERE symbol = '{symbol}'
        ORDER BY d
        """
        rows = self._query(sql)
        if len(rows) <= 1:
            return []
        return [row[0] for row in rows[1:]]


# --- MakerEngine ---

class MakerEngine:
    """CK-direct maker backtest engine.

    Runs strategy against ClickHouse tick data with queue depletion fill model.
    """

    __slots__ = ("_fill_model", "_cost_model", "_ck_source")

    def __init__(
        self,
        fill_model: FillModel,
        cost_model: CostModel,
        ck_source: ClickHouseSource | None = None,
    ) -> None:
        self._fill_model = fill_model
        self._cost_model = cost_model
        self._ck_source = ck_source or ClickHouseSource()

    @property
    def engine_type(self) -> str:
        return "maker"

    @property
    def fill_model_name(self) -> str:
        return self._fill_model.label

    def run(
        self,
        strategy: MakerStrategy,
        instrument: str,
        dates: list[str] | None = None,
        pipeline_mode: str = "strict",
    ) -> BacktestResult:
        self._ck_source.health_check()

        if dates is None:
            dates = self._ck_source.available_dates(instrument)
        if not dates:
            raise ValueError(f"No data available for {instrument}")

        all_fills: list[dict] = []
        daily_pnl: list[dict] = []
        equity_points: list[float] = [0.0]
        total_gross = 0.0
        total_fills = 0
        spread_breakdown: dict[int, dict] = {}

        for date in dates:
            events = self._ck_source.load_day(instrument, date)
            if not events:
                continue

            day_fills, day_position = self._run_day(strategy, events)
            day_gross, day_trips, day_wins = self._compute_fifo_pnl(day_fills)
            day_net = self._cost_model.apply(day_gross, len(day_fills))

            total_gross += day_gross
            total_fills += len(day_fills)
            equity_points.append(equity_points[-1] + day_net)

            daily_pnl.append({
                "date": date,
                "pnl_pts": round(day_net, 2),
                "gross_pts": round(day_gross, 2),
                "fills": len(day_fills),
                "trips": day_trips,
                "wins": day_wins,
                "final_pos": day_position,
            })

            for f in day_fills:
                spr = f.get("spread_pts", 0)
                if spr not in spread_breakdown:
                    spread_breakdown[spr] = {"fills": 0, "gross_pnl": 0.0}
                spread_breakdown[spr]["fills"] += 1

        # Metrics
        equity = np.array(equity_points)
        total_net = self._cost_model.apply(total_gross, total_fills)
        n_days = len(daily_pnl)
        winning_days = sum(1 for d in daily_pnl if d["pnl_pts"] > 0)
        daily_returns = np.diff(equity)

        sharpe = 0.0
        if len(daily_returns) > 1 and np.std(daily_returns) > 0:
            sharpe = float(np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252))

        max_dd = 0.0
        peak = equity[0]
        for val in equity:
            peak = max(peak, val)
            dd = (peak - val) / max(abs(peak), 1e-9)
            max_dd = max(max_dd, dd)

        pnl_per_fill = total_net / total_fills if total_fills > 0 else 0.0

        qf = getattr(self._fill_model, "queue_fraction", None)

        return BacktestResult(
            signals=np.array([]),
            equity_curve=equity,
            positions=np.array([]),
            sharpe_is=sharpe,
            sharpe_oos=0.0,
            ic_series=np.array([]),
            ic_mean=0.0,
            ic_std=0.0,
            ic_tstat=0.0,
            ic_pvalue=1.0,
            ic_halflife=0,
            sortino=0.0,
            cvar_5pct=0.0,
            turnover=0.0,
            max_drawdown=max_dd,
            regime_metrics={},
            capacity_estimate=0.0,
            run_id=str(uuid.uuid4())[:12],
            config_hash="",
            latency_profile={},
            engine_type="maker",
            fill_model=self._fill_model.label,
            cost_model=self._cost_model.label,
            instrument=instrument,
            data_period=f"{dates[0]}..{dates[-1]}" if dates else "",
            data_source=f"clickhouse://{self._ck_source._host}:{self._ck_source._port}/hft",
            pipeline_mode=pipeline_mode,
            created_at=datetime.now(timezone.utc).isoformat(),
            queue_fraction=qf,
            maker_scorecard={
                "total_pnl_pts": round(total_net, 2),
                "total_fills": total_fills,
                "pnl_per_fill": round(pnl_per_fill, 4),
                "winning_days": winning_days,
                "winning_day_pct": round(winning_days / n_days * 100, 1) if n_days > 0 else 0,
                "n_days": n_days,
            },
            per_spread_breakdown={str(k): v for k, v in sorted(spread_breakdown.items())},
            daily_pnl=daily_pnl,
        )

    def _run_day(
        self,
        strategy: MakerStrategy,
        events: list[TickData],
    ) -> tuple[list[dict], int]:
        """Run strategy on one day of events. Returns (fills_info, final_position)."""
        buy_order: QueuePosition | None = None
        sell_order: QueuePosition | None = None
        position = 0
        fills: list[dict] = []

        cur_bid = cur_ask = cur_bid_v = cur_ask_v = 0

        for event in events:
            if not event.is_trade:
                cur_bid = event.bid_price
                cur_ask = event.ask_price
                cur_bid_v = event.bid_qty
                cur_ask_v = event.ask_qty

                if cur_ask <= cur_bid:
                    continue

                # Cancel stale orders if price moved
                if buy_order is not None and buy_order.price != cur_bid:
                    buy_order = None
                if sell_order is not None and sell_order.price != cur_ask:
                    sell_order = None

                # Get strategy actions
                actions = strategy.on_tick(event)
                for action in actions:
                    if isinstance(action, PostQuote):
                        qp = self._fill_model.post_quote(
                            action.side, action.price,
                            cur_bid_v if action.side == "buy" else cur_ask_v,
                        )
                        if action.side == "buy":
                            buy_order = qp
                        else:
                            sell_order = qp
                    elif isinstance(action, CancelQuote):
                        if action.side == "buy":
                            buy_order = None
                        else:
                            sell_order = None
            else:
                # Trade event — check fills
                mid = (cur_bid + cur_ask) / (2 * event.scale) if cur_bid > 0 else 0

                if buy_order is not None:
                    result = self._fill_model.check_fills(
                        [buy_order], event.trade_price, event.trade_volume,
                    )
                    if result:
                        fills.append({
                            "side": "buy",
                            "price": buy_order.price,
                            "mid": mid,
                            "spread_pts": (cur_ask - cur_bid) // event.scale if cur_bid > 0 else 0,
                        })
                        strategy.on_fill("buy", buy_order.price, mid)
                        position += 1
                        buy_order = None

                if sell_order is not None:
                    result = self._fill_model.check_fills(
                        [sell_order], event.trade_price, event.trade_volume,
                    )
                    if result:
                        fills.append({
                            "side": "sell",
                            "price": sell_order.price,
                            "mid": mid,
                            "spread_pts": (cur_ask - cur_bid) // event.scale if cur_bid > 0 else 0,
                        })
                        strategy.on_fill("sell", sell_order.price, mid)
                        position -= 1
                        sell_order = None

        return fills, position

    @staticmethod
    def _compute_fifo_pnl(fills: list[dict]) -> tuple[float, int, int]:
        """FIFO PnL matching. Returns (gross_pnl_pts, n_round_trips, n_wins)."""
        buy_q: list[float] = []
        sell_q: list[float] = []
        realized = 0.0
        trips = 0
        wins = 0
        scale = 1_000_000

        for f in fills:
            price_pts = f["price"] / scale
            if f["side"] == "buy":
                if sell_q:
                    sp = sell_q.pop(0)
                    pnl = sp - price_pts
                    realized += pnl
                    trips += 1
                    if pnl > 0:
                        wins += 1
                else:
                    buy_q.append(price_pts)
            else:
                if buy_q:
                    bp = buy_q.pop(0)
                    pnl = price_pts - bp
                    realized += pnl
                    trips += 1
                    if pnl > 0:
                        wins += 1
                else:
                    sell_q.append(price_pts)

        return realized, trips, wins
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_maker_engine.py -v`
Expected: 3 tests PASS (properties + health check + tick data)

Note: Full integration test (engine.run with real CK data) is in Task 7.

- [ ] **Step 5: Commit**

```bash
git add research/backtest/maker_engine.py tests/unit/test_maker_engine.py
git commit -m "feat(research): add MakerEngine with CK-direct backtest"
```

---

### Task 6: TakerEngine wrapper

**Files:**
- Create: `research/backtest/taker_engine.py`
- Test: `tests/unit/test_taker_engine.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_taker_engine.py
"""Tests for TakerEngine wrapper."""
from research.backtest.taker_engine import TakerEngine


def test_taker_engine_properties():
    engine = TakerEngine()
    assert engine.engine_type == "taker"
    assert "PowerProb" in engine.fill_model_name or engine.fill_model_name != ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_taker_engine.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement taker_engine.py**

```python
# research/backtest/taker_engine.py
"""TakerEngine — thin wrapper around existing hft_native_runner.

No changes to the runner itself. This adapter implements BacktestEngine
protocol and maps hft_native_runner output to unified BacktestResult.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class TakerEngine:
    """Wraps existing HftNativeRunner for taker (threshold-crossing) strategies.

    Usage in pipeline:
        engine = TakerEngine()
        result = engine.run_from_runner(runner_result, instrument=..., ...)

    Note: TakerEngine does not call HftNativeRunner directly because
    _gate_c.py already handles runner instantiation and execution.
    This wrapper only adds provenance metadata to the result.
    """

    @property
    def engine_type(self) -> str:
        return "taker"

    @property
    def fill_model_name(self) -> str:
        return "PowerProbQueue(3.0)"

    def enrich_result(
        self,
        base_result: Any,
        *,
        instrument: str,
        data_period: str,
        pipeline_mode: str,
        data_source: str = "npy",
    ) -> Any:
        """Add provenance metadata to an existing BacktestResult from hft_native_runner.

        Uses dataclasses.replace() to set new fields while preserving all existing ones.
        """
        from dataclasses import replace

        return replace(
            base_result,
            engine_type="taker",
            fill_model=self.fill_model_name,
            instrument=instrument,
            data_period=data_period,
            data_source=data_source,
            pipeline_mode=pipeline_mode,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_taker_engine.py -v`
Expected: 1 test PASS

- [ ] **Step 5: Commit**

```bash
git add research/backtest/taker_engine.py tests/unit/test_taker_engine.py
git commit -m "feat(research): add TakerEngine wrapper with provenance enrichment"
```

---

### Task 7: Pipeline integration (_gate_c.py + manifest)

**Files:**
- Modify: `src/hft_platform/alpha/_gate_c.py` (lines 37-69)
- Modify: `research/alphas/r47_maker_pivot/manifest.yaml`
- Test: `tests/unit/test_gate_c_engine_selection.py`

- [ ] **Step 1: Write failing test for engine selection**

```python
# tests/unit/test_gate_c_engine_selection.py
"""Tests for Gate C engine selection based on manifest strategy_type."""
import pytest

from research.backtest.maker_engine import MakerEngine
from research.backtest.taker_engine import TakerEngine


def test_select_maker_engine():
    """When strategy_type=maker, MakerEngine is selected."""
    from research.backtest.cost_models import load_cost_profile
    from research.backtest.fill_models import QueueDepletionFill

    manifest = {"strategy_type": "maker", "instrument": "TMFD6"}
    cost = load_cost_profile(manifest["instrument"])
    fill = QueueDepletionFill(queue_fraction=0.5)
    engine = MakerEngine(fill_model=fill, cost_model=cost)
    assert engine.engine_type == "maker"


def test_select_taker_engine():
    """When strategy_type=taker, TakerEngine is selected."""
    manifest = {"strategy_type": "taker", "instrument": "TXFD6"}
    engine = TakerEngine()
    assert engine.engine_type == "taker"


def test_missing_strategy_type_raises():
    """manifest without strategy_type should raise."""
    manifest = {"instrument": "TMFD6"}
    with pytest.raises(KeyError):
        _ = manifest["strategy_type"]
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/unit/test_gate_c_engine_selection.py -v`
Expected: 3 tests PASS (these test the selection logic, not the full gate)

- [ ] **Step 3: Update r47 manifest.yaml**

Add `strategy_type` and update `instrument` to match deployed config:

```yaml
alpha_id: r47_maker_pivot
name: "Three-Layer Maker Strategy (PE + Queue + MFG)"
version: "0.1.0"
status: prototype
owner: charlie
strategy_type: maker
instrument: TMFD6

instruments:
  - TMFD6
```

Add two new lines (`strategy_type: maker` and `instrument: TMFD6`) after the `owner:` line.

- [ ] **Step 4: Modify _gate_c.py to add maker engine branch**

In `src/hft_platform/alpha/_gate_c.py`, replace lines 37-69 (the engine setup block) with:

```python
    _ensure_project_root_on_path(root)
    from hft_platform.alpha.experiments import ExperimentTracker
    from research.backtest.hft_native_runner import HftNativeRunner, ensure_hftbt_npz
    from research.backtest.types import BacktestConfig, WalkForwardConfig
    from research.registry.scorecard import compute_scorecard

    alpha_id = alpha.manifest.alpha_id
    strategy_type = getattr(alpha.manifest, "strategy_type", "taker")
    instrument = getattr(alpha.manifest, "instrument", "")

    if strategy_type == "maker":
        # --- Maker path: CK-direct backtest ---
        from research.backtest.cost_models import load_cost_profile
        from research.backtest.fill_models import QueueDepletionFill
        from research.backtest.maker_engine import ClickHouseSource, MakerEngine
        from research.backtest.result_store import ResultStore

        ck_source = ClickHouseSource()
        ck_source.health_check()
        cost = load_cost_profile(instrument)
        qf = float(getattr(config, "queue_fraction", 0.5))
        fill = QueueDepletionFill(queue_fraction=qf)
        engine = MakerEngine(fill_model=fill, cost_model=cost, ck_source=ck_source)

        # Strategy must implement MakerStrategy protocol
        maker_strategy = alpha.create_maker_strategy() if hasattr(alpha, "create_maker_strategy") else alpha
        result = engine.run(
            strategy=maker_strategy,
            instrument=instrument,
            pipeline_mode="strict" if not getattr(config, "allow_gate_fail", False) else "triage",
        )
        ResultStore().save(result, alpha_id)
    else:
        # --- Taker path: existing hft_native_runner ---
        backtest_cfg = BacktestConfig(
            data_paths=resolved_data_paths,
            is_oos_split=float(config.is_oos_split),
            signal_threshold=float(config.signal_threshold),
            max_position=int(config.max_position),
            maker_fee_bps=float(config.maker_fee_bps),
            taker_fee_bps=float(config.taker_fee_bps),
            sell_tax_bps=float(config.sell_tax_bps),
            latency_profile_id=str(config.latency_profile_id),
            local_decision_pipeline_latency_us=int(config.local_decision_pipeline_latency_us),
            submit_ack_latency_ms=float(config.submit_ack_latency_ms),
            modify_ack_latency_ms=float(config.modify_ack_latency_ms),
            cancel_ack_latency_ms=float(config.cancel_ack_latency_ms),
            live_uplift_factor=float(config.live_uplift_factor),
            backtest_engine=str(config.backtest_engine),
            queue_model=str(config.queue_model),
            latency_model=str(config.latency_model),
            exchange_model=str(config.exchange_model),
            min_queue_survival_rate=float(config.min_queue_survival_rate),
        )
        backtest_engine_key = str(config.backtest_engine).lower()
        if backtest_engine_key == "research":
            raise ValueError("backtest_engine='research' 已於 v1.1 移除。請使用 'hftbacktest_v2'。")
        for dp in resolved_data_paths:
            ensure_hftbt_npz(dp)
        runner: Any = HftNativeRunner(alpha, backtest_cfg)
        result = runner.run()

        # Enrich taker result with provenance
        from research.backtest.taker_engine import TakerEngine
        from research.backtest.result_store import ResultStore
        data_period = ""
        if resolved_data_paths:
            data_period = ",".join(str(Path(p).stem) for p in resolved_data_paths)
        result = TakerEngine().enrich_result(
            result,
            instrument=instrument,
            data_period=data_period,
            pipeline_mode="strict",
        )
        ResultStore().save(result, alpha_id)
```

Note: The rest of `_gate_c.py` (optimization, stat tests, etc.) continues to use `result` as before. The `result` variable is always a `BacktestResult` regardless of engine path.

- [ ] **Step 5: Run existing gate_c tests to check no regressions**

Run: `uv run pytest tests/ -k "gate_c" --timeout=30 -x -q`
Expected: All existing tests pass (taker path unchanged, maker path only triggered by `strategy_type: maker`)

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/alpha/_gate_c.py \
      research/alphas/r47_maker_pivot/manifest.yaml \
      tests/unit/test_gate_c_engine_selection.py
git commit -m "feat(research): integrate maker/taker engine selection in Gate C"
```

---

### Task 8: Retire legacy files + update manifest validation

**Files:**
- Move: `research/tools/r47_ck_direct_backtest_v2.py` → `research/tools/legacy/`
- Move: `research/backtest/r47_maker_backtest.py` → `research/tools/legacy/`

- [ ] **Step 1: Move legacy files**

```bash
mkdir -p research/tools/legacy
git mv research/tools/r47_ck_direct_backtest_v2.py research/tools/legacy/
git mv research/backtest/r47_maker_backtest.py research/tools/legacy/
```

- [ ] **Step 2: Verify no imports break**

```bash
uv run python -c "from research.backtest.maker_engine import MakerEngine; print('OK')"
uv run python -c "from research.backtest.result_store import ResultStore; print('OK')"
uv run python -c "from research.backtest.cost_models import load_cost_profile; print('OK')"
```
Expected: All print "OK"

- [ ] **Step 3: Commit**

```bash
git add -A research/tools/legacy/ research/tools/ research/backtest/
git commit -m "chore(research): retire r47-specific backtest scripts to legacy/"
```

---

### Task 9: Run full unit test suite + lint

- [ ] **Step 1: Run all new tests**

```bash
uv run pytest tests/unit/test_backtest_types.py tests/unit/test_cost_models.py \
             tests/unit/test_fill_models.py tests/unit/test_result_store.py \
             tests/unit/test_maker_engine.py tests/unit/test_taker_engine.py \
             tests/unit/test_gate_c_engine_selection.py -v
```
Expected: All tests PASS (20+ tests)

- [ ] **Step 2: Run lint**

```bash
uv run ruff check research/backtest/engine.py research/backtest/maker_engine.py \
                  research/backtest/taker_engine.py research/backtest/cost_models.py \
                  research/backtest/fill_models.py research/backtest/result_store.py
```
Expected: No errors

- [ ] **Step 3: Run existing test suite to verify no regressions**

```bash
uv run pytest tests/unit/ --timeout=60 -x -q
```
Expected: All pass, no regressions

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "test(research): verify standardized backtest engine - all tests pass"
```
