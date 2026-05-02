# Canary Live Metrics Writer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ClickHouse-backed live metrics collection so canary evaluation uses real performance data instead of fail-safe defaults.

**Architecture:** New `CanaryMetricsQuery` class queries 4 aggregated metrics from ClickHouse (`hft.fills` + `hft.orders`). It's injected into `CanaryAutoScheduler` which calls `fetch()` inline before each canary evaluation. CK failure falls back to existing fail-safe defaults.

**Tech Stack:** Python 3.12, clickhouse_connect, existing `hft.fills` and `hft.orders` tables.

**Spec:** `docs/superpowers/specs/2026-04-05-canary-live-metrics-writer-design.md`

---

### Task 1: Create CanaryMetricsQuery with CK aggregation queries

**Files:**
- Create: `src/hft_platform/alpha/canary_metrics.py`
- Test: `tests/unit/test_canary_metrics_query.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_canary_metrics_query.py`:

```python
"""Tests for CanaryMetricsQuery — ClickHouse-backed canary metrics."""
from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.alpha.canary_metrics import CanaryMetricsQuery


def _mock_client_with_results(
    slippage_row: list | None = None,
    drawdown_rows: list | None = None,
    error_rate_row: list | None = None,
    sessions_row: list | None = None,
) -> MagicMock:
    """Build a mock CK client that returns canned query results.

    clickhouse_connect client.query() returns an object with .result_rows.
    """
    client = MagicMock()

    # Default: valid data
    if slippage_row is None:
        slippage_row = [(1.5,)]  # avg slippage = 1.5 bps
    if drawdown_rows is None:
        drawdown_rows = [(100,), (80,)]  # cumulative pnl series: peak=100, final=80
    if error_rate_row is None:
        error_rate_row = [(10, 100)]  # 10 rejected out of 100 total
    if sessions_row is None:
        sessions_row = [(7,)]  # 7 distinct trading days

    def query_side_effect(sql, *args, **kwargs):
        result = MagicMock()
        sql_lower = sql.lower()
        if "avg(" in sql_lower and "slippage" in sql_lower or ("fill_price" in sql_lower and "decision_price" in sql_lower):
            result.result_rows = slippage_row
        elif "cumulative" in sql_lower or "running" in sql_lower or "sum(" in sql_lower:
            result.result_rows = drawdown_rows
        elif "rejected" in sql_lower or "error" in sql_lower or "countif" in sql_lower.replace(" ", ""):
            result.result_rows = error_rate_row
        elif "distinct" in sql_lower and "todate" in sql_lower:
            result.result_rows = sessions_row
        else:
            result.result_rows = []
        return result

    client.query.side_effect = query_side_effect
    return client


class TestCanaryMetricsQueryFetch:
    def test_fetch_returns_all_four_metrics(self):
        """Normal CK response → correct dict with 4 keys."""
        client = _mock_client_with_results()
        query = CanaryMetricsQuery(client_factory=lambda: client)
        result = query.fetch("alpha_1", "strat_1", 0)
        assert result is not None
        assert "slippage_bps" in result
        assert "drawdown_contribution" in result
        assert "execution_error_rate" in result
        assert "sessions_live" in result
        assert isinstance(result["slippage_bps"], float)
        assert isinstance(result["sessions_live"], int)

    def test_fetch_returns_none_on_ck_error(self):
        """CK client throws → returns None."""
        def bad_factory():
            raise ConnectionError("CK down")
        query = CanaryMetricsQuery(client_factory=bad_factory)
        result = query.fetch("alpha_1", "strat_1", 0)
        assert result is None

    def test_fetch_returns_none_on_query_error(self):
        """CK client.query throws → returns None."""
        client = MagicMock()
        client.query.side_effect = Exception("query failed")
        query = CanaryMetricsQuery(client_factory=lambda: client)
        result = query.fetch("alpha_1", "strat_1", 0)
        assert result is None

    def test_slippage_query_filters_by_strategy(self):
        """SQL contains strategy_id filter."""
        client = _mock_client_with_results()
        query = CanaryMetricsQuery(client_factory=lambda: client)
        query.fetch("alpha_1", "my_strategy", 0)
        # Check that at least one query call contains strategy_id
        calls = client.query.call_args_list
        sql_texts = [str(c[0][0]) for c in calls]
        assert any("my_strategy" in sql for sql in sql_texts)

    def test_since_ns_filter_applied(self):
        """SQL contains timestamp filter."""
        client = _mock_client_with_results()
        query = CanaryMetricsQuery(client_factory=lambda: client)
        since_ns = 1_700_000_000_000_000_000
        query.fetch("alpha_1", "strat_1", since_ns)
        calls = client.query.call_args_list
        sql_texts = [str(c[0][0]) for c in calls]
        assert any(str(since_ns) in sql for sql in sql_texts)

    def test_error_rate_division_by_zero(self):
        """Zero total orders → error_rate = 0.0 (not division error)."""
        client = _mock_client_with_results(error_rate_row=[(0, 0)])
        query = CanaryMetricsQuery(client_factory=lambda: client)
        result = query.fetch("alpha_1", "strat_1", 0)
        assert result is not None
        assert result["execution_error_rate"] == 0.0

    def test_drawdown_with_no_fills(self):
        """No fills → drawdown = 0.0."""
        client = _mock_client_with_results(drawdown_rows=[])
        query = CanaryMetricsQuery(client_factory=lambda: client)
        result = query.fetch("alpha_1", "strat_1", 0)
        assert result is not None
        assert result["drawdown_contribution"] == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_canary_metrics_query.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'hft_platform.alpha.canary_metrics'`

- [ ] **Step 3: Implement CanaryMetricsQuery**

Create `src/hft_platform/alpha/canary_metrics.py`:

```python
"""ClickHouse-backed canary metrics collection.

Queries ``hft.fills`` and ``hft.orders`` to compute live performance metrics
for canary evaluation.  Used inline by :class:`CanaryAutoScheduler` before
each evaluation cycle.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from structlog import get_logger

logger = get_logger("alpha.canary_metrics")


def _default_client_factory() -> Any:
    """Create a ClickHouse client using env vars (same pattern as audit.py)."""
    import clickhouse_connect

    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    return clickhouse_connect.get_client(host=host, port=port)


class CanaryMetricsQuery:
    """Fetch live canary metrics from ClickHouse.

    Each call to :meth:`fetch` creates a fresh client via *client_factory*
    (no pooling — canary evaluation runs at most once per 24h).
    """

    __slots__ = ("_client_factory",)

    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_factory = client_factory or _default_client_factory

    def fetch(
        self, alpha_id: str, strategy_id: str, since_ns: int,
    ) -> dict[str, Any] | None:
        """Query CK for canary metrics.  Returns *None* on any failure."""
        try:
            client = self._client_factory()
            slippage = self._query_slippage(client, strategy_id, since_ns)
            drawdown = self._query_drawdown(client, strategy_id, since_ns)
            error_rate = self._query_error_rate(client, strategy_id, since_ns)
            sessions = self._query_sessions(client, strategy_id, since_ns)
            return {
                "slippage_bps": slippage,
                "drawdown_contribution": drawdown,
                "execution_error_rate": error_rate,
                "sessions_live": sessions,
            }
        except Exception:  # noqa: BLE001
            logger.warning(
                "canary_metrics_fetch_failed",
                alpha_id=alpha_id,
                strategy_id=strategy_id,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Individual metric queries
    # ------------------------------------------------------------------

    @staticmethod
    def _query_slippage(client: Any, strategy_id: str, since_ns: int) -> float:
        """Average absolute slippage in basis points.

        Joins fills with orders to compare fill_price vs decision_price.
        Only considers fills where decision_price > 0 (has TCA data).
        """
        sql = (
            "SELECT avg(abs(f.price_scaled - o.price_scaled) "
            "/ o.price_scaled * 10000) AS slippage_bps "
            "FROM hft.fills f "
            "INNER JOIN hft.orders o ON f.client_order_id = o.order_id "
            f"WHERE f.strategy_id = '{strategy_id}' "
            f"AND f.ts_exchange >= {since_ns} "
            "AND o.price_scaled > 0"
        )
        result = client.query(sql)
        rows = result.result_rows
        if not rows or rows[0][0] is None:
            return 0.0
        return float(rows[0][0])

    @staticmethod
    def _query_drawdown(client: Any, strategy_id: str, since_ns: int) -> float:
        """Max drawdown contribution from running sum of realized PnL.

        Computes (peak_cumulative - final_cumulative) / abs(peak_cumulative).
        Returns 0.0 if no fills or flat PnL.
        """
        sql = (
            "SELECT sum(price_scaled * qty * "
            "  if(side = 'SELL', 1, -1)) AS cumulative_pnl "
            "FROM hft.fills "
            f"WHERE strategy_id = '{strategy_id}' "
            f"AND ts_exchange >= {since_ns} "
            "ORDER BY ts_exchange"
        )
        result = client.query(sql)
        rows = result.result_rows
        if not rows:
            return 0.0
        # Single aggregated row
        cumulative = float(rows[0][0] or 0)
        # For drawdown we need the running max — use a window query
        sql_dd = (
            "SELECT max(running_pnl) AS peak, "
            "  arrayElement(groupArray(running_pnl), length(groupArray(running_pnl))) AS final "
            "FROM ("
            "  SELECT sum(price_scaled * qty * if(side = 'SELL', 1, -1)) "
            "    OVER (ORDER BY ts_exchange) AS running_pnl "
            "  FROM hft.fills "
            f"  WHERE strategy_id = '{strategy_id}' "
            f"  AND ts_exchange >= {since_ns}"
            ")"
        )
        result_dd = client.query(sql_dd)
        dd_rows = result_dd.result_rows
        if not dd_rows or dd_rows[0][0] is None:
            return 0.0
        peak = float(dd_rows[0][0])
        final = float(dd_rows[0][1]) if dd_rows[0][1] is not None else 0.0
        if abs(peak) < 1e-9:
            return 0.0
        dd = (peak - final) / abs(peak)
        return max(0.0, dd)

    @staticmethod
    def _query_error_rate(client: Any, strategy_id: str, since_ns: int) -> float:
        """Execution error rate = rejected / total orders."""
        sql = (
            "SELECT "
            "  countIf(status = 'REJECTED') AS rejected, "
            "  count(*) AS total "
            "FROM hft.orders "
            f"WHERE strategy_id = '{strategy_id}' "
            f"AND ingest_ts >= {since_ns}"
        )
        result = client.query(sql)
        rows = result.result_rows
        if not rows:
            return 0.0
        rejected = int(rows[0][0] or 0)
        total = int(rows[0][1] or 0)
        if total == 0:
            return 0.0
        return float(rejected) / float(total)

    @staticmethod
    def _query_sessions(client: Any, strategy_id: str, since_ns: int) -> int:
        """Count distinct trading days with fills."""
        sql = (
            "SELECT count(distinct toDate(toDateTime(ts_exchange / 1000000000))) "
            "FROM hft.fills "
            f"WHERE strategy_id = '{strategy_id}' "
            f"AND ts_exchange >= {since_ns}"
        )
        result = client.query(sql)
        rows = result.result_rows
        if not rows or rows[0][0] is None:
            return 0
        return int(rows[0][0])
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/test_canary_metrics_query.py -v
```

Expected: 7 passed

- [ ] **Step 5: Lint check**

```bash
uv run ruff check src/hft_platform/alpha/canary_metrics.py tests/unit/test_canary_metrics_query.py
```

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/alpha/canary_metrics.py tests/unit/test_canary_metrics_query.py
git commit -m "feat(alpha): add CanaryMetricsQuery for CK-backed canary metrics"
```

---

### Task 2: Wire CanaryMetricsQuery into CanaryAutoScheduler

**Files:**
- Modify: `src/hft_platform/alpha/canary_scheduler.py:60-66` (__init__) and `evaluate_all` and `_build_metrics`
- Test: `tests/unit/test_canary_scheduler.py` (add 3 tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_canary_scheduler.py`:

```python
class TestSchedulerMetricsQuery:
    """Tests for CanaryAutoScheduler with CanaryMetricsQuery integration."""

    def test_evaluate_all_no_query_backward_compat(self, tmp_path):
        """metrics_query=None → fail-safe defaults used (current behavior)."""
        canary_dir = tmp_path / "canaries"
        yaml_path = canary_dir / "alpha_1.yaml"
        _write_canary_yaml(yaml_path, alpha_id="alpha_1", sessions_live=0, slippage_bps=0.0)

        monitor = CanaryMonitor(canary_dir=str(canary_dir))
        scheduler = CanaryAutoScheduler(monitor=monitor, metrics_query=None, dry_run=True)
        results = asyncio.get_event_loop().run_until_complete(scheduler.evaluate_all())
        assert len(results) == 1
        # With no metrics_query and live_metrics in YAML showing 0 slippage,
        # _build_metrics still reads from YAML (backward compat path)

    def test_evaluate_all_uses_ck_metrics(self, tmp_path):
        """metrics_query returns dict → passed to evaluate()."""
        canary_dir = tmp_path / "canaries"
        yaml_path = canary_dir / "alpha_1.yaml"
        _write_canary_yaml(yaml_path, alpha_id="alpha_1")

        monitor = CanaryMonitor(canary_dir=str(canary_dir))
        mock_query = MagicMock()
        mock_query.fetch.return_value = {
            "slippage_bps": 0.5,
            "drawdown_contribution": 0.001,
            "execution_error_rate": 0.0,
            "sessions_live": 10,
        }
        scheduler = CanaryAutoScheduler(
            monitor=monitor, metrics_query=mock_query, dry_run=True,
        )
        results = asyncio.get_event_loop().run_until_complete(scheduler.evaluate_all())
        assert len(results) == 1
        mock_query.fetch.assert_called_once()
        # With good metrics, should NOT be rolled_back
        assert results[0].state != "rolled_back"

    def test_evaluate_all_fallback_on_none(self, tmp_path):
        """metrics_query returns None → fail-safe defaults (rollback)."""
        canary_dir = tmp_path / "canaries"
        yaml_path = canary_dir / "alpha_1.yaml"
        _write_canary_yaml(yaml_path, alpha_id="alpha_1")

        monitor = CanaryMonitor(canary_dir=str(canary_dir))
        mock_query = MagicMock()
        mock_query.fetch.return_value = None
        scheduler = CanaryAutoScheduler(
            monitor=monitor, metrics_query=mock_query, dry_run=True,
        )
        results = asyncio.get_event_loop().run_until_complete(scheduler.evaluate_all())
        assert len(results) == 1
        # Fail-safe defaults trigger rollback
        assert results[0].state == "rolled_back"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_canary_scheduler.py::TestSchedulerMetricsQuery -v
```

Expected: FAIL (CanaryAutoScheduler doesn't accept `metrics_query` yet)

- [ ] **Step 3: Modify CanaryAutoScheduler**

In `src/hft_platform/alpha/canary_scheduler.py`:

**Modify `__init__`** (add `metrics_query` parameter):

```python
    def __init__(
        self,
        monitor: CanaryMonitor,
        interval_s: float | None = None,
        dry_run: bool | None = None,
        metrics_query: Any | None = None,
    ) -> None:
        self._monitor = monitor

        env_interval = float(os.getenv("HFT_CANARY_AUTO_INTERVAL_S", str(_DEFAULT_INTERVAL_S)))
        self._interval: float = interval_s if interval_s is not None else env_interval

        env_dry_run = os.getenv("HFT_CANARY_AUTO_DRY_RUN", "1") == "1"
        self._dry_run: bool = dry_run if dry_run is not None else env_dry_run

        self._task: asyncio.Task[None] | None = None
        self._metrics_query = metrics_query
```

**Extract `_failsafe_metrics` static method** from the old `_build_metrics`:

```python
    @staticmethod
    def _failsafe_metrics() -> dict[str, Any]:
        """Worst-case metrics triggering rollback when real data is unavailable."""
        return {
            "slippage_bps": _FAILSAFE_SLIPPAGE_BPS,
            "drawdown_contribution": _FAILSAFE_DRAWDOWN,
            "execution_error_rate": _FAILSAFE_ERROR_RATE,
            "sessions_live": 0,
        }
```

**Modify `evaluate_all`** — replace `self._build_metrics(canary)` with CK fetch + fallback:

```python
    async def evaluate_all(self) -> list[CanaryStatus]:
        canaries = self._monitor.load_active_canaries()
        results: list[CanaryStatus] = []

        for canary in canaries:
            alpha_id = canary.get("alpha_id")
            if not alpha_id:
                logger.warning("canary_auto_skip_no_id", canary_keys=list(canary.keys()))
                continue

            try:
                # Try CK metrics first, fall back to YAML then fail-safe
                live_metrics = None
                if self._metrics_query is not None:
                    strategy_id = canary.get("strategy_id", str(alpha_id))
                    since_ns = int(canary.get("promoted_at_ns", 0))
                    live_metrics = self._metrics_query.fetch(
                        str(alpha_id), str(strategy_id), since_ns,
                    )

                if live_metrics is None:
                    live_metrics = self._build_metrics(canary)

                status = self._monitor.evaluate(str(alpha_id), live_metrics)
                results.append(status)

                logger.info(
                    "canary_auto_evaluated",
                    alpha_id=alpha_id,
                    state=status.state,
                    reason=status.reason,
                    dry_run=self._dry_run,
                    metrics_source="clickhouse" if self._metrics_query is not None and live_metrics is not None else "yaml_fallback",
                )

                if not self._dry_run:
                    self._monitor.apply_decision(status)
                    logger.info(
                        "canary_auto_applied",
                        alpha_id=alpha_id,
                        state=status.state,
                    )
            except Exception as _exc:  # noqa: BLE001
                logger.error(
                    "canary_auto_evaluate_error",
                    alpha_id=alpha_id,
                    exc_info=True,
                )

        logger.info(
            "canary_auto_evaluate_all_done",
            total=len(canaries),
            evaluated=len(results),
            dry_run=self._dry_run,
        )
        return results
```

Keep the existing `_build_metrics` as the YAML fallback (no changes needed — it's used when `metrics_query` is None or returns None).

- [ ] **Step 4: Run new tests to verify they pass**

```bash
uv run pytest tests/unit/test_canary_scheduler.py::TestSchedulerMetricsQuery -v
```

Expected: 3 passed

- [ ] **Step 5: Run all canary scheduler tests for regressions**

```bash
uv run pytest tests/unit/test_canary_scheduler.py tests/unit/test_canary_failsafe.py -v --no-header -q 2>&1 | tail -5
```

Expected: All existing tests still pass

- [ ] **Step 6: Lint check**

```bash
uv run ruff check src/hft_platform/alpha/canary_scheduler.py tests/unit/test_canary_scheduler.py
```

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/alpha/canary_scheduler.py tests/unit/test_canary_scheduler.py
git commit -m "feat(alpha): wire CanaryMetricsQuery into scheduler evaluate_all"
```
