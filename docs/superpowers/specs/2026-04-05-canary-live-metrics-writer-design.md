# Canary Live Metrics Writer

**Date**: 2026-04-05
**Status**: Draft
**Author**: Claude (Debugging Team audit follow-up)

## Problem

`CanaryAutoScheduler._build_metrics()` reads live metrics from canary YAML's `live_metrics` block, but nothing writes to it. All metrics default to fail-safe worst-case values (slippage=999, drawdown=100%, error_rate=100%), which means:
- Canaries with missing data are immediately rolled back (correct fail-safe behavior)
- But canaries that ARE running successfully never get their real metrics recorded
- The scheduler cannot distinguish "no data" from "performing well"
- Escalation and graduation are unreachable without manual YAML editing

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Data source | ClickHouse aggregation | 24h eval cycle doesn't need real-time; CK has complete fill/order history |
| Execution timing | Inline in evaluate_all() | Guarantees fresh metrics before evaluation; no sync issues |
| Metrics target | Direct dict to evaluate() | No YAML write; YAML is config, not metrics store |

## Architecture

### Flow

```
CanaryAutoScheduler.evaluate_all()
  → for each canary:
      → CanaryMetricsQuery.fetch(alpha_id, strategy_id, since_ns)
          → ClickHouse: 4 aggregation queries
          → returns dict or None
      → if None: use fail-safe defaults (current behavior)
      → monitor.evaluate(alpha_id, live_metrics)
      → if not dry_run: monitor.apply_decision(status)
```

### New Components

| Component | File | Responsibility |
|-----------|------|----------------|
| `CanaryMetricsQuery` | `alpha/canary_metrics.py` | Encapsulates CK aggregation queries for canary metrics |

### Modified Components

| Component | Change |
|-----------|--------|
| `CanaryAutoScheduler.__init__` | Accept `metrics_query: CanaryMetricsQuery | None = None` |
| `CanaryAutoScheduler.evaluate_all` | Call `metrics_query.fetch()` before evaluate, fallback to fail-safe on None |
| `CanaryAutoScheduler._build_metrics` | Replaced by direct fetch + failsafe pattern |

### Unchanged Components

- `canary.py` — `CanaryMonitor.evaluate()` interface unchanged (accepts dict)
- `canary.py` — `apply_decision()` unchanged

## Detailed Design

### CanaryMetricsQuery

```python
class CanaryMetricsQuery:
    """Fetch live canary metrics from ClickHouse."""

    __slots__ = ("_client_factory",)

    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_factory = client_factory or _default_client_factory

    def fetch(
        self, alpha_id: str, strategy_id: str, since_ns: int
    ) -> dict[str, Any] | None:
        """Query CK for canary metrics. Returns None on any failure."""
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
        except Exception:
            logger.warning("canary_metrics_fetch_failed", alpha_id=alpha_id, exc_info=True)
            return None
```

### Metric Queries

| Metric | SQL Logic | CK Table |
|--------|-----------|----------|
| `slippage_bps` | `avg(abs(fill_price - decision_price) / decision_price * 10000)` where decision_price > 0 | `hft.fills` JOIN `hft.orders` on order_id |
| `drawdown_contribution` | `(max_cumulative_pnl - final_cumulative_pnl) / abs(max_cumulative_pnl)` from running sum of realized_pnl | `hft.fills` |
| `execution_error_rate` | `countIf(status='REJECTED') / count(*)` | `hft.orders` |
| `sessions_live` | `count(distinct toDate(exch_ts / 1000000000))` | `hft.fills` |

All queries filter by `strategy_id` and `exch_ts >= since_ns`.

### Scheduler Integration

```python
class CanaryAutoScheduler:
    def __init__(
        self,
        monitor: CanaryMonitor,
        interval_s: float | None = None,
        dry_run: bool | None = None,
        metrics_query: CanaryMetricsQuery | None = None,  # NEW
    ) -> None:
        ...
        self._metrics_query = metrics_query

    async def evaluate_all(self) -> list[CanaryStatus]:
        canaries = self._monitor.load_active_canaries()
        results: list[CanaryStatus] = []
        for canary in canaries:
            alpha_id = canary.get("alpha_id")
            strategy_id = canary.get("strategy_id", alpha_id)
            since_ns = int(canary.get("promoted_at_ns", 0))

            live_metrics = None
            if self._metrics_query is not None:
                live_metrics = self._metrics_query.fetch(
                    str(alpha_id), str(strategy_id), since_ns
                )
            if live_metrics is None:
                live_metrics = self._failsafe_metrics()

            status = self._monitor.evaluate(str(alpha_id), live_metrics)
            ...

    @staticmethod
    def _failsafe_metrics() -> dict[str, Any]:
        return {
            "slippage_bps": _FAILSAFE_SLIPPAGE_BPS,
            "drawdown_contribution": _FAILSAFE_DRAWDOWN,
            "execution_error_rate": _FAILSAFE_ERROR_RATE,
            "sessions_live": 0,
        }
```

### Backward Compatibility

- `metrics_query=None` (default) → fail-safe defaults, behavior identical to current code
- CK query failure → `fetch()` returns None → fail-safe defaults
- Existing canary YAML format unchanged; `live_metrics` block becomes unused (can be removed later)

## Test Plan

### Unit Tests (`test_canary_metrics_query.py`)

| Test | Verifies |
|------|----------|
| `test_fetch_returns_all_four_metrics` | Normal CK response → correct dict with 4 keys |
| `test_fetch_returns_none_on_ck_error` | CK client throws → returns None |
| `test_fetch_returns_none_on_empty_result` | CK returns empty rows → returns None |
| `test_slippage_query_filters_by_strategy` | SQL contains strategy_id filter |
| `test_since_ns_filter_applied` | SQL contains timestamp filter |

### Integration Tests (modify existing `test_canary_scheduler.py` or new file)

| Test | Verifies |
|------|----------|
| `test_evaluate_all_no_query_backward_compat` | metrics_query=None → fail-safe defaults used |
| `test_evaluate_all_uses_ck_metrics` | metrics_query returns dict → passed to evaluate() |
| `test_evaluate_all_fallback_on_none` | metrics_query returns None → fail-safe defaults |

## Files Changed Summary

| File | Action | Lines (est.) |
|------|--------|-------------|
| `alpha/canary_metrics.py` | **New** | ~80 |
| `alpha/canary_scheduler.py` | Modify __init__ + evaluate_all + extract _failsafe_metrics | ~20 |
| `tests/unit/test_canary_metrics_query.py` | **New** | ~90 |
| `tests/unit/test_canary_scheduler.py` | Modify/add 3 tests | ~30 |

**Total**: ~1 new file (production), 1 modified file, ~220 lines.
