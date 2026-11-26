"""Coverage tests for persisted idempotency decisions."""

from __future__ import annotations

from unittest.mock import patch

from hft_platform.gateway.dedup import (
    IdempotencyRecord,
    IdempotencyStore,
)

# ---------------------------------------------------------------------------
# _load_rust_dedup — module-level lazy loader (lines 29-43)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _init_rust_store — env var + error paths (lines 109-118)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# release — with rust store (lines 222-225)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# load — exception path (lines 297-298)
# ---------------------------------------------------------------------------


class TestLoadException:
    def test_load_handles_permission_error(self, tmp_path):
        """load() catches and logs permission errors."""
        path = str(tmp_path / "dedup.jsonl")
        # Create a file that can be found but fails on read
        with open(path, "w") as f:
            f.write("test\n")

        store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)

        with patch("builtins.open", side_effect=PermissionError("access denied")):
            # Should not raise
            store.load()
        assert store.size() == 0


# ---------------------------------------------------------------------------
# _dumps/_loads codec (msgspec via hft_platform.utils.jsonfast)
# ---------------------------------------------------------------------------


class TestDedupCodecRoundtrip:
    def test_roundtrip_serialization(self):
        """The _dumps/_loads functions produce compact JSON bytes and read it back."""
        from hft_platform.gateway.dedup import _dumps, _loads

        obj = {"key": "test", "approved": True, "reason_code": "OK", "cmd_id": 42}
        serialized = _dumps(obj)
        assert isinstance(serialized, bytes)

        deserialized = _loads(serialized)
        assert deserialized["key"] == "test"
        assert deserialized["approved"] is True
        assert deserialized["cmd_id"] == 42


# ---------------------------------------------------------------------------
# IdempotencyRecord defaults
# ---------------------------------------------------------------------------


class TestIdempotencyRecordDefaults:
    def test_default_values(self):
        rec = IdempotencyRecord(key="test")
        assert rec.key == "test"
        assert rec.approved is None
        assert rec.reason_code == ""
        assert rec.cmd_id == 0

    def test_explicit_values(self):
        rec = IdempotencyRecord(key="k", approved=True, reason_code="OK", cmd_id=99)
        assert rec.approved is True
        assert rec.reason_code == "OK"
        assert rec.cmd_id == 99


# ---------------------------------------------------------------------------
# _dumps/_loads codec without orjson
# ---------------------------------------------------------------------------


class TestDedupCodecWithoutOrjson:
    """The free-threaded build has no orjson wheel: the codec must not need it."""

    def test_module_reload_without_orjson_keeps_the_msgspec_codec(self):
        """Reload dedup module with orjson blocked: _dumps/_loads still work, byte for byte."""
        import builtins
        import importlib

        import hft_platform.gateway.dedup as dedup_mod

        # Save module state
        real_import = builtins.__import__

        def block_orjson(name, *args, **kwargs):
            if name == "orjson":
                raise ImportError("mocked: no orjson")
            return real_import(name, *args, **kwargs)

        try:
            # Block orjson and reload the module: it must not try to import it
            with patch.object(builtins, "__import__", side_effect=block_orjson):
                importlib.reload(dedup_mod)

            json_dumps = dedup_mod._dumps
            json_loads = dedup_mod._loads

            # Verify _dumps returns compact bytes
            data = {"key": "fb", "approved": True, "reason_code": "OK", "cmd_id": 5}
            result = json_dumps(data)
            assert result == b'{"key":"fb","approved":true,"reason_code":"OK","cmd_id":5}'

            # Verify _loads roundtrips correctly from bytes
            parsed = json_loads(result)
            assert parsed["key"] == "fb"
            assert parsed["approved"] is True
            assert parsed["cmd_id"] == 5

            # Verify _loads handles str input
            str_result = json_loads('{"key": "s", "cmd_id": 3}')
            assert str_result["key"] == "s"
            assert str_result["cmd_id"] == 3
        finally:
            # Reload again to restore the module state
            importlib.reload(dedup_mod)

    def test_persist_load_roundtrip_through_the_codec(self):
        """Exercise _dumps/_loads via persist/load to ensure they work end-to-end."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "dedup_json_fb.jsonl")
            store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
            store.check_or_reserve("fb-key")
            store.commit("fb-key", approved=True, reason_code="JSON_FB", cmd_id=77)
            store.persist()

            store2 = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
            store2.load()
            rec = store2.check_or_reserve("fb-key")
            assert rec is not None
            assert rec.approved is True
            assert rec.reason_code == "JSON_FB"
            assert rec.cmd_id == 77


# ---------------------------------------------------------------------------
# commit() first-commit-wins / overwrite blocked (lines 182-190)
# ---------------------------------------------------------------------------


class TestCommitOverwriteBlocked:
    """Cover the first-commit-wins guard in commit()."""

    def test_second_commit_blocked_after_approved(self):
        """Once a key is committed as approved, second commit is blocked."""
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.check_or_reserve("dup-key")
        store.commit("dup-key", approved=True, reason_code="OK", cmd_id=10)

        # Second commit with different values should be silently blocked
        store.commit("dup-key", approved=False, reason_code="HALT", cmd_id=20)

        # Original decision preserved
        rec = store.check_or_reserve("dup-key")
        assert rec is not None
        assert rec.approved is True
        assert rec.reason_code == "OK"
        assert rec.cmd_id == 10

    def test_second_commit_blocked_after_rejected(self):
        """Once a key is committed as rejected, second commit is blocked."""
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.check_or_reserve("rej-key")
        store.commit("rej-key", approved=False, reason_code="RISK", cmd_id=5)

        # Attempt to overwrite with approved=True
        store.commit("rej-key", approved=True, reason_code="RETRY", cmd_id=6)

        # Original rejection preserved
        rec = store.check_or_reserve("rej-key")
        assert rec is not None
        assert rec.approved is False
        assert rec.reason_code == "RISK"
        assert rec.cmd_id == 5

    def test_commit_allowed_when_still_reserved(self):
        """Commit succeeds when key is reserved but not yet committed (approved=None)."""
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.check_or_reserve("pending-key")
        store.commit("pending-key", approved=True, reason_code="FIRST", cmd_id=1)

        rec = store.check_or_reserve("pending-key")
        assert rec is not None
        assert rec.approved is True
        assert rec.reason_code == "FIRST"


# ---------------------------------------------------------------------------
# persist() temp file cleanup branch (line 259 -> 261)
# ---------------------------------------------------------------------------


class TestPersistTempFileCleanup:
    def test_persist_removes_temp_on_rename_failure(self):
        """persist() removes temp file when os.rename fails but temp exists."""
        import os
        import tempfile
        from unittest.mock import patch as mock_patch

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "dedup.jsonl")
            store = IdempotencyStore(window_size=10, persist_enabled=True, persist_path=path)
            store.check_or_reserve("k1")
            store.commit("k1", True, "OK", 1)

            def fail_rename(src, dst):
                raise OSError("rename failed")

            with mock_patch("os.rename", side_effect=fail_rename):
                store.persist()

            # Final file should not exist since rename failed
            assert not os.path.exists(path)
            # Temp file should also be cleaned up
            remaining = [f for f in os.listdir(tmpdir) if f.endswith(".tmp")]
            assert len(remaining) == 0

    def test_persist_skips_unlink_when_temp_already_gone(self):
        """persist() skips os.unlink when temp file no longer exists (line 259 false branch)."""
        import os
        import tempfile
        from unittest.mock import patch as mock_patch

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "dedup.jsonl")
            store = IdempotencyStore(window_size=10, persist_enabled=True, persist_path=path)
            store.check_or_reserve("k1")
            store.commit("k1", True, "OK", 1)

            original_rename = os.rename

            def fail_rename_and_remove_temp(src, dst):
                # Remove the temp file before the except clause checks for it
                if os.path.exists(src):
                    os.unlink(src)
                raise OSError("rename failed and temp gone")

            with mock_patch("os.rename", side_effect=fail_rename_and_remove_temp):
                store.persist()  # Should not raise

            # Neither the target nor any temp should exist
            assert not os.path.exists(path)
            remaining = [f for f in os.listdir(tmpdir) if f.endswith(".tmp")]
            assert len(remaining) == 0
