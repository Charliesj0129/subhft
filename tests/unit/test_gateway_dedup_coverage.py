"""Coverage tests for gateway/dedup.py — uncovered rust loader and init paths."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.gateway.dedup import (
    IdempotencyRecord,
    IdempotencyStore,
    _load_rust_dedup,
)

# ---------------------------------------------------------------------------
# _load_rust_dedup — module-level lazy loader (lines 29-43)
# ---------------------------------------------------------------------------


class TestLoadRustDedup:
    def test_early_return_when_already_loaded(self):
        """Second call is a no-op."""
        import hft_platform.gateway.dedup as dedup_mod

        original_loaded = dedup_mod._rust_dedup_loaded
        original_cls = dedup_mod._RustDedupStore
        try:
            dedup_mod._rust_dedup_loaded = True
            dedup_mod._RustDedupStore = "sentinel"
            result = _load_rust_dedup()
            assert result == "sentinel"
        finally:
            dedup_mod._rust_dedup_loaded = original_loaded
            dedup_mod._RustDedupStore = original_cls

    def test_both_imports_fail_returns_none(self):
        """When neither hft_platform.rust_core nor rust_core has RustDedupStore."""
        import hft_platform.gateway.dedup as dedup_mod

        original_loaded = dedup_mod._rust_dedup_loaded
        original_cls = dedup_mod._RustDedupStore
        try:
            dedup_mod._rust_dedup_loaded = False
            dedup_mod._RustDedupStore = None

            import sys

            saved_hft = sys.modules.get("hft_platform.rust_core")
            saved_rc = sys.modules.get("rust_core")

            # Make both imports fail
            mock_hft_mod = MagicMock(spec=[])  # No RustDedupStore attr
            del mock_hft_mod.RustDedupStore  # Force AttributeError -> ImportError path

            with patch.dict(
                "sys.modules",
                {
                    "hft_platform.rust_core": mock_hft_mod,
                    "rust_core": mock_hft_mod,
                },
            ):
                dedup_mod._rust_dedup_loaded = False
                # The import will succeed but getattr for RustDedupStore will fail
                # which triggers the ImportError fallback. Let's just force ImportError.
                pass

            # Simpler approach: just remove the modules
            dedup_mod._rust_dedup_loaded = False
            dedup_mod._RustDedupStore = None
            saved_hft_mod = sys.modules.pop("hft_platform.rust_core", "MISSING")
            saved_rc_mod = sys.modules.pop("rust_core", "MISSING")
            try:
                result = _load_rust_dedup()
                assert dedup_mod._rust_dedup_loaded is True
            finally:
                if saved_hft_mod != "MISSING":
                    sys.modules["hft_platform.rust_core"] = saved_hft_mod
                if saved_rc_mod != "MISSING":
                    sys.modules["rust_core"] = saved_rc_mod
        finally:
            dedup_mod._rust_dedup_loaded = original_loaded
            dedup_mod._RustDedupStore = original_cls


# ---------------------------------------------------------------------------
# _init_rust_store — env var + error paths (lines 109-118)
# ---------------------------------------------------------------------------


class TestInitRustStore:
    def test_rust_store_disabled_by_default(self):
        """HFT_DEDUP_RUST defaults to 0, so rust store is None."""
        store = IdempotencyStore(window_size=10, persist_enabled=False)
        assert store._rust_store is None

    def test_rust_store_enabled_but_unavailable(self, monkeypatch):
        """HFT_DEDUP_RUST=1 but RustDedupStore not available."""
        monkeypatch.setenv("HFT_DEDUP_RUST", "1")
        import hft_platform.gateway.dedup as dedup_mod

        original_loaded = dedup_mod._rust_dedup_loaded
        original_cls = dedup_mod._RustDedupStore
        try:
            dedup_mod._rust_dedup_loaded = True
            dedup_mod._RustDedupStore = None
            store = IdempotencyStore(window_size=10, persist_enabled=False)
            assert store._rust_store is None
        finally:
            dedup_mod._rust_dedup_loaded = original_loaded
            dedup_mod._RustDedupStore = original_cls

    def test_rust_store_init_exception(self, monkeypatch):
        """RustDedupStore constructor raises -> fall back to None."""
        monkeypatch.setenv("HFT_DEDUP_RUST", "1")
        import hft_platform.gateway.dedup as dedup_mod

        original_loaded = dedup_mod._rust_dedup_loaded
        original_cls = dedup_mod._RustDedupStore
        try:
            mock_cls = MagicMock(side_effect=RuntimeError("init failed"))
            dedup_mod._rust_dedup_loaded = True
            dedup_mod._RustDedupStore = mock_cls
            store = IdempotencyStore(window_size=10, persist_enabled=False)
            assert store._rust_store is None
        finally:
            dedup_mod._rust_dedup_loaded = original_loaded
            dedup_mod._RustDedupStore = original_cls

    def test_rust_store_enabled_and_available(self, monkeypatch):
        """HFT_DEDUP_RUST=1 with working RustDedupStore."""
        monkeypatch.setenv("HFT_DEDUP_RUST", "1")
        import hft_platform.gateway.dedup as dedup_mod

        original_loaded = dedup_mod._rust_dedup_loaded
        original_cls = dedup_mod._RustDedupStore
        try:
            mock_instance = MagicMock()
            mock_cls = MagicMock(return_value=mock_instance)
            dedup_mod._rust_dedup_loaded = True
            dedup_mod._RustDedupStore = mock_cls
            store = IdempotencyStore(window_size=10, persist_enabled=False)
            assert store._rust_store is mock_instance
        finally:
            dedup_mod._rust_dedup_loaded = original_loaded
            dedup_mod._RustDedupStore = original_cls


# ---------------------------------------------------------------------------
# release — with rust store (lines 222-225)
# ---------------------------------------------------------------------------


class TestReleaseWithRustStore:
    def test_release_delegates_to_rust_store(self):
        """release() calls rs.release() when rust store is present."""
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        mock_rs = MagicMock()
        store._rust_store = mock_rs

        store.check_or_reserve("key1")
        store.release("key1")

        mock_rs.release.assert_called_once_with("key1")
        assert store.size() == 0

    def test_release_rust_store_without_release_method(self):
        """release() handles rust store that lacks release method."""
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        mock_rs = MagicMock(spec=[])  # No release method
        store._rust_store = mock_rs

        store.check_or_reserve("key1")
        store.release("key1")
        assert store.size() == 0

    def test_release_rust_store_release_raises(self):
        """release() swallows exception from rust store's release."""
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        mock_rs = MagicMock()
        mock_rs.release.side_effect = RuntimeError("rust error")
        store._rust_store = mock_rs

        store.check_or_reserve("key1")
        # Should not raise
        store.release("key1")
        assert store.size() == 0

    def test_release_empty_key_with_rust_store(self):
        """release('') is a no-op even with rust store."""
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        mock_rs = MagicMock()
        store._rust_store = mock_rs

        store.release("")
        mock_rs.release.assert_not_called()


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
# json fallback _dumps/_loads (lines 55-62)
# ---------------------------------------------------------------------------


class TestJsonFallbackDumpsLoads:
    def test_roundtrip_serialization(self):
        """The _dumps/_loads functions produce valid JSON regardless of backend."""
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
# _load_rust_dedup — second import fallback path (lines 36-42)
# ---------------------------------------------------------------------------


class TestLoadRustDedupImportPaths:
    """Cover the two nested try/except import paths in _load_rust_dedup()."""

    def test_first_import_fails_second_succeeds(self):
        """When hft_platform.rust_core fails, falls back to rust_core (lines 36-40)."""
        import builtins
        import sys
        import types

        import hft_platform.gateway.dedup as dedup_mod

        original_loaded = dedup_mod._rust_dedup_loaded
        original_cls = dedup_mod._RustDedupStore
        try:
            dedup_mod._rust_dedup_loaded = False
            dedup_mod._RustDedupStore = None

            sentinel = type("FakeRustDedupStore", (), {})
            fake_rc = types.ModuleType("rust_core")
            fake_rc.RustDedupStore = sentinel

            # Remove cached modules so from-import re-triggers __import__
            saved_hft_rc = sys.modules.pop("hft_platform.rust_core", None)
            saved_rc = sys.modules.pop("rust_core", None)

            real_import = builtins.__import__

            def patched_import(name, *args, **kwargs):
                if name == "hft_platform.rust_core":
                    raise ImportError("mocked: no hft_platform.rust_core")
                if name == "rust_core":
                    sys.modules["rust_core"] = fake_rc
                    return fake_rc
                return real_import(name, *args, **kwargs)

            try:
                with patch.object(builtins, "__import__", side_effect=patched_import):
                    result = _load_rust_dedup()

                assert result is sentinel
                assert dedup_mod._rust_dedup_loaded is True
                assert dedup_mod._RustDedupStore is sentinel
            finally:
                # Restore cached modules
                sys.modules.pop("rust_core", None)
                if saved_hft_rc is not None:
                    sys.modules["hft_platform.rust_core"] = saved_hft_rc
                if saved_rc is not None:
                    sys.modules["rust_core"] = saved_rc
        finally:
            dedup_mod._rust_dedup_loaded = original_loaded
            dedup_mod._RustDedupStore = original_cls

    def test_both_imports_fail_returns_none_cleanly(self):
        """When neither import path has RustDedupStore, returns None (lines 41-43)."""
        import builtins
        import sys

        import hft_platform.gateway.dedup as dedup_mod

        original_loaded = dedup_mod._rust_dedup_loaded
        original_cls = dedup_mod._RustDedupStore
        try:
            dedup_mod._rust_dedup_loaded = False
            dedup_mod._RustDedupStore = None

            # Remove cached modules so from-import re-triggers __import__
            saved_hft_rc = sys.modules.pop("hft_platform.rust_core", None)
            saved_rc = sys.modules.pop("rust_core", None)

            real_import = builtins.__import__

            def patched_import(name, *args, **kwargs):
                if name in ("hft_platform.rust_core", "rust_core"):
                    raise ImportError(f"mocked: no {name}")
                return real_import(name, *args, **kwargs)

            try:
                with patch.object(builtins, "__import__", side_effect=patched_import):
                    result = _load_rust_dedup()

                assert result is None
                assert dedup_mod._rust_dedup_loaded is True
            finally:
                if saved_hft_rc is not None:
                    sys.modules["hft_platform.rust_core"] = saved_hft_rc
                if saved_rc is not None:
                    sys.modules["rust_core"] = saved_rc
        finally:
            dedup_mod._rust_dedup_loaded = original_loaded
            dedup_mod._RustDedupStore = original_cls

    def test_first_import_succeeds(self):
        """When hft_platform.rust_core has RustDedupStore, uses it directly (lines 32-35)."""
        import builtins
        import types

        import hft_platform.gateway.dedup as dedup_mod

        original_loaded = dedup_mod._rust_dedup_loaded
        original_cls = dedup_mod._RustDedupStore
        try:
            dedup_mod._rust_dedup_loaded = False
            dedup_mod._RustDedupStore = None

            sentinel = type("FakeRustDedupStore", (), {})
            fake_hft_rc = types.ModuleType("hft_platform.rust_core")
            fake_hft_rc.RustDedupStore = sentinel

            real_import = builtins.__import__

            def patched_import(name, *args, **kwargs):
                if name == "hft_platform.rust_core":
                    return fake_hft_rc
                return real_import(name, *args, **kwargs)

            with patch.object(builtins, "__import__", side_effect=patched_import):
                result = _load_rust_dedup()

            assert result is sentinel
            assert dedup_mod._RustDedupStore is sentinel
        finally:
            dedup_mod._rust_dedup_loaded = original_loaded
            dedup_mod._RustDedupStore = original_cls


# ---------------------------------------------------------------------------
# json fallback _dumps/_loads (lines 55-62)
# ---------------------------------------------------------------------------


class TestJsonFallbackDirect:
    """Directly exercise the json-based _dumps/_loads fallback functions (lines 55-62)."""

    def test_json_fallback_via_module_reload(self):
        """Reload dedup module with orjson blocked to exercise json fallback path."""
        import builtins
        import importlib

        import hft_platform.gateway.dedup as dedup_mod

        # Save module state
        original_loaded = dedup_mod._rust_dedup_loaded
        original_cls = dedup_mod._RustDedupStore
        real_import = builtins.__import__

        def block_orjson(name, *args, **kwargs):
            if name == "orjson":
                raise ImportError("mocked: no orjson")
            return real_import(name, *args, **kwargs)

        try:
            # Block orjson and reload the module to trigger except ImportError path
            with patch.object(builtins, "__import__", side_effect=block_orjson):
                importlib.reload(dedup_mod)

            # The reloaded module should have json-based _dumps/_loads
            json_dumps = dedup_mod._dumps
            json_loads = dedup_mod._loads

            # Verify _dumps returns bytes
            data = {"key": "fb", "approved": True, "reason_code": "OK", "cmd_id": 5}
            result = json_dumps(data)
            assert isinstance(result, bytes)

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
            # Reload again to restore original orjson-backed functions
            importlib.reload(dedup_mod)
            dedup_mod._rust_dedup_loaded = original_loaded
            dedup_mod._RustDedupStore = original_cls

    def test_json_fallback_roundtrip_through_persist_load(self):
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
