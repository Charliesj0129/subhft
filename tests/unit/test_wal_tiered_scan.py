"""P2-a regression test: WAL tiered scan covers archive/ and dlq/.

Bug: ``_loader_cleanup.py:_check_wal_accumulation`` previously called
``os.listdir(self.wal_dir)`` non-recursively. archive/ (34 k files / 3 GB
in the live incident) and dlq/ were silently invisible to Prometheus.
Live shipped: ``wal_directory_size_bytes=0`` despite ``du -sh /app/.wal=3.0G``.

Fix: per-tier scan via ``_scan_dir_jsonl`` and labeled gauges
``wal_directory_bytes{tier=active|archive|dlq}`` plus
``wal_file_count_tiered{tier=...}``.
"""

from __future__ import annotations

import os
import time

from hft_platform.recorder._loader_cleanup import _scan_dir_jsonl


class TestScanDirJsonl:
    def test_empty_dir_returns_zeros(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        size, count, oldest = _scan_dir_jsonl(str(tmp_path), now=time.time())
        assert size == 0
        assert count == 0

    def test_missing_dir_returns_zeros(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        missing = str(tmp_path / "doesnotexist")
        size, count, oldest = _scan_dir_jsonl(missing, now=time.time())
        assert size == 0
        assert count == 0

    def test_counts_jsonl_only_at_top_level(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Ignores non-jsonl files and any nested subdirectory contents."""
        (tmp_path / "a.jsonl").write_text("hello\n")
        (tmp_path / "b.jsonl").write_text("world\n")
        (tmp_path / "c.txt").write_text("ignored\n")
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "d.jsonl").write_text("nested\n")

        size, count, oldest = _scan_dir_jsonl(str(tmp_path), now=time.time())
        assert count == 2  # a.jsonl, b.jsonl only
        assert size == len("hello\n") + len("world\n")

    def test_oldest_mtime_returns_min(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        old = tmp_path / "old.jsonl"
        new = tmp_path / "new.jsonl"
        old.write_text("o")
        new.write_text("n")
        # Force old file to have an mtime 1 hour earlier
        past = time.time() - 3600
        os.utime(str(old), (past, past))

        size, count, oldest_mtime = _scan_dir_jsonl(str(tmp_path), now=time.time())
        assert count == 2
        assert oldest_mtime <= past + 1  # within 1s of the forced mtime
