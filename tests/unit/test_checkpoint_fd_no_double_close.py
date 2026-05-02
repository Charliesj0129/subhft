"""Regression test for the checkpoint fd double-close race (bug e1967c0c).

The 2026-04-25 commit introduced an ``os.close(fd)`` followed by a
``finally`` block that probed the same fd via ``os.fstat`` and closed it
a second time if the probe didn't raise. Between the two ``close``
calls, the WAL batch-timer daemon thread (`recorder/wal.py`'s
``_flush_timer_loop`` -> ``tempfile.mkstemp``) could grab the freed fd
integer (Linux always reuses the lowest free fd). The probe then saw an
"open" fd belonging to the WAL writer and the second ``close`` ripped
that fd out from under it, surfacing as
``[Errno 9] Bad file descriptor`` on the WAL's next ``write/flush/fsync``.

Production hft-engine logged 18 such events per 48h, each firing 50us-1ms
after a ``checkpoint_written`` log line.

The actionable invariant is: ``write_checkpoint`` MUST close each fd
integer at most once. We can't easily reproduce the actual race (needs
a concurrent thread plus interleaved ``tempfile.mkstemp``), but if no
fd is ever double-closed the race window does not exist.
"""

from __future__ import annotations

import os
from typing import List
from unittest.mock import MagicMock, patch

from hft_platform.execution.checkpoint import PositionCheckpointWriter


def _make_store():
    store = MagicMock()
    store._peak_equity_scaled = 1000
    store._total_realized_pnl_scaled = 500
    store.snapshot_positions.return_value = {}
    store._recovery_positions = {}
    return store


def test_write_checkpoint_does_not_double_close_fd(tmp_path):
    """No fd integer is closed more than once across a successful write."""
    ckpt_path = str(tmp_path / "ckpt.json")
    store = _make_store()
    writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)

    closed_fds: List[int] = []
    real_close = os.close

    def counting_close(fd: int) -> None:
        closed_fds.append(fd)
        real_close(fd)

    with patch("hft_platform.execution.checkpoint.os.close", side_effect=counting_close):
        writer.write_checkpoint()

    # The fd opened by ``tempfile.mkstemp`` is closed exactly once via the
    # ``with os.fdopen(fd, "wb") as f`` context manager. If the legacy
    # double-close pattern returns, the same integer would appear twice.
    assert len(closed_fds) == len(set(closed_fds)), (
        f"Double-close detected via hft_platform.execution.checkpoint.os.close: {closed_fds}"
    )


def test_write_checkpoint_does_not_double_close_fd_on_rename_failure(tmp_path):
    """Even when ``os.rename`` fails, the fd is closed exactly once."""
    ckpt_path = str(tmp_path / "ckpt.json")
    store = _make_store()
    writer = PositionCheckpointWriter(store, path=ckpt_path, interval_s=60)

    closed_fds: List[int] = []
    real_close = os.close

    def counting_close(fd: int) -> None:
        closed_fds.append(fd)
        real_close(fd)

    def failing_rename(src, dst):
        raise OSError("rename failed")

    with (
        patch("hft_platform.execution.checkpoint.os.close", side_effect=counting_close),
        patch("os.rename", side_effect=failing_rename),
    ):
        try:
            writer.write_checkpoint()
        except OSError:
            pass

    assert len(closed_fds) == len(set(closed_fds)), f"Double-close detected on rename-failure path: {closed_fds}"
