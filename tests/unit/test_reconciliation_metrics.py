"""WU-18: Reconciliation Prometheus Metrics tests.

Covers:
- reconciliation_sync_total counter (success / failure)
- reconciliation_sync_duration_seconds histogram
- reconciliation_discrepancy_total counter (info / warning / critical)
- reconciliation_consecutive_failures gauge
- reconciliation_last_success_ts gauge
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.reconciliation import (
    PositionDiscrepancy,
    ReconciliationService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockPosition:
    __slots__ = ("symbol", "net_qty")

    def __init__(self, symbol: str, net_qty: int) -> None:
        self.symbol = symbol
        self.net_qty = net_qty


def _make_service(
    *,
    client: MagicMock | None = None,
    store: MagicMock | None = None,
    storm_guard: MagicMock | None = None,
) -> ReconciliationService:
    if client is None:
        client = MagicMock()
        client.get_positions.return_value = []
    if store is None:
        store = MagicMock()
        store.positions = {}
    return ReconciliationService(client, store, {}, storm_guard=storm_guard)


# ---------------------------------------------------------------------------
# PositionDiscrepancy.severity
# ---------------------------------------------------------------------------


class TestPositionDiscrepancySeverity:

    def test_critical_on_sign_mismatch(self) -> None:
        d = PositionDiscrepancy(symbol="X", local_qty=10, broker_qty=-5, diff=15)
        assert d.severity == "critical"

    def test_warning_on_moderate_diff(self) -> None:
        d = PositionDiscrepancy(symbol="X", local_qty=50, broker_qty=35, diff=15)
        assert d.severity == "warning"

    def test_info_on_small_diff(self) -> None:
        d = PositionDiscrepancy(symbol="X", local_qty=50, broker_qty=45, diff=5)
        assert d.severity == "info"


# ---------------------------------------------------------------------------
# sync_portfolio metrics
# ---------------------------------------------------------------------------


class TestReconciliationMetrics:

    @pytest.mark.asyncio
    async def test_sync_success_increments_counter(self) -> None:
        client = MagicMock()
        client.get_positions.return_value = []
        mock_metrics = MagicMock()

        with patch(
            "hft_platform.execution.reconciliation.MetricsRegistry.get",
            return_value=mock_metrics,
        ):
            svc = _make_service(client=client)
            await svc.sync_portfolio()

        mock_metrics.reconciliation_sync_total.labels.assert_any_call(result="success")
        mock_metrics.reconciliation_sync_total.labels(result="success").inc.assert_called()

    @pytest.mark.asyncio
    async def test_sync_failure_increments_failure_counter(self) -> None:
        client = MagicMock()
        client.get_positions.side_effect = RuntimeError("broker down")
        mock_metrics = MagicMock()

        with patch(
            "hft_platform.execution.reconciliation.MetricsRegistry.get",
            return_value=mock_metrics,
        ):
            svc = _make_service(client=client)
            with pytest.raises(RuntimeError):
                await svc.sync_portfolio()

        mock_metrics.reconciliation_sync_total.labels.assert_any_call(result="failure")
        mock_metrics.reconciliation_sync_total.labels(result="failure").inc.assert_called()

    @pytest.mark.asyncio
    async def test_sync_duration_observed(self) -> None:
        client = MagicMock()
        client.get_positions.return_value = []
        mock_metrics = MagicMock()

        with patch(
            "hft_platform.execution.reconciliation.MetricsRegistry.get",
            return_value=mock_metrics,
        ):
            svc = _make_service(client=client)
            await svc.sync_portfolio()

        mock_metrics.reconciliation_sync_duration_seconds.observe.assert_called_once()
        duration = mock_metrics.reconciliation_sync_duration_seconds.observe.call_args[0][0]
        assert duration >= 0

    @pytest.mark.asyncio
    async def test_sync_duration_observed_on_failure(self) -> None:
        client = MagicMock()
        client.get_positions.side_effect = RuntimeError("fail")
        mock_metrics = MagicMock()

        with patch(
            "hft_platform.execution.reconciliation.MetricsRegistry.get",
            return_value=mock_metrics,
        ):
            svc = _make_service(client=client)
            with pytest.raises(RuntimeError):
                await svc.sync_portfolio()

        mock_metrics.reconciliation_sync_duration_seconds.observe.assert_called_once()

    @pytest.mark.asyncio
    async def test_last_success_ts_updated_on_success(self) -> None:
        client = MagicMock()
        client.get_positions.return_value = []
        mock_metrics = MagicMock()

        with patch(
            "hft_platform.execution.reconciliation.MetricsRegistry.get",
            return_value=mock_metrics,
        ):
            svc = _make_service(client=client)
            await svc.sync_portfolio()

        mock_metrics.reconciliation_last_success_ts.set.assert_called_once()
        ts = mock_metrics.reconciliation_last_success_ts.set.call_args[0][0]
        assert ts > 0

    @pytest.mark.asyncio
    async def test_last_success_ts_not_updated_on_failure(self) -> None:
        client = MagicMock()
        client.get_positions.side_effect = RuntimeError("fail")
        mock_metrics = MagicMock()

        with patch(
            "hft_platform.execution.reconciliation.MetricsRegistry.get",
            return_value=mock_metrics,
        ):
            svc = _make_service(client=client)
            with pytest.raises(RuntimeError):
                await svc.sync_portfolio()

        mock_metrics.reconciliation_last_success_ts.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_discrepancy_severity_counters(self) -> None:
        """When discrepancies exist, per-severity counters are incremented."""
        client = MagicMock()
        # Broker: symbol A has 200, symbol B has -5
        client.get_positions.return_value = [
            SimpleNamespace(code="A", quantity=200, direction="Action.Buy"),
            SimpleNamespace(code="B", quantity=5, direction="Action.Sell"),
        ]

        store = MagicMock()
        # Local: A has 5 (diff=195 → critical because > 100 and > 10% of 5)
        # B has 5 (diff=5+5=10 → info because ≤ 10)
        store.positions = {
            "acct:strat:A": _MockPosition(symbol="A", net_qty=5),
            "acct:strat:B": _MockPosition(symbol="B", net_qty=5),
        }

        mock_metrics = MagicMock()

        with patch(
            "hft_platform.execution.reconciliation.MetricsRegistry.get",
            return_value=mock_metrics,
        ):
            svc = ReconciliationService(client, store, {})
            await svc.sync_portfolio()

        # Check that discrepancy_total was called with severity labels
        severity_calls = [
            c[1]["severity"]
            for c in mock_metrics.reconciliation_discrepancy_total.labels.call_args_list
        ]
        assert len(severity_calls) == 2  # two discrepant symbols

    @pytest.mark.asyncio
    async def test_consecutive_failures_gauge_via_run(self) -> None:
        """The gauge should reflect _consecutive_failures during run()."""
        mock_metrics = MagicMock()

        client = MagicMock()
        call_count = 0

        async def _fail_then_stop() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # startup OK
            raise RuntimeError("down")

        with patch(
            "hft_platform.execution.reconciliation.MetricsRegistry.get",
            return_value=mock_metrics,
        ):
            svc = _make_service(client=client)
            svc.check_interval_s = 0.001
            svc.grace_failures = 2
            svc.backoff_base = 1.0
            svc.backoff_max = 0.001
            svc.sync_portfolio = _fail_then_stop  # type: ignore[assignment]

            import asyncio
            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.3)
            svc.running = False
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Gauge should have been set at least once with value >= 1
        set_calls = mock_metrics.reconciliation_consecutive_failures.set.call_args_list
        assert len(set_calls) >= 1
        values = [c[0][0] for c in set_calls]
        assert max(values) >= 1

    @pytest.mark.asyncio
    async def test_no_discrepancy_metrics_when_positions_match(self) -> None:
        """When positions match, no discrepancy severity counters should fire."""
        client = MagicMock()
        client.get_positions.return_value = [
            SimpleNamespace(code="2330", quantity=10, direction="Action.Buy"),
        ]

        store = MagicMock()
        store.positions = {"acct:strat:2330": _MockPosition(symbol="2330", net_qty=10)}

        mock_metrics = MagicMock()

        with patch(
            "hft_platform.execution.reconciliation.MetricsRegistry.get",
            return_value=mock_metrics,
        ):
            svc = ReconciliationService(client, store, {})
            await svc.sync_portfolio()

        mock_metrics.reconciliation_discrepancy_total.labels.assert_not_called()
