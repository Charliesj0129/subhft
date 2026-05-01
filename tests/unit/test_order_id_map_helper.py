"""P1-8: every ``order_id_map`` mutation must go through the consolidated
``_set_order_id_mapping`` / ``_del_order_id_mapping`` helpers on
``OrderAdapter``. The helpers must:

1. Take ``_order_id_map_lock`` (now an RLock so batch writers can hold it).
2. Be safe under concurrent multi-thread writes (no lost updates).
3. Emit a ``debug`` structlog event with the ``source=`` argument.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from hft_platform.order.adapter import OrderAdapter


def _make_adapter() -> OrderAdapter:
    """Build a bare OrderAdapter that has only the slots needed for the
    helpers under test, bypassing the heavy load_config path."""
    adapter = OrderAdapter.__new__(OrderAdapter)
    adapter.order_id_map = {}
    adapter._order_id_map_lock = threading.RLock()
    return adapter


def test_set_helper_writes_under_lock():
    adapter = _make_adapter()
    adapter._set_order_id_mapping("BROKER123", "strat:1", source="unit_test")
    assert adapter.order_id_map["BROKER123"] == "strat:1"


def test_del_helper_returns_prior_value():
    adapter = _make_adapter()
    adapter._set_order_id_mapping("BROKER123", "strat:1", source="seed")
    prior = adapter._del_order_id_mapping("BROKER123", source="unit_test")
    assert prior == "strat:1"
    assert "BROKER123" not in adapter.order_id_map
    # Idempotent: deleting absent key returns None.
    assert adapter._del_order_id_mapping("BROKER123", source="unit_test") is None


def test_helper_lock_is_reentrant():
    """Re-entrant lock allows batch writers to hold the lock while delegating
    individual writes through the helper. Non-reentrant Lock would deadlock."""
    adapter = _make_adapter()
    # Simulate batch writer pattern.
    with adapter._order_id_map_lock:
        adapter._set_order_id_mapping("A", "strat:1", source="batch")
        adapter._set_order_id_mapping("B", "strat:2", source="batch")
    assert adapter.order_id_map == {"A": "strat:1", "B": "strat:2"}


def test_concurrent_set_no_lost_writes():
    """Multi-thread stress: 4 threads × 250 unique keys each → exactly 1000
    entries. Without the lock, dict resize during concurrent insert can lose
    entries on CPython. The RLock guarantees serialised mutation."""
    adapter = _make_adapter()
    threads_n = 4
    writes_per_thread = 250
    barrier = threading.Barrier(threads_n)

    def _worker(tid: int) -> None:
        barrier.wait()  # maximise contention
        for i in range(writes_per_thread):
            key = f"t{tid}-k{i}"
            adapter._set_order_id_mapping(key, f"strat:{tid}:{i}", source=f"worker-{tid}")

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive()

    assert len(adapter.order_id_map) == threads_n * writes_per_thread


def test_set_helper_emits_debug_log_with_source():
    adapter = _make_adapter()
    with patch("hft_platform.order.adapter.logger") as mock_logger:
        adapter._set_order_id_mapping("TOKEN1", "strat:9", source="unit_set_log")
    # Assert logger.debug was called with the expected event + source.
    call_args_list = mock_logger.debug.call_args_list
    assert any(
        ("order_id_map_set",) == call.args and call.kwargs.get("source") == "unit_set_log" for call in call_args_list
    ), f"expected debug('order_id_map_set', source='unit_set_log') in {call_args_list}"


def test_del_helper_emits_debug_log_with_source():
    adapter = _make_adapter()
    adapter._set_order_id_mapping("TOKEN2", "strat:7", source="seed")
    with patch("hft_platform.order.adapter.logger") as mock_logger:
        adapter._del_order_id_mapping("TOKEN2", source="unit_del_log")
    call_args_list = mock_logger.debug.call_args_list
    assert any(
        ("order_id_map_del",) == call.args and call.kwargs.get("source") == "unit_del_log" for call in call_args_list
    ), f"expected debug('order_id_map_del', source='unit_del_log') in {call_args_list}"


def test_register_broker_ids_bulk_uses_helper():
    """The external batch-write entrypoint ``register_broker_ids_bulk`` must
    go through ``_set_order_id_mapping`` so its writes carry the same audit
    log as direct helper calls."""
    adapter = _make_adapter()
    with patch("hft_platform.order.adapter.logger") as mock_logger:
        changed = adapter.register_broker_ids_bulk(["X1", "X2", "X3"], "strat:42")
    assert changed is True
    sources = [
        call.kwargs.get("source")
        for call in mock_logger.debug.call_args_list
        if call.args and call.args[0] == "order_id_map_set"
    ]
    # All three writes MUST emit a debug log with the bulk source tag.
    assert sources.count("register_broker_ids_bulk") == 3
