"""Tests for DLQ write durability (P-06).

Verifies that write_to_dlq() calls fsync to ensure crash safety.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hft_platform.recorder._loader_dlq import write_to_dlq


def _make_svc(tmp_path):
    """Create a minimal WALLoaderService-like object."""
    dlq_dir = str(tmp_path / "dlq")
    return SimpleNamespace(
        dlq_dir=dlq_dir,
        metrics=MagicMock(),
    )


def test_dlq_write_calls_fsync(tmp_path):
    """write_to_dlq must call os.fsync() after writing for crash safety."""
    svc = _make_svc(tmp_path)
    rows = [{"symbol": "2330", "price": 100}]

    with patch("os.fsync", wraps=os.fsync) as mock_fsync:
        write_to_dlq(svc, "market_data", rows, "test error")

    assert mock_fsync.call_count == 1, f"Expected 1 fsync call, got {mock_fsync.call_count}"


def test_dlq_write_creates_file(tmp_path):
    """write_to_dlq must create a .jsonl file in the DLQ directory."""
    svc = _make_svc(tmp_path)
    rows = [{"x": 1}]

    write_to_dlq(svc, "orders", rows, "ch down")

    dlq_files = list((tmp_path / "dlq").glob("*.jsonl"))
    assert len(dlq_files) == 1
    content = dlq_files[0].read_text()
    assert "orders" in content
