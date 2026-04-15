"""Tests for fault event data models."""
from __future__ import annotations

import pytest


def test_fault_category_values():
    from hft_platform.healing.fault import FaultCategory
    assert FaultCategory.FEED == "feed"
    assert FaultCategory.BROKER == "broker"
    assert FaultCategory.INFRA == "infra"
    assert FaultCategory.POSITION == "position"
    assert FaultCategory.CONTRACT == "contract"
    assert FaultCategory.EXECUTION == "execution"


def test_fault_severity_ordering():
    from hft_platform.healing.fault import FaultSeverity
    assert FaultSeverity.TRANSIENT < FaultSeverity.DEGRADED
    assert FaultSeverity.DEGRADED < FaultSeverity.IMPAIRED
    assert FaultSeverity.IMPAIRED < FaultSeverity.CRITICAL


def test_risk_level_values():
    from hft_platform.healing.fault import RiskLevel
    assert RiskLevel.AUTO == 0
    assert RiskLevel.CONFIRM == 1


def test_fault_event_creation():
    from hft_platform.healing.fault import FaultCategory, FaultEvent, FaultSeverity
    event = FaultEvent(
        fault_id="f-001", category=FaultCategory.FEED,
        severity=FaultSeverity.DEGRADED, source="shioaji_client",
        description="Feed gap 2.5s on TMFD6",
        ts_ns=1_700_000_000_000_000_000,
        context={"symbol": "TMFD6", "gap_s": 2.5},
    )
    assert event.category == FaultCategory.FEED
    assert event.severity == FaultSeverity.DEGRADED
    assert event.context["symbol"] == "TMFD6"


def test_fault_event_is_frozen():
    from hft_platform.healing.fault import FaultCategory, FaultEvent, FaultSeverity
    event = FaultEvent(
        fault_id="f-002", category=FaultCategory.BROKER,
        severity=FaultSeverity.IMPAIRED, source="broker_client",
        description="Broker disconnected",
        ts_ns=1_700_000_000_000_000_000, context=None,
    )
    with pytest.raises(AttributeError):
        event.severity = FaultSeverity.CRITICAL
