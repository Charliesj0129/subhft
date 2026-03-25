"""Tests for OrphanDetector: classify, find_stale, disable/enable."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.order.orphan_detector import OrphanClassification, OrphanDetector


def _make_order(order_id: str, symbol: str, age_ns: int, now_ns: int) -> dict:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "timestamp_ns": now_ns - age_ns,
    }


class TestClassify:
    def test_active_order_within_threshold(self) -> None:
        detector = OrphanDetector(stale_threshold_ns=60_000_000_000)
        now_ns = 100_000_000_000
        with patch("hft_platform.order.orphan_detector.timebase") as mock_tb:
            mock_tb.now_ns.return_value = now_ns
            orders = [_make_order("O1", "2330", 10_000_000_000, now_ns)]
            results = detector.classify(orders)
        assert len(results) == 1
        assert results[0].status == "active"
        assert results[0].order_id == "O1"

    def test_stale_order_beyond_threshold(self) -> None:
        detector = OrphanDetector(stale_threshold_ns=60_000_000_000)
        now_ns = 200_000_000_000
        with patch("hft_platform.order.orphan_detector.timebase") as mock_tb:
            mock_tb.now_ns.return_value = now_ns
            orders = [_make_order("O2", "2330", 120_000_000_000, now_ns)]
            results = detector.classify(orders)
        assert results[0].status == "stale"

    def test_orphan_order_not_in_tracker(self) -> None:
        tracker = MagicMock()
        tracker.active_order_ids = MagicMock(return_value=["O1"])
        detector = OrphanDetector(stale_threshold_ns=60_000_000_000, local_tracker=tracker)
        now_ns = 100_000_000_000
        with patch("hft_platform.order.orphan_detector.timebase") as mock_tb:
            mock_tb.now_ns.return_value = now_ns
            orders = [_make_order("O_ORPHAN", "2330", 10_000_000_000, now_ns)]
            results = detector.classify(orders)
        assert results[0].status == "orphan"


class TestFindStale:
    def test_returns_only_stale_and_orphan(self) -> None:
        detector = OrphanDetector(stale_threshold_ns=50_000_000_000)
        now_ns = 200_000_000_000
        with patch("hft_platform.order.orphan_detector.timebase") as mock_tb:
            mock_tb.now_ns.return_value = now_ns
            orders = [
                _make_order("O1", "2330", 10_000_000_000, now_ns),  # active
                _make_order("O2", "2317", 100_000_000_000, now_ns),  # stale
            ]
            stale = detector.find_stale(orders)
        assert len(stale) == 1
        assert stale[0].order_id == "O2"


class TestDisableEnable:
    def test_disabled_returns_empty(self) -> None:
        detector = OrphanDetector()
        detector.disable()
        assert detector.enabled is False
        now_ns = 100_000_000_000
        with patch("hft_platform.order.orphan_detector.timebase") as mock_tb:
            mock_tb.now_ns.return_value = now_ns
            results = detector.classify([_make_order("O1", "2330", 10_000_000_000, now_ns)])
        assert results == []

    def test_enable_restores_classification(self) -> None:
        detector = OrphanDetector()
        detector.disable()
        detector.enable()
        assert detector.enabled is True
        now_ns = 100_000_000_000
        with patch("hft_platform.order.orphan_detector.timebase") as mock_tb:
            mock_tb.now_ns.return_value = now_ns
            results = detector.classify([_make_order("O1", "2330", 10_000_000_000, now_ns)])
        assert len(results) == 1
