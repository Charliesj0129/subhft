"""Coverage tests for hft_platform.ops.strategy_governor — missing line ranges.

Targets: quarantine, is_quarantined, rearm, build_cancel_intents,
_set_strategy_quarantine_active, _set_strategy_scope_state,
_build_transition, _tag_intent_reason.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.ops.autonomy import AutonomyMode, AutonomyTransition
from hft_platform.ops.strategy_governor import (
    StrategyHealthGovernor,
    StrategyQuarantine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_metrics():
    """Create a mock metrics object with required attributes."""
    metrics = MagicMock()
    metrics.autonomy_transitions_total = MagicMock()
    metrics.autonomy_transitions_total.labels.return_value = MagicMock()
    metrics.strategy_quarantine_active = MagicMock()
    metrics.strategy_quarantine_active.labels.return_value = MagicMock()
    metrics.autonomy_mode = MagicMock()
    metrics.autonomy_mode.labels.return_value = MagicMock()
    metrics.manual_rearm_required = MagicMock()
    metrics.manual_rearm_required.labels.return_value = MagicMock()
    return metrics


def _make_intent_factory():
    """Create a simple intent factory."""
    seq = [0]

    def factory(
        strategy_id,
        symbol,
        side,
        price,
        qty,
        tif,
        intent_type,
        target_order_id=None,
        source_ts_ns=None,
        trace_id=None,
    ):
        seq[0] += 1
        return OrderIntent(
            intent_id=seq[0],
            strategy_id=strategy_id,
            symbol=symbol,
            intent_type=intent_type,
            side=side,
            price=price,
            qty=qty,
            tif=tif,
            target_order_id=target_order_id,
        )

    return factory


# ---------------------------------------------------------------------------
# quarantine and is_quarantined (lines 62-67, 78-80)
# ---------------------------------------------------------------------------


def test_quarantine_sets_quarantine_state():
    metrics = _make_mock_metrics()
    evidence = MagicMock()
    governor = StrategyHealthGovernor(metrics=metrics, evidence_writer=evidence)

    assert governor.is_quarantined("strat1") is False

    transition = governor.quarantine("strat1", reason="strategy_exception")
    assert governor.is_quarantined("strat1") is True
    assert transition.to_mode == AutonomyMode.STRATEGY_QUARANTINED
    assert transition.manual_rearm_required is True

    # Verify metrics were called
    metrics.autonomy_transitions_total.labels.assert_called()
    metrics.strategy_quarantine_active.labels.assert_called()


def test_quarantine_already_quarantined():
    metrics = _make_mock_metrics()
    governor = StrategyHealthGovernor(metrics=metrics, evidence_writer=None)

    governor.quarantine("strat1", reason="strategy_exception")
    # Re-quarantine should use STRATEGY_QUARANTINED as from_mode
    transition = governor.quarantine("strat1", reason="strategy_reject_spike")
    assert transition.from_mode == AutonomyMode.STRATEGY_QUARANTINED


# ---------------------------------------------------------------------------
# rearm (lines 82-85)
# ---------------------------------------------------------------------------


def test_rearm_removes_quarantine():
    metrics = _make_mock_metrics()
    governor = StrategyHealthGovernor(metrics=metrics, evidence_writer=None)

    governor.quarantine("strat1", reason="strategy_exception")
    assert governor.is_quarantined("strat1") is True

    governor.rearm("strat1")
    assert governor.is_quarantined("strat1") is False


def test_rearm_not_quarantined_is_noop():
    metrics = _make_mock_metrics()
    governor = StrategyHealthGovernor(metrics=metrics, evidence_writer=None)
    governor.rearm("strat1")  # should not raise
    assert governor.is_quarantined("strat1") is False


# ---------------------------------------------------------------------------
# build_cancel_intents (lines 97-99, 103, 106)
# ---------------------------------------------------------------------------


def test_build_cancel_intents_not_quarantined():
    governor = StrategyHealthGovernor(metrics=_make_mock_metrics(), evidence_writer=None)
    intents = governor.build_cancel_intents(
        "strat1",
        live_orders=[("TXFD6", "ord1")],
        intent_factory=_make_intent_factory(),
    )
    assert intents == []


def test_build_cancel_intents_quarantined():
    governor = StrategyHealthGovernor(metrics=_make_mock_metrics(), evidence_writer=None)
    governor.quarantine("strat1", reason="strategy_exception")

    intents = governor.build_cancel_intents(
        "strat1",
        live_orders=[("TXFD6", "ord1"), ("TXFD6", "ord2")],
        intent_factory=_make_intent_factory(),
    )
    assert len(intents) == 2
    for intent in intents:
        assert intent.intent_type == IntentType.CANCEL


def test_build_cancel_intents_with_trace_info():
    governor = StrategyHealthGovernor(metrics=_make_mock_metrics(), evidence_writer=None)
    governor.quarantine("strat1", reason="strategy_exception")

    intents = governor.build_cancel_intents(
        "strat1",
        live_orders=[("TXFD6", "ord1")],
        intent_factory=_make_intent_factory(),
        source_ts_ns=12345,
        trace_id="trace-001",
    )
    assert len(intents) == 1


# ---------------------------------------------------------------------------
# _set_strategy_quarantine_active (line 111)
# ---------------------------------------------------------------------------


def test_set_strategy_quarantine_active_no_metrics():  # noqa: no-assert
    governor = StrategyHealthGovernor(metrics=None, evidence_writer=None)
    # Should not raise even with None metrics
    governor._set_strategy_quarantine_active("strat1", active=True)


def test_set_strategy_quarantine_active_no_metric_attr():  # noqa: no-assert
    metrics = MagicMock(spec=[])  # empty spec, no attributes
    governor = StrategyHealthGovernor(metrics=metrics, evidence_writer=None)
    governor._set_strategy_quarantine_active("strat1", active=True)


# ---------------------------------------------------------------------------
# _set_strategy_scope_state (lines 133-134)
# ---------------------------------------------------------------------------


def test_set_strategy_scope_state_no_metrics():  # noqa: no-assert
    governor = StrategyHealthGovernor(metrics=None, evidence_writer=None)
    governor._set_strategy_scope_state()  # should not raise


def test_set_strategy_scope_state_with_quarantine():
    metrics = _make_mock_metrics()
    governor = StrategyHealthGovernor(metrics=metrics, evidence_writer=None)
    governor.quarantine("strat1", reason="strategy_exception")
    governor._set_strategy_scope_state()
    # autonomy_mode should be set to STRATEGY_QUARANTINED value
    metrics.autonomy_mode.labels.assert_called()


def test_set_strategy_scope_state_without_quarantine():
    metrics = _make_mock_metrics()
    governor = StrategyHealthGovernor(metrics=metrics, evidence_writer=None)
    governor._set_strategy_scope_state()
    # Should set NORMAL mode
    metrics.autonomy_mode.labels.assert_called()


# ---------------------------------------------------------------------------
# _build_transition (lines 141-143, 146-155)
# ---------------------------------------------------------------------------


def test_build_transition_known_reason():
    governor = StrategyHealthGovernor(metrics=_make_mock_metrics(), evidence_writer=None)
    transition = governor._build_transition(
        from_mode=AutonomyMode.NORMAL,
        reason="strategy_exception",
    )
    assert transition.to_mode == AutonomyMode.STRATEGY_QUARANTINED
    assert transition.metric_reason == "strategy_exception"


def test_build_transition_unknown_reason_with_prefix_match():
    """When reason is unknown, try strategy_ prefix."""
    governor = StrategyHealthGovernor(metrics=_make_mock_metrics(), evidence_writer=None)
    # "reject_spike" is not in _ALLOWED_REASON_CODES, but "strategy_reject_spike" is
    transition = governor._build_transition(
        from_mode=AutonomyMode.NORMAL,
        reason="reject_spike",
    )
    assert transition.to_mode == AutonomyMode.STRATEGY_QUARANTINED
    assert transition.metric_reason == "strategy_reject_spike"


def test_build_transition_fully_unknown_reason():
    """Both reason and strategy_reason are unknown."""
    governor = StrategyHealthGovernor(metrics=_make_mock_metrics(), evidence_writer=None)
    transition = governor._build_transition(
        from_mode=AutonomyMode.NORMAL,
        reason="completely_unknown_reason_xyz",
    )
    assert transition.metric_reason == "unknown"


# ---------------------------------------------------------------------------
# _tag_intent_reason (lines 146-155)
# ---------------------------------------------------------------------------


def test_tag_intent_reason_order_intent():
    governor = StrategyHealthGovernor(metrics=_make_mock_metrics(), evidence_writer=None)
    intent = OrderIntent(
        intent_id=1,
        strategy_id="s1",
        symbol="TXFD6",
        intent_type=IntentType.CANCEL,
        side=Side.BUY,
        price=0,
        qty=0,
        tif=TIF.LIMIT,
    )
    tagged = governor._tag_intent_reason(intent, "test_reason")
    assert tagged.reason == "test_reason"


def test_tag_intent_reason_dict():
    governor = StrategyHealthGovernor(metrics=_make_mock_metrics(), evidence_writer=None)
    intent = {"strategy_id": "s1", "symbol": "TXFD6"}
    tagged = governor._tag_intent_reason(intent, "test_reason")
    assert tagged["reason"] == "test_reason"


def test_tag_intent_reason_tuple():
    governor = StrategyHealthGovernor(metrics=_make_mock_metrics(), evidence_writer=None)
    # Create a typed_intent_v1 tuple (16+ elements)
    intent = tuple(["typed_intent_v1"] + [None] * 15)
    tagged = governor._tag_intent_reason(intent, "test_reason")
    assert tagged[12] == "test_reason"


def test_tag_intent_reason_object_without_reason():
    governor = StrategyHealthGovernor(metrics=_make_mock_metrics(), evidence_writer=None)

    class DummyIntent:
        pass

    intent = DummyIntent()
    tagged = governor._tag_intent_reason(intent, "test_reason")
    assert tagged is intent  # returned as-is


# ---------------------------------------------------------------------------
# StrategyQuarantine dataclass
# ---------------------------------------------------------------------------


def test_strategy_quarantine_dataclass():
    transition = AutonomyTransition(
        scope="strategy",
        from_mode=AutonomyMode.NORMAL,
        to_mode=AutonomyMode.STRATEGY_QUARANTINED,
        reason="test",
    )
    sq = StrategyQuarantine(
        strategy_id="s1",
        reason="test",
        transition=transition,
    )
    assert sq.strategy_id == "s1"
    assert sq.reason == "test"
    assert sq.transition.to_mode == AutonomyMode.STRATEGY_QUARANTINED


# ---------------------------------------------------------------------------
# Evidence writer integration (lines 78-80)
# ---------------------------------------------------------------------------


def test_quarantine_with_evidence_writer():
    metrics = _make_mock_metrics()
    evidence = MagicMock()
    governor = StrategyHealthGovernor(metrics=metrics, evidence_writer=evidence)

    governor.quarantine("strat1", reason="strategy_exception")
    evidence.record_transition.assert_called_once()
    call_kwargs = evidence.record_transition.call_args[1]
    assert call_kwargs["scope"] == "strategy"
    assert call_kwargs["metadata"]["strategy_id"] == "strat1"
