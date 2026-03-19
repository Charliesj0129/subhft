"""WU-18: Reconciliation data structure tests."""
from __future__ import annotations

from hft_platform.execution.reconciliation import PositionDiscrepancy


class TestPositionDiscrepancySeverity:

    def test_critical_on_sign_mismatch(self) -> None:
        d = PositionDiscrepancy(symbol="X", local_qty=10, broker_qty=-5, diff=15)
        assert d.is_critical is True

    def test_not_critical_on_small_diff(self) -> None:
        d = PositionDiscrepancy(symbol="X", local_qty=50, broker_qty=45, diff=5)
        assert d.is_critical is False

    def test_critical_on_large_diff(self) -> None:
        d = PositionDiscrepancy(symbol="X", local_qty=1000, broker_qty=800, diff=200)
        assert d.is_critical is True

    def test_not_critical_when_both_zero(self) -> None:
        d = PositionDiscrepancy(symbol="X", local_qty=0, broker_qty=0, diff=0)
        assert d.is_critical is False

    def test_severity_critical(self) -> None:
        d = PositionDiscrepancy(symbol="X", local_qty=10, broker_qty=-5, diff=15)
        assert d.severity == "critical"

    def test_severity_warning(self) -> None:
        d = PositionDiscrepancy(symbol="X", local_qty=50, broker_qty=35, diff=15)
        assert d.severity == "warning"

    def test_severity_info(self) -> None:
        d = PositionDiscrepancy(symbol="X", local_qty=50, broker_qty=45, diff=5)
        assert d.severity == "info"
