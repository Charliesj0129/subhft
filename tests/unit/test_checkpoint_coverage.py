"""Coverage tests for execution/checkpoint.py — uncovered paths."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.checkpoint import (
    PositionCheckpointWriter,
    _is_closed,
    _taifex_trading_date,
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
# _taifex_trading_date
# ---------------------------------------------------------------------------


class TestTaifexTradingDate:
    def test_returns_string_format(self):
        result = _taifex_trading_date()
        assert len(result) == 8
        assert result.isdigit()

    @patch("hft_platform.execution.checkpoint.timebase")
    def test_night_session_uses_previous_day(self, mock_timebase):
        """Before 05:00 Taipei time, use D-1."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # 2026-04-16 03:00 Taipei -> should return 20260415
        tpe = ZoneInfo("Asia/Taipei")
        dt = datetime(2026, 4, 16, 3, 0, 0, tzinfo=tpe)
        mock_timebase.now_s.return_value = dt.timestamp()
        result = _taifex_trading_date()
        assert result == "20260415"

    @patch("hft_platform.execution.checkpoint.timebase")
    def test_day_session_uses_current_day(self, mock_timebase):
        """After 05:00 Taipei time, use current day."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tpe = ZoneInfo("Asia/Taipei")
        dt = datetime(2026, 4, 16, 10, 0, 0, tzinfo=tpe)
        mock_timebase.now_s.return_value = dt.timestamp()
        result = _taifex_trading_date()
        assert result == "20260416"


# ---------------------------------------------------------------------------
# PositionCheckpointWriter.write_checkpoint
# ---------------------------------------------------------------------------


class TestWriteCheckpoint:
    def test_writes_json_with_sha256(self, tmp_path):
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store(positions={"acc:strat:SYM": {"symbol": "SYM", "net_qty": 5}})
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)
        result_path = writer.write_checkpoint()
        assert result_path == ckpt_path
        assert os.path.exists(ckpt_path)

        with open(ckpt_path, "rb") as f:
            data = json.loads(f.read())
        assert "sha256" in data
        assert "positions" in data
        assert "acc:strat:SYM" in data["positions"]
        assert data["positions"]["acc:strat:SYM"]["net_qty"] == 5

    def test_writes_recovery_positions(self, tmp_path):
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store(
            recovery={
                "acc:strat:REC": {
                    "symbol": "REC",
                    "net_qty": 3,
                    "avg_price_scaled": 50000,
                    "realized_pnl_scaled": 100,
                    "fees_scaled": 10,
                }
            }
        )
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)
        writer.write_checkpoint()

        with open(ckpt_path, "rb") as f:
            data = json.loads(f.read())
        assert "acc:strat:REC" in data["positions"]
        assert data["positions"]["acc:strat:REC"]["net_qty"] == 3

    def test_unknown_basis_flag(self, tmp_path):
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store(positions={"acc:strat:UNK": {"symbol": "UNK", "net_qty": 2, "avg_price_scaled": -1}})
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)
        writer.write_checkpoint()

        with open(ckpt_path, "rb") as f:
            data = json.loads(f.read())
        assert data["positions"]["acc:strat:UNK"]["unknown_basis"] is True

    def test_creates_parent_directory(self, tmp_path):
        ckpt_path = str(tmp_path / "subdir" / "nested" / "ckpt.json")
        store = _make_store()
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)
        writer.write_checkpoint()
        assert os.path.exists(ckpt_path)

    def test_recovery_unknown_basis_flag(self, tmp_path):
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store(
            recovery={
                "acc:strat:NEG": {
                    "symbol": "NEG",
                    "net_qty": 1,
                    "avg_price_scaled": -1,
                    "realized_pnl_scaled": 0,
                    "fees_scaled": 0,
                }
            }
        )
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)
        writer.write_checkpoint()
        with open(ckpt_path, "rb") as f:
            data = json.loads(f.read())
        assert data["positions"]["acc:strat:NEG"]["unknown_basis"] is True


# ---------------------------------------------------------------------------
# PositionCheckpointWriter.load_checkpoint
# ---------------------------------------------------------------------------


class TestLoadCheckpoint:
    def test_load_valid_checkpoint(self, tmp_path):
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store(positions={"acc:strat:SYM": {"symbol": "SYM", "net_qty": 5}})
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)
        writer.write_checkpoint()

        loaded = PositionCheckpointWriter.load_checkpoint(ckpt_path)
        assert loaded is not None
        assert "positions" in loaded
        assert "sha256" in loaded

    def test_load_missing_file(self, tmp_path):
        result = PositionCheckpointWriter.load_checkpoint(str(tmp_path / "nope.json"))
        assert result is None

    def test_load_corrupt_json(self, tmp_path):
        ckpt_path = str(tmp_path / "bad.json")
        with open(ckpt_path, "w") as f:
            f.write("not json")
        result = PositionCheckpointWriter.load_checkpoint(ckpt_path)
        assert result is None

    def test_load_missing_sha256(self, tmp_path):
        ckpt_path = str(tmp_path / "nohash.json")
        with open(ckpt_path, "w") as f:
            json.dump({"positions": {}}, f)
        result = PositionCheckpointWriter.load_checkpoint(ckpt_path)
        assert result is None

    def test_load_sha256_mismatch(self, tmp_path):
        ckpt_path = str(tmp_path / "badhash.json")
        with open(ckpt_path, "w") as f:
            json.dump({"positions": {}, "sha256": "wrong"}, f)
        result = PositionCheckpointWriter.load_checkpoint(ckpt_path)
        assert result is None

    def test_cleans_stale_tmp_files(self, tmp_path):
        ckpt_path = str(tmp_path / "ckpt.json")
        stale = tmp_path / ".ckpt_abc123.tmp"
        stale.write_text("stale")
        PositionCheckpointWriter.load_checkpoint(ckpt_path)
        assert not stale.exists()


# ---------------------------------------------------------------------------
# PositionCheckpointWriter.clear_checkpoint
# ---------------------------------------------------------------------------


class TestClearCheckpoint:
    def test_clear_existing_file(self, tmp_path):
        ckpt_path = str(tmp_path / "ckpt.json")
        with open(ckpt_path, "w") as f:
            f.write("{}")
        result = PositionCheckpointWriter.clear_checkpoint(ckpt_path)
        assert result is True
        assert not os.path.exists(ckpt_path)

    def test_clear_nonexistent_file(self, tmp_path):
        result = PositionCheckpointWriter.clear_checkpoint(str(tmp_path / "nope.json"))
        assert result is False

    def test_clear_uses_default_path(self):
        # Should not raise; just verifies it resolves a path
        result = PositionCheckpointWriter.clear_checkpoint(None)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _is_closed helper
# ---------------------------------------------------------------------------


class TestIsClosed:
    def test_open_fd_returns_false(self, tmp_path):
        f = open(tmp_path / "test.txt", "w")
        fd = f.fileno()
        assert _is_closed(fd) is False
        f.close()

    def test_closed_fd_returns_true(self, tmp_path):
        f = open(tmp_path / "test.txt", "w")
        fd = f.fileno()
        f.close()
        assert _is_closed(fd) is True


# ---------------------------------------------------------------------------
# PositionCheckpointWriter.run async loop
# ---------------------------------------------------------------------------


class TestCheckpointRunLoop:
    @pytest.mark.asyncio
    async def test_run_calls_write_checkpoint(self, tmp_path):
        ckpt_path = str(tmp_path / "ckpt.json")
        store = _make_store()
        writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=0.01)

        import asyncio

        async def stop_after():
            await asyncio.sleep(0.05)
            writer.running = False

        asyncio.create_task(stop_after())
        await writer.run()
        assert writer.running is False
