"""Tests for checkpoint trading_date extension."""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_store(positions=None):
    """Create a mock PositionStore."""
    store = MagicMock()
    pos_dict = positions or {}
    store.positions = pos_dict
    store.snapshot_positions.return_value = pos_dict
    store.snapshot_positions_with_recovery.return_value = (pos_dict, {})
    store._peak_equity_scaled = 0
    store._total_realized_pnl_scaled = 0
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
    body_bytes = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
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


# ---------------------------------------------------------------------------
# M1: fees_scaled serialization
# ---------------------------------------------------------------------------


def _make_store_with_positions(positions=None):
    """Create a mock PositionStore with snapshot_positions support."""
    store = MagicMock()
    pos_dict = positions or {}
    store.positions = pos_dict
    store.snapshot_positions.return_value = pos_dict
    store.snapshot_positions_with_recovery.return_value = (pos_dict, {})
    store._peak_equity_scaled = 0
    store._total_realized_pnl_scaled = 0
    return store


def _make_position(symbol, net_qty, avg_price_scaled, realized_pnl_scaled, fees_scaled):
    """Build a simple namespace simulating a Position."""
    from types import SimpleNamespace

    return SimpleNamespace(
        symbol=symbol,
        net_qty=net_qty,
        avg_price_scaled=avg_price_scaled,
        realized_pnl_scaled=realized_pnl_scaled,
        fees_scaled=fees_scaled,
    )


def test_checkpoint_includes_fees_scaled(tmp_path):
    """M1: fees_scaled must be written to and read back from checkpoint."""
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    pos = _make_position("TMFD6", 2, 200000000, 50000, 1200)
    store = _make_store_with_positions({"acc:strat:TMFD6": pos})

    path = str(tmp_path / "ckpt.json")
    writer = PositionCheckpointWriter(store=store, path=path, trading_date_provider=lambda: "20260405")
    writer.write_checkpoint()

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    pos_data = data["positions"]["acc:strat:TMFD6"]
    assert pos_data["fees_scaled"] == 1200


def test_checkpoint_fees_scaled_zero_preserved(tmp_path):
    """M1: fees_scaled=0 should be written and read back as 0 (not omitted)."""
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    pos = _make_position("TXFD6", 1, 180000000, 0, 0)
    store = _make_store_with_positions({"acc:strat:TXFD6": pos})

    path = str(tmp_path / "ckpt.json")
    writer = PositionCheckpointWriter(store=store, path=path, trading_date_provider=lambda: "20260405")
    writer.write_checkpoint()

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    pos_data = data["positions"]["acc:strat:TXFD6"]
    assert "fees_scaled" in pos_data
    assert pos_data["fees_scaled"] == 0


# ---------------------------------------------------------------------------
# M2: portfolio aggregate persistence
# ---------------------------------------------------------------------------


def test_checkpoint_includes_portfolio_aggregates(tmp_path):
    """M2: peak_equity_scaled and total_realized_pnl_scaled must be in checkpoint."""
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    store = _make_store_with_positions()
    store._peak_equity_scaled = 999000
    store._total_realized_pnl_scaled = 850000

    path = str(tmp_path / "ckpt.json")
    writer = PositionCheckpointWriter(store=store, path=path, trading_date_provider=lambda: "20260405")
    writer.write_checkpoint()

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    assert data["peak_equity_scaled"] == 999000
    assert data["total_realized_pnl_scaled"] == 850000


def test_checkpoint_portfolio_aggregates_zero_written(tmp_path):
    """M2: zero aggregates are written (not omitted) to preserve the baseline."""
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    store = _make_store_with_positions()
    store._peak_equity_scaled = 0
    store._total_realized_pnl_scaled = 0

    path = str(tmp_path / "ckpt.json")
    writer = PositionCheckpointWriter(store=store, path=path, trading_date_provider=lambda: "20260405")
    writer.write_checkpoint()

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    assert "peak_equity_scaled" in data
    assert "total_realized_pnl_scaled" in data


# ---------------------------------------------------------------------------
# M3: _fill_lock held during checkpoint serialization
# ---------------------------------------------------------------------------


def test_checkpoint_takes_both_views_in_one_lock_acquisition(tmp_path):
    """M3, strengthened: the writer must take positions AND recovery atomically.

    It used to call ``snapshot_positions()`` (locked) and then read
    ``_recovery_positions`` bare, which is two acquisitions with a window in
    between; ``_seed_from_recovery`` popping inside that window drops the entry
    from both views and the checkpoint is written without it. Asserting the
    single-call API is what pins the guarantee -- asserting
    ``snapshot_positions`` was called does not, because the old code called it
    too and was still wrong.
    """
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    store = _make_store_with_positions()
    path = str(tmp_path / "ckpt.json")
    writer = PositionCheckpointWriter(store=store, path=path, trading_date_provider=lambda: "20260405")
    writer.write_checkpoint()

    store.snapshot_positions_with_recovery.assert_called_once()
    store.snapshot_positions.assert_not_called()


def test_checkpoint_snapshot_positions_called_not_direct_access(tmp_path):
    """M3: writer must not iterate store.positions directly (would be unlocked)."""
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    pos = _make_position("TXFD6", 1, 180000000, 0, 0)
    # snapshot returns isolated copy, positions dict returns different data
    store = _make_store_with_positions()
    store.snapshot_positions_with_recovery.return_value = ({"acc:strat:TXFD6": pos}, {})
    # positions is intentionally empty — writer must use the snapshot, not this
    store.positions = {}

    path = str(tmp_path / "ckpt.json")
    writer = PositionCheckpointWriter(store=store, path=path, trading_date_provider=lambda: "20260405")
    writer.write_checkpoint()

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    # Must have used snapshot (has the position), not positions (empty)
    assert "acc:strat:TXFD6" in data["positions"]
