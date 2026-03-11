"""Tests for RustDedupStore — Rust LRU dedup cache."""

from __future__ import annotations

import pytest

_RustDedupStore = None


def _get_store():
    global _RustDedupStore
    if _RustDedupStore is None:
        try:
            from hft_platform.rust_core import RustDedupStore  # type: ignore[attr-defined]
        except ImportError:
            try:
                from rust_core import RustDedupStore  # type: ignore[assignment]
            except ImportError:
                pytest.skip("rust_core not available")
        _RustDedupStore = RustDedupStore
    return _RustDedupStore


class TestRustDedupStore:
    def _make(self, window_size=100):
        cls = _get_store()
        return cls(window_size)

    def test_new_key_returns_miss(self):
        s = self._make()
        is_hit, approved, reason, cmd_id = s.check_or_reserve("key1")
        assert is_hit is False
        assert approved == -1  # RESERVED

    def test_duplicate_key_returns_hit(self):
        s = self._make()
        s.check_or_reserve("key1")
        s.commit("key1", True, "OK", 42)
        is_hit, approved, reason, cmd_id = s.check_or_reserve("key1")
        assert is_hit is True
        assert approved == 1  # APPROVED
        assert reason == "OK"
        assert cmd_id == 42

    def test_rejected_record(self):
        s = self._make()
        s.check_or_reserve("key1")
        s.commit("key1", False, "RISK_REJECT", 0)
        is_hit, approved, reason, cmd_id = s.check_or_reserve("key1")
        assert is_hit is True
        assert approved == 0  # REJECTED
        assert reason == "RISK_REJECT"

    def test_reserved_record_returned(self):
        s = self._make()
        s.check_or_reserve("key1")
        # Before commit, should still be hit with reserved status
        is_hit, approved, _, _ = s.check_or_reserve("key1")
        assert is_hit is True
        assert approved == -1  # still RESERVED

    def test_lru_eviction(self):
        s = self._make(window_size=3)
        s.check_or_reserve("a")
        s.check_or_reserve("b")
        s.check_or_reserve("c")
        # 'a' is oldest, should be evicted when 'd' is added
        s.check_or_reserve("d")
        assert s.size() == 3
        assert not s.contains("a")
        assert s.contains("b")
        assert s.contains("d")

    def test_lru_access_refreshes(self):
        s = self._make(window_size=3)
        s.check_or_reserve("a")
        s.check_or_reserve("b")
        s.check_or_reserve("c")
        # Access 'a' to refresh it
        s.check_or_reserve("a")
        # Now 'b' is oldest
        s.check_or_reserve("d")
        assert s.contains("a")
        assert not s.contains("b")

    def test_empty_key_is_miss(self):
        s = self._make()
        is_hit, _, _, _ = s.check_or_reserve("")
        assert is_hit is False

    def test_commit_without_reserve(self):
        s = self._make()
        s.commit("key1", True, "OK", 99)
        is_hit, approved, reason, cmd_id = s.check_or_reserve("key1")
        assert is_hit is True
        assert cmd_id == 99

    def test_size(self):
        s = self._make()
        assert s.size() == 0
        s.check_or_reserve("a")
        s.check_or_reserve("b")
        assert s.size() == 2

    def test_large_window(self):
        s = self._make(window_size=10000)
        for i in range(5000):
            s.check_or_reserve(f"key_{i}")
        assert s.size() == 5000

    def test_parity_with_python_dedup(self):
        """Verify Rust results match Python IdempotencyStore."""
        from hft_platform.gateway.dedup import IdempotencyStore

        py = IdempotencyStore(window_size=100, persist_enabled=False)
        rs = self._make(window_size=100)

        # New key
        py_result = py.check_or_reserve("k1")
        rs_hit, _, _, _ = rs.check_or_reserve("k1")
        assert py_result is None  # Python returns None for miss
        assert rs_hit is False

        # Commit
        py.commit("k1", True, "OK", 42)
        rs.commit("k1", True, "OK", 42)

        # Hit
        py_result = py.check_or_reserve("k1")
        rs_hit, rs_approved, rs_reason, rs_cmd = rs.check_or_reserve("k1")
        assert py_result is not None
        assert py_result.approved is True
        assert rs_hit is True
        assert rs_approved == 1
        assert rs_cmd == 42
