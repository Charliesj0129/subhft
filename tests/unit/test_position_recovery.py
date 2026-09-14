"""Tests for startup position recovery flow."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from hft_platform.contracts.constants import MANUAL_STRATEGY_ID

# ``_ckpt_date_within_tolerance`` accepts an adjacent trading date only while the
# Taipei wall clock is inside the 05:00-05:05 TAIFEX handover window. Any test
# that feeds a D-1 checkpoint therefore has two different correct answers
# depending on what time the suite happens to run, and pinning the clock is the
# only way to assert either of them.
#
# This is not hypothetical: the nightly CI run on 2026-09-13 executed
# ``test_recover_stale_checkpoint_broker_only`` at 21:04Z = 05:04 Taipei and
# failed with ``assert 'dual' == 'broker_only'`` while every PR run that day
# passed. The schedule lands inside the one six-minute window per day where the
# unpinned assertion is wrong.
_TAIPEI = ZoneInfo("Asia/Taipei")

# 2026-09-14 is an ordinary Monday; only the time-of-day matters to the gate.
_OUTSIDE_HANDOVER = datetime(2026, 9, 14, 11, 30, tzinfo=_TAIPEI)
_INSIDE_HANDOVER = datetime(2026, 9, 14, 5, 2, tzinfo=_TAIPEI)


@contextmanager
def _taipei_clock(when: datetime):
    """Pin ``startup_recon``'s clock so the handover gate is deterministic."""
    with patch(
        "hft_platform.execution.startup_recon.timebase.now_s",
        return_value=when.timestamp(),
    ):
        yield


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
    store.snapshot_positions_with_recovery.return_value = (dict(store.positions), {})

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
    """A D-1 checkpoint is discarded outside the 05:00-05:05 handover window.

    The clock is pinned because the gate reads it: see ``_taipei_clock``.
    """
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
    with _taipei_clock(_OUTSIDE_HANDOVER):
        result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "broker_only"
    assert result.positions_loaded == 1
    assert result.halted is False
    # Broker-only recovery uses load_recovery with broker qty
    # avg_price_scaled=-1 is sentinel for "unknown cost basis"
    # strategy_id=MANUAL_STRATEGY_ID marks manual ownership for broker-only positions
    store.load_recovery.assert_called_once_with(
        account_id="test",
        symbol="2330",
        net_qty=1000,
        avg_price_scaled=-1,
        realized_pnl_scaled=0,
        fees_scaled=0,
        strategy_id=MANUAL_STRATEGY_ID,
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


# ---------------------------------------------------------------------------
# The 05:00-05:05 TAIFEX handover window (``_ckpt_date_within_tolerance``).
#
# The +/-1-day tolerance exists so a checkpoint written at 04:59 with
# trading_date=D-1 is still recognised by a recovery run at 05:01, whose
# trading_date is already D. Until now nothing asserted either side of that
# gate: the only test that exercised it asserted the *outside* answer without
# pinning the clock, so the branch was covered by accident for 1434 minutes a
# day and contradicted for the other six.
# ---------------------------------------------------------------------------


def _stale_ckpt_verifier(tmp_path, ckpt_date, ckpt_qty=1000, broker_qty=1000):
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / f"ckpt_{ckpt_date}.json")
    _write_checkpoint(ckpt_path, ckpt_date, {"2330": {"net_qty": ckpt_qty}})
    return StartupPositionVerifier(
        client=_make_client([{"code": "2330", "quantity": broker_qty}]),
        position_store=_make_store(),
        checkpoint_path=ckpt_path,
    )


def test_previous_day_checkpoint_accepted_inside_the_handover_window(tmp_path):
    """At 05:02 Taipei a D-1 checkpoint is still the night session's own record.

    Quantities agree, so the only thing this can distinguish is which source
    recovery chose: ``dual`` means the checkpoint was read, ``broker_only``
    means it was discarded.
    """
    verifier = _stale_ckpt_verifier(tmp_path, "20260324")

    with _taipei_clock(_INSIDE_HANDOVER):
        result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))

    assert result.source == "dual"
    assert result.halted is False


def test_previous_day_checkpoint_rejected_one_minute_after_the_window(tmp_path):
    """05:06 is outside the window, so the same checkpoint is discarded."""
    verifier = _stale_ckpt_verifier(tmp_path, "20260324")

    with _taipei_clock(datetime(2026, 9, 14, 5, 6, tzinfo=_TAIPEI)):
        result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))

    assert result.source == "broker_only"


def test_previous_day_checkpoint_rejected_one_minute_before_the_window(tmp_path):
    """04:59 is outside it too — the gate opens at 05:00, not around it."""
    verifier = _stale_ckpt_verifier(tmp_path, "20260324")

    with _taipei_clock(datetime(2026, 9, 14, 4, 59, tzinfo=_TAIPEI)):
        result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))

    assert result.source == "broker_only"


def test_two_day_old_checkpoint_rejected_even_inside_the_handover_window(tmp_path):
    """The window widens the tolerance to one day, not to any stale file.

    A D-2 checkpoint cannot be a night session straddling this rollover, so the
    window must not admit it. Without this, a Monday 05:02 restart would read a
    Friday book as current.
    """
    verifier = _stale_ckpt_verifier(tmp_path, "20260323")

    with _taipei_clock(_INSIDE_HANDOVER):
        result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))

    assert result.source == "broker_only"


def test_same_trading_date_checkpoint_accepted_regardless_of_clock(tmp_path):
    """The window gates only the tolerance; an exact date match never needs it."""
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    for when in (_INSIDE_HANDOVER, _OUTSIDE_HANDOVER):
        ckpt_path = str(tmp_path / f"ckpt_same_{when.hour}.json")
        _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 1000}})
        verifier = StartupPositionVerifier(
            client=_make_client([{"code": "2330", "quantity": 1000}]),
            position_store=_make_store(),
            checkpoint_path=ckpt_path,
        )
        with _taipei_clock(when):
            result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
        assert result.source == "dual", f"same-date checkpoint rejected at {when:%H:%M}"
