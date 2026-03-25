"""Tests for startup position recovery flow."""

from __future__ import annotations

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
