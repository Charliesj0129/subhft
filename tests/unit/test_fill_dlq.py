"""Tests for orphaned fill DLQ (WU-03)."""

import os
from unittest.mock import MagicMock, patch

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.execution.fill_dlq import OrphanedFillDLQ


class TestOrphanedFillDLQ:
    """WU-03: Verify orphaned fill dead-letter queue."""

    def test_add_and_count(self):
        dlq = OrphanedFillDLQ(max_size=100)
        fill = MagicMock(symbol="2330", order_id="123", strategy_id="UNKNOWN")

        dlq.add(fill)

        assert dlq.count == 1

    def test_bounded_size(self):
        dlq = OrphanedFillDLQ(max_size=3)

        for i in range(5):
            fill = MagicMock(symbol=f"sym{i}")
            dlq.add(fill)

        assert dlq.count == 3  # Bounded to max_size

    def test_drain_returns_all_items(self):
        dlq = OrphanedFillDLQ(max_size=100)

        for i in range(3):
            dlq.add(MagicMock(symbol=f"sym{i}"))

        items = dlq.drain()

        assert len(items) == 3
        assert dlq.count == 0  # Empty after drain

    def test_drain_empty_returns_empty_list(self):
        dlq = OrphanedFillDLQ()

        items = dlq.drain()

        assert items == []
        assert dlq.count == 0

    def test_initial_count_is_zero(self):
        dlq = OrphanedFillDLQ()

        assert dlq.count == 0

    def test_retry_resolves_matching_fills(self):
        dlq = OrphanedFillDLQ(max_size=100)
        fill1 = MagicMock(symbol="2330", order_id="ORD1", strategy_id="UNKNOWN")
        fill2 = MagicMock(symbol="2317", order_id="ORD2", strategy_id="UNKNOWN")
        dlq.add(fill1)
        dlq.add(fill2)

        def resolver(fill):
            return "strat_a" if fill.order_id == "ORD1" else "UNKNOWN"

        resolved, still_orphaned = dlq.retry(resolver)

        assert len(resolved) == 1
        assert resolved[0].strategy_id == "strat_a"
        assert len(still_orphaned) == 1
        assert dlq.count == 1  # Only unresolved remain

    def test_retry_all_resolved(self):
        dlq = OrphanedFillDLQ(max_size=100)
        fill = MagicMock(symbol="2330", order_id="ORD1", strategy_id="UNKNOWN")
        dlq.add(fill)

        resolved, still_orphaned = dlq.retry(lambda f: "my_strat")

        assert len(resolved) == 1
        assert len(still_orphaned) == 0
        assert dlq.count == 0

    def test_retry_none_resolved(self):
        dlq = OrphanedFillDLQ(max_size=100)
        fill = MagicMock(symbol="2330", order_id="ORD1", strategy_id="UNKNOWN")
        dlq.add(fill)

        resolved, still_orphaned = dlq.retry(lambda f: "UNKNOWN")

        assert len(resolved) == 0
        assert len(still_orphaned) == 1
        assert dlq.count == 1

    def test_retry_empty_dlq(self):
        dlq = OrphanedFillDLQ()

        resolved, still_orphaned = dlq.retry(lambda f: "strat")

        assert resolved == []
        assert still_orphaned == []


def _make_fill(symbol: str, order_id: str = "ORD1") -> FillEvent:
    return FillEvent(
        fill_id=f"fill_{order_id}",
        account_id="ACC1",
        order_id=order_id,
        strategy_id="test",
        symbol=symbol,
        side=Side.BUY,
        qty=1,
        price=1000000,
        fee=0,
        tax=0,
        ingest_ts_ns=0,
        match_ts_ns=0,
    )


class TestOverflowEvictionPersistence:
    """Evicted fills must survive persist→load round-trip.

    Production scenario: DLQ hits 1000 entries, oldest fills evicted.
    On graceful shutdown, persist() must include evicted fills, not overwrite them.
    """

    def test_evicted_fills_survive_persist_load_cycle(self, tmp_path):
        persist_path = str(tmp_path / "dlq.jsonl")
        overflow_path = str(tmp_path / "dlq_overflow.jsonl")
        dlq = OrphanedFillDLQ(max_size=2, persist_path=persist_path)

        # Add 3 fills → first one is evicted
        dlq.add(_make_fill("EVICTED", "E1"))
        dlq.add(_make_fill("KEPT1", "K1"))
        dlq.add(_make_fill("KEPT2", "K2"))
        assert dlq.count == 2

        # Graceful shutdown
        dlq.persist()

        # Reload into new DLQ (larger so it can hold all)
        dlq2 = OrphanedFillDLQ(max_size=100, persist_path=persist_path)
        dlq2.load()

        # The evicted fill MUST be recovered
        all_fills = dlq2.drain()
        symbols = {f.symbol for f in all_fills}
        assert "EVICTED" in symbols, "Evicted fill was permanently lost"
        assert "KEPT1" in symbols
        assert "KEPT2" in symbols

    def test_multiple_evictions_all_recovered(self, tmp_path):
        persist_path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=2, persist_path=persist_path)

        # Add 5 fills → 3 evicted, only last 2 in memory
        for i in range(5):
            dlq.add(_make_fill(f"SYM{i}", f"ORD{i}"))
        assert dlq.count == 2

        dlq.persist()

        dlq2 = OrphanedFillDLQ(max_size=100, persist_path=persist_path)
        dlq2.load()

        all_fills = dlq2.drain()
        symbols = {f.symbol for f in all_fills}
        # All 5 should be recoverable (3 evicted + 2 in-memory)
        for i in range(5):
            assert f"SYM{i}" in symbols, f"SYM{i} lost"


def _raise_eio(directory: str) -> None:
    raise OSError(5, "EIO")


class TestPersistDoesNotDropFillsSilently:
    """K2: a fill that cannot be serialised must be quarantined and counted.

    ``persist()`` used to ``continue`` past a bad row and then rename the file
    over the previous one, so the fill vanished and the log still reported the
    snapshot size as if everything had been written.
    """

    @staticmethod
    def _unserializable(symbol: str, order_id: str) -> FillEvent:
        fill = _make_fill(symbol, order_id)
        fill.price = 2**70  # orjson rejects integers beyond 64 bits
        return fill

    def test_persist_reports_an_unserializable_fill_instead_of_dropping_it(self, tmp_path):
        path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.add(_make_fill("GOOD", "G1"))
        dlq.add(self._unserializable("BAD", "B1"))

        written, failed = dlq.persist()

        assert (written, failed) == (1, 1)
        with open(path + ".unserializable") as f:
            quarantined = f.read()
        assert "B1" in quarantined
        reloaded = OrphanedFillDLQ(max_size=10, persist_path=path)
        loaded, skipped = reloaded.load()
        assert (loaded, skipped) == (1, 0)
        assert [fill.symbol for fill in reloaded.drain()] == ["GOOD"]

    def test_persist_counts_the_fills_it_could_not_write(self, tmp_path):
        dlq = OrphanedFillDLQ(max_size=10, persist_path=str(tmp_path / "dlq.jsonl"))
        dlq.add(self._unserializable("BAD", "B1"))

        with patch("hft_platform.execution.fill_dlq.MetricsRegistry") as registry:
            dlq.persist()

        registry.get.return_value.fill_dlq_persist_failed_total.inc.assert_called_once_with(1)

    def test_persist_of_a_clean_queue_leaves_no_quarantine_file(self, tmp_path):
        path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.add(_make_fill("GOOD", "G1"))

        written, failed = dlq.persist()

        assert (written, failed) == (1, 0)
        assert not os.path.exists(path + ".unserializable")

    def test_overflow_eviction_of_an_unserializable_fill_is_quarantined(self, tmp_path):
        path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=1, persist_path=path)
        dlq.add(self._unserializable("BAD", "B1"))

        dlq.add(_make_fill("GOOD", "G1"))  # evicts BAD -> _persist_single(BAD)

        with open(path + ".unserializable") as f:
            assert "B1" in f.read()

    def test_persist_quarantines_every_fill_when_the_disk_refuses_the_write(self, tmp_path):
        path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.add(_make_fill("GOOD", "G1"))
        dlq.add(self._unserializable("BAD", "B1"))

        with patch("hft_platform.execution.fill_dlq.os.fsync", side_effect=OSError("disk full")):
            written, failed = dlq.persist()

        assert (written, failed) == (0, 2)
        assert not os.path.exists(path)
        with open(path + ".unserializable") as f:
            quarantined = f.read()
        assert "G1" in quarantined and "B1" in quarantined

    def test_persisting_twice_does_not_quarantine_the_same_fill_twice(self, tmp_path):
        path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.add(self._unserializable("BAD", "B1"))

        dlq.persist()
        with patch("hft_platform.execution.fill_dlq.MetricsRegistry") as registry:
            dlq.persist()

        with open(path + ".unserializable") as f:
            assert len(f.read().splitlines()) == 1
        registry.get.return_value.fill_dlq_persist_failed_total.inc.assert_not_called()

    def test_a_new_failing_fill_is_quarantined_after_an_earlier_one_was_drained(self, tmp_path):
        """CPython reuses the address of a freed object; keying dedup on id() hid later fills."""
        path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        rounds = 50
        for i in range(rounds):
            dlq.add(self._unserializable("BAD", f"B{i}"))
            dlq.persist()
            dlq.drain()

        with open(path + ".unserializable") as f:
            quarantined = f.read().splitlines()
        assert len(quarantined) == rounds

    def test_a_failing_quarantine_does_not_lose_the_incoming_fill(self, tmp_path):
        dlq = OrphanedFillDLQ(max_size=1, persist_path=str(tmp_path / "dlq.jsonl"))
        dlq.add(self._unserializable("BAD", "B1"))

        with patch("hft_platform.execution.fill_dlq.MetricsRegistry") as registry:
            registry.get.return_value.fill_dlq_persist_failed_total.inc.side_effect = RuntimeError("registry gone")
            registry.get.return_value.fill_dlq_overflow_total.inc.return_value = None
            dlq.add(_make_fill("GOOD", "G1"))  # evicts BAD; quarantine raises internally

        assert [fill.symbol for fill in dlq.drain()] == ["GOOD"]

    def test_a_failed_quarantine_write_is_retried_by_the_next_persist(self, tmp_path):
        """ENOSPC on the sidecar must not mark the fill as safe: the next persist() retries it."""
        path = str(tmp_path / "dlq.jsonl")
        sidecar = path + ".unserializable"
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.add(self._unserializable("BAD", "B1"))
        os.mkdir(sidecar)  # open(sidecar, "a") now raises IsADirectoryError, an OSError

        dlq.persist()
        os.rmdir(sidecar)
        dlq.persist()

        with open(sidecar) as f:
            assert "B1" in f.read()

    def test_a_failing_metric_does_not_stop_the_quarantine_write(self, tmp_path):
        path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.add(self._unserializable("BAD", "B1"))

        with patch("hft_platform.execution.fill_dlq.MetricsRegistry") as registry:
            registry.get.return_value.fill_dlq_persist_failed_total.inc.side_effect = RuntimeError("registry gone")
            dlq.persist()

        with open(path + ".unserializable") as f:
            assert "B1" in f.read()

    def test_overflow_quarantine_on_the_event_loop_does_not_fsync(self, tmp_path):
        """add() runs on the loop: a slow disk must not stall it on the failure path."""
        dlq = OrphanedFillDLQ(max_size=1, persist_path=str(tmp_path / "dlq.jsonl"))
        dlq.add(self._unserializable("BAD", "B1"))

        with patch("hft_platform.execution.fill_dlq.os.fsync") as fsync:
            dlq.add(_make_fill("GOOD", "G1"))  # evicts BAD -> _persist_single fails -> quarantine

        fsync.assert_not_called()
        with open(str(tmp_path / "dlq.jsonl") + ".unserializable") as f:
            assert "B1" in f.read()

    def test_persist_fsyncs_a_sidecar_the_event_loop_path_only_flushed(self, tmp_path):
        """add() flushes without fsync; the shutdown persist() must still make that sidecar durable."""
        path = str(tmp_path / "dlq.jsonl")
        sidecar = path + ".unserializable"
        dlq = OrphanedFillDLQ(max_size=1, persist_path=path)
        dlq.add(self._unserializable("BAD", "B1"))
        dlq.add(_make_fill("GOOD", "G1"))  # evicts BAD -> flush-only quarantine
        synced_inodes: list[int] = []
        real_fsync = os.fsync

        def _spy(fd):
            synced_inodes.append(os.fstat(fd).st_ino)
            real_fsync(fd)

        with patch("hft_platform.execution.fill_dlq.os.fsync", _spy):
            dlq.persist()

        assert os.stat(sidecar).st_ino in synced_inodes

    def test_persist_rewrites_the_sidecar_durably_when_its_fsync_failed(self, tmp_path):
        """A failed sidecar fsync must not let persist() rename the main file over the only copy."""
        path = str(tmp_path / "dlq.jsonl")
        sidecar = path + ".unserializable"
        dlq = OrphanedFillDLQ(max_size=1, persist_path=path)
        dlq.add(self._unserializable("BAD", "B1"))
        dlq.add(_make_fill("GOOD", "G1"))  # evicts BAD -> flush-only quarantine
        real_fsync = os.fsync
        sidecar_syncs: list[bool] = []

        def _fsync(fd):
            if os.fstat(fd).st_ino == os.stat(sidecar).st_ino:
                sidecar_syncs.append(True)
                if len(sidecar_syncs) == 1:
                    raise OSError(5, "EIO")
            real_fsync(fd)

        with patch("hft_platform.execution.fill_dlq.os.fsync", _fsync):
            dlq.persist()

        assert len(sidecar_syncs) == 2  # the failed sync, then a durable rewrite
        with open(sidecar) as f:
            assert "B1" in f.read()

    def test_persist_keeps_the_old_main_file_when_a_fill_has_no_durable_copy(self, tmp_path):
        """No sidecar, no rename over the only other copy: the old file survives as .bak and the shutdown can see it."""
        path = str(tmp_path / "dlq.jsonl")
        seed = OrphanedFillDLQ(max_size=10, persist_path=path)
        seed.add(_make_fill("OLD", "O1"))
        seed.persist()
        old_bytes = open(path, "rb").read()
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.load()
        dlq.add(self._unserializable("BAD", "B1"))
        os.mkdir(path + ".unserializable")  # the sidecar can never be opened

        written, failed = dlq.persist()

        assert (written, failed) == (1, 1)
        assert dlq.last_unrecovered == 1
        backups = sorted(tmp_path.glob("dlq.jsonl.bak.*"))
        assert len(backups) == 1 and backups[0].read_bytes() == old_bytes

    def test_a_durable_quarantine_leaves_nothing_unrecovered(self, tmp_path):
        path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.add(self._unserializable("BAD", "B1"))

        dlq.persist()

        assert dlq.last_unrecovered == 0
        assert not list(tmp_path.glob("dlq.jsonl.bak.*"))

    def test_a_repeated_persist_keeps_the_backup_that_holds_the_original_fill(self, tmp_path):
        """The second persist() must not delete the first backup: it may be the last copy of a fill."""
        path = str(tmp_path / "dlq.jsonl")
        seed = OrphanedFillDLQ(max_size=10, persist_path=path)
        seed.add(_make_fill("OLD", "O1"))
        seed.persist()
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.load()
        dlq.add(self._unserializable("BAD", "B1"))
        os.mkdir(path + ".unserializable")

        dlq.persist()
        dlq.persist()

        assert any(b"O1" in b.read_bytes() for b in tmp_path.glob("dlq.jsonl.bak.*"))

    def test_persist_does_not_overwrite_the_main_file_when_the_backup_link_fails(self, tmp_path):
        path = str(tmp_path / "dlq.jsonl")
        seed = OrphanedFillDLQ(max_size=10, persist_path=path)
        seed.add(_make_fill("OLD", "O1"))
        seed.persist()
        old_bytes = open(path, "rb").read()
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.load()
        dlq.add(_make_fill("NEW", "N1"))  # makes the would-be new main file differ from the old one
        dlq.add(self._unserializable("BAD", "B1"))
        os.mkdir(path + ".unserializable")

        with patch("hft_platform.execution.fill_dlq.os.link", side_effect=OSError(28, "ENOSPC")):
            dlq.persist()

        assert open(path, "rb").read() == old_bytes
        assert dlq.last_unrecovered > 0

    def test_persist_keeps_the_new_file_when_it_cannot_replace_the_main_file(self, tmp_path):
        """Unwritable sidecar plus a failing backup link: the serialised fills must stay on disk."""
        path = str(tmp_path / "dlq.jsonl")
        seed = OrphanedFillDLQ(max_size=10, persist_path=path)
        seed.add(_make_fill("OLD", "O1"))
        seed.persist()
        old_bytes = open(path, "rb").read()
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.load()
        dlq.add(_make_fill("NEW", "N1"))
        dlq.add(self._unserializable("BAD", "B1"))
        os.mkdir(path + ".unserializable")

        with patch("hft_platform.execution.fill_dlq.os.link", side_effect=OSError(28, "ENOSPC")):
            dlq.persist()

        assert open(path, "rb").read() == old_bytes
        assert any(b"N1" in t.read_bytes() for t in tmp_path.glob("*.tmp"))

    def test_persist_reports_fills_as_not_durable_when_the_final_directory_sync_fails(self, tmp_path):
        path = str(tmp_path / "dlq.jsonl")
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.add(_make_fill("GOOD", "G1"))

        with patch.object(OrphanedFillDLQ, "_fsync_dir", staticmethod(_raise_eio)):
            written, failed = dlq.persist()

        assert (written, failed) == (1, 0)
        assert dlq.last_unrecovered == 1

    def test_persist_syncs_the_directory_holding_the_copies_before_it_renames_the_main_file(self, tmp_path):
        """A new sidecar or backup name is lost on power failure unless its parent directory is fsynced."""
        path = str(tmp_path / "dlq.jsonl")
        seed = OrphanedFillDLQ(max_size=10, persist_path=path)
        seed.add(_make_fill("OLD", "O1"))
        seed.persist()
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.load()
        dlq.add(self._unserializable("BAD", "B1"))
        events: list[str] = []
        real_fsync_dir = OrphanedFillDLQ._fsync_dir
        real_rename = os.rename

        def fsync_dir(directory):
            events.append("dirsync")
            real_fsync_dir(directory)

        def rename(src, dst):
            events.append("rename")
            real_rename(src, dst)

        with (
            patch.object(OrphanedFillDLQ, "_fsync_dir", staticmethod(fsync_dir)),
            patch("hft_platform.execution.fill_dlq.os.rename", side_effect=rename),
        ):
            dlq.persist()

        assert events.index("dirsync") < events.index("rename")
        assert events.count("dirsync") >= 2  # sidecar creation, then the main file's own rename

    def test_persist_keeps_the_main_file_when_the_directory_sync_fails(self, tmp_path):
        path = str(tmp_path / "dlq.jsonl")
        seed = OrphanedFillDLQ(max_size=10, persist_path=path)
        seed.add(_make_fill("OLD", "O1"))
        seed.persist()
        old_bytes = open(path, "rb").read()
        dlq = OrphanedFillDLQ(max_size=10, persist_path=path)
        dlq.load()
        dlq.add(_make_fill("NEW", "N1"))
        dlq.add(self._unserializable("BAD", "B1"))

        with patch.object(OrphanedFillDLQ, "_fsync_dir", staticmethod(_raise_eio)):
            dlq.persist()

        assert open(path, "rb").read() == old_bytes
        assert dlq.last_unrecovered > 0

    def test_load_counts_the_corrupt_lines_it_skips(self, tmp_path):
        import orjson

        path = tmp_path / "dlq.jsonl"
        good = orjson.dumps(
            {
                "fill_id": "f1",
                "account_id": "A",
                "order_id": "O1",
                "strategy_id": "s",
                "symbol": "GOOD",
                "side": 1,
                "qty": 1,
                "price": 1,
                "fee": 0,
                "tax": 0,
                "ingest_ts_ns": 0,
                "match_ts_ns": 0,
            }
        )
        path.write_bytes(good + b"\n{not json\n" + b'{"side": 99}\n' + good + b"\n")
        dlq = OrphanedFillDLQ(max_size=10, persist_path=str(path))

        loaded, skipped = dlq.load()

        assert (loaded, skipped) == (2, 2)
        assert dlq.count == 2
