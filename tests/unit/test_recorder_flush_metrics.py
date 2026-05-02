"""Unit tests verifying recorder_batches_flushed_total and recorder_rows_flushed_total
are incremented on successful flush in Batcher._write_flush_buffer().
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.recorder.batcher import Batcher, GlobalMemoryGuard

# The lazy import inside batcher._write_flush_buffer is:
#   from hft_platform.observability.metrics import MetricsRegistry
# We patch the class on the module so the `from … import` gets our mock.
_METRICS_MODULE = "hft_platform.observability.metrics.MetricsRegistry"


@pytest.fixture(autouse=True)
def reset_memory_guard():
    GlobalMemoryGuard.reset()
    yield
    GlobalMemoryGuard.reset()


def _make_writer() -> AsyncMock:
    writer = AsyncMock()
    writer.write = AsyncMock()
    writer.write_columnar = AsyncMock()
    return writer


def _mock_registry() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return (registry, batches_counter, rows_counter) mocks."""
    mock_batches = MagicMock()
    mock_rows = MagicMock()
    registry = MagicMock()
    registry.recorder_batches_flushed_total = mock_batches
    registry.recorder_rows_flushed_total = mock_rows
    return registry, mock_batches, mock_rows


class TestRecorderFlushMetrics:
    """Verify flush counters are wired and incremented correctly."""

    @pytest.mark.asyncio
    async def test_batches_flushed_total_incremented_on_flush(self) -> None:
        """recorder_batches_flushed_total must increment by 1 per successful flush."""
        writer = _make_writer()
        b = Batcher("hft.market_data", flush_limit=100, writer=writer)
        await b.add({"price": 100_000, "qty": 10})

        registry, mock_batches, mock_rows = _mock_registry()
        with patch(_METRICS_MODULE) as mock_cls:
            mock_cls.get.return_value = registry
            await b.force_flush()

        mock_batches.labels.assert_called_with(table="hft.market_data")
        mock_batches.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_rows_flushed_total_incremented_with_row_count(self) -> None:
        """recorder_rows_flushed_total must be incremented by the number of rows flushed."""
        writer = _make_writer()
        b = Batcher("hft.market_data", flush_limit=100, writer=writer)

        rows_to_add = 5
        for i in range(rows_to_add):
            await b.add({"price": 100_000 + i, "qty": i + 1})

        registry, mock_batches, mock_rows = _mock_registry()
        with patch(_METRICS_MODULE) as mock_cls:
            mock_cls.get.return_value = registry
            await b.force_flush()

        mock_rows.labels.assert_called_with(table="hft.market_data")
        mock_rows.labels.return_value.inc.assert_called_once_with(rows_to_add)

    @pytest.mark.asyncio
    async def test_metrics_not_incremented_when_buffer_empty(self) -> None:
        """No metric increment when force_flush is called with an empty buffer."""
        writer = _make_writer()
        b = Batcher("hft.market_data", flush_limit=100, writer=writer)

        registry, mock_batches, mock_rows = _mock_registry()
        with patch(_METRICS_MODULE) as mock_cls:
            mock_cls.get.return_value = registry
            await b.force_flush()

        mock_batches.labels.return_value.inc.assert_not_called()
        mock_rows.labels.return_value.inc.assert_not_called()

    @pytest.mark.asyncio
    async def test_metrics_error_does_not_break_flush(self) -> None:
        """A crash in metrics code must never propagate and break the flush path."""
        writer = _make_writer()
        b = Batcher("hft.market_data", flush_limit=100, writer=writer)
        await b.add({"price": 100_000, "qty": 1})

        with patch(_METRICS_MODULE) as mock_cls:
            mock_cls.get.side_effect = RuntimeError("Prometheus exploded")
            # Must complete without raising
            await b.force_flush()

        # Buffer should be cleared (flush completed successfully despite metrics crash)
        assert b._active.row_count == 0
        assert b._standby.row_count == 0

    @pytest.mark.asyncio
    async def test_metrics_table_label_matches_batcher_table_name(self) -> None:
        """The 'table' label must match the Batcher's table_name."""
        writer = _make_writer()
        table = "hft.fills"
        b = Batcher(table, flush_limit=100, writer=writer)
        await b.add({"order_id": "abc", "price": 150_000})

        registry, mock_batches, mock_rows = _mock_registry()
        with patch(_METRICS_MODULE) as mock_cls:
            mock_cls.get.return_value = registry
            await b.force_flush()

        mock_batches.labels.assert_called_with(table=table)
        mock_rows.labels.assert_called_with(table=table)

    @pytest.mark.asyncio
    async def test_flush_on_limit_also_increments_metrics(self) -> None:
        """Auto-flush triggered by reaching flush_limit must also increment metrics."""
        writer = _make_writer()
        b = Batcher("hft.orders", flush_limit=3, writer=writer)

        registry, mock_batches, mock_rows = _mock_registry()
        with patch(_METRICS_MODULE) as mock_cls:
            mock_cls.get.return_value = registry
            # Third add triggers the auto-flush
            await b.add({"price": 100_000, "qty": 1})
            await b.add({"price": 101_000, "qty": 2})
            await b.add({"price": 102_000, "qty": 3})  # triggers flush at limit

        mock_batches.labels.return_value.inc.assert_called_once()
        mock_rows.labels.return_value.inc.assert_called_once_with(3)
