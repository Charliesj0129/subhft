"""Tests for ShadowOrderWriter — ClickHouse batch writer for shadow order records."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.order.shadow_writer import ShadowOrderWriter

_SAMPLE_RECORD = {
    "ts_ns": 1_700_000_000_000_000_000,
    "strategy_id": "test_strat",
    "symbol": "2330",
    "side": "BUY",
    "price": 5000000,
    "qty": 1,
    "intent_type": "NEW",
    "intent_id": "test-001",
    "shadow": True,
}


def _make_writer(batch_size: int = 50, enabled: bool = True) -> ShadowOrderWriter:
    return ShadowOrderWriter(batch_size=batch_size, enabled=enabled)


class TestShadowOrderWriterBatching:
    def test_writer_batches_records(self):
        """Adding 2 records with batch_size=3 should not flush — pending_count == 2."""
        writer = _make_writer(batch_size=3, enabled=True)
        writer.add(_SAMPLE_RECORD)
        writer.add(_SAMPLE_RECORD)
        assert writer.pending_count == 2

    def test_writer_flushes_at_batch_size(self):
        """Adding records equal to batch_size triggers automatic flush."""
        mock_client = MagicMock()
        writer = _make_writer(batch_size=2, enabled=True)

        with patch("hft_platform.order.shadow_writer._get_ch_client", return_value=mock_client):
            writer.add(_SAMPLE_RECORD)
            writer.add(_SAMPLE_RECORD)  # triggers flush

        mock_client.execute.assert_called_once()
        assert writer.pending_count == 0

    def test_writer_flush_on_demand(self):
        """Calling flush() with 1 pending record invokes client.execute once."""
        mock_client = MagicMock()
        writer = _make_writer(batch_size=10, enabled=True)

        with patch("hft_platform.order.shadow_writer._get_ch_client", return_value=mock_client):
            writer.add(_SAMPLE_RECORD)
            writer.flush()

        mock_client.execute.assert_called_once()
        assert writer.pending_count == 0

    def test_writer_flush_empty_is_noop(self):
        """flush() with no pending records makes no client calls."""
        mock_client = MagicMock()
        writer = _make_writer(batch_size=10, enabled=True)

        with patch("hft_platform.order.shadow_writer._get_ch_client", return_value=mock_client):
            writer.flush()

        mock_client.execute.assert_not_called()

    def test_writer_flush_failure_does_not_raise(self):
        """If client.execute raises, flush() catches the error and clears pending."""
        mock_client = MagicMock()
        mock_client.execute.side_effect = RuntimeError("CH unavailable")
        writer = _make_writer(batch_size=10, enabled=True)

        with patch("hft_platform.order.shadow_writer._get_ch_client", return_value=mock_client):
            writer.add(_SAMPLE_RECORD)
            writer.flush()  # must not raise

        assert writer.pending_count == 0

    def test_writer_disabled_does_not_call_client(self):
        """When disabled, flush() logs and drops records without calling client."""
        mock_client = MagicMock()
        writer = _make_writer(batch_size=2, enabled=False)

        with patch("hft_platform.order.shadow_writer._get_ch_client", return_value=mock_client):
            writer.add(_SAMPLE_RECORD)
            writer.flush()

        mock_client.execute.assert_not_called()
        assert writer.pending_count == 0
