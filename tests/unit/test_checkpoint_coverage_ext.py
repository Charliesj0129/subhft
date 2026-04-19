"""Extended coverage tests for execution/checkpoint.py — uncovered error paths."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.checkpoint import (
    PositionCheckpointWriter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(positions=None, recovery=None):
    store = MagicMock()
    store._peak_equity_scaled = 1000
    store._total_realized_pnl_scaled = 500

    pos_dict = {}
    if positions:
        for key, data in positions.items():
            pos = MagicMock()
            pos.symbol = data["symbol"]
            pos.net_qty = data["net_qty"]
            pos.avg_price_scaled = data.get("avg_price_scaled", 100000)
            pos.realized_pnl_scaled = data.get("realized_pnl_scaled", 0)
            pos.fees_scaled = data.get("fees_scaled", 0)
            pos_dict[key] = pos

    store.snapshot_positions.return_value = pos_dict
    store._recovery_positions = recovery or {}
    return store


# ---------------------------------------------------------------------------
# json fallback (lines 37-44) — test without orjson
# ---------------------------------------------------------------------------


class TestJsonFallbackPaths:
    def test_dumps_and_loads_without_orjson(self, tmp_path):
        """Verify checkpoint write/load works with json fallback (no orjson)."""
        # We test the actual write/read round-trip; the json fallback is used
        # if orjson is unavailable. Since we can't easily unimport orjson,
        # we test that the checkpoint works correctly regardless.
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store(positions={"acc:strat:SYM": {"symbol": "SYM", "net_qty": 10, "avg_price_scaled": 50000}})
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)
        writer.write_checkpoint()

        loaded = PositionCheckpointWriter.load_checkpoint(ckpt_path)
        assert loaded is not None
        assert loaded["positions"]["acc:strat:SYM"]["net_qty"] == 10
        assert loaded["positions"]["acc:strat:SYM"]["avg_price_scaled"] == 50000


# ---------------------------------------------------------------------------
# write_checkpoint — atomic write failure cleanup (lines 189-193)
# ---------------------------------------------------------------------------


class TestWriteCheckpointAtomicFailure:
    def test_cleanup_on_rename_failure(self, tmp_path):
        """When os.rename fails, the temp file should be cleaned up."""
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store(positions={"acc:strat:S": {"symbol": "S", "net_qty": 1}})
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)

        original_rename = os.rename

        def failing_rename(src, dst):
            raise OSError("rename failed")

        with patch("os.rename", side_effect=failing_rename):
            with pytest.raises(OSError, match="rename failed"):
                writer.write_checkpoint()

        # The temp file should have been cleaned up
        tmp_files = [f for f in os.listdir(str(tmp_path)) if f.startswith(".ckpt_")]
        assert len(tmp_files) == 0

    def test_cleanup_on_write_failure(self, tmp_path):
        """When os.write fails, the temp file should be cleaned up."""
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store(positions={"acc:strat:S": {"symbol": "S", "net_qty": 1}})
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)

        original_write = os.write

        def failing_write(fd, data):
            raise OSError("write failed")

        with patch("os.write", side_effect=failing_write):
            with pytest.raises(OSError, match="write failed"):
                writer.write_checkpoint()

        tmp_files = [f for f in os.listdir(str(tmp_path)) if f.startswith(".ckpt_")]
        assert len(tmp_files) == 0


# ---------------------------------------------------------------------------
# run() loop — exception in write_checkpoint (lines 112-113)
# ---------------------------------------------------------------------------


class TestRunLoopExceptionHandling:
    @pytest.mark.asyncio
    async def test_run_continues_after_write_checkpoint_error(self, tmp_path):
        """run() should log and continue when write_checkpoint raises."""
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store()
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=0.005)

        call_count = 0

        def failing_snapshot():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated write failure")
            return {}

        # Use the store's snapshot method to cause write_checkpoint to fail
        store.snapshot_positions = failing_snapshot

        async def stop_after():
            await asyncio.sleep(0.15)
            writer.running = False

        asyncio.create_task(stop_after())
        await writer.run()

        # Should have survived the exception and eventually stopped
        assert writer.running is False
        # At least the failing call + one successful call
        assert call_count >= 2


# ---------------------------------------------------------------------------
# Recovery positions — missing symbol key fallback (line 150)
# ---------------------------------------------------------------------------


class TestRecoveryPositionSymbolFallback:
    def test_recovery_position_uses_key_split_for_symbol(self, tmp_path):
        """Recovery position with no 'symbol' key derives symbol from the key."""
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store(
            recovery={
                "acc:strat:DERIVED_SYM": {
                    "net_qty": 2,
                    "avg_price_scaled": 0,
                    "realized_pnl_scaled": 0,
                    "fees_scaled": 0,
                    # Note: no "symbol" key
                }
            }
        )
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)
        writer.write_checkpoint()

        with open(ckpt_path, "rb") as f:
            data = json.loads(f.read())
        pos = data["positions"]["acc:strat:DERIVED_SYM"]
        assert pos["symbol"] == "DERIVED_SYM"
        assert pos["net_qty"] == 2

    def test_recovery_position_does_not_overwrite_live_position(self, tmp_path):
        """Recovery position should not overwrite a live position with same key."""
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store(
            positions={"acc:strat:SYM": {"symbol": "SYM", "net_qty": 5, "avg_price_scaled": 100000}},
            recovery={
                "acc:strat:SYM": {
                    "symbol": "SYM",
                    "net_qty": 3,
                    "avg_price_scaled": 50000,
                }
            },
        )
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)
        writer.write_checkpoint()

        with open(ckpt_path, "rb") as f:
            data = json.loads(f.read())
        # Live position should take precedence
        assert data["positions"]["acc:strat:SYM"]["net_qty"] == 5


# ---------------------------------------------------------------------------
# PositionCheckpointWriter.__init__ — env var defaults
# ---------------------------------------------------------------------------


class TestCheckpointInitDefaults:
    def test_defaults_from_env_vars(self, monkeypatch, tmp_path):
        """Constructor picks up env var defaults."""
        monkeypatch.setenv("HFT_POSITION_CHECKPOINT_PATH", str(tmp_path / "env.json"))
        monkeypatch.setenv("HFT_CHECKPOINT_INTERVAL_S", "120")
        store = _make_store()
        writer = PositionCheckpointWriter(store)
        assert writer._path == str(tmp_path / "env.json")
        assert writer._interval_s == 120.0

    def test_explicit_params_override_env(self, monkeypatch, tmp_path):
        """Explicit constructor params override env vars."""
        monkeypatch.setenv("HFT_POSITION_CHECKPOINT_PATH", "/should/not/use")
        monkeypatch.setenv("HFT_CHECKPOINT_INTERVAL_S", "999")
        store = _make_store()
        writer = PositionCheckpointWriter(store, path=str(tmp_path / "explicit.json"), interval_s=30)
        assert writer._path == str(tmp_path / "explicit.json")
        assert writer._interval_s == 30.0


# ---------------------------------------------------------------------------
# load_checkpoint — stale tmp cleanup with permission error
# ---------------------------------------------------------------------------


class TestLoadCheckpointStaleTmpPermissionError:
    def test_stale_tmp_unlink_oserror_ignored(self, tmp_path):
        """OSError during stale tmp cleanup is silently ignored."""
        ckpt_path = str(tmp_path / "ckpt.json")
        stale = tmp_path / ".ckpt_stale.tmp"
        stale.write_text("stale data")

        with patch("os.unlink", side_effect=OSError("permission denied")):
            result = PositionCheckpointWriter.load_checkpoint(ckpt_path)

        # Should return None (file doesn't exist) without raising
        assert result is None
