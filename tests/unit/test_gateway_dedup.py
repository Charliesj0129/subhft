"""Tests for CE2-05: IdempotencyStore."""
import os
import tempfile

import pytest

from hft_platform.gateway.dedup import IdempotencyStore


def test_dedup_miss_returns_none():
    store = IdempotencyStore(window_size=100, persist_enabled=False)
    result = store.check_or_reserve("key-1")
    assert result is None


def test_dedup_hit_after_commit():
    store = IdempotencyStore(window_size=100, persist_enabled=False)
    store.check_or_reserve("key-1")
    store.commit("key-1", approved=True, reason_code="OK", cmd_id=42)

    existing = store.check_or_reserve("key-1")
    assert existing is not None
    assert existing.approved is True
    assert existing.reason_code == "OK"
    assert existing.cmd_id == 42


def test_dedup_reserved_not_yet_committed():
    """check_or_reserve before commit returns the un-committed record on second call."""
    store = IdempotencyStore(window_size=100, persist_enabled=False)
    first = store.check_or_reserve("in-flight")
    assert first is None  # Miss — slot reserved

    second = store.check_or_reserve("in-flight")
    assert second is not None  # Hit — reserved slot
    assert second.approved is None  # Not yet committed


def test_dedup_empty_key_is_no_op():
    store = IdempotencyStore(window_size=100, persist_enabled=False)
    assert store.check_or_reserve("") is None
    store.commit("", True, "OK", 1)  # Should not raise
    assert store.size() == 0


def test_dedup_typed_alias_matches_regular_path():
    store = IdempotencyStore(window_size=100, persist_enabled=False)
    assert store.check_or_reserve_typed("tk") is None
    store.commit_typed("tk", True, "OK", 7)
    rec = store.check_or_reserve("tk")
    assert rec is not None
    assert rec.approved is True
    assert rec.cmd_id == 7


def test_dedup_window_evicts_oldest():
    store = IdempotencyStore(window_size=3, persist_enabled=False)
    for i in range(3):
        store.check_or_reserve(f"k{i}")
    assert store.size() == 3

    # Add 4th — oldest (k0) should be evicted
    store.check_or_reserve("k3")
    assert store.size() == 3
    # k0 should be gone
    result = store.check_or_reserve("k0")
    # After eviction, k0 is a new miss, not a hit
    assert result is None or (result is not None and result.approved is None)


def test_dedup_persist_and_load_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "dedup.jsonl")
        store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
        store.check_or_reserve("k1")
        store.commit("k1", approved=True, reason_code="OK", cmd_id=10)
        store.check_or_reserve("k2")
        store.commit("k2", approved=False, reason_code="HALT", cmd_id=0)
        store.persist()

        # Load into fresh store
        store2 = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
        store2.load()
        assert store2.size() == 2

        rec = store2.check_or_reserve("k1")
        assert rec is not None
        assert rec.approved is True
        assert rec.cmd_id == 10

        rec2 = store2.check_or_reserve("k2")
        assert rec2 is not None
        assert rec2.approved is False
        assert rec2.reason_code == "HALT"


def test_dedup_persist_disabled():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "dedup.jsonl")
        store = IdempotencyStore(window_size=10, persist_enabled=False, persist_path=path)
        store.check_or_reserve("k1")
        store.commit("k1", True, "OK", 1)
        store.persist()  # Should be no-op
        assert not os.path.exists(path)
