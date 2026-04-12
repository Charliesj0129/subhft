"""Tests for startup position recovery flow."""

from __future__ import annotations

import asyncio
import os
from dataclasses import fields
from unittest.mock import MagicMock


def _make_store():
    store = MagicMock()
    store.positions = {}
    return store


def _make_client(positions=None):
    client = MagicMock()
    client.get_positions.return_value = positions or []
    return client


def test_recovery_result_dataclass():
    from hft_platform.execution.startup_recon import RecoveryResult

    r = RecoveryResult(
        source="dual",
        positions_loaded=3,
        auto_corrected=1,
        halted=False,
        mismatches=[{"symbol": "2330", "action": "corrected"}],
    )
    assert r.source == "dual"
    assert r.positions_loaded == 3
    assert r.halted is False
    field_names = {f.name for f in fields(r)}
    assert field_names == {"source", "positions_loaded", "auto_corrected", "halted", "mismatches"}


def test_verifier_accepts_threshold_params():
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    v = StartupPositionVerifier(
        client=_make_client(),
        position_store=_make_store(),
        qty_threshold=20,
        futures_qty_threshold=5,
    )
    assert v._qty_threshold == 20
    assert v._futures_qty_threshold == 5


def test_verifier_threshold_defaults_from_env():
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    old_qty = os.environ.get("HFT_STARTUP_RECON_QTY_THRESHOLD")
    old_fut = os.environ.get("HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD")
    try:
        os.environ["HFT_STARTUP_RECON_QTY_THRESHOLD"] = "15"
        os.environ["HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD"] = "3"
        v = StartupPositionVerifier(
            client=_make_client(),
            position_store=_make_store(),
        )
        assert v._qty_threshold == 15
        assert v._futures_qty_threshold == 3
    finally:
        if old_qty is not None:
            os.environ["HFT_STARTUP_RECON_QTY_THRESHOLD"] = old_qty
        else:
            os.environ.pop("HFT_STARTUP_RECON_QTY_THRESHOLD", None)
        if old_fut is not None:
            os.environ["HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD"] = old_fut
        else:
            os.environ.pop("HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD", None)


# ---------------------------------------------------------------------------
# Task 3: recover() — dual-source merge + graduated response
# ---------------------------------------------------------------------------


def _write_checkpoint(path, trading_date, positions):
    """Write a valid checkpoint file for testing."""
    from hft_platform.execution.checkpoint import PositionCheckpointWriter
    from hft_platform.execution.positions import Position

    store = MagicMock()
    store.positions = {}
    store._peak_equity_scaled = 0
    store._total_realized_pnl_scaled = 0
    for sym, data in positions.items():
        pos = Position(
            account_id="test",
            strategy_id="",
            symbol=sym,
            net_qty=data["net_qty"],
            avg_price_scaled=data.get("avg_price_scaled", 0),
            realized_pnl_scaled=data.get("realized_pnl_scaled", 0),
        )
        store.positions[f"test::{sym}"] = pos
    store.snapshot_positions.return_value = dict(store.positions)

    writer = PositionCheckpointWriter(
        store=store,
        path=str(path),
        trading_date_provider=lambda: trading_date,
    )
    writer.write_checkpoint()


def test_recover_dual_source_match(tmp_path):
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 1000}})
    broker_positions = [{"code": "2330", "quantity": 1000}]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=ckpt_path,
    )
    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "dual"
    assert result.positions_loaded == 1
    assert result.auto_corrected == 0
    assert result.halted is False
    # Recovery positions are now stored via load_recovery, not directly in positions
    store.load_recovery.assert_called_once_with(
        account_id="test",
        symbol="2330",
        net_qty=1000,
        avg_price_scaled=0,
        realized_pnl_scaled=0,
        fees_scaled=0,
        strategy_id="",
    )


def test_recover_minor_discrepancy_auto_corrects(tmp_path):
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 1000}})
    broker_positions = [{"code": "2330", "quantity": 1005}]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=ckpt_path,
        qty_threshold=10,
    )
    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "dual"
    assert result.auto_corrected == 1
    assert result.halted is False
    # Auto-corrected to broker qty (1005) via load_recovery
    store.load_recovery.assert_called_once_with(
        account_id="test",
        symbol="2330",
        net_qty=1005,
        avg_price_scaled=0,
        realized_pnl_scaled=0,
        fees_scaled=0,
        strategy_id="",
    )


def test_recover_critical_discrepancy_halts(tmp_path):
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 1000}})
    broker_positions = [{"code": "2330", "quantity": 100}]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=ckpt_path,
        qty_threshold=10,
    )
    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.halted is True
    assert len(store.positions) == 0


def test_recover_side_mismatch_halts(tmp_path):
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 100}})
    broker_positions = [{"code": "2330", "quantity": -50, "direction": "Action.Sell"}]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=ckpt_path,
    )
    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.halted is True


def test_recover_stale_checkpoint_broker_only(tmp_path):
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260324", {"2330": {"net_qty": 500}})
    broker_positions = [{"code": "2330", "quantity": 1000}]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=ckpt_path,
    )
    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "broker_only"
    assert result.positions_loaded == 1
    assert result.halted is False
    # Broker-only recovery uses load_recovery with broker qty
    # avg_price_scaled=-1 is sentinel for "unknown cost basis"
    store.load_recovery.assert_called_once_with(
        account_id="test",
        symbol="2330",
        net_qty=1000,
        avg_price_scaled=-1,
        realized_pnl_scaled=0,
        fees_scaled=0,
        strategy_id="",
    )


def test_recover_broker_unavailable_checkpoint_only(tmp_path):
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 500, "avg_price_scaled": 6500000}})
    client = _make_client()
    client.get_positions.side_effect = Exception("broker down")
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=client,
        position_store=store,
        checkpoint_path=ckpt_path,
    )
    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "checkpoint_only"
    assert result.positions_loaded == 1
    assert result.halted is False


def test_recover_both_unavailable_halts(tmp_path):
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    client = _make_client()
    client.get_positions.side_effect = Exception("broker down")
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=client,
        position_store=store,
        checkpoint_path=str(tmp_path / "nonexistent.json"),
    )
    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.halted is True
    assert result.source == "empty"


def test_recover_no_checkpoint_broker_only(tmp_path):
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    broker_positions = [{"code": "TXFD6", "quantity": 2}]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=str(tmp_path / "nonexistent.json"),
    )
    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "broker_only"
    assert result.positions_loaded == 1
    assert result.halted is False
