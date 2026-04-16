import fcntl
import os
import tempfile
from unittest.mock import patch

from hft_platform.gateway.leader_lease import FileLeaderLease


def test_file_leader_lease_single_leader_and_failover():
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_path = f"{tmpdir}/gateway.lock"
        l1 = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="a")
        l2 = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="b")

        assert l1.tick() is True
        assert l1.is_leader() is True
        assert l2.tick() is False
        assert l2.is_leader() is False

        l1.release()
        assert l2.tick() is True
        assert l2.is_leader() is True

        l2.release()


# ── tick() when disabled (lines 56-57) ──────────────────────────────


def test_tick_returns_true_when_disabled(tmp_path):
    lease = FileLeaderLease(
        lease_path=str(tmp_path / "lease.lock"), enabled=False, owner_id="x"
    )
    result = lease.tick()
    assert result is True
    assert lease._is_leader is True


# ── tick() OSError on os.open (lines 62-65) ─────────────────────────


def test_tick_returns_false_when_open_raises_oserror(tmp_path):
    lease = FileLeaderLease(
        lease_path=str(tmp_path / "lease.lock"), enabled=True, owner_id="x"
    )
    # fd is None initially, so tick() will try os.open
    with patch("hft_platform.gateway.leader_lease.os.open", side_effect=OSError("permission denied")):
        result = lease.tick()
    assert result is False
    assert lease._is_leader is False


# ── tick() OSError on flock (lines 75-78) ────────────────────────────


def test_tick_returns_false_when_flock_raises_oserror(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    # First open succeeds (creates fd), then flock raises generic OSError
    with patch("hft_platform.gateway.leader_lease.fcntl.flock", side_effect=OSError("bad fd")):
        result = lease.tick()
    assert result is False
    assert lease._is_leader is False
    # Clean up the fd that was opened
    lease.release()


# ── release() when disabled (lines 82-83) ────────────────────────────


def test_release_sets_not_leader_when_disabled(tmp_path):
    lease = FileLeaderLease(
        lease_path=str(tmp_path / "lease.lock"), enabled=False, owner_id="x"
    )
    # Simulate having been "leader" via tick()
    lease.tick()
    assert lease._is_leader is True
    lease.release()
    assert lease._is_leader is False


# ── release() fd=None early return (line 88) ─────────────────────────


def test_release_returns_early_when_fd_is_none(tmp_path):
    lease = FileLeaderLease(
        lease_path=str(tmp_path / "lease.lock"), enabled=True, owner_id="x"
    )
    # fd is None by default; release should not raise
    lease._is_leader = True
    lease.release()
    assert lease._is_leader is False
    assert lease._fd is None


# ── release() flock(LOCK_UN) OSError (lines 91-92) ───────────────────


def test_release_ignores_oserror_on_flock_unlock(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    # Acquire so we have a valid fd
    assert lease.tick() is True
    # Patch flock to raise OSError during LOCK_UN
    with patch("hft_platform.gateway.leader_lease.fcntl.flock", side_effect=OSError("unlock failed")):
        lease.release()  # should not raise
    assert lease._is_leader is False
    assert lease._fd is None


# ── release() os.close OSError (lines 95-96) ─────────────────────────


def test_release_ignores_oserror_on_close(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    assert lease.tick() is True
    # Patch os.close to raise OSError
    with patch("hft_platform.gateway.leader_lease.os.close", side_effect=OSError("close failed")):
        lease.release()  # should not raise
    assert lease._is_leader is False
    assert lease._fd is None


# ── status() (line 99) ───────────────────────────────────────────────


def test_status_returns_expected_dict(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="node-1")
    status = lease.status()
    assert status["enabled"] is True
    assert status["is_leader"] is False
    assert status["lease_path"] == lease_path
    assert status["owner_id"] == "node-1"


def test_status_reflects_leader_state_after_tick(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="node-2")
    lease.tick()
    status = lease.status()
    assert status["is_leader"] is True
    lease.release()


# ── _write_heartbeat() fd=None early return (line 109) ───────────────


def test_write_heartbeat_returns_early_when_fd_none(tmp_path):
    lease = FileLeaderLease(
        lease_path=str(tmp_path / "lease.lock"), enabled=True, owner_id="x"
    )
    assert lease._fd is None
    # Calling _write_heartbeat directly should not raise
    lease._write_heartbeat()
    assert lease._fd is None  # unchanged


# ── _write_heartbeat() OSError on ftruncate/write/fsync (lines 121, 123)


def test_write_heartbeat_ignores_oserror_on_ftruncate(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    # Acquire lease so _fd is set
    assert lease.tick() is True
    # Patch ftruncate to raise
    with patch("hft_platform.gateway.leader_lease.os.ftruncate", side_effect=OSError("truncate failed")):
        lease._write_heartbeat()  # should not raise
    # Lease still valid
    assert lease._fd is not None
    lease.release()


def test_write_heartbeat_ignores_oserror_on_write(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    assert lease.tick() is True
    with patch("hft_platform.gateway.leader_lease.os.write", side_effect=OSError("write failed")):
        lease._write_heartbeat()  # should not raise
    assert lease._fd is not None
    lease.release()


def test_write_heartbeat_ignores_oserror_on_fsync(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    assert lease.tick() is True
    with patch("hft_platform.gateway.leader_lease.os.fsync", side_effect=OSError("fsync failed")):
        lease._write_heartbeat()  # should not raise
    assert lease._fd is not None
    lease.release()
