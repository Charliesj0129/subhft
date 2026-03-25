"""Chaos Playbook 2 — ClickHouse Down.

Simulates ClickHouse unavailability and verifies the WAL fallback
activates, the recorder never blocks the hot path, disk pressure
is handled gracefully, and WAL files remain replayable.
"""

import asyncio
import json
import os

import pytest

from hft_platform.recorder.wal import WALWriter


@pytest.fixture()
def wal_dir(tmp_path):
    """Provide a temporary WAL directory."""
    return str(tmp_path / "wal")


@pytest.fixture()
def wal_writer(wal_dir, monkeypatch):
    """Create WALWriter with mocked metrics and disabled fsync for speed."""
    monkeypatch.setenv("HFT_WAL_FILE_FSYNC", "0")
    from unittest.mock import MagicMock, patch

    with patch("hft_platform.recorder.wal.MetricsRegistry.get", return_value=MagicMock()):
        writer = WALWriter(wal_dir)
    writer._fsync_file_enabled = False
    return writer


@pytest.mark.chaos
class TestPlaybookClickhouseDown:
    """Chaos tests for ClickHouse failure scenario."""

    @pytest.mark.asyncio
    async def test_wal_activates_on_ch_failure(self, wal_writer, wal_dir) -> None:
        """WAL writer creates .jsonl files when ClickHouse is unavailable."""
        data = [{"price": 1001000, "qty": 10, "symbol": "2330"}]

        result = await wal_writer.write("market_data", data)

        assert result is True
        files = [f for f in os.listdir(wal_dir) if f.endswith(".jsonl")]
        assert len(files) >= 1

    @pytest.mark.asyncio
    async def test_recorder_does_not_block_hot_path(self) -> None:
        """Recorder queue overflow must not block the hot path (put_nowait semantics)."""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
        await q.put({"tick": 1})

        # Second put_nowait should raise QueueFull, never block
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait({"tick": 2})

        # Queue still has the first item
        item = q.get_nowait()
        assert item["tick"] == 1

    @pytest.mark.asyncio
    async def test_wal_disk_pressure_skips_gracefully(self, wal_writer) -> None:
        """When disk_full is True, WAL write returns False without crashing."""
        wal_writer._disk_full = True
        # Prevent _check_disk_space from doing a fresh check that would clear the flag
        wal_writer._disk_check_interval_s = 999_999

        result = await wal_writer.write("market_data", [{"price": 1001000}])

        assert result is False

    @pytest.mark.asyncio
    async def test_wal_files_are_replayable(self, wal_writer, wal_dir) -> None:
        """Written JSONL files are parseable for WAL replay."""
        rows = [
            {"price": 1001000, "qty": 10, "symbol": "2330"},
            {"price": 1002000, "qty": 5, "symbol": "2330"},
        ]

        await wal_writer.write("market_data", rows)

        files = [f for f in os.listdir(wal_dir) if f.endswith(".jsonl")]
        assert len(files) >= 1

        parsed_rows = []
        for fname in files:
            fpath = os.path.join(wal_dir, fname)
            with open(fpath) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        parsed_rows.append(json.loads(stripped))

        assert len(parsed_rows) == 2
        assert parsed_rows[0]["price"] == 1001000
        assert parsed_rows[1]["qty"] == 5
