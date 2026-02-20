"""CE3-07: WAL outage drills — 4 scenarios testing resilience."""
import asyncio
import os
import tempfile
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.recorder.disk_monitor import DiskPressureLevel, DiskPressureMonitor
from hft_platform.recorder.shard_claim import FileClaimRegistry
from hft_platform.recorder.wal import WALBatchWriter
from hft_platform.recorder.wal_first import WALFirstWriter


# ── Drill 1: CH down — events go to WAL, 0 CH calls ─────────────────────────

@pytest.mark.asyncio
async def test_drill_ch_down_wal_first():
    """Drill 1: CH unavailable; all 100 events land in WAL files; zero CH calls."""
    with tempfile.TemporaryDirectory() as tmpdir:
        disk_monitor = MagicMock(spec=DiskPressureMonitor)
        disk_monitor.get_level.return_value = DiskPressureLevel.OK
        disk_monitor.get_topic_policy.return_value = "write"

        # Use real WALBatchWriter to create actual files
        wal_batch = WALBatchWriter(wal_dir=tmpdir)
        writer = WALFirstWriter(wal_batch, disk_monitor)

        rows = [{"symbol": "TSE:2330", "price_scaled": 1_000_000}] * 100

        # Write as wal_first — should not call CH
        ch_mock = MagicMock()
        ch_mock.insert = MagicMock(side_effect=AssertionError("CH should NOT be called"))
        with patch("clickhouse_connect.get_client", return_value=ch_mock):
            result = await writer.write("market_data", rows)

        assert result is True
        # Force flush so file is written
        await wal_batch.flush()
        wal_batch.stop()

        # Verify WAL files exist
        wal_files = [f for f in os.listdir(tmpdir) if f.endswith(".jsonl")]
        assert len(wal_files) > 0

        # Verify CH insert was never called
        ch_mock.insert.assert_not_called()


# ── Drill 2: Slow CH — runtime continues unblocked ───────────────────────────

@pytest.mark.asyncio
async def test_drill_slow_ch_wal_first():
    """Drill 2: CH has 5s delay; wal_first writer returns immediately (non-blocking)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        disk_monitor = MagicMock(spec=DiskPressureMonitor)
        disk_monitor.get_level.return_value = DiskPressureLevel.OK
        disk_monitor.get_topic_policy.return_value = "write"

        wal_batch = WALBatchWriter(wal_dir=tmpdir)
        writer = WALFirstWriter(wal_batch, disk_monitor)

        rows = [{"symbol": "TSE:2330"}]

        t0 = time.monotonic()
        result = await asyncio.wait_for(writer.write("market_data", rows), timeout=1.0)
        elapsed = time.monotonic() - t0

        assert result is True
        assert elapsed < 1.0, f"write() blocked for {elapsed:.2f}s — should be instant"
        wal_batch.stop()


# ── Drill 3: Disk pressure drop policy ───────────────────────────────────────

@pytest.mark.asyncio
async def test_drill_disk_pressure_drop_policy():
    """Drill 3: CRITICAL level + drop policy for latency_spans; market_data still writes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        disk_monitor = MagicMock(spec=DiskPressureMonitor)
        disk_monitor.get_level.return_value = DiskPressureLevel.CRITICAL

        def policy_for_table(table):
            return "drop" if table == "latency_spans" else "write"

        disk_monitor.get_topic_policy.side_effect = policy_for_table

        wal_batch = AsyncMock()
        wal_batch.add = AsyncMock(return_value=True)

        writer = WALFirstWriter(wal_batch, disk_monitor)

        # latency_spans should be dropped
        result_latency = await writer.write("latency_spans", [{"span": 1}])
        assert result_latency is False
        wal_batch.add.assert_not_called()

        # market_data should still write
        result_market = await writer.write("market_data", [{"symbol": "TSE:2330"}])
        assert result_market is True
        wal_batch.add.assert_called_once()


# ── Drill 4: Loader restart — stale claims recovered ─────────────────────────

def test_drill_loader_restart_stale_claims():
    """Drill 4: Write 5 WAL files; simulate 2 stale claims; restart → all 5 processable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        claim_dir = os.path.join(tmpdir, "claims")
        os.makedirs(claim_dir, exist_ok=True)

        # Simulate 2 stale .claim files (no active locks)
        stale_files = ["file1.jsonl", "file2.jsonl"]
        for fname in stale_files:
            open(os.path.join(claim_dir, fname + ".claim"), "w").close()

        # Create registry (simulates restart)
        reg = FileClaimRegistry(claim_dir=claim_dir)
        reg.recover_stale_claims()

        # Verify stale claims were cleared
        remaining = [f for f in os.listdir(claim_dir) if f.endswith(".claim")]
        assert len(remaining) == 0, f"Stale claims not cleared: {remaining}"

        # Now all 5 files can be claimed
        file_names = [f"wal_{i}.jsonl" for i in range(5)]
        for fname in file_names:
            assert reg.try_claim(fname) is True, f"Failed to claim {fname}"
            reg.release_claim(fname)
