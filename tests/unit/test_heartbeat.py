"""Tests for heartbeat file writer."""

from __future__ import annotations

import os
import tempfile
import time


def test_write_heartbeat_creates_file():
    from hft_platform.services.heartbeat import write_heartbeat

    with tempfile.NamedTemporaryFile(delete=False, suffix=".heartbeat") as f:
        path = f.name
    try:
        write_heartbeat(path)
        assert os.path.exists(path)
        mtime = os.path.getmtime(path)
        assert abs(time.time() - mtime) < 2
    finally:
        os.unlink(path)


def test_write_heartbeat_updates_mtime():
    from hft_platform.services.heartbeat import write_heartbeat

    with tempfile.NamedTemporaryFile(delete=False, suffix=".heartbeat") as f:
        path = f.name
    try:
        write_heartbeat(path)
        mtime1 = os.path.getmtime(path)
        time.sleep(0.05)
        write_heartbeat(path)
        mtime2 = os.path.getmtime(path)
        assert mtime2 > mtime1
    finally:
        os.unlink(path)


def test_write_heartbeat_failure_does_not_raise():
    import os

    from hft_platform.services.heartbeat import write_heartbeat

    path = "/nonexistent/dir/heartbeat.tmp"
    write_heartbeat(path)  # Should not raise when directory doesn't exist
    assert not os.path.exists(path)  # File was not created (directory missing)


def test_write_heartbeat_writes_pid():
    from hft_platform.services.heartbeat import write_heartbeat

    with tempfile.NamedTemporaryFile(delete=False, suffix=".heartbeat") as f:
        path = f.name
    try:
        write_heartbeat(path)
        content = open(path).read().strip()
        assert content == str(os.getpid())
    finally:
        os.unlink(path)
