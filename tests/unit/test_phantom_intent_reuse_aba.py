"""M4: phantom intent_id reuse must not overwrite a prior phantom record.

Pre-M4, ``_phantom_intents[key] = intent`` overwrote any prior entry
when ``key = "{strategy_id}:{intent_id}"`` recurred. The same intent_id
can recur within a process lifetime via DLQ replay, strategy retry, or
just regular rolling intent_id allocation — every recurrence used to
silently lose the original phantom record, leaving its strategy
pending counter elevated forever (Bug D resurfacing).

M4 stores per-occurrence ``_PhantomEntry`` items in a list keyed by
``"strategy_id:intent_id"``. Multiple registrations accumulate; FIFO
resolution and TTL cleanup operate on the per-occurrence list; sibling
occurrences remain independently traceable.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.order.adapter import OrderAdapter, _PhantomEntry


@pytest.fixture
def tmp_config(tmp_path: Path) -> str:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("rate_limits: {}\ncircuit_breaker: {}\n")
    return str(cfg)


def _intent(intent_id: int = 1, *, strategy_id: str = "S1") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol="TMFD6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=10000,
        qty=1,
    )


def _make_adapter(tmp_config: str) -> OrderAdapter:
    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock())
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.update_order = MagicMock(return_value=MagicMock())
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=asyncio.Queue(),
        broker_client=client,
    )
    adapter.shadow_sink.enabled = False
    return adapter


class TestPhantomIntentReuseABA:
    def test_two_registrations_same_key_both_preserved(self, tmp_config):
        """Same intent_id submitted twice within process lifetime → both
        phantom entries preserved as separate per-occurrence records."""
        adapter = _make_adapter(tmp_config)
        i1 = _intent(intent_id=42)
        i2 = _intent(intent_id=42)  # same key, second submission
        with adapter._phantom_lock:
            adapter._register_phantom(i1)
            adapter._register_phantom(i2)
        records = adapter._phantom_records["S1:42"]
        assert len(records) == 2, "second registration must NOT overwrite the first"
        assert records[0].intent is i1
        assert records[1].intent is i2

    def test_resolve_pops_one_occurrence_at_a_time(self, tmp_config):
        """A single fill resolves a single phantom occurrence (FIFO).
        The sibling occurrence remains resolvable for its own fill."""
        adapter = _make_adapter(tmp_config)
        i1 = _intent(intent_id=42)
        i2 = _intent(intent_id=42)
        with adapter._phantom_lock:
            adapter._register_phantom(i1)
            adapter._register_phantom(i2)

        # First fill resolves first occurrence — second occurrence stays.
        fill1 = MagicMock(symbol="TMFD6", side=Side.BUY)
        strategy1 = adapter.resolve_phantom_fill(fill1)
        assert strategy1 == "S1"
        assert "S1:42" in adapter._phantom_records
        assert len(adapter._phantom_records["S1:42"]) == 1

        # Second fill resolves the remaining occurrence — key is now empty.
        fill2 = MagicMock(symbol="TMFD6", side=Side.BUY)
        strategy2 = adapter.resolve_phantom_fill(fill2)
        assert strategy2 == "S1"
        assert "S1:42" not in adapter._phantom_records

    def test_release_stale_processes_each_occurrence_independently(self, tmp_config):
        """release_stale_phantom_pendings emits one feedback per expired
        occurrence — both siblings under the same key are released."""
        adapter = _make_adapter(tmp_config)
        sink: asyncio.Queue = asyncio.Queue(maxsize=64)
        adapter.set_rejection_sink(sink)

        import time as _time

        aged_ts = _time.monotonic() - 1000.0
        i1 = _intent(intent_id=42)
        i2 = _intent(intent_id=42)
        with adapter._phantom_lock:
            adapter._phantom_records["S1:42"] = [
                _PhantomEntry(monotonic_ts=aged_ts, symbol="TMFD6", created_ns=0, intent=i1),
                _PhantomEntry(monotonic_ts=aged_ts, symbol="TMFD6", created_ns=0, intent=i2),
            ]
            adapter._phantom_order_keys["S1:42"] = (aged_ts, "TMFD6")
            adapter._phantom_intents["S1:42"] = i2

        loop = asyncio.new_event_loop()
        try:
            released = loop.run_until_complete(adapter.release_stale_phantom_pendings(ttl_s=30.0))
        finally:
            loop.close()

        assert released == 2, "both phantom occurrences must be released"
        assert "S1:42" not in adapter._phantom_records

    def test_clear_phantom_candidate_drops_all_occurrences(self, tmp_config):
        """``clear_phantom_candidate(key)`` removes ALL occurrences for the
        key; reconciliation expressing "this key is fully resolved" must
        drop every occurrence at once (otherwise a sibling lingers and
        gets misattributed to the next fill)."""
        adapter = _make_adapter(tmp_config)
        i1 = _intent(intent_id=42)
        i2 = _intent(intent_id=42)
        with adapter._phantom_lock:
            adapter._register_phantom(i1)
            adapter._register_phantom(i2)
        assert len(adapter._phantom_records["S1:42"]) == 2

        adapter.clear_phantom_candidate("S1:42")
        assert "S1:42" not in adapter._phantom_records
        assert "S1:42" not in adapter._phantom_order_keys
        assert "S1:42" not in adapter._phantom_intents

    def test_legacy_view_reflects_last_occurrence(self, tmp_config):
        """The backwards-compat ``_phantom_order_keys`` /
        ``_phantom_intents`` views align with the LAST occurrence per key
        (the pre-M4 contract). Internally the canonical store
        ``_phantom_records`` retains every occurrence."""
        adapter = _make_adapter(tmp_config)
        i1 = _intent(intent_id=42)
        i2 = _intent(intent_id=42)
        with adapter._phantom_lock:
            adapter._register_phantom(i1)
            adapter._register_phantom(i2)
        assert adapter._phantom_intents["S1:42"] is i2
        # Legacy ts also matches the latest entry's ts.
        last = adapter._phantom_records["S1:42"][-1]
        assert adapter._phantom_order_keys["S1:42"] == (last.monotonic_ts, last.symbol)

    def test_capacity_eviction_counts_each_occurrence(self, tmp_config):
        """Capacity sweep counts every occurrence, not every key. Two
        entries under one key still consume 2 of the budget."""
        adapter = _make_adapter(tmp_config)
        adapter._phantom_order_max = 4
        # Register 6 phantom occurrences (3 keys × 2 each, all stale).
        import time as _time

        old_ts = _time.monotonic() - 7200.0
        with adapter._phantom_lock:
            for sid in ("A", "B", "C"):
                key = f"{sid}:1"
                intent_one = _intent(intent_id=1, strategy_id=sid)
                intent_two = _intent(intent_id=1, strategy_id=sid)
                adapter._phantom_records[key] = [
                    _PhantomEntry(monotonic_ts=old_ts, symbol="TMFD6", created_ns=0, intent=intent_one),
                    _PhantomEntry(monotonic_ts=old_ts, symbol="TMFD6", created_ns=0, intent=intent_two),
                ]
            assert adapter._phantom_record_count() == 6
            removed = adapter._evict_stale_phantom_records(max_age_s=3600.0)
        # All 6 are stale (>1h), so the eviction sweep clears all 6.
        assert removed == 6
        assert adapter._phantom_record_count() == 0

    def test_concurrent_register_and_resolve_multi_occurrence(self, tmp_config):
        """Multi-thread stress: writers register phantoms for the same key
        repeatedly; resolver pops one occurrence per fill. No exceptions,
        no corrupt state."""
        adapter = _make_adapter(tmp_config)
        stop = threading.Event()
        errors: list[BaseException] = []

        def writer():
            try:
                while not stop.is_set():
                    with adapter._phantom_lock:
                        adapter._register_phantom(_intent(intent_id=42))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def resolver():
            try:
                fill = MagicMock(symbol="TMFD6", side=Side.BUY)
                while not stop.is_set():
                    adapter.resolve_phantom_fill(fill)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        w = threading.Thread(target=writer, daemon=True)
        r = threading.Thread(target=resolver, daemon=True)
        w.start()
        r.start()
        try:
            stop.wait(0.3)
        finally:
            stop.set()
            w.join(timeout=2)
            r.join(timeout=2)
        assert not errors, f"concurrent multi-occurrence raised: {errors[0]!r}"
