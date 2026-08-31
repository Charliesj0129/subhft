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
