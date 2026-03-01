import tempfile

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

