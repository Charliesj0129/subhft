"""Tests for OrphanDetector."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from hft_platform.core import timebase
from hft_platform.order.orphan_detector import OrphanDetector


def _make_order(age_ns: int = 0) -> dict:
    """Create a mock order dict with a created_ns timestamp."""
    return {"order_id": "O1", "symbol": "2330", "created_ns": timebase.now_ns() - age_ns}


class TestOrphanDetectorClassify:
    def test_fresh_order_not_stale(self) -> None:
        client = MagicMock()
        detector = OrphanDetector(client, stale_threshold_s=60.0)
        orders = [_make_order(age_ns=1_000_000_000)]  # 1 second old
        stale, active = detector._classify(orders)
        assert len(stale) == 0
        assert len(active) == 1

    def test_old_order_is_stale(self) -> None:
        client = MagicMock()
        detector = OrphanDetector(client, stale_threshold_s=60.0)
        orders = [_make_order(age_ns=120_000_000_000)]  # 120 seconds old
        stale, active = detector._classify(orders)
        assert len(stale) == 1
        assert len(active) == 0

    def test_order_without_created_ns_not_stale(self) -> None:
        client = MagicMock()
        detector = OrphanDetector(client, stale_threshold_s=60.0)
        orders = [{"order_id": "O1", "created_ns": 0}]
        stale, active = detector._classify(orders)
        assert len(stale) == 0


class TestOrphanDetectorCheckOnce:
    def test_check_once_returns_stale_orders(self) -> None:
        client = MagicMock()
        old_order = _make_order(age_ns=300_000_000_000)  # 300s old
        client.list_open_orders = MagicMock(return_value=[old_order])

        detector = OrphanDetector(client, stale_threshold_s=60.0)
        result = asyncio.get_event_loop().run_until_complete(detector.check_once())
        assert len(result) == 1

    def test_check_once_disabled_returns_empty(self) -> None:
        client = MagicMock()
        detector = OrphanDetector(client)
        detector.disable()
        result = asyncio.get_event_loop().run_until_complete(detector.check_once())
        assert result == []

    def test_orphan_callback_invoked(self) -> None:
        client = MagicMock()
        old_order = _make_order(age_ns=300_000_000_000)
        client.list_open_orders = MagicMock(return_value=[old_order])

        orphans_seen: list = []
        detector = OrphanDetector(
            client, stale_threshold_s=60.0, on_orphan=lambda o: orphans_seen.extend(o),
        )
        asyncio.get_event_loop().run_until_complete(detector.check_once())
        assert len(orphans_seen) == 1

    def test_orphan_count_accumulates(self) -> None:
        client = MagicMock()
        old_order = _make_order(age_ns=300_000_000_000)
        client.list_open_orders = MagicMock(return_value=[old_order])

        detector = OrphanDetector(client, stale_threshold_s=60.0)
        assert detector.orphan_count == 0
        asyncio.get_event_loop().run_until_complete(detector.check_once())
        assert detector.orphan_count == 1
        asyncio.get_event_loop().run_until_complete(detector.check_once())
        assert detector.orphan_count == 2
