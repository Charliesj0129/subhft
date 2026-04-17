"""Bug 15: TAIFEX night session rollover window for checkpoint trading_date.

The TAIFEX night session runs 15:00 → 05:00 next day (Taipei). A checkpoint
written in the 00:00-05:00 window is tagged with ``trading_date = D-1`` by
``_taifex_trading_date`` (since the session belongs to the previous calendar
day's T-day). If the platform restarts at 05:01, the naive calendar-date
comparison inside ``StartupPositionVerifier.recover`` would compute
``trading_date = D`` and reject the perfectly valid checkpoint as "stale",
silently discarding recoverable positions.

These tests lock in two mitigations:

1. ``recover`` MUST use the TAIFEX-aware trading-date helper so that during
   the night session (and its 00:00-05:00 rollover window) both sides of the
   comparison agree.
2. When the broker reports positions successfully, a ±1 calendar-day
   tolerance is applied so that an off-by-one across the 05:00 boundary still
   accepts the checkpoint (broker acts as an independent sanity check).
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from hft_platform.execution.checkpoint import (
    PositionCheckpointWriter,
    _taifex_trading_date,
)
from hft_platform.execution.startup_recon import StartupPositionVerifier


_TZ_TPE = ZoneInfo("Asia/Taipei")


def _mk_store():
    store = MagicMock()
    store.positions = {}
    store.snapshot_positions.return_value = {}
    store._peak_equity_scaled = 0
    store._total_realized_pnl_scaled = 0
    # load_recovery should be a no-op that counts invocations
    store.load_recovery = MagicMock()
    return store


def _write_checkpoint(path: str, trading_date: str) -> None:
    """Write a checkpoint containing a single non-zero futures position."""
    from types import SimpleNamespace

    store = MagicMock()
    pos = SimpleNamespace(
        symbol="TMFD6",
        net_qty=-1,
        avg_price_scaled=200_000_000,
        realized_pnl_scaled=0,
        fees_scaled=0,
    )
    store.snapshot_positions.return_value = {"acc:strat:TMFD6": pos}
    store.positions = {"acc:strat:TMFD6": pos}
    store._peak_equity_scaled = 0
    store._total_realized_pnl_scaled = 0
    writer = PositionCheckpointWriter(
        store=store,
        path=path,
        trading_date_provider=lambda: trading_date,
    )
    writer.write_checkpoint()


# ---------------------------------------------------------------------------
# Part A — _taifex_trading_date boundaries (existing behavior documentation)
# ---------------------------------------------------------------------------


def test_taifex_trading_date_at_0459_returns_previous_day():
    """04:59 CST: we are still inside the overnight session → D-1."""
    fake_now = datetime(2026, 4, 17, 4, 59, tzinfo=_TZ_TPE)
    with patch("hft_platform.execution.checkpoint.timebase.now_s", return_value=fake_now.timestamp()):
        assert _taifex_trading_date() == "20260416"


def test_taifex_trading_date_at_0501_returns_current_day():
    """05:01 CST: session has closed → D (today's calendar date)."""
    fake_now = datetime(2026, 4, 17, 5, 1, tzinfo=_TZ_TPE)
    with patch("hft_platform.execution.checkpoint.timebase.now_s", return_value=fake_now.timestamp()):
        assert _taifex_trading_date() == "20260417"


# ---------------------------------------------------------------------------
# Part B — recover() must use TAIFEX trading-date (the actual Bug 15 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_accepts_night_session_checkpoint_written_before_rollover(tmp_path):
    """Checkpoint written at 04:59 (trading_date=D-1). Recovery runs at 04:59.30.

    Before the fix: recover() used calendar date (D) → mismatch → stale.
    After the fix: recover() uses TAIFEX-aware date (D-1) → match.
    """
    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, trading_date="20260416")

    store = _mk_store()
    verifier = StartupPositionVerifier(
        client=MagicMock(),
        position_store=store,
        checkpoint_path=ckpt_path,
    )
    # Simulate a broker that's unavailable, so acceptance MUST come from the
    # checkpoint path (no broker fallback masking the bug).
    verifier._fetch_broker_positions = AsyncMock(side_effect=RuntimeError("broker offline"))  # type: ignore[method-assign]

    fake_now = datetime(2026, 4, 17, 4, 59, 30, tzinfo=_TZ_TPE)
    with patch("hft_platform.execution.startup_recon.timebase.now_s", return_value=fake_now.timestamp()), \
         patch("hft_platform.execution.checkpoint.timebase.now_s", return_value=fake_now.timestamp()):
        result = await verifier.recover()

    assert result.source == "checkpoint_only", (
        f"Expected checkpoint_only recovery, got {result.source!r}. "
        "recover() likely used calendar date instead of TAIFEX trading date."
    )
    assert result.positions_loaded == 1


@pytest.mark.asyncio
async def test_recover_accepts_checkpoint_across_0500_boundary_with_broker_tolerance(tmp_path):
    """Checkpoint at 04:59 (trading_date=D-1). Recovery at 05:01 (trading_date=D).

    Even with the TAIFEX-aware helper, the trading date legitimately rolls
    across the 05:00 boundary. Broker is available and confirms positions,
    so the ±1 day tolerance MUST accept the checkpoint rather than discard it.
    """
    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, trading_date="20260416")

    store = _mk_store()
    verifier = StartupPositionVerifier(
        client=MagicMock(),
        position_store=store,
        checkpoint_path=ckpt_path,
    )
    # Broker reports the same position (independent confirmation).
    verifier._fetch_broker_positions = AsyncMock(return_value={"TMFD6": -1})  # type: ignore[method-assign]

    fake_now = datetime(2026, 4, 17, 5, 1, 0, tzinfo=_TZ_TPE)
    with patch("hft_platform.execution.startup_recon.timebase.now_s", return_value=fake_now.timestamp()), \
         patch("hft_platform.execution.checkpoint.timebase.now_s", return_value=fake_now.timestamp()):
        result = await verifier.recover()

    # The legitimate checkpoint must NOT be discarded; dual-source recovery
    # (checkpoint + broker) is the expected outcome.
    assert result.source == "dual", (
        f"Expected dual-source recovery with ±1d tolerance; got {result.source!r}. "
        "Legitimate rollover-window checkpoint was silently discarded."
    )


@pytest.mark.asyncio
async def test_recover_rejects_checkpoint_more_than_one_day_stale(tmp_path):
    """Sanity: tolerance is ±1 day only — a truly stale (7 day old) checkpoint
    must still be rejected, falling back to broker_only."""
    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, trading_date="20260410")

    store = _mk_store()
    verifier = StartupPositionVerifier(
        client=MagicMock(),
        position_store=store,
        checkpoint_path=ckpt_path,
    )
    verifier._fetch_broker_positions = AsyncMock(return_value={"TMFD6": -1})  # type: ignore[method-assign]

    fake_now = datetime(2026, 4, 17, 10, 0, 0, tzinfo=_TZ_TPE)
    with patch("hft_platform.execution.startup_recon.timebase.now_s", return_value=fake_now.timestamp()), \
         patch("hft_platform.execution.checkpoint.timebase.now_s", return_value=fake_now.timestamp()):
        result = await verifier.recover()

    assert result.source == "broker_only"
