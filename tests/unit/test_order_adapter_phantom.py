"""Tests for D-03 phantom order candidate tracking on API timeout.

Verifies that timed-out mutating operations add entries to the phantom order
set and increment the phantom metric, while non-mutating timeouts do not.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderIntent,
    Side,
)
from hft_platform.order.adapter import OrderAdapter

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    return str(cfg)


@pytest.fixture(autouse=True)
def _mock_infra():
    """Patch heavy infra so tests don't need full stack."""
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        metrics = MagicMock()
        metrics.order_reject_total = MagicMock()
        metrics.order_actions_total = MagicMock()
        metrics.order_actions_total.labels.return_value = MagicMock()
        metrics.phantom_order_candidates_total = MagicMock()
        mm.get.return_value = metrics
        ml.get.return_value = MagicMock()
        md.return_value = MagicMock()
        yield


def _make_adapter(tmp_config: str, *, client: Any | None = None) -> OrderAdapter:
    order_q: asyncio.Queue = asyncio.Queue(maxsize=128)
    if client is None:
        client = MagicMock()
        client.place_order = MagicMock(return_value={"seq_no": "A1", "ord_no": "B2"})
        client.cancel_order = MagicMock(return_value={})
        client.update_order = MagicMock(return_value={})
        client.get_exchange = MagicMock(return_value="TSE")
        client.mode = "simulation"
        client.activate_ca = False
    return OrderAdapter(config_path=tmp_config, order_queue=order_q, broker_client=client)


def _make_intent(intent_id: int = 1, strategy_id: str = "s1") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol="2330",
        price=100_0000,
        qty=1,
        side=Side.BUY,
        intent_type=IntentType.NEW,
    )


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phantom_dispatch_does_not_inflate_reject_or_cb_metrics(tmp_config):
    """Bug D' (2026-04-20): phantom_order_candidate_dispatch_failed should NOT
    increment order_reject_total or call circuit_breaker.record_failure(). The
    order may have reached the broker (Bug 23 phantom design) — counting it as
    a rejection inflates dashboards, fires false Telegram CRITICAL alerts, and
    can eventually trip the circuit breaker on a healthy broker (observed
    14 phantoms / 5 min on 2026-04-20 incident).
    """
    adapter = _make_adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    intent = _make_intent(intent_id=99)

    # Reset CB state and metric mock counts for clean assertions
    adapter.metrics.order_reject_total.reset_mock()
    cb_failures_before = adapter.circuit_breaker.failure_count

    # Inline reproduction of the _api_worker except branch (so we can drive
    # it directly rather than spinning up the full worker with a fake-raising
    # _dispatch_to_api). After Bug D' fix, the metric/CB calls below must
    # NOT happen on the phantom-candidate code path.
    try:
        raise RuntimeError("simulated Shioaji place_order client-side exception")
    except Exception:
        await adapter._handle_dispatch_exception(intent=intent, cmd_id=99)

    adapter.metrics.order_reject_total.inc.assert_not_called()
    assert adapter.circuit_breaker.failure_count == cb_failures_before, (
        "phantom dispatch must not call circuit_breaker.record_failure()"
    )

    # The phantom candidate must still be tracked (so on_fill can reconcile)
    assert f"{intent.strategy_id}:{intent.intent_id}" in adapter._phantom_order_keys
    adapter.metrics.phantom_order_candidates_total.inc.assert_called_once()


@pytest.mark.asyncio
async def test_release_stale_phantom_pendings_emits_recovery_feedback(tmp_config):
    """Bug D (2026-04-20): phantom_pending=True feedbacks freeze strategies
    because R47.on_risk_feedback returns early on was_approved=True. When the
    broker never sends a fill or cancel callback (Shioaji client-side
    exception that didn't reach the broker), the strategy stays frozen.

    The recovery janitor must, after TTL, emit a SECOND RiskFeedback with
    ``was_approved=False`` so the strategy releases the pending counter and
    resumes quoting.
    """
    adapter = _make_adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)

    intent = _make_intent(intent_id=42)
    phantom_key = f"{intent.strategy_id}:{intent.intent_id}"

    # Simulate phantom registration (mirrors adapter.py:2118-2123 path).
    # M4: register via the canonical multi-occurrence store, then back-date
    # the timestamp so the recovery sweep treats it as eligible.
    adapter._send_dispatch_rejection(intent, "dispatch_failed", phantom_pending=True)
    from hft_platform.order.adapter import _PhantomEntry

    aged_ts = time.monotonic() - 999.0
    adapter._phantom_records[phantom_key] = [
        _PhantomEntry(
            monotonic_ts=aged_ts,
            symbol=intent.symbol,
            created_ns=0,
            intent=intent,
        )
    ]
    adapter._phantom_order_keys[phantom_key] = (aged_ts, intent.symbol)
    adapter._phantom_intents[phantom_key] = intent

    # Drain the initial phantom_pending feedback (was_approved=True)
    fb1 = sink.get_nowait()
    assert fb1.was_approved is True

    # Run recovery sweep with TTL=30s — our entry is 999s old, eligible
    released = await adapter.release_stale_phantom_pendings(ttl_s=30.0)

    assert released == 1
    assert phantom_key not in adapter._phantom_records
    assert phantom_key not in adapter._phantom_order_keys
    assert phantom_key not in adapter._phantom_intents

    fb2 = sink.get_nowait()
    assert fb2.was_approved is False, "recovery feedback must release pending"
    assert fb2.symbol == intent.symbol
    assert fb2.side == intent.side
    assert fb2.strategy_id == intent.strategy_id
    assert "phantom_recovery" in fb2.reason_code


@pytest.mark.asyncio
async def test_release_stale_phantom_pendings_skips_fresh_entries(tmp_config):
    """Fresh phantoms (within TTL) must NOT be released — broker may still
    send a fill/cancel callback. Only after TTL we assume orphaned."""
    adapter = _make_adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)

    intent = _make_intent(intent_id=43)
    phantom_key = f"{intent.strategy_id}:{intent.intent_id}"

    adapter._phantom_order_keys[phantom_key] = (time.monotonic(), intent.symbol)
    adapter._phantom_intents[phantom_key] = intent

    released = await adapter.release_stale_phantom_pendings(ttl_s=30.0)
    assert released == 0
    assert phantom_key in adapter._phantom_order_keys
    assert sink.empty()


@pytest.mark.asyncio
async def test_mutating_timeout_adds_phantom_candidate(tmp_config):
    """A mutating op timeout with intent should add to phantom set and inc metric."""
    adapter = _make_adapter(tmp_config)
    # Make _api_timeout_s very short so we timeout quickly
    adapter._api_timeout_s = 0.01

    # Broker call that hangs forever
    def _hang(*a, **kw):
        import time

        time.sleep(5)

    intent = _make_intent()

    result = await adapter._call_api(
        "place_order",
        _hang,
        intent=intent,
        max_retries=0,
    )

    assert result is None
    # R2-02: 2-part key format matching order_key convention
    expected_key = f"{intent.strategy_id}:{intent.intent_id}"
    assert expected_key in adapter._phantom_order_keys
    adapter.metrics.phantom_order_candidates_total.inc.assert_called_once()


@pytest.mark.asyncio
async def test_non_mutating_timeout_does_not_add_phantom(tmp_config):
    """A non-mutating op timeout should NOT add to phantom set."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 0.01

    def _hang(*a, **kw):
        import time

        time.sleep(5)

    intent = _make_intent()

    result = await adapter._call_api(
        "get_order_status",
        _hang,
        intent=intent,
        max_retries=0,
    )

    assert result is None
    assert len(adapter._phantom_order_keys) == 0
    adapter.metrics.phantom_order_candidates_total.inc.assert_not_called()


@pytest.mark.asyncio
async def test_mutating_timeout_without_intent_no_phantom(tmp_config):
    """A mutating op timeout without intent should NOT add to phantom set."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 0.01

    def _hang(*a, **kw):
        import time

        time.sleep(5)

    result = await adapter._call_api(
        "place_order",
        _hang,
        intent=None,
        max_retries=0,
    )

    assert result is None
    assert len(adapter._phantom_order_keys) == 0
    adapter.metrics.phantom_order_candidates_total.inc.assert_not_called()


def test_get_phantom_candidates_returns_frozenset(tmp_config):
    """get_phantom_candidates returns a frozenset copy of tracked keys.

    M4: keys come from the canonical ``_phantom_records`` store; the
    legacy ``_phantom_order_keys`` view is kept aligned for back-compat
    so this assertion still holds after upgrade.
    """
    adapter = _make_adapter(tmp_config)
    import time

    from hft_platform.order.adapter import _PhantomEntry

    now = time.monotonic()
    intent_a = _make_intent(intent_id=1)
    intent_b = _make_intent(intent_id=2, strategy_id="s2")
    adapter._phantom_records["s1:1"] = [
        _PhantomEntry(monotonic_ts=now, symbol="TXFD6", created_ns=0, intent=intent_a)
    ]
    adapter._phantom_records["s2:2"] = [
        _PhantomEntry(monotonic_ts=now, symbol="TXFD6", created_ns=0, intent=intent_b)
    ]

    result = adapter.get_phantom_candidates()

    assert isinstance(result, frozenset)
    assert result == frozenset({"s1:1", "s2:2"})
    # Mutating the returned set should not affect the adapter
    assert len(adapter._phantom_records) == 2


def test_clear_phantom_candidate_removes_key(tmp_config):
    """clear_phantom_candidate removes the specified key."""
    adapter = _make_adapter(tmp_config)
    import time

    now = time.monotonic()
    adapter._phantom_order_keys["s1:1"] = (now, "TXFD6")
    adapter._phantom_order_keys["s2:2"] = (now, "TXFD6")

    adapter.clear_phantom_candidate("s1:1")

    assert "s1:1" not in adapter._phantom_order_keys
    assert "s2:2" in adapter._phantom_order_keys


def test_clear_phantom_candidate_missing_key_noop(tmp_config):
    """clear_phantom_candidate on a nonexistent key is a no-op."""
    adapter = _make_adapter(tmp_config)
    import time

    adapter._phantom_order_keys["s1:1"] = (time.monotonic(), "TXFD6")

    adapter.clear_phantom_candidate("nonexistent:key:0")

    assert "s1:1" in adapter._phantom_order_keys
    assert len(adapter._phantom_order_keys) == 1


def test_phantom_key_format_is_two_part(tmp_config):
    """R2-02: Phantom key must use 2-part format matching order_key convention."""
    adapter = _make_adapter(tmp_config)
    import time

    adapter._phantom_order_keys["s1:42"] = (time.monotonic(), "TXFD6")

    candidates = adapter.get_phantom_candidates()
    for key in candidates:
        parts = key.split(":")
        assert len(parts) == 2, f"Expected 2-part key, got {len(parts)}-part: {key}"


@pytest.mark.asyncio
async def test_deadline_expired_increments_metric(tmp_config):
    """Expired pre-dispatch orders must increment order_deadline_expired_total."""
    import time as _time

    adapter = _make_adapter(tmp_config)
    adapter.metrics.order_deadline_expired_total = MagicMock()

    # Build a command whose deadline is already in the past
    from hft_platform.contracts.strategy import OrderCommand, StormGuardState

    past_deadline = _time.monotonic_ns() - 1_000_000  # 1 ms in the past
    cmd = OrderCommand(
        cmd_id=1,
        intent=_make_intent(),
        deadline_ns=past_deadline,
        storm_guard_state=StormGuardState.NORMAL,
    )
    await adapter.order_queue.put(cmd)

    # Run the adapter.run() coroutine briefly — the deadline check lives there
    run_task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0.05)
    adapter.running = False
    run_task.cancel()
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    adapter.metrics.order_deadline_expired_total.inc.assert_called_once()


@pytest.mark.asyncio
async def test_phantom_set_eviction_at_max_size(tmp_config):
    """R2-03: Phantom set should evict old entries when exceeding max size.

    M4 (2026-04-25): the canonical store is ``_phantom_records``; legacy
    ``_phantom_order_keys`` view is kept aligned by the helpers. Test
    pre-fills records directly with stale timestamps and verifies that
    the over-capacity sweep drops them.
    """
    adapter = _make_adapter(tmp_config)
    adapter._phantom_order_max = 5  # Low cap for testing
    adapter._api_timeout_s = 0.01

    # Pre-fill with old entries (simulate timestamps from 2 hours ago)
    import time

    from hft_platform.order.adapter import _PhantomEntry

    old_ts = time.monotonic() - 7200.0  # 2 hours ago
    for i in range(6):
        key = f"old_strat:{i}"
        intent_old = _make_intent(intent_id=i, strategy_id="old_strat")
        adapter._phantom_records[key] = [
            _PhantomEntry(
                monotonic_ts=old_ts,
                symbol="TXFD6",
                created_ns=0,
                intent=intent_old,
            )
        ]
        adapter._phantom_order_keys[key] = (old_ts, "TXFD6")
        adapter._phantom_intents[key] = intent_old

    assert adapter._phantom_record_count() == 6

    # Trigger a new phantom via timeout — this should trigger eviction
    def _hang(*a, **kw):
        time.sleep(5)

    intent = _make_intent(intent_id=999)
    await adapter._call_api("place_order", _hang, intent=intent, max_retries=0)

    # Old entries (>1 hour) should be evicted, only the new one remains
    assert adapter._phantom_record_count() <= adapter._phantom_order_max
    assert f"{intent.strategy_id}:{intent.intent_id}" in adapter._phantom_records
