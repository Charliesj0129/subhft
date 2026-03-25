"""Chaos Playbook 5 — Disk Full.

Simulates disk space exhaustion and verifies the WAL disk pressure
circuit breaker activates, recovers when space is freed, does not
crash trading when active, and caches check results within interval.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.wal import WALWriter


@pytest.fixture()
def wal_writer(tmp_path, monkeypatch):
    """Create WALWriter with mocked metrics and disabled fsync."""
    monkeypatch.setenv("HFT_WAL_FILE_FSYNC", "0")
    with patch("hft_platform.recorder.wal.MetricsRegistry.get", return_value=MagicMock()):
        writer = WALWriter(str(tmp_path / "wal"))
    writer._fsync_file_enabled = False
    return writer


@pytest.mark.chaos
class TestPlaybookDiskFull:
    """Chaos tests for disk full scenario."""

    def test_disk_pressure_detected(self, wal_writer) -> None:
        """Setting _disk_min_mb impossibly high triggers disk pressure detection."""
        # Force a fresh check by resetting the last check timestamp
        wal_writer._last_disk_check_ts = 0.0
        wal_writer._disk_min_mb = 999_999_999  # Impossibly high threshold

        result = wal_writer._check_disk_space()

        assert result is False
        assert wal_writer._disk_full is True

    def test_disk_pressure_recovery(self, wal_writer) -> None:
        """Setting _disk_min_mb very low allows recovery from disk pressure."""
        # First, simulate disk full
        wal_writer._last_disk_check_ts = 0.0
        wal_writer._disk_min_mb = 999_999_999
        wal_writer._check_disk_space()
        assert wal_writer._disk_full is True

        # Now, set threshold very low to simulate recovery
        wal_writer._last_disk_check_ts = 0.0
        wal_writer._disk_min_mb = 0.001  # Very low threshold

        result = wal_writer._check_disk_space()

        assert result is True
        assert wal_writer._disk_full is False

    @pytest.mark.asyncio
    async def test_wal_skip_does_not_crash_trading(self, wal_writer) -> None:
        """When disk_full is True, WAL write returns False without exception."""
        wal_writer._disk_full = True
        # Prevent _check_disk_space from doing a fresh check that would clear the flag
        wal_writer._disk_check_interval_s = 999_999

        result = await wal_writer.write("market_data", [{"price": 1001000, "qty": 10}])

        assert result is False
        # Trading continues — no exception raised

    def test_disk_check_interval_caching(self, wal_writer) -> None:
        """Cached check uses previous result within the check interval."""
        # Do initial check with impossibly high threshold
        wal_writer._last_disk_check_ts = 0.0
        wal_writer._disk_min_mb = 999_999_999
        wal_writer._check_disk_space()
        assert wal_writer._disk_full is True

        # Now change threshold to very low, but don't reset last check time
        # The cached result should still return disk_full=True
        wal_writer._disk_min_mb = 0.001

        result = wal_writer._check_disk_space()

        # Should return cached result (False = disk full)
        assert result is False
        assert wal_writer._disk_full is True

        # After resetting the check timestamp, fresh check should recover
        wal_writer._last_disk_check_ts = 0.0
        result = wal_writer._check_disk_space()
        assert result is True
        assert wal_writer._disk_full is False
