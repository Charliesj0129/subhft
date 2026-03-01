"""Tests for P2b: hft recorder status CLI command."""

from __future__ import annotations

import os
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch


def _run_recorder_status(wal_dir: str, ck_host: str = "localhost", extra_env: dict | None = None):
    """Helper to invoke cmd_recorder_status and capture stdout."""
    import argparse

    from hft_platform.cli import cmd_recorder_status

    args = argparse.Namespace(wal_dir=wal_dir, ck_host=ck_host)
    env = {"HFT_RECORDER_MODE": "direct", "HFT_CLICKHOUSE_HOST": ck_host}
    if extra_env:
        env.update(extra_env)

    with patch.dict(os.environ, env, clear=False):
        captured = StringIO()
        with patch("sys.stdout", captured):
            cmd_recorder_status(args)
    return captured.getvalue()


def test_no_wal_dir(tmp_path: Path):
    """Non-existent WAL dir should show '0 files' without raising."""
    nonexistent = str(tmp_path / "nonexistent_wal")
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        output = _run_recorder_status(nonexistent)
    assert "0 files" in output


def test_counts_wal_files(tmp_path: Path):
    """3 .wal files in WAL dir should report '3 files' in output."""
    for i in range(3):
        (tmp_path / f"segment_{i:04d}.wal").write_text("x")
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        output = _run_recorder_status(str(tmp_path))
    assert "3 files" in output


def test_ck_unreachable(tmp_path: Path):
    """When urlopen raises OSError, output should contain 'unreachable'."""
    with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
        output = _run_recorder_status(str(tmp_path))
    assert "unreachable" in output


def test_ck_ok(tmp_path: Path):
    """When urlopen succeeds with status 200, output should contain 'ok'."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    with patch("urllib.request.urlopen", return_value=mock_resp):
        output = _run_recorder_status(str(tmp_path))
    assert "ok" in output


def test_env_vars_shown(tmp_path: Path):
    """Patching HFT_RECORDER_MODE=wal_first should appear in output."""
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        output = _run_recorder_status(str(tmp_path), extra_env={"HFT_RECORDER_MODE": "wal_first"})
    assert "wal_first" in output
