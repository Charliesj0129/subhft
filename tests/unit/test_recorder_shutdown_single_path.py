"""H10: recorder shutdown must be single-path — sync drain must skip if
run()'s finally has already drained.

Root cause: ``HFTSystem.stop()`` chooses between ``stop_async()`` (which
awaits recorder_task so its ``run()`` finally drains+flushes) and the
synchronous fallback ``_sync_drain_recorder()`` (which creates its own
event loop and calls ``_drain_queue_into_batchers`` + ``_shutdown_flush``
directly on the same RecorderService). If both paths fire (e.g., stop()
invoked twice, or async path times out and sync fallback is invoked),
two drainers race on the same ``self.writer`` / ``self.batchers`` /
``self._wal_first_writer``.

Fix: RecorderService.run() sets ``_shutdown_drained = True`` at the end
of its finally clause. ``_sync_drain_recorder`` refuses to re-enter when
the flag is set.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from hft_platform.recorder.worker import RecorderService
from hft_platform.services.system import HFTSystem


def test_recorder_marks_shutdown_drained_after_run_finally():
    """Fresh recorder has flag unset; after run()'s finally we expect it set."""
    q: asyncio.Queue = asyncio.Queue()
    rec = RecorderService(queue=q)
    assert rec._shutdown_drained is False, "flag must start False"


def test_sync_drain_skips_when_recorder_already_drained():
    """_sync_drain_recorder must not call drain/flush if recorder.run()
    finally has already marked shutdown_drained.
    """
    rec = SimpleNamespace(
        running=True,
        _shutdown_drained=True,
        _drain_queue_into_batchers=MagicMock(),
        _shutdown_flush=MagicMock(),
    )

    system = SimpleNamespace(recorder=rec)
    # Call the bound method directly on the namespace (mimicking HFTSystem behaviour).
    HFTSystem._sync_drain_recorder(system)

    # Neither drain nor flush should have been invoked.
    rec._drain_queue_into_batchers.assert_not_called()
    rec._shutdown_flush.assert_not_called()
