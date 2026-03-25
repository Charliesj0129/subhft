"""Tests for checkpoint trading_date extension."""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_store(positions=None):
    """Create a mock PositionStore."""
    store = MagicMock()
    store.positions = positions or {}
    return store


def test_checkpoint_includes_trading_date(tmp_path):
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    path = str(tmp_path / "ckpt.json")
    store = _make_store()
    writer = PositionCheckpointWriter(
        store=store,
        path=path,
        trading_date_provider=lambda: "20260325",
    )
    writer.write_checkpoint()

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    assert data.get("trading_date") == "20260325"


def test_checkpoint_trading_date_covered_by_sha256(tmp_path):
    """Changing trading_date should invalidate the SHA-256 hash."""
    import json

    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    path = str(tmp_path / "ckpt.json")
    store = _make_store()
    writer = PositionCheckpointWriter(
        store=store,
        path=path,
        trading_date_provider=lambda: "20260325",
    )
    writer.write_checkpoint()

    # Tamper with trading_date
    with open(path, "rb") as f:
        raw = json.loads(f.read())
    raw["trading_date"] = "20260326"  # tamper
    with open(path, "w") as f:
        json.dump(raw, f)

    # Should fail verification
    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is None, "Tampered trading_date should fail SHA-256 check"


def test_checkpoint_backward_compat_no_trading_date(tmp_path):
    """Old checkpoint without trading_date should still load."""
    import hashlib
    import json

    path = str(tmp_path / "ckpt.json")

    # Write old-format checkpoint (no trading_date)
    body = {"timestamp_ns": 123456, "positions": {}}
    body_bytes = json.dumps(body, separators=(",", ":")).encode()
    sha = hashlib.sha256(body_bytes).hexdigest()
    body["sha256"] = sha
    with open(path, "w") as f:
        json.dump(body, f)

    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    assert data.get("trading_date") is None  # missing field → None


def test_checkpoint_default_trading_date_provider(tmp_path):
    """Without explicit provider, uses current date."""
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    path = str(tmp_path / "ckpt.json")
    store = _make_store()
    writer = PositionCheckpointWriter(store=store, path=path)
    writer.write_checkpoint()

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    td = data.get("trading_date")
    assert td is not None
    assert len(td) == 8
    assert td.isdigit()
