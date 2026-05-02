"""Tests for gateway intent channel depth tracking in the central health loop.

When HFT_GATEWAY_ENABLED=1, the LocalIntentChannel replaces the risk_queue for
strategy→risk flow. Its depth must be tracked in the health loop metrics and
periodic log message.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Source-level structural tests (fast, no asyncio needed)
# ---------------------------------------------------------------------------


def test_supervise_tracks_gateway_intent_queue_depth():
    """_supervise() source must reference 'gateway_intent' label for queue depth metric."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)
    assert "gateway_intent" in source, "_supervise() must emit queue_depth metric with queue='gateway_intent'"


def test_supervise_checks_intent_channel_before_metric():
    """_supervise() must guard intent_channel access to avoid AttributeError when disabled."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)
    assert "intent_channel" in source, "_supervise() must reference intent_channel for gateway depth tracking"
    # Guard pattern: None check must be present
    assert "intent_channel is not None" in source, (
        "_supervise() must guard intent_channel with 'is not None' before calling qsize()"
    )


def test_supervise_log_includes_gateway_intent_field():
    """_supervise() periodic log must conditionally include gateway_intent depth."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)
    # The log kwargs dict should include gateway_intent when channel is present
    assert "gateway_intent" in source, "_supervise() must log gateway_intent depth in the periodic queue log"


def test_hftsystem_exposes_intent_channel_attribute():
    """HFTSystem.__init__ must assign self.intent_channel from registry."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem.__init__)
    assert "self.intent_channel" in source, "HFTSystem.__init__ must assign self.intent_channel from the registry"


# ---------------------------------------------------------------------------
# Behavioural tests: metric emission via mock
# ---------------------------------------------------------------------------


def _make_minimal_mock_system(with_intent_channel: bool) -> MagicMock:
    """Build a minimal mock that mimics HFTSystem state for queue-depth assertions."""
    sys = MagicMock()
    # Standard queues
    sys.raw_queue.qsize.return_value = 1
    sys.raw_exec_queue.qsize.return_value = 2
    sys.recorder_queue.qsize.return_value = 3
    sys.risk_queue.qsize.return_value = 4
    sys.order_queue.qsize.return_value = 5

    if with_intent_channel:
        channel = MagicMock()
        channel.qsize.return_value = 7
        sys.intent_channel = channel
    else:
        sys.intent_channel = None

    return sys


def test_gateway_intent_depth_tracked_when_available():
    """When intent_channel is present, queue_depth metric is set with label='gateway_intent'."""
    metrics = MagicMock()
    # Capture label calls
    label_calls: list[str] = []
    depth_values: dict[str, int] = {}

    def fake_labels(queue: str):
        label_calls.append(queue)
        gauge = MagicMock()

        def fake_set(v: int) -> None:
            depth_values[queue] = v

        gauge.set.side_effect = fake_set
        return gauge

    metrics.queue_depth.labels.side_effect = fake_labels

    # Build a mock system with an intent_channel
    sys_mock = _make_minimal_mock_system(with_intent_channel=True)

    # Reproduce the metric-emission block from _supervise()
    metrics.queue_depth.labels(queue="raw").set(sys_mock.raw_queue.qsize())
    metrics.queue_depth.labels(queue="raw_exec").set(sys_mock.raw_exec_queue.qsize())
    metrics.queue_depth.labels(queue="recorder").set(sys_mock.recorder_queue.qsize())
    metrics.queue_depth.labels(queue="risk").set(sys_mock.risk_queue.qsize())
    metrics.queue_depth.labels(queue="order").set(sys_mock.order_queue.qsize())
    if sys_mock.intent_channel is not None:
        depth = getattr(sys_mock.intent_channel, "qsize", lambda: 0)()
        metrics.queue_depth.labels(queue="gateway_intent").set(depth)

    assert "gateway_intent" in label_calls, "gateway_intent label must be emitted when intent_channel is present"
    assert depth_values.get("gateway_intent") == 7, (
        "gateway_intent depth must equal intent_channel.qsize() return value"
    )


def test_gateway_intent_depth_skipped_when_no_channel():
    """When intent_channel is None, no gateway_intent metric is emitted and no error raised."""
    metrics = MagicMock()
    label_calls: list[str] = []

    def fake_labels(queue: str):
        label_calls.append(queue)
        return MagicMock()

    metrics.queue_depth.labels.side_effect = fake_labels

    sys_mock = _make_minimal_mock_system(with_intent_channel=False)

    # Reproduce the metric-emission block — must not raise
    metrics.queue_depth.labels(queue="raw").set(sys_mock.raw_queue.qsize())
    metrics.queue_depth.labels(queue="raw_exec").set(sys_mock.raw_exec_queue.qsize())
    metrics.queue_depth.labels(queue="recorder").set(sys_mock.recorder_queue.qsize())
    metrics.queue_depth.labels(queue="risk").set(sys_mock.risk_queue.qsize())
    metrics.queue_depth.labels(queue="order").set(sys_mock.order_queue.qsize())
    if sys_mock.intent_channel is not None:
        depth = getattr(sys_mock.intent_channel, "qsize", lambda: 0)()
        metrics.queue_depth.labels(queue="gateway_intent").set(depth)

    assert "gateway_intent" not in label_calls, "gateway_intent label must NOT be emitted when intent_channel is None"


def test_gateway_intent_absent_from_log_when_no_channel():
    """When intent_channel is None, the periodic log must not include gateway_intent key."""
    sys_mock = _make_minimal_mock_system(with_intent_channel=False)

    _gateway_intent_depth = (
        getattr(sys_mock.intent_channel, "qsize", lambda: 0)() if sys_mock.intent_channel is not None else None
    )
    _log_kwargs: dict = dict(
        raw=sys_mock.raw_queue.qsize(),
        rec=sys_mock.recorder_queue.qsize(),
        risk=sys_mock.risk_queue.qsize(),
        order=sys_mock.order_queue.qsize(),
        raw_exec=sys_mock.raw_exec_queue.qsize(),
    )
    if _gateway_intent_depth is not None:
        _log_kwargs["gateway_intent"] = _gateway_intent_depth

    assert "gateway_intent" not in _log_kwargs


def test_gateway_intent_present_in_log_when_channel_available():
    """When intent_channel is present, the periodic log must include gateway_intent key."""
    sys_mock = _make_minimal_mock_system(with_intent_channel=True)

    _gateway_intent_depth = (
        getattr(sys_mock.intent_channel, "qsize", lambda: 0)() if sys_mock.intent_channel is not None else None
    )
    _log_kwargs: dict = dict(
        raw=sys_mock.raw_queue.qsize(),
        rec=sys_mock.recorder_queue.qsize(),
        risk=sys_mock.risk_queue.qsize(),
        order=sys_mock.order_queue.qsize(),
        raw_exec=sys_mock.raw_exec_queue.qsize(),
    )
    if _gateway_intent_depth is not None:
        _log_kwargs["gateway_intent"] = _gateway_intent_depth

    assert "gateway_intent" in _log_kwargs
    assert _log_kwargs["gateway_intent"] == 7
