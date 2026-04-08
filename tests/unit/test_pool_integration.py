"""Tests for FacadeSlot integration into QuoteConnectionPool.

Verifies that:
1. FacadeSlots are created alongside facades in create_facades()
2. get_healthy_feed_gap_s() excludes non-CONNECTED slots
3. reconnect() skips CONNECTED facades and only reconnects unhealthy ones
4. subscribe_all() wraps callbacks to update last_data_mono
5. _schedule_reconnect() is idempotent for RECOVERING slots
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeState


def _write_symbols_yaml(path: str, symbols: list[dict[str, Any]]) -> None:
    """Write a minimal symbols YAML file."""
    with open(path, "w") as f:
        yaml.safe_dump({"symbols": symbols}, f, sort_keys=False)


def _make_pool(
    symbols: list[dict[str, Any]] | None = None,
    num_conns: int = 2,
) -> Any:
    """Create a QuoteConnectionPool with mocked facades.

    Returns (pool, symbols_path).
    """
    if symbols is None:
        symbols = [
            {"code": "2330", "exchange": "TSE", "group": 0},
            {"code": "2317", "exchange": "TSE", "group": 0},
            {"code": "TXFD6", "exchange": "FUT", "group": 1},
        ]
    tmp = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w")
    yaml.safe_dump({"symbols": symbols}, tmp, sort_keys=False)
    tmp.close()

    from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
        QuoteConnectionPool,
    )

    pool = QuoteConnectionPool(
        symbols_path=tmp.name,
        shioaji_cfg={"api_key": "test", "secret_key": "test"},
        num_conns=num_conns,
    )
    return pool, tmp.name


def _mock_facade(logged_in: bool = True) -> MagicMock:
    """Create a mock ShioajiClientFacade."""
    facade = MagicMock()
    facade.logged_in = logged_in
    facade.subscribed_count = 3
    facade.reconnect.return_value = True
    facade.login.return_value = True
    return facade


class TestSlotsCreatedOnCreateFacades:
    """Verify that create_facades() populates _slots with correct FacadeSlot objects."""

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_slots_length_matches_num_conns(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            assert len(pool._slots) == 2
            assert len(pool._clients) == 2
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_slot_conn_ids_are_sequential(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            assert pool._slots[0].conn_id == "0"
            assert pool._slots[1].conn_id == "1"
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_slot_symbols_populated_from_shard(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            # Group 0 has 2330, 2317
            assert pool._slots[0].symbols == {"2330", "2317"}
            # Group 1 has TXFD6
            assert pool._slots[1].symbols == {"TXFD6"}
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_slot_initial_state_is_recovering(self, mock_facade_cls: MagicMock) -> None:
        """Slots start RECOVERING until subscribe_all completes (H3 fix)."""
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            for slot in pool._slots:
                assert slot.state == FacadeState.RECOVERING
        finally:
            pool.cleanup_shards()
            os.unlink(path)


class TestGetHealthyFeedGapS:
    """Verify get_healthy_feed_gap_s() excludes non-CONNECTED slots."""

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_returns_inf_when_all_degraded(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            for slot in pool._slots:
                slot.state = FacadeState.DEGRADED
            assert pool.get_healthy_feed_gap_s() == float("inf")
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_returns_max_gap_among_connected(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            for slot in pool._slots:
                slot.state = FacadeState.CONNECTED
            # Make slot 0 have a larger gap by setting last_data_mono in the past
            pool._slots[0].last_data_mono = time.monotonic() - 5.0
            pool._slots[1].last_data_mono = time.monotonic() - 1.0
            gap = pool.get_healthy_feed_gap_s()
            assert gap >= 4.5  # slot 0 gap should be ~5s
            assert gap < 6.0
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_excludes_degraded_slot_from_gap(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            # Slot 0 is DEGRADED with huge gap — should be excluded
            pool._slots[0].state = FacadeState.DEGRADED
            pool._slots[0].last_data_mono = time.monotonic() - 100.0
            # Slot 1 is CONNECTED with small gap
            pool._slots[1].state = FacadeState.CONNECTED
            pool._slots[1].last_data_mono = time.monotonic() - 0.5
            gap = pool.get_healthy_feed_gap_s()
            assert gap < 2.0  # Only slot 1's gap counts
        finally:
            pool.cleanup_shards()
            os.unlink(path)


class TestReconnectPerFacade:
    """Verify reconnect() targets only non-CONNECTED facades."""

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_skips_connected_facades(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            for slot in pool._slots:
                slot.state = FacadeState.CONNECTED
            # Both CONNECTED — reconnect should be a no-op, return True
            result = pool.reconnect(reason="test")
            assert result is True
            # Neither facade's reconnect() should have been called
            for slot in pool._slots:
                slot.facade.reconnect.assert_not_called()
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_reconnects_disconnected_facade(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            pool._slots[0].state = FacadeState.CONNECTED
            pool._slots[1].state = FacadeState.DISCONNECTED
            result = pool.reconnect(reason="test")
            assert result is True
            # Slot 0 (CONNECTED) should NOT have been reconnected
            pool._slots[0].facade.reconnect.assert_not_called()
            # Slot 1 (DISCONNECTED) should have been reconnected
            pool._slots[1].facade.reconnect.assert_called_once()
            assert pool._slots[1].state == FacadeState.CONNECTED
            assert pool._slots[1].reconnect_failures == 0
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_force_reconnects_all_facades(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            result = pool.reconnect(reason="test", force=True)
            assert result is True
            for slot in pool._slots:
                slot.facade.reconnect.assert_called_once()
                assert slot.state == FacadeState.CONNECTED
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_reconnect_failure_increments_counter(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            pool._slots[0].state = FacadeState.DISCONNECTED
            pool._slots[0].facade.reconnect.return_value = False
            pool._slots[1].state = FacadeState.CONNECTED
            result = pool.reconnect(reason="test")
            assert result is False
            assert pool._slots[0].reconnect_failures == 1
            assert pool._slots[0].state == FacadeState.DISCONNECTED
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_reconnect_exception_increments_counter(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            pool._slots[0].state = FacadeState.DISCONNECTED
            pool._slots[0].facade.reconnect.side_effect = RuntimeError("network down")
            pool._slots[1].state = FacadeState.CONNECTED
            result = pool.reconnect(reason="test")
            assert result is False
            assert pool._slots[0].reconnect_failures == 1
            assert pool._slots[0].state == FacadeState.DISCONNECTED
        finally:
            pool.cleanup_shards()
            os.unlink(path)


class TestScheduleReconnect:
    """Verify _schedule_reconnect is idempotent for RECOVERING slots."""

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_schedule_actually_reconnects(self, mock_facade_cls: MagicMock) -> None:
        """_schedule_reconnect spawns a thread that calls facade.reconnect (C1 fix)."""
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            pool._slots[0].state = FacadeState.DEGRADED
            pool._schedule_reconnect("0")
            # Wait for background thread to complete
            import threading
            for t in threading.enumerate():
                if t.name == "facade-reconnect-0":
                    t.join(timeout=5)
            pool._slots[0].facade.reconnect.assert_called_once()
            assert pool._slots[0].state == FacadeState.CONNECTED
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_schedule_noop_when_already_recovering(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            pool._slots[0].state = FacadeState.RECOVERING
            before_mono = pool._slots[0].last_reconnect_mono
            pool._schedule_reconnect("0")
            # Should not have updated the timestamp
            assert pool._slots[0].last_reconnect_mono == before_mono
        finally:
            pool.cleanup_shards()
            os.unlink(path)


class TestNotifyWarmupReset:
    """Verify _notify_warmup_reset calls LOB and feature engine reset methods."""

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_calls_lob_and_feature_engine_reset(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            mock_lob = MagicMock()
            mock_fe = MagicMock()
            pool.set_reset_targets(mock_lob, mock_fe)
            pool._notify_warmup_reset("0")
            mock_lob.reset_books_for_symbols.assert_called_once_with(pool._slots[0].symbols)
            mock_fe.reset_symbols.assert_called_once_with(pool._slots[0].symbols)
        finally:
            pool.cleanup_shards()
            os.unlink(path)

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_noop_when_no_targets_set(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()
            # Should not raise when lob/feature_engine are None
            pool._notify_warmup_reset("0")
        finally:
            pool.cleanup_shards()
            os.unlink(path)


class TestSubscribeAllWrapper:
    """Verify subscribe_all wraps callback to update last_data_mono."""

    @patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade")
    def test_callback_updates_last_data_mono(self, mock_facade_cls: MagicMock) -> None:
        mock_facade_cls.side_effect = lambda **kwargs: _mock_facade()
        pool, path = _make_pool(num_conns=2)
        try:
            pool.create_facades()

            captured_cbs: list[Any] = []

            def capture_subscribe(cb: Any) -> None:
                captured_cbs.append(cb)

            for slot in pool._slots:
                slot.facade.subscribe_basket = capture_subscribe

            # Disable options refresh for test
            with patch.dict(os.environ, {"HFT_OPTIONS_AUTO_REFRESH": "0"}):
                pool.subscribe_all(lambda: "original")

            assert len(captured_cbs) == 2

            # Set last_data_mono to the past and call the wrapper
            pool._slots[0].last_data_mono = time.monotonic() - 100.0
            before = pool._slots[0].last_data_mono
            captured_cbs[0]()
            after = pool._slots[0].last_data_mono
            assert after > before
            assert after > time.monotonic() - 1.0
        finally:
            pool.cleanup_shards()
            os.unlink(path)
