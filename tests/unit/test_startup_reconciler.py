"""Bug #32 structural fix: startup broker-fill backfill.

Symptom: engine restart loses in-flight Shioaji callbacks; broker-side fills
that arrived during the restart window never reach hft.fills. Position drifts
silently.

Fix: on bootstrap (after broker login, before strategies start), reconciler
queries broker for today's fills (closed P&L details + open position FIFO),
diffs against hft.fills, inserts missing rows with strategy_id='UNKNOWN' so
forensic queries see the full picture.

Design choices (Bug #32-A):
  Q1: blocks bootstrap (strategies don't start until backfill completes)
  Q2: missing fills tagged strategy_id='UNKNOWN' (no time-window guessing)
  Q3: queries BOTH list_profit_loss_detail and list_position_detail
  Q4: emits startup_reconciler_missing_fills_total{result=...}
       and startup_reconciler_run_seconds histogram
  Q5: broker query failure is fail-soft (log + metric, don't crash bootstrap)
"""

from __future__ import annotations

import dataclasses
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.services.startup_reconciler import (
    ReconcileResult,
    StartupReconciler,
)


def _stub_metrics() -> MagicMock:
    m = MagicMock()
    for name in (
        "startup_reconciler_missing_fills_total",
        "startup_reconciler_run_seconds",
    ):
        setattr(m, name, MagicMock())
    return m


@dataclasses.dataclass
class _StubBrokerFill:
    """Mimics Shioaji P&L detail / position detail row attributes."""

    id: str = "FILL_BROKER_1"
    code: str = "TMFE6"
    order_id: str = "BROKER_v002A"
    seqno: str = "BROKER_v002A"  # some Shioaji versions use seqno
    action: str = "Buy"
    quantity: int = 1
    price: float = 37500.0
    ts: int = 1_776_000_000_000_000_000  # ns


def _broker_account_query(
    closed_details: list[_StubBrokerFill] | None = None,
    open_positions: list[_StubBrokerFill] | None = None,
    raise_on_pnl: bool = False,
) -> MagicMock:
    api = MagicMock()
    pnl_summary = [MagicMock(id=1)] if closed_details else []
    if raise_on_pnl:
        api.list_profit_loss = MagicMock(side_effect=RuntimeError("broker down"))
    else:
        api.list_profit_loss = MagicMock(return_value=pnl_summary)
    api.list_profit_loss_detail = MagicMock(return_value=closed_details or [])
    api.list_position_detail = MagicMock(return_value=open_positions or [])
    return api


@pytest.mark.asyncio
async def test_inserts_missing_fills_from_broker() -> None:
    """Broker reports 5 fills, CH has 3 → reconciler inserts the 2 missing."""
    metrics = _stub_metrics()
    broker_fills = [_StubBrokerFill(id=f"F{i}", order_id=f"O{i}", quantity=1) for i in range(5)]
    api = _broker_account_query(closed_details=broker_fills)
    ch = AsyncMock()
    ch.fetch_existing_fill_keys = AsyncMock(return_value={("O0", "F0"), ("O1", "F1"), ("O2", "F2")})
    ch.insert_fill = AsyncMock()

    reconciler = StartupReconciler(
        broker_account_query=api,
        ch_fills_query=ch,
        metrics=metrics,
        today=date(2026, 4, 20),
    )
    result: ReconcileResult = await reconciler.run()

    assert result.broker_fills == 5
    assert result.platform_fills == 3
    assert result.inserted == 2
    assert ch.insert_fill.await_count == 2
    inserted_keys = {
        (call.args[0]["broker_order_id"], call.args[0]["fill_id"]) for call in ch.insert_fill.await_args_list
    }
    assert inserted_keys == {("O3", "F3"), ("O4", "F4")}
    for call in ch.insert_fill.await_args_list:
        assert call.args[0]["strategy_id"] == "UNKNOWN"
        assert call.args[0]["source"] == "startup_reconciler"


@pytest.mark.asyncio
async def test_no_op_when_already_synced() -> None:
    """Broker fills == CH fills → 0 inserts."""
    metrics = _stub_metrics()
    broker_fills = [_StubBrokerFill(id=f"F{i}", order_id=f"O{i}") for i in range(3)]
    api = _broker_account_query(closed_details=broker_fills)
    ch = AsyncMock()
    ch.fetch_existing_fill_keys = AsyncMock(return_value={("O0", "F0"), ("O1", "F1"), ("O2", "F2")})
    ch.insert_fill = AsyncMock()

    reconciler = StartupReconciler(
        broker_account_query=api,
        ch_fills_query=ch,
        metrics=metrics,
        today=date(2026, 4, 20),
    )
    result = await reconciler.run()

    assert result.inserted == 0
    ch.insert_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedup_by_broker_order_id_and_fill_id() -> None:
    """Same (broker_order_id, fill_id) appearing twice in broker results
    should still only be considered once."""
    metrics = _stub_metrics()
    dup = _StubBrokerFill(id="F1", order_id="O1", quantity=1)
    api = _broker_account_query(closed_details=[dup, dup])
    ch = AsyncMock()
    ch.fetch_existing_fill_keys = AsyncMock(return_value=set())
    ch.insert_fill = AsyncMock()

    reconciler = StartupReconciler(
        broker_account_query=api,
        ch_fills_query=ch,
        metrics=metrics,
        today=date(2026, 4, 20),
    )
    result = await reconciler.run()

    assert result.inserted == 1
    assert ch.insert_fill.await_count == 1


@pytest.mark.asyncio
async def test_broker_api_failure_is_fail_soft() -> None:
    """If list_profit_loss raises, reconciler logs+meters but does not raise."""
    metrics = _stub_metrics()
    api = _broker_account_query(raise_on_pnl=True)
    ch = AsyncMock()
    ch.fetch_existing_fill_keys = AsyncMock(return_value=set())
    ch.insert_fill = AsyncMock()

    reconciler = StartupReconciler(
        broker_account_query=api,
        ch_fills_query=ch,
        metrics=metrics,
        today=date(2026, 4, 20),
    )
    # MUST NOT raise — bootstrap must continue
    result = await reconciler.run()

    assert result.inserted == 0
    assert result.broker_query_error is True
    metrics.startup_reconciler_missing_fills_total.labels.assert_any_call(result="error")
    ch.insert_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_handles_both_closed_and_open_positions() -> None:
    """Closed (list_profit_loss_detail) + open (list_position_detail) merged
    and deduped by (broker_order_id, fill_id)."""
    metrics = _stub_metrics()
    closed = [_StubBrokerFill(id="F1", order_id="O1")]
    open_pos = [
        _StubBrokerFill(id="F1", order_id="O1"),  # duplicate of closed
        _StubBrokerFill(id="F2", order_id="O2"),  # unique to open
    ]
    api = _broker_account_query(closed_details=closed, open_positions=open_pos)
    ch = AsyncMock()
    ch.fetch_existing_fill_keys = AsyncMock(return_value=set())
    ch.insert_fill = AsyncMock()

    reconciler = StartupReconciler(
        broker_account_query=api,
        ch_fills_query=ch,
        metrics=metrics,
        today=date(2026, 4, 20),
    )
    result = await reconciler.run()

    assert result.inserted == 2
    inserted_keys = {
        (call.args[0]["broker_order_id"], call.args[0]["fill_id"]) for call in ch.insert_fill.await_args_list
    }
    assert inserted_keys == {("O1", "F1"), ("O2", "F2")}
