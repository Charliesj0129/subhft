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


def test_tick_returns_true_when_disabled(tmp_path):
    lease = FileLeaderLease(lease_path=str(tmp_path / "lease.lock"), enabled=False, owner_id="x")
    result = lease.tick()
    assert result is True
    assert lease._is_leader is True


def test_tick_returns_false_when_open_raises_oserror(tmp_path):
    lease = FileLeaderLease(lease_path=str(tmp_path / "lease.lock"), enabled=True, owner_id="x")
    with patch("hft_platform.gateway.leader_lease.os.open", side_effect=OSError("permission denied")):
        result = lease.tick()
    assert result is False
    assert lease._is_leader is False


def test_tick_returns_false_when_flock_raises_oserror(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    with patch("hft_platform.gateway.leader_lease.fcntl.flock", side_effect=OSError("bad fd")):
        result = lease.tick()
    assert result is False
    assert lease._is_leader is False
    lease.release()


def test_release_sets_not_leader_when_disabled(tmp_path):
    lease = FileLeaderLease(lease_path=str(tmp_path / "lease.lock"), enabled=False, owner_id="x")
    lease.tick()
    assert lease._is_leader is True
    lease.release()
    assert lease._is_leader is False


def test_release_returns_early_when_fd_is_none(tmp_path):
    lease = FileLeaderLease(lease_path=str(tmp_path / "lease.lock"), enabled=True, owner_id="x")
    lease._is_leader = True
    lease.release()
    assert lease._is_leader is False
    assert lease._fd is None


def test_release_ignores_oserror_on_flock_unlock(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    assert lease.tick() is True
    with patch("hft_platform.gateway.leader_lease.fcntl.flock", side_effect=OSError("unlock failed")):
        lease.release()
    assert lease._is_leader is False
    assert lease._fd is None


def test_release_ignores_oserror_on_close(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    assert lease.tick() is True
    with patch("hft_platform.gateway.leader_lease.os.close", side_effect=OSError("close failed")):
        lease.release()
    assert lease._is_leader is False
    assert lease._fd is None


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


def test_write_heartbeat_returns_early_when_fd_none(tmp_path):
    lease = FileLeaderLease(lease_path=str(tmp_path / "lease.lock"), enabled=True, owner_id="x")
    assert lease._fd is None
    lease._write_heartbeat()
    assert lease._fd is None


def test_write_heartbeat_ignores_oserror_on_ftruncate(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    assert lease.tick() is True
    with patch("hft_platform.gateway.leader_lease.os.ftruncate", side_effect=OSError("truncate failed")):
        lease._write_heartbeat()
    assert lease._fd is not None
    lease.release()


def test_write_heartbeat_ignores_oserror_on_write(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    assert lease.tick() is True
    with patch("hft_platform.gateway.leader_lease.os.write", side_effect=OSError("write failed")):
        lease._write_heartbeat()
    assert lease._fd is not None
    lease.release()


def test_write_heartbeat_ignores_oserror_on_fsync(tmp_path):
    lease_path = str(tmp_path / "lease.lock")
    lease = FileLeaderLease(lease_path=lease_path, enabled=True, owner_id="x")
    assert lease.tick() is True
    with patch("hft_platform.gateway.leader_lease.os.fsync", side_effect=OSError("fsync failed")):
        lease._write_heartbeat()
    assert lease._fd is not None
    lease.release()
