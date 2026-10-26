"""The reconciliation-mismatch latch ends once the books agree again (2026-10-24).

It was operator-only: a mismatch held HALT until someone ran ``hft risk rearm
reconciliation_mismatch``. Now ``HFT_RECON_LATCH_RELEASE_AFTER_CLEAN`` (2)
consecutive trusted syncs with no discrepancy release it, with an audit line.
The same evidence, ``positions_verified_mono_ns``, is what the kill-switch exit
requires before it sends a closing order.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.risk import manual_latches


def _service(local: dict[str, int], broker: list[dict], *, order_mode: str = "live") -> ReconciliationService:
    client = MagicMock()
    client.get_positions = MagicMock(return_value=broker)
    store = MagicMock()
    snapshot = {
        sym: SimpleNamespace(symbol=sym, net_qty=qty, strategy_id="R47_MAKER_TMF") for sym, qty in local.items()
    }
    store.snapshot_positions = MagicMock(return_value=snapshot)
    store.positions = snapshot
    with patch("hft_platform.execution.reconciliation.MetricsRegistry") as m:
        m.get.return_value = MagicMock()
        return ReconciliationService(client, store, {}, MagicMock(), order_mode=order_mode)


def _latch() -> None:
    manual_latches.activate(
        manual_latches.RECONCILIATION_MISMATCH, "RECONCILIATION_MISMATCH: 1 critical", actor="engine"
    )


AGREE = ({"TMFK6": 1}, [{"code": "TMFK6", "quantity": 1}])
DISAGREE = ({"TMFK6": 1}, [{"code": "TMFK6", "quantity": -1}])


@pytest.mark.asyncio
async def test_two_clean_syncs_release_the_latch_with_an_audit_line() -> None:
    _latch()
    svc = _service(*AGREE)

    await svc.sync_portfolio()
    assert "reconciliation_mismatch" in manual_latches.active(), "one clean sync is not enough"
    await svc.sync_portfolio()

    assert manual_latches.active() == {}
    entry = json.loads((manual_latches.latch_dir() / manual_latches.AUDIT_FILE).read_text().splitlines()[-1])
    assert entry["actor"] == "engine:reconciliation"
    assert entry["latch"] == "reconciliation_mismatch"


@pytest.mark.asyncio
async def test_a_discrepancy_between_clean_syncs_restarts_the_count() -> None:
    _latch()
    svc = _service(*AGREE)
    await svc.sync_portfolio()

    svc.client.get_positions.return_value = DISAGREE[1]
    await svc.sync_portfolio()
    svc.client.get_positions.return_value = AGREE[1]
    await svc.sync_portfolio()

    assert "reconciliation_mismatch" in manual_latches.active()


@pytest.mark.asyncio
async def test_a_paper_order_mode_position_is_not_proof_and_keeps_the_latch() -> None:
    """Under sim a local lot the broker reports as 0 is set aside, not confirmed."""
    _latch()
    svc = _service({"TMFJ6": 1}, [{"code": "TMFJ6", "quantity": 0}], order_mode="sim")
    svc.broker_zero_debounce_observations = 1

    for _ in range(3):
        await svc.sync_portfolio()

    assert "reconciliation_mismatch" in manual_latches.active()
    assert svc.positions_verified_mono_ns == 0


@pytest.mark.asyncio
async def test_verified_time_is_set_by_a_clean_sync_and_cleared_by_a_discrepancy() -> None:
    svc = _service(*AGREE)
    assert svc.positions_verified_mono_ns == 0

    await svc.sync_portfolio()
    assert svc.positions_verified_mono_ns > 0

    svc.client.get_positions.return_value = DISAGREE[1]
    await svc.sync_portfolio()
    assert svc.positions_verified_mono_ns == 0


@pytest.mark.asyncio
async def test_a_failed_broker_query_clears_the_verified_time() -> None:
    svc = _service(*AGREE)
    await svc.sync_portfolio()

    svc.client.get_positions.return_value = None
    with pytest.raises(RuntimeError):
        await svc.sync_portfolio()

    assert svc.positions_verified_mono_ns == 0
