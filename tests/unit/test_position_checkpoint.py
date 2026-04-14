"""Tests for PositionCheckpointWriter (WU-04)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from unittest.mock import MagicMock

import pytest

from hft_platform.execution.checkpoint import PositionCheckpointWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_position(symbol: str, net_qty: int, avg_price: int, pnl: int) -> MagicMock:
    pos = MagicMock()
    pos.symbol = symbol
    pos.net_qty = net_qty
    pos.avg_price_scaled = avg_price
    pos.realized_pnl_scaled = pnl
    pos.fees_scaled = 0
    return pos


def _make_store(positions: dict | None = None) -> MagicMock:
    store = MagicMock()
    snapshot = positions or {}
    store.positions = snapshot
    store.snapshot_positions.return_value = snapshot
    store._peak_equity_scaled = 0
    store._total_realized_pnl_scaled = sum(pos.realized_pnl_scaled for pos in snapshot.values())
    return store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWriteCheckpoint:
    def test_writes_valid_json(self, tmp_path):
        path = str(tmp_path / "ckpt.json")
        store = _make_store({"acc:strat:2330": _make_position("2330", 10, 5000000, 100000)})
        writer = PositionCheckpointWriter(store, path=path, interval_s=1)
        writer.write_checkpoint()

        with open(path, "rb") as f:
            data = json.loads(f.read())

        assert "timestamp_ns" in data
        assert isinstance(data["timestamp_ns"], int)
        assert "positions" in data
        assert "sha256" in data
        pos = data["positions"]["acc:strat:2330"]
        assert pos["symbol"] == "2330"
        assert pos["net_qty"] == 10
        assert pos["avg_price_scaled"] == 5000000
        assert pos["realized_pnl_scaled"] == 100000

    def test_atomic_write_no_partial(self, tmp_path):
        """Checkpoint file should not exist partially — atomic rename."""
        path = str(tmp_path / "ckpt.json")
        store = _make_store()
        writer = PositionCheckpointWriter(store, path=path, interval_s=1)
        writer.write_checkpoint()

        # File exists and is valid JSON
        with open(path, "rb") as f:
            data = json.loads(f.read())
        assert isinstance(data, dict)

        # No leftover temp files
        remaining = [f for f in os.listdir(tmp_path) if f.startswith(".ckpt_")]
        assert remaining == []

    def test_sha256_hash_is_correct(self, tmp_path):
        path = str(tmp_path / "ckpt.json")
        store = _make_store({"k1": _make_position("AAPL", 5, 1500000, 0)})
        writer = PositionCheckpointWriter(store, path=path, interval_s=1)
        writer.write_checkpoint()

        with open(path, "rb") as f:
            data = json.loads(f.read())

        stored_sha = data.pop("sha256")
        # Re-serialize without sha256 and verify
        try:
            import orjson

            body = orjson.dumps(data)
        except ImportError:
            body = json.dumps(data, separators=(",", ":")).encode("utf-8")

        assert hashlib.sha256(body).hexdigest() == stored_sha

    def test_empty_positions(self, tmp_path):
        path = str(tmp_path / "ckpt.json")
        store = _make_store()
        writer = PositionCheckpointWriter(store, path=path, interval_s=1)
        writer.write_checkpoint()

        with open(path, "rb") as f:
            data = json.loads(f.read())
        assert data["positions"] == {}

    def test_creates_parent_directory(self, tmp_path):
        path = str(tmp_path / "nested" / "dir" / "ckpt.json")
        store = _make_store()
        writer = PositionCheckpointWriter(store, path=path, interval_s=1)
        writer.write_checkpoint()
        assert os.path.exists(path)


class TestLoadCheckpoint:
    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "ckpt.json")
        store = _make_store(
            {
                "a:s:X": _make_position("X", 3, 100000, 500),
                "a:s:Y": _make_position("Y", -2, 200000, -100),
            }
        )
        writer = PositionCheckpointWriter(store, path=path, interval_s=1)
        writer.write_checkpoint()

        loaded = PositionCheckpointWriter.load_checkpoint(path)
        assert loaded is not None
        assert len(loaded["positions"]) == 2
        assert loaded["positions"]["a:s:X"]["net_qty"] == 3
        assert loaded["positions"]["a:s:Y"]["net_qty"] == -2

    def test_missing_file_returns_none(self, tmp_path):
        result = PositionCheckpointWriter.load_checkpoint(str(tmp_path / "nonexistent.json"))
        assert result is None

    def test_corrupted_file_returns_none(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "wb") as f:
            f.write(b"NOT JSON AT ALL {{{")
        result = PositionCheckpointWriter.load_checkpoint(path)
        assert result is None

    def test_tampered_sha_returns_none(self, tmp_path):
        path = str(tmp_path / "ckpt.json")
        store = _make_store({"k": _make_position("Z", 1, 10000, 0)})
        writer = PositionCheckpointWriter(store, path=path, interval_s=1)
        writer.write_checkpoint()

        # Tamper with the file
        with open(path, "rb") as f:
            data = json.loads(f.read())
        data["positions"]["k"]["net_qty"] = 9999
        with open(path, "wb") as f:
            f.write(json.dumps(data).encode("utf-8"))

        result = PositionCheckpointWriter.load_checkpoint(path)
        assert result is None


class TestConfiguration:
    def test_default_path_uses_state_dir(self, monkeypatch):
        monkeypatch.delenv("HFT_POSITION_CHECKPOINT_PATH", raising=False)
        store = _make_store()
        writer = PositionCheckpointWriter(store)
        assert writer._path == ".state/position_checkpoint.json"

    def test_configurable_path(self, tmp_path):
        custom = str(tmp_path / "custom" / "pos.json")
        store = _make_store()
        writer = PositionCheckpointWriter(store, path=custom, interval_s=1)
        assert writer._path == custom

    def test_configurable_interval(self):
        store = _make_store()
        writer = PositionCheckpointWriter(store, path="/tmp/x.json", interval_s=30)
        assert writer._interval_s == 30.0

    def test_env_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_POSITION_CHECKPOINT_PATH", str(tmp_path / "env.json"))
        monkeypatch.setenv("HFT_CHECKPOINT_INTERVAL_S", "120")
        store = _make_store()
        writer = PositionCheckpointWriter(store)
        assert writer._path == str(tmp_path / "env.json")
        assert writer._interval_s == 120.0


class TestAsyncRun:
    @pytest.mark.asyncio
    async def test_run_writes_and_stops(self, tmp_path):
        path = str(tmp_path / "ckpt.json")
        store = _make_store({"k": _make_position("A", 1, 10000, 0)})
        writer = PositionCheckpointWriter(store, path=path, interval_s=0.05)

        async def stop_after_write():
            # Wait enough for at least one write
            for _ in range(20):
                await asyncio.sleep(0.02)
                if os.path.exists(path):
                    writer.running = False
                    return
            writer.running = False

        await asyncio.gather(writer.run(), stop_after_write())

        assert os.path.exists(path)
        loaded = PositionCheckpointWriter.load_checkpoint(path)
        assert loaded is not None
        assert "A" in loaded["positions"]["k"]["symbol"]
