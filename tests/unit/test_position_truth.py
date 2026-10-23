"""One position truth: recovered lots, checkpoint identity, fill dedup order, fees.

Each test pins a defect on origin/main ``b5e30cbd``:

* a fill for one strategy took another strategy's recovered lot (suffix match)
  or popped and dropped a recovered lot when its key already existed;
* the checkpoint carried no account/broker/venue, so a paper run's book
  restored into a live engine;
* the router marked a fill seen before applying it, so an orphan resolved from
  the DLQ was skipped as its own duplicate, and a fill whose apply raised could
  never be re-applied;
* the daily-loss stop was fed realized PnL gross of fees.
"""

from __future__ import annotations

import asyncio
import dataclasses
import glob
import json
import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.core import timebase
from hft_platform.execution.checkpoint import (
    PositionCheckpointWriter,
    checkpoint_identity,
    checkpoint_identity_mismatch,
)
from hft_platform.execution.fill_dlq import OrphanedFillDLQ
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.execution.router import ExecutionRouter
from hft_platform.execution.startup_recon import StartupPositionVerifier
from hft_platform.ops.position_flattener import PositionFlattener

SCALE = 10_000
PX = 46_600 * SCALE


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(tmp_path / "fill_dedup.jsonl"))
    monkeypatch.setenv("HFT_FILL_DLQ_PERSIST_PATH", str(tmp_path / "fill_dlq.jsonl"))


def _fill(
    *,
    strategy_id: str = "R47",
    account_id: str = "F1",
    symbol: str = "TMFK6",
    side: Side = Side.BUY,
    qty: int = 1,
    fee: int = 0,
    fill_id: str = "D1",
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id=account_id,
        order_id="O1",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=PX,
        fee=fee,
        tax=0,
        ingest_ts_ns=1,
        match_ts_ns=1,
    )


# ---------------------------------------------------------------------------
# Recovery merge
# ---------------------------------------------------------------------------


def test_recovery_merge_leaves_another_strategys_lot_in_place():
    store = PositionStore()
    # Stored under the broker id, so only the suffix fallback can match it.
    store.load_recovery("shioaji", "TMFK6", 1, PX, strategy_id="OTHER")

    store.on_fill(_fill(strategy_id="R47"))

    assert store.net_qty_for_symbol("TMFK6", "R47") == 1
    assert store.net_qty_for_symbol("TMFK6", "OTHER") == 1
    assert store.net_qty_for_symbol("TMFK6") == 2


def test_recovery_merge_still_matches_the_same_strategy_across_account_domains():
    store = PositionStore()
    store.load_recovery("shioaji", "TMFK6", 2, PX, strategy_id="R47")

    store.on_fill(_fill(strategy_id="R47", side=Side.SELL))

    assert store.net_qty_for_symbol("TMFK6", "R47") == 1


def test_recovery_merge_keeps_the_lot_when_the_key_already_has_a_position():
    store = PositionStore()
    store.on_fill(_fill(fill_id="D1"))
    store.load_recovery("F1", "TMFK6", 1, PX)  # legacy entry, no strategy

    store.on_fill(_fill(fill_id="D2"))

    assert store.net_qty_for_symbol("TMFK6") == 3  # 2 traded + 1 still parked


# ---------------------------------------------------------------------------
# One view for every reader
# ---------------------------------------------------------------------------


def test_reconciliation_reads_positions_and_recovery_in_one_snapshot():
    store = PositionStore()
    store.on_fill(_fill())
    store.load_recovery("F1", "TMFJ6", 1, PX, strategy_id="R47")
    calls: list[str] = []
    original = store.snapshot_positions_with_recovery

    def _spy():
        calls.append("both")
        return original()

    store.snapshot_positions_with_recovery = _spy  # type: ignore[method-assign]  # spy on the atomic read
    svc = ReconciliationService.__new__(ReconciliationService)
    svc.store = store

    positions, recovery = svc._local_snapshot()

    assert calls == ["both"]
    assert {p.symbol for p in positions.values()} == {"TMFK6"}
    assert [r["symbol"] for r in recovery.values()] == ["TMFJ6"]


def test_the_flattener_sees_a_lot_carried_across_a_restart():
    store = PositionStore()
    store.load_recovery("F1", "TMFJ6", 1, PX, strategy_id="R47")
    flattener = PositionFlattener(position_store=store, order_adapter=MagicMock())

    assert flattener._get_open_positions() == {"TMFJ6": 1}


def test_startup_verify_counts_a_parked_recovery_lot_as_local():
    store = PositionStore()
    store.load_recovery("F1", "TMFJ6", 1, PX, strategy_id="R47")
    verifier = StartupPositionVerifier(MagicMock(), store, checkpoint_path="")

    assert verifier._build_local_map() == {"TMFJ6": 1}


# ---------------------------------------------------------------------------
# Checkpoint identity
# ---------------------------------------------------------------------------

PAPER = checkpoint_identity(broker_id="shioaji", account_id="ACC", order_mode="sim")
LIVE = checkpoint_identity(broker_id="shioaji", account_id="ACC", order_mode="live")


def test_checkpoint_identity_has_no_account_number_and_buckets_the_venue():
    assert "ACC" not in json.dumps(PAPER)
    assert PAPER["venue"] == "paper"
    assert checkpoint_identity(broker_id="shioaji", account_id="ACC", order_mode="disabled") == PAPER
    assert checkpoint_identity(broker_id="SHIOAJI", account_id="ACC", order_mode="real") == LIVE
    assert checkpoint_identity_mismatch({"identity": PAPER}, LIVE) == ["venue"]
    assert checkpoint_identity_mismatch({}, LIVE) == []
    assert checkpoint_identity_mismatch({"identity": PAPER}, None) == []


def _write_checkpoint(path: str, identity: dict[str, str] | None) -> None:
    store = PositionStore()
    store.on_fill(_fill())
    store.restore_portfolio_aggregates(peak_equity_scaled=200_000_000, total_realized_pnl_scaled=100_000_000)
    writer = PositionCheckpointWriter(store, path=path, trading_date_provider=lambda: "20261023")
    writer.identity = identity
    writer.write_checkpoint()


def _empty_broker() -> MagicMock:
    client = MagicMock()
    client.get_positions = MagicMock(return_value=[])
    client.last_positions_error = None
    return client


def test_the_writer_stamps_the_identity_into_the_hashed_body(tmp_path):
    path = str(tmp_path / "ckpt.json")
    _write_checkpoint(path, PAPER)

    data = PositionCheckpointWriter.load_checkpoint(path)

    assert data is not None  # sha256 still verifies with the identity inside
    assert data["identity"] == PAPER


@pytest.mark.asyncio
async def test_recovery_refuses_a_checkpoint_from_the_other_venue(tmp_path):
    path = str(tmp_path / "ckpt.json")
    _write_checkpoint(path, PAPER)
    store = PositionStore()
    verifier = StartupPositionVerifier(_empty_broker(), store, checkpoint_path=path, order_mode="live")
    verifier.identity = LIVE

    result = await verifier.recover(trading_date="20261023", account_id="ACC")

    assert result.source != "dual" and result.source != "checkpoint"
    assert store.net_qty_for_symbol("TMFK6") == 0
    assert store._peak_equity_scaled == 0 and store.total_pnl == 0
    assert not os.path.exists(path)
    assert len(glob.glob(path + ".foreign.*")) == 1


@pytest.mark.asyncio
async def test_recovery_restores_a_legacy_checkpoint_without_identity(tmp_path):
    path = str(tmp_path / "ckpt.json")
    _write_checkpoint(path, None)
    store = PositionStore()
    verifier = StartupPositionVerifier(_empty_broker(), store, checkpoint_path=path, order_mode="sim")
    verifier.identity = PAPER

    await verifier.recover(trading_date="20261023", account_id="ACC")

    assert store._peak_equity_scaled == 200_000_000
    assert os.path.exists(path)


@pytest.mark.asyncio
async def test_recovery_restores_a_checkpoint_with_the_same_identity(tmp_path):
    path = str(tmp_path / "ckpt.json")
    _write_checkpoint(path, PAPER)
    store = PositionStore()
    verifier = StartupPositionVerifier(_empty_broker(), store, checkpoint_path=path, order_mode="sim")
    verifier.identity = PAPER

    await verifier.recover(trading_date="20261023", account_id="ACC")

    assert store._peak_equity_scaled == 200_000_000


# ---------------------------------------------------------------------------
# Router: apply, then mark seen; fees reach the daily-loss feed
# ---------------------------------------------------------------------------


def _router(store: Any, normalizer: Any, risk_engine: Any = None) -> ExecutionRouter:
    bus = MagicMock()
    bus.publish_many_nowait = MagicMock()
    router = ExecutionRouter(bus, asyncio.Queue(), {}, store, MagicMock(), risk_engine=risk_engine)
    router.normalizer = normalizer
    return router


async def _drain(router: ExecutionRouter, n: int) -> None:
    for _ in range(n):
        await router.raw_queue.put(RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns()))
    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_an_orphaned_fill_resolved_from_the_dlq_reaches_the_position(tmp_path, monkeypatch):
    dlq = OrphanedFillDLQ(persist_path=str(tmp_path / "dlq.jsonl"))
    monkeypatch.setattr("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq", lambda: dlq)
    store = PositionStore()
    normalizer = MagicMock()
    normalizer.normalize_fill = MagicMock(return_value=_fill(strategy_id="UNKNOWN"))
    normalizer._resolve_strategy_id = MagicMock(return_value="UNKNOWN")
    router = _router(store, normalizer)

    await _drain(router, 1)
    assert dlq.count == 1 and store.net_qty_for_symbol("TMFK6") == 0

    normalizer._resolve_strategy_id.return_value = "R47"
    await router._retry_orphaned_fills()

    assert store.net_qty_for_symbol("TMFK6", "R47") == 1


@pytest.mark.asyncio
async def test_a_fill_whose_apply_raised_is_applied_when_replayed():
    store = PositionStore()
    real_apply = store.on_fill_async
    failures = [RuntimeError("tracker")]

    async def _apply(fill: FillEvent):
        if failures:
            raise failures.pop()
        return await real_apply(fill)

    store.on_fill_async = _apply  # type: ignore[method-assign]  # first apply fails, the replay succeeds
    normalizer = MagicMock()
    normalizer.normalize_fill = MagicMock(return_value=_fill())
    router = _router(store, normalizer)

    await _drain(router, 2)  # the broker replays the same deal

    assert store.net_qty_for_symbol("TMFK6", "R47") == 1


@pytest.mark.asyncio
async def test_the_daily_loss_feed_is_charged_the_fee_of_an_opening_fill():
    store = PositionStore()
    risk = MagicMock()
    normalizer = MagicMock()
    normalizer.normalize_fill = MagicMock(return_value=_fill(fee=18 * SCALE))
    router = _router(store, normalizer, risk_engine=risk)

    await _drain(router, 1)

    risk.notify_fill_pnl.assert_called_once_with("R47", -18 * SCALE)


@pytest.mark.asyncio
async def test_the_daily_loss_feed_nets_the_fee_off_a_closing_fill():
    store = PositionStore()
    store.on_fill(_fill(fill_id="D0"))
    risk = MagicMock()
    normalizer = MagicMock()
    closing = dataclasses.replace(_fill(side=Side.SELL, fee=18 * SCALE, fill_id="D9"), price=PX + 5 * SCALE)
    normalizer.normalize_fill = MagicMock(return_value=closing)
    router = _router(store, normalizer, risk_engine=risk)

    await _drain(router, 1)

    # +5 points x 10 NTD/point on TMF = 50 NTD gross, minus an 18 NTD fee.
    multiplier = store.metadata.contract_multiplier("TMFK6")
    risk.notify_fill_pnl.assert_called_once_with("R47", 5 * SCALE * multiplier - 18 * SCALE)
