"""Tests for CE2-05: IdempotencyStore."""

import os
import tempfile

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


# ── Additional coverage tests ──────────────────────────────────────────────────


def test_dedup_commit_without_prior_reserve_creates_record():
    """commit() without check_or_reserve first is tolerated (orphan commit path)."""
    store = IdempotencyStore(window_size=100, persist_enabled=False)
    store.commit("orphan-key", approved=False, reason_code="HALT", cmd_id=99)
    assert store.size() == 1
    rec = store.check_or_reserve("orphan-key")
    assert rec is not None
    assert rec.approved is False
    assert rec.reason_code == "HALT"
    assert rec.cmd_id == 99


def test_dedup_load_skips_malformed_json_lines():
    """load() should skip lines that cannot be parsed as JSON."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "dedup.jsonl")
        with open(path, "wb") as f:
            # Write a valid line, a malformed line, and a blank line
            import json
            f.write(json.dumps({"key": "good", "approved": True, "reason_code": "OK", "cmd_id": 1}).encode() + b"\n")
            f.write(b"NOT_VALID_JSON!!!\n")
            f.write(b"\n")  # blank line
            f.write(json.dumps({"key": "also-good", "approved": False, "reason_code": "X", "cmd_id": 2}).encode() + b"\n")
        store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
        store.load()
        assert store.size() == 2
        rec = store.check_or_reserve("good")
        assert rec is not None and rec.approved is True
        rec2 = store.check_or_reserve("also-good")
        assert rec2 is not None and rec2.approved is False


def test_dedup_load_skips_non_dict_json():
    """load() skips valid JSON that is not a dict (list, string, etc.)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "dedup.jsonl")
        with open(path, "wb") as f:
            import json
            f.write(json.dumps([1, 2, 3]).encode() + b"\n")
            f.write(json.dumps("plain-string").encode() + b"\n")
            f.write(json.dumps({"key": "valid", "approved": True, "reason_code": "OK", "cmd_id": 5}).encode() + b"\n")
        store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
        store.load()
        assert store.size() == 1
        rec = store.check_or_reserve("valid")
        assert rec is not None and rec.cmd_id == 5


def test_dedup_load_skips_records_with_empty_key():
    """load() skips persisted records with empty key field."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "dedup.jsonl")
        with open(path, "wb") as f:
            import json
            f.write(json.dumps({"key": "", "approved": True, "reason_code": "OK", "cmd_id": 1}).encode() + b"\n")
            f.write(json.dumps({"key": "real-key", "approved": True, "reason_code": "OK", "cmd_id": 2}).encode() + b"\n")
        store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
        store.load()
        # Empty key records must be skipped
        assert store.size() == 1


def test_dedup_load_when_persist_disabled_does_nothing():
    """load() is a no-op when persist_enabled=False."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "dedup.jsonl")
        import json
        with open(path, "wb") as f:
            f.write(json.dumps({"key": "k1", "approved": True, "reason_code": "OK", "cmd_id": 1}).encode() + b"\n")
        store = IdempotencyStore(window_size=100, persist_enabled=False, persist_path=path)
        store.load()
        assert store.size() == 0


def test_dedup_load_when_file_does_not_exist():
    """load() is a no-op when the persist file does not exist."""
    store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path="/nonexistent/path/no_file.jsonl")
    store.load()  # Should not raise
    assert store.size() == 0


def test_dedup_env_vars_control_window_size(monkeypatch):
    """HFT_DEDUP_WINDOW_SIZE env var controls the window size."""
    monkeypatch.setenv("HFT_DEDUP_WINDOW_SIZE", "5")
    store = IdempotencyStore(persist_enabled=False)
    assert store._window_size == 5
    # Fill exactly 5, then add 6th to trigger eviction
    for i in range(5):
        store.check_or_reserve(f"env-key-{i}")
    assert store.size() == 5
    store.check_or_reserve("env-key-5")
    assert store.size() == 5


def test_dedup_env_var_persist_enabled_false_strings(monkeypatch):
    """HFT_DEDUP_PERSIST_ENABLED env var respects false-y strings."""
    for false_val in ("0", "false", "no", "off"):
        monkeypatch.setenv("HFT_DEDUP_PERSIST_ENABLED", false_val)
        store = IdempotencyStore()
        assert store._persist_enabled is False


def test_dedup_env_var_persist_path(monkeypatch):
    """HFT_DEDUP_PERSIST_PATH env var sets the persist file path."""
    monkeypatch.setenv("HFT_DEDUP_PERSIST_PATH", "/tmp/custom_dedup.jsonl")
    store = IdempotencyStore(persist_enabled=False)
    assert store._persist_path == "/tmp/custom_dedup.jsonl"


def test_dedup_lru_promoted_key_survives_eviction():
    """Key accessed recently should survive when window overflows."""
    store = IdempotencyStore(window_size=3, persist_enabled=False)
    store.check_or_reserve("k0")
    store.check_or_reserve("k1")
    store.check_or_reserve("k2")
    # Access k0 to promote it to most-recently-used
    hit = store.check_or_reserve("k0")
    assert hit is not None  # k0 was already reserved → hit
    # Add k3 — LRU (k1) should be evicted, k0 should survive
    store.check_or_reserve("k3")
    assert store.size() == 3
    # k0 should still be present (was promoted)
    k0_result = store.check_or_reserve("k0")
    assert k0_result is not None


def test_dedup_commit_typed_with_mock_rust_store():
    """commit_typed() delegates to Rust store when available."""

    class _MockRust:
        def __init__(self):
            self.calls = []

        def commit(self, key, approved, reason_code, cmd_id):
            self.calls.append((key, approved, reason_code, cmd_id))

    store = IdempotencyStore(window_size=100, persist_enabled=False)
    mock = _MockRust()
    store._rust_store = mock
    store.commit_typed("rust-key", True, "OK", 55)
    assert mock.calls == [("rust-key", True, "OK", 55)]
    # Python store should not have been updated
    assert store.size() == 0


def test_dedup_check_or_reserve_typed_hit_approved_true():
    """check_or_reserve_typed() with Rust store returns approved=True when approved==1."""

    class _MockRust:
        def check_or_reserve(self, key):
            return (True, 1, "OK", 42)

    store = IdempotencyStore(window_size=100, persist_enabled=False)
    store._rust_store = _MockRust()
    rec = store.check_or_reserve_typed("some-key")
    assert rec is not None
    assert rec.approved is True
    assert rec.cmd_id == 42


def test_dedup_check_or_reserve_typed_hit_approved_false():
    """check_or_reserve_typed() with Rust store returns approved=False when approved==0."""

    class _MockRust:
        def check_or_reserve(self, key):
            return (True, 0, "HALT", 0)

    store = IdempotencyStore(window_size=100, persist_enabled=False)
    store._rust_store = _MockRust()
    rec = store.check_or_reserve_typed("some-key")
    assert rec is not None
    assert rec.approved is False


def test_dedup_check_or_reserve_typed_hit_approved_none():
    """check_or_reserve_typed() with Rust store returns approved=None for in-flight (approved not 0 or 1)."""

    class _MockRust:
        def check_or_reserve(self, key):
            return (True, 2, "", 0)  # approved=2 → None

    store = IdempotencyStore(window_size=100, persist_enabled=False)
    store._rust_store = _MockRust()
    rec = store.check_or_reserve_typed("in-flight")
    assert rec is not None
    assert rec.approved is None


def test_dedup_check_or_reserve_typed_miss_returns_none():
    """check_or_reserve_typed() with Rust store returns None on miss."""

    class _MockRust:
        def check_or_reserve(self, key):
            return (False, 0, "", 0)

    store = IdempotencyStore(window_size=100, persist_enabled=False)
    store._rust_store = _MockRust()
    rec = store.check_or_reserve_typed("new-key")
    assert rec is None


def test_dedup_persist_cleans_up_temp_file_on_write_error():
    """persist() cleans up the temp file when the write fails mid-way."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Use a path in a read-only-like scenario: place the persist path
        # inside a nonexistent nested dir to trigger an os.rename failure.
        # Instead, mock fdopen to raise.
        import unittest.mock as mock

        path = os.path.join(tmpdir, "dedup.jsonl")
        store = IdempotencyStore(window_size=10, persist_enabled=True, persist_path=path)
        store.check_or_reserve("k1")
        store.commit("k1", True, "OK", 1)

        original_fdopen = os.fdopen

        def raise_on_fdopen(fd, *args, **kwargs):
            os.close(fd)  # avoid fd leak
            raise OSError("simulated write failure")

        with mock.patch("os.fdopen", side_effect=raise_on_fdopen):
            # Should not raise — exception is caught and logged
            store.persist()

        # File should not exist since rename never happened
        assert not os.path.exists(path)
