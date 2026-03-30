"""Integration tests for ElectronicEye strategy."""
from __future__ import annotations
from unittest.mock import MagicMock
import pytest


def _make_strategy(**overrides):
    from hft_platform.strategies.electronic_eye import ElectronicEye
    defaults = {
        "strategy_id": "electronic_eye",
        "quoter": {"min_edge_ticks": 2, "max_contracts_per_strike": 5, "refresh_interval_ms": 500, "cancel_on_stale_ms": 2000},
        "hedger": {"hedge_instrument": "TXFR1", "delta_threshold_lots": 3, "hedge_order_type": "MKT", "hedge_tif": "IOC", "hedge_cooldown_ms": 1000, "max_hedge_qty_per_order": 10},
        "guardian": {"warn_utilization_pct": 80, "stress_interval_s": 60, "max_worst_case_pnl_ntd": -500000},
        "publish": {"channel": "monitor:portfolio:greeks", "interval_ms": 1000},
    }
    defaults.update(overrides)
    return ElectronicEye(**defaults)


def test_electronic_eye_instantiation():
    s = _make_strategy()
    assert s.strategy_id == "electronic_eye"
    assert s.enabled is True


def test_electronic_eye_guardian_state():
    from hft_platform.strategies.electronic_eye import EyeState
    s = _make_strategy()
    assert s.guardian.state == EyeState.INIT


def test_on_risk_feedback_greeks_rejection():
    from hft_platform.contracts.strategy import RiskFeedback
    from hft_platform.strategies.electronic_eye import EyeState
    s = _make_strategy()
    s.guardian.activate()
    fb = RiskFeedback(intent_id=1, strategy_id="electronic_eye", symbol="TXO20000D6", reason_code="GREEKS_DELTA_LIMIT", timestamp_ns=1000)
    s.on_risk_feedback(fb)
    assert s.guardian.state == EyeState.RESTRICT


def test_on_risk_feedback_non_greeks_ignored():
    from hft_platform.contracts.strategy import RiskFeedback
    from hft_platform.strategies.electronic_eye import EyeState
    s = _make_strategy()
    s.guardian.activate()
    fb = RiskFeedback(intent_id=1, strategy_id="electronic_eye", symbol="TXO20000D6", reason_code="PRICE_OUTSIDE_BAND", timestamp_ns=1000)
    s.on_risk_feedback(fb)
    assert s.guardian.state == EyeState.QUOTING


def test_electronic_eye_has_hedger():
    s = _make_strategy()
    assert s._hedger._threshold == 3
    assert s._hedger._max_qty == 10


def test_electronic_eye_has_quoter_state():
    s = _make_strategy()
    assert s._quoter_state._max_per_strike == 5
