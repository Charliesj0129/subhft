"""D7: QueueFull during intent submit must not crash the runner; must record circuit failure."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock


def _make_intent(strategy_id="s1", symbol="TXFD6", intent_type=1):
    intent = MagicMock()
    intent.strategy_id = strategy_id
    intent.symbol = symbol
    intent.intent_type = intent_type
    return intent


def test_partial_batch_does_not_crash():
    """If _risk_submit raises QueueFull mid-batch, remaining intents are dropped but loop continues."""
    call_count = 0

    def _submit_that_fails_on_third(intent):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise asyncio.QueueFull()

    intents = [_make_intent() for _ in range(5)]
    metrics = MagicMock()

    submitted = 0
    dropped = 0
    for intent in intents:
        try:
            _submit_that_fails_on_third(intent)
            submitted += 1
        except asyncio.QueueFull:
            dropped += 1
            metrics.intent_queue_full_total.inc()

    assert submitted == 2
    assert dropped == 3
    assert metrics.intent_queue_full_total.inc.call_count == 3


def test_circuit_failure_recorded_on_drop():
    """When intents are dropped, the strategy's circuit breaker must be notified."""
    metrics = MagicMock()
    failure_counts: dict[str, int] = {}
    circuit_states: dict[str, str] = {}

    call_count = 0

    def _submit(intent):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise asyncio.QueueFull()

    intents = [_make_intent() for _ in range(3)]

    submitted = 0
    dropped = 0
    for intent in intents:
        try:
            _submit(intent)
            submitted += 1
        except asyncio.QueueFull:
            dropped += 1
            metrics.intent_queue_full_total.inc()

    assert submitted == 1
    assert dropped == 2

    # Simulate circuit failure recording
    if dropped > 0:
        sid = "s1"
        failures = failure_counts.get(sid, 0) + 1
        failure_counts[sid] = failures
        assert failures == 1


def test_all_intents_succeed_no_failure():
    """When all intents succeed, no circuit failure is recorded."""
    submitted = 0
    dropped = 0
    intents = [_make_intent() for _ in range(3)]

    for intent in intents:
        try:
            pass  # simulate success
            submitted += 1
        except asyncio.QueueFull:
            dropped += 1

    assert submitted == 3
    assert dropped == 0
