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

    A SIGN mismatch, because it is unconditionally critical and -- unlike a zero
    broker quantity -- does not take the broker-zero debounce, which returns
    before critical discrepancies are ever computed. The first version of this
    test used broker=0 and carried a fallback branch, so it never reached the
    critical path and would have passed even if that path cleared the latch
    again: the exact regression it claims to prevent.
    """
    svc = _service(local={"TXFA5": 3}, broker=[{"code": "TXFA5", "quantity": -3}])
    svc._drift_reduce_only = True

    await svc.sync_portfolio()

    assert svc._critical_drift_streak == 1, "premise: the critical branch was reached"
    assert svc.drift_reduce_only_active is True, "escalation to critical cleared the safety signal"


@pytest.mark.asyncio
async def test_a_clean_sample_that_straddled_a_fill_does_not_clear_the_drift():
    """The broker half is read before the local half, so a fill splits the sample.

    A fill between the two reads can make the OLDER broker quantity equal the
    NEWER local quantity -- a clean reading of a book that is not clean. Only
    de-asserting matters here: a false clean that clears the signal lets
    cooldown recovery or a manual re-arm reopen order flow with drift unresolved.
    """
    from unittest.mock import MagicMock, patch

    from hft_platform.execution.reconciliation import ReconciliationService

    client = MagicMock()
    store = MagicMock()
    snapshot = {"TXFA5": SimpleNamespace(symbol="TXFA5", net_qty=3, strategy_id="R47_MAKER_TMF")}
    store.snapshot_positions = MagicMock(return_value=snapshot)
    store.positions = snapshot

    # The fence advances between the broker read and the local read -- exactly
    # the window the two-phase sample opens.
    gens = iter([7, 8])
    type(store).fill_generation = property(lambda _self: next(gens))

    # Broker agrees with the local book, so the cycle looks perfectly clean.
    client.get_positions = MagicMock(return_value=[{"code": "TXFA5", "quantity": 3}])

    with patch("hft_platform.execution.reconciliation.MetricsRegistry") as m:
        m.get.return_value = MagicMock()
        svc = ReconciliationService(client, store, {}, MagicMock())
    svc._drift_reduce_only = True

    await svc.sync_portfolio()

    assert svc.drift_reduce_only_active is True, "a clean sample straddling a fill cleared the drift signal"

    del type(store).fill_generation


@pytest.mark.asyncio
async def test_a_clean_sample_with_no_intervening_fill_does_clear_the_drift():
    """The fence must not make the latch permanent -- the control for the test above."""
    svc = _service(local={"TXFA5": 3}, broker=[{"code": "TXFA5", "quantity": 3}])
    svc._drift_reduce_only = True

    await svc.sync_portfolio()

    assert svc.drift_reduce_only_active is False


@pytest.mark.asyncio
async def test_drift_deasserts_only_when_reconciliation_finds_no_discrepancy():
    """The one genuine resolution path, so the latch cannot become permanent."""
    svc = _service(local={"TXFA5": 3}, broker=[{"code": "TXFA5", "quantity": 3}])
    svc._drift_reduce_only = True

    await svc.sync_portfolio()

    assert svc.drift_reduce_only_active is False


@pytest.mark.asyncio
async def test_a_fresh_critical_discrepancy_asserts_reduce_only_before_the_halt_debounce():
    """Critical drift is the WORSE condition and had the WEAKER response.

    Noncritical drift asserted reduce-only at streak 2. Critical drift asserted
    nothing at all: it incremented a streak and waited out the full HALT
    debounce -- three observations at the 5 s interval, ~10 s in which
    risk-opening orders were still permitted with the local and broker books
    disagreeing about the SIGN of a position.

    Starts with the flag false on purpose. The earlier escalation test pre-set
    it to true, which is what let this gap hide underneath a passing suite.
    """
    from unittest.mock import AsyncMock, MagicMock

    svc = _service(local={"TXFA5": 3}, broker=[{"code": "TXFA5", "quantity": -3}])
    controller = MagicMock()
    controller.enter_reduce_only_async = AsyncMock()
    svc.platform_degrade_controller = controller

    assert svc.drift_reduce_only_active is False, "premise: nothing asserted yet"

    await svc.sync_portfolio()

    assert svc._critical_drift_streak == 1, "premise: the critical branch was reached"
    assert svc._halt_triggered is False, "premise: still inside the HALT debounce"
    assert svc.drift_reduce_only_active is True, "critical drift left order flow open during debounce"
    controller.enter_reduce_only_async.assert_awaited_once_with(reason="reconciliation_drift")


@pytest.mark.asyncio
async def test_a_straddled_clean_sample_does_not_release_the_reconciliation_hold():
    """The fence guarded one side effect and left the rest of the resolution.

    `_drift_reduce_only` was held on a distrusted sample, but the same sample
    still zeroed the critical streak, cleared `_halt_triggered`, and released
    StormGuard's reconciliation hold -- so StormGuard could de-escalate out of
    HALT after its cooldown while the critical drift was still unresolved, and
    reconciliation had to debounce all the way back up to re-trigger it.
    """
    from unittest.mock import MagicMock, patch

    from hft_platform.execution.reconciliation import ReconciliationService

    client = MagicMock()
    store = MagicMock()
    snapshot = {"TXFA5": SimpleNamespace(symbol="TXFA5", net_qty=3, strategy_id="R47_MAKER_TMF")}
    store.snapshot_positions = MagicMock(return_value=snapshot)
    store.positions = snapshot

    gens = iter([7, 8])
    type(store).fill_generation = property(lambda _self: next(gens))
    client.get_positions = MagicMock(return_value=[{"code": "TXFA5", "quantity": 3}])

    storm_guard = MagicMock()
    with patch("hft_platform.execution.reconciliation.MetricsRegistry") as m:
        m.get.return_value = MagicMock()
        svc = ReconciliationService(client, store, {}, storm_guard)

    # Already in HALT from unresolved critical drift.
    svc._drift_reduce_only = True
    svc._halt_triggered = True
    svc._critical_drift_streak = 3

    await svc.sync_portfolio()

    assert svc.drift_reduce_only_active is True
    assert svc._halt_triggered is True, "a distrusted sample cleared the HALT latch"
    assert svc._critical_drift_streak == 3, "a distrusted sample zeroed the critical streak"
    assert storm_guard.set_reconciliation_hold.call_count == 0, "a distrusted sample released the HALT hold"

    del type(store).fill_generation


# --- A corrupt sample is not evidence in EITHER direction ---------------------
#
# The first fence only guarded the no-discrepancy branch, on the reasoning that
# asserting reduce-only is always the safe direction. That was too glib. For
# futures `is_critical` is `abs(diff) >= 1`, so a straddled fill that invents a
# single lot of difference is CRITICAL, and one transient sample was enough to
# enter reduce-only and start the recovery cooldown. You do not act on a
# measurement you already know is corrupt -- in either direction.


def _straddling_store(local_qty: int):
    """A store whose fill fence advances between the two halves of the sample."""
    from unittest.mock import MagicMock

    store = MagicMock()
    snapshot = {"TXFA5": SimpleNamespace(symbol="TXFA5", net_qty=local_qty, strategy_id="R47_MAKER_TMF")}
    store.snapshot_positions = MagicMock(return_value=snapshot)
    store.positions = snapshot
    gens = iter([7, 8])
    type(store).fill_generation = property(lambda _self: next(gens))
    return store


@pytest.mark.asyncio
async def test_a_straddled_sample_that_disagrees_does_not_assert_reduce_only():
    """A fill between the two reads can INVENT a discrepancy, not just hide one."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from hft_platform.execution.reconciliation import ReconciliationService

    store = _straddling_store(local_qty=2)
    client = MagicMock()
    client.get_positions = MagicMock(return_value=[{"code": "TXFA5", "quantity": 1}])
    with patch("hft_platform.execution.reconciliation.MetricsRegistry") as m:
        m.get.return_value = MagicMock()
        svc = ReconciliationService(client, store, {}, MagicMock())
    controller = MagicMock()
    controller.enter_reduce_only_async = AsyncMock()
    svc.platform_degrade_controller = controller

    await svc.sync_portfolio()

    assert svc._critical_drift_streak == 0, "a straddled sample was counted as critical drift"
    assert svc.drift_reduce_only_active is False, "a straddled sample asserted reduce-only"
    controller.enter_reduce_only_async.assert_not_awaited()
    assert svc._untrusted_sample_streak == 1

    del type(store).fill_generation


@pytest.mark.asyncio
async def test_a_partial_broker_snapshot_is_not_treated_as_the_whole_book():
    """AccountGateway.get_positions() has THREE outcomes, and recon knew two.

    Both accounts failing returns None. ONE account failing returns the other
    account's positions and records `last_positions_error`. Comparing that
    half-book against the full local book reads every position of the missing
    account as broker=0 -- fabricated critical drift on real positions, on a
    path whose response to critical drift is now immediate reduce-only.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from hft_platform.execution.reconciliation import ReconciliationService

    store = MagicMock()
    snapshot = {"TXFA5": SimpleNamespace(symbol="TXFA5", net_qty=3, strategy_id="R47_MAKER_TMF")}
    store.snapshot_positions = MagicMock(return_value=snapshot)
    store.positions = snapshot
    store.fill_generation = 7

    client = MagicMock()
    # The futures query failed; the stock account answered. Non-None, partial.
    client.get_positions = MagicMock(return_value=[{"code": "2330", "quantity": 1000}])
    client.account_gateway.last_positions_error = "futopt: 500 Please check param."

    with patch("hft_platform.execution.reconciliation.MetricsRegistry") as m:
        m.get.return_value = MagicMock()
        svc = ReconciliationService(client, store, {}, MagicMock())
    controller = MagicMock()
    controller.enter_reduce_only_async = AsyncMock()
    svc.platform_degrade_controller = controller

    await svc.sync_portfolio()

    assert svc._critical_drift_streak == 0, "a partial broker snapshot fabricated critical drift"
    assert svc.drift_reduce_only_active is False
    controller.enter_reduce_only_async.assert_not_awaited()
    assert svc._untrusted_sample_streak == 1


@pytest.mark.asyncio
async def test_a_partial_broker_snapshot_cannot_clear_an_existing_drift_signal():
    """The other direction: an omitted broker position must not read as resolved."""
    from unittest.mock import MagicMock, patch

    from hft_platform.execution.reconciliation import ReconciliationService

    store = MagicMock()
    store.snapshot_positions = MagicMock(return_value={})
    store.positions = {}
    store.fill_generation = 7

    client = MagicMock()
    client.get_positions = MagicMock(return_value=[])
    client.account_gateway.last_positions_error = "futopt: 500 Please check param."

    storm_guard = MagicMock()
    with patch("hft_platform.execution.reconciliation.MetricsRegistry") as m:
        m.get.return_value = MagicMock()
        svc = ReconciliationService(client, store, {}, storm_guard)
    svc._drift_reduce_only = True
    svc._halt_triggered = True
    svc._critical_drift_streak = 3

    await svc.sync_portfolio()

    assert svc.drift_reduce_only_active is True, "a partial snapshot cleared the drift signal"
    assert svc._halt_triggered is True
    assert storm_guard.set_reconciliation_hold.call_count == 0


@pytest.mark.asyncio
async def test_consecutive_untrusted_samples_are_visible_rather_than_silent():
    """Skipping is right per-sample and dangerous in aggregate.

    A persistently bad source would stop reconciling anything and look calm
    doing it. The streak is the only thing standing between that and a silent
    blind spot, so it must count up and reset -- not merely be set once.
    """
    from unittest.mock import MagicMock, patch

    from hft_platform.execution.reconciliation import ReconciliationService

    store = MagicMock()
    store.snapshot_positions = MagicMock(return_value={})
    store.positions = {}
    store.fill_generation = 7

    client = MagicMock()
    client.get_positions = MagicMock(return_value=[])
    client.account_gateway.last_positions_error = "futopt: 500 Please check param."

    with patch("hft_platform.execution.reconciliation.MetricsRegistry") as m:
        m.get.return_value = MagicMock()
        svc = ReconciliationService(client, store, {}, MagicMock())

    for expected in (1, 2, 3):
        await svc.sync_portfolio()
        assert svc._untrusted_sample_streak == expected

    # A trustworthy sample must reset it, or the alert would latch forever.
    client.account_gateway.last_positions_error = None
    await svc.sync_portfolio()
    assert svc._untrusted_sample_streak == 0


# --- Sustained unverifiability must fail CLOSED, not merely log ---------------
#
# Skipping a corrupt sample is right per-sample and dangerous sustained: nothing
# was watching. ReconciliationDiscrepancyDetected cannot fire, because a skipped
# cycle never computes a discrepancy; reconciliation_last_success_ts had no
# alert rule at all. So a broker account outage meant positions went unverified
# indefinitely with trading fully open, and the only witness was a log line.


def _always_partial_service(threshold: int | None = None):
    from unittest.mock import AsyncMock, MagicMock, patch

    from hft_platform.execution.reconciliation import ReconciliationService

    store = MagicMock()
    store.snapshot_positions = MagicMock(return_value={})
    store.positions = {}
    store.fill_generation = 7

    client = MagicMock()
    client.get_positions = MagicMock(return_value=[])
    client.account_gateway.last_positions_error = "futopt: 500 Please check param."

    cfg = {"reconciliation": {"untrusted_sample_degrade_after": threshold}} if threshold else {}
    with patch("hft_platform.execution.reconciliation.MetricsRegistry") as m:
        m.get.return_value = MagicMock()
        svc = ReconciliationService(client, store, cfg, MagicMock())
    controller = MagicMock()
    controller.enter_reduce_only_async = AsyncMock()
    svc.platform_degrade_controller = controller
    return svc, client, controller


@pytest.mark.asyncio
async def test_sustained_untrustworthy_samples_enter_reduce_only():
    svc, _client, controller = _always_partial_service()

    for _ in range(svc.untrusted_sample_degrade_after - 1):
        await svc.sync_portfolio()
    assert svc.reconciliation_unverifiable_active is False, "escalated before the threshold"
    controller.enter_reduce_only_async.assert_not_awaited()

    await svc.sync_portfolio()

    assert svc.reconciliation_unverifiable_active is True, "positions unverifiable and trading stayed open"
    controller.enter_reduce_only_async.assert_awaited_once_with(reason="reconciliation_unverifiable")


@pytest.mark.asyncio
async def test_the_unverifiable_latch_is_not_re_entered_every_cycle():
    """It is a latch, not a per-cycle write: each entry persists off the loop."""
    svc, _client, controller = _always_partial_service(threshold=2)

    for _ in range(5):
        await svc.sync_portfolio()

    assert svc.reconciliation_unverifiable_active is True
    assert controller.enter_reduce_only_async.await_count == 1


@pytest.mark.asyncio
async def test_a_trustworthy_sample_resolves_the_unverifiable_state():
    """Otherwise the reason latches forever and only a restart clears it."""
    svc, client, _controller = _always_partial_service(threshold=2)

    await svc.sync_portfolio()
    await svc.sync_portfolio()
    assert svc.reconciliation_unverifiable_active is True

    client.account_gateway.last_positions_error = None
    await svc.sync_portfolio()

    assert svc.reconciliation_unverifiable_active is False
    assert svc._untrusted_sample_streak == 0


def test_the_unverifiable_reason_is_reported_and_auto_recoverable():
    """Both halves of the level-triggered contract, or it repeats a known bug.

    Reported by an input, so `check_auto_recovery` does not read "no input
    mentions it" as "it cleared"; and auto-recoverable, so it does not latch
    past the outage and force a manual re-arm.
    """
    from hft_platform.ops.platform_degrade import _AUTO_RECOVERABLE_REASONS

    _c, inputs, recon = _rig(drift=False)
    recon.reconciliation_unverifiable_active = True
    assert "reconciliation_unverifiable" in inputs.reduce_only_reasons()

    recon.reconciliation_unverifiable_active = False
    assert "reconciliation_unverifiable" not in inputs.reduce_only_reasons()

    assert "reconciliation_unverifiable" in _AUTO_RECOVERABLE_REASONS


@pytest.mark.asyncio
async def test_an_untrusted_cycle_still_refreshes_the_reduce_only_reference_map():
    """Skipping the DECISIONS must not freeze the exposure estimate.

    `reference_available_net_qty` is what reduce-only enforcement asks to decide
    whether an order actually reduces a position (order/adapter.py). A cycle
    that returned before `update_reference_positions` froze that map at the last
    trusted sample, so during exactly the outage that triggers reduce-only, the
    enforcement was reading arbitrarily stale exposure and could permit an order
    that does not reduce. The map already degrades to the platform's own fresh
    book when the broker reports nothing for a symbol.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from hft_platform.execution.reconciliation import ReconciliationService

    store = MagicMock()
    snapshot = {"TXFA5": SimpleNamespace(symbol="TXFA5", net_qty=5, strategy_id="R47_MAKER_TMF")}
    store.snapshot_positions = MagicMock(return_value=snapshot)
    store.positions = snapshot
    store.fill_generation = 7

    client = MagicMock()
    client.get_positions = MagicMock(return_value=[])
    client.account_gateway.last_positions_error = "futopt: 500 Please check param."

    with patch("hft_platform.execution.reconciliation.MetricsRegistry") as m:
        m.get.return_value = MagicMock()
        svc = ReconciliationService(client, store, {}, MagicMock())
    controller = MagicMock()
    controller.enter_reduce_only_async = AsyncMock()
    svc.platform_degrade_controller = controller

    await svc.sync_portfolio()

    controller.update_reference_positions.assert_called_once()
    kwargs = controller.update_reference_positions.call_args.kwargs
    assert kwargs["local_map"] == {"TXFA5": 5}, "reduce-only enforcement was left reading a frozen book"
    assert svc._untrusted_sample_streak == 1, "premise: this cycle was rejected"
