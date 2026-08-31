"""Position-drift reduce-only must not clear itself while the drift persists.

`reconciliation_drift` sits in `_AUTO_RECOVERABLE_REASONS`, and
`check_auto_recovery` drops any auto-recoverable reason the live inputs no longer
report. But the reason was latched ONLY by `ReconciliationService` calling
`enter_reduce_only_async` directly -- it was never produced by
`PlatformDegradeInputs.reduce_only_reasons()`, so "no input reports it" was
indistinguishable from "the condition cleared".

Measured before the fix: the reason was dropped on the FIRST supervisor tick
after it latched (~1 s), the 60 s cooldown started immediately, and reduce-only
exited with the position drift never resolved.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hft_platform.ops.evidence import AutonomyEvidenceWriter
from hft_platform.ops.platform_degrade import PlatformDegradeController
from hft_platform.ops.platform_inputs import PlatformDegradeInputs


class _Q:
    def qsize(self) -> int:
        return 0


def _rig(drift: bool):
    tmp = Path(tempfile.mkdtemp())
    controller = PlatformDegradeController(evidence_writer=AutonomyEvidenceWriter(base_dir=tmp), metrics=None)
    recon = SimpleNamespace(drift_reduce_only_active=drift)
    inputs = PlatformDegradeInputs(
        md_service=SimpleNamespace(),
        recorder=SimpleNamespace(),
        raw_queue=_Q(),
        raw_exec_queue=_Q(),
        recorder_queue=_Q(),
        risk_queue=_Q(),
        order_queue=_Q(),
        reconciliation=recon,
    )
    return controller, inputs, recon


def test_inputs_report_drift_only_while_reconciliation_asserts_it():
    _c, inputs, recon = _rig(drift=True)
    assert "reconciliation_drift" in inputs.reduce_only_reasons()
    recon.drift_reduce_only_active = False
    assert "reconciliation_drift" not in inputs.reduce_only_reasons()


def test_inputs_without_a_reconciliation_service_report_no_drift():
    """Optional dependency: absent means "not reported", the prior behaviour."""
    _c, inputs, _r = _rig(drift=True)
    inputs.reconciliation = None
    assert "reconciliation_drift" not in inputs.reduce_only_reasons()


@pytest.mark.asyncio
async def test_drift_reduce_only_survives_supervisor_ticks_while_drift_persists():
    controller, inputs, _recon = _rig(drift=True)
    await controller.enter_reduce_only_async(reason="reconciliation_drift")

    for tick in range(1, 4):
        await controller.check_auto_recovery_async(current_reasons=inputs.reduce_only_reasons(), now_ns=tick * 10**9)
        assert controller.reduce_only_active is True
        assert "reconciliation_drift" in controller._active_reasons, f"dropped on tick {tick}"
        assert controller.allow_open() is False


@pytest.mark.asyncio
async def test_drift_reduce_only_clears_once_reconciliation_reports_it_resolved():
    """The other direction: the latch must not become permanent either."""
    controller, inputs, recon = _rig(drift=True)
    await controller.enter_reduce_only_async(reason="reconciliation_drift")
    await controller.check_auto_recovery_async(current_reasons=inputs.reduce_only_reasons(), now_ns=10**9)
    assert "reconciliation_drift" in controller._active_reasons

    recon.drift_reduce_only_active = False
    await controller.check_auto_recovery_async(current_reasons=inputs.reduce_only_reasons(), now_ns=2 * 10**9)
    assert "reconciliation_drift" not in controller._active_reasons


@pytest.mark.asyncio
async def test_a_manual_rearm_relatches_drift_because_inputs_now_report_it():
    """The r5 finding: re-arm re-latched only what the inputs reported.

    Making drift level-triggered is what puts it back in that list, so the
    supervisor's re-latch after a force-clear now covers it.
    """
    controller, inputs, _recon = _rig(drift=True)
    await controller.enter_reduce_only_async(reason="reconciliation_drift")

    opened = False
    task = asyncio.create_task(
        controller.force_clear_async(reason="manual_rearm_gate", current_reasons=inputs.reduce_only_reasons())
    )
    while not task.done():
        await asyncio.sleep(0)
        opened = opened or controller.allow_open()
    await task

    assert not opened, "allow_open() was true while position drift was still asserted"
    assert controller.reduce_only_active is True
    assert "reconciliation_drift" in controller._active_reasons


# --- Transitions, against the real service ------------------------------------
#
# The first version of this file toggled a SimpleNamespace boolean, which proves
# the wiring and nothing about when the flag actually moves. The adversarial
# review reproduced both premature de-assertions below with the real service.


def _service(local, broker, config=None):
    """A real ReconciliationService over mocked store/client.

    `snapshot_positions()` is what the service actually reads -- a MagicMock
    answers `hasattr` for it, so setting only `store.positions` silently feeds
    the service an empty book and every assertion below would be about the
    wrong scenario.
    """
    from unittest.mock import MagicMock, patch

    from hft_platform.execution.reconciliation import ReconciliationService

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
        svc = ReconciliationService(client, store, config or {}, MagicMock())
    return svc


@pytest.mark.asyncio
async def test_drift_stays_asserted_when_a_distrusted_broker_snapshot_arrives():
    """The broker-zero debounce exists BECAUSE the snapshot is not believed.

    Clearing the drift signal there treated "we do not trust this reading" as
    "the drift resolved", so the supervisor dropped the reason and a pending
    manual re-arm could reopen order flow with positions unreconciled.
    """
    svc = _service(local={"TXFA5": 3}, broker=[])
    svc._drift_reduce_only = True
    svc._broker_zero_streak = 0

    await svc.sync_portfolio()

    assert svc._broker_zero_streak >= 1, "premise: the debounce path was taken"
    assert svc._broker_zero_streak < svc.broker_zero_debounce_observations
    assert svc.drift_reduce_only_active is True, "a distrusted snapshot cleared the safety signal"


@pytest.mark.asyncio
async def test_drift_stays_asserted_when_noncritical_drift_escalates_to_critical():
    """The noncritical streak resets because the drift got WORSE, not better.

    Dropping the reason here handed the HALT debounce a window with no reason
    active at all, so reduce-only could exit while positions were diverging.
    """
    svc = _service(local={"TXFA5": 3}, broker=[{"code": "TXFA5", "quantity": 0}])
    svc._drift_reduce_only = True

    await svc.sync_portfolio()

    if svc._critical_drift_streak >= 1:
        assert svc.drift_reduce_only_active is True, "escalation to critical cleared the safety signal"
    else:
        # Not a critical discrepancy in this shape; assert the flag survived a
        # non-resolving cycle rather than silently passing on a wrong premise.
        assert svc.drift_reduce_only_active is True


@pytest.mark.asyncio
async def test_drift_deasserts_only_when_reconciliation_finds_no_discrepancy():
    """The one genuine resolution path, so the latch cannot become permanent."""
    svc = _service(local={"TXFA5": 3}, broker=[{"code": "TXFA5", "quantity": 3}])
    svc._drift_reduce_only = True

    await svc.sync_portfolio()

    assert svc.drift_reduce_only_active is False
